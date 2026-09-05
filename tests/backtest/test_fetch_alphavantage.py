"""R2.8.1 §3.8 — Alpha Vantage historical NEWS adapter tests (hermetic).

No network: every fetch is exercised through an injected fake
transport. Synthetic fixtures ONLY, constructed from the established
bounded-probe response shape — the real probe payload stays outside the
repo under the managed root (~/.hermes/data/r28/alphavantage/) and
never enters the repo.

Covers the §3.8 substitutable historical NEWS contract:
- canonical row fields (existing news_headlines shape) with FP-4
  normalization and headline_hash REUSE from trading_core.news_effects;
- source mapping; requested-ticker association via ticker_sentiment
  only (present among multiple tickers; absent -> no row);
- time_published YYYYMMDDTHHMMSS -> aware ISO-8601 UTC (the Alpha
  Vantage NEWS_SENTIMENT temporal axis is UTC per the provider's own
  documentation, which describes time_from=20220410T0130 as 1:30am
  UTC);
- provider sentiment/relevance fields never enter the canonical row and
  never change the hash;
- payload validation fail-closed (non-object, envelopes, missing feed,
  wrong feed type, malformed items, missing title/source/time);
- provider Information / Note / Error Message envelopes rejected —
  never a successful zero-news span;
- structurally valid empty feed -> zero rows;
- apikey redaction from transport errors;
- rows feed IngestStore.upsert_headlines directly.
"""

import datetime as dt
import json

import pytest

from backtest.data import fetch_alphavantage
from backtest.data.ingest_core import (
    FetchLog,
    IngestionError,
    IngestStore,
)
from backtest.db.schema import open_db
from trading_core.news_effects import (
    headline_hash as canonical_headline_hash,
    normalize_headline_text,
)


@pytest.fixture
def store(tmp_path):
    conn = open_db(tmp_path / "bt.sqlite3")
    try:
        yield IngestStore(conn, run_id="test-run", config_version=1,
                          code_commit="abc")
    finally:
        conn.close()


@pytest.fixture
def av_creds(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.get_env_value",
                        lambda key: {"ALPHAVANTAGE_API_KEY": "t"}.get(key))


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


def _feed_item(**overrides):
    """Synthetic feed item in the established probe shape. Sentiment /
    relevance fields are present so tests can assert they are DROPPED."""
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


def _fetch(body, *, ticker="AAPL",
           start=dt.date(2019, 1, 1), end=dt.date(2019, 1, 30),
           fetch_log=None, calls=None):
    http = fake_transport({"alphavantage.co": [(200, body)]}, calls)
    return fetch_alphavantage.fetch_news_inventory(
        ticker=ticker, start=start, end=end, http_get=http,
        fetch_log=fetch_log)


# ---------------------------------------------------------------------------
# 1-5: canonical row shape, FP-4/hash reuse, source, timestamp
# ---------------------------------------------------------------------------

class TestCanonicalRows:
    def test_valid_payload_produces_canonical_row(self, av_creds):
        rows = _fetch(_payload([_feed_item()]))
        assert len(rows) == 1
        r = rows[0]
        assert set(r) == {"headline_hash", "source", "ticker",
                          "published_at", "headline_text_normalized",
                          "fetched_at"}

    def test_fp4_normalization_reused(self, av_creds):
        title = "Apple beats synthetic earnings estimates"
        rows = _fetch(_payload([_feed_item(title=title)]))
        assert rows[0]["headline_text_normalized"] == \
            normalize_headline_text(title)

    def test_headline_hash_reused(self, av_creds):
        title = "Apple beats synthetic earnings estimates"
        rows = _fetch(_payload([_feed_item(title=title)]))
        assert rows[0]["headline_hash"] == canonical_headline_hash(title)

    def test_source_mapped_from_provider_source(self, av_creds):
        rows = _fetch(_payload([_feed_item(source="Synthetic Wire")]))
        assert rows[0]["source"] == "Synthetic Wire"

    def test_ticker_is_requested_ticker(self, av_creds):
        rows = _fetch(_payload([_feed_item()]), ticker="AAPL")
        assert rows[0]["ticker"] == "AAPL"

    def test_published_at_aware_iso_utc(self, av_creds):
        rows = _fetch(_payload([_feed_item(
            time_published="20190102T153000")]))
        assert rows[0]["published_at"] == "2019-01-02T15:30:00+00:00"

    def test_fetched_at_present(self, av_creds):
        rows = _fetch(_payload([_feed_item()]))
        assert rows[0]["fetched_at"]
        assert "+00:00" in rows[0]["fetched_at"]

    def test_rows_feed_upsert_headlines(self, av_creds, store):
        rows = _fetch(_payload([_feed_item()]))
        assert store.upsert_headlines(rows) == 1
        assert store.headline_count("AAPL") == 1
        # idempotent re-ingest
        assert store.upsert_headlines(rows) == 0


