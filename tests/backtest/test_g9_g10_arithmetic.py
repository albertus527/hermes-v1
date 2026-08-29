"""G9/G10 structural arithmetic (§1.3.1 / §1.3.2): assert the gate geometry
on the exact §9.6 computation, not the disclosure approximations.
"""

import datetime as dt
from decimal import Decimal

import pytest

from trading_core.fees import (
    FeeInputStatus,
    RegulatoryRates,
    RoundingBranch,
    compute_round_trip_fees,
    gate_a_passes,
)
from trading_core.sizing import compute_sizing
from trading_core.types import ROLE_TRAIN

CURRENT = RegulatoryRates(
    statuses={c: FeeInputStatus.VERIFIED_RATE for c in ("SEC", "TAF", "CAT")},
    rates={"SEC": Decimal("0.0000206"), "TAF": Decimal("0.000166"),
           "CAT": Decimal("0.0000265")},
    schedule_version="test",
)
D = dt.date(2026, 8, 25)
REF = Decimal("100")  # $100 reference price; shares scale with notional


def _g9_g10(portfolio: Decimal, multiplier: Decimal, stop_pct: Decimal,
            vat=False, rounding=RoundingBranch.CEIL_CENT_PER_COMPONENT):
    sizing = compute_sizing(
        portfolio_value=portfolio, risk_per_trade=Decimal("0.01"),
        regime_multiplier=multiplier, stop_pct=stop_pct,
        entry_reference_price=REF)
    if not sizing.g10_pass:
        return False, sizing, None
    fees = compute_round_trip_fees(
        notional=sizing.notional, shares_est=sizing.shares_est,
        screening_date=D, regulatory=CURRENT, vat_on_regulatory=vat,
        rounding=rounding, run_role=ROLE_TRAIN)
    return gate_a_passes(fees.fees_rt, sizing.realised_risk), sizing, fees


class TestSection131:
    """$61: G9 ∧ G10 jointly and totally unsatisfiable in NEUTRAL/VOLATILE
    under ALL THREE rounding branches."""

    @pytest.mark.parametrize("rounding", list(RoundingBranch))
    @pytest.mark.parametrize("stop_pct", [
        Decimal("0.01"), Decimal("0.02"), Decimal("0.05"), Decimal("0.10"),
        Decimal("0.20"), Decimal("0.50"),
    ])
    def test_61_unsatisfiable_outside_risk_on(self, rounding, stop_pct):
        ok, sizing, fees = _g9_g10(Decimal("61"), Decimal("0.5"), stop_pct,
                                   rounding=rounding)
        assert not ok

    def test_61_risk_on_narrow_band(self):
        """RISK_ON $61: the G10 floor is stop_pct >= 6.1% (raw_notional =
        0.61/stop_pct >= $10); G9 additionally fails below an interior
        lower edge — the admissible band is strictly inside (0, 6.1%]."""
        # At/above the G10 floor: viable (stop_pct = 6.1% -> raw = $10.00)
        ok_floor, sizing, _ = _g9_g10(Decimal("61"), Decimal("1.0"),
                                      Decimal("0.061"))
        assert sizing.g10_pass and ok_floor
        # Below the interior G9 edge (e.g. stop_pct 1% -> raw $61, capped to
        # $54.90, fees dominate): fails G9
        ok_low, _, _ = _g9_g10(Decimal("61"), Decimal("1.0"), Decimal("0.01"))
        assert not ok_low
        # Extremely tight stop: G10 rejects (raw < $10)
        _, sizing, _ = _g9_g10(Decimal("61"), Decimal("1.0"), Decimal("0.07"))
        assert not sizing.g10_pass


class TestSection132:
    """$200 criterion-bearing run: feasible in admitting regimes, with a
    hard minimum-ATR% admissibility floor."""

    def test_200_neutral_fee_edge_near_27(self):
        """§1.3.2 NEUTRAL fee-edge geometry (canonical branch, exact §9.6
        computation): the round-trip fee is flat per notional band; the G9
        edge is where ceil-cent rounding drops a component cent. risk =
        $1.00, so burden = fees_rt / $1.00:
        - raw ≈ $33.33 -> fees_rt $0.31 (burden 31%): G9 fails
        - raw ≈ $28.57 -> fees_rt $0.27 (burden 27%): G9 fails
        - raw = $25.00 -> fees_rt $0.25 (burden 25%): G9 passes (≤ 0.25)
        """
        assert not _g9_g10(Decimal("200"), Decimal("0.5"), Decimal("0.03"))[0]
        assert not _g9_g10(Decimal("200"), Decimal("0.5"), Decimal("0.035"))[0]
        assert _g9_g10(Decimal("200"), Decimal("0.5"), Decimal("0.04"))[0]
        # Below the edge the disclosure stop band is ≈3.70%–10%; raw $10 at
        # stop_pct 0.10 sits at the G10 floor with burden 13%: passes
        assert _g9_g10(Decimal("200"), Decimal("0.5"), Decimal("0.10"))[0]
        # stop_pct beyond 10% (wider stop) -> G10 rejects (raw < $10)
        _, sizing, _ = _g9_g10(Decimal("200"), Decimal("0.5"), Decimal("0.11"))
        assert not sizing.g10_pass

    def test_200_risk_on_viable(self):
        ok, sizing, _ = _g9_g10(Decimal("200"), Decimal("1.0"),
                                Decimal("0.05"))
        assert ok and sizing.g10_pass
