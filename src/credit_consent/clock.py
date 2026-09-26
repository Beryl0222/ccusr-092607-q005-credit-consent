"""可控时钟。

犹豫期、账单日、逾期与异议期限只能由显式推进的时钟驱动，
不允许读取宿主机墙钟，避免测试与回溯时产生不确定行为。
时钟值由账本持久化，重启后不回退、阶段不重置。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


class ClockRejected(Exception):
    """时钟只能向前推进。"""


class ControllableClock:
    def __init__(self, start: Optional[datetime] = None) -> None:
        if start is None:
            start = datetime(2026, 9, 25, 0, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        self._now = self._as_aware(start)

    @staticmethod
    def _as_aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("时钟必须携带时区")
        return value

    @property
    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        if delta <= timedelta(0):
            raise ClockRejected("时钟只能向前推进")
        self._now += delta
        return self._now

    def set_to(self, value: datetime) -> datetime:
        """从持久化检查点恢复，或显式跳到未来时刻；不允许回到过去。"""
        value = self._as_aware(value)
        if value < self._now:
            raise ClockRejected("时钟不能回退")
        self._now = value
        return self._now

    def iso(self) -> str:
        return self._now.isoformat()