# ---------------------------------------------------------------------------
# 6-8: ticker association
# ---------------------------------------------------------------------------

class TestTickerAssociation:
    def test_multi_ticker_article_with_requested_ticker(self, av_creds):
        item = _feed_item(ticker_sentiment=[
            {"ticker": "MSFT", "relevance_score": "0.900000",
             "ticker_sentiment_label": "Bearish",
             "ticker_sentiment_score": -0.2},
            {"ticker": "AAPL", "relevance_score": "0.010000",
             "ticker_sentiment_label": "Neutral",
             "ticker_sentiment_score": 0.0},
        ])
        rows = _fetch(_payload([item]))
        assert len(rows) == 1
        assert rows[0]["ticker"] == "AAPL"

    def test_requested_ticker_absent_no_row(self, av_creds):
        item = _feed_item(ticker_sentiment=[
            {"ticker": "MSFT", "relevance_score": "0.900000",
             "ticker_sentiment_label": "Bearish",
             "ticker_sentiment_score": -0.2},
        ])
        rows = _fetch(_payload([item]), ticker="AAPL")
        assert rows == []

    def test_provider_ticker_formatting_normalized_for_comparison(
            self, av_creds):
        item = _feed_item(ticker_sentiment=[
            {"ticker": " aapl ", "relevance_score": "0.500000",
             "ticker_sentiment_label": "Neutral",
             "ticker_sentiment_score": 0.0},
        ])
        rows = _fetch(_payload([item]), ticker="AAPL")
        assert len(rows) == 1
        # the CANONICAL row carries the requested ticker, not the
        # provider-formatted variant
        assert rows[0]["ticker"] == "AAPL"

    def test_empty_ticker_sentiment_no_row(self, av_creds):
        item = _feed_item(ticker_sentiment=[])
        rows = _fetch(_payload([item]))
        assert rows == []


# ---------------------------------------------------------------------------
# 9: timestamp normalization
# ---------------------------------------------------------------------------

class TestTimestamps:
    @pytest.mark.parametrize("raw,expected", [
        ("20190101T000000", "2019-01-01T00:00:00+00:00"),
        ("20190131T235959", "2019-01-31T23:59:59+00:00"),
        ("20250715T120001", "2025-07-15T12:00:01+00:00"),
    ])
    def test_timestamp_normalization(self, av_creds, raw, expected):
        rows = _fetch(_payload([_feed_item(time_published=raw)]))
        assert rows[0]["published_at"] == expected

    @pytest.mark.parametrize("bad", [
        None, "", "not-a-timestamp", "20190102", "20190102T1530",
        "20190102T153000Z", "2019-01-02T15:30:00", 42,
        "20190102T153000extra",
    ])
    def test_invalid_timestamp_rejected(self, av_creds, bad):
        with pytest.raises(IngestionError, match="time_published"):
            _fetch(_payload([_feed_item(time_published=bad)]))

    def test_impossible_calendar_date_rejected(self, av_creds):
        with pytest.raises(IngestionError, match="time_published"):
            _fetch(_payload([_feed_item(time_published="20191340T999999")]))


# ---------------------------------------------------------------------------
# 10: zero results
# ---------------------------------------------------------------------------

class TestZeroResults:
    def test_empty_valid_feed_zero_rows(self, av_creds):
        rows = _fetch(_payload([]))
        assert rows == []

    def test_no_headlines_invented(self, av_creds):
        rows = _fetch(_payload([]))
        assert all(r["headline_hash"] for r in rows)


# ---------------------------------------------------------------------------
# 11-18: payload validation (fail closed)
# ---------------------------------------------------------------------------

