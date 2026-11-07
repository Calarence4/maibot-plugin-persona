from __future__ import annotations

from datetime import timedelta

from persona.constants import FALLBACK_STYLE_ID
from persona.models import normalize_record
from persona.scoring import (
    compute_initial,
    derive_title,
    retention,
    score_event,
    step_clarity,
    target_clarity,
)
from persona.settings import settings_from_mapping


def _settings():
    return settings_from_mapping({"general": {"owner_qq": "10001"}})


def test_owner_private_scores_high() -> None:
    scored = score_event(
        text="今天有点难过，想抱抱你，我们约定明天一起吃饭",
        is_owner=True,
        is_group=False,
        is_direct=True,
    )
    assert scored["importance"] >= 70
    assert "主人私聊" in scored["reasons"]
    assert "约定/承诺" in scored["reasons"]
    assert scored["kind"] in {"promise", "emotion"}


def test_first_owner_record_is_pinned_and_full_importance() -> None:
    scored = score_event(text="初次见面，请多指教", is_owner=True, is_group=False, is_direct=True, is_first_record=True)
    assert scored["importance"] == 100
    assert scored["pinned"] is True
    assert scored["kind"] == "first"


def test_short_unrelated_message_gets_penalty() -> None:
    scored = score_event(text="嗯", is_owner=False, is_group=False, is_direct=False)
    assert scored["importance"] < 8


def test_initial_tier_is_deterministic() -> None:
    first = compute_initial("mem_abc", 12)
    second = compute_initial("mem_abc", 12)
    assert first == second
    assert first["tier"] == 1
    assert first["probability"] == 0.55
    assert 0.0 <= first["probability"] <= 1.0


def test_retention_and_clarity_decay() -> None:
    settings = _settings()
    now = __import__("persona.common", fromlist=["utc_now"]).utc_now()

    recent = normalize_record({"importance": 90, "createdAt": now.isoformat(), "clarity": "vivid"})
    assert retention(recent, now=now, settings=settings) >= 80
    assert target_clarity(recent, now=now, settings=settings) == "vivid"

    old = normalize_record(
        {
            "importance": 20,
            "createdAt": (now - timedelta(days=100)).isoformat(),
            "clarity": "vivid",
        }
    )
    assert retention(old, now=now, settings=settings) < 10
    assert target_clarity(old, now=now, settings=settings) == "forgotten"
    assert step_clarity("vivid", "forgotten") == "clear"


def test_owner_important_has_clear_floor() -> None:
    settings = _settings()
    now = __import__("persona.common", fromlist=["utc_now"]).utc_now()
    record = normalize_record(
        {
            "importance": 70,
            "scope": "owner",
            "createdAt": (now - timedelta(days=200)).isoformat(),
        }
    )
    assert target_clarity(record, now=now, settings=settings) in {"clear", "vivid"}


def test_recall_does_not_change_with_exposure_without_flag() -> None:
    settings = _settings()
    record = normalize_record({"importance": 70, "recallCount": 99, "createdAt": "2026-11-07T00:00:00Z"})
    without = retention(record, settings=settings)
    record["recallCount"] = 0
    assert retention(record, settings=settings) == without


def test_title_generation() -> None:
    assert derive_title(is_owner=True, is_group=False, sender_name="主人", text="今天难过").startswith("和主人")
    assert "测试群" not in derive_title(is_owner=False, is_group=True, sender_name="", text="hi")
    assert FALLBACK_STYLE_ID == "fallback"
