"""Phase-0 data-ingestion job tests (§20 Phase 0) — hermetic.

No network: every fetch is exercised through an injected fake transport.
Covers:
- deterministic retry/backoff (retryable statuses retried, others fail
  closed immediately, transport errors retried, exhaustion fails closed);
- idempotent upserts (identical re-ingest = no-op; differing payload =
  deterministic error, never a silent overwrite);
- bars: SIP feed required, row shaping, pagination via next_page_token;
- corporate actions: dividend + split mapping, ratio = new_rate/old_rate,
  verified-zero manifest attestation for an event-free span;
- Finnhub news: FP-4 normalization/hash, unix→ISO published_at, window
  sweep, verified NEWS manifest;
- Finnhub earnings: bmo/amc/other hour mapping to G6 vocabulary;
- FRED VIXCLS: "." missing values skipped, idempotent upsert;
- coverage manifests: fail-closed semantics (no manifest from an
  incomplete sweep), verified re-write of identical row is a no-op;
- credentials absent -> fail closed (monkeypatched get_env_value).
"""

import datetime as dt
import json

import pytest

from backtest.data import fetch_alpaca, fetch_finnhub, fetch_fred
from backtest.data.ingest_core import (
    FetchLog,
    IngestionError,
    IngestStore,
    fetch_json,
)
from backtest.db.schema import open_db


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    conn = open_db(tmp_path / "bt.sqlite3")
    try:
        yield IngestStore(conn, run_id="test-run", config_version=1,
                          code_commit="abc")
    finally:
        conn.close()


def fake_transport(pages_by_url, calls=None):
    """Build an http_get fake serving canned (status, json-body) pages in
    sequence per URL (matched by substring). When a URL's page list is
    exhausted, the LAST page is re-served (repeating-response semantics
    for multi-window sweeps)."""
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


@pytest.fixture
def alpaca_creds(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.get_env_value",
                        lambda key: {"ALPACA_API_KEY": "k",
                                     "ALPACA_API_SECRET": "s"}.get(key))


@pytest.fixture
def finnhub_creds(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.get_env_value",
                        lambda key: {"FINNHUB_API_KEY": "t"}.get(key))


@pytest.fixture
def fred_creds(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.get_env_value",
                        lambda key: {"FRED_API_KEY": "f"}.get(key))


# ---------------------------------------------------------------------------
# retry / transport semantics
# ---------------------------------------------------------------------------

class TestFetchJsonRetries:
    def test_200_returns_parsed_json(self):
        http = fake_transport({"example.com": [(200, {"a": 1})]})
        assert fetch_json("https://example.com/x", http_get=http) == {"a": 1}

    def test_retryable_status_then_success(self):
        sleeps = []
        http = fake_transport({"example.com": [
            (429, {"e": "slow down"}), (200, {"ok": True})]})
        out = fetch_json("https://example.com/x", http_get=http,
                         sleep=sleeps.append)
        assert out == {"ok": True}
        assert len(sleeps) == 1 and sleeps[0] > 0

    def test_non_retryable_status_fails_closed_immediately(self):
        calls = []
        http = fake_transport({"example.com": [(403, {"e": "forbidden"})]},
                              calls)
        with pytest.raises(IngestionError, match="403"):
            fetch_json("https://example.com/x", http_get=http,
                       sleep=lambda _s: None)
        assert len(calls) == 1  # no retry burn on a hard denial

    def test_transport_error_retried_then_exhaustion(self):
        attempts = []

        def http_get(url, **_kw):
            attempts.append(url)
            raise ConnectionError("boom")

        with pytest.raises(IngestionError, match="exhausted"):
            fetch_json("https://example.com/x", http_get=http_get,
                       max_attempts=3, sleep=lambda _s: None)
        assert len(attempts) == 3

    def test_200_with_invalid_json_fails_closed(self):
        http = fake_transport({"example.com": []})

        def raw(url, headers=None, params=None, timeout=30.0):
            return 200, "not json"

        with pytest.raises(IngestionError, match="not valid JSON"):
            fetch_json("https://example.com/x", http_get=raw)

    def test_backoff_is_deterministic_exponential(self):
        sleeps = []

        def http_get(url, **_kw):
            return 429, "{}"

        with pytest.raises(IngestionError):
            fetch_json("https://example.com/x", http_get=http_get,
                       max_attempts=4, backoff_base=1.0,
                       sleep=sleeps.append)
        assert sleeps == [1.0, 2.0, 4.0]  # fixed schedule, no jitter


