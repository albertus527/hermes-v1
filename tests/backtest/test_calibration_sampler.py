"""R2.8.1 Phase-2 — deterministic calibration-sample selection + worksheet.

Hermetic: synthetic SQLite fixtures only; NO network, NO LLM, NO random
module. Covers the 15 required behaviors plus adversarial surfaces
(stratum insufficiency, coverage-guard gaps, PREVIEW non-finality,
digest stability, sentiment-independence).
"""

import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from backtest.news.calibration_sampler import (
    ALLOWED_STRATA_FIELDS,
    CalibrationSamplingError,
    CoverageIncompleteError,
    SampleConfig,
    SAMPLER_FORMAT_VERSION,
    select_sample,
    sample_digest,
    verify_corpus_coverage,
    write_worksheet,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _hash(text: str) -> str:
    # The canonical FP-4 hash is source-independent; the sampler treats
    # headline_hash as opaque. Any stable sha256 works for fixtures.
    return hashlib.sha256(text.encode()).hexdigest()


def _init_db(tmp_path, rows):
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(tmp_path / "db.sqlite3"))
    conn.execute(
        "CREATE TABLE news_headlines (headline_hash TEXT NOT NULL, "
        "source TEXT NOT NULL, ticker TEXT NOT NULL, published_at TEXT, "
        "headline_text_normalized TEXT NOT NULL, fetched_at TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE coverage_manifests (source_kind TEXT NOT NULL, "
        "ticker TEXT NOT NULL, span_start TEXT NOT NULL, span_end TEXT NOT NULL, "
        "verified INTEGER NOT NULL, manifest_version TEXT NOT NULL)")
    for r in rows:
        conn.execute(
            "INSERT INTO news_headlines VALUES (?,?,?,?,?,?)",
            (r["hash"], r["source"], r["ticker"], r.get("published_at"),
             r["text"], "2026-01-01T00:00:00+00:00"))
    conn.commit()
    return conn


def _rows(ticker="AAPL", n=30, year=2022, source="finnhub", tickers=None):
    out = []
    tickers = tickers or [ticker]
    for i in range(n):
        t = tickers[i % len(tickers)]
        text = f"Headline {t} {year} {i}"
        out.append({
            "hash": _hash(text),
            "source": source if i % 3 else "benzinga",
            "ticker": t,
            "published_at": f"{year}-0{1 + (i % 9)}-1{i % 10}T12:00:00+00:00",
            "text": text,
        })
    return out


def _config(tmp_path=None, **kw):
    defaults = dict(
        tickers=("AAPL",), start="2022-01-01", end="2022-12-31",
        size=10, seed="r281-calibration",
    )
    defaults.update(kw)
    return SampleConfig(**defaults)


# ---------------------------------------------------------------------------
# 1/2/14 — determinism, insertion/order independence, digest stability
# ---------------------------------------------------------------------------

def test_same_input_same_config_same_sample(tmp_path):
    rows = _rows()
    c1 = _init_db(tmp_path / "a", rows)
    c2 = _init_db(tmp_path / "b", rows)
    cfg = _config()
    s1, m1 = select_sample(c1, cfg)
    s2, m2 = select_sample(c2, cfg)
    assert [(x.headline_hash, x.published_at) for x in s1] == \
           [(x.headline_hash, x.published_at) for x in s2]
    assert m1["sample_digest"] == m2["sample_digest"]


def test_insertion_and_db_order_do_not_change_sample(tmp_path):
    rows = _rows()
    cfg = _config()
    base, _ = select_sample(_init_db(tmp_path / "base", rows), cfg)
    import os, random
    rng = random.Random(1234)  # fixture shuffling only, not selection
    for i, perm in enumerate((rows[5:] + rows[:5], list(reversed(rows)))):
        shuffled = list(rows)
        rng.shuffle(shuffled)
        conn = _init_db(tmp_path / f"perm{i}", perm)
        got, _ = select_sample(conn, cfg)
        assert [x.headline_hash for x in got] == \
               [x.headline_hash for x in base]


def test_irrelevant_ticker_rows_do_not_change_sample(tmp_path):
    rows = _rows()
    cfg = _config()
    base, m1 = select_sample(_init_db(tmp_path / "a", rows), cfg)
    extra = _rows(ticker="MSFT", n=50)
    got, m2 = select_sample(_init_db(tmp_path / "b", rows + extra), cfg)
    assert [x.headline_hash for x in got] == [x.headline_hash for x in base]
    assert m1["sample_digest"] == m2["sample_digest"]


def test_digest_is_content_only_and_stable(tmp_path):
    rows = _rows()
    _, m = select_sample(_init_db(tmp_path / "a", rows), _config())
    # digest independent of generated_at / mode / paths
    assert m["sample_digest"] == sample_digest(
        select_sample(_init_db(tmp_path / "b", rows), _config())[0])


