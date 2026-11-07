"""会话身份与 TurnContext 解析（《方案.md》§2.1）。

Host 的真实入站消息字段（由运行中转储确认）：

- ``message.session_id`` / ``message.message_id`` / ``message.timestamp``
- ``message.raw_message``：组件数组，元素含 ``type`` 与 ``data``
- ``message.processed_plain_text``（出站消息）
- ``message.message_info.user_info.{user_id,user_nickname}``
- ``message.message_info.group_info.{group_id,group_name}``
- ``message.message_info.additional_config.{self_id,bot_id,account_id,platform_io_account_id}``

Host 内部 ``session_id`` 是 32 位哈希，不是平台号；因此 ``conversationKey`` 尽量用平台字段拼，
缺失时退回 ``session_id``，并单独保存映射。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .common import first_str, parse_iso, stable_id, utc_now
from .sanitize import normalize_text

_BAD_KEY_CHARS = re.compile(r"[:：\s]+")
_NON_WORD = re.compile(r"[^0-9A-Za-z_\-*]+")


def _clean_key_part(value: object, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    text = _BAD_KEY_CHARS.sub("_", text)
    text = _NON_WORD.sub("_", text)
    return text or fallback


def component_text(raw_message: Any) -> str:
    """把 Host 的 raw_message 组件数组压平成可读文本。"""

    if isinstance(raw_message, str):
        return raw_message
    if not isinstance(raw_message, list):
        return ""
    chunks: list[str] = []
    for component in raw_message:
        if isinstance(component, str):
            chunks.append(component)
            continue
        if not isinstance(component, dict):
            continue
        kind = str(component.get("type") or "").lower()
        data = component.get("data")
        if kind in {"text", "plain"}:
            if isinstance(data, str):
                chunks.append(data)
            elif isinstance(data, dict):
                chunks.append(str(data.get("text") or ""))
        elif kind == "at":
            target = data.get("target_id") if isinstance(data, dict) else data
            chunks.append(f"@{target}" if target else "@")
        elif kind == "reply":
            continue
        elif kind == "image":
            chunks.append("[图片]")
        elif kind == "emoji":
            chunks.append("[表情]")
        elif kind == "voice":
            chunks.append("[语音]")
        elif kind == "file":
            chunks.append("[文件]")
        elif isinstance(data, str):
            chunks.append(data)
    return "".join(chunks)


def extract_text(message: dict[str, Any]) -> str:
    """从消息字典提取正文，优先 ``processed_plain_text``。"""

    for key in ("processed_plain_text", "plain_text", "text"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return component_text(message.get("raw_message"))


def _additional_config(message_info: dict[str, Any]) -> dict[str, Any]:
    value = message_info.get("additional_config")
    return value if isinstance(value, dict) else {}


def _user_info(message_info: dict[str, Any]) -> dict[str, Any]:
    value = message_info.get("user_info")
    return value if isinstance(value, dict) else {}


def _group_info(message_info: dict[str, Any]) -> dict[str, Any]:
    value = message_info.get("group_info")
    return value if isinstance(value, dict) else {}


def message_timestamp(message: dict[str, Any]):
    """解析消息时间；兼容 ISO 字符串与 epoch 秒。"""

    value = message.get("timestamp")
    if isinstance(value, (int, float)):
        from datetime import datetime, timezone

        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return utc_now()
    parsed = parse_iso(value)
    return parsed or utc_now()


@dataclass
class Conversation:
    """由 Host 消息字典解析出的会话身份。"""

    conversation_key: str
    session_id: str
    platform: str = "qq"
    account_id: str = ""
    route_scope: str = "*"
    chat_type: str = "private"
    chat_id: str = ""
    is_owner: bool = False
    is_direct_to_bot: bool = False
    is_group: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def session_alias(self) -> str:
        """没有 session_id 时用于渲染缓存的稳定别名。"""

        return self.session_id or stable_id("sess_", self.conversation_key, 16)


def build_conversation(
    message: dict[str, Any],
    *,
    owner_qq: str = "",
    owner_platform: str = "qq",
    owner_account: str = "",
    bot_names: list[str] | None = None,
) -> Conversation:
    """把入站/出站消息字典规范化为 :class:`Conversation`。"""

    message_info = message.get("message_info")
    if not isinstance(message_info, dict):
        message_info = {}
    user_info = _user_info(message_info)
    group_info = _group_info(message_info)
    additional = _additional_config(message_info)

    platform = first_str(message, "platform", default=owner_platform or "qq")
    account_id = first_str(
        message,
        "account_id",
        default=first_str(additional, "account_id", "platform_io_account_id", "self_id", "bot_id", default=""),
    )
    route_scope = first_str(message, "route_scope", "scope", default="*")
    user_id = first_str(user_info, "user_id", default=first_str(message, "user_id", "sender_id", default=""))
    group_id = first_str(group_info, "group_id", default=first_str(message, "group_id", default=""))
    session_id = first_str(message, "session_id", "stream_id", default="")

    is_group = bool(group_id) or str(message.get("chat_type") or "").lower() == "group"
    chat_type = "group" if is_group else "private"
    chat_id = group_id or user_id or session_id or "unknown"

    conversation_key = ":".join(
        (
            _clean_key_part(platform, "qq"),
            _clean_key_part(account_id, "default"),
            _clean_key_part(route_scope, "*"),
            chat_type,
            _clean_key_part(chat_id, "unknown"),
        )
    )

    text = extract_text(message)
    lowered = text.lower()
    names = [name.strip() for name in (bot_names or []) if name and name.strip()]
    mentions_bot = any(name in text for name in names)
    components = message.get("raw_message")
    self_ids = {
        first_str(additional, "self_id", "bot_id", default=""),
        account_id,
    }
    if isinstance(components, list):
        for component in components:
            if not isinstance(component, dict):
                continue
            if str(component.get("type") or "").lower() != "at":
                continue
            data = component.get("data")
            target = ""
            if isinstance(data, dict):
                target = first_str(data, "target_id", "qq", "user_id", default="")
            elif isinstance(data, (str, int)):
                target = str(data)
            if target and target in self_ids:
                mentions_bot = True

    is_owner = (
        bool(owner_qq)
        and user_id == owner_qq
        and (not owner_account or account_id == owner_account)
        and platform == (owner_platform or platform)
    )
    is_direct_to_bot = (not is_group) or mentions_bot or (bool(owner_qq) and user_id == owner_qq and "bot" in lowered)

    return Conversation(
        conversation_key=conversation_key,
        session_id=session_id,
        platform=platform,
        account_id=account_id,
        route_scope=route_scope,
        chat_type=chat_type,
        chat_id=chat_id,
        is_owner=is_owner,
        is_direct_to_bot=is_direct_to_bot,
        is_group=is_group,
        raw={
            "user_id": user_id,
            "user_name": first_str(user_info, "user_nickname", "nickname", "cardname", default=""),
            "group_id": group_id,
            "group_name": first_str(group_info, "group_name", default=""),
            "self_id": first_str(additional, "self_id", "bot_id", default=""),
            "text": normalize_text(text),
        },
    )


@dataclass
class TurnContext:
    """replyer 侧关联标识（《方案.md》§2.1）。"""

    conversation_key: str
    session_id: str
    turn_id: str
    reply_message_id: str = ""
    chat_type: str = "private"
    is_owner: bool = False
    request_type: str = ""

    @property
    def key(self) -> str:
        """用于内存索引的稳定键。"""

        return self.turn_id or self.session_id or self.conversation_key


def resolve_turn_context(kwargs: dict[str, Any], session_map: dict[str, str]) -> TurnContext | None:
    """从 replyer Hook 的 kwargs 解析当前 TurnContext。

    优先使用 ``session_id``（Host 保证同一次 replyer 链一致）；其次用 ``reply_message_id``
    查入站映射；两者都不可用返回 ``None``，调用方必须不注入、不选风格并记录原因。
    """

    session_id = first_str(kwargs, "session_id", "stream_id", default="")
    reply_message_id = first_str(kwargs, "reply_message_id", "replyMessageId", default="")
    conversation_key = ""
    if session_id and session_id in session_map:
        conversation_key = session_map[session_id]
    if not conversation_key and reply_message_id:
        conversation_key = session_map.get(f"msg:{reply_message_id}", "")
    if not conversation_key and session_id:
        conversation_key = f"session:{session_id}"
    if not conversation_key:
        return None

    chat_type = "group" if conversation_key.rstrip(":").endswith(":group") or ":group:" in conversation_key else "private"
    turn_id = stable_id("turn_", f"{session_id}|{reply_message_id}|{utc_now().timestamp():.3f}", 12)
    return TurnContext(
        conversation_key=conversation_key,
        session_id=session_id,
        turn_id=turn_id,
        reply_message_id=reply_message_id,
        chat_type=chat_type,
        request_type=first_str(kwargs, "request_type", default=""),
    )
