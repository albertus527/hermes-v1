"""§5 regime engine + §5.5 opening-drop filter tests."""

import datetime as dt

import numpy as np
import pytest

from trading_core.regime import (
    RegimeComputationError,
    compute_opening_drop,
    compute_regime,
    compute_trend_score,
    resolve_vix,
)
from trading_core.types import (
    REGIME_NEUTRAL,
    REGIME_RISK_OFF,
    REGIME_RISK_ON,
    REGIME_VOLATILE,
)


class FakeCalendar:
    def __init__(self, days):
        self.days = sorted(days)

    def is_trading_day(self, d):
        return d in self.days

    def prev_trading_day(self, d):
        before = [x for x in self.days if x < d]
        return before[-1] if before else None


def _uptrend(n=260):
    return list(np.linspace(50, 200, n))


def _downtrend(n=260):
    return list(np.linspace(200, 50, n))


class TestTrendScore:
    def test_strong_uptrend_scores_4(self):
        assert compute_trend_score(spy_closes=_uptrend(),
                                   qqq_closes=_uptrend()) == 4

    def test_strong_downtrend_scores_0(self):
        assert compute_trend_score(spy_closes=_downtrend(),
                                   qqq_closes=_downtrend()) == 0

    def test_insufficient_history(self):
        assert compute_trend_score(spy_closes=[1.0] * 100,
                                   qqq_closes=[1.0] * 100) is None


class TestVixGapRule:
    def test_exact_t_minus_1(self):
        v, used = resolve_vix(t_minus_1=dt.date(2026, 1, 2),
                              vix_by_date={dt.date(2026, 1, 2): 20.0})
        assert (v, used) == (20.0, dt.date(2026, 1, 2))

    def test_gap_within_5_calendar_days(self):
        v, used = resolve_vix(
            t_minus_1=dt.date(2026, 1, 6),
            vix_by_date={dt.date(2026, 1, 2): 30.0})
        assert v == 30.0 and used == dt.date(2026, 1, 2)

    def test_gap_beyond_5_days_fails(self):
        with pytest.raises(RegimeComputationError):
            resolve_vix(t_minus_1=dt.date(2026, 1, 10),
                        vix_by_date={dt.date(2026, 1, 1): 30.0})


class TestRegimeMapping:
    cal = FakeCalendar([dt.date(2026, 1, 2), dt.date(2026, 1, 5)])

    def _regime(self, spy, qqq, vix):
        return compute_regime(
            date=dt.date(2026, 1, 5), spy_closes=spy, qqq_closes=qqq,
            vix_by_date={dt.date(2026, 1, 2): vix}, calendar=self.cal)

    def test_risk_on(self):
        r = self._regime(_uptrend(), _uptrend(), 20.0)
        assert r.regime == REGIME_RISK_ON and r.multiplier == 1.0

    def test_volatile(self):
        r = self._regime(_uptrend(), _uptrend(), 30.0)
        assert r.regime == REGIME_VOLATILE and r.multiplier == 0.5

    def test_risk_off(self):
        r = self._regime(_downtrend(), _downtrend(), 20.0)
        assert r.regime == REGIME_RISK_OFF and not r.new_longs_allowed


class TestOpeningDrop:
    def test_trip_threshold(self):
        r = compute_opening_drop(spy_price_0944=97.9,
                                 spy_prior_official_close=100.0)
        assert r.tripped  # -2.1% <= -2%

    def test_boundary_not_tripped_above(self):
        r = compute_opening_drop(spy_price_0944=98.01,
                                 spy_prior_official_close=100.0)
        assert not r.tripped

    def test_exactly_minus_2pct_trips(self):
        r = compute_opening_drop(spy_price_0944=98.0,
                                 spy_prior_official_close=100.0)
        assert r.tripped  # <= -0.02

    def test_ex_date_split_ratio_handling(self):
        """§5.5: on an SPY split ex-date the prior official close is divided
        by the split ratio (executable space, the only adjustment)."""
        r = compute_opening_drop(spy_price_0944=99.0,
                                 spy_prior_official_close=200.0,
                                 spy_split_ratio_on_ex_date=2.0)
        assert r.prior_official_close == 100.0
        assert abs(r.opening_return - (-0.01)) < 1e-12
        assert not r.tripped
