"""R2.7 Phase-0 data ingestion — shared, provider-agnostic infrastructure.

Deterministic, idempotent, fail-closed ingestion jobs share this module:

- ``fetch_json`` / ``fetch_text``: HTTP GET with bounded deterministic
  retries (fixed exponential backoff, no jitter — determinism is a spec
  requirement, §20 Phase 0 data jobs). Transport is injected
  (``http_get``) so tests are hermetic: no test ever performs a network
  call, and no default transport is constructed at import time.
- ``IngestStore``: idempotent upsert helpers over the §16 tables the
  Phase-0 jobs write (``bars``, ``corp_actions``, ``news_headlines``,
  ``coverage_manifests``). Re-running a job with identical provider
  output is a no-op; a differing payload for the same logical row is a
  deterministic error (never a silent overwrite — mirrors the §11.5
  cache-immutability discipline).
- Explicit provenance: every row carries ``run_id`` / ``config_version``
  / ``code_commit``, and every fetch records provider, endpoint, request
  parameters, and the observed response window (``FetchRecord``).

No secrets are ever logged or persisted: credential values are used only
to build request headers/params inside the provider clients.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# Deterministic retry policy: fixed exponential backoff (2^attempt seconds),
# capped, NO jitter. Max attempts includes the first try.
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BACKOFF_BASE_SECONDS = 2.0
DEFAULT_BACKOFF_CAP_SECONDS = 30.0

# Transient HTTP statuses worth retrying (provider throttling / hiccup).
RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


class IngestionError(Exception):
    """Deterministic fail-closed ingestion failure (§20 Phase 0).

    Retriable transport exhaustion and non-2xx provider responses both
    surface here after the retry policy is exhausted; the job halts
    rather than writing partial coverage.
    """


DROP_INVALID_FP4_EMPTY = "DROP_INVALID_FP4_EMPTY"
"""Stable reason identifier for an otherwise well-formed provider item whose
FP-4-normalized headline text is empty (punctuation-only / non-substantive
titles). The item produces no canonical news_headlines row and no
headline_hash/classification; it does NOT by itself fail the enclosing
provider sweep. See P-NEWS-EMPTY."""


@dataclass(frozen=True)
class FetchRecord:
    """Provenance for one logical provider request (or paginated sweep).

    ``response_window`` records the actually-observed data range (e.g.
    earliest/latest headline timestamps) so coverage manifests are built
    from observed responses only — never from documentation claims.
    """

    provider: str
    endpoint: str
    params: dict[str, Any]
    fetched_at: str                       # ISO-8601 UTC wall clock (provenance only)
    response_window: dict[str, Any] = field(default_factory=dict)
    items: int = 0
    pages: int = 0
    drops: int = 0


class FetchLog:
    """Accumulates FetchRecords for a job run (explicit provider/date
    metadata, persisted with the coverage manifest).

    Drop accounting (P-NEWS-EMPTY observability): ``total_drops``
    describes the deterministic canonical exclusions represented by the
    inventory used by THIS run — both live HTTP exclusions
    (``FetchRecord.drops``) and exclusions restored from completed-leaf
    checkpoints on replay (``replay_drops``). A checkpoint replay emits
    NO new FetchRecord (one record per HTTP request is the invariant)
    but its stored exclusion count is folded into the run's totals via
    :meth:`add_replay_drops`, so exclusion evidence survives resume.
    """

    def __init__(self) -> None:
        self.records: list[FetchRecord] = []
        self.replay_drops: int = 0

    def add(self, record: FetchRecord) -> None:
        self.records.append(record)

    def add_replay_drops(self, drops: int) -> None:
        """Fold a replayed checkpoint's stored exclusion count into this
        run's drop accounting (never recounted, zero HTTP)."""
        self.replay_drops += int(drops)

    def to_json(self) -> str:
        docs = [r.__dict__ for r in self.records]
        if self.replay_drops:
            docs.append({
                "provider": "alphavantage",
                "endpoint": "CHECKPOINT_REPLAY",
                "params": {},
                "fetched_at": "",
                "response_window": {},
                "items": 0,
                "pages": 0,
                "drops": self.replay_drops,
                "replay": True,
            })
        return json.dumps(docs, sort_keys=True, indent=2)

    @property
    def total_items(self) -> int:
        return sum(r.items for r in self.records)

    @property
    def total_drops(self) -> int:
        return sum(r.drops for r in self.records) + self.replay_drops


