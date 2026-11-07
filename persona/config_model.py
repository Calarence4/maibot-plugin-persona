"""SDK 配置模型（``config_model``）。

Runner 会据此生成插件目录下的 ``config.toml`` 与 WebUI 配置 Schema；
运行期真正使用的是 :func:`persona.settings.settings_from_mapping` 转换出的 dataclass。
"""

from __future__ import annotations

from typing import Literal

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    """插件元配置节：Runner 会保留 ``plugin.config_version``。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = -1

    config_version: str = Field(
        default="0.1.0",
        description="配置版本（与插件版本同步，自动维护，不要手改）",
        json_schema_extra={"hidden": True, "disabled": True},
    )


class GeneralSection(PluginConfigBase):
    """基础身份与总开关。v1 只支持一个 primary owner。"""

    __ui_label__ = "基础"
    __ui_icon__ = "person"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="总开关")
    owner_qq: str = Field(default="", description="唯一主人 QQ 号（恰好一个非空 ID）")
    owner_platform: str = Field(default="qq", description="主人所在平台")
    owner_account: str = Field(default="", description="多账号时指定路由账号；留空不校验")
    timezone: str = Field(default="Asia/Shanghai", description="自然日与日维护使用的时区")
    dry_run: bool = Field(default=False, description="观察模式：只采集到 dry 区域，永不进入生产卡")
    bot_names: list[str] = Field(default_factory=list, description="bot 昵称/别名，用于判断群内是否直接对她说")
    collect_group_signals: bool = Field(default=True, description="群聊仅在直接对话或带情绪/事件信号时采集")


class MemorySection(PluginConfigBase):
    """记忆采集、衰减、容量与归档。"""

    __ui_label__ = "记忆"
    __ui_icon__ = "psychology"
    __ui_order__ = 1

    maintain_interval_minutes: int = Field(default=10, description="重维护（压缩/清理/备份）间隔，分钟")
    daily_maintain_hour: int = Field(default=4, description="每日衰减在本地时间几点后执行")
    max_chars: int = Field(default=60000, description="card.md 软上限；0 表示不限")
    warn_chars: int = Field(default=48000, description="容量提醒阈值")
    critical_chars: int = Field(default=54000, description="容量严重阈值")
    compact_target_chars: int = Field(default=54000, description="自动压缩目标")
    auto_compact: bool = Field(default=True, description="超过目标时自动降档压缩")
    max_records: int = Field(default=600, description="非保护记录条数软上限")
    inject_max_chars: int = Field(default=2200, description="单次记忆注入字符预算")
    fuzzy_text_chars: int = Field(default=80, description="fuzzy 档保留正文前缀字数")
    event_default_grace_hours: int = Field(default=24, description="datetime 事件的默认宽限期")
    recent_days: int = Field(default=7, description="新近记忆天数")
    fuzzy_after_days: int = Field(default=7, description="多少天后开始时间惩罚")
    forget_after_days: int = Field(default=60, description="非保护记忆最早遗忘天数")
    min_owner_importance: int = Field(default=65, description="主人重要记忆的重要度门槛")
    recall_reinforcement_enabled: bool = Field(default=False, description="可靠的再次提及上线后才开启 recall 加成")
    full_warn_bytes: int = Field(default=536870912, description="full.jsonl 单项告警线（512 MiB）")
    full_scan_bytes: int = Field(default=5242880, description="全量查询默认扫描尾部字节数（5 MiB）")
    inbox_retention_days: int = Field(default=7, description="已完全提交分片的保留天数")
    backup_daily: int = Field(default=7, description="每日备份保留份数")
    backup_weekly: int = Field(default=4, description="每周备份保留份数")
    max_inbox_batch: int = Field(default=500, description="单批归档事件上限")
    pending_reply_ttl_minutes: int = Field(default=10, description="出站回复关联入站回合的 TTL")
    recent_bonus_days: int = Field(default=7, description="新近加成窗口（天）")
    recent_bonus: int = Field(default=18, description="新近加成")
    recent_extra_days: int = Field(default=2, description="额外新近窗口（天）")
    recent_extra_bonus: int = Field(default=8, description="额外新近加成")
    decay_rate: float = Field(default=0.35, description="过期后每日 retention 惩罚")
    owner_decay_rate: float = Field(default=0.12, description="主人重要记忆的每日惩罚")
    catchup_max_days: int = Field(default=30, description="停机后最多补算天数")
    initial_tier_days: list[int] = Field(default_factory=lambda: [10, 20, 30], description="「最初」天数分档上界")
    initial_tier_probs: list[float] = Field(
        default_factory=lambda: [0.90, 0.55, 0.30, 0.03], description="「最初」各档命中概率"
    )


class PrivacySection(PluginConfigBase):
    """隐私矩阵与公开授权目标。"""

    __ui_label__ = "隐私"
    __ui_icon__ = "shield"
    __ui_order__ = 2

    public_groups: list[str] = Field(
        default_factory=list,
        description="允许 public 记忆注入的完整 conversationKey 列表（platform:account:route:chat_type:chat_id）",
    )


class StyleSection(PluginConfigBase):
    """风格子系统。"""

    __ui_label__ = "风格"
    __ui_icon__ = "style"
    __ui_order__ = 3

    enabled: bool = Field(default=False, description="是否启用风格选择")
    application_mode: Literal["disabled", "host", "plugin"] = Field(
        default="disabled", description="disabled / host / plugin；host 需 P3 探针通过"
    )
    select_mode: Literal["rules", "llm"] = Field(default="rules", description="rules 不等待网络；llm 只在候选集内单选")
    default_style: str = Field(default="casual", description="默认风格 id")
    style_llm_max_per_day: int = Field(default=20, description="风格 LLM 每日上限")
    style_llm_timeout_ms: int = Field(default=5000, description="风格 LLM 本地等待上限")
    min_dwell_turns: int = Field(default=2, description="最短停留轮数")
    switch_cooldown_turns: int = Field(default=3, description="切换冷却轮数")
    style_inject_max_chars: int = Field(default=1200, description="风格注入字符预算")
    total_inject_max_chars: int = Field(default=3600, description="记忆+风格总字符预算")


class RecognitionSection(PluginConfigBase):
    """情绪与意图识别。"""

    __ui_label__ = "识别"
    __ui_icon__ = "psychology_alt"
    __ui_order__ = 4

    enabled: bool = Field(default=True, description="启用规则识别")
    llm_enabled: bool = Field(default=False, description="启用二级 LLM 识别（后台、限额）")
    context_turns: int = Field(default=6, description="识别参考的最近轮数")
    cache_minutes: int = Field(default=10, description="LLM 结果缓存分钟")
    llm_max_per_day: int = Field(default=30, description="识别 LLM 每日上限")
    llm_timeout_ms: int = Field(default=5000, description="识别 LLM 本地等待上限")


class LLMSection(PluginConfigBase):
    """插件级 LLM 总账本。"""

    __ui_label__ = "LLM 预算"
    __ui_icon__ = "savings"
    __ui_order__ = 5

    total_max_per_day: int = Field(default=50, description="插件主动 LLM 调用每日总上限")
    reserve_for_purpose: int = Field(default=1, description="为 P4 预留的调用次数")


class PurposeSection(PluginConfigBase):
    """P4 动机子系统；默认关闭且只预览。"""

    __ui_label__ = "动机（P4）"
    __ui_icon__ = "favorite"
    __ui_order__ = 6

    enabled: bool = Field(default=False, description="启用动机子系统")
    delivery_mode: Literal["preview", "send"] = Field(default="preview", description="preview 只渲染素材，不发送")
    daily_max: int = Field(default=1, description="每日主动尝试上限")


class StorageSection(PluginConfigBase):
    """磁盘预算闸门。"""

    __ui_label__ = "存储"
    __ui_icon__ = "hard_drive"
    __ui_order__ = 7

    budget_bytes: int = Field(default=1258291200, description="总磁盘预算（约 1.2 GiB）")
    warn_ratio: float = Field(default=0.85, description="红色告警水位")
    stop_ingest_ratio: float = Field(default=0.95, description="停止新摄取的硬闸门")
    reservation_ttl_seconds: int = Field(default=600, description="写入预留过期秒数")


class DiagnosticsSection(PluginConfigBase):
    """P0 探针：记录真实 Hook 载荷结构（可选、默认关闭）。"""

    __ui_label__ = "诊断"
    __ui_icon__ = "bug_report"
    __ui_order__ = 8

    hook_payload_dump: bool = Field(default=False, description="把 Hook 载荷结构写入 runtime 目录，用于 P0 契约探针")
    max_files: int = Field(default=40, description="探针文件保留上限")
    include_text: bool = Field(default=False, description="是否在探针里保留脱敏后的文本片段（默认只保留类型与键）")


class PersonaConfig(PluginConfigBase):
    """「人格运行时」：叙事型记忆卡 + 全量存档 + 渐进披露风格。"""

    plugin: PluginSection = Field(default_factory=PluginSection, description="插件元配置")
    general: GeneralSection = Field(default_factory=GeneralSection, description="基础")
    memory: MemorySection = Field(default_factory=MemorySection, description="记忆")
    privacy: PrivacySection = Field(default_factory=PrivacySection, description="隐私")
    style: StyleSection = Field(default_factory=StyleSection, description="风格")
    recognition: RecognitionSection = Field(default_factory=RecognitionSection, description="识别")
    llm: LLMSection = Field(default_factory=LLMSection, description="LLM 预算")
    purpose: PurposeSection = Field(default_factory=PurposeSection, description="动机")
    storage: StorageSection = Field(default_factory=StorageSection, description="存储")
    diagnostics: DiagnosticsSection = Field(default_factory=DiagnosticsSection, description="诊断")
