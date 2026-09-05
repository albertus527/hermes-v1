"""R2.8 §3.7 — `hermes backtest fetch-eodhd-earnings` CLI job tests.

Hermetic: every fetch runs through an injected fake transport (no
network, synthetic data only). Mirrors the Finnhub-earnings job shape:

- parser: `fetch-eodhd-earnings` subcommand + defaults;
- dispatch: `cmd_backtest` routes the handler to the EODHD handler;
- retained artifacts land under the provider-managed
  ~/.hermes/data/r28/eodhd/normalized/ root (NOT _save_report's
  backtest/reports/) via atomic_write_text;
- artifact rows are the canonical {ticker, event_date, timing} G6
  shape; timing vocabulary survives the CLI job end-to-end;
- coverage rides the existing shared coverage_manifests mechanism with
  source_kind='EARNINGS' and a distinct manifest_version;
- failures (transport, unknown non-null timing, malformed payload)
  write NO verified coverage and no event artifact;
- a successful zero-row sweep IS verified coverage (§3.7/§11.6);
- the API token never reaches any artifact or printed output;
- the Finnhub earnings job is unchanged.
"""

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path

import pytest

from backtest.data import fetch_eodhd
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


SECRET = "SECRET-EODHD-TOKEN-abc123"


@pytest.fixture
def eodhd_creds(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.get_env_value",
                        lambda key: {"EODHD_API_KEY": SECRET}.get(key))


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
    """Route fetch_eodhd's fetch_json calls through a fake transport so
    the REAL sweep + normalization + validation code executes."""
    real_fetch_json = fetch_eodhd.fetch_json

    def _install(http):
        def patched(url, **kwargs):
            kwargs["http_get"] = http
            return real_fetch_json(url, **kwargs)
        monkeypatch.setattr(fetch_eodhd, "fetch_json", patched)
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


START, END = "2026-01-01", "2026-01-31"


def good_body():
    """Synthetic EODHD wrapper payload covering all three timing rows."""
    return {"earnings": [
        {"code": "AAPL.US", "report_date": "2026-01-06",
         "before_after_market": "BeforeMarket", "date": "2025-12-31"},
        {"code": "AAPL.US", "report_date": "2026-01-27",
         "before_after_market": "AfterMarket", "date": "2025-12-31"},
        {"code": "AAPL.US", "report_date": "2026-01-15",
         "before_after_market": None, "date": "2025-12-31"},
    ]}


# ---------------------------------------------------------------------------
# parser + dispatch
# ---------------------------------------------------------------------------

class TestParser:
    def test_fetch_eodhd_earnings_subcommand_parses(self):
        args = parse_backtest_args(
            ["backtest", "fetch-eodhd-earnings", "--tickers", "AAPL",
             "--start", START, "--end", END])
        assert args.backtest_handler == "fetch-eodhd-earnings"
        assert args.tickers == "AAPL"
        assert args.start == START
        assert args.end == END

    def test_defaults_match_finnhub_conventions(self):
        args = parse_backtest_args(
            ["backtest", "fetch-eodhd-earnings", "--tickers", "AAPL",
             "--start", START, "--end", END])
        assert args.manifest_version == "eodhd-earnings-1"
        assert args.run_id == "fetch-eodhd-earnings"

    def test_required_arguments_enforced(self):
        with pytest.raises(SystemExit):
            parse_backtest_args(
                ["backtest", "fetch-eodhd-earnings", "--tickers", "AAPL"])


