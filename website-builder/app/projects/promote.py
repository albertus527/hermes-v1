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

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.core.authz import AuthzError, require_mutating_role, require_owner_role
from app.core.contracts import OperationResult
from app.core.lifecycle import LifecycleError, ProjectLifecycle
from app.core.state import ProjectStateStore
from app.sandbox.runner import ProjectRunner


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
                "Another project is currently being built (MAX_WORKERS=1)",
                error_code="WORKER_BUSY",
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

        if state.lifecycle not in (
            ProjectLifecycle.PREVIEW_READY.value,
            ProjectLifecycle.LIVE.value,
        ):
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

        # ---- Capture last-known-good production identity BEFORE
        # promoting, so a failed post-promotion smoke check can roll back.
        previous_result = self.deps.vercel.find_production_deployment(
            app_id, vercel_project, expected_name=expected_name)
        if not previous_result.success:
            return previous_result
        previous_deployment_id = previous_result.data.get("deployment_id")

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
            locked.deployment["promotion_intent"] = {
                "operation_id": operation_id,
                "deployment_id": deployment_id,
                "previous_production_deployment_id": previous_deployment_id,
                "stage": "publishing",
                "created_at": time.time(),
            }
            self.store.save(locked)

            # Writer remains held through the first mutating adapter call.
            promote_result = self.deps.vercel.promote_deployment(
                app_id, vercel_project, deployment_id, operation_id,
                source_revision, artifact_sha256, expected_name=expected_name,
            )
        if not promote_result.success:
            self._fail(project_id, "PROMOTE_FAILED", promote_result.error_code or "PROMOTE_FAILED")
            return promote_result

        production_url = promote_result.data["production_url"]
        self._update_intent(project_id, stage="promoted", production_url=production_url)

        # ---- Mandatory production smoke check before ever marking LIVE.
        smoke_dir = (self.smoke_dir_root or workspace) / "qa" / "production_smoke"
        smoke_result = self.deps.smoke.run(production_url, smoke_dir)
        self._update_intent(project_id, stage="smoked", smoke=smoke_result.data)
        if not smoke_result.success:
            self._rollback_and_fail(
                project_id, app_id, vercel_project, previous_deployment_id,
                operation_id, source_revision, artifact_sha256,
                error_code="SMOKE_FAILED", expected_name=expected_name,
            )
            return OperationResult(
                success=False, error="SMOKE_FAILED", error_code="SMOKE_FAILED",
                data=smoke_result.data,
            )

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
        chat_id = self.deps.chat_id_for(project_id, state)
        if chat_id:
            self.deps.telegram.send_text(
                chat_id, f"🚀 Live: {production_url}"
            )

        return OperationResult.ok({
            "production_url": production_url,
            "deployment_id": deployment_id,
            "operation_id": operation_id,
        })

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _update_intent(self, project_id: str, **fields) -> None:
        with self.store.acquire_writer(project_id) as state:
            intent = state.deployment.get("promotion_intent")
            if intent:
                intent.update(fields)
                self.store.save(state)

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

    def _rollback_and_fail(
        self, project_id, app_id, vercel_project, previous_deployment_id,
        operation_id: str, source_revision: int, artifact_sha256: str, error_code: str,
        expected_name=None,
    ):
        """Roll the production alias back to the prior last-known-good
        deployment (if one existed) before failing the lifecycle closed.

        If there was no prior production deployment, there is nothing to
        roll back to -- production is left as whatever Vercel's own state
        is (the newly promoted, smoke-FAILED deployment), and the failure
        is recorded so a human can intervene. This mirrors Phase 9's
        fail-closed-on-ambiguity posture: we never guess a rollback target.
        """
        if previous_deployment_id:
            try:
                self.deps.vercel.promote_deployment(
                    app_id, vercel_project, previous_deployment_id,
                    "rollback:" + operation_id, source_revision, artifact_sha256,
                    expected_name=expected_name,
                )
            except Exception:
                pass
        self._fail(project_id, "PRODUCTION_SMOKE_FAILED", error_code)