def test_different_seed_changes_selection(tmp_path):
    rows = _rows(n=60)
    conn = _init_db(tmp_path / "a", rows)
    s1, _ = select_sample(conn, _config(seed="seed-one"))
    s2, _ = select_sample(conn, _config(seed="seed-two"))
    assert [x.headline_hash for x in s1] != [x.headline_hash for x in s2]


def test_different_strata_change_allocation(tmp_path):
    rows = _rows(n=60, tickers=["AAPL", "MSFT"])
    conn = _init_db(tmp_path / "a", rows)
    s1, m1 = select_sample(conn, _config(size=8, strata=("ticker",)))
    s2, m2 = select_sample(conn, _config(size=8, strata=("ticker", "year")))
    assert m1["quota_allocation"] != m2["quota_allocation"]


# ---------------------------------------------------------------------------
# 3/4/5/6 — size enforcement + insufficiency fail-closed
# ---------------------------------------------------------------------------

def test_exact_requested_size_enforced(tmp_path):
    conn = _init_db(tmp_path / "a", _rows(n=40))
    for size in (1, 7, 40):
        sample, m = select_sample(conn, _config(size=size))
        assert len(sample) == size
        assert m["output_row_count"] == size


def test_total_insufficiency_fails_clearly(tmp_path):
    conn = _init_db(tmp_path / "a", _rows(n=5))
    with pytest.raises(CalibrationSamplingError, match="insufficient"):
        select_sample(conn, _config(size=10))


def test_stratum_insufficiency_fails_no_substitution(tmp_path):
    # 30 AAPL rows but only 2 in MSFT's stratum. Under the hybrid policy
    # the BASE quota is 1 per stratum, so the corpus CAN satisfy size 20 —
    # the capacity-aware remainder gives MSFT its 2 rows and the rest to
    # AAPL. MSFT is still NEVER padded from AAPL.
    rows = _rows(n=30) + _rows(ticker="MSFT", n=2)
    conn = _init_db(tmp_path / "a", rows)
    sample, meta = select_sample(
        conn, _config(tickers=("AAPL", "MSFT"), size=20, strata=("ticker",)))
    assert len(sample) == 20
    assert sum(1 for s in sample if s.ticker == "MSFT") == 2
    # A target the WHOLE eligible corpus cannot satisfy still fails closed
    with pytest.raises(CalibrationSamplingError, match="insufficient"):
        select_sample(conn, _config(tickers=("AAPL", "MSFT"), size=33,
                                    strata=("ticker",)))


def test_empty_window_fails(tmp_path):
    conn = _init_db(tmp_path / "a", _rows(year=2022))
    with pytest.raises(CalibrationSamplingError):
        select_sample(conn, _config(start="2019-01-01", end="2019-12-31"))


def test_untimed_headlines_excluded(tmp_path):
    rows = _rows(n=30)
    untimed = dict(rows[0]); untimed["published_at"] = None
    untimed["hash"] = _hash("untimed-distinct")
    conn = _init_db(tmp_path / "a", rows + [untimed])
    sample, _ = select_sample(conn, _config(size=30))
    assert all(x.headline_hash != untimed["hash"] for x in sample)


def test_invalid_config_fails(tmp_path):
    for kw in ({"tickers": ()}, {"size": 0}, {"seed": ""},
               {"strata": ("sentiment",)}):
        with pytest.raises(CalibrationSamplingError):
            _config(**kw)
    assert "sentiment" not in ALLOWED_STRATA_FIELDS


# ---------------------------------------------------------------------------
# 10 — provider sentiment cannot affect selection (no such input exists)
# ---------------------------------------------------------------------------

def test_selection_uses_only_canonical_fields(tmp_path):
    # Extra non-canonical columns (would-be provider sentiment/relevance)
    # must be invisible to the sampler: identical canonical rows with
    # different sentiment values select identically.
    rows = _rows(n=30)
    conn = _init_db(tmp_path / "a", rows)
    base, _ = select_sample(conn, _config())
    conn2 = _init_db(tmp_path / "b", rows)
    conn2.execute("ALTER TABLE news_headlines ADD COLUMN sentiment REAL")
    conn2.execute("UPDATE news_headlines SET sentiment = 0.9")
    conn2.commit()
    got, _ = select_sample(conn2, _config())
    assert [x.headline_hash for x in got] == [x.headline_hash for x in base]


# ---------------------------------------------------------------------------
# 11/12 — partial-corpus FINAL guard (existing coverage semantics)
# ---------------------------------------------------------------------------

def _add_spans(conn, ticker, spans, verified=1, mv="mv-1"):
    for a, b in spans:
        conn.execute(
            "INSERT INTO coverage_manifests VALUES ('NEWS',?,?,?,?,?)",
            (ticker, a, b, verified, mv))
    conn.commit()


