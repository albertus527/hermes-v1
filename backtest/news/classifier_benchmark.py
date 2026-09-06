"""R2.8.1 Phase-2 — offline news-classifier benchmark harness.

BENCHMARK HARNESS ONLY. This module NEVER calls an LLM, never touches the
classification cache, never modifies canonical news rows, coverage
manifests, or the source worksheet. It consumes:

  A. a MANUALLY LABELED calibration worksheet (the CSV produced by
     ``calibration_sampler.write_worksheet`` with ``human_label`` filled
     in by a human), and
  B. one or more OFFLINE candidate-prediction JSONL files.

The human labels are the sole source of truth. A candidate prediction
file is produced by whatever offline process the operator ran; this
harness only scores it.

EXACT R2.8.1 contract (audit-extracted, nothing invented):
- Label dimensions (§11.4 pre-registered label set):
  ``category, direction, severity, ma_role`` — nothing else.
  ``keyword_override`` is deterministic code behavior and is NOT a
  human label (§11.4). ``confidence`` is not part of the pre-registered
  label set.
- Enums + the §11.1 conditional ``ma_role`` validity rule are reused
  verbatim from the existing strict validator.
- Identity: the sampler's stable identity — ``sample_id`` +
  ``headline_hash`` (+ ticker/source echo). ``headline_hash`` must
  equal the FP-4 recomputation from ``headline_text``.
- Spec-defined normative quantity: the §11.4 ≥200-headline minimum
  (``meets_minimum``). That is the ONLY normative gate implemented.
- NO accuracy threshold, NO weighted aggregate score, NO tie-break,
  NO automatic classifier selection exists in the frozen spec —
  final selection is HUMAN ADJUDICATION REQUIRED. All agreement
  figures are DESCRIPTIVE METRICS.

Determinism: same inputs → same report and same digests, independent
of input row/file ordering, wall clock, and PYTHONHASHSEED. Rows are
canonically sorted before any comparison or hashing; ``generated_at``
is provenance-only and never enters a digest.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from trading_core.news_effects import headline_hash as compute_headline_hash

from backtest.news.calibration import LABEL_FIELDS, MIN_LABELED_HEADLINES
from backtest.news.cache import MalformedClassificationError

BENCHMARK_FORMAT_VERSION = "r281-classifier-benchmark-1"

#: The human-label column of the worksheet holds a compact JSON object
#: with EXACTLY the §11.4 label fields.
WORKSHEET_IDENTITY_COLUMNS = ("sample_id", "headline_hash", "ticker")


class BenchmarkInputError(Exception):
    """Fail-closed: the labeled dataset or a candidate file violates
    the benchmark input contract."""


# ---------------------------------------------------------------------------
# Human-labeled worksheet (immutable input; read-only)
# ---------------------------------------------------------------------------


def _parse_human_label(raw, where: str) -> dict:
    """Parse + strictly validate one human label cell."""
    if raw is None or not str(raw).strip():
        raise BenchmarkInputError(
            f"{where}: human_label is blank — the worksheet is not fully "
            "labeled; evaluation fails closed (model predictions must "
            "never populate missing human labels)")
    try:
        label = json.loads(raw)
    except ValueError as exc:
        raise BenchmarkInputError(
            f"{where}: human_label is not valid JSON: {exc}") from exc
    if not isinstance(label, dict) or set(label) != set(LABEL_FIELDS):
        raise BenchmarkInputError(
            f"{where}: human_label must have exactly {list(LABEL_FIELDS)} "
            "fields")
    # Strict §11.1 enum + conditional-validity check (fail-closed).
    # confidence is not a label dimension; a placeholder passes the
    # shared validator which we only use for enum/shape enforcement.
    from backtest.news.cache import validate_classification_payload
    try:
        validate_classification_payload({
            "ticker": "BENCH",
            "category": label["category"],
            "direction": label["direction"],
            "severity": label["severity"],
            "ma_role": label["ma_role"],
            "confidence": 1.0,
            "published_at": "2024-01-01T00:00:00+00:00",
            "headline_hash": "x" * 64,
            "source": "benchmark",
            "keyword_override": False,
            "schema_version": "news_schema_v3",
            "model_version": "benchmark-label",
        })
    except MalformedClassificationError as exc:
        raise BenchmarkInputError(
            f"{where}: human label invalid: {exc}") from exc
    return {f: label[f] for f in LABEL_FIELDS}


@dataclass(frozen=True)
class LabeledRow:
    sample_id: str
    headline_hash: str
    ticker: str
    source: str
    headline_text: str
    label: dict


def load_labeled_worksheet(path) -> list[LabeledRow]:
    """Load the labeled calibration worksheet CSV (read-only; the source
    worksheet is never modified by this harness). Fail-closed on: missing
    columns, blank required labels/fields, malformed or invalid labels,
    FP-4 hash mismatch, or duplicate identity."""
    rows: list[LabeledRow] = []
    seen: dict[tuple, int] = {}
    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        required = set(WORKSHEET_IDENTITY_COLUMNS) | {
            "source", "headline_text", "human_label"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise BenchmarkInputError(
                f"worksheet is missing required columns: {sorted(missing)}")
        for lineno, item in enumerate(reader, start=2):
            where = f"worksheet row {lineno}"
            for col in WORKSHEET_IDENTITY_COLUMNS + ("headline_text",):
                if not (item.get(col) or "").strip():
                    raise BenchmarkInputError(
                        f"{where}: required column {col!r} is blank")
            label = _parse_human_label(item.get("human_label"), where)
            recomputed = compute_headline_hash(item["headline_text"])
            if recomputed != item["headline_hash"]:
                raise BenchmarkInputError(
                    f"{where}: headline_hash mismatch — stored "
                    f"{item['headline_hash']!r} != FP-4 recomputation "
                    f"{recomputed!r}")
            identity = (item["sample_id"], item["headline_hash"],
                        item["ticker"])
            if identity in seen:
                raise BenchmarkInputError(
                    f"{where}: duplicate labeled identity {identity} "
                    f"(first seen on row {seen[identity]})")
            seen[identity] = lineno
            rows.append(LabeledRow(
                sample_id=item["sample_id"],
                headline_hash=item["headline_hash"],
                ticker=item["ticker"],
                source=item.get("source") or "",
                headline_text=item["headline_text"],
                label=label,
            ))
    if not rows:
        raise BenchmarkInputError("worksheet contains no labeled rows")
    return rows


# ---------------------------------------------------------------------------
# Offline candidate predictions (JSONL)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidatePrediction:
    sample_id: str
    headline_hash: str
    ticker: str
    label: dict


@dataclass
class CandidateFile:
    candidate_id: str
    prompt_version: str        # contract/prompt version metadata (may be "")
    predictions: dict[str, CandidatePrediction]   # keyed by sample_id


def _parse_candidate_label(raw, where: str) -> dict:
    if not isinstance(raw, dict) or set(raw) != set(LABEL_FIELDS):
        raise BenchmarkInputError(
            f"{where}: prediction must have exactly {list(LABEL_FIELDS)} "
            "fields")
    from backtest.news.cache import validate_classification_payload
    try:
        validate_classification_payload({
            "ticker": "BENCH",
            "category": raw["category"],
            "direction": raw["direction"],
            "severity": raw["severity"],
            "ma_role": raw["ma_role"],
            "confidence": 1.0,
            "published_at": "2024-01-01T00:00:00+00:00",
            "headline_hash": "x" * 64,
            "source": "benchmark",
            "keyword_override": False,
            "schema_version": "news_schema_v3",
            "model_version": "benchmark-candidate",
        })
    except MalformedClassificationError as exc:
        raise BenchmarkInputError(
            f"{where}: prediction invalid: {exc}") from exc
    return {f: raw[f] for f in LABEL_FIELDS}


def load_candidate_file(path) -> CandidateFile:
    """Load one offline candidate-prediction JSONL file. Line shape::

        {"candidate_id": "...", "prompt_version": "..." (optional),
         "sample_id": "...", "headline_hash": "...", "ticker": "...",
         "label": {"category": ..., "direction": ..., "severity": ...,
                   "ma_role": ...}}

    Fail-closed on: duplicate (candidate_id, sample_id), unknown/blank
    identity fields, malformed or invalid label enums, or inconsistent
    per-line candidate_id within one file."""
    path = Path(path)
    candidate_ids: set[str] = set()
    obj: dict = {}
    predictions: dict[str, CandidatePrediction] = {}
    prompt_version = ""
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            where = f"{path.name} line {lineno}"
            if not line.strip():
                raise BenchmarkInputError(f"{where}: blank line")
            try:
                obj = json.loads(line)
            except ValueError as exc:
                raise BenchmarkInputError(
                    f"{where}: not valid JSON: {exc}") from exc
            if not isinstance(obj, dict):
                raise BenchmarkInputError(f"{where}: line is not an object")
            cid = obj.get("candidate_id")
            if not isinstance(cid, str) or not cid.strip():
                raise BenchmarkInputError(
                    f"{where}: candidate_id must be a non-empty string")
            candidate_ids.add(cid)
            sid = obj.get("sample_id")
            hh = obj.get("headline_hash")
            ticker = obj.get("ticker")
            for name, val in (("sample_id", sid), ("headline_hash", hh),
                              ("ticker", ticker)):
                if not isinstance(val, str) or not val.strip():
                    raise BenchmarkInputError(
                        f"{where}: {name} must be a non-empty string")
            label = _parse_candidate_label(obj.get("label"), where)
            if sid in predictions:
                raise BenchmarkInputError(
                    f"{where}: duplicate candidate prediction identity "
                    f"({cid}, {sid})")
            predictions[sid] = CandidatePrediction(
                sample_id=sid, headline_hash=hh, ticker=ticker, label=label)
    if not predictions:
        raise BenchmarkInputError(f"{path.name}: no predictions")
    if len(candidate_ids) != 1:
        raise BenchmarkInputError(
            f"{path.name}: multiple candidate_id values in one file: "
            f"{sorted(candidate_ids)}")
    return CandidateFile(
        candidate_id=next(iter(candidate_ids)),
        prompt_version=str(obj.get("prompt_version") or ""),
        predictions=predictions,
    )


def load_candidate_files(paths) -> list[CandidateFile]:
    """Load candidate files; fail-closed on duplicate candidate_id across
    files (one candidate = one identity). Ordering of the returned list
    follows the given paths but never affects results."""
    out: list[CandidateFile] = []
    seen: dict[str, str] = {}
    for p in paths:
        cf = load_candidate_file(p)
        if cf.candidate_id in seen:
            raise BenchmarkInputError(
                f"duplicate candidate_id {cf.candidate_id!r} in both "
                f"{seen[cf.candidate_id]} and {p}")
        seen[cf.candidate_id] = str(p)
        out.append(cf)
    return out


# ---------------------------------------------------------------------------
# Cross-validation of candidate identities against the labeled worksheet
# ---------------------------------------------------------------------------


def check_candidate_coverage(labeled: list[LabeledRow],
                             candidates: CandidateFile) -> None:
    """Fail-closed identity/coverage validation for ONE candidate against
    the labeled set: wrong-headline predictions, ticker mismatch, unknown
    identities, and missing predictions all raise."""
    by_sid = {r.sample_id: r for r in labeled}
    if len(by_sid) != len(labeled):
        raise BenchmarkInputError(
            "labeled worksheet has duplicate sample_id values")
    for sid, pred in sorted(candidates.predictions.items()):
        where = f"candidate {candidates.candidate_id!r} sample {sid!r}"
        row = by_sid.get(sid)
        if row is None:
            raise BenchmarkInputError(
                f"{where}: unknown sample_id (not in the labeled worksheet)")
        if pred.headline_hash != row.headline_hash:
            raise BenchmarkInputError(
                f"{where}: headline_hash {pred.headline_hash!r} does not "
                f"match the labeled headline {row.headline_hash!r} — "
                "prediction is for the wrong headline")
        if pred.ticker != row.ticker:
            raise BenchmarkInputError(
                f"{where}: ticker {pred.ticker!r} does not match labeled "
                f"ticker {row.ticker!r}")
    missing = sorted(set(by_sid) - set(candidates.predictions))
    if missing:
        raise BenchmarkInputError(
            f"candidate {candidates.candidate_id!r} is partially evaluated: "
            f"{len(missing)} labeled samples have no prediction "
            f"(e.g. {missing[:5]})")


# ---------------------------------------------------------------------------
# Evaluation + report
# ---------------------------------------------------------------------------


@dataclass
class CandidateResult:
    candidate_id: str
    prompt_version: str
    labeled_count: int
    exact_label_match: int
    per_field_correct: dict = field(default_factory=dict)
    per_field_accuracy: dict = field(default_factory=dict)
    exact_match_rate: float | None = None
    meets_minimum: bool = False


@dataclass
class BenchmarkReport:
    benchmark_format_version: str = BENCHMARK_FORMAT_VERSION
    labeled_count: int = 0
    labels_digest: str = ""
    candidates: list = field(default_factory=list)   # CandidateResult dicts
    candidates_digest: str = ""
    meets_minimum: bool = False
    descriptive_metrics_note: str = (
        "All agreement figures are DESCRIPTIVE METRICS. The frozen "
        "R2.8.1 specification defines no accuracy threshold, no weighted "
        "aggregate score, no tie-break, and no automatic classifier "
        "selection.")
    normative_status: str = "HUMAN ADJUDICATION REQUIRED"
    evaluation_error: str | None = None
    generated_at: str | None = None   # provenance ONLY; never digested

    def to_json(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True, indent=2)


def _stable_digest(obj) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _evaluate_candidate(labeled: list[LabeledRow],
                        candidate: CandidateFile) -> CandidateResult:
    """Deterministic per-dimension agreement (DESCRIPTIVE). Both sides are
    canonically sorted first, so row/file ordering cannot matter."""
    per_field_correct = {f: 0 for f in LABEL_FIELDS}
    exact = 0
    preds = candidate.predictions
    for row in labeled:                      # labeled is canonically sorted
        pred = preds[row.sample_id]
        all_match = True
        for f in LABEL_FIELDS:
            if pred.label[f] == row.label[f]:
                per_field_correct[f] += 1
            else:
                all_match = False
        if all_match:
            exact += 1
    n = len(labeled)
    return CandidateResult(
        candidate_id=candidate.candidate_id,
        prompt_version=candidate.prompt_version,
        labeled_count=n,
        exact_label_match=exact,
        per_field_correct=per_field_correct,
        per_field_accuracy={f: per_field_correct[f] / n for f in LABEL_FIELDS},
        exact_match_rate=exact / n,
        meets_minimum=n >= MIN_LABELED_HEADLINES,
    )


def run_benchmark(labeled: list[LabeledRow],
                  candidate_files: list[CandidateFile]) -> BenchmarkReport:
    """Evaluate every candidate independently and deterministically.

    Fail-closed validation runs FIRST (coverage/identity for every
    candidate) so a partial or mismatched candidate set never produces a
    report. Metrics are descriptive only; the sole normative quantity is
    the spec-defined §11.4 ≥200 minimum."""
    ordered = sorted(labeled, key=lambda r: (r.sample_id, r.headline_hash,
                                             r.ticker))
    for candidate in candidate_files:
        check_candidate_coverage(ordered, candidate)
    report = BenchmarkReport(
        labeled_count=len(ordered),
        labels_digest=_stable_digest(
            [{f: getattr(r, f) for f in ("sample_id", "headline_hash",
                                         "ticker", "label")} for r in ordered]),
        candidates=[],
    )
    for candidate in sorted(candidate_files, key=lambda c: c.candidate_id):
        result = _evaluate_candidate(ordered, candidate)
        report.candidates.append(result.__dict__.copy())
    report.candidates_digest = _stable_digest(
        [{"candidate_id": c.candidate_id,
          "predictions": [
              {"sample_id": sid,
               **c.predictions[sid].label}
              for sid in sorted(c.predictions)]}
         for c in sorted(candidate_files, key=lambda c: c.candidate_id)])
    report.meets_minimum = len(ordered) >= MIN_LABELED_HEADLINES
    return report


def write_report(report: BenchmarkReport, out_path) -> Path:
    """Atomically persist the machine-readable report. Writes ONLY the
    report file — never the worksheet, cache, or any canonical store."""
    from utils import atomic_write_text
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out_path, report.to_json())
    return out_path
