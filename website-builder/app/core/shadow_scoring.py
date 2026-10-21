"""Shadow scoring — a diagnostic-only, write-only record.

Shadow scoring exists to MEASURE the rubric without letting it influence a
user. That guarantee has to be structural, not a promise: the engine ranks on
``candidate.suitability``, so a shadow result that is ever attached to a
candidate is LIVE the moment the engine looks at it.

So the record here is deliberately write-only:

* it is a separate frozen type, in its own module;
* it is written to the workspace, never returned into the candidate pool;
* it carries no credential, no brief, no requirement text, no screenshot
  bytes -- only scores and the identifiers needed to bind the record to what
  it describes.

Nothing outside this module reads it. The deployment manifest does not
include it, so it cannot reach a published site.

The scoring itself is NOT implemented here. This module defines the record,
its writer, its reader, and the fail-closed rules that decide when a shadow
attempt is even allowed to happen. The execution seam lives in
``app.core.shadow_runner``.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

#: The file the shadow record is written to, inside the workspace.
SHADOW_FILE_NAME = "shadow-scoring.json"

#: The schema version of the shadow record.
SHADOW_SCHEMA_VERSION = 1

#: The invocation states. A record moves PENDING -> one of the terminal
#: states. A PENDING record that is still present when a new run starts
#: means a previous process died between the dispatch and the completion
#: write, so the outcome of that paid call is UNKNOWN.
STATE_PENDING = "pending"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_INDETERMINATE = "indeterminate"

#: The terminal states. A record in one of these is a finished attempt.
TERMINAL_STATES = frozenset({STATE_COMPLETED, STATE_FAILED,
                              STATE_INDETERMINATE})

#: The scoring contract a shadow record is bound to. It MUST match the
#: contract the live path would use, otherwise the measurement is not
#: comparable with a future active run.
SHADOW_CONTRACT_VERSION = "n8-design-v1"

#: Fields that are never allowed in a shadow record. The record is an
#: internal diagnostic, and the brief is the user's own words -- keeping it
#: out of a file that could ever be surfaced is the point of the allow-list.
FORBIDDEN_FIELDS = frozenset({
    "brief",
    "state",
    "questions",
    "api_key",
    "credential",
    "authorization",
    "headers",
    "request",
    "payload",
    "base64_data",
    "image",
    "images",
    "requirement_texts",
    "requirements",
    "answer",
    "answers",
})


class ShadowScoringError(Exception):
    """A shadow attempt could not be recorded."""


@dataclass(frozen=True)
class ShadowScore:
    """One diagnostic record for one scoring attempt.

    Every field is either a score, a status, or an identifier. There is no
    field for user content, and there is deliberately no way to add one:
    the dataclass is frozen and the writer validates the field names.
    """

    project_id: str
    candidate_id: str
    source_revision: str
    requirement_set_id: str
    scoring_contract_version: str
    #: The measured suitability, or None when the attempt produced none.
    suitability: Optional[float] = None
    #: The six dimension scores, empty when the attempt produced none.
    dimensions: Tuple[Tuple[str, float], ...] = ()
    #: The per-requirement diagnostic statuses, empty when none were produced.
    diagnostics: Tuple[Tuple[str, str], ...] = ()
    #: The screenshot digests the measurement was bound to.
    screenshot_digests: Tuple[str, ...] = ()
    #: True when the attempt degraded (transport failure, timeout, malformed).
    degraded: bool = False
    #: A stable, sanitised status code. Never a raw exception text.
    status: str = ""
    #: The provider's own error code, when there was one.
    error_code: str = ""
    #: How long the attempt took, in milliseconds.
    latency_ms: int = 0
    #: The measured cost of the attempt, when the provider reported one.
    cost: Optional[float] = None
    #: The pinned model that was requested.
    model: str = ""
    #: The resolved model, when the provider reported one.
    resolved_model: str = ""
    #: The wall-clock time the attempt was recorded, as an epoch second.
    measured_at: float = 0.0
    #: The schema version of this record.
    schema_version: int = SHADOW_SCHEMA_VERSION
    #: The durable invocation identity. It is derived from the things that
    #: define the measurement -- the project, the revision, the requirement
    #: set and the contract -- so the same measurement always has the same
    #: identity, and a different measurement never can.
    invocation_id: str = ""
    #: The invocation state: pending, completed, failed or indeterminate.
    invocation_state: str = STATE_PENDING

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "invocation_id": self.invocation_id,
            "invocation_state": self.invocation_state,
            "project_id": self.project_id,
            "candidate_id": self.candidate_id,
            "source_revision": self.source_revision,
            "requirement_set_id": self.requirement_set_id,
            "scoring_contract_version": self.scoring_contract_version,
            "suitability": self.suitability,
            "dimensions": [list(pair) for pair in self.dimensions],
            "diagnostics": [list(pair) for pair in self.diagnostics],
            "screenshot_digests": list(self.screenshot_digests),
            "degraded": self.degraded,
            "status": self.status,
            "error_code": self.error_code,
            "latency_ms": self.latency_ms,
            "cost": self.cost,
            "model": self.model,
            "resolved_model": self.resolved_model,
            "measured_at": self.measured_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ShadowScore":
        if not isinstance(payload, Mapping):
            raise ShadowScoringError("shadow record is not an object")
        unexpected = set(payload) & FORBIDDEN_FIELDS
        if unexpected:
            raise ShadowScoringError(
                f"shadow record carries forbidden fields: {sorted(unexpected)}"
            )
        try:
            return cls(
                project_id=str(payload.get("project_id") or ""),
                candidate_id=str(payload.get("candidate_id") or ""),
                source_revision=str(payload.get("source_revision") or ""),
                requirement_set_id=str(payload.get("requirement_set_id") or ""),
                scoring_contract_version=str(
                    payload.get("scoring_contract_version") or ""
                ),
                suitability=(
                    float(payload["suitability"])
                    if payload.get("suitability") is not None else None
                ),
                dimensions=tuple(
                    (str(k), float(v)) for k, v in (payload.get("dimensions") or [])
                ),
                diagnostics=tuple(
                    (str(k), str(v)) for k, v in (payload.get("diagnostics") or [])
                ),
                screenshot_digests=tuple(
                    str(d) for d in (payload.get("screenshot_digests") or [])
                ),
                degraded=bool(payload.get("degraded")),
                status=str(payload.get("status") or ""),
                error_code=str(payload.get("error_code") or ""),
                latency_ms=int(payload.get("latency_ms") or 0),
                cost=(
                    float(payload["cost"])
                    if payload.get("cost") is not None else None
                ),
                model=str(payload.get("model") or ""),
                resolved_model=str(payload.get("resolved_model") or ""),
                measured_at=float(payload.get("measured_at") or 0.0),
                schema_version=int(
                    payload.get("schema_version") or SHADOW_SCHEMA_VERSION
                ),
                invocation_id=str(payload.get("invocation_id") or ""),
                invocation_state=str(
                    payload.get("invocation_state") or STATE_PENDING
                ),
            )
        except (TypeError, ValueError) as exc:
            raise ShadowScoringError(f"shadow record is malformed: {exc}") from exc


@dataclass(frozen=True)
class ShadowAttempt:
    """The outcome of one shadow invocation, for one candidate.

    A record is produced for EVERY attempt, including the ones that failed.
    A failure that is not recorded is indistinguishable from a call that
    never happened, and the whole point of the record is to be able to tell
    those apart later.
    """

    project_id: str
    candidate_id: str
    source_revision: str
    requirement_set_id: str
    scoring_contract_version: str = SHADOW_CONTRACT_VERSION
    suitability: Optional[float] = None
    dimensions: Tuple[Tuple[str, float], ...] = ()
    diagnostics: Tuple[Tuple[str, str], ...] = ()
    screenshot_digests: Tuple[str, ...] = ()
    degraded: bool = False
    status: str = ""
    error_code: str = ""
    latency_ms: int = 0
    cost: Optional[float] = None
    model: str = ""
    resolved_model: str = ""
    measured_at: float = 0.0
    #: A free-form operator note, e.g. why the attempt was refused.
    note: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)
    #: The durable invocation identity.
    invocation_id: str = ""
    #: The invocation state: pending, completed, failed or indeterminate.
    invocation_state: str = STATE_PENDING

    def to_record(self) -> ShadowScore:
        return ShadowScore(
            invocation_id=self.invocation_id,
            invocation_state=self.invocation_state,
            project_id=self.project_id,
            candidate_id=self.candidate_id,
            source_revision=self.source_revision,
            requirement_set_id=self.requirement_set_id,
            scoring_contract_version=self.scoring_contract_version,
            suitability=self.suitability,
            dimensions=self.dimensions,
            diagnostics=self.diagnostics,
            screenshot_digests=self.screenshot_digests,
            degraded=self.degraded,
            status=self.status,
            error_code=self.error_code,
            latency_ms=self.latency_ms,
            cost=self.cost,
            model=self.model,
            resolved_model=self.resolved_model,
            measured_at=self.measured_at,
        )

    def to_dict(self) -> dict:
        payload = self.to_record().to_dict()
        if self.note:
            payload["note"] = self.note
        return payload


#: The reservation lock file, one per invocation identity.
LOCK_PREFIX = ".shadow-lock-"


def lock_path(workspace: Any, invocation_id: str) -> Path:
    """Where the reservation lock for an invocation identity lives."""
    return Path(workspace) / f"{LOCK_PREFIX}{invocation_id}"


def _pid_alive(pid: int) -> bool:
    """Whether a process id is still running."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but belongs to another user.
        return True
    except OSError:
        return False
    return True


