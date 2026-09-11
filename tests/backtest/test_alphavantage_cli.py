"""R2.8.1 §3.8 — `hermes backtest fetch-alphavantage-news` CLI job tests.

Hermetic: every fetch runs through an injected fake transport (no
network, synthetic data only — the real probe payload stays outside the
repo under the managed root and never enters the repo or these tests).

Covers the production-integration contract:
- parser: `fetch-alphavantage-news` subcommand + defaults;
- dispatch: `cmd_backtest` routes the handler; exit codes 2/3/4;
- canonical rows reach news_headlines through the EXISTING
  IngestStore.upsert_headlines path (no second NEWS path);
- coverage rides the existing shared coverage_manifests mechanism with
  source_kind='NEWS' and manifest_version 'alphavantage-news-1',
  span bounds in the existing NEWS timestamp convention;
- verified coverage ONLY after a complete sweep of demonstrably
  complete windows; saturation, envelopes, malformed payloads, and
  transport failures write NO verified coverage (§3.8 N-1-f);
- a successful zero-headline sweep IS verified coverage (§11.6);
- canonical rows from earlier successful windows may persist while the
  later-failing ticker/span stays unverified (repo precedent: the
  Finnhub/EODHD jobs persist per-ticker as the sweep proceeds; rerun is
  idempotent);
- the API key never reaches any artifact, error, or printed output;
  ALPHA_VANTAGE_KEY (optional-skills) is NOT a substitute credential;
- no classification-cache population, no strategy behavior invoked;
- rerun is idempotent with no duplicate canonical rows;
- the Finnhub news job is unchanged.
"""

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path

import pytest

from backtest.data import fetch_alphavantage
from backtest.db.schema import open_db


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def fake_transport(pages_by_url, calls=None):
    """http_get fake serving canned (status, json-body) pages per URL
    fragment (matched by substring); the last page repeats once the
    list is exhausted (multi-window sweeps)."""
    if calls is None:
        calls = []

    def http_get(url, headers=None, params=None, timeout=30.0):
        calls.append({"url": url, "params": dict(params or {})})
        for fragment, pages in pages_by_url.items():
            if fragment in url:
                if len(pages) > 1:
                    status, body = pages.pop(0)
                else:
                    status, body = pages[0]
                return status, json.dumps(body)
        return 404, "{}"

    return http_get


SECRET = "SECRET-AV-KEY-xyz789"


@pytest.fixture
def av_creds(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.get_env_value",
                        lambda key: {"ALPHAVANTAGE_API_KEY": SECRET}.get(key))


@pytest.fixture
def finnhub_creds(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.get_env_value",
                        lambda key: {"FINNHUB_API_KEY": "t"}.get(key))


@pytest.fixture
def hermes_home():
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


@pytest.fixture
def store_db(hermes_home):
    """Initialize the shared §16 backtest store (the job refuses to run
    against a missing store -> exit 2)."""
    db_dir = hermes_home / "backtest"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "backtest.sqlite3"
    conn = open_db(db_path)
    conn.close()
    return db_path


@pytest.fixture
def inject_transport(monkeypatch):
    """Route fetch_alphavantage's fetch_json calls through a fake
    transport so the REAL sweep + normalization + validation +
    saturation code executes."""
    real_fetch_json = fetch_alphavantage.fetch_json

    def _install(http):
        def patched(url, **kwargs):
            kwargs["http_get"] = http
            return real_fetch_json(url, **kwargs)
        monkeypatch.setattr(fetch_alphavantage, "fetch_json", patched)
        return patched
    return _install


def parse_backtest_args(argv):
    from hermes_cli.main import cmd_backtest
    from hermes_cli.subcommands.backtest import build_backtest_parser
    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_backtest_parser(subparsers, cmd_backtest=cmd_backtest)
    return parser.parse_args(argv)


def run_job(argv):
    args = parse_backtest_args(argv)
    return args.func(args)


def manifest_rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT source_kind, ticker, span_start, span_end, verified, "
            "manifest_version FROM coverage_manifests").fetchall()
    finally:
        conn.close()


def headline_rows(db_path, ticker=None):
    conn = sqlite3.connect(db_path)
    try:
        if ticker is None:
            return conn.execute(
                "SELECT headline_hash, source, ticker, published_at, "
                "headline_text_normalized FROM news_headlines").fetchall()
        return conn.execute(
            "SELECT headline_hash, source, ticker, published_at, "
            "headline_text_normalized FROM news_headlines "
            "WHERE ticker=?", (ticker,)).fetchall()
    finally:
        conn.close()


