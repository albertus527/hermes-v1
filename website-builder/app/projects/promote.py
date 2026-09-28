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
  * R2: publishing the EXACT tested source to the friendly per-project branch
    BEFORE any production side effect, so a release that cannot be published
    never touches production at all. The Git branch is a publication HISTORY;
    what is LIVE is decided by ``deployment.last_live_release`` in
    application state and never inferred from a branch head,
  * R2: strict A/B/C/D reconciliation of the publication head on resume and on
    a rejected push. A conflicting remote head is reported, never re-parented
    onto; an unreadable remote fails closed,
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

The R2 publication stage order is a single linear graph, enforced in
``app.projects.release``:

    PREPARED -> GIT_CONFIRMED -> PRODUCTION_CONFIRMED -> SMOKE_PASSED -> COMMITTED

Publication-not-configured is a *status*, not a shortcut past GIT_CONFIRMED:
the machine still advances through stage 2, as a local no-op with zero Git
subprocesses, so resume and recovery have one code path.

The reorder is a deliberate inversion of R1, which published after LIVE and
therefore kept the site live even when Git was unreachable. A Git failure now
means production is never touched. That is the point: the branch and the live
release can no longer disagree without the disagreement being *recorded*, and
a revision that was confirmed on Git but never reached production is
explicitly visible as such rather than inferred from a branch head.

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
from urllib.parse import urlsplit

from app.core.authz import AuthzError, require_mutating_role, require_owner_role
from app.core.contracts import OperationResult, StaleOperationIntent
from app.core.lifecycle import LifecycleError, ProjectLifecycle
from app.core.state import ProjectStateStore
from app.projects import release as release_contract
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


