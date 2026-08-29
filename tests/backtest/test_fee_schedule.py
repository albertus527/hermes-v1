"""Fee-schedule state machine tests (§9.7): verified rate / verified zero /
unverified -> PROVISIONAL; TRAIN_SUBSTITUTED_ZERO role restrictions; the
$0.01 minimum never lifts a substituted zero; fee-computation-date rules.
"""

import datetime as dt
from decimal import Decimal

import pytest

from trading_core.errors import FeeScheduleUnverified, TrainFeeSubstitutionViolation
from trading_core.fees import (
    FeeContext,
    FeeInputStatus,
    RegulatoryRates,
    RoundingBranch,
    compute_actual_side_fees,
    compute_round_trip_fees,
    compute_side_fees,
    require_verified_for_live,
)
from trading_core.types import ROLE_LIVE, ROLE_TEST, ROLE_TRAIN

D = dt.date(2020, 3, 16)  # an unverified historical date

VERIFIED = RegulatoryRates(
    statuses={c: FeeInputStatus.VERIFIED_RATE for c in ("SEC", "TAF", "CAT")},
    rates={"SEC": Decimal("0.0000206"), "TAF": Decimal("0.000166"),
           "CAT": Decimal("0.0000265")},
    schedule_version="v-test",
)
UNVERIFIED = RegulatoryRates(statuses={}, schedule_version="v-test")
VERIFIED_ZERO = RegulatoryRates(
    statuses={c: FeeInputStatus.VERIFIED_ZERO for c in ("SEC", "TAF", "CAT")},
    schedule_version="v-test",
)


class TestRoleRestrictions:
    def test_train_substitution_legal_in_train_screening(self):
        comp = compute_round_trip_fees(
            notional=Decimal("12.20"), shares_est=Decimal("0.0674"),
            screening_date=D, regulatory=UNVERIFIED,
            vat_on_regulatory=False,
            rounding=RoundingBranch.CEIL_CENT_PER_COMPONENT,
            run_role=ROLE_TRAIN)
        assert comp.substituted_components == ("CAT", "SEC", "TAF")
        assert comp.train_fee_substituted

    def test_train_substitution_legal_in_train_exit_actual(self):
        side = compute_actual_side_fees(
            side="sell", notional_actual=Decimal("12.20"),
            shares_filled=Decimal("0.0674"), fill_date=D,
            regulatory=UNVERIFIED, vat_on_regulatory=False,
            rounding=RoundingBranch.EXACT, run_role=ROLE_TRAIN)
        assert all(c.fee_input_status in (FeeInputStatus.TRAIN_SUBSTITUTED_ZERO,
                                          FeeInputStatus.BACKWARD_PROJECTED_CONSTANT)
                   for c in side.components)

    def test_test_run_write_raises_engine_exception(self):
        with pytest.raises(TrainFeeSubstitutionViolation):
            compute_round_trip_fees(
                notional=Decimal("12.20"), shares_est=Decimal("0.0674"),
                screening_date=D, regulatory=UNVERIFIED,
                vat_on_regulatory=False,
                rounding=RoundingBranch.EXACT, run_role=ROLE_TEST)

    def test_live_run_write_raises_engine_exception(self):
        with pytest.raises(TrainFeeSubstitutionViolation):
            compute_round_trip_fees(
                notional=Decimal("12.20"), shares_est=Decimal("0.0674"),
                screening_date=D, regulatory=UNVERIFIED,
                vat_on_regulatory=False,
                rounding=RoundingBranch.EXACT, run_role=ROLE_LIVE)

    def test_substitution_illegal_in_entry_actual_context(self):
        # §9.7 item 3: substitution is legal only in SCREENING_RT /
        # EXIT_ACTUAL — the context guard must fire even in a TRAIN run.
        with pytest.raises(TrainFeeSubstitutionViolation):
            compute_side_fees(
                side="sell", notional=Decimal("12.20"),
                shares=Decimal("0.0674"), fee_computation_date=D,
                fee_context=FeeContext.ENTRY_ACTUAL,
                regulatory=UNVERIFIED, vat_on_regulatory=False,
                rounding=RoundingBranch.EXACT, run_role=ROLE_TRAIN)

    def test_live_unverified_blocks_buy(self):
        with pytest.raises(FeeScheduleUnverified):
            require_verified_for_live(UNVERIFIED)
        require_verified_for_live(VERIFIED)  # no raise
        require_verified_for_live(VERIFIED_ZERO)  # verified zero is fine