# ---------------------------------------------------------------------------
# IngestStore idempotency
# ---------------------------------------------------------------------------

class TestIngestStoreIdempotency:
    def test_bar_reingest_is_noop(self, store):
        row = {"ticker": "SPY", "ts_label_start": "2026-01-05T14:30:00Z",
               "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "100",
               "feed": "sip", "timeframe": "1Min", "adjustment": "raw"}
        assert store.upsert_bars([row]) == 1
        assert store.upsert_bars([dict(row)]) == 0
        assert store.bar_count("SPY", "1Min", "raw") == 1

    def test_bar_conflicting_payload_fails_closed(self, store):
        row = {"ticker": "SPY", "ts_label_start": "2026-01-05T14:30:00Z",
               "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "100",
               "feed": "sip", "timeframe": "1Min", "adjustment": "raw"}
        store.upsert_bars([row])
        conflicting = dict(row, c="9")
        with pytest.raises(IngestionError, match="differing"):
            store.upsert_bars([conflicting])

    def test_headline_reingest_noop_and_conflict(self, store):
        row = {"headline_hash": "h" * 64, "source": "finnhub",
               "ticker": "AAPL", "published_at": "2026-01-05T14:30:00+00:00",
               "headline_text_normalized": "aapl beats",
               "fetched_at": "2026-08-29T00:00:00+00:00"}
        assert store.upsert_headlines([row]) == 1
        assert store.upsert_headlines([dict(row)]) == 0
        with pytest.raises(IngestionError):
            store.upsert_headlines([dict(row, published_at=None)])

    def test_corp_action_reingest_noop(self, store):
        row = {"ticker": "AAPL", "event_type": "SPLIT",
               "ex_date": "2020-08-31", "split_ratio": "4",
               "cash_amount_per_share": None, "record_date": None,
               "pay_date": None, "corp_actions_version": "v1"}
        assert store.upsert_corp_actions([row]) == 1
        assert store.upsert_corp_actions([dict(row)]) == 0

    def test_manifest_identical_rewrite_noop_conflict_fails(self, store):
        row = {"source_kind": "NEWS", "ticker": "AAPL",
               "span_start": "2024-01-01", "span_end": "2024-12-31",
               "verified": True, "manifest_version": "m1"}
        assert store.write_manifest([row]) == 1
        assert store.write_manifest([dict(row)]) == 0
        with pytest.raises(IngestionError, match="refusing rewrite"):
            store.write_manifest([dict(row, verified=False)])

    def test_vix_upsert_idempotent_and_conflict(self, store):
        assert store.upsert_vix([{"observation_date": "2026-01-05",
                                  "value": "18.52"}]) == 1
        assert store.upsert_vix([{"observation_date": "2026-01-05",
                                  "value": "18.52"}]) == 0
        with pytest.raises(IngestionError):
            store.upsert_vix([{"observation_date": "2026-01-05",
                               "value": "19.00"}])


# ---------------------------------------------------------------------------
# Alpaca bars
# ---------------------------------------------------------------------------

class TestAlpacaBars:
    def test_bars_rows_and_pagination(self, alpaca_creds):
        pages = [
            (200, {"bars": [
                {"t": "2026-01-05T14:30:00Z", "o": 1.0, "h": 2.0,
                 "l": 0.5, "c": 1.5, "v": 100}],
                "next_page_token": "p2"}),
            (200, {"bars": [
                {"t": "2026-01-05T14:31:00Z", "o": 1.5, "h": 2.5,
                 "l": 1.0, "c": 2.0, "v": 200}], "next_page_token": None}),
        ]
        http = fake_transport({"/v2/stocks/SPY/bars": pages})
        log = FetchLog()
        rows = fetch_alpaca.fetch_bars(
            ticker="SPY", timeframe="1Min", adjustment="raw",
            start=dt.date(2026, 1, 5), end=dt.date(2026, 1, 5),
            http_get=http, fetch_log=log)
        assert len(rows) == 2
        assert rows[0]["ts_label_start"] == "2026-01-05T14:30:00Z"
        assert rows[0]["feed"] == "sip"
        assert rows[0]["adjustment"] == "raw"
        assert log.records[0].pages == 2

    def test_non_sip_feed_rejected(self, alpaca_creds):
        with pytest.raises(IngestionError, match="SIP"):
            fetch_alpaca.fetch_bars(
                ticker="SPY", timeframe="1Min", adjustment="raw",
                start=dt.date(2026, 1, 5), end=dt.date(2026, 1, 5),
                feed="iex")

    def test_missing_credentials_fail_closed(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.get_env_value",
                            lambda key: None)
        with pytest.raises(fetch_alpaca.CredentialsMissing):
            fetch_alpaca.fetch_bars(
                ticker="SPY", timeframe="1Min", adjustment="raw",
                start=dt.date(2026, 1, 5), end=dt.date(2026, 1, 5))


