"""Phase 10 revision orchestrator for Website Builder R1/R2.

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
     Reservation also FREEZES the revision base -- the exact source identity
     this revision will hydrate from -- after admission is proven and before
     the first mutation, so a refusal leaves durable state untouched.
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
from app.deploy.hydrate import (
    BASE_KIND_DRAFT,
    BASE_KIND_LIVE,
    DRAFT_SNAPSHOT_UNAVAILABLE,
    HYDRATION_STAGING_UNAVAILABLE,
    HYDRATION_STATE_UNPERSISTED,
    HydrationError,
    RevisionBase,
    build_draft_base,
    build_live_base,
)
from app.deploy.git_output import repository_name_for_url
from app.deploy.snapshot import TestedSnapshot, record_checks, source_fingerprint
from app.core.selfcontained import EXTERNAL_RUNTIME_DEPENDENCY
from app.projects.build import (
    _CHEAP_CHECK_SEQUENCE,
    normalize_and_check_self_contained,
    run_fixed_checks,
)
from app.projects.release import (
    CanonicalSourceRefusal,
    canonical_source_repo_verdict,
    canonical_source_verdict,
)
from app.qa.orchestrator import QAOrchestrator
from app.sandbox.runner import PointerResolutionError, ProjectRunner

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
    diagnostics: Optional[Dict[str, Any]] = None
    data: Optional[Dict[str, Any]] = None
    # Which frozen base this revision hydrated from, and the identity it was
    # acquired under. Both are None when the ``workspace`` seam was used.
    base_kind: Optional[str] = None
    hydrated_from: Optional[Dict[str, Any]] = None


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
        hydrator=None,
        source_repo_url: Optional[str] = None,
    ):
        self.runner = runner
        self.store = store
        self.hermes_adapter = hermes_adapter
        self.preview_orchestrator = preview_orchestrator
        self.web3forms_access_key = web3forms_access_key
        # The exact-source authority. Injected by ``compose()`` because a LIVE
        # revision additionally needs the output repository and the
        # operator-configured remote, which only the composition root holds.
        self.hydrator = hydrator
        # Operator-configured source remote (SSH form). Used only to prove the
        # recorded repository identity still names the configured one, offline.
        self.source_repo_url = source_repo_url

    # ------------------------------------------------------------------
    # Revision base (frozen at reservation time)
    # ------------------------------------------------------------------

    def _configured_repo_name(self) -> Optional[str]:
        return repository_name_for_url(self.source_repo_url)

    def _freeze_base(self, state, seq: int) -> RevisionBase:
        """Compute the revision base, or refuse before any mutation.

        LIVE takes its identity ONLY from ``last_live_release`` and DRAFT only
        from ``tested_snapshot``. There is deliberately no third source and no
        "best effort" combination of the two: a base assembled from whatever
        happens to be on disk is exactly the mutable-workspace reconstruction
        this design removes.
        """
        if state.lifecycle == ProjectLifecycle.LIVE.value:
            try:
                verdict = canonical_source_verdict(state)
                if not verdict.ready:
                    raise CanonicalSourceRefusal(
                        verdict.error_code or "CANONICAL_SOURCE_COMMIT_MISSING")
                repo_verdict = canonical_source_repo_verdict(
                    state, self._configured_repo_name())
                if not repo_verdict.ready:
                    raise CanonicalSourceRefusal(repo_verdict.error_code)
            except CanonicalSourceRefusal as exc:
                # Re-raised as a hydration refusal so every admission code
                # travels the same classified path; nothing has been mutated.
                raise HydrationError(exc.error_code, str(exc)) from None
            return build_live_base(
                state.deployment["last_live_release"],
                seq=seq,
                reserved_at=time.time(),
                requirements_version=state.revisions.requirements_version,
                design_dna_version=state.revisions.design_dna_version,
            )

        # DRAFT: the last tested snapshot IS the source. It must exist, parse,
        # and agree with the ``checked`` binding -- otherwise the bytes a
        # revision would continue from are unknown, and "unknown" is not a
        # starting point.
        snapshot_payload = (state.deployment or {}).get("tested_snapshot")
        if not isinstance(snapshot_payload, dict):
            raise HydrationError(
                DRAFT_SNAPSHOT_UNAVAILABLE, "no tested snapshot is recorded")
        try:
            snapshot = TestedSnapshot.from_dict(snapshot_payload)
        except Exception as exc:
            raise HydrationError(
                DRAFT_SNAPSHOT_UNAVAILABLE, type(exc).__name__) from None
        checked = (state.deployment or {}).get("checked") or {}
        if (checked.get("source_sha256") != snapshot.source_sha256
                or checked.get("artifact_sha256") != snapshot.artifact_sha256):
            raise HydrationError(
                DRAFT_SNAPSHOT_UNAVAILABLE,
                "the tested snapshot does not match the checked binding")
        return build_draft_base(
            snapshot,
            seq=seq,
            reserved_at=time.time(),
            requirements_version=state.revisions.requirements_version,
            design_dna_version=state.revisions.design_dna_version,
            source_revision=state.revisions.source_revision,
        )

    def reserved_base(self, project_id: str, seq: int) -> Optional[RevisionBase]:
        """The frozen base of the still-unapplied reservation for *seq*.

        Read from DURABLE state, never recomputed: a revision hydrates from
        what it was admitted against, not from what the project looks like now.
        """
        state = self.store.load(project_id)
        if state is None:
            return None
        entry = next(
            (e for e in (state.pending_revisions or [])
             if isinstance(e, dict) and e.get("seq") == seq
             and e.get("applied") is False and isinstance(e.get("base"), dict)),
            None,
        )
        if entry is None:
            return None
        try:
            return RevisionBase.from_dict(entry["base"])
        except HydrationError:
            return None

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
            # Admission, then freeze -- both strictly BEFORE the first
            # mutation below. A refusal returns here with no sequence bump, no
            # lifecycle change, no reservation append and no cache write, so
            # durable state is byte-equivalent either way.
            try:
                base = self._freeze_base(state, seq)
            except HydrationError as exc:
                return OperationResult.fail(exc.error_code, error_code=exc.error_code)
            state.revisions.queued_revision_seq = seq
            self.store.transition_lifecycle_locked(state, ProjectLifecycle.REVISION_REQUESTED)
            state.pending_revisions.append({
                "seq": seq,
                "principal_id": principal_id,
                "reserved_at": time.time(),
                "applied": False,
                "base": base.to_dict(),
            })
            # Bound the ledger atomically with this append. Pruning always keeps
            # unapplied reservations, so the one just added (and any other
            # awaiting the re-drive path) survives.
            state.prune_bounded_ledgers()
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
        on_remote_boundary=None,
    ) -> RevisionResult:
        """Apply a previously-reserved ordered revision.

        Must be called with the SAME ``seq`` returned by a successful
        ``reserve()``. Fails closed if the reservation does not match
        exactly current state (stale/duplicate re-application, or no
        matching reservation at all).

        Requires an owner or authorized reviewer principal/reference token.
        Unauthorized attempts are rejected before any workspace, Design DNA,
        or lifecycle mutation.

        ``workspace`` is a COMPATIBILITY / TEST SEAM and nothing else. It is
        never supplied by production: the sole production caller is the
        Telegram dispatcher, which passes only ``project_id``, ``seq``,
        ``request_text`` and ``principal_id``. When it is ``None`` -- which is
        every production call -- hydration from the reservation's frozen base
        is MANDATORY and there is no fallback.

        There is deliberately no ``workspace or create_workspace(...)``
        fallback any more. That expression *was* "silently continue from
        whatever mutable workspace happened to be left on disk", which is the
        single behaviour this exact-source design exists to remove. There is
        also no public flag that would let a runtime caller choose an arbitrary
        directory: the only way to inject a workspace is this parameter, and
        only tests use it.
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
            # ---- exact-source hydration -------------------------------
            # Runs BEFORE the writer block on purpose. Everything that makes a
            # revision observable -- ``source_revision``, the QA/preview/
            # approval invalidation, FRONTEND, the build, QA, the preview and
            # any provider call -- happens strictly after this, so a refusal
            # here leaves the project exactly as it was.
            base_kind = None
            hydrated_from = None
            if workspace is None:
                frozen = self.reserved_base(project_id, seq)
                if frozen is None:
                    return RevisionResult(
                        False, project_id, seq,
                        error="REVISION_NOT_RESERVED",
                        error_code="REVISION_NOT_RESERVED")
                if self.hydrator is None:
                    # No exact-source authority is wired into this runtime, so
                    # no revision can be proven to start from the right bytes.
                    # Fail closed: there is deliberately no fallback to a
                    # leftover workspace.
                    return self._fail(project_id, seq,
                                      "no hydrator is configured",
                                      HYDRATION_STAGING_UNAVAILABLE,
                                      base_kind=frozen.base_kind)
                try:
                    outcome = self.hydrator.hydrate(project_id, seq, frozen)
                except PointerResolutionError as exc:
                    return self._fail(project_id, seq, exc.error_code, exc.error_code,
                                      base_kind=frozen.base_kind)
                except HydrationError as exc:
                    if exc.error_code == HYDRATION_STATE_UNPERSISTED:
                        # NOT a hydration failure. The pointer swap committed
                        # and the promoted workspace is intact; only the durable
                        # READY / pointer_mode write was lost. The lifecycle
                        # deliberately stays at REVISION_REQUESTED so the same
                        # reservation can be re-driven, which resumes into the
                        # already-swapped case: persist the write, clean up, and
                        # continue. Failing the revision here would strand a
                        # workspace the project is already using.
                        return RevisionResult(
                            False, project_id, seq,
                            error=exc.error_code, error_code=exc.error_code,
                            base_kind=frozen.base_kind,
                        )
                    return self._fail(project_id, seq, exc.error_code,
                                      exc.error_code, base_kind=frozen.base_kind)
                workspace = outcome.workspace
                base_kind = outcome.base_kind
                hydrated_from = outcome.hydrated_from

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
                # Operation id for the supervised FRONTEND invocation below, so
                # this revision's watchdog is isolated from any other build's.
                revision_operation_id = str(locked.revisions.source_revision)
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
                            build_operation_id=revision_operation_id,
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
            # BUILD-TIME self-contained gate (Phase 7 parity): normalize
            # supported external dependencies into local assets and reject any
            # remaining unsupported external runtime dependency BEFORE the
            # ``checked`` binding is recorded. It reuses the existing revision
            # failure handling -- no second, asset-specific repair system.
            self_contained = normalize_and_check_self_contained(project_id, workspace)
            if not self_contained.ok:
                return self._fail(
                    project_id, seq,
                    self_contained.error_text() or EXTERNAL_RUNTIME_DEPENDENCY,
                    EXTERNAL_RUNTIME_DEPENDENCY,
                )
            before = source_fingerprint(workspace)
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
                try:
                    try:
                        import inspect
                        preview_params = inspect.signature(
                            self.preview_orchestrator.run_owned
                        ).parameters
                        accepts_boundary = (
                            'on_remote_boundary' in preview_params
                            or any(p.kind == inspect.Parameter.VAR_KEYWORD
                                   for p in preview_params.values())
                        )
                    except (TypeError, ValueError):
                        accepts_boundary = False
                    if accepts_boundary:
                        preview = self.preview_orchestrator.run_owned(
                            project_id, workspace, slot_held=True,
                            on_remote_boundary=on_remote_boundary,
                        )
                    else:
                        preview = self.preview_orchestrator.run_owned(
                            project_id, workspace, slot_held=True,
                        )
                except Exception as exc:
                    refreshed = self.store.load(project_id)
                    if self._is_delivered_unapplied_reservation(refreshed, seq):
                        with self.store.acquire_writer(project_id) as locked:
                            if not self._is_delivered_unapplied_reservation(locked, seq):
                                return RevisionResult(
                                    False, project_id, seq,
                                    error='PREVIEW_RECONCILIATION_REQUIRED',
                                    error_code='PREVIEW_RECONCILIATION_REQUIRED',
                                )
                            locked.revisions.revision_seq = seq
                            for entry in locked.pending_revisions:
                                if entry.get('seq') == seq:
                                    entry['applied'] = True
                                    entry['applied_at'] = time.time()
                            self.store.save(locked)
                        return RevisionResult(True, project_id, seq, reconciled=True)
                    return self._fail(
                        project_id, seq, type(exc).__name__,
                        'PREVIEW_RECONCILIATION_REQUIRED',
                    )
                if not preview.success:
                    preview_error = preview.error or preview.error_code or "PREVIEW_FAILED_AFTER_REVISION"
                    preview_code = preview.error_code or "PREVIEW_FAILED_AFTER_REVISION"
                    if preview_code in {"SMOKE_FAILED", "VERCEL_BYPASS_AUTH_FAILED"}:
                        with self.store.acquire_writer(project_id) as locked:
                            locked.revisions.revision_seq = seq
                            for entry in locked.pending_revisions:
                                if entry.get("seq") == seq:
                                    entry["applied"] = True
                                    entry["applied_at"] = time.time()
                                    entry["preview_failed"] = True
                                    entry["error_code"] = preview_code
                            locked.failure = {
                                "phase": "preview",
                                "seq": seq,
                                "error": preview_error,
                                "error_code": preview_code,
                                "failed_at": time.time(),
                            }
                            self.store.save(locked)
                        return RevisionResult(
                            False, project_id, seq, error=preview_error,
                            error_code=preview_code,
                            diagnostics=dict(getattr(preview, 'data', {}) or {}),
                            data=dict(getattr(preview, 'data', {}) or {}),
                        )
                    return self._fail(
                        project_id, seq, preview_error, preview_code,
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

            return RevisionResult(True, project_id, seq, base_kind=base_kind,
                                  hydrated_from=hydrated_from)
        finally:
            self.runner.release_project(project_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _fail(self, project_id: str, seq: int, error: str, error_code: str,
              *, base_kind: Optional[str] = None,
              hydrated_from: Optional[Dict[str, Any]] = None) -> RevisionResult:
        """Record one terminal revision failure and return it.

        The persisted ``error_code`` and the returned ``error_code`` are the
        same value. A caller that only inspects the return value and an
        operator reading the durable record must never be able to disagree
        about why a revision failed; before this, the durable record carried
        only free text, so a classification that existed in memory was gone by
        the time anyone looked at the project.
        """
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
                "error_code": error_code,
                "failed_at": time.time(),
            }
            self.store.save(state)
        return RevisionResult(False, project_id, seq, error=error,
                              error_code=error_code, base_kind=base_kind,
                              hydrated_from=hydrated_from)

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
