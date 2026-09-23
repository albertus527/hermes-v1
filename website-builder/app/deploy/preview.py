"""Phase 9 preview orchestration.

Owns nothing about Vercel/Telegram/browser directly — that lives in
app.deploy.adapters. This module owns:

  * binding the exact tested source+dist bytes to a preview operation
    (rejecting anything that drifted since Phase 8 QA passed),
  * committing those exact bytes into the separate output Git repository,
  * persisting durable operation intent BEFORE any external side effect,
  * reconciling ambiguous/ timed-out provider calls via lookup-by-identity
    rather than blind resend,
  * mandatory anonymous smoke test before any preview is ever shown,
  * only marking "latest shown preview" after screenshot delivery AND the
    preview URL text delivery both succeeded.

No network call happens here without every prerequisite adapter being
constructed and passed in explicitly by the caller. There is no default
Vercel/Telegram credential resolution in this module.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.contracts import OperationResult
from app.core.state import ProjectStateStore
from app.deploy.git_output import OutputGitRepository
from app.deploy.snapshot import TestedSnapshot, source_fingerprint


# ---------------------------------------------------------------------------
# Post-delivery follow-up delivery-state machine
# ---------------------------------------------------------------------------
#
# Durable on ``latest_shown_preview["follow_up_state"]``. This message is a
# pure UX nudge ("mau revisi atau bikin baru?") — it carries NO product
# state, and its delivery must never turn an already-delivered preview into
# a failed preview.
#
#   ABSENT / None  -> definitely NOT attempted. Safe to (re)try: no bytes
#                     have been sent for this operation, so a retry cannot
#                     duplicate. This is where a definite local/pre-send
#                     failure (no chat target, name resolution failure,
#                     persist failure) leaves the state.
#   PENDING        -> a Telegram send was ATTEMPTED but its outcome is
#                      UNKNOWN (crash mid-send, transport error, adapter
#                      reported failure). Ambiguous by construction: the
#                      message may or may not have landed. FAIL CLOSED —
#                      never blindly resend, because a resend can duplicate
#                      and there is no idempotency key for a plain
#                      ``send_text``.
#   SENT           -> the adapter CONFIRMED delivery (success=True). Never
#                      resend.
#
# This mirrors the existing fail-closed handling of ambiguous preview
# Telegram sends in this module (``photo_attempted`` / ``text_attempted``
# followed by ``DELIVERY_RECONCILIATION_REQUIRED``): persist the attempt
# BEFORE the side effect, and treat an unconfirmed attempt as ambiguous
# rather than retrying it.
FOLLOW_UP_PENDING = "PENDING"
FOLLOW_UP_SENT = "SENT"

# Per-message delivery outcome for the photo/text preview sends.
# ``photo_outcome``/``text_outcome`` are persisted BEFORE the corresponding
# remote send so a crash mid-delivery is reconcilable without inventing
# certainty about an ambiguous send.
#
#   NOT_SENT  -> the adapter DEFINITELY did not send (input/validation failure
#                or an explicit Telegram rejection). Safe to re-drive once.
#   PENDING   -> the send outcome is UNKNOWN (ambiguous transport failure, or
#                a crash after Telegram may have accepted but before the
#                message_id was recorded). Never resend -- possible duplicate.
#   SENT      -> the adapter CONFIRMED delivery (success=True, message_id
#                recorded). Never resend.
_DELIVERY_NOT_SENT = "NOT_SENT"
_DELIVERY_PENDING = "PENDING"
_DELIVERY_SENT = "SENT"


def _delivery_outcome_of(result) -> str:
    """Map an adapter OperationResult to a delivery outcome.

    Only an explicit, well-formed rejection is "definitely not sent". Any
    transport-level ambiguity (AMBIGUOUS_SEND, exceptions, malformed payloads)
    is PENDING -- the message may already have been accepted by Telegram.
    """
    if result is None:
        return _DELIVERY_PENDING
    if getattr(result, "success", False):
        return _DELIVERY_SENT
    if getattr(result, "error_code", None) == "TELEGRAM_REJECTED":
        return _DELIVERY_NOT_SENT
    return _DELIVERY_PENDING


@dataclass
class PreviewDeps:
    """All external boundary objects. None of these are constructed here."""

    vercel: Any
    telegram: Any
    smoke: Any
    output_repo: OutputGitRepository
    chat_id_for: Any  # callable(project_id, state) -> str, may return None
    app_id_for: Any = field(default=lambda project_id: project_id)
    # Optional callable(project_id, state) -> Optional[str]. Returns the
    # human project/display name (e.g. "webbandung") for the natural
    # post-delivery follow-up message. None (or returning None) disables the
    # follow-up so existing call sites behave exactly as before.
    display_name_for: Any = None
    # Optional callable(project_id, state) -> Optional[str]. Returns the
    # friendly Vercel slug to request for THIS project (already-bound slug
    # if one exists, else the freshly-derived candidate). None disables
    # slug-based project resolution entirely -- the orchestrator falls back
    # to the existing opaque-name ensure_project/lookup_project path so
    # every pre-existing call site behaves exactly as before.
    slug_for: Any = None
    # Optional callable(project_id, state, slug) -> None. Persists the
    # slug binding EXACTLY ONCE after the first successful Vercel project
    # creation/reconciliation under that slug. No-op when slug_for is None.
    bind_slug: Any = None


class PreviewOrchestrator:
    """Application-owned Phase 9 orchestration. Caller supplies all adapters."""

    def __init__(self, store: ProjectStateStore, deps: PreviewDeps,
                 smoke_dir_root: Optional[Path] = None, runner=None):
        self.store = store
        self.deps = deps
        self.smoke_dir_root = smoke_dir_root
        # Optional single-worker-slot guard (ProjectRunner, MAX_WORKERS=1).
        # When set, run_owned acquires the slot for any caller that does not
        # already own it, so a dispatch-side reconcile can never overlap a
        # build/revise-triggered preview on the same process.
        self.runner = runner

    # ------------------------------------------------------------------
    # Entry point — runs inside the same worker ownership as Phase 7/8.
    # ------------------------------------------------------------------

    def run_owned(self, project_id: str, workspace: Path, *,
                  slot_held: bool = False) -> OperationResult:
        """Serialize preview orchestrators without nesting project writer locks.

        ProjectRunner MAX_WORKERS=1 provides exclusive worker-slot ownership
        across build, revise, promote, and preview executions. Callers that
        already hold the slot (Phase 7 build, revision) pass
        ``slot_held=True``; every other caller (e.g. dispatch-side preview
        reconciliation) acquires the slot here, so two preview executions
        can never run in parallel after the preview-worker lock removal.
        """
        acquired = False
        if self.runner is not None and not slot_held:
            if not self.runner.acquire_project(project_id):
                return OperationResult.fail('PREVIEW_BUSY', error_code='PREVIEW_BUSY')
            acquired = True
        try:
            return self._run(project_id, workspace)
        except Exception:
            # Persisted intents survive crashes/exceptions; retry only reconciles.
            # Log operator-side: the failure text itself is a stable code.
            logging.getLogger(__name__).exception(
                "Preview execution for %s raised unexpectedly", project_id
            )
            return OperationResult.fail('PREVIEW_RECONCILIATION_REQUIRED',
                                        error_code='PREVIEW_RECONCILIATION_REQUIRED')
        finally:
            if acquired:
                self.runner.release_project(project_id)

    def _run(self, project_id, workspace):
        state = self.store.load(project_id)
        if state is None:
            return OperationResult.fail("NO_PROJECT_STATE", error_code="NO_PROJECT_STATE")
        if (state.lifecycle != 'PREVIEW_READY' or state.revisions.source_revision < 1
                or state.revisions.source_revision != state.revisions.qa_revision
                or not state.deployment.get('tested_snapshot')):
            return OperationResult.fail('QA_REQUIRED', error_code='QA_REQUIRED')
        checked = state.deployment.get("checked") or {}
        expected_source = checked.get("source_sha256")
        expected_artifact = checked.get("artifact_sha256")
        if not expected_source or not expected_artifact:
            return OperationResult.fail(
                "NO_TESTED_SNAPSHOT", error_code="NO_TESTED_SNAPSHOT"
            )
        if checked.get("source_revision") != state.revisions.qa_revision:
            # Repair/revision bumped source_revision after this snapshot was
            # bound; the caller must re-run QA before a preview can be made.
            return OperationResult.fail("STALE_QA_BINDING", error_code="STALE_QA_BINDING")

        try:
            snapshot = TestedSnapshot.from_dict(state.deployment['tested_snapshot'])
            if (snapshot.source_sha256 != expected_source or
                    snapshot.artifact_sha256 != expected_artifact):
                raise ValueError('STALE_QA_BINDING')
            snapshot.verify(workspace)
        except ValueError as exc:
            return OperationResult.fail(str(exc), error_code=str(exc))

        operation_id = hashlib.sha256((project_id + ':' + str(state.revisions.source_revision)
                                      + ':' + snapshot.identity).encode()).hexdigest()
        shown = state.deployment.get('latest_shown_preview', {})
        if shown.get('operation_id') == operation_id:
            # Short-circuit: this exact preview was durably delivered. The
            # follow-up is re-evaluated here so a crash between "preview
            # marked shown" and "follow-up attempted" is recoverable — but
            # ONLY when the durable state says the follow-up was definitely
            # never attempted. An attempted-but-unconfirmed send (PENDING)
            # is intentionally NOT retried (it may duplicate).
            self._maybe_send_follow_up(
                project_id, shown.get('preview_url', ''),
                operation_id=operation_id, shown=shown,
            )
            return OperationResult.ok(shown)
        source_revision = state.revisions.source_revision

        # ---- 1. Persist durable operation intent BEFORE any side effect ----
        with self.store.acquire_writer(project_id) as locked:
            intent = locked.deployment.get("preview_intent")
            if not intent or intent.get("operation_id") != operation_id:
                locked.deployment["preview_intent"] = {
                    "operation_id": operation_id,
                    "source_revision": source_revision,
                    "source_sha256": snapshot.source_sha256,
                    "artifact_sha256": snapshot.artifact_sha256,
                    "stage": "created",
                    "created_at": time.time(),
                }
                self.store.save(locked)
            state = locked

        try:
            git_identity = self.deps.output_repo.commit(project_id, snapshot)
        except Exception as exc:  # pragma: no cover - defensive: local git failure
            return OperationResult.fail(f"OUTPUT_COMMIT_FAILED: {exc}", error_code="OUTPUT_COMMIT_FAILED")
        self._update_intent(project_id, operation_id, stage="committed", git=git_identity)

        # ---- 2. Ensure owned Vercel project identity ----
        app_id = self.deps.app_id_for(project_id)
        previous = state.deployment.get('preview_intent', {})
        attempted = state.deployment.get('project_create_attempted', False)
        with self.store.acquire_writer(project_id) as locked:
            locked.deployment['project_create_attempted'] = True
            self.store.save(locked)
        self._update_intent(project_id, operation_id, project_attempted=True)

        slug = None
        if self.deps.slug_for is not None:
            try:
                slug = self.deps.slug_for(project_id, state)
            except Exception:
                slug = None
        # Canonical Vercel project name for downstream identity validation,
        # determined BEFORE and independently of any provider response:
        # the friendly slug when one is bound/derivable, else None (legacy
        # opaque hash-derived name path). Threaded through every adapter call
        # that revalidates the owned project so those checks assert the same
        # canonical name instead of re-deriving the opaque default.
        expected_name = slug if slug else None

        if slug:
            # Friendly-slug resolution path. The SHA-based ownership marker
            # (checked inside ensure_project_with_slug) remains the sole
            # identity authority; the slug never proves ownership. A
            # collision with a foreign/unowned project surfaces as a
            # distinct, fail-closed result -- never adopted/overwritten.
            project_result = self.deps.vercel.ensure_project_with_slug(app_id, slug)
            if not project_result.success:
                return project_result
            vercel_project = project_result.data["project"]
            # BUG 4: once we have STARTED using a friendly Vercel project
            # identity, that binding MUST be durable before we proceed as if
            # the preview identity is safe. Previously a persistence failure
            # here was logged and execution continued -- the preview would be
            # deployed/delivered under a friendly project while local state
            # still had no bound slug (and a later run would either re-create
            # or silently fall back to the opaque name). Fail closed instead:
            # the remote project already exists, so we must NEVER fall back to
            # the opaque name; the next run reconciles the SAME remote project
            # via the idempotent set_vercel_slug_once binding.
            if self.deps.bind_slug is not None:
                try:
                    self.deps.bind_slug(project_id, state, slug)
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Failed to persist vercel_slug binding for %s", project_id
                    )
                    return OperationResult.fail(
                        "SLUG_BIND_PERSIST_FAILED",
                        error_code="SLUG_BIND_PERSIST_FAILED",
                    )
        else:
            project_result = (self.deps.vercel.lookup_project(app_id) if attempted
                              else self.deps.vercel.ensure_project(app_id))
            if not project_result.success:
                return project_result
            vercel_project = project_result.data["project"]

        # ---- 2b. Consume Vercel's unavoidable first-deployment
        # auto-promotion with content-free bytes BEFORE any real user
        # artifact is ever deployed. Pure remote reconciliation -- no new
        # local state, safe to call on every run (no-ops once production
        # already exists, whether from a prior bootstrap or real content).
        bootstrap_result = self.deps.vercel.ensure_bootstrap(
            app_id, vercel_project, expected_name=expected_name)
        if not bootstrap_result.success:
            return bootstrap_result

        # ---- 3. Deploy, reconciling any ambiguous prior attempt by lookup ----
        attempted = previous.get('deployment_attempted', False)
        self._update_intent(project_id, operation_id, deployment_attempted=True,
                            project=vercel_project)
        deployment = (self.deps.vercel.find_deployment_by_operation_id(
            app_id, vercel_project, operation_id, source_revision, snapshot.artifact_sha256,
            expected_name=expected_name
        ) if attempted else self._deploy_or_reconcile(
            app_id, vercel_project, snapshot, operation_id, source_revision,
            expected_name=expected_name
        ))
        if not deployment.success:
            return deployment
        preview_url = deployment.data["preview_url"]
        self._update_intent(project_id, operation_id, stage="deployed",
                            deployment_id=deployment.data["deployment_id"],
                            preview_url=preview_url)

        # ---- 4. Bounded readiness polling ----
        ready = self._await_ready(app_id, vercel_project, operation_id, source_revision,
                                  snapshot, expected_name=expected_name)
        if not ready.success:
            return ready

        if (ready.data.get('deployment_id') != deployment.data.get('deployment_id')
                or ready.data.get('preview_url') != preview_url):
            return OperationResult.fail('DEPLOYMENT_IDENTITY_MISMATCH',
                                        error_code='DEPLOYMENT_IDENTITY_MISMATCH')

        # ---- 5. Mandatory anonymous smoke test ----
        from app.runtime import _diag_log  # deferred: avoids import cycle
        _diag_log("5.before_PreviewSmokeTester_run")
        smoke_dir = (self.smoke_dir_root or workspace) / "qa" / "preview_smoke"
        smoke_result = self.deps.smoke.run(preview_url, smoke_dir)
        self._update_intent(project_id, operation_id, stage="smoked", smoke=smoke_result.data)
        if not smoke_result.success:
            return OperationResult(success=False, error="SMOKE_FAILED",
                                   error_code="SMOKE_FAILED", data=smoke_result.data)

        # ---- 6. Telegram delivery — durable identities, fail closed on ambiguity ----
        chat_id = self.deps.chat_id_for(project_id, state)
        if not chat_id:
            return OperationResult.fail("NO_DELIVERY_TARGET", error_code="NO_DELIVERY_TARGET")

        shots = {}
        for key in ('desktop_screenshot', 'mobile_screenshot'):
            path = Path(smoke_result.data.get(key, ''))
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(smoke_dir.resolve()):
                return OperationResult.fail('NO_SMOKE_SCREENSHOT', error_code='NO_SMOKE_SCREENSHOT')
            png = path.read_bytes()
            if not png.startswith(b'\x89PNG\r\n\x1a\n'):
                return OperationResult.fail('NO_SMOKE_SCREENSHOT', error_code='NO_SMOKE_SCREENSHOT')
            shots[key] = {'path': str(path), 'sha256': hashlib.sha256(png).hexdigest()}
        self._update_intent(project_id, operation_id, screenshots=shots)
        photo_path = shots['desktop_screenshot']['path']

        # ---- Outcome-aware delivery reconciliation --------------------------
        # Legacy rows only carry photo_attempted/text_attempted booleans; the
        # outcome fields may be absent on rows written before this change.
        # Read the durable outcome for each message independently.
        #
        # BACKWARD COMPATIBILITY: a legacy row with ``*_attempted`` True but
        # no ``*_outcome`` cannot prove whether the send happened -- that is
        # UNKNOWN, and unknown means fail closed (never resend), NOT "safe".
        photo_outcome = previous.get('photo_outcome')
        if photo_outcome is None and previous.get('photo_attempted'):
            photo_outcome = _DELIVERY_PENDING
        text_outcome = previous.get('text_outcome')
        if text_outcome is None and previous.get('text_attempted'):
            text_outcome = _DELIVERY_PENDING

        # Photo delivery: a CONFIRMED SENT message is skipped (never resent);
        # a provably NOT_SENT message is re-driven once; a PENDING (ambiguous)
        # message fails closed -- never resend a possibly-delivered photo.
        if photo_outcome == _DELIVERY_PENDING:
            return OperationResult.fail('DELIVERY_RECONCILIATION_REQUIRED',
                                        error_code='DELIVERY_RECONCILIATION_REQUIRED')

        snapshot.verify(workspace)
        if photo_outcome in (None, _DELIVERY_NOT_SENT):
            # CRITICAL ORDERING: durably persist the ATTEMPT (PENDING) BEFORE
            # the first possible Telegram side effect. If this write fails we
            # must NOT send -- a send without durable evidence is exactly the
            # unmarked-send crash window that produces duplicate deliveries.
            try:
                self._update_intent(
                    project_id, operation_id,
                    photo_attempted=True, chat_id=str(chat_id),
                    photo_outcome=_DELIVERY_PENDING,
                )
            except Exception:
                return OperationResult.fail(
                    'DELIVERY_STATE_PERSIST_FAILED',
                    error_code='DELIVERY_STATE_PERSIST_FAILED',
                )
            photo_result = self.deps.telegram.send_photo(
                chat_id, photo_path, caption=f"Preview ready: {preview_url}"
            )
            new_outcome = _delivery_outcome_of(photo_result)
            self._update_intent(
                project_id, operation_id,
                photo_outcome=new_outcome,
                photo_message_id=(
                    photo_result.data.get("message_id")
                    if new_outcome == _DELIVERY_SENT else None
                ),
            )
            if not photo_result.success:
                return photo_result
            self._update_intent(project_id, operation_id, stage="photo_sent")
            photo_outcome = _DELIVERY_SENT

        # Text delivery: same outcome-aware gate.
        if text_outcome == _DELIVERY_PENDING:
            return OperationResult.fail('DELIVERY_RECONCILIATION_REQUIRED',
                                        error_code='DELIVERY_RECONCILIATION_REQUIRED')

        if text_outcome in (None, _DELIVERY_NOT_SENT):
            # Same pre-send durable PENDING write as the photo path: the
            # remote send may only happen after the attempt is on disk.
            try:
                self._update_intent(
                    project_id, operation_id,
                    text_attempted=True,
                    text_outcome=_DELIVERY_PENDING,
                )
            except Exception:
                return OperationResult.fail(
                    'DELIVERY_STATE_PERSIST_FAILED',
                    error_code='DELIVERY_STATE_PERSIST_FAILED',
                )
            text_result = self.deps.telegram.send_text(
                chat_id, f"Preview: {preview_url}\nReply with what you'd like changed."
            )
            new_outcome = _delivery_outcome_of(text_result)
            self._update_intent(
                project_id, operation_id,
                text_outcome=new_outcome,
                text_message_id=(
                    text_result.data.get("message_id")
                    if new_outcome == _DELIVERY_SENT else None
                ),
            )
            if not text_result.success:
                return text_result
            text_outcome = _DELIVERY_SENT

        # ---- 7. Mark latest shown preview only after BOTH sends succeeded ----
        snapshot.verify(workspace)
        with self.store.acquire_writer(project_id) as locked:
            if (locked.revisions.source_revision != source_revision
                    or locked.revisions.qa_revision != source_revision
                    or locked.lifecycle != 'PREVIEW_READY'
                    or locked.deployment['preview_intent']['operation_id'] != operation_id):
                return OperationResult.fail('STALE_QA_BINDING', error_code='STALE_QA_BINDING')
            # Both messages must be CONFIRMED SENT before the preview is shown.
            # On a re-drive that skipped one send (already SENT), the
            # message_id already lives in the durable intent -- never read it
            # from a skipped (un-bound) local result variable.
            prior = locked.deployment["preview_intent"]
            if (prior.get("photo_outcome") != _DELIVERY_SENT
                    or prior.get("text_outcome") != _DELIVERY_SENT):
                return OperationResult.fail('DELIVERY_RECONCILIATION_REQUIRED',
                                            error_code='DELIVERY_RECONCILIATION_REQUIRED')
            locked.revisions.preview_revision = source_revision
            locked.deployment["preview_intent"]["stage"] = "shown"
            locked.deployment["preview_intent"]["text_message_id"] = prior.get("text_message_id")
            locked.deployment["latest_shown_preview"] = {
                "operation_id": operation_id,
                "source_revision": source_revision,
                "preview_url": preview_url,
                "deployment_id": deployment.data["deployment_id"],
                "source_sha256": snapshot.source_sha256,
                "artifact_sha256": snapshot.artifact_sha256,
                "shown_at": time.time(),
                # Follow-up not yet attempted for this operation. Persisted
                # as an explicit key (not merely absent) so a later reader
                # can distinguish "definitely not attempted" from a
                # partially-written row.
                "follow_up_state": None,
            }
            self.store.save(locked)

        # ---- 8. Natural follow-up AFTER durable delivery success ----------
        # UX-only. Never fails the already-delivered preview: every failure
        # path below is log-only and returns normally.
        self._maybe_send_follow_up(
            project_id, preview_url, operation_id=operation_id,
            shown=locked.deployment["latest_shown_preview"],
        )

        return OperationResult.ok({
            "preview_url": preview_url,
            "deployment_id": deployment.data["deployment_id"],
            "operation_id": operation_id,
        })

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _follow_up_state(shown: Dict[str, Any]) -> Optional[str]:
        """Read the durable follow-up state from ``latest_shown_preview``.

        Returns ``FOLLOW_UP_SENT`` / ``FOLLOW_UP_PENDING`` / ``None``
        (definitely-not-attempted). Reads the legacy ``follow_up_sent``
        boolean shape for compatibility with any row written by an earlier
        build: ``True`` maps to SENT (so it is never resent); a missing or
        ``False`` value maps to "not attempted" (retryable).
        """
        if shown.get("follow_up_sent") is True:
            return FOLLOW_UP_SENT
        state = shown.get("follow_up_state")
        if state in (FOLLOW_UP_PENDING, FOLLOW_UP_SENT):
            return state
        return None

    def _maybe_send_follow_up(
        self,
        project_id: str,
        preview_url: str,
        *,
        operation_id: str,
        shown: Dict[str, Any],
    ) -> None:
        """Attempt the post-delivery follow-up under fail-closed semantics.

        Delivery contract (see the FOLLOW_UP_* state machine above):

          * SENT or PENDING -> do nothing. PENDING means a prior send was
            ATTEMPTED with an unknown outcome; resending could duplicate a
            message that already arrived, and ``send_text`` has no
            idempotency key to reconcile against. We accept a possible
            lost UX nudge rather than risk a duplicate. An operator can see
            the unresolved PENDING row in project state.
          * not attempted -> resolve local prerequisites first. Any local
            failure (no project state, no display name, no chat target,
            marker persist failure) happens BEFORE any bytes are sent and
            therefore leaves the state "not attempted" — safely retryable
            on the next pass.
          * Once local prerequisites are satisfied the state is durably
            flipped to PENDING BEFORE the send, so a crash mid-send can
            never cause a blind resend. A confirmed ``success=True`` result
            flips it to SENT.

        This method NEVER raises and never returns a failure — the preview
        it belongs to has already been delivered.
        """
        existing = self._follow_up_state(shown)
        if existing == FOLLOW_UP_SENT:
            return
        if existing == FOLLOW_UP_PENDING:
            logging.getLogger(__name__).warning(
                "Follow-up for project %s (operation %s) has an unconfirmed "
                "prior attempt; not resending (ambiguous send, fail closed).",
                project_id, operation_id,
            )
            return

        state = self.store.load(project_id)
        if state is None:
            return
        follow_up_name = None
        if self.deps.display_name_for is not None:
            try:
                follow_up_name = self.deps.display_name_for(project_id, state)
            except Exception:
                follow_up_name = None
        if not follow_up_name:
            # Definite pre-send local failure — nothing was sent; retryable.
            return
        chat_id = None
        try:
            chat_id = self.deps.chat_id_for(project_id, state)
        except Exception:
            chat_id = None
        if not chat_id:
            # Definite pre-send local failure — nothing was sent; retryable.
            return

        # ---- Persist the ATTEMPT before any side effect (fail closed) ----
        try:
            with self.store.acquire_writer(project_id) as locked:
                current = locked.deployment.get("latest_shown_preview") or {}
                if current.get("operation_id") != operation_id:
                    return  # operation superseded — nothing to follow up on
                if self._follow_up_state(current) in (FOLLOW_UP_PENDING, FOLLOW_UP_SENT):
                    return
                current["follow_up_state"] = FOLLOW_UP_PENDING
                self.store.save(locked)
        except Exception:
            # Local failure BEFORE any send — safe to retry on a later pass.
            logging.getLogger(__name__).exception(
                "Failed to persist follow-up attempt marker for %s", project_id
            )
            return

        follow_up_text = (
            f"Website {follow_up_name} udah siap \U0001F389\n"
            f"{preview_url}\n\n"
            "Mau revisi website ini, atau mau bikin website baru?"
        )
        # From here on the outcome is potentially ambiguous: the request may
        # have reached Telegram even if we observe a failure. Never retry an
        # unconfirmed attempt.
        try:
            result = self.deps.telegram.send_text(chat_id, follow_up_text)
        except Exception:
            logging.getLogger(__name__).exception(
                "Ambiguous follow-up send for %s (operation %s); leaving "
                "PENDING and NOT resending.",
                project_id, operation_id,
            )
            return
        if not getattr(result, "success", False):
            logging.getLogger(__name__).warning(
                "Follow-up send for %s (operation %s) reported failure; "
                "outcome ambiguous, leaving PENDING and NOT resending.",
                project_id, operation_id,
            )
            return

        # Confirmed delivered — record it so no replay ever resends.
        try:
            with self.store.acquire_writer(project_id) as locked:
                current = locked.deployment.get("latest_shown_preview") or {}
                if (current.get("operation_id") == operation_id
                        and self._follow_up_state(current) == FOLLOW_UP_PENDING):
                    current["follow_up_state"] = FOLLOW_UP_SENT
                    self.store.save(locked)
        except Exception:
            # Worst case: the row stays PENDING, which fails CLOSED (a later
            # pass will not resend). At-most-once is preserved.
            logging.getLogger(__name__).exception(
                "Failed to persist confirmed follow-up for %s", project_id
            )

    def _update_intent(self, project_id: str, operation_id: str, **fields) -> None:
        with self.store.acquire_writer(project_id) as state:
            intent = state.deployment.get("preview_intent")
            if intent and intent.get("operation_id") == operation_id:
                intent.update(fields)
                self.store.save(state)

    def _deploy_or_reconcile(self, app_id, project, snapshot, operation_id, source_revision,
                             *, expected_name=None):
        """Deploy; on ambiguity/timeout, reconcile via lookup — never resend blindly."""
        try:
            result = self.deps.vercel.deploy_static_files(
                app_id, project, dict(snapshot.dist),
                operation_id, source_revision, snapshot.artifact_sha256,
                expected_name=expected_name,
            )
        except Exception as exc:  # pragma: no cover - defensive
            result = OperationResult.fail(str(exc), error_code="DEPLOY_EXCEPTION")

        if result.success:
            return result

        # Any failure from deploy_static_files is already fail-closed
        # (adapters.py never marks retryable=True). Before giving up,
        # check whether the operation actually landed under a timeout.
        lookup = self.deps.vercel.find_deployment_by_operation_id(
            app_id, project, operation_id, source_revision, snapshot.artifact_sha256,
            expected_name=expected_name
        )
        if lookup.success:
            return lookup
        return result

    def _await_ready(self, app_id, project, operation_id, source_revision, snapshot,
                     max_polls: int = 20, interval: float = 1.5, *, expected_name=None):
        for _ in range(max_polls):
            lookup = self.deps.vercel.find_deployment_by_operation_id(
                app_id, project, operation_id, source_revision, snapshot.artifact_sha256,
                expected_name=expected_name
            )
            if not lookup.success:
                if lookup.error_code == "NOT_FOUND":
                    time.sleep(interval)
                    continue
                return lookup
            state = lookup.data.get("state")
            if state in ("READY", "ready"):
                return lookup
            if state in ("ERROR", "error", "CANCELED", "canceled"):
                return OperationResult.fail("DEPLOYMENT_FAILED", error_code="DEPLOYMENT_FAILED")
            time.sleep(interval)
        return OperationResult.fail("DEPLOYMENT_TIMEOUT", error_code="DEPLOYMENT_TIMEOUT")