# ---------------------------------------------------------------------------
# Alpaca corporate actions
# ---------------------------------------------------------------------------

class TestAlpacaCorpActions:
    def _sweep(self, alpaca_creds, body):
        http = fake_transport({"/v1/corporate-actions": [(200, body)]})
        return fetch_alpaca.fetch_corporate_actions(
            ticker="AAPL", start=dt.date(2020, 1, 1),
            end=dt.date(2020, 12, 31), corp_actions_version="v1",
            http_get=http)

    def test_dividend_and_split_mapping(self, alpaca_creds):
        result = self._sweep(alpaca_creds, {
            "corporate_actions": {
                "cash_dividends": [{
                    "symbol": "AAPL", "ex_date": "2020-11-06",
                    "record_date": "2020-11-09",
                    "payable_date": "2020-11-12", "rate": 0.205}],
                "forward_splits": [{
                    "symbol": "AAPL", "ex_date": "2020-08-31",
                    "record_date": "2020-08-24",
                    "payable_date": "2020-08-24",
                    "new_rate": 4, "old_rate": 1}],
            }, "next_page_token": None})
        by_type = {e["event_type"]: e for e in result.events}
        assert by_type["CASH_DIVIDEND"]["cash_amount_per_share"] == "0.205"
        assert by_type["CASH_DIVIDEND"]["pay_date"] == "2020-11-12"
        assert by_type["SPLIT"]["split_ratio"] == "4"  # new_rate/old_rate
        assert result.sweep_complete is True

    def test_reverse_split_ratio_below_one(self, alpaca_creds):
        result = self._sweep(alpaca_creds, {
            "corporate_actions": {"reverse_splits": [{
                "symbol": "X", "ex_date": "2020-05-01",
                "new_rate": 1, "old_rate": 8}]},
            "next_page_token": None})
        assert result.events[0]["split_ratio"] == "0.125"

    def test_verified_zero_manifest_for_empty_span(self, alpaca_creds):
        result = self._sweep(alpaca_creds, {
            "corporate_actions": {}, "next_page_token": None})
        assert result.events == []
        rows = fetch_alpaca.corp_actions_manifest_rows(
            result, manifest_version="m1")
        # §3.6 item 4: a verified attestation over an event-free span IS
        # the verified-zero state.
        assert rows == [{"source_kind": "CORP_ACTIONS", "ticker": "AAPL",
                         "span_start": "2020-01-01",
                         "span_end": "2020-12-31", "verified": True,
                         "manifest_version": "m1"}]

    def test_incomplete_sweep_attests_nothing(self):
        incomplete = fetch_alpaca.CorpActionsResult(
            ticker="AAPL", start=dt.date(2020, 1, 1),
            end=dt.date(2020, 12, 31), events=[], sweep_complete=False)
        assert fetch_alpaca.corp_actions_manifest_rows(
            incomplete, manifest_version="m1") == []


# ---------------------------------------------------------------------------
# Finnhub news + earnings
# ---------------------------------------------------------------------------

