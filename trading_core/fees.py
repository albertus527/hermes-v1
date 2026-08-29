"""R2.7 canonical fee model (spec §9.6/§9.7) — the Phase-0 gate module.

Everything is ``decimal.Decimal`` arithmetic so the §1.4 six-branch
worked-example targets reproduce exactly. This is THE single fee
implementation for live and backtest.

§9.6 formula summary (Pluang components, per side):

    transaction = 0.30% * notional_side
    jfx_kbi     = min(0.05% * notional_side, $0.10)
    SEC (sell)  = max($0.0000206 * sell_notional, $0.01)
    TAF (sell)  = max($0.000166 * shares, $0.01), cap $8.30
    CAT (sell)  = max($0.0000265 * shares, $0.01)

VAT 11% applies to transaction + JFX/KBI; to regulatory components only
under the ``vat_on_regulatory = true`` sensitivity branch. Rounding applies
per component (or per side) AFTER VAT where VAT applies; the schedule $0.01
minimums and the TAF cap are schedule terms applied BEFORE rounding. A
TRAIN_SUBSTITUTED_ZERO component is omitted from the formula entirely and
can never be lifted above $0.00 (§9.6/§9.7 item 3).

Under ``CEIL_CENT_PER_SIDE`` the charged amount is a single side total
(ceiling of the sum of post-VAT positive components); per-component rows
retain their exact post-VAT amounts so every §16 ``fee_calculations`` row
remains reconstructible, and a ``side_total`` carries the charged value.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from enum import Enum

from trading_core.errors import FeeScheduleUnverified, TrainFeeSubstitutionViolation
from trading_core.types import ROLE_LIVE, ROLE_TEST, ROLE_TRAIN

# ---------------------------------------------------------------------------
# Current published Pluang broker constants (§9.6) — KNOWN current schedule;
# backward-projected across the full backtest window per FP-8/§13.9 item 18
# (provenance BACKWARD_PROJECTED_CONSTANT).
# ---------------------------------------------------------------------------

TRANSACTION_RATE = Decimal("0.003")            # 0.30% of notional, per side
JFX_KBI_RATE = Decimal("0.0005")               # 0.05% of notional, per side
JFX_KBI_CAP = Decimal("0.10")                  # $0.10 cap
VAT_RATE = Decimal("0.11")                     # 11%
CENT = Decimal("0.01")
TAF_CAP = Decimal("8.30")

_REGULATORY_COMPONENTS = ("SEC", "TAF", "CAT")


class RoundingBranch(Enum):
    CEIL_CENT_PER_COMPONENT = "CEIL_CENT_PER_COMPONENT"   # canonical
    CEIL_CENT_PER_SIDE = "CEIL_CENT_PER_SIDE"
    EXACT = "EXACT"


class FeeInputStatus(Enum):
    VERIFIED_RATE = "VERIFIED_RATE"
    VERIFIED_ZERO = "VERIFIED_ZERO"
    TRAIN_SUBSTITUTED_ZERO = "TRAIN_SUBSTITUTED_ZERO"
    BACKWARD_PROJECTED_CONSTANT = "BACKWARD_PROJECTED_CONSTANT"


class FeeContext(Enum):
    SCREENING_RT = "SCREENING_RT"
    ENTRY_ACTUAL = "ENTRY_ACTUAL"
    EXIT_ACTUAL = "EXIT_ACTUAL"


# §9.7 item 3: regulatory substitution is legal only in these contexts
_SUBSTITUTABLE_CONTEXTS = (FeeContext.SCREENING_RT, FeeContext.EXIT_ACTUAL)


@dataclass(frozen=True)
class RegulatoryRates:
    """Resolved SEC/TAF/CAT state for one fee-computation date.

    ``rates[c]`` holds the Decimal rate when the status is VERIFIED_RATE.
    A component absent from ``statuses`` is treated as unverified
    (TRAIN_SUBSTITUTED_ZERO-eligible in TRAIN runs; illegal elsewhere).
    """

    statuses: dict[str, FeeInputStatus]
    rates: dict[str, Decimal] = field(default_factory=dict)
    schedule_version: str = ""

    def status_of(self, component: str) -> FeeInputStatus:
        return self.statuses.get(component, FeeInputStatus.TRAIN_SUBSTITUTED_ZERO)

    def rate_of(self, component: str) -> Decimal | None:
        return self.rates.get(component)


@dataclass(frozen=True)
class FeeComponent:
    """One priced fee component (§16 fee_calculations row shape)."""

    side: str                      # "buy" | "sell"
    component: str                 # TRANSACTION | JFX_KBI | SEC | TAF | CAT
    base_amount: Decimal           # after schedule min/cap, before VAT
    vat_amount: Decimal
    rounded_amount: Decimal        # per-component charged amount (see module docstring for CEIL_CENT_PER_SIDE)
    fee_context: FeeContext
    fee_computation_date: _dt.date
    fee_input_status: FeeInputStatus
    schedule_version: str = ""


@dataclass(frozen=True)
class FeeComputation:
    components: tuple[FeeComponent, ...]
    fees_buy: Decimal
    fees_sell: Decimal
    fees_rt: Decimal
    substituted_components: tuple[str, ...] = ()
    schedule_version: str = ""

    @property
    def train_fee_substituted(self) -> bool:
        return bool(self.substituted_components)


@dataclass(frozen=True)
class SideFees:
    """Result for one priced side."""

    components: tuple[FeeComponent, ...]
    total: Decimal                 # the amount actually charged for the side


def _ceil_cent(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_CEILING)


def compute_side_fees(
    *,
    side: str,
    notional: Decimal,
    shares: Decimal,
    fee_computation_date: _dt.date,
    fee_context: FeeContext,
    regulatory: RegulatoryRates,
    vat_on_regulatory: bool,
    rounding: RoundingBranch,
    run_role: str,
) -> SideFees:
    """Price one side of a trade. Pure; exact; no I/O, no wall clock.

    Role rules (§9.7 item 3): a TRAIN_SUBSTITUTED_ZERO component is legal
    only for run_role == TRAIN and only in SCREENING_RT / EXIT_ACTUAL
    contexts; anything else is a deterministic engine exception (§19 item 6).
    """
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
    if run_role not in (ROLE_TRAIN, ROLE_TEST, ROLE_LIVE):
        raise ValueError(f"unknown run_role {run_role!r}")

    schedule_version = regulatory.schedule_version
    priced: list[FeeComponent] = []

    def _status_for(comp: str) -> FeeInputStatus:
        status = regulatory.status_of(comp)
        if status is FeeInputStatus.TRAIN_SUBSTITUTED_ZERO:
            if run_role != ROLE_TRAIN:
                raise TrainFeeSubstitutionViolation(
                    f"{comp} unverified at {fee_computation_date} and "
                    f"TRAIN_SUBSTITUTED_ZERO requires run_role=TRAIN (got {run_role})",
                    {"component": comp, "date": str(fee_computation_date),
                     "run_role": run_role, "context": fee_context.value},
                )
            if fee_context not in _SUBSTITUTABLE_CONTEXTS:
                raise TrainFeeSubstitutionViolation(
                    f"{comp} substitution in {fee_context.value} context is not "
                    f"permitted (only SCREENING_RT / EXIT_ACTUAL)",
                    {"component": comp, "date": str(fee_computation_date),
                     "context": fee_context.value},
                )
        return status

    def _emit(component: str, base: Decimal, vat: Decimal,
              status: FeeInputStatus) -> None:
        priced.append(FeeComponent(
            side=side, component=component,
            base_amount=base, vat_amount=vat,
            rounded_amount=Decimal(0),  # resolved in the rounding pass below
            fee_context=fee_context,
            fee_computation_date=fee_computation_date,
            fee_input_status=status,
            schedule_version=schedule_version,
        ))

    # --- Broker transaction fee (§9.6; backward-projected constant, FP-8) ---
    tx_base = TRANSACTION_RATE * notional
    _emit("TRANSACTION", tx_base, VAT_RATE * tx_base,
          FeeInputStatus.BACKWARD_PROJECTED_CONSTANT)

    # --- JFX & KBI fee (§9.6; backward-projected constant) ---
    jfx_base = min(JFX_KBI_RATE * notional, JFX_KBI_CAP)
    _emit("JFX_KBI", jfx_base, VAT_RATE * jfx_base,
          FeeInputStatus.BACKWARD_PROJECTED_CONSTANT)

    # --- Regulatory components (sell side only) ---
    if side == "sell":
        for comp in _REGULATORY_COMPONENTS:
            status = _status_for(comp)
            if status is FeeInputStatus.TRAIN_SUBSTITUTED_ZERO:
                # §9.7 item 3: omitted from the schedule formula entirely;
                # exactly $0.00 — no minimum, cap, VAT, or rounding lift.
                _emit(comp, Decimal(0), Decimal(0), status)
                continue
            if status is FeeInputStatus.VERIFIED_ZERO:
                # Verifiably did not apply: $0.00; the $0.01 floor prices
                # the rate formula, not a non-applicable component.
                _emit(comp, Decimal(0), Decimal(0), status)
                continue
            rate = regulatory.rate_of(comp)
            if rate is None:
                raise ValueError(f"VERIFIED_RATE status for {comp} without a rate")
            if comp == "SEC":
                base = max(rate * notional, CENT)
            elif comp == "TAF":
                base = min(max(rate * shares, CENT), TAF_CAP)
            else:  # CAT
                base = max(rate * shares, CENT)
            vat = VAT_RATE * base if vat_on_regulatory else Decimal(0)
            _emit(comp, base, vat, status)

    # --- Rounding (after VAT where VAT applies) ---
    post_vat = [(c, c.base_amount + c.vat_amount) for c in priced]
    if rounding is RoundingBranch.EXACT:
        rounded = {id(c): v for c, v in post_vat}
        total = sum((v for _, v in post_vat), Decimal(0))
    elif rounding is RoundingBranch.CEIL_CENT_PER_COMPONENT:
        rounded = {id(c): _ceil_cent(v) for c, v in post_vat}
        total = sum(rounded.values(), Decimal(0))
    elif rounding is RoundingBranch.CEIL_CENT_PER_SIDE:
        # True-zero components are formula-omitted and excluded from the
        # aggregate; the charged amount is the ceiling of the side sum.
        aggregate = sum(
            (v for c, v in post_vat if c.base_amount > 0 or c.vat_amount > 0),
            Decimal(0),
        )
        total = _ceil_cent(aggregate) if aggregate > 0 else Decimal(0)
        rounded = {id(c): v for c, v in post_vat}  # rows keep exact amounts
    else:  # pragma: no cover - Enum exhaustiveness
        raise ValueError(f"unknown rounding branch {rounding!r}")

    components = tuple(
        FeeComponent(
            side=c.side, component=c.component,
            base_amount=c.base_amount, vat_amount=c.vat_amount,
            rounded_amount=rounded[id(c)],
            fee_context=c.fee_context,
            fee_computation_date=c.fee_computation_date,
            fee_input_status=c.fee_input_status,
            schedule_version=c.schedule_version,
        )
        for c, _ in post_vat
    )
    return SideFees(components=components, total=total)


def compute_round_trip_fees(
    *,
    notional: Decimal,
    shares_est: Decimal,
    screening_date: _dt.date,
    regulatory: RegulatoryRates,
    vat_on_regulatory: bool = False,
    rounding: RoundingBranch = RoundingBranch.CEIL_CENT_PER_COMPONENT,
    run_role: str = ROLE_TRAIN,
) -> FeeComputation:
    """N-08 Gate-A screening round-trip at entry notional and shares_est.

    Rate selection and verification state use the screening (decision) date
    per §9.7 item 1; the caller supplies ``RegulatoryRates`` resolved at
    that date.
    """
    buy = compute_side_fees(
        side="buy", notional=notional, shares=shares_est,
        fee_computation_date=screening_date,
        fee_context=FeeContext.SCREENING_RT,
        regulatory=regulatory,
        vat_on_regulatory=vat_on_regulatory,
        rounding=rounding, run_role=run_role,
    )
    sell = compute_side_fees(
        side="sell", notional=notional, shares=shares_est,
        fee_computation_date=screening_date,
        fee_context=FeeContext.SCREENING_RT,
        regulatory=regulatory,
        vat_on_regulatory=vat_on_regulatory,
        rounding=rounding, run_role=run_role,
    )
    substituted = tuple(sorted({
        c.component for c in (*buy.components, *sell.components)
        if c.fee_input_status is FeeInputStatus.TRAIN_SUBSTITUTED_ZERO
    }))
    return FeeComputation(
        components=buy.components + sell.components,
        fees_buy=buy.total, fees_sell=sell.total,
        fees_rt=buy.total + sell.total,
        substituted_components=substituted,
        schedule_version=regulatory.schedule_version,
    )


def compute_actual_side_fees(
    *,
    side: str,
    notional_actual: Decimal,
    shares_filled: Decimal,
    fill_date: _dt.date,
    regulatory: RegulatoryRates,
    vat_on_regulatory: bool,
    rounding: RoundingBranch,
    run_role: str,
) -> SideFees:
    """N-05: actual fill fees on actual fill notional and shares.

    The regulatory fee-computation date is the fill trading date (§9.7
    item 1); ``regulatory`` must be resolved at that date. Substitution is
    legal only for sell-side actuals (EXIT_ACTUAL) in TRAIN runs.
    """
    context = FeeContext.ENTRY_ACTUAL if side == "buy" else FeeContext.EXIT_ACTUAL
    return compute_side_fees(
        side=side, notional=notional_actual, shares=shares_filled,
        fee_computation_date=fill_date, fee_context=context,
        regulatory=regulatory, vat_on_regulatory=vat_on_regulatory,
        rounding=rounding, run_role=run_role,
    )


def require_verified_for_live(regulatory: RegulatoryRates) -> None:
    """§9.7 item 6 / §19 item 7: a live fee-schedule gap blocks BUY output."""
    for comp in _REGULATORY_COMPONENTS:
        if regulatory.status_of(comp) is FeeInputStatus.TRAIN_SUBSTITUTED_ZERO:
            raise FeeScheduleUnverified(
                f"no verified fee_schedule entry for {comp}; live BUY blocked "
                f"(FEE_SCHEDULE_UNVERIFIED)"
            )


def gate_a_burden(fees_rt: Decimal, realised_risk: Decimal) -> Decimal | None:
    """§9.4 Gate A burden; None when realised_risk is zero (undefined)."""
    if realised_risk <= 0:
        return None
    return fees_rt / realised_risk


def gate_a_passes(fees_rt: Decimal, realised_risk: Decimal,
                  threshold: Decimal = Decimal("0.25")) -> bool:
    """§9.4: burden ≤ 0.25 (threshold ASSUMPTION / MUST TEST)."""
    burden = gate_a_burden(fees_rt, realised_risk)
    return burden is not None and burden <= threshold
