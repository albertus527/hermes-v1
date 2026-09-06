"""R2.8.1 Phase-2 — OFFLINE classification-cache population plumbing.

Consumes a PRE-RECORDED offline classification-result artifact (§20
Phase 2, offline injection path — the same contract the live population
job writes); it NEVER calls an LLM, never touches OpenRouter, never
reads the network, and never classifies a headline itself. The artifact
must carry the full strict news_schema_v3 payload per headline, plus
explicit classifier-provenance, and is validated against the canonical
``news_headlines`` inventory and the run-pinned verified NEWS coverage
manifests (existing ``coverage_manifests`` semantics — no new
completeness definition).

Validated rows are published ATOMICALLY for the requested population
unit (all-or-nothing) via a caller-owned SAVEPOINT over the commit-free
cache insert (``_insert_no_commit`` — wrapping the commitful insert
would destroy the savepoint and silently publish partial work; see
references/alphavantage-news-atomic-publication-fp4-empty.md).

Conflict semantics (existing frozen contracts, nothing invented):
- identical replay of an existing cache key (same payload) is idempotent;
- a DIFFERING payload for an existing key raises
  :class:`CacheKeyConflictError` (§11.5 historical caches are never
  overwritten — no replacement semantics exist in the spec);
- differing effect fields across (headline_hash, ticker) raise
  ``NewsCacheIntegrityFailure`` (P-4, §16 rule 9).

A PREVIEW/VALIDATE mode performs every check but never writes the cache.

Pinned-classifier guard: the population caller must supply the EXACT
expected classifier pin (``expected_model_version`` — the full
``openrouter/<provider>/<model>@<version>`` identifier per §11.5) and
the expected contract/prompt version (``llm_config_version``). Every
result row must match; mixed provenance within one artifact fails
closed. No real classifier identifier is hard-coded here.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from trading_core.errors import NewsCacheIntegrityFailure
from trading_core.news_effects import (
    Classification,
    headline_hash as compute_headline_hash,
    normalize_headline_text,
)

from backtest.news.cache import (
    CACHE_KEY_COLUMNS,
    CacheKeyConflictError,
    HeadlineInventory,
    MalformedClassificationError,
    NewsClassificationCache,
    validate_classification_payload,
)

__all__ = [
    "OFFLINE_POPULATION_FORMAT_VERSION",
    "ArtifactFormatError",
    "PopulationProvenanceError",
    "PopulationCompletenessError",
    "CoverageGuardError",
    "OfflinePopulationReport",
    "load_offline_population_artifact",
    "populate_cache_from_offline_artifact",
]

#: Artifact format version (the smallest deterministic offline
#: production-artifact format; distinct from the benchmark candidate
#: format, which lacks production provenance).
OFFLINE_POPULATION_FORMAT_VERSION = "r281-offline-classification-1"

#: Required per-row provenance fields (§11.5 pin components).
_PIN_FIELDS = ("model_version", "schema_version")


class ArtifactFormatError(Exception):
    """The offline artifact violates the deterministic input contract."""


class PopulationProvenanceError(Exception):
    """Classifier provenance does not match the expected pin (fail
    closed — never silently accept results from another classifier)."""


class PopulationCompletenessError(Exception):
    """The artifact identity set does not EXACTLY equal the canonical
    expected identity set for the explicitly declared population scope
    (missing, extra, or duplicate identity — all fail closed; a strict
    subset of the canonical inventory can never be published as
    complete)."""


class CoverageGuardError(Exception):
    """FINAL population refused: verified NEWS coverage does not attest
    the requested population inventory (existing §11.6 semantics)."""


@dataclass(frozen=True)
class OfflineArtifactRow:
    """One validated offline classification result."""
    ticker: str
    source: str
    headline_hash: str
    payload: dict          # full strict news_schema_v3 payload


@dataclass
class OfflinePopulationReport:
    """Deterministic population/audit record (§20 Phase 2 shape,
    offline path). Wall-clock is provenance-only and never digested."""
    format_version: str = OFFLINE_POPULATION_FORMAT_VERSION
    mode: str = "PREVIEW"                     # PREVIEW | FINAL
    expected_model_version: str = ""
    llm_config_version: str = ""
    requested_tickers: list = field(default_factory=list)
    requested_start: str = ""                 # explicit scope input; never
    requested_end: str = ""                   #   inferred from the artifact
    requested_manifest_versions: list = field(default_factory=list)
    expected_identity_count: int = 0          # canonical expected inventory
    artifact_identity_count: int = 0
    missing_identity_count: int = 0
    extra_identity_count: int = 0
    validated_identity_count: int = 0
    missing_identities: list = field(default_factory=list)
    extra_identities: list = field(default_factory=list)
    coverage_manifest_versions: list = field(default_factory=list)
    input_row_count: int = 0
    validated_row_count: int = 0
    inserted_row_count: int = 0
    idempotent_existing_row_count: int = 0
    coverage_evidence: list = field(default_factory=list)
    input_digest: str = ""
    classifier_pin_digest: str = ""
    errors: list = field(default_factory=list)
    complete: bool = False
    phase2_complete: bool = False             # always False (see below)
    phase2_note: str = (
        "Cache population success does NOT make Phase 2 complete; the "
        "human-labeled calibration worksheet, the §11.4 ≥200-headline "
        "benchmark, and HUMAN classifier adjudication remain outstanding.")
    generated_at: str | None = None           # provenance ONLY; never digested

    def to_json(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True, indent=2)


def _stable_digest(obj) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# Artifact loading + validation (fail-closed; nothing skipped)
# ---------------------------------------------------------------------------

def _parse_iso(value, where: str):
    import datetime as _dt
    try:
        parsed = _dt.datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ArtifactFormatError(
            f"{where}: published_at {value!r} is not an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise ArtifactFormatError(
            f"{where}: published_at {value!r} lacks timezone offset")
    return parsed


def load_offline_population_artifact(
    artifact_path,
    *,
    expected_model_version: str,
    llm_config_version: str,
) -> list[OfflineArtifactRow]:
    """Load + fully validate an offline classification-result artifact.

    Strict contract (JSON document):
      {
        "format_version": "r281-offline-classification-1",
        "llm_config_version": "...",
        "results": [
            {"ticker": ..., "source": ..., "published_at": ...,
             "headline_text_normalized": ...,
             "classification": { full strict news_schema_v3 payload }}
        ]
      }

    Every row must validate against the strict §11.1 validator; identity
    fields are recomputed from the headline text (the artifact may not
    assert identity); provenance must exactly match the expected pin and
    be uniform across the artifact. Duplicate (cache-key, ticker)
    identities within one artifact fail closed.
    """
    from backtest.news.classifier import PinnedModelMissing, parse_pinned_model
    try:
        model_version, _ = parse_pinned_model(expected_model_version)
    except PinnedModelMissing as exc:
        raise PopulationProvenanceError(
            f"expected classifier pin invalid: {exc}") from exc
    try:
        doc = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArtifactFormatError(f"artifact unreadable: {exc}") from exc
    if not isinstance(doc, dict):
        raise ArtifactFormatError("artifact is not a JSON object")
    if doc.get("format_version") != OFFLINE_POPULATION_FORMAT_VERSION:
        raise ArtifactFormatError(
            f"unsupported format_version {doc.get('format_version')!r} "
            f"(expected {OFFLINE_POPULATION_FORMAT_VERSION!r})")
    results = doc.get("results")
    if not isinstance(results, list) or not results:
        raise ArtifactFormatError("artifact has no results list")
    artifact_llm_cfg = str(doc.get("llm_config_version", "") or "")
    if artifact_llm_cfg != (llm_config_version or ""):
        raise PopulationProvenanceError(
            f"artifact llm_config_version {artifact_llm_cfg!r} does not "
            f"match expected {llm_config_version!r}")

    rows: list[OfflineArtifactRow] = []
    seen_ids: set[tuple[str, str, str, str]] = set()
    for i, item in enumerate(results):
        where = f"results[{i}]"
        if not isinstance(item, dict):
            raise ArtifactFormatError(f"{where}: not a JSON object")
        for req in ("ticker", "source", "published_at",
                    "headline_text_normalized", "classification"):
            if req not in item:
                raise ArtifactFormatError(f"{where}: missing {req!r}")
        ticker = item["ticker"]
        source = item["source"]
        published_at = _parse_iso(item["published_at"], where)
        text = item["headline_text_normalized"]
        if not isinstance(ticker, str) or not ticker.strip():
            raise ArtifactFormatError(f"{where}: ticker must be non-empty")
        if not isinstance(source, str) or not source.strip():
            raise ArtifactFormatError(f"{where}: source must be non-empty")
        if not isinstance(text, str):
            raise ArtifactFormatError(
                f"{where}: headline_text_normalized must be a string")
        norm = normalize_headline_text(text)
        if not norm:
            # FP-4-empty rows never reach the canonical inventory, so a
            # result for one cannot be valid (strict, not dropped).
            raise ArtifactFormatError(
                f"{where}: headline_text_normalized is FP-4-empty")
        h_hash = compute_headline_hash(norm)
        payload = item["classification"]
        if not isinstance(payload, dict):
            raise ArtifactFormatError(
                f"{where}: classification is not a JSON object")
        # Recompute identity/provenance fields — the artifact cannot
        # assert them (same policy as the live classifier client).
        stamped = dict(payload)
        stamped["ticker"] = ticker
        stamped["source"] = source
        stamped["published_at"] = published_at.isoformat()
        stamped["headline_hash"] = h_hash
        stamped["schema_version"] = "news_schema_v3"
        stamped["model_version"] = model_version
        for f in _PIN_FIELDS:
            supplied = payload.get(f)
            if supplied is not None and supplied != stamped[f]:
                raise PopulationProvenanceError(
                    f"{where}: provenance {f}={supplied!r} does not match "
                    f"the expected pin {stamped[f]!r}")
        try:
            validate_classification_payload(stamped)
        except MalformedClassificationError as exc:
            raise ArtifactFormatError(f"{where}: malformed classification: "
                                      f"{exc}") from exc
        identity = (h_hash, source, "news_schema_v3", model_version, ticker)
        if identity in seen_ids:
            raise ArtifactFormatError(
                f"{where}: duplicate result identity within artifact "
                f"(headline_hash={h_hash}, source={source}, "
                f"ticker={ticker})")
        seen_ids.add(identity)
        rows.append(OfflineArtifactRow(ticker=ticker, source=source,
                                       headline_hash=h_hash, payload=stamped))
    return rows


# ---------------------------------------------------------------------------
# Canonical inventory validation + atomic publication
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Declared population scope + canonical expected inventory
# ---------------------------------------------------------------------------

def _window_bounds(start: str, end: str):
    """Resolve possibly date-only bounds to an inclusive UTC window — the
    SAME convention as the calibration sampler (date-only bounds start at
    midnight UTC and end at 23:59:59 UTC, the NEWS manifest span
    convention; timestamp bounds are used as given)."""
    import datetime as _dt
    if len(start) == 10:
        start_dt = _dt.datetime.fromisoformat(
            start).replace(tzinfo=_dt.timezone.utc)
    else:
        start_dt = _parse_iso(start, "requested_start")
    if len(end) == 10:
        end_dt = (_dt.datetime.fromisoformat(end).replace(
            tzinfo=_dt.timezone.utc) + _dt.timedelta(
            hours=23, minutes=59, seconds=59))
    else:
        end_dt = _parse_iso(end, "requested_end")
    if end_dt < start_dt:
        raise ArtifactFormatError(
            f"requested window end {end!r} precedes start {start!r}")
    return start_dt, end_dt


def _expected_identity_set(inventory, conn, requested_tickers,
                           start_dt, end_dt):
    """Build the expected canonical identity set INDEPENDENTLY of the
    artifact: for each explicitly requested ticker, load canonical timed
    news_headlines, restrict to the requested window (existing R2.8.1
    timestamp/window convention — aware datetimes compared against the
    inclusive [start_dt, end_dt] bounds), and emit the existing
    classification identity (headline_hash, source, ticker). Untimed
    canonical headlines never enter the expected set (existing §11.1
    behavior: they are dropped at ingestion and produce no effect)."""
    expected: dict[tuple[str, str, str], str] = {}
    for ticker in requested_tickers:
        for h, s, t, pub, _text in inventory.timed_headlines_all():
            if t != ticker:
                continue
            at = _parse_iso(pub, f"canonical headline {h}")
            if start_dt <= at <= end_dt:
                expected[(h, s, ticker)] = pub
    return expected


def _verify_whole_window_coverage(conn, requested_tickers, start_dt,
                                  end_dt, manifest_versions) -> list:
    """Fail closed unless, for EVERY requested ticker, the UNION of
    verified NEWS covered spans (existing ``coverage_manifests``
    semantics, run-pinned manifest versions) covers the ENTIRE requested
    window. Span-union/gap logic reused from the calibration FINAL guard
    (``calibration_sampler.verify_corpus_coverage``). Interior gaps fail
    even when no artifact headline occurs inside them."""
    import datetime as _dt
    evidence = []
    for ticker in requested_tickers:
        spans = []
        for mv in manifest_versions:
            for a, b in conn.execute(
                    "SELECT span_start, span_end FROM coverage_manifests "
                    "WHERE source_kind='NEWS' AND ticker=? AND verified=1 "
                    "AND manifest_version=?", (ticker, mv)).fetchall():
                spans.append((_parse_iso(a, "span_start"),
                              _parse_iso(b, "span_end")))
        merged: list[list] = []
        for a, b in sorted(spans):
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        gaps = []
        cursor = start_dt
        for a, b in merged:
            if a > cursor:
                gaps.append((cursor.isoformat(), min(a, end_dt).isoformat()))
            if b > cursor:
                cursor = b
            if cursor > end_dt:
                break
        if cursor < end_dt:
            gaps.append((cursor.isoformat(), end_dt.isoformat()))
        evidence.append({
            "ticker": ticker,
            "window_start": start_dt.isoformat(),
            "window_end": end_dt.isoformat(),
            "verified_span_count": len(spans),
            "merged_span_count": len(merged),
            "uncovered_sub_intervals": [
                {"start": g[0], "end": g[1]} for g in gaps],
            "fully_covered": not gaps,
        })
        if gaps:
            raise CoverageGuardError(
                f"verified NEWS coverage does NOT attest the ENTIRE "
                f"requested window for ticker={ticker} (manifest_versions="
                f"{list(manifest_versions)}); uncovered sub-intervals: "
                + "; ".join(f"[{a}, {b}]" for a, b in gaps))
    return evidence


def populate_cache_from_offline_artifact(
    artifact_path,
    *,
    cache: NewsClassificationCache,
    inventory: HeadlineInventory,
    expected_model_version: str,
    manifest_versions: list,
    requested_tickers: list,
    requested_start: str,
    requested_end: str,
    llm_config_version: str = "",
    final: bool = False,
    run_id: str = "offline-news-cache-population",
    config_version: int = 0,
    code_commit: str = "",
    classified_at_wallclock: str | None = None,
) -> OfflinePopulationReport:
    """Validate an offline classification artifact against the EXPLICITLY
    DECLARED population scope (requested tickers / start / end /
    run-pinned NEWS manifest versions — never inferred from the artifact)
    and — in FINAL mode only — atomically publish it into the existing
    classification cache.

    Completeness contract (fail closed in BOTH modes):
      artifact_identity_set == canonical_expected_identity_set
    where the expected set is built independently from the canonical
    inventory restricted to the declared scope. Missing, extra, or
    duplicate identities refuse population before any write.

    Coverage contract (fail closed in BOTH modes): for every requested
    ticker, the union of verified NEWS covered spans under the pinned
    manifest versions must cover the ENTIRE requested window (existing
    ``coverage_manifests`` semantics — interior gaps fail even when no
    artifact headline occurs inside them).

    PREVIEW mode (final=False) performs every check and writes NOTHING.
    """
    from backtest.news.classifier import PinnedModelMissing, parse_pinned_model
    try:
        model_version, _ = parse_pinned_model(expected_model_version)
    except PinnedModelMissing as exc:
        raise PopulationProvenanceError(str(exc)) from exc
    manifest_versions = [m for m in manifest_versions if m]
    if not manifest_versions:
        raise CoverageGuardError(
            "no run-pinned NEWS coverage manifest_version supplied (§11.6)")
    if not requested_tickers:
        raise ArtifactFormatError(
            "population scope requires explicitly requested tickers — "
            "scope is NEVER inferred from the artifact")
    if not requested_start or not requested_end:
        raise ArtifactFormatError(
            "population scope requires explicit --start/--end bounds — "
            "scope is NEVER inferred from the artifact")
    requested_tickers = sorted({t.strip() for t in requested_tickers
                                if t and t.strip()})
    if not requested_tickers:
        raise ArtifactFormatError("requested tickers are empty")

    rows = load_offline_population_artifact(
        artifact_path,
        expected_model_version=expected_model_version,
        llm_config_version=llm_config_version)

    conn = inventory._conn  # noqa: SLF001 — same-package read helper
    start_dt, end_dt = _window_bounds(requested_start, requested_end)

    # --- expected canonical inventory, built INDEPENDENTLY of the artifact
    expected = _expected_identity_set(
        inventory, conn, requested_tickers, start_dt, end_dt)

    report = OfflinePopulationReport(
        mode="FINAL" if final else "PREVIEW",
        expected_model_version=model_version,
        llm_config_version=llm_config_version,
        requested_tickers=list(requested_tickers),
        requested_start=requested_start,
        requested_end=requested_end,
        requested_manifest_versions=list(manifest_versions),
        coverage_manifest_versions=list(manifest_versions),
        expected_identity_count=len(expected),
        artifact_identity_count=len(rows),
        input_row_count=len(rows),
    )

    # --- canonical inventory validation of artifact rows (fail closed,
    # nothing skipped) ------------------------------------------------------
    canonical: dict[tuple[str, str], tuple] = {}
    for r in inventory.timed_headlines_all():
        canonical[(r[0], r[1])] = r  # (headline_hash, source) -> row
    validated = []
    seen = set()
    for row in rows:
        key = (row.headline_hash, row.source)
        if key not in canonical:
            report.errors.append({
                "error_class": "unknown_headline_identity",
                "headline_hash": row.headline_hash, "source": row.source})
            continue
        chash, csource, cticker, cpublished, ctext = canonical[key]
        if cticker != row.ticker:
            report.errors.append({
                "error_class": "ticker_mismatch",
                "headline_hash": row.headline_hash,
                "expected_ticker": cticker, "got_ticker": row.ticker})
            continue
        if row.ticker not in requested_tickers:
            report.errors.append({
                "error_class": "identity_outside_declared_scope",
                "headline_hash": row.headline_hash, "source": row.source,
                "ticker": row.ticker})
            continue
        # The artifact may not relocate a canonical headline in time: the
        # row's published_at must equal the canonical published_at (a
        # mismatch would silently move the headline outside the declared
        # window / coverage semantics).
        if _parse_iso(row.payload["published_at"], row.headline_hash) != \
                _parse_iso(cpublished, f"canonical {row.headline_hash}"):
            report.errors.append({
                "error_class": "published_at_mismatch",
                "headline_hash": row.headline_hash, "source": row.source,
                "ticker": row.ticker,
                "expected_published_at": cpublished,
                "got_published_at": row.payload["published_at"]})
            continue
        identity = (row.headline_hash, row.source, row.ticker)
        if identity in seen:
            report.errors.append({
                "error_class": "duplicate_identity",
                "headline_hash": row.headline_hash, "source": row.source,
                "ticker": row.ticker})
            continue
        seen.add(identity)
        validated.append(row)

    # --- EXACT set-equality completeness (fail closed BEFORE any write,
    # in BOTH modes; verified coverage does NOT excuse a missing
    # classification) -------------------------------------------------------
    artifact_ids = set(seen)
    expected_ids = set(expected)
    missing = sorted(expected_ids - artifact_ids)
    extra = sorted(artifact_ids - expected_ids)
    report.missing_identity_count = len(missing)
    report.extra_identity_count = len(extra)
    report.missing_identities = [
        {"headline_hash": h, "source": s, "ticker": t}
        for h, s, t in missing]
    report.extra_identities = [
        {"headline_hash": h, "source": s, "ticker": t}
        for h, s, t in extra]
    if extra:
        report.errors.append({
            "error_class": "artifact_identity_outside_expected_inventory",
            "count": len(extra)})
    if missing:
        report.errors.append({
            "error_class": "missing_requested_identity",
            "count": len(missing)})
    if report.errors:
        report.validated_identity_count = len(validated)
        report.complete = False
        return report  # fail closed BEFORE any write; no partial state

    # FP-4 recomputation guard: text -> hash must equal the inventory hash.
    for row in validated:
        ctext = canonical[(row.headline_hash, row.source)][4]
        if compute_headline_hash(normalize_headline_text(ctext)) != \
                row.headline_hash:
            raise ArtifactFormatError(
                f"headline_hash mismatch for headline_hash="
                f"{row.headline_hash!r}")

    report.validated_row_count = len(validated)
    report.validated_identity_count = len(validated)

    # --- digests (order-independent; computed BEFORE any write) ----------
    report.input_digest = _stable_digest([
        {"headline_hash": r.headline_hash, "source": r.source,
         "ticker": r.ticker, "published_at": r.payload["published_at"],
         "classification": r.payload}
        for r in sorted(validated, key=lambda r: (r.headline_hash, r.source,
                                                  r.ticker))])
    report.classifier_pin_digest = _stable_digest({
        "model_version": model_version,
        "schema_version": "news_schema_v3",
        "llm_config_version": llm_config_version})

    # --- whole-scope coverage guard: the union of verified NEWS covered
    # spans must cover the ENTIRE requested window for EVERY requested
    # ticker (existing coverage_manifests semantics; calibration-guard
    # span-union/gap logic). Artifact-row timestamps are irrelevant to
    # this check — an interior gap fails even with no headline inside it.
    report.coverage_evidence = _verify_whole_window_coverage(
        conn, requested_tickers, start_dt, end_dt, manifest_versions)

    if not final:
        report.complete = True
        return report  # PREVIEW: zero writes

    # --- FINAL: atomic publication, one caller-owned SAVEPOINT -----------
    if cache._conn.in_transaction:
        # Do not adopt an unknown outer transaction: refuse rather than
        # risk publishing foreign work together with ours.
        raise ArtifactFormatError(
            "cache connection has an open transaction; refusing to "
            "publish (transaction ownership must be unambiguous)")
    inserted = 0
    idempotent = 0
    cache._conn.execute("SAVEPOINT population_unit")
    try:
        cached: set[tuple] = cache.cached_keys()
        for row in validated:
            classification = validate_classification_payload(row.payload)
            wrote = cache._insert_no_commit(
                classification, payload=row.payload,
                classified_at_wallclock=classified_at_wallclock,
                run_id=run_id, config_version=config_version,
                code_commit=code_commit)
            key = (row.headline_hash, row.source, "news_schema_v3",
                   model_version, row.ticker)
            cached.add(key)
            if wrote:
                inserted += 1
            else:
                idempotent += 1
        cache._conn.execute("RELEASE population_unit")
        cache._conn.commit()
    except (CacheKeyConflictError, NewsCacheIntegrityFailure,
            MalformedClassificationError, sqlite3.Error) as exc:
        cache._conn.execute("ROLLBACK TO population_unit")
        cache._conn.execute("RELEASE population_unit")
        cache._conn.rollback()
        if isinstance(exc, (CacheKeyConflictError, NewsCacheIntegrityFailure)):
            raise
        raise ArtifactFormatError(f"publication failed: {exc}") from exc
    report.inserted_row_count = inserted
    report.idempotent_existing_row_count = idempotent
    report.complete = True
    return report
