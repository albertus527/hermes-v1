"""P-3 RISK_OFF same-session advisory persistence + P-A-05 per-position
trend failure + exit priority ordering (§8.6)."""

import datetime as dt
from decimal import Decimal

import numpy as np

from trading_core.exits import (
    ExitEvaluationContext,
    RiskOffAdvisoryState,
    evaluate_exit,
    trend_failure_condition,
)
from trading_core.types import Bar, ExitReason

D = dt.date(2026, 1, 5)


def _ctx(bars, **kw):
    defaults = dict(
        scan_label="10:00", session_date=D,
        scan_datetime=dt.datetime(2026, 1, 5, 10, 0),
        exec_bars_today=bars,
        stop_price=Decimal("90"), target_price=Decimal("110"),
        risk_off_advisory_pending=False, globally_excluded=False,
        confirmed_critical_news=False, bearish_high_active=False,
        signal_closes=(),
    )
    defaults.update(kw)
    return ExitEvaluationContext(**defaults)


def _bar(label, close="100"):
    return Bar(ticker="T", o=Decimal(close), h=Decimal(close),
               l=Decimal(close), c=Decimal(close), v=Decimal("100"),
               label=label)


class TestExitPriority:
    def test_stop_beats_target_and_others(self):
        bars = {"09:44": _bar("09:44", close="85")}  # below stop 90
        r = evaluate_exit(_ctx(bars, confirmed_critical_news=True,
                               risk_off_advisory_pending=True))
        assert r.fired is ExitReason.STOP

    def test_risk_off_beats_target(self):
        bars = {"09:44": _bar("09:44", close="115")}  # above target 110
        r = evaluate_exit(_ctx(bars, risk_off_advisory_pending=True))
        assert r.fired is ExitReason.REGIME_RISK_OFF

    def test_critical_news_beats_target(self):
        bars = {"09:44": _bar("09:44", close="115")}
        r = evaluate_exit(_ctx(bars, confirmed_critical_news=True))
        assert r.fired is ExitReason.NEWS_CRITICAL

    def test_target_reached(self):
        bars = {"09:44": _bar("09:44", close="115")}
        r = evaluate_exit(_ctx(bars))
        assert r.fired is ExitReason.TARGET

    def test_trend_failure(self):
        # 60 declining closes -> both last bars below EMA50
        closes = list(np.linspace(200, 100, 60))
        bars = {"09:44": _bar("09:44", close="100")}
        r = evaluate_exit(_ctx(bars, signal_closes=closes))
        assert r.fired is ExitReason.TREND_FAILURE

    def test_trim_advisory_is_lowest_priority(self):
        bars = {"09:44": _bar("09:44", close="100")}
        r = evaluate_exit(_ctx(bars, bearish_high_active=True))
        assert r.fired is ExitReason.TRIM_ADVISORY

    def test_no_trigger(self):
        bars = {"09:44": _bar("09:44", close="100")}
        assert evaluate_exit(_ctx(bars)).fired is None


class TestRiskOffPersistence:
    """P-3: suppressed scan does not disarm; advisory issues at the next
    non-suppressed exit-evaluating scan of the same session; lapses at
    session close if unissued."""

    def test_pending_then_issued_same_session(self):
        state = RiskOffAdvisoryState(detection_session=D)
        # 10:00 suppressed (no bars) -> PENDING event
        out = state.on_scan("10:00", suppressed=True, position_open=True)
        assert out == "RISK_OFF_ADVISORY_PENDING"
        assert state.pending
        # 15:30 not suppressed -> ISSUED
        out = state.on_scan("15:30", suppressed=False, position_open=True)
        assert out == "ISSUED"
        assert not state.pending and state.issued_scan == "15:30"

    def test_unissued_lapses_at_session_close(self):
        state = RiskOffAdvisoryState(detection_session=D)
        state.on_scan("10:00", suppressed=True, position_open=True)
        state.on_scan("15:30", suppressed=True, position_open=True)
        out = state.on_session_close(position_open=True)
        assert out == "RISK_OFF_ADVISORY_LAPSED"
        assert not state.pending

    def test_position_close_clears_without_lapse(self):
        state = RiskOffAdvisoryState(detection_session=D)
        state.on_scan("10:00", suppressed=False, position_open=False)
        assert state.on_session_close(position_open=False) is None


class TestTrendFailureCondition:
    def test_two_closes_below_ema50(self):
        closes = list(np.linspace(200, 100, 60))
        assert trend_failure_condition(closes)

    def test_not_when_recovered(self):
        closes = list(np.linspace(200, 100, 58)) + [150, 250]
        assert not trend_failure_condition(closes)

    def test_insufficient_history(self):
        assert not trend_failure_condition([100.0] * 40)
