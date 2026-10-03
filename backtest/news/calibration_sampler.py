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
implemented as the PRODUCTION policy:

- HYBRID stratification — canonical STOCKS stratify by
  ``ticker | calendar_year``; canonical ETFs stratify by ``ticker`` ONLY.
  Instrument class comes from the canonical ``universe.yaml`` artifact
  (never symbol-naming heuristics, never DB row density), so the sparse
  ETF tier cannot manufacture unsatisfiable per-year strata.
- one MANDATORY base item for every required stratum (the full
  cross-product of requested tickers, window years, and class strata);
  remaining slots are allocated capacity-aware in the deterministic
  SHA-256 ranking (``_remainder_rank``).
- source is NOT quota-bearing and provider sentiment/relevance fields
  are never read.

Coverage-interval convention (R2.8.1): persisted ``coverage_manifests``
spans are CLOSED, SECOND-RESOLUTION intervals, so consecutive calendar
partitions (``...23:59:59`` -> ``...00:00:00``) ARE adjacent. This is
exact adjacency implied by the persisted timestamp resolution, NOT a
tolerance — any larger hole fails closed (:func:`merge_verified_spans`).

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

SAMPLER_FORMAT_VERSION = "r281-calibration-sample-2"

#: Mechanism default only — NOT the normative R2.8.1 sampling policy.
#: The EMPTY tuple is the sentinel for the human-adjudicated HYBRID
#: policy (``strata_for_config``); any non-empty value is an explicit
#: caller override applied to every requested ticker.
DEFAULT_STRATA: tuple[str, ...] = ()
ALLOWED_STRATA_FIELDS = ("ticker", "year", "source")

#: Strata assigned to each canonical instrument class by the
#: HUMAN-ADJUDICATED hybrid stratification policy. Stock tickers stratify
#: by ticker x calendar year; ETF tickers stratify by TICKER ONLY (the ETF
#: tier of the canonical corpus is too sparse to support per-year strata).
#: Class is read from the canonical ``universe.yaml`` artifact — never
#: from symbol-naming heuristics and never from DB row density.
CLASS_STRATA = {"stock": ("ticker", "year"), "etf": ("ticker",)}

#: R2.8.1 coverage-interval convention (see ``merge_verified_spans``).
#: Persisted ``coverage_manifests`` spans are CLOSED, SECOND-RESOLUTION
#: intervals: ``span_end`` is the last covered second, so consecutive
#: calendar partitions (``...23:59:59`` then ``...00:00:00``) ARE adjacent.
#: This is NOT a tolerance — exact adjacency is the only adjacency the
#: persisted timestamp resolution implies. A two-second (or larger) hole
#: still fails closed, and the requested window's own bounds must still be
#: covered exactly.
COVERAGE_INTERVAL_RESOLUTION = _dt.timedelta(seconds=1)
COVERAGE_INTERVAL_CONVENTION = (
    "closed second-resolution spans; two spans are contiguous iff "
    "next.span_start <= previous.span_end + 1 second (exact adjacency "
    "implied by the persisted timestamp resolution — NOT a tolerance); "
    "the requested window bounds themselves must be covered exactly"
)

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


class InstrumentClassError(CalibrationSamplingError):
    """The canonical instrument class could not be resolved fail-closed."""


# ---------------------------------------------------------------------------
# R2.8.1 coverage-interval convention (one shared implementation)
# ---------------------------------------------------------------------------

