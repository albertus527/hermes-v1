"""R2.8 §3.7 — EODHD historical-earnings adapter tests (hermetic).

No network: every fetch is exercised through an injected fake
transport. Synthetic fixtures ONLY — real EODHD payloads stay under
the managed root (~/.hermes/data/r28/eodhd/) and never enter the repo.

Covers the §3.7 substitutable-source contract:
- timing mapping AfterMarket/BeforeMarket/null → G6 vocabulary;
- unknown non-null timing → deterministic failure (C-1: never a
  silent UNSPECIFIED);
- AAPL.US → AAPL symbol normalization; malformed symbols rejected;
- report_date is the canonical event date (fiscal-period ``date``
  field ignored); missing report_date fails closed;
- wrapper payload["earnings"] parsing (missing/array-shape errors);
- empty earnings arrays stay empty — no invented events;
- duplicate policy: identical manifest rewrite is a no-op, verified
  flip is refused (existing IngestStore semantics);
- normalized events feed §7.2 G6 directly (EarningsEvent +
  map_earnings_event_session).
"""

import datetime as dt
import json

import pytest

from backtest.data import fetch_eodhd
from backtest.data.ingest_core import (
    FetchLog,
    IngestionError,
    IngestStore,
)
from backtest.db.schema import open_db


@pytest.fixture
def store(tmp_path):
    conn = open_db(tmp_path / "bt.sqlite3")
    try:
        yield IngestStore(conn, run_id="test-run", config_version=1,
                          code_commit="abc")
    finally:
        conn.close()


@pytest.fixture
def eodhd_creds(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.get_env_value",
                        lambda key: {"EODHD_API_KEY": "t"}.get(key))


def fake_transport(pages_by_url, calls=None):
    """http_get fake serving canned (status, json-body) pages in
    sequence per URL (matched by substring); the last page repeats."""
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


def _row(**overrides):
    row = {"code": "AAPL.US", "report_date": "2026-02-04",
           "date": "2025-12-31", "before_after_market": "AfterMarket",
           "currency": "USD", "actual": 1.05, "estimate": 1.04}
    row.update(overrides)
    return row


def _wrapper(rows):
    return {"type": "Earnings", "description": "Historical and upcoming "
            "Earnings", "symbols": "AAPL.US", "earnings": rows}


def _fetch(body, *, ticker="AAPL",
           start=dt.date(2026, 2, 1), end=dt.date(2026, 2, 28),
           fetch_log=None):
    http = fake_transport({"calendar/earnings": [(200, body)]})
    return fetch_eodhd.fetch_earnings_calendar(
        ticker=ticker, start=start, end=end, http_get=http,
        fetch_log=fetch_log)


# ---------------------------------------------------------------------------
# 1-4: timing normalization (§3.7 C-1)
# ---------------------------------------------------------------------------

class TestTimingNormalization:
    def test_after_market_maps_to_after_market_close(self, eodhd_creds):
        events = _fetch(_wrapper([_row(before_after_market="AfterMarket")]))
        assert events == [{"ticker": "AAPL", "event_date": "2026-02-04",
                           "timing": "after-market-close"}]

    def test_before_market_maps_to_before_market_open(self, eodhd_creds):
        events = _fetch(_wrapper([_row(before_after_market="BeforeMarket")]))
        assert events[0]["timing"] == "before-market-open"

    def test_null_timing_maps_to_unspecified(self, eodhd_creds):
        events = _fetch(_wrapper([_row(before_after_market=None)]))
        assert events[0]["timing"] == "unspecified"

    def test_missing_timing_key_maps_to_unspecified(self, eodhd_creds):
        row = _row()
        del row["before_after_market"]
        events = _fetch(_wrapper([row]))
        assert events[0]["timing"] == "unspecified"

    @pytest.mark.parametrize("value", [
        "DuringMarket", "MarketHours", "aftermarket", "Aftermarket",
        "UNKNOWN", "UnknownButSpecified", "", " ", 5, True,
    ])
    def test_unknown_non_null_timing_rejected(self, eodhd_creds, value):
        with pytest.raises(IngestionError, match="before_after_market"):
            _fetch(_wrapper([_row(before_after_market=value)]))