class TestPayloadValidation:
    def test_non_object_payload_rejected(self, av_creds):
        with pytest.raises(IngestionError, match="object"):
            _fetch([_feed_item()])

    def test_missing_feed_rejected(self, av_creds):
        with pytest.raises(IngestionError, match="feed"):
            _fetch({"items": 0, "sentiment_score_definition": "x"})

    def test_feed_not_a_list_rejected(self, av_creds):
        with pytest.raises(IngestionError, match="array"):
            _fetch({"feed": {"title": "oops"}})

    def test_feed_item_not_an_object_rejected(self, av_creds):
        with pytest.raises(IngestionError, match="object"):
            _fetch(_payload(["not an object"]))

    @pytest.mark.parametrize("title", [None, "", "   ", 42])
    def test_missing_or_invalid_title_rejected(self, av_creds, title):
        with pytest.raises(IngestionError, match="title"):
            _fetch(_payload([_feed_item(title=title)]))

    def test_title_missing_key_rejected(self, av_creds):
        item = _feed_item()
        del item["title"]
        with pytest.raises(IngestionError, match="title"):
            _fetch(_payload([item]))

    @pytest.mark.parametrize("source", [None, "", "   ", 42])
    def test_missing_or_invalid_source_rejected(self, av_creds, source):
        with pytest.raises(IngestionError, match="source"):
            _fetch(_payload([_feed_item(source=source)]))

    def test_punctuation_only_title_rejected(self, av_creds):
        # Normalizes to empty FP-4 text — a canonical headline cannot
        # be empty, so this fails closed rather than fabricating.
        with pytest.raises(IngestionError, match="FP-4"):
            _fetch(_payload([_feed_item(title="?!...")]))


# ---------------------------------------------------------------------------
# malformed ticker_sentiment
# ---------------------------------------------------------------------------

class TestMalformedTickerSentiment:
    def test_non_list_rejected(self, av_creds):
        with pytest.raises(IngestionError, match="ticker_sentiment"):
            _fetch(_payload([_feed_item(ticker_sentiment={"ticker": "AAPL"})]))

    def test_missing_key_rejected(self, av_creds):
        item = _feed_item()
        del item["ticker_sentiment"]
        with pytest.raises(IngestionError, match="ticker_sentiment"):
            _fetch(_payload([item]))

    def test_non_object_entry_rejected(self, av_creds):
        with pytest.raises(IngestionError, match="ticker_sentiment"):
            _fetch(_payload([_feed_item(ticker_sentiment=["AAPL"])]))

    def test_entry_missing_ticker_rejected(self, av_creds):
        with pytest.raises(IngestionError, match="ticker"):
            _fetch(_payload([_feed_item(ticker_sentiment=[
                {"relevance_score": "0.5",
                 "ticker_sentiment_label": "Neutral",
                 "ticker_sentiment_score": 0.0}])]))


# ---------------------------------------------------------------------------
# 19-21: provider envelopes — never a successful zero-news span
# ---------------------------------------------------------------------------

class TestProviderEnvelopes:
    def test_information_envelope_rejected(self, av_creds):
        with pytest.raises(IngestionError, match="Information"):
            _fetch({"Information": "Thank you for using Alpha Vantage!"})

    def test_note_envelope_rejected(self, av_creds):
        with pytest.raises(IngestionError, match="Note"):
            _fetch({"Note": "API call frequency is 25 per day."})

    def test_error_message_envelope_rejected(self, av_creds):
        with pytest.raises(IngestionError, match="Error Message"):
            _fetch({"Error Message": "Invalid API call."})

    def test_envelope_alongside_feed_still_rejected(self, av_creds):
        # An envelope key present together with a feed is still a
        # provider notice, not a usable response.
        body = {**_payload([_feed_item()]),
                "Note": "API call frequency is 25 per day."}
        with pytest.raises(IngestionError, match="Note"):
            _fetch(body)


# ---------------------------------------------------------------------------
# 22-23: provider sentiment never enters canonical semantics
# ---------------------------------------------------------------------------

