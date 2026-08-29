"""N-02 availability + §19 item 2 / P-1 required-bar semantics."""

import datetime as dt
from decimal import Decimal

import pytest

from trading_core.exits import ExitEvaluationContext, evaluate_exit
from trading_core.gates import check_required_bars
from trading_core.indicators import (
    bars_in_p1_window,
    missing_required_bar_labels,
    session_vwap,
    session_volume_pace,
)
from trading_core.types import Bar, n02_boundary_label


def _bar(label: str, close: str = "100", vol: str = "1000") -> Bar:
    return Bar(ticker="T", o=Decimal(close), h=Decimal(close),
               l=Decimal(close), c=Decimal(close), v=Decimal(vol),
               label=label)


class TestN02Boundaries:
    """At 10:00 the last consumable bar is 09:44; at 15:30 it is 15:14;
    half-day 12:00 -> 11:44; boundary inclusivity."""

    def test_1000_scan(self):
        assert n02_boundary_label("10:00") == "09:44"

    def test_1530_scan(self):
        assert n02_boundary_label("15:30") == "15:14"

    def test_halfday_1200_scan(self):
        assert n02_boundary_label("12:00") == "11:44"

    def test_boundary_inclusive(self):
        bars = {"09:44": _bar("09:44")}
        ctx = ExitEvaluationContext(
            scan_label="10:00", session_date=dt.date(2026, 1, 5),
            scan_datetime=dt.datetime(2026, 1, 5, 10, 0),
            exec_bars_today=bars,
            stop_price=Decimal("90"), target_price=Decimal("110"),
            risk_off_advisory_pending=False, globally_excluded=False,
            confirmed_critical_news=False, bearish_high_active=False)
        result = evaluate_exit(ctx)
        assert not result.suppressed
        assert result.evaluation_bar_label == "09:44"


class TestP1RequiredBars:
    def test_missing_one_required_bar_excludes(self):
        labels = [f"09:{m:02d}" for m in range(30, 40) if m != 33] + ["09:44"]
        check = check_required_bars(labels)
        assert check.excluded
        assert check.missing_labels == ("09:33",)

    def test_missing_0944_excludes(self):
        labels = [f"09:{m:02d}" for m in range(30, 40)]
        check = check_required_bars(labels)
        assert check.excluded and check.missing_labels == ("09:44",)

    def test_sparse_0940_0943_alone_never_excludes(self):
        labels = [f"09:{m:02d}" for m in range(30, 40)] + ["09:44"]
        check = check_required_bars(labels)
        assert not check.excluded
        assert check.missing_labels == ()

    def test_full_set_not_excluded(self):
        labels = [f"09:{m:02d}" for m in range(30, 40)] + \
                 [f"09:{m:02d}" for m in range(40, 45)]
        assert not check_required_bars(labels).excluded

    def test_vwap_zero_volume_guard(self):
        bars = {lab: _bar(lab, vol="0") for lab in
                [f"09:{m:02d}" for m in range(30, 40)] + ["09:44"]}
        assert session_vwap(bars) is None  # VWAP_UNDEFINED_ZERO_VOLUME

    def test_vwap_uses_returned_bars(self):
        bars = {lab: _bar(lab, close="100", vol="100")
                for lab in ["09:30", "09:31", "09:44"]}
        v = session_vwap(bars)
        assert v == Decimal("100")

    def test_pace_uses_returned_bars(self):
        bars = {lab: _bar(lab, vol="10") for lab in ["09:30", "09:44"]}
        priors = [Decimal("40")] * 20
        pace = session_volume_pace(bars, priors)
        assert pace == Decimal("20") / Decimal("40")


class TestExitLastAvailableBar:
    def test_exit_eval_on_earlier_last_available_bar(self):
        """Exit evaluation proceeds on an earlier last-available bar when
        the boundary bar is absent."""
        bars = {"09:35": _bar("09:35", close="95")}
        ctx = ExitEvaluationContext(
            scan_label="10:00", session_date=dt.date(2026, 1, 5),
            scan_datetime=dt.datetime(2026, 1, 5, 10, 0),
            exec_bars_today=bars,
            stop_price=Decimal("90"), target_price=Decimal("110"),
            risk_off_advisory_pending=False, globally_excluded=False,
            confirmed_critical_news=False, bearish_high_active=False)
        result = evaluate_exit(ctx)
        assert not result.suppressed
        assert result.evaluation_bar_label == "09:35"
        assert result.evaluation_price == Decimal("95")

    def test_exit_eval_suppressed_when_no_bar_at_or_before_boundary(self):
        bars = {"09:50": _bar("09:50")}  # after the 09:44 boundary
        ctx = ExitEvaluationContext(
            scan_label="10:00", session_date=dt.date(2026, 1, 5),
            scan_datetime=dt.datetime(2026, 1, 5, 10, 0),
            exec_bars_today=bars,
            stop_price=Decimal("90"), target_price=Decimal("110"),
            risk_off_advisory_pending=False, globally_excluded=False,
            confirmed_critical_news=False, bearish_high_active=False)
        result = evaluate_exit(ctx)
        assert result.suppressed  # EXIT_EVAL_SUPPRESSED

    def test_zero_volume_never_suppresses_exits(self):
        bars = {"09:44": _bar("09:44", close="100", vol="0")}
        ctx = ExitEvaluationContext(
            scan_label="10:00", session_date=dt.date(2026, 1, 5),
            scan_datetime=dt.datetime(2026, 1, 5, 10, 0),
            exec_bars_today=bars,
            stop_price=Decimal("90"), target_price=Decimal("110"),
            risk_off_advisory_pending=False, globally_excluded=False,
            confirmed_critical_news=False, bearish_high_active=False)
        assert not evaluate_exit(ctx).suppressed
