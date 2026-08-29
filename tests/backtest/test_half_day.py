"""Half-day DELAYED sequencing (FP-6) + §13.2/§13.3 scenario timing
foundations (Phase-1 deterministic constants; fills are Phase-3)."""

import datetime as dt

from backtest.calendar import session_close
from trading_core.types import n02_boundary_label


def test_half_day_delays_fill_after_1200_exit_scan():
    """FP-6: a half-day DELAYED entry fill (12:00) is sequenced AFTER the
    12:00 exit scan; its first exit evaluation is the next trading day's
    10:00 scan. This test pins the schedule constants."""
    # Half-day exit scan boundary: 12:00 scan -> last bar 11:44
    assert n02_boundary_label("12:00") == "11:44"
    # The 11:44 evaluation bar predates the 12:00 fill -> the fill is not
    # exit-evaluated at that scan.
    assert n02_boundary_label("12:00") < "12:00"


def test_half_day_constants():
    import pytest
    pytest.importorskip("pandas_market_calendars")
    # 2025-11-28: 13:00 close half-day
    close = session_close(dt.date(2025, 11, 28))
    assert (close.hour, close.minute) == (13, 0)
