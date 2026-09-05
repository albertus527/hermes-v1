"""R2.8.1 §3.8 — Alpha Vantage historical NEWS adapter (substitutable
historical news source under the §3.8 provider-neutral contract).

Grounded in the established bounded NEWS_SENTIMENT probe (AAPL,
2019-01-01 .. 2019-01-31, 26 feed rows; the real payload stays outside
the repo under the managed root — synthetic fixtures only in tests):

- Endpoint: ``GET https://www.alphavantage.co/query`` with
  ``function=NEWS_SENTIMENT``, ``tickers=<T>``, ``time_from`` /
  ``time_to`` (``YYYYMMDDTHHMMSS`` bounds), and the credential in the
  ``apikey`` query parameter.
- Top-level payload: ``{"items": …, "sentiment_score_definition": …,
  "relevance_score_definition": …, "feed": […]}``.
- Feed item fields: ``authors, banner_image, category_within_source,
  overall_sentiment_label, overall_sentiment_score, source,
  source_domain, summary, ticker_sentiment, time_published, title,
  topics, url``.
- ``time_published``: naive ``YYYYMMDDTHHMMSS`` (e.g.
  ``20190101T000000``).
- ``ticker_sentiment``: list of ``{relevance_score, ticker,
  ticker_sentiment_label, ticker_sentiment_score}``.

Canonical mapping (§3.8 N-1-a — the EXISTING ``news_headlines`` row
shape; no new field, no second NEWS path):

======================  =================================================
Alpha Vantage           canonical
======================  =================================================
``title``               FP-4 ``headline_text_normalized`` via the
                        EXISTING ``normalize_headline_text``, and the
                        EXISTING source-independent ``headline_hash``
``source``              ``source``
requested ticker        ``ticker``
``time_published``      ``published_at`` (aware ISO-8601 UTC)
fetch wall clock        ``fetched_at`` (existing ``_now_utc_iso``
                        provenance pattern)
======================  =================================================

Provider sentiment hard boundary (§3.8 N-1-d): ``overall_sentiment_*``,
``ticker_sentiment_*``, ``relevance_score``, ``topics``, ``summary``
and every other provider-generated field are DROPPED at this boundary.
They never enter a canonical row, never influence ``headline_hash``,
and never reach ``news_classifications`` / P-4 / G9 / scoring. The only
canonical classification is the existing pinned-model Phase-2 process.
``ticker_sentiment`` is inspected ONLY to establish ticker association.

Timestamp semantics (deterministic, from provider-established
semantics — no offset is invented): Alpha Vantage's official
NEWS_SENTIMENT documentation describes its ``time_from`` / ``time_to``
news timestamps in ``YYYYMMDDTHHMM`` format and explicitly describes
the example ``time_from=20220410T0130`` as 1:30am UTC. The Alpha
Vantage NEWS_SENTIMENT temporal axis is therefore UTC by provider
documentation, and observed ``time_published`` values using the same
provider news timestamp convention (``YYYYMMDDTHHMMSS``, no offset in
the provider representation) are normalized to aware ISO-8601 UTC.
This is consistent with the repository's canonical UTC ingestion axis
(the Finnhub adapter renders provider instants as aware UTC ISO-8601),
which the mapping preserves.

Ticker association: a feed article becomes a canonical row for the
requested ticker T ONLY when ``ticker_sentiment`` (a list) contains a
well-formed entry whose ``ticker`` matches T (whitespace-stripped,
case-insensitive — provider formatting normalized for comparison
only). Association is never inferred from title text and never from
sentiment/relevance values. A structurally valid article that does NOT
establish association with T is skipped — it never becomes a canonical
row for T (mirrors the Finnhub news adapter's skip of items with no
usable headline text). A malformed ``ticker_sentiment`` structure is a
deterministic rejection of the payload (EODHD malformed-item
precedent).

Provider envelopes: top-level ``Information`` / ``Note`` /
``Error Message`` keys are deterministic rejections — a provider
informational/error response is NEVER a successful zero-news span.

Pagination / completeness (§3.8 N-1-f): this adapter exposes bounded
date-window fetching, but provider truncation/pagination semantics are
NOT established by the probe. This checkpoint writes NO coverage
manifests and claims NO completeness beyond what a response itself
establishes; completeness attestation is deferred to the later
production-ingestion checkpoint.

No secrets are persisted: ALPHAVANTAGE_API_KEY is read via
``hermes_cli.config.get_env_value`` (the optional-skills
``ALPHA_VANTAGE_KEY`` variable is deliberately NOT read) and used only
as a query parameter; ``apikey`` values are redacted from any error
text before it propagates.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Callable

from trading_core.news_effects import headline_hash as compute_headline_hash
from trading_core.news_effects import normalize_headline_text

from backtest.data.ingest_core import (
    FetchLog,
    FetchRecord,
    IngestionError,
    _now_utc_iso,
    fetch_json,
)

ALPHAVANTAGE_BASE = "https://www.alphavantage.co/query"

# Bounded date-window fetching (§3.8 N-1-f): sweep in windows rather
# than one unbounded multi-year request. The window size is an adapter
# implementation detail and claims nothing about provider completeness.
NEWS_WINDOW_DAYS = 30

# Provider informational/error envelope keys — a 200 body carrying one
# of these is a provider notice, never a successful empty feed.
_ENVELOPE_KEYS = ("Information", "Note", "Error Message")

# Naive provider timestamp: exactly YYYYMMDDTHHMMSS (20190101T000000).
_TIME_PUBLISHED_RE = re.compile(r"^\d{8}T\d{6}$")


class CredentialsMissing(IngestionError):
    """ALPHAVANTAGE_API_KEY is not configured (§20 Phase 0 credential
    prerequisite). Fail-closed: the job refuses to run."""


def _redact_apikey(text: Any) -> str:
    """Strip an ``apikey=…`` query value from error text before it can
    reach a log line or a stored report."""
    s = str(text)
    if "apikey=" in s:
        head, _, _tail = s.partition("apikey=")
        s = head + "apikey=<redacted>"
    return s


def _alphavantage_params() -> dict[str, str]:
    from hermes_cli.config import get_env_value
    token = get_env_value("ALPHAVANTAGE_API_KEY")
    if not token:
        raise CredentialsMissing(
            "ALPHAVANTAGE_API_KEY is not configured (~/.hermes/.env); the "
            "Alpha Vantage NEWS ingestion job refuses to run")
    return {"apikey": token}


def _parse_feed(payload: Any) -> list:
    """Validate the top-level NEWS_SENTIMENT payload and return the
    ``feed`` list. Fail closed on: non-object payloads, provider
    informational/error envelopes, missing ``feed``, non-list ``feed``.
    """
    if not isinstance(payload, dict):
        raise IngestionError(
            f"Alpha Vantage NEWS_SENTIMENT payload must be an object with "
            f"a 'feed' array, got {type(payload).__name__}")
    for key in _ENVELOPE_KEYS:
        notice = payload.get(key)
        if notice is not None:
            raise IngestionError(_redact_apikey(
                f"Alpha Vantage NEWS_SENTIMENT returned a provider "
                f"{key!r} envelope instead of a feed: {notice!r} — "
                f"refusing to treat it as a successful zero-news span"))
    feed = payload.get("feed")
    if feed is None:
        raise IngestionError(
            "Alpha Vantage NEWS_SENTIMENT payload is missing the 'feed' "
            "array")
    if not isinstance(feed, list):
        raise IngestionError(
            f"Alpha Vantage NEWS_SENTIMENT 'feed' must be an array, got "
            f"{type(feed).__name__}")
    return feed


def _parse_time_published(value: Any) -> str:
    """Naive ``YYYYMMDDTHHMMSS`` → aware ISO-8601 UTC (see module
    docstring for the timezone determination). Missing or malformed
    values fail closed — a canonical ``published_at`` is never invented
    (§3.8 N-1-a)."""
    if not isinstance(value, str) or not _TIME_PUBLISHED_RE.match(value):
        raise IngestionError(
            f"Alpha Vantage feed item has missing/malformed "
            f"time_published {value!r} — the canonical published_at "
            f"cannot be invented (R2.8.1 §3.8 N-1-a)")
    try:
        parsed = _dt.datetime.strptime(value, "%Y%m%dT%H%M%S")
    except ValueError as exc:
        raise IngestionError(
            f"Alpha Vantage feed item has malformed time_published "
            f"{value!r}: {exc}") from exc
    return parsed.replace(tzinfo=_dt.timezone.utc).isoformat()


def _establishes_association(item: dict, ticker: str) -> bool:
    """Whether the article's ``ticker_sentiment`` structure establishes
    association with the requested ticker (whitespace-stripped,
    case-insensitive comparison — provider ticker formatting is
    normalized for comparison only). Sentiment and relevance values are
    never consulted. Malformed structures fail closed."""
    entries = item.get("ticker_sentiment")
    if not isinstance(entries, list):
        raise IngestionError(
            f"Alpha Vantage feed item {item.get('title')!r} has malformed "
            f"ticker_sentiment (expected a list, got "
            f"{type(entries).__name__}) — ticker association cannot be "
            f"established")
    wanted = ticker.strip().upper()
    for entry in entries:
        if not isinstance(entry, dict):
            raise IngestionError(
                f"Alpha Vantage feed item {item.get('title')!r} has a "
                f"malformed ticker_sentiment entry (expected an object, "
                f"got {type(entry).__name__})")
        entry_ticker = entry.get("ticker")
        if not isinstance(entry_ticker, str) or not entry_ticker.strip():
            raise IngestionError(
                f"Alpha Vantage feed item {item.get('title')!r} has a "
                f"ticker_sentiment entry with missing/invalid ticker: "
                f"{entry_ticker!r}")
        if entry_ticker.strip().upper() == wanted:
            return True
    return False


def _rows_from_feed(feed: list, *, ticker: str,
                    fetched_at: str | None = None) -> list[dict]:
    """Normalize validated feed items into canonical ``news_headlines``
    rows for the requested ticker. Structurally malformed items fail
    closed; well-formed items not associated with the requested ticker
    are skipped (never a row for a ticker the article does not
    establish). No provider sentiment/relevance field is carried over.
    """
    fetched_at = fetched_at or _now_utc_iso()
    rows: list[dict] = []
    for item in feed:
        if not isinstance(item, dict):
            raise IngestionError(
                f"Alpha Vantage feed item must be an object, got "
                f"{type(item).__name__}")
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            raise IngestionError(
                f"Alpha Vantage feed item has missing/invalid title: "
                f"{title!r} — the canonical headline text cannot be "
                f"invented (R2.8.1 §3.8 N-1-a)")
        source = item.get("source")
        if not isinstance(source, str) or not source.strip():
            raise IngestionError(
                f"Alpha Vantage feed item {title!r} has missing/invalid "
                f"source: {source!r} — the canonical source cannot be "
                f"invented (R2.8.1 §3.8 N-1-a)")
        if not _establishes_association(item, ticker):
            continue  # not associated with the requested ticker
        normalized = normalize_headline_text(title)
        if not normalized:
            raise IngestionError(
                f"Alpha Vantage feed item title {title!r} normalizes to "
                f"empty FP-4 text — the canonical headline cannot be "
                f"empty")
        rows.append({
            "headline_hash": compute_headline_hash(title),
            "source": source.strip(),
            "ticker": ticker,
            "published_at": _parse_time_published(
                item.get("time_published")),
            "headline_text_normalized": normalized,
            "fetched_at": fetched_at,
        })
    return rows


def normalize_news_payload(payload: Any, *, ticker: str,
                           fetched_at: str | None = None) -> list[dict]:
    """Normalize one NEWS_SENTIMENT payload into canonical
    ``news_headlines`` rows for ``ticker`` (shape ready for
    ``IngestStore.upsert_headlines``).

    Deterministic validation failures (envelopes, malformed payload /
    feed / items, missing canonical fields) raise
    :class:`IngestionError`. A structurally valid ``"feed": []`` stays
    zero rows — no headlines are invented. Articles that do not
    establish association with ``ticker`` produce no row.
    """
    return _rows_from_feed(
        _parse_feed(payload), ticker=ticker, fetched_at=fetched_at)


def _window_stamp(d: _dt.date, *, end_of_day: bool = False) -> str:
    """Provider-format window bound: ``YYYYMMDDTHHMMSS`` at midnight or
    23:59:59 — the same UTC-axis bounds the manifest machinery uses."""
    t = _dt.time(23, 59, 59) if end_of_day else _dt.time(0, 0, 0)
    return _dt.datetime.combine(d, t).strftime("%Y%m%dT%H%M%S")


def fetch_news_inventory(
    *,
    ticker: str,
    start: _dt.date,
    end: _dt.date,
    http_get: Callable[..., tuple[int, str]] | None = None,
    fetch_log: FetchLog | None = None,
) -> list[dict]:
    """Sweep Alpha Vantage NEWS_SENTIMENT for one ticker over
    [start, end] in bounded date windows.

    Returns canonical rows for ``IngestStore.upsert_headlines`` with
    FP-4-normalized text, the existing source-independent
    ``headline_hash``, and aware-UTC ISO-8601 ``published_at``. This
    checkpoint writes NO coverage manifests — a completed sweep here
    does not by itself attest verified NEWS coverage (§3.8 N-1-e/f).

    The ``apikey`` credential is redacted from any transport error text
    before it propagates (the key rides in the query parameters).
    """
    log = fetch_log or FetchLog()
    rows: list[dict] = []
    fetched_at = _now_utc_iso()
    base_params = _alphavantage_params()  # fail closed before any request
    window_start = start
    while window_start <= end:
        window_end = min(
            window_start + _dt.timedelta(days=NEWS_WINDOW_DAYS - 1), end)
        try:
            payload = fetch_json(
                ALPHAVANTAGE_BASE,
                params={**base_params,
                        "function": "NEWS_SENTIMENT",
                        "tickers": ticker,
                        "time_from": _window_stamp(window_start),
                        "time_to": _window_stamp(window_end,
                                                 end_of_day=True)},
                http_get=http_get)
        except IngestionError as exc:
            raise IngestionError(_redact_apikey(exc)) from exc
        feed = _parse_feed(payload)
        rows.extend(_rows_from_feed(
            feed, ticker=ticker, fetched_at=fetched_at))
        # FetchRecord params deliberately exclude the credential.
        log.add(FetchRecord(
            provider="alphavantage", endpoint="NEWS_SENTIMENT",
            params={"tickers": ticker,
                    "time_from": _window_stamp(window_start),
                    "time_to": _window_stamp(window_end,
                                             end_of_day=True)},
            fetched_at=fetched_at, items=len(feed)))
        window_start = window_end + _dt.timedelta(days=1)
    return rows
