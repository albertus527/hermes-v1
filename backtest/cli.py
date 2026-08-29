"""`hermes backtest` command handlers (R2.7 Phase 0/1/2 verbs).

Phase-0 verbs: `init`, `fee-smoke`. Phase-2 verbs: `populate-news-cache`
(the ONLY authorized live-LLM context — fails closed until the pinned
model is configured), `news-cache-report` (completeness + P-4 integrity
over an existing cache; pure reads), and `calibrate-news` (§11.4 framework
runner; refuses to claim PASS). Data-fetch verbs (fetch-alpaca,
fetch-finnhub, fetch-fred) arrive with their phases.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _backtest_db_path() -> Path:
    return _hermes_home() / "backtest" / "backtest.sqlite3"


def _load_pinned_model() -> str:
    """backtest.pinned_model from config.yaml (behavioral setting)."""
    from hermes_cli.config import load_config_readonly
    cfg = load_config_readonly()
    backtest_cfg = cfg.get("backtest") or {}
    return str(backtest_cfg.get("pinned_model") or "").strip()


def _save_report(text: str, name: str) -> Path:
    """Atomically persist a report artifact under
    $HERMES_HOME/backtest/reports/."""
    from utils import atomic_write_text
    reports = _hermes_home() / "backtest" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / name
    atomic_write_text(path, text)
    return path


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


def cmd_populate_news_cache(args) -> int:
    """§20 Phase 2 — the news-cache population job (the ONLY authorized
    bulk live-LLM context; NOT a backtest). Fails closed until the pinned
    model identifier is configured."""
    from backtest.news.cache import (
        HeadlineInventory,
        open_news_cache,
    )
    from backtest.news.cache_populate import (
        populate_news_cache_entries,
    )
    from backtest.news.classifier import PinnedModelMissing

    pinned = _load_pinned_model()
    if not pinned:
        print("BLOCKED: backtest.pinned_model is not set.")
        print("  The exact pinned 'openrouter/<provider>/<model>@<version>'")
        print("  identifier (§11.5) is an unresolved external prerequisite;")
        print("  the population job refuses to guess a model.")
        return 3
    db_path = _backtest_db_path()
    if not db_path.exists():
        print(f"No backtest store at {db_path} — run `hermes backtest init` "
              "and the Phase-0 news ingest first.")
        return 2
    cache = open_news_cache(db_path)
    inventory = HeadlineInventory(cache._conn)
    # The Phase-0 Finnhub ingest writes news_headlines rows; the population
    # job classifies their (ticker, source, published_at, text) records.
    import datetime as _dt
    rows = cache._conn.execute(
        "SELECT ticker, source, published_at, headline_text_normalized "
        "FROM news_headlines WHERE published_at IS NOT NULL "
        "ORDER BY ticker, published_at").fetchall()
    entries = []
    for r in rows:
        entries.append((r["ticker"], r["source"],
                        _dt.datetime.fromisoformat(r["published_at"]),
                        r["headline_text_normalized"]))
    manifest_versions = [getattr(args, "manifest_version", "") or ""]
    manifest_versions = [m for m in manifest_versions if m]
    if not manifest_versions:
        print("BLOCKED: --manifest-version is required (run-pinned NEWS "
              "coverage manifest, §11.6).")
        return 3
    try:
        report = populate_news_cache_entries(
            cache=cache, inventory=inventory,
            manifest_versions=manifest_versions,
            pinned_model=pinned,
            llm_config_version=getattr(args, "llm_config_version", "") or "",
            classified_at_wallclock=_now_iso(),
            run_id=getattr(args, "run_id", "") or "news-cache-population",
        )
    except PinnedModelMissing as exc:
        print(f"BLOCKED: {exc}")
        return 3
    path = _save_report(report.to_json(),
                        "news_cache_population_report.json")
    print(f"Population report written: {path}")
    print(f"  model_version:         {report.model_version}")
    print(f"  headlines in scope:    {report.headlines_total}")
    print(f"  newly classified:      {report.headlines_classified}")
    print(f"  already cached:        {report.headlines_skipped_existing}")
    print(f"  cache misses:          {len(report.cache_misses)}")
    print(f"  P-4 conflicts:         {len(report.p4_conflicts)}")
    print(f"  malformed records:     {len(report.malformed)}")
    print(f"  complete:              {report.complete}")
    return 0 if report.complete else 1


def cmd_news_cache_report(args) -> int:
    """§20 Phase 2 — cache-completeness + P-4 integrity report over an
    EXISTING cache. Pure reads: no LLM, no writes, deterministic."""
    from backtest.news.cache import HeadlineInventory, open_news_cache
    from backtest.news.cache_populate import (
        compute_cache_misses,
        compute_p4_conflicts,
    )
    db_path = _backtest_db_path()
    if not db_path.exists():
        print(f"No backtest store at {db_path} — nothing to report on.")
        return 2
    pinned = _load_pinned_model()
    if not pinned:
        print("BLOCKED: backtest.pinned_model is not set — cannot identify "
              "the cache slice to report on (§11.5).")
        return 3
    from backtest.news.classifier import parse_pinned_model
    model_version, _ = parse_pinned_model(pinned)
    cache = open_news_cache(db_path, read_only=True)
    inventory = HeadlineInventory(cache._conn)
    manifest_versions = [m for m in
                         [getattr(args, "manifest_version", "") or ""]
                         if m]
    if not manifest_versions:
        print("BLOCKED: --manifest-version is required (§11.6).")
        return 3
    misses = compute_cache_misses(
        cache=cache, inventory=inventory,
        manifest_versions=manifest_versions,
        model_version=model_version,
        schema_version="news_schema_v3")
    conflicts = compute_p4_conflicts(
        cache, model_version=model_version,
        schema_version="news_schema_v3")
    payload = {
        "model_version": model_version,
        "schema_version": "news_schema_v3",
        "coverage_manifest_versions": manifest_versions,
        "headline_count": cache.headline_count(),
        "cache_misses": misses,
        "p4_conflicts": conflicts,
        "complete": not (misses or conflicts),
    }
    import json as _json
    path = _save_report(_json.dumps(payload, sort_keys=True, indent=2),
                        "news_cache_integrity_report.json")
    print(f"Integrity report written: {path}")
    print(f"  cached classifications: {payload['headline_count']}")
    print(f"  cache misses:           {len(misses)}")
    print(f"  P-4 conflicts:          {len(conflicts)}")
    print(f"  complete:               {payload['complete']}")
    return 0 if payload["complete"] else 1


def cmd_calibrate_news(args) -> int:
    """§11.4 — run the calibration framework against a labeled JSON set.
    Never claims PASS; reports NOT EVALUABLE when the set is missing or
    below the ≥200-headline minimum."""
    from backtest.news.calibration import (
        CalibrationDatasetError,
        evaluate_calibration,
        load_labeled_headlines,
    )
    labeled_path = getattr(args, "labeled_set", None)
    if not labeled_path:
        print("BLOCKED: --labeled-set <path> is required. The ≥200 "
              "manually labeled headlines (§11.4) are an external "
              "prerequisite not present in this repository.")
        return 3
    try:
        labeled = load_labeled_headlines(labeled_path)
    except (OSError, ValueError, CalibrationDatasetError) as exc:
        print(f"BLOCKED: labeled set invalid: {exc}")
        return 3
    report = evaluate_calibration(labeled, classifications={})
    path = _save_report(report.to_json(), "news_calibration_report.json")
    print(f"Calibration report written: {path}")
    print(f"  labeled headlines: {report.labeled_count}")
    print(f"  status:            {report.status}")
    return 0


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()