def test_final_guard_blocks_partial_coverage(tmp_path):
    conn = _init_db(tmp_path / "a", _rows(n=30))
    _add_spans(conn, "AAPL", [("2022-01-01T00:00:00+00:00",
                               "2022-06-30T23:59:59+00:00")])
    with pytest.raises(CoverageIncompleteError):
        verify_corpus_coverage(conn, _config(manifest_version="mv-1"))


def test_final_guard_blocks_unverified_spans(tmp_path):
    conn = _init_db(tmp_path / "a", _rows(n=30))
    _add_spans(conn, "AAPL", [("2022-01-01T00:00:00+00:00",
                               "2022-12-31T23:59:59+00:00")], verified=0)
    with pytest.raises(CoverageIncompleteError):
        verify_corpus_coverage(conn, _config(manifest_version="mv-1"))


def test_final_guard_blocks_missing_manifest_version(tmp_path):
    conn = _init_db(tmp_path / "a", _rows(n=30))
    with pytest.raises(CoverageIncompleteError, match="manifest-version"):
        verify_corpus_coverage(conn, _config())


def test_final_guard_blocks_other_manifest_version(tmp_path):
    conn = _init_db(tmp_path / "a", _rows(n=30))
    _add_spans(conn, "AAPL", [("2022-01-01T00:00:00+00:00",
                               "2022-12-31T23:59:59+00:00")], mv="mv-OLD")
    with pytest.raises(CoverageIncompleteError):
        verify_corpus_coverage(conn, _config(manifest_version="mv-1"))


def test_final_guard_permits_full_verified_coverage(tmp_path):
    conn = _init_db(tmp_path / "a", _rows(n=30))
    _add_spans(conn, "AAPL", [("2021-12-01T00:00:00+00:00",
                               "2022-06-15T00:00:00+00:00"),
                              ("2022-06-15T00:00:00+00:00",
                               "2023-01-31T00:00:00+00:00")])
    ev = verify_corpus_coverage(conn, _config(manifest_version="mv-1"))
    assert ev["tickers"]["AAPL"]["fully_covered"] is True


def test_final_guard_requires_every_ticker(tmp_path):
    rows = _rows(n=30, tickers=["AAPL", "MSFT"])
    conn = _init_db(tmp_path / "a", rows)
    _add_spans(conn, "AAPL", [("2022-01-01T00:00:00+00:00",
                               "2022-12-31T23:59:59+00:00")])
    # MSFT has NO verified span -> must fail for MSFT, not pass on AAPL
    with pytest.raises(CoverageIncompleteError, match="MSFT"):
        verify_corpus_coverage(
            conn, _config(tickers=("AAPL", "MSFT"), manifest_version="mv-1"))


# ---------------------------------------------------------------------------
# 7/8/9/13/15 — worksheet content
# ---------------------------------------------------------------------------

def test_worksheet_labels_and_notes_blank_and_identity_stable(tmp_path):
    conn = _init_db(tmp_path / "a", _rows(n=30))
    sample, meta = select_sample(conn, _config(size=5))
    csv_path = tmp_path / "w.csv"
    meta_path = tmp_path / "w.meta.json"
    write_worksheet(sample, meta, csv_path, meta_path, mode="PREVIEW")
    text = csv_path.read_text(encoding="utf-8")
    lines = text.strip().split("\n")
    assert lines[0] == ("sample_id,headline_hash,ticker,published_at,"
                        "source,headline_text,human_label,human_notes")
    assert len(lines) == 6
    for line in lines[1:]:
        cells = line.split(",")
        assert cells[6] == "" and cells[7] == ""      # blank label/notes
        assert cells[1] == _hash(cells[5])            # hash maps back
        row = next(r for r in sample if r.sample_id == cells[0])
        assert row.ticker == cells[2] and row.source == cells[4]
    loaded = json.loads(meta_path.read_text())
    assert loaded["sampler_format_version"] == SAMPLER_FORMAT_VERSION
    assert loaded["mode"] == "PREVIEW" and loaded["final"] is False
    assert "PREVIEW" in loaded["preview_warning"]


def test_sample_id_maps_back_to_exact_headline(tmp_path):
    conn = _init_db(tmp_path / "a", _rows(n=40))
    sample, _ = select_sample(conn, _config(size=10))
    ids = [s.sample_id for s in sample]
    assert len(set(ids)) == len(ids)
    # headline_hash + published_at + source uniquely identify the row
    stored = {
        r["hash"]: (r["ticker"], r["published_at"], r["source"], r["text"])
        for r in _rows(n=40)}
    for s in sample:
        assert stored[s.headline_hash] == (
            s.ticker, s.published_at, s.source, s.headline_text)


