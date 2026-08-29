"""`hermes backtest` command handlers (R2.7 Phase 0 scaffolding verbs).

Only Phase-0 verbs exist: `init` (materialize artifacts + create the DB)
and `fee-smoke` (run the §1.4 six-branch fee smoke test against the
deterministic fee module). Data-fetch verbs (fetch-alpaca, fetch-finnhub,
fetch-fred) and later-phase verbs arrive with their phases.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def cmd_init(args) -> int:
    from backtest.artifacts import ensure_artifacts
    from backtest.db.schema import open_db

    artifacts = ensure_artifacts()
    db_dir = _hermes_home() / "backtest"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "backtest.sqlite3"
    conn = open_db(db_path)
    try:
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
    finally:
        conn.close()
    print(f"Backtest store initialized: {db_path}")
    print(f"  schema_version: {version[0] if version else 'unknown'}")
    print(f"  artifacts:      {artifacts}")
    print(f"    - universe.yaml / fee_schedule.yaml (versioned seeds)")
    return 0


def cmd_fee_smoke(args) -> int:
    """§1.4 six-branch smoke test (Phase-0 gate). Exits non-zero on any
    mismatch; this is the same computation tests/backtest/test_fees_smoke
    asserts."""
    from trading_core.fees import RegulatoryRates, FeeInputStatus, RoundingBranch
    from trading_core.fees import compute_round_trip_fees
    from trading_core.types import ROLE_TRAIN
    import datetime as dt

    notional = Decimal("12.20")
    ref_price = Decimal("181")
    shares_est = notional / ref_price
    risk = Decimal("0.61")
    rates = RegulatoryRates(
        statuses={c: FeeInputStatus.VERIFIED_RATE for c in ("SEC", "TAF", "CAT")},
        rates={"SEC": Decimal("0.0000206"), "TAF": Decimal("0.000166"),
               "CAT": Decimal("0.0000265")},
        schedule_version="smoke-test",
    )
    expected = {
        (False, RoundingBranch.CEIL_CENT_PER_COMPONENT): ("0.06", "0.09", "0.15", "24.59"),
        (False, RoundingBranch.CEIL_CENT_PER_SIDE): ("0.05", "0.08", "0.13", "21.31"),
        (False, RoundingBranch.EXACT): ("0.047397", "0.077397", "0.124794", "20.46"),
        (True, RoundingBranch.CEIL_CENT_PER_COMPONENT): ("0.06", "0.12", "0.18", "29.51"),
        (True, RoundingBranch.CEIL_CENT_PER_SIDE): ("0.05", "0.09", "0.14", "22.95"),
        (True, RoundingBranch.EXACT): ("0.047397", "0.080697", "0.128094", "21.00"),
    }
    failures = 0
    print(f"{'vat_reg':<7} {'rounding':<26} {'buy':>9} {'sell':>9} {'rt':>9} {'burden':>8}  ok")
    for (vat, branch), (eb, es, ert, eburden) in expected.items():
        comp = compute_round_trip_fees(
            notional=notional, shares_est=shares_est,
            screening_date=dt.date(2026, 8, 25), regulatory=rates,
            vat_on_regulatory=vat, rounding=branch, run_role=ROLE_TRAIN)
        burden = (comp.fees_rt / risk * 100).quantize(Decimal("0.01"))
        ok = (
            comp.fees_buy == Decimal(eb)
            and comp.fees_sell == Decimal(es)
            and comp.fees_rt == Decimal(ert)
            and burden == Decimal(eburden)
        )
        failures += 0 if ok else 1
        print(f"{str(vat):<7} {branch.value:<26} {comp.fees_buy:>9} "
              f"{comp.fees_sell:>9} {comp.fees_rt:>9} {burden:>7}%  "
              f"{'PASS' if ok else 'FAIL'}")
    if failures:
        print(f"\n§1.4 smoke test FAILED ({failures}/6 rows mismatched)")
        return 1
    print("\n§1.4 six-branch smoke test: all 6 rows match exactly (PASS)")
    return 0
