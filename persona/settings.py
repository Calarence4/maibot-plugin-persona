"""插件配置的纯标准库视图。

SDK 的 ``config_model`` 只负责声明与生成 ``config.toml``；真正运行期使用的是这里的
不可变 dataclass。这样核心逻辑可以在没有 pydantic / MaiBot Host 的环境里直接测试。

配置键与《方案.md》§9 对齐，默认值即文档默认值。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Literal

from .common import as_bool, as_float, as_int

ApplicationMode = Literal["disabled", "host", "plugin"]
SelectMode = Literal["rules", "llm"]
DeliveryMode = Literal["preview", "send"]


@dataclass
class GeneralConfig:
    """基础身份与开关。"""

    enabled: bool = True
    owner_qq: str = ""
    owner_platform: str = "qq"
    owner_account: str = ""
    timezone: str = "Asia/Shanghai"
    dry_run: bool = False
    bot_names: list[str] = field(default_factory=list)
    collect_group_signals: bool = True


@dataclass
class MemoryConfig:
    """记忆、衰减、容量与归档。"""

    maintain_interval_minutes: int = 10
    daily_maintain_hour: int = 4
    max_chars: int = 60000
    warn_chars: int = 48000
    critical_chars: int = 54000
    compact_target_chars: int = 54000
    auto_compact: bool = True
    max_records: int = 600
    inject_max_chars: int = 2200
    fuzzy_text_chars: int = 80
    event_default_grace_hours: int = 24
    recent_days: int = 7
    fuzzy_after_days: int = 7
    forget_after_days: int = 60
    min_owner_importance: int = 65
    recall_reinforcement_enabled: bool = False
    full_warn_bytes: int = 536870912
    full_scan_bytes: int = 5242880
    inbox_retention_days: int = 7
    backup_daily: int = 7
    backup_weekly: int = 4
    max_inbox_batch: int = 500
    pending_reply_ttl_minutes: int = 10
    recent_bonus_days: int = 7
    recent_bonus: int = 18
    recent_extra_days: int = 2
    recent_extra_bonus: int = 8
    decay_rate: float = 0.35
    owner_decay_rate: float = 0.12
    catchup_max_days: int = 30
    initial_tier_days: list[int] = field(default_factory=lambda: [10, 20, 30])
    initial_tier_probs: list[float] = field(
        default_factory=lambda: [0.90, 0.55, 0.30, 0.03]
    )


@dataclass
class PrivacyConfig:
    """隐私矩阵。"""

    public_groups: list[str] = field(default_factory=list)


@dataclass
class StyleConfig:
    """风格子系统。"""

    enabled: bool = False
    application_mode: str = "disabled"
    select_mode: str = "rules"
    default_style: str = "casual"
    style_llm_max_per_day: int = 20
    style_llm_timeout_ms: int = 5000
    min_dwell_turns: int = 2
    switch_cooldown_turns: int = 3
    style_inject_max_chars: int = 1200
    total_inject_max_chars: int = 3600


@dataclass
class RecognitionConfig:
    """情绪与意图识别。"""

    enabled: bool = True
    llm_enabled: bool = False
    context_turns: int = 6
    cache_minutes: int = 10
    llm_max_per_day: int = 30
    llm_timeout_ms: int = 5000


@dataclass
class LLMConfig:
    """插件级 LLM 预算账本。"""

    total_max_per_day: int = 50
    reserve_for_purpose: int = 1


@dataclass
class PurposeConfig:
    """P4 动机子系统；默认只预览。"""

    enabled: bool = False
    delivery_mode: str = "preview"
    daily_max: int = 1


@dataclass
class DiagnosticsConfig:
    """P0 探针：把真实 Hook 载荷结构写到 runtime_dir（不写正文）。"""

    hook_payload_dump: bool = False
    max_files: int = 40
    include_text: bool = False


@dataclass
class StorageConfig:
    """磁盘闸门。"""

    budget_bytes: int = 1258291200
    warn_ratio: float = 0.85
    stop_ingest_ratio: float = 0.95
    reservation_ttl_seconds: int = 600


@dataclass
class Settings:
    """聚合配置。"""

    general: GeneralConfig = field(default_factory=GeneralConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    style: StyleConfig = field(default_factory=StyleConfig)
    recognition: RecognitionConfig = field(default_factory=RecognitionConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    purpose: PurposeConfig = field(default_factory=PurposeConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    diagnostics: DiagnosticsConfig = field(default_factory=DiagnosticsConfig)

    # 运行期校验结果（不参与序列化）
    errors: list[str] = field(default_factory=list, compare=False)
    warnings: list[str] = field(default_factory=list, compare=False)

    @property
    def memory_enabled(self) -> bool:
        """记忆写入与注入是否可用。"""

        return bool(self.general.enabled and not self.errors)

    def summary(self) -> dict[str, Any]:
        """给管理接口用的可序列化摘要。"""

        return {
            "enabled": self.general.enabled,
            "memory_enabled": self.memory_enabled,
            "owner_qq_configured": bool(self.general.owner_qq),
            "owner_platform": self.general.owner_platform,
            "owner_account": self.general.owner_account,
            "timezone": self.general.timezone,
            "dry_run": self.general.dry_run,
            "style_enabled": self.style.enabled,
            "style_application_mode": self.style.application_mode,
            "purpose_enabled": self.purpose.enabled,
            "diagnostics": self.diagnostics.hook_payload_dump,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def _as_str(value: Any, default: str) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return default
    return str(value)


def _build(cls: type, raw: Any) -> Any:
    """按 dataclass 字段把映射填充为实例，忽略未知键。"""

    if not isinstance(raw, dict):
        return cls()
    kwargs: dict[str, Any] = {}
    for item in dataclasses.fields(cls):
        if item.name not in raw:
            continue
        value = raw[item.name]
        default = item.default_factory() if item.default_factory is not dataclasses.MISSING else item.default
        kwargs[item.name] = _coerce(value, default)
    return cls(**kwargs)


def _coerce(value: Any, default: Any) -> Any:
    """按默认值类型宽松转换配置值。"""

    if isinstance(default, bool):
        return as_bool(value, default)
    if isinstance(default, int):
        return as_int(value, default)
    if isinstance(default, float):
        return as_float(value, default)
    if isinstance(default, list):
        if isinstance(value, (list, tuple)):
            return list(value)
        return default
    if isinstance(default, str):
        return _as_str(value, default)
    return value if value is not None else default


def settings_from_mapping(raw: dict[str, Any] | None) -> Settings:
    """从 ``config.toml`` 的字典构造并校验配置。"""

    data = raw if isinstance(raw, dict) else {}
    settings = Settings(
        general=_build(GeneralConfig, data.get("general")),
        memory=_build(MemoryConfig, data.get("memory")),
        privacy=_build(PrivacyConfig, data.get("privacy")),
        style=_build(StyleConfig, data.get("style")),
        recognition=_build(RecognitionConfig, data.get("recognition")),
        llm=_build(LLMConfig, data.get("llm")),
        purpose=_build(PurposeConfig, data.get("purpose")),
        storage=_build(StorageConfig, data.get("storage")),
        diagnostics=_build(DiagnosticsConfig, data.get("diagnostics")),
    )
    validate_settings(settings)
    return settings


def _validate_conversation_keys(values: Any, label: str, errors: list[str]) -> list[str]:
    """校验完整 conversationKey 列表。"""

    if not isinstance(values, list):
        errors.append(f"{label} 必须是 conversationKey 列表")
        return []
    result: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{label} 含空值")
            continue
        text = item.strip()
        parts = text.split(":")
        if len(parts) < 5:
            errors.append(f"{label} 中的 {text!r} 不是完整 conversationKey（需要 platform:account:route:chat_type:chat_id）")
            continue
        result.append(text)
    return result


def validate_settings(settings: Settings) -> None:
    """校验配置并把问题写入 ``errors/warnings``（原地修改）。"""

    errors: list[str] = []
    warnings: list[str] = []

    owner = (settings.general.owner_qq or "").strip()
    if not owner:
        errors.append("general.owner_qq 未配置：记忆写入与注入已禁用")
    elif "," in owner or "，" in owner or any(ch.isspace() for ch in owner):
        errors.append("general.owner_qq 必须是恰好一个非空 ID 字符串（不支持逗号分隔的多主人）")

    memory = settings.memory
    if memory.max_chars > 0:
        if not (memory.warn_chars <= memory.critical_chars <= memory.max_chars):
            errors.append("memory 容量约束必须满足 warn_chars <= critical_chars <= max_chars")
        if not (0 < memory.compact_target_chars <= memory.max_chars):
            errors.append("memory.compact_target_chars 必须满足 0 < compact_target <= max_chars")
    if memory.fuzzy_text_chars < 20:
        warnings.append("memory.fuzzy_text_chars 过小，fuzzy 档可能不可读")
    if memory.inject_max_chars > settings.style.total_inject_max_chars:
        errors.append("memory.inject_max_chars 不能大于 style.total_inject_max_chars")
    if memory.max_inbox_batch < 1:
        errors.append("memory.max_inbox_batch 必须 >= 1")

    if settings.style.application_mode not in {"disabled", "host", "plugin"}:
        errors.append("style.application_mode 只能是 disabled / host / plugin")
    if settings.style.select_mode not in {"rules", "llm"}:
        errors.append("style.select_mode 只能是 rules / llm")
    if settings.style.application_mode == "host" and settings.style.enabled:
        warnings.append("style.application_mode=host 依赖 P3 expression 探针，未验证时不会真正接管 Host 候选")
    if settings.style.enabled and settings.style.application_mode == "disabled":
        warnings.append("style.enabled=true 但 application_mode=disabled，不会注入风格")

    if settings.purpose.enabled and settings.purpose.delivery_mode not in {"preview", "send"}:
        errors.append("purpose.delivery_mode 只能是 preview / send")
    if settings.purpose.enabled and settings.purpose.delivery_mode == "send":
        warnings.append("P4 delivery_mode=send 属高风险路径，需要 P4 preflight 通过后再启用")

    if settings.storage.budget_bytes < 64 * 1024 * 1024:
        errors.append("storage.budget_bytes 小于 64 MiB，拒绝启动")
    if not (0.5 <= settings.storage.warn_ratio < settings.storage.stop_ingest_ratio <= 1.0):
        errors.append("storage 水位必须满足 0.5 <= warn_ratio < stop_ingest_ratio <= 1.0")

    groups = _validate_conversation_keys(settings.privacy.public_groups, "privacy.public_groups", errors)
    settings.privacy.public_groups = groups

    if settings.general.timezone.strip() == "":
        errors.append("general.timezone 不能为空")

    settings.errors = errors
    settings.warnings = warnings
