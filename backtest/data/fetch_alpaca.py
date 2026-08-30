"""R2.7 Phase-0 — Alpaca market-data ingestion (bars + corporate actions).

Grounded strictly in the published Alpaca Market Data API:

- Bars: ``GET {DATA_BASE}/v2/stocks/{symbol}/bars`` with
  ``timeframe`` (``1Min`` / ``1Day``), ``start`` / ``end`` (RFC-3339 or
  YYYY-MM-DD), ``adjustment`` (``raw`` = executable series, ``split`` =
  signal series; ``all``/total-return is reporting-only), ``feed=sip``
  (§3.5 feed-parity hard requirement), ``limit`` + ``next_page_token``
  pagination. Response items carry ``t`` (bar start, RFC-3339), ``o,h,l,c,v``.
- Corporate actions: ``GET {DATA_BASE}/v1/corporate-actions`` with
  ``symbols`` (comma list), ``types`` (``forward_split,reverse_split,
  cash_dividend``), ``start`` / ``end`` (process_date interval), ``limit``
  + ``next_page_token``. Response shape:
  ``{"corporate_actions": {"cash_dividends": [...], "forward_splits":
  [...], "reverse_splits": [...]}, "next_page_token": ...}``;
  dividend items carry ``symbol, ex_date, record_date, payable_date,
  rate``; split items carry ``symbol, ex_date, record_date,
  payable_date, new_rate, old_rate``.

The free tier is rate-limited (spec §3.1: 200 calls/min) — the caller
paces; this module only fails closed on exhaustion.

Coverage honesty (FP-5): the corporate-actions manifest produced here
attests ONLY the spans actually queried with a successful response —
``verified`` reflects a successful, complete pagination sweep over the
requested span, nothing more. Provider-side completeness (whether Alpaca
truly covers 2018+ for every ticker) remains MUST CONFIRM (§3.1/§3.6) —
this job records what the provider returned, it does not certify the
provider.

No secrets are persisted; credentials are read via
``hermes_cli.config.get_env_value`` and used only in request headers.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from zoneinfo import ZoneInfo

from trading_core.types import (
    ADJUSTMENT_RAW,
    ADJUSTMENT_SPLIT,
    ADJUSTMENT_TOTAL,
    FEED_SIP,
    TIMEFRAME_1DAY,
    TIMEFRAME_1MIN,
)

from backtest.data.ingest_core import (
    FetchLog,
    FetchRecord,
    IngestionError,
    IngestStore,
    _now_utc_iso,
    fetch_json,
)

DATA_BASE = "https://data.alpaca.markets"

# Adjustment mapping to the provider vocabulary (§3.3): signal = split,
# executable = raw, accounting = all (total-return, reporting-only).
_ALPACA_ADJUSTMENT = {
    ADJUSTMENT_SPLIT: "split",
    ADJUSTMENT_RAW: "raw",
    ADJUSTMENT_TOTAL: "all",
}

# Default page size for bars requests (API max 10000).
BARS_PAGE_LIMIT = 10000
# Corporate-actions page limit (API max 1000).
CA_PAGE_LIMIT = 1000

ET = ZoneInfo("America/New_York")


class CredentialsMissing(IngestionError):
    """ALPACA_API_KEY / ALPACA_API_SECRET are not configured (§20 Phase 0
    credential prerequisite). Fail-closed: the job refuses to run."""


def _alpaca_headers() -> dict[str, str]:
    from hermes_cli.config import get_env_value
    key = get_env_value("ALPACA_API_KEY")
    secret = get_env_value("ALPACA_API_SECRET")
    if not key or not secret:
        raise CredentialsMissing(
            "ALPACA_API_KEY / ALPACA_API_SECRET are not configured "
            "(~/.hermes/.env); the Alpaca ingestion job refuses to run")
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
    }


def _paginate(url: str, *, headers: dict[str, str],
              base_params: dict[str, Any],
              http_get: Callable[..., tuple[int, str]] | None,
              fetch_log: FetchLog, provider: str) -> list[dict]:
    """Follow next_page_token to exhaustion (fail-closed: pagination that
    errors mid-sweep aborts the whole job; partial spans are never
    recorded as verified)."""
    items: list[dict] = []
    params = dict(base_params)
    pages = 0
    while True:
        doc = fetch_json(url, headers=headers, params=params,
                         http_get=http_get)
        pages += 1
        if not isinstance(doc, dict):
            raise IngestionError(f"{url}: unexpected non-object response")
        if "corporate_actions" in doc:
            items.append(doc["corporate_actions"] or {})
            token = doc.get("next_page_token")
        elif "bars" in doc:
            items.extend(doc.get("bars") or [])
            token = doc.get("next_page_token")
        else:
            raise IngestionError(f"{url}: unexpected response shape")
        if not token:
            break
        params = dict(base_params)
        params["page_token"] = token
    fetch_log.add(FetchRecord(
        provider=provider, endpoint=url, params=dict(base_params),
        fetched_at=_now_utc_iso(), items=len(items), pages=pages))
    return items


def fetch_bars(
    *,
    ticker: str,
    timeframe: str,
    adjustment: str,
    start: _dt.date,
    end: _dt.date,
    feed: str = FEED_SIP,
    http_get: Callable[..., tuple[int, str]] | None = None,
    fetch_log: FetchLog | None = None,
) -> list[dict]:
    """Fetch one ticker's bars for one adjustment, paginated.

    Returns rows shaped for ``IngestStore.upsert_bars``:
    ``{ticker, ts_label_start, o, h, l, c, v, feed, timeframe,
    adjustment}``. ``ts_label_start`` is the provider bar-start timestamp
    (N-01) in UTC ISO-8601. A non-SIP feed is rejected here — §3.5 bars
    must be SIP, and IEX bars must never reach decision logic.
    """
    if feed != FEED_SIP:
        raise IngestionError(
            f"feed={feed!r} rejected: R2.7 §3.5 requires consolidated SIP "
            "bars for every decision-consumed bar")
    if adjustment not in _ALPACA_ADJUSTMENT:
        raise IngestionError(f"unknown adjustment {adjustment!r}")
    url = f"{DATA_BASE}/v2/stocks/{ticker}/bars"
    params = {
        "timeframe": "1Min" if timeframe == TIMEFRAME_1MIN else "1D",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "adjustment": _ALPACA_ADJUSTMENT[adjustment],
        "feed": feed,
        "limit": BARS_PAGE_LIMIT,
    }
    raw = _paginate(url, headers=_alpaca_headers(), base_params=params,
                    http_get=http_get,
                    fetch_log=fetch_log or FetchLog(),
                    provider="alpaca")
    rows: list[dict] = []
    for b in raw:
        rows.append({
            "ticker": ticker,
            "ts_label_start": b["t"],
            "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"],
            "feed": feed,
            "timeframe": timeframe,
            "adjustment": adjustment,
        })
    return rows


@dataclass(frozen=True)
class CorpActionsResult:
    """One ticker's corporate-action sweep + the span it attests."""

    ticker: str
    start: _dt.date
    end: _dt.date
    events: list[dict]     # rows for IngestStore.upsert_corp_actions
    sweep_complete: bool   # True iff pagination ran to exhaustion


