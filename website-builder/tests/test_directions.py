"""Phase 13 tests: lightweight design directions for no-reference,
non-delegated users.

Tests do NOT require a live LLM, browser, or npm. Hermes adapter is a
mock; FrontendBuilder's fixed checks/QA/preview boundaries are stubbed
exactly like test_build.py/test_revise.py.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.contracts import OperationResult
from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore
from app.projects.build import FrontendBuilder
from app.projects.directions import (
    DirectionsOrchestrator,
    design_authority_delegated,
    direction_build_instructions,
    has_references,
)
from app.sandbox.runner import ProjectRunner


OWNER = "owner-1"
STRANGER = "stranger-9"

_TWO_DIRECTIONS = {
    "success": True,
    "directions": [
        {
            "label": "Minimal Editorial",
            "descriptor": "Clean, generous whitespace, serif headings.",
            "palette": {"primary": "#111111", "secondary": "#ffffff", "accent": "#c9a227"},
        },
        {
            "label": "Bold Modern",
            "descriptor": "High contrast, geometric sans, punchy accent color.",
            "palette": {"primary": "#000000", "secondary": "#f4f4f4", "accent": "#ff3b30"},
        },
    ],
}


def _queue_project(store: ProjectStateStore, project_id: str) -> None:
    store.transition_lifecycle(project_id, ProjectLifecycle.READY)
    store.transition_lifecycle(project_id, ProjectLifecycle.QUEUED)


class DirectionsOrchestratorTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmpdir_obj.name)
        self.state_root = self.tmpdir / "state"
        self.store = ProjectStateStore(self.state_root)
        self.mock_adapter = MagicMock()
        self.orchestrator = DirectionsOrchestrator(self.store, hermes_adapter=self.mock_adapter)

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    def _owned_project(self, project_id: str) -> None:
        with self.store.acquire_writer(project_id) as state:
            state.brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}
            state.roles["owner"] = OWNER
            self.store.save(state)


class TestProposeDirections(DirectionsOrchestratorTestBase):
    def test_propose_returns_bounded_2_to_3_directions_without_full_build(self):
        self._owned_project("proj-a")
        self.mock_adapter.frontend_propose_directions.return_value = _TWO_DIRECTIONS

        result = self.orchestrator.propose(
            "proj-a", brief={"name": "Northcut", "what": "barbershop", "why": "booking WA"},
            workspace=self.tmpdir / "unused", principal_id=OWNER,
        )

        self.assertTrue(result.success)
        self.assertEqual(len(result.directions), 2)
        # Bounded/lightweight call only -- never a full build invocation.
        self.mock_adapter.frontend_build.assert_not_called()
        self.mock_adapter.frontend_propose_directions.assert_called_once()

        state = self.store.load("proj-a")
        self.assertEqual(state.design_directions, _TWO_DIRECTIONS["directions"])
        self.assertIsNone(state.selected_direction)

    def test_propose_rejects_out_of_bound_direction_count(self):
        self._owned_project("proj-b")
        self.mock_adapter.frontend_propose_directions.return_value = {
            "success": True,
            "directions": [_TWO_DIRECTIONS["directions"][0]],  # only 1 -- invalid
        }

        result = self.orchestrator.propose(
            "proj-b", brief={}, workspace=self.tmpdir / "unused", principal_id=OWNER,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INVALID_DIRECTION_COUNT")
        state = self.store.load("proj-b")
        self.assertEqual(state.design_directions, [])

    def test_propose_fails_closed_when_adapter_reports_failure(self):
        self._owned_project("proj-c")
        self.mock_adapter.frontend_propose_directions.return_value = {
            "success": False,
            "error": "FAST unavailable",
        }

        result = self.orchestrator.propose(
            "proj-c", brief={}, workspace=self.tmpdir / "unused", principal_id=OWNER,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "DIRECTIONS_PROPOSAL_FAILED")

    def test_propose_rejected_for_unauthorized_principal(self):
        self._owned_project("proj-d")
        result = self.orchestrator.propose(
            "proj-d", brief={}, workspace=self.tmpdir / "unused", principal_id=STRANGER,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "UNAUTHORIZED_ROLE")
        self.mock_adapter.frontend_propose_directions.assert_not_called()

    def test_propose_rejected_without_hermes_adapter(self):
        self._owned_project("proj-e")
        orchestrator = DirectionsOrchestrator(self.store, hermes_adapter=None)
        result = orchestrator.propose(
            "proj-e", brief={}, workspace=self.tmpdir / "unused", principal_id=OWNER,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "HERMES_ADAPTER_UNAVAILABLE")


class TestChooseDirection(DirectionsOrchestratorTestBase):
    def _proposed(self, project_id: str) -> None:
        self._owned_project(project_id)
        self.mock_adapter.frontend_propose_directions.return_value = _TWO_DIRECTIONS
        self.orchestrator.propose(
            project_id, brief={}, workspace=self.tmpdir / "unused", principal_id=OWNER,
        )

    def test_choose_persists_selection(self):
        self._proposed("proj-f")
        result = self.orchestrator.choose_direction("proj-f", 1, principal_id=OWNER)
        self.assertTrue(result.success)

        state = self.store.load("proj-f")
        self.assertEqual(state.selected_direction, _TWO_DIRECTIONS["directions"][1])

    def test_choose_rejects_out_of_range_index(self):
        self._proposed("proj-g")
        result = self.orchestrator.choose_direction("proj-g", 5, principal_id=OWNER)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INVALID_DIRECTION_INDEX")

    def test_choose_rejects_when_nothing_proposed(self):
        self._owned_project("proj-h")
        result = self.orchestrator.choose_direction("proj-h", 0, principal_id=OWNER)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "NO_DIRECTIONS_PROPOSED")

    def test_choose_rejected_for_unauthorized_principal(self):
        self._proposed("proj-i")
        result = self.orchestrator.choose_direction("proj-i", 0, principal_id=STRANGER)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "UNAUTHORIZED_ROLE")

        state = self.store.load("proj-i")
        self.assertIsNone(state.selected_direction)

    def test_choose_rejected_without_any_principal(self):
        self._proposed("proj-j")
        result = self.orchestrator.choose_direction("proj-j", 0)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "UNAUTHORIZED_ROLE")


class TestDelegationAndReferenceBypass(unittest.TestCase):
    """Phase 13 must never apply when design authority was delegated or
    references were attached -- Phase 7/12 behavior is unchanged."""

    def test_delegated_design_authority_detected(self):
        self.assertTrue(
            design_authority_delegated({"design_authority_delegated": True})
        )
        self.assertFalse(design_authority_delegated({}))
        self.assertFalse(design_authority_delegated({"design_authority_delegated": False}))

    def test_has_references_detected(self):
        self.assertTrue(has_references({"UX": {"item": "x", "evidence": "y"}}))
        self.assertFalse(has_references({}))
        self.assertFalse(has_references(None))

    def test_direction_build_instructions_none_when_no_selection(self):
        self.assertIsNone(direction_build_instructions(None))
        self.assertIsNone(direction_build_instructions({}))

    def test_direction_build_instructions_composed_when_selected(self):
        instructions = direction_build_instructions(_TWO_DIRECTIONS["directions"][0])
        self.assertIn("Minimal Editorial", instructions)
        self.assertIn("Clean, generous whitespace", instructions)
        self.assertIn("#111111", instructions)


class TestSingleBuildInvariant(unittest.TestCase):
    """Phase 13's contract: choosing a direction results in exactly ONE
    real FrontendBuilder.build() invocation using ONLY the selected
    direction -- never three full builds, and the existing delegated-design
    (Phase 7) path is completely unaffected when no direction was chosen."""

    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmpdir_obj.name)
        self.workspace_root = self.tmpdir / "workspaces"
        self.state_root = self.tmpdir / "state"
        self.store = ProjectStateStore(self.state_root)
        self.runner = ProjectRunner(self.workspace_root, self.store)
        self.mock_adapter = MagicMock()
        self.builder = FrontendBuilder(self.runner, self.store, hermes_adapter=self.mock_adapter)

        # Stub npm artifact creation like test_build.py's autouse fixture.
        original = ProjectRunner.create_workspace

        def create(runner, project_id):
            workspace = original(runner, project_id)
            (workspace / "dist").mkdir(exist_ok=True)
            (workspace / "dist" / "index.html").write_bytes(b"<html>fake build</html>")
            return workspace

        patcher = patch.object(ProjectRunner, "create_workspace", create)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    def _design_dna_response(self):
        return {
            "success": True,
            "design_dna": {
                "version": 1,
                "brand_personality": "minimal editorial",
                "palette": {"primary": "#111111"},
                "typography": {"heading_font": "Inter"},
                "spacing": {"density": "comfortable"},
                "page_inventory": ["home"],
                "layout": {"navigation": "top-bar"},
                "motion": {"enabled": True},
                "primary_cta": {"label": "Book Now", "destination": None},
                "assets": [],
                "verified_content": {"name": "Northcut", "what": "barbershop"},
                "unresolved_facts": ["cta_destination"],
            },
        }

    def test_build_after_choosing_direction_invokes_frontend_exactly_once_with_selection(self):
        project_id = "proj-single-build"
        self.store.transition_lifecycle(project_id, ProjectLifecycle.READY)

        orchestrator = DirectionsOrchestrator(self.store, hermes_adapter=self.mock_adapter)
        self.mock_adapter.frontend_propose_directions.return_value = _TWO_DIRECTIONS
        with self.store.acquire_writer(project_id) as state:
            state.roles["owner"] = OWNER
            self.store.save(state)

        propose_result = orchestrator.propose(
            project_id, brief={"name": "Northcut"}, workspace=self.tmpdir, principal_id=OWNER,
        )
        self.assertTrue(propose_result.success)

        choose_result = orchestrator.choose_direction(project_id, 0, principal_id=OWNER)
        self.assertTrue(choose_result.success)
        self.store.transition_lifecycle(project_id, ProjectLifecycle.QUEUED)

        self.mock_adapter.frontend_build.return_value = self._design_dna_response()
        brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}

        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = {
                "npm_ci": {"success": True},
                "npm_build": {"success": True},
                "npm_typecheck": {"success": True},
            }
            with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
                mock_qa = MagicMock()
                mock_qa.run.return_value = MagicMock(success=True, error=None)
                mock_qa_cls.return_value = mock_qa
                result = self.builder.build(project_id, brief)

        self.assertTrue(result.success)
        # Exactly ONE full build call -- never three.
        self.mock_adapter.frontend_build.assert_called_once()
        _, kwargs = self.mock_adapter.frontend_build.call_args
        instructions = kwargs.get("design_dna_instructions") or ""
        self.assertIn("Minimal Editorial", instructions)
        self.assertNotIn("Bold Modern", instructions)

    def test_build_unaffected_when_no_direction_selected(self):
        """Delegated-design (Phase 7) path: selected_direction stays None,
        so design_dna_instructions is None exactly as before this pass."""
        project_id = "proj-no-selection"
        _queue_project(self.store, project_id)

        self.mock_adapter.frontend_build.return_value = self._design_dna_response()
        brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA",
                  "design_authority_delegated": True}

        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = {
                "npm_ci": {"success": True},
                "npm_build": {"success": True},
                "npm_typecheck": {"success": True},
            }
            with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
                mock_qa = MagicMock()
                mock_qa.run.return_value = MagicMock(success=True, error=None)
                mock_qa_cls.return_value = mock_qa
                result = self.builder.build(project_id, brief)

        self.assertTrue(result.success)
        self.mock_adapter.frontend_build.assert_called_once()
        _, kwargs = self.mock_adapter.frontend_build.call_args
        # Phase 14 contact-form policy is always composed now (explicit
        # no-form instruction when nothing is configured); Phase 12/13
        # instructions remain absent for the delegated-design path.
        instructions = kwargs.get("design_dna_instructions") or ""
        self.assertIn("CONTACT FORM", instructions)
        self.assertNotIn("DESIGN REFERENCES", instructions)
        self.assertNotIn("SELECTED DESIGN DIRECTION", instructions)


if __name__ == "__main__":
    unittest.main()
