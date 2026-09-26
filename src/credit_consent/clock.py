"""可控时钟。

犹豫期、账单日、逾期与异议期限全部由该时钟驱动；
生产环境可接入真实时间源，测试中可任意推进而不影响事件历史。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class ControlledClock:
    _now: datetime

    def __init__(self, start: datetime | None = None) -> None:
        if start is None:
            start = datetime(2026, 9, 26, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        if start.tzinfo is None:
            raise ValueError("时钟起点必须携带时区")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def now_iso(self) -> str:
        return self._now.isoformat()

    def advance(self, delta: timedelta) -> datetime:
        self._now += delta
        return self._now

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("时钟设置必须携带时区")
        self._now = value