def fetch_corporate_actions(
    *,
    ticker: str,
    start: _dt.date,
    end: _dt.date,
    corp_actions_version: str,
    http_get: Callable[..., tuple[int, str]] | None = None,
    fetch_log: FetchLog | None = None,
) -> CorpActionsResult:
    """Fetch splits + cash dividends for one ticker over [start, end].

    Only the §3.6-relevant types are requested: ``forward_split``,
    ``reverse_split`` (split ratio = new_rate / old_rate, per the
    provider's documented rate semantics), and ``cash_dividend`` (rate =
    cash amount per share). Stock dividends, spin-offs, mergers, name
    changes etc. are NOT part of the §3.6 contract and are not ingested.

    The sweep's observed window is [start, end] — the manifest the caller
    writes attests exactly this queried span, not the provider's wider
    history.
    """
    url = f"{DATA_BASE}/v1/corporate-actions"
    params = {
        "symbols": ticker,
        "types": "forward_split,reverse_split,cash_dividend",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "limit": CA_PAGE_LIMIT,
    }
    log = fetch_log or FetchLog()
    pages = _paginate(url, headers=_alpaca_headers(), base_params=params,
                      http_get=http_get, fetch_log=log, provider="alpaca")
    events: list[dict] = []
    for page in pages:
        ca = page if isinstance(page, dict) else {}
        for div in ca.get("cash_dividends") or []:
            events.append(_dividend_row(ticker, div, corp_actions_version))
        for split in (ca.get("forward_splits") or []) + \
                (ca.get("reverse_splits") or []):
            events.append(_split_row(ticker, split, corp_actions_version))
    return CorpActionsResult(
        ticker=ticker, start=start, end=end, events=events,
        sweep_complete=True)


def _parse_date(value: Any) -> _dt.date | None:
    if not value:
        return None
    return _dt.date.fromisoformat(str(value))


def _dividend_row(ticker: str, item: dict, version: str) -> dict:
    rate = item.get("rate")
    if rate is None:
        raise IngestionError(
            f"cash_dividend for {ticker} without rate: {item}")
    return {
        "ticker": ticker,
        "event_type": "CASH_DIVIDEND",
        "ex_date": item["ex_date"],
        "split_ratio": None,
        "cash_amount_per_share": str(Decimal(str(rate))),
        "record_date": _iso_or_none(item.get("record_date")),
        "pay_date": _iso_or_none(item.get("payable_date")),
        "corp_actions_version": version,
    }


def _split_row(ticker: str, item: dict, version: str) -> dict:
    try:
        new_rate = Decimal(str(item["new_rate"]))
        old_rate = Decimal(str(item["old_rate"]))
    except (KeyError, InvalidOperation) as exc:
        raise IngestionError(
            f"split for {ticker} without new_rate/old_rate: {item}") from exc
    if old_rate == 0:
        raise IngestionError(f"split for {ticker} has old_rate 0: {item}")
    # Ratio = new shares per old share (a 2-for-1 split has ratio 2).
    return {
        "ticker": ticker,
        "event_type": "SPLIT",
        "ex_date": item["ex_date"],
        "split_ratio": str(new_rate / old_rate),
        "cash_amount_per_share": None,
        "record_date": _iso_or_none(item.get("record_date")),
        "pay_date": _iso_or_none(item.get("payable_date")),
        "corp_actions_version": version,
    }


def _iso_or_none(value: Any) -> str | None:
    d = _parse_date(value)
    return d.isoformat() if d else None


def corp_actions_manifest_rows(
    result: CorpActionsResult, *, manifest_version: str) -> list[dict]:
    """FP-5 verified attestation rows for a completed sweep. A verified
    attestation over a span with no events IS the verified-zero state
    (§3.6 item 4) — it is written identically."""
    if not result.sweep_complete:
        return []  # fail-closed: incomplete sweep attests nothing
    return [{
        "source_kind": "CORP_ACTIONS",
        "ticker": result.ticker,
        "span_start": result.start.isoformat(),
        "span_end": result.end.isoformat(),
        "verified": True,
        "manifest_version": manifest_version,
    }]
