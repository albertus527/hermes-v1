"""Persistent project state for Website Builder R1.

File-based JSON state with one-writer-per-project atomic ownership.
No database, no Redis, no distributed lock service.
"""

from __future__ import annotations

import json
import os
import tempfile
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Generator, Optional, Set

from .lifecycle import ProjectLifecycle, transition as lifecycle_transition


@dataclass
class RevisionState:
    """Revision tracking per canonical spec §20."""

    requirements_version: int = 0
    design_dna_version: int = 0
    source_revision: int = 0
    qa_revision: int = 0
    preview_revision: int = 0
    approved_revision: int = 0
    # Phase 11: last revision successfully promoted to production (LIVE).
    live_revision: int = 0
    # Phase 10: strictly-ordered natural-language revision requests.
    # revision_seq is the last SUCCESSFULLY APPLIED ordered revision number.
    # queued_revision_seq is the last SUBMITTED (queued or applied) ordered
    # revision number — submission-time ordering authority so duplicate/
    # out-of-order submissions are rejected before ever touching the
    # workspace or Design DNA.
    revision_seq: int = 0
    queued_revision_seq: int = 0


@dataclass
class DomainState:
    """Domain discovery state per canonical spec §8."""

    domain: Optional[str] = None
    status: str = "UNKNOWN"
    price: Optional[str] = None
    currency: Optional[str] = None
    term: Optional[str] = None
    renewal_price: Optional[str] = None
    source: Optional[str] = None
    checked_at: Optional[float] = None
    alternatives: list = field(default_factory=list)
    deferred: bool = False


    # Phase 16 connection lifecycle is separate from availability status.
    # Intent survives discovery changes and process restarts; never clear
    # attempted writes merely because a provider read fails or returns 404.
    connection_stage: str = "NONE"
    connection: Dict[str, Any] = field(default_factory=dict)
    dns_records: list = field(default_factory=list)
    verified_at: Optional[float] = None
    attached_at: Optional[float] = None
    last_error: Optional[str] = None


@dataclass
class ProjectState:
    """Persistent project state per canonical spec §24."""

    project_id: str
    owner_id: Optional[str] = None
    channel: Optional[str] = None
    conversation_id: Optional[str] = None
    lifecycle: str = ProjectLifecycle.DISCOVERING.value
    brief: Dict[str, Any] = field(default_factory=dict)
    revisions: RevisionState = field(default_factory=RevisionState)
    domain: DomainState = field(default_factory=DomainState)
    design_dna: Dict[str, Any] = field(default_factory=dict)
    design_references: Dict[str, Any] = field(default_factory=dict)
    # Phase 13: 2-3 lightweight FRONTEND-proposed design directions
    # (label/descriptor/palette only -- never a full build), and the one
    # the user chose. selected_direction is None until choose_direction()
    # persists a pick; a None selection leaves the existing Phase 7/12
    # full-build instruction wiring completely unaffected.
    design_directions: list = field(default_factory=list)
    selected_direction: Optional[Dict[str, Any]] = None
    pending_revisions: list = field(default_factory=list)
    pause_state: Dict[str, Any] = field(default_factory=dict)
    repository: Dict[str, Any] = field(default_factory=dict)
    deployment: Dict[str, Any] = field(default_factory=dict)
    production_url: Optional[str] = None
    failure: Optional[Dict[str, Any]] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    processed_events: Set[str] = field(default_factory=set)
    dispatch_events: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Local ACL: owner publishes, reviewers revise/approve, viewers only read.
    # Explicit roles are authoritative, including an explicitly absent owner.
    roles: Dict[str, Any] = field(
        default_factory=lambda: {"owner": None, "reviewers": [], "viewers": []}
    )
    # Local viewer bearer references. Keyed by SHA-256 hex
    # digest of the raw token (the raw token is returned to the caller
    # exactly once at creation time and is never persisted in plaintext).
    # Each entry: {"role": "viewer", "created_at": float,
    # "label": Optional[str]}.
    reference_tokens: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["processed_events"] = sorted(self.processed_events)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProjectState":
        data = dict(data)
        data["revisions"] = RevisionState(**data.get("revisions", {}))
        data["domain"] = DomainState(**data.get("domain", {}))
        data["processed_events"] = set(data.get("processed_events", []))
        # Only absent legacy ACLs inherit owner_id. Malformed/null ACLs stay
        # malformed so every shared authorization gate fails closed.
        if "roles" not in data:
            data["roles"] = {"owner": data.get("owner_id"), "reviewers": [], "viewers": []}
        return cls(**data)


