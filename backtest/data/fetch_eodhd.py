"""R2.8 §3.7 — EODHD historical-earnings ingestion (substitutable
earnings source, G6 inputs).

Grounded strictly in the audited EODHD earnings-calendar contract
(skill reference ``eodhd-earnings-contract.md``, probed 2019-01-01 ..
2025-12-31 over the frozen 20-stock universe):

- Endpoint: ``GET /api/calendar/earnings`` with ``symbols=<TICKER>.US``,
  ``from``/``to`` date bounds, ``fmt=json``, and the credential in the
  ``api_token`` query parameter. Per-symbol fetches only — never the
  whole US market.
- Wrapper shape: rows live under ``payload["earnings"]`` — a top-level
  object wrapper, NOT a bare list.
- Canonical event date = ``report_date``. The ``date`` field is the
  fiscal-period date and MUST NOT be used for G6 (§3.7).
- Ticker = ``code``, normalized by stripping exactly the ``.US``
  suffix (``AAPL.US`` → ``AAPL``) in this adapter only. Malformed
  symbols — no suffix, unexpected suffix, empty/null — are
  deterministic rejections, never guesses.

Timing normalization (§3.7 C-1 — no silent UNSPECIFIED):

===========================  =======================
EODHD ``before_after_market``  Canonical G6 timing
===========================  =======================
``"AfterMarket"``            ``after-market-close``
``"BeforeMarket"``           ``before-market-open``
``null``/missing             ``unspecified``
anything else non-null       deterministic failure
===========================  =======================

A known-but-different timing value (e.g. ``"DuringMarket"``) is
NON-COMPLIANT, not ``unspecified``. Only a genuinely null/missing
provider value maps to ``unspecified``. (The Finnhub adapter predates
C-1 and maps ``dmh`` → ``unspecified``; do not copy that behavior.)

Coverage honesty (§3.7 / §11.6): manifests attest ONLY the spans
actually queried with a successful response. A zero-row earnings
array inside a completed sweep is still a verified covered span;
missing rows are reported, never repaired or invented. Observed
provider quirks (e.g. COST returning 26 rows where every other symbol
returns 28) are surfaced as diagnostics, not backfilled.

No secrets are persisted: EODHD_API_KEY is read via
``hermes_cli.config.get_env_value`` and used only as a query param.
Because the credential rides in the URL, error text is redacted —
``api_token`` values never reach logs, manifests, or stored reports.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Callable

from backtest.data.ingest_core import (
    FetchLog,
    FetchRecord,
    IngestionError,
    _now_utc_iso,
    fetch_json,
)


def _date_to_utc_midnight(d: _dt.date) -> str:
    return _dt.datetime(d.year, d.month, d.day,
                        tzinfo=_dt.timezone.utc).isoformat()


def _date_to_utc_end_of_day(d: _dt.date) -> str:
    return _dt.datetime(d.year, d.month, d.day, 23, 59, 59,
                        tzinfo=_dt.timezone.utc).isoformat()

EODHD_BASE = "https://eodhd.com/api"

# EODHD date-range requests are capped by the provider; sweep in
# windows small enough to stay well inside any documented limit
# (mirrors the Finnhub earnings-window discipline).
EARNINGS_WINDOW_DAYS = 90

# EODHD `before_after_market` vocabulary → §7.2 G6 timing vocabulary.
# EXACTLY these three keys: any other non-null value is a
# deterministic IngestionError (§3.7 C-1 — never a silent UNSPECIFIED).
_BEFORE_AFTER_MAP = {
    "AfterMarket": "after-market-close",
    "BeforeMarket": "before-market-open",
}


class CredentialsMissing(IngestionError):
    """EODHD_API_KEY is not configured (§20 Phase 0 credential
    prerequisite). Fail-closed: the job refuses to run."""


def _redact_token(text: Any) -> str:
    """Strip an ``api_token=…`` query value from error/URL text before
    it can reach a log line or a stored report."""
    s = str(text)
    if "api_token=" in s:
        head, _, _tail = s.partition("api_token=")
        s = head + "api_token=<redacted>"
    return s


def _eodhd_params() -> dict[str, str]:
    from hermes_cli.config import get_env_value
    token = get_env_value("EODHD_API_KEY")
    if not token:
        raise CredentialsMissing(
            "EODHD_API_KEY is not configured (~/.hermes/.env); the "
            "EODHD earnings ingestion job refuses to run")
    return {"api_token": token, "fmt": "json"}


def _normalize_symbol(code: Any) -> str:
    """``AAPL.US`` → ``AAPL`` in this adapter only. Malformed symbols —
    missing suffix, unexpected suffix, empty, non-string, embedded
    whitespace — are deterministic rejections, never guesses."""
    if not isinstance(code, str):
        raise IngestionError(f"EODHD earnings row has non-string code: {code!r}")
    symbol = code.strip()
    if not symbol:
        raise IngestionError("EODHD earnings row has empty code")
    if not symbol.endswith(".US"):
        raise IngestionError(
            f"EODHD earnings code {code!r} is missing the expected "
            f"'.US' suffix — refusing to guess the ticker")
    ticker = symbol[:-3]
    if not ticker or "." in ticker or " " in ticker:
        raise IngestionError(
            f"EODHD earnings code {code!r} is not a well-formed "
            f"'<TICKER>.US' symbol — refusing to guess the ticker")
    return ticker


def _normalize_timing(code: Any, before_after_market: Any) -> str:
    """Map EODHD ``before_after_market`` onto the G6 timing vocabulary.

    ``None`` → ``unspecified``; the two known values map to their G6
    strings; ANY other non-null value is a deterministic failure
    (§3.7 C-1 — a known-but-different timing value is NON-COMPLIANT,
    not unspecified).
    """
    if before_after_market is None:
        return "unspecified"
    if not isinstance(before_after_market, str):
        raise IngestionError(
            f"EODHD earnings {code!r} has non-string "
            f"before_after_market: {before_after_market!r}")
    timing = _BEFORE_AFTER_MAP.get(before_after_market)
    if timing is None:
        raise IngestionError(
            f"EODHD earnings {code!r} has non-compliant "
            f"before_after_market {before_after_market!r}; known values "
            f"are AfterMarket/BeforeMarket/null (R2.8 §3.7 C-1) — "
            f"refusing to silently map to unspecified")
    return timing


def _parse_report_date(code: Any, report_date: Any) -> str:
    """Canonical earnings event date = ``report_date`` (§3.7). The
    fiscal-period ``date`` field is deliberately ignored. Missing or
    malformed report_date fails closed — an event date is never
    guessed from another field."""
    if not isinstance(report_date, str) or not report_date.strip():
        raise IngestionError(
            f"EODHD earnings {code!r} is missing report_date — the "
            f"canonical event date cannot be invented (R2.8 §3.7)")
    try:
        return _dt.date.fromisoformat(report_date.strip()).isoformat()
    except ValueError as exc:
        raise IngestionError(
            f"EODHD earnings {code!r} has malformed report_date "
            f"{report_date!r}: {exc}") from exc


def normalize_earnings_payload(payload: Any) -> list[dict]:
    """Normalize one EODHD wrapper payload ``{"earnings": [...]}`` into
    §7.2 G6 ``EarningsEvent``-compatible event dicts:
    ``{ticker, event_date, timing}``.

    Deterministic validation failures (unknown timing, malformed
    symbol, missing report_date) raise :class:`IngestionError`. An
    empty ``earnings`` array stays empty — no events are invented.
    """
    if not isinstance(payload, dict):
        raise IngestionError(
            f"EODHD earnings payload must be an object wrapper with an "
            f"'earnings' key, got {type(payload).__name__}")
    earnings = payload.get("earnings")
    if earnings is None:
        raise IngestionError(
            "EODHD earnings payload is missing the 'earnings' array")
    if not isinstance(earnings, list):
        raise IngestionError(
            f"EODHD earnings payload 'earnings' must be an array, got "
            f"{type(earnings).__name__}")
    events: list[dict] = []
    for item in earnings:
        if not isinstance(item, dict):
            raise IngestionError(
                f"EODHD earnings row must be an object, got "
                f"{type(item).__name__}")
        code = item.get("code")
        ticker = _normalize_symbol(code)
        timing = _normalize_timing(code, item.get("before_after_market"))
        event_date = _parse_report_date(code, item.get("report_date"))
        events.append({
            "ticker": ticker,
            "event_date": event_date,
            "timing": timing,
        })
    return events


def fetch_earnings_calendar(
    *,
    ticker: str,
    start: _dt.date,
    end: _dt.date,
    http_get: Callable[..., tuple[int, str]] | None = None,
    fetch_log: FetchLog | None = None,
) -> list[dict]:
    """Sweep the EODHD earnings calendar for one ticker over
    [start, end] in provider-safe windows.

    Returns event dicts shaped for §7.2 G6 (``EarningsEvent``-
    compatible): ``{ticker, event_date, timing}`` where timing is the
    G6 vocabulary (``before-market-open`` / ``after-market-close`` /
    ``unspecified``) and ``event_date`` is the provider
    ``report_date``. The fiscal-period ``date`` field is never used.

    The ``api_token`` credential is redacted from any transport error
    text before it propagates (the key rides in the URL query string).
    """
    log = fetch_log or FetchLog()
    events: list[dict] = []
    fetched_at = _now_utc_iso()
    base_params = _eodhd_params()  # fail closed before any request
    window_start = start
    while window_start <= end:
        window_end = min(
            window_start + _dt.timedelta(days=EARNINGS_WINDOW_DAYS - 1), end)
        try:
            payload = fetch_json(
                f"{EODHD_BASE}/calendar/earnings",
                params={**base_params,
                        "symbols": f"{ticker}.US",
                        "from": window_start.isoformat(),
                        "to": window_end.isoformat()},
                http_get=http_get)
        except IngestionError as exc:
            raise IngestionError(_redact_token(exc)) from exc
        window_events = normalize_earnings_payload(payload)
        # Cross-check: rows for a different symbol than requested are a
        # provider-shape anomaly, not silently-ingested data.
        for event in window_events:
            if event["ticker"] != ticker:
                raise IngestionError(
                    f"EODHD earnings response for {ticker}.US contains row "
                    f"for {event['ticker']}.US — refusing to ingest "
                    f"cross-symbol rows")
        events.extend(window_events)
        log.add(FetchRecord(
            provider="eodhd", endpoint="calendar/earnings",
            params={"symbols": f"{ticker}.US",
                    "from": window_start.isoformat(),
                    "to": window_end.isoformat()},
            fetched_at=fetched_at, items=len(window_events)))
        window_start = window_end + _dt.timedelta(days=1)
    return events


def earnings_manifest_rows(*, ticker: str, start: _dt.date, end: _dt.date,
                           manifest_version: str) -> list[dict]:
    """Verified EARNINGS covered-span attestation for a completed sweep
    (a zero-event span is still verified coverage of the calendar
    query — absence of provider rows is reported, never repaired).
    Same timestamp form as the Finnhub rows so one manifest reader
    serves every earnings source_kind='EARNINGS' provider."""
    return [{
        "source_kind": "EARNINGS",
        "ticker": ticker,
        "span_start": _date_to_utc_midnight(start),
        "span_end": _date_to_utc_end_of_day(end),
        "verified": True,
        "manifest_version": manifest_version,
    }]
