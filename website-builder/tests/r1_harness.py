"""TEST-ONLY local R1 end-to-end simulation harness.

The harness wires the REAL production Website Builder components together
(ConversationRouter, ConversationRegistryStore, ProjectStateStore,
IntakeProcessor, TelegramDispatcher, FrontendBuilder, PreviewOrchestrator,
RevisionOrchestrator, PromotionOrchestrator, lifecycle/state models, real JSON
persistence, real claim/revision logic) and replaces ONLY the external
boundaries with deterministic, in-process fakes:

  * Hermes / FAST / FRONTEND / VISION -> :class:`FakeHermes`
  * Vercel HTTP/provider boundary      -> :class:`FakeVercel`
  * Telegram outbound boundary         -> :class:`FakeTelegramOut`
  * Playwright/smoke boundary          -> :class:`FakeSmoke`
  * Output git repository              -> :class:`FakeOutputRepo`
  * Filesystem-mutating worker steps   -> :class:`RecordingRunner` (real
    workspace creation, stub command execution)

This module is intentionally small and deterministic. It contains NO
production logic and is never imported by production code. All state lives
under pytest's ``tmp_path``; nothing touches the real p6/p7 state, the real
network, or the real Telegram/Vercel APIs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.channels.dispatch import AuthenticatedTelegramContext, TelegramDispatcher
from app.conversations import ConversationRouter
from app.core.contracts import OperationResult
from app.core.intake import IntakeProcessor
from app.core.lifecycle import ProjectLifecycle
from app.core.registry import ConversationRegistryStore
from app.core.state import ProjectStateStore
from app.deploy.preview import PreviewDeps, PreviewOrchestrator
from app.deploy.snapshot import TestedSnapshot, source_fingerprint
from app.projects.build import FrontendBuilder
from app.projects.promote import PromoteDeps, PromotionOrchestrator
from app.projects.revise import RevisionOrchestrator
from app.runtime import TelegramReceiveLoop


# ---------------------------------------------------------------------------
# Fake external boundaries
# ---------------------------------------------------------------------------


class FakeHermes:
    """Deterministic stand-in for the HermesAdapter boundary.

    Production code calls two distinct surfaces:

      * ``_run_fast_programmatic(prompt=..., role=..., skills=...)`` — the
        zero-tool FAST/FRONTEND/VISION programmatic boundary. This fake
        answers based on the prompt's role marker so routing, intent
        classification, and QA vision all work.
      * ``frontend_build(project_id, brief, workspace, design_dna_instructions)``
        — the FRONTEND build subprocess. Scriptable per project.
      * ``fast_interpret(text, ...)`` — the intake NAME/WHAT/WHY extractor.
    """

    def __init__(self):
        # Scripted FAST routing decision (website-builder conversation router).
        # When None, ``_run_fast_programmatic`` for a router prompt returns a
        # PROJECT_TURN-style fallback so tests can opt into deterministic
        # routing. Set via ``set_router_decision``.
        self.router_decision: Optional[Dict[str, Any]] = None
        # Scripted per-project intent classification, keyed by lifecycle.
        self.intent_response: Optional[str] = None
        # Scripted intake interpretation dict (fast_interpret).
        self.intake_response: Optional[Dict[str, Any]] = None
        # Scripted FRONTEND build result(s).
        self.frontend_result: Optional[Dict[str, Any]] = None
        self.frontend_results: List[Dict[str, Any]] = []
        # Scripted VISION QA response (JSON string or dict).
        self.vision_result: Optional[Any] = None
        # Counters.
        self.fast_calls: List[str] = []
        self.frontend_calls = 0

    # -- scripting helpers -------------------------------------------------
    def set_router_decision(
        self, intent: str, *, confidence: str = "high",
        target_project_name: Optional[str] = None,
        proposed_new_project_name: Optional[str] = None,
    ) -> None:
        self.router_decision = {
            "intent": intent,
            "target_project_name": target_project_name,
            "proposed_new_project_name": proposed_new_project_name,
            "confidence": confidence,
        }

    def clear_router_decision(self) -> None:
        self.router_decision = None

    def queue_frontend_result(self, result: Dict[str, Any]) -> None:
        self.frontend_results.append(result)

    # -- production boundary ----------------------------------------------
    def _run_fast_programmatic(self, prompt, model=None, provider=None,
                               skills=None, content_parts=None,
                               require_vision=False, role=None):
        # Classify by the prompt's shape. This mirrors how the real adapter
        # would produce a role-specific answer without any network access.
        text = prompt or ""
        if "routing one Website Builder conversation turn" in text:
            self.fast_calls.append("router")
            if self.router_decision is None:
                # Safe default: treat as a continuation of the active project.
                payload = {"intent": "PROJECT_TURN", "target_project_name": None,
                           "proposed_new_project_name": None, "confidence": "high"}
            else:
                payload = dict(self.router_decision)
            return _HermesResult(True, json.dumps(payload))
        if "Classify the user's message into exactly one" in text or "bounded intent" in text:
            self.fast_calls.append("intent")
            if self.intent_response is None:
                return _HermesResult(True, json.dumps({"intent": "INTAKE"}))
            return _HermesResult(True, self.intent_response)
        # VISION QA prompt.
        self.fast_calls.append("vision")
        if self.vision_result is None:
            return _HermesResult(True, json.dumps({"verdict": "pass", "findings": []}))
        if isinstance(self.vision_result, str):
            return _HermesResult(True, self.vision_result)
        return _HermesResult(True, json.dumps(self.vision_result))

    def fast_interpret(self, text, project_id=None, conversation_context=None):
        if self.intake_response is not None:
            return dict(self.intake_response)
        # Default deterministic: echo nothing (needs clarification).
        return {
            "scope": "UNCLEAR", "name": None, "what": None, "why": None,
            "why_destination": None, "ambiguity": None,
            "clarification_needed": True,
            "clarification_question": "What is the website about?",
            "readiness": "NEEDS_CLARIFICATION",
        }

    def frontend_build(self, project_id, brief, workspace, design_dna_instructions=None):
        self.frontend_calls += 1
        if self.frontend_results:
            result = self.frontend_results.pop(0)
        elif self.frontend_result is not None:
            result = self.frontend_result
        else:
            result = {"success": True, "design_dna": _design_dna()}
        # Materialize a minimal valid site into the workspace so the real
        # cheap checks / snapshot logic has something to fingerprint.
        if result.get("success"):
            _materialize_site(Path(workspace))
        return dict(result)

    def validate_role_configuration(self):
        return {}

    def vision_inspect(self, desktop_path, mobile_path, brief, design_dna=None):
        """Deterministic VISION QA boundary. Defaults to a clean pass."""
        if self.vision_result is None:
            return {"pass": True, "blocking": [], "observations": [],
                    "summary": "ok"}
        if isinstance(self.vision_result, str):
            return json.loads(self.vision_result)
        return dict(self.vision_result)


class _HermesResult:
    """Minimal HermesResult-compatible object."""

    def __init__(self, success: bool, response: str = "", error: Optional[str] = None,
                 exit_code: int = 0):
        self.success = success
        self.response = response
        self.error = error
        self.exit_code = exit_code


class FakeVercel:
    """Deterministic Vercel boundary.

    Models the four provider surfaces production uses, with full control over
    GET/POST status codes, transport timeouts, and lookup outcomes.
    """

    def __init__(self):
        self._project = {"id": "prj_1", "name": "wb", "accountId": "team",
                         "env": []}
        # scripted behaviours
        self.get_status = 404           # GET existing project -> 404 (absent)
        self.post_status = 201          # POST create -> 201
        self.post_raises = None         # set to exception instance to raise
        self.lookup_result = "ok"       # "ok" | "absent" | "malformed"
        # counters
        self.get_calls = 0
        self.post_calls = 0
        self.deploy_calls = 0
        self.promote_calls = 0
        self.bootstrap_calls = 0
        self.lookup_calls = 0
        self.bypass_generation_calls = 0
        self.bypass_behavior = None  # None | "forbidden" | "ambiguous"
        # PHASE E recovery: the REMOTE protection-bypass state as Vercel would
        # report it on GET. ``None`` -> no bypass exists yet; a string -> the
        # LIVE map-key shape (exactly ONE entry whose KEY is the secret); the
        # sentinel strings below model an ambiguous/unreadable read and a
        # malformed remote shape. A successful generation records its secret
        # here so a retry can be RECONCILED instead of rotated.
        self.remote_bypass = None
        self.bypass_read_calls = 0
        # PHASE A/G: scriptable bootstrap confirmation states.
        self.bootstrap_states: List[str] = []
        # PHASE A/G: when True, the FIRST bootstrap attempt observes the real
        # "bound but BUILDING" transient state; later attempts observe READY.
        self.bootstrap_building_then_ready = False
        self._bootstrap_confirmed = False
        self.created_projects: List[str] = []
        self.deployed_operations: List[str] = []
        self.promoted_deployments: List[str] = []
        # Records every same-operation recovery adoption read, so a test can
        # assert a recovery reconciled before it considered any promote.
        self.reconcile_external_calls: List[Dict[str, Any]] = []
        # Bound friendly project name (slug) once created.
        self.project_name = None
        self.last_deployment: Dict[str, Any] = {}
        self.lookup_deployment = "ok"

    @property
    def project_id(self) -> str:
        """The immutable Vercel project id the boundary currently reports."""
        return self._project["id"]

    # -- production boundary ----------------------------------------------
    def ensure_project(self, app_id):
        self.get_calls += 1
        return OperationResult.ok({"project": self._project, "app_id": app_id})

    def ensure_project_with_slug(self, app_id, slug):
        self.get_calls += 1
        # Once a project has been created for this harness, later resolutions
        # must observe it as EXISTING (GET 200), exactly like the real
        # provider — otherwise every later preview would re-create.
        if self.project_name == slug:
            self.get_status = 200
        if self.get_status == 404:
            self.post_calls += 1
            if self.post_raises is not None:
                # The real VercelAdapter catches transport exceptions and
                # classifiers them; mirror that contract here so the
                # orchestrator sees the same result the adapter would produce.
                return OperationResult.fail(
                    "AMBIGUOUS_PROJECT_CREATE", error_code="AMBIGUOUS_PROJECT_CREATE"
                )
            if self.post_status not in (200, 201):
                if self.post_status == 409:
                    # A deterministic collision surfaces as a proven,
                    # non-ambiguous SLUG_COLLISION (never transport ambiguity).
                    return OperationResult.fail("SLUG_COLLISION", error_code="SLUG_COLLISION")
                if self.post_status == 400:
                    return OperationResult.fail(
                        "PROJECT_CREATE_REJECTED",
                        error_code="PROJECT_CREATE_REJECTED",
                    )
                return OperationResult.fail(
                    "AMBIGUOUS_PROJECT_CREATE", error_code="AMBIGUOUS_PROJECT_CREATE"
                )
            self.created_projects.append(slug)
            self.project_name = slug
            self._project = {"id": "prj_1", "name": slug, "accountId": "team", "env": []}
            return OperationResult.ok({"project": self._project, "app_id": app_id, "slug": slug})
        if self.get_status == 200:
            return OperationResult.ok({"project": self._project, "app_id": app_id, "slug": slug})
        return OperationResult.fail("PROJECT_RECONCILIATION_REQUIRED",
                                    error_code="PROJECT_RECONCILIATION_REQUIRED")

    def lookup_project(self, app_id, *, expected_name=None):
        self.lookup_calls += 1
        if self.lookup_result == "absent":
            return OperationResult.fail("PROJECT_RECONCILIATION_REQUIRED",
                                        error_code="PROJECT_RECONCILIATION_REQUIRED")
        if self.lookup_result == "malformed":
            return OperationResult.fail("PROJECT_RECONCILIATION_REQUIRED",
                                        error_code="PROJECT_RECONCILIATION_REQUIRED")
        project = dict(self._project)
        if expected_name:
            project["name"] = expected_name
            self.project_name = expected_name
        return OperationResult.ok({"project": project, "app_id": app_id})

    def ensure_bootstrap(self, app_id, project, expected_name=None):
        self.bootstrap_calls += 1
        # PHASE A/G: scriptable bootstrap confirmation. ``bootstrap_states``
        # may hold an explicit per-call sequence; otherwise, when
        # ``bootstrap_building_then_ready`` is set, the FIRST attempt for a
        # project observes the real "bound but BUILDING" transient state and
        # every later attempt observes READY (mirrors the real p7 timing).
        if self.bootstrap_states:
            state = self.bootstrap_states.pop(0)
        elif self.bootstrap_building_then_ready and not self._bootstrap_confirmed:
            self._bootstrap_confirmed = True
            state = "building"
        else:
            state = None
        if state == "building":
            return OperationResult.fail(
                "BOOTSTRAP_CONFIRMATION_TIMEOUT",
                error_code="BOOTSTRAP_CONFIRMATION_TIMEOUT",
            )
        if state == "ready":
            return OperationResult.ok({
                "bootstrapped": True,
                "deployment_id": "dpl_bootstrap_1",
                "reconciled": False,
                "confirmed": True,
            })
        return OperationResult.ok({"bootstrapped": False, "already_current_production": True})

    def ensure_protection_bypass(self, app_id, project, *, expected_name=None):
        """PHASE E boundary: generate a PROJECT-SPECIFIC bypass secret.

        Counts generation attempts so tests can assert exactly-once / no-
        rotation semantics. ``bypass_behavior`` may be set to a code string to
        simulate a sanitized failure. A successful generation records the
        remote state (map KEY = secret) so a later run RECONCILES it.
        """
        self.bypass_generation_calls += 1
        if self.bypass_behavior == "forbidden":
            return OperationResult.fail(
                "BYPASS_PROVISION_FORBIDDEN",
                error_code="BYPASS_PROVISION_FORBIDDEN",
            )
        if self.bypass_behavior == "ambiguous":
            return OperationResult.fail(
                "AMBIGUOUS_BYPASS_PROVISION",
                error_code="AMBIGUOUS_BYPASS_PROVISION",
            )
        project_id = (project or {}).get("id", "prj_1")
        # Deterministic per-project secret from the project id.
        secret = "bypass-" + hashlib.sha256(project_id.encode()).hexdigest()[:16]
        # Vercel stores the created bypass remotely: the GET map KEY IS the
        # secret (LIVE evidence). Record it so a retry reconciles (no rotation).
        if self.remote_bypass is None:
            self.remote_bypass = secret
        return OperationResult.ok({"project_id": project_id, "secret": secret})

    def reconcile_protection_bypass(self, app_id, project, *, expected_name=None):
        """Read-only recovery boundary: inspect the LIVE remote bypass.

        Models the RECOVERABLE-SCAN contract (``exists``/``secret``) so the
        provisioner can distinguish a PROVEN absence from an unreadable read:
          * ``remote_bypass is None``  -> ok({'exists': False}) (no bypass),
          * a secret string            -> ok({'exists': True, 'secret': ...}),
          * 'AMBIGUOUS'                -> sanitized ambiguous failure,
          * 'MALFORMED'                -> fail closed (never mine).
        Never issues a PATCH.
        """
        self.bypass_read_calls += 1
        if self.remote_bypass == "AMBIGUOUS":
            return OperationResult.fail(
                "AMBIGUOUS_BYPASS_PROVISION",
                error_code="AMBIGUOUS_BYPASS_PROVISION",
            )
        if self.remote_bypass == "MALFORMED":
            return OperationResult.fail(
                "BYPASS_RECONCILIATION_REQUIRED",
                error_code="BYPASS_RECONCILIATION_REQUIRED",
            )
        if self.remote_bypass is None:
            return OperationResult.ok({"exists": False})
        return OperationResult.ok({"exists": True, "secret": self.remote_bypass})

    def deploy_static_files(self, app_id, project, files, operation_id,
                            source_revision, artifact_sha256, expected_name=None):
        self.deploy_calls += 1
        self.deployed_operations.append(operation_id)
        self.last_deployment = {
            "deployment_id": "dpl_" + str(self.deploy_calls),
            "preview_url": "https://tested.vercel.app",
            "state": "READY",
        }
        return OperationResult.ok(dict(self.last_deployment))

    def find_deployment_by_operation_id(self, app_id, project, operation_id,
                                        source_revision, artifact_sha256,
                                        expected_name=None):
        # Read-only reconciliation/readiness path. Must report the SAME
        # deployment identity the create/deploy call produced.
        self.lookup_calls += 1
        if getattr(self, "lookup_deployment", "ok") == "not_found":
            return OperationResult.fail("NOT_FOUND", error_code="NOT_FOUND")
        if getattr(self, "lookup_deployment", "ok") == "error":
            return OperationResult.ok({"deployment_id": "dpl_1",
                                       "preview_url": "https://tested.vercel.app",
                                       "state": "ERROR"})
        return OperationResult.ok(dict(self.last_deployment))

    def find_production_deployment(self, app_id, project, *, expected_name=None):
        return OperationResult.ok({"deployment_id": None})

    def promote_deployment(self, app_id, project, deployment_id, operation_id,
                           source_revision, artifact_sha256, *, expected_name=None):
        self.promote_calls += 1
        self.promoted_deployments.append(deployment_id)
        return OperationResult.ok({
            "deployment_id": deployment_id,
            "production_url": "https://prod.vercel.app",
            "state": "READY",
        })

    def reconcile_production_deployment(self, app_id, project, expected_identity, *,
                                        expected_name=None):
        return OperationResult.ok({"status": "NOT_PROMOTED", "deployment_id": None})

    def reconcile_external_promotion(self, app_id, project, intended_identity, *,
                                     expected_name=None):
        self.reconcile_external_calls.append(dict(intended_identity))
        return OperationResult.ok({"status": "PROMOTED_UNPROVEN", "deployment_id": None})


class FakeTelegramOut:
    """Deterministic outbound Telegram boundary. Records every send."""

    def __init__(self):
        self.photo_calls: List[tuple] = []
        self.text_calls: List[tuple] = []
        self.send_photo_raises = None

    def send_text(self, chat_id, text, **kwargs):
        self.text_calls.append((str(chat_id), text))
        return OperationResult.ok({"message_id": len(self.text_calls)})

    def send_photo(self, chat_id, path, caption="", **kwargs):
        self.photo_calls.append((str(chat_id), str(path), caption))
        if self.send_photo_raises is not None:
            raise self.send_photo_raises
        return OperationResult.ok({"message_id": len(self.photo_calls)})


class FakeSmoke:
    def __init__(self, success=True):
        self.success = success
        self.calls: List[str] = []
        # PHASE F: records the bypass secret passed to each run so tests can
        # assert the smoke received the project-specific credential, and that
        # it is never sent to non-Vercel origins.
        self.bypass_secrets: List[Any] = []
        # Failure classification mirroring the REAL PreviewSmokeTester's
        # `failure_classification`, so the runtime recovery gate exercises the
        # production decision path. None means "not classified" (legacy).
        self.failure_classification: Optional[str] = None
        # Structured, sanitized records (mirrors the real tester's shape).
        self.failure_records: List[Dict[str, Any]] = []
        self._success_sequence: List[bool] = []

    def run(self, url, out_dir, *, bypass_secret=None):
        self.calls.append(url)
        self.bypass_secrets.append(bypass_secret)
        success = self._success_sequence.pop(0) if self._success_sequence else self.success
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        shot = Path(out_dir) / "desktop.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        data = {"desktop_screenshot": str(shot), "mobile_screenshot": str(shot),
                "url": url, "failures": [] if success else ["x"]}
        if not success:
            if self.failure_classification:
                data["failure_classification"] = self.failure_classification
            if self.failure_records:
                data["failure_records"] = list(self.failure_records)
        return OperationResult(
            success=success,
            data=data,
            error_code=None if success else "SMOKE_FAILED",
        )


class FakeOutputRepo:
    """Stand-in for OutputGitRepository — no real git process, no external
    mutation. Mirrors the commit() return contract preview/promote rely on."""

    def __init__(self):
        self.commits: List[str] = []

    def commit(self, project_id, snapshot):
        self.commits.append(project_id)
        return {
            "path": "/tmp/fake-output",
            "branch": "preview/" + project_id + "/" + snapshot.identity,
            "commit": hashlib.sha256(snapshot.identity.encode()).hexdigest()[:40],
            "source_sha256": snapshot.source_sha256,
            "artifact_sha256": snapshot.artifact_sha256,
        }

    def push_github(self, identity, url, *, enabled=False):
        return {"pushed": False}


class RecordingRunner:
    """Real workspace creation + real single-worker slot, but every fixed
    check (npm ci/build/typecheck) is a deterministic success so no process
    is ever spawned. Returns a real subprocess.CompletedProcess so the
    production cheap-check code path executes unchanged."""

    def __init__(self, workspace_root: Path):
        self.workspace_root = Path(workspace_root)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self._active: Optional[str] = None
        self.commands: List[list] = []

    def create_workspace(self, project_id: str) -> Path:
        path = self.workspace_root / project_id
        (path / "src").mkdir(parents=True, exist_ok=True)
        (path / "dist").mkdir(parents=True, exist_ok=True)
        return path

    def acquire_project(self, project_id: str) -> bool:
        if self._active is not None:
            return False
        self._active = project_id
        return True

    def release_project(self, project_id: str) -> None:
        if self._active == project_id:
            self._active = None

    def run_command(self, project_id, command, cwd=None, **kwargs):
        import subprocess
        self.commands.append(list(command))
        return subprocess.CompletedProcess(args=list(command), returncode=0,
                                           stdout="", stderr="")


class FakeRenderer:
    """Deterministic stand-in for the local Vite preview render server."""

    def __init__(self, *args, **kwargs):
        pass

    def start(self, project_id, workspace):
        return _RenderHandle(project_id=project_id, port=0, process=None,
                             url="http://127.0.0.1:0/preview")

    def stop(self, handle):
        return None


class _RenderHandle:
    def __init__(self, project_id, port, process, url):
        self.project_id = project_id
        self.port = port
        self.process = process
        self.url = url


class FakeScreenshotCapture:
    """Writes valid PNGs and matching live metrics for the QA orchestrator."""

    DESKTOP = (1440, 900)
    MOBILE = (390, 844)

    def capture(self, url, qa_dir, attempt):
        from app.qa.screenshot import BrowserMetrics, ScreenshotSet
        out = Path(qa_dir) / f"attempt-{attempt}"
        out.mkdir(parents=True, exist_ok=True)
        desktop = out / "desktop.png"
        mobile = out / "mobile.png"
        desktop.write_bytes(_png_bytes(*self.DESKTOP))
        mobile.write_bytes(_png_bytes(*self.MOBILE))
        desktop_metrics = BrowserMetrics(
            inner_width=self.DESKTOP[0],
            inner_height=self.DESKTOP[1],
            document_client_width=self.DESKTOP[0],
            document_scroll_width=self.DESKTOP[0],
            body_scroll_width=self.DESKTOP[0],
        )
        mobile_metrics = BrowserMetrics(
            inner_width=self.MOBILE[0],
            inner_height=self.MOBILE[1],
            document_client_width=self.MOBILE[0],
            document_scroll_width=self.MOBILE[0],
            body_scroll_width=self.MOBILE[0],
        )
        return ScreenshotSet(
            desktop=desktop,
            mobile=mobile,
            desktop_metrics=desktop_metrics,
            mobile_metrics=mobile_metrics,
        )


def _png_bytes(width: int, height: int) -> bytes:
    import struct
    import zlib

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + ctype + data
                + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b""))


# ---------------------------------------------------------------------------
# Scenario harness
# ---------------------------------------------------------------------------


class LocalR1Scenario:
    """One isolated, deterministic R1 conversation with full production wiring.

    Usage::

        h = LocalR1Scenario(tmp_path)
        h.set_router_decision("CREATE_PROJECT", proposed_new_project_name=None)
        h.send_user_message("aku mau bikin web untuk membaca, soft tone dan bright")
        h.send_user_message("kitsunereading")
        h.run_build_and_preview()
    """

    def __init__(self, tmp_path: Path, *, chat_id: str = "555",
                 user_id: int = 1):
        self.root = Path(tmp_path)
        self.chat_id = str(chat_id)
        self.user_id = str(user_id)
        self.state_root = self.root / "state"
        self.workspace_root = self.root / "workspaces"
        self.hermes_home = self.root / "hermes-home"

        # Real persistence.
        self.store = ProjectStateStore(self.state_root)
        self.registry = ConversationRegistryStore(self.state_root / "conversations")

        # Fake boundaries.
        self.hermes = FakeHermes()
        self.vercel = FakeVercel()
        self.telegram = FakeTelegramOut()
        self.smoke = FakeSmoke()
        self.output_repo = FakeOutputRepo()
        self.runner = RecordingRunner(self.workspace_root)

        # Real domain logic.
        self.intake = IntakeProcessor(self.store, hermes_adapter=self.hermes)
        self.router = ConversationRouter(self.store, self.registry,
                                        telegram_out=self.telegram, hermes=self.hermes)

        # PHASE E: real secret store under the scenario's hermes-home, wired
        # to the real provisioner over the fake Vercel boundary.
        from app.core.secrets import BypassSecretStore
        from app.deploy.bypass import BypassProvisioner
        self.bypass_store = BypassSecretStore(self.hermes_home / "vercel-bypass")
        self.bypass_provisioner = BypassProvisioner(self.vercel, self.bypass_store)

        self.preview = PreviewOrchestrator(
            self.store,
            PreviewDeps(
                vercel=self.vercel,
                telegram=self.telegram,
                smoke=self.smoke,
                output_repo=self.output_repo,
                chat_id_for=lambda pid, state: state.conversation_id,
                display_name_for=lambda pid, state: (
                    self.registry.display_name_for(state.conversation_id, pid)
                    if state is not None and state.conversation_id else None
                ),
                slug_for=self._slug_for,
                bind_slug=self._bind_slug,
                ensure_bypass=lambda app_id, project, expected_name=None: (
                    self.bypass_provisioner.ensure(
                        app_id, project, expected_name=expected_name
                    )
                ),
                bypass_is_stored=lambda project_id, state: bool(
                    self.bypass_store.get(self.vercel.project_id)
                ),
                reprovision_bypass=lambda app_id, project, expected_name=None: (
                    self.bypass_provisioner.ensure(
                        app_id, project, expected_name=expected_name,
                        reprovision=True,
                    )
                ),
                sleep=lambda _seconds: None,
            ),
            runner=self.runner,
        )
        self.builder = FrontendBuilder(
            runner=self.runner, store=self.store,
            hermes_adapter=self.hermes, preview_orchestrator=self.preview,
        )
        self.revise = RevisionOrchestrator(
            runner=self.runner, store=self.store, hermes_adapter=self.hermes,
            preview_orchestrator=self.preview,
        )
        self.promote = PromotionOrchestrator(
            self.runner, self.store,
            PromoteDeps(
                vercel=self.vercel, telegram=self.telegram, smoke=self.smoke,
                chat_id_for=lambda pid, state: state.conversation_id,
                slug_for=self._bound_slug_for,
            ),
        )
        self.dispatcher = TelegramDispatcher(
            store=self.store, intake=self.intake, revise=self.revise,
            promote=self.promote, workspace_for=self.runner.create_workspace,
            builder=self.builder, preview=self.preview,
        )

        # Injection point for Bug 4 (bind_slug persistence failure).
        self.bind_slug_raises: Optional[Exception] = None
        # Injection point for Bug 7 (crash AFTER durable preview delivery but
        # BEFORE revision finalization).
        self.after_preview_delivery_hook: Optional[Callable[[], None]] = None

        self._event_seq = 0
        self._loop = self._make_loop()

    # -- slug wiring (mirrors app/runtime.py compose()) --------------------
    def _slug_for(self, pid, state):
        if state is None or not state.conversation_id:
            return None
        entry = self.registry.load_or_create(state.conversation_id).find_by_id(pid)
        if entry is None:
            return None
        if entry.vercel_slug:
            return entry.vercel_slug
        # PHASE C: converge registry.display_name onto the confirmed
        # brief['name'] BEFORE deriving the slug (mirrors app/runtime.py).
        confirmed = (state.brief or {}).get("name")
        if isinstance(confirmed, str) and confirmed.strip():
            resolution = self.registry.converge_display_name(
                state.conversation_id, pid, confirmed.strip()
            )
            if resolution.status in ("ok", "noop") and resolution.entry is not None:
                entry = resolution.entry
        from app.core.registry import slugify_display_name
        return slugify_display_name(entry.display_name)

    def _bind_slug(self, pid, state, slug):
        if self.bind_slug_raises is not None:
            raise self.bind_slug_raises
        self.registry.set_vercel_slug_once(state.conversation_id, pid, slug)

    def _bound_slug_for(self, pid, state):
        if state is None or not state.conversation_id:
            return None
        entry = self.registry.load_or_create(state.conversation_id).find_by_id(pid)
        return entry.vercel_slug if entry else None

    def _make_loop(self) -> TelegramReceiveLoop:
        return TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=self.dispatcher,
            telegram_out=self.telegram,
            hermes=self.hermes,
            transport=None,
            conversations=self.router,
        )

    def restart(self) -> None:
        """Simulate a process restart over the SAME temp state root."""
        self._loop = self._make_loop()

    def restart_components(self) -> None:
        """Recreate production collaborators while preserving external fakes."""
        from app.core.secrets import BypassSecretStore
        from app.deploy.bypass import BypassProvisioner
        self.store = ProjectStateStore(self.state_root)
        self.registry = ConversationRegistryStore(self.state_root / "conversations")
        self.runner = RecordingRunner(self.workspace_root)
        self.intake = IntakeProcessor(self.store, hermes_adapter=self.hermes)
        self.router = ConversationRouter(
            self.store, self.registry, telegram_out=self.telegram, hermes=self.hermes
        )
        self.bypass_store = BypassSecretStore(self.hermes_home / "vercel-bypass")
        self.bypass_provisioner = BypassProvisioner(self.vercel, self.bypass_store)
        self.preview = PreviewOrchestrator(
            self.store,
            PreviewDeps(
                vercel=self.vercel,
                telegram=self.telegram,
                smoke=self.smoke,
                output_repo=self.output_repo,
                chat_id_for=lambda pid, state: state.conversation_id,
                display_name_for=lambda pid, state: (
                    self.registry.display_name_for(state.conversation_id, pid)
                    if state is not None and state.conversation_id else None
                ),
                slug_for=self._slug_for,
                bind_slug=self._bind_slug,
                ensure_bypass=lambda app_id, project, expected_name=None: (
                    self.bypass_provisioner.ensure(
                        app_id, project, expected_name=expected_name
                    )
                ),
                bypass_is_stored=lambda project_id, state: bool(
                    self.bypass_store.get(self.vercel.project_id)
                ),
                reprovision_bypass=lambda app_id, project, expected_name=None: (
                    self.bypass_provisioner.ensure(
                        app_id, project, expected_name=expected_name,
                        reprovision=True,
                    )
                ),
                sleep=lambda _seconds: None,
            ),
            runner=self.runner,
        )
        self.builder = FrontendBuilder(
            runner=self.runner, store=self.store,
            hermes_adapter=self.hermes, preview_orchestrator=self.preview,
        )
        self.revise = RevisionOrchestrator(
            runner=self.runner, store=self.store, hermes_adapter=self.hermes,
            preview_orchestrator=self.preview,
        )
        self.promote = PromotionOrchestrator(
            self.runner, self.store,
            PromoteDeps(
                vercel=self.vercel, telegram=self.telegram, smoke=self.smoke,
                chat_id_for=lambda pid, state: state.conversation_id,
                slug_for=self._bound_slug_for,
            ),
        )
        self.dispatcher = TelegramDispatcher(
            store=self.store, intake=self.intake, revise=self.revise,
            promote=self.promote, workspace_for=self.runner.create_workspace,
            builder=self.builder, preview=self.preview,
        )
        self._loop = self._make_loop()

    def _wipe_bypass_store(self) -> None:
        """Test-only: empty the LOCAL secure bypass store (simulating a run
        where the remote bypass exists but the local secret was never
        persisted, e.g. a prior response-parse failure). The REMOTE bypass
        state on the fake Vercel boundary is left intact."""
        root = self.hermes_home / "vercel-bypass"
        if root.exists():
            for child in root.iterdir():
                if child.is_file():
                    child.unlink()

    # -- scripting passthroughs -------------------------------------------
    def set_router_decision(self, *args, **kwargs):
        self.hermes.set_router_decision(*args, **kwargs)

    def clear_router_decision(self):
        self.hermes.clear_router_decision()

    def set_intent_response(self, intent: str):
        # Production `_parse_intent_response` expects the BARE intent name
        # (the prompt instructs "Respond with ONLY the intent name"), so the
        # fake must return plain text -- not JSON -- or classification would
        # always fail-safe to INTAKE and mask the routing/recovery paths.
        self.hermes.intent_response = intent

    def set_intake_response(self, **fields):
        base = {
            "scope": "WEBSITE", "name": None, "what": None, "why": None,
            "why_destination": None, "ambiguity": None,
            "clarification_needed": False, "clarification_question": None,
            "readiness": "DISCOVERY_READY",
        }
        base.update(fields)
        self.hermes.intake_response = base

    def set_frontend_result(self, result: Dict[str, Any]):
        self.hermes.frontend_result = result

    def set_vision_result(self, result: Any):
        self.hermes.vision_result = result

    def set_vercel_behavior(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self.vercel, k, v)

    def set_smoke_result(self, success: bool):
        self.smoke.success = success
        self.smoke._success_sequence = []

    def set_smoke_sequence(self, results):
        self.smoke._success_sequence = list(results)
        if results:
            self.smoke.success = results[0]

    def set_smoke_failure(self, *, classification: Optional[str] = None,
                          records: Optional[List[Dict[str, Any]]] = None):
        """Configure a FAILING smoke with an optional classification/records,
        mirroring the real PreviewSmokeTester's sanitized diagnostic shape."""
        self.smoke.success = False
        self.smoke.failure_classification = classification
        self.smoke.failure_records = list(records or [])

    def set_telegram_behavior(self, *, send_photo_raises=None):
        self.telegram.send_photo_raises = send_photo_raises

    # -- durable truth -----------------------------------------------------
    def registry_entry(self, project_id: Optional[str] = None):
        reg = self.registry.load_or_create(self.chat_id)
        if project_id is None:
            project_id = reg.active_project_id
        return reg.find_by_id(project_id) if project_id else None

    def project_state(self, project_id: Optional[str] = None):
        if project_id is None:
            project_id = self.registry.active_project_id(self.chat_id)
        return self.store.load(project_id) if project_id else None

    @property
    def current_project_id(self) -> Optional[str]:
        return self.registry.active_project_id(self.chat_id)

    @property
    def current_slug(self) -> Optional[str]:
        entry = self.registry_entry()
        return entry.vercel_slug if entry else None

    @property
    def current_lifecycle(self) -> Optional[str]:
        state = self.project_state()
        return state.lifecycle if state else None

    @property
    def vercel_calls(self) -> Dict[str, int]:
        return {
            "create_post": self.vercel.post_calls,
            "get": self.vercel.get_calls,
            "deploy_post": self.vercel.deploy_calls,
            "promote_post": self.vercel.promote_calls,
            "bootstrap": self.vercel.bootstrap_calls,
            "bypass_generation": self.vercel.bypass_generation_calls,
        }

    @property
    def telegram_calls(self) -> Dict[str, int]:
        return {"photo": len(self.telegram.photo_calls),
                "text": len(self.telegram.text_calls)}

    # -- durable-state accessors (assert against PERSISTED truth) ----------
    @property
    def conversation_registry(self):
        return self.registry.load_or_create(self.chat_id)

    @property
    def pending_action(self) -> Optional[Dict[str, str]]:
        return self.conversation_registry.pending_action

    def latest_shown_preview(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        state = self.project_state(project_id)
        return (state.deployment.get("latest_shown_preview") or {}) if state else {}

    def preview_intent(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        state = self.project_state(project_id)
        return (state.deployment.get("preview_intent") or {}) if state else {}

    def pending_revisions(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        state = self.project_state(project_id)
        return list(state.pending_revisions) if state else []

    def dispatch_events(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        state = self.project_state(project_id)
        return dict(state.dispatch_events) if state else {}

    def revision_counters(self, project_id: Optional[str] = None) -> Dict[str, int]:
        state = self.project_state(project_id)
        if state is None:
            return {}
        r = state.revisions
        return {"revision_seq": r.revision_seq,
                "queued_revision_seq": r.queued_revision_seq,
                "source_revision": r.source_revision,
                "qa_revision": r.qa_revision,
                "preview_revision": r.preview_revision,
                "approved_revision": r.approved_revision,
                "live_revision": r.live_revision}

    # -- driving the flow --------------------------------------------------
    def send_user_message(self, text: str, *, event_id: Optional[str] = None) -> None:
        self._event_seq += 1
        eid = event_id if event_id is not None else self._event_seq
        update = {
            "update_id": eid,
            "message": {
                "from": {"id": self.user_id},
                "chat": {"id": int(self.chat_id)},
                "text": text,
                "date": eid if isinstance(eid, int) else 0,
            },
        }
        self._loop._process_update(update)

    def run_build_and_preview(self, project_id: Optional[str] = None) -> None:
        """Drive the canonical build -> QA -> preview pipeline for a project
        in READY (or WAITING_INPUT with a complete brief) state.

        Uses the REAL FrontendBuilder + PreviewOrchestrator with the fake
        boundaries. Admission/transitioning is done through the dispatcher's
        real "build" action so claim persistence is exercised too.
        """
        pid = project_id or self.current_project_id
        self.runner.create_workspace(pid)
        self.builder.build(pid, dict(self.store.load(pid).brief))

    def run_revision(self, text: str) -> Any:
        pid = self.current_project_id
        state = self.store.load(pid)
        seq = state.revisions.queued_revision_seq + 1
        principal = self.principal_id
        reserve = self.revise.reserve(pid, seq, principal_id=principal)
        if not reserve.success:
            return reserve
        return self.revise.apply(pid, seq, text, principal_id=principal)

    @property
    def principal_id(self) -> str:
        """The principal id the dispatcher derives for this chat/user."""
        return AuthenticatedTelegramContext(self.user_id, self.chat_id).principal_id

    def approve(self) -> Any:
        return self.promote.approve(self.current_project_id,
                                    principal_id=self.principal_id)

    def promote_now(self) -> Any:
        pid = self.current_project_id
        return self.promote.promote(pid, self.runner.create_workspace(pid),
                                    principal_id=self.principal_id)

    # -- direct seeding helpers (bypass intake auto-build) ------------------
    def seed_project(self, display_name: str = "kitsunereading",
                     *, brief: Optional[Dict[str, Any]] = None,
                     project_id: Optional[str] = None) -> str:
        """Create a registry entry + materialized project state + ACL WITHOUT
        driving intake (so no auto-build fires). Used by tests that need to
        start from a specific lifecycle boundary while still exercising the
        real registry/state persistence."""
        entry = self.registry.allocate_project(self.chat_id, display_name)
        pid = project_id or entry.project_id
        with self.store.acquire_writer(pid) as state:
            state.owner_id = self.principal_id
            state.roles["owner"] = self.principal_id
            state.channel = "telegram"
            state.conversation_id = self.chat_id
            state.brief = dict(brief or {"name": display_name, "what": "reading rental",
                                         "why": "rent books"})
            self.store.save(state)
        self.registry.set_active(self.chat_id, pid)
        return pid


# ---------------------------------------------------------------------------
# Boundary patching helper
# ---------------------------------------------------------------------------


def patch_qa_boundaries(monkeypatch) -> None:
    """Replace the QA orchestrator's render/screenshot defaults with the
    deterministic fakes so the REAL QA pipeline runs without a browser.

    Only the *boundary classes* the orchestrator instantiates by default are
    patched (``app.qa.orchestrator.LocalRenderer`` and ``ScreenshotCapture``);
    every other line of QA control flow is production code.
    """
    from app.qa import orchestrator as qa_module

    monkeypatch.setattr(qa_module, "LocalRenderer", FakeRenderer, raising=False)
    monkeypatch.setattr(qa_module, "ScreenshotCapture", FakeScreenshotCapture,
                        raising=False)


# ---------------------------------------------------------------------------
# Deterministic content helpers
# ---------------------------------------------------------------------------


def _design_dna() -> Dict[str, Any]:
    return {
        "version": 1,
        "brand_personality": "soft, bright, calm",
        "palette": {"primary": "#f7f3ea", "accent": "#f2b880"},
        "typography": {"heading_font": "Inter", "body_font": "Source Sans 3"},
        "spacing": {"density": "airy"},
        "page_inventory": ["home"],
        "layout": {"navigation": "top-bar"},
        "motion": {"enabled": True},
        "primary_cta": {"label": "Sewa Buku", "destination": None},
        "assets": [],
        "verified_content": {"name": None, "what": "reading rental", "why": "rent books"},
        "unresolved_facts": [],
    }


def _materialize_site(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "src").mkdir(exist_ok=True)
    (workspace / "dist").mkdir(exist_ok=True)
    (workspace / "src" / "App.tsx").write_bytes(b"export default () => null\n")
    (workspace / "dist" / "index.html").write_bytes(b"<html>reading rental</html>\n")
    # FRONTEND is responsible for persisting Design DNA into the workspace;
    # the deterministic QA check reads it back from here.
    (workspace / "design-dna.json").write_text(
        json.dumps(_design_dna(), indent=2), encoding="utf-8"
    )


def make_preview_ready(store: ProjectStateStore, project_id: str, workspace: Path,
                       source_revision: int = 1) -> TestedSnapshot:
    """Deterministically put a project into the exact PREVIEW_READY shape the
    real QA pipeline produces (used by tests that need a preview boundary to
    start from without re-running the whole build)."""
    if not (Path(workspace) / "dist" / "index.html").exists():
        _materialize_site(Path(workspace))
    snap = TestedSnapshot.capture(workspace)
    with store.acquire_writer(project_id) as state:
        state.lifecycle = ProjectLifecycle.PREVIEW_READY.value
        state.revisions.source_revision = source_revision
        state.revisions.qa_revision = source_revision
        state.revisions.preview_revision = 0
        state.deployment["checked"] = {
            "source_revision": source_revision,
            "source_sha256": snap.source_sha256,
            "artifact_sha256": snap.artifact_sha256,
        }
        state.deployment["tested_snapshot"] = snap.to_dict()
        store.save(state)
    return snap


def source_hash(workspace: Path) -> str:
    return source_fingerprint(workspace)


@dataclass
class ObservedSideEffects:
    vercel_create_posts: int = 0
    vercel_deploy_posts: int = 0
    vercel_promote_posts: int = 0
    telegram_photos: int = 0
    telegram_texts: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)


def snapshot_side_effects(h: LocalR1Scenario) -> ObservedSideEffects:
    return ObservedSideEffects(
        vercel_create_posts=h.vercel.post_calls,
        vercel_deploy_posts=h.vercel.deploy_calls,
        vercel_promote_posts=h.vercel.promote_calls,
        telegram_photos=len(h.telegram.photo_calls),
        telegram_texts=len(h.telegram.text_calls),
    )
