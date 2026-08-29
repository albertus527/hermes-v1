"""§7.1 pipeline ordering tests: S0.0 before S0.1; N-10 same-scan lockout;
Stage-5 no-resumption; §5.5 computed even when S0.3 would end the pipeline.
"""

import datetime as dt
from decimal import Decimal

from trading_core.engine import GlobalInputs, scan_main_1000
from trading_core.exits import RiskOffAdvisoryState
from trading_core.pipeline import OpenPosition, PendingNextSessionEntry
from trading_core.regime import OpeningDropResult, RegimeResult
from trading_core.types import Bar

SESSION = dt.date(2026, 1, 5)
SCAN_T = dt.datetime(2026, 1, 5, 10, 0)
WINDOW_END = dt.date(2026, 6, 30)


def _regime(name="RISK_ON", mult=1.0):
    return RegimeResult(trend_score=4, vol_state="NORMAL", regime=name,
                        multiplier=mult, new_longs_allowed=True,
                        vix_value=20.0, vix_date_used=SESSION,
                        t_minus_1=SESSION)


def _opening_drop(tripped=False):
    def compute(**kw):
        return OpeningDropResult(
            opening_return=-0.03 if tripped else 0.001,
            tripped=tripped, price_0944=kw["spy_price_0944"],
            prior_official_close=kw["spy_prior_official_close"],
            split_ratio_applied=kw.get("spy_split_ratio_on_ex_date"))
    return compute


def _global(**kw):
    defaults = dict(regime=_regime(), data_fresh=True,
                    globally_excluded=False,
                    spy_0944_price=Decimal("500"),
                    spy_prior_official_close=Decimal("500"))
    defaults.update(kw)
    return GlobalInputs(**defaults)


def _scan(**kw):
    defaults = dict(
        session_date=SESSION, scan_datetime=SCAN_T, snapshots=[],
        universe=set(), run_surface="backtest", gbl=_global(),
        pending_next_session=None, open_position=None,
        risk_off_state=None, window_end=WINDOW_END,
        portfolio_value=Decimal("200"), risk_per_trade=Decimal("0.01"),
        stop_mult=Decimal("2.5"), fee_regulatory=None,
        vat_on_regulatory=False,
        fee_rounding=None, run_role="TRAIN",
        trading_sessions=[SESSION], official_closes={},
        compute_opening_drop=_opening_drop(),
        is_trading_day=lambda d: True,
        next_trading_day=lambda d: d + dt.timedelta(days=1),
    )
    defaults.update(kw)
    return scan_main_1000(**defaults)


class TestS0Ordering:
    def test_s00_resolves_before_data_freshness(self):
        """S0.0 voiding happens even when S0.1 would halt (data stale)."""
        cand = PendingNextSessionEntry(ticker="T", scheduled_fill_date=SESSION)
        result = _scan(pending_next_session=cand,
                       gbl=_global(data_fresh=False),
                       compute_opening_drop=_opening_drop(tripped=True))
        codes = [c for c, _ in result.events.codes]
        assert codes.index("NEXT_SESSION_VOIDED") < codes.index("DATA_DEGRADED")

    def test_s00_no_filter_expires(self):
        cand = PendingNextSessionEntry(ticker="T", scheduled_fill_date=SESSION)
        result = _scan(pending_next_session=cand,
                       gbl=_global(spy_0944_price=None))
        codes = [c for c, _ in result.events.codes]
        assert "ENTRY_UNFILLABLE_NO_FILTER" in codes

    def test_s00_window_end_precedence(self):
        cand = PendingNextSessionEntry(ticker="T", scheduled_fill_date=SESSION)
        result = _scan(pending_next_session=cand,
                       window_end=SESSION - dt.timedelta(days=1))
        codes = [c for c, _ in result.events.codes]
        assert "ENTRY_UNFILLABLE_WINDOW_END" in codes

    def test_n10_same_scan_lockout(self):
        """An exit trigger at S0.2 ends the pipeline before Stage 1."""
        pos = OpenPosition(ticker="T", stop_price=Decimal("90"),
                           target_price=Decimal("110"),
                           entry_fill_ts=SCAN_T)
        bars = {"09:44": Bar(ticker="T", o=Decimal("85"), h=Decimal("85"),
                             l=Decimal("85"), c=Decimal("85"),
                             v=Decimal("10"), label="09:44")}
        result = _scan(open_position=pos,
                       gbl=_global(position_bars={"T": bars}))
        assert result.action == "EXIT"
        assert result.stage3_ranking == []
        assert result.exit_evaluation.fired is not None

    def test_s03_position_open_blocks_entries(self):
        pos = OpenPosition(ticker="T", stop_price=Decimal("90"),
                           target_price=Decimal("110"),
                           entry_fill_ts=SCAN_T)
        bars = {"09:44": Bar(ticker="T", o=Decimal("100"), h=Decimal("100"),
                             l=Decimal("100"), c=Decimal("100"),
                             v=Decimal("10"), label="09:44")}
        result = _scan(open_position=pos,
                       gbl=_global(position_bars={"T": bars}))
        assert result.action == "NO ACTION"
        assert result.reason_code == "MAX_POSITIONS"

    def test_opening_drop_computed_despite_s03(self):
        """§5.5 is computed whenever S0.0/S0.4 requires it, even when S0.3
        would otherwise end the pipeline — the S0.0 path proves it: a
        scheduled NEXT_SESSION candidate computes the filter first."""
        cand = PendingNextSessionEntry(ticker="T", scheduled_fill_date=SESSION)
        pos = OpenPosition(ticker="X", stop_price=Decimal("90"),
                           target_price=Decimal("110"),
                           entry_fill_ts=SCAN_T)
        bars = {"09:44": Bar(ticker="X", o=Decimal("100"), h=Decimal("100"),
                             l=Decimal("100"), c=Decimal("100"),
                             v=Decimal("10"), label="09:44")}
        result = _scan(pending_next_session=cand, open_position=pos,
                       gbl=_global(position_bars={"X": bars}))
        assert result.opening_drop is not None  # computed despite position open

    def test_s04_opening_drop_suppresses(self):
        result = _scan(compute_opening_drop=_opening_drop(tripped=True))
        assert result.reason_code == "OPENING_DROP"

    def test_s05_risk_off_blocks(self):
        result = _scan(gbl=_global(regime=_regime("RISK_OFF", None)))
        assert result.reason_code == "RISK_OFF"
