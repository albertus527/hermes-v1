"""R2.7 §11.5/§16 — the deterministic news-classification cache.

Backtests replay from this store ONLY (§21 item 18); a backtest never
issues a live LLM call. The cache key is
``(headline_hash, source, schema_version, model_version)``; ``source`` is
cache-identity metadata, never a classifier input (P-4). Historical caches
are never overwritten (§11.5): re-populating an existing key with identical
payload is an idempotent no-op; a differing payload for the same key is a
deterministic error, never a silent overwrite.

All effect-field equality enforcement (P-4) happens here at write AND read
time so the deterministic core receives only consistent entries — the
trading-side assertion in ``trading_core/news_effects.assert_cache_integrity``
remains the last line of defense.

This module performs NO I/O except SQLite on the connection it is given
(or opens read-only) and never reads the wall clock for any decision.
``classified_at_wallclock`` is recorded verbatim when supplied by the
population job and has no decision effect (N-21).
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from pathlib import Path

from trading_core.errors import NewsCacheIntegrityFailure
from trading_core.news_effects import (
    CATEGORIES,
    DIRECTIONS,
    MA_ROLES,
    SCHEMA_VERSION_V3,
    SEVERITIES,
    Classification,
    headline_hash as compute_headline_hash,
)

# §11.5: cache key columns (the table adds ticker for the P-4 assertion).
CACHE_KEY_COLUMNS = ("headline_hash", "source", "schema_version", "model_version")

# §16 news_classifications columns managed by this store.
_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS news_classifications (
    headline_hash TEXT NOT NULL, ticker TEXT NOT NULL, source TEXT NOT NULL,
    ma_role TEXT NOT NULL, keyword_override INTEGER NOT NULL DEFAULT 0,
    json_payload TEXT NOT NULL,
    model_version TEXT NOT NULL, schema_version TEXT NOT NULL,
    classified_at_wallclock TEXT, published_at TEXT,
    activation_start TEXT, activation_end TEXT,
    run_id TEXT NOT NULL DEFAULT '', config_version INTEGER NOT NULL DEFAULT 0,
    code_commit TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_news_cls_cache_key
    ON news_classifications(headline_hash, source, schema_version, model_version);
CREATE INDEX IF NOT EXISTS idx_news_cls_hash_ticker
    ON news_classifications(headline_hash, ticker);
"""

_REQUIRED_PAYLOAD_FIELDS = (
    "ticker", "category", "direction", "severity", "ma_role",
    "confidence", "published_at", "headline_hash", "source",
    "keyword_override", "schema_version", "model_version",
)


class MalformedClassificationError(Exception):
    """Deterministic fail-closed rejection of a malformed classification
    record (§11.1 strict news_schema_v3 shape). Never retried, never
    repaired by guesswork; the record is rejected and the condition is
    reportable so the population job can repair the cache."""


class CacheKeyConflictError(Exception):
    """A differing payload was offered for an existing immutable cache key
    (§11.5 'historical caches are never overwritten')."""


def _parse_published_at(value: str) -> _dt.datetime:
    """Parse an ISO-8601 published_at timestamp; require timezone awareness
    (window arithmetic is meaningless without it)."""
    try:
        parsed = _dt.datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise MalformedClassificationError(
            f"published_at {value!r} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise MalformedClassificationError(
            f"published_at {value!r} lacks timezone offset")
    return parsed