def _reap_stale_lock(path: Path) -> bool:
    """Remove a lock whose owner is dead. A live owner is NEVER displaced.

    A lock we cannot read is not reapable: it might belong to a process
    that is alive, and stealing it would let two invocations dispatch.
    """
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
        pid = int(data.get("pid", 0))
    except (OSError, ValueError, TypeError):
        return False
    if pid and pid != os.getpid() and not _pid_alive(pid):
        try:
            path.unlink()
            return True
        except OSError:
            return True
    return False


class ShadowLockBusy(Exception):
    """Another invocation holds the reservation for this identity."""


def acquire_reservation(
    workspace: Any,
    invocation_id: str,
    *,
    timeout: float = 2.0,
) -> Path:
    """Reserve an invocation identity, atomically across processes.

    Returns the lock path when the reservation was taken, and raises
    ``ShadowLockBusy`` when another live process holds it.

    A caller that cannot take the reservation must NOT dispatch: the
    holder may already have spent the call.
    """
    path = lock_path(workspace, invocation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                payload = json.dumps(
                    {"pid": os.getpid(), "time": time.time()}).encode("utf-8")
                os.write(fd, payload)
            finally:
                os.close(fd)
            return path
        except FileExistsError:
            if _reap_stale_lock(path):
                continue
            if time.monotonic() >= deadline:
                raise ShadowLockBusy(
                    f"another shadow invocation holds {invocation_id}"
                )
            time.sleep(0.02)


@contextmanager
def reservation(workspace: Any, invocation_id: str, *, timeout: float = 2.0):
    """Hold the reservation for an invocation identity, or refuse.

    The holder is the only process allowed to dispatch for that identity.
    A process that cannot take the reservation must NOT dispatch: the
    holder may already have spent the call.
    """
    path = acquire_reservation(workspace, invocation_id, timeout=timeout)
    try:
        yield path
    finally:
        release_reservation(path)


def release_reservation(path: Any) -> None:
    """Release a reservation. A missing lock is already released."""
    try:
        Path(path).unlink()
    except (OSError, FileNotFoundError):
        pass


def shadow_path(workspace: Any) -> Path:
    """Where the shadow record lives, inside the workspace."""
    return Path(workspace) / SHADOW_FILE_NAME


def write_shadow_record(
    workspace: Any,
    attempts: Tuple[ShadowAttempt, ...],
    *,
    project_id: str,
    source_revision: str,
    notes: Tuple[str, ...] = (),
) -> Path:
    """Write the shadow records for one invocation, atomically.

    A write failure is a failure of the RECORD, not of the build: the caller
    is told, and the build proceeds. The record is written to a temporary
    file and moved into place, so a crash never leaves a half-written
    record that would later be read as a complete one.
    """
    # The file holds EVERY invocation, merged by invocation identity. A
    # write for one revision can never erase another revision's attempts,
    # so an old revision can neither overwrite nor impersonate the current
    # one.
    existing = read_shadow_attempts(workspace)
    merged: dict = {}
    for attempt in existing:
        merged[attempt.invocation_id or attempt.candidate_id] = attempt
    for attempt in attempts:
        merged[attempt.invocation_id or attempt.candidate_id] = attempt

    records = [a.to_dict() for a in merged.values()]
    payload = {
        "schema_version": SHADOW_SCHEMA_VERSION,
        "project_id": project_id,
        "source_revision": source_revision,
        # Operator-facing notes for this invocation, e.g. that an earlier
        # attempt for the same scope used a different candidate set.
        "notes": list(notes),
        # The revisions this file holds, so a reader can see at a glance
        # that it is not a single-revision file.
        "source_revisions": sorted({a.source_revision for a in merged.values()}),
        "attempts": records,
    }
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    target = shadow_path(workspace)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(target.parent),
        prefix=".shadow-scoring-", suffix=".tmp", delete=False,
    )
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, target)
    except OSError as exc:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise ShadowScoringError(
            f"could not write the shadow record: {exc}"
        ) from exc
    return target