START, END = "2019-01-01", "2019-01-31"


def _feed_item(**overrides):
    """Synthetic Alpha Vantage feed item (established probe shape).
    Sentiment/relevance fields present so tests assert they're dropped."""
    item = {
        "authors": ["Synthetic Author"],
        "banner_image": "https://example.com/img.jpg",
        "category_within_source": "n/a",
        "overall_sentiment_label": "Bullish",
        "overall_sentiment_score": 0.35,
        "source": "Synthetic Wire",
        "source_domain": "synthetic.example",
        "summary": "Synthetic summary text.",
        "ticker_sentiment": [
            {"ticker": "AAPL", "relevance_score": "0.123456",
             "ticker_sentiment_label": "Bullish",
             "ticker_sentiment_score": 0.28},
        ],
        "time_published": "20190102T153000",
        "title": "Apple beats synthetic earnings estimates",
        "topics": [{"topic": "Earnings", "relevance_score": "0.5"}],
        "url": "https://example.com/synthetic",
    }
    item.update(overrides)
    return item


def _payload(items):
    return {
        "items": len(items),
        "sentiment_score_definition": "synthetic definition",
        "relevance_score_definition": "synthetic definition",
        "feed": items,
    }


def good_body(ticker="AAPL", title="Apple beats synthetic earnings estimates"):
    return _payload([
        _feed_item(title=title,
                   ticker_sentiment=[_ts_entry(ticker)]),
        _feed_item(title=f"{title} — second",
                   time_published="20190115T090000",
                   ticker_sentiment=[_ts_entry(ticker)]),
    ])


def _ts_entry(ticker):
    return {"ticker": ticker, "relevance_score": "0.100000",
            "ticker_sentiment_label": "Neutral",
            "ticker_sentiment_score": 0.0}


def saturated_body(n=fetch_alphavantage.NEWS_WINDOW_LIMIT, ticker="AAPL"):
    return _payload([
        _feed_item(title=f"Synthetic headline number {i}",
                   time_published=f"201901{i % 28 + 1:02d}T120000",
                   ticker_sentiment=[_ts_entry(ticker)])
        for i in range(n)])


# ---------------------------------------------------------------------------
# 1-5: parser
# ---------------------------------------------------------------------------

class TestParser:
    def test_fetch_alphavantage_news_subcommand_parses(self):
        args = parse_backtest_args(
            ["backtest", "fetch-alphavantage-news", "--tickers", "AAPL",
             "--start", START, "--end", END])
        assert args.backtest_handler == "fetch-alphavantage-news"
        assert args.tickers == "AAPL"
        assert args.start == START
        assert args.end == END
        # durable resume checkpoints default to the provider-managed
        # root (implementation state only, never coverage evidence)
        assert args.checkpoint_dir == ""

    def test_required_ticker_start_end_enforced(self):
        with pytest.raises(SystemExit):
            parse_backtest_args(
                ["backtest", "fetch-alphavantage-news", "--tickers", "AAPL"])
        with pytest.raises(SystemExit):
            parse_backtest_args(
                ["backtest", "fetch-alphavantage-news", "--start", START,
                 "--end", END])

    def test_default_manifest_version(self):
        args = parse_backtest_args(
            ["backtest", "fetch-alphavantage-news", "--tickers", "AAPL",
             "--start", START, "--end", END])
        assert args.manifest_version == "alphavantage-news-1"
        assert args.run_id == "fetch-alphavantage-news"

    def test_custom_manifest_version(self):
        args = parse_backtest_args(
            ["backtest", "fetch-alphavantage-news", "--tickers", "AAPL",
             "--start", START, "--end", END,
             "--manifest-version", "alphavantage-news-2"])
        assert args.manifest_version == "alphavantage-news-2"

    def test_custom_run_id(self):
        args = parse_backtest_args(
            ["backtest", "fetch-alphavantage-news", "--tickers", "AAPL",
             "--start", START, "--end", END, "--run-id", "av-news-2026q1"])
        assert args.run_id == "av-news-2026q1"


# ---------------------------------------------------------------------------
# 6 + dispatch/exit codes
# ---------------------------------------------------------------------------

