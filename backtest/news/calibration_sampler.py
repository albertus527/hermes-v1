"""R2.8.1 Phase-2 — deterministic calibration-sample selection + worksheet.

Selects a deterministic sample of human-calibration headlines from the
ALREADY-PERSISTED canonical ``news_headlines`` inventory (§16 schema).
The sampler never fetches provider data, never calls an LLM, and never
reads provider sentiment/relevance fields (the schema has none, and the
SELECT projects only canonical fields).

Determinism contract: same database snapshot + same SampleConfig =>
the same ordered sample, independent of database row order, wall-clock
time, Python hash randomization, and random-module state. Selection
keys are derived from SHA-256 over an explicit seed + stable canonical
identifiers.

Spec §11.4 requires >= 200 manually labeled headlines but does NOT
prescribe how the sample is distributed across ticker / year / source.
The R2.8.1 calibration sampling policy has been HUMAN-ADJUDICATED and is
implemented as the PRODUCTION policy: ticker × calendar-year strata over
the requested universe/window, equal floor allocation per stratum, and a
deterministic SHA-256-ranked remainder (``_remainder_rank``). Source is
not quota-bearing and provider sentiment/relevance fields are never read.

Partial-corpus safety: FINAL worksheet generation requires, per the
EXISTING coverage-manifest semantics (``coverage_manifests``,
``source_kind='NEWS'``, ``verified=1``, run-pinned manifest_version),
that the union of verified spans cover the ENTIRE requested corpus
window for every requested ticker. No competing completeness definition
is introduced; an unverified window fails closed.
"""

from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SAMPLER_FORMAT_VERSION = "r281-calibration-sample-1"

#: Mechanism default only — NOT the normative R2.8.1 sampling policy.
DEFAULT_STRATA = ("ticker", "year")
ALLOWED_STRATA_FIELDS = ("ticker", "year", "source")

WORKSHEET_COLUMNS = (
    "sample_id",
    "headline_hash",
    "ticker",
    "published_at",
    "source",
    "headline_text",
    "human_label",
    "human_notes",
)


class CalibrationSamplingError(Exception):
    """The requested sampling contract cannot be satisfied (fail-closed)."""


class CoverageIncompleteError(CalibrationSamplingError):
    """The verified NEWS coverage does not attest the requested window."""


@dataclass(frozen=True)
class SampleConfig:
    tickers: tuple[str, ...]
    start: str                     # inclusive ISO timestamp/date bound
    end: str                       # inclusive
    size: int
    seed: str
    strata: tuple[str, ...] = DEFAULT_STRATA
    manifest_version: str = ""

    def __post_init__(self) -> None:
        if not self.tickers:
            raise CalibrationSamplingError("at least one ticker is required")
        if len(set(self.tickers)) != len(self.tickers):
            raise CalibrationSamplingError("tickers must be unique")
        if self.size <= 0:
            raise CalibrationSamplingError("sample size must be positive")
        for f in self.strata:
            if f not in ALLOWED_STRATA_FIELDS:
                raise CalibrationSamplingError(
                    f"stratum field {f!r} not in {ALLOWED_STRATA_FIELDS}")
        if len(set(self.strata)) != len(self.strata):
            raise CalibrationSamplingError("strata fields must be unique")
        if not self.seed:
            raise CalibrationSamplingError("a non-empty seed is required")
        if not self.start or not self.end:
            raise CalibrationSamplingError("start and end bounds are required")


@dataclass(frozen=True)
class SelectedHeadline:
    sample_id: str
    headline_hash: str
    ticker: str
    published_at: str
    source: str
    headline_text: str


def _parse_ts(value: str) -> _dt.datetime:
    """Parse a stored canonical published_at / span bound (ISO-8601)."""
    return _dt.datetime.fromisoformat(value)


def _window_bounds(start: str, end: str) -> tuple[_dt.datetime, _dt.datetime]:
    """Resolve possibly date-only bounds to an inclusive UTC window.

    Date-only bounds follow the ingestion convention: start at midnight
    UTC, end at 23:59:59 UTC (the NEWS manifest span convention).
    Timestamp bounds are used as given; ``end`` is inclusive.
    """
    if len(start) == 10:
        start_dt = _dt.datetime.fromisoformat(start).replace(tzinfo=_dt.timezone.utc)
    else:
        start_dt = _parse_ts(start)
    if len(end) == 10:
        end_dt = (_dt.datetime.fromisoformat(end).replace(tzinfo=_dt.timezone.utc)
                  + _dt.timedelta(hours=23, minutes=59, seconds=59))
    else:
        end_dt = _parse_ts(end)
    if end_dt < start_dt:
        raise CalibrationSamplingError(
            f"window end {end!r} precedes start {start!r}")
    return start_dt, end_dt


