"""Shared primitives for the R2.7 deterministic core.

These types describe the *point-in-time snapshot* inputs consumed by the
pure decision functions (spec §14.2). None of them perform I/O; providers
(backtest/data/) build them from storage.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Series / adjustment vocabulary (§3.3)
# ---------------------------------------------------------------------------

ADJUSTMENT_SPLIT = "split"          # signal series
ADJUSTMENT_RAW = "raw"              # executable series (unadjusted)
ADJUSTMENT_TOTAL = "total"          # accounting series (reporting-only)

FEED_SIP = "sip"                    # §3.5 feed parity: decision bars are SIP

TIMEFRAME_1MIN = "1Min"
TIMEFRAME_1DAY = "1Day"

# Exit-evaluating scans (§8.6, §12)
SCAN_MAIN_1000 = "10:00"
SCAN_PRECLOSE_1530 = "15:30"
SCAN_HALFDAY_1200 = "12:00"
EXIT_SCAN_LABELS = (SCAN_MAIN_1000, SCAN_PRECLOSE_1530, SCAN_HALFDAY_1200)

# §19 item 2 / P-1 required entry-side executable 1-min bar labels
REQUIRED_OPENING_RANGE_LABELS = tuple(f"09:{m:02d}" for m in range(30, 40))
REQUIRED_EVALUATION_LABEL = "09:44"
REQUIRED_ENTRY_BAR_LABELS = REQUIRED_OPENING_RANGE_LABELS + (REQUIRED_EVALUATION_LABEL,)
# VWAP / session pace are computed over returned bars in this inclusive range
P1_WINDOW_FIRST_LABEL = "09:30"
P1_WINDOW_LAST_LABEL = "09:44"

# Regimes (§5.3)
REGIME_RISK_ON = "RISK_ON"
REGIME_NEUTRAL = "NEUTRAL"
REGIME_VOLATILE = "VOLATILE"
REGIME_RISK_OFF = "RISK_OFF"

# Run roles (§16 run_history.run_role)
ROLE_TRAIN = "TRAIN"
ROLE_TEST = "TEST"
ROLE_LIVE = "LIVE"


class ExitPriority(Enum):
    """§8.6 priority ordering (lower value = higher priority)."""

    STOP_BREACH = 1
    REGIME_RISK_OFF = 2
    NEWS_BEARISH_CRITICAL = 3
    TARGET_REACHED = 4
    TREND_FAILURE = 5
    NEWS_BEARISH_HIGH = 6  # TRIM advisory only; no-op in the baseline


class ExitReason(Enum):
    STOP = "STOP"
    REGIME_RISK_OFF = "REGIME_RISK_OFF"
    NEWS_CRITICAL = "NEWS_CRITICAL"
    TARGET = "TARGET"
    TREND_FAILURE = "TREND_FAILURE"
    TRIM_ADVISORY = "TRIM_ADVISORY"
    WINDOW_END_FORCE_CLOSE = "WINDOW_END_FORCE_CLOSE"
    CORP_EVENT_FORCE_CLOSE = "CORP_EVENT_FORCE_CLOSE"


# ---------------------------------------------------------------------------
# Snapshot value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar. ``label`` is the bar-start-time label (N-01) in
    ``HH:MM`` session-local form for 1-min bars; ``date`` is the session
    date for daily bars."""

    ticker: str
    o: Decimal
    h: Decimal
    l: Decimal
    c: Decimal
    v: Decimal
    feed: str = FEED_SIP
    adjustment: str = ADJUSTMENT_RAW
    timeframe: str = TIMEFRAME_1MIN
    label: str = ""           # e.g. "09:44" for 1-min bars
    date: _dt.date | None = None  # session date

    @property
    def close_time_minutes(self) -> int | None:
        """Session-local close minute for a 1-min bar label (N-01)."""
        if self.timeframe != TIMEFRAME_1MIN or not self.label:
            return None
        hh, mm = self.label.split(":")
        return int(hh) * 60 + int(mm) + 1