class TestDispatchAndExitCodes:
    def test_cmd_backtest_routes_to_av_handler(self, monkeypatch):
        from backtest import cli as backtest_cli
        from hermes_cli.main import cmd_backtest
        seen = {}

        def spy(args):
            seen["called"] = True
            return 42

        monkeypatch.setattr(backtest_cli, "cmd_fetch_alphavantage_news", spy)
        args = parse_backtest_args(
            ["backtest", "fetch-alphavantage-news", "--tickers", "AAPL",
             "--start", START, "--end", END])
        assert cmd_backtest(args) == 42
        assert seen.get("called") is True

    def test_missing_credential_exits_3(self, store_db, monkeypatch, capsys):
        monkeypatch.setattr("hermes_cli.config.get_env_value",
                            lambda key: None)
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 3
        captured = capsys.readouterr()
        assert "ALPHAVANTAGE_API_KEY" in captured.out

    def test_missing_store_exits_2(self, av_creds):
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 2

    def test_optional_skills_var_is_not_a_substitute_credential(
            self, store_db, monkeypatch):
        # ALPHA_VANTAGE_KEY (optional-skills finance/stocks) must NOT
        # satisfy the backtest credential — only ALPHAVANTAGE_API_KEY.
        monkeypatch.setattr("hermes_cli.config.get_env_value",
                            lambda key: {"ALPHA_VANTAGE_KEY": "t"}.get(key))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 3


# ---------------------------------------------------------------------------
# 7-9: successful sweeps; canonical rows reach news_headlines
# ---------------------------------------------------------------------------

class TestSuccessfulJob:
    def test_one_ticker_sweep_succeeds(self, av_creds, store_db,
                                       inject_transport):
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        assert len(headline_rows(store_db, "AAPL")) == 2

    def test_multi_ticker_sweep_succeeds(self, av_creds, store_db,
                                         inject_transport):
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body("AAPL")),
                                 (200, good_body("MSFT",
                                                 "Microsoft rises on "
                                                 "synthetic cloud numbers"))]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL,MSFT",
                      "--start", START, "--end", END])
        assert rc == 0
        assert len(headline_rows(store_db, "AAPL")) == 2
        assert len(headline_rows(store_db, "MSFT")) == 2

    def test_canonical_rows_reach_news_headlines(self, av_creds, store_db,
                                                 inject_transport):
        from trading_core.news_effects import (
            headline_hash as canonical_hash,
            normalize_headline_text,
        )
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        rows = headline_rows(store_db, "AAPL")
        assert len(rows) == 2
        hh, source, ticker, published, text = rows[0]
        assert hh == canonical_hash(
            "Apple beats synthetic earnings estimates")
        assert source == "Synthetic Wire"
        assert ticker == "AAPL"
        assert published == "2019-01-02T15:30:00+00:00"
        assert text == normalize_headline_text(
            "Apple beats synthetic earnings estimates")

    def test_no_provider_sentiment_in_canonical_rows(
            self, av_creds, store_db, inject_transport):
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        conn = sqlite3.connect(store_db)
        try:
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(news_headlines)")]
            # §16 schema untouched: no provider-sentiment columns exist.
            assert "overall_sentiment_score" not in cols
            assert "ticker_sentiment_score" not in cols
            rows = conn.execute(
                "SELECT headline_text_normalized FROM news_headlines"
            ).fetchall()
            for (text,) in rows:
                assert "Bullish" not in text
                assert "0.28" not in text
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 10-13: coverage manifest
# ---------------------------------------------------------------------------

