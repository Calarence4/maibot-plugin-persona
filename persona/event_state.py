"""事件日期的统一状态函数（《方案.md》§3.8）。

所有渲染、排序与验收都必须调用 :func:`event_state`，不得各写一套判断。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any

from .common import parse_iso, utc_now


@dataclass(frozen=True)
class EventState:
    """事件状态与动态优先级素材。"""

    state: str  # upcoming / current / expired / none
    start: datetime | None = None
    end: datetime | None = None
    precision: str = ""

    @property
    def is_visible(self) -> bool:
        """默认是否允许自动注入。"""

        return self.state in {"upcoming", "current", "none"}


def event_state(record: dict[str, Any], *, now: datetime | None = None, tz: tzinfo, default_grace_hours: int = 24) -> EventState:
    """计算单条记录的事件状态。

    - ``day``：整个自然日为 current，次日 00:00 起 expired；
    - ``datetime``：开始前 upcoming，开始后 current，到 ``validUntil`` 或默认宽限期后 expired；
    - 无 ``eventDate``：none，正常参与渲染。
    """

    current = now or utc_now()
    raw_date = record.get("eventDate")
    parsed = parse_iso(raw_date)
    if parsed is None and isinstance(raw_date, str):
        try:
            day = datetime.strptime(raw_date.strip()[:10], "%Y-%m-%d").replace(tzinfo=tz)
        except ValueError:
            day = None
        if day is not None:
            start = day
            end = day + timedelta(days=1)
            if current < start:
                return EventState("upcoming", start, end, "day")
            if current < end:
                return EventState("current", start, end, "day")
            return EventState("expired", start, end, "day")

    if parsed is None:
        return EventState("none", None, None, "")

    precision = str(record.get("eventDatePrecision") or "datetime")
    if precision == "day":
        start = parsed
        end = start + timedelta(days=1)
        if current < start:
            return EventState("upcoming", start, end, "day")
        if current < end:
            return EventState("current", start, end, "day")
        return EventState("expired", start, end, "day")

    start = parsed
    valid_until = parse_iso(record.get("validUntil"))
    end = valid_until or (start + timedelta(hours=max(0, default_grace_hours)))
    if current < start:
        return EventState("upcoming", start, end, "datetime")
    if current < end:
        return EventState("current", start, end, "datetime")
    return EventState("expired", start, end, "datetime")


def event_state_for_day(record: dict[str, Any], *, now: datetime | None, tz: tzinfo) -> str:
    """只返回状态字符串的便捷函数。"""

    return event_state(record, now=now, tz=tz).state


def sort_priority(record: dict[str, Any], *, now: datetime | None, tz: tzinfo, state: EventState | None = None) -> float:
    """计算事件排序优先级；临近加分只在这里出现，不回写基础分。"""

    current = now or utc_now()
    base = float(record.get("importance") or 1)
    resolved = state or event_state(record, now=current, tz=tz)
    if resolved.start:
        delta_days = abs((resolved.start - current).total_seconds()) / 86400.0
        if delta_days <= 3:
            base += 12
        elif delta_days <= 7:
            base += 6
    if resolved.state == "upcoming" and resolved.start:
        hours = (resolved.start - current).total_seconds() / 3600.0
        if hours <= 24:
            base += 8
        elif hours <= 72:
            base += 4
    if resolved.state == "expired":
        base -= 20
    return base
