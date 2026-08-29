"""R2.7 §8.4/§8.5/§8.7 stop and target calculations (reference-anchored).

N-06: stop and target are anchored to ``entry_reference_price`` (the
09:44-bar close, executable series), fixed at decision time, never
re-anchored to the fill price. Percentage form everywhere; no
absolute-dollar variant; not trailing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

DEFAULT_STOP_MULT = Decimal("2.5")   # ASSUMPTION / MUST TEST (§8.4)
TARGET_R_MULTIPLE = Decimal("2")     # §8.5 take-profit 2R — ASSUMPTION / MUST TEST


@dataclass(frozen=True)
class StopTarget:
    stop_pct: Decimal        # signal-series ratio: stop_mult * ATR14 / close(T-1)
    stop_price: Decimal      # executable space
    target_price: Decimal    # executable space


def compute_stop_pct(
    *,
    atr14: Decimal,
    close_t_minus_1: Decimal,
    stop_mult: Decimal = DEFAULT_STOP_MULT,
) -> Decimal:
    """§8.4: stop_pct = stop_mult * ATR14 / close(T-1) [signal-series ratio]."""
    if close_t_minus_1 <= 0:
        raise ValueError("close_t_minus_1 must be positive")
    return stop_mult * atr14 / close_t_minus_1


def compute_stop_target(
    *,
    entry_reference_price: Decimal,
    stop_pct: Decimal,
) -> StopTarget:
    """§8.4/§8.5: reference-anchored stop and 2R target (executable space)."""
    if entry_reference_price <= 0:
        raise ValueError("entry_reference_price must be positive")
    return StopTarget(
        stop_pct=stop_pct,
        stop_price=entry_reference_price * (Decimal(1) - stop_pct),
        target_price=entry_reference_price * (Decimal(1) + TARGET_R_MULTIPLE * stop_pct),
    )


def adopted_position_synthetic_stop(
    *,
    reference_price: Decimal,
    atr14: Decimal,
    close_t_minus_1: Decimal,
    stop_mult: Decimal = DEFAULT_STOP_MULT,
) -> StopTarget:
    """§8.7 synthetic stop for ADOPTED positions. ``reference_price`` is the
    extracted average cost if available, else close(T-1) (executable).
    Tagged SYNTHETIC everywhere downstream."""
    pct = compute_stop_pct(atr14=atr14, close_t_minus_1=close_t_minus_1,
                           stop_mult=stop_mult)
    return compute_stop_target(entry_reference_price=reference_price, stop_pct=pct)


# ---------------------------------------------------------------------------
# §15.2 audit-metric formulas (RP-10)
# ---------------------------------------------------------------------------


def stop_overshoot(*, stop_price: Decimal, exit_fill_price: Decimal) -> Decimal:
    """§15.2: (stop_price - exit_fill_price) / stop_price; reported only for
    exit_reason = STOP; negative values are favourable fills."""
    return (stop_price - exit_fill_price) / stop_price


def realised_risk_actual(*, shares_filled: Decimal, fill_price: Decimal,
                         stop_price: Decimal) -> Decimal:
    """N-07/§9.3: signed; may be <= 0 under ENTERED_BEYOND_STOP (§13.2)."""
    return shares_filled * (fill_price - stop_price)


def risk_divergence(*, realised_risk_actual_value: Decimal,
                    realised_risk: Decimal) -> Decimal:
    """§15.2: realised_risk_actual / realised_risk - 1 (signed)."""
    return realised_risk_actual_value / realised_risk - Decimal(1)
