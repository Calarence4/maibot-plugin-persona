"""核心数据模型：采集事件、记忆记录、清晰度。

记录结构严格对齐《方案.md》§3.1；这里使用普通 dict 持久化，便于全量日志直接追加与
管理台按字段读取，同时用 :func:`normalize_record` 保证缺字段安全。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .common import iso_utc
from .constants import CLARITY_ORDER, FALLBACK_STYLE_ID

SCHEMA_VERSION = 1


@dataclass
class Event:
    """一行 inbox 事件（已完成规范化、过滤与首次脱敏）。"""

    event_key: str
    conversation_key: str
    session_id: str
    platform: str
    account_id: str
    route_scope: str
    chat_type: str
    chat_id: str
    message_id: str
    user_id: str
    sender_name: str
    text: str
    timestamp: str
    received_at: str
    is_owner: bool = False
    is_direct_to_bot: bool = False
    is_group: bool = False
    source: str = "qq"
    dry_run: bool = False
    signals: dict[str, Any] = field(default_factory=dict)
    reply_to_message_id: str = ""
    sanitized_hits: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转成可写入 JSONL 的字典。"""

        return {
            "v": SCHEMA_VERSION,
            "eventKey": self.event_key,
            "conversationKey": self.conversation_key,
            "sessionId": self.session_id,
            "platform": self.platform,
            "accountId": self.account_id,
            "routeScope": self.route_scope,
            "chatType": self.chat_type,
            "chatId": self.chat_id,
            "messageId": self.message_id,
            "userId": self.user_id,
            "senderName": self.sender_name,
            "text": self.text,
            "timestamp": self.timestamp,
            "receivedAt": self.received_at,
            "isOwner": self.is_owner,
            "isDirectToBot": self.is_direct_to_bot,
            "isGroup": self.is_group,
            "source": self.source,
            "dryRun": self.dry_run,
            "signals": dict(self.signals),
            "replyToMessageId": self.reply_to_message_id,
            "sanitizedHits": dict(self.sanitized_hits),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        """从 JSONL 字典还原事件。"""

        return cls(
            event_key=str(data.get("eventKey") or ""),
            conversation_key=str(data.get("conversationKey") or ""),
            session_id=str(data.get("sessionId") or ""),
            platform=str(data.get("platform") or "qq"),
            account_id=str(data.get("accountId") or ""),
            route_scope=str(data.get("routeScope") or "*"),
            chat_type=str(data.get("chatType") or "private"),
            chat_id=str(data.get("chatId") or ""),
            message_id=str(data.get("messageId") or ""),
            user_id=str(data.get("userId") or ""),
            sender_name=str(data.get("senderName") or ""),
            text=str(data.get("text") or ""),
            timestamp=str(data.get("timestamp") or ""),
            received_at=str(data.get("receivedAt") or ""),
            is_owner=bool(data.get("isOwner")),
            is_direct_to_bot=bool(data.get("isDirectToBot")),
            is_group=bool(data.get("isGroup")),
            source=str(data.get("source") or "qq"),
            dry_run=bool(data.get("dryRun")),
            signals=dict(data.get("signals") or {}),
            reply_to_message_id=str(data.get("replyToMessageId") or ""),
            sanitized_hits=dict(data.get("sanitizedHits") or {}),
        )


def empty_record() -> dict[str, Any]:
    """返回一条空记录骨架。"""

    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": "",
        "revision": 0,
        "archiveKey": "",
        "sourceEventKeys": [],
        "platform": "qq",
        "accountId": "",
        "routeScope": "*",
        "conversationKey": "",
        "source": "qq",
        "scope": "private",
        "allowedConversationKeys": [],
        "authorizedBy": None,
        "authorizedAt": None,
        "kind": "chat",
        "title": "",
        "text": "",
        "botReplies": [],
        "eventDate": None,
        "validUntil": None,
        "eventDatePrecision": None,
        "eventDateSource": None,
        "importance": 1,
        "importanceReasons": [],
        "pinned": False,
        "clarity": "vivid",
        "initial": None,
        "createdAt": iso_utc(),
        "lastRecalledAt": None,
        "recallCount": 0,
        "decaySteps": 0,
        "mergedFrom": [],
        "correctedFrom": None,
        "changeReason": None,
        "archivedRevision": 0,
    }


def normalize_record(raw: dict[str, Any]) -> dict[str, Any]:
    """补齐缺字段，确保旧数据/手工数据可安全渲染。"""

    record = empty_record()
    record.update({key: value for key, value in raw.items() if key in record or key.startswith("_")})
    record["sourceEventKeys"] = [str(item) for item in record.get("sourceEventKeys") or []]
    record["allowedConversationKeys"] = [str(item) for item in record.get("allowedConversationKeys") or []]
    record["importanceReasons"] = [str(item) for item in record.get("importanceReasons") or []]
    record["botReplies"] = [item for item in record.get("botReplies") or [] if isinstance(item, dict)]
    record["mergedFrom"] = [str(item) for item in record.get("mergedFrom") or []]
    record["importance"] = int(record.get("importance") or 1)
    record["revision"] = int(record.get("revision") or 0)
    record["recallCount"] = int(record.get("recallCount") or 0)
    record["decaySteps"] = int(record.get("decaySteps") or 0)
    record["pinned"] = bool(record.get("pinned"))
    clarity = str(record.get("clarity") or "vivid")
    record["clarity"] = clarity if clarity in CLARITY_ORDER else "vivid"
    return record


def clarity_rank(clarity: str) -> int:
    """返回清晰度等级（越大越清晰）。"""

    try:
        index = list(reversed(CLARITY_ORDER)).index(clarity)
    except ValueError:
        return 0
    return index


def better_clarity(*values: str) -> str:
    """取最清晰的档位。"""

    candidates = [value for value in values if value in CLARITY_ORDER]
    if not candidates:
        return "vivid"
    return max(candidates, key=clarity_rank)


def worse_clarity(*values: str) -> str:
    """取最模糊的档位。"""

    candidates = [value for value in values if value in CLARITY_ORDER]
    if not candidates:
        return "vivid"
    return min(candidates, key=clarity_rank)


def initial_hit(record: dict[str, Any]) -> bool:
    """「最初」是否命中（兼容 bool 与 dict 两种历史写法）。"""

    value = record.get("initial")
    if isinstance(value, dict):
        return bool(value.get("hit"))
    return bool(value)


def is_protected(record: dict[str, Any], min_owner_importance: int) -> bool:
    """保护记录：initial、pinned、主人重要记录、初次相遇。"""

    if record.get("pinned") or initial_hit(record) or record.get("kind") == "first":
        return True
    if record.get("scope") == "owner" and int(record.get("importance") or 0) >= min_owner_importance:
        return True
    return False


def record_title(record: dict[str, Any], limit: int = 40) -> str:
    """取标题；缺失时从正文推导。"""

    title = str(record.get("title") or "").strip()
    if title:
        return title
    text = str(record.get("text") or "").strip().replace("\n", " ")
    return (text[:limit] + "…") if len(text) > limit else text


def default_style_id() -> str:
    """返回内置兜底风格 id。"""

    return FALLBACK_STYLE_ID