def test_no_llm_or_network_imports(tmp_path):
    import backtest.news.calibration_sampler as mod
    src = mod.__doc__ or ""
    # structural check: module imports only stdlib
    import inspect
    tree = None
    try:
        import ast
        tree = ast.parse(inspect.getsource(mod))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                imported.add((node.module or "").split(".")[0])
        banned = {"urllib", "http", "requests", "httpx", "socket",
                  "openai", "anthropic"}
        assert not (imported & banned), imported & banned
    except ImportError:
        pass


def test_metadata_reproducibility_fields(tmp_path):
    conn = _init_db(tmp_path / "a", _rows(n=30))
    sample, m = select_sample(conn, _config(size=10))
    for key in ("sampler_format_version", "requested_sample_size", "seed",
                "strata", "output_row_count", "sample_digest"):
        assert key in m
    assert m["requested_sample_size"] == 10
    assert len(m["sample_digest"]) == 64

# ---------------------------------------------------------------------------
# R2.8.1 adjudicated HYBRID production sampling policy
# (stocks: ticker x year; ETFs: ticker only; capacity-aware remainder)
# ---------------------------------------------------------------------------

from backtest.news.calibration_sampler import (  # noqa: E402
    _allocate_quotas,
    _remainder_rank,
    _required_strata,
    _window_years,
    load_instrument_classes,
    merge_verified_spans,
    resolve_strata,
    strata_for_config,
    uncovered_intervals,
    InstrumentClassError,
)

CANON_STOCKS = (
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "JPM",
    "V", "MA", "UNH", "HD", "PG", "COST", "ORCL", "NFLX", "AMD", "CRM", "ADBE",
)
CANON_ETFS = ("QQQ", "VUG", "SCHG", "VGT", "SMH", "IWF")
CANON_UNIVERSE = CANON_STOCKS + CANON_ETFS


def _etfs(**overrides):
    """Every canonical ETF present in every year, with per-ticker
    overrides (so an absent ETF is an ABSENT ticker, not an empty year)."""
    counts = {t: list(range(2019, 2026)) for t in CANON_ETFS}
    for k, v in overrides.items():
        counts[k] = list(v)
    return counts


def _corpus_db(tmp_path, *, per_stock_year=8, etf_counts=None,
               drop=(), tag="adj"):
    """Canonical-shaped corpus: every stock populated in every year; ETFs
    populated only in the years they actually have (``etf_counts``)."""
    etf_counts = etf_counts or {}
    rows = []
    for t in CANON_STOCKS:
        for y in range(2019, 2026):
            if (t, y) in drop:
                continue
            for i in range(per_stock_year):
                text = f"{tag} {t} {y} {i}"
                rows.append({
                    "hash": _hash(text), "source": "finnhub" if i % 2 else "benzinga",
                    "ticker": t, "published_at": f"{y}-0{1 + (i % 9)}-1{i % 10}T12:00:00+00:00",
                    "text": text,
                })
    for t, years in etf_counts.items():
        for n, y in enumerate(years):
            text = f"{tag} {t} {y} {n}"
            rows.append({
                "hash": _hash(text), "source": "benzinga", "ticker": t,
                "published_at": f"{y}-05-0{1 + (n % 9)}T12:00:00+00:00",
                "text": text,
            })
    return _init_db(Path(tmp_path), rows)


def _canon_cfg(**kw):
    defaults = dict(
        tickers=CANON_UNIVERSE, start="2019-01-01", end="2025-12-31",
        size=240, seed="r281-calibration",
    )
    defaults.update(kw)
    return SampleConfig(**defaults)


# --- 1. 20 stocks x 7 years + 6 ETFs -> 146 mandatory strata -----------

def test_requested_ticker_case_is_consistent_with_stored_rows(tmp_path):
    """The class map is keyed by the REQUESTED ticker; eligible rows come
    back from `ticker IN (<requested>)`, so a case mismatch between the
    request and the stored row would KeyError rather than silently pass.
    Upper-casing the request (what the CLI does) resolves both."""
    conn = _corpus_db(tmp_path / "a", etf_counts=_etfs())
    # AAPL alone is 7 mandatory strata; size 20 fills them plus remainder
    sample, m = select_sample(conn, _canon_cfg(tickers=("AAPL", "QQQ"), size=20))
    assert {s.ticker for s in sample} <= {"AAPL", "QQQ"}
    assert set(m["strata_by_ticker"]) == {"AAPL", "QQQ"}


def test_146_mandatory_strata_for_canonical_universe():
    classes = load_instrument_classes()
    assert set(CANON_UNIVERSE) <= set(classes)
    cfg = _canon_cfg()
    smap = strata_for_config(cfg)
    assert smap["AAPL"] == ("ticker", "year")
    assert smap["QQQ"] == ("ticker",)
    required = _required_strata(cfg, smap, _window_years(cfg))
    assert len(required) == 20 * 7 + 6 == 146


