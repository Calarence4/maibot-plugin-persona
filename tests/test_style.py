from __future__ import annotations

from persona.settings import settings_from_mapping
from persona.style import (
    Reference,
    Style,
    load_styles,
    render_index,
    render_style_block,
    select_references,
    select_style,
)


def _styles() -> dict[str, Style]:
    return {
        "fallback": Style(id="fallback", priority=0, prompt="自然回复。", scenes=["any"]),
        "casual": Style(id="casual", priority=10, prompt="日常。", intents=["chat"], scenes=["private"]),
        "angry": Style(
            id="angry",
            priority=50,
            prompt="短促、带刺。",
            emotions=["angry"],
            intents=["rant"],
            scenes=["private"],
            references=[Reference(tags=["angry"], text="行吧，随便你。")],
        ),
    }


def _settings(**style):
    base = {"enabled": True, "application_mode": "plugin", "select_mode": "rules", "default_style": "casual"}
    base.update(style)
    return settings_from_mapping(
        {
            "general": {"owner_qq": "10001"},
            "style": base,
        }
    )


def test_rules_selection_respects_min_dwell_and_cooldown() -> None:
    settings = _settings()
    styles = _styles()
    state: dict = {}
    conversation = "qq:888:*:private:10001"

    first, _ = select_style(
        styles=styles,
        style_state=state,
        conversation_key=conversation,
        emotion="angry",
        intent="rant",
        scene="private",
        settings=settings,
    )
    assert first == "casual"

    second, reason = select_style(
        styles=styles,
        style_state=state,
        conversation_key=conversation,
        emotion="angry",
        intent="rant",
        scene="private",
        settings=settings,
    )
    assert second == "casual"
    assert "最短停留" in reason or "冷却" in reason

    third, _ = select_style(
        styles=styles,
        style_state=state,
        conversation_key=conversation,
        emotion="angry",
        intent="rant",
        scene="private",
        settings=settings,
    )
    assert third == "casual"

    # 最短停留 2 轮 + 切换冷却 3 轮：第 4 次才允许切换
    fourth, reason = select_style(
        styles=styles,
        style_state=state,
        conversation_key=conversation,
        emotion="angry",
        intent="rant",
        scene="private",
        settings=settings,
    )
    assert fourth == "angry"
    assert "切换" in reason


def test_style_disabled_selects_nothing() -> None:
    settings = _settings(enabled=False)
    style_id, reason = select_style(
        styles=_styles(),
        style_state={},
        conversation_key="qq:888:*:private:10001",
        settings=settings,
    )
    assert style_id == ""
    assert "未启用" in reason


def test_render_style_block_has_budget_and_no_style_name() -> None:
    settings = _settings(style_inject_max_chars=20)
    style = Style(id="angry", prompt="这是一段非常长的风格描述，需要被预算截断。" * 3)
    block = render_style_block(style, settings, budget_chars=20)
    assert len(block) <= 20
    assert "angry" not in block


def test_reference_selection_matches_scene_then_falls_back() -> None:
    style = Style(
        id="x",
        references=[
            Reference(tags=["group"], text="群聊语料"),
            Reference(tags=["angry"], text="生气语料"),
            Reference(tags=[], text="通用语料"),
        ],
    )
    assert select_references(style, scene="group", limit=2) == ["群聊语料"]
    assert select_references(style, scene="unknown", limit=2)[0] == "通用语料"


def test_load_styles_skips_invalid_id(tmp_path) -> None:
    styles_dir = tmp_path / "styles"
    (styles_dir / "Bad_ID").mkdir(parents=True)
    (styles_dir / "Bad_ID" / "style.json").write_text('{"id": "Bad_ID"}', encoding="utf-8")
    styles = load_styles(styles_dir)
    assert "Bad_ID" not in styles
    assert "fallback" in styles

    (styles_dir / "good-style").mkdir()
    (styles_dir / "good-style" / "style.json").write_text(
        '{"id": "good-style", "name": "好风格", "priority": 5}', encoding="utf-8"
    )
    (styles_dir / "good-style" / "prompt.md").write_text("说人话。", encoding="utf-8")
    (styles_dir / "good-style" / "reference.md").write_text("标签: chat\n嗯。", encoding="utf-8")
    loaded = load_styles(styles_dir)
    assert loaded["good-style"].priority == 5
    assert loaded["good-style"].references[0].tags == ["chat"]
    index = render_index(loaded)
    assert "good-style" in index