class TestProviderSentimentExclusion:
    def test_sentiment_fields_not_in_canonical_row(self, av_creds):
        rows = _fetch(_payload([_feed_item(
            overall_sentiment_label="Very-Bullish",
            overall_sentiment_score=0.95,
            ticker_sentiment=[{"ticker": "AAPL",
                               "relevance_score": "0.999999",
                               "ticker_sentiment_label": "Bullish",
                               "ticker_sentiment_score": 0.99},
                              {"ticker": "MSFT",
                               "relevance_score": "0.000001",
                               "ticker_sentiment_label": "Bearish",
                               "ticker_sentiment_score": -0.99}],
        )]))
        assert len(rows) == 1
        assert set(rows[0]) == {"headline_hash", "source", "ticker",
                                "published_at", "headline_text_normalized",
                                "fetched_at"}

    def test_sentiment_does_not_change_hash(self, av_creds):
        title = "Apple beats synthetic earnings estimates"
        calm = _fetch(_payload([_feed_item(
            title=title, overall_sentiment_score=0.0,
            ticker_sentiment=[{"ticker": "AAPL",
                               "relevance_score": "0.000001",
                               "ticker_sentiment_label": "Neutral",
                               "ticker_sentiment_score": 0.0}])]))
        excited = _fetch(_payload([_feed_item(
            title=title, overall_sentiment_score=0.99,
            ticker_sentiment=[{"ticker": "AAPL",
                               "relevance_score": "0.999999",
                               "ticker_sentiment_label": "Bullish",
                               "ticker_sentiment_score": 0.99}])]))
        assert calm[0]["headline_hash"] == excited[0]["headline_hash"]
        assert calm[0]["headline_hash"] == canonical_headline_hash(title)

    def test_sentiment_never_associates_ticker(self, av_creds):
        # A screaming relevance/sentiment score for a DIFFERENT ticker
        # must not establish association with the requested one.
        item = _feed_item(ticker_sentiment=[
            {"ticker": "MSFT", "relevance_score": "0.999999",
             "ticker_sentiment_label": "Bullish",
             "ticker_sentiment_score": 0.99},
        ])
        rows = _fetch(_payload([item]), ticker="AAPL")
        assert rows == []


# ---------------------------------------------------------------------------
# 24: secret redaction
# ---------------------------------------------------------------------------

class TestCredentialsAndRedaction:
    def test_missing_credentials_fail_closed(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.get_env_value",
                            lambda key: None)
        with pytest.raises(fetch_alphavantage.CredentialsMissing):
            fetch_alphavantage.fetch_news_inventory(
                ticker="AAPL", start=dt.date(2019, 1, 1),
                end=dt.date(2019, 1, 31))

    def test_optional_skills_env_var_not_read(self, monkeypatch):
        # ALPHA_VANTAGE_KEY (optional-skills) must NOT satisfy the
        # backtest credential — only ALPHAVANTAGE_API_KEY.
        monkeypatch.setattr("hermes_cli.config.get_env_value",
                            lambda key: {"ALPHA_VANTAGE_KEY": "t"}.get(key))
        with pytest.raises(fetch_alphavantage.CredentialsMissing):
            fetch_alphavantage.fetch_news_inventory(
                ticker="AAPL", start=dt.date(2019, 1, 1),
                end=dt.date(2019, 1, 31))

    def test_api_key_redacted_from_transport_error(self, av_creds):
        def http_get(url, headers=None, params=None, timeout=30.0):
            # Non-retryable status whose error body embeds the secret
            # (mirrors a provider echoing the request query back).
            return 403, "forbidden apikey=SECRET_API_KEY_VALUE"

        with pytest.raises(IngestionError) as excinfo:
            fetch_alphavantage.fetch_news_inventory(
                ticker="AAPL", start=dt.date(2019, 1, 1),
                end=dt.date(2019, 1, 31), http_get=http_get)
        assert "SECRET_API_KEY_VALUE" not in str(excinfo.value)
        assert "apikey=<redacted>" in str(excinfo.value)

    def test_api_key_redacted_from_provider_envelope(self, av_creds):
        # An envelope body echoing the key is also redacted.
        with pytest.raises(IngestionError) as excinfo:
            _fetch({"Information":
                    "quota exceeded for apikey=SECRET_API_KEY_VALUE"})
        assert "SECRET_API_KEY_VALUE" not in str(excinfo.value)

    def test_fetch_record_params_exclude_credential(self, av_creds):
        log = FetchLog()
        _fetch(_payload([_feed_item()]), fetch_log=log)
        assert len(log.records) == 1
        rec = log.records[0]
        assert "apikey" not in rec.params
        assert rec.provider == "alphavantage"
        assert rec.endpoint == "NEWS_SENTIMENT"

    def test_redact_apikey_unit(self):
        assert fetch_alphavantage._redact_apikey(
            "x?apikey=ABC&function=NEWS_SENTIMENT failed") == \
            "x?apikey=<redacted>"
        assert fetch_alphavantage._redact_apikey("no key here") == \
            "no key here"


