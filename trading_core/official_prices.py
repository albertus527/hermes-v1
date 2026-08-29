"""R2.7 N-22/N-25 official open/close resolution (P-2).

N-22: "official close" = close of the daily executable bar for that
session; "official open" = its open. The last intraday minute bar is never
substituted for either.

N-25: wherever a rule requires the official open/close of a nominal date,
the value is taken from the daily executable bar of the nearest exchange
trading session at or before that nominal date for which the bar exists;
if no such session exists at or after the window start, the nearest
following such session inside the window is used. If neither exists inside
the window, the affected test window is excluded
(WINDOW_EXCLUDED_OFFICIAL_PRICE_UNAVAILABLE).

Every substitution is logged with the nominal date, the substituted
session, the consuming rule, and the OPEN/CLOSE side (§16 rule 8).
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping

from trading_core.types import ADJUSTMENT_RAW, DailyBar, FEED_SIP


@dataclass(frozen=True)
class OfficialPriceSubstitution:
    nominal_date: _dt.date
    substituted_session: _dt.date
    consuming_rule: str
    side: str  # "OPEN" | "CLOSE"


@dataclass
class OfficialPriceLog:
    """Collects every N-25 substitution for §16 persistence."""

    substitutions: list[OfficialPriceSubstitution] = field(default_factory=list)
    unresolvable: list[OfficialPriceSubstitution] = field(default_factory=list)

    def record(self, sub: OfficialPriceSubstitution) -> None:
        self.substitutions.append(sub)


class OfficialPriceUnresolvable(Exception):
    """N-25: neither at-or-before nor following session exists inside the
    window -> the affected test window is excluded."""

    def __init__(self, sub: OfficialPriceSubstitution):
        self.substitution = sub
        super().__init__(
            f"WINDOW_EXCLUDED_OFFICIAL_PRICE_UNAVAILABLE: nominal "
            f"{sub.nominal_date} ({sub.side}) for {sub.consuming_rule}")


def _daily_exec_bars_index(
    bars: Mapping[_dt.date, DailyBar] | list[DailyBar],
) -> dict[_dt.date, DailyBar]:
    """Index executable daily bars by session date, enforcing the §3.5
    feed-parity contract on this consumer."""
    if not isinstance(bars, Mapping):
        bars = {b.date: b for b in bars}
    out: dict[_dt.date, DailyBar] = {}
    for d, b in bars.items():
        if b.adjustment != ADJUSTMENT_RAW:
            raise ValueError(
                f"official prices consume the executable (unadjusted) series; "
                f"got adjustment={b.adjustment!r} for {b.ticker} {d}")
        if b.feed != FEED_SIP:
            from trading_core.errors import FeedParityViolation
            raise FeedParityViolation(
                f"official-price bar {b.ticker} {d} has feed={b.feed!r}",
                {"ticker": b.ticker, "date": str(d), "feed": b.feed})
        out[d] = b
    return out


def resolve_official_price(
    *,
    nominal_date: _dt.date,
    side: str,                      # "OPEN" | "CLOSE"
    consuming_rule: str,
    bars: Mapping[_dt.date, DailyBar] | list[DailyBar],
    window_start: _dt.date,
    window_end: _dt.date,
    log: OfficialPriceLog | None = None,
) -> tuple[_dt.date, Decimal]:
    """Resolve the official open/close of ``nominal_date`` under N-25.

    Returns (substituted_session, price). Raises
    ``OfficialPriceUnresolvable`` when no qualifying daily executable bar
    exists inside the window.
    """
    if side not in ("OPEN", "CLOSE"):
        raise ValueError("side must be 'OPEN' or 'CLOSE'")
    index = _daily_exec_bars_index(bars)

    # Nearest session at or before the nominal date with an existing bar.
    at_or_before = [d for d in index if d <= nominal_date]
    if at_or_before:
        session = max(at_or_before)
    else:
        # N-25 fallback: only when no such session exists at or after the
        # window start — take the nearest following session inside the
        # window.
        following = [d for d in index
                     if window_start <= d <= window_end and d > nominal_date]
        if not following:
            sub = OfficialPriceSubstitution(nominal_date, nominal_date,
                                            consuming_rule, side)
            if log is not None:
                log.unresolvable.append(sub)
            raise OfficialPriceUnresolvable(sub)
        session = min(following)

    bar = index[session]
    price = bar.o if side == "OPEN" else bar.c
    if log is not None and session != nominal_date:
        log.record(OfficialPriceSubstitution(nominal_date, session,
                                             consuming_rule, side))
    return session, price
