"""R2.7 §5 market regime engine + §5.5 opening-drop filter.

Computed once daily at 08:15 ET from prior trading-day closes (T-1, N-14)
on the signal series. Pure functions; ``t``/dates are explicit parameters.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Protocol, Sequence

from trading_core.indicators import ema
from trading_core.types import (
    REGIME_NEUTRAL,
    REGIME_RISK_OFF,
    REGIME_RISK_ON,
    REGIME_VOLATILE,
)

VIX_HIGH_THRESHOLD = 25.0            # ASSUMPTION / MUST TEST (§5.2)
OPENING_DROP_THRESHOLD = -0.02       # ASSUMPTION / MUST TEST (§5.5)


class CalendarLike(Protocol):
    """Minimal exchange-calendar surface the regime engine needs."""

    def is_trading_day(self, d: _dt.date) -> bool: ...
    def prev_trading_day(self, d: _dt.date) -> _dt.date | None: ...


@dataclass(frozen=True)
class RegimeResult:
    trend_score: int
    vol_state: str            # "HIGH" | "NORMAL"
    regime: str
    multiplier: float | None  # None in RISK_OFF (no new longs)
    new_longs_allowed: bool
    vix_value: float | None
    vix_date_used: _dt.date | None
    t_minus_1: _dt.date


class RegimeComputationError(Exception):
    """VIX gap-rule failure -> global exclusion (§5.2 / §19 item 2)."""


def compute_trend_score(
    *,
    spy_closes: Sequence[float],
    qqq_closes: Sequence[float],
) -> int | None:
    """§5.1: count of TRUE among the four SPY/QQQ vs EMA50/200 conditions.

    Signal-series closes through T-1 inclusive (oldest first). Returns None
    when fewer than 200 closes exist (EMA200 undefined).
    """
    if len(spy_closes) < 200 or len(qqq_closes) < 200:
        return None
    spy_ema50 = ema(spy_closes, 50)[-1]
    spy_ema200 = ema(spy_closes, 200)[-1]
    qqq_ema50 = ema(qqq_closes, 50)[-1]
    qqq_ema200 = ema(qqq_closes, 200)[-1]
    return int(spy_closes[-1] > spy_ema200) + int(spy_ema50 > spy_ema200) \
        + int(qqq_closes[-1] > qqq_ema200) + int(qqq_ema50 > qqq_ema200)


def resolve_vix(
    *,
    t_minus_1: _dt.date,
    vix_by_date: dict[_dt.date, float],
) -> tuple[float, _dt.date]:
    """§5.2 gap rule: VIXCLS(T-1); if missing, the most recent observation
    within the preceding 5 calendar days; otherwise raise (global
    exclusion per §19 item 2)."""
    for offset in range(0, 6):
        candidate = t_minus_1 - _dt.timedelta(days=offset)
        if candidate in vix_by_date:
            return vix_by_date[candidate], candidate
    raise RegimeComputationError(
        f"VIXCLS missing for {t_minus_1} and the preceding 5 calendar days")


def compute_regime(
    *,
    date: _dt.date,
    spy_closes: Sequence[float],
    qqq_closes: Sequence[float],
    vix_by_date: dict[_dt.date, float],
    calendar: CalendarLike,
) -> RegimeResult:
    """§5.3 regime mapping. ``date`` is the trading session being prepared
    (regime computed at 08:15 from T-1 closes)."""
    t_minus_1 = calendar.prev_trading_day(date)
    if t_minus_1 is None:
        raise RegimeComputationError(f"no prior trading day before {date}")
    trend = compute_trend_score(spy_closes=spy_closes, qqq_closes=qqq_closes)
    if trend is None:
        raise RegimeComputationError(
            "fewer than 200 signal closes; EMA200 undefined")
    vix_value, vix_date_used = resolve_vix(t_minus_1=t_minus_1, vix_by_date=vix_by_date)
    vol_state = "HIGH" if vix_value > VIX_HIGH_THRESHOLD else "NORMAL"

    if trend <= 1:
        regime, mult, allowed = REGIME_RISK_OFF, None, False
    elif trend == 4 and vol_state == "NORMAL":
        regime, mult, allowed = REGIME_RISK_ON, 1.0, True
    elif trend in (2, 3) and vol_state == "NORMAL":
        regime, mult, allowed = REGIME_NEUTRAL, 0.5, True
    elif vol_state == "HIGH" and 2 <= trend <= 4:
        regime, mult, allowed = REGIME_VOLATILE, 0.5, True
    else:  # pragma: no cover - mapping is total over trend x vol
        raise RegimeComputationError(
            f"unmapped regime combination trend={trend} vol={vol_state}")
    return RegimeResult(
        trend_score=trend, vol_state=vol_state, regime=regime,
        multiplier=mult, new_longs_allowed=allowed,
        vix_value=vix_value, vix_date_used=vix_date_used,
        t_minus_1=t_minus_1,
    )


# ---------------------------------------------------------------------------
# §5.5 opening-drop filter (SPY, global; both legs executable space, RP-09)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpeningDropResult:
    opening_return: float
    tripped: bool
    price_0944: float
    prior_official_close: float   # after §5.5 ex-date split adjustment
    split_ratio_applied: float | None


def compute_opening_drop(
    *,
    spy_price_0944: float,
    spy_prior_official_close: float,
    spy_split_ratio_on_ex_date: float | None = None,
    threshold: float = OPENING_DROP_THRESHOLD,
) -> OpeningDropResult:
    """§5.5: SPY 09:44 executable close vs T-1 official close, executable
    (unadjusted) space on both legs. If an SPY split ex-date falls on day
    T, the prior official close is divided by the split ratio (the only
    adjustment applied)."""
    prior = spy_prior_official_close
    if spy_split_ratio_on_ex_date is not None:
        prior = prior / spy_split_ratio_on_ex_date
    opening_return = (spy_price_0944 - prior) / prior
    return OpeningDropResult(
        opening_return=opening_return,
        tripped=opening_return <= threshold,
        price_0944=spy_price_0944,
        prior_official_close=prior,
        split_ratio_applied=spy_split_ratio_on_ex_date,
    )