# ---------------------------------------------------------------------------
# fetch / window contract
# ---------------------------------------------------------------------------

class TestFetchWindows:
    def test_window_sweep_chunks_requests(self, av_creds):
        calls = []
        # A 31-day span exceeds the 30-day window: Jan 1..30 then
        # Jan 31 alone.
        _fetch(_payload([]), start=dt.date(2019, 1, 1),
               end=dt.date(2019, 1, 31), calls=calls)
        assert len(calls) == 2
        assert calls[0]["params"]["function"] == "NEWS_SENTIMENT"
        assert calls[0]["params"]["tickers"] == "AAPL"
        assert calls[0]["params"]["time_from"] == "20190101T000000"
        assert calls[0]["params"]["time_to"] == "20190130T235959"
        assert calls[1]["params"]["time_from"] == "20190131T000000"
        assert calls[1]["params"]["time_to"] == "20190131T235959"

    def test_long_span_sweeps_in_30_day_windows(self, av_creds):
        calls = []
        log = FetchLog()
        _fetch(_payload([]), start=dt.date(2019, 1, 1),
               end=dt.date(2019, 3, 31), calls=calls, fetch_log=log)
        # 90 days / 30-day windows -> 3 requests
        assert len(calls) == 3
        assert len(log.records) == 3
        assert [c["params"]["time_from"] for c in calls] == [
            "20190101T000000", "20190131T000000", "20190302T000000"]
        assert [c["params"]["time_to"] for c in calls] == [
            "20190130T235959", "20190301T235959", "20190331T235959"]

    def test_request_never_places_key_in_url_path(self, av_creds):
        calls = []
        _fetch(_payload([]), calls=calls)
        assert "t=" not in calls[0]["url"]
        assert calls[0]["url"] == \
            "https://www.alphavantage.co/query"

    def test_associated_and_unassociated_items_in_one_feed(
            self, av_creds):
        feed = [
            _feed_item(title="Apple beats synthetic earnings estimates"),
            _feed_item(title="Microsoft misses synthetic estimates",
                       ticker_sentiment=[{"ticker": "MSFT",
                                          "relevance_score": "0.9",
                                          "ticker_sentiment_label":
                                              "Bearish",
                                          "ticker_sentiment_score": -0.2}]),
            _feed_item(title="Apple announces synthetic buyback",
                       time_published="20190115T090000"),
        ]
        rows = _fetch(_payload(feed))
        assert len(rows) == 2
        assert {r["headline_hash"] for r in rows} == {
            canonical_headline_hash("Apple beats synthetic earnings "
                                    "estimates"),
            canonical_headline_hash("Apple announces synthetic buyback"),
        }
        assert {r["published_at"] for r in rows} == {
            "2019-01-02T15:30:00+00:00", "2019-01-15T09:00:00+00:00"}


# ---------------------------------------------------------------------------
# 25: no network dependency — every http_get is the injected fake; a
# real transport is never constructed (module never imports httpx).
# ---------------------------------------------------------------------------

class TestHermetic:
    def test_module_has_no_network_import(self):
        import backtest.data.fetch_alphavantage as m
        assert not hasattr(m, "httpx")

    def test_normalize_is_pure_no_transport(self):
        rows = fetch_alphavantage.normalize_news_payload(
            _payload([_feed_item()]), ticker="AAPL",
            fetched_at="2019-02-01T00:00:00+00:00")
        assert len(rows) == 1
        assert rows[0]["fetched_at"] == "2019-02-01T00:00:00+00:00"

    def test_normalize_empty_feed_pure(self):
        assert fetch_alphavantage.normalize_news_payload(
            _payload([]), ticker="AAPL") == []
