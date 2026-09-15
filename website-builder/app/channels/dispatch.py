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
import json
from dataclasses import dataclass

from app.channels.telegram import TelegramNormalizer
from app.channels.whatsapp import WhatsAppNormalizer
from app.core.authz import AuthzError, ProjectAccess, require_mutating_role, require_owner_role, valid_principal
from app.core.contracts import OperationResult
from app.core.lifecycle import ProjectLifecycle
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


class TelegramDispatcher:
    def __init__(self, store, intake, revise=None, promote=None, workspace_for=None,
                 reference_intake=None, directions=None, domain=None, builder=None):
        self.store, self.intake = store, intake
        self.revise, self.promote = revise, promote
        self.workspace_for = workspace_for
        self.access = ProjectAccess(store)
        self.reference_intake, self.directions = reference_intake, directions
        self.domain, self.builder = domain, builder

    def dispatch(self, payload, project_id, action, *, authenticated=None, seq=None,
                 reference_token=None, data=None, role=None, url=None, brief=None,
                 index=None, hostname=None, ownership_claim=False):
        try:
            if isinstance(authenticated, AuthenticatedTelegramContext):
                normalizer = TelegramNormalizer
            elif isinstance(authenticated, AuthenticatedWhatsAppContext):
                normalizer = WhatsAppNormalizer
            else:
                raise AuthzError()
            principal = authenticated.principal_id
            try:
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
            }
            if collaborators.get(action) is None:
                return OperationResult.fail("UNSUPPORTED_ACTION", error_code="UNSUPPORTED_ACTION")
            domain_action = action in {"domain_connect", "domain_prepare", "domain_verify"}
            if (domain_action or action in {"directions_propose", "publish"}) and self.workspace_for is None:
                return OperationResult.fail("UNSUPPORTED_ACTION", error_code="UNSUPPORTED_ACTION")
            key = hashlib.sha256(json.dumps(
                [principal, authenticated.conversation_id, message.event_id],
                separators=(",", ":")).encode()).hexdigest()
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
                if action == "build":
                    if direction_choice_pending(state):
                        return OperationResult.fail("DIRECTION_CHOICE_PENDING", error_code="DIRECTION_CHOICE_PENDING")
                    if (state.lifecycle != "READY" or state.revisions.source_revision != 0
                            or state.pause_state.get("paused")):
                        return OperationResult.fail("BUILD_NOT_ALLOWED_IN_LIFECYCLE", error_code="BUILD_NOT_ALLOWED_IN_LIFECYCLE")
                    self.store.transition_lifecycle_locked(state, ProjectLifecycle.QUEUED)
                persisted_brief = dict(state.brief)
                state.dispatch_events[key] = {"action": action, "status": "CLAIMED"}
                self.store.save(state)
            auth = {"principal_id": principal, "reference_token": reference_token}
            try:
                if action == "intake":
                    result = self.intake.process(message, project_id)
                    self.intake.apply_to_project(project_id, result, principal_id=principal, event_id="dispatch:" + key)
                    result = OperationResult.ok({"readiness": result.readiness.value})
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
                    result = self.builder.build(project_id, persisted_brief)
                elif action == "revise":
                    result = self.revise.reserve(project_id, seq, principal_id=principal)
                    if result.success:
                        result = self.revise.apply(project_id, seq, message.text, principal_id=principal)
                elif action == "approve":
                    result = self.promote.approve(project_id, principal_id=principal)
                else:
                    result = self.promote.promote(project_id, self.workspace_for(project_id), principal_id=principal)
            except Exception:
                return OperationResult.fail("EVENT_RECONCILIATION_REQUIRED", error_code="EVENT_RECONCILIATION_REQUIRED")
            with self.store.acquire_writer(project_id) as state:
                state.dispatch_events[key]["status"] = "DONE" if result.success else "FAILED"
                self.store.save(state)
            return result
        except AuthzError as exc:
            return OperationResult.fail(exc.error_code, error_code=exc.error_code)
