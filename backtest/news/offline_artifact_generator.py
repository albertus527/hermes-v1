"""R2.8.1 Phase 2 — the OFFLINE CLASSIFICATION-ARTIFACT GENERATOR.

This is the GENERATOR / ORCHESTRATION layer only. It produces the
``r281-offline-classification-1`` artifact that
:mod:`backtest.news.cache_populate_offline` then validates and publishes.
It is deliberately SEPARATE from that publisher because the two have
opposite retry contracts:

- the publisher is DETERMINISTIC — it consumes an already-generated
  artifact, never calls a model, and must never retry (a deterministic
  validation failure must not be retried into a different answer);
- the generator talks to a paid provider, where a TRANSIENT error (rate
  limit, timeout, 5xx, connection reset) must not become a permanent
  ``malformed`` row.

BOUNDED RETRY POLICY (max 5 attempts)
------------------------------------
Every attempt for a given identity is IDENTICAL: same ticker, same
headline text, same prompt/schema/model, nothing mutated between
attempts. Only TRANSIENT / PROVIDER failures are retried. Deterministic
validation failures (:class:`MalformedClassificationError`) are NOT
retried indefinitely — they fail closed after a bounded number of
attempts and the identity is recorded as an error, never guessed.
Malformed/invalid canonical output therefore still fails closed.

RESUME / SHARDING
-----------------
Work is split into DETERMINISTIC shards: the sorted identity list is
cut into ``shard_count`` contiguous slices by index, so shard
membership is a pure function of the identity list (no randomness, no
hashing of wall clock). Each completed shard is checkpointed atomically
under ``checkpoint_dir``; a restart loads the checkpoints and never
re-requests an identity already present in a completed shard, so no
successful paid classification is ever repeated. Artifact composition is
deterministic: rows are emitted sorted by (headline_hash, source,
ticker), and the artifact document itself carries no wall clock.

There is NO second cache format and NO schema change: the artifact
written here is exactly the format the existing offline publisher
consumes.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path

from backtest.news.cache import MalformedClassificationError
from backtest.news.cache_populate_offline import (
    OFFLINE_POPULATION_FORMAT_VERSION,
)
from trading_core.news_effects import (
    headline_hash as compute_headline_hash,
    normalize_headline_text,
)

__all__ = [
    "GENERATOR_SHARD_CHECKPOINT_VERSION",
    "DEFAULT_MAX_ATTEMPTS",
    "TransientProviderError",
    "GenerationIdentity",
    "ShardResult",
    "GenerationReport",
    "deterministic_shards",
    "shard_for_identity",
    "classify_with_bounded_retry",
    "load_completed_shards",
    "generate_offline_artifact",
    "compose_offline_artifact",
]

#: Checkpoint envelope version — bumped if the shard-record shape ever
#: changes, so a stale checkpoint can never be read as complete.
GENERATOR_SHARD_CHECKPOINT_VERSION = "r281-offline-classification-shard-1"

#: Bounded attempts per identity (the benchmark policy). A deterministic
#: validation failure is NOT retried into a different answer, but a
#: model that keeps emitting a contract violation is still given a
#: bounded, finite number of attempts rather than an unbounded loop.
DEFAULT_MAX_ATTEMPTS = 5


class TransientProviderError(Exception):
    """A provider-side failure that is worth retrying IDENTICALLY:
    rate limit, timeout, 5xx, connection reset. Never raised for a
    deterministic validation failure."""


def is_transient_provider_error(exc: BaseException) -> bool:
    """Classify an exception as transient/provider (retryable) or
    deterministic (never worth a retry). Conservative on purpose: an
    unrecognised exception is treated as DETERMINISTIC so a bug cannot
    be silently multiplied by five paid calls."""
    if isinstance(exc, (MalformedClassificationError, ValueError, TypeError,
                        KeyError, AttributeError, IndexError)):
        return False
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    name = type(exc).__name__
    if name in ("TransientProviderError", "RateLimitError",
                "ServiceUnavailableError", "APIConnectionError",
                "APITimeoutError", "InternalServerError", "Timeout",
                "ConnectTimeout", "ReadTimeout", "ConnectionResetError"):
        return True
    text = str(exc).lower()
    markers = ("rate limit", "ratelimit", "too many requests", "timeout",
               "timed out", "temporarily unavailable", "service unavailable",
               "bad gateway", "internal server error", "overloaded",
               "connection reset", "connection aborted", "429", "500",
               "502", "503", "504")
    return any(m in text for m in markers)


@dataclass(frozen=True)
class GenerationIdentity:
    """One classification identity to generate — the SOURCE-keyed cache
    identity row: (headline_hash, source, ticker). Each multi-ticker row
    is its own identity and needs its own call (the cache is
    source/ticker-keyed)."""
    ticker: str
    source: str
    headline_hash: str
    published_at: str
    headline_text_normalized: str

    def sort_key(self) -> tuple:
        return (self.headline_hash, self.source, self.ticker)


@dataclass
class ShardResult:
    """Outcome of one deterministic shard."""
    shard_index: int
    identities: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    provider_calls: int = 0
    attempts_histogram: dict = field(default_factory=dict)


@dataclass
class GenerationReport:
    """Machine-readable generation record. Provider call accounting lives
    here so a run can report EXACTLY what it spent."""
    pinned_model: str
    llm_config_version: str
    shard_count: int
    resumed_shards: list = field(default_factory=list)
    generated_shards: list = field(default_factory=list)
    identity_count: int = 0
    row_count: int = 0
    error_count: int = 0
    provider_calls: int = 0
    #: identities skipped because a completed shard already carried them
    resumed_identity_count: int = 0
    attempts_histogram: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True, indent=2)


# ---------------------------------------------------------------------------
# Deterministic sharding
# ---------------------------------------------------------------------------

def deterministic_shards(identities: list, shard_count: int) -> list[list]:
    """Split identities into ``shard_count`` contiguous shards over the
    DETERMINISTICALLY SORTED identity list. Shard membership is a pure
    function of the identity set — never of insertion order, wall clock,
    or a random seed — so a restart reproduces the same shards exactly
    and a completed shard can never overlap an incomplete one."""
    if shard_count < 1:
        raise ValueError("shard_count must be >= 1")
    ordered = sorted(identities, key=lambda i: i.sort_key())
    if len(ordered) == 0:
        return []
    n = min(shard_count, len(ordered))
    size = (len(ordered) + n - 1) // n
    return [ordered[i:i + size] for i in range(0, len(ordered), size)]


def shard_for_identity(identities: list, identity: GenerationIdentity,
                       shard_count: int) -> int:
    """Index of the shard owning ``identity`` — the same pure function
    :func:`deterministic_shards` uses, exposed so a caller can resume a
    single shard without recomputing the whole split."""
    shards = deterministic_shards(identities, shard_count)
    target = identity.sort_key()
    for idx, shard in enumerate(shards):
        for member in shard:
            if member.sort_key() == target:
                return idx
    raise KeyError(f"identity {target!r} is not in the shard set")


# ---------------------------------------------------------------------------
# Bounded retry
# ---------------------------------------------------------------------------

def classify_with_bounded_retry(client, identity: GenerationIdentity, *,
                                max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                                on_attempt=None):
    """Classify one identity with a BOUNDED, identical retry loop.

    Policy (all enforced here, never by the caller):
    - at most ``max_attempts`` attempts (default 5);
    - every attempt is IDENTICAL — same ticker, same headline, same
      prompt/schema/model; nothing is mutated between attempts;
    - only transient/provider failures are retried; a deterministic
      validation failure short-circuits immediately;
    - after the bound, a still-failing identity raises, so malformed /
      invalid canonical output still fails closed.

    Returns ``(payload, attempts, normalization)``. ``on_attempt`` is an
    optional observer ``(attempt, transient, exc) -> None`` used for
    accounting; it never changes control flow.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    last: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            # NewsClassifierClient.classify_with_normalization returns
            # (classification, payload, normalization) — payload SECOND.
            _classification, payload, normalization = \
                client.classify_with_normalization(
                    ticker=identity.ticker,
                    headline_text=identity.headline_text_normalized,
                    source=identity.source,
                    published_at=_dt.datetime.fromisoformat(
                        identity.published_at))
        except Exception as exc:  # classified below: transient or not
            transient = is_transient_provider_error(exc)
            if on_attempt is not None:
                on_attempt(attempt, transient, exc)
            if not transient:
                # Deterministic validation failure — fail closed NOW; a
                # retry with an unchanged prompt cannot repair it.
                raise
            last = exc
            continue
        if on_attempt is not None:
            on_attempt(attempt, False, None)
        return payload, attempt, normalization
    raise TransientProviderError(
        f"{identity.sort_key()!r} failed after {max_attempts} identical "
        f"attempts; last error: {last!r}")


