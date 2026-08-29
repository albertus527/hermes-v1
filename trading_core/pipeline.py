"""R2.7 §7.1 canonical pipeline order (Stage 0 → Stage 5), 10:00 main scan.

Exact ordering (normative):

    S0.0  Pending-entry resolution (NEXT_SESSION only when scheduled today)
    S0.1  G2 data freshness (global) — failure -> DATA DEGRADED, halt
    S0.2  Exit evaluation on the open position; any exit trigger fires ->
          no new entry is evaluated this scan (N-10); pipeline ends
    S0.3  max_positions check — position open -> no new entry; pipeline ends
    S0.4  Opening-drop filter (SPY, global, §5.5) — compute or REUSE the
          day's §5.5 result; tripped -> suppress ordinary same-day BUYs;
          pipeline ends
    S0.5  Regime check — RISK_OFF -> no new entries
    Stage 1  G3, G4, G5, G6, G7, G8 (per ticker)
    Stage 2  score survivors; discard below active cutoff
    Stage 3  rank (score desc, ticker asc)
    Stage 4  sizing-dependent gates in rank order (G10, G9); first passing
             candidate is the BUY CANDIDATE; stop
    Stage 5  NEWS_UNVERIFIED downgrade to WATCH / DATA-UNVERIFIED; Stage 4
             is NOT resumed for lower-ranked candidates (DECIDED)

The §5.5 computation required by S0.0/S0.4 occurs irrespective of whether
S0.3 would otherwise end the pipeline (P-A-04, N-12).

This module is scan-agnostic about data provisioning: every input arrives
as an explicit snapshot parameter. Fill mechanics (Phase 3) consume the
returned decision objects.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from trading_core import gates as g
from trading_core.exits import ExitEvaluationContext, evaluate_exit
from trading_core.fees import (
    FeeComputation,
    RoundingBranch,
    compute_round_trip_fees,
)
from trading_core.indicators import (
    atr,
    ema,
    evaluation_price_0944,
    relative_strength_20,
    rsi,
    session_volume_pace,
    session_vwap,
)
from trading_core.news_effects import catalyst_score_points, g7_vetoed_at
from trading_core.regime import OpeningDropResult, RegimeResult
from trading_core.scoring import (
    ScoreInputs,
    active_cutoff,
    display_band,
    rank_candidates,
    score_candidate,
)
from trading_core.sizing import compute_sizing
from trading_core.stops import compute_stop_pct, compute_stop_target
from trading_core.types import (
    REGIME_RISK_OFF,
    Bar,
    SymbolSnapshot,
)

# Stage-5 no-resumption is a normative DECIDED behavior (§7.1).
STAGE5_NO_RESUMPTION = True


@dataclass(frozen=True)
class PendingNextSessionEntry:
    """A NEXT_SESSION candidate scheduled for an official-open fill on the
    current session (§13.2). Created at a prior scan; resolved at S0.0."""

    ticker: str
    scheduled_fill_date: _dt.date
    decision_context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class S0Resolution:
    kind: str          # NONE | FILLED_STANDS | NEXT_SESSION_VOIDED |
                       # ENTRY_UNFILLABLE_NO_FILTER | ENTRY_UNFILLABLE_WINDOW_END
    candidate: PendingNextSessionEntry | None = None


@dataclass(frozen=True)
class OpenPosition:
    """Minimal open-position snapshot for exit evaluation (§8.6)."""

    ticker: str
    stop_price: Decimal
    target_price: Decimal
    entry_fill_ts: _dt.datetime


@dataclass
class PipelineEvents:
    """Structured event records collected during one scan (§16 shapes)."""

    codes: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def emit(self, code: str, **details: Any) -> None:
        self.codes.append((code, details))


@dataclass
class Stage1Survivor:
    snapshot: SymbolSnapshot
    gate_results: list[g.GateResult]


@dataclass
class Stage4Evaluation:
    ticker: str
    g10_pass: bool
    g9_pass: bool | None          # None when G10 rejected first
    sizing: Any = None
    fee_computation: FeeComputation | None = None
    burden: Decimal | None = None


@dataclass
class MainScanResult:
    """The 10:00-scan decision record. ``action`` is one of the §8.1
    vocabulary values: BUY CANDIDATE | WATCH | EXIT | TRIM | NO ACTION."""

    action: str
    events: PipelineEvents
    s0_resolution: S0Resolution
    opening_drop: OpeningDropResult | None
    exit_evaluation: Any = None
    stage1_results: list[Stage1Survivor] = field(default_factory=list)
    stage2_discarded: list[str] = field(default_factory=list)
    stage3_ranking: list[str] = field(default_factory=list)
    stage4_evaluations: list[Stage4Evaluation] = field(default_factory=list)
    selected_candidate: str | None = None
    band: str = ""
    cutoff: int = 0
    reason_code: str = ""


# ---------------------------------------------------------------------------
# S0.0 pending NEXT_SESSION resolution (P-A-04)
# ---------------------------------------------------------------------------


def resolve_pending_next_session(
    *,
    candidate: PendingNextSessionEntry | None,
    session_date: _dt.date,
    window_end: _dt.date,
    spy_0944_price: Decimal | None,
    spy_prior_official_close: Decimal | None,
    spy_split_ratio_on_ex_date: float | None,
    compute_opening_drop: Callable[..., OpeningDropResult],
    events: PipelineEvents,
) -> tuple[S0Resolution, OpeningDropResult | None]:
    """S0.0: resolve a pending NEXT_SESSION entry BEFORE S0.1.

    The §5.5 computation happens whenever S0.0 requires it (a NEXT_SESSION
    fill scheduled today), irrespective of whether S0.3 would otherwise end
    the pipeline. Returns the resolution and the day's §5.5 result for
    reuse by S0.4 (computed once per day, N-12)."""
    if candidate is None or candidate.scheduled_fill_date != session_date:
        return S0Resolution(kind="NONE"), None

    # §13.2: a scheduled fill date outside the walk-forward window expires
    # unfilled; ENTRY_UNFILLABLE_WINDOW_END takes precedence.
    if candidate.scheduled_fill_date > window_end:
        events.emit("ENTRY_UNFILLABLE_WINDOW_END",
                    ticker=candidate.ticker,
                    scheduled_fill_date=str(candidate.scheduled_fill_date))
        return S0Resolution(kind="ENTRY_UNFILLABLE_WINDOW_END",
                            candidate=candidate), None

    opening_drop: OpeningDropResult | None = None
    if spy_0944_price is None or spy_prior_official_close is None:
        # §19 item 2 pending-NEXT_SESSION exception: SPY filter data
        # unavailable -> expires unfilled.
        events.emit("ENTRY_UNFILLABLE_NO_FILTER",
                    ticker=candidate.ticker,
                    scheduled_fill_date=str(candidate.scheduled_fill_date))
        return S0Resolution(kind="ENTRY_UNFILLABLE_NO_FILTER",
                            candidate=candidate), None

    opening_drop = compute_opening_drop(
        spy_price_0944=float(spy_0944_price),
        spy_prior_official_close=float(spy_prior_official_close),
        spy_split_ratio_on_ex_date=spy_split_ratio_on_ex_date,
    )
    if opening_drop.tripped:
        # Voided: no position, no accounting, no exit evaluation.
        events.emit("NEXT_SESSION_VOIDED",
                    ticker=candidate.ticker,
                    scheduled_fill_date=str(candidate.scheduled_fill_date),
                    opening_return=opening_drop.opening_return)
        return S0Resolution(kind="NEXT_SESSION_VOIDED",
                            candidate=candidate), opening_drop

    return S0Resolution(kind="FILLED_STANDS", candidate=candidate), opening_drop