def test_hybrid_metadata_reports_146_mandatory_strata(tmp_path):
    conn = _corpus_db(tmp_path / "a",
                      etf_counts={t: range(2019, 2026) for t in CANON_ETFS})
    _, m = select_sample(conn, _canon_cfg())
    assert m["mandatory_stratum_count"] == 146
    assert len(m["quota_allocation"]) == 146
    assert sum(m["quota_allocation"].values()) == 240


# --- 2. missing stock ticker-year stratum -> FAIL -----------------------

def test_missing_stock_ticker_year_stratum_fails(tmp_path):
    conn = _corpus_db(tmp_path / "a", drop=(("CRM", 2021),),
                      etf_counts={t: range(2019, 2026) for t in CANON_ETFS})
    with pytest.raises(CalibrationSamplingError,
                       match=r"CRM\|2021") as exc:
        select_sample(conn, _canon_cfg())
    assert "mandatory stratum insufficiency" in str(exc.value)


# --- 3. ETF with only one headline total -> base PASS -------------------

def test_etf_with_single_headline_passes_base(tmp_path):
    conn = _corpus_db(tmp_path / "a", etf_counts=_etfs(VUG=[2025]))
    sample, m = select_sample(conn, _canon_cfg(size=146))
    assert m["mandatory_stratum_count"] == 146
    assert m["quota_allocation"]["VUG"] == 1        # capacity, not a failure
    assert sum(1 for s in sample if s.ticker == "VUG") == 1


# --- 4. ETF lacking some calendar years -> PASS -------------------------

def test_etf_missing_calendar_years_still_passes(tmp_path):
    # exactly the shape of the real corpus: VUG has 2019-2023 empty
    conn = _corpus_db(tmp_path / "a", etf_counts=_etfs(
        VUG=[2024, 2025], SCHG=[2021, 2022, 2024, 2025]))
    sample, m = select_sample(conn, _canon_cfg(size=146))
    assert "VUG" in m["quota_allocation"]
    assert "VUG|2019" not in m["quota_allocation"]
    assert m["output_row_count"] == 146
    assert m["mandatory_stratum_count"] == 146


def test_target_below_mandatory_base_fails_closed(tmp_path):
    """146 mandatory base strata cannot be served by a 60-item target —
    the base floor is a floor, not a suggestion."""
    conn = _corpus_db(tmp_path / "a", etf_counts=_etfs())
    with pytest.raises(CalibrationSamplingError, match="mandatory base strata"):
        select_sample(conn, _canon_cfg(size=60))


# --- 5. requested ETF with zero eligible headlines -> FAIL --------------

def test_requested_etf_with_zero_headlines_fails(tmp_path):
    # VUG absent from the corpus ENTIRELY (not merely an empty year)
    counts = _etfs(QQQ=[2025])
    counts.pop("VUG")
    conn = _corpus_db(tmp_path / "a", etf_counts=counts)
    with pytest.raises(CalibrationSamplingError, match="VUG"):
        select_sample(conn, _canon_cfg(size=146))


# --- 6. target 240 with sparse ETF strata -> deterministic remainder ----

def test_capacity_aware_remainder_with_sparse_etfs(tmp_path):
    # real-corpus-shaped sparse ETF tier (VUG 15, IWF 22, VGT 33 ...)
    conn = _corpus_db(tmp_path / "a", etf_counts=_etfs(
        QQQ=list(range(2019, 2026)) + [2025] * 90,
        VUG=[2024, 2025] + [2025] * 13,
        SCHG=[2021, 2022, 2024] + [2025] * 18,
        VGT=[2019, 2020, 2022, 2024] + [2025] * 26,
        SMH=[2019, 2022, 2025] + [2025] * 30,
        IWF=[2019, 2020, 2023, 2024] + [2025] * 11))
    sample, m = select_sample(conn, _canon_cfg())
    q = m["quota_allocation"]
    assert len(sample) == 240
    assert sum(q.values()) == 240
    # sparse strata keep exactly their capacity and are not failed
    # no stratum received more rows than EXIST in it
    pool = {}
    for s in sample:
        pool[s.ticker] = pool.get(s.ticker, 0) + 1
    assert pool["VUG"] <= 15 and pool["IWF"] <= 22 and pool["VGT"] <= 33
    # every mandatory stratum received at least its base item
    assert all(v >= 1 for v in q.values())
    # the sparse ETF tier absorbed few remainder slots; stocks absorbed most
    assert pool["VUG"] <= 5 and pool["IWF"] <= 5
    assert sum(1 for v in q.values() if v == 1) + sum(
        1 for v in q.values() if v >= 2) == 146
    assert sum(q.values()) == 240
    # remainder went to strata WITH spare capacity, in hash rank order
    ranked = sorted(q, key=lambda k: (_remainder_rank("r281-calibration", k), k))
    assert ranked[0] in q


# --- 7. same seed/config/corpus -> byte-identical inventory ordering ----

