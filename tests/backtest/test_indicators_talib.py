"""Phase-1 exit gate: TA-Lib cross-validation of EMA / RSI / ATR.

R2.7 §6: maximum relative error <= 1e-6 per value after a warm-up of
5 x period bars, on synthetic series. TA-Lib is a dev/validation-only
dependency — the production deterministic core never imports it.

The test SKIPS (does not fail) when the TA-Lib C library is not available
on the host; the §6 tolerance comparison runs whenever the wrapper imports.
"""

import numpy as np
import pytest

talib = pytest.importorskip("talib", reason="TA-Lib C library not installed")

from trading_core.indicators import atr, ema, rsi

TOL = 1e-6

rng = np.random.default_rng(27)
N = 1200
CLOSES = 100 + np.cumsum(rng.normal(0, 1, N))
CLOSES = np.maximum(CLOSES, 1.0)
HIGHS = CLOSES + np.abs(rng.normal(0, 0.5, N))
LOWS = CLOSES - np.abs(rng.normal(0, 0.5, N))


def _assert_close(ours, theirs, period):
    warm = 5 * period
    ours = ours[warm:]
    theirs = theirs[warm:]
    mask = ~np.isnan(ours) & ~np.isnan(theirs)
    assert mask.any()
    ours, theirs = ours[mask], theirs[mask]
    denom = np.maximum(np.abs(theirs), 1e-12)
    rel = np.abs(ours - theirs) / denom
    assert float(rel.max()) <= TOL, f"max rel err {rel.max():.3e} > {TOL}"


@pytest.mark.parametrize("period", [20, 50, 200])
def test_ema_matches_talib(period):
    _assert_close(ema(CLOSES, period), talib.EMA(CLOSES, timeperiod=period), period)


def test_atr_matches_talib():
    ours = atr(HIGHS, LOWS, CLOSES, 14)
    theirs = talib.ATR(HIGHS, LOWS, CLOSES, timeperiod=14)
    _assert_close(ours, theirs, 14)


def test_rsi_matches_talib():
    ours = rsi(CLOSES, 14)
    theirs = talib.RSI(CLOSES, timeperiod=14)
    _assert_close(ours, theirs, 14)
