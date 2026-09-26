"""Trusted local dispatch. Payload content never grants authority.

Claims are saved under the project writer before effects. Unknown outcomes stay
claimed across restarts; replay never retries an ambiguous mutation.

WhatsApp is ONLY another transport/channel adapter feeding this SAME
dispatch pipeline (see app/channels/whatsapp.py). It does not create a
parallel project lifecycle, revision system, deployment system, or
authorization/deduplication store -- ``AuthenticatedWhatsAppContext`` below
mirrors ``AuthenticatedTelegramContext`` exactly, and ``TelegramDispatcher``
picks the matching normalizer purely from the context type.
"""
import hashlib
import inspect
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

from app.channels.telegram import NormalizedMessage, TelegramNormalizer
from app.channels.whatsapp import WhatsAppNormalizer
from app.core.authz import AuthzError, ProjectAccess, require_mutating_role, require_owner_role, valid_principal
from app.core.contracts import OperationResult
from app.core.lifecycle import LifecycleError, ProjectLifecycle, has_outgoing_transitions
from app.projects.directions import direction_choice_pending


@dataclass(frozen=True)
class AuthenticatedTelegramContext:
    user_id: str
    conversation_id: str

    @property
    def principal_id(self):
        if not valid_principal(self.user_id) or not valid_principal(self.conversation_id):
            raise AuthzError()
        return "telegram:" + self.user_id


@dataclass(frozen=True)
class AuthenticatedWhatsAppContext:
    """Trusted WhatsApp principal, derived ONLY after webhook signature
    verification (see app.channels.whatsapp.verify_webhook_signature).

    ``user_id``/``conversation_id`` are the verified WhatsApp sender
    identifier (Meta's ``messages[0].from``). Website Builder only ever
    exchanges direct 1:1 messages with a sender, so both fields are the
    same verified identifier -- mirroring how Telegram's private-chat
    principal derivation collapses user_id/conversation_id for DMs.
    """

    user_id: str
    conversation_id: str

    @property
    def principal_id(self):
        if not valid_principal(self.user_id) or not valid_principal(self.conversation_id):
            raise AuthzError()
        return "whatsapp:" + self.user_id


def dispatch_normalized(dispatcher, message, project_id, action, *, authenticated=None, seq=None,
                       reference_token=None, data=None, role=None, url=None, brief=None,
                       index=None, hostname=None, ownership_claim=False):
    if not isinstance(authenticated, (AuthenticatedWhatsAppContext, AuthenticatedTelegramContext)):
        raise AuthzError()
    return dispatcher.dispatch(
        message,
        project_id,
        action,
        authenticated=authenticated,
        seq=seq,
        reference_token=reference_token,
        data=data,
        role=role,
        url=url,
        brief=brief,
        index=index,
        hostname=hostname,
        ownership_claim=ownership_claim,
    )