# ---------------------------------------------------------------------------
# 5-6: symbol normalization
# ---------------------------------------------------------------------------

class TestSymbolNormalization:
    def test_us_suffix_stripped(self, eodhd_creds):
        events = _fetch(_wrapper([_row(code="AAPL.US")]))
        assert events[0]["ticker"] == "AAPL"

    @pytest.mark.parametrize("code", [
        "AAPL",           # no suffix
        "AAPL.XYZ",       # unexpected suffix
        "AAPL.LSE",
        "",               # empty
        "   ",            # whitespace-only
        ".US",            # empty ticker body
        "AA PL.US",       # embedded whitespace
        None,             # null
        42,               # non-string
    ])
    def test_malformed_symbol_rejected(self, eodhd_creds, code):
        with pytest.raises(IngestionError):
            _fetch(_wrapper([_row(code=code)]))


# ---------------------------------------------------------------------------
# 7: canonical event date = report_date
# ---------------------------------------------------------------------------

class TestReportDate:
    def test_missing_report_date_rejected(self, eodhd_creds):
        row = _row()
        del row["report_date"]
        with pytest.raises(IngestionError, match="report_date"):
            _fetch(_wrapper([row]))

    def test_null_report_date_rejected(self, eodhd_creds):
        with pytest.raises(IngestionError, match="report_date"):
            _fetch(_wrapper([_row(report_date=None)]))

    def test_malformed_report_date_rejected(self, eodhd_creds):
        with pytest.raises(IngestionError, match="report_date"):
            _fetch(_wrapper([_row(report_date="not-a-date")]))

    def test_fiscal_period_date_field_is_not_used(self, eodhd_creds):
        # The provider `date` (fiscal period) differs from report_date;
        # the event date must be report_date.
        events = _fetch(_wrapper(
            [_row(report_date="2026-02-04", date="2025-12-31")]))
        assert events[0]["event_date"] == "2026-02-04"


# ---------------------------------------------------------------------------
# 8-9: wrapper parsing
# ---------------------------------------------------------------------------

class TestWrapperParsing:
    def test_payload_earnings_key_parsed(self, eodhd_creds):
        events = _fetch(_wrapper([
            _row(code="AAPL.US", report_date="2026-02-04",
                 before_after_market="BeforeMarket"),
            _row(code="AAPL.US", report_date="2026-05-01",
                 before_after_market=None),
        ]))
        assert [(e["event_date"], e["timing"]) for e in events] == [
            ("2026-02-04", "before-market-open"),
            ("2026-05-01", "unspecified"),
        ]

    def test_missing_earnings_key_rejected(self, eodhd_creds):
        with pytest.raises(IngestionError, match="earnings"):
            _fetch({"type": "Earnings"})

    def test_non_object_payload_rejected(self, eodhd_creds):
        with pytest.raises(IngestionError, match="object wrapper"):
            _fetch([_row()])

    def test_non_array_earnings_rejected(self, eodhd_creds):
        with pytest.raises(IngestionError, match="array"):
            _fetch({"earnings": {"code": "AAPL.US"}})

    def test_non_object_row_rejected(self, eodhd_creds):
        with pytest.raises(IngestionError, match="row"):
            _fetch({"earnings": ["AAPL.US"]})

    def test_empty_earnings_array_stays_empty(self, eodhd_creds):
        events = _fetch(_wrapper([]))
        assert events == []

    def test_cross_symbol_row_rejected(self, eodhd_creds):
        # A response for AAPL.US containing an MSFT.US row is a provider
        # anomaly — never silently ingested.
        with pytest.raises(IngestionError, match="cross-symbol"):
            _fetch(_wrapper([_row(code="MSFT.US")]))

    def test_window_sweep_chunks_requests(self, eodhd_creds):
        http = fake_transport({"calendar/earnings": [(200, _wrapper([]))]})
        log = FetchLog()
        fetch_eodhd.fetch_earnings_calendar(
            ticker="AAPL", start=dt.date(2026, 1, 1),
            end=dt.date(2026, 3, 31), http_get=http, fetch_log=log)
        # 90 days / 90-day windows -> 1 record; a 180-day span -> 2
        assert len(log.records) == 1
        assert log.records[0].provider == "eodhd"
        assert log.records[0].items == 0
        fetch_eodhd.fetch_earnings_calendar(
            ticker="AAPL", start=dt.date(2026, 1, 1),
            end=dt.date(2026, 6, 29), http_get=http, fetch_log=log)
        assert len(log.records) == 3