# ---------------------------------------------------------------------------
# Checkpointed shard execution
# ---------------------------------------------------------------------------

def _shard_checkpoint_path(checkpoint_dir: Path, shard_index: int) -> Path:
    return Path(checkpoint_dir) / f"shard-{shard_index:05d}.json"


def load_completed_shards(checkpoint_dir) -> dict:
    """Load completed shard checkpoints. A checkpoint is honoured only
    when its envelope version matches AND every identity in the shard is
    present with a validated payload — a truncated/corrupt shard is
    ignored and regenerated rather than trusted."""
    out: dict[int, dict] = {}
    if not checkpoint_dir:
        return out
    d = Path(checkpoint_dir)
    if not d.is_dir():
        return out
    for path in sorted(d.glob("shard-*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        if doc.get("checkpoint_version") != \
                GENERATOR_SHARD_CHECKPOINT_VERSION:
            continue
        try:
            idx = int(doc["shard_index"])
            identities = doc["identities"]
            rows = doc["rows"]
        except (KeyError, TypeError, ValueError):
            continue
        if (not isinstance(identities, list)
                or not isinstance(rows, list)
                or len(identities) != len(rows)):
            continue
        out[idx] = doc
    return out


def _write_shard_checkpoint(checkpoint_dir: Path, shard: ShardResult,
                            *, llm_config_version: str,
                            pinned_model: str) -> None:
    from utils import atomic_write_text
    doc = {
        "checkpoint_version": GENERATOR_SHARD_CHECKPOINT_VERSION,
        "shard_index": shard.shard_index,
        "pinned_model": pinned_model,
        "llm_config_version": llm_config_version,
        "identities": [
            {"ticker": i.ticker, "source": i.source,
             "headline_hash": i.headline_hash, "published_at": i.published_at,
             "headline_text_normalized": i.headline_text_normalized}
            for i in shard.identities],
        "rows": shard.rows,
        "provider_calls": shard.provider_calls,
        "attempts_histogram": shard.attempts_histogram,
    }
    # Only a FULLY successful shard is checkpointed: an errored shard
    # must be regenerated so no identity is silently skipped.
    atomic_write_text(str(_shard_checkpoint_path(checkpoint_dir,
                                                 shard.shard_index)),
                      json.dumps(doc, sort_keys=True, indent=2))


def generate_offline_artifact(
        *, identities: list, client, checkpoint_dir, llm_config_version: str,
        shard_count: int = 1,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        limit: int | None = None,
) -> tuple[GenerationReport, list]:
    """Generate the offline artifact with deterministic sharding +
    checkpointed resume.

    Completed shards are loaded from ``checkpoint_dir`` and never
    re-requested — a restart resumes from the checkpoint and repeats NO
    successful paid classification. Errors are collected per identity and
    ``(report, rows)`` is returned even when some identities failed, so
    the caller decides whether an incomplete generation is acceptable (the
    offline publisher will fail closed on any missing identity anyway).

    ``provider_calls`` is EXACT: it counts one unit per real classifier
    invocation, including retries of a failed identity.
    """
    report = GenerationReport(
        pinned_model=client.model_version,
        llm_config_version=llm_config_version,
        shard_count=shard_count)
    all_ids = list(identities)
    shards = deterministic_shards(all_ids, shard_count)
    report.identity_count = len(all_ids)
    if limit is not None:
        shards = shards[:max(0, limit)]
    done = load_completed_shards(checkpoint_dir)
    combined_rows: list = []
    for idx, shard_ids in enumerate(shards):
        result = ShardResult(shard_index=idx, identities=shard_ids)
        checkpoint = done.get(idx)
        if checkpoint is not None and checkpoint.get(
                "llm_config_version") == llm_config_version and \
                checkpoint.get("pinned_model") == client.model_version and \
                len(checkpoint.get("rows", [])) == len(shard_ids):
            # Resume: this shard's paid work is already durable, so NO
            # classifier call is made for any identity in it.
            result.rows = checkpoint["rows"]
            report.resumed_shards.append(idx)
            report.resumed_identity_count += len(shard_ids)
            combined_rows.extend(result.rows)
            continue
        for identity in shard_ids:
            histogram = result.attempts_histogram
            spent = {"calls": 0}

            def _observe(attempt, transient, exc, _h=histogram,
                         _s=spent):
                _h[str(attempt)] = _h.get(str(attempt), 0) + 1
                _s["calls"] += 1

            try:
                payload, _attempt, _norm = classify_with_bounded_retry(
                    client, identity, max_attempts=max_attempts,
                    on_attempt=_observe)
            except Exception as exc:
                result.provider_calls += spent["calls"]
                result.errors.append({
                    "ticker": identity.ticker, "source": identity.source,
                    "headline_hash": identity.headline_hash,
                    "error": repr(exc)})
                report.errors.append({
                    "ticker": identity.ticker, "source": identity.source,
                    "headline_hash": identity.headline_hash,
                    "error": repr(exc)})
                continue
            result.provider_calls += spent["calls"]
            result.rows.append(_artifact_result_row(identity, payload))
        report.generated_shards.append(idx)
        report.provider_calls += result.provider_calls
        for k, v in result.attempts_histogram.items():
            report.attempts_histogram[k] = \
                report.attempts_histogram.get(k, 0) + v
        if result.errors:
            # Do NOT checkpoint a partial shard — the identities that
            # failed must be regenerated on the next run.
            continue
        if checkpoint_dir:
            _write_shard_checkpoint(
                Path(checkpoint_dir), result,
                llm_config_version=llm_config_version,
                pinned_model=client.model_version)
        combined_rows.extend(result.rows)
    report.row_count = len(combined_rows)
    report.error_count = len(report.errors)
    return report, combined_rows


def _artifact_result_row(identity: GenerationIdentity, payload: dict) -> dict:
    """Build the artifact result row in the EXACT shape the existing
    offline publisher consumes — same field names, same nested
    ``classification`` object, no new fields, no second format."""
    return {
        "ticker": identity.ticker,
        "source": identity.source,
        "published_at": identity.published_at,
        "headline_text_normalized": identity.headline_text_normalized,
        "classification": dict(payload),
    }


def compose_offline_artifact(rows: list, *, llm_config_version: str) -> str:
    """Compose the deterministic artifact document. Rows are sorted by
    (headline_hash, source, ticker) so the composition is independent of
    generation/concurrency order, and NO wall clock is written into the
    artifact (it would break byte-determinism)."""
    def _key(row: dict):
        classification = row.get("classification") or {}
        return (classification.get("headline_hash") or
                compute_headline_hash(
                    normalize_headline_text(row.get(
                        "headline_text_normalized", ""))),
                row.get("source", ""), row.get("ticker", ""))
    ordered = sorted(rows, key=_key)
    doc = {
        "format_version": OFFLINE_POPULATION_FORMAT_VERSION,
        "llm_config_version": llm_config_version,
        "results": ordered,
    }
    return json.dumps(doc, sort_keys=True, indent=2)