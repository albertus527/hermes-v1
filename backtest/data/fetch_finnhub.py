"""R2.7 Phase-0 — Finnhub ingestion (raw news inventory + earnings calendar).

Grounded strictly in the published Finnhub API:

- Company news: ``GET https://finnhub.io/api/v1/company-news?symbol=…&from=YYYY-MM-DD&to=YYYY-MM-DD&token=…``
  Response: array of ``{category, datetime (unix seconds), headline, id,
  image, related, source, summary, url}``. ``datetime`` is unix epoch
  seconds (UTC).
- Earnings calendar: ``GET https://finnhub.io/api/v1/calendar/earnings?from=…&to=…&symbol=…&token=…``
  Response: ``{"earningsCalendar": [{date, epsActual, epsEstimate,
  epsRevised, hour (bmo|amc|dmh|time), quarter, revenueActual,
  revenueEstimate, symbol, year}]}``. ``hour`` values: ``bmo`` (before
  market open), ``amc`` (after market close), ``dmh`` (during market
  hours); a plain ``"time"`` string may appear when the hour is not
  finalized — mapped to ``unspecified``.

Coverage honesty (FP-5): the NEWS/EARNINGS manifests produced here
attest ONLY the spans actually queried with a successful response.
Finnhub's free-tier history depth is a MUST CONFIRM (§3.1) — this job
records what the provider returned for the requested span, including a
ZERO-ITEM response, which is still a verified covered span (absence of
headlines inside a covered span is not a coverage gap, §11.6).

Headline normalization uses the FP-4 canonical form (NFKC → casefold →
whitespace collapse → strip → punctuation strip → SHA-256) so
``headline_hash`` in the inventory is source-independent and identical
to what the Phase-2 cache-population job will key on.

No secrets are persisted; FINNHUB_API_KEY is read via
``hermes_cli.config.get_env_value`` and used only as a query param.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Callable

from trading_core.news_effects import headline_hash as compute_headline_hash
from trading_core.news_effects import normalize_headline_text

from backtest.data.ingest_core import (
    FetchLog,
    FetchRecord,
    IngestionError,
    IngestStore,
    _now_utc_iso,
    fetch_json,
)

FINNHUB_BASE = "https://finnhub.io/api/v1"

# Finnhub company-news date ranges are capped by the provider; sweep in
# windows small enough to stay well inside any documented limit.
NEWS_WINDOW_DAYS = 30
EARNINGS_WINDOW_DAYS = 90

# Provider `hour` vocabulary → §7.2 G6 timing vocabulary.
_HOUR_MAP = {
    "bmo": "before-market-open",
    "amc": "after-market-close",
    "dmh": "unspecified",   # during market hours — not bmo/amc; G6 treats as unspecified
}


class CredentialsMissing(IngestionError):
    """FINNHUB_API_KEY is not configured (§20 Phase 0 credential
    prerequisite). Fail-closed: the job refuses to run."""


def _finnhub_params() -> dict[str, str]:
    from hermes_cli.config import get_env_value
    token = get_env_value("FINNHUB_API_KEY")
    if not token:
        raise CredentialsMissing(
            "FINNHUB_API_KEY is not configured (~/.hermes/.env); the "
            "Finnhub ingestion job refuses to run")
    return {"token": token}


def _fetch_company_news_page(
    *, ticker: str, start: _dt.date, end: _dt.date,
    http_get: Callable[..., tuple[int, str]] | None,
) -> list[dict]:
    doc = fetch_json(
        f"{FINNHUB_BASE}/company-news",
        params={**_finnhub_params(), "symbol": ticker,
                "from": start.isoformat(), "to": end.isoformat()},
        http_get=http_get)
    if not isinstance(doc, list):
        raise IngestionError(
            f"company-news {ticker} {start}..{end}: expected array response")
    return doc


def fetch_news_inventory(
    *,
    ticker: str,
    start: _dt.date,
    end: _dt.date,
    http_get: Callable[..., tuple[int, str]] | None = None,
    fetch_log: FetchLog | None = None,
) -> list[dict]:
    """Sweep company news for one ticker over [start, end] in provider-safe
    windows. Returns rows for ``IngestStore.upsert_headlines`` with the
    FP-4-normalized text and source-independent ``headline_hash``.

    ``published_at`` is the provider ``datetime`` (unix seconds, UTC)
    rendered as an aware ISO-8601 timestamp. Untimed headlines do not
    occur at this layer (provider always supplies datetime); the §11.1
    drop rule guards consumption, not ingestion.
    """
    log = fetch_log or FetchLog()
    rows: list[dict] = []
    fetched_at = _now_utc_iso()
    window_start = start
    while window_start <= end:
        window_end = min(
            window_start + _dt.timedelta(days=NEWS_WINDOW_DAYS - 1), end)
        page = _fetch_company_news_page(
            ticker=ticker, start=window_start, end=window_end,
            http_get=http_get)
        for item in page:
            text = str(item.get("headline") or "").strip()
            if not text:
                continue  # no headline text — nothing to classify
            normalized = normalize_headline_text(text)
            if not normalized:
                continue
            published = _unix_to_iso(item.get("datetime"))
            rows.append({
                "headline_hash": compute_headline_hash(text),
                "source": str(item.get("source") or "finnhub"),
                "ticker": ticker,
                "published_at": published,
                "headline_text_normalized": normalized,
                "fetched_at": fetched_at,
            })
        log.add(FetchRecord(
            provider="finnhub", endpoint="company-news",
            params={"symbol": ticker, "from": window_start.isoformat(),
                    "to": window_end.isoformat()},
            fetched_at=fetched_at, items=len(page)))
        window_start = window_end + _dt.timedelta(days=1)
    return rows


def _unix_to_iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return None
    return _dt.datetime.fromtimestamp(
        ts, tz=_dt.timezone.utc).isoformat()


def fetch_earnings_calendar(
    *,
    ticker: str,
    start: _dt.date,
    end: _dt.date,
    http_get: Callable[..., tuple[int, str]] | None = None,
    fetch_log: FetchLog | None = None,
) -> list[dict]:
    """Sweep the earnings calendar for one ticker over [start, end].

    Returns event dicts shaped for §7.2 G6 (``EarningsEvent``-compatible):
    ``{ticker, event_date, timing}`` where timing is the G6 vocabulary
    (``before-market-open`` / ``after-market-close`` / ``unspecified``).
    The provider's non-bmo/amc hour values and missing hours map to
    ``unspecified``.
    """
    log = fetch_log or FetchLog()
    events: list[dict] = []
    fetched_at = _now_utc_iso()
    window_start = start
    while window_start <= end:
        window_end = min(
            window_start + _dt.timedelta(days=EARNINGS_WINDOW_DAYS - 1), end)
        doc = fetch_json(
            f"{FINNHUB_BASE}/calendar/earnings",
            params={**_finnhub_params(), "symbol": ticker,
                    "from": window_start.isoformat(),
                    "to": window_end.isoformat()},
            http_get=http_get)
        cal = doc.get("earningsCalendar") if isinstance(doc, dict) else None
        if cal is None:
            raise IngestionError(
                f"calendar/earnings {ticker} {window_start}..{window_end}: "
                "missing earningsCalendar")
        for item in cal:
            date = item.get("date")
            if not date:
                continue
            hour = str(item.get("hour") or "").lower()
            events.append({
                "ticker": ticker,
                "event_date": str(date),
                "timing": _HOUR_MAP.get(hour, "unspecified"),
            })
        log.add(FetchRecord(
            provider="finnhub", endpoint="calendar/earnings",
            params={"symbol": ticker, "from": window_start.isoformat(),
                    "to": window_end.isoformat()},
            fetched_at=fetched_at, items=len(cal)))
        window_start = window_end + _dt.timedelta(days=1)
    return events


def news_manifest_rows(*, ticker: str, start: _dt.date, end: _dt.date,
                       manifest_version: str) -> list[dict]:
    """Verified NEWS covered-span attestation for a completed sweep. A
    zero-headline span is still a verified covered span (§11.6). Span
    bounds are midnight-UTC timestamps of the inclusive date range —
    the format ``HeadlineInventory.covered`` parses (§11.6 covered-span
    semantics are timestamp-based for NEWS)."""
    return [{
        "source_kind": "NEWS",
        "ticker": ticker,
        "span_start": _date_to_utc_midnight(start),
        "span_end": _date_to_utc_end_of_day(end),
        "verified": True,
        "manifest_version": manifest_version,
    }]


def earnings_manifest_rows(*, ticker: str, start: _dt.date, end: _dt.date,
                           manifest_version: str) -> list[dict]:
    """Verified EARNINGS covered-span attestation for a completed sweep
    (ETFs have no earnings events; a zero-event span is still verified
    coverage of the calendar query). Same timestamp form as NEWS rows so
    one manifest reader serves both source kinds."""
    return [{
        "source_kind": "EARNINGS",
        "ticker": ticker,
        "span_start": _date_to_utc_midnight(start),
        "span_end": _date_to_utc_end_of_day(end),
        "verified": True,
        "manifest_version": manifest_version,
    }]


def _date_to_utc_midnight(d: _dt.date) -> str:
    return _dt.datetime(d.year, d.month, d.day,
                        tzinfo=_dt.timezone.utc).isoformat()


def _date_to_utc_end_of_day(d: _dt.date) -> str:
    return _dt.datetime(d.year, d.month, d.day, 23, 59, 59,
                        tzinfo=_dt.timezone.utc).isoformat()
