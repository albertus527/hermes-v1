"""Stage 2/3 scoring + Stage 5 no-resumption tests (§7.1, N-13)."""

import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

from trading_core.scoring import (
    ScoreInputs,
    active_cutoff,
    display_band,
    rank_candidates,
    score_candidate,
)
from trading_core.types import REGIME_NEUTRAL, REGIME_RISK_ON, Bar

ET = ZoneInfo("America/New_York")


def _bar(label, close="100", vol="100"):
    return Bar(ticker="T", o=Decimal(close), h=Decimal(close),
               l=Decimal(close), c=Decimal(close), v=Decimal(vol),
               label=label)


def _inputs(ticker="AAA", **kw):
    bars = {f"09:{m:02d}": _bar(f"09:{m:02d}") for m in range(30, 40)}
    bars["09:44"] = _bar("09:44")
    defaults = dict(
        ticker=ticker, ema20=110.0, ema50=100.0, ema200=90.0,
        close_t_minus_1=115.0, atr14=2.0,
        rsi14_t_minus_1=60.0, rsi14_t_minus_6=50.0, rs20_vs_spy=0.05,
        daily_dollar_volume_recent=2.0, daily_dollar_volume_prior=1.0,
        price_0944=Decimal("101"), session_vwap=Decimal("100"),
        exec_bars_today=bars,
        session_pace=Decimal("1.5"), catalyst_points=0,
    )
    defaults.update(kw)
    return ScoreInputs(**defaults)


class TestScoring:
    def test_perfect_candidate_scores_max(self):
        r = score_candidate(_inputs())
        # a = 2/115; vwap_dist = 15*min(1, (1/100)/a) = 15*0.575 = 8.625
        # trend 30 + momentum 25 + intraday (8.625 + 10) + volume 10 = 83.625 -> 84
        assert r.components["trend"] == 30.0
        assert r.components["momentum"] == 25.0
        assert r.components["intraday_or_break"] == 10.0
        assert r.components["volume"] == 10.0
        assert r.final_total_int == 84

    def test_n13_half_up_rounding(self):
        r = score_candidate(_inputs(rsi14_t_minus_1=45.0,
                                    rsi14_t_minus_6=None,
                                    rs20_vs_spy=None,
                                    session_pace=Decimal("0.5"),
                                    daily_dollar_volume_recent=0.5))
        # trend: ema20>ema50>ema200 (+10) and close>ema20 (+10) = 20;
        # momentum 0; intraday vwap_dist 8.625; or_break 0; volume 0
        # -> raw 28.625 -> 29
        assert r.raw_total == Decimal("28.625")
        assert r.final_total_int == 29

    def test_catalyst_clip(self):
        r = score_candidate(_inputs(catalyst_points=15))
        # clipped upstream to ±10; passing 15 is out-of-contract, but the
        # sum must still use the given value (clip lives in news_effects)
        assert r.components["catalyst"] == 15.0


class TestRanking:
    def test_tiebreak_ascending_ticker(self):
        a = score_candidate(_inputs(ticker="ZZZ"))
        b = score_candidate(_inputs(ticker="AAA"))
        ranked = rank_candidates([a, b], active_cutoff(REGIME_RISK_ON))
        assert [r.ticker for r in ranked] == ["AAA", "ZZZ"]

    def test_below_cutoff_discarded(self):
        low = score_candidate(_inputs(
            ticker="LOW", rsi14_t_minus_1=45.0, rsi14_t_minus_6=None,
            rs20_vs_spy=None, session_pace=Decimal("0.5"),
            daily_dollar_volume_recent=0.5, ema20=95.0))
        ranked = rank_candidates([low], active_cutoff(REGIME_RISK_ON))
        assert ranked == []

    def test_neutral_cutoff_raised(self):
        assert active_cutoff(REGIME_NEUTRAL) == 80
        assert active_cutoff(REGIME_RISK_ON) == 70

    def test_display_bands(self):
        assert display_band(90, 70) == "Strong"
        assert display_band(70, 70) == "Moderate"
        assert display_band(69, 70) == "below-cutoff"
