"""FRONTEND revision failures must never be silent.

FRONTEND is prompted to answer with ``{"success": true|false,
"design_dna_path": "...", "error": "..."}`` -- so ``error`` is PRESENT AND
EMPTY on a *successful* build. The parser used to key success purely off
``error is None``, which reclassified a completed FRONTEND run as a failure
with a blank reason; the revision orchestrator then persisted that blank
verbatim and an operator was told nothing.

Two separate contracts are asserted here:

  1. The PARSER: ``error`` is not a success signal, the explicit ``success``
     field is authoritative, a blank reason is never a reason, and a declared
     failure with no usable reason is classified rather than silent.
  2. The ORCHESTRATOR: whatever the adapter hands back, a persisted revision
     failure carries a non-empty diagnostic. This is the invariant that the
     parser fix alone does not guarantee -- any adapter (mock, alternate, or
     future) must be unable to persist ``state.failure.error == ""``.

No live LLM, browser, or npm is required. QAOrchestrator and
PreviewOrchestrator boundaries are stubbed exactly as in ``test_revise.py``.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.core.contracts import OperationResult
from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore
# Aliased for the same reason as in test_revise.py: ``TestedSnapshot`` is a
# dataclass, and pytest would otherwise try to collect a class named "Test*".
from app.deploy.snapshot import TestedSnapshot as Snapshot
from app.hermes.adapter import (
    FRONTEND_RESULT_CONTRACT_INVALID,
    HermesAdapter,
    HermesResult,
)
from app.projects.revise import (
    _FRONTEND_FAILURE_FALLBACK,
    _frontend_failure_diagnostic,
    RevisionOrchestrator,
)
from app.sandbox.runner import ProjectRunner


OWNER = "owner-1"

DESIGN_DNA = {
    "version": 2,
    "typography": {"heading_font": "Inter", "body_font": "Inter"},
}


def _make_workspace(root: Path, project_id: str, *, token: str | None = None) -> Path:
    """Materialize a workspace, optionally as a pointer-mode ``.ops`` op."""
    if token is None:
        ws = root / "workspaces" / project_id
    else:
        ws = root / "workspaces" / project_id / ".ops" / token
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "App.tsx").write_text("// content", encoding="utf-8")
    (ws / "design-dna.json").write_text('{"version": 1}', encoding="utf-8")
    # ``record_checks`` fingerprints the built ``dist`` tree; production
    # workspaces always have one after a FRONTEND build.
    (ws / "dist").mkdir(exist_ok=True)
    (ws / "dist" / "index.html").write_text("<html>rev</html>", encoding="utf-8")
    if token is not None:
        (root / "workspaces" / project_id / "current").write_text(
            f"{token}\n", encoding="utf-8")
    return ws


class RevisionFailureTestBase(unittest.TestCase):
    """Shared fixture: a PREVIEW_READY project with a passing-check stub."""

    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmpdir_obj.name)
        self.workspace_root = self.tmpdir / "workspaces"
        self.store = ProjectStateStore(self.tmpdir / "state")
        self.runner = ProjectRunner(self.workspace_root, self.store)
        self.mock_adapter = MagicMock()
        self.mock_preview = MagicMock()
        self.mock_preview.run_owned.return_value = OperationResult.ok(
            {"preview_url": "https://x.vercel.app"})
        self.orchestrator = RevisionOrchestrator(
            self.runner, self.store,
            hermes_adapter=self.mock_adapter,
            preview_orchestrator=self.mock_preview,
        )
        # The revision pipeline re-runs the fixed cheap checks before QA so it
        # can re-record the ``checked`` binding the preview stage requires.
        # These tests never spawn npm, so the shared runner is stubbed exactly
        # like QAOrchestrator/preview are.
        self._checks_patch = patch(
            "app.projects.revise.run_fixed_checks",
            return_value={"npm_ci": {"success": True},
                          "npm_build": {"success": True},
                          "npm_typecheck": {"success": True}},
        )
        self._checks_patch.start()
        self.addCleanup(self._checks_patch.stop)

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    def _passing_qa(self):
        return patch(
            "app.projects.revise.QAOrchestrator",
            return_value=MagicMock(run=MagicMock(
                return_value=MagicMock(success=True, error=None))),
        )

    def _preview_ready(self, project_id: str, *, token: str | None = None) -> Path:
        ws = _make_workspace(self.tmpdir, project_id, token=token)
        snapshot = Snapshot.capture(ws)
        with self.store.acquire_writer(project_id) as state:
            state.brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}
            state.design_dna = {"version": 1, "typography": {
                "heading_font": "Inter", "body_font": "Inter"}}
            state.lifecycle = ProjectLifecycle.RUNNING.value
            state.lifecycle = ProjectLifecycle.PREVIEW_READY.value
            state.roles["owner"] = OWNER
            # A DRAFT revision continues from the last tested snapshot, so
            # PREVIEW_READY must carry the ``checked`` binding + snapshot that
            # ``reserve()`` proves as the admissible base.
            state.deployment["checked"] = {
                "source_revision": state.revisions.source_revision,
                "source_sha256": snapshot.source_sha256,
                "artifact_sha256": snapshot.artifact_sha256,
            }
            state.deployment["tested_snapshot"] = snapshot.to_dict()
            self.store.save(state)
        return ws


# ---------------------------------------------------------------------------
# 1. The parser: real ``_parse_frontend_response``, no mocks.
# ---------------------------------------------------------------------------

class TestFrontendResultContract(unittest.TestCase):
    """``error`` is not a success signal; ``success`` is."""

    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmpdir_obj.name)
        self.adapter = HermesAdapter(
            ProjectStateStore(self.tmpdir / "state"),
            hermes_home=self.tmpdir / ".hermes-website",
            repo_root=self.tmpdir / "repo",
        )
        self.workspace = self.tmpdir / "ws"
        (self.workspace / "src").mkdir(parents=True)

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    def _parse(self, response: str) -> dict:
        return self.adapter._parse_frontend_response(response, self.workspace)

    def test_declared_success_with_an_empty_error_is_a_success(self):
        """THE observed failure: exit 0, ``"success": true, "error": ""``.

        Before the fix ``success`` was ``error is None``, so this returned
        ``success=False`` with a blank reason.
        """
        result = self._parse(
            '{"success": true, "design_dna_path": "d.json", "error": ""}')
        self.assertTrue(result["success"])
        self.assertIsNone(result["error"])
        self.assertNotIn("error_code", result)

    def test_declared_success_with_no_error_key_is_a_success(self):
        result = self._parse('{"success": true, "design_dna_path": "d.json"}')
        self.assertTrue(result["success"])
        self.assertIsNone(result["error"])

    def test_a_blank_error_is_never_a_failure_reason(self):
        for blank in ("", "   ", "\n", "\t\n "):
            with self.subTest(blank=repr(blank)):
                self.assertTrue(self._parse(
                    json.dumps({"success": True, "error": blank}))["success"])
                # ...and it does not manufacture a failure on its own either.
                self.assertTrue(self._parse(json.dumps({"error": blank}))["success"])

    def test_declared_failure_keeps_its_reason(self):
        result = self._parse('{"success": false, "error": "brand facts missing"}')
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "brand facts missing")
        # A reason was reported, so this is NOT an invalid contract.
        self.assertNotIn("error_code", result)

    def test_declared_failure_with_a_blank_reason_is_classified(self):
        for payload in ({"success": False, "error": ""},
                        {"success": False, "error": "  \n "},
                        {"success": False, "error": None},
                        {"success": False}):
            with self.subTest(payload=payload):
                result = self._parse(json.dumps(payload))
                self.assertFalse(result["success"])
                self.assertEqual(result["error_code"],
                                 FRONTEND_RESULT_CONTRACT_INVALID)
                # A failure must always be able to explain itself.
                self.assertTrue((result["error"] or "").strip(),
                                f"blank diagnostic for {payload!r}")

    def test_a_legacy_summary_with_only_a_reason_is_still_a_failure(self):
        """No explicit ``success`` field: the reason IS the declaration."""
        result = self._parse('{"design_dna_path": "d.json", "error": "nope"}')
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "nope")

    def test_truncated_and_empty_responses_keep_artifact_recovery(self):
        """The supervision-timeout recovery path passes ``""``.

        No declaration at all means the artifacts on disk are authoritative,
        which is exactly the behaviour the timeout recovery relies on.
        """
        for response in ("", "I built the site but my summary got cut off...",
                         '{"success": true, "design_dna_path": "d.jso'):
            with self.subTest(response=response):
                result = self._parse(response)
                self.assertTrue(result["success"])
                self.assertIsNone(result["error"])

    def test_design_dna_is_still_loaded_from_the_workspace(self):
        """The parser's other job is unchanged by the contract fix."""
        (self.workspace / "design-dna.json").write_text(
            json.dumps({"version": 3, "brand_personality": "premium"}),
            encoding="utf-8")
        result = self._parse('{"success": true, "error": ""}')
        self.assertEqual(result["design_dna"]["brand_personality"], "premium")

    def test_a_non_dict_payload_is_not_a_declaration(self):
        """A bare JSON array declares nothing; artifacts decide (legacy)."""
        self.assertTrue(self._parse("[1, 2, 3]")["success"])


