"""插件级 LLM 预算账本（《方案.md》§9、§10）。

Host replyer 自身的调用不计入本账本；这里只统计插件主动发起的识别/风格/动机调用。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .common import atomic_write_json, local_day, read_json, resolve_timezone, utc_now
from .settings import Settings

LOGGER = logging.getLogger("plugin.com.calarence.persona")


class LLMLedger:
    """按配置时区自然日重置的调用计数。"""

    def __init__(self, path: Path, settings: Settings) -> None:
        self.path = path
        self.settings = settings
        self._data = self._load()

    def _today(self) -> str:
        tz = resolve_timezone(self.settings.general.timezone)
        return local_day(utc_now(), tz)

    def _load(self) -> dict[str, Any]:
        data = read_json(self.path, None)
        today = self._today()
        if not isinstance(data, dict) or str(data.get("date") or "") != today:
            return {"date": today, "counts": {}, "total": 0}
        counts = data.get("counts")
        if not isinstance(counts, dict):
            data["counts"] = {}
        data.setdefault("total", 0)
        return data

    def _rollover(self) -> None:
        today = self._today()
        if str(self._data.get("date") or "") != today:
            self._data = {"date": today, "counts": {}, "total": 0}

    def remaining(self, purpose: str, purpose_limit: int) -> int:
        """返回该用途今日剩余次数。"""

        self._rollover()
        total_limit = max(0, self.settings.llm.total_max_per_day)
        reserve = max(0, self.settings.llm.reserve_for_purpose)
        used_total = int(self._data.get("total") or 0)
        used_purpose = int((self._data.get("counts") or {}).get(purpose) or 0)
        return max(0, min(purpose_limit - used_purpose, total_limit - reserve - used_total))

    def try_consume(self, purpose: str, purpose_limit: int) -> bool:
        """扣一次预算；不足返回 ``False``，不抛异常。"""

        self._rollover()
        if self.remaining(purpose, purpose_limit) <= 0:
            LOGGER.info("LLM 预算已用尽：purpose=%s", purpose)
            return False
        counts = self._data.setdefault("counts", {})
        counts[purpose] = int(counts.get(purpose) or 0) + 1
        self._data["total"] = int(self._data.get("total") or 0) + 1
        self._data["updatedAt"] = utc_now().isoformat()
        self.save()
        return True

    def usage(self) -> dict[str, Any]:
        """返回今日使用情况。"""

        self._rollover()
        return {
            "date": self._data.get("date"),
            "counts": dict(self._data.get("counts") or {}),
            "total": int(self._data.get("total") or 0),
            "totalLimit": self.settings.llm.total_max_per_day,
            "reserve": self.settings.llm.reserve_for_purpose,
        }

    def save(self) -> None:
        """原子落盘。"""

        atomic_write_json(self.path, self._data)
