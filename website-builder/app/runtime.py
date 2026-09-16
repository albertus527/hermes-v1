"""Production runtime composition root for Website Builder R1 Milestone A.

Constructs the existing collaborators and wires them into a single canonical
executable process. No new agent framework, model role, queue, database, or
service is introduced — this module only composes what already exists.

Canonical invocation:
    cd website-builder
    python -m app
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import yaml

from app.channels.dispatch import AuthenticatedTelegramContext, TelegramDispatcher
from app.channels.telegram import NormalizedMessage, TelegramNormalizer
from app.core.intake import IntakeProcessor
from app.core.state import ProjectStateStore
from app.deploy.adapters import TelegramAdapter, UrllibHttpTransport, VercelAdapter
from app.deploy.git_output import OutputGitRepository
from app.deploy.preview import PreviewDeps, PreviewOrchestrator
from app.hermes.adapter import HermesAdapter
from app.projects.build import FrontendBuilder
from app.projects.directions import DirectionsOrchestrator
from app.projects.domain import CustomDomainOrchestrator, DomainDeps
from app.projects.promote import PromoteDeps, PromotionOrchestrator
from app.projects.references import ReferenceIntake
from app.projects.revise import RevisionOrchestrator
from app.sandbox.runner import ProjectRunner

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class ConfigurationError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""


@dataclass(frozen=True)
class RuntimeConfig:
    """Validated runtime configuration. Secrets are never logged."""

    telegram_bot_token: str = field(repr=False)
    hermes_home: Path
    workspace_root: Path
    state_root: Path
    output_repo_path: Path
    vercel_token: str = field(repr=False)
    vercel_team_id: str
    vercel_ownership_namespace: str
    web3forms_access_key: Optional[str] = field(default=None, repr=False)
    smoke_browser_factory: Optional[Any] = field(default=None, repr=False)

    def __post_init__(self):
        # Expand ~ in paths
        object.__setattr__(self, "hermes_home", Path(self.hermes_home).expanduser())
        object.__setattr__(self, "workspace_root", Path(self.workspace_root).expanduser())
        object.__setattr__(self, "state_root", Path(self.state_root).expanduser())
        object.__setattr__(self, "output_repo_path", Path(self.output_repo_path).expanduser())


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"Missing required environment variable: {name}")
    return value


def _optional_env(name: str) -> Optional[str]:
    value = os.environ.get(name, "").strip()
    return value if value else None


def load_runtime_config(config_path: Optional[Path] = None) -> RuntimeConfig:
    """Load and validate runtime configuration from config.yaml + environment.

    Fails closed with a clear error when a required dependency is unavailable.
    Never prints secret values.
    """
    # Load non-secret defaults from config.yaml
    cfg: Dict[str, Any] = {}
    default_cfg_path = Path(__file__).parent.parent / "config" / "default.yaml"
    path = config_path or default_cfg_path
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    wb = cfg.get("website_builder", {})

    # Required secrets from environment
    telegram_bot_token = _required_env("TELEGRAM_BOT_TOKEN")
    vercel_token = _required_env("VERCEL_TOKEN")
    vercel_team_id = _required_env("VERCEL_TEAM_ID")

    # Optional secrets
    web3forms_access_key = _optional_env("WEB3FORMS_ACCESS_KEY")

    # Paths — env overrides config, config overrides defaults
    hermes_home = Path(
        os.environ.get("HERMES_HOME", "").strip()
        or wb.get("hermes_home", "~/.hermes-website")
    )
    workspace_root = Path(
        os.environ.get("WEBSITE_BUILDER_WORKSPACE_ROOT", "").strip()
        or wb.get("workspace_root", "~/website-workspaces")
    )
    state_root = Path(
        os.environ.get("WEBSITE_BUILDER_STATE_ROOT", "").strip()
        or wb.get("state_root", "~/.website-builder/state")
    )
    output_repo_path = Path(
        os.environ.get("WEBSITE_BUILDER_OUTPUT_REPO", "").strip()
        or wb.get("output_repo", "~/.website-builder/output-repo")
    )

    # Vercel ownership namespace — a stable installation ID, not a secret.
    # Derived from team_id + a configurable salt so reinstalls don't collide.
    vercel_ownership_namespace = os.environ.get(
        "VERCEL_OWNERSHIP_NAMESPACE", ""
    ).strip() or f"wb-{vercel_team_id}"

    # Smoke browser factory — optional; if absent, preview/promotion smoke
    # tests will fail closed at runtime (which is the correct behavior).
    smoke_browser_factory = _load_smoke_browser_factory()

    return RuntimeConfig(
        telegram_bot_token=telegram_bot_token,
        hermes_home=hermes_home,
        workspace_root=workspace_root,
        state_root=state_root,
        output_repo_path=output_repo_path,
        vercel_token=vercel_token,
        vercel_team_id=vercel_team_id,
        vercel_ownership_namespace=vercel_ownership_namespace,
        web3forms_access_key=web3forms_access_key,
        smoke_browser_factory=smoke_browser_factory,
    )


def _load_smoke_browser_factory():
    """Load the Playwright browser factory for smoke tests.

    Returns None when Playwright is not installed — smoke tests will then
    fail closed at runtime, which is the correct behavior for an optional
    dependency.
    """
    try:
        from playwright.sync_api import sync_playwright

        def factory():
            pw = sync_playwright().start()
            return pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )

        return factory
    except ImportError:
        logger.warning(
            "Playwright not installed — preview/production smoke tests will fail closed. "
            "Install with: pip install playwright && playwright install chromium"
        )
        return None


# ---------------------------------------------------------------------------
# Startup role preflight
# ---------------------------------------------------------------------------


# Expected per-role schema surfaced in startup diagnostics. Operator-owned —
# the runtime never invents or seeds concrete model/provider mappings.
ROLE_CONFIG_SCHEMA_HINT = (
    "website_builder.models.<ROLE>.model (non-empty string) and "
    "website_builder.models.<ROLE>.provider (non-empty string) "
    "for <ROLE> in FAST, FRONTEND, VISION"
)


def preflight_role_validation(config: RuntimeConfig) -> bool:
    """Fail-closed startup validation of all Website Builder model roles.

    Resolves FAST/FRONTEND/VISION against the REAL profile configuration
    before any polling begins. Aggregates per-role diagnostics and logs the
    exact profile config path plus the expected schema. Never logs secrets,
    never invents operator configuration, and never starts the runtime when
    any role is missing or malformed.

    Returns True only when every required role resolves (and VISION passes
    the image-input capability gate).
    """
    from app.hermes.adapter import HermesAdapter

    probe = HermesAdapter(store=None, hermes_home=config.hermes_home)  # type: ignore[arg-type]
    report = probe.validate_role_configuration()

    if report["ok"]:
        logger.info("Model role preflight OK: FAST, FRONTEND, VISION")
        return True

    logger.error(
        "Website Builder model role configuration is incomplete — refusing to start."
    )
    for role in sorted(report["errors"]):
        logger.error("  role %s: %s", role, report["errors"][role])
    logger.error("Profile config path: %s", report["config_path"])
    logger.error("Expected schema: %s", ROLE_CONFIG_SCHEMA_HINT)
    logger.error(
        "Set these keys in the Website Builder profile config (no defaults are "
        "invented; this installation is operator-configured)."
    )
    return False


# ---------------------------------------------------------------------------
# Composition root
# ---------------------------------------------------------------------------


@dataclass
class RuntimeComposition:
    """The fully wired Website Builder runtime."""

    config: RuntimeConfig
    store: ProjectStateStore
    runner: ProjectRunner
    hermes: HermesAdapter
    intake: IntakeProcessor
    references: ReferenceIntake
    directions: DirectionsOrchestrator
    builder: FrontendBuilder
    revise: RevisionOrchestrator
    promote: PromotionOrchestrator
    domain: CustomDomainOrchestrator
    preview: PreviewOrchestrator
    telegram_out: TelegramAdapter
    dispatcher: TelegramDispatcher


def compose(config: RuntimeConfig) -> RuntimeComposition:
    """Construct and inject all existing collaborators.

    Uses existing constructors/contracts only. No new logic is introduced.
    """
    # Ensure directories exist
    config.hermes_home.mkdir(parents=True, exist_ok=True)
    config.workspace_root.mkdir(parents=True, exist_ok=True)
    config.state_root.mkdir(parents=True, exist_ok=True)

    # Core state
    store = ProjectStateStore(config.state_root)

    # Sandbox runner (MAX_WORKERS=1)
    runner = ProjectRunner(
        workspace_root=config.workspace_root,
        state_store=store,
        hermes_home=config.hermes_home,
    )

    # Hermes adapter (FAST/FRONTEND/VISION)
    hermes = HermesAdapter(
        store=store,
        hermes_home=config.hermes_home,
    )

    # Intake processor
    intake = IntakeProcessor(store, hermes_adapter=hermes)

    # Reference intake
    references = ReferenceIntake(store, hermes_adapter=hermes)

    # Directions orchestrator
    directions = DirectionsOrchestrator(store, hermes_adapter=hermes)

    # Telegram outbound adapter (reuse existing from deploy/adapters.py)
    telegram_out = TelegramAdapter(config.telegram_bot_token)

    # Vercel adapter
    vercel = VercelAdapter(
        token=config.vercel_token,
        team_id=config.vercel_team_id,
        ownership_namespace=config.vercel_ownership_namespace,
    )

    # Output Git repository
    output_repo = OutputGitRepository(
        config.output_repo_path,
        hermes_root=Path(__file__).parent.parent.parent,
    )

    # Chat ID resolver: project owner conversation ID
    def chat_id_for(project_id: str, state) -> Optional[str]:
        return state.conversation_id

    # Preview orchestrator deps
    preview_deps = PreviewDeps(
        vercel=vercel,
        telegram=telegram_out,
        smoke=config.smoke_browser_factory,
        output_repo=output_repo,
        chat_id_for=chat_id_for,
    )
    preview = PreviewOrchestrator(store, preview_deps)

    # Frontend builder
    builder = FrontendBuilder(
        runner=runner,
        store=store,
        hermes_adapter=hermes,
        preview_orchestrator=preview,
        web3forms_access_key=config.web3forms_access_key,
    )

    # Revision orchestrator
    revise = RevisionOrchestrator(
        runner=runner,
        store=store,
        hermes_adapter=hermes,
        preview_orchestrator=preview,
        web3forms_access_key=config.web3forms_access_key,
    )

    # Promotion orchestrator deps
    promote_deps = PromoteDeps(
        vercel=vercel,
        telegram=telegram_out,
        smoke=config.smoke_browser_factory,
        chat_id_for=chat_id_for,
    )
    promote = PromotionOrchestrator(runner, store, promote_deps)

    # Custom domain orchestrator deps
    domain_deps = DomainDeps(
        vercel=vercel,
        smoke=config.smoke_browser_factory,
    )
    domain = CustomDomainOrchestrator(runner, store, domain_deps)

    # Workspace resolver for dispatcher
    def workspace_for(project_id: str) -> Path:
        return runner.create_workspace(project_id)

    # Central dispatcher — the ONLY mutation authority
    dispatcher = TelegramDispatcher(
        store=store,
        intake=intake,
        revise=revise,
        promote=promote,
        workspace_for=workspace_for,
        reference_intake=references,
        directions=directions,
        domain=domain,
        builder=builder,
    )

    return RuntimeComposition(
        config=config,
        store=store,
        runner=runner,
        hermes=hermes,
        intake=intake,
        references=references,
        directions=directions,
        builder=builder,
        revise=revise,
        promote=promote,
        domain=domain,
        preview=preview,
        telegram_out=telegram_out,
        dispatcher=dispatcher,
    )


# ---------------------------------------------------------------------------
# Telegram receive loop
# ---------------------------------------------------------------------------


class TelegramProviderError(RuntimeError):
    """Structured Telegram provider failure. Never contains the token."""


class ConversationIntent(str, Enum):
    """Bounded intent vocabulary for natural-conversation routing.

    AI interprets intent; deterministic application code retains authority.
    FAST classifies into this bounded set only. Never an arbitrary string.
    """

    INTAKE = "INTAKE"
    REVISE = "REVISE"
    APPROVE = "APPROVE"
    PUBLISH = "PUBLISH"


# Intents that are valid for each lifecycle state. FAST is only offered
# the subset that makes sense for the current project state.
_LIFECYCLE_INTENTS: Dict[str, List[ConversationIntent]] = {
    "DISCOVERING": [ConversationIntent.INTAKE],
    "WAITING_INPUT": [ConversationIntent.INTAKE],
    "READY": [ConversationIntent.INTAKE],
    "QUEUED": [ConversationIntent.INTAKE],
    "RUNNING": [ConversationIntent.INTAKE],
    "PREVIEW_READY": [
        ConversationIntent.INTAKE,
        ConversationIntent.REVISE,
        ConversationIntent.APPROVE,
        ConversationIntent.PUBLISH,
    ],
    "REVISION_REQUESTED": [ConversationIntent.INTAKE],
    "PUBLISHING": [ConversationIntent.INTAKE],
    "LIVE": [
        ConversationIntent.INTAKE,
        ConversationIntent.REVISE,
    ],
    "FAILED": [ConversationIntent.INTAKE],
    "PAUSED": [ConversationIntent.INTAKE],
    "CANCELED": [ConversationIntent.INTAKE],
}


class TelegramReceiveLoop:
    """Bounded getUpdates long-polling loop.

    Uses the official Telegram Bot API via stdlib urllib only.
    No third-party Telegram SDK.
    """

    def __init__(
        self,
        bot_token: str,
        dispatcher: TelegramDispatcher,
        telegram_out: TelegramAdapter,
        hermes: Optional[HermesAdapter] = None,
        transport=None,
        poll_timeout: int = 30,
        error_backoff: float = 5.0,
    ):
        self.bot_token = bot_token
        self.dispatcher = dispatcher
        self.telegram_out = telegram_out
        self.hermes = hermes
        self.transport = transport or UrllibHttpTransport()
        self.poll_timeout = poll_timeout
        self.error_backoff = error_backoff
        self._stop_event = threading.Event()
        self._offset: Optional[int] = None

    def stop(self) -> None:
        """Signal the loop to stop gracefully."""
        self._stop_event.set()

    def _get_updates(self) -> list:
        """Fetch updates from Telegram Bot API.

        Returns a list of Update dicts. Raises on provider failure.
        """
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        params = {"timeout": self.poll_timeout}
        if self._offset is not None:
            params["offset"] = self._offset

        full_url = url + "?" + urlencode(params)
        response = self.transport.request(
            "GET", full_url, headers={}, timeout=self.poll_timeout + 10
        )
        payload = json.loads(response.body)

        if not isinstance(payload, dict) or payload.get("ok") is not True:
            error_desc = (
                payload.get("description", "unknown")
                if isinstance(payload, dict)
                else "invalid response"
            )
            # Never log the token
            raise TelegramProviderError(f"Telegram getUpdates failed: {error_desc}")

        result = payload.get("result", [])
        if not isinstance(result, list):
            raise TelegramProviderError(
                "Telegram getUpdates returned malformed result"
            )

        return result

    # ------------------------------------------------------------------
    # Natural-conversation intent classification
    # ------------------------------------------------------------------

    def _classify_intent(
        self,
        text: str,
        lifecycle: str,
        project_id: Optional[str] = None,
    ) -> ConversationIntent:
        """Classify a natural-language turn into a bounded intent.

        Uses the EXISTING FAST Hermes role via the same zero-tool
        programmatic boundary as fast_interpret. AI interprets intent;
        deterministic application code retains authority over whether
        the resulting action is authorized or valid.

        Fail-safe: any FAST failure, malformed output, unsupported intent,
        or material ambiguity returns INTAKE — never a destructive action.
        """
        valid_intents = _LIFECYCLE_INTENTS.get(lifecycle, [ConversationIntent.INTAKE])

        # If only INTAKE is valid for this lifecycle, skip FAST entirely.
        if valid_intents == [ConversationIntent.INTAKE]:
            return ConversationIntent.INTAKE

        # If Hermes is unavailable, fall back to INTAKE.
        if self.hermes is None:
            return ConversationIntent.INTAKE

        # Build the bounded intent-classification prompt.
        intent_names = [i.value for i in valid_intents]
        prompt = self._build_intent_prompt(text, lifecycle, intent_names)

        try:
            result = self.hermes._run_fast_programmatic(
                prompt=prompt,
                role="FAST",
                skills=["website-builder-environment", "website-builder-product-scope"],
            )
        except Exception:
            return ConversationIntent.INTAKE

        if not result.success:
            return ConversationIntent.INTAKE

        return self._parse_intent_response(result.response, valid_intents)

    def _build_intent_prompt(
        self, text: str, lifecycle: str, valid_intents: List[str]
    ) -> str:
        """Build the bounded intent-classification prompt for FAST."""
        return f"""You are FAST, classifying a user's natural-language message
