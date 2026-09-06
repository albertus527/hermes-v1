"""R2.8.1 Phase-2 — offline classification-cache population tests.

Hermetic: tmp_path SQLite stores, synthetic fixtures, NO LLM, NO network,
NO credentials. Covers the full offline population path: validation,
atomicity, idempotency, conflicts, provenance guard, coverage guard,
digests, and the cache-only backtest invariant.
"""

import datetime as dt
import json
import sqlite3

import pytest

from backtest.news.cache import (
    HeadlineInventory,
    NewsClassificationCache,
    open_news_cache,
)
from backtest.news.cache_populate_offline import (
    ArtifactFormatError,
    CoverageGuardError,
    OfflinePopulationReport,
    PopulationProvenanceError,
    load_offline_population_artifact,
    populate_cache_from_offline_artifact,
)

PIN = "openrouter/testorg/test-model@v1"
SCHEMA = "news_schema_v3"

T0 = dt.datetime(2024, 1, 5, 12, 0, tzinfo=dt.timezone.utc)


def _hash(text):
    from trading_core.news_effects import headline_hash
    return headline_hash(text)


def _make_store(db_path, headlines, spans):
    """headlines: list of (ticker, source, text, published_at);
    spans: list of (ticker, span_start, span_end)."""
    cache = open_news_cache(db_path)
    conn = cache._conn
    from trading_core.news_effects import normalize_headline_text
    for ticker, source, text, pub in headlines:
        conn.execute(
            "INSERT INTO news_headlines (headline_hash, source, ticker, "
            "published_at, headline_text_normalized, fetched_at) VALUES "
            "(?,?,?,?,?,?)",
            (_hash(normalize_headline_text(text)), source, ticker,
             pub.isoformat(), normalize_headline_text(text),
             "2024-01-01T00:00:00+00:00"))
    for ticker, start, end in spans:
        conn.execute(
            "INSERT INTO coverage_manifests (source_kind, ticker, "
            "span_start, span_end, verified, manifest_version) VALUES "
            "('NEWS',?,?,?,?,?)", (ticker, start.isoformat(),
                                   end.isoformat(), 1, "mv-1"))
    conn.commit()
    return cache


def _classification(ticker, source, text, pub, *, category="EARNINGS",
                    direction="BULLISH", severity="MEDIUM",
                    ma_role="NEITHER", confidence=0.9,
                    keyword_override=False):
    from trading_core.news_effects import normalize_headline_text
    return {
        "ticker": ticker, "source": source,
        "published_at": pub.isoformat(),
        "headline_text_normalized": normalize_headline_text(text),
        "classification": {
            "ticker": ticker, "category": category,
            "direction": direction, "severity": severity,
            "ma_role": ma_role, "confidence": confidence,
            "published_at": pub.isoformat(),
            "headline_hash": _hash(normalize_headline_text(text)),
            "source": source, "keyword_override": keyword_override,
            "schema_version": SCHEMA, "model_version": PIN,
        },
    }


def _artifact(results, llm_config_version="cfg-1"):
    return {"format_version": "r281-offline-classification-1",
            "llm_config_version": llm_config_version, "results": results}


def _write(tmp_path, doc, name="artifact.json"):
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return str(p)


def _populate(cache, artifact, tmp_path, *, final=True, pin=PIN,
              manifests=("mv-1",), llm="cfg-1", tickers=("AAPL",),
              start="2024-01-01", end="2024-01-31"):
    return populate_cache_from_offline_artifact(
        artifact, cache=cache, inventory=HeadlineInventory(cache._conn),
        expected_model_version=pin, manifest_versions=list(manifests),
        requested_tickers=list(tickers), requested_start=start,
        requested_end=end, llm_config_version=llm, final=final)


_HL = [("AAPL", "finnhub", "Apple beats earnings expectations", T0),
       ("MSFT", "finnhub", "Microsoft announces new product line", T0)]
_SPANS = [("AAPL", dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
           dt.datetime(2024, 2, 1, tzinfo=dt.timezone.utc)),
          ("MSFT", dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
           dt.datetime(2024, 2, 1, tzinfo=dt.timezone.utc))]


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "bt.sqlite3"
    return _make_store(db, _HL, _SPANS)