class TestDispatch:
    def test_cmd_backtest_routes_to_eodhd_handler(self, monkeypatch):
        from backtest import cli as backtest_cli
        from hermes_cli.main import cmd_backtest
        seen = {}

        def spy(args):
            seen["called"] = True
            return 42

        monkeypatch.setattr(backtest_cli, "cmd_fetch_eodhd_earnings", spy)
        args = parse_backtest_args(
            ["backtest", "fetch-eodhd-earnings", "--tickers", "AAPL",
             "--start", START, "--end", END])
        assert cmd_backtest(args) == 42
        assert seen.get("called") is True

    def test_missing_credential_exits_3(self, store_db, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.get_env_value",
                            lambda key: None)
        rc = run_job(["backtest", "fetch-eodhd-earnings", "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 3

    def test_missing_store_exits_2(self, eodhd_creds):
        rc = run_job(["backtest", "fetch-eodhd-earnings", "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 2


# ---------------------------------------------------------------------------
# successful job: artifact + coverage + timing survival
# ---------------------------------------------------------------------------

class TestSuccessfulJob:
    def test_artifact_written_under_provider_normalized_root(
            self, eodhd_creds, store_db, hermes_home, inject_transport):
        inject_transport(fake_transport(
            {"calendar/earnings": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-eodhd-earnings", "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        root = hermes_home / "data" / "r28" / "eodhd"
        events_path = root / "normalized" / \
            f"earnings_events_{START}_{END}.json"
        assert events_path.exists()
        # The retained artifact is provider-scoped: inside the provider
        # retention root, NOT under the generic reports directory.
        assert root in events_path.parents
        assert (hermes_home / "backtest" / "reports" /
                f"earnings_events_{START}_{END}.json").exists() is False
        # Fetch log artifact stays under the provider root too.
        assert (root / "normalized" /
                f"fetch_eodhd_earnings_{START}_{END}.json").exists()

    def test_artifact_rows_are_canonical_event_shape(
            self, eodhd_creds, store_db, hermes_home, inject_transport):
        inject_transport(fake_transport(
            {"calendar/earnings": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-eodhd-earnings", "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        events = json.loads(
            (hermes_home / "data" / "r28" / "eodhd" / "normalized" /
             f"earnings_events_{START}_{END}.json").read_text())
        assert len(events) == 3
        for ev in events:
            assert set(ev.keys()) == {"ticker", "event_date", "timing"}
            assert ev["ticker"] == "AAPL"

    def test_timing_vocabulary_survives_the_cli_job(
            self, eodhd_creds, store_db, hermes_home, inject_transport):
        inject_transport(fake_transport(
            {"calendar/earnings": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-eodhd-earnings", "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        events = json.loads(
            (hermes_home / "data" / "r28" / "eodhd" / "normalized" /
             f"earnings_events_{START}_{END}.json").read_text())
        timings = {e["event_date"]: e["timing"] for e in events}
        assert timings["2026-01-06"] == "before-market-open"
        assert timings["2026-01-27"] == "after-market-close"
        assert timings["2026-01-15"] == "unspecified"

    def test_artifact_events_feed_gate_g6_directly(
            self, eodhd_creds, store_db, hermes_home, inject_transport):
        """The persisted rows must construct EarningsEvent and evaluate
        through the unchanged G6 machinery with no translation."""
        from trading_core.gates import (
            EarningsEvent, map_earnings_event_session,
        )
        inject_transport(fake_transport(
            {"calendar/earnings": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-eodhd-earnings", "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        events = json.loads(
            (hermes_home / "data" / "r28" / "eodhd" / "normalized" /
             f"earnings_events_{START}_{END}.json").read_text())
        by_date = {e["event_date"]: e for e in events}

        def _next_td(d):
            nxt = d + dt.timedelta(days=1)
            while nxt.weekday() >= 5:
                nxt += dt.timedelta(days=1)
            return nxt

        is_td = lambda d: d.weekday() < 5  # noqa: E731
        # 2026-01-06 is a Tuesday; BeforeMarket maps to the same session.
        ev_bmo = EarningsEvent(
            event_date=dt.date.fromisoformat(by_date["2026-01-06"]["event_date"]),
            timing=by_date["2026-01-06"]["timing"])
        assert map_earnings_event_session(
            ev_bmo, is_trading_day=is_td, next_trading_day=_next_td
        ) == dt.date(2026, 1, 6)
        # 2026-01-27 is a Wednesday; AfterMarket maps to the NEXT session.
        ev_amc = EarningsEvent(
            event_date=dt.date.fromisoformat(by_date["2026-01-27"]["event_date"]),
            timing=by_date["2026-01-27"]["timing"])
        assert map_earnings_event_session(
            ev_amc, is_trading_day=is_td, next_trading_day=_next_td
        ) == dt.date(2026, 1, 28)

    def test_no_token_in_artifacts_or_output(
            self, eodhd_creds, store_db, hermes_home, inject_transport,
            capsys):
        inject_transport(fake_transport(
            {"calendar/earnings": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-eodhd-earnings", "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        captured = capsys.readouterr()
        assert SECRET not in captured.out + captured.err
        normalized_dir = hermes_home / "data" / "r28" / "eodhd" / "normalized"
        for artifact in normalized_dir.iterdir():
            assert SECRET not in artifact.read_text(), artifact.name

    def test_success_writes_existing_earnings_manifest_semantics(
            self, eodhd_creds, store_db, inject_transport):
        inject_transport(fake_transport(
            {"calendar/earnings": [(200, good_body())]}))
        rc = run_job(["backtest", "fetch-eodhd-earnings", "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        rows = manifest_rows(store_db)
        assert len(rows) == 1
        source_kind, ticker, span_start, span_end, verified, version = rows[0]
        assert source_kind == "EARNINGS"
        assert ticker == "AAPL"
        assert span_start == "2026-01-01T00:00:00+00:00"
        assert span_end == "2026-01-31T23:59:59+00:00"
        assert verified == 1
        assert version == "eodhd-earnings-1"

    def test_successful_zero_row_sweep_is_verified_coverage(
            self, eodhd_creds, store_db, hermes_home, inject_transport):
        inject_transport(fake_transport(
            {"calendar/earnings": [(200, {"earnings": []})]}))
        rc = run_job(["backtest", "fetch-eodhd-earnings", "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 0
        rows = manifest_rows(store_db)
        assert len(rows) == 1 and rows[0][4] == 1  # verified EARNINGS span
        events = json.loads(
            (hermes_home / "data" / "r28" / "eodhd" / "normalized" /
             f"earnings_events_{START}_{END}.json").read_text())
        assert events == []  # verified-zero: nothing invented


# ---------------------------------------------------------------------------
# failures write no coverage
# ---------------------------------------------------------------------------

class TestFailClosedCoverage:
    def _assert_no_verified_coverage(self, store_db, hermes_home):
        assert manifest_rows(store_db) == []
        assert not (hermes_home / "data" / "r28" / "eodhd" / "normalized" /
                    f"earnings_events_{START}_{END}.json").exists()

    def test_provider_failure_exits_4_no_coverage(
            self, eodhd_creds, store_db, hermes_home, inject_transport,
            capsys):
        # Non-retryable status (no retry burn, no real backoff sleeps).
        inject_transport(fake_transport(
            {"calendar/earnings": [(403, {"e": "forbidden"})]}))
        rc = run_job(["backtest", "fetch-eodhd-earnings", "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 4
        captured = capsys.readouterr()
        assert SECRET not in captured.out + captured.err  # token redacted
        self._assert_no_verified_coverage(store_db, hermes_home)

    def test_unknown_non_null_timing_exits_4_no_coverage(
            self, eodhd_creds, store_db, hermes_home, inject_transport):
        body = {"earnings": [{"code": "AAPL.US", "report_date": "2026-01-20",
                              "before_after_market": "DuringMarket"}]}
        inject_transport(fake_transport({"calendar/earnings": [(200, body)]}))
        rc = run_job(["backtest", "fetch-eodhd-earnings", "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 4
        self._assert_no_verified_coverage(store_db, hermes_home)

    def test_malformed_payload_exits_4_no_coverage(
            self, eodhd_creds, store_db, hermes_home, inject_transport):
        # 200 with a non-wrapper shape: no "earnings" key at all.
        inject_transport(fake_transport(
            {"calendar/earnings": [(200, {"unexpected": []})]}))
        rc = run_job(["backtest", "fetch-eodhd-earnings", "--tickers", "AAPL",
                      "--start", START, "--end", END])
        assert rc == 4
        self._assert_no_verified_coverage(store_db, hermes_home)

    def test_multi_ticker_failure_writes_no_partial_coverage(
            self, eodhd_creds, store_db, hermes_home, inject_transport):
        # First ticker succeeds, second fails: the job halts before
        # attesting anything for the failed ticker AND the failure
        # surfaces as exit 4 (no catch-and-continue partial coverage).
        pages = {"calendar/earnings": [
            (200, good_body()),
            (200, {"earnings": [{"code": "MSFT.US",
                                 "report_date": "2026-01-20",
                                 "before_after_market": "Whenever"}]}),
        ]}
        inject_transport(fake_transport(pages))
        rc = run_job(["backtest", "fetch-eodhd-earnings",
                      "--tickers", "AAPL,MSFT",
                      "--start", START, "--end", END])
        assert rc == 4
        rows = manifest_rows(store_db)
        assert [r[1] for r in rows] == ["AAPL"]  # only the completed sweep


# ---------------------------------------------------------------------------
# Finnhub earnings job remains unchanged
# ---------------------------------------------------------------------------

class TestFinnhubEarningsUnchanged:
    def test_finnhub_job_still_writes_reports_artifact(
            self, finnhub_creds, store_db, hermes_home):
        from backtest.data import fetch_finnhub
        http = fake_transport({"calendar/earnings": [(200, {
            "earningsCalendar": [
                {"date": "2026-01-06", "hour": "bmo", "symbol": "AAPL"}]})]})
        original = fetch_finnhub.fetch_json

        def patched(url, **kwargs):
            kwargs["http_get"] = http
            return original(url, **kwargs)

        fetch_finnhub.fetch_json = patched
        try:
            rc = run_job(["backtest", "fetch-finnhub-earnings",
                          "--tickers", "AAPL",
                          "--start", START, "--end", END])
        finally:
            fetch_finnhub.fetch_json = original
        assert rc == 0
        # Unchanged location: the Finnhhub event artifact still lands in
        # the generic reports dir, NOT the EODHD provider root.
        assert (hermes_home / "backtest" / "reports" /
                f"earnings_events_{START}_{END}.json").exists()
        assert not (hermes_home / "data" / "r28" / "eodhd" / "normalized" /
                    f"earnings_events_{START}_{END}.json").exists()
        rows = manifest_rows(store_db)
        assert len(rows) == 1 and rows[0][0] == "EARNINGS"
        assert rows[0][5] == "finnhub-earnings-1"

    def test_finnhub_parser_defaults_unchanged(self):
        args = parse_backtest_args(
            ["backtest", "fetch-finnhub-earnings", "--tickers", "AAPL",
             "--start", START, "--end", END])
        assert args.backtest_handler == "fetch-finnhub-earnings"
        assert args.manifest_version == "finnhub-earnings-1"
        assert args.run_id == "fetch-finnhub-earnings"