class TestCoverageManifest:
    def test_coverage_row_source_kind_news(self, av_creds, store_db,
                                           inject_transport):
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        rows = manifest_rows(store_db)
        assert len(rows) == 1
        source_kind, ticker, span_start, span_end, verified, version = rows[0]
        assert source_kind == "NEWS"
        assert ticker == "AAPL"
        assert verified == 1
        assert version == "alphavantage-news-1"

    def test_requested_span_bounds(self, av_creds, store_db,
                                   inject_transport):
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        _, _, span_start, span_end, _, _ = manifest_rows(store_db)[0]
        # Existing NEWS timestamp-bound convention: midnight-UTC start,
        # 23:59:59-UTC end of the inclusive date range.
        assert span_start == "2019-01-01T00:00:00+00:00"
        assert span_end == "2019-01-31T23:59:59+00:00"

    def test_custom_manifest_version_reaches_manifest(
            self, av_creds, store_db, inject_transport):
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END,
                      "--manifest-version", "alphavantage-news-2"])
        assert rc == 0
        assert manifest_rows(store_db)[0][5] == "alphavantage-news-2"

    def test_verified_coverage_only_after_complete_sweep(
            self, av_creds, store_db, inject_transport):
        # A multi-annual-window span: EVERY annual window must succeed
        # and be established complete before the span is attested.
        # Here the first window succeeds and the second fails — no
        # coverage. (The 2018-12-01..2019-02-28 range is two annual
        # windows: 2018 partial + 2019 partial.)
        pages = [(200, good_body()),
                 (403, {"e": "forbidden"})]
        inject_transport(fake_transport({"alphavantage.co": pages}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", "2018-12-01", "--end", "2019-02-28"])
        assert rc == 4
        assert manifest_rows(store_db) == []

    def test_complete_zero_news_span_is_verified_coverage(
            self, av_creds, store_db, inject_transport):
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, _payload([]))]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        rows = manifest_rows(store_db)
        assert len(rows) == 1 and rows[0][4] == 1  # verified zero span
        assert headline_rows(store_db) == []        # nothing invented

    def test_checkpoints_written_and_resume_skips_http(
            self, av_creds, store_db, inject_transport, tmp_path):
        # Full CLI path with an explicit --checkpoint-dir: run 1 writes
        # the leaf checkpoint; run 2 is fully served from it (no HTTP).
        calls = []
        ckpt = store_db.parent / "av-resume-test"

        def http(url, headers=None, params=None, timeout=30.0):
            calls.append(1)
            return 200, json.dumps(_payload([
                _feed_item(), _feed_item(title="Second synthetic headline",
                                         time_published="20190115T090000")]))

        inject_transport(http)
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END,
                      "--checkpoint-dir", str(ckpt)])
        assert rc == 0
        assert len(calls) == 1
        assert len(list(ckpt.glob("*.json"))) == 1
        n_manifests_after_run1 = len(manifest_rows(store_db))
        # Resume run: checkpoint hit, no HTTP request, no duplicate rows,
        # no additional coverage claims.
        inject_transport(http)
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END,
                      "--checkpoint-dir", str(ckpt)])
        assert rc == 0
        assert len(calls) == 1  # no provider request on resume
        assert len(headline_rows(store_db, "AAPL")) == 2
        assert len(manifest_rows(store_db)) == n_manifests_after_run1


# ---------------------------------------------------------------------------
# 14-21: failures write no verified coverage
# ---------------------------------------------------------------------------

