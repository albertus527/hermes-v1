"""R2.7 §8.6 exit logic — priority-ordered, close-based at scan times only.

Evaluated only at scheduled exit-evaluating scans (10:00 / 15:30 /
half-day 12:00). The §19 item 2 / P-1 boundary rule decides whether
evaluation is suppressed; otherwise the evaluation price is the close of
the last available in-session executable bar at or before the scan's N-02
availability boundary.

P-A-05: trend failure is per open position, origin at the position's entry
fill; fires at the earliest scheduled non-suppressed exit-evaluating scan
whose two most recent completed daily signal bars both close below EMA50;
a suppressed scan does not disarm it.

P-3: a RISK_OFF EXIT advisory detected at 08:15 persists across suppressed
exit scans to the next non-suppressed exit-evaluating scan of the same
session and lapses at session close if unissued
(RISK_OFF_ADVISORY_PENDING / RISK_OFF_ADVISORY_LAPSED).
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping, Sequence

from trading_core.indicators import ema
from trading_core.types import (
    Bar,
    ExitReason,
    label_to_minutes,
    n02_boundary_label,
)

EXIT_PRIORITY = (
    ExitReason.STOP,
    ExitReason.REGIME_RISK_OFF,
    ExitReason.NEWS_CRITICAL,
    ExitReason.TARGET,
    ExitReason.TREND_FAILURE,
    ExitReason.TRIM_ADVISORY,       # §8.6 item 6: TRIM only; baseline no-op
)


@dataclass(frozen=True)
class ExitEvaluationContext:
    """Point-in-time inputs for one exit evaluation scan."""

    scan_label: str                       # "10:00" | "15:30" | "12:00"
    session_date: _dt.date
    scan_datetime: _dt.datetime           # explicit decision timestamp t
    exec_bars_today: Mapping[str, Bar]    # ticker's in-session 1-min bars
    stop_price: Decimal
    target_price: Decimal
    risk_off_advisory_pending: bool       # detected at this session's 08:15
    globally_excluded: bool               # §19 item 2: RISK_OFF unavailable
    confirmed_critical_news: bool         # §11.3 two-source rule output
    bearish_high_active: bool             # TRIM advisory trigger
    # signal daily closes through T-1 (for trend failure), oldest first:
    signal_closes: Sequence[float] = ()


@dataclass(frozen=True)
class ExitEvaluation:
    suppressed: bool                      # EXIT_EVAL_SUPPRESSED
    boundary_label: str                   # scan's N-02 boundary label
    evaluation_bar_label: str | None      # last available bar used
    evaluation_price: Decimal | None
    fired: ExitReason | None              # highest-priority trigger
    trend_failure_condition_met: bool     # reevaluated each scan (P-A-05)


def last_available_bar_label(
    bars: Mapping[str, Bar],
    boundary_label: str,
) -> str | None:
    """§8.6/P-1: the last in-session executable bar labeled at or before
    the scan's N-02 availability boundary; None when no such bar exists."""
    boundary = label_to_minutes(boundary_label)
    candidates = [lab for lab in bars if label_to_minutes(lab) <= boundary]
    if not candidates:
        return None
    return max(candidates, key=label_to_minutes)


def trend_failure_condition(signal_closes: Sequence[float]) -> bool:
    """P-A-05 condition: the two most recent completed daily signal bars
    both satisfy close < EMA50. Requires at least 51 bars."""
    if len(signal_closes) < 51:
        return False
    ema50 = ema(signal_closes, 50)
    return (signal_closes[-1] < ema50[-1]
            and signal_closes[-2] < ema50[-2])


def evaluate_exit(ctx: ExitEvaluationContext) -> ExitEvaluation:
    """Priority-ordered exit evaluation at one scheduled scan.

    Suppression (§19 item 2): EXIT_EVAL_SUPPRESSED iff no executable 1-min
    bar exists in the session labeled at or before the scan's N-02
    boundary. A suppressed scan does NOT disarm trend failure or a pending
    RISK_OFF advisory — the caller re-evaluates at the next scan of the
    same session.
    """
    boundary = n02_boundary_label(ctx.scan_label)
    label = last_available_bar_label(ctx.exec_bars_today, boundary)
    if label is None:
        return ExitEvaluation(
            suppressed=True, boundary_label=boundary,
            evaluation_bar_label=None, evaluation_price=None,
            fired=None, trend_failure_condition_met=False)

    price = ctx.exec_bars_today[label].c
    tf_met = trend_failure_condition(ctx.signal_closes)

    fired: ExitReason | None = None
    if price <= ctx.stop_price:
        fired = ExitReason.STOP
    elif ctx.risk_off_advisory_pending and not ctx.globally_excluded:
        fired = ExitReason.REGIME_RISK_OFF
    elif ctx.confirmed_critical_news:
        fired = ExitReason.NEWS_CRITICAL
    elif price >= ctx.target_price:
        fired = ExitReason.TARGET
    elif tf_met:
        fired = ExitReason.TREND_FAILURE
    elif ctx.bearish_high_active:
        fired = ExitReason.TRIM_ADVISORY  # no-op in the baseline backtest

    return ExitEvaluation(
        suppressed=False, boundary_label=boundary,
        evaluation_bar_label=label, evaluation_price=price,
        fired=fired, trend_failure_condition_met=tf_met)


# ---------------------------------------------------------------------------
# P-3 RISK_OFF same-session advisory persistence state machine
# ---------------------------------------------------------------------------


@dataclass
class RiskOffAdvisoryState:
    """Tracks one session's RISK_OFF advisory lifecycle (P-3).

    A RISK_OFF detection at 08:15 is issued at the next non-suppressed
    exit-evaluating scan of that session while the position remains open;
    suppression does not disarm it; an unissued advisory lapses at that
    session's close and is re-detected next session if the regime remains
    RISK_OFF (§8.6 item 2, §19 item 2, §16 rule 10).
    """

    detection_session: _dt.date
    pending: bool = True
    issued_scan: str | None = None
    suppressed_scans: list[str] = field(default_factory=list)

    def on_scan(self, scan_label: str, *, suppressed: bool,
                position_open: bool) -> str | None:
        """Advance the state machine at one scheduled exit-evaluating scan.

        Returns an event code when one must be persisted:
        ``RISK_OFF_ADVISORY_PENDING`` on a suppressed scan, ``ISSUED`` when
        the advisory fires, ``None`` otherwise (position closed → advisory
        no longer tracked).
        """
        if not self.pending:
            return None
        if not position_open:
            self.pending = False
            return None
        if suppressed:
            self.suppressed_scans.append(scan_label)
            return "RISK_OFF_ADVISORY_PENDING"
        self.pending = False
        self.issued_scan = scan_label
        return "ISSUED"

    def on_session_close(self, *, position_open: bool) -> str | None:
        """End-of-session: an unissued advisory lapses (P-3)."""
        if self.pending and position_open:
            self.pending = False
            return "RISK_OFF_ADVISORY_LAPSED"
        return None
