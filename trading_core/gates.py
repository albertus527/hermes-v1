"""R2.7 hard gates G2–G10 (§7.2) — pure predicates.

Every gate returns a GateResult carrying a full ``inputs_json`` capture for
the §16 ``gate_results`` table. Binary; any failure rejects; no
compensation by score.

Series assignment (N-11): G4/G5 consume signal daily; G8 consumes
executable 1-min (post-P-1 required-bar survival); G9/G10 are sizing-
dependent (Stage 4). G2 is global (Stage 0 S0.1) and modeled in
pipeline.py as a data-freshness input, not per-ticker.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Sequence

from trading_core.indicators import (
    evaluation_price_0944,
    missing_required_bar_labels,
    session_vwap,
)

# Gate thresholds (ASSUMPTION / MUST TEST where the spec says so)
G5_RSI_MAX = 75.0                       # §7.2 G5 — ASSUMPTION / MUST TEST
GATE_A_THRESHOLD = Decimal("0.25")      # §9.4 — ASSUMPTION / MUST TEST


@dataclass(frozen=True)
class EarningsEvent:
    """§7.2 G6 input: one provider earnings-calendar event."""

    event_date: _dt.date
    timing: str   # "before-market-open" | "after-market-close" | "unspecified"


@dataclass(frozen=True)
class GateResult:
    gate_id: str
    passed: bool
    inputs_json: dict[str, Any]
    reason_code: str = ""          # e.g. G6_DISABLED_COVERAGE
    stage: str = "1"               # §7.1 stage ("0", "1", "4")


# ---------------------------------------------------------------------------
# §19 item 2 / P-1 entry-side required bars
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequiredBarCheck:
    excluded: bool
    missing_labels: tuple[str, ...]   # missing subset of the required set


def check_required_bars(returned_labels) -> RequiredBarCheck:
    """P-1: required executable 1-min bars are the ten 09:30–09:39 bars plus
    09:44. Absence of 09:40–09:43 alone never excludes (§21 item 30)."""
    missing = tuple(missing_required_bar_labels(returned_labels))
    return RequiredBarCheck(excluded=bool(missing), missing_labels=missing)


# ---------------------------------------------------------------------------
# G3 universe / tradeability
# ---------------------------------------------------------------------------


def gate_g3(*, ticker: str, universe: set[str], pluang_confirmed: bool,
            run_surface: str) -> GateResult:
    """G3: ticker in current universe.yaml; live requires pluang_confirmed;
    backtest may include unconfirmed tickers with disclosed bias."""
    in_universe = ticker in universe
    if run_surface == "live":
        passed = in_universe and pluang_confirmed
    else:
        passed = in_universe
    return GateResult(
        gate_id="G3", passed=passed, stage="1",
        inputs_json={"ticker": ticker, "in_universe": in_universe,
                     "pluang_confirmed": pluang_confirmed,
                     "run_surface": run_surface},
        reason_code="" if passed else "G3_UNIVERSE_OR_TRADEABILITY",
    )


# ---------------------------------------------------------------------------
# G4 trend / G5 overextension (signal daily, T-1)
# ---------------------------------------------------------------------------


def gate_g4(*, close_t_minus_1: float, ema50_t_minus_1: float,
            ema200_t_minus_1: float) -> GateResult:
    """G4: close(T-1) > EMA50(T-1) AND EMA50(T-1) > EMA200(T-1)."""
    passed = (close_t_minus_1 > ema50_t_minus_1
              and ema50_t_minus_1 > ema200_t_minus_1)
    return GateResult(
        gate_id="G4", passed=passed, stage="1",
        inputs_json={"close_t_minus_1": close_t_minus_1,
                     "ema50": ema50_t_minus_1, "ema200": ema200_t_minus_1},
        reason_code="" if passed else "G4_TREND",
    )


def gate_g5(*, rsi14_t_minus_1: float) -> GateResult:
    """G5: RSI14(T-1) <= 75."""
    passed = rsi14_t_minus_1 <= G5_RSI_MAX
    return GateResult(
        gate_id="G5", passed=passed, stage="1",
        inputs_json={"rsi14_t_minus_1": rsi14_t_minus_1,
                     "threshold": G5_RSI_MAX},
        reason_code="" if passed else "G5_OVEREXTENSION",
    )


# ---------------------------------------------------------------------------
# G6 earnings blackout (P-A-02 + I-1)
# ---------------------------------------------------------------------------


def map_earnings_event_session(
    event: EarningsEvent,
    *,
    is_trading_day,
    next_trading_day,
) -> _dt.date:
    """§7.2 G6 d(e) mapping (P-A-02, I-1):

    - before-market-open -> trading day of the event calendar date
    - after-market-close -> next trading day
    - unspecified -> trading day of the event calendar date
    - I-1: when a before-market-open or unspecified event calendar date is
      NOT a trading day, d(e) is the next trading day.
    """
    if event.timing == "after-market-close":
        return next_trading_day(event.event_date)
    # before-market-open | unspecified
    if is_trading_day(event.event_date):
        return event.event_date
    return next_trading_day(event.event_date)


def gate_g6(
    *,
    ticker: str,
    asset_class: str,
    decision_date: _dt.date,
    trading_sessions: Sequence[_dt.date],
    events: Sequence[EarningsEvent],
    coverage_enabled: bool,
    manifest_version: str,
    is_trading_day,
    next_trading_day,
) -> GateResult:
    """§7.2 G6: fails iff any event has d(e) in {T, T+1, T+2} (trading-day
    indices). ETFs: N/A, passes. Coverage-gap rule: outside verified
    EARNINGS covered spans, G6 passes and logs G6_DISABLED_COVERAGE."""
    if asset_class == "etf":
        return GateResult(gate_id="G6", passed=True, stage="1",
                          inputs_json={"ticker": ticker, "asset_class": "etf"},
                          reason_code="G6_NA_ETF")
    if not coverage_enabled:
        return GateResult(gate_id="G6", passed=True, stage="1",
                          inputs_json={"ticker": ticker,
                                       "decision_date": str(decision_date),
                                       "manifest_version": manifest_version},
                          reason_code="G6_DISABLED_COVERAGE")

    sessions = sorted(trading_sessions)
    try:
        t_idx = sessions.index(decision_date)
    except ValueError as exc:
        raise ValueError(f"decision date {decision_date} not a trading session") from exc
    blackout = {t_idx, t_idx + 1, t_idx + 2}

    mapped: list[dict[str, Any]] = []
    for e in events:
        d_e = map_earnings_event_session(
            e, is_trading_day=is_trading_day, next_trading_day=next_trading_day)
        try:
            d_idx = sessions.index(d_e)
        except ValueError:
            d_idx = None  # outside known calendar — cannot be in blackout
        mapped.append({
            "event_date": str(e.event_date),
            "provider_timing": e.timing,
            "mapped_session": str(d_e),
            "decision_date": str(decision_date),
            "earnings_manifest_version": manifest_version,
        })
        if d_idx is not None and d_idx in blackout:
            return GateResult(
                gate_id="G6", passed=False, stage="1",
                inputs_json={"ticker": ticker, "events": mapped},
                reason_code="G6_EARNINGS_BLACKOUT",
            )
    return GateResult(
        gate_id="G6", passed=True, stage="1",
        inputs_json={"ticker": ticker, "events": mapped},
    )


# ---------------------------------------------------------------------------
# G8 intraday bias (executable 1-min, post-P-1 survival)
# ---------------------------------------------------------------------------


def gate_g8(*, exec_bars_today: Mapping[str, Any]) -> GateResult:
    """G8: close of the executable 09:44 bar > session VWAP over returned
    09:30–09:44 executable bars. Evaluated only after the ticker survives
    the §19 item 2 entry-side required-bar rule; the caller must have
    applied the §6.1 zero-volume VWAP guard first."""
    price = evaluation_price_0944(exec_bars_today)
    vwap = session_vwap(exec_bars_today)
    if price is None or vwap is None:
        raise ValueError("G8 requires the 09:44 bar and a defined VWAP")
    passed = price > vwap
    return GateResult(
        gate_id="G8", passed=passed, stage="1",
        inputs_json={"price_0944": str(price), "session_vwap": str(vwap)},
        reason_code="" if passed else "G8_INTRADAY_BIAS",
    )