def _one_result():
    return _classification("AAPL", "finnhub",
                           "Apple beats earnings expectations", T0)


# 1. valid artifact validates
def test_valid_artifact_validates(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    rows = load_offline_population_artifact(
        _write(tmp_path, _artifact([_one_result()])),
        expected_model_version=PIN, llm_config_version="cfg-1")
    assert len(rows) == 1
    assert rows[0].payload["model_version"] == PIN


# 2. exact complete artifact atomically populates
def test_final_populates_all_rows(tmp_path):
    doc = _artifact([
        _one_result(),
        _classification("MSFT", "finnhub",
                        "Microsoft announces new product line", T0)])
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    report = _populate(cache, _write(tmp_path, doc), tmp_path,
                       tickers=("AAPL", "MSFT"))
    assert report.complete
    assert report.inserted_row_count == 2
    assert report.expected_identity_count == 2
    assert report.missing_identity_count == 0
    assert report.extra_identity_count == 0
    assert cache.headline_count() == 2


# 3. identical replay is idempotent
def test_identical_replay_idempotent(tmp_path):
    doc = _artifact([_one_result()])
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    path = _write(tmp_path, doc)
    r1 = _populate(cache, path, tmp_path)
    r2 = _populate(cache, path, tmp_path)
    assert r1.inserted_row_count == 1 and r1.idempotent_existing_row_count == 0
    assert r2.inserted_row_count == 0
    assert r2.idempotent_existing_row_count == 1
    assert cache.headline_count() == 1


# 4. conflicting existing classification fails closed
def test_conflicting_classification_fails_closed(tmp_path):
    doc = _artifact([_one_result()])
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    _populate(cache, _write(tmp_path, doc, "a1.json"), tmp_path)
    conflict = _classification("AAPL", "finnhub",
                               "Apple beats earnings expectations", T0)
    conflict["classification"]["direction"] = "BEARISH"
    conflict["classification"]["headline_hash"] = _hash(
        "Apple beats earnings expectations")
    from backtest.news.cache import CacheKeyConflictError
    with pytest.raises(CacheKeyConflictError):
        _populate(cache, _write(tmp_path, _artifact([conflict]), "a2.json"),
                  tmp_path)


# 5. conflicting provenance fails closed
def test_conflicting_provenance_fails_closed(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    doc = _artifact([_one_result()])
    # artifact claims a different model_version for the same payload
    row = _one_result()
    row["classification"]["model_version"] = "openrouter/other/model@v9"
    with pytest.raises(PopulationProvenanceError):
        load_offline_population_artifact(
            _write(tmp_path, _artifact([row])),
            expected_model_version=PIN, llm_config_version="cfg-1")
    assert cache.headline_count() == 0


# 6. unknown headline fails closed
def test_unknown_headline_fails_closed(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    report = _populate(cache, _write(tmp_path, _artifact([
        _classification("GOOG", "finnhub", "Unknown ticker headline", T0)])),
        tmp_path, final=True)
    assert not report.complete
    assert report.errors[0]["error_class"] == "unknown_headline_identity"
    assert cache.headline_count() == 0


# 7. wrong ticker fails closed
def test_wrong_ticker_fails_closed(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    report = _populate(cache, _write(tmp_path, _artifact([
        _classification("MSFT", "finnhub",
                        "Apple beats earnings expectations", T0)])),
        tmp_path, final=True)
    assert not report.complete
    assert report.errors[0]["error_class"] == "ticker_mismatch"
    assert cache.headline_count() == 0


# 8. duplicate input identity fails closed
def test_duplicate_identity_fails_closed(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    with pytest.raises(ArtifactFormatError):
        _populate(cache, _write(tmp_path, _artifact([_one_result(),
                                                     _one_result()])),
                  tmp_path)
    assert cache.headline_count() == 0


# 9. missing requested identity fails closed (EXACT set-equality — the
# artifact can never publish a strict subset of the canonical inventory)
def test_missing_requested_identity_fails_closed(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    # scope requests BOTH tickers, artifact carries only AAPL's result —
    # MSFT's canonical headline is omitted. The omitted row lies INSIDE
    # verified coverage, so the old per-row guard would have passed;
    # exact set-equality must still fail closed.
    doc = _artifact([_one_result()])
    report = _populate(cache, _write(tmp_path, doc), tmp_path,
                       tickers=("AAPL", "MSFT"))
    assert not report.complete
    assert report.missing_identity_count == 1
    assert report.errors[0]["error_class"] == "missing_requested_identity"
    assert report.missing_identities[0]["ticker"] == "MSFT"
    assert cache.headline_count() == 0  # ZERO rows published


# 10. extra identity fails closed
def test_extra_identity_fails_closed(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    doc = _artifact([
        _one_result(),
        _classification("AAPL", "bloomberg",
                        "Apple beats earnings expectations", T0)])
    report = _populate(cache, _write(tmp_path, doc), tmp_path, final=True)
    assert not report.complete
    assert report.errors[0]["error_class"] == "unknown_headline_identity"
    assert cache.headline_count() == 0


# 11. malformed classification fails closed
def test_malformed_classification_fails_closed(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    row = _one_result()
    del row["classification"]["severity"]
    with pytest.raises(ArtifactFormatError):
        _populate(cache, _write(tmp_path, _artifact([row])), tmp_path)
    assert cache.headline_count() == 0


# 12. invalid enum fails closed
def test_invalid_enum_fails_closed(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    row = _one_result()
    row["classification"]["category"] = "GOSSIP"
    with pytest.raises(ArtifactFormatError):
        _populate(cache, _write(tmp_path, _artifact([row])), tmp_path)
    assert cache.headline_count() == 0


# 13. invalid ma_role conditional fails closed
def test_invalid_ma_role_conditional_fails_closed(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    row = _one_result()
    row["classification"]["category"] = "EARNINGS"
    row["classification"]["ma_role"] = "TARGET"
    with pytest.raises(ArtifactFormatError):
        _populate(cache, _write(tmp_path, _artifact([row])), tmp_path)
    assert cache.headline_count() == 0


# 14. mixed classifier provenance fails closed
def test_mixed_provenance_fails_closed(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    rows = [_one_result(),
            _classification("MSFT", "finnhub",
                            "Microsoft announces new product line", T0)]
    rows[1]["classification"]["model_version"] = "openrouter/x/y@v2"
    with pytest.raises(PopulationProvenanceError):
        _populate(cache, _write(tmp_path, _artifact(rows)), tmp_path)
    assert cache.headline_count() == 0


# 15. expected classifier-pin mismatch fails closed
def test_pin_mismatch_fails_closed(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    with pytest.raises(PopulationProvenanceError):
        _populate(cache, _write(tmp_path, _artifact([_one_result()])),
                  tmp_path, pin="openrouter/otherorg/other-model@v2")
    assert cache.headline_count() == 0


# 16. coverage gap inside the requested window cannot populate (FINAL
# or PREVIEW) — the interior-gap case; no artifact row lies inside the gap
def test_incomplete_coverage_final_refused(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    # requested window extends past the verified span (span ends Jan 31)
    doc = _artifact([_one_result()])
    with pytest.raises(CoverageGuardError):
        _populate(cache, _write(tmp_path, doc), tmp_path, final=True,
                  end="2024-02-15")
    assert cache.headline_count() == 0
    # identical failure in PREVIEW (same checks, zero writes)
    with pytest.raises(CoverageGuardError):
        _populate(cache, _write(tmp_path, doc, "p.json"), tmp_path,
                  final=False, end="2024-02-15")
    assert cache.headline_count() == 0


# 16b. INTERIOR coverage gap fails closed even when an artifact row
# occurs on both sides of the gap and the gap itself holds no headline
def test_interior_coverage_gap_fails_closed(tmp_path):
    gap_start = dt.datetime(2024, 1, 10, tzinfo=dt.timezone.utc)
    gap_end = dt.datetime(2024, 1, 17, tzinfo=dt.timezone.utc)
    spans = [("AAPL", dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
              gap_start),
             ("AAPL", gap_end,
              dt.datetime(2024, 2, 1, tzinfo=dt.timezone.utc))]
    cache = _make_store(tmp_path / "a.sqlite3", _HL, spans)
    # headline occurs Jan 5 — outside the gap; old per-row check passed,
    # whole-window union must still fail
    with pytest.raises(CoverageGuardError) as exc:
        _populate(cache, _write(tmp_path, _artifact([_one_result()])),
                  tmp_path, final=True, tickers=("AAPL",))
    assert "2024-01-10" in str(exc.value)
    assert cache.headline_count() == 0


# 16c. multiple ADJACENT/OVERLAPPING verified spans whose union covers
# the entire window are allowed (existing coverage semantics)
def test_adjacent_overlapping_spans_allowed(tmp_path):
    spans = [("AAPL", dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
              dt.datetime(2024, 1, 15, tzinfo=dt.timezone.utc)),
             ("AAPL", dt.datetime(2024, 1, 15, tzinfo=dt.timezone.utc),
              dt.datetime(2024, 1, 20, tzinfo=dt.timezone.utc)),
             ("AAPL", dt.datetime(2024, 1, 18, tzinfo=dt.timezone.utc),
              dt.datetime(2024, 2, 1, tzinfo=dt.timezone.utc))]
    cache = _make_store(tmp_path / "a.sqlite3", _HL, spans)
    report = _populate(cache, _write(tmp_path, _artifact([_one_result()])),
                       tmp_path, final=True, tickers=("AAPL",))
    assert report.complete
    ev = [e for e in report.coverage_evidence if e["ticker"] == "AAPL"][0]
    assert ev["fully_covered"] and ev["merged_span_count"] == 1
    assert cache.headline_count() == 1


# 17. whole-window verified coverage permits FINAL
def test_complete_coverage_permits_final(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    report = _populate(cache, _write(tmp_path, _artifact([_one_result()])),
                       tmp_path, final=True)
    assert report.complete
    assert report.coverage_evidence
    assert report.coverage_evidence[0]["fully_covered"]


# 18. PREVIEW performs zero writes
def test_preview_writes_nothing(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    doc = _artifact([_one_result()])
    report = _populate(cache, _write(tmp_path, doc), tmp_path, final=False)
    assert report.complete and report.mode == "PREVIEW"
    assert report.inserted_row_count == 0
    assert cache.headline_count() == 0


# 19. failure on row N leaves zero partial publication
def test_partial_failure_leaves_no_rows(tmp_path):
    from backtest.news.cache import CacheKeyConflictError
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    # seed the AAPL population
    seed = _artifact([_one_result()])
    _populate(cache, _write(tmp_path, seed, "seed.json"), tmp_path,
              tickers=("AAPL",))
    assert cache.headline_count() == 1
    cache._conn.execute(
        "INSERT INTO news_headlines (headline_hash, source, ticker, "
        "published_at, headline_text_normalized, fetched_at) VALUES "
        "(?,?,?,?,?,?)",
        (_hash("Third canonical headline"), "finnhub", "AAPL",
         dt.datetime(2024, 1, 7, tzinfo=dt.timezone.utc).isoformat(),
         "Third canonical headline", "2024-01-01T00:00:00+00:00"))
    cache._conn.commit()
    third = _classification("AAPL", "finnhub", "Third canonical headline",
                            dt.datetime(2024, 1, 7, tzinfo=dt.timezone.utc))
    conflicting_seed = _one_result()
    conflicting_seed["classification"]["severity"] = "CRITICAL"
    conflicting_seed["classification"]["headline_hash"] = _hash(
        "Apple beats earnings expectations")
    doc = _artifact([third, conflicting_seed])
    with pytest.raises(CacheKeyConflictError):
        _populate(cache, _write(tmp_path, doc, "conflict.json"), tmp_path,
                  tickers=("AAPL",), start="2024-01-01", end="2024-01-10")
    assert cache.headline_count() == 1  # seed row only; third NOT published


# 20. input row ordering does not change digests
def test_row_order_does_not_change_digests(tmp_path):
    rows = [_classification("AAPL", "finnhub",
                            "Apple beats earnings expectations", T0),
            _classification("MSFT", "finnhub",
                            "Microsoft announces new product line", T0)]
    r1 = load_offline_population_artifact(
        _write(tmp_path, _artifact(rows), "ord1.json"),
        expected_model_version=PIN, llm_config_version="cfg-1")
    r2 = load_offline_population_artifact(
        _write(tmp_path, _artifact(list(reversed(rows))), "ord2.json"),
        expected_model_version=PIN, llm_config_version="cfg-1")
    from backtest.news.cache_populate_offline import _stable_digest
    d1 = _stable_digest([{"headline_hash": r.headline_hash,
                          "source": r.source, "ticker": r.ticker,
                          "published_at": r.payload["published_at"],
                          "classification": r.payload}
                         for r in sorted(r1, key=lambda r: (
                             r.headline_hash, r.source, r.ticker))])
    d2 = _stable_digest([{"headline_hash": r.headline_hash,
                          "source": r.source, "ticker": r.ticker,
                          "published_at": r.payload["published_at"],
                          "classification": r.payload}
                         for r in sorted(r2, key=lambda r: (
                             r.headline_hash, r.source, r.ticker))])
    assert d1 == d2


# 21 + 22. same input + same pin => stable digests; wall clock irrelevant
def test_stable_digests_independent_of_wallclock(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    doc = _artifact([_one_result()])
    r1 = _populate(cache, _write(tmp_path, doc), tmp_path, final=False)
    r2 = _populate(cache, _write(tmp_path, doc), tmp_path, final=False)
    assert r1.input_digest == r2.input_digest
    assert r1.classifier_pin_digest == r2.classifier_pin_digest
    assert r1.generated_at is None  # wall clock never digested


# 23. cache miss never triggers an LLM/network fallback
def test_cache_miss_is_deterministic_no_llm(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    inv = HeadlineInventory(cache._conn)
    assert cache.lookup(_hash("Apple beats earnings expectations"),
                        "finnhub", schema_version=SCHEMA,
                        model_version=PIN, ticker="AAPL") is None


# 24. population path has no network/LLM dependency
def test_population_module_never_imports_llm_stack():
    import subprocess
    import sys as _sys
    code = (
        "import sys; before=set(sys.modules); "
        "import backtest.news.cache_populate_offline; "
        "import backtest.news.cache; "
        "print(sorted(m for m in set(sys.modules)-before "
        "if 'auxiliary' in m or 'openai' in m.lower()))")
    out = subprocess.run([_sys.executable, "-c", code],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"
    import backtest.news.cache_populate_offline as mod
    assert not hasattr(mod, "call_llm")


# 25. production news_headlines and coverage_manifests are never mutated
def test_news_headlines_and_manifests_untouched(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    conn = cache._conn
    before_hl = conn.execute(
        "SELECT COUNT(*), COALESCE(GROUP_CONCAT(headline_hash),'') "
        "FROM news_headlines").fetchone()
    before_cm = conn.execute(
        "SELECT COUNT(*), COALESCE(GROUP_CONCAT(ticker),'') "
        "FROM coverage_manifests").fetchone()
    _populate(cache, _write(tmp_path, _artifact([_one_result()])),
              tmp_path, final=True)
    after_hl = conn.execute(
        "SELECT COUNT(*), COALESCE(GROUP_CONCAT(headline_hash),'') "
        "FROM news_headlines").fetchone()
    after_cm = conn.execute(
        "SELECT COUNT(*), COALESCE(GROUP_CONCAT(ticker),'') "
        "FROM coverage_manifests").fetchone()
    assert before_hl == after_hl
    assert before_cm == after_cm


# 26. benchmark candidate JSONL cannot bypass production provenance
def test_benchmark_artifact_rejected(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    bench = {"format_version": "r281-classifier-benchmark-1",
             "candidate_id": "cand-1", "predictions": []}
    with pytest.raises(ArtifactFormatError):
        _populate(cache, _write(tmp_path, bench), tmp_path, final=True)
    assert cache.headline_count() == 0


# 27. the report never claims Phase-2 complete
def test_report_never_claims_phase2_complete(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    report = _populate(cache, _write(tmp_path, _artifact([_one_result()])),
                       tmp_path, final=True)
    assert report.phase2_complete is False
    assert "HUMAN" in report.phase2_note or "adjudication" in \
        report.phase2_note.lower()


# additional: FP-4-empty text rejected
def test_fp4_empty_text_rejected(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    row = _one_result()
    row["headline_text_normalized"] = "!!!"
    with pytest.raises(ArtifactFormatError):
        _populate(cache, _write(tmp_path, _artifact([row])), tmp_path)
    assert cache.headline_count() == 0


# additional: no run-pinned manifest -> refuse
def test_missing_manifest_refused(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    with pytest.raises(CoverageGuardError):
        _populate(cache, _write(tmp_path, _artifact([_one_result()])),
                  tmp_path, manifests=[])
    assert cache.headline_count() == 0


# additional: empty pin -> provenance error, never a guessed model
def test_empty_pin_refused(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    with pytest.raises(PopulationProvenanceError):
        _populate(cache, _write(tmp_path, _artifact([_one_result()])),
                  tmp_path, pin="")
    assert cache.headline_count() == 0


# additional: llm_config_version mismatch between artifact and expectation
def test_llm_config_version_mismatch(tmp_path):
    with pytest.raises(PopulationProvenanceError):
        load_offline_population_artifact(
            _write(tmp_path, _artifact([_one_result()],
                                       llm_config_version="cfg-2")),
            expected_model_version=PIN, llm_config_version="cfg-1")


# additional: real CLI handler end-to-end (parser -> dispatch -> handler)
def test_cli_end_to_end(tmp_path, monkeypatch):
    import hermes_cli.subcommands.backtest as bt_parser
    from backtest import cli as bt_cli

    db = tmp_path / "bt.sqlite3"
    _make_store(db, _HL, _SPANS)
    monkeypatch.setattr(bt_cli, "_backtest_db_path", lambda: db)

    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="backtest_command")
    bt_parser.build_backtest_parser(subs, cmd_backtest=lambda a: None)

    artifact = _write(tmp_path, _artifact([
        _one_result(),
        _classification("MSFT", "finnhub",
                        "Microsoft announces new product line", T0)]))
    report_path = tmp_path / "pop_report.json"

    # PREVIEW: zero writes
    args = parser.parse_args([
        "backtest", "populate-news-classification-cache-offline",
        "--artifact", artifact, "--expected-model-version", PIN,
        "--tickers", "AAPL", "MSFT", "--start", "2024-01-01",
        "--end", "2024-01-31",
        "--manifest-version", "mv-1", "--llm-config-version", "cfg-1",
        "--output-report", str(report_path)])
    rc = bt_cli.cmd_populate_news_classification_cache_offline(args)
    assert rc == 0
    cache = open_news_cache(db, read_only=True)
    assert cache.headline_count() == 0
    cache._conn.close()

    # FINAL: writes
    args = parser.parse_args([
        "backtest", "populate-news-classification-cache-offline",
        "--artifact", artifact, "--expected-model-version", PIN,
        "--tickers", "AAPL", "MSFT", "--start", "2024-01-01",
        "--end", "2024-01-31",
        "--manifest-version", "mv-1", "--llm-config-version", "cfg-1",
        "--final", "--output-report", str(report_path)])
    rc = bt_cli.cmd_populate_news_classification_cache_offline(args)
    assert rc == 0
    cache = open_news_cache(db, read_only=True)
    assert cache.headline_count() == 2
    cache._conn.close()
    saved = json.loads(report_path.read_text())
    assert saved["phase2_complete"] is False
    assert saved["mode"] == "FINAL"


# ---------------------------------------------------------------------------
# Declared-scope adversarial tests (R2.8.1 correction)
# ---------------------------------------------------------------------------

# extra VALID canonical identity OUTSIDE the requested date window fails
def test_extra_identity_outside_window_fails_closed(tmp_path):
    T_jun = dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc)
    hl = _HL + [("AAPL", "finnhub", "Apple June guidance update", T_jun)]
    spans = _SPANS + [("AAPL", dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc),
                       dt.datetime(2024, 6, 30, tzinfo=dt.timezone.utc))]
    cache = _make_store(tmp_path / "a.sqlite3", hl, spans)
    # requested window is January only; artifact carries the June headline
    doc = _artifact([_one_result(),
                     _classification("AAPL", "finnhub",
                                     "Apple June guidance update", T_jun)])
    report = _populate(cache, _write(tmp_path, doc), tmp_path,
                       tickers=("AAPL",), start="2024-01-01",
                       end="2024-01-31")
    assert not report.complete
    assert report.extra_identity_count == 1
    assert report.errors[0]["error_class"] == \
        "artifact_identity_outside_expected_inventory"
    assert cache.headline_count() == 0


# artifact containing only a subset of the requested tickers fails closed
def test_artifact_subset_of_requested_tickers_fails_closed(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    doc = _artifact([_one_result()])
    report = _populate(cache, _write(tmp_path, doc), tmp_path,
                       tickers=("AAPL", "MSFT"))
    assert not report.complete
    assert report.missing_identity_count == 1
    assert cache.headline_count() == 0


# artifact cannot define/narrow requested ticker scope: requesting only
# MSFT while the artifact carries only AAPL fails (identity outside scope)
def test_artifact_cannot_define_ticker_scope(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    doc = _artifact([_one_result()])  # AAPL only
    report = _populate(cache, _write(tmp_path, doc), tmp_path,
                       tickers=("MSFT",))
    assert not report.complete
    classes = {e["error_class"] for e in report.errors}
    assert "identity_outside_declared_scope" in classes
    assert "missing_requested_identity" in classes
    # the report scope comes from the REQUEST, not the artifact
    assert report.requested_tickers == ["MSFT"]
    assert cache.headline_count() == 0


# artifact cannot define/narrow requested date window: a narrower window
# is not adopted from artifact timestamps; the canonical window governs
def test_artifact_cannot_define_date_window(tmp_path):
    T_jan20 = dt.datetime(2024, 1, 20, tzinfo=dt.timezone.utc)
    hl = _HL + [("AAPL", "finnhub", "Apple late-January outlook", T_jan20)]
    cache = _make_store(tmp_path / "a.sqlite3", hl, _SPANS)
    # artifact carries only the Jan-20 headline and drops the Jan-5 one —
    # it cannot shrink the window to make itself complete
    doc = _artifact([_classification("AAPL", "finnhub",
                                     "Apple late-January outlook", T_jan20)])
    report = _populate(cache, _write(tmp_path, doc), tmp_path,
                       tickers=("AAPL",), start="2024-01-01",
                       end="2024-01-31")
    assert not report.complete
    assert report.missing_identity_count == 1
    assert report.requested_start == "2024-01-01"
    assert report.requested_end == "2024-01-31"
    assert cache.headline_count() == 0


# 10,000/9,999 represented at smaller scale: 40 expected, 39 present
def test_missing_one_of_many_fails_closed(tmp_path):
    hl, arts = [], []
    for i in range(40):
        pub = dt.datetime(2024, 1, 2 + i // 30, (i % 20) + 1,
                          tzinfo=dt.timezone.utc)
        text = f"Synthetic AAPL headline number {i:03d}"
        hl.append(("AAPL", "finnhub", text, pub))
        if i != 39:  # omit exactly ONE canonical identity
            arts.append(_classification("AAPL", "finnhub", text, pub))
    spans = [("AAPL", dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
              dt.datetime(2024, 2, 1, tzinfo=dt.timezone.utc))]
    cache = _make_store(tmp_path / "a.sqlite3", hl, spans)
    doc = _artifact(arts)
    report = _populate(cache, _write(tmp_path, doc), tmp_path,
                       tickers=("AAPL",))
    assert report.expected_identity_count == 40
    assert report.artifact_identity_count == 39
    assert report.missing_identity_count == 1
    assert not report.complete
    assert cache.headline_count() == 0  # even 39/40 valid rows publish NOTHING


# PREVIEW applies identical completeness/coverage checks and writes zero
def test_preview_applies_same_completeness_and_coverage(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    doc = _artifact([_one_result()])
    report = _populate(cache, _write(tmp_path, doc), tmp_path,
                       final=False, tickers=("AAPL", "MSFT"))
    assert report.mode == "PREVIEW"
    assert not report.complete  # same completeness failure as FINAL
    assert report.missing_identity_count == 1
    assert report.inserted_row_count == 0
    assert cache.headline_count() == 0
    # and same whole-window coverage failure
    with pytest.raises(CoverageGuardError):
        _populate(cache, _write(tmp_path, doc, "c.json"), tmp_path,
                  final=False, tickers=("AAPL",), end="2024-02-15")
    assert cache.headline_count() == 0


# explicit scope is reflected in the report, independent of the artifact
def test_report_scope_reflects_explicit_inputs(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    doc = _artifact([_one_result(),
                     _classification("MSFT", "finnhub",
                                     "Microsoft announces new product line",
                                     T0)])
    report = _populate(cache, _write(tmp_path, doc), tmp_path,
                       tickers=("MSFT", "AAPL"), start="2024-01-02",
                       end="2024-01-28")
    assert report.requested_tickers == ["AAPL", "MSFT"]  # sorted, from input
    assert report.requested_start == "2024-01-02"
    assert report.requested_end == "2024-01-28"
    assert report.requested_manifest_versions == ["mv-1"]
    assert report.expected_identity_count == 2


# input ordering does not affect expected-set comparison or digests
def test_scope_ordering_does_not_change_outcome(tmp_path):
    cache1 = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    cache2 = _make_store(tmp_path / "b.sqlite3", _HL, _SPANS)
    doc = _artifact([
        _one_result(),
        _classification("MSFT", "finnhub",
                        "Microsoft announces new product line", T0)])
    r1 = _populate(cache1, _write(tmp_path, doc), tmp_path,
                   tickers=("AAPL", "MSFT"))
    rows2 = [
        _classification("MSFT", "finnhub",
                        "Microsoft announces new product line", T0),
        _one_result()]
    r2 = _populate(
        cache2,
        _write(tmp_path, _artifact(list(reversed(rows2))), "ord2.json"),
        tmp_path, tickers=("MSFT", "AAPL"))
    assert r1.complete and r2.complete
    assert r1.input_digest == r2.input_digest
    assert r1.expected_identity_count == r2.expected_identity_count


# missing required scope arguments fail closed (no artifact inference)
def test_missing_scope_arguments_refused(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    doc = _write(tmp_path, _artifact([_one_result()]))
    with pytest.raises(ArtifactFormatError):
        populate_cache_from_offline_artifact(
            doc, cache=cache, inventory=HeadlineInventory(cache._conn),
            expected_model_version=PIN, manifest_versions=["mv-1"],
            requested_tickers=[], requested_start="2024-01-01",
            requested_end="2024-01-31", llm_config_version="cfg-1",
            final=True)
    with pytest.raises(ArtifactFormatError):
        populate_cache_from_offline_artifact(
            doc, cache=cache, inventory=HeadlineInventory(cache._conn),
            expected_model_version=PIN, manifest_versions=["mv-1"],
            requested_tickers=["AAPL"], requested_start="",
            requested_end="2024-01-31", llm_config_version="cfg-1",
            final=True)
    assert cache.headline_count() == 0


# artifact may not relocate a canonical headline outside the declared
# window by asserting a different published_at
def test_published_at_relocation_fails_closed(tmp_path):
    cache = _make_store(tmp_path / "a.sqlite3", _HL, _SPANS)
    row = _one_result()
    row["published_at"] = "2024-02-05T12:00:00+00:00"
    row["classification"]["published_at"] = row["published_at"]
    report = _populate(cache, _write(tmp_path, _artifact([row])), tmp_path,
                       tickers=("AAPL",))
    assert not report.complete
    assert report.errors[0]["error_class"] == "published_at_mismatch"
    assert cache.headline_count() == 0


import argparse  # noqa: E402
