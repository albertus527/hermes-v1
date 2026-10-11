"""Reviewer state store: transactional state machine + append-only audit.

Concurrency model (C7):

* Every mutable review row carries a monotonically increasing ``version``.
* All writes take an ``expected_version`` and use ``UPDATE ... WHERE
  version = ?`` (compare-and-swap). A stale browser tab, delayed autosave,
  duplicated POST, or interrupted operation therefore cannot clobber a newer
  draft or a locked label — it gets :class:`StaleWriteError`.
* A draft write is refused (``LockedLabelError``) whenever the row is LOCKED;
  a locked label changes ONLY through :meth:`revise`, which requires an
  explicit reason.
* Every mutation appends an ``audit`` row inside the SAME transaction, so no
  partially written review or unlogged change is possible.

The store never reads the wall clock for a decision; timestamps are recorded
for provenance only.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import threading

from backtest.calibration_reviewer.labels import (
    InvalidHumanLabelError,
    validate_human_label,
)

VALID_CERTAINTY = ("CERTAIN", "UNSURE", "NEED_CONTEXT")
STATUS_UNREVIEWED = "UNREVIEWED"
STATUS_DRAFT = "DRAFT"
STATUS_LOCKED = "LOCKED"


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class StoreError(Exception):
    """Base class for reviewer store errors."""


class StaleWriteError(StoreError):
    """The supplied expected_version does not match the stored version."""


class LockedLabelError(StoreError):
    """A draft write or delete targeted a LOCKED label."""


class NotLockedError(StoreError):
    """An operation that requires a LOCKED label targeted a non-locked row."""


class InvalidStateError(StoreError):
    """An operation was attempted from an invalid state."""


class Store:
    """Thin transactional wrapper over the reviewer SQLite connection."""

    def __init__(self, conn: sqlite3.Connection, *, actor: str = "reviewer"):
        self._conn = conn
        self._actor = actor
        # The stdlib threaded server shares one connection; serialize access
        # so concurrent requests cannot interleave transactions.
        self._lock = threading.RLock()

    def locked(self):
        """Context manager serializing all store operations on the conn."""
        return self._lock

    # -- audit -----------------------------------------------------------

    def _audit(self, *, dataset_id, sample_id, event, prev_status=None,
               new_status=None, prev_label=None, new_label=None, reason=None,
               post_reveal=0, detail=None):
        self._conn.execute(
            "INSERT INTO audit (dataset_id, sample_id, event, prev_status, "
            "new_status, prev_label, new_label, reason, post_reveal, actor, "
            "at, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                dataset_id, sample_id, event, prev_status, new_status,
                json.dumps(prev_label, sort_keys=True) if prev_label else None,
                json.dumps(new_label, sort_keys=True) if new_label else None,
                reason, 1 if post_reveal else 0, self._actor, _utcnow(),
                json.dumps(detail, sort_keys=True) if detail else None,
            ),
        )

    # -- initialization / integrity --------------------------------------

    def initialize_from_datasets(self, loaded: dict) -> None:
        """Load datasets + samples, verifying integrity on re-init.

        Fails closed on any source-hash change or sample-identity change;
        never reimports, reorders, or overwrites already reviewed samples.
        """
        for dataset_id, ds in loaded.items():
            spec = ds.spec
            existing = self._conn.execute(
                "SELECT source_sha256, source_rows, role FROM dataset "
                "WHERE dataset_id=?", (dataset_id,)).fetchone()
            if existing is None:
                self._conn.execute(
                    "INSERT INTO dataset (dataset_id, display_name, role, "
                    "source_path, source_sha256, source_size, source_rows, "
                    "sampling_digest, loaded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (dataset_id, spec.display_name, spec.role,
                     str(spec.csv_path), ds.source_sha256, ds.source_size,
                     ds.row_count, ds.sampling_digest, _utcnow()))
                for s in ds.samples:
                    self._conn.execute(
                        "INSERT INTO sample (dataset_id, sample_id, ord, "
                        "ticker, published_at, source, headline_text, "
                        "headline_hash) VALUES (?,?,?,?,?,?,?,?)",
                        (dataset_id, s["sample_id"], s["ord"], s["ticker"],
                         s["published_at"], s["source"], s["headline_text"],
                         s["headline_hash"]))
                self._audit(dataset_id=dataset_id, sample_id=None,
                            event="DATASET_INITIALIZED",
                            detail={"source_sha256": ds.source_sha256,
                                    "rows": ds.row_count})
                continue
            # Re-init: verify integrity, fail closed on change.
            if existing["source_sha256"] != ds.source_sha256:
                raise StoreError(
                    f"{dataset_id}: source CSV hash changed "
                    f"({existing['source_sha256']} -> {ds.source_sha256}); "
                    "refusing to reimport. Source artifacts are immutable.")
            if existing["source_rows"] != ds.row_count:
                raise StoreError(
                    f"{dataset_id}: source row count changed "
                    f"({existing['source_rows']} -> {ds.row_count})")
            self._verify_sample_identity(dataset_id, ds)
        self._conn.commit()

    def _verify_sample_identity(self, dataset_id, ds) -> None:
        stored = {
            r["sample_id"]: (r["ord"], r["headline_hash"], r["ticker"])
            for r in self._conn.execute(
                "SELECT sample_id, ord, headline_hash, ticker FROM sample "
                "WHERE dataset_id=?", (dataset_id,))
        }
        incoming = {
            s["sample_id"]: (s["ord"], s["headline_hash"], s["ticker"])
            for s in ds.samples
        }
        if stored != incoming:
            missing = sorted(set(stored) - set(incoming))
            added = sorted(set(incoming) - set(stored))
            changed = sorted(
                sid for sid in set(stored) & set(incoming)
                if stored[sid] != incoming[sid])
            raise StoreError(
                f"{dataset_id}: sample identity changed (missing={missing[:5]}, "
                f"added={added[:5]}, changed={changed[:5]}); refusing to "
                "reorder or overwrite reviewed samples.")

    # -- reads -----------------------------------------------------------

    def list_datasets(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT dataset_id, display_name, role, source_sha256, source_rows, "
            "sampling_digest FROM dataset ORDER BY dataset_id").fetchall()
        return [dict(r) for r in rows]

    def get_sample(self, dataset_id: str, sample_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT s.dataset_id, s.sample_id, s.ord, s.ticker, "
            "s.published_at, s.source, s.headline_text, s.headline_hash, "
            "COALESCE(r.status, 'UNREVIEWED') AS status, "
            "r.category, r.direction, r.severity, r.ma_role, "
            "r.certainty, COALESCE(r.notes, '') AS notes, "
            "COALESCE(r.flagged, 0) AS flagged, r.rubric_version, "
            "COALESCE(r.rubric_approved, 0) AS rubric_approved, "
            "COALESCE(r.revealed_consensus, 0) AS revealed_consensus, "
            "COALESCE(r.revealed_model, 0) AS revealed_model, "
            "COALESCE(r.revision_no, 0) AS revision_no, "
            "COALESCE(r.version, 0) AS version, r.updated_at "
            "FROM sample s LEFT JOIN review r "
            "ON s.dataset_id=r.dataset_id AND s.sample_id=r.sample_id "
            "WHERE s.dataset_id=? AND s.sample_id=?",
            (dataset_id, sample_id)).fetchone()
        return dict(row) if row else None

    def ordered_sample_ids(self, dataset_id: str) -> list[str]:
        return [r["sample_id"] for r in self._conn.execute(
            "SELECT sample_id FROM sample WHERE dataset_id=? ORDER BY ord",
            (dataset_id,))]

    def resume_sample_id(self, dataset_id: str) -> str | None:
        """Lowest-ord sample that is UNREVIEWED or DRAFT (never LOCKED)."""
        row = self._conn.execute(
            "SELECT s.sample_id FROM sample s LEFT JOIN review r "
            "ON s.dataset_id=r.dataset_id AND s.sample_id=r.sample_id "
            "WHERE s.dataset_id=? AND (r.status IS NULL OR r.status IN "
            "('UNREVIEWED','DRAFT')) ORDER BY s.ord LIMIT 1",
            (dataset_id,)).fetchone()
        return row["sample_id"] if row else None

    def progress(self, dataset_id: str) -> dict:
        total = self._conn.execute(
            "SELECT COUNT(*) FROM sample WHERE dataset_id=?",
            (dataset_id,)).fetchone()[0]
        counts = {"UNREVIEWED": 0, "DRAFT": 0, "LOCKED": 0}
        for r in self._conn.execute(
                "SELECT status, COUNT(*) c FROM review WHERE dataset_id=? "
                "GROUP BY status", (dataset_id,)):
            counts[r["status"]] = r["c"]
        # Samples with no review row are UNREVIEWED.
        reviewed_rows = sum(counts.values())
        counts["UNREVIEWED"] += total - reviewed_rows
        flagged = self._conn.execute(
            "SELECT COUNT(*) FROM review WHERE dataset_id=? AND flagged=1",
            (dataset_id,)).fetchone()[0]
        canonical_locked = self._conn.execute(
            "SELECT COUNT(*) FROM review WHERE dataset_id=? AND "
            "status='LOCKED' AND rubric_approved=1",
            (dataset_id,)).fetchone()[0]
        return {
            "dataset_id": dataset_id,
            "total": total,
            "unreviewed": counts["UNREVIEWED"],
            "draft": counts["DRAFT"],
            "locked": counts["LOCKED"],
            "flagged": flagged,
            "remaining": counts["UNREVIEWED"] + counts["DRAFT"],
            # Only rubric-approved, explicitly locked labels count canonically.
            "canonical_locked": canonical_locked,
        }

    # -- rubric approval -------------------------------------------------

    def approve_rubric(self, rubric_version: str, *, note: str = "") -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"rubric_approved:{rubric_version}", _utcnow()))
        self._audit(dataset_id="*", sample_id=None, event="RUBRIC_APPROVED",
                    reason=note or None,
                    detail={"rubric_version": rubric_version})
        self._conn.commit()

    def is_rubric_approved(self, rubric_version: str) -> bool:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key=?",
            (f"rubric_approved:{rubric_version}",)).fetchone()
        return row is not None

    # -- helpers ---------------------------------------------------------

    def _ensure_review_row(self, dataset_id, sample_id) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM review WHERE dataset_id=? AND sample_id=?",
            (dataset_id, sample_id)).fetchone()
        if row is None:
            now = _utcnow()
            self._conn.execute(
                "INSERT INTO review (dataset_id, sample_id, status, created_at, "
                "updated_at, version) VALUES (?,?,'UNREVIEWED',?,?,0)",
                (dataset_id, sample_id, now, now))
            row = self._conn.execute(
                "SELECT * FROM review WHERE dataset_id=? AND sample_id=?",
                (dataset_id, sample_id)).fetchone()
        return row

    @staticmethod
    def _validate_certainty(certainty):
        if certainty is not None and certainty not in VALID_CERTAINTY:
            raise InvalidStateError(f"invalid certainty {certainty!r}")

    def _cas_update(self, dataset_id, sample_id, expected_version, assignments,
                    params) -> int:
        """Compare-and-swap update; returns the new version."""
        new_version = expected_version + 1
        sql = (
            "UPDATE review SET " + ", ".join(assignments) +
            ", version=?, updated_at=? WHERE dataset_id=? AND sample_id=? "
            "AND version=?")
        cur = self._conn.execute(
            sql, (*params, new_version, _utcnow(), dataset_id, sample_id,
                  expected_version))
        if cur.rowcount != 1:
            raise StaleWriteError(
                f"stale write for {dataset_id}/{sample_id}: expected version "
                f"{expected_version}")
        return new_version

    # -- mutations -------------------------------------------------------

    def save_draft(self, dataset_id, sample_id, *, label=None, certainty=None,
                   notes="", flagged=None, expected_version) -> int:
        """Save a partial/complete draft. Refuses to overwrite a LOCKED row."""
        self._validate_certainty(certainty)
        if label is not None:
            # Partial labels are allowed in a draft, but any present field must
            # be a valid enum value; full conditional validity is enforced at
            # lock time.
            from backtest.calibration_reviewer.labels import _ALLOWED
            for k, v in label.items():
                if k not in _ALLOWED:
                    raise InvalidHumanLabelError(f"unknown label field {k!r}")
                if v in (None, ""):
                    continue
                if v not in _ALLOWED[k]:
                    raise InvalidHumanLabelError(f"unknown {k} {v!r}")
        row = self._ensure_review_row(dataset_id, sample_id)
        if row["status"] == STATUS_LOCKED:
            raise LockedLabelError(
                f"{dataset_id}/{sample_id} is LOCKED; use revise() with a "
                "reason to change it")
        prev_label = self._row_label(row)
        assignments = ["status=?", "certainty=?", "notes=?"]
        params = [STATUS_DRAFT, certainty, notes or ""]
        if label is not None:
            assignments += ["category=?", "direction=?", "severity=?",
                            "ma_role=?"]
            params += [label.get("category") or None,
                       label.get("direction") or None,
                       label.get("severity") or None,
                       label.get("ma_role") or None]
        if flagged is not None:
            assignments.append("flagged=?")
            params.append(1 if flagged else 0)
        new_version = self._cas_update(
            dataset_id, sample_id, expected_version, assignments, params)
        new_label = {k: label[k] for k in ("category", "direction", "severity",
                                           "ma_role") if label and label.get(k)}
        self._audit(dataset_id=dataset_id, sample_id=sample_id,
                    event="SAVE_DRAFT", prev_status=row["status"],
                    new_status=STATUS_DRAFT, prev_label=prev_label,
                    new_label=new_label or None)
        self._conn.commit()
        return new_version

    def lock(self, dataset_id, sample_id, *, label, certainty=None, notes="",
             expected_version, rubric_version, rubric_approved) -> int:
        """Explicitly lock a label. Requires all four valid fields."""
        validated = validate_human_label(label)  # raises on invalid/incomplete
        self._validate_certainty(certainty)
        row = self._ensure_review_row(dataset_id, sample_id)
        if row["status"] == STATUS_LOCKED:
            raise LockedLabelError(
                f"{dataset_id}/{sample_id} already LOCKED; use revise()")
        prev_label = self._row_label(row)
        lock_seq = int(row["lock_seq"]) + 1
        assignments = [
            "status=?", "category=?", "direction=?", "severity=?", "ma_role=?",
            "certainty=?", "notes=?", "rubric_version=?", "rubric_approved=?",
            "lock_seq=?",
        ]
        params = [STATUS_LOCKED, validated.category, validated.direction,
                  validated.severity, validated.ma_role, certainty,
                  notes or "", rubric_version, 1 if rubric_approved else 0,
                  lock_seq]
        new_version = self._cas_update(
            dataset_id, sample_id, expected_version, assignments, params)
        self._audit(dataset_id=dataset_id, sample_id=sample_id, event="LOCK",
                    prev_status=row["status"], new_status=STATUS_LOCKED,
                    prev_label=prev_label, new_label=validated.as_dict(),
                    detail={"rubric_version": rubric_version,
                            "rubric_approved": bool(rubric_approved)})
        self._conn.commit()
        return new_version

    def set_flag(self, dataset_id, sample_id, *, flagged, expected_version) -> int:
        row = self._ensure_review_row(dataset_id, sample_id)
        new_version = self._cas_update(
            dataset_id, sample_id, expected_version, ["flagged=?"],
            [1 if flagged else 0])
        self._audit(dataset_id=dataset_id, sample_id=sample_id,
                    event="FLAG" if flagged else "UNFLAG",
                    prev_status=row["status"], new_status=row["status"])
        self._conn.commit()
        return new_version

    def reveal(self, dataset_id, sample_id, *, kind, expected_version) -> int:
        """Mark consensus or model prediction as revealed. LOCKED only."""
        if kind not in ("consensus", "model"):
            raise InvalidStateError(f"unknown reveal kind {kind!r}")
        row = self._conn.execute(
            "SELECT * FROM review WHERE dataset_id=? AND sample_id=?",
            (dataset_id, sample_id)).fetchone()
        if row is None or row["status"] != STATUS_LOCKED:
            raise NotLockedError(
                "prediction reveal requires a LOCKED label")
        col = "revealed_consensus" if kind == "consensus" else "revealed_model"
        new_version = self._cas_update(
            dataset_id, sample_id, expected_version, [f"{col}=1"], [])
        self._audit(dataset_id=dataset_id, sample_id=sample_id,
                    event="REVEAL", prev_status=STATUS_LOCKED,
                    new_status=STATUS_LOCKED, detail={"kind": kind})
        self._conn.commit()
        return new_version

    def revise(self, dataset_id, sample_id, *, label, certainty=None, notes="",
               reason, expected_version, rubric_version, rubric_approved) -> int:
        """Revise a LOCKED label. Requires an explicit reason (append-only)."""
        if not reason or not str(reason).strip():
            raise InvalidStateError("revision requires a non-empty reason")
        validated = validate_human_label(label)
        self._validate_certainty(certainty)
        row = self._conn.execute(
            "SELECT * FROM review WHERE dataset_id=? AND sample_id=?",
            (dataset_id, sample_id)).fetchone()
        if row is None or row["status"] != STATUS_LOCKED:
            raise NotLockedError("revision requires a LOCKED label")
        prev_label = self._row_label(row)
        post_reveal = 1 if (row["revealed_consensus"] or row["revealed_model"]) else 0
        revision_no = int(row["revision_no"]) + 1
        lock_seq = int(row["lock_seq"]) + 1
        assignments = [
            "category=?", "direction=?", "severity=?", "ma_role=?", "certainty=?",
            "notes=?", "rubric_version=?", "rubric_approved=?", "revision_no=?",
            "lock_seq=?",
        ]
        params = [validated.category, validated.direction, validated.severity,
                  validated.ma_role, certainty, notes or "", rubric_version,
                  1 if rubric_approved else 0, revision_no, lock_seq]
        new_version = self._cas_update(
            dataset_id, sample_id, expected_version, assignments, params)
        self._audit(dataset_id=dataset_id, sample_id=sample_id, event="REVISE",
                    prev_status=STATUS_LOCKED, new_status=STATUS_LOCKED,
                    prev_label=prev_label, new_label=validated.as_dict(),
                    reason=str(reason).strip(), post_reveal=post_reveal)
        self._conn.commit()
        return new_version

    # -- audit / export reads --------------------------------------------

    def audit_ledger(self, dataset_id: str | None = None) -> list[dict]:
        if dataset_id:
            rows = self._conn.execute(
                "SELECT * FROM audit WHERE dataset_id=? ORDER BY audit_id",
                (dataset_id,)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM audit ORDER BY audit_id").fetchall()
        return [dict(r) for r in rows]

    def locked_rows(self, dataset_id: str) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT s.sample_id, s.ord, s.ticker, s.published_at, s.source, "
            "s.headline_text, s.headline_hash, r.category, r.direction, "
            "r.severity, r.ma_role, r.certainty, r.notes, r.flagged, "
            "r.rubric_version, r.rubric_approved, r.revealed_consensus, "
            "r.revealed_model, r.revision_no, r.lock_seq, r.version, "
            "r.created_at, r.updated_at FROM sample s JOIN review r "
            "ON s.dataset_id=r.dataset_id AND s.sample_id=r.sample_id "
            "WHERE s.dataset_id=? AND r.status='LOCKED' ORDER BY s.ord",
            (dataset_id,))]

    @staticmethod
    def _row_label(row) -> dict | None:
        if row is None:
            return None
        label = {k: row[k] for k in ("category", "direction", "severity",
                                     "ma_role")}
        return label if all(label.values()) else None
