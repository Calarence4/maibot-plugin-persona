"""持久化层：路径、磁盘闸门、当前卡、全量卡、checkpoint、操作日志与擦除屏障。

约定（《方案.md》§1.1、§3.7、§7、§10）：

- 所有隐私文件权限 ``0600``；
- 全量卡是追加式修订日志，``archiveKey = id:revision`` 幂等；
- 崩溃恢复按分片 offset 重放；
- 磁盘写入统一经过 reservation ledger；
- 文件损坏时隔离，不用空文件覆盖。
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .common import (
    FileLock,
    append_jsonl,
    atomic_write_json,
    directory_size,
    ensure_dir,
    iso_utc,
    iter_jsonl,
    read_bytes_tail,
    read_json,
    sha256_hex,
)
from .models import normalize_record
from .settings import Settings

__all__ = [
    "PluginPaths",
    "StorageFull",
    "StorageGuard",
    "CardStore",
    "FullLog",
    "CheckpointStore",
    "OperationLog",
    "ErasedBarrier",
    "WriterState",
    "StaleWriter",
]


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PluginPaths:
    """插件数据目录与运行时目录。"""

    root: Path
    runtime: Path

    @classmethod
    def from_dirs(cls, data_dir: Path | str, runtime_dir: Path | str) -> "PluginPaths":
        return cls(root=Path(data_dir), runtime=Path(runtime_dir))

    @property
    def memory_dir(self) -> Path:
        return self.root / "memory"

    @property
    def inbox_dir(self) -> Path:
        return self.memory_dir / "inbox"

    @property
    def backups_dir(self) -> Path:
        return self.memory_dir / "backups"

    @property
    def styles_dir(self) -> Path:
        return self.root / "styles"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def locks_dir(self) -> Path:
        return self.root / ".locks"

    @property
    def card_json(self) -> Path:
        return self.memory_dir / "card.json"

    @property
    def card_md(self) -> Path:
        return self.memory_dir / "card.md"

    @property
    def full_jsonl(self) -> Path:
        return self.memory_dir / "full.jsonl"

    @property
    def full_stats(self) -> Path:
        return self.memory_dir / "full-stats.json"

    @property
    def full_index(self) -> Path:
        return self.memory_dir / "full-index.json"

    @property
    def erased_json(self) -> Path:
        return self.memory_dir / "erased.json"

    @property
    def checkpoint_json(self) -> Path:
        return self.inbox_dir / "checkpoint.json"

    @property
    def dedupe_jsonl(self) -> Path:
        return self.inbox_dir / "dedupe.jsonl"

    @property
    def operations_jsonl(self) -> Path:
        return self.memory_dir / "operations.jsonl"

    @property
    def writer_state(self) -> Path:
        return self.state_dir / "writer-state.json"

    @property
    def session_map(self) -> Path:
        return self.state_dir / "session-map.json"

    @property
    def recognition_state(self) -> Path:
        return self.state_dir / "recognition.json"

    @property
    def style_state(self) -> Path:
        return self.state_dir / "style-state.json"

    @property
    def purpose_state(self) -> Path:
        return self.state_dir / "purpose.json"

    @property
    def exposure_state(self) -> Path:
        return self.state_dir / "candidate-exposure.json"

    @property
    def llm_ledger(self) -> Path:
        return self.state_dir / "llm-ledger.json"

    @property
    def reply_inbox(self) -> Path:
        return self.state_dir / "reply-inbox.jsonl"

    @property
    def prompts_dir(self) -> Path:
        return self.runtime / "prompts"

    @property
    def styles_index(self) -> Path:
        return self.runtime / "styles" / "index.md"

    @property
    def fts_dir(self) -> Path:
        return self.runtime / "fts"

    @property
    def writer_lock(self) -> Path:
        return self.locks_dir / "writer.lock"

    @property
    def operations_lock(self) -> Path:
        return self.locks_dir / "operations.lock"

    @property
    def storage_lock(self) -> Path:
        return self.locks_dir / "storage.lock"

    @property
    def tick_lock(self) -> Path:
        return self.locks_dir / "tick.lock"

    def ensure(self) -> None:
        """创建全部基础目录。"""

        for path in (
            self.memory_dir,
            self.inbox_dir,
            self.backups_dir,
            self.styles_dir,
            self.state_dir,
            self.locks_dir,
            self.prompts_dir,
            self.styles_index.parent,
        ):
            ensure_dir(path)


# ---------------------------------------------------------------------------
# 磁盘闸门
# ---------------------------------------------------------------------------


class StorageFull(RuntimeError):
    """磁盘预算不足。"""

    def __init__(self, used: int, incoming: int, budget: int, level: str, purpose: str) -> None:
        self.used = used
        self.incoming = incoming
        self.budget = budget
        self.level = level
        self.purpose = purpose
        super().__init__(f"磁盘预算不足({level})：used={used} incoming={incoming} budget={budget} purpose={purpose}")


class StorageGuard:
    """跨 Runner 的 reservation ledger 与水位判断。"""

    def __init__(self, paths: PluginPaths, settings: Settings) -> None:
        self.paths = paths
        self.settings = settings
        self._cached_used = 0
        self._cached_at = 0.0
        self._cache_ttl = 30.0

    def _ledger_path(self) -> Path:
        return self.paths.state_dir / "storage-ledger.json"

    def _load_ledger(self) -> dict[str, Any]:
        data = read_json(self._ledger_path(), {"reservations": {}})
        if not isinstance(data, dict) or not isinstance(data.get("reservations"), dict):
            return {"reservations": {}}
        return data

    def _save_ledger(self, data: dict[str, Any]) -> None:
        atomic_write_json(self._ledger_path(), data)

    def used_bytes(self, *, force: bool = False) -> int:
        """统计 data_dir + runtime_dir 的占用（缓存 30 秒）。"""

        now = time.monotonic()
        if not force and self._cached_at and now - self._cached_at < self._cache_ttl:
            return self._cached_used
        used = directory_size(self.paths.root) + directory_size(self.paths.runtime)
        self._cached_used = used
        self._cached_at = now
        return used

    def _cleanup_locked(self, data: dict[str, Any]) -> bool:
        now = time.time()
        reservations = data.get("reservations", {})
        expired = [
            key
            for key, value in reservations.items()
            if not isinstance(value, dict) or float(value.get("expiresAt") or 0) <= now
        ]
        for key in expired:
            reservations.pop(key, None)
        return bool(expired)

    def reserve(
        self,
        incoming_bytes: int,
        purpose: str,
        *,
        required_temp: int = 0,
        essential: bool = False,
    ) -> str:
        """登记一次写入预留；超阈值抛 :class:`StorageFull`。"""

        incoming = max(0, int(incoming_bytes))
        temp = max(0, int(required_temp))
        budget = max(1, int(self.settings.storage.budget_bytes))
        stop_ratio = float(self.settings.storage.stop_ingest_ratio)
        reservation_id = f"res_{uuid.uuid4().hex[:16]}"
        with FileLock(self.paths.storage_lock) as lock:
            del lock
            data = self._load_ledger()
            self._cleanup_locked(data)
            # 用 30 秒缓存，避免每条入站消息都递归遍历数据目录；tick 会定期强制刷新。
            used = self.used_bytes()
            reserved = sum(int(item.get("bytes") or 0) + int(item.get("requiredTemp") or 0) for item in data["reservations"].values() if isinstance(item, dict))
            projected = used + reserved + incoming + temp
            limit = budget if essential else int(budget * stop_ratio)
            if projected > limit:
                self._save_ledger(data)
                level = "stop" if not essential else "hard"
                raise StorageFull(used, incoming + temp, budget, level, purpose)
            data["reservations"][reservation_id] = {
                "bytes": incoming,
                "requiredTemp": temp,
                "purpose": purpose,
                "createdAt": iso_utc(),
                "expiresAt": time.time() + max(60, int(self.settings.storage.reservation_ttl_seconds)),
            }
            self._save_ledger(data)
        return reservation_id

    def release(self, reservation_id: str) -> None:
        """释放预留；预留不存在视为成功。"""

        with FileLock(self.paths.storage_lock):
            data = self._load_ledger()
            if data["reservations"].pop(reservation_id, None) is not None:
                self._save_ledger(data)
        self._cached_at = 0.0

    def status(self, *, force: bool = False) -> dict[str, Any]:
        """返回水位快照。"""

        used = self.used_bytes(force=force)
        budget = max(1, int(self.settings.storage.budget_bytes))
        with FileLock(self.paths.storage_lock):
            data = self._load_ledger()
            self._cleanup_locked(data)
            reserved = sum(int(item.get("bytes") or 0) + int(item.get("requiredTemp") or 0) for item in data["reservations"].values() if isinstance(item, dict))
        ratio = (used + reserved) / budget
        warn = ratio >= float(self.settings.storage.warn_ratio)
        stop = ratio >= float(self.settings.storage.stop_ingest_ratio)
        return {
            "usedBytes": used,
            "reservedBytes": reserved,
            "budgetBytes": budget,
            "ratio": round(ratio, 4),
            "level": "stop" if stop else ("warn" if warn else "ok"),
            "ingestAllowed": not stop,
        }

    def can_ingest(self) -> bool:
        """95% 闸门：停止新摄取与新增修订。"""

        return bool(self.status()["ingestAllowed"])


# ---------------------------------------------------------------------------
# 当前卡
# ---------------------------------------------------------------------------


def default_card() -> dict[str, Any]:
    """空卡骨架。"""

    return {
        "schemaVersion": 1,
        "cardRevision": 0,
        "privacyRevision": 0,
        "records": {},
        "stats": {
            "created": 0,
            "forgotten": 0,
            "deleted": 0,
            "compacted": 0,
            "lastMaintainAt": None,
        },
        "firstOwnerAt": None,
        "lastDailyMaintainDate": None,
        "updatedAt": iso_utc(),
    }


class CardStore:
    """当前衰减卡的原子读写。"""

    def __init__(self, paths: PluginPaths, settings: Settings) -> None:
        self.paths = paths
        self.settings = settings
        self.recovered = False
        self.recovery_note = ""

    def load(self) -> dict[str, Any]:
        """读取当前卡；损坏时隔离并尝试从全量卡重建。"""

        raw = read_json(self.paths.card_json, None)
        if isinstance(raw, dict) and isinstance(raw.get("records"), dict):
            card = default_card()
            card.update(raw)
            card["records"] = {key: normalize_record(value) for key, value in raw["records"].items() if isinstance(value, dict)}
            return card
        if not self.paths.card_json.exists() and self.paths.full_jsonl.exists() and self.paths.full_jsonl.stat().st_size > 0:
            rebuilt = self.rebuild_from_full()
            if rebuilt is not None:
                self.recovered = True
                self.recovery_note = "card.json 缺失，已从 full.jsonl 重建"
                return rebuilt
        if self.paths.card_json.exists():
            self.recovered = True
            stamp = time.strftime("%Y%m%d-%H%M%S")
            quarantine = self.paths.card_json.with_name(f"card.corrupt-{stamp}.json")
            try:
                shutil.move(str(self.paths.card_json), str(quarantine))
            except OSError:
                pass
            self.recovery_note = f"card.json 损坏，已隔离为 {quarantine.name}，尝试从 full.jsonl 重建"
            rebuilt = self.rebuild_from_full()
            if rebuilt is not None:
                return rebuilt
        return default_card()

    def rebuild_from_full(self) -> dict[str, Any] | None:
        """从追加式全量日志重建最新可见修订。"""

        if not self.paths.full_jsonl.exists():
            return None
        latest: dict[str, dict[str, Any]] = {}
        deleted: set[str] = set()
        for _offset, payload in iter_jsonl(self.paths.full_jsonl):
            if not isinstance(payload, dict):
                continue
            record_id = str(payload.get("id") or "")
            if not record_id:
                continue
            if payload.get("op") == "delete":
                deleted.add(record_id)
                latest.pop(record_id, None)
                continue
            record = payload.get("record")
            if isinstance(record, dict):
                latest[record_id] = normalize_record(record)
                deleted.discard(record_id)
        if not latest:
            return None
        card = default_card()
        card["records"] = latest
        card["stats"]["created"] = len(latest)
        card["stats"]["deleted"] = len(deleted)
        card["updatedAt"] = iso_utc()
        self.recovery_note += f"（从 full 恢复 {len(latest)} 条）"
        return card

    def save(self, card: dict[str, Any]) -> None:
        """原子写 card.json，并重渲染人看的 card.md。"""

        from .render import render_card_markdown

        card["updatedAt"] = iso_utc()
        atomic_write_json(self.paths.card_json, card)
        text = render_card_markdown(card, self.settings)
        atomic_write_text_safe(self.paths.card_md, text)


def atomic_write_text_safe(path: Path, text: str) -> None:
    """包装 ``atomic_write_text``，避免循环导入。"""

    from .common import atomic_write_text

    atomic_write_text(path, text)


# ---------------------------------------------------------------------------
# 全量卡
# ---------------------------------------------------------------------------


class FullLog:
    """追加式修订日志与统计/索引缓存。"""

    def __init__(self, paths: PluginPaths, storage: StorageGuard) -> None:
        self.paths = paths
        self.storage = storage
        self._keys: set[str] = set()
        self._bytes = 0
        self._lines = 0
        self._upserts = 0
        self._deletes = 0
        self._loaded = False
        self._dirty = False

    @property
    def path(self) -> Path:
        return self.paths.full_jsonl

    def ensure(self) -> None:
        ensure_dir(self.path.parent)
        if not self.path.exists():
            fd = os.open(str(self.path), os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)

    def load_index(self, *, force: bool = False) -> None:
        """加载索引；文件大小不符时流式重建。"""

        if self._loaded and not force:
            return
        self.ensure()
        size = self.path.stat().st_size
        index = read_json(self.paths.full_index, None)
        if isinstance(index, dict) and int(index.get("bytes") or -1) == size:
            self._keys = set(str(item) for item in index.get("keys") or [])
            self._bytes = size
            self._lines = int(index.get("lines") or 0)
            self._upserts = int(index.get("upserts") or 0)
            self._deletes = int(index.get("deletes") or 0)
            self._loaded = True
            return
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        started = time.monotonic()
        keys: set[str] = set()
        lines = upserts = deletes = 0
        for _offset, payload in iter_jsonl(self.path):
            if not isinstance(payload, dict):
                continue
            lines += 1
            archive_key = str(payload.get("archiveKey") or "")
            if archive_key:
                keys.add(archive_key)
            if payload.get("op") == "delete":
                deletes += 1
            else:
                upserts += 1
        self._keys = keys
        self._bytes = self.path.stat().st_size if self.path.exists() else 0
        self._lines = lines
        self._upserts = upserts
        self._deletes = deletes
        self._loaded = True
        self._dirty = True
        elapsed = time.monotonic() - started
        if elapsed > 1.0:
            import logging

            logging.getLogger("plugin.com.calarence.persona").info("full 索引重建完成：%d 行，耗时 %.2fs", lines, elapsed)

    def has(self, archive_key: str) -> bool:
        """该修订是否已经归档。"""

        self.load_index()
        return archive_key in self._keys

    def append_upsert(self, record: dict[str, Any]) -> bool:
        """追加一条 upsert；``archiveKey`` 命中时返回 ``False``。"""

        self.load_index()
        archive_key = f"{record.get('id')}:{int(record.get('revision') or 0)}"
        if archive_key in self._keys:
            return False
        payload = {
            "op": "upsert",
            "archiveKey": archive_key,
            "id": record.get("id"),
            "revision": int(record.get("revision") or 0),
            "at": iso_utc(),
            "record": record,
        }
        reservation = self.storage.reserve(
            len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 2,
            "full.upsert",
            essential=False,
        )
        try:
            written = append_jsonl(self.path, payload)
        finally:
            self.storage.release(reservation)
        self._bytes += written
        self._lines += 1
        self._upserts += 1
        self._keys.add(archive_key)
        self._dirty = True
        return True

    def append_delete(self, record_id: str, revision: int, erasure_id: str, *, reason: str = "") -> bool:
        """追加一条 delete；同一 archiveKey 不重复。"""

        self.load_index()
        archive_key = f"{record_id}:{int(revision)}"
        if archive_key in self._keys:
            return False
        payload = {
            "op": "delete",
            "archiveKey": archive_key,
            "id": record_id,
            "revision": int(revision),
            "erasureId": erasure_id,
            "reason": reason,
            "at": iso_utc(),
        }
        reservation = self.storage.reserve(256, "full.delete")
        try:
            written = append_jsonl(self.path, payload)
        finally:
            self.storage.release(reservation)
        self._bytes += written
        self._lines += 1
        self._deletes += 1
        self._keys.add(archive_key)
        self._dirty = True
        return True

    def flush(self) -> None:
        """落盘索引与统计。"""

        if not self._dirty:
            return
        atomic_write_json(
            self.paths.full_index,
            {
                "bytes": self.path.stat().st_size if self.path.exists() else self._bytes,
                "lines": self._lines,
                "upserts": self._upserts,
                "deletes": self._deletes,
                "keys": sorted(self._keys),
                "updatedAt": iso_utc(),
            },
        )
        atomic_write_json(
            self.paths.full_stats,
            {
                "bytes": self.path.stat().st_size if self.path.exists() else self._bytes,
                "lines": self._lines,
                "upserts": self._upserts,
                "deletes": self._deletes,
                "records": len({key.split(":", 1)[0] for key in self._keys if ":" in key}),
                "updatedAt": iso_utc(),
            },
        )
        self._dirty = False

    def query_tail(
        self,
        *,
        scan_bytes: int | None = None,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """只扫描文件尾部（默认 ``full_scan_bytes``），返回最新可见修订。"""

        limit = max(1, min(500, int(limit)))
        scan = int(scan_bytes or self.storage.settings.memory.full_scan_bytes)
        try:
            file_size = self.path.stat().st_size
        except OSError:
            return []
        data = read_bytes_tail(self.path, scan)
        if not data:
            return []
        if file_size > len(data) and b"\n" in data:
            # 尾部截断时，第一条可能是半行，丢弃。
            data = data.split(b"\n", 1)[1]
        latest: dict[str, dict[str, Any]] = {}
        deleted: set[str] = set()
        for line in data.split(b"\n"):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            record_id = str(payload.get("id") or "")
            if not record_id:
                continue
            if payload.get("op") == "delete":
                deleted.add(record_id)
                latest.pop(record_id, None)
                continue
            record = payload.get("record")
            if isinstance(record, dict) and (predicate is None or predicate(record)):
                latest[record_id] = record
                deleted.discard(record_id)
        return list(latest.values())[-limit:][::-1]

    def compact(self, keep: Callable[[dict[str, Any]], bool]) -> dict[str, int]:
        """按谓词重写全量卡（擦除/压缩），调用方必须持有 writer 锁。"""

        self.load_index()
        tmp = self.path.with_name(f".full.compact-{uuid.uuid4().hex[:8]}.jsonl")
        kept = removed = 0
        reservation = self.storage.reserve(
            int(self.path.stat().st_size * 1.1) if self.path.exists() else 0,
            "full.compact",
            required_temp=int(self.path.stat().st_size) if self.path.exists() else 0,
            essential=True,
        )
        try:
            with open(self.path, "rb") as source, open(tmp, "wb") as target:
                for line in source:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        payload = json.loads(stripped.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(payload, dict) or not keep(payload):
                        removed += 1
                        continue
                    target.write(line if line.endswith(b"\n") else line + b"\n")
                    kept += 1
                target.flush()
                os.fsync(target.fileno())
            os.replace(tmp, self.path)
        finally:
            self.storage.release(reservation)
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        self._loaded = False
        self.load_index(force=True)
        self.flush()
        return {"kept": kept, "removed": removed}


# ---------------------------------------------------------------------------
# checkpoint
# ---------------------------------------------------------------------------


class CheckpointStore:
    """按分片保存 offset 与已提交版本。"""

    def __init__(self, paths: PluginPaths) -> None:
        self.paths = paths

    def load(self) -> dict[str, Any]:
        data = read_json(self.paths.checkpoint_json, None)
        if not isinstance(data, dict):
            return {"version": 1, "committedRevision": 0, "shards": {}}
        shards = data.get("shards")
        if not isinstance(shards, dict):
            data["shards"] = {}
        data.setdefault("version", 1)
        data.setdefault("committedRevision", 0)
        return data

    def save(self, data: dict[str, Any]) -> None:
        atomic_write_json(self.paths.checkpoint_json, data)


# ---------------------------------------------------------------------------
# 操作日志
# ---------------------------------------------------------------------------


class OperationLog:
    """管理操作登记；append-only，按 ``op_id`` 取最新状态。"""

    def __init__(self, paths: PluginPaths) -> None:
        self.paths = paths

    def ensure(self) -> None:
        ensure_dir(self.paths.operations_jsonl.parent)
        if not self.paths.operations_jsonl.exists():
            fd = os.open(str(self.paths.operations_jsonl), os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)

    def append(self, *, op_type: str, status: str, payload: dict[str, Any] | None = None, op_id: str = "", result: dict[str, Any] | None = None) -> str:
        """登记一次操作并返回 op_id。"""

        self.ensure()
        identifier = op_id or f"op_{uuid.uuid4().hex[:16]}"
        entry = {
            "opId": identifier,
            "type": op_type,
            "status": status,
            "payload": payload or {},
            "result": result or {},
            "at": iso_utc(),
        }
        with FileLock(self.paths.operations_lock):
            append_jsonl(self.paths.operations_jsonl, entry)
        return identifier

    def update(self, op_id: str, status: str, *, result: dict[str, Any] | None = None) -> None:
        """追加状态变更。"""

        self.append(op_type="status", status=status, op_id=op_id, result=result)

    def latest(self, op_id: str) -> dict[str, Any] | None:
        """返回某操作的最新状态。"""

        found: dict[str, Any] | None = None
        for _offset, payload in iter_jsonl(self.paths.operations_jsonl):
            if isinstance(payload, dict) and payload.get("opId") == op_id:
                if found is None:
                    found = dict(payload)
                else:
                    if payload.get("status") != "status":
                        found.update({key: value for key, value in payload.items() if key not in {"payload", "result"}})
                    found["status"] = payload.get("status") or found.get("status")
                    if payload.get("result"):
                        found["result"] = payload["result"]
        return found

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """列出最近操作（按 opId 去重，新状态覆盖旧状态）。"""

        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for _offset, payload in iter_jsonl(self.paths.operations_jsonl):
            if not isinstance(payload, dict):
                continue
            op_id = str(payload.get("opId") or "")
            if not op_id:
                continue
            if op_id not in merged:
                merged[op_id] = dict(payload)
                order.append(op_id)
            else:
                current = merged[op_id]
                if payload.get("type") != "status":
                    for key, value in payload.items():
                        if key not in {"payload", "result"}:
                            current[key] = value
                current["status"] = payload.get("status") or current.get("status")
                if payload.get("result"):
                    current["result"] = payload["result"]
        return [merged[op_id] for op_id in order[-limit:]][::-1]

    def rewrite(self, transform: Callable[[dict[str, Any]], dict[str, Any] | None]) -> int:
        """重写操作日志（物理擦除用），返回保留条数。"""

        self.ensure()
        tmp = self.paths.operations_jsonl.with_name(f".ops-{uuid.uuid4().hex[:8]}.jsonl")
        kept = 0
        with open(self.paths.operations_jsonl, "rb") as source, open(tmp, "wb") as target:
            for line in source:
                try:
                    payload = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                rewritten = transform(payload)
                if rewritten is None:
                    continue
                target.write((json.dumps(rewritten, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
                kept += 1
            target.flush()
            os.fsync(target.fileno())
        os.replace(tmp, self.paths.operations_jsonl)
        return kept


# ---------------------------------------------------------------------------
# 擦除屏障
# ---------------------------------------------------------------------------


def _hash_value(value: str) -> str:
    return sha256_hex(value)[:32]


class ErasedBarrier:
    """``erased.json``：只保存 id/key 哈希与时间，防止任何副本复活。"""

    def __init__(self, paths: PluginPaths) -> None:
        self.paths = paths
        self._ids: set[str] = set()
        self._keys: set[str] = set()
        self.entries: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        data = read_json(self.paths.erased_json, {"version": 1, "entries": {}})
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            entries = {}
        self.entries = entries
        self._ids = set()
        self._keys = set()
        for entry in entries.values():
            if not isinstance(entry, dict):
                continue
            if entry.get("idHash"):
                self._ids.add(str(entry["idHash"]))
            for key_hash in entry.get("keyHashes") or []:
                self._keys.add(str(key_hash))

    def is_erased(self, record_id: str = "", event_keys: Iterable[str] | None = None) -> bool:
        """是否命中擦除屏障。"""

        if record_id and _hash_value(record_id) in self._ids:
            return True
        for key in event_keys or []:
            if _hash_value(str(key)) in self._keys:
                return True
        return False

    def add(self, *, erasure_id: str, record_id: str, event_keys: Iterable[str]) -> None:
        """登记一个擦除目标。"""

        entry = {
            "erasureId": erasure_id,
            "idHash": _hash_value(record_id) if record_id else "",
            "keyHashes": sorted({_hash_value(str(key)) for key in event_keys if key}),
            "erasedAt": iso_utc(),
        }
        self.entries[erasure_id] = entry
        if entry["idHash"]:
            self._ids.add(entry["idHash"])
        self._keys.update(entry["keyHashes"])
        self.save()

    def merge(self, other: ErasedBarrier) -> int:
        """恢复时求并集，禁止旧备份覆盖当前屏障。"""

        merged = 0
        for erasure_id, entry in other.entries.items():
            if erasure_id not in self.entries:
                self.entries[erasure_id] = entry
                merged += 1
                if entry.get("idHash"):
                    self._ids.add(str(entry["idHash"]))
                self._keys.update(str(item) for item in entry.get("keyHashes") or [])
        if merged:
            self.save()
        return merged

    def save(self) -> None:
        ensure_dir(self.paths.erased_json.parent)
        atomic_write_json(
            self.paths.erased_json,
            {"version": 1, "count": len(self.entries), "entries": self.entries, "updatedAt": iso_utc()},
        )

    def key_hashes(self) -> set[str]:
        """返回事件键哈希集合（供 ingest 过滤）。"""

        return set(self._keys)

    def id_hashes(self) -> set[str]:
        """返回记录 id 哈希集合。"""

        return set(self._ids)


# ---------------------------------------------------------------------------
# Writer generation / fencing
# ---------------------------------------------------------------------------


class StaleWriter(RuntimeError):
    """当前实例已失去 writer 代际。"""


class WriterState:
    """writer generation 与 fencing 标记。"""

    def __init__(self, paths: PluginPaths, token: str = "") -> None:
        self.paths = paths
        self.token = token or uuid.uuid4().hex
        self.generation = 0
        self.lastCommitRevision = 0

    def load(self) -> dict[str, Any]:
        data = read_json(self.paths.writer_state, None)
        return data if isinstance(data, dict) else {}

    def take(self, *, revision: int = 0) -> None:
        """持有 writer 锁后接管代际。"""

        data = self.load()
        generation = int(data.get("generation") or 0) + 1
        self.generation = generation
        self.lastCommitRevision = revision or int(data.get("lastCommitRevision") or 0)
        atomic_write_json(
            self.paths.writer_state,
            {
                "token": self.token,
                "generation": generation,
                "pid": os.getpid(),
                "acquiredAt": iso_utc(),
                "heartbeatAt": iso_utc(),
                "lastCommitRevision": self.lastCommitRevision,
            },
        )

    def heartbeat(self) -> None:
        """刷新心跳。"""

        data = self.load()
        data.update({"token": self.token, "heartbeatAt": iso_utc(), "pid": os.getpid()})
        atomic_write_json(self.paths.writer_state, data)

    def still_valid(self) -> bool:
        """mutation 前校验：代际是否仍属于自己。"""

        data = self.load()
        return bool(data) and str(data.get("token") or "") == self.token

    def assert_valid(self) -> None:
        """代际失效时抛出。"""

        if not self.still_valid():
            raise StaleWriter("writer 代际已失效，停止 mutation")

    def record_commit(self, revision: int) -> None:
        """记录最后提交版本。"""

        self.lastCommitRevision = revision
        data = self.load()
        data.update({"token": self.token, "lastCommitRevision": revision, "heartbeatAt": iso_utc()})
        atomic_write_json(self.paths.writer_state, data)

    def release(self) -> None:
        """释放代际标记（保留 generation 计数）。"""

        data = self.load()
        if str(data.get("token") or "") == self.token:
            data["token"] = ""
            data["heartbeatAt"] = iso_utc()
            atomic_write_json(self.paths.writer_state, data)
