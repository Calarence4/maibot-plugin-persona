from __future__ import annotations

from datetime import timedelta

from persona.common import iso_utc, utc_now
from persona.models import normalize_record
from persona.render import (
    card_block_lengths,
    render_conversation_prompt,
    render_record_block,
)
from persona.settings import settings_from_mapping

OWNER = "qq:888:*:private:10001"
PRIVATE = "qq:888:*:private:20002"
GROUP_A = "qq:888:*:group:30001"
GROUP_B = "qq:888:*:group:30002"


def _settings(**overrides):
    base = {"general": {"owner_qq": "10001"}, "privacy": {"public_groups": []}}
    base.update(overrides)
    return settings_from_mapping(base)


def _record(record_id: str, conversation: str, **overrides) -> dict:
    record = normalize_record(
        {
            "id": record_id,
            "revision": 1,
            "conversationKey": conversation,
            "scope": "private",
            "importance": 70,
            "text": "这是一条足够长的记忆正文，用来测试渲染档位与预算裁剪。",
            "title": "测试记忆",
            "clarity": "vivid",
            "createdAt": iso_utc(utc_now()),
        }
    )
    record.update(overrides)
    return record


def _card(records: list[dict]) -> dict:
    return {
        "schemaVersion": 1,
        "cardRevision": 3,
        "privacyRevision": 0,
        "records": {record["id"]: record for record in records},
        "stats": {},
        "firstOwnerAt": None,
    }


def test_each_clarity_actually_shortens_text() -> None:
    settings = _settings()
    record = _record(
        "mem_1",
        OWNER,
        scope="owner",
        text="这是一段很长的正文。" * 10,
        botReplies=[
            {"text": "很长很长的回复内容" * 5, "status": "sent"},
            {"text": "第二条回复也相当长" * 5, "status": "sent"},
            {"text": "第三条回复同样很长" * 5, "status": "sent"},
        ],
    )
    vivid = render_record_block(record, "vivid", settings)
    clear = render_record_block(record, "clear", settings)
    fuzzy = render_record_block(record, "fuzzy", settings)
    trace = render_record_block(record, "trace", settings)
    assert len(vivid) > len(clear) > len(fuzzy) > len(trace)


def test_owner_memory_never_in_group() -> None:
    settings = _settings()
    card = _card([_record("mem_owner", OWNER, scope="owner", text="主人私聊秘密")])
    owner = render_conversation_prompt(card, OWNER, settings)
    group = render_conversation_prompt(card, GROUP_A, settings)
    assert "主人私聊秘密" in owner["text"]
    assert group["text"] == ""
    assert group["memoryIds"] == []


def test_private_memory_only_in_same_conversation() -> None:
    settings = _settings()
    card = _card([_record("mem_private", PRIVATE, text="普通私聊秘密")])
    same = render_conversation_prompt(card, PRIVATE, settings)
    other = render_conversation_prompt(card, OWNER, settings)
    assert "普通私聊秘密" in same["text"]
    assert other["text"] == ""


def test_group_memory_only_in_source_group() -> None:
    settings = _settings()
    card = _card([_record("mem_group", GROUP_A, scope="group", text="A群的事情")])
    a = render_conversation_prompt(card, GROUP_A, settings)
    b = render_conversation_prompt(card, GROUP_B, settings)
    assert "A群的事情" in a["text"]
    assert b["text"] == ""


def test_public_requires_target_and_config_allowlist() -> None:
    settings = settings_from_mapping(
        {
            "general": {"owner_qq": "10001"},
            "privacy": {"public_groups": [GROUP_B]},
        }
    )
    public = _record(
        "mem_public",
        GROUP_A,
        scope="public",
        text="可以公开的事情",
        allowedConversationKeys=[GROUP_B],
        authorizedBy="10001",
        authorizedAt=iso_utc(),
    )
    allowed = render_conversation_prompt(_card([public]), GROUP_B, settings)
    source = render_conversation_prompt(_card([public]), GROUP_A, settings)
    assert "可以公开的事情" in allowed["text"]
    assert source["text"] == ""

    settings_empty = settings_from_mapping({"general": {"owner_qq": "10001"}, "privacy": {"public_groups": []}})
    assert render_conversation_prompt(_card([public]), GROUP_B, settings_empty)["text"] == ""


def test_group_render_redacts_cross_domain_ids() -> None:
    settings = _settings()
    record = _record(
        "mem_group",
        GROUP_A,
        scope="group",
        text="我的QQ是1234567，链接 https://example.com/x",
    )
    result = render_conversation_prompt(_card([record]), GROUP_A, settings)
    assert "1234567" not in result["text"]
    assert "https://example.com" not in result["text"]


def test_budget_is_hard_and_never_exceeds() -> None:
    settings = settings_from_mapping(
        {
            "general": {"owner_qq": "10001"},
            "memory": {"inject_max_chars": 600},
            "style": {"total_inject_max_chars": 900},
        }
    )
    records = [
        _record(f"mem_{index}", OWNER, scope="owner", text="长记忆内容。" * 40, importance=70 + index)
        for index in range(30)
    ]
    result = render_conversation_prompt(_card(records), OWNER, settings, budget_chars=600)
    assert result["chars"] <= 600
    assert result["memoryIds"]
    assert result["truncated"] is True


def test_expired_event_not_auto_injected() -> None:
    settings = _settings()
    old = (utc_now() - timedelta(days=30)).strftime("%Y-%m-%d")
    record = _record(
        "mem_event",
        OWNER,
        scope="owner",
        text="过期的事件",
        eventDate=old,
        eventDatePrecision="day",
    )
    assert render_conversation_prompt(_card([record]), OWNER, settings)["text"] == ""

    record["pinned"] = True
    assert "过期的事件" in render_conversation_prompt(_card([record]), OWNER, settings)["text"]


def test_block_lengths_cover_all_clarities() -> None:
    lengths = card_block_lengths(_record("mem_1", OWNER, scope="owner"), _settings())
    assert set(lengths) == {"vivid", "clear", "fuzzy", "trace", "forgotten"}
    assert lengths["vivid"] > lengths["trace"] > 0
    assert lengths["forgotten"] == 0
