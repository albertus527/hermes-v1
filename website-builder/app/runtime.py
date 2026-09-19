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
from app.core.registry import ConversationRegistryStore, DuplicateProjectName, slugify_display_name
from app.conversations import ConversationRoute, ConversationRouter
from app.deploy.adapters import (
    PreviewSmokeTester,
    TelegramAdapter,
    UrllibHttpTransport,
    VercelAdapter,
)
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


def preflight_node_toolchain(starter_dir: Optional[Path] = None) -> None:
    """Validate that local node and npm satisfy the fixed starter toolchain contract.

    Fail-closed contract:
    - success returns normally (logs resolved versions and executable paths)
    - any failure raises RuntimeError with an actionable, sanitized reason
    - all subprocess invocations keep bounded timeouts (10s)
    - no secrets are ever logged
    """
    import shutil
    import subprocess

    if starter_dir is None:
        starter_dir = Path(__file__).resolve().parent.parent.parent / "templates" / "frontend-starter"

    nvmrc_path = starter_dir / ".nvmrc"
    expected_nvmrc = "26.5.0"
    if nvmrc_path.is_file():
        expected_nvmrc = nvmrc_path.read_text(encoding="utf-8").strip()

    node_bin = shutil.which("node")
    if not node_bin:
        logger.error("Node toolchain preflight failed: 'node' executable not found in PATH")
        raise RuntimeError(
            "Node.js executable ('node') not found in PATH. "
            f"Install Node {expected_nvmrc} (see starter .nvmrc) and ensure it is on PATH."
        )

    try:
        res = subprocess.run(
            [node_bin, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=True,
        )
        node_version_str = res.stdout.strip()
    except Exception as exc:
        logger.error("Node toolchain preflight failed: unable to execute '%s --version': %s", node_bin, exc)
        raise RuntimeError(
            f"Unable to execute '{node_bin} --version'. Ensure Node {expected_nvmrc} is installed."
        ) from exc

    clean_ver = node_version_str.lstrip("v")
    try:
        parts = [int(p) for p in clean_ver.split(".")[:3]]
        major = parts[0]
    except (ValueError, IndexError) as exc:
        logger.error("Node toolchain preflight failed: unparseable node version '%s'", node_version_str)
        raise RuntimeError(
            f"Unparseable Node version '{node_version_str}' reported by 'node --version'."
        ) from exc

    if major != 26:
        logger.error(
            "Node toolchain drift: starter requires node >=26 <27 (.nvmrc %s), "
            "but resolved node is %s at %s.",
            expected_nvmrc,
            node_version_str,
            node_bin,
        )
        raise RuntimeError(
            f"Node version {node_version_str} does not satisfy starter requirement "
            f"(>=26 <27, .nvmrc {expected_nvmrc}). Activate Node {expected_nvmrc} before "
            "starting — npm ci/build would otherwise fail with EBADENGINE."
        )

    npm_bin = shutil.which("npm")
    if not npm_bin:
        logger.error("Node toolchain preflight failed: 'npm' executable not found in PATH")
        raise RuntimeError(
            "npm executable ('npm') not found in PATH. "
            f"Install Node {expected_nvmrc} (bundles npm) and ensure it is on PATH."
        )

    try:
        res_npm = subprocess.run(
            [npm_bin, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=True,
        )
        npm_version_str = res_npm.stdout.strip()
    except Exception as exc:
        logger.error("Node toolchain preflight failed: unable to execute '%s --version': %s", npm_bin, exc)
        raise RuntimeError(
            f"Unable to execute '{npm_bin} --version'. Reinstall Node {expected_nvmrc}."
        ) from exc

    logger.info(
        "Node toolchain preflight OK: node=%s (%s), npm=%s (%s)",
        node_version_str,
        node_bin,
        npm_version_str,
        npm_bin,
    )


def preflight_smoke_support(browser_factory: Optional[Any]) -> None:
    """Prove the configured preview-smoke browser factory actually works.

    Preview smoke is mandatory in R1, so startup must fail closed BEFORE any
    Telegram polling or deployment when the Chromium browser cannot launch.
    This check invokes the factory through its real production contract
    (``factory() -> Browser``, as used by ``PreviewSmokeTester``) and performs
    the minimum operation that proves usability — opening and closing a page —
    then closes every acquired resource deterministically. It never navigates
    anywhere: no network access and no Vercel/deployment work happens here.

    Raises RuntimeError with an actionable, sanitized reason on failure.
    """
    if browser_factory is None:
        logger.error(
            "Preview smoke preflight failed: Playwright browser factory is unavailable. "
            "Preview smoke is mandatory in Website Builder R1 — install with: "
            "pip install playwright && playwright install chromium"
        )
        raise RuntimeError(
            "Playwright browser factory is unavailable. Preview smoke is mandatory in "
            "Website Builder R1 — install with: pip install playwright && playwright install chromium"
        )

    browser = None
    page = None
    try:
        browser = browser_factory()
        page = browser.new_page()
    except Exception as exc:
        logger.error(
            "Preview smoke preflight failed: browser factory did not produce a usable "
            "browser/page: %s",
            exc,
        )
        raise RuntimeError(
            "Playwright browser is not usable in this environment (launch or page-open "
            "failed). Verify Chromium is installed: playwright install chromium"
        ) from exc
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                logger.warning("Preview smoke preflight: page close failed", exc_info=True)
        if browser is not None:
            try:
                browser.close()
            except Exception:
                logger.warning("Preview smoke preflight: browser close failed", exc_info=True)

    logger.info("Preview smoke preflight OK: browser factory produced a usable browser")


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
    conversations: ConversationRouter
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

    # Hermes adapter (FAST/FRONTEND/VISION) -- constructed BEFORE the
    # conversation router so the SAME instance can be injected into it.
    # Building a second HermesAdapter here would silently disable FAST-first
    # conversation routing (conversations.hermes stays None) while still
    # working for per-project intent classification.
    hermes = HermesAdapter(
        store=store,
        hermes_home=config.hermes_home,
    )

    # Conversation-level project registry + router. Separate from per-project
    # state so the registry survives any active-project switch. Shares the
    # single HermesAdapter instance above for FAST-first routing.
    registry_store = ConversationRegistryStore(config.state_root / "conversations")
    conversations = ConversationRouter(store, registry_store, hermes=hermes)

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

    # Preview orchestrator deps — display_name_for wires the natural
    # post-delivery follow-up ("Website webbandung udah siap...") and is allowed
    # to be absent in tests that do not construct a registry.
    #
    # slug_for / bind_slug wire the friendly Vercel project name feature:
    # slug_for resolves (in order) an ALREADY-BOUND vercel_slug for this
    # project, or else derives a fresh candidate from the registry's
    # display_name -- never invented, never truncated. bind_slug persists
    # that candidate EXACTLY ONCE on first successful Vercel resolution
    # (idempotent — see ConversationRegistryStore.set_vercel_slug_once).
    def _slug_for(pid, state):
        if state is None or not state.conversation_id:
            return None
        registry = registry_store.load_or_create(state.conversation_id)
        entry = registry.find_by_id(pid)
        if entry is None:
            return None
        if entry.vercel_slug:
            return entry.vercel_slug
        return slugify_display_name(entry.display_name)

    def _bind_slug(pid, state, slug):
        if state is None or not state.conversation_id:
            return
        registry_store.set_vercel_slug_once(state.conversation_id, pid, slug)

    # Bound-slug resolver for POST-preview flows (promotion, custom domain).
    # Unlike preview's slug_for -- which may derive a fresh candidate in
    # order to CREATE the project -- post-preview flows only run after a
    # successful preview, which has already created the Vercel project and
    # bound its slug. They therefore resolve ONLY the persisted binding,
    # never derive: an unbound entry means the project lives under the
    # legacy opaque hash-derived name.
    def _bound_slug_for(pid, state):
        if state is None or not state.conversation_id:
            return None
        registry = registry_store.load_or_create(state.conversation_id)
        entry = registry.find_by_id(pid)
        if entry is None:
            return None
        return entry.vercel_slug

    smoke_tester = PreviewSmokeTester(config.smoke_browser_factory)

    preview_deps = PreviewDeps(
        vercel=vercel,
        telegram=telegram_out,
        smoke=smoke_tester,
        output_repo=output_repo,
        chat_id_for=chat_id_for,
        display_name_for=lambda pid, state: (
            registry_store.display_name_for(state.conversation_id, pid)
            if state is not None and state.conversation_id
            else None
        ),
        slug_for=_slug_for,
        bind_slug=_bind_slug,
    )
    preview = PreviewOrchestrator(store, preview_deps, runner=runner)

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
        smoke=smoke_tester,
        chat_id_for=chat_id_for,
        slug_for=_bound_slug_for,
    )
    promote = PromotionOrchestrator(runner, store, promote_deps)

    # Custom domain orchestrator deps
    domain_deps = DomainDeps(
        vercel=vercel,
        smoke=smoke_tester,
        slug_for=_bound_slug_for,
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
        preview=preview,
    )

    return RuntimeComposition(
        config=config,
        store=store,
        conversations=conversations,
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
    NEW_PROJECT = "NEW_PROJECT"
    SELECT_PROJECT = "SELECT_PROJECT"
    LIST_PROJECTS = "LIST_PROJECTS"


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
        conversations: Optional[ConversationRouter] = None,
        display_name_for=None,
    ):
        self.bot_token = bot_token
        self.dispatcher = dispatcher
        self.telegram_out = telegram_out
        self.hermes = hermes
        self.conversations = conversations
        self.display_name_for = display_name_for
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
        state=None,
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

        Routing is conversation-first when a ``ConversationRouter`` is wired
        in: the conversation registry resolves the immutable internal project
        (by active pointer or by human name) BEFORE any per-project gate runs,
        so "revisi webbandung" and "sekarang bikin webjogja" land on the right
        project deterministically. With no router wired (legacy direct
        tests/embedding) the loop falls back to the R1 one-project-per-
        conversation behavior exactly as before.

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

            if self.conversations is not None:
                self._process_update_routed(update, message, authenticated)
                return
            self._process_update_legacy(update, message, authenticated)
        except Exception:
            logger.exception(
                "Unexpected error processing update %s", update.get("update_id")
            )

    # ------------------------------------------------------------------
    # Conversation-routed processing (multi-project per conversation)
    # ------------------------------------------------------------------

    def _process_update_routed(self, update, message, authenticated) -> None:
        router = self.conversations
        conversation_id = message.conversation_id

        # Load bounded active-project context to enrich the FAST prompt so
        # intake answers are not mistaken for new-project requests. This is a
        # read-only operation; no mutation happens here. When there is no
        # active project (first contact, all projects pending, etc.) the
        # context is None and the router behaves exactly as before.
        active_project_context = self._load_active_project_context(conversation_id)

        route = router.route(
            conversation_id,
            message.text,
            event_id=message.event_id,
            active_project_context=active_project_context,
        )

        if route.route == ConversationRoute.LIST_PROJECTS:
            self._safe_send(conversation_id, route.reply or "Belum ada project.")
            return

        if route.route == ConversationRoute.NEW_PROJECT:
            self._handle_new_project(update, message, authenticated, route)
            return

        if route.route == ConversationRoute.CLARIFICATION:
            text = route.clarification or "Aku belum yakin maksudnya. Bisa dijelasin lagi?"
            self._safe_send(conversation_id, text)
            return

        # Ordinary turn against an existing (resolved + activated) project.
        project_id = route.project_id
        state = self.dispatcher.store.load(project_id)
        if state is None:
            # Registry pointed at a project that does not exist — repair the
            # pointer instead of routing writes at a missing project.
            logger.warning(
                "Active project %s for conversation %s has no state; asking for clarification",
                project_id, conversation_id,
            )
            self._safe_send(
                conversation_id,
                "Project itu tidak ditemukan. Mau buka yang mana?",
            )
            return
        self._dispatch_project_turn(update, project_id, authenticated, message, state, route)

    def _dispatch_project_turn(
        self, update, project_id, authenticated, message, state, route=None
    ) -> None:
        """Per-project intent routing. All existing gates stay authoritative."""
        # Preview reconciliation check: if project is in PREVIEW_READY with a tested_snapshot
        # but latest_shown_preview is missing, reconcile Phase 9 before dispatching the turn.
        if (
            state is not None
            and state.lifecycle == "PREVIEW_READY"
            and state.deployment.get("tested_snapshot")
            and not state.deployment.get("latest_shown_preview")
        ):
            recon = self.dispatcher.dispatch(
                update, project_id, "reconcile_preview", authenticated=authenticated
            )
            if recon.success:
                state = self.dispatcher.store.load(project_id)

        lifecycle = state.lifecycle if state else "DISCOVERING"

        forced = getattr(route, "forced_intent", None) if route is not None else None
        if forced == "REVISE" and lifecycle in ("PREVIEW_READY", "LIVE"):
            intent = ConversationIntent.REVISE
        else:
            intent = self._classify_intent(message.text, lifecycle, project_id)

        if intent == ConversationIntent.REVISE:
            self._handle_revise(update, project_id, authenticated, message, state)
        elif intent == ConversationIntent.APPROVE:
            self._handle_approve(update, project_id, authenticated, state)
        elif intent == ConversationIntent.PUBLISH:
            self._handle_publish(update, project_id, authenticated, state)
        else:
            self._handle_intake(update, project_id, authenticated, message, state)

    def _handle_new_project(self, update, message, authenticated, route) -> None:
        """Create (or recover) a project for the conversation's NEW_PROJECT intent.

        Two allocation shapes arrive here:

          * Pre-allocated entry (``route.project_id`` set): either the router
            already allocated the id during routing, OR the replay guard
            detected the registry persisted this event's allocation while
            ProjectState creation never ran (crash window). Either way the
            recovery path MUST reuse that exact id — never allocate a second
            logical identity for the same Telegram event.
          * Unallocated entry (``route.project_id`` empty): allocate now via
            ``router.materialize_new_project``, which itself re-uses an
            existing registry entry for the same normalized display name
            (deterministic duplicate suppression).
        """
        router = self.conversations
        conversation_id = message.conversation_id

        entry = route.entry
        pre_project_id = getattr(route, "project_id", None) or getattr(
            entry, "project_id", None
        )

        if pre_project_id:
            # Recover-or-confirm: the registry entry already exists for this id.
            try:
                router.registry.set_active(conversation_id, pre_project_id)
            except Exception:
                logger.exception("Failed to persist active project for recovery")
            if message.event_id:
                try:
                    router.registry.record_event(
                        conversation_id, message.event_id, pre_project_id
                    )
                except Exception:
                    logger.exception("Failed to persist replay mapping during recovery")
            project_id = pre_project_id
        else:
            display_name = entry.display_name if entry else None
            if not display_name:
                self._safe_send(
                    conversation_id,
                    "Oke, bikin website baru. Mau kasih nama apa untuk website barunya?",
                )
                return
            try:
                entry = router.materialize_new_project(
                    conversation_id, display_name, event_id=message.event_id
                )
            except DuplicateProjectName:
                self._safe_send(
                    conversation_id,
                    "Nama itu sudah dipakai. Mau lanjut ke project itu, atau kasih nama lain?",
                )
                return
            project_id = entry.project_id

        # If the project state ALREADY exists on disk (crash between
        # ProjectAccess.create and a later crash, then Telegram retried the
        # event), skip the create dispatch — there is nothing to redo.
        state = self.dispatcher.store.load(project_id)
        if state is None:
            create_result = self.dispatcher.dispatch(
                update,
                project_id,
                "create",
                authenticated=authenticated,
            )
            if not create_result.success:
                # Roll the registry entry back ONLY when we allocated it in
                # this turn (fresh allocation path). For a pre-allocated
                # recovery id, the registry mapping is the source of truth
                # and must survive so a later retry can still recover.
                if not pre_project_id:
                    router.rollback_project(conversation_id, project_id)
                logger.error(
                    "Failed to create project %s: %s",
                    project_id, create_result.error_code,
                )
                self._safe_send(conversation_id, "Sesuatu gagal. Coba lagi ya.")
                return
            state = self.dispatcher.store.load(project_id)

        if state is None:
            # Defensive: state must exist now; if not, surface a clarification
            # instead of crashing the loop.
            self._safe_send(
                conversation_id,
                "Project-nya belum bisa dibuka. Coba kirim ulang pesannya ya.",
            )
            return
        self._handle_intake(update, project_id, authenticated, message, state)

    def _handle_legacy_first_project(self, update, message, authenticated, project_id) -> None:
        create_result = self.dispatcher.dispatch(
            update, project_id, "create", authenticated=authenticated
        )
        if not create_result.success:
            logger.error(
                "Failed to create project %s: %s", project_id, create_result.error_code
            )
            return

    def _load_active_project_context(
        self, conversation_id: str
    ) -> Optional[Dict[str, Any]]:
        """Load bounded active-project context for FAST prompt enrichment.

        Returns a dict with:
          active_project_name: str            — the human display name
          active_project_lifecycle: str       — e.g. "WAITING_INPUT"
          next_missing_intake_field: str|None — first of (name, what, why)
                                               that is still None in the brief

        Returns None when there is no active project, the state cannot be
        loaded, or the conversations router is not wired in.
        """
        router = self.conversations
        if router is None:
            return None
        registry = router.registry.load_or_create(conversation_id)
        active_id = registry.active_project_id
        if not active_id:
            return None
        entry = registry.find_by_id(active_id)
        if entry is None:
            return None
        try:
            state = self.dispatcher.store.load(active_id)
        except Exception:
            logger.exception(
                "Failed to load active project state %s for context enrichment",
                active_id,
            )
            return None
        if state is None:
            return None
        # Determine the first missing intake field from the accumulated brief.
        brief = state.brief or {}
        next_missing: Optional[str] = None
        for field_name in ("name", "what", "why"):
            if not brief.get(field_name):
                next_missing = field_name
                break
        return {
            "active_project_name": entry.display_name,
            "active_project_lifecycle": state.lifecycle,
            "next_missing_intake_field": next_missing,
        }

    # ------------------------------------------------------------------
    # Legacy (no router) processing — R1 one-project-per-conversation
    # ------------------------------------------------------------------

    def _process_update_legacy(self, update, message, authenticated) -> None:
        conversation_id = message.conversation_id
        project_id = f"tg-{conversation_id}"
        state = self.dispatcher.store.load(project_id)
        if state is None:
            self._handle_legacy_first_project(update, message, authenticated, project_id)
            state = self.dispatcher.store.load(project_id)
        if (
            state is not None
            and state.lifecycle == "PREVIEW_READY"
            and state.deployment.get("tested_snapshot")
            and not state.deployment.get("latest_shown_preview")
        ):
            recon = self.dispatcher.dispatch(
                update, project_id, "reconcile_preview", authenticated=authenticated
            )
            if recon.success:
                state = self.dispatcher.store.load(project_id)

        lifecycle = state.lifecycle if state else "DISCOVERING"
        intent = self._classify_intent(message.text, lifecycle, project_id, state=state)
        if intent == ConversationIntent.REVISE:
            self._handle_revise(update, project_id, authenticated, message, state)
        elif intent == ConversationIntent.APPROVE:
            self._handle_approve(update, project_id, authenticated, state)
        elif intent == ConversationIntent.PUBLISH:
            self._handle_publish(update, project_id, authenticated, state)
        else:
            self._handle_intake(update, project_id, authenticated, message, state)

    def _safe_send(self, chat_id: str, text: str) -> None:
        try:
            self.telegram_out.send_text(chat_id, text)
        except Exception:
            logger.exception("Failed to send message to chat %s", chat_id)

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

        # Last mile: the intake pipeline already derived the smallest blocking
        # clarification question (from the existing FAST interpretation or the
        # deterministic per-field fallback). Surface it to the user exactly
        # once. Replay of the same Telegram update is short-circuited inside
        # the dispatcher as {"duplicate": True} with NO clarification_question
        # payload, so this send path never fires twice for the same event.
        if result.data.get("duplicate"):
            return
        clarification = result.data.get("clarification_question")
        if clarification:
            try:
                self.telegram_out.send_text(message.conversation_id, clarification)
            except Exception:
                logger.exception(
                    "Failed to send clarification to chat %s", message.conversation_id
                )
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
            "SLUG_COLLISION": (
                "Nama itu sudah dipakai untuk link preview. Mau pakai nama "
                "lain, atau aku kasih beberapa pilihan?"
            ),
            "DEPLOYMENT_ALREADY_LIVE": "This deployment is already live in production.",
        }
        if error_code and error_code.startswith("CHEAP_CHECKS_FAILED:"):
            check_name = error_code.split(":", 1)[1]
            text = f"Build verification failed ({check_name}). Please try again."
        elif error_code == "TOOLCHAIN_MUTATION_REJECTED":
            text = "Build failed due to toolchain policy violation. Please try again."
        elif error_code and error_code.startswith("INFRASTRUCTURE_ERROR:"):
            text = "An infrastructure error occurred during verification. Please try again."
        elif error_code == "PREVIEW_BUSY":
            text = "The project is busy with another build/preview operation. Please try again shortly."
        elif error_code in ("UNEXPECTED_BUILD_ERROR", "UNEXPECTED_QA_ERROR"):
            text = "An unexpected error occurred. Please try again."
        else:
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


def main(argv: Optional[List[str]] = None) -> int:
    """Canonical Website Builder runtime entrypoint."""
    if argv is None:
        argv = sys.argv[1:]

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

    # Fail closed BEFORE any polling or composition:
    # 1. Local Node toolchain must satisfy the starter contract.
    # 2. Preview smoke support (Playwright/Chromium) must actually work.
    #    (Both raise RuntimeError with an actionable sanitized reason; the
    #    detail was already logged by the preflight itself.)
    try:
        preflight_node_toolchain()
        preflight_smoke_support(config.smoke_browser_factory)
    except RuntimeError:
        return 1

    if "--preflight" in argv:
        logger.info(
            "Preflight checks passed: Node toolchain and preview smoke support verified."
        )
        return 0

    # 3. Every model role must resolve against the real profile config
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
        conversations=composition.conversations,
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