def validate_classification_payload(payload: dict) -> Classification:
    """Strict news_schema_v3 validation (§11.1), fail-closed.

    Checks (in order):
    - payload is a JSON object with exactly the required fields present
      (extra fields are rejected — strict JSON);
    - enum validity of category / direction / severity / ma_role;
    - §11.1 conditional validity: ``ma_role`` required (non-NEITHER) for
      M&A, forced NEITHER elsewhere;
    - confidence is a number in [0, 1];
    - published_at parses to an aware datetime (untimed headlines are
      dropped at ingestion upstream and never reach the cache);
    - headline_hash matches FP-4 recomputation is NOT checked here (the
      raw text is not part of the cached payload); the population job
      verifies it before writing.
    """
    if not isinstance(payload, dict):
        raise MalformedClassificationError("payload is not a JSON object")
    missing = [f for f in _REQUIRED_PAYLOAD_FIELDS if f not in payload]
    if missing:
        raise MalformedClassificationError(
            f"missing required fields: {sorted(missing)}")
    extra = sorted(set(payload) - set(_REQUIRED_PAYLOAD_FIELDS))
    if extra:
        raise MalformedClassificationError(
            f"unexpected fields (strict news_schema_v3): {extra}")
    for field in ("category", "direction", "severity", "ma_role",
                  "headline_hash", "source", "schema_version",
                  "model_version", "ticker"):
        if not isinstance(payload.get(field), str) or not payload[field]:
            raise MalformedClassificationError(
                f"field {field!r} must be a non-empty string")
    if payload["schema_version"] != SCHEMA_VERSION_V3:
        raise MalformedClassificationError(
            f"unsupported schema_version {payload['schema_version']!r}")
    if payload["category"] not in CATEGORIES:
        raise MalformedClassificationError(
            f"unknown category {payload['category']!r}")
    if payload["direction"] not in DIRECTIONS:
        raise MalformedClassificationError(
            f"unknown direction {payload['direction']!r}")
    if payload["severity"] not in SEVERITIES:
        raise MalformedClassificationError(
            f"unknown severity {payload['severity']!r}")
    if payload["ma_role"] not in MA_ROLES:
        raise MalformedClassificationError(
            f"unknown ma_role {payload['ma_role']!r}")
    if payload["category"] != "M&A" and payload["ma_role"] != "NEITHER":
        raise MalformedClassificationError(
            f"ma_role must be NEITHER for category {payload['category']!r}")
    if payload["category"] == "M&A" and payload["ma_role"] == "NEITHER":
        # §11.1: "When category = M&A, ma_role is required and must be one
        # of TARGET | ACQUIRER | NEITHER". NEITHER is grammatically valid;
        # it maps to a non-TARGET M&A classification (no veto branch).
        pass
    confidence = payload["confidence"]
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise MalformedClassificationError("confidence must be a number")
    if not 0.0 <= float(confidence) <= 1.0:
        raise MalformedClassificationError(
            f"confidence {confidence!r} outside [0, 1]")
    if not isinstance(payload["keyword_override"], bool):
        raise MalformedClassificationError("keyword_override must be a boolean")
    published_at = _parse_published_at(payload["published_at"])
    try:
        return Classification(
            ticker=payload["ticker"],
            category=payload["category"],
            direction=payload["direction"],
            severity=payload["severity"],
            ma_role=payload["ma_role"],
            confidence=float(confidence),
            published_at=published_at,
            headline_hash=payload["headline_hash"],
            source=payload["source"],
            keyword_override=payload["keyword_override"],
        )
    except ValueError as exc:
        raise MalformedClassificationError(str(exc)) from exc


def _row_to_classification(row: sqlite3.Row) -> Classification:
    """Rebuild a validated Classification from a cache row (fail-closed:
    malformed rows raise instead of yielding partial data)."""
    payload = json.loads(row["json_payload"])
    return validate_classification_payload(payload)