# ---------------------------------------------------------------------------
# 2. The orchestrator: the persisted diagnostic is never blank.
# ---------------------------------------------------------------------------

class TestDiagnosticPrecedence(unittest.TestCase):
    """``_frontend_failure_diagnostic`` must always return a usable string."""

    def test_prefers_the_adapters_own_reason(self):
        self.assertEqual(
            _frontend_failure_diagnostic(
                {"success": False, "error": "boom", "error_code": "E",
                 "invocation": {"outcome": "idle_timeout"}}),
            "boom")

    def test_falls_back_to_the_stable_error_code(self):
        self.assertEqual(
            _frontend_failure_diagnostic(
                {"success": False, "error": "", "error_code": "FRONTEND_IDLE_TIMEOUT",
                 "invocation": {"outcome": "idle_timeout"}}),
            "FRONTEND_IDLE_TIMEOUT")

    def test_falls_back_to_the_supervision_receipt_outcome(self):
        self.assertEqual(
            _frontend_failure_diagnostic(
                {"success": False, "error_code": "",
                 "invocation": {"outcome": "idle_timeout"}}),
            "idle_timeout")

    def test_a_result_with_no_signal_at_all_still_names_the_class(self):
        for result in ({"success": False},
                       {"success": False, "error": None, "error_code": None},
                       {"success": False, "error": "   "},
                       {"success": False, "invocation": {}},
                       {"success": False, "invocation": "not-a-receipt"}):
            with self.subTest(result=result):
                diagnostic = _frontend_failure_diagnostic(result)
                self.assertEqual(diagnostic, _FRONTEND_FAILURE_FALLBACK)
                self.assertTrue(diagnostic.strip())


