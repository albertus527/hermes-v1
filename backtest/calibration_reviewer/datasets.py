"""Read-only loaders for the three §11.4 calibration datasets.

The source CSVs are historical artifacts and are NEVER written, renamed, or
modified. They are opened ``rb`` only. The loader:

* computes the exact SHA-256 of the source file bytes (provenance),
* assigns a frozen canonical review order from the file's own row order,
* splits each row into BLIND fields (safe before lock) and SECRET fields
  (never serialized to the browser before lock),
* carries the dataset's ``sampling_digest`` from the adjacent ``.meta.json``
  when present.

Blind fields : sample_id, headline_hash, ticker, published_at, source,
               headline_text.
Secret fields: human_label (prior LLM-consensus spot-checked label),
               human_notes, challenge_reason, candidate_dimension,
               direct_event_evidence — plus any model prediction, which is
               fetched separately from the read-only cache (see reveal.py).
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# Columns that are always present and safe to show before lock.
BLIND_COLUMNS = (
    "sample_id", "headline_hash", "ticker", "published_at", "source",
    "headline_text",
)
# Columns that MUST NOT reach the browser before lock.
SECRET_COLUMNS = (
    "human_label", "human_notes", "challenge_reason", "candidate_dimension",
    "direct_event_evidence",
)

CALIBRATION_ROOT = Path(
    os.path.expanduser("~/.hermes/backtest/calibration")
)


@dataclass(frozen=True)
class DatasetSpec:
    dataset_id: str
    display_name: str
    csv_path: Path
    meta_path: Path | None
    role: str  # "CANONICAL" | "SAFETY_CHALLENGE" | "SAFETY_SUPPLEMENTAL"


# Dataset A is the canonical §11.4 population (all 240). B and C are separate
# safety-coverage evaluation sets and are NEVER pooled with A.
DATASET_SPECS: tuple[DatasetSpec, ...] = (
    DatasetSpec(
        "A_CANONICAL_240",
        "Canonical 240 (Dataset A)",
        CALIBRATION_ROOT / "r281_calibration_sheet_human_adjudicated.csv",
        CALIBRATION_ROOT / "r281_calibration_worksheet.meta.json",
        "CANONICAL",
    ),
    DatasetSpec(
        "B_SAFETY_99",
        "Safety challenge 99 (Dataset B)",
        CALIBRATION_ROOT / "r281_safety_challenge_2026-10-03"
        / "r281_safety_challenge_human_adjudicated.csv",
        CALIBRATION_ROOT / "r281_safety_challenge_2026-10-03"
        / "r281_safety_challenge_worksheet.meta.json",
        "SAFETY_CHALLENGE",
    ),
    DatasetSpec(
        "C_SUPPLEMENTAL_36",
        "Supplemental direct-event 36 (Dataset C)",
        CALIBRATION_ROOT / "r281_supplemental_direct_event_2026-10-04"
        / "r281_supplemental_direct_event_human_adjudicated.csv",
        CALIBRATION_ROOT / "r281_supplemental_direct_event_2026-10-04"
        / "r281_supplemental_direct_event_worksheet.meta.json",
        "SAFETY_SUPPLEMENTAL",
    ),
)

SPECS_BY_ID = {s.dataset_id: s for s in DATASET_SPECS}


class DatasetLoadError(Exception):
    """A source dataset is missing, malformed, or changed unexpectedly."""


@dataclass
class LoadedDataset:
    spec: DatasetSpec
    source_sha256: str
    source_size: int
    sampling_digest: str | None
    samples: list[dict] = field(default_factory=list)   # blind fields + ord
    secrets: dict[str, dict] = field(default_factory=dict)  # sample_id -> secret

    @property
    def row_count(self) -> int:
        return len(self.samples)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_meta_digest(meta_path: Path | None) -> str | None:
    if meta_path is None or not meta_path.exists():
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    for key in ("deterministic_digest", "sample_digest"):
        value = doc.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def load_dataset(spec: DatasetSpec) -> LoadedDataset:
    """Load one dataset read-only; fail closed on schema problems."""
    if not spec.csv_path.exists():
        raise DatasetLoadError(f"source CSV missing: {spec.csv_path}")
    digest = sha256_file(spec.csv_path)
    size = spec.csv_path.stat().st_size

    with open(spec.csv_path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []
        missing = [c for c in BLIND_COLUMNS if c not in fieldnames]
        if missing:
            raise DatasetLoadError(
                f"{spec.dataset_id}: missing required columns {missing}")
        rows = list(reader)

    loaded = LoadedDataset(
        spec=spec,
        source_sha256=digest,
        source_size=size,
        sampling_digest=_read_meta_digest(spec.meta_path),
    )
    for ord_, row in enumerate(rows):
        sample_id = (row.get("sample_id") or "").strip()
        if not sample_id:
            raise DatasetLoadError(
                f"{spec.dataset_id}: row {ord_} has empty sample_id")
        if sample_id in loaded.secrets:
            raise DatasetLoadError(
                f"{spec.dataset_id}: duplicate sample_id {sample_id!r}")
        blind = {c: (row.get(c) or "").strip() for c in BLIND_COLUMNS}
        blind["ord"] = ord_
        loaded.samples.append(blind)
        loaded.secrets[sample_id] = {
            c: (row.get(c) or "").strip() for c in SECRET_COLUMNS
        }
    return loaded


def load_all(specs: tuple[DatasetSpec, ...] = DATASET_SPECS
             ) -> dict[str, LoadedDataset]:
    return {spec.dataset_id: load_dataset(spec) for spec in specs}
