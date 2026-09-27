"""Exact-source hydration for Website Builder R2.

A revision never starts from "whatever workspace happens to exist". It starts
from a source whose identity was proven and frozen at reservation time:

    LIVE   -- the exact ``publication_commit`` recorded in ``last_live_release``
    DRAFT  -- the exact durable ``tested_snapshot``

Both traverse the same four stages, in the same order, through the same
operation directory::

    RESERVED -> FETCHED -> VERIFIED -> READY

``READY`` is not "the bytes are on disk". It is "the pointer has been swapped
and that swap is durably recorded", and it is the only stage after which the
operation directory may be used. Before it, staging is unreachable by every
runtime component, because the only directory any runtime helper can resolve is
the one the ``current`` pointer names.

The ordering at the end is deliberate, and it is the one ordering whose failure
is recoverable::

    swap pointer -> persist ``pointer_mode`` + ``READY`` in ONE save -> sweep

Persisting first and swapping second would let a crash between the two leave
``pointer_mode = True`` with no ``current`` -- an unrecoverable wedge, because
``pointer_mode`` is monotonic and legacy fallback is then permanently
forbidden. Swap-then-save inverts that: if the save is lost, the pointer and
the promoted workspace are intact, and the same operation can finish the write
on its next attempt without re-materializing anything.
"""
from __future__ import annotations

import logging
import re
import shutil
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.deploy.git_output import (
    MaterializationRefusal,
    assert_write_target,
    refuse_tree_entry,
    verify_repository_identity,
)
from app.deploy.snapshot import EXCLUDED, TestedSnapshot, read_tree, digest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------

DRAFT_SNAPSHOT_UNAVAILABLE = "DRAFT_SNAPSHOT_UNAVAILABLE"

HYDRATION_BASE_DRIFT = "HYDRATION_BASE_DRIFT"
HYDRATION_COMMIT_UNAVAILABLE = "HYDRATION_COMMIT_UNAVAILABLE"
HYDRATION_TREE_MISMATCH = "HYDRATION_TREE_MISMATCH"
HYDRATION_REPO_MISMATCH = "HYDRATION_REPO_MISMATCH"
HYDRATION_SOURCE_MISMATCH = "HYDRATION_SOURCE_MISMATCH"
HYDRATION_ARTIFACT_MISMATCH = "HYDRATION_ARTIFACT_MISMATCH"
HYDRATION_UNSAFE_ENTRY = "HYDRATION_UNSAFE_ENTRY"
HYDRATION_UNSUPPORTED_LFS = "HYDRATION_UNSUPPORTED_LFS"
HYDRATION_STAGING_UNAVAILABLE = "HYDRATION_STAGING_UNAVAILABLE"
HYDRATION_RECORD_INVALID = "HYDRATION_RECORD_INVALID"
HYDRATION_RECOVERY_REQUIRED = "HYDRATION_RECOVERY_REQUIRED"
#: Post-swap only. The workspace IS current and valid; the durable record of
#: that is what could not be written. Only the owning operation's own resume
#: finishes it -- an arbitrary new operation does not.
HYDRATION_STATE_UNPERSISTED = "HYDRATION_STATE_UNPERSISTED"

#: How a ``MaterializationRefusal`` reason maps to a user-facing code. One
#: entry per reason the materializer can produce, so no refusal silently falls
#: back to a generic code.
_REFUSAL_CODES = {
    "tree_mismatch": HYDRATION_TREE_MISMATCH,
    "lfs": HYDRATION_UNSUPPORTED_LFS,
    "commit": HYDRATION_COMMIT_UNAVAILABLE,
}

#: Runtime directories that live inside every resolved workspace. Created in
#: the staging workspace before VERIFIED, so a workspace that becomes current
#: is immediately usable by every helper.
RUNTIME_DIRNAMES = (".hermes", ".browser", ".runtime")


def _code_for_refusal(exc: MaterializationRefusal) -> str:
    return _REFUSAL_CODES.get(exc.reason, HYDRATION_UNSAFE_ENTRY)


class HydrationError(RuntimeError):
    """One classified hydration refusal. Never carries a host path."""

    def __init__(self, error_code: str, message: str = "", **details: Any):
        self.error_code = error_code
        self.details = dict(details)
        super().__init__(message or error_code)


# ---------------------------------------------------------------------------
# Revision base
# ---------------------------------------------------------------------------

BASE_KIND_LIVE = "LIVE"
BASE_KIND_DRAFT = "DRAFT"
BASE_KINDS = (BASE_KIND_LIVE, BASE_KIND_DRAFT)

_SHA1_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_OP_TOKEN_RE = re.compile(r"rev-[0-9]+")