class TestRevisionFailurePersistence(RevisionFailureTestBase):
    """End-to-end: nothing that reaches ``state.failure`` is ever blank."""

    # -- A: the observed path now proceeds --------------------------------
    def test_a_frontend_success_with_an_empty_error_proceeds(self):
        ws = self._preview_ready("proj-a")
        self.orchestrator.reserve("proj-a", 1, principal_id=OWNER)
        # Exactly the shape that produced the silent failure.
        self.mock_adapter.frontend_build.return_value = {
            "success": True,
            "design_dna": DESIGN_DNA,
            "error": None,
        }
        with self._passing_qa():
            result = self.orchestrator.apply(
                "proj-a", 1, "make the header red", workspace=ws, principal_id=OWNER)
        self.assertTrue(result.success, result.error)
        state = self.store.load("proj-a")
        self.assertEqual(state.revisions.revision_seq, 1)
        self.assertFalse(state.failure)

    # -- B / B2 / B3: the persisted diagnostic is non-empty ---------------
    def test_a_non_empty_reason_is_persisted_verbatim(self):
        ws = self._preview_ready("proj-b")
        self.orchestrator.reserve("proj-b", 1, principal_id=OWNER)
        self.mock_adapter.frontend_build.return_value = {
            "success": False, "error": "FRONTEND exploded",
        }
        result = self.orchestrator.apply(
            "proj-b", 1, "req", workspace=ws, principal_id=OWNER)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "FRONTEND_REVISION_FAILED")
        failure = self.store.load("proj-b").failure
        self.assertEqual(failure["error"], "FRONTEND exploded")
        self.assertEqual(failure["error_code"], "FRONTEND_REVISION_FAILED")
        self.assertEqual(failure["phase"], "revision")

    def test_a_blank_reason_falls_back_to_the_error_code(self):
        ws = self._preview_ready("proj-b2")
        self.orchestrator.reserve("proj-b2", 1, principal_id=OWNER)
        self.mock_adapter.frontend_build.return_value = {
            "success": False, "error": "", "error_code": "FRONTEND_IDLE_TIMEOUT",
        }
        self.orchestrator.apply("proj-b2", 1, "req", workspace=ws, principal_id=OWNER)
        failure = self.store.load("proj-b2").failure
        self.assertEqual(failure["error"], "FRONTEND_IDLE_TIMEOUT")
        self.assertEqual(failure["error_code"], "FRONTEND_IDLE_TIMEOUT")

    def test_a_result_with_no_signal_still_persists_a_diagnostic(self):
        ws = self._preview_ready("proj-b3")
        self.orchestrator.reserve("proj-b3", 1, principal_id=OWNER)
        self.mock_adapter.frontend_build.return_value = {"success": False}
        self.orchestrator.apply("proj-b3", 1, "req", workspace=ws, principal_id=OWNER)
        failure = self.store.load("proj-b3").failure
        self.assertEqual(failure["error"], _FRONTEND_FAILURE_FALLBACK)
        # The default classification is unchanged.
        self.assertEqual(failure["error_code"], "FRONTEND_REVISION_FAILED")

    # -- C: the classified contract failure -------------------------------
    def test_an_invalid_frontend_contract_is_classified_and_non_empty(self):
        ws = self._preview_ready("proj-c")
        self.orchestrator.reserve("proj-c", 1, principal_id=OWNER)
        self.mock_adapter.frontend_build.return_value = {
            "success": False,
            "error": ("FRONTEND reported success=false without a usable error "
                      "reason (invalid result contract)"),
            "error_code": FRONTEND_RESULT_CONTRACT_INVALID,
        }
        result = self.orchestrator.apply(
            "proj-c", 1, "req", workspace=ws, principal_id=OWNER)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, FRONTEND_RESULT_CONTRACT_INVALID)
        failure = self.store.load("proj-c").failure
        self.assertEqual(failure["error_code"], FRONTEND_RESULT_CONTRACT_INVALID)
        self.assertTrue(failure["error"].strip())

    # -- D: an exception with an empty str() ------------------------------
    def test_an_exception_with_an_empty_message_still_records_its_class(self):
        ws = self._preview_ready("proj-d")
        self.orchestrator.reserve("proj-d", 1, principal_id=OWNER)
        self.mock_adapter.frontend_build.side_effect = RuntimeError("")
        result = self.orchestrator.apply(
            "proj-d", 1, "req", workspace=ws, principal_id=OWNER)
        self.assertFalse(result.success)
        failure = self.store.load("proj-d").failure
        self.assertEqual(failure["error_code"], "FRONTEND_REVISION_EXCEPTION")
        # ``str(exc)`` is empty, so the class name is the only signal there is.
        self.assertIn("RuntimeError", failure["error"])

    # -- E: the reservation is not consumed by a failed revision ---------
    def test_a_frontend_failure_leaves_the_reservation_unapplied(self):
        ws = self._preview_ready("proj-e")
        self.orchestrator.reserve("proj-e", 1, principal_id=OWNER)
        self.mock_adapter.frontend_build.return_value = {
            "success": False, "error": "",
        }
        self.orchestrator.apply("proj-e", 1, "req", workspace=ws, principal_id=OWNER)
        state = self.store.load("proj-e")
        self.assertEqual(state.revisions.revision_seq, 0)
        self.assertEqual(state.revisions.queued_revision_seq, 1)
        self.assertTrue(all(e["applied"] is False for e in state.pending_revisions))

    # -- F: nothing downstream runs after a FRONTEND failure --------------
    def test_no_qa_or_preview_runs_after_a_frontend_failure(self):
        ws = self._preview_ready("proj-f")
        self.orchestrator.reserve("proj-f", 1, principal_id=OWNER)
        self.mock_adapter.frontend_build.return_value = {
            "success": False, "error": "",
        }
        with patch("app.projects.revise.QAOrchestrator") as qa_cls:
            self.orchestrator.apply(
                "proj-f", 1, "req", workspace=ws, principal_id=OWNER)
        qa_cls.assert_not_called()
        self.mock_preview.run_owned.assert_not_called()
        self.assertEqual(self.store.load("proj-f").lifecycle,
                         ProjectLifecycle.FAILED.value)