class TestFinnhubNews:
    def test_headline_rows_use_fp4_hash_and_utc_iso(self, finnhub_creds):
        from trading_core.news_effects import headline_hash
        body = [{
            "category": "company", "datetime": 1767637800,
            "headline": "Apple beats earnings estimates",
            "id": 1, "source": "Reuters",
            "summary": "", "url": "https://x"}]
        http = fake_transport({"company-news": [(200, body)]})
        rows = fetch_finnhub.fetch_news_inventory(
            ticker="AAPL", start=dt.date(2026, 1, 5),
            end=dt.date(2026, 1, 6), http_get=http)
        assert len(rows) == 1
        r = rows[0]
        assert r["headline_hash"] == headline_hash(
            "Apple beats earnings estimates")
        assert r["source"] == "Reuters"
        assert r["ticker"] == "AAPL"
        assert r["published_at"].startswith("2026-01-05T")
        assert "+00:00" in r["published_at"]

    def test_window_sweep_chunks_requests(self, finnhub_creds):
        http = fake_transport({"company-news": [(200, [])]})
        log = FetchLog()
        fetch_finnhub.fetch_news_inventory(
            ticker="AAPL", start=dt.date(2026, 1, 1),
            end=dt.date(2026, 3, 31), http_get=http, fetch_log=log)
        # 90 days / 30-day windows -> 3 fetch records
        assert len(log.records) == 3

    def test_missing_credentials_fail_closed(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.get_env_value",
                            lambda key: None)
        with pytest.raises(fetch_finnhub.CredentialsMissing):
            fetch_finnhub.fetch_news_inventory(
                ticker="AAPL", start=dt.date(2026, 1, 1),
                end=dt.date(2026, 1, 31))

    def test_news_manifest_verified_zero_headline_span(self):
        rows = fetch_finnhub.news_manifest_rows(
            ticker="QQQ", start=dt.date(2026, 1, 1),
            end=dt.date(2026, 1, 31), manifest_version="m1")
        # Timestamp-form span bounds (what HeadlineInventory.covered parses);
        # inclusive of the full end date.
        assert rows == [{
            "source_kind": "NEWS", "ticker": "QQQ",
            "span_start": "2026-01-01T00:00:00+00:00",
            "span_end": "2026-01-31T23:59:59+00:00",
            "verified": True, "manifest_version": "m1"}]


class TestFinnhubEarnings:
    def test_hour_mapping_to_g6_vocabulary(self, finnhub_creds):
        body = {"earningsCalendar": [
            {"date": "2026-02-04", "hour": "bmo", "symbol": "AAPL"},
            {"date": "2026-02-05", "hour": "amc", "symbol": "AAPL"},
            {"date": "2026-02-06", "hour": "dmh", "symbol": "AAPL"},
            {"date": "2026-02-07", "hour": None, "symbol": "AAPL"},
        ]}
        http = fake_transport({"calendar/earnings": [(200, body)]})
        events = fetch_finnhub.fetch_earnings_calendar(
            ticker="AAPL", start=dt.date(2026, 2, 1),
            end=dt.date(2026, 2, 28), http_get=http)
        timings = {e["event_date"]: e["timing"] for e in events}
        assert timings["2026-02-04"] == "before-market-open"
        assert timings["2026-02-05"] == "after-market-close"
        assert timings["2026-02-06"] == "unspecified"
        assert timings["2026-02-07"] == "unspecified"

    def test_earnings_events_feed_gate_g6(self, finnhub_creds):
        """The fetched events must be directly consumable by §7.2 G6."""
        from trading_core.gates import EarningsEvent, map_earnings_event_session
        body = {"earningsCalendar": [
            {"date": "2026-01-06", "hour": "bmo", "symbol": "AAPL"}]}
        http = fake_transport({"calendar/earnings": [(200, body)]})
        events = fetch_finnhub.fetch_earnings_calendar(
            ticker="AAPL", start=dt.date(2026, 1, 1),
            end=dt.date(2026, 1, 31), http_get=http)
        ev = EarningsEvent(
            event_date=dt.date.fromisoformat(events[0]["event_date"]),
            timing=events[0]["timing"])

        def _next_td(d):
            nxt = d + dt.timedelta(days=1)
            while nxt.weekday() >= 5:
                nxt += dt.timedelta(days=1)
            return nxt

        # 2026-01-06 is a Tuesday; bmo maps to the same trading session
        assert map_earnings_event_session(
            ev, is_trading_day=lambda d: d.weekday() < 5,
            next_trading_day=_next_td) == dt.date(2026, 1, 6)


# ---------------------------------------------------------------------------
# FRED VIXCLS
# ---------------------------------------------------------------------------

