"""R2.7 §9.1–§9.5 canonical sizing (decision time).

Exact Decimal arithmetic. All parameters are ASSUMPTION / MUST TEST per
spec; the formulas themselves are normative.

    risk_dollars  = portfolio_value * risk_per_trade * regime_multiplier
    raw_notional  = risk_dollars / stop_pct
    raw_notional < $10.00 -> REJECT at G10 (rejection, not clamp-up)
    notional      = min(raw_notional, 0.90 * portfolio_value)
    shares_est    = notional / entry_reference_price     [screening only]
    realised_risk = notional * stop_pct                  [screening, N-07]
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

G10_MIN_RAW_NOTIONAL = Decimal("10.00")
DEPLOY_CAP_FRACTION = Decimal("0.90")
GATE_A_THRESHOLD = Decimal("0.25")


@dataclass(frozen=True)
class SizingResult:
    risk_dollars: Decimal
    raw_notional: Decimal
    g10_pass: bool
    notional: Decimal | None          # None when G10 rejects
    shares_est: Decimal | None
    realised_risk: Decimal | None     # screening (N-07)
    capped_by_90pct: bool


def compute_sizing(
    *,
    portfolio_value: Decimal,
    risk_per_trade: Decimal,
    regime_multiplier: Decimal,
    stop_pct: Decimal,
    entry_reference_price: Decimal,
) -> SizingResult:
    """§9.1 canonical sizing. ``stop_pct`` and ``entry_reference_price``
    come from §8.4 (signal-series ratio / executable anchor)."""
    if stop_pct <= 0:
        raise ValueError("stop_pct must be positive")
    if entry_reference_price <= 0:
        raise ValueError("entry_reference_price must be positive")
    risk_dollars = portfolio_value * risk_per_trade * regime_multiplier
    raw_notional = risk_dollars / stop_pct
    if raw_notional < G10_MIN_RAW_NOTIONAL:
        # G10 rejection, not clamp-up (§9.1, D-12, §21 item 11).
        return SizingResult(
            risk_dollars=risk_dollars, raw_notional=raw_notional,
            g10_pass=False, notional=None, shares_est=None,
            realised_risk=None, capped_by_90pct=False)
    cap = DEPLOY_CAP_FRACTION * portfolio_value
    notional = min(raw_notional, cap)
    return SizingResult(
        risk_dollars=risk_dollars,
        raw_notional=raw_notional,
        g10_pass=True,
        notional=notional,
        shares_est=notional / entry_reference_price,
        realised_risk=notional * stop_pct,
        capped_by_90pct=notional < raw_notional,
    )
