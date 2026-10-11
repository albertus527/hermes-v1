"""Exports (determinism, digest), reveal gating, and DB isolation."""

from __future__ import annotations

import json

import pytest

from backtest.calibration_reviewer import exports
from backtest.calibration_reviewer.datasets import load_dataset
from backtest.calibration_reviewer.reveal import (
    consensus_reveal,
    model_reveal,
)
from backtest.calibration_reviewer.schema import (
    PRODUCTION_DB,
    assert_isolated_path,
    open_reviewer_db,
)
from backtest.calibration_reviewer.store import Store

from .conftest import make_dataset

LABEL = {"category": "EARNINGS", "direction": "BULLISH", "severity": "MEDIUM",
         "ma_role": "NEITHER"}


def _store_and_loaded(tmp_path):
    spec = make_dataset(tmp_path, n=4)
    conn = open_reviewer_db(tmp_path / "reviewer.sqlite3")
    store = Store(conn)
    loaded = load_dataset(spec)
    store.initialize_from_datasets({"A_CANONICAL_240": loaded})
    return store, loaded


def test_labels_csv_deterministic(tmp_path):
    store, loaded = _store_and_loaded(tmp_path)
    a = exports.export_labels_csv(store, "A_CANONICAL_240", loaded)
    b = exports.export_labels_csv(store, "A_CANONICAL_240", loaded)
    assert a == b


def test_labels_csv_has_one_row_per_sample(tmp_path):
    store, loaded = _store_and_loaded(tmp_path)
    csv_bytes = exports.export_labels_csv(store, "A_CANONICAL_240", loaded)
    lines = csv_bytes.decode().strip().split("\n")
    assert len(lines) == loaded.row_count + 1  # header + rows


def test_export_states_and_counts(tmp_path):
    store, loaded = _store_and_loaded(tmp_path)
    store.approve_rubric("r")
    ids = store.ordered_sample_ids("A_CANONICAL_240")
    store.lock("A_CANONICAL_240", ids[0], label=LABEL, expected_version=0,
               rubric_version="r", rubric_approved=True)
    store.save_draft("A_CANONICAL_240", ids[1], label={"category": "EARNINGS"},
                     expected_version=0)
    rows = exports.build_rows(store, "A_CANONICAL_240", loaded)
    states = {r["sample_id"]: r["review_state"] for r in rows}
    assert states[ids[0]] == "LOCKED"
    assert states[ids[1]] == "DRAFT"
    assert states[ids[2]] == "UNREVIEWED"
    locked = [r for r in rows if r["review_state"] == "LOCKED"][0]
    assert locked["counts_toward_11_4"] == 1


def test_export_post_reveal_revision_tagged(tmp_path):
    store, loaded = _store_and_loaded(tmp_path)
    store.approve_rubric("r")
    ids = store.ordered_sample_ids("A_CANONICAL_240")
    v = store.lock("A_CANONICAL_240", ids[0], label=LABEL, expected_version=0,
                   rubric_version="r", rubric_approved=True)
    v = store.reveal("A_CANONICAL_240", ids[0], kind="consensus",
                     expected_version=v)
    store.revise("A_CANONICAL_240", ids[0], label={**LABEL, "severity": "HIGH"},
                 reason="post-reveal", expected_version=v, rubric_version="r",
                 rubric_approved=True)
    rows = exports.build_rows(store, "A_CANONICAL_240", loaded)
    row = [r for r in rows if r["sample_id"] == ids[0]][0]
    assert row["post_reveal_revision"] == 1
    assert row["revision_no"] == 1


def test_integrity_json_digest_reproducible(tmp_path):
    store, loaded = _store_and_loaded(tmp_path)
    a = exports.export_integrity_json(store, {"A_CANONICAL_240": loaded})
    b = exports.export_integrity_json(store, {"A_CANONICAL_240": loaded})
    assert a == b
    doc = json.loads(a)
    assert doc["datasets"][0]["source_sha256"] == loaded.source_sha256


def test_progress_json_no_false_pass_claims(tmp_path):
    store, loaded = _store_and_loaded(tmp_path)
    doc = json.loads(exports.export_progress_json(store, "A_CANONICAL_240", loaded))
    blob = json.dumps(doc)
    assert "HUMAN_ACCEPTED" not in blob
    assert "PHASE_2_PASS" not in blob
    assert doc["counts_toward_11_4"] == 0


# ---- reveal -------------------------------------------------------------

def test_consensus_reveal_marks_provenance(tmp_path):
    _, loaded = _store_and_loaded(tmp_path)
    rev = consensus_reveal(loaded, "A_CANONICAL_240-0000")
    assert rev["provenance"] == "LLM_CONSENSUS_SPOT_CHECKED"
    assert rev["label"]["category"] == "OTHER"
    assert "NOT independently human-adjudicated" in rev["disclaimer"]


def test_model_reveal_missing_cache_is_failsoft(tmp_path):
    result = model_reveal(tmp_path / "nope.sqlite3",
                          headline_hash="0" * 64, ticker="AAPL")
    assert result["available"] is False


def test_model_reveal_reads_only(tmp_path):
    import sqlite3
    cache = tmp_path / "cache.sqlite3"
    conn = sqlite3.connect(cache)
    conn.execute(
        "CREATE TABLE news_classifications (headline_hash TEXT, ticker TEXT, "
        "model_version TEXT, schema_version TEXT, json_payload TEXT)")
    payload = json.dumps({
        "ticker": "AAPL", "category": "EARNINGS", "direction": "BULLISH",
        "severity": "MEDIUM", "ma_role": "NEITHER", "confidence": 0.9,
        "published_at": "2020-01-01T00:00:00+00:00",
        "headline_hash": "a" * 64, "source": "TEST",
        "keyword_override": False, "schema_version": "news_schema_v3",
        "model_version": "openrouter/openai/gpt-6-luna"})
    conn.execute("INSERT INTO news_classifications VALUES (?,?,?,?,?)",
                 ("a" * 64, "AAPL", "openrouter/openai/gpt-6-luna",
                  "news_schema_v3", payload))
    conn.commit()
    conn.close()
    before = cache.read_bytes()
    result = model_reveal(cache, headline_hash="a" * 64, ticker="AAPL")
    assert result["available"] is True
    assert result["label"]["category"] == "EARNINGS"
    assert result["confidence"] == 0.9
    # Cache file unchanged (read-only access).
    assert cache.read_bytes() == before


# ---- isolation ----------------------------------------------------------

def test_reviewer_db_is_not_production(tmp_path):
    resolved = assert_isolated_path(tmp_path / "reviewer.sqlite3")
    assert resolved != PRODUCTION_DB


def test_open_reviewer_db_refuses_production():
    with pytest.raises(RuntimeError):
        open_reviewer_db(PRODUCTION_DB)


def test_production_db_untouched_by_reviewer(tmp_path):
    """A full reviewer lifecycle must not create/modify the production DB."""
    if not PRODUCTION_DB.exists():
        pytest.skip("production DB not present in this environment")
    before = PRODUCTION_DB.read_bytes()
    spec = make_dataset(tmp_path, n=2)
    conn = open_reviewer_db(tmp_path / "reviewer.sqlite3")
    store = Store(conn)
    loaded = load_dataset(spec)
    store.initialize_from_datasets({"A_CANONICAL_240": loaded})
    store.lock("A_CANONICAL_240", "A_CANONICAL_240-0000", label=LABEL,
               expected_version=0, rubric_version="r", rubric_approved=True)
    assert PRODUCTION_DB.read_bytes() == before