@dataclass(frozen=True)
class DailyBar:
    """Daily bar (daily timeframe). Used for both signal and executable
    series; the ``adjustment`` field distinguishes them (§3.3)."""

    ticker: str
    date: _dt.date
    o: Decimal
    h: Decimal
    l: Decimal
    c: Decimal
    v: Decimal
    feed: str = FEED_SIP
    adjustment: str = ADJUSTMENT_RAW


def label_to_minutes(label: str) -> int:
    """'09:44' -> minutes since session-local midnight."""
    hh, mm = label.split(":")
    return int(hh) * 60 + int(mm)


def minutes_to_label(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def n02_boundary_label(scan_label: str) -> str:
    """N-02 availability boundary (inclusive 1-min bar label) for a scan.

    A bar is available at decision time ``t`` iff
    ``bar_close_time <= t - 15 min`` (inclusive). With bar-start labeling a
    bar labeled ``L`` closes at ``L + 1 min``, so the last available label
    at scan ``t`` is the bar labeled ``t - 16 min``.
    """
    return minutes_to_label(label_to_minutes(scan_label) - 16)


def truncate_shares(value: Decimal) -> Decimal:
    """N-04: share precision is truncation to 4 decimal places."""
    return value.quantize(Decimal("0.0001"), rounding=ROUND_FLOOR)


def round_half_up_int(value: Decimal) -> int:
    """N-13: score arithmetic rounds half-up: floor(x + 0.5)."""
    return int((value + Decimal("0.5")).to_integral_value(rounding=ROUND_FLOOR))


# ---------------------------------------------------------------------------
# Pipeline context (the point-in-time snapshot handed to Stage 1–5)
# ---------------------------------------------------------------------------


@dataclass
class SymbolSnapshot:
    """All inputs the deterministic pipeline consumes for one ticker at one
    10:00 scan on session ``D``. Every field must be point-in-time correct
    (N-02) and carry the §3.3 series provenance of its source."""

    ticker: str
    session_date: _dt.date
    asset_class: str = "stock"          # "stock" | "etf"
    pluang_confirmed: bool = False

    # Signal series (split-adjusted daily), through T-1 inclusive,
    # oldest first. ``dates[i]`` is the session of ``closes[i]`` etc.
    signal_dates: list[_dt.date] = field(default_factory=list)
    signal_closes: list[Decimal] = field(default_factory=list)
    signal_highs: list[Decimal] = field(default_factory=list)
    signal_lows: list[Decimal] = field(default_factory=list)
    signal_volumes: list[Decimal] = field(default_factory=list)
    spy_signal_closes: list[Decimal] = field(default_factory=list)
    spy_signal_dates: list[_dt.date] = field(default_factory=list)

    # Executable 1-min bars actually returned for session D within
    # labels 09:30–09:44 (P-1; dict label -> Bar).
    exec_bars_today: dict[str, Bar] = field(default_factory=dict)

    # Prior-20-session cumulative volume over returned 09:30–09:44
    # executable bars (session pace denominator; §6.1). Session dates must
    # be the 20 sessions immediately preceding ``session_date``.
    prior_session_window_volumes: dict[_dt.date, Decimal] = field(default_factory=dict)

    # §7.2 G6 earnings events (stock tickers only).
    earnings_events: list[Any] = field(default_factory=list)
    earnings_coverage_enabled: bool = True   # §7.2 coverage-gap rule
    earnings_manifest_version: str = ""

    # News classifications / raw headline inventory for §11 consumption.
    classifications: list[Any] = field(default_factory=list)
    raw_headlines: list[Any] = field(default_factory=list)
    news_coverage_enabled: bool = True       # §11.6 coverage-gap rule
    news_manifest_version: str = ""

    def t_minus_1_close(self) -> Decimal | None:
        return self.signal_closes[-1] if self.signal_closes else None
