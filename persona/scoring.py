"""纯规则重要度、清晰度与衰减（《方案.md》§3.3–§3.6）。

约束：零 Embedding、零本地模型、零记忆 LLM；``importance`` 是静态基础分，
事件临近加分只在渲染时计算动态 ``recallPriority``。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from .common import clamp, parse_iso, utc_now
from .constants import (
    ABOUT_SELF_WORDS,
    CLARITY_THRESHOLDS,
    CONFLICT_WORDS,
    EMOTION_LEXICON,
    GROUP_SIGNAL_WORDS,
    IMPORTANCE_ABOUT_SELF,
    IMPORTANCE_BASE,
    IMPORTANCE_CONFLICT,
    IMPORTANCE_DIRECT,
    IMPORTANCE_EMOTION_MAX,
    IMPORTANCE_FIRST,
    IMPORTANCE_OWNER_IN_GROUP,
    IMPORTANCE_OWNER_PRIVATE,
    IMPORTANCE_PROMISE,
    IMPORTANCE_TOO_SHORT,
    INITIAL_TIER_DAYS,
    INITIAL_TIER_PROBABILITIES,
    PROMISE_WORDS,
)
from .models import better_clarity, clarity_rank, initial_hit, worse_clarity
from .settings import Settings


def _count_words(text: str, words: tuple[str, ...]) -> int:
    return sum(text.count(word) for word in words if word)


def analyze_text(text: str) -> dict[str, Any]:
    """统计情绪浓度、事件信号与长度特征。"""

    lowered = text.lower()
    hits: dict[str, int] = {}
    for emotion, words in EMOTION_LEXICON.items():
        count = sum(lowered.count(word.lower()) for word in words)
        if count:
            hits[emotion] = count
    emotion_hits = sum(hits.values())
    # 感叹号、重复字符与长度放大情绪，最多 +24。
    punctuation = min(6, text.count("!") + text.count("！"))
    repeats = min(4, _max_repeat_run(text) - 2) if _max_repeat_run(text) > 3 else 0
    emotion_score = int(clamp(emotion_hits * 6 + punctuation + repeats, 0, IMPORTANCE_EMOTION_MAX))

    promise = _count_words(text, PROMISE_WORDS) > 0
    conflict = _count_words(text, CONFLICT_WORDS) > 0
    about_self = _count_words(text, ABOUT_SELF_WORDS) > 0
    # 群聊采集信号只认「强情绪 / 事件」，平静寒暄（calm/bored）不算。
    strong_emotions = {"happy", "excited", "sad", "angry", "wronged", "shy", "nervous", "jealous"}
    group_signal = bool(set(hits) & strong_emotions) or promise or conflict or any(
        word in text for word in GROUP_SIGNAL_WORDS if word not in EMOTION_LEXICON["calm"] and word not in EMOTION_LEXICON["bored"]
    )
    return {
        "emotionScore": emotion_score,
        "emotions": sorted(hits, key=lambda key: hits[key], reverse=True),
        "emotionHits": hits,
        "promise": promise,
        "conflict": conflict,
        "aboutSelf": about_self,
        "tooShort": len(text.strip()) < 8,
        "groupSignal": group_signal,
    }


def _max_repeat_run(text: str) -> int:
    """返回最长连续重复字符长度。"""

    best = 0
    current = 0
    previous = ""
    for char in text:
        if char == previous:
            current += 1
        else:
            current = 1
            previous = char
        best = max(best, current)
    return best


def score_event(
    *,
    text: str,
    is_owner: bool,
    is_group: bool,
    is_direct: bool,
    is_first_record: bool = False,
) -> dict[str, Any]:
    """按 §3.3 表格计算静态重要度、原因标签与类型。"""

    signals = analyze_text(text)
    importance = IMPORTANCE_BASE
    reasons: list[str] = ["基础"]

    if is_owner and not is_group:
        importance += IMPORTANCE_OWNER_PRIVATE
        reasons.append("主人私聊")
    elif is_owner and is_group:
        importance += IMPORTANCE_OWNER_IN_GROUP
        reasons.append("主人在群里说话")

    if is_direct and not is_owner:
        importance += IMPORTANCE_DIRECT
        reasons.append("直接对她说")

    if signals["emotionScore"]:
        importance += int(signals["emotionScore"])
        reasons.append(f"情绪浓度 {int(signals['emotionScore'])}")

    if is_first_record:
        if is_owner:
            importance = 100
            reasons.append("最早的主人互动")
        else:
            importance += IMPORTANCE_FIRST
            reasons.append("第一次")

    if signals["promise"]:
        importance += IMPORTANCE_PROMISE
        reasons.append("约定/承诺")
    if signals["conflict"]:
        importance += IMPORTANCE_CONFLICT
        reasons.append("矛盾/道歉/和好")
    if signals["aboutSelf"]:
        importance += IMPORTANCE_ABOUT_SELF
        reasons.append("关于自己")
    if signals["tooShort"]:
        importance += IMPORTANCE_TOO_SHORT
        reasons.append("太短/信息量低")

    importance = int(clamp(importance, 1, 100))
    kind = "chat"
    if is_first_record and is_owner:
        kind = "first"
    elif signals["promise"]:
        kind = "promise"
    elif signals["conflict"]:
        kind = "conflict"
    elif signals["emotionScore"] >= 6:
        kind = "emotion"
    elif signals["aboutSelf"]:
        kind = "self"

    return {
        "importance": importance,
        "reasons": reasons,
        "kind": kind,
        "pinned": bool(is_first_record and is_owner),
        "signals": signals,
    }


def derive_title(*, is_owner: bool, is_group: bool, sender_name: str, text: str) -> str:
    """生成人看标题；不写敏感原文以外的元信息。"""

    prefix = "和主人" if is_owner else (f"群里·{sender_name}" if is_group and sender_name else ("群聊" if is_group else f"私聊·{sender_name}" if sender_name else "私聊"))
    snippet = " ".join(text.split())
    if len(snippet) > 24:
        snippet = snippet[:24] + "…"
    return f"{prefix}：{snippet}" if snippet else prefix


def initial_tier_for_days(days: int) -> tuple[int, float]:
    """按距 ``firstOwnerAt`` 的天数返回 ``(tier, probability)``。"""

    for index, threshold in enumerate(INITIAL_TIER_DAYS):
        if days < threshold:
            return index, INITIAL_TIER_PROBABILITIES[index]
    return len(INITIAL_TIER_DAYS) - 1, INITIAL_TIER_PROBABILITIES[-1]


def compute_initial(record_id: str, days_since_first_owner: int) -> dict[str, Any]:
    """用记录 id 稳定哈希决定「最初」命中，重载不漂移。"""

    tier, probability = initial_tier_for_days(max(0, days_since_first_owner))
    digest = hashlib.sha256(f"initial:{record_id}".encode("utf-8")).digest()
    draw = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return {
        "tier": tier,
        "probability": probability,
        "hit": draw < probability,
        "decidedBy": "sha256(initial:id)",
    }


def _days_between(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds() / 86400.0)


def record_activity(record: dict[str, Any]) -> datetime:
    """返回记录的最近活动时间（创建或复习）。"""

    candidates = []
    for key in ("lastRecalledAt", "createdAt", "lastBotReplyAt"):
        parsed = parse_iso(record.get(key))
        if parsed:
            candidates.append(parsed)
    return max(candidates) if candidates else utc_now()


def retention(record: dict[str, Any], *, now: datetime | None = None, settings: Settings) -> float:
    """按 §3.4 公式计算 retention，夹在 0–100。"""

    current = now or utc_now()
    memory = settings.memory
    value = float(record.get("importance") or 1)

    if memory.recall_reinforcement_enabled:
        value += min(12.0, int(record.get("recallCount") or 0) * 2.0)

    activity = record_activity(record)
    age_days = _days_between(activity, current)
    created = parse_iso(record.get("createdAt")) or activity
    created_age = _days_between(created, current)
    if created_age <= memory.recent_days:
        value += memory.recent_bonus
        if created_age <= memory.recent_extra_days:
            value += memory.recent_extra_bonus

    if age_days > memory.fuzzy_after_days:
        rate = memory.owner_decay_rate if (
            record.get("scope") == "owner" and int(record.get("importance") or 0) >= memory.min_owner_importance
        ) else memory.decay_rate
        value -= (age_days - memory.fuzzy_after_days) * rate

    return float(clamp(value, 0, 100))


def target_clarity(record: dict[str, Any], *, now: datetime | None = None, settings: Settings) -> str:
    """计算目标清晰度；保护记录有下限。"""

    current = now or utc_now()
    memory = settings.memory
    if record.get("pinned") or initial_hit(record) or record.get("kind") == "first":
        return "vivid"

    value = retention(record, now=current, settings=settings)
    computed = "forgotten"
    for clarity, threshold in CLARITY_THRESHOLDS:
        if value >= threshold:
            computed = clarity
            break

    created = parse_iso(record.get("createdAt"))
    created_age = _days_between(created, current) if created else 0.0
    floor = "trace"
    if created_age <= memory.recent_days:
        floor = "clear"
    if record.get("scope") == "owner" and int(record.get("importance") or 0) >= memory.min_owner_importance:
        floor = "clear"
    if initial_hit(record):
        floor = better_clarity(floor, "clear")

    if computed == "forgotten":
        protected = bool(record.get("pinned") or initial_hit(record) or record.get("kind") == "first")
        if protected:
            return "vivid"
        if created_age < memory.forget_after_days:
            return worse_clarity(floor, "trace")
        return "forgotten"

    return better_clarity(computed, floor)


def step_clarity(current: str, target: str, *, max_steps: int = 1) -> str:
    """每日最多升降一档。"""

    order = ["vivid", "clear", "fuzzy", "trace", "forgotten"]
    try:
        current_index = order.index(current)
        target_index = order.index(target)
    except ValueError:
        return target
    if target_index > current_index:
        return order[min(len(order) - 1, current_index + max_steps)]
    if target_index < current_index:
        return order[max(0, current_index - max_steps)]
    return current


def clarity_below(current: str, target: str) -> bool:
    """``current`` 是否比 ``target`` 更模糊。"""

    return clarity_rank(current) < clarity_rank(target)


def dynamic_priority(record: dict[str, Any], *, now: datetime | None = None, settings: Settings) -> float:
    """渲染时的动态优先级：事件临近加分，不回写基础分。"""

    current = now or utc_now()
    score = float(record.get("importance") or 1)
    event_date = parse_iso(record.get("eventDate"))
    if event_date:
        delta_days = abs((event_date - current).total_seconds()) / 86400.0
        if delta_days <= 3:
            score += 12
        elif delta_days <= 7:
            score += 6
    return score