class TestFailClosedCoverage:
    def test_mid_window_transport_failure_no_coverage(
            self, av_creds, store_db, inject_transport):
        # Non-retryable status (no retry burn, no real backoff sleeps).
        inject_transport(fake_transport(
            {"alphavantage.co": [(403, {"e": "forbidden"})]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 4
        assert manifest_rows(store_db) == []

    def test_information_envelope_no_coverage(
            self, av_creds, store_db, inject_transport):
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, {
                "Information": "Thank you for using Alpha Vantage!"})]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 4
        assert manifest_rows(store_db) == []

    def test_note_envelope_no_coverage(
            self, av_creds, store_db, inject_transport):
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, {
                "Note": "API call frequency is 25 per day."})]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 4
        assert manifest_rows(store_db) == []

    def test_error_message_envelope_no_coverage(
            self, av_creds, store_db, inject_transport):
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, {
                "Error Message": "Invalid API call."})]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 4
        assert manifest_rows(store_db) == []

    def test_malformed_payload_no_coverage(
            self, av_creds, store_db, inject_transport):
        # 200 with a non-envelope, non-feed shape.
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, {"unexpected": []})]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 4
        assert manifest_rows(store_db) == []

    def test_malformed_headline_no_coverage(
            self, av_creds, store_db, inject_transport):
        # Structurally valid feed whose item has a malformed timestamp.
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, _payload([
                _feed_item(time_published="not-a-timestamp")]))]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 4
        assert manifest_rows(store_db) == []

    def test_malformed_ticker_association_no_coverage(
            self, av_creds, store_db, inject_transport):
        # Structurally valid feed whose item has a malformed
        # ticker_sentiment structure.
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, _payload([
                _feed_item(ticker_sentiment={"ticker": "AAPL"})]))]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 4
        assert manifest_rows(store_db) == []

    def test_saturated_window_no_coverage(
            self, av_creds, store_db, inject_transport):
        # A window at the provider-max limit: completeness NOT
        # established — fail closed, never verified coverage (§3.8
        # N-1-f: a truncated response must not silently create
        # verified coverage).
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, saturated_body())]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 4
        assert manifest_rows(store_db) == []
        # and no rows were ingested from the saturated window
        assert headline_rows(store_db) == []

    def test_partial_rows_behavior_matches_architecture(
            self, av_creds, store_db, inject_transport):
        """Persistence ordering (existing architecture, documented):
        the adapter accumulates a ticker's rows across ALL windows and
        returns them only after the complete sweep, so a mid-sweep
        window failure persists NO rows at all for that ticker —
        stronger than the permissive partial-persist case (all-or-
        nothing per ticker/span). No rollback semantics are invented:
        across tickers, an earlier ticker's COMPLETED sweep (rows +
        its own verified span) does persist before a later ticker
        fails — that ticker's span was completely swept, so §11.6 is
        not weakened. Verified here end-to-end."""
        # Window 1 (2018 partial year) succeeds, window 2 (2019 partial
        # year) fails: NO rows persist for AAPL and NO coverage is
        # written.
        pages = [(200, good_body()),
                 (403, {"e": "forbidden"})]
        inject_transport(fake_transport({"alphavantage.co": pages}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", "2018-12-01", "--end", "2019-02-28"])
        assert rc == 4
        assert headline_rows(store_db) == []
        assert manifest_rows(store_db) == []
        # Rerun with both windows repaired is deterministic: rows land
        # exactly once, coverage is attested only now.
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body()),
                                 (200, good_body())]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", "2018-12-01", "--end", "2019-02-28"])
        assert rc == 0
        rows = headline_rows(store_db, "AAPL")
        assert len(rows) == 2
        assert len(rows) == len({tuple(r) for r in rows})
        manifests = manifest_rows(store_db)
        assert len(manifests) == 1 and manifests[0][4] == 1

    def test_multi_ticker_mid_job_failure_no_coverage_for_failed_ticker(
            self, av_creds, store_db, inject_transport):
        # AAPL's full sweep (both its annual windows) succeeds; MSFT's
        # sweep fails on its first window: the failed ticker gets NO
        # rows and NO verified coverage, while the completed ticker
        # keeps its verified span (its span was completely swept —
        # per-ticker attestation, same repo precedent as the
        # Finnhub/EODHD jobs).
        pages = [(200, good_body("AAPL")),
                 (200, good_body("AAPL")),
                 (403, {"e": "forbidden"})]
        inject_transport(fake_transport({"alphavantage.co": pages}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL,MSFT",
                      "--start", "2018-12-01", "--end", "2019-02-28"])
        assert rc == 4
        assert headline_rows(store_db, "MSFT") == []
        rows = manifest_rows(store_db)
        assert [r[1] for r in rows] == ["AAPL"]
        assert rows[0][4] == 1

    def test_rerun_is_idempotent_no_duplicate_rows(
            self, av_creds, store_db, inject_transport):
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        rows = headline_rows(store_db, "AAPL")
        assert len(rows) == 2
        assert len(rows) == len({tuple(r) for r in rows})
        manifests = manifest_rows(store_db)
        assert len(manifests) == 1  # identical manifest rewrite is a no-op


# ---------------------------------------------------------------------------
# 25: secret handling
# ---------------------------------------------------------------------------

class TestSecrets:
    def test_api_key_never_in_output_artifacts_or_fetch_log(
            self, av_creds, store_db, hermes_home, inject_transport, capsys):
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        captured = capsys.readouterr()
        assert SECRET not in captured.out + captured.err
        reports = hermes_home / "backtest" / "reports"
        for artifact in reports.iterdir():
            assert SECRET not in artifact.read_text(), artifact.name
        # persisted fetch metadata (FetchRecord params) excludes it
        log = json.loads(
            (reports / f"fetch_alphavantage_news_{START}_{END}.json")
            .read_text())
        for rec in log:
            assert "apikey" not in rec["params"]
            assert SECRET not in json.dumps(rec)

    def test_api_key_never_in_error_output(
            self, av_creds, store_db, inject_transport, capsys):
        # Non-retryable transport failure whose error body embeds the
        # secret — must be redacted from CLI error output.
        def http_get(url, headers=None, params=None, timeout=30.0):
            return 403, f"forbidden apikey={SECRET}"

        inject_transport(http_get)
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 4
        captured = capsys.readouterr()
        assert SECRET not in captured.out + captured.err
        assert "apikey=<redacted>" in captured.out + captured.err

    def test_api_key_never_in_envelope_error_output(
            self, av_creds, store_db, inject_transport, capsys):
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, {
                "Information": f"quota exceeded for apikey={SECRET}"})]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 4
        captured = capsys.readouterr()
        assert SECRET not in captured.out + captured.err


# ---------------------------------------------------------------------------
# 27-29: isolation + hermeticity
# ---------------------------------------------------------------------------

