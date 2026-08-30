"""Market-data provider interface (R2.7 §3.1–§3.5).

The deterministic core never talks to providers directly; callers build
point-in-time snapshots through this ABC. Two planned implementations:

- SQLite historical store (backtest, Phase 0+)
- Alpaca REST adapter (live, future; with the §3.2 15-minute boundary retry)

All decision-consumed bars must satisfy feed == 'sip' (§3.5; enforced by
backtest/feed_parity.py).
"""

from __future__ import annotations

import datetime as _dt
from abc import ABC, abstractmethod
from typing import Sequence

from trading_core.types import Bar, DailyBar


class MarketDataProvider(ABC):
    """Bars in the three §3.3 series: split (signal), raw (executable),
    total (accounting, reporting-only)."""

    @abstractmethod
    def get_daily_bars(
        self, ticker: str, *, adjustment: str,
        start: _dt.date, end: _dt.date,
    ) -> list[DailyBar]:
        """Daily bars in [start, end] inclusive."""

    @abstractmethod
    def get_minute_bars(
        self, ticker: str, *, session: _dt.date, adjustment: str,
        first_label: str = "09:30", last_label: str = "15:59",
    ) -> dict[str, Bar]:
        """1-min bars for one session keyed by bar-start label (N-01)."""

    def feed(self) -> str:
        return "sip"
