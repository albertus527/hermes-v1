"""N-25 official-price substitution + daily-equity emission semantics (P-2)."""

import datetime as dt
from decimal import Decimal

import pytest

from trading_core.official_prices import (
    OfficialPriceLog,
    OfficialPriceUnresolvable,
    resolve_official_price,
)
from trading_core.types import ADJUSTMENT_RAW, ADJUSTMENT_SPLIT, DailyBar

W_START = dt.date(2020, 1, 1)
W_END = dt.date(2020, 6, 30)


def _bar(d: dt.date, o="100", c="101") -> DailyBar:
    return DailyBar(ticker="T", date=d, o=Decimal(o), h=Decimal(c),
                    l=Decimal(o), c=Decimal(c), v=Decimal("1000"))


class TestN25Substitution:
    def test_exact_date_no_substitution_logged(self):
        bars = {dt.date(2020, 3, 2): _bar(dt.date(2020, 3, 2))}
        log = OfficialPriceLog()
        session, price = resolve_official_price(
            nominal_date=dt.date(2020, 3, 2), side="CLOSE",
            consuming_rule="TEST", bars=bars,
            window_start=W_START, window_end=W_END, log=log)
        assert session == dt.date(2020, 3, 2)
        assert price == Decimal("101")
        assert log.substitutions == []

    def test_nearest_at_or_before(self):
        bars = {dt.date(2020, 3, 2): _bar(dt.date(2020, 3, 2)),
                dt.date(2020, 3, 4): _bar(dt.date(2020, 3, 4), c="105")}
        log = OfficialPriceLog()
        # nominal 2020-03-03 (no bar) -> falls back to 2020-03-02
        session, price = resolve_official_price(
            nominal_date=dt.date(2020, 3, 3), side="CLOSE",
            consuming_rule="WINDOW_END_FORCE_CLOSE", bars=bars,
            window_start=W_START, window_end=W_END, log=log)
        assert session == dt.date(2020, 3, 2)
        assert price == Decimal("101")
        assert len(log.substitutions) == 1
        sub = log.substitutions[0]
        assert sub.nominal_date == dt.date(2020, 3, 3)
        assert sub.substituted_session == dt.date(2020, 3, 2)
        assert sub.side == "CLOSE"

    def test_nearest_following_fallback(self):
        """No session at-or-before inside the window -> nearest following."""
        bars = {dt.date(2020, 1, 10): _bar(dt.date(2020, 1, 10), o="55")}
        log = OfficialPriceLog()
        session, price = resolve_official_price(
            nominal_date=dt.date(2020, 1, 1), side="OPEN",
            consuming_rule="BENCHMARK_PURCHASE", bars=bars,
            window_start=W_START, window_end=W_END, log=log)
        assert session == dt.date(2020, 1, 10)
        assert price == Decimal("55")
        assert log.substitutions[0].side == "OPEN"

    def test_unresolvable_window_excluded(self):
        bars = {}
        log = OfficialPriceLog()
        with pytest.raises(OfficialPriceUnresolvable):
            resolve_official_price(
                nominal_date=dt.date(2020, 3, 3), side="CLOSE",
                consuming_rule="WINDOW_END_FORCE_CLOSE", bars=bars,
                window_start=W_START, window_end=W_END, log=log)
        assert len(log.unresolvable) == 1  # WINDOW_EXCLUDED_OFFICIAL_PRICE_UNAVAILABLE

    def test_feed_parity_enforced(self):
        bad = DailyBar(ticker="T", date=dt.date(2020, 3, 2),
                       o=Decimal("1"), h=Decimal("1"), l=Decimal("1"),
                       c=Decimal("1"), v=Decimal("1"), feed="iex")
        from trading_core.errors import FeedParityViolation
        with pytest.raises(FeedParityViolation):
            resolve_official_price(
                nominal_date=dt.date(2020, 3, 2), side="CLOSE",
                consuming_rule="TEST", bars=[bad],
                window_start=W_START, window_end=W_END)

    def test_adjustment_must_be_executable(self):
        bad = DailyBar(ticker="T", date=dt.date(2020, 3, 2),
                       o=Decimal("1"), h=Decimal("1"), l=Decimal("1"),
                       c=Decimal("1"), v=Decimal("1"),
                       adjustment=ADJUSTMENT_SPLIT)
        with pytest.raises(ValueError):
            resolve_official_price(
                nominal_date=dt.date(2020, 3, 2), side="CLOSE",
                consuming_rule="TEST", bars=[bad],
                window_start=W_START, window_end=W_END)