class TestIsolation:
    def test_no_classification_cache_population(self, av_creds, store_db,
                                                inject_transport):
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        conn = sqlite3.connect(store_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM news_classifications").fetchone()[0]
        finally:
            conn.close()
        assert count == 0

    def test_no_strategy_behavior_invoked(self, av_creds, store_db,
                                          inject_transport, monkeypatch):
        # The fetch job must not touch trading_core strategy surfaces;
        # any call into entry_pipeline would fail this test loudly.
        import trading_core.entry_pipeline as ep
        calls = []

        def _boom(name):
            def f(*a, **kw):
                calls.append(name)
                raise AssertionError(
                    f"strategy surface {name} invoked by fetch job")
            return f

        for name in ("is_news_unverified", "g7_vetoed_at",
                     "catalyst_score_points", "confirmed_bearish_critical_exit"):
            if hasattr(ep, name):
                monkeypatch.setattr(ep, name, _boom(name), raising=True)
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        assert calls == []

    def test_no_network_dependency(self, av_creds, store_db,
                                   inject_transport, monkeypatch):
        # The injected transport is the ONLY transport; assert the real
        # default transport would have been used otherwise by proving
        # the job never constructs one (module never imports httpx at
        # import time and the handler routes through fetch_json only).
        import socket
        monkeypatch.setattr(
            socket, "create_connection",
            lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("network access attempted")))
        monkeypatch.setattr(
            socket, "getaddrinfo",
            lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("DNS resolution attempted")))
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0


# ---------------------------------------------------------------------------
# verified-span skip (PART A): manifest-only skip, zero provider requests
# ---------------------------------------------------------------------------

def _insert_verified_manifest(db_path, ticker, span_start, span_end,
                              manifest_version="alphavantage-news-1",
                              verified=1):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO coverage_manifests (source_kind, ticker, "
            "span_start, span_end, verified, manifest_version, run_id, "
            "config_version, code_commit) "
            "VALUES ('NEWS', ?, ?, ?, ?, ?, 'seed', 'v', 'c')",
            (ticker, span_start, span_end, verified, manifest_version))
        conn.commit()
    finally:
        conn.close()


SPAN_START = "2019-01-01T00:00:00+00:00"
SPAN_END = "2019-01-31T23:59:59+00:00"


