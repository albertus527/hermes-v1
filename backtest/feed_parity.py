"""§3.5 feed-parity assertion (Phase 0 hard requirement, §19 item 8).

Live and backtest bars must both be consolidated SIP. Any decision-consumed
bar with feed != 'sip' fails the run (backtest) / is DATA DEGRADED (live).
"""

from __future__ import annotations

from typing import Iterable, Protocol

from trading_core.errors import FeedParityViolation
from trading_core.types import FEED_SIP


class _HasFeed(Protocol):
    @property
    def feed(self) -> str: ...


def assert_feed_parity(bars: Iterable[_HasFeed], *, consumer: str) -> None:
    """Hard assertion over an iterable of bar-like objects with .feed.

    Raises FeedParityViolation (a §19 item 6 deterministic engine halt
    subclass) on the first non-SIP bar.
    """
    for b in bars:
        if b.feed != FEED_SIP:
            raise FeedParityViolation(
                f"{consumer}: decision-consumed bar has feed={b.feed!r} "
                f"(must be 'sip')",
                {"consumer": consumer, "feed": b.feed,
                 "ticker": getattr(b, "ticker", None),
                 "date": str(getattr(b, "date", "") or ""),
                 "label": getattr(b, "label", "")})
