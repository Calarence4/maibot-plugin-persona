"""纯标准库实现的通用工具：时间、JSON、原子写、文件锁、预算与哈希。

设计约束（来自《方案.md》§7）：

- 持久化排程用 UTC wall time，自然日使用配置时区；
- 临时文件 + fsync + ``os.replace`` + 目录 fsync；
- 跨 Runner 用 ``fcntl.flock``；Windows 本地开发用 ``msvcrt`` 兜底；
- 所有 mutation 共享同一把 writer 锁，tick 单飞。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone, tzinfo
from functools import lru_cache
from pathlib import Path
from typing import Any

UTC = timezone.utc


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """返回当前 UTC 时间。"""

    return datetime.now(UTC)


def iso_utc(value: datetime | None = None) -> str:
    """把时间格式化为带 Z 的 RFC3339 字符串。"""

    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: Any) -> datetime | None:
    """解析 RFC3339 / ISO8601 字符串；失败返回 ``None``。"""

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


@lru_cache(maxsize=16)
def resolve_timezone(name: str) -> tzinfo:
    """解析配置时区；缺少 tzdata 时对常见时区回退到固定偏移。"""

    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        fixed_offsets = {
            "Asia/Shanghai": 8,
            "Asia/Hong_Kong": 8,
            "Asia/Taipei": 8,
            "Asia/Tokyo": 9,
            "UTC": 0,
            "Etc/UTC": 0,
        }
        hours = fixed_offsets.get(name, 8)
        return timezone(timedelta(hours=hours), name=name)


def local_day(value: datetime, tz: tzinfo) -> str:
    """返回配置时区下的自然日 ``YYYY-MM-DD``。"""

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(tz).strftime("%Y-%m-%d")


def local_datetime(value: datetime, tz: tzinfo) -> datetime:
    """把时间转换到配置时区。"""

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(tz)


def next_local_hour(now: datetime, tz: tzinfo, hour: int) -> datetime:
    """返回配置时区下一个 ``hour`` 点的 UTC 时间。"""

    current = local_datetime(now, tz)
    target = current.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= current:
        target = target + timedelta(days=1)
    return target.astimezone(UTC)


# ---------------------------------------------------------------------------
# 哈希与基础类型
# ---------------------------------------------------------------------------


def sha256_hex(data: str | bytes) -> str:
    """返回 SHA-256 十六进制摘要。"""

    if isinstance(data, str):
        data = data.encode("utf-8", errors="replace")
    return hashlib.sha256(data).hexdigest()


def stable_id(prefix: str, key: str, length: int = 16) -> str:
    """由稳定输入派生 id，保证重放时命中同一个 id。"""

    return f"{prefix}{sha256_hex(key)[:length]}"


def new_id(prefix: str) -> str:
    """生成随机 id。"""

    return f"{prefix}{uuid.uuid4().hex}"


def clamp(value: float, low: float, high: float) -> float:
    """把数值夹在闭区间内。"""

    return max(low, min(high, value))


def as_int(value: Any, default: int = 0) -> int:
    """宽松转整数。"""

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    """宽松转浮点数。"""

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_bool(value: Any, default: bool = False) -> bool:
    """宽松转布尔值。"""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "是"}:
            return True
        if lowered in {"0", "false", "no", "off", "否"}:
            return False
    return default


def first_str(mapping: Mapping[str, Any], *keys: str, default: str = "") -> str:
    """按顺序取第一个非空字符串字段。"""

    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return default


# ---------------------------------------------------------------------------
# 文件读写
# ---------------------------------------------------------------------------


def ensure_dir(path: Path) -> Path:
    """创建目录（含父目录）。"""

    path.mkdir(parents=True, exist_ok=True)
    return path


def _fsync_dir(path: Path) -> None:
    """尽力 fsync 目录；Windows 上打开目录会失败，静默跳过。"""

    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str) -> None:
    """原子写入 UTF-8 文本，并尽量收紧权限到 ``0600``。"""

    ensure_dir(path.parent)
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def atomic_write_json(path: Path, data: Any) -> None:
    """原子写入 JSON（UTF-8，不转义中文，带缩进）。"""

    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    """读取 JSON；文件不存在或损坏时返回默认值。"""

    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError):
        return default


def append_jsonl(path: Path, payload: Mapping[str, Any]) -> int:
    """追加一行 JSONL 并 fsync，返回写入字节数。"""

    ensure_dir(path.parent)
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    data = line.encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    return len(data)


def iter_jsonl(path: Path, offset: int = 0, *, complete_lines_only: bool = False) -> Iterable[tuple[int, Any]]:
    """从 ``offset`` 开始逐行读取 JSONL，产出 ``(结束偏移, 对象)``。

    损坏行会被跳过，但偏移仍然前进，避免单个坏行阻塞整个归档。
    """

    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            position = offset
            for raw in handle:
                position += len(raw)
                if complete_lines_only and not raw.endswith(b"\n"):
                    break
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    yield position, json.loads(stripped.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
    except FileNotFoundError:
        return


def read_bytes_tail(path: Path, limit: int) -> bytes:
    """读取文件尾部最多 ``limit`` 字节。"""

    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            return handle.read(limit)
    except OSError:
        return b""


def file_signature(path: Path) -> tuple[int, int, int, str] | None:
    """返回 ``(mtime_ns, size, inode, content_hash)``；文件不存在返回 ``None``。"""

    try:
        stat = path.stat()
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        return (stat.st_mtime_ns, stat.st_size, int(getattr(stat, "st_ino", 0)), digest)
    except OSError:
        return None


def directory_size(path: Path) -> int:
    """递归统计目录大小（字节）。"""

    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
    except OSError:
        return total
    return total


def prune_empty_dirs(root: Path) -> None:
    """自底向上删除空目录。"""

    try:
        for current, _dirs, files in os.walk(root, topdown=False):
            if current == str(root):
                continue
            if not files and not os.listdir(current):
                os.rmdir(current)
    except OSError:
        return


# ---------------------------------------------------------------------------
# 文件锁
# ---------------------------------------------------------------------------

_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()

try:  # pragma: no cover - 平台相关
    import fcntl as _fcntl
except ImportError:  # pragma: no cover
    _fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover - 平台相关
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover
    _msvcrt = None  # type: ignore[assignment]


class FileLock:
    """跨进程（Linux）与进程内（Windows 开发）均可用的排他文件锁。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None
        self._local: threading.Lock | None = None

    def _local_lock(self) -> threading.Lock:
        key = str(self.path)
        with _LOCAL_LOCKS_GUARD:
            lock = _LOCAL_LOCKS.get(key)
            if lock is None:
                lock = threading.Lock()
                _LOCAL_LOCKS[key] = lock
            return lock

    def acquire(self, *, blocking: bool = False, timeout: float = 0.0) -> bool:
        """尝试获取锁；非阻塞失败立即返回 ``False``。"""

        local = self._local_lock()
        if blocking:
            acquired_local = local.acquire(timeout=timeout) if timeout > 0 else local.acquire()
        else:
            acquired_local = local.acquire(blocking=False)
        if not acquired_local:
            return False
        ensure_dir(self.path.parent)
        try:
            handle = open(self.path, "a+b")
        except OSError:
            local.release()
            return False
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                if _fcntl is not None:
                    _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX | (_fcntl.LOCK_NB if not blocking else 0))
                elif _msvcrt is not None:  # pragma: no cover - Windows
                    handle.seek(0)
                    _msvcrt.locking(handle.fileno(), _msvcrt.LK_NBLCK, 1)
                self._handle = handle
                self._local = local
                return True
            except OSError:
                if not blocking or time.monotonic() >= deadline:
                    handle.close()
                    local.release()
                    return False
                time.sleep(0.05)

    def release(self) -> None:
        """释放锁。"""

        handle, local = self._handle, self._local
        self._handle = None
        self._local = None
        if handle is not None:
            try:
                if _fcntl is not None:
                    _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
                elif _msvcrt is not None:  # pragma: no cover - Windows
                    handle.seek(0)
                    _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            finally:
                handle.close()
        if local is not None and local.locked():
            try:
                local.release()
            except RuntimeError:
                pass

    def __enter__(self) -> "FileLock":
        if not self.acquire():
            raise TimeoutError(f"无法获取文件锁: {self.path}")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()