class TestTrueZeroSubstitution:
    """§9.7 item 3: a substituted component is exactly $0.00 and cannot be
    lifted by minimum, cap, VAT, or any rounding branch."""

    @pytest.mark.parametrize("rounding", list(RoundingBranch))
    @pytest.mark.parametrize("vat", [False, True])
    def test_substituted_zero_never_lifted(self, rounding, vat):
        side = compute_side_fees(
            side="sell", notional=Decimal("12.20"), shares=Decimal("0.0674"),
            fee_computation_date=D, fee_context=FeeContext.EXIT_ACTUAL,
            regulatory=UNVERIFIED, vat_on_regulatory=vat,
            rounding=rounding, run_role=ROLE_TRAIN)
        for c in side.components:
            if c.component in ("SEC", "TAF", "CAT"):
                assert c.fee_input_status is FeeInputStatus.TRAIN_SUBSTITUTED_ZERO
                assert c.base_amount == 0
                assert c.vat_amount == 0
                assert c.rounded_amount == 0

    def test_verified_zero_also_not_lifted_by_minimum(self):
        side = compute_side_fees(
            side="sell", notional=Decimal("12.20"), shares=Decimal("0.0674"),
            fee_computation_date=D, fee_context=FeeContext.EXIT_ACTUAL,
            regulatory=VERIFIED_ZERO, vat_on_regulatory=True,
            rounding=RoundingBranch.CEIL_CENT_PER_COMPONENT,
            run_role=ROLE_TRAIN)
        for c in side.components:
            if c.component in ("SEC", "TAF", "CAT"):
                assert c.fee_input_status is FeeInputStatus.VERIFIED_ZERO
                assert c.rounded_amount == 0


class TestScheduleResolution:
    def test_effective_dated_resolution(self, tmp_path):
        schedule_file = tmp_path / "fee_schedule.yaml"
        schedule_file.write_text(
            """
version: "v-resolve"
entries:
  - component: SEC
    rate: 0.0000206
    effective: ["2020-01-01", "2020-12-31"]
    verified: true
  - component: TAF
    applicable: false
    effective: ["2020-01-01", "2020-12-31"]
    verified: true
  # CAT absent entirely -> unverified
""")
        from trading_core.fee_schedule import load_fee_schedule
        sched = load_fee_schedule(schedule_file)
        rates = sched.regulatory_rates_at(dt.date(2020, 6, 15))
        assert rates.statuses["SEC"] is FeeInputStatus.VERIFIED_RATE
        assert rates.rates["SEC"] == Decimal("0.0000206")
        assert rates.statuses["TAF"] is FeeInputStatus.VERIFIED_ZERO
        assert rates.statuses["CAT"] is FeeInputStatus.TRAIN_SUBSTITUTED_ZERO

    def test_outside_span_is_unverified(self, tmp_path):
        schedule_file = tmp_path / "fee_schedule.yaml"
        schedule_file.write_text(
            """
version: "v2"
entries:
  - component: SEC
    rate: 0.0000206
    effective: ["2020-01-01", "2020-12-31"]
    verified: true
""")
        from trading_core.fee_schedule import load_fee_schedule
        sched = load_fee_schedule(schedule_file)
        rates = sched.regulatory_rates_at(dt.date(2021, 1, 4))
        assert rates.statuses["SEC"] is FeeInputStatus.TRAIN_SUBSTITUTED_ZERO

    def test_fee_computation_date_drives_state(self, tmp_path):
        """§9.7 item 1: rate selection and verification state use the SAME
        fee-computation date; different dates may resolve differently."""
        schedule_file = tmp_path / "fee_schedule.yaml"
        schedule_file.write_text(
            """
version: "v3"
entries:
  - component: SEC
    rate: 0.0000111
    effective: ["2020-01-01", "2020-06-30"]
    verified: true
  - component: SEC
    rate: 0.0000222
    effective: ["2020-07-01", null]
    verified: true
""")
        from trading_core.fee_schedule import load_fee_schedule
        sched = load_fee_schedule(schedule_file)
        r1 = sched.regulatory_rates_at(dt.date(2020, 3, 2))
        r2 = sched.regulatory_rates_at(dt.date(2020, 8, 3))
        assert r1.rates["SEC"] == Decimal("0.0000111")
        assert r2.rates["SEC"] == Decimal("0.0000222")
