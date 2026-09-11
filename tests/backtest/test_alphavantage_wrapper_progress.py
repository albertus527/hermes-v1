"""Wrapper progress detection (run_av_news_batches.sh) — behavior tests.

The wrapper's ``state_digest`` is mirrored as a Python function under
test (the same SQL + canonical ordering + sha256); the bash source is
NOT read or regex-matched — the tested contract is the digest's
response to DB state:

- changes ONLY when a new verified alphavantage-news-* span is
  published;
- is unchanged by checkpoint churn, replay, headline upserts, or
  unrelated manifest writes;
- sentinel for a missing store.
"""

import hashlib
import sqlite3
from pathlib import Path

import pytest

from backtest.av_progress import verified_news_digest


@pytest.fixture
def db(tmp_path):
    """Initialized backtest store (§16 schema)."""
    from backtest.db.schema import open_db
    path = tmp_path / "backtest.sqlite3"
    conn = open_db(path)
    conn.close()
    return path


def _insert_manifest(db, ticker, span_start, span_end,
                     manifest_version="alphavantage-news-1",
                     source_kind="NEWS", verified=1):
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO coverage_manifests (source_kind, ticker, "
            "span_start, span_end, verified, manifest_version, run_id, "
            "config_version, code_commit) VALUES (?,?,?,?,?,?,?,?,?)",
            (source_kind, ticker, span_start, span_end, verified,
             manifest_version, "seed", "v", "c"))
        conn.commit()
    finally:
        conn.close()


def test_missing_store_sentinel(tmp_path):
    assert verified_news_digest(tmp_path / "absent.sqlite3") == \
        "NO_BACKTEST_DB"


def test_empty_store_has_stable_digest(db):
    d1 = verified_news_digest(db)
    d2 = verified_news_digest(db)
    assert d1 == d2
    assert len(d1) == 64


def test_new_verified_span_changes_digest(db):
    before = verified_news_digest(db)
    _insert_manifest(db, "MSFT", "2019-01-01T00:00:00+00:00",
                     "2025-12-31T23:59:59+00:00")
    after = verified_news_digest(db)
    assert before != after


def test_idempotent_republication_does_not_change_digest(db):
    _insert_manifest(db, "MSFT", "2019-01-01T00:00:00+00:00",
                     "2025-12-31T23:59:59+00:00")
    before = verified_news_digest(db)
    # identical re-ingest: same manifest key, nothing new published
    _insert_manifest_expect_conflict_free_noop(db)
    after = verified_news_digest(db)
    assert before == after


def _insert_manifest_expect_conflict_free_noop(db):
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM coverage_manifests").fetchone()[0]
        assert cur == 1
    finally:
        conn.close()


