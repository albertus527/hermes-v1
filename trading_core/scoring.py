"""R2.7 §7.4–§7.6 scoring, rounding, ranking, cutoffs, bands.

Closed-form score components over point-in-time inputs (N-13: half-up
integer rounding before cutoff, banding, and ranking; tie-break higher
score then ascending ticker).

Series assignment (N-11): Trend/Momentum/Volume-daily components consume
the signal daily series; Intraday/Volume-session components consume
executable 1-min bars (post-P-1 survival). ``a`` = ATR14(T-1)/close(T-1)
is a signal-series ratio.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from trading_core.indicators import opening_range, session_volume_pace
from trading_core.types import Bar, round_half_up_int

# Cutoffs / bands (§7.5/§7.6 — ASSUMPTION / MUST TEST)
CUTOFF_RISK_ON = 70
CUTOFF_RAISED = 80           # NEUTRAL / VOLATILE
BAND_STRONG_MIN = 85

# Score component thresholds (ASSUMPTION / MUST TEST)
RSI_BAND_LOW, RSI_BAND_HIGH = 50.0, 70.0
PACE_BREAK_THRESHOLD = Decimal("1.2")
PACE_VOLUME_THRESHOLD = Decimal("1.0")


@dataclass(frozen=True)
class ScoreInputs:
    """All inputs for §7.4. Decimals for intraday (exact); floats for the
    signal-series indicator values (numpy outputs)."""

    ticker: str
    # signal daily (T-1):
    ema20: float
    ema50: float
    ema200: float
    close_t_minus_1: float
    atr14: float
    rsi14_t_minus_1: float
    rsi14_t_minus_6: float | None
    rs20_vs_spy: float | None
    daily_dollar_volume_recent: float    # mean close*volume over T-10..T-1
    daily_dollar_volume_prior: float     # mean close*volume over T-20..T-11
    # executable 1-min session D (post-P-1):
    price_0944: Decimal
    session_vwap: Decimal
    exec_bars_today: dict[str, Bar]
    session_pace: Decimal | None         # None when the lookback is incomplete
    # §7.4 catalyst modifier (from news_effects.catalyst_score_points)
    catalyst_points: int


@dataclass(frozen=True)
class ScoreResult:
    ticker: str
    components: dict[str, float]
    raw_total: Decimal
    final_total_int: int


def score_candidate(inp: ScoreInputs) -> ScoreResult:
    """§7.4 closed-form score components."""
    comp: dict[str, float] = {}

    # Trend quality (30)
    trend = 0.0
    if inp.ema20 > inp.ema50 > inp.ema200:
        trend += 10
    if inp.close_t_minus_1 > inp.ema20:
        trend += 10
    if inp.rs20_vs_spy is not None and inp.rs20_vs_spy > 0:
        trend += 10
    comp["trend"] = trend

    # Momentum (25)
    momentum = 0.0
    if RSI_BAND_LOW <= inp.rsi14_t_minus_1 <= RSI_BAND_HIGH:
        momentum += 15
    if inp.rsi14_t_minus_6 is not None and \
            (inp.rsi14_t_minus_1 - inp.rsi14_t_minus_6) > 0:
        momentum += 10
    comp["momentum"] = momentum

    # Intraday (25)
    a = Decimal(str(inp.atr14)) / Decimal(str(inp.close_t_minus_1))
    vwap_dist = Decimal(0)
    if a > 0:
        rel = (inp.price_0944 - inp.session_vwap) / inp.session_vwap
        vwap_dist = Decimal(15) * min(Decimal(1), max(Decimal(0), rel / a))
    or_break = 0.0
    or_levels = opening_range(inp.exec_bars_today)
    if or_levels is not None and inp.session_pace is not None:
        or_high, _or_low = or_levels
        if inp.price_0944 > or_high and inp.session_pace > PACE_BREAK_THRESHOLD:
            or_break = 10.0
    comp["intraday_vwap_distance"] = float(vwap_dist)
    comp["intraday_or_break"] = or_break

    # Volume (10)
    volume = 0.0
    if inp.daily_dollar_volume_recent > inp.daily_dollar_volume_prior:
        volume += 5
    if inp.session_pace is not None and inp.session_pace >= PACE_VOLUME_THRESHOLD:
        volume += 5
    comp["volume"] = volume

    # Catalyst modifier (±10, already clipped by news_effects)
    comp["catalyst"] = float(inp.catalyst_points)

    raw = Decimal(str(comp["trend"])) + Decimal(str(comp["momentum"])) \
        + vwap_dist + Decimal(str(comp["intraday_or_break"])) \
        + Decimal(str(comp["volume"])) + Decimal(inp.catalyst_points)
    final = round_half_up_int(raw)  # N-13: floor(x + 0.5)
    return ScoreResult(ticker=inp.ticker, components=comp,
                       raw_total=raw, final_total_int=final)


def active_cutoff(regime: str) -> int:
    """§7.5: RISK_ON >= 70; NEUTRAL/VOLATILE >= 80. RISK_OFF admits no new
    entries at all (handled at Stage 0 S0.5)."""
    from trading_core.types import REGIME_RISK_ON
    return CUTOFF_RISK_ON if regime == REGIME_RISK_ON else CUTOFF_RAISED


def display_band(final_total_int: int, cutoff: int) -> str:
    """§7.6 display bands (Strong / Moderate only; integer scores make the
    bands exhaustive above the cutoff)."""
    if final_total_int >= BAND_STRONG_MIN:
        return "Strong"
    if cutoff <= final_total_int <= BAND_STRONG_MIN - 1:
        return "Moderate"
    return "below-cutoff"


def rank_candidates(results: Sequence[ScoreResult], cutoff: int) -> list[ScoreResult]:
    """Stage 2 discard + Stage 3 ranking: discard below the active cutoff;
    rank by integer score descending, tie-break ascending ticker (N-13)."""
    survivors = [r for r in results if r.final_total_int >= cutoff]
    return sorted(survivors, key=lambda r: (-r.final_total_int, r.ticker))
