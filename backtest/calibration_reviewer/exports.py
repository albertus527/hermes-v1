"""Deterministic exports (C3): GET streams bytes, never writes.

Every export is a pure function of reviewer state + immutable source digests.
The bytes are byte-for-byte reproducible for identical state (deterministic
ordering, LF newlines, no timestamps embedded in row data — the generation
timestamp is a separate digest field the caller may include in JSON, not in
the CSV body).

Exports distinguish:
* LLM consensus (``LLM_CONSENSUS_SPOT_CHECKED``),
* human draft,
* human locked label,
* post-reveal human revision,
* unreviewed sample.

Only rubric-approved, explicitly locked labels are marked
``counts_toward_11_4 = true``. Exports never claim ``HUMAN_ACCEPTED`` or
``PHASE_2_PASS``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json

from backtest.calibration_reviewer.datasets import LoadedDataset

REVIEW_STATE_COLUMNS = (
    "dataset_id", "sample_id", "ord", "ticker", "published_at", "source",
    "headline_text", "headline_hash", "review_state", "category", "direction",
    "severity", "ma_role", "certainty", "flagged", "rubric_version",
    "rubric_approved", "revealed_consensus", "revealed_model", "revision_no",
    "post_reveal_revision", "counts_toward_11_4", "notes", "locked_at",
    "updated_at",
)


def _csv_bytes(rows: list[dict], columns: tuple[str, ...]) -> bytes:
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=list(columns), lineterminator="\n",
                            extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    return buf.getvalue().encode("utf-8")


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_rows(store, dataset_id: str, loaded: LoadedDataset) -> list[dict]:
    """Merge reviewer state over the full, frozen sample order.

    Every sample appears exactly once. Unreviewed samples carry
    ``review_state = UNREVIEWED`` and empty label fields.
    """
    ordered = store.ordered_sample_ids(dataset_id)
    rows: list[dict] = []
    for ord_, sample_id in enumerate(ordered):
        rec = store.get_sample(dataset_id, sample_id)
        status = (rec or {}).get("status") or "UNREVIEWED"
        rubric_approved = bool((rec or {}).get("rubric_approved"))
        revision_no = int((rec or {}).get("revision_no") or 0)
        revealed = bool((rec or {}).get("revealed_consensus")
                        or (rec or {}).get("revealed_model"))
        post_reveal_revision = 1 if (revision_no > 0 and revealed) else 0
        counts = 1 if (status == "LOCKED" and rubric_approved) else 0
        rows.append({
            "dataset_id": dataset_id,
            "sample_id": sample_id,
            "ord": ord_,
            "ticker": (rec or {}).get("ticker", ""),
            "published_at": (rec or {}).get("published_at", ""),
            "source": (rec or {}).get("source", ""),
            "headline_text": (rec or {}).get("headline_text", ""),
            "headline_hash": (rec or {}).get("headline_hash", ""),
            "review_state": status,
            "category": (rec or {}).get("category") or "",
            "direction": (rec or {}).get("direction") or "",
            "severity": (rec or {}).get("severity") or "",
            "ma_role": (rec or {}).get("ma_role") or "",
            "certainty": (rec or {}).get("certainty") or "",
            "flagged": int((rec or {}).get("flagged") or 0),
            "rubric_version": (rec or {}).get("rubric_version") or "",
            "rubric_approved": 1 if rubric_approved else 0,
            "revealed_consensus": int(bool((rec or {}).get("revealed_consensus"))),
            "revealed_model": int(bool((rec or {}).get("revealed_model"))),
            "revision_no": revision_no,
            "post_reveal_revision": post_reveal_revision,
            "counts_toward_11_4": counts,
            "notes": (rec or {}).get("notes") or "",
            "locked_at": (rec or {}).get("updated_at") if status == "LOCKED" else "",
            "updated_at": (rec or {}).get("updated_at") or "",
        })
    return rows


def export_labels_csv(store, dataset_id: str, loaded: LoadedDataset) -> bytes:
    return _csv_bytes(build_rows(store, dataset_id, loaded),
                      REVIEW_STATE_COLUMNS)


def export_progress_json(store, dataset_id: str, loaded: LoadedDataset) -> bytes:
    progress = store.progress(dataset_id)
    doc = {
        "dataset_id": dataset_id,
        "display_name": loaded.spec.display_name,
        "role": loaded.spec.role,
        "source_path": str(loaded.spec.csv_path),
        "source_sha256": loaded.source_sha256,
        "sampling_digest": loaded.sampling_digest,
        "progress": progress,
        "counts_toward_11_4": progress["canonical_locked"],
        "note": ("Only rubric-approved, explicitly locked labels count toward "
                 "§11.4. No acceptance or phase-pass status is claimed "
                 "automatically."),
    }
    return _json_bytes(doc)


def export_provenance_json(store, dataset_id: str, loaded: LoadedDataset) -> bytes:
    ledger = store.audit_ledger(dataset_id)
    doc = {
        "dataset_id": dataset_id,
        "source_sha256": loaded.source_sha256,
        "sampling_digest": loaded.sampling_digest,
        "audit_ledger": ledger,
        "audit_count": len(ledger),
    }
    return _json_bytes(doc)


def _json_bytes(doc) -> bytes:
    return (json.dumps(doc, sort_keys=True, indent=2,
                       ensure_ascii=False) + "\n").encode("utf-8")


def export_integrity_json(store, loaded_by_id: dict) -> bytes:
    datasets = []
    for dataset_id, loaded in sorted(loaded_by_id.items()):
        rows = build_rows(store, dataset_id, loaded)
        labels_csv = _csv_bytes(rows, REVIEW_STATE_COLUMNS)
        datasets.append({
            "dataset_id": dataset_id,
            "display_name": loaded.spec.display_name,
            "role": loaded.spec.role,
            "source_path": str(loaded.spec.csv_path),
            "source_sha256": loaded.source_sha256,
            "source_size": loaded.source_size,
            "sampling_digest": loaded.sampling_digest,
            "row_count": loaded.row_count,
            "labels_csv_sha256": digest_bytes(labels_csv),
            "progress": store.progress(dataset_id),
        })
    doc = {
        "datasets": datasets,
        "combined_note": (
            "Datasets are reported separately; the combined result is NOT "
            "representative of the historical corpus."),
    }
    return _json_bytes(doc)