def _eligible_rows(conn: sqlite3.Connection, config: SampleConfig) -> list[dict]:
    """Canonical, timed, in-window eligible headlines — canonical fields
    ONLY, ordered deterministically (never DB row order)."""
    start_dt, end_dt = _window_bounds(config.start, config.end)
    placeholders = ",".join("?" for _ in config.tickers)
    rows = conn.execute(
        "SELECT ticker, headline_hash, source, published_at, "
        "headline_text_normalized FROM news_headlines "
        f"WHERE ticker IN ({placeholders}) AND published_at IS NOT NULL "
        "ORDER BY ticker, headline_hash, source, published_at",
        tuple(config.tickers),
    ).fetchall()
    eligible: list[dict] = []
    for ticker, h, source, published_at, text in rows:
        ts = _parse_ts(published_at)
        if start_dt <= ts <= end_dt:
            eligible.append({
                "ticker": ticker,
                "headline_hash": h,
                "source": source,
                "published_at": published_at,
                "headline_text": text,
                "year": str(ts.year),
            })
    return eligible


def _stratum_key(row: dict, strata: tuple[str, ...]) -> str:
    return "|".join(str(row[f]) for f in strata)


def _select_key(seed: str, stratum_key: str, row: dict) -> str:
    """Deterministic per-headline selection key (SHA-256 over explicit
    seed + stable canonical identifiers). No random module, no
    hash() (PYTHONHASHSEED-proof), no wall-clock."""
    material = "|".join([
        seed, stratum_key, row["headline_hash"], row["ticker"],
        row["source"], row["published_at"],
    ]).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _remainder_rank(seed: str, stratum_key: str) -> str:
    """Deterministic remainder-eligibility rank for a stratum.

    SHA-256 over the sampler format/version + the explicit seed + the
    canonical stratum key ONLY. Independent of database row order,
    ticker alphabetical order, Python hash randomization, and wall
    clock (adjudicated R2.8.1 remainder-allocation policy).
    """
    material = "|".join([
        SAMPLER_FORMAT_VERSION, seed, stratum_key,
    ]).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _allocate_quotas(strata_keys: list[str], size: int,
                     seed: str) -> dict[str, int]:
    """R2.8.1 PRODUCTION calibration sampling policy (human-adjudicated):

    - equal floor allocation to every required stratum
    - remainder slots go to the first N strata in the deterministic
      SHA-256 ranking (``_remainder_rank``), NOT lexical/sorted position
    """
    base, remainder = divmod(size, len(strata_keys))
    ranked = sorted(
        strata_keys, key=lambda k: (_remainder_rank(seed, k), k))
    quotas = {key: base for key in strata_keys}
    for key in ranked[:remainder]:
        quotas[key] += 1
    return quotas


def select_sample(conn: sqlite3.Connection, config: SampleConfig) -> tuple[list[SelectedHeadline], dict]:
    """Select the ordered deterministic sample.

    Returns (ordered sample, selection metadata). Raises
    :class:`CalibrationSamplingError` when the eligible corpus cannot
    satisfy the exact requested contract (total or per-stratum
    insufficiency) — never silently substitutes or lowers the target.
    """
    eligible = _eligible_rows(conn, config)
    by_stratum: dict[str, list[dict]] = {}
    for row in eligible:
        by_stratum.setdefault(_stratum_key(row, config.strata), []).append(row)
    if not by_stratum:
        raise CalibrationSamplingError(
            "no eligible timed headlines in the requested window/tickers")
    if len(eligible) < config.size:
        raise CalibrationSamplingError(
            f"insufficient eligible corpus: {len(eligible)} eligible "
            f"headlines < requested sample size {config.size}")

    quotas = _allocate_quotas(list(by_stratum), config.size, config.seed)
    selected: list[dict] = []
    shortfalls = []
    for stratum_key, quota in quotas.items():
        pool = by_stratum[stratum_key]
        if len(pool) < quota:
            shortfalls.append(
                f"stratum {stratum_key!r}: {len(pool)} eligible < quota {quota}")
            continue
        ranked = sorted(
            pool,
            key=lambda r: (_select_key(config.seed, stratum_key, r),
                           r["headline_hash"], r["published_at"], r["source"]),
        )
        selected.extend(ranked[:quota])
    if shortfalls:
        raise CalibrationSamplingError(
            "stratum insufficiency — refusing to substitute or lower the "
            "requested target: " + "; ".join(shortfalls))

    ordered = sorted(
        selected,
        key=lambda r: (r["published_at"], r["headline_hash"],
                       r["source"], r["ticker"]),
    )
    sample = [
        SelectedHeadline(
            sample_id=f"CAL-{i:04d}-{r['headline_hash']}",
            headline_hash=r["headline_hash"],
            ticker=r["ticker"],
            published_at=r["published_at"],
            source=r["source"],
            headline_text=r["headline_text"],
        )
        for i, r in enumerate(ordered, start=1)
    ]
    metadata = {
        "sampler_format_version": SAMPLER_FORMAT_VERSION,
        "requested_sample_size": config.size,
        "seed": config.seed,
        "strata": list(config.strata),
        "tickers": list(config.tickers),
        "window_start": config.start,
        "window_end": config.end,
        "eligible_count": len(eligible),
        "output_row_count": len(sample),
        "sample_digest": sample_digest(sample),
        "quota_allocation": dict(sorted(quotas.items())),
        "sampling_policy": {
            "status": "FINAL — human-adjudicated R2.8.1 production policy",
            "target_sample_size": config.size,
            "primary_strata": list(config.strata),
            "quota_rule": "equal floor allocation to every required stratum",
            "remainder_rule": (
                "one extra slot to each of the first N (N = size mod "
                "stratum_count) strata ranked by SHA-256 over "
                "sampler_format_version + seed + canonical stratum key; "
                "independent of DB row order, ticker alphabetical order, "
                "Python hash randomization, and wall clock"),
            "source_quota_bearing": False,
            "source_role": "diagnostic reporting only; never alters selection",
            "proportional_volume_weighting": False,
            "provider_sentiment_or_relevance_inputs": False,
            "insufficient_stratum_behavior": (
                "fail closed; no redistribution or substitution from any "
                "other stratum"),
        },
    }
    return sample, metadata


