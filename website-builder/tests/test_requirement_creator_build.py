"""N2 — Requirement Creator integration into the Phase-7 build path.

Proves the two integration properties the roadmap requires:

* **Feature-flagged, disabled by default** — with the flag OFF the workspace
  gains no document and the deployment mapping is byte-identical to pre-N2.
* **Same release commit** — with the flag ON, ``PRD.md`` and ``Design.md`` are
  written into the workspace BEFORE the checked source snapshot is taken, so
  they are part of the checked bytes the release publishes.

No live LLM, browser, or npm is required (FRONTEND and cheap checks are stubbed,
exactly as the existing Phase-7 orchestration tests do).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore
from app.core.requirement_creator import RequirementCreatorConfig
from app.core.document_contracts import DESIGN_FILE_NAME, PRD_FILE_NAME
from app.core.document_store import DEPLOYMENT_DOCUMENTS_KEY
from app.projects.build import FrontendBuilder
from app.sandbox.runner import ProjectRunner


@pytest.fixture(autouse=True)
def fake_build_artifact(monkeypatch):
    original = ProjectRunner.create_workspace

    def create(runner, project_id):
        workspace = original(runner, project_id)
        (workspace / "dist").mkdir(exist_ok=True)
        (workspace / "dist" / "index.html").write_bytes(b"<html>fake</html>")
        return workspace

    monkeypatch.setattr(ProjectRunner, "create_workspace", create)


def _dna() -> dict:
    return {
        "version": 1,
        "brand_personality": "premium, minimalist",
        "palette": {"primary": "#000000"},
        "typography": {"heading_font": "Inter"},
        "spacing": {"density": "comfortable"},
        "page_inventory": ["home"],
        "layout": {"navigation": "top-bar"},
        "motion": {"enabled": True},
        "primary_cta": {"label": "Book Now", "destination": None},
        "assets": [],
        "verified_content": {"name": "Northcut", "what": "barbershop"},
        "unresolved_facts": ["cta_destination"],
    }


def _passing_checks() -> dict:
    return {
        "npm_ci": {"success": True},
        "npm_build": {"success": True},
        "npm_typecheck": {"success": True},
    }


def _queue(store: ProjectStateStore, project_id: str) -> None:
    store.transition_lifecycle(project_id, ProjectLifecycle.READY)
    store.transition_lifecycle(project_id, ProjectLifecycle.QUEUED)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.workspace_root = Path(self.tmpdir) / "workspaces"
        self.state_root = Path(self.tmpdir) / "state"
        self.store = ProjectStateStore(self.state_root)
        self.runner = ProjectRunner(self.workspace_root, self.store)
        self.mock_adapter = MagicMock()
        self.mock_adapter.frontend_build.return_value = {
            "success": True,
            "design_dna": _dna(),
        }

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _build(self, builder, project_id, brief):
        _queue(self.store, project_id)
        with patch.object(builder, "_run_fixed_checks", return_value=_passing_checks()):
            with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
                mock_qa = MagicMock()
                mock_qa.run.return_value = MagicMock(success=True, error=None)
                mock_qa_cls.return_value = mock_qa
                return builder.build(project_id, brief)


class DisabledByDefaultTests(_Base):
    def test_flag_off_writes_no_documents(self):
        builder = FrontendBuilder(
            self.runner, self.store, hermes_adapter=self.mock_adapter
        )
        result = self._build(
            builder, "proj-off", {"name": "Northcut", "what": "barbershop", "why": "wa"}
        )
        self.assertTrue(result.success)
        workspace = result.workspace
        assert workspace is not None
        self.assertFalse((workspace / PRD_FILE_NAME).exists())
        self.assertFalse((workspace / DESIGN_FILE_NAME).exists())

    def test_flag_off_leaves_deployment_key_absent(self):
        builder = FrontendBuilder(
            self.runner, self.store, hermes_adapter=self.mock_adapter
        )
        self._build(builder, "proj-off2", {"name": "N", "what": "w", "why": "y"})
        state = self.store.load("proj-off2")
        assert state is not None
        self.assertNotIn(DEPLOYMENT_DOCUMENTS_KEY, state.deployment)


class EnabledTests(_Base):
    def _enabled_builder(self):
        return FrontendBuilder(
            self.runner,
            self.store,
            hermes_adapter=self.mock_adapter,
            requirement_creator_config=RequirementCreatorConfig(enabled=True),
        )

    def test_flag_on_writes_both_documents(self):
        builder = self._enabled_builder()
        result = self._build(
            builder, "proj-on", {"name": "Northcut", "what": "barbershop", "why": "wa"}
        )
        self.assertTrue(result.success)
        workspace = result.workspace
        assert workspace is not None
        self.assertTrue((workspace / PRD_FILE_NAME).exists())
        self.assertTrue((workspace / DESIGN_FILE_NAME).exists())

    def test_flag_on_records_digests_in_deployment(self):
        builder = self._enabled_builder()
        self._build(builder, "proj-on2", {"name": "N", "what": "w", "why": "y"})
        state = self.store.load("proj-on2")
        assert state is not None
        self.assertIn(DEPLOYMENT_DOCUMENTS_KEY, state.deployment)
        records = state.deployment[DEPLOYMENT_DOCUMENTS_KEY]
        self.assertEqual(set(records["digests"]), {"prd", "design"})

    def test_documents_are_inside_the_checked_source_snapshot(self):
        """The documents must be part of the bytes the release publishes."""
        from app.deploy.snapshot import source_fingerprint

        builder = self._enabled_builder()
        result = self._build(
            builder, "proj-on3", {"name": "N", "what": "w", "why": "y"}
        )
        workspace = result.workspace
        assert workspace is not None
        # The checked binding recorded by the build must match a fingerprint
        # that INCLUDES the documents.
        state = self.store.load("proj-on3")
        assert state is not None
        checked = state.deployment.get("checked")
        self.assertIsNotNone(checked)
        # Recompute the fingerprint with the documents present and confirm it
        # equals what was checked.
        self.assertEqual(checked["source_sha256"], source_fingerprint(workspace))

    def test_documents_record_source_revision(self):
        builder = self._enabled_builder()
        self._build(builder, "proj-on4", {"name": "N", "what": "w", "why": "y"})
        state = self.store.load("proj-on4")
        assert state is not None
        records = state.deployment[DEPLOYMENT_DOCUMENTS_KEY]
        for entry in records["entries"]:
            self.assertEqual(entry["source_revision"], state.revisions.source_revision)

    def test_compilation_failure_does_not_fail_the_build(self):
        """Documents are additive: a compile error must not lose the website."""
        builder = self._enabled_builder()
        with patch(
            "app.projects.build.compile_prd",
            side_effect=ValueError("boom"),
        ):
            result = self._build(
                builder, "proj-on5", {"name": "N", "what": "w", "why": "y"}
            )
        self.assertTrue(result.success)
        state = self.store.load("proj-on5")
        assert state is not None
        # The failure is recorded for an operator, not silently dropped.
        self.assertEqual(
            state.deployment[DEPLOYMENT_DOCUMENTS_KEY].get("error"), "ValueError"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