class TestFredVix:
    def test_missing_values_skipped(self, fred_creds):
        body = {"observations": [
            {"date": "2026-01-02", "value": "18.52"},
            {"date": "2026-01-05", "value": "."},   # FRED missing encoding
            {"date": "2026-01-06", "value": "19.10"},
        ]}
        http = fake_transport({"series/observations": [(200, body)]})
        rows = fetch_fred.fetch_vixcls(start=dt.date(2026, 1, 1),
                                       end=dt.date(2026, 1, 31),
                                       http_get=http)
        assert rows == [{"observation_date": "2026-01-02", "value": "18.52"},
                        {"observation_date": "2026-01-06", "value": "19.10"}]

    def test_missing_credentials_fail_closed(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.get_env_value",
                            lambda key: None)
        with pytest.raises(fetch_fred.CredentialsMissing):
            fetch_fred.fetch_vixcls(start=dt.date(2026, 1, 1),
                                    end=dt.date(2026, 1, 31))

    def test_stored_vix_feeds_regime_gap_rule(self, store, fred_creds):
        """Stored observations must be consumable by §5.2 resolve_vix."""
        from trading_core.regime import resolve_vix
        body = {"observations": [
            {"date": "2026-01-02", "value": "18.52"},
            {"date": "2026-01-06", "value": "19.10"}]}
        http = fake_transport({"series/observations": [(200, body)]})
        rows = fetch_fred.fetch_vixcls(start=dt.date(2026, 1, 1),
                                       end=dt.date(2026, 1, 31),
                                       http_get=http)
        store.upsert_vix(rows)
        vix_by_date = {
            dt.date.fromisoformat(r["observation_date"]): float(r["value"])
            for r in rows}
        # T-1 = 2026-01-05 (Monday) has no observation; the 5-day gap rule
        # falls back to 2026-01-02.
        value, used = resolve_vix(t_minus_1=dt.date(2026, 1, 5),
                                  vix_by_date=vix_by_date)
        assert (value, used) == (18.52, dt.date(2026, 1, 2))


# ---------------------------------------------------------------------------
# End-to-end job semantics against the real §16 store
# ---------------------------------------------------------------------------

class TestJobEndToEnd:
    def test_corp_actions_job_writes_events_and_manifest(self, store,
                                                         alpaca_creds):
        http = fake_transport({"/v1/corporate-actions": [(200, {
            "corporate_actions": {"cash_dividends": [{
                "symbol": "SPY", "ex_date": "2026-03-20",
                "record_date": "2026-03-21", "payable_date": "2026-04-30",
                "rate": 1.65}]}, "next_page_token": None})]})
        result = fetch_alpaca.fetch_corporate_actions(
            ticker="SPY", start=dt.date(2026, 1, 1),
            end=dt.date(2026, 6, 30), corp_actions_version="ca-v1",
            http_get=http)
        assert store.upsert_corp_actions(result.events) == 1
        manifest = fetch_alpaca.corp_actions_manifest_rows(
            result, manifest_version="ca-v1")
        assert store.write_manifest(manifest) == 1

        # §3.6 verification predicate: the written manifest verifies the span
        from trading_core.corporate_actions import (
            CoverageAttestation, corp_actions_span_verified,
        )
        attestations = [
            CoverageAttestation(
                source_kind="CORP_ACTIONS", ticker="SPY",
                span_start=dt.date.fromisoformat(m["span_start"]),
                span_end=dt.date.fromisoformat(m["span_end"]),
                verified=m["verified"], manifest_version=m["manifest_version"])
            for m in manifest]
        assert corp_actions_span_verified(
            ticker="SPY", span_start=dt.date(2026, 2, 1),
            span_end=dt.date(2026, 3, 31), attestations=attestations,
            manifest_version="ca-v1")
        # Unattested ticker is NOT verified (fail-closed, §3.6 item 4)
        assert not corp_actions_span_verified(
            ticker="QQQ", span_start=dt.date(2026, 2, 1),
            span_end=dt.date(2026, 3, 31), attestations=attestations,
            manifest_version="ca-v1")

    def test_news_inventory_feeds_population_completeness(self, store,
                                                          finnhub_creds):
        """Headlines + manifest written by the fetch job must be directly
        consumable by the Phase-2 cache-population machinery: zero cache
        misses requires the manifest-backed covered-span lookup to work."""
        from backtest.news.cache import HeadlineInventory
        body = [{"category": "company", "datetime": 1767637800,
                 "headline": "Apple beats earnings estimates",
                 "id": 1, "source": "Reuters"}]
        http = fake_transport({"company-news": [(200, body)]})
        rows = fetch_finnhub.fetch_news_inventory(
            ticker="AAPL", start=dt.date(2026, 1, 5),
            end=dt.date(2026, 1, 6), http_get=http)
        store.upsert_headlines(rows)
        store.write_manifest(fetch_finnhub.news_manifest_rows(
            ticker="AAPL", start=dt.date(2026, 1, 5),
            end=dt.date(2026, 1, 6), manifest_version="nm1"))

        inv = HeadlineInventory(store._conn)
        timed = inv.timed_headlines("AAPL")
        assert len(timed) == 1
        h_hash, source, published_at = timed[0]
        assert inv.covered("AAPL", manifest_version="nm1", at=published_at)
        # outside the manifest span -> coverage gap (never NEWS_UNVERIFIED)
        assert not inv.covered("AAPL", manifest_version="nm1",
                               at=published_at.replace(
                                   year=published_at.year - 1))
