"""R2.7 §6 indicator set — the single pinned pandas/numpy module.

Identical code path live and backtest. Pure functions over series; no I/O,
no wall clock. Conventions (§5.1, §6):

- EMA: recursive, alpha = 2/(N+1), seeded with the SMA of the first N
  closes.
- ATR14 / RSI14: Wilder smoothing; seed = simple average of the first 14
  periods, then Wilder recursion.
- Session VWAP: typical price (H+L+C)/3, volume-weighted, over the
  executable bars actually returned with labels 09:30–09:44 (P-1);
  zero cumulative volume -> VWAP undefined (VWAP_UNDEFINED_ZERO_VOLUME,
  entry-side only, §6.1/§19 item 2).
- Session volume pace: cumulative session volume over returned 09:30–09:44
  executable bars / mean of the same quantity over the prior 20 sessions
  (executable volumes; split artifact disclosed §3.3/§13.9 item 13).
- Opening range: max high / min low over bars labeled 09:30–09:39 (all ten
  required, §19 item 2).
- 20-day relative strength: close(T-1)/close(T-21) - 1, minus the same for
  SPY (signal series).

TA-Lib cross-validation at rel. err <= 1e-6 after 5x period warm-up is the
Phase-1 exit gate and lives in tests/backtest/ (validation-only; the
production core never imports TA-Lib).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Mapping, Sequence

import numpy as np

from trading_core.types import (
    P1_WINDOW_FIRST_LABEL,
    P1_WINDOW_LAST_LABEL,
    Bar,
    REQUIRED_EVALUATION_LABEL,
    REQUIRED_ENTRY_BAR_LABELS,
    REQUIRED_OPENING_RANGE_LABELS,
    label_to_minutes,
)

# ---------------------------------------------------------------------------
# EMA / ATR / RSI (signal daily series)
# ---------------------------------------------------------------------------


def ema(closes: Sequence[float], period: int) -> np.ndarray:
    """Recursive EMA, alpha = 2/(N+1), SMA seed over the first N values.

    Returns a float64 ndarray aligned with ``closes``; values before index
    ``period - 1`` are NaN (insufficient data).
    """
    x = np.asarray(closes, dtype=np.float64)
    out = np.full(x.shape, np.nan)
    n = len(x)
    if n < period:
        return out
    alpha = 2.0 / (period + 1.0)
    seed = float(np.mean(x[:period]))
    out[period - 1] = seed
    for i in range(period, n):
        out[i] = alpha * x[i] + (1.0 - alpha) * out[i - 1]
    return out


def true_range(highs: Sequence[float], lows: Sequence[float],
               closes: Sequence[float]) -> np.ndarray:
    """True range series; index 0 uses high-low (no prior close)."""
    h = np.asarray(highs, dtype=np.float64)
    l = np.asarray(lows, dtype=np.float64)
    c = np.asarray(closes, dtype=np.float64)
    tr = np.full(h.shape, np.nan)
    if len(h) == 0:
        return tr
    tr[0] = h[0] - l[0]
    for i in range(1, len(h)):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    return tr


def atr(highs: Sequence[float], lows: Sequence[float],
        closes: Sequence[float], period: int = 14) -> np.ndarray:
    """Wilder ATR (§6: seed = simple average of the first 14 periods, then
    Wilder recursion atr = (prev * (period-1) + tr) / period).

    TR at index 0 is undefined (no prior close), so the seed averages the
    first ``period`` *computable* true ranges (indices 1..period) and the
    first defined output sits at index ``period`` — matching TA-Lib."""
    tr = true_range(highs, lows, closes)
    out = np.full(tr.shape, np.nan)
    n = len(tr)
    if n <= period:
        return out
    seed = float(np.mean(tr[1:period + 1]))
    out[period] = seed
    for i in range(period + 1, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def rsi(closes: Sequence[float], period: int = 14) -> np.ndarray:
    """Wilder RSI. Seed = simple averages of the first ``period`` gains and
    losses; then Wilder recursion on average gain/loss.

    Edge conventions (match TA-Lib): zero average loss with positive
    average gain -> 100; both zero -> 50 (no movement); RSI is NaN before
    index ``period``.
    """
    x = np.asarray(closes, dtype=np.float64)
    out = np.full(x.shape, np.nan)
    n = len(x)
    if n <= period:
        return out
    deltas = np.diff(x)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))

    def _to_rsi(ag: float, al: float) -> float:
        if al == 0.0:
            return 100.0 if ag > 0.0 else 50.0
        rs = ag / al
        return 100.0 - 100.0 / (1.0 + rs)

    out[period] = _to_rsi(avg_gain, avg_loss)
    for i in range(period + 1, n):
        g = gains[i - 1]
        l = losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        out[i] = _to_rsi(avg_gain, avg_loss)
    return out


def relative_strength_20(closes: Sequence[float],
                         spy_closes: Sequence[float]) -> float | None:
    """20-day RS vs SPY (§6.1): close(T-1)/close(T-21) - 1 minus SPY's.

    ``closes``/``spy_closes`` are signal-series closes through T-1
    inclusive, oldest first. Returns None when fewer than 21 values exist.
    """
    if len(closes) < 21 or len(spy_closes) < 21:
        return None
    own = closes[-1] / closes[-21] - 1.0
    spy = spy_closes[-1] / spy_closes[-21] - 1.0
    return own - spy


# ---------------------------------------------------------------------------
# P-1 intraday helpers (executable 1-min series)
# ---------------------------------------------------------------------------


def missing_required_bar_labels(returned_labels: Iterable[str]) -> list[str]:
    """§19 item 2 / P-1: the subset of required entry-side labels absent
    from ``returned_labels``. Empty list -> the ticker is NOT excluded."""
    have = set(returned_labels)
    return [lab for lab in REQUIRED_ENTRY_BAR_LABELS if lab not in have]


def bars_in_p1_window(bars: Mapping[str, Bar]) -> list[Bar]:
    """Executable bars actually returned with labels 09:30–09:44 (P-1),
    ordered by label."""
    first = label_to_minutes(P1_WINDOW_FIRST_LABEL)
    last = label_to_minutes(P1_WINDOW_LAST_LABEL)
    return [bars[lab] for lab in sorted(bars, key=label_to_minutes)
            if first <= label_to_minutes(lab) <= last]


def session_vwap(bars: Mapping[str, Bar]) -> Decimal | None:
    """Session VWAP over returned 09:30–09:44 executable bars (typical
    price (H+L+C)/3, volume-weighted). Returns None when cumulative volume
    is zero (VWAP_UNDEFINED_ZERO_VOLUME guard; entry-side only)."""
    window = bars_in_p1_window(bars)
    pv = Decimal(0)
    vol = Decimal(0)
    for b in window:
        typical = (b.h + b.l + b.c) / Decimal(3)
        pv += typical * b.v
        vol += b.v
    if vol == 0:
        return None
    return pv / vol


def session_cumulative_volume(bars: Mapping[str, Bar]) -> Decimal:
    """Cumulative volume over returned 09:30–09:44 executable bars."""
    return sum((b.v for b in bars_in_p1_window(bars)), Decimal(0))


def session_volume_pace(bars: Mapping[str, Bar],
                        prior_20_session_window_volumes: Sequence[Decimal]
                        ) -> Decimal | None:
    """Session pace (§6.1): today's returned 09:30–09:44 cumulative
    executable volume divided by the mean of the same quantity over the
    prior 20 sessions. None when the lookback is incomplete/zero."""
    priors = list(prior_20_session_window_volumes)
    if len(priors) < 20:
        return None
    denom = sum(priors, Decimal(0)) / Decimal(20)
    if denom == 0:
        return None
    return session_cumulative_volume(bars) / denom


def opening_range(bars: Mapping[str, Bar]) -> tuple[Decimal, Decimal] | None:
    """Opening range 09:30–09:40: (max high, min low) over the ten bars
    labeled 09:30–09:39. All ten are required (§19 item 2); returns None
    when any is absent (caller applies the exclusion)."""
    if missing_required_bar_labels(bars.keys()):
        return None
    selected = [bars[lab] for lab in REQUIRED_OPENING_RANGE_LABELS]
    return (max(b.h for b in selected), min(b.l for b in selected))


def evaluation_price_0944(bars: Mapping[str, Bar]) -> Decimal | None:
    """Close of the executable 09:44 bar (entry_reference_price basis)."""
    bar = bars.get(REQUIRED_EVALUATION_LABEL)
    return bar.c if bar is not None else None
