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
  * reusing the Phase 9 owned-Vercel-project adapters to promote the EXACT
    approved artifact -- no rebuild of our own, no new files. Following the
    Vercel CLI, a preview-target deployment is promoted by CREATION (the
    created deployment inherits the approved deployment's exact files) and a
    production-target deployment by alias remap,
  * a BOUNDED READ-ONLY confirmation of production truth afterwards. Vercel
    promotion is asynchronous, so "still the old binding when we stopped
    looking" is ambiguous -- never a confirmed failure. Only a provider-side
    terminal rejection or job failure is terminal; a timeout leaves the
    durable intent intact and the operation reconcilable,
  * same-operation recovery for a project that already failed mid-promotion,
    which reconciles remote truth and adopts it when -- and only when -- the
    exact trusted identity is proven, with no second promote request,
  * a mandatory production smoke check before ever marking the lifecycle
    LIVE; on smoke failure the lifecycle fails closed to FAILED and the
    production alias is rolled back to the prior last-known-good
    deployment (or left untouched if there was none),
  * MAX_WORKERS=1 ownership for the promotion operation, shared with
    Phase 7/8/9/10 via the same ``ProjectRunner`` project slot,
  * Telegram notification of the LIVE promotion via the Phase 9 adapter.

Two URL concepts are kept strictly apart and are never derived from one
another:

  ``deployment_url``
      The promoted deployment's own, DEPLOYMENT-SPECIFIC Vercel hostname
      (``<project>-<hash>-<team>.vercel.app``). Internal identity only:
      reconciliation, rollback, and diagnostics. It may sit behind
      Deployment Protection and must never be presented to a user.

  ``canonical_production_url``
      The project's own public default domain, resolved from authoritative
      Vercel project/domain state AFTER the production binding is confirmed.
      This is what gets smoke-checked, persisted as ``production_url``, and
      sent to the user.

After the site is LIVE and smoke-verified, the exact tested snapshot commit
of the approved revision is published to a friendly per-project branch in
the operator-configured source repository. That publication is strictly
after LIVE: a failure to publish never rolls production back and never
un-promotes a working site.

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
    # R1-B: the application-owned output Git repository holding the exact
    # tested snapshot commits, used to publish the LIVE source to the friendly
    # branch. None disables publication entirely (legacy behavior: no GitHub
    # side effect of any kind after LIVE).
    output_repo: Any = None
    # R1-B: the explicit, operator-configured source repository remote
    # (SSH form, e.g. git@github.com:owner/repo.git). Never read from
    # generated project files; the adapter revalidates the exact form.
    source_repo_url: Optional[str] = None
    # R1-B: optional callable(project_id, state, expected_name) -> Optional[str]
    # returning the friendly branch name for this project. The caller passes
    # the already-verified canonical Vercel project name (the bound slug), so
    # the branch and the canonical public host can never drift. None disables
    # publication.
    source_branch_for: Any = None
    # R1-B: optional path to the GitHub deploy key used for the push. It is
    # passed to git as GIT_SSH_COMMAND and is never persisted, logged, or
    # placed in a URL.
    source_ssh_key: Any = None


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
                 principal_id=None, reference_token=None,
                 recovery: bool = False) -> OperationResult:
        with self.store.acquire_writer(project_id) as state:
            try:
                require_owner_role(state, principal_id, reference_token)
            except AuthzError as exc:
                return OperationResult.fail(exc.error_code, error_code=exc.error_code)
        return self._promote_authorized(project_id, workspace, principal_id,
                                        reference_token, recovery=recovery)

    def _promote_authorized(self, project_id, workspace, principal_id=None,
                            reference_token=None, recovery: bool = False):
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

        # A crash mid-promotion leaves the project in PUBLISHING, and a
        # promotion that concluded as a failure leaves it in FAILED with the
        # durable intent intact. Re-entering the SAME operation (the approval's
        # operation_id matches the persisted promotion_intent's) is the intended
        # resume in both cases, NOT a new operation -- it reuses the durable
        # previous-production identity so the rollback target is never
        # recomputed against a drifted state.
        #
        # The FAILED arm is deliberately narrower: a FAILED project is only
        # resumable when the intent belongs to THIS approval, comes from a
        # promotion-phase failure, and carries the persisted previous-production
        # record a recovery needs. The PUBLISHING arm keeps its original,
        # looser gate so a pre-hardening intent with no ``previous_production``
        # key still reaches the lookup that fails it closed, instead of being
        # turned away at the door.
        _intent = state.deployment.get("promotion_intent") or {}
        _failure = state.failure or {}
        is_resume = (
            _intent.get("operation_id") == approval.get("operation_id")
            and (
                state.lifecycle == ProjectLifecycle.PUBLISHING.value
                or (
                    state.lifecycle == ProjectLifecycle.FAILED.value
                    and _failure.get("phase") == "promotion"
                    and "previous_production" in _intent
                )
            )
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
                # Current live result, not a new promotion. ``production_url``
                # is the canonical public URL persisted at LIVE; the
                # deployment-specific hostname is returned alongside it, but
                # it is internal identity only and is never what a user is
                # shown.
                return OperationResult.ok({
                    "production_url": state.production_url,
                    "deployment_url": last_live.get("deployment_url"),
                    "deployment_id": deployment_id,
                    "operation_id": operation_id,
                    "already_live": True,
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

        # ---- Same-operation resume: reconcile remote truth BEFORE issuing
        # any promote request, and never issue a second one for an operation
        # whose promotion may already have been applied.
        #
        # Two read-only passes, in order:
        #
        #  1. ADOPTION -- did this exact approved artifact already become
        #     production, whoever applied it (a crash after the remote call, or
        #     an operator promoting by hand with the Vercel CLI, which mints a
        #     new deployment id)? This is the only path that can reach LIVE
        #     with zero promote POSTs.
        #  2. ORDINARY RECONCILE -- is the intended deployment itself the
        #     production binding? If yes, adopt. If the provider CONCLUSIVELY
        #     reports this promotion as failed, fall through and promote
        #     normally. Anything ambiguous stops here, fail closed, with the
        #     intent intact and no promote sent.
        resume_identity = None
        resume_deployment_url = None
        if is_same_operation:
            decision, adopted, adopted_url = self._adopt_external_promotion(
                project_id, app_id, vercel_project, intended_identity, expected_name,
                recovery=recovery,
            )
            if decision == "unproven":
                return OperationResult.fail(
                    "PROMOTE_FAILED", error_code="PROMOTION_IDENTITY_UNPROVEN",
                )
            if decision == "adopted":
                resume_identity = adopted
                resume_deployment_url = adopted_url
            else:
                reconcile = self.deps.vercel.reconcile_production_deployment(
                    app_id, vercel_project, intended_identity, expected_name=expected_name,
                )
                status = (reconcile.data or {}).get("status") if reconcile.success else None
                if status == "PROMOTED":
                    resume_identity = intended_identity
                    resume_deployment_url = (reconcile.data or {}).get("deployment_url")
                elif status == "NOT_PROMOTED":
                    # Conclusively not promoted: fall through to the normal
                    # promote below (safe -- the provider itself reports this
                    # promotion job as terminally failed).
                    pass
                else:
                    # Ambiguous resume: do NOT blindly re-promote.
                    self._fail_reconciliation_required(
                        project_id, "PROMOTION_RECONCILIATION_REQUIRED",
                        intended_identity, "promote",
                    )
                    return OperationResult.fail(
                        "PROMOTION_RECONCILIATION_REQUIRED",
                        error_code="PROMOTION_RECONCILIATION_REQUIRED",
                    )

        if resume_identity is not None:
            resume_deployment_url = self._deployment_url_or_fallback(
                resume_deployment_url, resume_identity)
            logger.info(
                "Promotion confirmed deployment=%s (reconciled)",
                resume_identity["deployment_id"],
            )
            self._update_intent(
                project_id, operation_id, stage="promoted",
                deployment_url=resume_deployment_url,
            )
            return self._post_promote(
                project_id, workspace, app_id, vercel_project,
                previous_identity,
                deployment_url=resume_deployment_url,
                intended_identity=resume_identity,
                expected_name=expected_name, reconciled=True,
                approval=approval, source_revision=source_revision,
            )

        promote_result = self.deps.vercel.promote_deployment(
            app_id, vercel_project, deployment_id, operation_id,
            source_revision, artifact_sha256, expected_name=expected_name,
        )

        if not promote_result.success:
            error_code = promote_result.error_code or "PROMOTE_FAILED"
            # ``PROMOTE_RECONCILIATION_REQUIRED`` is the adapter's single
            # "ambiguous, do not re-promote" signal. Everything else is
            # TERMINAL and conclusive: ``PROMOTE_REJECTED`` (the provider
            # refused the request on its merits), ``PROMOTE_NOT_APPLIED`` (the
            # provider reports this promotion job as terminally failed) and
            # ``PROMOTE_FAILED`` (a created deployment reached a terminal build
            # state). Only the ambiguous class may be re-decided below.
            if error_code != "PROMOTE_RECONCILIATION_REQUIRED":
                self._fail(project_id, "PROMOTE_FAILED", error_code)
                return promote_result
            # ---- AMBIGUOUS or terminally-failed remote promote. The request
            # may have reached Vercel, or a promotion job may still be in
            # flight. NEVER treat ambiguity as a confirmed failure (Vercel may
            # already be serving the approved artifact, and a timed-out wait
            # does not stop the remote promotion) and NEVER blindly re-send the
            # promote. Re-read production truth and decide from the COMPLETE
            # identity tuple.
            reconcile = self.deps.vercel.reconcile_production_deployment(
                app_id, vercel_project, intended_identity, expected_name=expected_name,
            )
            status = (reconcile.data or {}).get("status") if reconcile.success else None
            if status == "PROMOTED":
                # Confirmed promoted -> continue the normal post-promote flow
                # (production smoke, then LIVE persistence).
                deployment_url = self._deployment_url_or_fallback(
                    (reconcile.data or {}).get("deployment_url"), intended_identity)
                logger.info("Promotion confirmed deployment=%s (reconciled)", deployment_id)
                self._update_intent(project_id, intended_identity["operation_id"],
                                    stage="promoted", deployment_url=deployment_url)
                return self._post_promote(
                    project_id, workspace, app_id, vercel_project,
                    previous_identity, deployment_url=deployment_url,
                    intended_identity=intended_identity,
                    expected_name=expected_name, reconciled=True,
                    approval=approval, source_revision=source_revision,
                )
            if status == "NOT_PROMOTED":
                # The provider CONCLUSIVELY reports this promotion as failed,
                # so the approved artifact provably never took over. A stale
                # binding on its own never reaches this branch: an ambiguous
                # promote resolves to PROMOTION_RECONCILIATION_REQUIRED below,
                # which keeps the intent intact and stays resumable.
                self._fail(project_id, "PROMOTE_FAILED", "PROMOTE_NOT_APPLIED")
                return OperationResult.fail(
                    "PROMOTE_FAILED", error_code="PROMOTE_NOT_APPLIED",
                )
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

        # Promote-by-creation mints a NEW deployment id that becomes the
        # production identity; the approved preview id stays in the intent for
        # provenance and rollback. An alias remap returns no new id, so this
        # falls back to the approved deployment itself.
        promoted_identity = dict(
            intended_identity,
            deployment_id=promote_result.data.get("promoted_deployment_id")
            or intended_identity["deployment_id"],
        )
        deployment_url = promote_result.data.get("production_url")
        logger.info("Promotion confirmed deployment=%s", promoted_identity["deployment_id"])
        self._update_intent(
            project_id, intended_identity["operation_id"], stage="promoted",
            deployment_url=deployment_url,
            promoted_deployment_id=promoted_identity["deployment_id"],
        )
        return self._post_promote(
            project_id, workspace, app_id, vercel_project, previous_identity,
            deployment_url=deployment_url, intended_identity=promoted_identity,
            expected_name=expected_name, reconciled=False,
            approval=approval, source_revision=source_revision,
        )

    def _adopt_external_promotion(self, project_id, app_id, vercel_project,
                                  intended_identity, expected_name, *,
                                  recovery: bool = False):
        """Reconcile remote truth for a SAME-OPERATION resume and adopt a
        promotion that has ALREADY been applied, with zero promote POSTs.

        Read-only. This covers both a crash after the remote call and a
        promotion an operator applied out of band -- notably the Vercel CLI's
        ``vercel promote``, which for a preview-target deployment promotes by
        CREATION and therefore mints a NEW deployment id that the approved
        deployment id alone cannot recognize.

        Returns ``(decision, identity, deployment_url)`` where decision is one of:

          ``"adopted"``  -- provider truth proves the approved artifact is
              live; ``identity`` is the promoted deployment's identity,
              ``deployment_url`` its deployment-specific host, and the caller
              continues to production smoke and LIVE persistence.
          ``"unproven"`` -- production moved, but nothing ties it to the
              approved artifact (for example a deployment created without our
              identity and for which the provider kept no lineage record).
              Terminal for this attempt and deliberately non-destructive: the
              durable intent is left intact and nothing remote is touched. This
              is a RECOVERY-only verdict (``recovery=True``): during an ordinary
              publish or a first-attempt resume, "production is bound to
              something that is not (yet) our deployment" is the normal
              pre-promote state and simply falls through to the ordinary
              reconcile below, so it is never fatal there.
          ``"pending"``  -- not promoted, or truth unreadable. The caller falls
              through to the ordinary same-operation reconcile.
        """
        external = self.deps.vercel.reconcile_external_promotion(
            app_id, vercel_project, intended_identity, expected_name=expected_name,
        )
        status = (external.data or {}).get("status") if external.success else None
        if status == "PROMOTED":
            data = external.data or {}
            adopted = data.get("deployment_id")
            if not isinstance(adopted, str) or not adopted:
                return "pending", None, None
            identity = dict(intended_identity, deployment_id=adopted)
            logger.info(
                "Promotion adopted deployment=%s proof=%s project=%s",
                adopted, data.get("proof"), project_id,
            )
            self._update_intent(
                project_id, intended_identity["operation_id"], stage="promoted",
                promoted_deployment_id=adopted,
                deployment_url=data.get("production_url"),
            )
            return "adopted", identity, data.get("production_url")
        if status == "PROMOTED_UNPROVEN":
            if not recovery:
                # Not a recovery: production simply is not (yet) the approved
                # deployment. That is the normal pre-promote state, so fall
                # through to the ordinary reconcile instead of failing an
                # ordinary publish.
                return "pending", None, None
            logger.warning(
                "Promotion identity unproven project=%s binding=%s",
                project_id, (external.data or {}).get("deployment_id"),
            )
            self._fail(
                project_id, "PROMOTE_FAILED", "PROMOTION_IDENTITY_UNPROVEN",
            )
            return "unproven", None, None
        return "pending", None, None

    def resume_publish(self, project_id: str, workspace: Path,
                       principal_id: Optional[str] = None,
                       reference_token: Optional[str] = None) -> OperationResult:
        """Operator-facing SAME-OPERATION recovery for a project whose publish
        already failed mid-promotion.

        This is the only way to reach the resume branch, and it refuses unless
        the durable ``promotion_intent`` belongs to the current approval,
        carries the persisted previous-production record, and the lifecycle is
        PUBLISHING or a promotion-phase FAILED. A first publish can therefore
        never be confused with a recovery, and a recovery can never invent a
        new operation.

        Recovery is READ-ONLY against the provider until it is proven that the
        promotion has NOT been applied: remote truth is reconciled first, and
        an already-applied promotion is adopted (production smoke, then LIVE)
        with zero promote requests. Only a conclusive "not promoted" verdict
        lets the normal promote run.

        Owner-only, exactly like ``promote``: a reviewer may approve a preview
        but must not be able to publish or recover one.
        """
        state = self.store.load(project_id)
        if state is None:
            return OperationResult.fail("NO_PROJECT_STATE", error_code="NO_PROJECT_STATE")
        intent = state.deployment.get("promotion_intent") or {}
        approval = state.deployment.get("approval") or {}
        failure = state.failure or {}
        last_live = state.deployment.get("last_live_deployment") or {}
        resumable = (
            bool(approval.get("operation_id"))
            and intent.get("operation_id") == approval.get("operation_id")
            and "previous_production" in intent
            and (
                state.lifecycle == ProjectLifecycle.PUBLISHING.value
                or (
                    state.lifecycle == ProjectLifecycle.FAILED.value
                    and failure.get("phase") == "promotion"
                )
                # An operator re-invoking a recovery that already reached LIVE
                # is a duplicate, not a new recovery. The exact-identity match
                # routes it into the existing idempotent no-op; a LIVE project
                # with any other identity is refused below.
                or (
                    state.lifecycle == ProjectLifecycle.LIVE.value
                    and last_live.get("operation_id") == approval.get("operation_id")
                )
            )
        )
        if not resumable:
            # Refuse BEFORE any remote call and before acquiring the worker
            # slot: a normal publish, or a failure from another phase, is not
            # a recovery and must keep its own error surface.
            return OperationResult.fail(
                "RESUME_NOT_APPLICABLE", error_code="RESUME_NOT_APPLICABLE",
            )
        # ``recovery=True`` so that production bound to a deployment this
        # operation cannot prove is fatal HERE, where the whole point is to
        # decide whether the remote state may be adopted, rather than being
        # folded into the ordinary pre-promote "not live yet" state.
        if not self.runner.acquire_project(project_id):
            return OperationResult.fail(
                "WORKER_BUSY", error_code="WORKER_BUSY", retryable=True,
            )
        try:
            return self._promote(
                project_id, workspace, principal_id, reference_token,
                recovery=True,
            )
        finally:
            self.runner.release_project(project_id)

    def _post_promote(
        self, project_id, workspace, app_id, vercel_project, previous_identity,
        deployment_url, intended_identity, expected_name, reconciled,
        approval, source_revision,
    ) -> OperationResult:
        """Post-promote flow after a CONFIRMED promote (direct or reconciled).

        Order is load-bearing and is the R1 contract:

            resolve canonical public URL -> smoke THAT url -> LIVE (persist
            canonical + deployment URLs) -> publish the exact tested source
            to the friendly branch -> send the canonical LIVE URL.

        On smoke failure the alias is rolled back to the persisted previous
        production and the EXACT observed rollback outcome is persisted.
        """
        deployment_id = intended_identity["deployment_id"]
        operation_id = intended_identity["operation_id"]
        artifact_sha256 = intended_identity["artifact_sha256"]

        # ---- 1. Resolve the CANONICAL public production URL, now that the
        # production binding is confirmed. This is the URL the user will see
        # and the URL that must be proven reachable, so it is resolved BEFORE
        # the smoke check rather than after it.
        canonical = self._resolve_canonical(app_id, vercel_project, expected_name)
        if canonical is None:
            # Fail closed, and do it through the ordinary promotion-phase
            # failure record: that keeps the durable ``promotion_intent``
            # (including its previous-production identity) intact, which is
            # exactly what makes this SAME operation resumable. Deliberately
            # NOT a rollback: the remote promotion is confirmed good and the
            # site may well be serving; only our own URL resolution failed.
            # Rolling back a working site over a local formatting problem
            # would be a worse outcome than the problem.
            self._fail(
                project_id, "CANONICAL_PRODUCTION_URL_UNRESOLVED",
                "CANONICAL_PRODUCTION_URL_UNRESOLVED",
            )
            return OperationResult.fail(
                "CANONICAL_PRODUCTION_URL_UNRESOLVED",
                error_code="CANONICAL_PRODUCTION_URL_UNRESOLVED",
            )
        logger.info(
            "Canonical production URL resolved project=%s source=%s",
            project_id, canonical.get("canonical_source"),
        )

        # ---- 2. Mandatory production smoke check, against the CANONICAL
        # url. The deployment-specific hostname may sit behind Deployment
        # Protection and is never a meaningful health check of what users
        # actually load.
        smoke_dir = (self.smoke_dir_root or workspace) / "qa" / "production_smoke"
        smoke_result = self._run_production_smoke(
            canonical["canonical_production_url"], smoke_dir,
            (vercel_project or {}).get("id"),
        )
        self._update_intent(project_id, operation_id, stage="smoked",
                            smoke=smoke_result.data)
        if not smoke_result.success:
            logger.warning(
                "Production smoke FAILED project=%s", project_id,
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

        # ---- 3. Only now transition PUBLISHING -> LIVE.
        with self.store.acquire_writer(project_id) as locked:
            if (
                locked.deployment.get("approval") != approval
                or locked.lifecycle != ProjectLifecycle.PUBLISHING.value
            ):
                return OperationResult.fail("STALE_APPROVAL", error_code="STALE_APPROVAL")
            self.store.transition_lifecycle_locked(locked, ProjectLifecycle.LIVE)
            locked.revisions.live_revision = source_revision
            # The USER-FACING production URL is the canonical public one. The
            # deployment-specific hostname is persisted next to it for
            # reconciliation, rollback and diagnostics, and is never sent.
            locked.production_url = canonical["canonical_production_url"]
            # A recovery resume arrives with the failure record of the attempt
            # that failed. LIVE is the outcome, so that record is now stale and
            # must not be left behind for an operator to misread.
            locked.failure = None
            intent = locked.deployment["promotion_intent"]
            intent["stage"] = "live"
            # Both URLs are recorded, each under its own name. The intent is
            # an internal record, so keeping the two keys distinct is what
            # stops a future reader from mistaking one for the other.
            intent["canonical_production_url"] = canonical["canonical_production_url"]
            intent["deployment_url"] = deployment_url
            locked.deployment["last_live_deployment"] = {
                "operation_id": operation_id,
                "deployment_id": deployment_id,
                # Canonical public URL: the one reported to the user.
                "production_url": canonical["canonical_production_url"],
                # Deployment-specific Vercel hostname: internal identity only.
                "deployment_url": deployment_url,
                "source_revision": source_revision,
                "source_sha256": approval["source_sha256"],
                "artifact_sha256": artifact_sha256,
                "live_at": time.time(),
            }
            self.store.save(locked)
            state = locked

        # ---- 4. Publish the EXACT tested source of this LIVE revision to the
        # friendly branch. Strictly after LIVE, and strictly non-destructive:
        # a publication failure leaves the site live and records that a source
        # sync is still required.
        sync_status = self._publish_live_source(
            project_id, state, expected_name, source_revision, approval,
        )

        # ---- 5. Telegram notification of LIVE promotion (best-effort in the
        # sense that a failed notification does not un-promote; the site
        # is already live and smoke-verified at this point).
        logger.info("Project LIVE production_url=%s", canonical["canonical_production_url"])
        chat_id = self.deps.chat_id_for(project_id, state)
        if chat_id:
            self.deps.telegram.send_text(
                chat_id, f"🚀 Live: {canonical['canonical_production_url']}"
            )

        return OperationResult.ok({
            "production_url": canonical["canonical_production_url"],
            "deployment_url": deployment_url,
            "deployment_id": deployment_id,
            "operation_id": operation_id,
            "reconciled": reconciled,
            "source_sync": sync_status,
        })

    # ------------------------------------------------------------------
    # Canonical public production URL
    # ------------------------------------------------------------------

    def _resolve_canonical(self, app_id, vercel_project, expected_name):
        """The canonical public production URL, or None when unresolvable.

        Delegates to the adapter, which proves project ownership and then
        reads authoritative project/domain state, falling back to the
        deterministic ``https://<verified name>.vercel.app/`` form. A
        collaborator that predates the helper (or raises) is treated as
        unresolvable rather than silently degrading to a deployment hostname:
        there is no honest URL to return in that case.
        """
        helper = getattr(self.deps.vercel, "canonical_production_url", None)
        if not callable(helper):
            return None
        try:
            result = helper(app_id, vercel_project, expected_name=expected_name)
        except Exception:
            return None
        if not getattr(result, "success", False):
            return None
        data = result.data or {}
        url = data.get("canonical_production_url")
        if (not isinstance(url, str) or not url.startswith("https://")
                or "vercel.app" not in url or url.rstrip("/") == "https://vercel.app"):
            return None
        return data

    @staticmethod
    def _deployment_url_or_fallback(observed, identity):
        """The deployment-specific URL for internal records.

        Prefers the real host the adapter read from the provider. Falls back
        to the deployment-id-derived form ONLY for diagnostics, and that value
        is never smoke-tested, never persisted as ``production_url``, and
        never sent to a user.
        """
        if isinstance(observed, str) and observed.startswith("https://"):
            return observed
        return PromotionOrchestrator._production_url_for(identity)

    # ------------------------------------------------------------------
    # LIVE source publication (friendly branch)
    # ------------------------------------------------------------------

    def _publish_live_source(self, project_id, state, expected_name,
                             source_revision, approval):
        """Publish the exact tested snapshot commit of this LIVE revision.

        Returns one of ``"SYNCED"``, ``"SOURCE_SYNC_REQUIRED"`` or ``None``
        (not attempted: publication is not configured for this installation).

        Never rolls production back, never re-smokes, never un-transitions
        LIVE. A failure is recorded in ``state.repository`` so an operator can
        see that the published branch is behind the live site.
        """
        repo = self.deps.output_repo
        url = self.deps.source_repo_url
        if repo is None or not url:
            return None
        branch = None
        if self.deps.source_branch_for is not None:
            try:
                branch = self.deps.source_branch_for(project_id, state, expected_name)
            except Exception:
                branch = None
        if not branch:
            logger.warning(
                "LIVE source publication skipped project=%s: no resolvable branch",
                project_id,
            )
            return None
        ssh_key = self.deps.source_ssh_key
        if not ssh_key:
            # Not configured, not attempted, not a failure: an operator who
            # has not installed a deploy key yet must not see every publish
            # reported as an unsynced source.
            logger.warning(
                "LIVE source publication skipped project=%s branch=%s: "
                "no GitHub deploy key configured",
                project_id, branch,
            )
            return None

        # The commit that must be published: the exact TestedSnapshot commit
        # the approved revision produced. Never re-derived from the mutable
        # workspace, never recomputed.
        try:
            git_identity = self._tested_commit_for(
                state, approval.get("operation_id"), approval=approval)
        except ValueError as exc:
            logger.error(
                "LIVE source publication FAILED project=%s branch=%s (no trusted "
                "tested commit: %s)", project_id, branch, exc,
            )
            self._record_source_sync_required(project_id, branch, "NO_TRUSTED_TESTED_COMMIT")
            return "SOURCE_SYNC_REQUIRED"
        previous = (state.repository or {}).get("publication_commit")
        try:
            published = repo.publish_project_branch(
                git_identity["commit"], branch, url,
                source_revision=source_revision,
                source_sha256=approval.get("source_sha256"),
                artifact_sha256=approval.get("artifact_sha256"),
                previous_publication_commit=previous,
                ssh_key=ssh_key,
            )
        except Exception as exc:
            # Only the exception TYPE is logged/persisted: a git failure's
            # message can embed the remote and local filesystem paths.
            logger.error(
                "LIVE source publication FAILED project=%s branch=%s (type=%s)",
                project_id, branch, type(exc).__name__,
            )
            self._record_source_sync_required(
                project_id, branch, "GITHUB_PUBLICATION_FAILED",
                tested_commit=git_identity["commit"])
            return "SOURCE_SYNC_REQUIRED"

        self._record_source_sync(
            project_id, branch,
            tested_commit=git_identity["commit"],
            publication_commit=published["publication_commit"],
            source_revision=source_revision,
        )
        logger.info(
            "LIVE source published project=%s branch=%s tested=%s publication=%s",
            project_id, branch, git_identity["commit"],
            published["publication_commit"],
        )
        return "SYNCED"

    @staticmethod
    def _tested_commit_for(state, operation_id, approval=None):
        """The exact TestedSnapshot commit bound to this approved preview.

        Read from the preview operation intent and cross-checked against the
        approval's own source/artifact hashes. Any disagreement means the
        commit we would push is not provably the approved artifact, so it is
        refused rather than pushed on trust.
        """
        intent = (state.deployment or {}).get("preview_intent") or {}
        git_identity = intent.get("git") or {}
        commit = git_identity.get("commit")
        if intent.get("operation_id") != operation_id or not commit:
            raise ValueError("preview intent does not belong to this operation")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("tested commit is not a commit id")
        if approval is not None:
            if git_identity.get("source_sha256") != approval.get("source_sha256"):
                raise ValueError("tested commit source hash does not match approval")
            if git_identity.get("artifact_sha256") != approval.get("artifact_sha256"):
                raise ValueError("tested commit artifact hash does not match approval")
        return git_identity

    def _record_source_sync(self, project_id, branch, *, tested_commit,
                            publication_commit, source_revision):
        with self.store.acquire_writer(project_id) as state:
            state.repository = {
                "provider": "github",
                "repo": self._source_repo_name(),
                "branch": branch,
                "tested_commit": tested_commit,
                "publication_commit": publication_commit,
                "source_revision": source_revision,
                "sync_status": "SYNCED",
                "synced_at": time.time(),
            }
            self.store.save(state)

    def _source_repo_name(self):
        """``owner/name`` for the configured remote, or None.

        Derived from the operator-configured remote so the persisted record
        names the repository without carrying a scheme, a host, or a key path.
        """
        url = self.deps.source_repo_url
        if not isinstance(url, str):
            return None
        match = re.fullmatch(r"git@github\.com:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\.git", url)
        return f"{match.group(1)}/{match.group(2)}" if match else None

    def _record_source_sync_required(self, project_id, branch, error_code,
                                     tested_commit=None):
        """Record that the published branch is behind the live site.

        ``tested_commit`` is the revision's source that SHOULD be in the
        branch, which is what an operator needs to reconcile. No
        ``publication_commit`` is written, because none was confirmed to
        exist on the remote, and the previous one is deliberately PRESERVED:
        it is the parent authority for the next publication and names the
        last revision that genuinely reached the remote.
        """
        with self.store.acquire_writer(project_id) as state:
            record = dict(state.repository or {})
            record.update({
                "provider": "github",
                "repo": self._source_repo_name() or record.get("repo"),
                "branch": branch,
                "sync_status": "SOURCE_SYNC_REQUIRED",
                "last_error_code": error_code,
                "recorded_at": time.time(),
            })
            if tested_commit:
                record["tested_commit"] = tested_commit
            state.repository = record
            self.store.save(state)

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