def read_shadow_record(workspace: Any) -> Optional[dict]:
    """Read the shadow record, or None when there is no readable one.

    A corrupt record is treated as absent rather than raising, so a damaged
    diagnostic file can never take down a build.
    """
    path = shadow_path(workspace)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def read_shadow_attempts(workspace: Any) -> Tuple[ShadowAttempt, ...]:
    """Read the attempts, skipping any that cannot be understood."""
    payload = read_shadow_record(workspace)
    if not payload:
        return ()
    out = []
    for raw in payload.get("attempts") or ():
        try:
            record = ShadowScore.from_dict(raw)
        except ShadowScoringError:
            continue
        out.append(ShadowAttempt(
            invocation_id=record.invocation_id,
            invocation_state=record.invocation_state,
            project_id=record.project_id,
            candidate_id=record.candidate_id,
            source_revision=record.source_revision,
            requirement_set_id=record.requirement_set_id,
            scoring_contract_version=record.scoring_contract_version,
            suitability=record.suitability,
            dimensions=record.dimensions,
            diagnostics=record.diagnostics,
            screenshot_digests=record.screenshot_digests,
            degraded=record.degraded,
            status=record.status,
            error_code=record.error_code,
            latency_ms=record.latency_ms,
            cost=record.cost,
            model=record.model,
            resolved_model=record.resolved_model,
            measured_at=record.measured_at,
        ))
    return tuple(out)