def _default_http_get(url: str, *, headers: dict[str, str] | None = None,
                      params: dict[str, Any] | None = None,
                      timeout: float = 30.0) -> tuple[int, str]:
    """Real transport (httpx). Never constructed at import time; the jobs
    call it lazily so tests can inject a fake and hermetic runs never
    touch the network."""
    import httpx  # lazy: base dependency, but keep import off module load

    response = httpx.get(url, headers=headers, params=params,
                         timeout=timeout)
    return response.status_code, response.text


def fetch_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    http_get: Callable[..., tuple[int, str]] | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
    backoff_cap: float = DEFAULT_BACKOFF_CAP_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = 30.0,
) -> Any:
    """GET ``url`` and parse JSON, with deterministic bounded retries.

    Retries on transport errors and on the retryable status set; any other
    non-200 status fails closed immediately with :class:`IngestionError`.
    A 200 whose body is not valid JSON also fails closed (never guessed).
    """
    transport = http_get or _default_http_get
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            status, body = transport(url, headers=headers, params=params,
                                     timeout=timeout)
        except Exception as exc:  # transport error — retryable
            last_error = exc
            if attempt + 1 < max_attempts:
                sleep(min(backoff_base * (2 ** attempt), backoff_cap))
                continue
            break
        if status == 200:
            try:
                return json.loads(body)
            except (ValueError, TypeError) as exc:
                raise IngestionError(
                    f"{url}: 200 response is not valid JSON: {exc}") from exc
        if status in RETRYABLE_STATUS_CODES:
            last_error = IngestionError(f"{url}: HTTP {status}")
            if attempt + 1 < max_attempts:
                sleep(min(backoff_base * (2 ** attempt), backoff_cap))
                continue
            break
        # Non-retryable status: fail closed now.
        raise IngestionError(f"{url}: HTTP {status}: {body[:300]}")
    raise IngestionError(
        f"{url}: exhausted {max_attempts} attempts; last error: {last_error!r}")