class TelegramDispatcher:
    def __init__(self, store, intake, revise=None, promote=None, workspace_for=None,
                 reference_intake=None, directions=None, domain=None, builder=None, preview=None):
        self.store, self.intake = store, intake
        self.revise, self.promote = revise, promote
        self.workspace_for = workspace_for
        self.access = ProjectAccess(store)
        self.reference_intake, self.directions = reference_intake, directions
        self.domain, self.builder = domain, builder
        self.preview = preview or getattr(builder, "preview_orchestrator", None)

    def dispatch(self, payload, project_id, action, *, authenticated=None, seq=None,
                 reference_token=None, data=None, role=None, url=None, brief=None,
                 index=None, hostname=None, ownership_claim=False, claim_suffix=None):
        try:
            if isinstance(authenticated, AuthenticatedTelegramContext):
                normalizer = TelegramNormalizer
            elif isinstance(authenticated, AuthenticatedWhatsAppContext):
                normalizer = WhatsAppNormalizer
            else:
                raise AuthzError()
            principal = authenticated.principal_id
            try:
                if isinstance(payload, NormalizedMessage):
                    message = payload
                else:
                    message = normalizer.normalize(payload)
            except (AttributeError, TypeError, ValueError):
                raise AuthzError() from None
            if (message is None or message.user_id != authenticated.user_id
                    or message.conversation_id != authenticated.conversation_id):
                raise AuthzError()
            if action == "create":
                channel = "whatsapp" if isinstance(authenticated, AuthenticatedWhatsAppContext) else "telegram"
                self.access.create(project_id, principal, channel=channel,
                                   conversation_id=authenticated.conversation_id)
                return OperationResult.ok({"project_id": project_id})
            if action == "read":
                return OperationResult.ok(self.access.read(project_id, principal))
            collaborators = {
                "intake": self.intake, "reference_upload": self.reference_intake,
                "reference_url": self.reference_intake, "directions_propose": self.directions,
                "directions_choose": self.directions, "domain_connect": self.domain,
                "domain_prepare": self.domain, "domain_verify": self.domain,
                "build": self.builder, "revise": self.revise,
                "approve": self.promote, "publish": self.promote,
                "reconcile_preview": self.preview,
            }
            if collaborators.get(action) is None:
                return OperationResult.fail("UNSUPPORTED_ACTION", error_code="UNSUPPORTED_ACTION")
            domain_action = action in {"domain_connect", "domain_prepare", "domain_verify"}
            if (domain_action or action in {"directions_propose", "publish", "reconcile_preview"}) and self.workspace_for is None:
                return OperationResult.fail("UNSUPPORTED_ACTION", error_code="UNSUPPORTED_ACTION")
            key = hashlib.sha256(json.dumps(
                [principal, authenticated.conversation_id, message.event_id],
                separators=(",", ":")).encode()).hexdigest()
            if claim_suffix:
                # Internal, non-user-initiated dispatches that share a Telegram
                # event with the user's own turn (e.g. the pre-turn
                # reconcile_preview) MUST NOT consume the event-derived key —
                # that key belongs to the user's action. Deriving a distinct
                # sub-key (same pattern as the ":auto_build" sub-claim below)
                # keeps "one Telegram event -> one user claim" while letting an
                # internal reconcile run under its own claim without colliding
                # with the turn's intake/revise/approve/publish dispatch.
                key = hashlib.sha256((key + claim_suffix).encode()).hexdigest()
            with self.store.acquire_writer(project_id) as state:
                guard = require_owner_role if domain_action or action == "publish" else require_mutating_role
                guard(state, principal, reference_token)
                existing = state.dispatch_events.get(key)
                if existing:
                    if existing.get("action") != action:
                        return OperationResult.fail("EVENT_ACTION_MISMATCH", error_code="EVENT_ACTION_MISMATCH")
                    if existing.get("status") == "DONE":
                        return OperationResult.ok({"duplicate": True})
                    # Structured failure can still follow a partial remote write.
                    return OperationResult.fail("EVENT_RECONCILIATION_REQUIRED", error_code="EVENT_RECONCILIATION_REQUIRED")
                if domain_action and ownership_claim is not True:
                    return OperationResult.fail("OWNERSHIP_CLAIM_REQUIRED", error_code="OWNERSHIP_CLAIM_REQUIRED")
                if action.startswith("directions_") or action.startswith("reference_"):
                    if (state.lifecycle not in {"DISCOVERING", "READY"}
                            or state.revisions.source_revision != 0 or state.pause_state.get("paused")):
                        prefix = "DIRECTIONS" if action.startswith("directions_") else "REFERENCE"
                        code = prefix + "_NOT_ALLOWED_IN_LIFECYCLE"
                        return OperationResult.fail(code, error_code=code)
                if action == "reconcile_preview":
                    if (state.lifecycle != "PREVIEW_READY"
                            or not state.deployment.get("tested_snapshot")
                            or state.pause_state.get("paused")):
                        return OperationResult.fail("PREVIEW_RECONCILIATION_NOT_ALLOWED", error_code="PREVIEW_RECONCILIATION_NOT_ALLOWED")
                if action == "build":
                    if direction_choice_pending(state):
                        return OperationResult.fail("DIRECTION_CHOICE_PENDING", error_code="DIRECTION_CHOICE_PENDING")
                    if (state.lifecycle != "READY" or state.revisions.source_revision != 0
                            or state.pause_state.get("paused")):
                        return OperationResult.fail("BUILD_NOT_ALLOWED_IN_LIFECYCLE", error_code="BUILD_NOT_ALLOWED_IN_LIFECYCLE")
                    self.store.transition_lifecycle_locked(state, ProjectLifecycle.QUEUED)
                if action == "intake":
                    # Intake drives lifecycle transitions internally (pause,
                    # resume, ready, clarification). A state with no outgoing
                    # edge can never accept any of them, so reject it HERE —
                    # before the claim write — with an honest code. Letting
                    # apply_to_project raise instead produced a
                    # LifecycleError that the generic handler below reported
                    # as EVENT_RECONCILIATION_REQUIRED, telling the user a
                    # previous operation was ambiguous when nothing had
                    # happened at all.
                    try:
                        _lc = ProjectLifecycle(state.lifecycle)
                    except ValueError:
                        _lc = None
                    if _lc is None or not has_outgoing_transitions(_lc):
                        return OperationResult.fail(
                            "PROJECT_NOT_ACCEPTING_INPUT",
                            error_code="PROJECT_NOT_ACCEPTING_INPUT",
                        )
                persisted_brief = dict(state.brief)
                # H-6: ALL build entry paths must use the same remote-boundary
                # semantics. The build claim starts with reached_remote=False
                # so a purely local/pre-remote failure (e.g.
                # CHEAP_CHECKS_FAILED before any Vercel call) is provably
                # retryable. Legacy claims with a missing flag stay UNKNOWN.
                if action in {"build", "revise"}:
                    state.dispatch_events[key] = {
                        "action": action,
                        "status": "CLAIMED",
                        "reached_remote": False,
                    }
                else:
                    state.dispatch_events[key] = {"action": action, "status": "CLAIMED"}
                # Bound the ledger atomically with this append, so a crash can
                # never leave it half-pruned. Pruning never drops a CLAIMED
                # entry, so the claim just written here is always kept.
                state.prune_bounded_ledgers()
                self.store.save(state)
            # H-6: ONE shared remote-boundary helper used by BOTH build entry
            # paths (explicit "build" and the intake auto-build sub-claim). It
            # durably flips the named claim's reached_remote=True IMMEDIATELY
            # BEFORE the first possible remote (Vercel) side effect. If that
            # persistence fails the callback raises, build() aborts before the
            # remote call, and no duplicate external side effect can occur.
            # Defined unconditionally so both branches close over the SAME
            # function object.
            def _mark_build_reached_remote(build_claim_key):
                with self.store.acquire_writer(project_id) as rs:
                    claim = rs.dispatch_events.get(build_claim_key)
                    if claim is None or claim.get("status") != "CLAIMED":
                        raise RuntimeError(
                            "build claim is no longer claimed; refusing remote boundary"
                        )
                    claim["reached_remote"] = True
                    self.store.save(rs)

            auth = {"principal_id": principal, "reference_token": reference_token}
            try:
                if action == "intake":
                    intake_result = self.intake.process(message, project_id)
                    self.intake.apply_to_project(project_id, intake_result, principal_id=principal, event_id="dispatch:" + key)
                    # Surface the persisted intake outcome so the caller can
                    # reply/clarify without re-interpreting or re-persisting.
                    result = OperationResult.ok({
                        "readiness": intake_result.readiness.value,
                        "scope": intake_result.scope.value,
                        "clarification_question": intake_result.clarification_question,
                        "pause_detected": intake_result.pause_detected,
                        "resume_detected": intake_result.resume_detected,
                    })
                    # Auto-trigger build when intake completed the
                    # requirements gate, as a SEPARATE sub-claim keyed off
                    # this same event. This keeps "one Telegram event -> one
                    # dispatch claim" for intake while still admitting the
                    # build under its own claim, so replay of the SAME
                    # update never triggers a second build and the caller
                    # never needs a second top-level dispatch() call whose
                    # event-derived key would otherwise collide with this
                    # intake claim (same principal/conversation/event_id).
                    if self.builder is not None:
                        build_key = hashlib.sha256(
                            (key + ":auto_build").encode()
                        ).hexdigest()
                        with self.store.acquire_writer(project_id) as bstate:
                            existing_claim = bstate.dispatch_events.get(build_key)
                            # F4: a FAILED build claim is retryable ONLY when
                            # the build provably never reached a remote side
                            # effect (reached_remote is explicitly False). A
                            # claim that is DONE, reached remote
                            # (reached_remote True), or lacks the evidence
                            # entirely (legacy/missing -> UNKNOWN) stays
                            # fail-closed -- a possibly-remote build must never
                            # be replayed.
                            retryable_pre_remote = (
                                existing_claim is not None
                                and existing_claim.get("action") == "build"
                                and existing_claim.get("status") == "FAILED"
                                and existing_claim.get("reached_remote") is False
                            )
                            admissible = (
                                (existing_claim is None or retryable_pre_remote)
                                and bstate.lifecycle == "READY"
                                and bstate.revisions.source_revision == 0
                                and not bstate.pause_state.get("paused")
                                and not direction_choice_pending(bstate)
                            )
                            if admissible:
                                self.store.transition_lifecycle_locked(bstate, ProjectLifecycle.QUEUED)
                                auto_build_brief = dict(bstate.brief)
                                # Stamp reached_remote=False at claim time so
                                # the flag exists before any side effect; the
                                # builder flips it to True only just before the
                                # first remote (Vercel) call.
                                bstate.dispatch_events[build_key] = {
                                    "action": "build",
                                    "status": "CLAIMED",
                                    "reached_remote": False,
                                }
                                self.store.save(bstate)
                            else:
                                auto_build_brief = None
                        if auto_build_brief is not None:
                            # H-6: reuse the SAME remote-boundary helper as the
                            # explicit build path (defined above, keyed by the
                            # claim key) so both entry paths durably flip
                            # reached_remote=True before the first Vercel call.
                            _auto_boundary = (
                                lambda bk=build_key: _mark_build_reached_remote(bk)
                            )
                            builder = self.builder
                            build_attr = getattr(builder, "build", None)
                            build_sig = (
                                inspect.signature(build_attr)
                                if callable(build_attr) else None
                            )

                            def _invoke_build(pid, brf, boundary):
                                # Only pass on_remote_boundary when the
                                # collaborator can accept it; test doubles and
                                # legacy builders with the pre-boundary
                                # 2-argument signature stay supported.
                                if build_sig is not None and (
                                    "on_remote_boundary" in build_sig.parameters
                                    or any(
                                        p.kind == inspect.Parameter.VAR_KEYWORD
                                        for p in build_sig.parameters.values()
                                    )
                                ):
                                    return builder.build(
                                        pid, brf, on_remote_boundary=boundary
                                    )
                                return builder.build(pid, brf)

                            try:
                                build_result = _invoke_build(
                                    project_id, auto_build_brief, _auto_boundary,
                                )
                            except Exception:
                                build_result = OperationResult.fail(
                                    "EVENT_RECONCILIATION_REQUIRED",
                                    error_code="EVENT_RECONCILIATION_REQUIRED",
                                )
                            # Record the build sub-claim's terminal status in
                            # its OWN protected block. Previously unguarded, so a
                            # lock timeout or a vanished sub-claim raised here
                            # and was caught by the OUTER handler, which
                            # finalizes the INTAKE claim and reported
                            # EVENT_RECONCILIATION_REQUIRED — telling the user
                            # that an operation needed reconciliation when the
                            # intake had in fact succeeded. A failure to write
                            # this bookkeeping must never overwrite a real
                            # intake outcome; the sub-claim stays CLAIMED and
                            # fails closed on its own replay.
                            build_success = bool(getattr(build_result, "success", False))
                            try:
                                with self.store.acquire_writer(project_id) as bstate:
                                    sub_claim = bstate.dispatch_events.get(build_key)
                                    if sub_claim is not None:
                                        sub_claim["status"] = (
                                            "DONE" if build_success else "FAILED"
                                        )
                                        sub_claim["build_diagnostics"] = dict(
                                            getattr(build_result, "diagnostics", {}) or {}
                                        )
                                        self.store.save(bstate)
                            except Exception:
                                logging.getLogger(__name__).exception(
                                    "Failed to persist auto-build sub-claim for "
                                    "project '%s'; sub-claim left unchanged",
                                    project_id,
                                )
                            result.data["build_triggered"] = True
                            result.data["build_success"] = build_success
                            result.data["build_reached_remote"] = bool(
                                getattr(build_result, "reached_remote", False)
                            )
                            result.data["build_diagnostics"] = dict(
                                getattr(build_result, "diagnostics", {}) or {}
                            )
                            if not build_success:
                                # Prefer a real string error_code so the reply
                                # can be mapped to specific copy; fall back to
                                # `error` (which may be free text, and is
                                # already a stable code in every real result).
                                # The isinstance guard matters: a collaborator
                                # that omits the attribute would otherwise
                                # surface an auto-created sentinel here.
                                _code = getattr(build_result, "error_code", None)
                                result.data["build_error"] = (
                                    _code if isinstance(_code, str) and _code
                                    else getattr(build_result, "error", None)
                                )
                elif action == "reference_upload":
                    result = self.reference_intake.add_upload(project_id, data, role, **auth)
                elif action == "reference_url":
                    result = self.reference_intake.add_url(project_id, url, role, **auth)
                elif action == "directions_propose":
                    result = self.directions.propose(project_id, persisted_brief, self.workspace_for(project_id), **auth)
                elif action == "directions_choose":
                    result = self.directions.choose_direction(project_id, index, **auth)
                elif domain_action:
                    method = {"domain_connect": "connect", "domain_prepare": "prepare", "domain_verify": "verify"}[action]
                    result = getattr(self.domain, method)(project_id, hostname, self.workspace_for(project_id), ownership_claim=True, **auth)
                elif action == "build":
                    builder = self.builder
                    build_attr = getattr(builder, "build", None)
                    if build_attr is not None and not callable(build_attr):
                        build_attr = None
                    build_sig = (
                        inspect.signature(build_attr)
                        if build_attr is not None else None
                    )
                    # Zero-arg closure over THIS claim's key -- the same
                    # durable-before-remote semantics as the auto-build path.
                    _explicit_boundary = (
                        lambda bk=key: _mark_build_reached_remote(bk)
                    )
                    try:
                        if build_sig is not None and (
                            "on_remote_boundary" in build_sig.parameters
                            or any(
                                p.kind == inspect.Parameter.VAR_KEYWORD
                                for p in build_sig.parameters.values()
                            )
                        ):
                            result = builder.build(
                                project_id, persisted_brief,
                                on_remote_boundary=_explicit_boundary,
                            )
                        else:
                            result = builder.build(project_id, persisted_brief)
                    except TypeError as exc:
                        # A builder that cannot accept the keyword (pre-boundary
                        # collaborator / test double) must be retried with the
                        # legacy signature so no dispatch path regresses.
                        if "on_remote_boundary" not in str(exc):
                            raise
                        result = builder.build(project_id, persisted_brief)
                elif action == "revise":
                    result = self.revise.reserve(project_id, seq, principal_id=principal)
                    if result.success:
                        revise_sig = inspect.signature(self.revise.apply)
                        if 'on_remote_boundary' in revise_sig.parameters or any(
                            p.kind == inspect.Parameter.VAR_KEYWORD
                            for p in revise_sig.parameters.values()
                        ):
                            result = self.revise.apply(
                                project_id, seq, message.text,
                                principal_id=principal,
                                on_remote_boundary=lambda: _mark_build_reached_remote(key),
                            )
                        else:
                            result = self.revise.apply(
                                project_id, seq, message.text, principal_id=principal,
                            )
                elif action == "approve":
                    result = self.promote.approve(project_id, principal_id=principal)
                elif action == "publish":
                    approval = self.promote.approve(project_id, principal_id=principal)
                    if not approval.success:
                        result = approval
                    else:
                        result = self.promote.promote(project_id, self.workspace_for(project_id), principal_id=principal)
                elif action == "reconcile_preview":
                    result = self.preview.run_owned(project_id, self.workspace_for(project_id))
                else:
                    result = self.promote.promote(project_id, self.workspace_for(project_id), principal_id=principal)
            except LifecycleError:
                # A lifecycle precondition rejected this turn. No remote side
                # effect was attempted and none can have been partially
                # applied, so this is definitively NOT an ambiguous operation.
                # Finalize the claim FAILED (so nothing stays CLAIMED) but
                # report the honest code instead of
                # EVENT_RECONCILIATION_REQUIRED, which would invite a retry
                # that can never succeed.
                logger.info(
                    "Lifecycle precondition rejected action '%s' on project '%s'",
                    action, project_id,
                )
                try:
                    with self.store.acquire_writer(project_id) as state:
                        claim = state.dispatch_events.get(key)
                        if claim is not None and claim.get("status") == "CLAIMED":
                            claim["status"] = "FAILED"
                        self.store.save(state)
                except Exception:
                    logger.exception(
                        "Failed to finalize dispatch claim for action '%s' on project '%s'",
                        action, project_id,
                    )
                return OperationResult.fail(
                    "PROJECT_NOT_ACCEPTING_INPUT",
                    error_code="PROJECT_NOT_ACCEPTING_INPUT",
                )
            except Exception as exc:
                # H-5: EVERY dispatch attempt must leave its durable claim in a
                # meaningful terminal or recoverable state. A collaborator
                # raising must NOT leave the claim stuck at CLAIMED (which
                # would make every later replay fail closed forever and wedge
                # the project). Classification depends on remote-boundary
                # evidence, never on the exception type:
                #   reached_remote is False  -> proven pre-remote failure ->
                #       the existing retryable FAILED state
                #   True / missing / unknown -> fail closed, reconciliation
                #       required, no replay
                logger.exception(
                    "Unexpected error during dispatch of action '%s' on project '%s': %s",
                    action, project_id, exc,
                )
                try:
                    with self.store.acquire_writer(project_id) as state:
                        claim = state.dispatch_events.get(key)
                        if claim is not None and claim.get("status") == "CLAIMED":
                            claim["status"] = "FAILED"
                        self.store.save(state)
                except Exception:
                    # Finalization persistence itself failed. Do not crash out
                    # of dispatch and do not fabricate a terminal status we
                    # could not persist: the durable claim (CLAIMED) already
                    # fails closed on replay. Report reconciliation-required.
                    logger.exception(
                        "Failed to finalize dispatch claim for action '%s' on "
                        "project '%s'; retaining fail-closed semantics",
                        action, project_id,
                    )
                return OperationResult.fail(
                    "EVENT_RECONCILIATION_REQUIRED",
                    error_code="EVENT_RECONCILIATION_REQUIRED",
                )
            if action == "revise" and not result.success:
                result.data = dict(getattr(result, "data", {}) or {})
                diagnostics = dict(getattr(result, "diagnostics", {}) or {})
                if diagnostics:
                    result.data["revision_diagnostics"] = diagnostics
            try:
                with self.store.acquire_writer(project_id) as state:
                    claim = state.dispatch_events.get(key)
                    if claim is not None:
                        claim["status"] = "DONE" if result.success else "FAILED"
                        if action == "revise" and not result.success:
                            claim["revision_diagnostics"] = dict(
                                result.data.get("revision_diagnostics", {}) or {}
                            )
                        self.store.save(state)
            except Exception:
                # The action already completed (result in hand). A persistence
                # failure here must never escape as an unhandled crash and must
                # never mutate the durable claim into a state that could allow
                # a blind replay: the claim is left as-is (CLAIMED / prior
                # status) and the real result is still returned.
                logger.exception(
                    "Failed to persist final status for dispatch of action '%s' "
                    "on project '%s'; durable claim left unchanged",
                    action, project_id,
                )
            return result
        except AuthzError as exc:
            return OperationResult.fail(exc.error_code, error_code=exc.error_code)
        except TimeoutError:
            # The exclusive writer lock could not be acquired (a concurrent
            # writer held it past the timeout). This is a transient
            # contention outcome, not an ambiguous remote operation, and it
            # must not be reported as EVENT_RECONCILIATION_REQUIRED: nothing
            # external was touched. No claim was written on the paths that
            # can reach here before the claim block, so none is left stale.
            logger.warning(
                "Writer lock timeout during dispatch of action '%s' on project '%s'",
                action, project_id,
            )
            return OperationResult.fail(
                "WRITER_LOCK_TIMEOUT", error_code="WRITER_LOCK_TIMEOUT"
            )
        except Exception:
            # dispatch() is the single mutation authority: it must never raise
            # into the receive loop, and it must never leave a durable claim
            # behind in a state that is neither terminal nor recoverable. The
            # per-action handlers above already finalized their own claims;
            # anything arriving here did so before or outside a claim.
            logger.exception(
                "Unhandled error during dispatch of action '%s' on project '%s'",
                action, project_id,
            )
            return OperationResult.fail(
                "DISPATCH_INTERNAL_ERROR", error_code="DISPATCH_INTERNAL_ERROR"
            )
