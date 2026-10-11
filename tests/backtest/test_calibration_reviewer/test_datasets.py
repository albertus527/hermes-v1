"""Dataset loading, provenance digests, and source-integrity enforcement."""

from __future__ import annotations

import hashlib

import pytest

from backtest.calibration_reviewer.datasets import (
    BLIND_COLUMNS,
    SECRET_COLUMNS,
    DatasetLoadError,
    load_dataset,
    sha256_file,
)
from backtest.calibration_reviewer.schema import open_reviewer_db
from backtest.calibration_reviewer.store import Store, StoreError

from .conftest import make_dataset


def test_blind_and_secret_columns_disjoint():
    assert set(BLIND_COLUMNS).isdisjoint(set(SECRET_COLUMNS))


def test_load_dataset_blind_projection(tmp_path):
    spec = make_dataset(tmp_path, n=3)
    ds = load_dataset(spec)
    assert ds.row_count == 3
    # Blind samples carry ONLY blind columns (+ ord).
    for sample in ds.samples:
        assert set(sample) == set(BLIND_COLUMNS) | {"ord"}
        assert "human_label" not in sample
        assert "human_notes" not in sample
    # Secrets are captured separately.
    for secret in ds.secrets.values():
        assert "human_label" in secret
        assert "human_notes" in secret


def test_load_dataset_digest_matches_file(tmp_path):
    spec = make_dataset(tmp_path, n=2)
    ds = load_dataset(spec)
    assert ds.source_sha256 == sha256_file(spec.csv_path)


def test_load_dataset_reads_bom_and_crlf(tmp_path):
    spec = make_dataset(tmp_path, n=2)
    # The fixture wrote a BOM; loading must not choke on it.
    ds = load_dataset(spec)
    assert ds.samples[0]["sample_id"].startswith("A_CANONICAL_240")


def test_load_dataset_missing_file(tmp_path):
    from backtest.calibration_reviewer.datasets import DatasetSpec
    spec = DatasetSpec("X", "X", tmp_path / "nope.csv", None, "CANONICAL")
    with pytest.raises(DatasetLoadError):
        load_dataset(spec)


def test_source_file_is_not_modified_by_load(tmp_path):
    spec = make_dataset(tmp_path, n=4)
    before = sha256_file(spec.csv_path)
    load_dataset(spec)
    assert sha256_file(spec.csv_path) == before


def test_tampered_source_hash_fails_closed(tmp_path):
    spec = make_dataset(tmp_path, n=3)
    conn = open_reviewer_db(tmp_path / "reviewer.sqlite3")
    store = Store(conn)
    store.initialize_from_datasets({"A_CANONICAL_240": load_dataset(spec)})

    # Tamper: rewrite the CSV with a changed headline (same row identity).
    with open(spec.csv_path, "r", encoding="utf-8-sig", newline="") as fh:
        content = fh.read()
    content = content.replace("Secret consensus headline number 0",
                              "Tampered headline number 0")
    with open(spec.csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(content)

    with pytest.raises(StoreError) as exc:
        store.initialize_from_datasets({"A_CANONICAL_240": load_dataset(spec)})
    assert "hash changed" in str(exc.value)


def test_reordered_samples_fail_closed(tmp_path):
    """Same bytes' row set but changed order must be detected via identity."""
    spec = make_dataset(tmp_path, n=3)
    conn = open_reviewer_db(tmp_path / "reviewer.sqlite3")
    store = Store(conn)
    loaded = load_dataset(spec)
    store.initialize_from_datasets({"A_CANONICAL_240": loaded})

    # Simulate an identity change: mutate the stored sample order directly.
    conn.execute("UPDATE sample SET ord=ord+10 WHERE dataset_id=?",
                 ("A_CANONICAL_240",))
    conn.commit()
    with pytest.raises(StoreError) as exc:
        store.initialize_from_datasets({"A_CANONICAL_240": loaded})
    assert "identity changed" in str(exc.value)


def test_duplicate_sample_id_rejected(tmp_path):
    spec = make_dataset(tmp_path, n=3)
    with open(spec.csv_path, "r", encoding="utf-8-sig", newline="") as fh:
        lines = fh.read().splitlines()
    # Duplicate the first data row.
    lines.append(lines[1])
    with open(spec.csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("\n".join(lines))
    with pytest.raises(DatasetLoadError):
        load_dataset(spec)
