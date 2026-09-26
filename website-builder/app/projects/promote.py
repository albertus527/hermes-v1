"""Phase 11 promotion orchestrator for Website Builder R1.

Promotes an already-approved, already-tested preview deployment to the
production alias. This module owns:

  * binding an approval to the EXACT preview deployment identity that was
    shown to the user (operation_id + deployment_id + source/artifact
    hashes) -- not merely a revision counter,
  * verifying, at promotion time, that the approval identity still matches
    the project's CURRENT latest shown preview -- if a newer preview was
    produced after approval (revision, revise, or a fresh preview run),
    the approval is stale and promotion is fail-closed / blocked,
  * reusing the Phase 9 owned-Vercel-project adapters to alias/promote the
    EXACT existing preview deployment -- never rebuilding or redeploying,
  * a mandatory production smoke check before ever marking the lifecycle
    LIVE; on smoke failure the lifecycle fails closed to FAILED and the
    production alias is rolled back to the prior last-known-good
    deployment (or left untouched if there was none),
  * MAX_WORKERS=1 ownership for the promotion operation, shared with
    Phase 7/8/9/10 via the same ``ProjectRunner`` project slot,
  * Telegram notification of the LIVE promotion via the Phase 9 adapter.

No network call happens here without every prerequisite adapter being
constructed and passed in explicitly by the caller, matching the Phase 9
``PreviewOrchestrator`` convention.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.core.authz import AuthzError, require_mutating_role, require_owner_role
from app.core.contracts import OperationResult, StaleOperationIntent
from app.core.lifecycle import LifecycleError, ProjectLifecycle
from app.core.state import ProjectStateStore
from app.sandbox.runner import ProjectRunner

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Previous-production classification
# ---------------------------------------------------------------------------
# "Is there a previous production deployment?" has FOUR honest answers, not
# two. Collapsing KNOWN_BOOTSTRAP into either of the old answers is what made
# the first real publish of a project fail closed forever: the bootstrap
# placeholder consumes Vercel's unavoidable first-deployment auto-promotion,
# so EVERY new project publishes against a production deployment that carries
# no content identity -- which the old code could only read as "unknown".
#
#   NO_PRODUCTION
#       No production deployment exists. No rollback target.
#   KNOWN_BOOTSTRAP
#       Provider state positively proves the production deployment is this
#       project's own content-free bootstrap placeholder (see
#       ``VercelAdapter._is_proven_bootstrap``). It is not a user website, so
#       it is not a meaningful rollback target. First real publish may proceed
#       with previous_production = null.
#   REAL_PRODUCTION
#       A previously published user deployment with a COMPLETE, re-promotable
#       identity. Rollback semantics are mandatory and unchanged.
#   UNKNOWN_OR_INCOMPLETE_PRODUCTION
#       A production deployment exists but cannot be positively identified --
#       an unknown or incomplete identity. FAIL CLOSED with INCOMPLETE_LOOKUP.
#       Never guess which deployment was live.
#
# Classification is additive and backward compatible: a collaborator that
# returns no classification at all is treated as UNKNOWN (fail closed), and the
# two previously-meaningful shapes (no deployment_id, or a complete identity)
# keep their exact previous meaning.
PREVIOUS_PRODUCTION_NONE = "NO_PRODUCTION"
PREVIOUS_PRODUCTION_BOOTSTRAP = "KNOWN_BOOTSTRAP"
PREVIOUS_PRODUCTION_REAL = "REAL_PRODUCTION"
PREVIOUS_PRODUCTION_UNKNOWN = "UNKNOWN_OR_INCOMPLETE_PRODUCTION"


def _safe_identity(identity) -> Optional[dict]:
    """A minimal, non-secret identity projection safe to persist in state.

    Only the four trusted identity fields are kept (never a URL, token, or
    raw provider response object). ``None`` is preserved as ``None`` so "no
    previous production" stays distinguishable from an incomplete identity.
    """
    if not isinstance(identity, dict):
        return None
    return {
        "deployment_id": identity.get("deployment_id"),
        "operation_id": identity.get("operation_id"),
        "source_revision": identity.get("source_revision"),
        "artifact_sha256": identity.get("artifact_sha256"),
    }


def _previous_identity_complete(identity) -> bool:
    """A previous-production identity is complete only when it carries the
    full deployment + repository identity needed to re-promote that exact
    deployment as a rollback target. Anything less must be treated as
    unknowable (never guess which deployment was previously production).
    """
    return (
        isinstance(identity, dict)
        and bool(identity.get("deployment_id"))
        and isinstance(identity.get("operation_id"), str)
        and bool(identity.get("operation_id"))
        and isinstance(identity.get("source_revision"), int)
        and identity.get("source_revision") >= 1
        and isinstance(identity.get("artifact_sha256"), str)
        and bool(identity.get("artifact_sha256"))
    )


def _classify_previous_production(result):
    """Classify the current production deployment as
    ``(kind, rollback_identity)``.

    ``rollback_identity`` is the full, re-promotable identity for a REAL
    production deployment and ``None`` for NO_PRODUCTION, KNOWN_BOOTSTRAP and
    UNKNOWN_OR_INCOMPLETE_PRODUCTION -- in the last case because there is
    provably no safe rollback target, which is precisely why the caller must
    fail closed rather than promote.

    Classification is derived only from the adapter's provider-truth result.
    An absent discriminator is UNKNOWN, never a guess: a collaborator that
    cannot distinguish a bootstrap from an unknown deployment has not proven
    anything.
    """
    data = (getattr(result, "data", None) or {}) if result is not None else {}
    if not isinstance(data, dict):
        return PREVIOUS_PRODUCTION_UNKNOWN, None
    if not getattr(result, "success", False):
        # A FAILED lookup proves nothing -- not even that there is no
        # production. The caller returns the failure unchanged; classifying it
        # as "no production" would be reading a failed read as a fact.
        return PREVIOUS_PRODUCTION_UNKNOWN, None
    identifier = data.get("deployment_id")
    if not identifier:
        return PREVIOUS_PRODUCTION_NONE, None
    identity = {
        "deployment_id": identifier,
        "operation_id": data.get("operation_id"),
        "source_revision": data.get("source_revision"),
        "artifact_sha256": data.get("artifact_sha256"),
    }
    if _previous_identity_complete(identity):
        return PREVIOUS_PRODUCTION_REAL, identity
    proof = data.get("bootstrap_proof")
    if (
        isinstance(proof, dict)
        and proof.get("deployment_id") == identifier
        and isinstance(proof.get("bootstrap_operation_id"), str)
        and bool(proof.get("bootstrap_operation_id"))
    ):
        return PREVIOUS_PRODUCTION_BOOTSTRAP, None
    return PREVIOUS_PRODUCTION_UNKNOWN, None


@dataclass
class PromoteDeps:
    """All external boundary objects. None of these are constructed here."""

    vercel: Any
    telegram: Any
    smoke: Any
    chat_id_for: Any  # callable(project_id, state) -> str, may return None
    app_id_for: Any = field(default=lambda project_id: project_id)
    # Optional callable(project_id, state) -> Optional[str]. Returns the
    # ALREADY-BOUND friendly Vercel slug from trusted registry state -- never
    # derived fresh here (promotion only runs after a successful preview,
    # which is what binds the slug). None disables slug resolution: the
    # legacy opaque hash-derived project name governs, exactly as before.
    slug_for: Any = None
    # PHASE F: optional callable(vercel_project_id) -> Optional[str] returning
    # the project-specific Vercel automation-bypass secret for the production
    # smoke (same access contract as preview smoke). None disables bypass
    # (legacy behavior: production smoke runs without a bypass header).
    bypass_for: Any = None


class PromotionOrchestrator:
    """Application-owned Phase 11 orchestration.

    Two-step protocol, mirroring the Phase 10 reserve()/apply() split:

      ``approve(project_id)`` -- binds the CURRENT latest shown preview
      identity as the approved identity. Does not touch Vercel or
      Telegram. Fails closed if there is no shown preview yet.

      ``promote(project_id, workspace)`` -- verifies the bound approval
      identity still matches the project's current latest shown preview
      (fail-closed / STALE_APPROVAL otherwise), then promotes the EXACT
      approved Vercel deployment to production, runs a mandatory
      production smoke check, and only then transitions PUBLISHING ->
      LIVE. On smoke failure, rolls the alias back (if a prior production
      deployment existed) and transitions to FAILED.
    """

    def __init__(
        self,
        runner: ProjectRunner,
        store: ProjectStateStore,
        deps: PromoteDeps,
        smoke_dir_root: Optional[Path] = None,
    ):
        self.runner = runner
        self.store = store
        self.deps = deps
        self.smoke_dir_root = smoke_dir_root

    # ------------------------------------------------------------------
    # Step 1: approval -- binds the exact shown preview identity.
    # ------------------------------------------------------------------

    def approve(
        self,
        project_id: str,
        approved_by: Optional[str] = None,
        principal_id: Optional[str] = None,
        reference_token: Optional[str] = None,
    ) -> OperationResult:
        """Bind the CURRENT latest shown preview as the approved identity.

        Fails closed if the project has never shown a preview, or if the
        shown preview's revision does not match the current source
        revision (i.e. something changed since the preview was shown and
        it is no longer current).

        Requires an owner or authorized reviewer principal/reference token.
        Unauthorized attempts are rejected before any state mutation.
        """
        with self.store.acquire_writer(project_id) as state:
            try:
                require_mutating_role(
                    state, principal_id=principal_id, reference_token=reference_token
                )
            except AuthzError as exc:
                return OperationResult.fail(exc.error_code, error_code=exc.error_code)
            if state.lifecycle not in (
                ProjectLifecycle.PREVIEW_READY.value,
                ProjectLifecycle.LIVE.value,
            ):
                return OperationResult.fail(
                    "APPROVAL_NOT_ALLOWED_IN_LIFECYCLE",
                    error_code="APPROVAL_NOT_ALLOWED_IN_LIFECYCLE",
                )
            shown = state.deployment.get("latest_shown_preview")
            if not shown or not shown.get("operation_id"):
                return OperationResult.fail("NO_SHOWN_PREVIEW", error_code="NO_SHOWN_PREVIEW")
            if (
                shown.get("source_revision") != state.revisions.source_revision
                or shown.get("source_revision") != state.revisions.preview_revision
            ):
                return OperationResult.fail("STALE_QA_BINDING", error_code="STALE_QA_BINDING")

            approval = {
                "operation_id": shown["operation_id"],
                "source_revision": shown["source_revision"],
                "preview_url": shown.get("preview_url"),
                "deployment_id": shown.get("deployment_id"),
                "source_sha256": shown.get("source_sha256"),
                "artifact_sha256": shown.get("artifact_sha256"),
                "approved_by": principal_id,
                "approved_at": time.time(),
            }
            if not approval["deployment_id"] or not approval["source_sha256"] or not approval["artifact_sha256"]:
                return OperationResult.fail("INCOMPLETE_PREVIEW_IDENTITY", error_code="INCOMPLETE_PREVIEW_IDENTITY")

            state.deployment["approval"] = approval
            state.revisions.approved_revision = shown["source_revision"]
            self.store.save(state)
        logger.info(
            "Approval accepted project=%s revision=%s deployment=%s",
            project_id, approval["source_revision"], approval["deployment_id"],
        )
        return OperationResult.ok(approval)

    # ------------------------------------------------------------------
    # Step 2: promotion -- verify-then-promote-then-smoke-then-LIVE.
    # ------------------------------------------------------------------

    def promote(
        self,
        project_id: str,
        workspace: Path,
        principal_id: Optional[str] = None,
        reference_token: Optional[str] = None,
    ) -> OperationResult:
        """Serialize promotion without nesting project writer locks.

        Final production promotion is owner-only -- a reviewer may approve
        a preview but must not be able to unilaterally publish it live.
        """
        state = self.store.load(project_id)
        if state is None:
            return OperationResult.fail("NO_PROJECT_STATE", error_code="NO_PROJECT_STATE")
        try:
            require_owner_role(
                state, principal_id=principal_id, reference_token=reference_token
            )
        except AuthzError as exc:
            return OperationResult.fail(exc.error_code, error_code=exc.error_code)

        if not self.runner.acquire_project(project_id):
            return OperationResult.fail(
                "WORKER_BUSY",
                error_code="WORKER_BUSY",
                retryable=True,
            )
        try:
            return self._promote(project_id, workspace, principal_id, reference_token)
        finally:
            self.runner.release_project(project_id)

    def _promote(self, project_id: str, workspace: Path,
                 principal_id=None, reference_token=None) -> OperationResult:
        with self.store.acquire_writer(project_id) as state:
            try:
                require_owner_role(state, principal_id, reference_token)
            except AuthzError as exc:
                return OperationResult.fail(exc.error_code, error_code=exc.error_code)
        return self._promote_authorized(project_id, workspace, principal_id, reference_token)

    def _promote_authorized(self, project_id, workspace, principal_id=None, reference_token=None):
        state = self.store.load(project_id)
        if state is None:
            return OperationResult.fail("NO_PROJECT_STATE", error_code="NO_PROJECT_STATE")
        try:
            require_owner_role(state, principal_id, reference_token)
        except AuthzError as exc:
            return OperationResult.fail(exc.error_code, error_code=exc.error_code)

        approval = state.deployment.get("approval")
        if not approval or not approval.get("operation_id"):
            return OperationResult.fail("NOT_APPROVED", error_code="NOT_APPROVED")

        # A crash mid-promotion leaves the project in PUBLISHING. Re-entering
        # the SAME operation (approval's operation_id matches the persisted
        # promotion_intent's) is the intended crash-recovery resume, NOT a new
        # operation -- it reuses the durable previous-production identity so
        # the rollback target is never recomputed against a drifted state.
        _intent = state.deployment.get("promotion_intent") or {}
        is_resume = (
            state.lifecycle == ProjectLifecycle.PUBLISHING.value
            and _intent.get("operation_id") == approval.get("operation_id")
        )
        if state.lifecycle not in (
            ProjectLifecycle.PREVIEW_READY.value,
            ProjectLifecycle.LIVE.value,
        ) and not is_resume:
            return OperationResult.fail(
                "PROMOTION_NOT_ALLOWED_IN_LIFECYCLE",
                error_code="PROMOTION_NOT_ALLOWED_IN_LIFECYCLE",
            )

        # ---- Guard against stale approval: the CURRENT shown preview
        # must be EXACTLY the one that was approved. Any newer revision,
        # revise, or re-run preview since approval invalidates it.
        shown = state.deployment.get("latest_shown_preview") or {}
        if (
            shown.get("operation_id") != approval.get("operation_id")
            or shown.get("deployment_id") != approval.get("deployment_id")
            or shown.get("source_sha256") != approval.get("source_sha256")
            or shown.get("artifact_sha256") != approval.get("artifact_sha256")
            or state.revisions.source_revision != approval.get("source_revision")
            or state.revisions.preview_revision != approval.get("source_revision")
        ):
            return OperationResult.fail("STALE_APPROVAL", error_code="STALE_APPROVAL")

        operation_id = approval["operation_id"]
        source_revision = approval["source_revision"]
        artifact_sha256 = approval["artifact_sha256"]
        deployment_id = approval["deployment_id"]
        logger.info(
            "Publish starting project=%s revision=%s", project_id, source_revision,
        )

        # Idempotent no-op: already LIVE with this EXACT approved identity
        # (e.g. a duplicate/retried promote call). Nothing to do — do not
        # re-promote, re-smoke, or attempt a PUBLISHING transition (LIVE ->
        # PUBLISHING is not a valid lifecycle edge).
        if state.lifecycle == ProjectLifecycle.LIVE.value:
            last_live = state.deployment.get("last_live_deployment") or {}
            if (
                last_live.get("operation_id") == operation_id
                and last_live.get("deployment_id") == deployment_id
                and last_live.get("source_revision") == source_revision
            ):
                return OperationResult.ok({
                    "production_url": state.production_url,
                    "deployment_id": deployment_id,
                    "operation_id": operation_id,
                })
            return OperationResult.fail(
                "PROMOTION_NOT_ALLOWED_IN_LIFECYCLE",
                error_code="PROMOTION_NOT_ALLOWED_IN_LIFECYCLE",
            )

        app_id = self.deps.app_id_for(project_id)
        slug = None
        if self.deps.slug_for is not None:
            try:
                slug = self.deps.slug_for(project_id, state)
            except Exception:
                slug = None
        # Canonical Vercel project name, resolved from trusted bound registry
        # state BEFORE and independently of any provider response. Threaded
        # through every adapter call that revalidates the owned project so
        # those checks assert the canonical name instead of re-deriving the
        # opaque hash default (same threading contract as PreviewOrchestrator).
        expected_name = slug if slug else None
        project_result = self.deps.vercel.lookup_project(app_id, expected_name=expected_name)
        if not project_result.success:
            return project_result
        vercel_project = project_result.data["project"]

        # ---- Classify the CURRENT production BEFORE promoting, so a failed
        # post-promotion smoke check knows whether it has a real rollback
        # target at all. Classification is explicit (four kinds) because
        # "a deployment exists" and "we know which deployment was live" are
        # different facts, and only the second one licenses a rollback.
        previous_result = self.deps.vercel.find_production_deployment(
            app_id, vercel_project, expected_name=expected_name)
        if not previous_result.success:
            return previous_result
        previous_class, fresh_previous = _classify_previous_production(previous_result)
        logger.info(
            "Previous production classified=%s project=%s deployment=%s",
            previous_class,
            project_id,
            (fresh_previous or {}).get("deployment_id")
            or (previous_result.data or {}).get("deployment_id"),
        )
        if previous_class == PREVIOUS_PRODUCTION_UNKNOWN:
            # A production deployment exists but cannot be positively
            # identified. We can never guess a rollback target, so we refuse
            # to promote at all -- the first, pre-side-effect fail-closed gate.
            return OperationResult.fail("INCOMPLETE_LOOKUP", error_code="INCOMPLETE_LOOKUP")

        # ---- Transition PREVIEW_READY/LIVE -> PUBLISHING before any
        # external side effect, mirroring the durable-intent pattern used
        # by Phase 9's preview orchestrator.
        with self.store.acquire_writer(project_id) as locked:
            try:
                require_owner_role(locked, principal_id, reference_token)
            except AuthzError as exc:
                return OperationResult.fail(exc.error_code, error_code=exc.error_code)
            if (locked.deployment.get("approval") != approval
                    or locked.deployment.get("latest_shown_preview") != shown
                    or locked.revisions.source_revision != source_revision
                    or locked.revisions.qa_revision != source_revision
                    or locked.revisions.preview_revision != source_revision):
                return OperationResult.fail("STALE_APPROVAL", error_code="STALE_APPROVAL")
            if locked.lifecycle != ProjectLifecycle.PUBLISHING.value:
                try:
                    self.store.transition_lifecycle_locked(locked, ProjectLifecycle.PUBLISHING)
                except LifecycleError as exc:
                    return OperationResult.fail(str(exc), error_code="INVALID_LIFECYCLE_TRANSITION")
            existing_intent = locked.deployment.get("promotion_intent") or {}
            is_same_operation = existing_intent.get("operation_id") == operation_id
            if is_same_operation and "previous_production" in existing_intent:
                # Re-entry into the SAME promotion operation with a persisted
                # previous-production identity: REUSE it. Never recompute --
                # a crash after the remote promote but before the
                # stage="promoted" write would otherwise cause the retry to
                # capture the just-promoted (broken) deployment as its own
                # rollback target.
                previous_identity = existing_intent["previous_production"]
                if previous_identity is not None and not _previous_identity_complete(
                        previous_identity):
                    return OperationResult.fail(
                        "INCOMPLETE_LOOKUP", error_code="INCOMPLETE_LOOKUP")
            elif is_same_operation and "previous_production" not in existing_intent:
                # Legacy/partial intent written before previous_production was
                # persisted: the durable record cannot prove the rollback
                # target's identity. Fail closed rather than guessing.
                return OperationResult.fail(
                    "INCOMPLETE_LOOKUP", error_code="INCOMPLETE_LOOKUP")
            else:
                previous_identity = fresh_previous
                locked.deployment["promotion_intent"] = {
                    "operation_id": operation_id,
                    "deployment_id": deployment_id,
                    # Full trusted previous-production identity, persisted
                    # BEFORE any remote side effect. None means "no rollback
                    # target" -- either no production deployment at all, or a
                    # proven bootstrap placeholder. The explicit classification
                    # keeps those two cases distinguishable on a resume
                    # instead of collapsing them.
                    "previous_production": previous_identity,
                    "previous_production_class": previous_class,
                    "previous_production_deployment_id": (
                        previous_identity.get("deployment_id")
                        if previous_identity else None
                    ),
                    "stage": "publishing",
                    "created_at": time.time(),
                }
            self.store.save(locked)
            # Report the classification this intent ACTUALLY carries: on a
            # same-operation resume the persisted one is authoritative, because
            # the fresh lookup describes post-crash remote state, not the
            # rollback target the operation was started against.
            intent_class = (
                (locked.deployment.get("promotion_intent") or {}).get(
                    "previous_production_class"
                ) or previous_class
            )

            # The exact trusted identity of the deployment we intended to
            # promote. Reconciliation compares the COMPLETE tuple -- never a
            # URL or a bare deployment_id.
            intended_identity = {
                "deployment_id": deployment_id,
                "operation_id": operation_id,
                "source_revision": source_revision,
                "artifact_sha256": artifact_sha256,
            }

        # ---- Writer lock RELEASED. Every remote call happens from here on.
        #
        # The exclusive writer lock is a cross-process file lock with a
        # bounded wait; holding it across a production-mutating POST (and
        # across the reconcile reads that decide whether to send it) blocked
        # every other operation on this project for the duration of a network
        # round trip that can stall under latency, 5xx or rate limiting.
        # The durable intent above is exactly what makes releasing it safe:
        # a crash at any point from here is recovered by the same-operation
        # resume, which reconciles remote truth before acting again.
        logger.info(
            "Promotion intent persisted project=%s operation=%s class=%s",
            project_id, operation_id, intent_class,
        )

        # ---- Same-operation resume after an AMBIGUOUS remote promote:
        # reconcile remote truth BEFORE re-issuing any promote request.
        # If Vercel already promoted this exact deployment, adopt that
        # truth (never a second promote POST). If remote truth says it was
        # NOT promoted, the normal promote below is safe (it will fail
        # closed on any residual ambiguity without duplicating a
        # side effect). If truth is still ambiguous, stop here -- fail
        # closed, keep PUBLISHING, and do not send a second promote.
        resume_reconciled_url = None
        resume_fail_closed = False
        if is_same_operation:
            reconcile = self.deps.vercel.reconcile_production_deployment(
                app_id, vercel_project, intended_identity, expected_name=expected_name,
            )
            status = (reconcile.data or {}).get("status") if reconcile.success else None
            if status == "PROMOTED":
                resume_reconciled_url = self._production_url_for(intended_identity)
                logger.info("Promotion confirmed deployment=%s (reconciled)", deployment_id)
                self._update_intent(project_id, operation_id,
                                    stage="promoted",
                                    production_url=resume_reconciled_url)
            elif status == "NOT_PROMOTED":
                # Conclusively not promoted: fall through to the normal
                # promote below (safe -- the intended deployment is not
                # the live target).
                pass
            else:
                # Ambiguous resume: do NOT blindly re-promote.
                self._fail_reconciliation_required(
                    project_id, "PROMOTION_RECONCILIATION_REQUIRED",
                    intended_identity, "promote",
                )
                resume_fail_closed = True

        if resume_reconciled_url is not None or resume_fail_closed:
            promote_result = None
        else:
            promote_result = self.deps.vercel.promote_deployment(
                app_id, vercel_project, deployment_id, operation_id,
                source_revision, artifact_sha256, expected_name=expected_name,
            )

        if resume_fail_closed:
            return OperationResult.fail(
                "PROMOTION_RECONCILIATION_REQUIRED",
                error_code="PROMOTION_RECONCILIATION_REQUIRED",
            )
        if resume_reconciled_url is not None:
            return self._post_promote(
                project_id, workspace, app_id, vercel_project,
                previous_identity, production_url=resume_reconciled_url,
                intended_identity=intended_identity,
                expected_name=expected_name, reconciled=True,
                approval=approval, source_revision=source_revision,
            )

        if not promote_result.success:
            error_code = promote_result.error_code or "PROMOTE_FAILED"
            if error_code != "PROMOTE_RECONCILIATION_REQUIRED":
                self._fail(project_id, "PROMOTE_FAILED", error_code)
                return promote_result
            # ---- AMBIGUOUS remote promote: the request may have reached
            # Vercel but the outcome is unknown. NEVER treat this as a
            # confirmed failure (Vercel may already be serving the new
            # deployment) and NEVER blindly re-send the promote. Re-read
            # production truth and decide from the COMPLETE identity tuple.
            reconcile = self.deps.vercel.reconcile_production_deployment(
                app_id, vercel_project, intended_identity, expected_name=expected_name,
            )
            status = (reconcile.data or {}).get("status") if reconcile.success else None
            if status == "PROMOTED":
                # Confirmed promoted -> continue the normal post-promote flow
                # (production smoke, then LIVE persistence).
                production_url = self._production_url_for(intended_identity)
                logger.info("Promotion confirmed deployment=%s (reconciled)", deployment_id)
                self._update_intent(project_id, intended_identity["operation_id"],
                                    stage="promoted", production_url=production_url)
                return self._post_promote(
                    project_id, workspace, app_id, vercel_project,
                    previous_identity, production_url=production_url,
                    intended_identity=intended_identity,
                    expected_name=expected_name, reconciled=True,
                    approval=approval, source_revision=source_revision,
                )
            elif status == "NOT_PROMOTED":
                # Remote truth conclusively shows the intended deployment was
                # NOT promoted -> a real, confirmed failure.
                self._fail(project_id, "PROMOTE_FAILED", "PROMOTE_NOT_APPLIED")
                return OperationResult.fail(
                    "PROMOTE_FAILED", error_code="PROMOTE_NOT_APPLIED",
                )
            else:
                # Still ambiguous / lookup failed / identity incomplete:
                # preserve PUBLISHING + the intact promotion_intent and fail
                # closed as reconciliation-required. A later same-operation
                # resume reconciles again and reuses the SAME intent (no
                # second promote POST).
                self._fail_reconciliation_required(
                    project_id, "PROMOTION_RECONCILIATION_REQUIRED",
                    intended_identity, "promote",
                )
                return OperationResult.fail(
                    "PROMOTION_RECONCILIATION_REQUIRED",
                    error_code="PROMOTION_RECONCILIATION_REQUIRED",
                )

        production_url = promote_result.data["production_url"]
        logger.info("Promotion confirmed deployment=%s", deployment_id)
        self._update_intent(project_id, intended_identity["operation_id"],
                            stage="promoted", production_url=production_url)
        return self._post_promote(
            project_id, workspace, app_id, vercel_project, previous_identity,
            production_url=production_url, intended_identity=intended_identity,
            expected_name=expected_name, reconciled=False,
            approval=approval, source_revision=source_revision,
        )

    def _post_promote(
        self, project_id, workspace, app_id, vercel_project, previous_identity,
        production_url, intended_identity, expected_name, reconciled,
        approval, source_revision,
    ) -> OperationResult:
        """Normal post-promote flow after a CONFIRMED promote (direct or
        reconciled): production smoke, then PUBLISHING -> LIVE, then the
        best-effort Telegram notification.

        On smoke failure the alias is rolled back to the persisted previous
        production and the EXACT observed rollback outcome is persisted.
        """
        deployment_id = intended_identity["deployment_id"]
        operation_id = intended_identity["operation_id"]
        artifact_sha256 = intended_identity["artifact_sha256"]

        # ---- Mandatory production smoke check before ever marking LIVE.
        smoke_dir = (self.smoke_dir_root or workspace) / "qa" / "production_smoke"
        smoke_result = self._run_production_smoke(
            production_url, smoke_dir,
            (vercel_project or {}).get("id"),
        )
        self._update_intent(project_id, operation_id, stage="smoked",
                            smoke=smoke_result.data)
        if not smoke_result.success:
            logger.warning(
                "Production smoke FAILED project=%s rollback=%s",
                project_id, (smoke_result.data or {}).get("url") and "see-state",
            )
            rollback_status = self._rollback_and_fail(
                project_id, app_id, vercel_project, previous_identity,
                error_code="SMOKE_FAILED", expected_name=expected_name,
            )
            return OperationResult(
                success=False, error="SMOKE_FAILED", error_code="SMOKE_FAILED",
                data={
                    **(smoke_result.data or {}),
                    "rollback": rollback_status,
                    "production_smoke_failed": True,
                },
            )
        # Logged only AFTER the check: a failed smoke must never produce a
        # "smoke passed" line for an operator to act on.
        logger.info("Production smoke passed project=%s", project_id)

        # ---- Only now transition PUBLISHING -> LIVE.
        with self.store.acquire_writer(project_id) as locked:
            if (
                locked.deployment.get("approval") != approval
                or locked.lifecycle != ProjectLifecycle.PUBLISHING.value
            ):
                return OperationResult.fail("STALE_APPROVAL", error_code="STALE_APPROVAL")
            self.store.transition_lifecycle_locked(locked, ProjectLifecycle.LIVE)
            locked.revisions.live_revision = source_revision
            locked.production_url = production_url
            locked.deployment["promotion_intent"]["stage"] = "live"
            locked.deployment["last_live_deployment"] = {
                "operation_id": operation_id,
                "deployment_id": deployment_id,
                "production_url": production_url,
                "source_revision": source_revision,
                "source_sha256": approval["source_sha256"],
                "artifact_sha256": artifact_sha256,
                "live_at": time.time(),
            }
            self.store.save(locked)
            state = locked

        # ---- Telegram notification of LIVE promotion (best-effort in the
        # sense that a failed notification does not un-promote; the site
        # is already live and smoke-verified at this point).
        logger.info("Project LIVE production_url=%s", production_url)
        chat_id = self.deps.chat_id_for(project_id, state)
        if chat_id:
            self.deps.telegram.send_text(
                chat_id, f"🚀 Live: {production_url}"
            )

        return OperationResult.ok({
            "production_url": production_url,
            "deployment_id": deployment_id,
            "operation_id": operation_id,
            "reconciled": reconciled,
        })

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _update_intent(self, project_id: str, operation_id: str, **fields) -> None:
        """Persist an intent update for *operation_id*, or fail closed.

        A mismatch (or a missing intent) is NOT a no-op. The caller has just
        driven, or is about to drive, a production-altering remote call whose
        only durable evidence is this record; silently doing nothing would let
        that happen unrecorded and defeat the same-operation resume design.
        Raising propagates to the dispatcher's fail-closed handler.
        """
        with self.store.acquire_writer(project_id) as state:
            intent = state.deployment.get("promotion_intent")
            if not intent or intent.get("operation_id") != operation_id:
                raise StaleOperationIntent(
                    "promotion_intent does not belong to this operation"
                )
            intent.update(fields)
            self.store.save(state)

    @staticmethod
    def _production_url_for(identity: dict) -> Optional[str]:
        """Derive the canonical production URL for an identity from PROVIDER
        state only.

        The promote-adapter response URL is never trusted for identity, so an
        ambiguous promote reconciled as successful must not invent a URL from
        the request. ``deployment_id`` is a Vercel deployment id (e.g.
        ``dpl_abc``) and the canonical alias host is ``<id>.vercel.app``; the
        smoke check re-reads real provider truth through that URL.
        """
        dev = identity.get("deployment_id") if isinstance(identity, dict) else None
        if isinstance(dev, str) and dev and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", dev):
            return "https://" + dev + ".vercel.app"
        return None

    def _fail(self, project_id: str, error: str, error_code: str) -> None:
        with self.store.acquire_writer(project_id) as state:
            if state.lifecycle != ProjectLifecycle.FAILED.value:
                try:
                    self.store.transition_lifecycle_locked(state, ProjectLifecycle.FAILED)
                except LifecycleError:
                    pass
            state.failure = {
                "phase": "promotion",
                "error": error,
                "error_code": error_code,
                "failed_at": time.time(),
            }
            self.store.save(state)

    def _fail_reconciliation_required(
        self, project_id: str, error_code: str, target_identity, stage: str,
    ) -> None:
        """Fail closed on an AMBIGUOUS remote side effect WITHOUT transitioning
        to terminal FAILED.

        The project's lifecycle is left in PUBLISHING and the durable
        ``promotion_intent`` (including the persisted previous-production
        identity) is left intact, so a later SAME-OPERATION resume can
        reconcile remote truth before acting again -- and never blindly
        re-send a promote. Only the failure record and the intent stage are
        updated; no new promotion intent is created and previous_production
        is never overwritten.
        """
        with self.store.acquire_writer(project_id) as state:
            self._fail_reconciliation_required_locked(
                state, error_code, target_identity, stage,
            )

    def _fail_reconciliation_required_locked(
        self, state, error_code: str, target_identity, stage: str,
    ) -> None:
        """Locked-body variant: caller already holds the project writer lock."""
        intent = state.deployment.get("promotion_intent")
        if intent:
            intent["stage"] = stage
        state.failure = {
            "phase": "promotion",
            "error": error_code,
            "error_code": error_code,
            "reconciliation_required": True,
            "target_identity": _safe_identity(target_identity),
            "failed_at": time.time(),
        }
        self.store.save(state)

    def _fail_with_rollback_outcome(
        self, project_id: str, rollback_status: str, previous_identity,
        expected_name=None, exception_class: Optional[str] = None,
    ) -> None:
        """Persist a production-smoke failure TOGETHER with the exact observed
        rollback outcome, then fail the lifecycle closed.

        A rollback is a remote side effect too: its outcome must be observed
        and recorded, never discarded or collapsed into an undifferentiated
        ``PRODUCTION_SMOKE_FAILED``. ``rollback_status`` is one of
        ``SUCCEEDED`` / ``FAILED`` / ``RECONCILIATION_REQUIRED`` /
        ``TARGET_IDENTITY_INCOMPLETE``.
        """
        primary_error_code = "PRODUCTION_SMOKE_FAILED"
        code_map = {
            # Rollback succeeded: preserve the EXISTING durable convention
            # (error=PRODUCTION_SMOKE_FAILED, error_code=SMOKE_FAILED) while
            # adding an explicit rollback outcome the operator can read.
            "SUCCEEDED": "SMOKE_FAILED",
            "FAILED": "ROLLBACK_FAILED",
            "RECONCILIATION_REQUIRED": "ROLLBACK_RECONCILIATION_REQUIRED",
            "TARGET_IDENTITY_INCOMPLETE": "ROLLBACK_TARGET_IDENTITY_INCOMPLETE",
        }
        error_code = code_map.get(rollback_status, "ROLLBACK_FAILED")
        rollback_target = _safe_identity(previous_identity)
        with self.store.acquire_writer(project_id) as state:
            if state.lifecycle != ProjectLifecycle.FAILED.value:
                try:
                    self.store.transition_lifecycle_locked(state, ProjectLifecycle.FAILED)
                except LifecycleError:
                    pass
            state.failure = {
                "phase": "promotion",
                "error": primary_error_code,
                "error_code": error_code,
                "primary_error_code": primary_error_code,
                "rollback": rollback_status,
                "rollback_target": rollback_target,
                "rollback_reconciliation_required": rollback_status in (
                    "RECONCILIATION_REQUIRED", "TARGET_IDENTITY_INCOMPLETE",
                ),
                "failed_at": time.time(),
            }
            if exception_class is not None:
                # Only the exception TYPE NAME is persisted -- never the raw
                # message, transport body, or any credential-bearing detail.
                state.failure["rollback_exception_class"] = exception_class
            self.store.save(state)

    def _run_production_smoke(self, production_url, smoke_dir, vercel_project_id=None):
        """PHASE F: run the production smoke with the SAME access contract as
        the preview smoke (project-specific bypass), when configured.

        The bypass secret (if any) is passed only when the smoke collaborator
        accepts it; legacy 2-arg smoke doubles keep working unchanged. The
        secret is scoped to ``*.vercel.app`` by the smoke tester itself, so a
        custom-domain production URL simply receives no bypass header.
        """
        secret = None
        if self.deps.bypass_for is not None:
            try:
                secret = self.deps.bypass_for(vercel_project_id)
            except Exception:
                secret = None
        if secret is None:
            return self.deps.smoke.run(production_url, smoke_dir)
        try:
            import inspect
            params = inspect.signature(self.deps.smoke.run).parameters
            accepts = (
                "bypass_secret" in params
                or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
            )
        except (TypeError, ValueError):
            accepts = False
        if accepts:
            return self.deps.smoke.run(
                production_url, smoke_dir, bypass_secret=secret
            )
        return self.deps.smoke.run(production_url, smoke_dir)

    def _rollback_and_fail(self, project_id, app_id, vercel_project, previous_identity,
        error_code: str, expected_name=None,
    ):
        """Roll the production alias back to the prior last-known-good
        deployment (if one existed) before failing the lifecycle closed, and
        PERSIST the observed rollback outcome.

        If there was no prior production deployment, there is nothing to
        roll back to -- production is left as whatever Vercel's own state
        is (the newly promoted, smoke-FAILED deployment), and the failure
        is recorded so a human can intervene. This mirrors Phase 9's
        fail-closed-on-ambiguity posture: we never guess a rollback target.

        The rollback re-promotes the previous deployment using THE PREVIOUS
        DEPLOYMENT'S OWN persisted identity (its wbOperation/wbRevision/
        wbArtifact), which is exactly what ``promote_deployment`` validates
        before promoting. Deriving that identity from the CURRENT promotion
        operation instead is a guaranteed meta mismatch and silently leaves
        the smoke-failed deployment live. If the persisted previous identity
        is incomplete, we fail closed -- we never guess which deployment was
        previously production.

        The rollback is attempted EXACTLY ONCE. Its result is never
        discarded: a falsy/ambiguous ``OperationResult`` (the adapter reports
        failure that way, not by raising) is classified and persisted as a
        distinct failure code, and a raised exception is classified and
        persisted rather than swallowed.

        Returns the classified rollback status string.
        """
        if previous_identity is None:
            # No prior production -> nothing to roll back to. Preserve the
            # normal production-smoke-failure semantics; do not invent a
            # rollback target.
            self._fail(project_id, "PRODUCTION_SMOKE_FAILED", error_code)
            return "NO_PREVIOUS_PRODUCTION"
        if not _previous_identity_complete(previous_identity):
            # Incomplete identity: fail closed. Surface a distinct error so
            # an operator knows the rollback could not run safely.
            self._fail_with_rollback_outcome(
                project_id, "TARGET_IDENTITY_INCOMPLETE", previous_identity,
            )
            return "TARGET_IDENTITY_INCOMPLETE"
        try:
            rollback_result = self.deps.vercel.promote_deployment(
                app_id, vercel_project,
                previous_identity["deployment_id"],
                previous_identity["operation_id"],
                previous_identity["source_revision"],
                previous_identity["artifact_sha256"],
                expected_name=expected_name,
            )
        except Exception as exc:
            # Never swallow: classify the exception and persist a distinct
            # rollback failure. Raw transport contents/secrets are never
            # persisted -- only the exception type name.
            self._fail_with_rollback_outcome(
                project_id, "FAILED", previous_identity,
                exception_class=type(exc).__name__,
            )
            return "FAILED"
        if rollback_result is not None and rollback_result.success:
            rollback_status = "SUCCEEDED"
        else:
            # The adapter reports failures as a falsy OperationResult / error
            # result -- classify before collapsing.
            rollback_error_code = (
                getattr(rollback_result, "error_code", None)
                if rollback_result is not None else None
            )
            if rollback_error_code in (
                "PROMOTE_RECONCILIATION_REQUIRED", "PROMOTE_VERIFICATION_FAILED",
            ):
                rollback_status = "RECONCILIATION_REQUIRED"
            else:
                rollback_status = "FAILED"
        self._fail_with_rollback_outcome(
            project_id, rollback_status, previous_identity,
            exception_class=None,
        )
        return rollback_status