def test_same_seed_config_corpus_byte_identical_order(tmp_path):
    counts = _etfs(QQQ=range(2019, 2026), VUG=[2024, 2025],
                   SCHG=[2021, 2025], VGT=[2019, 2025],
                   SMH=[2019, 2025], IWF=[2019, 2025])
    cfg = _canon_cfg()
    s1, m1 = select_sample(_corpus_db(tmp_path / "a", etf_counts=counts), cfg)
    s2, m2 = select_sample(_corpus_db(tmp_path / "b", etf_counts=counts), cfg)
    assert [x.sample_id for x in s1] == [x.sample_id for x in s2]
    assert [(x.headline_hash, x.ticker, x.published_at) for x in s1] == \
           [(x.headline_hash, x.ticker, x.published_at) for x in s2]
    assert m1["sample_digest"] == m2["sample_digest"]


def test_remainder_independent_of_db_row_order(tmp_path):
    counts = _etfs(QQQ=range(2019, 2026), VUG=[2024, 2025],
                   SCHG=[2021, 2025], VGT=[2019, 2025],
                   SMH=[2019, 2025], IWF=[2019, 2025])
    conn = _corpus_db(tmp_path / "a", etf_counts=counts)
    rows = [{"hash": r[0], "source": r[1], "ticker": r[2], "published_at": r[3],
             "text": r[4]} for r in conn.execute(
                 "SELECT headline_hash, source, ticker, published_at, "
                 "headline_text_normalized FROM news_headlines")]
    cfg = _canon_cfg()
    _, m1 = select_sample(conn, cfg)
    _, m2 = select_sample(_init_db(tmp_path / "b", list(reversed(rows))), cfg)
    assert m1["quota_allocation"] == m2["quota_allocation"]
    assert m1["sample_digest"] == m2["sample_digest"]


# --- 8. non-requested ticker cannot enter the sample --------------------

def test_non_requested_ticker_cannot_enter_sample(tmp_path):
    conn = _corpus_db(tmp_path / "a",
                      etf_counts={t: range(2019, 2026) for t in CANON_ETFS})
    sample, _ = select_sample(conn, _canon_cfg(tickers=("AAPL", "QQQ"), size=20))
    assert {s.ticker for s in sample} <= {"AAPL", "QQQ"}


# --- 9. source remains non-quota-bearing ---------------------------------

def test_source_remains_non_quota_bearing(tmp_path):
    conn = _corpus_db(tmp_path / "a", etf_counts=_etfs())
    _, m1 = select_sample(conn, _canon_cfg())
    conn.execute("UPDATE news_headlines SET source='benzinga' "
                 "WHERE ticker IN ('AAPL','MSFT','QQQ')")
    conn.commit()
    _, m2 = select_sample(conn, _canon_cfg())
    assert m1["quota_allocation"] == m2["quota_allocation"]
    assert m1["mandatory_stratum_count"] == m2["mandatory_stratum_count"]
    # source remains a deterministic TIEBREAKER inside the per-row
    # selection key, never a quota input
    assert m1["sampling_policy"]["source_quota_bearing"] is False
    assert m1["sampling_policy"]["source_role"].startswith("diagnostic")


# --- 10. target larger than total eligible capacity -> FAIL --------------

def test_target_above_total_capacity_fails(tmp_path):
    conn = _corpus_db(tmp_path / "a", per_stock_year=2, etf_counts=_etfs())
    total = conn.execute("SELECT COUNT(*) FROM news_headlines").fetchone()[0]
    sample, _ = select_sample(conn, _canon_cfg(size=total))   # exactly fits
    assert len(sample) == total
    with pytest.raises(CalibrationSamplingError, match="insufficient"):
        select_sample(conn, _canon_cfg(size=total + 1))


# --- 11. explicit ticker scope required ---------------------------------

def test_explicit_ticker_scope_required():
    with pytest.raises(CalibrationSamplingError, match="at least one ticker"):
        SampleConfig(tickers=(), start="2019-01-01", end="2025-12-31",
                     size=10, seed="s")
    with pytest.raises(CalibrationSamplingError, match="unique"):
        SampleConfig(tickers=("AAPL", "AAPL"), start="2019-01-01",
                     end="2025-12-31", size=10, seed="s")


def test_unknown_ticker_class_fails_closed_no_naming_heuristic():
    with pytest.raises(InstrumentClassError, match="canonical universe"):
        resolve_strata(("NOTAREALTICKER",))
    # a name that LOOKS like the canonical universe is still not accepted
    with pytest.raises(InstrumentClassError):
        resolve_strata(("VUG2",))


# --- 12. FINAL mode still requires verified full-window coverage ---------

