"""本地性能基线（不依赖 Host）。

测量：600 条当前卡的 card.md 渲染、按会话 prompt 渲染、注入块组装、注入预算裁剪。
这些数字只代表本机 Python，不替代方案 §10 的 RSS/真机验收。
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

from persona.common import iso_utc, utc_now  # noqa: E402
from persona.models import normalize_record  # noqa: E402
from persona.render import render_card_markdown, render_conversation_prompt  # noqa: E402
from persona.service import PersonaService  # noqa: E402
from persona.settings import settings_from_mapping  # noqa: E402


def build_card(count: int = 600) -> dict:
    records = {}
    for index in range(count):
        record = normalize_record(
            {
                "id": f"mem_{index:05d}",
                "revision": 1,
                "conversationKey": "qq:888:*:private:10001",
                "scope": "owner",
                "importance": 60 + (index % 40),
                "title": f"记忆 {index}",
                "text": "今天发生了一件值得记住的事情，我们聊了很久，也约定下次再见面。" * 2,
                "clarity": ["vivid", "clear", "fuzzy", "trace"][index % 4],
                "createdAt": iso_utc(utc_now()),
            }
        )
        records[record["id"]] = record
    return {
        "schemaVersion": 1,
        "cardRevision": 1,
        "privacyRevision": 0,
        "records": records,
        "stats": {},
    }


def percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[index]


def main() -> None:
    settings = settings_from_mapping(
        {
            "general": {"owner_qq": "10001"},
            "memory": {"inject_max_chars": 2200, "max_chars": 60000},
            "style": {"total_inject_max_chars": 3600},
        }
    )
    card = build_card(600)

    card_times = []
    for _ in range(5):
        started = time.perf_counter()
        markdown = render_card_markdown(card, settings)
        card_times.append((time.perf_counter() - started) * 1000)
    print(f"card.md: {len(markdown)} chars, p50={statistics.median(card_times):.1f}ms, max={max(card_times):.1f}ms")

    render_times = []
    for _ in range(50):
        started = time.perf_counter()
        result = render_conversation_prompt(card, "qq:888:*:private:10001", settings, budget_chars=2200)
        render_times.append((time.perf_counter() - started) * 1000)
    print(
        "per-conversation prompt: "
        f"chars={result['chars']} ids={len(result['memoryIds'])} "
        f"p50={statistics.median(render_times):.1f}ms p95={percentile(render_times, 0.95):.1f}ms"
    )

    with tempfile.TemporaryDirectory() as temp:
        service = PersonaService(Path(temp) / "data", Path(temp) / "runtime", {"general": {"owner_qq": "10001"}})
        service.initialize()
        compose_times = []
        for _ in range(100):
            started = time.perf_counter()
            block = service._compose_injection(result["text"], "短风格文本" * 20)
            compose_times.append((time.perf_counter() - started) * 1000)
        print(
            f"compose injection: chars={len(block)} "
            f"p50={statistics.median(compose_times):.2f}ms p95={percentile(compose_times, 0.95):.2f}ms"
        )


if __name__ == "__main__":
    main()
