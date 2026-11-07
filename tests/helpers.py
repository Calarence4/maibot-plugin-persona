"""测试共用工具。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from persona.service import PersonaService  # noqa: E402


def merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def make_config(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "general": {
            "enabled": True,
            "owner_qq": "10001",
            "owner_platform": "qq",
            "timezone": "Asia/Shanghai",
            "bot_names": ["花凪みお"],
        },
        "memory": {
            "max_chars": 60000,
            "max_records": 600,
            "inject_max_chars": 2200,
            "daily_maintain_hour": 4,
        },
        "style": {
            "enabled": False,
            "application_mode": "disabled",
            "default_style": "casual",
        },
    }
    return merge(base, overrides)


def make_service(tmp_path: Path, **overrides: Any) -> PersonaService:
    service = PersonaService(tmp_path / "data", tmp_path / "runtime", make_config(**overrides))
    service.initialize()
    return service


def inbound(
    *,
    session: str = "sess-owner",
    message_id: str = "m1",
    text: str = "今天有点难过，想抱抱",
    user_id: str = "10001",
    user_name: str = "主人",
    group_id: str = "",
    account_id: str = "888",
    timestamp: str = "2026-11-07T10:00:00+08:00",
    platform: str = "qq",
) -> dict[str, Any]:
    message_info: dict[str, Any] = {
        "user_info": {"user_id": user_id, "user_nickname": user_name},
        "additional_config": {"self_id": account_id, "account_id": account_id},
    }
    if group_id:
        message_info["group_info"] = {"group_id": group_id, "group_name": "测试群"}
    return {
        "platform": platform,
        "account_id": account_id,
        "scope": "*",
        "session_id": session,
        "message_id": message_id,
        "timestamp": timestamp,
        "raw_message": [{"type": "text", "data": text}],
        "message_info": message_info,
    }


def owner_conversation(account: str = "888", chat_id: str = "10001") -> str:
    return f"qq:{account}:*:private:{chat_id}"


def group_conversation(group_id: str, account: str = "888") -> str:
    return f"qq:{account}:*:group:{group_id}"
