"""Phase-0 gate: exact reproduction of the R2.7 §1.4 six-branch fee table.

Worked example: portfolio $61.00, RISK_ON (multiplier 1.0), ATR% 2.0%,
stop_mult 2.5 -> stop_pct 5.0%, raw_notional $12.20 (under the 90% cap),
shares_est = 12.20 / 181 at the $181 reference price; all three sell-side
regulatory minimums bind at $0.01. Realised risk (screening) = $0.61.

All six rows must match EXACTLY (fees_buy / fees_sell / fees_rt / burden).
"""

import datetime as dt
from decimal import Decimal

import pytest

from trading_core.fees import (
    FeeInputStatus,
    RegulatoryRates,
    RoundingBranch,
    compute_round_trip_fees,
    gate_a_burden,
)
from trading_core.types import ROLE_TRAIN

NOTIONAL = Decimal("12.20")
REF_PRICE = Decimal("181")
SHARES_EST = NOTIONAL / REF_PRICE
RISK = Decimal("0.61")

CURRENT_RATES = RegulatoryRates(
    statuses={c: FeeInputStatus.VERIFIED_RATE for c in ("SEC", "TAF", "CAT")},
    rates={
        "SEC": Decimal("0.0000206"),
        "TAF": Decimal("0.000166"),
        "CAT": Decimal("0.0000265"),
    },
    schedule_version="test-schedule",
)

# §1.4 six-branch table (Phase-0 smoke-test targets)
EXPECTED = [
    # (vat_on_regulatory, rounding, fees_buy, fees_sell, fees_rt, burden%)
    (False, RoundingBranch.CEIL_CENT_PER_COMPONENT,
     Decimal("0.06"), Decimal("0.09"), Decimal("0.15"), Decimal("24.59")),
    (False, RoundingBranch.CEIL_CENT_PER_SIDE,
     Decimal("0.05"), Decimal("0.08"), Decimal("0.13"), Decimal("21.31")),
    (False, RoundingBranch.EXACT,
     Decimal("0.047397"), Decimal("0.077397"), Decimal("0.124794"),
     Decimal("20.46")),
    (True, RoundingBranch.CEIL_CENT_PER_COMPONENT,
     Decimal("0.06"), Decimal("0.12"), Decimal("0.18"), Decimal("29.51")),
    (True, RoundingBranch.CEIL_CENT_PER_SIDE,
     Decimal("0.05"), Decimal("0.09"), Decimal("0.14"), Decimal("22.95")),
    (True, RoundingBranch.EXACT,
     Decimal("0.047397"), Decimal("0.080697"), Decimal("0.128094"),
     Decimal("21.00")),
]


@pytest.mark.parametrize(
    "vat,rounding,exp_buy,exp_sell,exp_rt,exp_burden", EXPECTED,
    ids=[f"vat={v}-{r.value}" for v, r, *_ in EXPECTED],
)
def test_s1_4_row_exact(vat, rounding, exp_buy, exp_sell, exp_rt, exp_burden):
    comp = compute_round_trip_fees(
        notional=NOTIONAL, shares_est=SHARES_EST,
        screening_date=dt.date(2026, 8, 25), regulatory=CURRENT_RATES,
        vat_on_regulatory=vat, rounding=rounding, run_role=ROLE_TRAIN)
    assert comp.fees_buy == exp_buy, f"fees_buy {comp.fees_buy} != {exp_buy}"
    assert comp.fees_sell == exp_sell, f"fees_sell {comp.fees_sell} != {exp_sell}"
    assert comp.fees_rt == exp_rt, f"fees_rt {comp.fees_rt} != {exp_rt}"
    burden = (comp.fees_rt / RISK * 100).quantize(Decimal("0.01"))
    assert burden == exp_burden, f"burden {burden}% != {exp_burden}%"


def test_canonical_branch_gate_a_passes_at_example():
    """The canonical cell (vat=false, CEIL_CENT_PER_COMPONENT) passes Gate A
    at 24.59% <= 25%; the vat=true per-component branch fails at 29.51%."""
    comp_pass = compute_round_trip_fees(
        notional=NOTIONAL, shares_est=SHARES_EST,
        screening_date=dt.date(2026, 8, 25), regulatory=CURRENT_RATES,
        vat_on_regulatory=False,
        rounding=RoundingBranch.CEIL_CENT_PER_COMPONENT, run_role=ROLE_TRAIN)
    assert gate_a_burden(comp_pass.fees_rt, RISK) <= Decimal("0.25")

    comp_fail = compute_round_trip_fees(
        notional=NOTIONAL, shares_est=SHARES_EST,
        screening_date=dt.date(2026, 8, 25), regulatory=CURRENT_RATES,
        vat_on_regulatory=True,
        rounding=RoundingBranch.CEIL_CENT_PER_COMPONENT, run_role=ROLE_TRAIN)
    assert gate_a_burden(comp_fail.fees_rt, RISK) > Decimal("0.25")
