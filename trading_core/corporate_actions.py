"""R2.7 corporate-actions deterministic mechanics (§3.6, §13.6).

All event data comes exclusively from the §3.6 contract (designated
provider = Alpaca Corporate Actions endpoint; versioned dataset;
verified-zero attestations via coverage_manifests). Deriving split ratios
or dividends from adjusted/unadjusted price quotients is PROHIBITED
(§3.6 rule 3, §21 item 28) — nothing here computes ratios from prices.

Contents:
- SPLIT mechanics: mechanical share/stop/target adjustment on ex-date
  (§13.6).
- P-A-01 dividend entitlement (ex-date), net dividend, credit timing
  (earliest of pay date / exit fill / window-final official close), full
  attribution to the entitled trade.
- P-A-03 symbol-change/delisting detection rule (the 10:00-scan detection
  predicate; fill timing/pricing is a Phase-3 simulator concern).
- FP-5 verification predicates over coverage_manifests attestations.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

EVENT_SPLIT = "SPLIT"
EVENT_CASH_DIVIDEND = "CASH_DIVIDEND"

DEFAULT_WITHHOLDING_RATE = Decimal("0.30")  # §9.8 default; treaty MUST CONFIRM


@dataclass(frozen=True)
class CorporateAction:
    ticker: str
    event_type: str            # SPLIT | CASH_DIVIDEND
    ex_date: _dt.date
    split_ratio: Decimal | None = None          # SPLIT only
    cash_amount_per_share: Decimal | None = None  # CASH_DIVIDEND only
    record_date: _dt.date | None = None         # provenance only (§3.6 item 7)
    pay_date: _dt.date | None = None
    corp_actions_version: str = ""

    def __post_init__(self) -> None:
        if self.event_type == EVENT_SPLIT and self.split_ratio is None:
            raise ValueError("SPLIT event requires split_ratio")
        if self.event_type == EVENT_CASH_DIVIDEND and self.cash_amount_per_share is None:
            raise ValueError("CASH_DIVIDEND requires cash_amount_per_share")
        if self.event_type not in (EVENT_SPLIT, EVENT_CASH_DIVIDEND):
            raise ValueError(f"unknown event_type {self.event_type!r}")


@dataclass(frozen=True)
class CoverageAttestation:
    """One coverage_manifests record (§16). A verified attestation may cover
    a span with NO events (verified-zero, §3.6 item 4)."""

    source_kind: str           # NEWS | EARNINGS | CORP_ACTIONS | FEE_SCHEDULE
    ticker: str
    span_start: _dt.date
    span_end: _dt.date
    verified: bool
    manifest_version: str

    def covers(self, d: _dt.date) -> bool:
        return self.span_start <= d <= self.span_end


def corp_actions_span_verified(
    *,
    ticker: str,
    span_start: _dt.date,
    span_end: _dt.date,
    attestations: Sequence[CoverageAttestation],
    manifest_version: str,
) -> bool:
    """§3.6 item 4: a (ticker, span) is verified iff the run-pinned
    manifest_version contains a verified CORP_ACTIONS attestation whose
    span covers it. Absence of an attestation -> CORP_ACTIONS_UNVERIFIED."""
    days = span_start
    # The span must be coverable by the union of verified attestations;
    # check day-by-day coverage over the span (attestations may tile).
    covered: set[_dt.date] = set()
    for a in attestations:
        if a.source_kind != "CORP_ACTIONS" or a.ticker != ticker:
            continue
        if not a.verified or a.manifest_version != manifest_version:
            continue
        d = max(a.span_start, span_start)
        while d <= min(a.span_end, span_end):
            covered.add(d)
            d += _dt.timedelta(days=1)
    while days <= span_end:
        if days not in covered:
            return False
        days += _dt.timedelta(days=1)
    return True


# ---------------------------------------------------------------------------
# §13.6 split mechanics
# ---------------------------------------------------------------------------


def apply_split(
    *,
    shares: Decimal,
    stop_price: Decimal,
    target_price: Decimal,
    split_ratio: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """Mechanical adjustment of shares, stop_price, target_price by the
    split ratio on ex-date. ``split_ratio`` is new-shares-per-old-share
    (a 2-for-1 split has ratio 2)."""
    if split_ratio <= 0:
        raise ValueError("split_ratio must be positive")
    return shares * split_ratio, stop_price / split_ratio, target_price / split_ratio


# ---------------------------------------------------------------------------
# P-A-01 dividend mechanics
# ---------------------------------------------------------------------------


def dividend_entitled(*, shares: Decimal, **_: object) -> bool:
    """P-A-01: entitled iff shares > 0 immediately before the opening of the
    ex-date session (caller supplies the ex-date post-split count)."""
    return shares > 0


def net_dividend(
    *,
    shares_at_ex_date: Decimal,
    cash_amount_per_share: Decimal,
    withholding_rate: Decimal = DEFAULT_WITHHOLDING_RATE,
) -> Decimal:
    """P-A-01: shares_held_at_ex_date * amount * (1 - withholding_rate)."""
    return shares_at_ex_date * cash_amount_per_share * (Decimal(1) - withholding_rate)


def dividend_credit_date(
    *,
    event: CorporateAction,
    exit_fill_ts: _dt.datetime | None,
    window_final_close: tuple[_dt.date, _dt.datetime] | None,
) -> tuple[str, _dt.date | _dt.datetime]:
    """P-A-01 credit timing: the EARLIEST of (i) pay date, (ii) the
    position's exit fill timestamp, (iii) the window's final official close.

    Returns (branch, comparable timestamp). N-25 resolution of an absent
    official close is the caller's concern (trading_core/official_prices).
    """
    candidates: list[tuple[str, _dt.datetime]] = []
    if event.pay_date is not None:
        candidates.append(("PAY_DATE", _dt.datetime(
            event.pay_date.year, event.pay_date.month, event.pay_date.day)))
    if exit_fill_ts is not None:
        candidates.append(("EXIT_FILL", exit_fill_ts))
    if window_final_close is not None:
        candidates.append(("WINDOW_FINAL_CLOSE", window_final_close[1]))
    if not candidates:
        raise ValueError("no dividend credit branch available")
    return min(candidates, key=lambda kv: kv[1])


# ---------------------------------------------------------------------------
# P-A-03 symbol-change / delisting detection
# ---------------------------------------------------------------------------


def corp_event_force_close_detected(
    *,
    session_date: _dt.date,
    ticker_has_executable_bar_in_session: bool,
    universe_records_delisting_or_symbol_change: bool,
) -> bool:
    """P-A-03: the detection scan is the 10:00 scan of the first trading
    day on which BOTH (a) the open position's ticker has no executable bar
    and (b) universe metadata records a delisting or symbol change.

    Fill timestamp then follows the run's exit scenario (§13.3); fill price
    is the last available executable price with sell slippage; full sell
    fees with the fill date as the §9.7 fee-computation date. Those are
    Phase-3 simulator mechanics; this predicate is the deterministic
    detection half (Phase 1)."""
    return (not ticker_has_executable_bar_in_session
            and universe_records_delisting_or_symbol_change)
