"""最小采集、首次脱敏与 append-only inbox（《方案.md》§3.2）。

顺序严格固定：规范化 → 最小采集过滤 → 首次脱敏 → 生成 eventKey → append。
脱敏必须在第一次写 inbox 之前完成；之后任何渲染都无法撤回已落盘的秘密。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from .common import FileLock, append_jsonl, iso_utc, local_day, resolve_timezone
from .identity import Conversation, build_conversation, extract_text, message_timestamp
from .models import Event
from .sanitize import normalize_text, redact_secrets
from .scoring import analyze_text
from .settings import Settings
from .store import ErasedBarrier, PluginPaths, StorageFull, StorageGuard

LOGGER = logging.getLogger("plugin.com.calarence.persona")


def _event_key(conversation_key: str, message_id: str) -> str:
    return f"{conversation_key}:{message_id or uuid.uuid4().hex[:12]}"


def _reply_target(message: dict[str, Any]) -> str:
    components = message.get("raw_message")
    if not isinstance(components, list):
        return ""
    for component in components:
        if not isinstance(component, dict):
            continue
        if str(component.get("type") or "").lower() != "reply":
            continue
        data = component.get("data")
        if isinstance(data, dict):
            for key in ("id", "message_id", "target_id"):
                if data.get(key):
                    return str(data[key])
        elif isinstance(data, (str, int)):
            return str(data)
    return ""


def should_collect(
    conversation: Conversation,
    text: str,
    settings: Settings,
) -> tuple[bool, str]:
    """最小采集过滤；返回 ``(是否采集, 原因)``。"""

    cleaned = text.strip()
    if not cleaned:
        return False, "无文本"
    if cleaned.startswith(("/", "／", ".")) and len(cleaned) <= 200:
        return False, "命令消息"
    if cleaned in {"[图片]", "[表情]", "[语音]", "[文件]", "[图片][表情]"}:
        return False, "无文本媒体"

    if conversation.is_owner and not conversation.is_group:
        return True, "主人私聊"
    if not conversation.is_group:
        return True, "直接私聊"
    if conversation.is_direct_to_bot:
        return True, "群内直接对她说"

    signals = analyze_text(cleaned)
    if settings.general.collect_group_signals and signals["groupSignal"]:
        return True, "群内情绪/事件信号"
    return False, "不相关群消息"


class Ingestor:
    """单 append writer：每个 Runner generation 只写自己的分片。"""

    def __init__(
        self,
        paths: PluginPaths,
        settings: Settings,
        storage: StorageGuard,
        barrier: ErasedBarrier,
        *,
        generation: str,
    ) -> None:
        self.paths = paths
        self.settings = settings
        self.storage = storage
        self.barrier = barrier
        self.generation = (generation or uuid.uuid4().hex)[:16]
        self._seen: set[str] = set()
        self._loaded_dedupe = False
        self._stop_logged = False
        self._erased_keys = barrier.key_hashes()

    # -- dedupe ---------------------------------------------------------

    def _load_dedupe(self) -> None:
        if self._loaded_dedupe:
            return
        self._loaded_dedupe = True
        path = self.paths.dedupe_jsonl
        if not path.exists():
            return
        try:
            with open(path, "rb") as handle:
                for line in handle:
                    digest = line.strip()
                    if digest:
                        self._seen.add(digest.decode("ascii", errors="ignore"))
        except OSError as exc:
            LOGGER.warning("读取 dedupe 索引失败，将依赖 archiveKey 幂等: %s", exc)

    @staticmethod
    def _digest(event_key: str) -> str:
        return hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:32]

    # -- prepare --------------------------------------------------------

    def prepare(self, message: dict[str, Any]) -> tuple[Event | None, str]:
        """规范化并脱敏一条入站消息；不采集时返回 ``(None, 原因)``。"""

        if not isinstance(message, dict):
            return None, "message 不是字典"
        if not self.settings.memory_enabled:
            return None, "插件未启用或配置错误"

        conversation = build_conversation(
            message,
            owner_qq=self.settings.general.owner_qq,
            owner_platform=self.settings.general.owner_platform,
            owner_account=self.settings.general.owner_account,
            bot_names=self.settings.general.bot_names,
        )
        raw_text = extract_text(message)
        text = normalize_text(raw_text)
        collect, reason = should_collect(conversation, text, self.settings)
        if not collect:
            return None, reason

        sanitized, hits = redact_secrets(text)
        sanitized = normalize_text(sanitized)
        if not sanitized:
            return None, "脱敏后为空"

        message_id = str(message.get("message_id") or message.get("msg_id") or "").strip()
        event_key = _event_key(conversation.conversation_key, message_id)
        digest = self._digest(event_key)
        self._load_dedupe()
        if digest in self._seen:
            return None, "重复事件"
        if digest in self._erased_keys or self.barrier.is_erased("", [event_key]):
            return None, "命中擦除屏障"

        timestamp = message_timestamp(message)
        source = "qq-owner" if conversation.is_owner else ("qq-group" if conversation.is_group else "qq-private")
        event = Event(
            event_key=event_key,
            conversation_key=conversation.conversation_key,
            session_id=conversation.session_id,
            platform=conversation.platform,
            account_id=conversation.account_id,
            route_scope=conversation.route_scope,
            chat_type=conversation.chat_type,
            chat_id=conversation.chat_id,
            message_id=message_id,
            user_id=str(conversation.raw.get("user_id") or ""),
            sender_name=str(conversation.raw.get("user_name") or ""),
            text=sanitized,
            timestamp=iso_utc(timestamp),
            received_at=iso_utc(),
            is_owner=conversation.is_owner,
            is_direct_to_bot=conversation.is_direct_to_bot,
            is_group=conversation.is_group,
            source=source,
            dry_run=bool(self.settings.general.dry_run),
            signals=analyze_text(sanitized),
            reply_to_message_id=_reply_target(message),
            sanitized_hits=hits,
        )
        return event, reason

    # -- append ---------------------------------------------------------

    def shard_path(self, event: Event) -> Path:
        tz = resolve_timezone(self.settings.general.timezone)
        day = local_day(message_timestamp({"timestamp": event.timestamp}), tz)
        base = self.paths.inbox_dir / ("dry" if event.dry_run else "") / day
        return base / f"{self.generation}.jsonl"

    def append(self, event: Event) -> bool:
        """append 到当前 generation 分片；重复/超预算返回 ``False``。"""

        if not self.settings.memory_enabled:
            return False
        if not self.storage.can_ingest():
            if not self._stop_logged:
                LOGGER.error("磁盘占用达到停止摄取阈值，采集已降级（详见 /persona stats）")
                self._stop_logged = True
            return False
        path = self.shard_path(event)
        payload = event.to_dict()
        line_size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 2
        try:
            reservation = self.storage.reserve(line_size, "inbox.append")
        except StorageFull as exc:
            LOGGER.error("inbox append 预留失败：%s", exc)
            return False
        try:
            append_jsonl(path, payload)
        finally:
            self.storage.release(reservation)

        self._load_dedupe()
        digest = self._digest(event.event_key)
        self._seen.add(digest)
        try:
            with FileLock(self.paths.inbox_dir / "dedupe.lock") as _lock:
                append_jsonl(self.paths.dedupe_jsonl, {"k": digest, "at": iso_utc()})
        except Exception as exc:  # dedupe 索引失败不应阻断采集
            LOGGER.warning("dedupe 索引写入失败（依赖 archiveKey 幂等）：%s", exc)
        self._stop_logged = False
        return True

    def handle_message(self, message: dict[str, Any]) -> bool:
        """prepare + append 的便捷入口。"""

        event, reason = self.prepare(message)
        if event is None:
            LOGGER.debug("跳过采集：%s", reason)
            return False
        return self.append(event)


def list_inbox_shards(paths: PluginPaths) -> list[Path]:
    """枚举**所有**未提交分片（不是「昨天+今天」）。"""

    shards: list[Path] = []
    if not paths.inbox_dir.exists():
        return shards
    for path in paths.inbox_dir.rglob("*.jsonl"):
        if path.name in {"dedupe.jsonl"}:
            continue
        if "dry" in path.parts:
            continue
        shards.append(path)
    shards.sort(key=lambda item: (item.parent.name, item.name))
    return shards