#: The LIVE-only identity fields on a base. Present and well-formed on a LIVE
#: record; exactly ``None`` on a DRAFT one. A DRAFT has no commit, no
#: repository and no branch -- it has a frozen snapshot -- and a placeholder in
#: any of these would be a fabricated identity that no later reader could tell
#: from a proven one.
_LIVE_IDENTITY_FIELDS = (
    "publication_commit", "publication_tree", "tested_commit", "tested_tree",
    "publication_repo", "publication_branch",
)


@dataclass(frozen=True)
class RevisionBase:
    """The revision base, frozen at reservation time.

    Once frozen this never drifts. A later publication, a swapped
    ``tested_snapshot``, a moved branch tip: none of them change what this
    revision was admitted against, and ``assert_immutable`` is what enforces
    that instead of trusting every caller to remember.
    """

    base_kind: str
    reserved_at: float
    revision_seq: int
    requirements_version: int
    design_dna_version: int
    source_revision: int
    source_sha256: str
    artifact_sha256: str
    publication_commit: Optional[str] = None
    publication_tree: Optional[str] = None
    tested_commit: Optional[str] = None
    tested_tree: Optional[str] = None
    publication_repo: Optional[str] = None
    publication_branch: Optional[str] = None
    snapshot_identity: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: Any) -> "RevisionBase":
        if not isinstance(data, dict):
            raise HydrationError(HYDRATION_RECORD_INVALID,
                                 "revision base is not an object")
        known = {f.name for f in fields(cls)}
        try:
            base = cls(**{name: data[name] for name in data if name in known})
        except TypeError as exc:
            raise HydrationError(
                HYDRATION_RECORD_INVALID, "revision base is incomplete") from exc
        base.validate()
        return base

    def validate(self) -> None:
        if self.base_kind not in BASE_KINDS:
            raise HydrationError(HYDRATION_RECORD_INVALID,
                                 "unknown revision base kind")
        if (not isinstance(self.revision_seq, int)
                or isinstance(self.revision_seq, bool)):
            raise HydrationError(HYDRATION_RECORD_INVALID,
                                 "revision base seq is not an int")
        for name in ("source_sha256", "artifact_sha256"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
                raise HydrationError(HYDRATION_RECORD_INVALID,
                                     f"{name} is not a digest")
        if self.base_kind == BASE_KIND_LIVE:
            for name in ("publication_commit", "publication_tree",
                         "tested_commit", "tested_tree"):
                value = getattr(self, name)
                if not isinstance(value, str) or not _SHA1_RE.fullmatch(value):
                    raise HydrationError(HYDRATION_RECORD_INVALID,
                                         f"{name} is not a commit id")
            for name in ("publication_repo", "publication_branch"):
                if not isinstance(getattr(self, name), str) \
                        or not getattr(self, name):
                    raise HydrationError(HYDRATION_RECORD_INVALID,
                                         f"{name} is missing")
            if self.snapshot_identity is not None:
                raise HydrationError(
                    HYDRATION_RECORD_INVALID,
                    "a LIVE base must not carry a snapshot identity")
        else:
            for name in _LIVE_IDENTITY_FIELDS:
                if getattr(self, name) is not None:
                    raise HydrationError(
                        HYDRATION_RECORD_INVALID,
                        f"a DRAFT base must not carry {name}")
            if not isinstance(self.snapshot_identity, str) \
                    or not _SHA256_RE.fullmatch(self.snapshot_identity):
                raise HydrationError(HYDRATION_RECORD_INVALID,
                                     "snapshot_identity is not a digest")

    def assert_immutable(self, persisted: Any) -> None:
        """Refuse to hydrate from a base that is not the reserved one.

        Compared against the ON-DISK reservation, never against something
        recomputed from current state: re-deriving the base to compare against
        it would be circular, and re-deriving it is exactly the failure this
        exists to catch.
        """
        try:
            other = RevisionBase.from_dict(persisted)
        except HydrationError as exc:
            raise HydrationError(
                HYDRATION_BASE_DRIFT,
                "the reserved revision base is unreadable") from exc
        if other.to_dict() != self.to_dict():
            raise HydrationError(
                HYDRATION_BASE_DRIFT, "the reserved revision base changed")


def build_live_base(record: Dict[str, Any], *, seq: int, reserved_at: float,
                    requirements_version: int,
                    design_dna_version: int) -> RevisionBase:
    """Freeze the LIVE base from an admitted ``last_live_release``."""
    return RevisionBase(
        base_kind=BASE_KIND_LIVE,
        reserved_at=reserved_at,
        revision_seq=seq,
        requirements_version=requirements_version,
        design_dna_version=design_dna_version,
        source_revision=record["source_revision"],
        publication_commit=record["publication_commit"],
        publication_tree=record["publication_tree"],
        tested_commit=record["tested_commit"],
        tested_tree=record["tested_tree"],
        publication_repo=record["publication_repo"],
        publication_branch=record["publication_branch"],
        source_sha256=record["source_sha256"],
        artifact_sha256=record["artifact_sha256"],
    )


def build_draft_base(snapshot: TestedSnapshot, *, seq: int, reserved_at: float,
                     requirements_version: int, design_dna_version: int,
                     source_revision: int) -> RevisionBase:
    """Freeze the DRAFT base from the durable tested snapshot."""
    return RevisionBase(
        base_kind=BASE_KIND_DRAFT,
        reserved_at=reserved_at,
        revision_seq=seq,
        requirements_version=requirements_version,
        design_dna_version=design_dna_version,
        source_revision=source_revision,
        source_sha256=snapshot.source_sha256,
        artifact_sha256=snapshot.artifact_sha256,
        snapshot_identity=snapshot.identity,
    )


def monotonic_pointer_mode(deployment: Dict[str, Any]) -> None:
    """Set ``pointer_mode`` forward only. Never ``False``, never removed.

    Monotonic by construction: the only write this function can make is
    ``True``, so a later operation, a sweep, or a load/migration round-trip
    can never retroactively claim the project never crossed the pointer
    boundary. That claim is the one historical fact the resolver consults, and
    it is irreversible on purpose.
    """
    if deployment.get("pointer_mode") is not True:
        deployment["pointer_mode"] = True


# ---------------------------------------------------------------------------
# Hydration record
# ---------------------------------------------------------------------------

HYDRATION_RESERVED = "RESERVED"
HYDRATION_FETCHED = "FETCHED"
HYDRATION_VERIFIED = "VERIFIED"
HYDRATION_READY = "READY"
HYDRATION_STAGES = (HYDRATION_RESERVED, HYDRATION_FETCHED,
                    HYDRATION_VERIFIED, HYDRATION_READY)

#: The LIVE-only identity fields on the hydration record. LIVE populates all
#: of them; DRAFT carries exactly ``None`` for all of them (D28).
_RECORD_LIVE_IDENTITY = (
    "commit", "repo", "branch", "expected_tree", "tested_commit_corroborated",
)

_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_BRANCH_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}")