class TestPointerModeRevisionFailure(RevisionFailureTestBase):
    """Case G: the production shape -- hydration, ``.ops/<token>``, no seam.

    Production never passes ``workspace``; the workspace comes from the
    hydrator's pointer swap. A silent failure here would be invisible in every
    seam-injected test above, so the pointer path gets the same non-empty
    diagnostic guarantee.
    """

    class _Hydrator:
        """Minimal stand-in for the real pointer-swap hydrator."""

        def __init__(self, root: Path, project_id: str):
            self._root = root
            self._project_id = project_id

        def hydrate(self, project_id, seq, base):
            ws = _make_workspace(self._root, project_id, token="rev-2")
            return SimpleNamespace(
                workspace=ws, base_kind="draft",
                hydrated_from={"fetched_locally": True},
            )

    def test_a_frontend_failure_in_the_pointer_workspace_is_not_silent(self):
        self._preview_ready("proj-g")
        self.orchestrator.hydrator = self._Hydrator(self.tmpdir, "proj-g")
        self.orchestrator.reserve("proj-g", 1, principal_id=OWNER)
        self.mock_adapter.frontend_build.return_value = {
            "success": False, "error": "",
        }

        # No ``workspace`` seam: the orchestrator must resolve .ops/rev-2.
        result = self.orchestrator.apply("proj-g", 1, "req", principal_id=OWNER)

        self.assertFalse(result.success)
        called_with = self.mock_adapter.frontend_build.call_args.kwargs
        self.assertEqual(Path(called_with["workspace"]).name, "rev-2")
        self.assertEqual(Path(called_with["workspace"]).parent.name, ".ops")

        failure = self.store.load("proj-g").failure
        self.assertEqual(failure["phase"], "revision")
        self.assertEqual(failure["error"], _FRONTEND_FAILURE_FALLBACK)
        self.assertEqual(failure["error_code"], "FRONTEND_REVISION_FAILED")
