"""Operator-only QA re-acceptance of an ALREADY-BUILT, previously FAILED revision.

A failed build can fail for reasons that are NOT defects in the artifact — a
detector false positive, an exhausted repair budget, or an infrastructure
hiccup during the acceptance gates. When the artifact itself is sound, the
honest recovery is to RE-RUN ACCEPTANCE on the SAME source revision: no new
FRONTEND generation, no repaired bytes, no reset budget. This module is that
recovery, and nothing more.

It is deliberately NOT a Telegram path and NOT a public HTTP endpoint. It is
reachable ONLY through the operator command line
(``python -m app --qa-reaccept <project_id> --as <principal_id>``), and only by
the project's OWNER. A user turn can never drive it.

Hard rules (each one is enforced in code, not merely documented):

* **No FRONTEND.** The evaluation half
  (:meth:`app.qa.orchestrator.QAOrchestrator.evaluate_for_reacceptance`)
  contains no ``frontend_build`` call. If any gate finds something that WOULD
  require a repair, the run FAILS CLOSED with
  ``QA_REACCEPTANCE_REPAIR_REQUIRED`` instead of invoking FRONTEND.
* **Budget untouched.** The durable FRONTEND ledger is never read, never
  reserved, never refunded. A project at ``used=2/2`` stays at ``used=2/2``.
* **Same revision.** ``source_revision`` is never bumped. The source bytes are
  pinned to the failed build's recorded ``deployment["checked"]`` identity and
  re-verified before acceptance; a changed source or artifact fails closed.
* **Fresh evidence only.** Deterministic build/typecheck, browser screenshots,
  VISION, the Impeccable critic and the (corrected) design-fidelity evaluator
  are all re-run. No stale PASS/VISION verdict is ever reused.
* **N6-E parity.** The recorded guidance snapshot the last FRONTEND invocation
  was given is re-verified; a mismatch fails closed.
* **Original evidence preserved.** The failed build's ``state.failure`` is
  never overwritten; re-acceptance evidence lives under a distinct key
  (``deployment["qa_reacceptance"]``) so the two runs are distinguishable.
* **No publication.** A passing re-acceptance creates a tested snapshot and
  moves the project to ``PREVIEW_READY``. It NEVER deploys a Preview, approves,
  publishes, or promotes.

Lifecycle: the ONLY lawful entry is ``FAILED -> QA_REACCEPTING``; the only
exits are ``PREVIEW_READY`` (pass) and ``FAILED`` (any block or crash). The
state can never reach QUEUED/RUNNING/REVISION_REQUESTED/PUBLISHING, so it can
never start a build or a publication.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.authz import AuthzError, require_owner_role
from app.core.lifecycle import ProjectLifecycle
from app.deploy.snapshot import (
    TestedSnapshot,
    digest,
    read_tree,
    source_fingerprint,
)
from app.qa.orchestrator import QAOrchestrator

logger = logging.getLogger(__name__)

#: ``state.deployment`` key under which ALL re-acceptance evidence lives. It is
#: deliberately distinct from ``critic`` / ``design_fidelity`` / ``qa`` /
#: ``checked`` so the failed build's own evidence is never overwritten.
DEPLOYMENT_KEY = "qa_reacceptance"

#: Terminal statuses for one re-acceptance operation.
STATUS_CLAIMED = "CLAIMED"
STATUS_ACCEPTED = "ACCEPTED"
STATUS_REJECTED = "REJECTED"

#: Bound on the retained re-acceptance history (operator evidence only).
MAX_HISTORY = 10


class ReacceptanceError(Exception):
    """A named precondition rejected a QA re-acceptance run.

    The code is the actionable, stable application error — never free text.
    """

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class VisionOnlyAdapter:
    """Adapter proxy that delegates VISION but makes FRONTEND unreachable.

    Re-acceptance MUST re-run VISION (fresh pixels, never a stale verdict), so a
    real adapter is needed. It must NEVER invoke FRONTEND, so every
    ``frontend_build`` access raises: a repair path that somehow reached here is
    a bug and fails loudly instead of silently consuming a budget slot.

    Everything else is delegated unchanged, so ``vision_inspect`` and any future
    read-only capability behave exactly as the real adapter.
    """

    _FORBIDDEN = frozenset({
        "frontend_build", "frontend_repair", "frontend_revision",
        "frontend_compile_repair",
    })

    def __init__(self, adapter):
        object.__setattr__(self, "_adapter", adapter)

    def __getattr__(self, name: str):
        if name in VisionOnlyAdapter._FORBIDDEN:
            raise AssertionError(
                f"FRONTEND must not be invoked during QA re-acceptance ({name})"
            )
        return getattr(object.__getattribute__(self, "_adapter"), name)


@dataclass
class ReacceptanceResult:
    """Outcome of one operator QA re-acceptance attempt."""

    success: bool
    project_id: str
    error: Optional[str] = None
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "project_id": self.project_id,
            "error": self.error,
            "evidence": dict(self.evidence),
        }


def _claim_id(project_id: str, source_revision: int) -> str:
    """Deterministic claim identity: one claim per (project, revision).

    Replay of the SAME operation therefore maps to the SAME claim, which is
    how a duplicate is detected rather than re-run.
    """
    return hashlib.sha256(
        f"qa-reaccept:{project_id}:{source_revision}".encode("utf-8")
    ).hexdigest()[:16]


def _now() -> float:
    return time.time()


def _dist_digest(workspace: Path) -> str:
    return digest(read_tree(Path(workspace) / "dist"))


def run_qa_reacceptance(
    *,
    store,
    orchestrator: QAOrchestrator,
    workspace: Path,
    project_id: str,
    principal_id: str,
) -> ReacceptanceResult:
    """Run ONE operator QA re-acceptance of an existing failed revision.

    Fails closed (raising :class:`ReacceptanceError`) on anything it cannot
    prove. The caller (the operator entrypoint) turns the raised code into a
    process exit status; nothing here prints user-facing copy.

    The caller MUST hold no writer lock on ``project_id``.
    """
    workspace = Path(workspace)

    # --- 1. Authorization: the OWNER of THIS project, stated explicitly. -----
    state = store.load(project_id)
    if state is None:
        raise ReacceptanceError("NO_PROJECT_STATE")
    try:
        require_owner_role(state, principal_id)
    except AuthzError:
        raise ReacceptanceError("UNAUTHORIZED_ROLE") from None

    # --- 2. Admissibility: durable revision, identity, artifacts. -----------
    source_revision = int(state.revisions.source_revision)

    # Idempotency FIRST: an ALREADY-accepted claim for this exact revision is a
    # no-op, regardless of the current lifecycle (which is PREVIEW_READY after a
    # successful re-acceptance). This is what makes a replay safe and
    # non-duplicating.
    existing = (state.deployment or {}).get(DEPLOYMENT_KEY)
    if isinstance(existing, dict):
        if (existing.get("status") == STATUS_ACCEPTED
                and int(existing.get("source_revision", -1)) == source_revision):
            return ReacceptanceResult(
                success=True, project_id=project_id,
                evidence={"already_accepted": True,
                          "source_revision": source_revision,
                          "claim_id": existing.get("claim_id")},
            )
        if (existing.get("status") == STATUS_CLAIMED
                and int(existing.get("source_revision", -1)) == source_revision):
            raise ReacceptanceError("QA_REACCEPTANCE_IN_PROGRESS")

    if state.lifecycle == ProjectLifecycle.QA_REACCEPTING.value:
        # A previous run is (or was) in flight. Refuse a concurrent/replayed
        # run; startup reconciliation is what clears a crashed one.
        raise ReacceptanceError("QA_REACCEPTANCE_IN_PROGRESS")
    if state.lifecycle != ProjectLifecycle.FAILED.value:
        raise ReacceptanceError("QA_REACCEPTANCE_NOT_ALLOWED_IN_LIFECYCLE")
    if source_revision < 1:
        raise ReacceptanceError("QA_REACCEPTANCE_NO_REVISION")

    checked = (state.deployment or {}).get("checked")
    if not isinstance(checked, dict) or not checked.get("source_sha256"):
        # No build/typecheck identity was ever bound -> nothing to re-accept.
        raise ReacceptanceError("QA_REACCEPTANCE_NO_IDENTITY")
    expected_source = checked.get("source_sha256")
    expected_artifact = checked.get("artifact_sha256")

    if not workspace.is_dir() or not (workspace / "dist").is_dir():
        raise ReacceptanceError("QA_REACCEPTANCE_NO_ARTIFACTS")

    # --- 3. Immutable identity gate, BEFORE any evaluation. ------------------
    current_source = source_fingerprint(workspace)
    if current_source != expected_source:
        raise ReacceptanceError("QA_REACCEPTANCE_STALE_SOURCE")
    current_artifact = _dist_digest(workspace)
    if expected_artifact is not None and current_artifact != expected_artifact:
        raise ReacceptanceError("QA_REACCEPTANCE_STALE_ARTIFACT")

    # --- 4. N6-E guidance parity against the RECORDED invocation. ------------
    from app.core.guidance_snapshot import latest_invocation, verify_invocation

    recorded = latest_invocation(state)
    mismatch = verify_invocation(workspace, recorded)
    if mismatch:
        logger.warning(
            "QA re-acceptance N6-E parity rejected for %s: %s", project_id, mismatch
        )
        raise ReacceptanceError("GUIDANCE_VERSION_MISMATCH")

    # --- 5. Claim + transition (single writer, atomic). ----------------------
    claim = _claim_id(project_id, source_revision)
    started_at = _now()
    with store.acquire_writer(project_id) as locked:
        # Re-check under the lock: a concurrent writer may have advanced state.
        if locked.lifecycle != ProjectLifecycle.FAILED.value:
            raise ReacceptanceError("QA_REACCEPTANCE_NOT_ALLOWED_IN_LIFECYCLE")
        if int(locked.revisions.source_revision) != source_revision:
            raise ReacceptanceError("QA_REACCEPTANCE_STALE_SOURCE")
        prior = (locked.deployment or {}).get(DEPLOYMENT_KEY)
        if isinstance(prior, dict) and prior.get("status") == STATUS_CLAIMED:
            raise ReacceptanceError("QA_REACCEPTANCE_IN_PROGRESS")
        history = list(prior.get("history") or []) if isinstance(prior, dict) else []
        locked.deployment[DEPLOYMENT_KEY] = {
            "status": STATUS_CLAIMED,
            "claim_id": claim,
            "source_revision": source_revision,
            "source_sha256": expected_source,
            "artifact_sha256": expected_artifact,
            "operator_principal": principal_id,
            "started_at": started_at,
            # Preservation: the original failed build's failure record is kept
            # VERBATIM here, so re-acceptance can never erase it even if a
            # later stage overwrites state.failure.
            "original_failure": locked.failure,
            "history": history,
        }
        store.transition_lifecycle_locked(locked, ProjectLifecycle.QA_REACCEPTING)
        store.save(locked)

    # --- 6. Repair-free evaluation (fresh build/screenshots/VISION/critic/
    #        fidelity). Any repair need fails closed inside the orchestrator. -
    try:
        evaluation = orchestrator.evaluate_for_reacceptance(
            project_id, workspace, dict(state.brief or {}), state.design_dna or None
        )
    except Exception:  # noqa: BLE001 - never leave the project wedged
        logger.exception("QA re-acceptance evaluation raised for %s", project_id)
        _finalize_rejection(
            store, project_id, source_revision, claim, principal_id, started_at,
            reason="QA_REACCEPTANCE_UNEXPECTED_ERROR",
            attempt=None, critic=None, fidelity=None, expected_source=expected_source,
        )
        raise ReacceptanceError("QA_REACCEPTANCE_UNEXPECTED_ERROR") from None

    if not evaluation.passed:
        _finalize_rejection(
            store, project_id, source_revision, claim, principal_id, started_at,
            reason=evaluation.reason,
            attempt=evaluation.attempt, critic=evaluation.critic,
            fidelity=evaluation.fidelity, expected_source=expected_source,
        )
        return ReacceptanceResult(
            success=False, project_id=project_id, error=evaluation.reason,
            evidence={"source_revision": source_revision, "claim_id": claim,
                      "repair_required": evaluation.repair_required},
        )

    # --- 7. Acceptance: source still pinned; bind a NEW tested snapshot. -----
    final_source = source_fingerprint(workspace)
    if final_source != expected_source:
        _finalize_rejection(
            store, project_id, source_revision, claim, principal_id, started_at,
            reason="QA_REACCEPTANCE_STALE_SOURCE",
            attempt=evaluation.attempt, critic=evaluation.critic,
            fidelity=evaluation.fidelity, expected_source=expected_source,
        )
        return ReacceptanceResult(
            success=False, project_id=project_id,
            error="QA_REACCEPTANCE_STALE_SOURCE",
            evidence={"source_revision": source_revision, "claim_id": claim},
        )

    snapshot = TestedSnapshot.capture(workspace)
    attempt_dict = evaluation.attempt.to_dict() if evaluation.attempt is not None else None
    finished_at = _now()

    with store.acquire_writer(project_id) as locked:
        if locked.lifecycle != ProjectLifecycle.QA_REACCEPTING.value:
            raise ReacceptanceError("QA_REACCEPTANCE_NOT_ALLOWED_IN_LIFECYCLE")
        prior = (locked.deployment or {}).get(DEPLOYMENT_KEY) or {}
        history = list(prior.get("history") or [])
        history.append({
            "status": STATUS_ACCEPTED,
            "claim_id": claim,
            "source_revision": source_revision,
            "finished_at": finished_at,
            "reason": evaluation.reason,
        })
        history = history[-MAX_HISTORY:]
        locked.deployment[DEPLOYMENT_KEY] = {
            "status": STATUS_ACCEPTED,
            "claim_id": claim,
            "source_revision": source_revision,
            "source_sha256": expected_source,
            "artifact_sha256": expected_artifact,
            "accepted_artifact_sha256": snapshot.artifact_sha256,
            "operator_principal": principal_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "reason": evaluation.reason,
            "attempt": attempt_dict,
            "critic": evaluation.critic,
            "fidelity": evaluation.fidelity,
            "original_failure": prior.get("original_failure", locked.failure),
            "history": history,
        }
        # A NEW tested snapshot, bound to the FINAL bytes that passed.
        locked.deployment["tested_snapshot"] = snapshot.to_dict()
        locked.revisions.qa_revision = source_revision
        locked.revisions.preview_revision = 0
        store.transition_lifecycle_locked(locked, ProjectLifecycle.PREVIEW_READY)
        store.save(locked)

    return ReacceptanceResult(
        success=True, project_id=project_id,
        evidence={
            "source_revision": source_revision,
            "claim_id": claim,
            "source_sha256": expected_source,
            "accepted_artifact_sha256": snapshot.artifact_sha256,
            "tested_snapshot_identity": snapshot.identity,
            "repair_required": False,
        },
    )


def _finalize_rejection(
    store, project_id: str, source_revision: int, claim: str, principal_id: str,
    started_at: float, *, reason: str, attempt, critic, fidelity,
    expected_source: Optional[str],
) -> None:
    """Record a REJECTED re-acceptance and fail the project closed.

    The original ``state.failure`` is NEVER touched: the rejection is recorded
    under ``deployment[DEPLOYMENT_KEY]``, and the lifecycle moves
    ``QA_REACCEPTING -> FAILED``. No tested snapshot is written.
    """
    attempt_dict = attempt.to_dict() if attempt is not None else None
    finished_at = _now()
    try:
        with store.acquire_writer(project_id) as locked:
            if locked.lifecycle != ProjectLifecycle.QA_REACCEPTING.value:
                # Something else already moved it; do not fight over state.
                return
            prior = (locked.deployment or {}).get(DEPLOYMENT_KEY) or {}
            history = list(prior.get("history") or [])
            history.append({
                "status": STATUS_REJECTED,
                "claim_id": claim,
                "source_revision": source_revision,
                "finished_at": finished_at,
                "reason": reason,
            })
            history = history[-MAX_HISTORY:]
            locked.deployment[DEPLOYMENT_KEY] = {
                "status": STATUS_REJECTED,
                "claim_id": claim,
                "source_revision": source_revision,
                "source_sha256": expected_source,
                "operator_principal": principal_id,
                "started_at": started_at,
                "finished_at": finished_at,
                "reason": reason,
                "attempt": attempt_dict,
                "critic": critic,
                "fidelity": fidelity,
                "original_failure": prior.get("original_failure", locked.failure),
                "history": history,
            }
            store.transition_lifecycle_locked(locked, ProjectLifecycle.FAILED)
            store.save(locked)
    except Exception:  # noqa: BLE001 - recording must never mask the true cause
        logger.exception("Could not persist QA re-acceptance rejection for %s", project_id)


__all__ = [
    "DEPLOYMENT_KEY",
    "STATUS_CLAIMED",
    "STATUS_ACCEPTED",
    "STATUS_REJECTED",
    "MAX_HISTORY",
    "ReacceptanceError",
    "ReacceptanceResult",
    "VisionOnlyAdapter",
    "run_qa_reacceptance",
]