class IngestStore:
    """Idempotent writer over the §16 Phase-0 tables.

    Backed by an existing ``backtest.db.schema`` connection (the caller
    opens it; this class never creates files).
    """

    def __init__(self, conn: sqlite3.Connection, *, run_id: str = "",
                 config_version: int = 0, code_commit: str = ""):
        self._conn = conn
        self.run_id = run_id
        self.config_version = config_version
        self.code_commit = code_commit

    # -- bars (§16) -------------------------------------------------------

    def upsert_bars(self, rows: list[dict]) -> int:
        """Idempotent bar upsert keyed (ticker, ts_label_start, timeframe,
        adjustment). Identical re-ingest is a no-op; a differing payload
        for the same key is a deterministic error. Returns NEW rows."""
        new = 0
        for r in rows:
            key = (r["ticker"], r["ts_label_start"], r["timeframe"],
                   r["adjustment"])
            existing = self._conn.execute(
                "SELECT o, h, l, c, v, feed FROM bars WHERE ticker=? AND "
                "ts_label_start=? AND timeframe=? AND adjustment=?",
                key).fetchone()
            values = (str(r["o"]), str(r["h"]), str(r["l"]), str(r["c"]),
                      str(r["v"]), r["feed"])
            if existing is not None:
                if tuple(existing) != values:
                    raise IngestionError(
                        f"bars row {key} already exists with differing "
                        f"values {tuple(existing)} != {values}")
                continue
            self._conn.execute(
                "INSERT INTO bars (ticker, ts_label_start, o, h, l, c, v, "
                "feed, timeframe, adjustment, run_id, config_version, "
                "code_commit) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["ticker"], r["ts_label_start"], values[0], values[1],
                 values[2], values[3], values[4], values[5],
                 r["timeframe"], r["adjustment"], self.run_id,
                 self.config_version, self.code_commit))
            new += 1
        self._conn.commit()
        return new

    # -- corp_actions (§16 / §3.6) ----------------------------------------

    def upsert_corp_actions(self, rows: list[dict]) -> int:
        """Idempotent corporate-action upsert keyed (ticker, event_type,
        ex_date, corp_actions_version). §3.6 rule 3 is enforced upstream:
        nothing here derives ratios from prices."""
        new = 0
        for r in rows:
            key = (r["ticker"], r["event_type"], r["ex_date"],
                   r["corp_actions_version"])
            existing = self._conn.execute(
                "SELECT split_ratio, cash_amount_per_share, record_date, "
                "pay_date FROM corp_actions WHERE ticker=? AND event_type=? "
                "AND ex_date=? AND corp_actions_version=?", key).fetchone()
            values = (r.get("split_ratio"), r.get("cash_amount_per_share"),
                      r.get("record_date"), r.get("pay_date"))
            if existing is not None:
                if tuple(existing) != values:
                    raise IngestionError(
                        f"corp_actions row {key} exists with differing "
                        f"values {tuple(existing)} != {values}")
                continue
            self._conn.execute(
                "INSERT INTO corp_actions (ticker, event_type, ex_date, "
                "split_ratio, cash_amount_per_share, record_date, pay_date, "
                "corp_actions_version, run_id, config_version, code_commit) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (r["ticker"], r["event_type"], r["ex_date"],
                 values[0], values[1], values[2], values[3],
                 r["corp_actions_version"], self.run_id,
                 self.config_version, self.code_commit))
            new += 1
        self._conn.commit()
        return new

    # -- news_headlines (§16 / FP-4/FP-5) ----------------------------------

    def upsert_headlines(self, rows: list[dict]) -> int:
        """Idempotent headline-inventory upsert keyed (headline_hash,
        source, ticker). ``headline_text_normalized`` must already be the
        FP-4 normalized form; ``published_at`` is NULL for untimed
        headlines (dropped at ingestion upstream per §11.1 — this store
        still records them for inventory transparency is NOT done: the
        §16 table persists what the provider returned; the §11.1 drop is
        a consumption rule)."""
        new = 0
        for r in rows:
            key = (r["headline_hash"], r["source"], r["ticker"])
            existing = self._conn.execute(
                "SELECT published_at, headline_text_normalized FROM "
                "news_headlines WHERE headline_hash=? AND source=? AND "
                "ticker=?", key).fetchone()
            values = (r.get("published_at"), r["headline_text_normalized"])
            if existing is not None:
                if tuple(existing) != values:
                    raise IngestionError(
                        f"news_headlines row {key} exists with differing "
                        f"values")
                continue
            self._conn.execute(
                "INSERT INTO news_headlines (headline_hash, source, ticker, "
                "published_at, headline_text_normalized, fetched_at, run_id,"
                " config_version, code_commit) VALUES (?,?,?,?,?,?,?,?,?)",
                (r["headline_hash"], r["source"], r["ticker"],
                 values[0], values[1], r["fetched_at"], self.run_id,
                 self.config_version, self.code_commit))
            new += 1
        self._conn.commit()
        return new

    # -- coverage_manifests (§16 / FP-5) ------------------------------------

    def write_manifest(self, rows: list[dict]) -> int:
        """Append coverage-manifest records. Unlike the data tables, a
        manifest is a versioned assertion artifact: rows are keyed
        (source_kind, ticker, span_start, span_end, manifest_version) and
        re-writing an identical row is a no-op; conflicting rows fail
        closed. A verified span with zero events is written as a
        verified-zero attestation (§3.6 item 4)."""
        new = 0
        for r in rows:
            key = (r["source_kind"], r["ticker"], r["span_start"],
                   r["span_end"], r["manifest_version"])
            existing = self._conn.execute(
                "SELECT verified FROM coverage_manifests WHERE "
                "source_kind=? AND ticker=? AND span_start=? AND span_end=?"
                " AND manifest_version=?", key).fetchone()
            if existing is not None:
                if bool(existing[0]) != bool(r["verified"]):
                    raise IngestionError(
                        f"coverage_manifests row {key} exists with "
                        f"verified={bool(existing[0])}, refusing rewrite to "
                        f"{bool(r['verified'])}")
                continue
            self._conn.execute(
                "INSERT INTO coverage_manifests (source_kind, ticker, "
                "span_start, span_end, verified, manifest_version, run_id, "
                "config_version, code_commit) VALUES (?,?,?,?,?,?,?,?,?)",
                (r["source_kind"], r["ticker"], r["span_start"],
                 r["span_end"], 1 if r["verified"] else 0,
                 r["manifest_version"], self.run_id,
                 self.config_version, self.code_commit))
            new += 1
        self._conn.commit()
        return new

    # -- Alpha Vantage news publication (§3.8 atomic unit) ------------------

    def publish_alphavantage_news(
        self,
        *,
        headlines: list[dict],
        manifest: list[dict],
    ) -> tuple[int, int]:
        """Atomically persist ONE Alpha Vantage publication unit:
        canonical ``news_headlines`` rows + corresponding
        ``coverage_manifests`` rows.

        Required invariant (P-NEWS-ATOMIC): either BOTH become durable or
        NEITHER does. If headline upsert raises, no manifest is written. If
        manifest write raises, headline changes for this publication unit are
        rolled back.

        Returns ``(headlines_new, manifest_new)`` — the number of NEW rows
        in each table (idempotent: identical re-ingest returns 0, 0).

        Existing already-committed rows from OTHER publication units are
        never modified. Checkpoints are filesystem artifacts and are NOT part
        of this transaction (they remain resume state only).

        Transaction design: this method owns the savepoint lifecycle. The
        existing ``upsert_headlines`` / ``write_manifest`` each call
        ``self._conn.commit()`` internally and cannot be used directly
        (an internal commit would make headline changes durable before the
        manifest phase, breaking atomicity). Instead the savepoint-scoped
        ``_upsert_headlines_atomic`` / ``_write_manifest_atomic`` variants
        perform the same idempotent insert-or-conflict logic but NEVER
        commit — only this method commits, and only after BOTH phases
        succeed. On ANY exception from either phase the savepoint is rolled
        back, unwinding only this publication unit's changes while leaving
        pre-existing committed rows untouched.
        """
        self._conn.execute("SAVEPOINT av_news_unit")
        try:
            headlines_new = self._upsert_headlines_atomic(headlines)
            manifest_new = self._write_manifest_atomic(manifest)
        except Exception:
            # Roll back everything done in THIS publication unit. Pre-
            # existing committed rows are untouched (a savepoint rollback
            # only undoes changes made since the SAVEPOINT was opened).
            # ROLLBACK TO rolls back to the savepoint but does NOT destroy
            # it; RELEASE destroys it (a no-op commit since we just rolled
            # back, leaving a clean savepoint-free connection state).
            self._conn.execute("ROLLBACK TO SAVEPOINT av_news_unit")
            self._conn.execute("RELEASE SAVEPOINT av_news_unit")
            raise
        self._conn.execute("RELEASE SAVEPOINT av_news_unit")
        self._conn.commit()
        return (headlines_new, manifest_new)

    def _upsert_headlines_atomic(self, rows: list[dict]) -> int:
        """Savepoint-scoped variant of ``upsert_headlines`` — same
        idempotent insert-or-conflict logic, but does NOT call
        ``self._conn.commit()``. The caller (``publish_alphavantage_news``)
        owns the savepoint lifecycle and the final commit."""
        new = 0
        for r in rows:
            key = (r["headline_hash"], r["source"], r["ticker"])
            existing = self._conn.execute(
                "SELECT published_at, headline_text_normalized FROM "
                "news_headlines WHERE headline_hash=? AND source=? AND "
                "ticker=?", key).fetchone()
            values = (r.get("published_at"), r["headline_text_normalized"])
            if existing is not None:
                if tuple(existing) != values:
                    raise IngestionError(
                        f"news_headlines row {key} exists with differing "
                        f"values")
                continue
            self._conn.execute(
                "INSERT INTO news_headlines (headline_hash, source, ticker, "
                "published_at, headline_text_normalized, fetched_at, run_id,"
                " config_version, code_commit) VALUES (?,?,?,?,?,?,?,?,?)",
                (r["headline_hash"], r["source"], r["ticker"],
                 values[0], values[1], r["fetched_at"], self.run_id,
                 self.config_version, self.code_commit))
            new += 1
        return new

    def _write_manifest_atomic(self, rows: list[dict]) -> int:
        """Savepoint-scoped variant of ``write_manifest`` — same append-
        or-conflict logic, but does NOT call ``self._conn.commit()``. The
        caller (``publish_alphavantage_news``) owns the savepoint
        lifecycle and the final commit."""
        new = 0
        for r in rows:
            key = (r["source_kind"], r["ticker"], r["span_start"],
                   r["span_end"], r["manifest_version"])
            existing = self._conn.execute(
                "SELECT verified FROM coverage_manifests WHERE "
                "source_kind=? AND ticker=? AND span_start=? AND span_end=?"
                " AND manifest_version=?", key).fetchone()
            if existing is not None:
                if bool(existing[0]) != bool(r["verified"]):
                    raise IngestionError(
                        f"coverage_manifests row {key} exists with "
                        f"verified={bool(existing[0])}, refusing rewrite to "
                        f"{bool(r['verified'])}")
                continue
            self._conn.execute(
                "INSERT INTO coverage_manifests (source_kind, ticker, "
                "span_start, span_end, verified, manifest_version, run_id, "
                "config_version, code_commit) VALUES (?,?,?,?,?,?,?,?,?)",
                (r["source_kind"], r["ticker"], r["span_start"],
                 r["span_end"], 1 if r["verified"] else 0,
                 r["manifest_version"], self.run_id,
                 self.config_version, self.code_commit))
            new += 1
        return new

    # -- vix_observations (§3.1/§5.2 FRED VIXCLS) ---------------------------

    def upsert_vix(self, rows: list[dict]) -> int:
        """Idempotent VIXCLS upsert keyed observation_date. A differing
        value for an existing date is a deterministic error (FRED VIXCLS
        is a fixed historical series; revisions are surfaced rather than
        silently overwritten)."""
        new = 0
        for r in rows:
            existing = self._conn.execute(
                "SELECT value FROM vix_observations WHERE observation_date=?",
                (r["observation_date"],)).fetchone()
            if existing is not None:
                if existing[0] != r["value"]:
                    raise IngestionError(
                        f"vix_observations {r['observation_date']} exists "
                        f"with value {existing[0]!r}, refusing rewrite to "
                        f"{r['value']!r}")
                continue
            self._conn.execute(
                "INSERT INTO vix_observations (observation_date, value, "
                "series_id, run_id, config_version, code_commit) "
                "VALUES (?,?,?,?,?,?)",
                (r["observation_date"], r["value"],
                 r.get("series_id", "VIXCLS"),
                 self.run_id, self.config_version, self.code_commit))
            new += 1
        self._conn.commit()
        return new

    # -- reads --------------------------------------------------------------

    def headline_count(self, ticker: str | None = None) -> int:
        if ticker is None:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM news_headlines").fetchone()[0])
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM news_headlines WHERE ticker=?",
            (ticker,)).fetchone()[0])

    def bar_count(self, ticker: str, timeframe: str, adjustment: str) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM bars WHERE ticker=? AND timeframe=? AND "
            "adjustment=?", (ticker, timeframe, adjustment)).fetchone()[0])


def _now_utc_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()
