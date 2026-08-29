"""Feed-parity assertion tests (§3.5 / §19 item 8)."""

from decimal import Decimal

import pytest

from backtest.feed_parity import assert_feed_parity
from trading_core.errors import FeedParityViolation
from trading_core.types import Bar


def _bar(feed="sip"):
    return Bar(ticker="T", o=Decimal("1"), h=Decimal("1"), l=Decimal("1"),
               c=Decimal("1"), v=Decimal("1"), feed=feed, label="09:44")


def test_sip_bars_pass():
    assert_feed_parity([_bar(), _bar()], consumer="test")


def test_non_sip_fails_run():
    with pytest.raises(FeedParityViolation) as exc:
        assert_feed_parity([_bar("sip"), _bar("iex")], consumer="G8")
    assert exc.value.reason_code == "FEED_PARITY_VIOLATION"
