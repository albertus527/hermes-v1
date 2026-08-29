"""R2.7 §3.1 exchange-calendar abstraction (NYSE via pandas_market_calendars).

The dependency stays behind this one module so it can be replaced by the
manual override table fallback if the package misbehaves (§3.1 DECIDED).
All functions take/return plain ``datetime.date``; session close/open are
returned as timezone-aware ``America/New_York`` datetimes via zoneinfo
(never via hermes_time — wall clock is banned from decision paths).
"""

from __future__ import annotations

import datetime as _dt
from functools import lru_cache
from typing import Iterable
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CALENDAR_NAME = "NYSE"


@lru_cache(maxsize=1)
def _calendar():
    import pandas_market_calendars as mcal
    return mcal.get_calendar(CALENDAR_NAME)


@lru_cache(maxsize=16)
def _schedule(start: str, end: str):
    cal = _calendar()
    return cal.schedule(start_date=start, end_date=end)


def _session_rows(d: _dt.date):
    sched = _schedule(d.isoformat(), d.isoformat())
    return sched


def is_trading_day(d: _dt.date) -> bool:
    return len(_session_rows(d)) > 0


def is_half_day(d: _dt.date) -> bool:
    """Half-day = regular session closing at 13:00 ET (§12)."""
    sched = _session_rows(d)
    if len(sched) == 0:
        return False
    close = sched.iloc[0]["market_close"].tz_convert(ET)
    return (close.hour, close.minute) == (13, 0)


def session_open(d: _dt.date) -> _dt.datetime | None:
    sched = _session_rows(d)
    if len(sched) == 0:
        return None
    return sched.iloc[0]["market_open"].tz_convert(ET).to_pydatetime()


def session_close(d: _dt.date) -> _dt.datetime | None:
    sched = _session_rows(d)
    if len(sched) == 0:
        return None
    return sched.iloc[0]["market_close"].tz_convert(ET).to_pydatetime()


def next_trading_day(d: _dt.date) -> _dt.date:
    """First trading day strictly after ``d``."""
    cur = d + _dt.timedelta(days=1)
    # bounded scan: 16 consecutive days always contain a trading day
    sched = _schedule(cur.isoformat(), (cur + _dt.timedelta(days=16)).isoformat())
    if len(sched) == 0:
        raise ValueError(f"no trading day within 16 days after {d}")
    return sched.index[0].date()


def prev_trading_day(d: _dt.date) -> _dt.date | None:
    """Most recent trading day strictly before ``d``."""
    cur = d - _dt.timedelta(days=16)
    sched = _schedule(cur.isoformat(), (d - _dt.timedelta(days=1)).isoformat())
    if len(sched) == 0:
        return None
    return sched.index[-1].date()


def trading_sessions(start: _dt.date, end: _dt.date) -> list[_dt.date]:
    """All trading days in [start, end] inclusive."""
    if end < start:
        return []
    sched = _schedule(start.isoformat(), end.isoformat())
    return [idx.date() for idx in sched.index]


def trading_day_index(sessions: Iterable[_dt.date]) -> dict[_dt.date, int]:
    return {d: i for i, d in enumerate(sorted(sessions))}
