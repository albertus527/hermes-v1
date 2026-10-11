"""Hermetic fixtures for the §11.4 calibration reviewer tests.

Every fixture uses ``tmp_path`` — no real calibration CSV, no production
backtest store, no network, no LLM. Source fixtures are tiny synthetic CSVs
that mimic the real schema (including a UTF-8 BOM and the secret columns).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from backtest.calibration_reviewer.datasets import DatasetSpec
from backtest.calibration_reviewer.server import ReviewerApp


def _write_csv(path: Path, columns, rows) -> None:
    # Mimic the real artifacts: UTF-8 BOM + CRLF line endings.
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), lineterminator="\r\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def make_dataset(tmp_path: Path, dataset_id="A_CANONICAL_240", n=6,
                 role="CANONICAL", with_cues=False) -> DatasetSpec:
    csv_path = tmp_path / f"{dataset_id}.csv"
    meta_path = tmp_path / f"{dataset_id}.meta.json"
    base_cols = ["sample_id", "headline_hash", "ticker", "published_at",
                 "source", "headline_text", "human_label", "human_notes"]
    cue_cols = []
    if with_cues:
        cue_cols = ["challenge_reason", "candidate_dimension",
                    "direct_event_evidence"]
    columns = base_cols + cue_cols
    rows = []
    for i in range(n):
        label = {
            "category": "OTHER" if i % 2 == 0 else "EARNINGS",
            "direction": "NEUTRAL" if i % 2 == 0 else "BULLISH",
            "severity": "LOW",
            "ma_role": "NEITHER",
        }
        row = {
            "sample_id": f"{dataset_id}-{i:04d}",
            "headline_hash": f"{i:064x}",
            "ticker": "AAPL" if i % 2 == 0 else "MSFT",
            "published_at": f"2020-01-{i+1:02d}T12:00:00+00:00",
            "source": "TEST",
            "headline_text": f"Secret consensus headline number {i}",
            "human_label": json.dumps(label),
            "human_notes": f"SECRET_NOTES_{i}",
        }
        if with_cues:
            row["challenge_reason"] = f"SECRET_CUE_{i}"
            row["candidate_dimension"] = f"SECRET_DIM_{i}"
            row["direct_event_evidence"] = f"SECRET_EVIDENCE_{i}"
        rows.append(row)
    _write_csv(csv_path, columns, rows)
    meta_path.write_text(json.dumps({"sample_digest": "deadbeef" * 8}),
                         encoding="utf-8")
    return DatasetSpec(dataset_id, f"Test {dataset_id}", csv_path, meta_path, role)


@pytest.fixture
def specs(tmp_path):
    return (make_dataset(tmp_path, "A_CANONICAL_240", n=6, role="CANONICAL"),
            make_dataset(tmp_path, "B_SAFETY_99", n=4, role="SAFETY_CHALLENGE",
                         with_cues=True))


@pytest.fixture
def app(tmp_path, specs):
    db_path = tmp_path / "reviewer.sqlite3"
    return ReviewerApp(db_path, cache_db_path=None, specs=specs)


@pytest.fixture
def app_with_specs(tmp_path, specs):
    """Factory returning (app, specs, tmp_path) for tests needing the specs."""
    db_path = tmp_path / "reviewer.sqlite3"
    application = ReviewerApp(db_path, cache_db_path=None, specs=specs)
    return application, specs, tmp_path