@dataclass(frozen=True)
class HydrationRecord:
    """``deployment.hydration``: the ONE current operation record.

    A later operation supersedes this record with its own ``RESERVED``; a
    previous ``READY`` is a normal completed operation, not a fault. What a
    supersede must never do is erase the historical fact that a swap committed,
    which is why that fact lives in ``pointer_mode`` and not here.
    """

    operation_id: str
    state: str
    base_kind: str
    commit: Optional[str]
    repo: Optional[str]
    branch: Optional[str]
    expected_tree: Optional[str]
    tested_commit_corroborated: Optional[bool]
    fetched_locally: bool
    op_dir: str
    op_token: str
    expected_source_sha256: str
    expected_artifact_sha256: str
    updated_at: float
    error_code: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: Any) -> "HydrationRecord":
        """Load and enforce the per-kind field contract, in BOTH directions.

        A violation is refused at load, never silently coerced: a LIVE record
        missing its identity, or a DRAFT record carrying one, means the record
        and the workspace it describes are not the same thing.
        """
        if not isinstance(data, dict):
            raise HydrationError(HYDRATION_RECORD_INVALID,
                                 "hydration record is not an object")
        known = {f.name for f in fields(cls)}
        try:
            record = cls(**{name: data[name] for name in data if name in known})
        except TypeError as exc:
            # A record missing a field is a record that describes a workspace
            # this code cannot reason about. Refuse it as such rather than
            # letting a constructor error escape as an unclassified crash.
            raise HydrationError(
                HYDRATION_RECORD_INVALID, "hydration record is incomplete") from exc
        record.validate()
        return record

    def validate(self) -> None:
        if not isinstance(self.operation_id, str) or not self.operation_id:
            raise HydrationError(HYDRATION_RECORD_INVALID,
                                 "operation_id is required")
        if self.state not in HYDRATION_STAGES:
            raise HydrationError(HYDRATION_RECORD_INVALID,
                                 f"unknown hydration state {self.state!r}")
        if self.base_kind not in BASE_KINDS:
            raise HydrationError(HYDRATION_RECORD_INVALID, "unknown base_kind")
        for name in ("expected_source_sha256", "expected_artifact_sha256"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
                raise HydrationError(HYDRATION_RECORD_INVALID,
                                     f"{name} is not a digest")
        if not isinstance(self.op_token, str) \
                or not _OP_TOKEN_RE.fullmatch(self.op_token):
            raise HydrationError(HYDRATION_RECORD_INVALID,
                                 "op_token is not a revision token")
        if not isinstance(self.fetched_locally, bool):
            raise HydrationError(HYDRATION_RECORD_INVALID,
                                 "fetched_locally must be a bool")
        if self.base_kind == BASE_KIND_LIVE:
            if not isinstance(self.commit, str) or not _SHA1_RE.fullmatch(self.commit):
                raise HydrationError(HYDRATION_RECORD_INVALID,
                                     "a LIVE record requires commit")
            if not isinstance(self.expected_tree, str) \
                    or not _SHA1_RE.fullmatch(self.expected_tree):
                raise HydrationError(HYDRATION_RECORD_INVALID,
                                     "a LIVE record requires expected_tree")
            if not isinstance(self.repo, str) or not _REPO_RE.fullmatch(self.repo):
                raise HydrationError(HYDRATION_RECORD_INVALID,
                                     "a LIVE record requires repo")
            if not isinstance(self.branch, str) \
                    or not _BRANCH_RE.fullmatch(self.branch):
                raise HydrationError(HYDRATION_RECORD_INVALID,
                                     "a LIVE record requires branch")
            if not isinstance(self.tested_commit_corroborated, bool):
                raise HydrationError(
                    HYDRATION_RECORD_INVALID,
                    "a LIVE record requires a tested_commit corroboration")
        else:
            for name in _RECORD_LIVE_IDENTITY:
                if getattr(self, name) is not None:
                    raise HydrationError(
                        HYDRATION_RECORD_INVALID,
                        f"a DRAFT record must carry {name} as null")
            if self.fetched_locally is not True:
                raise HydrationError(
                    HYDRATION_RECORD_INVALID,
                    "a DRAFT hydration is always acquired locally")

    def with_state(self, state: str, **changes: Any) -> "HydrationRecord":
        data = self.to_dict()
        data["state"] = state
        data["updated_at"] = time.time()
        data.update(changes)
        return HydrationRecord.from_dict(data)

    @property
    def hydrated_from(self) -> Dict[str, Any]:
        """Diagnostics shape, identical for both kinds; ``None`` where N/A."""
        return {
            "kind": self.base_kind,
            "commit": self.commit,
            "fetched_locally": self.fetched_locally,
            "tested_commit_corroborated": self.tested_commit_corroborated,
        }


@dataclass(frozen=True)
class HydrationOutcome:
    """What a successful hydration produced."""

    workspace: Path
    record: HydrationRecord

    @property
    def base_kind(self) -> str:
        return self.record.base_kind

    @property
    def hydrated_from(self) -> Dict[str, Any]:
        return self.record.hydrated_from


#: The exact repository-relative names a hydration is allowed to produce.
#: Derived from what was ADMITTED (the commit's own entry list, or the frozen
#: snapshot's own keys) and re-checked against the real filesystem.
ExpectedNames = Tuple[Optional[frozenset], Optional[frozenset]]


def _reserved_record(base: RevisionBase, operation_id: str, op_token: str,
                     op_dir: Path) -> HydrationRecord:
    """The RESERVED record. Nothing has been acquired yet.

    It already carries the LIVE identity, because at RESERVED the identity is
    known -- it was frozen at reservation time. What is not known yet is
    whether the object is local, which is exactly what ``FETCHED`` reports.
    """
    live = base.base_kind == BASE_KIND_LIVE
    return HydrationRecord(
        operation_id=operation_id,
        state=HYDRATION_RESERVED,
        base_kind=base.base_kind,
        commit=base.publication_commit if live else None,
        repo=base.publication_repo if live else None,
        branch=base.publication_branch if live else None,
        expected_tree=base.publication_tree if live else None,
        tested_commit_corroborated=None,
        fetched_locally=not live,
        op_dir=str(op_dir),
        op_token=op_token,
        expected_source_sha256=base.source_sha256,
        expected_artifact_sha256=base.artifact_sha256,
        updated_at=time.time(),
        error_code=None,
    )


# ---------------------------------------------------------------------------
# The hydrator
# ---------------------------------------------------------------------------


class WorkspaceHydrator:
    """Materializes a disposable revision workspace from a frozen base.

    ``runner`` supplies the pointer layout (the single resolver authority) and
    ``store`` supplies durable state. LIVE additionally needs the output
    repository and the operator-configured source remote: without them there is
    no exact source to acquire, and the LIVE path fails closed rather than
    falling back to anything that happens to be on disk.
    """

    def __init__(self, runner, store, *, output_repo=None, source_repo_url=None,
                 source_ssh_key=None):
        self.runner = runner
        self.store = store
        self.output_repo = output_repo
        self.source_repo_url = source_repo_url
        self.source_ssh_key = source_ssh_key

    # -- durable record -------------------------------------------------

    def _load_record(self, project_id: str) -> Optional[HydrationRecord]:
        state = self.store.load(project_id)
        if state is None:
            return None
        raw = (state.deployment or {}).get("hydration")
        if raw is None:
            return None
        return HydrationRecord.from_dict(raw)

    def _save_record(self, project_id: str, record: HydrationRecord) -> None:
        with self.store.acquire_writer(project_id) as state:
            state.deployment["hydration"] = record.to_dict()
            self.store.save(state)

    def _assert_pointer_mode_is_bool(self, project_id: str) -> None:
        state = self.store.load(project_id)
        if state is None:
            return
        value = (state.deployment or {}).get("pointer_mode")
        if value is not None and not isinstance(value, bool):
            raise HydrationError(HYDRATION_RECORD_INVALID,
                                 "pointer_mode is not a bool")

    def _commit_ready(self, project_id: str, record: HydrationRecord) -> HydrationRecord:
        """Persist ``pointer_mode`` + ``READY`` in ONE durable write.

        Called only AFTER the pointer has already been swapped. Never rolls
        back and never deletes: at this point the workspace is committed and
        valid, and the only thing missing is the durable statement that it is.
        Losing that write is a durability failure, not a hydration failure, and
        the same operation can finish it on its next attempt.
        """
        self._assert_pointer_mode_is_bool(project_id)
        ready = record.with_state(HYDRATION_READY)
        try:
            with self.store.acquire_writer(project_id) as state:
                monotonic_pointer_mode(state.deployment)
                hydration = ready.to_dict()
                existing = state.deployment.get("hydration")
                if (isinstance(existing, dict)
                        and existing.get("operation_id") == ready.operation_id):
                    # Carry forward any field a newer writer added, so this
                    # completion never silently drops durable evidence.
                    for key, value in existing.items():
                        hydration.setdefault(key, value)
                state.deployment["hydration"] = hydration
                self.store.save(state)
        except HydrationError:
            raise
        except Exception as exc:
            raise HydrationError(
                HYDRATION_STATE_UNPERSISTED, type(exc).__name__) from exc
        return ready

    # -- entry point ----------------------------------------------------

    def hydrate(self, project_id: str, seq: int,
                base: RevisionBase) -> HydrationOutcome:
        """Materialize and promote one revision workspace.

        Raises ``HydrationError`` on every refusal. Two of those codes are not
        refusals of the work itself: ``HYDRATION_STATE_UNPERSISTED`` (the work
        is done; only the durable record of it was lost) and
        ``HYDRATION_RECOVERY_REQUIRED`` (another operation owns the current
        workspace and must finish first).
        """
        if not isinstance(base, RevisionBase):
            raise HydrationError(HYDRATION_RECORD_INVALID,
                                 "no revision base supplied")
        base.validate()
        op_token = f"rev-{seq}"
        operation_id = op_token

        # The pointer resolve runs FIRST and independently of the record
        # table: an unusable pointer is a refusal whatever the record claims,
        # and a resolvable pointer is a filesystem fact no record overrules.
        self.runner.resolve_workspace(project_id)
        pointer = self.runner.pointer_token(project_id)

        # The base handed in must still BE the reserved base.
        self._assert_reserved_base(project_id, seq, base)

        record = self._load_record(project_id)
        op_dir = self.runner.op_dir_for(project_id, seq)

        # ---- case (a): the swap already committed for OUR operation dir.
        if pointer == op_token:
            if record is not None and record.operation_id == operation_id:
                # Preserve the directory: no rebuild, no re-materialization,
                # no delete. The swap already made it current and it is intact.
                ready = self._commit_ready(project_id, record)
            else:
                ready = self._commit_ready(
                    project_id, _reserved_record(base, operation_id, op_token, op_dir))
            self.runner.sweep_ops(project_id)
            return HydrationOutcome(self.runner.resolve_workspace(project_id), ready)

        if record is not None and record.operation_id != operation_id:
            if record.state != HYDRATION_READY and pointer == record.op_token:
                # Never delete or sweep that directory, and never start a new
                # hydration on top of it: the owning operation must finish.
                raise HydrationError(
                    HYDRATION_RECOVERY_REQUIRED,
                    "another operation owns the current workspace",
                    operation_id=record.operation_id)
            # A foreign READY is a normal previous completed operation and a
            # non-READY record that is not the pointer target is abandoned
            # staging. Both fall through; the previous current stays alive
            # until the new swap commits.
            self.runner.sweep_ops(project_id, keep=(record.op_token,))

        if (record is not None and record.operation_id == operation_id
                and record.state == HYDRATION_VERIFIED):
            # ---- case (b): verified staging, swap not yet committed.
            outcome = self._resume_verified(project_id, operation_id, op_token,
                                            op_dir, base, record)
            if outcome is not None:
                return outcome
            # The staging did not survive its own digests. It is disposable,
            # so fall through and rebuild it from the frozen base rather than
            # promoting bytes nothing has vouched for since the restart.

        return self._run(project_id, operation_id, op_token, op_dir, base)

    # -- internals ------------------------------------------------------

    def _assert_reserved_base(self, project_id: str, seq: int,
                              base: RevisionBase) -> None:
        state = self.store.load(project_id)
        entry = next(
            (e for e in ((state.pending_revisions if state else None) or [])
             if isinstance(e, dict) and e.get("seq") == seq
             and e.get("applied") is False),
            None,
        )
        if entry is None or not isinstance(entry.get("base"), dict):
            raise HydrationError(
                HYDRATION_BASE_DRIFT,
                "the revision reservation carries no frozen base")
        base.assert_immutable(entry["base"])

    def _resume_verified(self, project_id, operation_id, op_token, op_dir,
                         base, record) -> Optional[HydrationOutcome]:
        """Finish a VERIFIED operation whose swap had not committed yet.

        Staging has been sitting on disk across a restart and nothing protects
        it, so it is re-verified from the filesystem before it is allowed to
        become current. A mismatch means it is no longer what was verified, so
        it is discarded and the caller rebuilds it from the frozen base.
        """
        if op_dir.is_symlink() or not op_dir.is_dir():
            return None
        try:
            self._verify(op_dir, expected_source=base.source_sha256,
                         expected_artifact=base.artifact_sha256)
        except HydrationError:
            return None
        self.runner.write_pointer(project_id, op_token)
        ready = self._commit_ready(project_id, record)
        self.runner.sweep_ops(project_id)
        return HydrationOutcome(self.runner.resolve_workspace(project_id), ready)

    def _run(self, project_id, operation_id, op_token, op_dir,
             base) -> HydrationOutcome:
        """RESERVED -> FETCHED -> VERIFIED -> READY."""
        # A directory under our own seq-scoped name belongs to this exact
        # reservation and is therefore always disposable. A symlink under that
        # name is not ours to follow or to delete, and a non-directory is not a
        # staging area at all.
        if op_dir.is_symlink():
            raise HydrationError(HYDRATION_STAGING_UNAVAILABLE,
                                 "staging directory is a symlink")
        if op_dir.exists() and not op_dir.is_dir():
            raise HydrationError(HYDRATION_STAGING_UNAVAILABLE,
                                 "staging path is not a directory")
        if op_dir.exists():
            shutil.rmtree(op_dir, ignore_errors=True)
        # Bound ``.ops`` before staging exists. ``keep`` protects this
        # operation's own directory; the CURRENT pointer target is protected
        # unconditionally by the sweep itself, which is what lets a new
        # operation run its whole sequence while the previous workspace stays
        # usable.
        self.runner.sweep_ops(project_id, keep=(op_token,))

        reserved = _reserved_record(base, operation_id, op_token, op_dir)
        self._save_record(project_id, reserved)

        fetched, expected = self._acquire(project_id, op_dir, base, reserved)
        # FETCHED is recorded only once acquisition AND materialization are
        # both done. A crash before this point is indistinguishable from a
        # crash during acquisition, and both are answered by discarding staging
        # and starting again from the same frozen base.
        self._save_record(project_id, fetched)

        verified = self._verify_and_record(project_id, op_dir, base, fetched,
                                           expected)
        return self._promote(project_id, op_token, verified)

    # -- FETCHED --------------------------------------------------------

    def _acquire(self, project_id: str, op_dir: Path, base: RevisionBase,
                 reserved: HydrationRecord) -> Tuple[HydrationRecord, ExpectedNames]:
        if base.base_kind == BASE_KIND_DRAFT:
            return self._acquire_draft(project_id, op_dir, base, reserved)
        return self._acquire_live(op_dir, base, reserved)

    def _acquire_draft(self, project_id, op_dir, base, reserved):
        """DRAFT acquisition: local, and deliberately no Git at all.

        The durable ``tested_snapshot`` IS the source. There is no object to
        resolve and no remote to ask, so this path spawns zero subprocesses and
        performs zero network operations. ``last_live_release`` is never read:
        a draft is not a publication and has no Git identity.
        """
        state = self.store.load(project_id)
        payload = ((state.deployment if state else {}) or {}).get("tested_snapshot")
        if not isinstance(payload, dict):
            raise HydrationError(DRAFT_SNAPSHOT_UNAVAILABLE, "no tested snapshot")
        try:
            snapshot = TestedSnapshot.from_dict(payload)
        except Exception as exc:
            raise HydrationError(
                DRAFT_SNAPSHOT_UNAVAILABLE, type(exc).__name__) from exc
        if snapshot.source_sha256 != base.source_sha256:
            raise HydrationError(HYDRATION_SOURCE_MISMATCH,
                                 "the tested snapshot source drifted")
        if snapshot.artifact_sha256 != base.artifact_sha256:
            raise HydrationError(HYDRATION_ARTIFACT_MISMATCH,
                                 "the tested snapshot artifact drifted")
        if snapshot.identity != base.snapshot_identity:
            raise HydrationError(HYDRATION_BASE_DRIFT,
                                 "the tested snapshot identity drifted")

        _materialize_snapshot(op_dir, snapshot)
        # ``fetched_locally`` is True for DRAFT unconditionally: the source is
        # the durable snapshot, and asserting that per-kind is what stops a
        # DRAFT record from ever claiming a network acquisition.
        record = reserved.with_state(HYDRATION_FETCHED, fetched_locally=True,
                                     tested_commit_corroborated=None)
        return record, (frozenset(snapshot.source), frozenset(snapshot.dist))

    def _acquire_live(self, op_dir, base, reserved):
        """LIVE acquisition: prove the exact publication identity, then write it.

        ``publication_commit`` is the source identity. ``tested_commit`` only
        corroborates: it is a different object that happened to share a tree,
        and materializing it in place of the publication commit would be
        substituting one commit for another whenever the publication object
        happened to be missing locally.
        """
        if self.output_repo is None:
            raise HydrationError(HYDRATION_COMMIT_UNAVAILABLE,
                                 "no output repository is configured")
        repo = self.output_repo
        # Repository identity is checked OFFLINE, before any network access, so
        # a mismatch is refused without ever asking the wrong remote anything.
        try:
            verify_repository_identity(self.source_repo_url, base.publication_repo)
        except MaterializationRefusal as exc:
            raise HydrationError(HYDRATION_REPO_MISMATCH, exc.reason) from exc

        if base.tested_tree != base.publication_tree:
            raise HydrationError(
                HYDRATION_TREE_MISMATCH,
                "the recorded tested and publication trees differ")

        fetched_locally = True
        corroborated = False
        if repo.has_tested_snapshot_commit(base.tested_commit):
            if repo.commit_tree(base.tested_commit) != base.tested_tree:
                raise HydrationError(
                    HYDRATION_TREE_MISMATCH,
                    "the tested commit's tree does not match")
            corroborated = True

        if not repo.has_commit(base.publication_commit):
            try:
                repo.fetch_pinned_commit(
                    base.publication_commit, self.source_repo_url,
                    ssh_key=self.source_ssh_key)
            except Exception as exc:
                raise HydrationError(
                    HYDRATION_COMMIT_UNAVAILABLE, type(exc).__name__) from exc
            # Re-assert after the fetch: a refspec that moved, a proxy that
            # answered with something else, and a repository that no longer
            # holds the object all look exactly like a successful exit.
            if not repo.has_commit(base.publication_commit):
                raise HydrationError(HYDRATION_COMMIT_UNAVAILABLE,
                                     "the publication commit is absent")
            # The corroboration was proven against the pre-fetch object store.
            corroborated = False
            fetched_locally = False

        if repo.commit_tree(base.publication_commit) != base.publication_tree:
            raise HydrationError(
                HYDRATION_TREE_MISMATCH,
                "the publication commit's tree does not match")

        try:
            written = repo.materialize_commit(
                base.publication_commit, base.publication_tree, op_dir)
        except MaterializationRefusal as exc:
            raise HydrationError(_code_for_refusal(exc), exc.reason,
                                 path=exc.path) from exc

        record = reserved.with_state(
            HYDRATION_FETCHED,
            tested_commit_corroborated=corroborated,
            fetched_locally=fetched_locally,
        )
        return record, (frozenset(written["source"]), frozenset(written["dist"]))

    # -- VERIFIED -------------------------------------------------------

    def _verify_and_record(self, project_id, op_dir, base, fetched,
                           expected) -> HydrationRecord:
        # The runtime directories live INSIDE the resolved workspace, so they
        # are created here -- before verification, because a workspace that
        # becomes current without them fails every helper on first use. They
        # are excluded from the source fingerprint and the artifact digest, so
        # creating them here changes neither.
        for name in RUNTIME_DIRNAMES:
            (op_dir / name).mkdir(exist_ok=True)
        self._verify(op_dir, expected_source=base.source_sha256,
                     expected_artifact=base.artifact_sha256, expected=expected)
        verified = fetched.with_state(HYDRATION_VERIFIED)
        # VERIFIED is a DURABLE stage, not an in-memory one. The resume-after-
        # restart path reads it from disk to know that staging is complete and
        # only needs the swap; without this write a crash between verification
        # and the swap would be indistinguishable from a crash before it.
        self._save_record(project_id, verified)
        return verified

    def _verify(self, op_dir: Path, *, expected_source: str, expected_artifact: str,
                expected: ExpectedNames = (None, None)) -> None:
        """Prove the bytes on disk are the bytes that were admitted.

        One INDEPENDENT re-walk of the real filesystem feeds all three
        assertions. It is independent of the write path: it re-derives the
        trees from disk rather than from anything the writer believed it
        wrote, so it catches a symlink, an unsafe name, an extra file and a
        missing file -- none of which the writer can vouch for on its own.
        """
        try:
            source = read_tree(op_dir, EXCLUDED)
            dist = read_tree(Path(op_dir) / "dist")
        except (ValueError, OSError) as exc:
            raise HydrationError(
                HYDRATION_UNSAFE_ENTRY,
                "the hydrated workspace is not readable") from exc

        expected_source_names, expected_dist_names = expected
        if expected_source_names is not None and set(source) != set(expected_source_names):
            raise HydrationError(
                HYDRATION_UNSAFE_ENTRY,
                "the hydrated source names are not the committed names")
        if expected_dist_names is not None and set(dist) != set(expected_dist_names):
            raise HydrationError(
                HYDRATION_UNSAFE_ENTRY,
                "the hydrated artifact names are not the committed names")
        if not source:
            raise HydrationError(HYDRATION_UNSAFE_ENTRY,
                                 "the hydrated workspace is empty")
        if digest(source) != expected_source:
            raise HydrationError(HYDRATION_SOURCE_MISMATCH,
                                 "the hydrated source does not match")
        if digest(dist) != expected_artifact:
            raise HydrationError(HYDRATION_ARTIFACT_MISMATCH,
                                 "the hydrated artifact does not match")

    # -- READY ----------------------------------------------------------

    def _promote(self, project_id, op_token, verified) -> HydrationOutcome:
        """The pointer commit boundary.

        The swap is what makes the directory this project's workspace. After
        it, that directory is never deleted and never rolled back: it is the
        current workspace, and "disposable" stopped being true the instant the
        pointer named it.
        """
        self.runner.write_pointer(project_id, op_token)
        ready = self._commit_ready(project_id, verified)
        # Only now is it safe to drop the previous operation: the new one is
        # current AND its READY / pointer_mode write has landed.
        self.runner.sweep_ops(project_id)
        return HydrationOutcome(self.runner.resolve_workspace(project_id), ready)


# ---------------------------------------------------------------------------
# DRAFT materialization
# ---------------------------------------------------------------------------


def _materialize_snapshot(op_dir: Path, snapshot: TestedSnapshot) -> None:
    """Write a frozen snapshot into a fresh operation workspace.

    ``source`` keys are root-relative and land at the root; ``dist`` keys are
    ``dist``-relative and land under ``dist/``. That is NOT the mapping the
    LIVE path uses, because a commit is addressed by its full repository path
    while a snapshot stores the two trees separately. The safety predicate and
    the per-entry containment check are identical, so a snapshot key cannot
    escape the operation directory any more than a commit path can.
    """
    op_dir.mkdir(parents=True, exist_ok=True)
    for name, content in sorted(snapshot.source.items()):
        destination = op_dir / name
        try:
            refuse_tree_entry("100644", "blob", name)
            assert_write_target(op_dir, destination)
        except MaterializationRefusal as exc:
            raise HydrationError(_code_for_refusal(exc), exc.reason,
                                 path=exc.path) from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    for name, content in sorted(snapshot.dist.items()):
        relative = "dist/" + name
        destination = op_dir / relative
        try:
            refuse_tree_entry("100644", "blob", relative)
            assert_write_target(op_dir, destination)
        except MaterializationRefusal as exc:
            raise HydrationError(_code_for_refusal(exc), exc.reason,
                                 path=name) from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
