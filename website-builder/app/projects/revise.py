"""Phase 10 revision orchestrator for Website Builder R1.

Applies strictly-ordered natural-language revision requests against a
project's persisted Design DNA. FRONTEND performs the actual compatible
edit; this module owns ordering, invalidation, worker-slot ownership, and
hand-off into the existing Phase 8 QA and Phase 9 preview pipelines.

Ordering model (two-phase, fail-closed):

  1. ``reserve(project_id, seq)`` -- submission-time ordering authority.
     ``seq`` must be exactly ``queued_revision_seq + 1``. A duplicate or
     out-of-order submission is rejected here, before any workspace,
     Design DNA, or lifecycle mutation happens. Reservation is what moves
     the project into REVISION_REQUESTED, which blocks a second concurrent
     reservation until this one is applied (or explicitly abandoned).
  2. ``apply(project_id, seq, request_text)`` -- performs the actual
     revision under the single MAX_WORKERS=1 worker slot shared with
     Phase 7/8/9. Requires an exact, still-current reservation for the
     same ``seq``. Never re-applies an already-applied or stale seq.

QA/preview/approval invalidation happens BEFORE invoking FRONTEND -- even
a failed or partially-completed mutation may have changed source on disk,
so any prior tested/shown state must never be trusted afterward.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.authz import AuthzError, require_mutating_role
from app.core.contracts import OperationResult
from app.core.design_dna import typography_violation_message, validate_typography
from app.core.composition import compose_project_instructions, validate_composed_dna
from app.core.lifecycle import LifecycleError, ProjectLifecycle
from app.core.state import ProjectStateStore
from app.deploy.snapshot import record_checks, source_fingerprint
from app.projects.build import _CHEAP_CHECK_SEQUENCE, run_fixed_checks
from app.qa.orchestrator import QAOrchestrator
from app.sandbox.runner import ProjectRunner

logger = logging.getLogger(__name__)


@dataclass
class RevisionResult:
    """Result of applying (or failing to apply) one ordered revision."""

    success: bool
    project_id: str
    seq: int
    error: Optional[str] = None
    error_code: Optional[str] = None
    # True when this call did NOT re-run the revision but reconciled a
    # crash-window reservation whose preview had already been delivered.
    reconciled: bool = False


class RevisionOrchestrator:
    """Application-owned Phase 10 orchestration.

    Shares the single MAX_WORKERS=1 worker slot with Phase 7/8/9 via the
    same ``ProjectRunner``. Revisions are strictly ordered by a caller-
    assigned monotonic ``seq`` per project.
    """

    def __init__(
        self,
        runner: ProjectRunner,
        store: ProjectStateStore,
        hermes_adapter=None,
        preview_orchestrator=None,
        web3forms_access_key: Optional[str] = None,
    ):
        self.runner = runner
        self.store = store
        self.hermes_adapter = hermes_adapter
        self.preview_orchestrator = preview_orchestrator
        self.web3forms_access_key = web3forms_access_key

    # ------------------------------------------------------------------
    # Ordering reservation
    # ------------------------------------------------------------------

    def reserve(
        self,
        project_id: str,
        seq: int,
        principal_id: Optional[str] = None,
        reference_token: Optional[str] = None,
    ) -> OperationResult:
        """Reserve the next ordered revision slot.

        Fails closed on any gap, duplicate, or out-of-order submission,
        and on any lifecycle state that cannot accept a revision. Does not
        touch the workspace, Design DNA, or the worker slot.

        Requires an owner or authorized reviewer principal/reference token.
        Unauthorized attempts are rejected before any state mutation.
        """
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            return OperationResult.fail("INVALID_REVISION_SEQ", error_code="INVALID_REVISION_SEQ")
        with self.store.acquire_writer(project_id) as state:
            try:
                require_mutating_role(
                    state, principal_id=principal_id, reference_token=reference_token
                )
            except AuthzError as exc:
                return OperationResult.fail(exc.error_code, error_code=exc.error_code)
            expected = state.revisions.queued_revision_seq + 1
            # F6 re-drive: a crash between reserve() and apply() leaves a
            # pending reservation for the CURRENT queued seq with
            # applied=False and the lifecycle parked in REVISION_REQUESTED.
            # Re-submitting that EXACT reservation is a safe idempotent
            # re-drive (apply() still guards revision_seq >= seq, so it can
            # never apply twice). Checked BEFORE the out-of-order gate because
            # the current queued seq is intentionally not queued+1 here.
            # A reservation owned by a DIFFERENT principal is a genuine
            # conflict -- never adopt another principal's slot.
            if (seq == state.revisions.queued_revision_seq
                    and state.lifecycle == ProjectLifecycle.REVISION_REQUESTED.value):
                pending = next(
                    (e for e in state.pending_revisions
                     if isinstance(e, dict) and e.get("seq") == seq
                     and e.get("applied") is False),
                    None,
                )
                if pending is not None:
                    if pending.get("principal_id") == principal_id:
                        # Adopt the existing un-applied reservation unchanged;
                        # do NOT bump the sequence or re-append.
                        return OperationResult.ok({"seq": seq, "redriven": True})
                    return OperationResult.fail(
                        "OUT_OF_ORDER_REVISION", error_code="OUT_OF_ORDER_REVISION",
                    )
            if seq != expected:
                return OperationResult.fail(
                    "OUT_OF_ORDER_REVISION", error_code="OUT_OF_ORDER_REVISION",
                )
            if state.lifecycle not in (
                ProjectLifecycle.PREVIEW_READY.value,
                ProjectLifecycle.LIVE.value,
            ):
                return OperationResult.fail(
                    "REVISION_NOT_ALLOWED_IN_LIFECYCLE",
                    error_code="REVISION_NOT_ALLOWED_IN_LIFECYCLE",
                )
            state.revisions.queued_revision_seq = seq
            self.store.transition_lifecycle_locked(state, ProjectLifecycle.REVISION_REQUESTED)
            state.pending_revisions.append({
                "seq": seq,
                "principal_id": principal_id,
                "reserved_at": time.time(),
                "applied": False,
            })
            self.store.save(state)
        return OperationResult.ok({"seq": seq})

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------

    def _is_delivered_unapplied_reservation(self, state, seq: int) -> bool:
        """True when ``seq`` is a crash-window revision whose preview was
        already durably delivered but never finalized.

        Evidence required (all of it, fail-closed otherwise):
          * the reservation for ``seq`` exists and is unapplied;
          * ``revision_seq`` has not yet reached ``seq``;
          * the CURRENT source revision's preview was durably shown
            (``latest_shown_preview.source_revision`` matches the live
            ``source_revision`` and ``preview_revision``);
          * that preview was shown AFTER the reservation was created
            (``shown_at > reserved_at``) — this distinguishes the revision's
            OWN delivered preview from the pre-revision preview that was
            already on the project when the reservation was made.
        """
        if state is None:
            return False
        rev = state.revisions
        if rev.queued_revision_seq != seq or rev.revision_seq >= seq:
            return False
        reservation = next(
            (e for e in (state.pending_revisions or [])
             if isinstance(e, dict) and e.get("seq") == seq
             and e.get("applied") is False),
            None,
        )
        if reservation is None:
            return False
        shown = state.deployment.get("latest_shown_preview") or {}
        if not (
            bool(shown.get("operation_id"))
            and shown.get("source_revision") == rev.source_revision
            and rev.preview_revision == rev.source_revision
        ):
            return False
        reserved_at = reservation.get("reserved_at")
        shown_at = shown.get("shown_at")
        if not isinstance(reserved_at, (int, float)) or not isinstance(shown_at, (int, float)):
            # Missing timestamps -> cannot prove ordering -> fail closed.
            return False
        return shown_at > reserved_at

    def apply(
        self,
        project_id: str,
        seq: int,
        request_text: str,
        workspace: Optional[Path] = None,
        principal_id: Optional[str] = None,
        reference_token: Optional[str] = None,
    ) -> RevisionResult:
        """Apply a previously-reserved ordered revision.

        Must be called with the SAME ``seq`` returned by a successful
        ``reserve()``. Fails closed if the reservation does not match
        exactly current state (stale/duplicate re-application, or no
        matching reservation at all).

        Requires an owner or authorized reviewer principal/reference token.
        Unauthorized attempts are rejected before any workspace, Design DNA,
        or lifecycle mutation.
        """
        state = self.store.load(project_id)
        if state is None:
            return RevisionResult(False, project_id, seq, error="NO_PROJECT_STATE",
                                  error_code="NO_PROJECT_STATE")
        try:
            require_mutating_role(
                state, principal_id=principal_id, reference_token=reference_token
            )
        except AuthzError as exc:
            return RevisionResult(False, project_id, seq, error=exc.error_code,
                                  error_code=exc.error_code)
        if state.revisions.queued_revision_seq != seq:
            return RevisionResult(False, project_id, seq, error="REVISION_NOT_RESERVED",
                                  error_code="REVISION_NOT_RESERVED")
        if state.revisions.revision_seq >= seq:
            # Already applied, or a stale/duplicate replay -- never re-apply.
            return RevisionResult(False, project_id, seq, error="REVISION_ALREADY_APPLIED",
                                  error_code="REVISION_ALREADY_APPLIED")

        # ---- BUG 7 crash-window reconciliation -------------------------
        # A crash AFTER a durable preview delivery but BEFORE the final
        # revision application write leaves: the reservation unapplied,
        # ``revision_seq`` still < seq, and a preview already shown for the
        # CURRENT source revision (QA already reached PREVIEW_READY). In that
        # case the revision's remote effect (the preview) already happened and
        # must NOT be repeated. Finalize seq N exactly once against the
        # already-delivered preview: advance ``revision_seq`` and mark the
        # reservation applied. No frontend/QA/preview re-run, no re-send.
        if self._is_delivered_unapplied_reservation(state, seq):
            with self.store.acquire_writer(project_id) as locked:
                try:
                    require_mutating_role(locked, principal_id, reference_token)
                except AuthzError as exc:
                    return RevisionResult(False, project_id, seq, error=exc.error_code,
                                          error_code=exc.error_code)
                if not self._is_delivered_unapplied_reservation(locked, seq):
                    return RevisionResult(False, project_id, seq,
                                          error="REVISION_NOT_RESERVED",
                                          error_code="REVISION_NOT_RESERVED")
                locked.revisions.revision_seq = seq
                for entry in locked.pending_revisions:
                    if entry.get("seq") == seq:
                        entry["applied"] = True
                        entry["applied_at"] = time.time()
                self.store.save(locked)
            return RevisionResult(True, project_id, seq, reconciled=True)

        if state.lifecycle != ProjectLifecycle.REVISION_REQUESTED.value:
            return RevisionResult(False, project_id, seq, error="REVISION_NOT_RESERVED",
                                  error_code="REVISION_NOT_RESERVED")

        if not self.runner.acquire_project(project_id):
            return RevisionResult(
                False, project_id, seq,
                error="Another project is currently being built (MAX_WORKERS=1)",
                error_code="WORKER_BUSY",
            )

        try:
            # QUEUED -> RUNNING, mirroring Phase 7. Invalidate QA/preview/
            # approval BEFORE invoking FRONTEND -- even a failed or partial
            # mutation may have changed source on disk.
            with self.store.acquire_writer(project_id) as locked:
                try:
                    require_mutating_role(locked, principal_id, reference_token)
                except AuthzError as exc:
                    return RevisionResult(False, project_id, seq, error=exc.error_code,
                                          error_code=exc.error_code)
                if (locked.revisions.queued_revision_seq != seq
                        or locked.revisions.revision_seq >= seq
                        or locked.lifecycle != ProjectLifecycle.REVISION_REQUESTED.value
                        or not any(isinstance(entry, dict) and entry.get("seq") == seq
                                   and entry.get("principal_id") == principal_id
                                   and entry.get("applied") is False
                                   for entry in locked.pending_revisions)):
                    return RevisionResult(False, project_id, seq, error="REVISION_NOT_RESERVED",
                                          error_code="REVISION_NOT_RESERVED")
                state = locked
                try:
                    instructions = compose_project_instructions(
                        state, access_key=self.web3forms_access_key,
                        task=self._build_revision_instructions(state.design_dna, request_text),
                    )
                except (ValueError, TypeError, KeyError) as exc:
                    return RevisionResult(False, project_id, seq, error=str(exc),
                                          error_code="INVALID_PERSISTED_REFERENCES")
                brief = dict(state.brief)
                workspace = workspace or self.runner.create_workspace(project_id)
                self.store.transition_lifecycle_locked(locked, ProjectLifecycle.QUEUED)
                self.store.transition_lifecycle_locked(locked, ProjectLifecycle.RUNNING)
                locked.revisions.source_revision += 1
                locked.revisions.qa_revision = 0
                locked.revisions.preview_revision = 0
                locked.revisions.approved_revision = 0
                locked.deployment.pop("qa", None)
                locked.deployment.pop("approval", None)
                locked.deployment.pop("checked", None)
                locked.deployment.pop("tested_snapshot", None)
                locked.deployment.pop("latest_shown_preview", None)
                locked.deployment.pop("preview_intent", None)
                self.store.save(locked)

                # Authorization and first external mutation share the writer.
                # A revoke that wins this lock prevents all workspace effects.
                #
                # H-2: a collaborator that RAISES (instead of returning a
                # falsy result) must take the SAME durable failure path as a
                # returned failure. Before this fix an exception here escaped
                # apply() while the project was already RUNNING with a bumped
                # source_revision and its QA/preview evidence cleared -- the
                # normal falsy-result failure handling was skipped and the
                # project was stranded RUNNING with no recovery path.
                # Classify the exception into a safe, bounded failure, record
                # it durably (same shape as the returned-failure path), and
                # fall through to the existing _fail() terminal transition.
                # The exception detail is preserved (never double-static) but
                # captured here so the writer lock is released before the
                # terminal failure state is committed.
                try:
                    frontend_result = (
                        self.hermes_adapter.frontend_build(
                            project_id=project_id, brief=brief, workspace=workspace,
                            design_dna_instructions=instructions,
                        ) if self.hermes_adapter is not None else
                        {"success": False, "error": "Hermes adapter not configured"}
                    )
                except Exception as exc:
                    logger.exception(
                        "frontend_build raised during revision apply for project %s seq %s",
                        project_id, seq,
                    )
                    frontend_result = {
                        "success": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "error_code": "FRONTEND_REVISION_EXCEPTION",
                    }
            if not frontend_result.get("success"):
                return self._fail(
                    project_id, seq,
                    frontend_result.get("error", "FRONTEND revision failed"),
                    frontend_result.get("error_code") or "FRONTEND_REVISION_FAILED",
                )

            try:
                validate_composed_dna(frontend_result.get("design_dna"), state)
            except ValueError as exc:
                return self._fail(project_id, seq, str(exc), "REFERENCE_SYNTHESIS_INVALID")
            new_design_dna = frontend_result.get("design_dna") or state.design_dna

            # Enforce the fixed Design DNA typography constraint (max 2
            # font families) on the persisted document BEFORE it is ever
            # trusted for a subsequent QA/preview cycle.
            if not validate_typography(new_design_dna):
                return self._fail(
                    project_id, seq,
                    typography_violation_message(new_design_dna),
                    "DESIGN_DNA_TYPOGRAPHY_VIOLATION",
                )

            with self.store.acquire_writer(project_id) as locked:
                locked.design_dna = new_design_dna
                if isinstance(new_design_dna, dict):
                    locked.revisions.design_dna_version = new_design_dna.get(
                        "version", locked.revisions.design_dna_version + 1
                    )
                self.store.save(locked)

            # Phase 7 parity: a revision clears the previous ``checked``
            # binding (see the invalidation block above), so it must re-run the
            # SAME fixed cheap checks and re-record the binding BEFORE QA. QA's
            # preview hand-off requires ``deployment['checked']`` to capture the
            # tested snapshot; without this, QA would reach PREVIEW_READY with
            # no tested_snapshot and Phase 9 preview would fail closed with
            # QA_REQUIRED (the revision could never preview).
            before = source_fingerprint(workspace)
            checks = run_fixed_checks(self.runner, project_id, workspace)
            if not all(r.get("success", False) for r in checks.values()):
                failed = next(
                    (name for name in _CHEAP_CHECK_SEQUENCE
                     if not checks.get(name, {}).get("success", False)),
                    "npm_build",
                )
                return self._fail(
                    project_id, seq,
                    f"Revision cheap checks failed: {failed}",
                    f"CHEAP_CHECKS_FAILED:{failed}",
                )
            record_checks(self.store, project_id, workspace, before)

            qa_orchestrator = QAOrchestrator(
                self.runner, self.store, hermes_adapter=self.hermes_adapter,
                web3forms_access_key=self.web3forms_access_key,
            )
            qa_result = qa_orchestrator.run(
                project_id=project_id,
                workspace=workspace,
                brief=brief,
                design_dna=new_design_dna,
            )

            if not qa_result.success:
                # QAOrchestrator already transitions to FAILED and records
                # its own failure detail; just record the revision-level
                # failure without re-transitioning.
                return self._fail(
                    project_id, seq, qa_result.error or "QA failed after revision",
                    "QA_FAILED_AFTER_REVISION",
                )

            if self.preview_orchestrator is not None:
                preview = self.preview_orchestrator.run_owned(
                    project_id, workspace, slot_held=True
                )
                if not preview.success:
                    return self._fail(
                        project_id, seq, preview.error or preview.error_code,
                        preview.error_code or "PREVIEW_FAILED_AFTER_REVISION",
                    )

            # Success -- mark this ordered revision applied. This is the
            # ONLY place revision_seq advances; it must exactly match the
            # reserved seq to keep the applied stream gap-free.
            with self.store.acquire_writer(project_id) as locked:
                if locked.revisions.queued_revision_seq != seq:
                    return RevisionResult(False, project_id, seq,
                                          error="STALE_REVISION_RESERVATION",
                                          error_code="STALE_REVISION_RESERVATION")
                locked.revisions.revision_seq = seq
                for entry in locked.pending_revisions:
                    if entry.get("seq") == seq:
                        entry["applied"] = True
                        entry["applied_at"] = time.time()
                self.store.save(locked)

            return RevisionResult(True, project_id, seq)
        finally:
            self.runner.release_project(project_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _fail(self, project_id: str, seq: int, error: str, error_code: str) -> RevisionResult:
        with self.store.acquire_writer(project_id) as state:
            if state.lifecycle != ProjectLifecycle.FAILED.value:
                try:
                    self.store.transition_lifecycle_locked(state, ProjectLifecycle.FAILED)
                except LifecycleError:
                    pass
            state.failure = {
                "phase": "revision",
                "seq": seq,
                "error": error,
                "failed_at": time.time(),
            }
            self.store.save(state)
        return RevisionResult(False, project_id, seq, error=error, error_code=error_code)

    def _build_revision_instructions(
        self, design_dna: Optional[Dict[str, Any]], request_text: str
    ) -> str:
        dna_note = json.dumps(design_dna, indent=2) if design_dna else "(none)"
        return f"""REVISION REQUEST (Phase 10 ordered natural-language revision).

Existing Design DNA (preserve compatible earlier intent; do not redesign
unrelated areas; make the smallest change satisfying the request):
{dna_note}

User requested change:
{request_text}

Instructions:
- Apply only the requested change.
- Preserve typography: at most 2 font families total in Design DNA.
- Do NOT invent business facts.
- Update design-dna.json to reflect only what actually changed.
"""