def sample_digest(sample: list[SelectedHeadline]) -> str:
    """Stable digest of the ordered selected sample (content-only; no
    wall-clock, no file names, no paths)."""
    rows = [
        {k: getattr(s, k) for k in (
            "sample_id", "headline_hash", "ticker", "published_at",
            "source", "headline_text")}
        for s in sample
    ]
    blob = json.dumps(rows, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# Finalization guard — reuses the EXISTING coverage-manifest semantics
# ---------------------------------------------------------------------------

def _verified_spans(conn: sqlite3.Connection, ticker: str,
                    manifest_version: str) -> list[tuple[_dt.datetime, _dt.datetime]]:
    rows = conn.execute(
        "SELECT span_start, span_end FROM coverage_manifests WHERE "
        "source_kind='NEWS' AND ticker=? AND verified=1 AND manifest_version=? "
        "ORDER BY span_start",
        (ticker, manifest_version),
    ).fetchall()
    return [(_parse_ts(a), _parse_ts(b)) for a, b in rows]


def verify_corpus_coverage(conn: sqlite3.Connection, config: SampleConfig) -> dict:
    """FINAL-generation guard: for every requested ticker the union of
    VERIFIED NEWS covered spans (existing ``coverage_manifests``
    semantics, run-pinned ``manifest_version``) must cover the ENTIRE
    requested window. Raises :class:`CoverageIncompleteError` on any
    gap — the partial AV corpus can never pass."""
    if not config.manifest_version:
        raise CoverageIncompleteError(
            "FINAL generation requires --manifest-version (the run-pinned "
            "NEWS coverage manifest_version)")
    start_dt, end_dt = _window_bounds(config.start, config.end)
    evidence = {"manifest_version": config.manifest_version, "tickers": {}}
    for ticker in config.tickers:
        spans = sorted(_verified_spans(conn, ticker, config.manifest_version))
        merged: list[list[_dt.datetime]] = []
        for a, b in spans:
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
        covered_frac = None
        if gaps:
            raise CoverageIncompleteError(
                f"verified NEWS coverage does NOT attest the requested "
                f"window for {ticker} (manifest_version="
                f"{config.manifest_version!r}); uncovered sub-intervals: "
                + "; ".join(f"[{a}, {b}]" for a, b in gaps))
        evidence["tickers"][ticker] = {
            "window_start": start_dt.isoformat(),
            "window_end": end_dt.isoformat(),
            "verified_span_count": len(spans),
            "fully_covered": True,
        }
    return evidence


# ---------------------------------------------------------------------------
# Worksheet generation
# ---------------------------------------------------------------------------

def write_worksheet(sample: list[SelectedHeadline], metadata: dict,
                    out_csv: Path, out_meta: Path, *, mode: str,
                    generated_at: str | None = None) -> None:
    """Write the human-label worksheet + reproducibility metadata.

    ``human_label`` and ``human_notes`` are ALWAYS blank. No
    model-generated ground truth, no provider sentiment/relevance.
    ``generated_at`` (if provided) is provenance-only and never enters
    the sample digest.
    """
    if mode not in ("PREVIEW", "FINAL"):
        raise CalibrationSamplingError(f"unknown mode {mode!r}")
    meta = dict(metadata)
    meta["mode"] = mode
    meta["final"] = mode == "FINAL"
    if mode == "PREVIEW":
        meta["preview_warning"] = (
            "PREVIEW / NON-FINAL — generated from a possibly INCOMPLETE "
            "corpus; must never be represented as the final R2.8.1 "
            "human calibration dataset")
    if generated_at is not None:
        meta["generated_at"] = generated_at   # provenance only
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(WORKSHEET_COLUMNS)
        for s in sample:
            writer.writerow([
                s.sample_id, s.headline_hash, s.ticker, s.published_at,
                s.source, s.headline_text, "", "",
            ])
    from utils import atomic_write_text
    atomic_write_text(Path(out_meta), json.dumps(meta, sort_keys=True, indent=2))