class NewsClassificationCache:
    """SQLite-backed classification cache (§11.5 cache identity; §16 table).

    Single-writer batch usage (population job) and read-only replay usage
    share one store implementation; the deterministic core's
    ``ClassificationStore`` needs are served by :meth:`lookup` /
    :meth:`all_classifications`, which are pure cache reads.

    Rows are accessed as ``sqlite3.Row`` (set by :func:`open_news_cache`);
    column-name access is required so payload reconstruction never depends
    on column order.
    """

    def __init__(self, conn: sqlite3.Connection, *, _skip_ddl: bool = False):
        self._conn = conn
        if self._conn.in_transaction:
            self._conn.commit()
        if not _skip_ddl:
            self._conn.executescript(_CACHE_DDL)
            self._conn.commit()

    @classmethod
    def _read_only(cls, conn: sqlite3.Connection) -> "NewsClassificationCache":
        """Read-only replay handle: skip DDL (query_only connections reject
        even no-op CREATE statements)."""
        return cls(conn, _skip_ddl=True)

    # -- writes (population time only) ------------------------------------

    def insert(self, classification: Classification, *, payload: dict,
               classified_at_wallclock: str | None = None,
               run_id: str = "", config_version: int = 0,
               code_commit: str = "",
               activation_start: str | None = None,
               activation_end: str | None = None) -> bool:
        """Insert one cache entry. Idempotent when the identical payload is
        re-materialized for the same cache key AND ticker; raises
        :class:`CacheKeyConflictError` on a differing payload for an
        existing (cache key, ticker) row (§11.5 historical caches are never
        overwritten) and :class:`NewsCacheIntegrityFailure` on a P-4
        effect-field conflict across sources for the same
        (headline_hash, ticker).

        The same headline text from the same source MAY legitimately be
        classified for two different tickers (P-4 groups by
        (headline_hash, ticker)); those are distinct rows.

        Returns True when a row was written, False when it was already
        present (idempotent no-op).
        """
        existing = self._conn.execute(
            "SELECT json_payload FROM news_classifications WHERE "
            "headline_hash=? AND source=? AND schema_version=? AND "
            "model_version=? AND ticker=?",
            (classification.headline_hash, classification.source,
             payload["schema_version"], payload["model_version"],
             classification.ticker),
        ).fetchone()
        if existing is not None:
            if json.loads(existing["json_payload"]) != payload:
                raise CacheKeyConflictError(
                    "immutable cache key already holds a differing payload "
                    f"(headline_hash={classification.headline_hash}, "
                    f"source={classification.source}, "
                    f"ticker={classification.ticker})")
            return False  # idempotent re-materialization
        # P-4 pre-write assertion across ALL existing rows for this
        # (headline_hash, ticker), including other sources/keys.
        self.assert_p4(classification)
        self._conn.execute(
            "INSERT INTO news_classifications ("
            "headline_hash, ticker, source, ma_role, keyword_override, "
            "json_payload, model_version, schema_version, "
            "classified_at_wallclock, published_at, "
            "activation_start, activation_end, "
            "run_id, config_version, code_commit) VALUES ("
            "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                classification.headline_hash, classification.ticker,
                classification.source, classification.ma_role,
                1 if classification.keyword_override else 0,
                json.dumps(payload, sort_keys=True),
                payload["model_version"], payload["schema_version"],
                classified_at_wallclock,
                payload["published_at"],
                activation_start, activation_end,
                run_id, config_version, code_commit,
            ),
        )
        self._conn.commit()
        return True

    def assert_p4(self, classification: Classification) -> None:
        """P-4 (§11.5/§16 rule 9): every existing row sharing
        (headline_hash, ticker) — across ALL source-keyed cache identities —
        must carry identical effect fields. A mismatch is a deterministic
        cache-integrity failure."""
        rows = self._conn.execute(
            "SELECT json_payload FROM news_classifications WHERE "
            "headline_hash=? AND ticker=?",
            (classification.headline_hash, classification.ticker),
        ).fetchall()
        for row in rows:
            other = validate_classification_payload(json.loads(row["json_payload"]))
            if other.effect_fields != classification.effect_fields:
                raise NewsCacheIntegrityFailure(
                    f"(headline_hash={classification.headline_hash}, "
                    f"ticker={classification.ticker}) has conflicting effect "
                    f"fields across cache entries",
                    {"headline_hash": classification.headline_hash,
                     "ticker": classification.ticker,
                     "first": list(other.effect_fields),
                     "second": list(classification.effect_fields)},
                )

    # -- reads (replay time; deterministic, no LLM) ------------------------

    def lookup(self, headline_hash: str, source: str, *,
               schema_version: str, model_version: str,
               ticker: str | None = None) -> Classification | None:
        """Deterministic cache-key lookup. Returns None on a miss — a miss
        is never fabricated; §11.2 trigger (b) turns covered-span misses
        into NEWS_UNVERIFIED at replay time.

        ``ticker`` disambiguates when the same headline text from the same
        source is classified for more than one ticker (P-4 groups effect
        fields by (headline_hash, ticker)). Omitting it is legal only when
        at most one ticker matches; an ambiguous no-ticker lookup fails
        closed instead of guessing.
        """
        if ticker is not None:
            row = self._conn.execute(
                "SELECT json_payload FROM news_classifications WHERE "
                "headline_hash=? AND source=? AND schema_version=? AND "
                "model_version=? AND ticker=?",
                (headline_hash, source, schema_version, model_version,
                 ticker),
            ).fetchone()
            if row is None:
                return None
            return _row_to_classification(row)
        rows = self._conn.execute(
            "SELECT ticker, json_payload FROM news_classifications WHERE "
            "headline_hash=? AND source=? AND schema_version=? AND "
            "model_version=?",
            (headline_hash, source, schema_version, model_version),
        ).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise MalformedClassificationError(
                f"ambiguous cache lookup: (headline_hash={headline_hash}, "
                f"source={source}) is classified for multiple tickers "
                f"{sorted(r['ticker'] for r in rows)}; pass ticker=")
        return _row_to_classification(rows[0])

    def all_classifications(self, *, model_version: str,
                            schema_version: str = SCHEMA_VERSION_V3
                            ) -> list[Classification]:
        """Every cached classification for one (model_version, schema_version)
        pin, P-4-asserted as a set (§16 rule 9 before any news effect is
        consumed). Malformed rows fail closed via
        :class:`MalformedClassificationError`."""
        rows = self._conn.execute(
            "SELECT json_payload FROM news_classifications WHERE "
            "model_version=? AND schema_version=?",
            (model_version, schema_version),
        ).fetchall()
        out = [_row_to_classification(r) for r in rows]
        from trading_core.news_effects import assert_cache_integrity
        assert_cache_integrity(out)
        return out

    def cached_keys(self) -> set[tuple[str, str, str, str, str]]:
        """All populated cache keys (headline_hash, source, schema_version,
        model_version, ticker) — used by the completeness report and
        idempotent population."""
        return {
            (r[0], r[1], r[2], r[3], r[4]) for r in self._conn.execute(
                "SELECT headline_hash, source, schema_version, "
                "model_version, ticker FROM news_classifications")
        }

    def headline_count(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM news_classifications").fetchone()[0])


class HeadlineInventory:
    """Read-side helper over the §16 ``news_headlines`` raw inventory
    (populated by the Phase-0 Finnhub fetch job). Deterministic replay
    lookup: timed headlines for a ticker, plus run-pinned verified NEWS
    covered-span queries against ``coverage_manifests``."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def timed_headlines(self, ticker: str) -> list[tuple[str, str, _dt.datetime]]:
        """(headline_hash, source, published_at) for every TIMED headline of
        the ticker. Untimed headlines (published_at NULL, HEADLINE_UNTIMED)
        are dropped — they produce no effect and never raise
        NEWS_UNVERIFIED (§11.1)."""
        rows = self._conn.execute(
            "SELECT headline_hash, source, published_at FROM news_headlines "
            "WHERE ticker=? AND published_at IS NOT NULL",
            (ticker,),
        ).fetchall()
        return [(r[0], r[1], _parse_published_at(r[2])) for r in rows]

    def covered(self, ticker: str, *, manifest_version: str,
                at: _dt.datetime) -> bool:
        """§11.6: ``at`` lies inside a verified NEWS covered span of the
        run-pinned manifest_version. Coverage gaps are handled by §11.6
        neutral-disable and are NEVER NEWS_UNVERIFIED."""
        rows = self._conn.execute(
            "SELECT span_start, span_end FROM coverage_manifests WHERE "
            "source_kind='NEWS' AND ticker=? AND verified=1 AND "
            "manifest_version=?",
            (ticker, manifest_version),
        ).fetchall()
        for span_start, span_end in rows:
            if _parse_published_at(span_start) <= at <= _parse_published_at(span_end):
                return True
        return False


def open_news_cache(db_path: str | Path, *, journal_mode: str = "wal",
                    read_only: bool = False) -> NewsClassificationCache:
    """Open (creating if needed) the backtest store and return the news
    cache over it. Uses the Phase-0 ``backtest.db.schema`` initializer so
    the full §16 schema exists; the news DDL above is idempotent.

    ``read_only=True`` opens an existing store in SQLite read-only URI
    mode (replay path): the DDL is skipped because ``CREATE TABLE IF NOT
    EXISTS`` is still a write on a query_only connection. The caller gets
    a cache whose write methods will fail — the replay path never writes.
    """
    from backtest.db.schema import open_db
    if read_only:
        uri = f"file:{Path(db_path)}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=1")
        return NewsClassificationCache._read_only(conn)
    conn = open_db(db_path, journal_mode=journal_mode)
    conn.row_factory = sqlite3.Row
    return NewsClassificationCache(conn)
