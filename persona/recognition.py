"""用户情绪 / 意图识别（《方案.md》§5）。

一级只做确定性词典与标点/重复字符/句长规则；二级 LLM 仅在规则冲突、连续未识别或
低置信时后台调用一次，同会话上下文哈希缓存 10 分钟。失败保留带年龄的上一状态并标记
``recognition_failed``，绝不伪造「新平静状态」。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from .common import iso_utc, parse_iso, read_json, utc_now
from .constants import EMOTION_LEXICON, INTENTS
from .sanitize import fingerprint
from .settings import Settings

LOGGER = logging.getLogger("plugin.com.calarence.persona")

_INTENT_ORDER = list(INTENTS)
_QUESTION_RE = re.compile(r"[?？]")
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class Recognition:
    """单轮识别结果。"""

    emotion: str
    intent: str
    emotion_confidence: float
    intent_confidence: float
    failed: bool = False
    needs_llm: bool = False


def _score_emotions(text: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    for emotion, words in EMOTION_LEXICON.items():
        hits = sum(text.count(word) for word in words)
        if hits:
            scores[emotion] = float(hits * 2)
    exclamations = text.count("!") + text.count("！")
    if exclamations:
        for emotion in ("excited", "angry"):
            if emotion in scores:
                scores[emotion] += min(3.0, exclamations)
    return scores


def _score_intents(text: str) -> dict[str, float]:
    from .constants import INTENT_LEXICON

    scores: dict[str, float] = {}
    for intent, words in INTENT_LEXICON.items():
        hits = sum(text.count(word) for word in words)
        if hits:
            scores[intent] = float(hits * 2)
    if _QUESTION_RE.search(text):
        scores["question"] = scores.get("question", 0.0) + 3.0
    return scores


def classify_rules(text: str, previous: dict[str, Any] | None = None) -> Recognition:
    """纯规则一级识别。"""

    emotion_scores = _score_emotions(text)
    intent_scores = _score_intents(text)

    if emotion_scores:
        ordered = sorted(emotion_scores.items(), key=lambda item: item[1], reverse=True)
        emotion, top = ordered[0]
        second = ordered[1][1] if len(ordered) > 1 else 0.0
        confidence = min(1.0, top / 6.0)
        conflict = (top - second) < 0.5
    else:
        emotion = str((previous or {}).get("emotion") or "calm")
        confidence = 0.2
        conflict = True

    if intent_scores:
        ordered_intents = sorted(intent_scores.items(), key=lambda item: item[1], reverse=True)
        intent, top_intent = ordered_intents[0]
        intent_confidence = min(1.0, top_intent / 5.0)
    else:
        intent = str((previous or {}).get("intent") or "chat")
        intent_confidence = 0.2

    unknown_streak = int((previous or {}).get("unknownStreak") or 0)
    if not emotion_scores:
        unknown_streak += 1
    else:
        unknown_streak = 0

    return Recognition(
        emotion=emotion,
        intent=intent,
        emotion_confidence=round(confidence, 3),
        intent_confidence=round(intent_confidence, 3),
        needs_llm=conflict or confidence < 0.4 or unknown_streak >= 3,
    )


class RecognitionEngine:
    """按 conversationKey 隔离的识别状态。"""

    def __init__(self, path, settings: Settings) -> None:
        self.path = path
        self.settings = settings
        self.state: dict[str, Any] = self._load()
        self._llm_cache: dict[str, tuple[str, dict[str, Any]]] = {}

    def _load(self) -> dict[str, Any]:
        data = read_json(self.path, None)
        return data if isinstance(data, dict) else {}

    def get(self, conversation_key: str) -> dict[str, Any]:
        """返回会话当前识别状态（可能为空）。"""

        value = self.state.get(conversation_key)
        return value if isinstance(value, dict) else {}

    def observe(self, conversation_key: str, text: str, *, now: datetime | None = None) -> dict[str, Any]:
        """在入站时更新识别状态；返回最新状态。"""

        current = now or utc_now()
        previous = self.get(conversation_key)
        recognition = classify_rules(text, previous)
        turns = [item for item in previous.get("turns") or [] if isinstance(item, dict)]
        turns.append(
            {
                "emotion": recognition.emotion,
                "intent": recognition.intent,
                "at": iso_utc(current),
            }
        )
        turns = turns[-3:]
        state = {
            "emotion": recognition.emotion,
            "intent": recognition.intent,
            "confidence": recognition.emotion_confidence,
            "intentConfidence": recognition.intent_confidence,
            "needsLlm": recognition.needs_llm,
            "recognitionFailed": False,
            "unknownStreak": 0 if recognition.emotion_confidence >= 0.4 else int(previous.get("unknownStreak") or 0) + 1,
            "turns": turns,
            "updatedAt": iso_utc(current),
            "textHash": fingerprint(text),
        }
        self.state[conversation_key] = state
        return state

    def mark_failed(self, conversation_key: str) -> None:
        """识别失败：保留上一状态并标记，不伪造新状态。"""

        state = self.get(conversation_key)
        if not state:
            return
        state["recognitionFailed"] = True
        state["failedAt"] = iso_utc()
        self.state[conversation_key] = state

    def context_hash(self, conversation_key: str) -> str:
        """用于 LLM 缓存的上下文哈希。"""

        state = self.get(conversation_key)
        parts = [str(state.get("textHash") or ""), str(state.get("emotion") or ""), str(state.get("intent") or "")]
        return fingerprint("|".join(parts))

    def cached_llm(self, conversation_key: str) -> dict[str, Any] | None:
        """10 分钟内命中缓存返回结果。"""

        cached = self._llm_cache.get(conversation_key)
        if not cached:
            return None
        stamp, value = cached
        if (utc_now() - (parse_iso(stamp) or utc_now())).total_seconds() > self.settings.recognition.cache_minutes * 60:
            self._llm_cache.pop(conversation_key, None)
            return None
        return value

    def store_llm(self, conversation_key: str, value: dict[str, Any]) -> None:
        self._llm_cache[conversation_key] = (iso_utc(), value)

    async def refine_with_llm(
        self,
        conversation_key: str,
        text: str,
        generate: Callable[[str], Awaitable[dict[str, Any]]],
        *,
        timeout_ms: int | None = None,
    ) -> dict[str, Any] | None:
        """按需调用一次 LLM；超时只停止本地等待，不立即重试。"""

        if not self.settings.recognition.llm_enabled:
            return None
        cached = self.cached_llm(conversation_key)
        if cached is not None:
            return cached
        prompt = (
            "你是情绪/意图分类器。只输出 JSON："
            '{"emotion":"happy|excited|calm|sad|angry|wronged|shy|nervous|bored|jealous",'
            '"intent":"chat|greeting|question|comfort|encourage|tease|rant|coquetry|refute|explain|apologize|care|complain|close",'
            '"confidence":0.0}\n'
            f"文本：{text[:1000]}"
        )
        timeout = (timeout_ms or self.settings.recognition.llm_timeout_ms) / 1000.0
        try:
            result = await asyncio.wait_for(generate(prompt), timeout=timeout)
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001 - 识别失败必须降级
            LOGGER.warning("识别 LLM 失败/超时，保留上一状态: %s", exc)
            self.mark_failed(conversation_key)
            return None
        parsed = self._parse_llm(result)
        if parsed is None:
            self.mark_failed(conversation_key)
            return None
        self.store_llm(conversation_key, parsed)
        state = self.get(conversation_key)
        state.update(
            {
                "emotion": parsed["emotion"],
                "intent": parsed["intent"],
                "confidence": parsed["confidence"],
                "needsLlm": False,
                "recognitionFailed": False,
                "updatedAt": iso_utc(),
            }
        )
        self.state[conversation_key] = state
        return parsed

    @staticmethod
    def _parse_llm(result: Any) -> dict[str, Any] | None:
        text = ""
        if isinstance(result, dict):
            text = str(result.get("response") or result.get("content") or "")
        elif isinstance(result, str):
            text = result
        match = _JSON_RE.search(text)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        emotion = str(payload.get("emotion") or "")
        intent = str(payload.get("intent") or "")
        try:
            confidence = float(payload.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if not emotion or not intent:
            return None
        return {"emotion": emotion, "intent": intent, "confidence": max(0.0, min(1.0, confidence))}

    def signature(self, conversation_key: str) -> tuple[str, str, str]:
        """会话识别指纹。"""

        state = self.get(conversation_key)
        return (
            str(state.get("emotion") or ""),
            str(state.get("intent") or ""),
            str(state.get("updatedAt") or ""),
        )


def prompt_for_style_index(styles_table: str) -> str:
    """构造 LLM 风格选择提示词（仅在 select_mode=llm 时使用）。"""

    return (
        "你是回复风格选择器。根据索引和当前对话，从候选中单选一个风格。\n"
        '只输出 JSON：{"style_id":"...","confidence":0.0}\n\n'
        f"{styles_table}"
    )
