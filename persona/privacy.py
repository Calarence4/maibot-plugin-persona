"""隐私矩阵（《方案.md》§3.9）。

硬规则：

- ``owner`` / ``private`` 永不进群；不存在「当事人在场即可读」；
- ``group`` 只能进来源群；
- ``public`` 必须同时满足记录级 ``allowedConversationKeys`` 与配置 ``privacy.public_groups``；
- 授权撤销立即让所有派生 prompt 失效（由 privacyRevision 触发）；
- 擦除屏障在 ingest、查询、恢复、FTS、render 五处统一过滤（本模块负责 render 与查询侧）。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .settings import Settings
from .store import ErasedBarrier

SCOPE_OWNER = "owner"
SCOPE_PRIVATE = "private"
SCOPE_GROUP = "group"
SCOPE_PUBLIC = "public"

SCOPE_ORDER = {
    SCOPE_OWNER: 0,
    SCOPE_PRIVATE: 1,
    SCOPE_GROUP: 2,
    SCOPE_PUBLIC: 3,
}


def record_scope(record: dict[str, Any]) -> str:
    """返回归一化的 scope。"""

    scope = str(record.get("scope") or SCOPE_PRIVATE)
    return scope if scope in SCOPE_ORDER else SCOPE_PRIVATE


def record_targets(record: dict[str, Any]) -> list[str]:
    """返回记录被显式授权的会话列表。"""

    return [str(item) for item in record.get("allowedConversationKeys") or [] if item]


def can_inject(
    record: dict[str, Any],
    conversation_key: str,
    settings: Settings,
    *,
    erased: ErasedBarrier | None = None,
    admin: bool = False,
) -> bool:
    """判断一条记忆能否注入指定会话。"""

    if admin:
        return True
    if erased is not None and erased.is_erased(str(record.get("id") or ""), record.get("sourceEventKeys")):
        return False
    if str(record.get("clarity") or "") == "forgotten":
        return False

    scope = record_scope(record)
    source_key = str(record.get("conversationKey") or "")

    if scope in {SCOPE_OWNER, SCOPE_PRIVATE, SCOPE_GROUP}:
        return bool(conversation_key) and conversation_key == source_key

    if scope == SCOPE_PUBLIC:
        targets = set(record_targets(record))
        allowed = set(settings.privacy.public_groups)
        return bool(conversation_key) and conversation_key in targets and conversation_key in allowed

    return False


def filter_for_conversation(
    records: Iterable[dict[str, Any]],
    conversation_key: str,
    settings: Settings,
    *,
    erased: ErasedBarrier | None = None,
) -> list[dict[str, Any]]:
    """返回该会话可见的记录。"""

    return [record for record in records if can_inject(record, conversation_key, settings, erased=erased)]


def is_cross_domain(scope: str) -> bool:
    """group/public 渲染是否需要跨域脱敏。"""

    return scope in {SCOPE_GROUP, SCOPE_PUBLIC}


def validate_public_grant(
    *,
    scope: str,
    allowed_targets: list[str],
    authorized_by: str,
    authorized_at: str,
    settings: Settings,
) -> tuple[bool, str]:
    """校验 public 授权目标必须是 ``public_groups`` 的子集。"""

    if scope != SCOPE_PUBLIC:
        return True, ""
    configured = set(settings.privacy.public_groups)
    targets = set(allowed_targets or [])
    if not targets:
        return False, "public 记录必须至少包含一个 allowedConversationKeys 目标"
    extra = targets - configured
    if extra:
        return False, f"授权目标不在 privacy.public_groups 中: {sorted(extra)}"
    if not authorized_by or not authorized_at:
        return False, "public 记录必须记录 authorizedBy 与 authorizedAt"
    return True, ""
