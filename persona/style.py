"""风格子系统（《方案.md》§4）。

- 风格目录：``styles/<style_id>/{style.json,prompt.md,reference.md}``；
- 内置兜底 id 是 ``fallback``，不是非法的 ``_fallback``；
- ``styles/index.md`` 只是选择器输入，不进入 replyer 常驻上下文；
- 默认 ``select_mode=rules``，热路径不等待网络；
- 一次只选一个风格，L1/L2 按字符预算截断。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .common import atomic_write_json, atomic_write_text, ensure_dir, iso_utc, read_json
from .constants import CASUAL_STYLE, DEFAULT_STYLE_INDEX_HEADER, FALLBACK_STYLE, FALLBACK_STYLE_ID
from .settings import Settings

LOGGER = logging.getLogger("plugin.com.calarence.persona")

_STYLE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,39}$")
_TAG_LINE_RE = re.compile(r"^\s*标签\s*[:：]\s*(.+)$")


@dataclass
class Reference:
    """一条 L2 语料。"""

    tags: list[str]
    text: str


@dataclass
class Style:
    """一个风格。"""

    id: str
    name: str = ""
    description: str = ""
    emotions: list[str] = field(default_factory=list)
    intents: list[str] = field(default_factory=list)
    scenes: list[str] = field(default_factory=list)
    priority: int = 0
    prompt: str = ""
    references: list[Reference] = field(default_factory=list)


def _parse_references(text: str) -> list[Reference]:
    """解析 ``reference.md``：空行分隔，条目首行可选 ``标签: a,b``。"""

    entries: list[Reference] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        tags: list[str] = []
        match = _TAG_LINE_RE.match(lines[0])
        if match:
            tags = [part.strip().lower() for part in re.split(r"[,，、\s]+", match.group(1)) if part.strip()]
            lines = lines[1:]
        if not lines:
            continue
        entries.append(Reference(tags=tags, text="\n".join(lines).strip()))
    return entries


def _seed_style(styles_dir: Path, payload: dict[str, Any]) -> None:
    """写入内置风格目录（已存在则不覆盖）。"""

    style_id = str(payload.get("id"))
    directory = styles_dir / style_id
    ensure_dir(directory)
    style_json = directory / "style.json"
    prompt_md = directory / "prompt.md"
    reference_md = directory / "reference.md"
    if not style_json.exists():
        atomic_write_json(
            style_json,
            {
                key: value
                for key, value in payload.items()
                if key not in {"prompt", "reference"}
            },
        )
    if not prompt_md.exists():
        atomic_write_text(prompt_md, str(payload.get("prompt") or "") + "\n")
    if not reference_md.exists():
        atomic_write_text(reference_md, "\n".join(str(item) for item in payload.get("reference") or []) + "\n")


def ensure_builtin_styles(styles_dir: Path) -> None:
    """首次运行时生成 ``fallback`` 与 ``casual`` 两个内置风格。"""

    ensure_dir(styles_dir)
    for payload in (FALLBACK_STYLE, CASUAL_STYLE):
        try:
            _seed_style(styles_dir, payload)
        except OSError as exc:
            LOGGER.warning("初始化内置风格 %s 失败: %s", payload.get("id"), exc)


def load_styles(styles_dir: Path) -> dict[str, Style]:
    """加载全部合法风格；坏文件只跳过并告警。"""

    styles: dict[str, Style] = {}
    if not styles_dir.exists():
        return styles
    for directory in sorted(styles_dir.iterdir()):
        if not directory.is_dir():
            continue
        style_json = directory / "style.json"
        raw = read_json(style_json, None)
        if not isinstance(raw, dict):
            LOGGER.warning("风格 %s 的 style.json 缺失或损坏，已跳过", directory.name)
            continue
        style_id = str(raw.get("id") or directory.name).strip()
        if not _STYLE_ID_RE.match(style_id):
            LOGGER.warning("风格 id %r 不合法（只允许小写字母/数字/连字符），已跳过", style_id)
            continue
        prompt_path = directory / "prompt.md"
        prompt = ""
        if prompt_path.exists():
            try:
                prompt = prompt_path.read_text(encoding="utf-8").strip()
            except OSError:
                prompt = ""
        prompt = prompt or str(raw.get("prompt") or "")
        references: list[Reference] = []
        reference_path = directory / "reference.md"
        if reference_path.exists():
            try:
                references = _parse_references(reference_path.read_text(encoding="utf-8"))
            except OSError:
                references = []
        styles[style_id] = Style(
            id=style_id,
            name=str(raw.get("name") or style_id),
            description=str(raw.get("description") or ""),
            emotions=[str(item) for item in raw.get("emotions") or []],
            intents=[str(item) for item in raw.get("intents") or []],
            scenes=[str(item) for item in raw.get("scenes") or []],
            priority=int(raw.get("priority") or 0),
            prompt=prompt,
            references=references,
        )
    if FALLBACK_STYLE_ID not in styles:
        styles[FALLBACK_STYLE_ID] = Style(
            id=FALLBACK_STYLE_ID,
            name="自然",
            emotions=list(FALLBACK_STYLE["emotions"]),
            intents=list(FALLBACK_STYLE["intents"]),
            scenes=["any"],
            priority=0,
            prompt=str(FALLBACK_STYLE["prompt"]),
            references=_parse_references("\n\n".join(str(item) for item in FALLBACK_STYLE["reference"])),
        )
    return styles


def render_index(styles: dict[str, Style]) -> str:
    """生成选择器索引（L0），永不进入 replyer 上下文。"""

    lines = [DEFAULT_STYLE_INDEX_HEADER.rstrip(), ""]
    for style in sorted(styles.values(), key=lambda item: (-item.priority, item.id)):
        lines.append(
            "| {id} | {name} | {emotions} | {intents} | {scenes} | {priority} |".format(
                id=style.id,
                name=style.name,
                emotions=",".join(style.emotions),
                intents=",".join(style.intents),
                scenes=",".join(style.scenes),
                priority=style.priority,
            )
        )
    lines.append("")
    return "\n".join(lines)


def default_style_id(styles: dict[str, Style], settings: Settings) -> str:
    """选择默认风格；配置缺失时依次退回 fallback。"""

    if settings.style.default_style in styles:
        return settings.style.default_style
    if "casual" in styles:
        return "casual"
    return FALLBACK_STYLE_ID if FALLBACK_STYLE_ID in styles else next(iter(styles), FALLBACK_STYLE_ID)


def _state_for(state: dict[str, Any], conversation_key: str) -> dict[str, Any]:
    value = state.get(conversation_key)
    if not isinstance(value, dict):
        value = {}
        state[conversation_key] = value
    return value


def select_style(
    *,
    styles: dict[str, Style],
    style_state: dict[str, Any],
    conversation_key: str,
    emotion: str = "",
    intent: str = "",
    scene: str = "",
    settings: Settings,
    turn_delta: int = 1,
) -> tuple[str, str]:
    """rules 模式选择风格，带最短停留与切换冷却。

    返回 ``(style_id, 原因)``。
    """

    state = _state_for(style_state, conversation_key)
    current = str(state.get("styleId") or "")
    total_turns = int(state.get("totalTurns") or 0) + max(0, turn_delta)
    turns_in_style = int(state.get("turnsInStyle") or 0) + max(0, turn_delta)
    last_switch = int(state.get("lastSwitchTurn") or 0)

    if not settings.style.enabled:
        return "", "风格未启用"

    if not current or current not in styles:
        current = default_style_id(styles, settings)
        turns_in_style = 0
        last_switch = total_turns
        state.update({"styleId": current, "turnsInStyle": turns_in_style, "lastSwitchTurn": last_switch})

    candidates = [
        style
        for style in styles.values()
        if (not style.emotions or (emotion and emotion in style.emotions))
        and (not style.intents or (intent and intent in style.intents))
        and (not style.scenes or scene in style.scenes or "any" in style.scenes or not scene)
    ]
    if not candidates:
        # 没有明确情绪/意图时保持当前或默认风格，避免被高优先级专用风格“抢走”。
        fallback_id = default_style_id(styles, settings)
        for candidate_id in (current, fallback_id, FALLBACK_STYLE_ID):
            if candidate_id in styles:
                candidates = [styles[candidate_id]]
                break
    if not candidates:
        candidates = list(styles.values())
    best = max(candidates, key=lambda style: (style.priority, style.id == current))
    reason = "规则匹配"

    if best.id != current:
        if turns_in_style < settings.style.min_dwell_turns:
            best = styles.get(current, best)
            reason = f"最短停留未满（{turns_in_style}/{settings.style.min_dwell_turns}）"
        elif total_turns - last_switch < settings.style.switch_cooldown_turns:
            best = styles.get(current, best)
            reason = f"切换冷却中（{total_turns - last_switch}/{settings.style.switch_cooldown_turns}）"
        else:
            last_switch = total_turns
            turns_in_style = 0
            reason = f"切换到优先级更高的 {best.id}"

    state.update(
        {
            "styleId": best.id,
            "totalTurns": total_turns,
            "turnsInStyle": turns_in_style + (1 if best.id == current else 0),
            "lastSwitchTurn": last_switch,
            "updatedAt": iso_utc(),
        }
    )
    return best.id, reason


def select_references(style: Style, *, scene: str = "", emotion: str = "", intent: str = "", limit: int = 3) -> list[str]:
    """按 scene/tag 选 2–3 条语料；无匹配才按文件顺序。"""

    tags = {tag for tag in (scene, emotion, intent) if tag}
    matched = [ref for ref in style.references if tags & set(ref.tags)]
    if not matched and tags:
        matched = [ref for ref in style.references if not ref.tags]
    if not matched:
        matched = list(style.references)
    selected = [ref.text for ref in matched[: max(1, limit)]]
    return selected


def render_style_block(
    style: Style,
    settings: Settings,
    *,
    scene: str = "",
    emotion: str = "",
    intent: str = "",
    budget_chars: int | None = None,
) -> str:
    """渲染 L1 + L2 风格块；不含风格名称与解释。"""

    if not settings.style.enabled or settings.style.application_mode != "plugin":
        return ""
    budget = int(budget_chars if budget_chars is not None else settings.style.style_inject_max_chars)
    parts: list[str] = []
    if style.prompt.strip():
        parts.append(style.prompt.strip())
    for reference in select_references(style, scene=scene, emotion=emotion, intent=intent):
        parts.append(reference.strip())
    text = "\n".join(part for part in parts if part).strip()
    if not text:
        return ""
    if len(text) > budget:
        text = text[: max(0, budget - 1)] + "…"
    return text


def style_state_signature(style_state: dict[str, Any], conversation_key: str) -> tuple[str, int]:
    """返回会话风格状态指纹，用于判断是否需要重渲染。"""

    state = style_state.get(conversation_key)
    if not isinstance(state, dict):
        return ("", 0)
    return (str(state.get("styleId") or ""), int(state.get("lastSwitchTurn") or 0))