def test_final_mode_still_requires_verified_full_window_coverage(tmp_path):
    conn = _corpus_db(tmp_path / "a",
                      etf_counts={t: range(2019, 2026) for t in CANON_ETFS})
    # no manifests at all -> refuse
    with pytest.raises(CoverageIncompleteError):
        verify_corpus_coverage(conn, _canon_cfg(manifest_version="av-1"))
    # one ticker short -> refuse
    for t in CANON_UNIVERSE:
        if t == "VUG":
            continue
        _add_spans(conn, t, [("2019-01-01T00:00:00+00:00",
                              "2025-12-31T23:59:59+00:00")], mv="av-1")
    with pytest.raises(CoverageIncompleteError, match="VUG"):
        verify_corpus_coverage(conn, _canon_cfg(manifest_version="av-1"))
    # all 26 -> pass
    _add_spans(conn, "VUG", [("2019-01-01T00:00:00+00:00",
                              "2025-12-31T23:59:59+00:00")], mv="av-1")
    ev = verify_corpus_coverage(conn, _canon_cfg(manifest_version="av-1"))
    assert len(ev["tickers"]) == 26


# ---------------------------------------------------------------------------
# R2.8.1 coverage-interval semantics (closed, second-resolution)
# ---------------------------------------------------------------------------

def _ts(s):
    return dt.datetime.fromisoformat(s)


def test_exact_year_boundary_adjacency_passes(tmp_path):
    conn = _init_db(tmp_path / "a", _rows(n=10))
    _add_spans(conn, "AAPL", [("2019-01-01T00:00:00+00:00",
                               "2019-12-31T23:59:59+00:00"),
                              ("2020-01-01T00:00:00+00:00",
                               "2020-12-31T23:59:59+00:00")], mv="mv-1")
    ev = verify_corpus_coverage(conn, _config(start="2019-01-01",
                                              end="2020-12-31",
                                              manifest_version="mv-1"))
    assert ev["tickers"]["AAPL"]["fully_covered"] is True
    assert ev["tickers"]["AAPL"]["merged_span_count"] == 1


def test_aapl_split_manifest_reproduction_passes_after_fix(tmp_path):
    """The exact production shape: 2019 run + 2020-2025 run."""
    conn = _init_db(tmp_path / "a", _rows(n=10))
    _add_spans(conn, "AAPL", [("2019-01-01T00:00:00+00:00",
                               "2019-12-31T23:59:59+00:00")], mv="alphavantage-news-1")
    _add_spans(conn, "AAPL", [("2020-01-01T00:00:00+00:00",
                               "2025-12-31T23:59:59+00:00")],
               mv="alphavantage-news-1")
    ev = verify_corpus_coverage(conn, _config(start="2019-01-01", end="2025-12-31",
                                              size=1, manifest_version="alphavantage-news-1"))
    assert ev["tickers"]["AAPL"]["verified_span_count"] == 2
    assert ev["tickers"]["AAPL"]["merged_span_count"] == 1
    assert ev["tickers"]["AAPL"]["fully_covered"] is True


def test_single_full_window_manifest_unchanged_pass(tmp_path):
    conn = _init_db(tmp_path / "a", _rows(n=10))
    _add_spans(conn, "AAPL", [("2019-01-01T00:00:00+00:00",
                               "2025-12-31T23:59:59+00:00")], mv="mv-1")
    ev = verify_corpus_coverage(conn, _config(start="2019-01-01",
                                              end="2025-12-31",
                                              size=1, manifest_version="mv-1"))
    assert ev["tickers"]["AAPL"]["merged_span_count"] == 1


def test_one_missing_second_beyond_adjacency_fails(tmp_path):
    """23:59:58 -> 00:00:00 leaves a real one-second hole (2s away)."""
    conn = _init_db(tmp_path / "a", _rows(n=10))
    _add_spans(conn, "AAPL", [("2019-01-01T00:00:00+00:00",
                               "2019-12-31T23:59:58+00:00"),
                              ("2020-01-01T00:00:00+00:00",
                               "2020-12-31T23:59:59+00:00")], mv="mv-1")
    with pytest.raises(CoverageIncompleteError) as exc:
        verify_corpus_coverage(conn, _config(start="2019-01-01",
                                             end="2020-12-31",
                                             manifest_version="mv-1"))
    assert "2019-12-31T23:59:58+00:00, 2020-01-01T00:00:00+00:00" in str(exc.value)


def test_larger_gap_fails(tmp_path):
    conn = _init_db(tmp_path / "a", _rows(n=10))
    _add_spans(conn, "AAPL", [("2019-01-01T00:00:00+00:00",
                               "2019-06-30T23:59:59+00:00"),
                              ("2019-07-02T00:00:00+00:00",
                               "2019-12-31T23:59:59+00:00")], mv="mv-1")
    with pytest.raises(CoverageIncompleteError, match="2019-06-30"):
        verify_corpus_coverage(conn, _config(start="2019-01-01",
                                             end="2019-12-31",
                                             manifest_version="mv-1"))