# ---------------------------------------------------------------------------
# 10: duplicate policy (existing IngestStore semantics)
# ---------------------------------------------------------------------------

class TestDuplicatePolicy:
    def test_manifest_identical_rewrite_noop_conflict_fails(self, store):
        rows = fetch_eodhd.earnings_manifest_rows(
            ticker="AAPL", start=dt.date(2026, 1, 1),
            end=dt.date(2026, 1, 31), manifest_version="eodhd-e1")
        assert rows == [{
            "source_kind": "EARNINGS", "ticker": "AAPL",
            "span_start": "2026-01-01T00:00:00+00:00",
            "span_end": "2026-01-31T23:59:59+00:00",
            "verified": True, "manifest_version": "eodhd-e1"}]
        assert store.write_manifest(rows) == 1
        assert store.write_manifest(list(rows)) == 0
        with pytest.raises(IngestionError, match="refusing rewrite"):
            store.write_manifest([dict(rows[0], verified=False)])


# ---------------------------------------------------------------------------
# 11: integration with existing earnings ingestion (§7.2 G6)
# ---------------------------------------------------------------------------

class TestG6Integration:
    def test_earnings_events_feed_gate_g6(self, eodhd_creds):
        """Normalized EODHD events must be directly consumable by §7.2
        G6 — same contract as the Finnhub feed test."""
        from trading_core.gates import (
            EarningsEvent,
            map_earnings_event_session,
        )
        events = _fetch(_wrapper([_row(
            report_date="2026-01-06", before_after_market="BeforeMarket")]))
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

    def test_after_market_close_maps_to_next_session(self, eodhd_creds):
        from trading_core.gates import (
            EarningsEvent,
            map_earnings_event_session,
        )
        # 2026-01-09 is a Friday; amc maps to the next trading session
        events = _fetch(_wrapper([_row(
            report_date="2026-01-09", before_after_market="AfterMarket")]))
        ev = EarningsEvent(
            event_date=dt.date.fromisoformat(events[0]["event_date"]),
            timing=events[0]["timing"])

        def _next_td(d):
            nxt = d + dt.timedelta(days=1)
            while nxt.weekday() >= 5:
                nxt += dt.timedelta(days=1)
            return nxt

        assert map_earnings_event_session(
            ev, is_trading_day=lambda d: d.weekday() < 5,
            next_trading_day=_next_td) == dt.date(2026, 1, 12)


# ---------------------------------------------------------------------------
# credentials + secret hygiene
# ---------------------------------------------------------------------------

class TestCredentials:
    def test_missing_credentials_fail_closed(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.get_env_value",
                            lambda key: None)
        with pytest.raises(fetch_eodhd.CredentialsMissing):
            fetch_eodhd.fetch_earnings_calendar(
                ticker="AAPL", start=dt.date(2026, 1, 1),
                end=dt.date(2026, 1, 31))

    def test_api_token_redacted_from_transport_errors(self, eodhd_creds):
        def http_get(url, headers=None, params=None, timeout=30.0):
            # Non-retryable status whose error body embeds the secret
            # (mirrors a provider echoing the request query back).
            return 403, "forbidden api_token=SECRET_TOKEN_VALUE"

        with pytest.raises(IngestionError) as excinfo:
            fetch_eodhd.fetch_earnings_calendar(
                ticker="AAPL", start=dt.date(2026, 1, 1),
                end=dt.date(2026, 1, 31), http_get=http_get)
        assert "SECRET_TOKEN_VALUE" not in str(excinfo.value)
        assert "api_token=<redacted>" in str(excinfo.value)

    def test_redact_token_unit(self):
        assert fetch_eodhd._redact_token(
            "x?api_token=ABC&fmt=json failed") == \
            "x?api_token=<redacted>"
        assert fetch_eodhd._redact_token("no token here") == \
            "no token here"