def merge_verified_spans(
    spans: list[tuple[_dt.datetime, _dt.datetime]],
) -> list[list[_dt.datetime]]:
    """Merge verified coverage spans under the R2.8.1 interval convention.

    Spans are CLOSED, second-resolution intervals, so two spans are
    contiguous iff ``next.start <= previous.end + 1 second``. This is
    exact adjacency, NOT a tolerance: a two-second hole still separates
    the merged runs and is reported as an uncovered sub-interval.
    Overlapping spans union normally (``previous.end`` is extended to the
    maximum).
    """
    merged: list[list[_dt.datetime]] = []
    for a, b in sorted(spans):
        if a > b:
            raise CalibrationSamplingError(
                f"inverted coverage span: start {a.isoformat()} is after "
                f"end {b.isoformat()}")
        if merged and a <= merged[-1][1] + COVERAGE_INTERVAL_RESOLUTION:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def uncovered_intervals(
    merged: list[list[_dt.datetime]],
    start_dt: _dt.datetime,
    end_dt: _dt.datetime,
) -> list[tuple[str, str]]:
    """Uncovered sub-intervals of ``[start_dt, end_dt]`` given merged runs.

    The window's own bounds are matched EXACTLY — the interval convention
    never extends a run past the requested window.
    """
    gaps: list[tuple[str, str]] = []
    cursor = start_dt
    for a, b in merged:
        if b < start_dt or a > end_dt:
            continue
        a = max(a, start_dt)
        b = min(b, end_dt)
        if a > cursor:
            gaps.append((cursor.isoformat(), a.isoformat()))
        if b > cursor:
            cursor = b
        if cursor > end_dt:
            break
    if cursor < end_dt:
        gaps.append((cursor.isoformat(), end_dt.isoformat()))
    return gaps


# ---------------------------------------------------------------------------
# Canonical instrument class (universe.yaml) — never heuristics
# ---------------------------------------------------------------------------

def _universe_path() -> Path:
    from backtest.artifacts import UNIVERSE_SEED, artifacts_dir

    # The profile-materialized artifact wins when present (user edits
    # increment universe_version); otherwise the committed §4.1 seed is
    # the canonical classification source.
    live = Path(artifacts_dir()) / UNIVERSE_SEED.name
    return live if live.exists() else UNIVERSE_SEED


def load_instrument_classes() -> dict[str, str]:
    """``{ticker: class}`` from the canonical ``universe.yaml`` artifact.

    Fails closed (``InstrumentClassError``) when the artifact is missing,
    unparseable, or a requested ticker is absent from it — class is never
    inferred from symbol naming or from DB row density.
    """
    import yaml

    path = _universe_path()
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise InstrumentClassError(
            f"canonical universe artifact is missing: {path}") from exc
    except Exception as exc:  # pragma: no cover - unparseable YAML
        raise InstrumentClassError(
            f"canonical universe artifact is unreadable: {path} ({exc})"
        ) from exc
    classes: dict[str, str] = {}
    for entry in (doc.get("tickers") or []):
        if not isinstance(entry, dict):
            continue
        ticker = str(entry.get("ticker", "")).strip().upper()
        cls = str(entry.get("class", "")).strip().lower()
        if not ticker or not cls:
            raise InstrumentClassError(
                f"universe entry missing ticker/class: {entry!r}")
        classes[ticker] = cls
    if not classes:
        raise InstrumentClassError(
            f"canonical universe artifact declares no tickers: {path}")
    return classes