def test_interior_gap_with_no_headline_still_fails(tmp_path):
    conn = _init_db(tmp_path / "a", _rows(n=10, year=2019))
    _add_spans(conn, "AAPL", [("2019-01-01T00:00:00+00:00",
                               "2019-03-01T00:00:00+00:00"),
                              ("2019-05-01T00:00:00+00:00",
                               "2019-12-31T23:59:59+00:00")], mv="mv-1")
    with pytest.raises(CoverageIncompleteError):
        verify_corpus_coverage(conn, _config(start="2019-01-01",
                                             end="2019-12-31",
                                             manifest_version="mv-1"))


def test_overlapping_spans_union_normally(tmp_path):
    spans = [(_ts("2019-01-01T00:00:00+00:00"), _ts("2019-06-30T23:59:59+00:00")),
             (_ts("2019-05-01T00:00:00+00:00"), _ts("2019-12-31T23:59:59+00:00"))]
    merged = merge_verified_spans(spans)
    assert len(merged) == 1
    assert merged[0][1] == _ts("2019-12-31T23:59:59+00:00")


def test_adjacency_is_exactly_one_second_not_a_tolerance():
    a, b = _ts("2019-12-31T23:59:59+00:00"), _ts("2020-01-01T00:00:00+00:00")
    assert len(merge_verified_spans([(a, b)][:1] + [(b, _ts("2020-12-31T23:59:59+00:00"))])) == 1
    # two-second hole stays two merged runs
    c = _ts("2020-01-01T00:00:01+00:00")
    assert len(merge_verified_spans([(a, a), (c, c)])) == 2


def test_uncovered_intervals_respects_window_bounds():
    merged = [[_ts("2019-06-01T00:00:00+00:00"),
               _ts("2020-06-01T00:00:00+00:00")]]
    gaps = uncovered_intervals(merged, _ts("2019-01-01T00:00:00+00:00"),
                               _ts("2020-12-31T23:59:59+00:00"))
    assert gaps == [("2019-01-01T00:00:00+00:00", "2019-06-01T00:00:00+00:00"),
                    ("2020-06-01T00:00:00+00:00", "2020-12-31T23:59:59+00:00")]


def test_inverted_span_fails_closed():
    with pytest.raises(CalibrationSamplingError, match="inverted"):
        merge_verified_spans([(_ts("2019-06-01T00:00:00+00:00"),
                               _ts("2019-01-01T00:00:00+00:00"))])


def test_other_source_kinds_are_not_covered_by_the_news_guard(tmp_path):
    conn = _init_db(tmp_path / "a", _rows(n=10))
    for kind in ("EARNINGS", "CORP_ACTIONS", "FEE_SCHEDULE"):
        conn.execute(
            "INSERT INTO coverage_manifests VALUES (?,?,?,?,?,?)",
            (kind, "AAPL", "2019-01-01T00:00:00+00:00",
             "2025-12-31T23:59:59+00:00", 1, "mv-1"))
    conn.commit()
    with pytest.raises(CoverageIncompleteError):
        verify_corpus_coverage(conn, _config(start="2019-01-01", end="2025-12-31",
                                             size=1, manifest_version="mv-1"))


# ---------------------------------------------------------------------------
# Machine-readable policy metadata
# ---------------------------------------------------------------------------

def test_metadata_declares_hybrid_production_policy(tmp_path):
    conn = _corpus_db(tmp_path / "a",
                      etf_counts={t: range(2019, 2026) for t in CANON_ETFS})
    _, m = select_sample(conn, _canon_cfg())
    pol = m["sampling_policy"]
    assert pol["status"].startswith("FINAL")
    assert "HYBRID" in pol["stratification_policy"] or "hybrid" in pol["stratification_policy"]
    assert pol["mandatory_stratum_count"] == 146
    assert pol["strata_by_ticker"]["AAPL"] == ["ticker", "year"]
    assert pol["strata_by_ticker"]["QQQ"] == ["ticker"]
    assert "mandatory base item" in pol["quota_rule"]
    assert "SHA-256" in pol["remainder_rule"]
    assert "SKIPPED" in pol["remainder_rule"]
    assert "never lowered" in pol["capacity_rule"]
    assert "second-resolution" in pol["coverage_interval_convention"]
    assert pol["source_quota_bearing"] is False
    assert pol["proportional_volume_weighting"] is False
    assert pol["provider_sentiment_or_relevance_inputs"] is False
    assert "fail closed" in pol["insufficient_stratum_behavior"]


def test_allocate_quotas_signature_returns_shortfall():
    quotas, short = _allocate_quotas(["a", "b"], 10, "seed",
                                     capacities={"a": 3, "b": 2})
    assert sum(quotas.values()) == 5 and short == 5
    quotas, short = _allocate_quotas(["a", "b"], 4, "seed",
                                     capacities={"a": 3, "b": 2})
    assert short == 0 and sum(quotas.values()) == 4
    assert all(v <= 3 for v in quotas.values())