into exactly one bounded intent for Website Builder R1.

Current project lifecycle: {lifecycle}
Valid intents for this state: {', '.join(valid_intents)}

Intent definitions:
- INTAKE: Initial requirements, requirement continuation, clarification
  answers, ordinary pre-build conversation, or any ambiguous/unclear intent.
- REVISE: The user requests changes to the current preview/site (e.g. "make
  the hero smaller", "change the button color", "hero kegedean").
- APPROVE: The user clearly intends to accept the current preview (e.g.
  "looks good", "approve", "ok"). This binds the preview but does NOT publish.
- PUBLISH: The user clearly intends to go-live / publish to production
  (e.g. "oke live", "publish", "go live", "launch"). This requires an
  existing approval and triggers production promotion.

Rules:
- Respond with ONLY the intent name, nothing else.
- If the intent is ambiguous or could be multiple things, respond INTAKE.
- Never guess PUBLISH for ambiguous text. PUBLISH requires explicit go-live
  language.
- Never guess APPROVE for ambiguous text. APPROVE requires clear acceptance
  of the current preview.
- REVISE requires the user to be asking for a change to something that
  already exists (a preview or live site).

User message:
{text}
"""

    def _parse_intent_response(
        self, response: str, valid_intents: List[ConversationIntent]
    ) -> ConversationIntent:
        """Parse FAST intent response. Fail-safe to INTAKE on any ambiguity."""
        text = response.strip().upper()
        # Remove any markdown fences, quotes, or extra whitespace
        text = text.strip("`\"' \n\r")

        for intent in valid_intents:
            if text == intent.value:
                return intent

        # Any non-exact match is ambiguous → INTAKE
        return ConversationIntent.INTAKE

    # ------------------------------------------------------------------
    # Update processing
    # ------------------------------------------------------------------

    def _process_update(self, update: dict) -> None:
        """Process a single Telegram Update through the existing dispatcher.

        Malformed/unsupported updates are logged and skipped — they never
        crash the process.
        """
        try:
            # Normalize through the existing TelegramNormalizer
            message = TelegramNormalizer.normalize(update)
            if message is None:
                logger.debug(
                    "Skipping non-message update: %s", update.get("update_id")
                )
                return

            # Derive authenticated context from verified Update fields only.
            # Never trust user-supplied text/payload fields to select a principal.
            user_id = message.user_id
            conversation_id = message.conversation_id
            if not user_id or not conversation_id:
                logger.warning(
                    "Update %s missing user/chat identity", message.event_id
                )
                return

            authenticated = AuthenticatedTelegramContext(user_id, conversation_id)

            # Determine project_id: use conversation_id as the stable project
            # identifier for R1 (one project per Telegram conversation).
            project_id = f"tg-{conversation_id}"

            # Check if project exists; if not, create it first
            state = self.dispatcher.store.load(project_id)
            if state is None:
                create_result = self.dispatcher.dispatch(
                    update,
                    project_id,
                    "create",
                    authenticated=authenticated,
                )
                if not create_result.success:
                    logger.error(
                        "Failed to create project %s: %s",
                        project_id,
                        create_result.error_code,
                    )
                    return
                state = self.dispatcher.store.load(project_id)

            # Classify the natural-language intent using FAST, bounded by
            # the current lifecycle state. AI interprets; code enforces.
            lifecycle = state.lifecycle if state else "DISCOVERING"
            intent = self._classify_intent(message.text, lifecycle, project_id)

            # Route to the appropriate dispatcher action(s) based on the
            # classified intent. The dispatcher's existing authz/lifecycle/
            # dedup gates remain authoritative — FAST never bypasses them.
            if intent == ConversationIntent.REVISE:
                self._handle_revise(update, project_id, authenticated, message, state)
            elif intent == ConversationIntent.APPROVE:
                self._handle_approve(update, project_id, authenticated, state)
            elif intent == ConversationIntent.PUBLISH:
                self._handle_publish(update, project_id, authenticated, state)
            else:
                # INTAKE — default conversational flow
                self._handle_intake(update, project_id, authenticated, message, state)

        except Exception:
            logger.exception(
                "Unexpected error processing update %s", update.get("update_id")
            )

    def _handle_intake(
        self,
        update: dict,
        project_id: str,
        authenticated: AuthenticatedTelegramContext,
        message: NormalizedMessage,
        state,
    ) -> None:
        """Handle INTAKE intent — ordinary conversation / requirements.

        The dispatcher's "intake" action now admits the follow-on build (when
        the requirements gate becomes READY) as a SEPARATE sub-claim under
        the SAME single "one Telegram event -> claim before effects"
        boundary — see TelegramDispatcher.dispatch(). There is no second
        top-level dispatch("build", ...) call here: that would derive its
        claim key from the identical (principal, conversation_id, event_id)
        tuple as the intake claim already recorded above and collide with
        it (EVENT_ACTION_MISMATCH) on replay, or -- if issued before this
        function returns -- run outside any claim at all.
        """
        result = self.dispatcher.dispatch(
            update,
            project_id,
            "intake",
            authenticated=authenticated,
        )

        if not result.success:
            logger.warning(
                "Intake dispatch failed for project %s: %s",
                project_id,
                result.error_code,
            )
            self._send_error_reply(message.conversation_id, result.error_code)
            return

        if result.data.get("build_triggered") and not result.data.get("build_success"):
            logger.warning(
                "Build dispatch failed for project %s: %s",
                project_id,
                result.data.get("build_error"),
            )
            self._send_error_reply(message.conversation_id, result.data.get("build_error"))

    def _handle_revise(
        self,
        update: dict,
        project_id: str,
        authenticated: AuthenticatedTelegramContext,
        message: NormalizedMessage,
        state,
    ) -> None:
        """Handle REVISE intent — user requests changes to current preview/site.

        Derives the revision sequence from existing persisted state.
        The dispatcher's revise action handles reserve() + apply() atomically.
        """
        if state is None:
            self._send_error_reply(message.conversation_id, "NO_PROJECT_STATE")
            return

        # Derive revision sequence from existing persisted state.
        # The RevisionOrchestrator contract requires seq = queued_revision_seq + 1.
        seq = state.revisions.queued_revision_seq + 1

        result = self.dispatcher.dispatch(
            update,
            project_id,
            "revise",
            authenticated=authenticated,
            seq=seq,
        )

        if not result.success:
            logger.warning(
                "Revise dispatch failed for project %s: %s",
                project_id,
                result.error_code,
            )
            self._send_error_reply(message.conversation_id, result.error_code)

    def _handle_approve(
        self,
        update: dict,
        project_id: str,
        authenticated: AuthenticatedTelegramContext,
        state,
    ) -> None:
        """Handle APPROVE intent — user accepts the current preview.

        This binds the approval to the exact shown preview identity.
        It does NOT publish — publication is a separate step.
        """
        result = self.dispatcher.dispatch(
            update,
            project_id,
            "approve",
            authenticated=authenticated,
        )

        if not result.success:
            logger.warning(
                "Approve dispatch failed for project %s: %s",
                project_id,
                result.error_code,
            )
            self._send_error_reply(
                state.conversation_id if state else "unknown", result.error_code
            )

    def _handle_publish(
        self,
        update: dict,
        project_id: str,
        authenticated: AuthenticatedTelegramContext,
        state,
    ) -> None:
        """Handle PUBLISH intent — user explicitly intends to go-live.

        The dispatcher's "publish" action is the single application-owned
        go-live operation: it approves the current exact shown preview and,
        if approval succeeds, promotes that exact approved preview. One
        Telegram event = one dispatch claim, so replay is idempotent.
        """
        conversation_id = state.conversation_id if state else "unknown"

        result = self.dispatcher.dispatch(
            update,
            project_id,
            "publish",
            authenticated=authenticated,
        )

        if not result.success:
            logger.warning(
                "Publish dispatch failed for project %s: %s",
                project_id,
                result.error_code,
            )
            self._send_error_reply(conversation_id, result.error_code)

    def _send_error_reply(self, chat_id: str, error_code: Optional[str]) -> None:
        """Send a sanitized error reply to the user. Never leaks internals."""
        messages = {
            "UNAUTHORIZED_ROLE": "You are not authorized to modify this project.",
            "BUILD_NOT_ALLOWED_IN_LIFECYCLE": "The project is not ready to build yet.",
            "DIRECTION_CHOICE_PENDING": "Please choose a design direction first.",
            "EVENT_RECONCILIATION_REQUIRED": "A previous operation needs reconciliation. Please try again.",
            "UNSUPPORTED_ACTION": "That action is not supported.",
            "REVISION_NOT_ALLOWED_IN_LIFECYCLE": "Revisions are not allowed right now.",
            "APPROVAL_NOT_ALLOWED_IN_LIFECYCLE": "Approval is not allowed right now.",
            "PROMOTION_NOT_ALLOWED_IN_LIFECYCLE": "Publication is not allowed right now.",
            "NO_SHOWN_PREVIEW": "No preview has been shown yet.",
            "STALE_APPROVAL": "The approval is stale. Please review the latest preview.",
            "STALE_QA_BINDING": "The preview is outdated. Please wait for the latest build.",
            "NOT_APPROVED": "The preview must be approved before publication.",
            "OUT_OF_ORDER_REVISION": "Revision is out of order. Please try again.",
            "REVISION_ALREADY_APPLIED": "This revision has already been applied.",
        }
        text = messages.get(error_code, "Something went wrong. Please try again.")
        try:
            self.telegram_out.send_text(chat_id, text)
        except Exception:
            logger.exception("Failed to send error reply to chat %s", chat_id)

    def run(self) -> None:
        """Main polling loop. Runs until stop() is called or SIGINT/SIGTERM."""
        logger.info(
            "Telegram receive loop starting (poll timeout=%ds)", self.poll_timeout
        )

        while not self._stop_event.is_set():
            try:
                updates = self._get_updates()
            except TelegramProviderError as exc:
                logger.error("Telegram provider failure: %s", exc)
                self._stop_event.wait(self.error_backoff)
                continue
            except Exception:
                logger.exception("Unexpected error fetching updates")
                self._stop_event.wait(self.error_backoff)
                continue

            for update in updates:
                if self._stop_event.is_set():
                    break
                # Advance offset so acknowledged updates are not re-consumed
                update_id = update.get("update_id")
                if update_id is not None:
                    self._offset = update_id + 1
                self._process_update(update)

        logger.info("Telegram receive loop stopped")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> int:
    """Canonical Website Builder runtime entrypoint."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    try:
        config = load_runtime_config()
    except ConfigurationError as exc:
        logger.error("Configuration error: %s", exc)
        return 1

    # Fail closed BEFORE any polling or composition: every model role must
    # resolve against the real profile config (and VISION must be
    # vision-capable). A partial role set previously started the loop and
    # failed per-message at runtime.
    if not preflight_role_validation(config):
        return 1

    try:
        composition = compose(config)
    except Exception:
        logger.exception("Failed to compose runtime")
        return 1

    loop = TelegramReceiveLoop(
        bot_token=config.telegram_bot_token,
        dispatcher=composition.dispatcher,
        telegram_out=composition.telegram_out,
        hermes=composition.hermes,
    )

    # Graceful shutdown on SIGINT/SIGTERM
    def shutdown(signum, frame):
        logger.info("Received signal %d, shutting down...", signum)
        loop.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        loop.run()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        loop.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