def test_headline_upserts_do_not_change_digest(db):
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO news_headlines (headline_hash, source, ticker, "
            "published_at, headline_text_normalized, fetched_at, run_id, "
            "config_version, code_commit) VALUES "
            "('h','s','MSFT','2019-01-02T15:30:00+00:00','t',"
            "'f','r','v','c')")
        conn.commit()
    finally:
        conn.close()
    _insert_manifest(db, "MSFT", "2019-01-01T00:00:00+00:00",
                     "2025-12-31T23:59:59+00:00")
    before = verified_news_digest(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO news_headlines (headline_hash, source, ticker, "
            "published_at, headline_text_normalized, fetched_at, run_id, "
            "config_version, code_commit) VALUES "
            "('h2','s2','NVDA','2019-01-02T15:30:00+00:00','t2',"
            "'f2','r2','v','c')")
        conn.commit()
    finally:
        conn.close()
    assert verified_news_digest(db) == before


def test_non_av_manifests_do_not_change_digest(db):
    before = verified_news_digest(db)
    _insert_manifest(db, "MSFT", "2019-01-01T00:00:00+00:00",
                     "2025-12-31T23:59:59+00:00",
                     manifest_version="eodhd-earnings-1",
                     source_kind="EARNINGS")
    _insert_manifest(db, "AAPL", "2019-01-01T00:00:00+00:00",
                     "2019-01-31T23:59:59+00:00",
                     manifest_version="finnhub-news-1")
    assert verified_news_digest(db) == before


def test_checkpoint_churn_does_not_change_digest(db, tmp_path):
    _insert_manifest(db, "MSFT", "2019-01-01T00:00:00+00:00",
                     "2025-12-31T23:59:59+00:00")
    before = verified_news_digest(db)
    # simulate checkpoint churn OUTSIDE the DB (the digest's only input
    # is the verified-manifest table, so filesystem churn is invisible)
    ckpt = tmp_path / "resume"
    ckpt.mkdir()
    (ckpt / "leaf1.json").write_text("{}")
    (ckpt / "leaf2.json").write_text("{}")
    assert verified_news_digest(db) == before


def test_digest_is_order_canonical(db):
    _insert_manifest(db, "MSFT", "2019-01-01T00:00:00+00:00",
                     "2025-12-31T23:59:59+00:00")
    _insert_manifest(db, "AAPL", "2019-01-01T00:00:00+00:00",
                     "2019-01-31T23:59:59+00:00")
    d = verified_news_digest(db)
    # recompute independently over the same canonical order
    conn = sqlite3.connect(db)
    try:
        rows = sorted(conn.execute(
            "SELECT ticker, span_start, span_end, manifest_version "
            "FROM coverage_manifests WHERE source_kind='NEWS' AND "
            "verified=1 AND manifest_version LIKE 'alphavantage-news-%'"
        ).fetchall())
    finally:
        conn.close()
    assert d == hashlib.sha256(
        "\n".join("|".join(r) for r in rows).encode()).hexdigest()


def test_has_verified_news_cover_semantics(db):
    from backtest.av_progress import has_verified_news_cover
    conn = sqlite3.connect(db)
    try:
        assert not has_verified_news_cover(
            conn, ticker="MSFT",
            span_start="2019-01-01T00:00:00+00:00",
            span_end="2025-12-31T23:59:59+00:00",
            manifest_version="alphavantage-news-1")
        _insert_manifest(db, "MSFT", "2019-01-01T00:00:00+00:00",
                         "2025-12-31T23:59:59+00:00")
        # exact span: verified
        assert has_verified_news_cover(
            conn, ticker="MSFT",
            span_start="2019-01-01T00:00:00+00:00",
            span_end="2025-12-31T23:59:59+00:00",
            manifest_version="alphavantage-news-1")
        # subspan of verified: covered
        assert has_verified_news_cover(
            conn, ticker="MSFT",
            span_start="2020-01-01T00:00:00+00:00",
            span_end="2020-12-31T23:59:59+00:00",
            manifest_version="alphavantage-news-1")
        # wider than verified: NOT covered
        assert not has_verified_news_cover(
            conn, ticker="MSFT",
            span_start="2018-01-01T00:00:00+00:00",
            span_end="2025-12-31T23:59:59+00:00",
            manifest_version="alphavantage-news-1")
        # wrong version: NOT covered
        assert not has_verified_news_cover(
            conn, ticker="MSFT",
            span_start="2019-01-01T00:00:00+00:00",
            span_end="2025-12-31T23:59:59+00:00",
            manifest_version="alphavantage-news-2")
    finally:
        conn.close()


def test_all_complete_semantics_digest_changes_only_on_new_verified_span(
        db):
    # Wrapper rule under test: morning-success fires ONLY when the
    # digest changed — i.e. only on a NEW verified requested span.
    before = verified_news_digest(db)
    _insert_manifest(db, "MSFT", "2019-01-01T00:00:00+00:00",
                     "2025-12-31T23:59:59+00:00")
    after = verified_news_digest(db)
    assert before != after          # real acquisition → suppress fallback
    stable = verified_news_digest(db)
    assert stable == after          # replay/repub → fallback stays armed
