"""Corporate-actions deterministic mechanics (§13.6, P-A-01, P-A-03) +
FP-5 coverage verification."""

import datetime as dt
from decimal import Decimal

from trading_core.corporate_actions import (
    CorporateAction,
    CoverageAttestation,
    apply_split,
    corp_actions_span_verified,
    corp_event_force_close_detected,
    dividend_credit_date,
    dividend_entitled,
    net_dividend,
)


class TestSplitMechanics:
    def test_mechanical_adjustment(self):
        shares, stop, target = apply_split(
            shares=Decimal("10"), stop_price=Decimal("90"),
            target_price=Decimal("120"), split_ratio=Decimal("2"))
        assert shares == Decimal("20")
        assert stop == Decimal("45")
        assert target == Decimal("60")

    def test_reverse_split(self):
        shares, stop, target = apply_split(
            shares=Decimal("10"), stop_price=Decimal("90"),
            target_price=Decimal("120"), split_ratio=Decimal("0.5"))
        assert shares == Decimal("5")
        assert stop == Decimal("180")
        assert target == Decimal("240")


class TestDividends:
    def test_ex_date_entitlement(self):
        assert dividend_entitled(shares=Decimal("0.5"))
        assert not dividend_entitled(shares=Decimal("0"))

    def test_net_dividend_withholding(self):
        net = net_dividend(shares_at_ex_date=Decimal("10"),
                           cash_amount_per_share=Decimal("1.00"),
                           withholding_rate=Decimal("0.30"))
        assert net == Decimal("7.00")

    def test_credit_earliest_of_three(self):
        ev = CorporateAction(
            ticker="T", event_type="CASH_DIVIDEND", ex_date=dt.date(2026, 2, 2),
            cash_amount_per_share=Decimal("1"), pay_date=dt.date(2026, 3, 1),
            corp_actions_version="v1")
        exit_ts = dt.datetime(2026, 2, 10, 10, 5)
        window_close = (dt.date(2026, 6, 30),
                        dt.datetime(2026, 6, 30, 16, 0))
        branch, when = dividend_credit_date(
            event=ev, exit_fill_ts=exit_ts, window_final_close=window_close)
        assert branch == "EXIT_FILL" and when == exit_ts

    def test_credit_pay_date_when_no_exit(self):
        ev = CorporateAction(
            ticker="T", event_type="CASH_DIVIDEND", ex_date=dt.date(2026, 2, 2),
            cash_amount_per_share=Decimal("1"), pay_date=dt.date(2026, 3, 1),
            corp_actions_version="v1")
        window_close = (dt.date(2026, 6, 30),
                        dt.datetime(2026, 6, 30, 16, 0))
        branch, when = dividend_credit_date(
            event=ev, exit_fill_ts=None, window_final_close=window_close)
        assert branch == "PAY_DATE"


class TestSymbolChangeDelisting:
    def test_detection_requires_both_conditions(self):
        assert corp_event_force_close_detected(
            session_date=dt.date(2026, 1, 6),
            ticker_has_executable_bar_in_session=False,
            universe_records_delisting_or_symbol_change=True)
        assert not corp_event_force_close_detected(
            session_date=dt.date(2026, 1, 6),
            ticker_has_executable_bar_in_session=True,
            universe_records_delisting_or_symbol_change=True)
        assert not corp_event_force_close_detected(
            session_date=dt.date(2026, 1, 6),
            ticker_has_executable_bar_in_session=False,
            universe_records_delisting_or_symbol_change=False)


class TestCoverageVerification:
    def _att(self, start, end, verified=True, version="m1"):
        return CoverageAttestation(
            source_kind="CORP_ACTIONS", ticker="T",
            span_start=start, span_end=end, verified=verified,
            manifest_version=version)

    def test_verified_zero_span_counts(self):
        """A verified attestation with no events still verifies the span."""
        assert corp_actions_span_verified(
            ticker="T", span_start=dt.date(2026, 1, 1),
            span_end=dt.date(2026, 1, 31),
            attestations=[self._att(dt.date(2026, 1, 1), dt.date(2026, 1, 31))],
            manifest_version="m1")

    def test_absent_attestation_is_unverified(self):
        assert not corp_actions_span_verified(
            ticker="T", span_start=dt.date(2026, 1, 1),
            span_end=dt.date(2026, 1, 31), attestations=[],
            manifest_version="m1")

    def test_wrong_manifest_version_unverified(self):
        assert not corp_actions_span_verified(
            ticker="T", span_start=dt.date(2026, 1, 1),
            span_end=dt.date(2026, 1, 31),
            attestations=[self._att(dt.date(2026, 1, 1), dt.date(2026, 1, 31),
                                    version="m2")],
            manifest_version="m1")

    def test_partial_coverage_unverified(self):
        assert not corp_actions_span_verified(
            ticker="T", span_start=dt.date(2026, 1, 1),
            span_end=dt.date(2026, 1, 31),
            attestations=[self._att(dt.date(2026, 1, 1), dt.date(2026, 1, 15))],
            manifest_version="m1")
