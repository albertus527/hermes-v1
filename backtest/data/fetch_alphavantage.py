"""R2.8.1 §3.8 — Alpha Vantage historical NEWS adapter (substitutable
historical news source under the §3.8 provider-neutral contract).

Grounded in the established bounded NEWS_SENTIMENT probe (AAPL,
2019-01-01 .. 2019-01-31, 26 feed rows; the real payload stays outside
the repo under the managed root — synthetic fixtures only in tests):

- Endpoint: ``GET https://www.alphavantage.co/query`` with
  ``function=NEWS_SENTIMENT``, ``tickers=<T>``, ``time_from`` /
  ``time_to`` REQUEST bounds (``YYYYMMDDTHHMM`` minute resolution —
  see ``_window_stamp``) and the credential in the ``apikey`` query
  parameter. (The RESPONSE field ``time_published`` is
  ``YYYYMMDDTHHMMSS`` — a different, response-side format.)
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

Completeness / saturation contract (§3.8 N-1-f — the deterministic
mechanism the production ingestion job attests coverage under):

The official Alpha Vantage NEWS_SENTIMENT contract (verified against
the live provider documentation) states: "By default, limit=50 and the
API will return up to 50 matching results. You can also set limit=1000
to output up to 1000 results." — the provider returns ALL matching
results for the requested window up to the requested limit, and there
is NO continuation token. The 2026-08-29 capability probe confirmed
the credential honors explicit limits above the default 50 (a
``limit=1000`` request returned 288 articles, i.e. fewer than
requested — all matches returned).

Therefore every window request carries an EXPLICIT
``limit=NEWS_WINDOW_LIMIT`` (1000, the provider maximum), and:

    len(feed) < 1000  → the window is COMPLETE under the provider
                        contract (all matching results returned);
    len(feed) >= 1000 → the window is SATURATED — completeness is NOT
                        established (results may have been truncated
                        at the provider maximum) and the sweep fails
                        closed with :class:`WindowSaturatedError`.

A saturated window is NEVER silently attested as verified coverage,
and never treated as a successful zero/partial result (§3.8 N-1-f:
provider response-size limits must not silently create verified
coverage). A 30-day window holding 1000+ associated articles is far
outside the observed provider density (26/288 items per month/year
for AAPL), so saturation indicates a provider-contract change rather
than genuine news volume; either way the span stays unverified.

IMPLEMENTATION BEHAVIOR (annual-first adaptive subdivision — the
canonical specification prescribes no provider-specific chunk sizes):
a saturated window is NOT an immediate failure. The requested range is
first partitioned by CALENDAR YEAR; a window returning exactly the
provider maximum is deterministically bisected into two contiguous,
non-overlapping, gap-free child intervals (minute-granularity exact
instants — the YYYYMMDDTHHMM request-stamp precision) and each child
is fetched recursively until every leaf returns fewer than the limit.
If an interval below the minimum minute granularity still returns the
maximum, the sweep FAILS CLOSED: the span stays unverified and must
not receive a verified coverage manifest row.

No secrets are persisted: ALPHAVANTAGE_API_KEY is read via
``hermes_cli.config.get_env_value`` (the optional-skills
``ALPHA_VANTAGE_KEY`` variable is deliberately NOT read) and used only
as a query parameter; ``apikey`` values are redacted from any error
text before it propagates.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from trading_core.news_effects import headline_hash as compute_headline_hash
from trading_core.news_effects import normalize_headline_text

from backtest.data.ingest_core import (
    DROP_INVALID_FP4_EMPTY,
    FetchLog,
    FetchRecord,
    IngestionError,
    _now_utc_iso,
    fetch_json,
)

ALPHAVANTAGE_BASE = "https://www.alphavantage.co/query"

# Explicit per-window result limit (the provider-documented maximum).
# The official contract: "You can also set limit=1000 to output up to
# 1000 results" — all matching results up to the limit are returned in
# one response, with no continuation token. A feed of exactly this
# length is SATURATED (completeness unestablished) — see the module
# docstring.
NEWS_WINDOW_LIMIT = 1000

# Rate-limit pacing: minimum delay (seconds) between actual Alpha
# Vantage HTTP requests. Requests are strictly sequential (no parallel
# fetching); the sleeper is injectable via ``fetch_news_inventory(
# sleep_fn=...)`` (unit tests inject a no-op recorder and never sleep).
# The module-level ``_sleep`` is the CLI default and may be monkeypatched
# in CLI-level hermetic tests.
NEWS_PACING_SECONDS = 1.0
_sleep = time.sleep

# ---------------------------------------------------------------------------
# Durable resume checkpoints (implementation state only — NOT canonical
# coverage evidence). One JSON artifact per completed UNSATURATED leaf
# interval, keyed by a deterministic identity over (request-contract
# version, ticker, exact leaf start/end minute stamps). NEVER contains
# credentials. Written atomically (temp + fsync + rename via the shared
# ``utils.atomic_write_text``), so an interrupted write leaves at most a
# ``.tmp_*`` file that is never read as a completed checkpoint. A
# checkpoint records that the leaf was successfully fetched, validated,
# normalized, and is reusable — it does NOT by itself create or imply
# any coverage_manifests row; canonical coverage still requires the
# complete adaptive traversal of the requested span to succeed.
# ---------------------------------------------------------------------------
CHECKPOINT_FORMAT = "alphavantage-news-resume-1"


def _checkpoint_dir() -> "Path":
    from hermes_constants import get_hermes_home
    return (Path(get_hermes_home()) / "data" / "r28" / "alphavantage" /
            "resume")


def _checkpoint_identity(ticker: str, a: _dt.datetime,
                         b: _dt.datetime) -> str:
    """Deterministic checkpoint identity: request-contract version,
    ticker, and the EXACT minute-precision leaf bounds. No API key or
    secret material participates. A different contract version, ticker,
    or interval yields a different identity (safe-refetch on mismatch)."""
    payload = "|".join([
        CHECKPOINT_FORMAT, ticker.strip().upper(),
        _instant_stamp(a), _instant_stamp(b)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _checkpoint_path(ticker: str, a: _dt.datetime, b: _dt.datetime,
                     checkpoint_dir: Any) -> "Path":
    return Path(checkpoint_dir) / f"{_checkpoint_identity(ticker, a, b)}.json"


def _save_checkpoint(path: "Path", *, ticker: str, a: _dt.datetime,
                     b: _dt.datetime, rows: list[dict],
                     drops: int = 0) -> None:
    """Atomically persist a COMPLETED (unsaturated, validated, fully
    normalized) leaf checkpoint. ``complete: true`` is written only in
    the same atomic rename as the rows, so a crash mid-write can never
    produce a readable completed checkpoint.

    ``drops`` records the deterministic canonical-exclusion count for
    DROP_INVALID_FP4_EMPTY observed while normalizing this leaf's raw
    provider feed (P-NEWS-EMPTY observability). Only the COUNT is
    persisted — never the dropped raw provider item, whose canonical
    content does not exist (no row, no hash). The count must survive
    checkpoint/resume: a later run that replays this leaf re-reports
    the exclusions represented by the inventory it consumed.
    """
    from utils import atomic_write_text
    doc = {
        "format": CHECKPOINT_FORMAT,
        "complete": True,
        "ticker": ticker,
        "time_from": _instant_stamp(a),
        "time_to": _instant_stamp(b),
        "rows": rows,
        "drops": {"DROP_INVALID_FP4_EMPTY": int(drops)},
    }
    atomic_write_text(path, json.dumps(doc, sort_keys=True))


def _load_checkpoint(path: "Path", *, ticker: str, a: _dt.datetime,
                     b: _dt.datetime) -> tuple[list[dict], int] | None:
    """Return ``(stored canonical rows, DROP_INVALID_FP4_EMPTY count)``
    iff the artifact is a COMPLETE, structurally valid checkpoint for
    EXACTLY this (contract version, ticker, interval). Anything else —
    missing, malformed JSON, incomplete, identity mismatch, non-object
    rows — is ignored (fail-safe refetch), never treated as a completed
    leaf.

    Legacy checkpoints written before drop-metadata persistence carry no
    ``drops`` field; they remain fully readable and replay with a drop
    count of 0 (the exclusion evidence of a legacy leaf is unknowable
    without re-querying the provider, which replay must never do).
    """
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    if (doc.get("format") != CHECKPOINT_FORMAT
            or doc.get("complete") is not True
            or doc.get("ticker") != ticker
            or doc.get("time_from") != _instant_stamp(a)
            or doc.get("time_to") != _instant_stamp(b)):
        return None
    rows = doc.get("rows")
    if not isinstance(rows, list):
        return None
    if not all(isinstance(r, dict) for r in rows):
        return None
    drops_doc = doc.get("drops")
    fp4_drops = 0
    if isinstance(drops_doc, dict):
        value = drops_doc.get(DROP_INVALID_FP4_EMPTY)
        if isinstance(value, int) and not isinstance(value, bool) \
                and value >= 0:
            fp4_drops = value
    return rows, fp4_drops


def _save_saturation_marker(path: "Path", *, ticker: str,
                            a: _dt.datetime, b: _dt.datetime) -> None:
    """Atomically persist a SATURATION marker for this exact
    (contract version, ticker, interval). A marker is NOT a completed
    checkpoint and NOT coverage evidence: it records only that a
    successful, validated response for this exact node returned the
    provider's documented maximum, so the node must be deterministically
    subdivided. The truncated saturated feed rows are never stored."""
    from utils import atomic_write_text
    doc = {
        "format": CHECKPOINT_FORMAT,
        "saturated": True,
        "ticker": ticker,
        "time_from": _instant_stamp(a),
        "time_to": _instant_stamp(b),
    }
    atomic_write_text(path, json.dumps(doc, sort_keys=True))


def _load_saturation_marker(path: "Path", *, ticker: str,
                            a: _dt.datetime, b: _dt.datetime) -> bool:
    """True iff the artifact is a structurally valid SATURATION marker
    for EXACTLY this (contract version, ticker, interval). Anything
    else — missing, malformed, identity/version mismatch — is ignored
    (fail-safe refetch), never trusted."""
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(doc, dict):
        return False
    return (doc.get("format") == CHECKPOINT_FORMAT
            and doc.get("saturated") is True
            and doc.get("ticker") == ticker
            and doc.get("time_from") == _instant_stamp(a)
            and doc.get("time_to") == _instant_stamp(b)
            and "rows" not in doc
            and doc.get("complete") is not True)

# Provider informational/error envelope keys — a 200 body carrying one
# of these is a provider notice, never a successful empty feed.
_ENVELOPE_KEYS = ("Information", "Note", "Error Message")

# Naive provider timestamp: exactly YYYYMMDDTHHMMSS (20190101T000000).
_TIME_PUBLISHED_RE = re.compile(r"^\d{8}T\d{6}$")


class CredentialsMissing(IngestionError):
    """ALPHAVANTAGE_API_KEY is not configured (§20 Phase 0 credential
    prerequisite). Fail-closed: the job refuses to run."""


class WindowSaturatedError(IngestionError):
    """A window response returned at least NEWS_WINDOW_LIMIT items —
    the provider may have truncated at its documented maximum, so
    completeness for that window is NOT established (§3.8 N-1-f). The
    sweep fails closed; the affected ticker/span stays unverified and
    MUST NOT receive a verified coverage manifest row."""


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


def _canonicalize_rows(rows: list[dict]) -> list[dict]:
    """Deterministically canonicalize a batch of rows from ONE Alpha Vantage
    fetched interval by collapsing intra-interval duplicates by canonical
    identity (headline_hash, source, ticker).

    Point-in-time safety rule: when multiple occurrences of the same canonical
    identity carry different published_at values but the same
    headline_text_normalized, the row with MAX(published_at) is retained — a
    deterministic conservative rule that never introduces a headline earlier
    than any timestamp observed for that canonical identity. This is an
    anti-lookahead canonicalization, not a "correct timestamp" selection.

    Content conflicts (same identity, same source/ticker, but differing
    headline_text_normalized) fail closed — they are never silently resolved.

    Single-occurrence identities and exact-duplicate identities (same
    published_at AND same headline_text_normalized) are preserved/collapsed
    unchanged.

    Different (headline_hash, source, ticker) tuples are separate identities
    and never merged.

    This function operates on rows ALREADY constructed from the provider feed;
    it does not re-parse the feed, does not consult provider sentiment/relevance
    fields, and does not distinguish tickers or sources.
    """
    # Group by canonical identity: (headline_hash, source, ticker)
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for r in rows:
        key = (r["headline_hash"], r["source"], r["ticker"])
        groups.setdefault(key, []).append(r)

    out: list[dict] = []
    for key, group in groups.items():
        if len(group) == 1:
            out.append(group[0])
            continue

        # Multiple occurrences for the same identity.
        texts = {r["headline_text_normalized"] for r in group}
        if len(texts) > 1:
            raise IngestionError(
                f"Alpha Vantage intra-interval duplicate for identity "
                f"{key} has differing headline_text_normalized values "
                f"{texts!r} — content conflict cannot be resolved "
                f"deterministically (R2.8.1 §3.8 N-1-a)"
            )

        # Same normalized text. Collapse to the row with MAX(published_at).
        # Timestamps are aware ISO-8601 UTC strings; lexicographic order
        # matches chronological order for this format.
        canonical = max(group, key=lambda r: r["published_at"])
        out.append(canonical)

    return out


def _rows_from_feed(feed: list, *, ticker: str,
                    fetched_at: str | None = None) -> tuple[list[dict], int]:
    """Normalize validated feed items into canonical ``news_headlines``
    rows for the requested ticker. Structurally malformed items fail
    closed; well-formed items not associated with the requested ticker
    are skipped (never a row for a ticker the article does not
    establish). No provider sentiment/relevance field is carried over.

    After row construction, intra-interval duplicates by canonical identity
    (headline_hash, source, ticker) are deterministically canonicalized:

    - Exact duplicates (same published_at + same headline_text_normalized)
      collapse to one row.
    - Same identity + same normalized text + different published_at:
      the row with MAX(published_at) is retained (point-in-time safety;
      never introduces an earlier timestamp than any observed for that
      identity).
    - Same identity + different headline_text_normalized: IngestionError
      (content conflict, fail closed).

    P-NEWS-EMPTY (DROP_INVALID_FP4_EMPTY): a structurally valid item whose
    ``normalize_headline_text(title)`` returns empty is DROPPED — it
    produces no canonical row, no IngestionError, no invented text from
    summary/url/etc. The drop is counted deterministically and observable
    via the enclosing FetchRecord.

    This canonicalization is Alpha-Vantage-adapter-local and is applied
    AFTER row construction but BEFORE the rows reach checkpoint persistence
    or the aggregate inventory.
    """
    fetched_at = fetched_at or _now_utc_iso()
    rows: list[dict] = []
    drops = 0
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
            # P-NEWS-EMPTY: provider supplied a structurally valid,
            # non-empty, non-whitespace title, but FP-4 normalization
            # yields empty text (e.g. punctuation-only). DROP_INVALID_FP4_EMPTY.
            drops += 1
            continue
        rows.append({
            "headline_hash": compute_headline_hash(title),
            "source": source.strip(),
            "ticker": ticker,
            "published_at": _parse_time_published(
                item.get("time_published")),
            "headline_text_normalized": normalized,
            "fetched_at": fetched_at,
        })
    return _canonicalize_rows(rows), drops


def normalize_news_payload(payload: Any, *, ticker: str,
                           fetched_at: str | None = None
                           ) -> tuple[list[dict], int]:
    """Normalize one NEWS_SENTIMENT payload into canonical
    ``news_headlines`` rows for ``ticker`` (shape ready for
    ``IngestStore.upsert_headlines``).

    Deterministic validation failures (envelopes, malformed payload /
    feed / items, missing canonical fields) raise
    :class:`IngestionError`. A structurally valid ``"feed": []`` stays
    zero rows — no headlines are invented. Articles that do not
    establish association with ``ticker`` produce no row.

    Returns ``(rows, drops)`` where ``drops`` is the count of provider
    items dropped because FP-4 normalized text was empty
    (DROP_INVALID_FP4_EMPTY). The invalid items are NOT persisted and do
    NOT by themselves fail the enclosing sweep.
    """
    return _rows_from_feed(
        _parse_feed(payload), ticker=ticker, fetched_at=fetched_at)


def _window_stamp(d: _dt.date, *, end_of_day: bool = False) -> str:
    """Provider-format REQUEST window bound: ``YYYYMMDDTHHMM`` at
    00:00 or 23:59 — the NEWS_SENTIMENT ``time_from`` / ``time_to``
    contract (minute resolution; see the official API documentation).
    This is the REQUEST format only; the response-side
    ``time_published`` field is ``YYYYMMDDTHHMMSS`` and is parsed
    separately by ``_parse_time_published``."""
    t = _dt.time(23, 59) if end_of_day else _dt.time(0, 0)
    return _dt.datetime.combine(d, t).strftime("%Y%m%dT%H%M")


def _instant_stamp(a: _dt.datetime) -> str:
    """Exact minute-resolution REQUEST stamp for an arbitrary aware-UTC
    instant: ``YYYYMMDDTHHMM`` — the same NEWS_SENTIMENT
    ``time_from`` / ``time_to`` contract as ``_window_stamp``, used for
    the adaptive subdivision's exact child boundaries (mid-split
    instants are minute-aligned but not necessarily 00:00/23:59)."""
    return a.strftime("%Y%m%dT%H%M")


# ---------------------------------------------------------------------------
# ANNUAL-FIRST ADAPTIVE SUBDIVISION (implementation behavior only — the
# canonical specification prescribes NO provider-specific chunk sizes)
# ---------------------------------------------------------------------------

# Request timestamp precision: the adapter's ``_window_stamp`` format is
# YYYYMMDDTHHMM — minute resolution. The recursive subdivision below
# therefore operates on MINUTE-granularity aware-UTC intervals; a
# one-minute interval is the smallest safely representable subdivision
# unit. An interval that can no longer be subdivided (span < 2 minutes
# in the minute-stamp representation) and still returns exactly the
# provider maximum rows FAILS CLOSED with :class:`WindowSaturatedError`
# (never truncated, never attested complete, never converted to
# verified-zero).
_MIN_MINUTE_SPAN = 1  # minutes


def _annual_windows(start: _dt.date, end: _dt.date) -> list[tuple[_dt.date,
                                                                  _dt.date]]:
    """Deterministic CALENDAR-YEAR initial partition of the inclusive
    requested date range. Partial first/last years keep the EXACT
    requested boundaries (no rounding outward)."""
    windows: list[tuple[_dt.date, _dt.date]] = []
    year = start.year
    while year <= end.year:
        w_start = max(start, _dt.date(year, 1, 1))
        w_end = min(end, _dt.date(year, 12, 31))
        windows.append((w_start, w_end))
        year += 1
    return windows


def _window_to_datetimes(w: tuple[_dt.date, _dt.date]) -> tuple[_dt.datetime,
                                                                _dt.datetime]:
    """Inclusive date-window -> aware-UTC [start_instant, end_instant]
    internal representation: midnight-UTC of the first day through
    23:59-UTC of the last day (the exact instants the minute-resolution
    REQUEST stamps encode)."""
    start_d, end_d = w
    return (_dt.datetime(start_d.year, start_d.month, start_d.day,
                         tzinfo=_dt.timezone.utc),
            _dt.datetime(end_d.year, end_d.month, end_d.day, 23, 59,
                         tzinfo=_dt.timezone.utc))


def _split_interval(a: _dt.datetime, b: _dt.datetime) -> tuple[_dt.datetime,
                                                              _dt.datetime]:
    """Deterministic, exact bisection of the inclusive instant interval
    [a, b] (aware UTC, minute-aligned) into two contiguous, NON-
    overlapping, gap-free children whose union reconstructs exactly the
    parent:

        left  = [a, m]      right = [m + 1 minute, b]

    with m = a + floor((b - a) / 2 minutes) minutes. The same parent
    always produces the same children; no boundary timestamp is shared
    (the right child starts one minute after the left child ends) and
    no instant inside [a, b] is uncovered (the provider stamps are
    minute-resolution, so consecutive minutes are contiguous).
    """
    span_minutes = round((b - a).total_seconds() // 60)
    if span_minutes < 2 * _MIN_MINUTE_SPAN:
        raise WindowSaturatedError(
            f"interval {a.isoformat()}..{b.isoformat()} cannot be safely "
            f"subdivided below the minimum granularity of "
            f"{_MIN_MINUTE_SPAN} minute(s) (the NEWS_SENTIMENT "
            f"YYYYMMDDTHHMM request-stamp precision)")
    mid = a + _dt.timedelta(minutes=span_minutes // 2)
    return (mid, mid + _dt.timedelta(minutes=1))


def fetch_news_inventory(
    *,
    ticker: str,
    start: _dt.date,
    end: _dt.date,
    http_get: Callable[..., tuple[int, str]] | None = None,
    fetch_log: FetchLog | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    checkpoint_dir: str | None = None,
) -> tuple[list[dict], int]:
    """Sweep Alpha Vantage NEWS_SENTIMENT for one ticker over
    [start, end] using ANNUAL-FIRST ADAPTIVE SUBDIVISION
    (implementation behavior only; the canonical specification
    prescribes no provider-specific chunk sizes), with durable
    quota-safe resume checkpoints.

    Algorithm (deterministic, strictly sequential):

    1. The requested inclusive range is initially partitioned by
       CALENDAR YEAR (partial first/last years keep the exact requested
       boundaries).
    2. Each window is fetched sequentially with an explicit
       ``limit=NEWS_WINDOW_LIMIT`` (the provider-documented maximum,
       no continuation token) and validated by the existing payload
       validation.
    3. ``len(feed) < NEWS_WINDOW_LIMIT`` → the window is UNSATURATED
       (demonstrably complete under the provider contract) and becomes
       a leaf.
    4. ``len(feed) == NEWS_WINDOW_LIMIT`` → the window is SATURATED —
       completeness is NOT established. It is deterministically split
       into two contiguous, non-overlapping, gap-free child intervals
       (minute-granularity exact bisection, see ``_split_interval``)
       and each child is fetched recursively.
    5. Termination: every leaf returns fewer than the limit, OR an
       un-subdividable (below minimum minute granularity) saturated
       interval FAILS CLOSED with :class:`WindowSaturatedError` —
       never silently truncated, never attested complete, never
       converted to verified-zero.

    Resume checkpoints (implementation state only — NOT canonical
    coverage evidence): when ``checkpoint_dir`` is provided, every
    completed UNSATURATED leaf (successful response, validated
    payload, established ticker association/normalization, fewer than
    the limit) is atomically persisted as a resume artifact keyed by
    the request-contract version + ticker + exact minute bounds. A
    later run restores valid completed leaves WITHOUT issuing their
    HTTP request; absent/invalid/mismatched artifacts are ignored and
    safely refetched. Saturated parents/intermediates, failed
    requests, malformed payloads, and provider envelopes are NEVER
    checkpointed. Parent completion remains derived solely from the
    complete adaptive traversal; a checkpoint alone never creates a
    coverage_manifests row and never attests the requested span.

    Only after ALL required leaves succeed is the requested
    ticker/span's sweep complete and its rows returned — the state the
    caller may attest as verified NEWS coverage (§3.8 N-1-f). A
    saturated, malformed, or envelope-bearing response anywhere in the
    adaptive tree fails the whole requested span closed (no partial
    attestation, no verified coverage for the incomplete parent span).

    Rate-limit pacing: requests remain strictly sequential; a
    ``NEWS_PACING_SECONDS`` delay is applied between consecutive HTTP
    requests via the injectable ``sleep_fn`` (default ``time.sleep``;
    tests inject a no-op). A checkpoint HIT is not an HTTP request and
    consumes no pacing delay; pacing applies only between actual
    provider requests.

    Determinism: a fully successful sweep returns the same rows
    whether fetched in one uninterrupted run or resumed from valid
    checkpoints (checkpointed rows were stored under the same
    fetched-at provenance as their original leaf fetch; the caller's
    ``IngestStore.upsert_headlines`` is idempotent by headline_hash).

    The ``apikey`` credential is redacted from any transport error text
    before it propagates (the key rides in the query parameters) and
    is NEVER persisted in a checkpoint.
    """
    log = fetch_log or FetchLog()
    rows: list[dict] = []
    fetched_at = _now_utc_iso()
    base_params = _alphavantage_params()  # fail closed before any request
    sleeper = sleep_fn if sleep_fn is not None else _sleep
    ckpt_dir = Path(checkpoint_dir) if checkpoint_dir else None

    def request_window(a: _dt.datetime, b: _dt.datetime) -> list:
        try:
            payload = fetch_json(
                ALPHAVANTAGE_BASE,
                params={**base_params,
                        "function": "NEWS_SENTIMENT",
                        "tickers": ticker,
                        "time_from": _instant_stamp(a),
                        "time_to": _instant_stamp(b),
                        "limit": str(NEWS_WINDOW_LIMIT)},
                http_get=http_get)
        except IngestionError as exc:
            raise IngestionError(_redact_apikey(exc)) from exc
        return _parse_feed(payload)

    def fetch_interval(a: _dt.datetime, b: _dt.datetime,
                       first: bool) -> tuple[list[dict], int]:
        ckpt_path = (_checkpoint_path(ticker, a, b, ckpt_dir)
                     if ckpt_dir is not None else None)
        # 1) Saturation-marker HIT: not an HTTP request — deterministically
        #    reconstruct the same two children and continue traversal.
        if ckpt_path is not None and _load_saturation_marker(
                ckpt_path, ticker=ticker, a=a, b=b):
            left, right = _split_interval(a, b)
            rows_l, drops_l = fetch_interval(a, left, False)
            rows_r, drops_r = fetch_interval(right, b, False)
            return rows_l + rows_r, drops_l + drops_r
        # 2) Completed-leaf HIT: restore the validated, normalized rows
        #    without touching the provider (and without pacing — a
        #    checkpoint read is not an HTTP request).
        if ckpt_path is not None:
            cached = _load_checkpoint(ckpt_path, ticker=ticker, a=a, b=b)
            if cached is not None:
                # Replay canonicalization: a legacy checkpoint written before
                # the intra-response duplicate canonicalization patch may
                # contain conflicting duplicate identities (same
                # headline_hash/source/ticker with different published_at).
                # Apply the SAME adapter-local canonicalization as the live
                # fetch path so resume semantics are consistent.
                # CHECKPOINT REPLAY SEMANTICS (drop accounting): the stored
                # DROP_INVALID_FP4_EMPTY count is the deterministic count of
                # provider items EXCLUDED from the canonical rows this leaf
                # contributed. Replaying the checkpoint re-reports that count
                # — drop counts describe canonical exclusions represented by
                # the inventory consumed by this run, not merely exclusions
                # observed during HTTP calls made by this process — so the
                # exclusion evidence survives a resume even when the original
                # run died before persisting its fetch report. The count was
                # recorded once during the live fetch and is replayed
                # verbatim (never recounted — the provider is not re-queried;
                # legacy pre-drop-metadata checkpoints replay with 0).
                cached_rows, cached_drops = cached
                rows = _canonicalize_rows(list(cached_rows))
                # Fold the restored exclusion count into this run's drop
                # accounting (a replay emits no FetchRecord — one record
                # per HTTP request is the run invariant).
                if cached_drops and fetch_log is not None:
                    fetch_log.add_replay_drops(cached_drops)
                return rows, cached_drops
        # 3) MISS: the normal sequential paced request path.
        if not first:
            sleeper(NEWS_PACING_SECONDS)
        feed = request_window(a, b)
        # 4) SATURATED parent: not complete, rows discarded (never stored as
        #    canonical inventory). Persist the saturation marker BEFORE
        #    recursing so a later quota-interrupted run can skip this
        #    node entirely, then deterministically subdivide (recursion
        #    depth is bounded; below the minimum minute granularity the
        #    split itself raises WindowSaturatedError — fail closed,
        #    nothing persisted as complete for this node).
        if len(feed) >= NEWS_WINDOW_LIMIT:
            if ckpt_path is not None:
                _save_saturation_marker(ckpt_path, ticker=ticker, a=a, b=b)
            left, right = _split_interval(a, b)
            rows_l, drops_l = fetch_interval(a, left, False)
            rows_r, drops_r = fetch_interval(right, b, False)
            return rows_l + rows_r, drops_l + drops_r
        window_rows, window_drops = _rows_from_feed(
            feed, ticker=ticker, fetched_at=fetched_at)
        # UNSATURATED leaf completed: request succeeded, payload
        # validated, association/normalization done, rows in hand —
        # atomically persist the completed checkpoint.
        if ckpt_dir is not None:
            ckpt_dir_path = Path(ckpt_dir)
            ckpt_dir_path.mkdir(parents=True, exist_ok=True)
            _save_checkpoint(
                _checkpoint_path(ticker, a, b, ckpt_dir_path),
                ticker=ticker, a=a, b=b, rows=window_rows,
                drops=window_drops)
        # FetchRecord params deliberately exclude the credential.
        # Saturation is determined from the RAW provider feed count
        # (len(feed)) BEFORE invalid-row dropping/canonicalization, so
        # FP-4-empty drops never change the saturated/unsaturated verdict.
        log.add(FetchRecord(
            provider="alphavantage", endpoint="NEWS_SENTIMENT",
            params={"tickers": ticker,
                    "time_from": _instant_stamp(a),
                    "time_to": _instant_stamp(b),
                    "limit": str(NEWS_WINDOW_LIMIT)},
            fetched_at=fetched_at, items=len(feed), drops=window_drops))
        return window_rows, window_drops

    all_rows: list[dict] = []
    total_drops = 0
    for i, window in enumerate(_annual_windows(start, end)):
        a, b = _window_to_datetimes(window)
        rows_i, drops_i = fetch_interval(a, b, first=(i == 0))
        all_rows.extend(rows_i)
        total_drops += drops_i
    return all_rows, total_drops


def _date_to_utc_midnight(d: _dt.date) -> str:
    return _dt.datetime(d.year, d.month, d.day,
                        tzinfo=_dt.timezone.utc).isoformat()


def _date_to_utc_end_of_day(d: _dt.date) -> str:
    return _dt.datetime(d.year, d.month, d.day, 23, 59, 59,
                        tzinfo=_dt.timezone.utc).isoformat()


def news_manifest_rows(*, ticker: str, start: _dt.date, end: _dt.date,
                       manifest_version: str) -> list[dict]:
    """Verified NEWS covered-span attestation for a completed sweep of
    demonstrably complete windows (§3.8 / §11.6). A zero-headline span
    is still a verified covered span (§11.6 verified-zero semantics,
    unchanged). Span bounds are midnight-UTC / 23:59:59-UTC timestamps
    of the inclusive date range — the same form the Finnhub NEWS rows
    and ``HeadlineInventory.covered`` use; no new manifest_version
    semantics, no provider-ID field (§3.8 N-1-e)."""
    return [{
        "source_kind": "NEWS",
        "ticker": ticker,
        "span_start": _date_to_utc_midnight(start),
        "span_end": _date_to_utc_end_of_day(end),
        "verified": True,
        "manifest_version": manifest_version,
    }]