class TestVerifiedSpanSkip:
    def test_verified_ticker_skipped_zero_provider_requests(
            self, av_creds, store_db, inject_transport, capsys):
        _insert_verified_manifest(store_db, "AAPL", SPAN_START, SPAN_END)
        calls = []

        def http(*a, **kw):
            calls.append(1)
            return 403, "{}"

        inject_transport(http)
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        assert calls == []            # ZERO provider requests
        out = capsys.readouterr().out
        assert "AAPL: already verified for requested NEWS span, skipped" in out

    def test_narrower_verified_span_does_not_skip(
            self, av_creds, store_db, inject_transport, capsys):
        # Verified coverage of only a SUBSPAN must not skip the wider
        # requested span — the row must cover the full bounds.
        _insert_verified_manifest(store_db, "AAPL",
                                  "2019-01-01T00:00:00+00:00",
                                  "2019-01-15T23:59:59+00:00")
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        assert "already verified" not in capsys.readouterr().out
        assert len(manifest_rows(store_db)) == 2  # narrow seed + full span

    def test_wider_verified_span_does_skip(
            self, av_creds, store_db, inject_transport, capsys):
        # A superset row legitimately covers the requested span.
        _insert_verified_manifest(store_db, "AAPL",
                                  "2018-12-01T00:00:00+00:00",
                                  "2019-02-28T23:59:59+00:00")
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        assert "already verified" in capsys.readouterr().out

    def test_wrong_manifest_version_does_not_skip(
            self, av_creds, store_db, inject_transport, capsys):
        _insert_verified_manifest(store_db, "AAPL", SPAN_START, SPAN_END,
                                  manifest_version="alphavantage-news-2")
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        assert "already verified" not in capsys.readouterr().out

    def test_unverified_manifest_does_not_skip(
            self, av_creds, store_db, inject_transport, capsys):
        # A covering row that is NOT verified grants no skip. Seeded
        # under a distinct manifest version because the strict
        # write_manifest semantics (unchanged) refuse rewriting an
        # identical key from verified=False to True — which is itself
        # the fail-closed guarantee that unverified attestation never
        # silently becomes verified.
        _insert_verified_manifest(store_db, "AAPL", SPAN_START, SPAN_END,
                                  manifest_version="alphavantage-news-0",
                                  verified=0)
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END,
                      "--manifest-version", "alphavantage-news-1"])
        assert rc == 0
        assert "already verified" not in capsys.readouterr().out

    def test_non_news_source_kind_does_not_skip(
            self, av_creds, store_db, inject_transport, capsys):
        conn = sqlite3.connect(store_db)
        try:
            conn.execute(
                "INSERT INTO coverage_manifests (source_kind, ticker, "
                "span_start, span_end, verified, manifest_version, run_id, "
                "config_version, code_commit) "
                "VALUES ('EARNINGS', 'AAPL', ?, ?, 1, "
                "'alphavantage-news-1', 'seed', 'v', 'c')",
                (SPAN_START, SPAN_END))
            conn.commit()
        finally:
            conn.close()
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        assert "already verified" not in capsys.readouterr().out

    def test_checkpoint_only_state_does_not_skip(
            self, av_creds, store_db, inject_transport, tmp_path, capsys):
        # Checkpoints exist but NO verified manifest row: must NOT skip.
        ckpt = tmp_path / "av-resume-skip-test"
        ckpt.mkdir()
        # Seed a syntactically valid completed-leaf checkpoint over the
        # full requested window (checkpoint content is validated by the
        # loader; here it only needs to EXIST to prove checkpoints are
        # never sufficient for the skip).
        from backtest.data.fetch_alphavantage import (
            _checkpoint_path, _save_checkpoint, _window_to_datetimes,
        )
        a = dt.datetime(2019, 1, 1, 0, 0, tzinfo=dt.timezone.utc)
        b = dt.datetime(2019, 1, 31, 23, 59, tzinfo=dt.timezone.utc)
        _save_checkpoint(_checkpoint_path("AAPL", a, b, ckpt),
                         ticker="AAPL", a=a, b=b, rows=[], drops=0)
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END,
                      "--checkpoint-dir", str(ckpt)])
        assert rc == 0
        assert "already verified" not in capsys.readouterr().out
        assert len(manifest_rows(store_db)) == 1

    def test_headlines_without_verified_manifest_do_not_skip(
            self, av_creds, store_db, inject_transport, capsys):
        # news_headlines presence (from another publication unit) is
        # NEVER coverage evidence — the manifest row is the only gate.
        conn = sqlite3.connect(store_db)
        try:
            conn.execute(
                "INSERT INTO news_headlines (headline_hash, source, ticker,"
                " published_at, headline_text_normalized, fetched_at,"
                " run_id, config_version, code_commit)"
                " VALUES ('h1', 'S', 'AAPL', '2019-01-02T15:30:00+00:00',"
                " 'text', '2026-01-01T00:00:00+00:00', 'seed', 'v', 'c')")
            conn.commit()
        finally:
            conn.close()
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        assert "already verified" not in capsys.readouterr().out

    def test_multi_ticker_skips_completed_and_processes_next(
            self, av_creds, store_db, inject_transport, capsys):
        # AAPL fully verified; MSFT not — MSFT must be processed
        # immediately (skip is per-ticker, not per-batch).
        _insert_verified_manifest(store_db, "AAPL", SPAN_START, SPAN_END)
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body(
                "MSFT", "Microsoft rises on synthetic cloud numbers"))]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL,MSFT",
                      "--start", START, "--end", END])
        assert rc == 0
        out = capsys.readouterr().out
        assert "AAPL: already verified for requested NEWS span, skipped" in out
        assert "MSFT: 2 headlines ingested" in out
        assert len(headline_rows(store_db, "MSFT")) == 2
        assert len(headline_rows(store_db, "AAPL")) == 0  # no replay publish
        versions = manifest_rows(store_db)
        assert len(versions) == 2  # AAPL seed + MSFT new

    def test_verified_ticker_replay_path_avoided_idempotent_repub(
            self, av_creds, store_db, inject_transport, tmp_path, capsys):
        # A full successful run followed by a rerun: the second run must
        # take the skip path (no HTTP, no replay, no publication), and
        # DB state must be unchanged.
        ckpt = tmp_path / "av-resume-idem-test"
        inject_transport(fake_transport(
            {"alphavantage.co": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END,
                      "--checkpoint-dir", str(ckpt)])
        assert rc == 0
        headlines_before = headline_rows(store_db, "AAPL")
        manifests_before = manifest_rows(store_db)
        calls = []

        def http(*a, **kw):
            calls.append(1)
            return 403, "{}"

        inject_transport(http)
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", START, "--end", END,
                      "--checkpoint-dir", str(ckpt)])
        assert rc == 0
        assert calls == []  # skip happened before any provider/checkpoint work
        assert headline_rows(store_db, "AAPL") == headlines_before
        assert manifest_rows(store_db) == manifests_before
        assert "already verified" in capsys.readouterr().out

    def test_skip_path_executes_without_nameerror(
            self, av_creds, store_db, inject_transport, capsys):
        # Regression: production hit
        # NameError: name '_date_to_utc_end_of_day' is not defined when the
        # verified-span skip ran. The date-bound helpers must resolve from
        # the consolidated import in cmd_fetch_alphavantage_news — the skip
        # path must execute end-to-end without NameError (any second
        # un-imported reference would fail right here).
        _insert_verified_manifest(store_db, "AAPL",
                                  "2019-01-01T00:00:00+00:00",
                                  "2025-12-31T23:59:59+00:00")
        inject_transport(fake_transport(
            {"alphavantage.co": [(403, "{}")]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", "2019-01-01", "--end", "2025-12-31"])
        assert rc == 0
        assert "already verified" in capsys.readouterr().out

    def test_date_bound_helpers_match_existing_utc_convention(self):
        # The skip must use the EXISTING canonical date-bound convention —
        # inclusive YYYY-MM-DD end → 23:59:59 UTC, start → midnight UTC —
        # identical to the Finnhub/EODHD NEWS bound convention and to the
        # bounds news_manifest_rows writes (test_requested_span_bounds).
        from backtest.data.fetch_alphavantage import (
            _date_to_utc_midnight, _date_to_utc_end_of_day)
        assert _date_to_utc_midnight(dt.date(2019, 1, 1)) == \
            "2019-01-01T00:00:00+00:00"
        assert _date_to_utc_end_of_day(dt.date(2025, 12, 31)) == \
            "2025-12-31T23:59:59+00:00"

    def test_skip_uses_canonical_bounds_for_full_span_resolution(
            self, av_creds, store_db, inject_transport, capsys):
        # End-to-end bound resolution: a verified manifest row seeded at
        # the EXACT canonical bounds produced from start=2019-01-01 /
        # end=2025-12-31 must skip — proving the skip resolves
        # midnight-UTC start and inclusive 23:59:59-UTC end via the
        # canonical helpers (not some ad-hoc bound convention).
        _insert_verified_manifest(store_db, "AAPL",
                                  "2019-01-01T00:00:00+00:00",
                                  "2025-12-31T23:59:59+00:00")
        inject_transport(fake_transport(
            {"alphavantage.co": [(403, "{}")]}))
        rc = run_job(["backtest", "fetch-alphavantage-news",
                      "--tickers", "AAPL",
                      "--start", "2019-01-01", "--end", "2025-12-31"])
        assert rc == 0
        assert "already verified" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Finnhub news job remains unchanged
# ---------------------------------------------------------------------------

class TestFinnhubNewsUnchanged:
    def test_finnhub_job_still_writes_reports_artifact(
            self, finnhub_creds, store_db, hermes_home):
        from backtest.data import fetch_finnhub
        http = fake_transport({"company-news": [(200, [{
            "category": "company news", "datetime": 1546477200,
            "headline": "Apple announces synthetic Finnhub headline",
            "id": 1, "image": "", "related": "AAPL",
            "source": "Synthetic Finnhub Wire", "summary": "",
            "url": "https://example.com/finnhub"}])]})
        original = fetch_finnhub.fetch_json

        def patched(url, **kwargs):
            kwargs["http_get"] = http
            return original(url, **kwargs)

        fetch_finnhub.fetch_json = patched
        try:
            rc = run_job(["backtest", "fetch-finnhub-news",
                          "--tickers", "AAPL",
                          "--start", START, "--end", END])
        finally:
            fetch_finnhub.fetch_json = original
        assert rc == 0
        # Unchanged location: the Finnhub fetch log still lands in the
        # generic reports dir; the manifest keeps its own version.
        assert (hermes_home / "backtest" / "reports" /
                f"fetch_finnhub_news_{START}_{END}.json").exists()
        rows = manifest_rows(store_db)
        assert len(rows) == 1 and rows[0][0] == "NEWS"
        assert rows[0][5] == "finnhub-news-1"

    def test_finnhub_parser_defaults_unchanged(self):
        args = parse_backtest_args(
            ["backtest", "fetch-finnhub-news", "--tickers", "AAPL",
             "--start", START, "--end", END])
        assert args.backtest_handler == "fetch-finnhub-news"
        assert args.manifest_version == "finnhub-news-1"
        assert args.run_id == "fetch-finnhub-news"