def _release_smoke_evidence(raw: dict, status: str, requested_url: str) -> Optional[dict]:
    """The smoke evidence a release record carries, or None when it is not
    provably about the URL we checked.

    The target host and path are derived from ``requested_url`` -- the URL
    THIS call actually passed to the smoke collaborator -- rather than trusted
    from the collaborator's own report. A collaborator that cannot say which
    host it exercised is not evidence, and a collaborator that reports a
    DIFFERENT host than the one we asked about is a contract violation, so
    both fail closed here instead of being recorded as a passed release.

    Failure evidence is passed through with whatever target the collaborator
    reported: it is diagnostics, and inventing a target for a failure would be
    a guess about what was being tested when it failed.
    """
    if status != "PASSED":
        return {
            "status": status,
            "at": raw.get("at"),
            "target_host": raw.get("target_host"),
            "target_path": raw.get("target_path"),
            "failure_classification": raw.get("failure_classification"),
        }
    host = urlsplit(requested_url).hostname
    path = urlsplit(requested_url).path or "/"
    if not host:
        return None
    reported = raw.get("target_host")
    if isinstance(reported, str) and reported and reported != host:
        return None
    return {
        "status": "PASSED",
        "at": raw.get("at"),
        "target_host": host,
        "target_path": path,
        "failure_classification": None,
    }


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
    # side effect of any kind).
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
    # R2: the durable release-contract writer (stage advances, the single
    # COMMITTED write, failure bookkeeping). Constructed from the same store
    # when omitted, so a caller that never heard of R2 still gets the R2
    # contract rather than a silently weaker one.
    releases: Any = None


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
        self.releases = deps.releases or release_contract.ReleaseCoordinator(store)

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
        is_same_operation = _intent.get("operation_id") == approval.get("operation_id")
        is_resume = (
            is_same_operation
            and (
                state.lifecycle == ProjectLifecycle.PUBLISHING.value
                or (
                    state.lifecycle == ProjectLifecycle.FAILED.value
                    and _failure.get("phase") == "promotion"
                    and "previous_production" in _intent
                )
            )
        )

        # R2 supersede guard, before any side effect: a NEW operation may not
        # begin while an earlier operation's publication is held for
        # reconciliation. That record describes a remote state this system
        # could not read or could not reconcile; starting a different operation
        # would publish against a branch head that may have moved, which is
        # exactly the ambiguity the record exists to hold open. A terminal
        # pending publication is supersedable and is replaced by the new
        # PREPARED record.
        #
        # Deliberately AFTER the same-operation test, and not a refusal for a
        # resume: holding the record open is what makes the same operation
        # resumable, so blocking the resume too would strand the project with
        # no forward path at all.
        if not is_same_operation and self.releases.is_reconciliation_required(state):
            pending = self.releases.pending(state) or {}
            logger.warning(
                "Publish refused project=%s: publication held for reconciliation "
                "operation=%s code=%s",
                project_id, pending.get("operation_id"), pending.get("last_error_code"),
            )
            return OperationResult.fail(
                release_contract.ERROR_SUPERSEDE_FORBIDDEN,
                error_code=release_contract.ERROR_SUPERSEDE_FORBIDDEN,
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
        #
        # Matched on ``last_live_release`` when one exists, because that is
        # the authoritative LIVE release. ``last_live_deployment`` stays the
        # fallback for a pre-upgrade project whose release record is a lazily
        # derived LEGACY_PARTIAL one; that payload is returned labelled as
        # partial and is never dressed up as a full release identity.
        if state.lifecycle == ProjectLifecycle.LIVE.value:
            release = state.deployment.get("last_live_release")
            if not isinstance(release, dict) or release.get("operation_id") != operation_id:
                release = None
            elif release.get("completeness") != release_contract.COMPLETENESS_COMPLETE:
                # A lazily derived LEGACY_PARTIAL identity is a real previous
                # release, so it is still a valid match -- but it is never
                # dressed up as a full release identity.
                if release.get("completeness") == \
                        release_contract.COMPLETENESS_LEGACY_PARTIAL:
                    release = dict(release)
                else:
                    release = None
            last_live = state.deployment.get("last_live_deployment") or {}
            identity_matches = (
                release is not None
                or (
                    not isinstance(state.deployment.get("last_live_release"), dict)
                    and last_live.get("operation_id") == operation_id
                    and last_live.get("deployment_id") == deployment_id
                    and last_live.get("source_revision") == source_revision
                )
            )
            if identity_matches:
                # Current live result, not a new promotion. ``production_url``
                # is the canonical public URL persisted at LIVE; the
                # deployment-specific hostname is returned alongside it, but
                # it is internal identity only and is never what a user is
                # shown.
                return OperationResult.ok({
                    "production_url": state.production_url,
                    "deployment_url": (release or last_live).get("deployment_url"),
                    "deployment_id": deployment_id,
                    "operation_id": operation_id,
                    "already_live": True,
                    "release": release,
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

            # R2: the pending publication is written in the SAME locked write
            # as the promotion intent, because both describe one operation and
            # either alone is an incomplete record. The branch is resolved
            # HERE, at PREPARED, rather than after LIVE: the friendly branch
            # name belongs in the durable publication intent, and a crash after
            # the push must not lose it.
            #
            # A same-operation resume with an INTACT pending record keeps it
            # exactly as it is -- its stage is how far the previous process
            # actually got, and rebuilding it would both lose that and forge a
            # second intended commit. A same-operation resume with NO pending
            # record is a pre-upgrade state (R1 never wrote one); it gets a
            # PREPARED record, and the git stage reconciles from there.
            existing_pending = locked.deployment.get("pending_publication")
            prepare_failed = False
            if not (is_same_operation
                    and isinstance(existing_pending, dict)
                    and existing_pending.get("operation_id") == operation_id):
                try:
                    locked.deployment["pending_publication"] = self._prepare_publication(
                        locked, expected_name, operation_id, source_revision, approval,
                    )
                except (release_contract.ReleaseRecordError, ValueError):
                    # No trusted tested commit, or an unbuildable intended
                    # commit: the branch is never advanced and no production
                    # side effect has happened. There is no pending record to
                    # leave behind -- there is nothing that was ever intended.
                    locked.deployment.pop("pending_publication", None)
                    prepare_failed = True
            self.store.save(locked)
            if prepare_failed:
                # The lifecycle failure is written AFTER the lock is released:
                # ``_fail`` takes the same per-project writer lock, which is a
                # file lock and not reentrant.
                pass
            else:
                # Report the classification this intent ACTUALLY carries: on a
                # same-operation resume the persisted one is authoritative,
                # because the fresh lookup describes post-crash remote state,
                # not the rollback target the operation was started against.
                intent_class = (
                    (locked.deployment.get("promotion_intent") or {}).get(
                        "previous_production_class"
                    ) or previous_class
                )

                # The exact trusted identity of the deployment we intended to
                # promote. Reconciliation compares the COMPLETE tuple -- never
                # a URL or a bare deployment_id.
                intended_identity = {
                    "deployment_id": deployment_id,
                    "operation_id": operation_id,
                    "source_revision": source_revision,
                    "artifact_sha256": artifact_sha256,
                }

        if prepare_failed:
            self._fail(
                project_id, "PUBLICATION_PREPARE_FAILED",
                release_contract.ERROR_NO_TRUSTED_TESTED_COMMIT,
            )
            return OperationResult.fail(
                release_contract.ERROR_NO_TRUSTED_TESTED_COMMIT,
                error_code=release_contract.ERROR_NO_TRUSTED_TESTED_COMMIT,
            )

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

        # ---- R2 GIT STAGE. The exact tested source is published to the
        # friendly branch BEFORE any Vercel call. This is the batch's central
        # inversion of R1: a publication that cannot be proven now means
        # production is never touched, instead of going live with the branch
        # silently behind.
        git_result = self._confirm_git(project_id, operation_id)
        if not git_result.success:
            return git_result

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
                    #
                    # The pending publication is held exactly as the parallel
                    # ambiguous-promote path below holds it. This branch used to
                    # record only the promotion failure, which left
                    # ``reconciliation_required`` False and let a NEW operation
                    # supersede a publication the state machine says is
                    # unresolved -- the same hold, on a path that reached the
                    # same conclusion by a different route.
                    self._mark_publication_reconciliation_required(
                        project_id, operation_id,
                        "PROMOTION_RECONCILIATION_REQUIRED",
                    )
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
                # The Git stage already completed, so the record knows how far
                # this operation got and where it stopped.
                self._mark_publication_terminal_failure(
                    project_id, operation_id, error_code)
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
                self._mark_publication_terminal_failure(
                    project_id, operation_id, "PROMOTE_NOT_APPLIED")
                self._fail(project_id, "PROMOTE_FAILED", "PROMOTE_NOT_APPLIED")
                return OperationResult.fail(
                    "PROMOTE_FAILED", error_code="PROMOTE_NOT_APPLIED",
                )
            # Still ambiguous / lookup failed / identity incomplete:
            # preserve PUBLISHING + the intact promotion_intent and fail
            # closed as reconciliation-required. A later same-operation
            # resume reconciles again and reuses the SAME intent (no
            # second promote POST). The pending publication is held the same
            # way, so a NEW operation cannot supersede it either.
            self._mark_publication_reconciliation_required(
                project_id, operation_id, "PROMOTION_RECONCILIATION_REQUIRED",
            )
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

        Authorization is checked BEFORE the resumability gate. Refusing an
        unauthorized caller with "not applicable" instead of "unauthorized"
        would both skip the gate and tell an unauthorized principal whether
        this project is mid-publish.
        """
        state = self.store.load(project_id)
        if state is None:
            return OperationResult.fail("NO_PROJECT_STATE", error_code="NO_PROJECT_STATE")
        try:
            require_owner_role(state, principal_id, reference_token)
        except AuthzError as exc:
            return OperationResult.fail(exc.error_code, error_code=exc.error_code)
        intent = state.deployment.get("promotion_intent") or {}
        approval = state.deployment.get("approval") or {}
        failure = state.failure or {}
        last_live = state.deployment.get("last_live_deployment") or {}
        # R2: a recovery must carry an INTACT pending publication. Without one
        # there is no record of which commit was intended for the branch or how
        # far the Git stage got, so there is nothing to reconcile and nothing
        # to resume.
        #
        # The one exception is a project already LIVE for this operation: the
        # COMMITTED write deliberately clears the pending record, so its
        # absence there means "this recovery already finished", not "this
        # recovery cannot be described". That duplicate routes into the
        # idempotent no-op below, which is where a repeated operator recovery
        # belongs.
        pending = self.releases.pending(state)
        pending_intact = bool(
            pending and pending.get("operation_id") == approval.get("operation_id"))
        already_live = (
            state.lifecycle == ProjectLifecycle.LIVE.value
            and last_live.get("operation_id") == approval.get("operation_id")
        )
        resumable = (
            bool(approval.get("operation_id"))
            and intent.get("operation_id") == approval.get("operation_id")
            and "previous_production" in intent
            and (pending_intact or already_live)
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
                or already_live
            )
        )
        if not resumable:
            # Refuse BEFORE any remote call and before acquiring the worker
            # slot: a normal publish, or a failure from another phase, is not
            # a recovery and must keep its own error surface.
            return OperationResult.fail(
                "RESUME_NOT_APPLICABLE", error_code="RESUME_NOT_APPLICABLE",
            )
        # ``recovery=True`` only when this operation actually reached the
        # production stage. That flag turns "production is bound to a
        # deployment this operation cannot attribute" from the normal
        # pre-promote state into a fatal one, which is right when a promote
        # request may already be in flight and wrong when the Git stage failed
        # first: the pending record proves no provider side effect was ever
        # possible, so there is nothing ambiguous to protect against.
        try:
            stage = (pending or {}).get("stage")
            reached_production = (
                stage is not None
                and release_contract.at_least(
                    stage, release_contract.STAGE_PRODUCTION_CONFIRMED)
            )
        except release_contract.ReleaseStageError:
            reached_production = False
        if not self.runner.acquire_project(project_id):
            return OperationResult.fail(
                "WORKER_BUSY", error_code="WORKER_BUSY", retryable=True,
            )
        try:
            return self._promote(
                project_id, workspace, principal_id, reference_token,
                recovery=reached_production,
            )
        finally:
            self.runner.release_project(project_id)

    def _post_promote(
        self, project_id, workspace, app_id, vercel_project, previous_identity,
        deployment_url, intended_identity, expected_name, reconciled,
        approval, source_revision,
    ) -> OperationResult:
        """Post-promote flow after a CONFIRMED promote (direct or reconciled).

        Order is load-bearing and is the R2 contract. The Git stage already
        happened BEFORE ``promote_deployment`` was called (see
        ``_confirm_git``); what remains is:

            resolve canonical public URL -> smoke THAT url -> SMOKE_PASSED ->
            one atomic COMMITTED write -> send the canonical LIVE URL.

        On smoke failure the alias is rolled back to the persisted previous
        production and the EXACT observed rollback outcome is persisted. The
        Git branch is never rolled back: it is durable history, so after a
        smoke failure the branch legitimately holds this release while
        ``last_live_release`` still describes the previous one.

        Every stage step below is CONDITIONAL on how far the record already got.
        A crash between any two of them leaves the durable record ahead of this
        process, and re-running a step the record has already completed is how
        an operation used to become unrecoverable: ``ensure_stage`` correctly
        refuses to walk a stage backwards, so an unconditional
        ``PRODUCTION_CONFIRMED`` call turned a crash after ``SMOKE_PASSED``
        into a permanent ``PROMOTION_STAGE_LOST``. The stage graph itself is
        untouched; the orchestration simply resumes from where the record says
        it is, which is what a stage is for.
        """
        deployment_id = intended_identity["deployment_id"]
        operation_id = intended_identity["operation_id"]
        artifact_sha256 = intended_identity["artifact_sha256"]

        # ---- 0. How far did an earlier process actually get? The record is
        # the only honest answer: a remote side effect that happened without a
        # durable record of it must be re-derived, never re-issued.
        stage_state = self.store.load(project_id)
        if stage_state is None:
            return OperationResult.fail("NO_PROJECT_STATE", error_code="NO_PROJECT_STATE")
        pending = self.releases.pending(stage_state) or {}
        if pending.get("operation_id") != operation_id:
            self._fail(project_id, "PROMOTE_FAILED", "PROMOTION_STAGE_LOST")
            return OperationResult.fail("PROMOTE_FAILED", error_code="PROMOTION_STAGE_LOST")
        try:
            reached = release_contract.stage_index(pending.get("stage"))
        except release_contract.ReleaseStageError:
            reached = -1
        production_already_confirmed = reached >= release_contract.stage_index(
            release_contract.STAGE_PRODUCTION_CONFIRMED)
        smoke_already_passed = reached >= release_contract.stage_index(
            release_contract.STAGE_SMOKE_PASSED)

        # ---- 1. PRODUCTION_CONFIRMED. The exact intended artifact is now
        # bound to the production alias, so the stage advances before any
        # local work that can fail. Skipped when the record is already at or
        # past it -- the promote really did happen, and the evidence for it is
        # already durable.
        if not production_already_confirmed:
            try:
                self.releases.ensure_stage(
                    project_id, operation_id, release_contract.STAGE_PRODUCTION_CONFIRMED,
                    production={
                        "deployment_id": deployment_id,
                        "promoted_deployment_id": deployment_id,
                        "confirmed_at": time.time(),
                    },
                )
            except (StaleOperationIntent, release_contract.ReleaseStageError):
                self._fail(project_id, "PROMOTE_FAILED", "PROMOTION_STAGE_LOST")
                return OperationResult.fail("PROMOTE_FAILED", error_code="PROMOTION_STAGE_LOST")

        # ---- 2. Resolve the CANONICAL public production URL, now that the
        # production binding is confirmed. This is the URL the user will see
        # and the URL that must be proven reachable, so it is resolved BEFORE
        # the smoke check rather than after it.
        #
        # Re-drive / recovery: promotion_intent is the sole durable authority.
        # If the verified canonical URL for this exact deployment_id is already
        # recorded in promotion_intent, reuse it rather than re-resolving.
        state_for_intent = self.store.load(project_id)
        intent = (state_for_intent.deployment.get("promotion_intent") or {}) if state_for_intent else {}
        recorded_url = intent.get("canonical_production_url")
        recorded_dep_id = intent.get("canonical_deployment_id")

        if (recorded_url and isinstance(recorded_url, str)
                and recorded_dep_id == intended_identity["deployment_id"]):
            host = intent.get("canonical_host") or urlsplit(recorded_url).hostname
            canonical = {
                "canonical_production_url": recorded_url,
                "canonical_source": intent.get("canonical_source", "VERCEL_PRODUCTION_ALIAS"),
                "canonical_host": host,
                "project_name": expected_name,
            }
            logger.info(
                "Canonical production URL reused from intent project=%s source=%s url=%s host=%s",
                project_id, canonical.get("canonical_source"),
                canonical.get("canonical_production_url"), host,
            )
        else:
            canonical = self._resolve_canonical(
                app_id, vercel_project, expected_name,
                deployment_id=intended_identity["deployment_id"],
            )
            if canonical is None:
                # Fail closed, and do it through the ordinary promotion-phase
                # failure record: that keeps the durable ``promotion_intent``
                # (including its previous-production identity) intact, which is
                # exactly what makes this SAME operation resumable. Deliberately
                # NOT a rollback: the remote promotion is confirmed good and the
                # site may well be serving; only our own URL resolution failed.
                # Rolling back a working site over a local formatting problem
                # would be a worse outcome than the problem.
                self._mark_publication_terminal_failure(
                    project_id, operation_id,
                    "CANONICAL_PRODUCTION_URL_UNRESOLVED",
                )
                self._fail(
                    project_id, "CANONICAL_PRODUCTION_URL_UNRESOLVED",
                    "CANONICAL_PRODUCTION_URL_UNRESOLVED",
                )
                return OperationResult.fail(
                    "CANONICAL_PRODUCTION_URL_UNRESOLVED",
                    error_code="CANONICAL_PRODUCTION_URL_UNRESOLVED",
                )
            host = canonical.get("canonical_host") or urlsplit(canonical["canonical_production_url"]).hostname
            self._update_intent(
                project_id, operation_id,
                canonical_production_url=canonical["canonical_production_url"],
                canonical_source=canonical.get("canonical_source"),
                canonical_host=host,
                canonical_deployment_id=intended_identity["deployment_id"],
            )
            logger.info(
                "Canonical production URL resolved project=%s source=%s url=%s host=%s",
                project_id, canonical.get("canonical_source"),
                canonical.get("canonical_production_url"), host,
            )

        # ---- 3. Mandatory production smoke check, against the CANONICAL
        # url. The deployment-specific hostname may sit behind Deployment
        # Protection and is never a meaningful health check of what users
        # actually load.
        #
        # Skipped when the record is already at SMOKE_PASSED. The smoke ran,
        # it passed, and its evidence is durable on the pending record; running
        # it again would be a second external action for a fact already
        # established, and this stage is a statement of fact, not an event to
        # re-emit.
        if smoke_already_passed:
            passed_evidence = dict(pending.get("smoke") or {})
            logger.info(
                "Resuming after SMOKE_PASSED project=%s; smoke evidence reused",
                project_id,
            )
        else:
            passed_evidence, smoke_failure = self._smoke_production(
                project_id, operation_id, workspace, app_id, vercel_project,
                expected_name, canonical, previous_identity)
            if smoke_failure is not None:
                return smoke_failure
            try:
                self.releases.ensure_stage(
                    project_id, operation_id, release_contract.STAGE_SMOKE_PASSED,
                    smoke=passed_evidence,
                )
            except (StaleOperationIntent, release_contract.ReleaseStageError):
                self._fail(project_id, "PROMOTE_FAILED", "PROMOTION_STAGE_LOST")
                return OperationResult.fail("PROMOTE_FAILED",
                                            error_code="PROMOTION_STAGE_LOST")

        # ---- 4. The single atomic COMMITTED write: lifecycle LIVE,
        # live_revision, canonical production_url, the derived
        # last_live_deployment projection, the authoritative
        # last_live_release, and the clearing of pending_publication.
        #
        # Re-read rather than reuse the stage snapshot above: the smoke ran in
        # between, and an approval replaced during it must still be caught here.
        state = self.store.load(project_id)
        if state is None:
            return OperationResult.fail("NO_PROJECT_STATE", error_code="NO_PROJECT_STATE")
        if state.deployment.get("approval") != approval:
            return OperationResult.fail("STALE_APPROVAL", error_code="STALE_APPROVAL")
        try:
            release_record = self._commit_release(
                project_id, operation_id, source_revision, deployment_id,
                deployment_url, canonical["canonical_production_url"],
                approval, passed_evidence,
            )
        except (StaleOperationIntent, release_contract.ReleaseRecordError,
                release_contract.ReleaseStageError):
            # The release could not be written; the project stays PUBLISHING
            # with its intent intact, so a same-operation resume retries the
            # commit rather than re-running the whole operation.
            return OperationResult.fail(
                "RELEASE_COMMIT_FAILED", error_code="RELEASE_COMMIT_FAILED",
            )
        logger.info("Project LIVE production_url=%s", canonical["canonical_production_url"])

        # ---- 5. Telegram notification of LIVE promotion (best-effort in the
        # sense that a failed notification does not un-promote; the site
        # is already live and smoke-verified at this point).
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
            "release": release_record,
        })

    def _smoke_production(self, project_id, operation_id, workspace, app_id,
                          vercel_project, expected_name, canonical,
                          previous_identity):
        """Run the mandatory production smoke and classify the outcome.

        Returns ``(passed_evidence, None)`` when the canonical production URL
        was proven, or ``(None, failure_result)`` when it was not -- the caller
        returns that result verbatim, so the smoke stage owns every side effect
        it implies (the failure record, the rollback, the persisted evidence)
        rather than reporting success and leaving them to a caller that might
        forget.

        Only the PASSED path produces release evidence. A failure records what
        was observed and rolls production back to the persisted previous
        identity; it never records a release, because the release has not
        happened.
        """
        smoke_dir = (self.smoke_dir_root or workspace) / "qa" / "production_smoke"
        smoke_result = self._run_production_smoke(
            canonical["canonical_production_url"], smoke_dir,
            (vercel_project or {}).get("id"),
        )
        self._update_intent(project_id, operation_id, stage="smoked",
                            smoke=smoke_result.data)
        smoke_evidence = {
            "at": time.time(),
            "target_host": (smoke_result.data or {}).get("target_host"),
            "target_path": (smoke_result.data or {}).get("target_path"),
            "failure_classification": (smoke_result.data or {}).get(
                "failure_classification"),
        }
        if not smoke_result.success:
            logger.warning(
                "Production smoke FAILED project=%s", project_id,
            )
            # The failure evidence is persisted BEFORE the rollback, so a crash
            # inside the rollback cannot lose the reason this release stopped.
            self._mark_publication_terminal_failure(
                project_id, operation_id, "SMOKE_FAILED",
                smoke=_release_smoke_evidence(
                    smoke_evidence, "FAILED",
                    canonical["canonical_production_url"]),
            )
            rollback_status = self._rollback_and_fail(
                project_id, app_id, vercel_project, previous_identity,
                error_code="SMOKE_FAILED", expected_name=expected_name,
            )
            return None, OperationResult(
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
        passed_evidence = _release_smoke_evidence(
            smoke_evidence, "PASSED", canonical["canonical_production_url"])
        if passed_evidence is None:
            # The smoke reported success but did not describe WHAT it checked.
            # A release identity that cannot say which host was proven is not
            # a release identity; fail closed rather than record a guess.
            self._mark_publication_terminal_failure(
                project_id, operation_id, "SMOKE_EVIDENCE_INCOMPLETE",
            )
            self._fail(
                project_id, "SMOKE_EVIDENCE_INCOMPLETE",
                "SMOKE_EVIDENCE_INCOMPLETE",
            )
            return None, OperationResult.fail(
                "SMOKE_EVIDENCE_INCOMPLETE",
                error_code="SMOKE_EVIDENCE_INCOMPLETE",
            )
        return passed_evidence, None

    def _commit_release(self, project_id, operation_id, source_revision,
                       deployment_id, deployment_url, production_url,
                       approval, smoke_evidence):
        """Build and commit the authoritative LIVE release record."""
        state = self.store.load(project_id)
        if state is None:
            raise StaleOperationIntent("project state disappeared before commit")
        pending = self.releases.pending(state) or {}
        record = release_contract.build_last_live_release(
            operation_id=operation_id,
            source_revision=source_revision,
            source_sha256=approval["source_sha256"],
            artifact_sha256=approval["artifact_sha256"],
            deployment_id=deployment_id,
            production_url=production_url,
            deployment_url=deployment_url,
            smoke=smoke_evidence,
            publication=pending.get("publication") or {},
        )
        return self.releases.commit_release(
            project_id, operation_id=operation_id, release=record)

    # ------------------------------------------------------------------
    # Canonical public production URL
    # ------------------------------------------------------------------

    def _resolve_canonical(self, app_id, vercel_project, expected_name, deployment_id=None):
        """The canonical public production URL, or None when unresolvable.

        Delegates strictly to the adapter's ``canonical_production_url`` with
        ``expected_deployment_id``. A collaborator that fails or returns an
        invalid result is treated as unresolvable rather than silently degrading
        to a deployment hostname: there is no honest URL to return in that case.
        """
        helper = getattr(self.deps.vercel, "canonical_production_url", None)
        if not callable(helper):
            return None
        result = helper(
            app_id, vercel_project,
            expected_name=expected_name,
            expected_deployment_id=deployment_id,
        )
        if not getattr(result, "success", False):
            return None
        data = result.data or {}
        url = data.get("canonical_production_url")
        if not isinstance(url, str) or not url.startswith("https://"):
            return None
        try:
            p = urlsplit(url)
            if (p.scheme != "https" or p.port not in (None, 443)
                    or p.username or p.password or p.fragment
                    or not p.hostname
                    or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.vercel\.app", p.hostname)):
                return None
        except Exception:
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
    # R2 Git stage: PREPARED -> GIT_CONFIRMED
    # ------------------------------------------------------------------
    #
    # Publication happens BEFORE production. Three properties this section
    # exists to guarantee:
    #
    #   * A successful exact fast-forward push is sufficient proof. The
    #     accepted push already established that the remote's previous head
    #     was exactly the parent we built against, so GIT_CONFIRMED is
    #     persisted immediately with NO remote read. Re-reading it would be a
    #     redundant network call and a second source of truth for a fact the
    #     push already settled.
    #   * The remote is read in exactly two situations -- resume of a record
    #     already persisted at GIT_CONFIRMED or later, and a rejected/ambiguous
    #     push -- and both run the same A/B/C/D classifier.
    #   * C and D stay distinct. C means the remote was READ and holds an
    #     unexpected state; D means the remote could not be read and we
    #     therefore assert nothing. Neither is ever re-parented onto.
    #
    # Publication-not-configured is NOT a shortcut past this stage. It advances
    # to GIT_CONFIRMED as a local no-op with zero Git subprocesses, so resume
    # and recovery have one code path.

    def _publication_config(self, state):
        """``{'repo', 'url', 'ssh_key', 'repo_name'}`` or ``None``.

        ``None`` means publication is not configured for this installation --
        not configured, not attempted, not a failure. An operator who has not
        supplied a remote, a branch resolver or a deploy key must not see every
        publish reported as an unsynchronised source.

        Deliberately NOT the branch: the branch is resolved once at PREPARED
        and travels in the pending record, so the push and the reconcile read
        the same ref even if a slug binding changes underneath the operation.
        """
        if self.deps.output_repo is None or not self.deps.source_repo_url:
            return None
        if self.deps.source_branch_for is None:
            return None
        if not self.deps.source_ssh_key:
            return None
        return {
            "repo": self.deps.output_repo,
            "url": self.deps.source_repo_url,
            "ssh_key": self.deps.source_ssh_key,
            "repo_name": self._source_repo_name(),
        }

    def _publication_branch(self, state, expected_name):
        """The friendly branch for this project, or None.

        Raises ``ReleaseRecordError`` when a branch resolver that was
        configured declines to name one: an operator who configured
        publication but cannot resolve the branch has a real problem, and
        silently treating it as "publication not configured" would drop a
        release's source history without a word.
        """
        try:
            return self.deps.source_branch_for(
                state.project_id, state, expected_name)
        except Exception:
            return None

    def _prepare_publication(self, state, expected_name, operation_id,
                             source_revision, approval):
        """Build the PREPARED pending-publication record. No network access.

        The deterministic intended commit is built and persisted BEFORE the
        push, so a lost post-push state write is recognisable as case A on
        restart: the retried build produces a byte-identical commit.

        The trusted tested commit is resolved ONLY when publication is actually
        configured. An unconfigured installation has no commit to resolve and
        no reason to demand one -- requiring it would make every deploy-key-less
        install fail closed over a capability it never asked for.
        """
        config = self._publication_config(state)
        branch = self._publication_branch(state, expected_name) if config else None
        prepared = None
        parent = release_contract.resolve_branch_parent(state)
        if config is not None:
            if not branch:
                raise release_contract.ReleaseRecordError(
                    "No resolvable publication branch")
            # The exact TestedSnapshot commit bound to this approved preview.
            # Reused rather than re-derived: it is the single existing trust
            # path from approval to commit, and a second one would be a second
            # thing to keep honest.
            git_identity = self._tested_commit_for(
                state, operation_id, approval=approval)
            try:
                prepared = config["repo"].prepare_publication(
                    git_identity["commit"], branch, config["url"],
                    source_revision=source_revision,
                    source_sha256=approval.get("source_sha256"),
                    artifact_sha256=approval.get("artifact_sha256"),
                    previous_publication_commit=parent,
                    ssh_key=config["ssh_key"],
                )
            except Exception as exc:
                # Only the exception TYPE is logged: a git failure's message can
                # embed the remote and local filesystem paths. The intended
                # commit does not exist, so there is nothing to publish and the
                # operation fails closed at PREPARED.
                logger.error(
                    "Publication prepare FAILED project=%s branch=%s (type=%s)",
                    state.project_id, branch, type(exc).__name__,
                )
                raise release_contract.ReleaseRecordError(str(exc)) from exc
        return release_contract.build_pending_publication(
            operation_id=operation_id,
            source_revision=source_revision,
            source_sha256=approval.get("source_sha256"),
            artifact_sha256=approval.get("artifact_sha256"),
            prepared=prepared,
            parent=parent,
            branch=branch,
            repo=config["repo_name"] if config else None,
        )

    def _confirm_git(self, project_id, operation_id):
        """Drive the publication from its current stage to GIT_CONFIRMED.

        The only place a push happens. Three arms:

          * already at GIT_CONFIRMED or later (a resume) -- read the remote head
            once and require it to be the intended commit. Any other valid head
            is case C; an unreadable remote is case D. Both fail closed.
          * PREPARED with publication not configured -- local no-op advance.
          * PREPARED with publication configured -- one push. Accepted means
            GIT_CONFIRMED with no remote read; rejected means one A/B/C/D
            reconciliation, and B retries the exact intended commit once.
        """
        state = self.store.load(project_id)
        if state is None:
            return OperationResult.fail("NO_PROJECT_STATE", error_code="NO_PROJECT_STATE")
        pending = self.releases.pending(state)
        if not pending or pending.get("operation_id") != operation_id:
            return OperationResult.fail(
                "PROMOTION_STAGE_LOST", error_code="PROMOTION_STAGE_LOST",
            )
        stage = pending.get("stage")
        publication = pending.get("publication") or {}

        if release_contract.at_least(stage, release_contract.STAGE_GIT_CONFIRMED):
            # Resume. This process has no push receipt, so the fact has to be
            # re-derived from the remote -- but only for a configured
            # publication. A NOT_CONFIGURED record has nothing to re-derive, so
            # it is simply re-affirmed: the Git side of the operation is not
            # held for reconciliation, and a Vercel-level ambiguity that a
            # successful resume has overtaken must not keep the record closed.
            if publication.get("configured") is not True:
                try:
                    self.releases.confirm_publication(project_id, operation_id)
                except (StaleOperationIntent,
                        release_contract.ReleaseRecordError) as exc:
                    logger.error(
                        "Publication re-affirmation FAILED project=%s (type=%s)",
                        project_id, type(exc).__name__,
                    )
                    return OperationResult.fail(
                        "PUBLICATION_STAGE_LOST", error_code="PUBLICATION_STAGE_LOST",
                    )
                return OperationResult.ok({"stage": stage,
                                           "verdict": release_contract.PUBLICATION_NOT_CONFIGURED})
            return self._resume_git(project_id, operation_id, publication)

        if publication.get("configured") is not True:
            # Local no-op confirmation. No push, no ls-remote, no update-ref:
            # nothing was published, so there is nothing to confirm, and
            # publication_head is deliberately left alone.
            try:
                self.releases.confirm_publication(project_id, operation_id)
            except (StaleOperationIntent, release_contract.ReleaseRecordError):
                return self._git_fail_closed(
                    project_id, operation_id, "PUBLICATION_STAGE_LOST", terminal=True)
            return OperationResult.ok({"stage": release_contract.STAGE_GIT_CONFIRMED,
                                       "verdict": release_contract.PUBLICATION_NOT_CONFIGURED})

        return self._push_git(project_id, operation_id, publication)

    def _push_git(self, project_id, operation_id, publication):
        """One push of the intended commit, then A/B/C/D on rejection.

        This is the only arm that may ever retry a push (verdict B_RETRY), and
        only because the record is still at PREPARED: the remote genuinely has
        not seen this commit, because nothing ever told it to.
        """
        config = self._publication_target(project_id, publication)
        if config is None:
            # Publication was configured at PREPARED and the configuration
            # disappeared underneath us. Fail closed rather than pretend the
            # release was published. Local, so it is not a head conflict.
            return self._git_fail_closed(
                project_id, operation_id,
                release_contract.ERROR_PUBLICATION_INPUT_INVALID, terminal=True)
        push = config["repo"].push_prepared_publication(
            config["url"], publication.get("intended_commit"), config["branch"],
            ssh_key=config["ssh_key"],
        )
        if push.success:
            # An accepted push IS the proof. No remote read.
            return self._git_confirmed(project_id, operation_id, publication)

        if (push.data or {}).get("rejected_as") != "NON_FAST_FORWARD":
            # A conclusive refusal (auth, hook, permissions, or a local mirror
            # write that failed after an accepted push). Re-pushing would
            # repeat the identical failure, and the branch is not ours to move
            # or to roll back.
            return self._git_fail_closed(
                project_id, operation_id,
                push.error_code or release_contract.ERROR_PUSH_FAILED, terminal=True)

        # Rejected as non-fast-forward: the remote moved under us. Reconcile
        # once, and only ever along the A/B/C/D matrix.
        reconciled = self._reconcile_head(project_id, publication, config)
        if reconciled.success:
            verdict = (reconciled.data or {}).get("verdict")
            if verdict == release_contract.VERDICT_A_ADOPT:
                return self._git_confirmed(
                    project_id, operation_id, publication,
                    remote_head=(reconciled.data or {}).get("remote_head"))
            # B_RETRY: push the EXACT intended commit, once. It was built
            # against a parent the remote still holds, so this is a clean
            # fast-forward, not a re-parent.
            retry = config["repo"].push_prepared_publication(
                config["url"], publication.get("intended_commit"), config["branch"],
                ssh_key=config["ssh_key"],
            )
            if retry.success:
                return self._git_confirmed(project_id, operation_id, publication)
            return self._git_fail_closed(
                project_id, operation_id, release_contract.ERROR_PUSH_FAILED, terminal=True)
        return self._git_fail_closed(
            project_id, operation_id, reconciled.error_code,
            reconciliation_required=True)

    def _resume_git(self, project_id, operation_id, publication):
        """Re-verify a publication a previous process already confirmed.

        One remote read, one decision, and never a push:

          * head == intended_commit -> continue (case A).
          * any other valid head    -> case C, fail closed. Somebody moved the
            branch to something we did not publish -- including BACK to
            ``intended_parent``, which is not a rewind we get to "fix"; that is
            a conflict to report, never a commit to build on top of.
          * unreadable remote       -> case D, fail closed. We assert nothing
            about a state we could not read.

        B_RETRY is unreachable here and deliberately so. The persisted stage
        says the push already landed; if the remote no longer holds the
        intended commit, the remote changed after that confirmation, and
        re-pushing would be a second publication of a record that already
        claims it published once. A rejected initial push (still at PREPARED)
        is the only place a retry is licensed, and that is ``_push_git``.
        """
        config = self._publication_target(project_id, publication)
        if config is None:
            # The target vanished under a record that says it published. Local,
            # so it is not a head conflict and not a remote read.
            return self._git_fail_closed(
                project_id, operation_id,
                release_contract.ERROR_PUBLICATION_INPUT_INVALID,
                reconciliation_required=True)
        reconciled = self._reconcile_head(
            project_id, publication, config, already_confirmed=True)
        if reconciled.success:
            return self._git_confirmed(
                project_id, operation_id, publication,
                remote_head=(reconciled.data or {}).get("remote_head"))
        return self._git_fail_closed(
            project_id, operation_id, reconciled.error_code,
            reconciliation_required=True)

    def _publication_target(self, project_id, publication):
        """The push target for a pending record: config plus its OWN branch.

        The branch comes from the pending record, not from a fresh resolve. The
        branch name was written into the durable publication intent at
        PREPARED; re-resolving it here would let a mid-operation slug change
        send this operation's commit to a different ref than the one the
        reconcile and every later read use.
        """
        state = self.store.load(project_id)
        if state is None:
            return None
        config = self._publication_config(state)
        if config is None:
            return None
        branch = (publication or {}).get("branch")
        if not branch:
            return None
        return {**config, "branch": branch}

    def _reconcile_head(self, project_id, publication, config,
                        already_confirmed=False):
        """The single remote read: classify the branch head as A/B/C/D.

        ``already_confirmed`` narrows the matrix to resume semantics (A/C/D,
        never a retry); see ``reconcile_publication_head`` for why that
        narrowing is a safety property.
        """
        return config["repo"].reconcile_publication_head(
            config["url"], config["branch"],
            publication.get("intended_commit"),
            publication.get("intended_parent"),
            ssh_key=config["ssh_key"],
            already_confirmed=already_confirmed,
        )

    def _git_confirmed(self, project_id, operation_id, publication, *, remote_head=None):
        """Persist GIT_CONFIRMED (and advance the branch parent authority)."""
        try:
            self.releases.confirm_publication(
                project_id, operation_id, remote_head=remote_head)
        except (StaleOperationIntent, release_contract.ReleaseRecordError) as exc:
            logger.error(
                "Publication confirmation FAILED project=%s (type=%s)",
                project_id, type(exc).__name__,
            )
            return OperationResult.fail(
                "PUBLICATION_STAGE_LOST", error_code="PUBLICATION_STAGE_LOST")
        logger.info(
            "Publication confirmed project=%s branch=%s commit=%s",
            project_id, publication.get("branch"), publication.get("intended_commit"),
        )
        return OperationResult.ok({
            "stage": release_contract.STAGE_GIT_CONFIRMED,
            "verdict": release_contract.VERDICT_A_ADOPT,
        })

    def _git_fail_closed(self, project_id, operation_id, error_code, *,
                         terminal: bool = False, reconciliation_required: bool = False):
        """Record a Git-stage failure and return it.

        ``terminal`` fails the lifecycle (a conclusive refusal). The default
        HOLDS PUBLISHING with the intent intact, because a C or D verdict means
        the remote state is unresolved -- not that the release is known bad.
        Holding is also what blocks a new operation from superseding this one.
        """
        logger.error(
            "Publication FAILED project=%s operation=%s code=%s terminal=%s",
            project_id, operation_id, error_code, terminal,
        )
        try:
            if reconciliation_required:
                self.releases.mark_reconciliation_required(
                    project_id, operation_id, error_code)
            else:
                self.releases.mark_terminal_failure(
                    project_id, operation_id, error_code,
                    publication_failed=not reconciliation_required,
                )
        except (StaleOperationIntent, release_contract.ReleaseStageError):
            pass
        if terminal:
            self._fail(project_id, "PUBLICATION_FAILED", error_code)
        return OperationResult.fail(error_code, error_code=error_code)

    def _mark_publication_terminal_failure(self, project_id, operation_id,
                                           error_code, smoke=None):
        """Best-effort pending-publication failure bookkeeping."""
        try:
            self.releases.mark_terminal_failure(
                project_id, operation_id, error_code, smoke=smoke)
        except (StaleOperationIntent, release_contract.ReleaseStageError):
            pass

    def _mark_publication_reconciliation_required(self, project_id, operation_id,
                                                  error_code):
        """Hold the pending publication open for reconciliation."""
        try:
            self.releases.mark_reconciliation_required(
                project_id, operation_id, error_code)
        except (StaleOperationIntent, release_contract.ReleaseStageError):
            pass

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