def resolve_strata(tickers: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    """Per-ticker strata under the adjudicated hybrid policy.

    Returns ``{ticker: ("ticker", "year")}`` for canonical stocks and
    ``{ticker: ("ticker",)}`` for canonical ETFs. A ticker whose class is
    not one of ``CLASS_STRATA`` fails closed rather than falling back to
    a naming heuristic.
    """
    classes = load_instrument_classes()
    resolved: dict[str, tuple[str, ...]] = {}
    for ticker in tickers:
        cls = classes.get(ticker.strip().upper())
        if cls is None:
            raise InstrumentClassError(
                f"ticker {ticker!r} is not in the canonical universe "
                f"artifact; instrument class cannot be inferred")
        if cls not in CLASS_STRATA:
            raise InstrumentClassError(
                f"ticker {ticker!r} has canonical class {cls!r} which has "
                f"no adjudicated calibration strata")
        resolved[ticker] = CLASS_STRATA[cls]
    return resolved


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


def strata_for_config(config: "SampleConfig") -> dict[str, tuple[str, ...]]:
    """Per-ticker strata for a config: the hybrid policy by default, or
    the caller's explicit ``strata`` override applied to every ticker."""
    if not config.strata:
        return resolve_strata(config.tickers)
    return {t: config.strata for t in config.tickers}


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


def _window_years(config: SampleConfig) -> list[str]:
    """Every calendar year the requested window touches, ascending."""
    start_dt, end_dt = _window_bounds(config.start, config.end)
    return [str(y) for y in range(start_dt.year, end_dt.year + 1)]


def _required_strata(config: SampleConfig,
                     strata_map: dict[str, tuple[str, ...]],
                     years: list[str]) -> list[str] | None:
    """The mandatory BASE strata for a config: the full cross-product of
    requested tickers x the window's calendar years x the hybrid policy's
    per-class strata. Deterministic and order-independent of DB contents.

    Under the adjudicated hybrid policy this yields
    ``20 stocks x 7 years + 6 ETFs x 1 = 146`` strata for the canonical
    R2.8.1 universe and 2019–2025 window.

    ``source`` is not part of any hybrid stratum and cannot be enumerated
    from the requested scope, so an explicit ``source``-bearing override
    returns ``None`` and the caller falls back to the populated strata
    present in the requested window — a caller-chosen mechanism, never
    the production policy.
    """
    required: list[str] = []
    for ticker in config.tickers:
        fields = strata_map[ticker]
        if "source" in fields:
            return None
        for year in years:
            values = {"ticker": ticker, "year": year}
            required.append("|".join(str(values[f]) for f in fields))
    # A ticker-only stratum (ETF) is enumerated once per window year but
    # names the SAME stratum — the mandatory set is a SET of strata.
    return sorted(set(required))


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


def _allocate_quotas(strata_keys: list[str], size: int, seed: str,
                     capacities: dict[str, int] | None = None,
                     base_quota: int = 1) -> tuple[dict[str, int], int]:
    """R2.8.1 PRODUCTION calibration sampling policy (human-adjudicated):

    - a MANDATORY base quota (default 1) to every required stratum
    - remaining slots go to the first strata in the deterministic
      SHA-256 ranking (``_remainder_rank``), NOT lexical/sorted position
    - the remainder pass is CAPACITY-AWARE: a stratum that already holds
      all of its eligible rows is skipped and the slot moves on to the
      next-ranked stratum with spare capacity. A sparse stratum is
      therefore never a base-stratum failure — it simply does not take an
      extra slot.
    - the target is never lowered: if the whole eligible corpus cannot
      satisfy ``size`` after every stratum is capped at its capacity, the
      remaining shortfall is returned for the caller to fail closed on.
    """
    keys = list(strata_keys)
    caps = {k: max(0, (capacities or {}).get(k, size)) for k in keys}
    quotas = {key: min(base_quota, caps[key]) for key in keys}
    base_total = sum(quotas.values())
    if base_total > size:
        # The mandatory base floor alone already exceeds the requested
        # target. The floor is mandatory and the target is never lowered,
        # so this is a refusal, not a silent trim.
        return quotas, base_total - size
    remaining = size - base_total
    if remaining > 0:
        ranked = sorted(keys, key=lambda k: (_remainder_rank(seed, k), k))
        # Walk the ranking repeatedly: a stratum may absorb more than one
        # extra slot, so spare capacity anywhere can absorb the shortfall.
        progressed = True
        while remaining > 0 and progressed:
            progressed = False
            for key in ranked:
                if remaining <= 0:
                    break
                if quotas[key] < caps[key]:
                    quotas[key] += 1
                    remaining -= 1
                    progressed = True
    return quotas, remaining


def select_sample(conn: sqlite3.Connection, config: SampleConfig) -> tuple[list[SelectedHeadline], dict]:
    """Select the ordered deterministic sample.

    Returns (ordered sample, selection metadata). Raises
    :class:`CalibrationSamplingError` when the eligible corpus cannot
    satisfy the exact requested contract (total or per-stratum
    insufficiency) — never silently substitutes or lowers the target.
    """
    eligible = _eligible_rows(conn, config)
    strata_map = strata_for_config(config)
    window_years = _window_years(config)
    by_stratum: dict[str, list[dict]] = {}
    for row in eligible:
        by_stratum.setdefault(
            _stratum_key(row, strata_map[row["ticker"]]), []).append(row)
    if not by_stratum:
        raise CalibrationSamplingError(
            "no eligible timed headlines in the requested window/tickers")
    if len(eligible) < config.size:
        raise CalibrationSamplingError(
            f"insufficient eligible corpus: {len(eligible)} eligible "
            f"headlines < requested sample size {config.size}")

    # MANDATORY BASE STRATA are the FULL cross-product over the requested
    # tickers, the window's calendar years, and the hybrid policy's class
    # strata — a required stratum with no eligible row fails closed and is
    # NEVER substituted with an unrelated row.
    required = _required_strata(config, strata_map, window_years)
    if required is None:
        # Explicit source-bearing override: strata are not enumerable
        # from the requested scope, so the populated strata inside the
        # requested window define the mandatory set.
        required = sorted(by_stratum)
    empty = sorted(k for k in required if k not in by_stratum)
    if empty:
        raise CalibrationSamplingError(
            "mandatory stratum insufficiency — refusing to substitute or "
            "lower the requested target: empty required strata: "
            + "; ".join(empty))
    for key in required:
        by_stratum.setdefault(key, [])

    quotas, shortfall = _allocate_quotas(
        sorted(required), config.size, config.seed,
        capacities={k: len(v) for k, v in by_stratum.items()})
    if shortfall > 0:
        # Either the mandatory base alone already exceeds the target, or
        # the eligible corpus cannot fill it. The target is never lowered
        # and the target is never exceeded.
        raise CalibrationSamplingError(
            "stratum insufficiency — refusing to substitute or lower the "
            f"requested target: the {len(required)} mandatory base strata "
            f"require {sum(min(1, len(by_stratum[k])) for k in required)} "
            f"items but the requested target is {config.size}"
            + (f"; total eligible capacity across mandatory strata is "
               f"{sum(len(by_stratum[k]) for k in required)}, short by "
               f"{shortfall}" if shortfall <= config.size else
               " (the base floor alone already exceeds the target)"))
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
        "strata_by_ticker": {t: list(f) for t, f in sorted(strata_map.items())},
        "mandatory_stratum_count": len(required),
        "sampling_policy": {
            "status": "FINAL — human-adjudicated R2.8.1 production policy",
            "target_sample_size": config.size,
            "stratification_policy": (
                "hybrid: canonical STOCKS stratify by ticker x calendar "
                "year; canonical ETFs stratify by TICKER ONLY. Instrument "
                "class is read from the canonical universe.yaml artifact — "
                "never from symbol-naming heuristics and never from DB row "
                "density. The 12 empty ETF ticker-year strata of the real "
                "corpus are no longer mandatory base strata."),
            "strata_by_ticker": {t: list(f) for t, f in sorted(strata_map.items())},
            "mandatory_stratum_count": len(required),
            "quota_rule": (
                "one mandatory base item for every required stratum"),
            "remainder_rule": (
                "remaining slots (size - mandatory stratum count) are "
                "allocated in SHA-256 ranking over sampler_format_version + "
                "seed + canonical stratum key; a stratum whose eligible pool "
                "is exhausted is SKIPPED and the slot moves to the next-ranked "
                "stratum with spare capacity, so a sparse stratum stays valid "
                "with a single item and is never a base-stratum failure; "
                "independent of DB row order, ticker alphabetical order, "
                "Python hash randomization, and wall clock"),
            "capacity_rule": (
                "no stratum receives more rows than exist; the target is "
                "never lowered — a target above the total eligible capacity "
                "of the mandatory strata fails closed"),
            "coverage_interval_convention": COVERAGE_INTERVAL_CONVENTION,
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
    requested window under the R2.8.1 coverage-interval convention
    (:func:`merge_verified_spans`). Raises
    :class:`CoverageIncompleteError` on any real gap — the partial AV
    corpus can never pass.

    The convention is exact second-resolution adjacency, NOT a tolerance:
    a two-second (or larger) hole still fails closed, and no ticker is
    special-cased anywhere in this path.
    """
    if not config.manifest_version:
        raise CoverageIncompleteError(
            "FINAL generation requires --manifest-version (the run-pinned "
            "NEWS coverage manifest_version)")
    start_dt, end_dt = _window_bounds(config.start, config.end)
    evidence = {
        "manifest_version": config.manifest_version,
        "interval_convention": COVERAGE_INTERVAL_CONVENTION,
        "tickers": {},
    }
    for ticker in config.tickers:
        spans = _verified_spans(conn, ticker, config.manifest_version)
        merged = merge_verified_spans(spans)
        gaps = uncovered_intervals(merged, start_dt, end_dt)
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
            "merged_span_count": len(merged),
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