class ProjectStateStore:
    """File-based persistent state store with atomic writes and per-project locking."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks_dir = self.root / ".locks"
        self._locks_dir.mkdir(exist_ok=True)

    @staticmethod
    def _validate_id(project_id: str) -> None:
        if not isinstance(project_id, str) or not re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?", project_id
        ):
            raise ValueError("Invalid project ID")
        if project_id.upper() in {"CON", "PRN", "AUX", "NUL", *(
            f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)
        )}:
            raise ValueError("Reserved project ID")

    def _project_path(self, project_id: str) -> Path:
        self._validate_id(project_id)
        path = self.root / f"{project_id}.json"
        if path.is_symlink():
            raise ValueError("State symlinks are forbidden")
        return path

    def _lock_path(self, project_id: str) -> Path:
        self._validate_id(project_id)
        return self._locks_dir / f"{project_id}.lock"

    def load(self, project_id: str) -> Optional[ProjectState]:
        path = self._project_path(project_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return ProjectState.from_dict(data)

    def save(self, state: ProjectState) -> None:
        state.updated_at = time.time()
        path = self._project_path(state.project_id)
        # Atomic write via temp file + rename
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state.to_dict(), f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    @contextmanager
    def acquire_writer(
        self, project_id: str, timeout: float = 30.0
    ) -> Generator[ProjectState, None, None]:
        """Acquire exclusive writer lock for a project.

        Uses a lock file with O_CREAT|O_EXCL for atomic acquisition.
        Yields the current state; caller must call save() to persist.
        """
        lock_path = self._lock_path(project_id)
        deadline = time.monotonic() + timeout
        acquired = False
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                acquired = True
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        if not acquired:
            raise TimeoutError(f"Could not acquire writer lock for project {project_id}")

        try:
            state = self.load(project_id)
            if state is None:
                state = ProjectState(project_id=project_id)
            yield state
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    def transition_lifecycle(
        self, project_id: str, target: ProjectLifecycle
    ) -> ProjectState:
        """Load, transition, and save project lifecycle atomically.

        For callers that do NOT already hold the writer lock.
        """
        with self.acquire_writer(project_id) as state:
            current = ProjectLifecycle(state.lifecycle)
            state.lifecycle = lifecycle_transition(current, target).value
            self.save(state)
            return state

    def transition_lifecycle_locked(
        self, state: ProjectState, target: ProjectLifecycle
    ) -> ProjectState:
        """Transition lifecycle on an already-locked ProjectState.

        For callers that DO already hold the writer lock (inside
        acquire_writer). Does NOT reacquire the lock.

        This is the single deterministic lifecycle validation authority.
        """
        current = ProjectLifecycle(state.lifecycle)
        state.lifecycle = lifecycle_transition(current, target).value
        return state

    def is_event_processed(self, project_id: str, event_id: str) -> bool:
        """Legacy intake event lookup; dispatch owns its separate scoped claims."""
        state = self.load(project_id)
        if state is None:
            return False
        return event_id in state.processed_events

    def mark_event_processed(self, project_id: str, event_id: str) -> None:
        """Persist a legacy event marker while acquiring the writer internally."""
        with self.acquire_writer(project_id) as state:
            state.processed_events.add(event_id)
            self.save(state)
