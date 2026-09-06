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
    # 30 AAPL rows but only 2 in MSFT's stratum; ticker-stratified size 20
    # demands ~10 per ticker -> must fail, never pad MSFT from AAPL.
    rows = _rows(n=30) + _rows(ticker="MSFT", n=2)
    conn = _init_db(tmp_path / "a", rows)
    with pytest.raises(CalibrationSamplingError, match="stratum insufficiency"):
        select_sample(conn, _config(tickers=("AAPL", "MSFT"), size=20,
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
# R2.8.1 adjudicated production sampling policy (remainder allocation)
# ---------------------------------------------------------------------------

from backtest.news.calibration_sampler import _allocate_quotas, _remainder_rank


def _adjudication_db(tmp_path, tickers=26, years=7, per_stratum=30,
                     tag="adj"):
    """26 tickers x 7 years, generous rows in every stratum."""
    ticker_names = tuple(f"T{i:02d}" for i in range(tickers))
    rows = []
    for ti, t in enumerate(ticker_names):
        for y in range(2019, 2019 + years):
            for i in range(per_stratum):
                text = f"{tag} {t} {y} {i}"
                rows.append({
                    "hash": _hash(text),
                    "source": "finnhub" if i % 2 else "benzinga",
                    "ticker": t,
                    "published_at": f"{y}-0{1 + (i % 9)}-1{i % 10}T12:00:00+00:00",
                    "text": text,
                })
    return _init_db(Path(tmp_path), rows), ticker_names


ADJ_TICKERS = tuple(f"T{i:02d}" for i in range(26))


def _adj_cfg(tickers=ADJ_TICKERS, start="2019-01-01", end="2025-12-31",
             size=240, strata=("ticker", "year"),
             seed="r281-calibration") -> SampleConfig:
    return SampleConfig(tickers=tickers, start=start, end=end,
                        size=size, strata=strata, seed=seed)


def test_182_base_plus_58_deterministic_extras(tmp_path):
    conn, _ = _adjudication_db(tmp_path / "a")
    _, m = select_sample(conn, _adj_cfg())
    quotas = m["quota_allocation"]
    assert len(quotas) == 182
    base = [q for q in quotas.values() if q == 1]
    extra = [q for q in quotas.values() if q == 2]
    assert len(base) == 182 - 58 and len(extra) == 58
    assert sum(quotas.values()) == 240


def test_every_required_ticker_year_receives_at_least_one(tmp_path):
    conn, tickers = _adjudication_db(tmp_path / "a")
    _, m = select_sample(conn, _adj_cfg(tickers=tickers))
    quotas = m["quota_allocation"]
    expected = {f"{t}|{y}" for t in tickers for y in range(2019, 2026)}
    assert set(quotas) == expected
    assert all(q >= 1 for q in quotas.values())


def test_exactly_58_strata_receive_second_allocation(tmp_path):
    conn, tickers = _adjudication_db(tmp_path / "a")
    _, m = select_sample(conn, _adj_cfg(tickers=tickers))
    extras = [k for k, q in m["quota_allocation"].items() if q == 2]
    assert len(extras) == 58
    # extras match the deterministic hash ranking, not lexical order
    ranked = sorted((f"{t}|{y}" for t in tickers for y in range(2019, 2026)),
                    key=lambda k: (_remainder_rank("r281-calibration", k), k))
    assert set(extras) == set(ranked[:58])


def test_remainder_allocation_independent_of_db_order(tmp_path):
    conn, tickers = _adjudication_db(tmp_path / "a")
    _, m1 = select_sample(conn, _adj_cfg(tickers=tickers))
    # rebuild the same corpus with reversed row order
    rows = []
    for r in conn.execute("SELECT headline_hash, source, ticker, "
                          "published_at, headline_text_normalized "
                          "FROM news_headlines"):
        rows.append({"hash": r[0], "source": r[1], "ticker": r[2],
                     "published_at": r[3], "text": r[4]})
    conn2 = _init_db(tmp_path / "b", list(reversed(rows)))
    _, m2 = select_sample(conn2, _adj_cfg(tickers=tickers))
    assert m1["quota_allocation"] == m2["quota_allocation"]
    assert m1["sample_digest"] == m2["sample_digest"]


def test_lexical_ticker_order_does_not_determine_remainder(tmp_path):
    # with 26 zero-padded tickers the hash ranking must not equal the
    # lexical ranking of stratum keys for the tested seed
    tickers = ADJ_TICKERS
    keys = [f"{t}|{y}" for t in tickers for y in range(2019, 2026)]
    lexical = sorted(keys)[:58]
    hash_ranked = sorted(keys, key=lambda k: (
        _remainder_rank("r281-calibration", k), k))[:58]
    assert lexical != hash_ranked


def test_same_seed_config_identical_remainder(tmp_path):
    conn, tickers = _adjudication_db(tmp_path / "a")
    cfg = _adj_cfg(tickers=tickers)
    _, m1 = select_sample(conn, cfg)
    conn2, _ = _adjudication_db(tmp_path / "b", tag="adj")
    _, m2 = select_sample(conn2, cfg)
    assert m1["quota_allocation"] == m2["quota_allocation"]
    assert m1["sample_digest"] == m2["sample_digest"]


def test_changing_seed_changes_remainder_preserving_quotas(tmp_path):
    conn, tickers = _adjudication_db(tmp_path / "a")
    keys = [f"{t}|{y}" for t in tickers for y in range(2019, 2026)]
    a = _allocate_quotas(keys, 240, "seed-alpha")
    b = _allocate_quotas(keys, 240, "seed-beta")
    assert a != b
    assert sum(a.values()) == sum(b.values()) == 240
    assert all(v in (1, 2) for v in a.values())
    assert all(v in (1, 2) for v in b.values())
    _, m1 = select_sample(conn, _adj_cfg(tickers=tickers, seed="seed-alpha"))
    _, m2 = select_sample(conn, _adj_cfg(tickers=tickers, seed="seed-beta"))
    assert m1["quota_allocation"] != m2["quota_allocation"]
    assert sum(m2["quota_allocation"].values()) == 240
    assert len(m2["quota_allocation"]) == 182


def test_source_distribution_does_not_affect_quota_allocation(tmp_path):
    # rebuild corpus with a wildly skewed source distribution
    conn, tickers = _adjudication_db(tmp_path / "a")
    conn.execute("UPDATE news_headlines SET source='benzinga' "
                 "WHERE ticker IN ('T00','T01','T02')")
    conn.commit()
    _, m1 = select_sample(conn, _adj_cfg(tickers=tickers))
    conn2, _ = _adjudication_db(tmp_path / "b", tag="adj")
    _, m2 = select_sample(conn2, _adj_cfg(tickers=tickers))
    assert m1["quota_allocation"] == m2["quota_allocation"]
    # source diversity is diagnostic-only: reported, never quota-bearing
    assert m1["sampling_policy"]["source_quota_bearing"] is False


def test_insufficient_assigned_stratum_fails_closed(tmp_path):
    conn, tickers = _adjudication_db(tmp_path / "a")
    # strip T05's 2023 stratum down to 1 row (quota requires 1..2)
    conn.execute("DELETE FROM news_headlines WHERE ticker='T05' "
                 "AND published_at LIKE '2023-%' AND rowid > "
                 "(SELECT MIN(rowid) FROM news_headlines WHERE ticker='T05' "
                 "AND published_at LIKE '2023-%')")
    conn.commit()
    # size 480 -> 2 per stratum, but T05|2023 can only supply 1
    with pytest.raises(CalibrationSamplingError, match="stratum insufficiency"):
        select_sample(conn, _adj_cfg(tickers=tickers, size=480))


def test_metadata_declares_production_policy_machine_readably(tmp_path):
    conn, tickers = _adjudication_db(tmp_path / "a")
    _, m = select_sample(conn, _adj_cfg(tickers=tickers))
    pol = m["sampling_policy"]
    assert pol["status"].startswith("FINAL")
    assert pol["primary_strata"] == ["ticker", "year"]
    assert pol["quota_rule"].startswith("equal floor")
    assert "SHA-256" in pol["remainder_rule"]
    assert pol["source_quota_bearing"] is False
    assert pol["proportional_volume_weighting"] is False
    assert pol["provider_sentiment_or_relevance_inputs"] is False
    assert "fail closed" in pol["insufficient_stratum_behavior"]
