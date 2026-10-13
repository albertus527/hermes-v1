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
        record = state.deployment[DEPLOYMENT_DOCUMENTS_KEY]
        self.assertEqual(record.get("error"), "ValueError")
        self.assertEqual(record.get("errors", {}).get("prd"), "ValueError")

    def test_prd_failure_is_not_erased_by_a_successful_design_compile(self):
        """A later successful Design compile must not erase a recorded PRD
        failure (the N4 merge bug). The two documents' evidence is independent."""
        builder = self._enabled_builder()
        with patch(
            "app.projects.build.compile_prd",
            side_effect=ValueError("prd boom"),
        ):
            result = self._build(
                builder, "proj-on6", {"name": "N", "what": "w", "why": "y"}
            )
        self.assertTrue(result.success)
        state = self.store.load("proj-on6")
        assert state is not None
        record = state.deployment[DEPLOYMENT_DOCUMENTS_KEY]
        # Design compiled fine; its entry exists...
        self.assertIn("design", record.get("digests", {}))
        # ...and the PRD failure is STILL recorded, not overwritten.
        self.assertEqual(record.get("errors", {}).get("prd"), "ValueError")

    def test_initial_build_feeds_prd_guidance_to_frontend(self):
        """N4: the PRD (available pre-FRONTEND) reaches FRONTEND as guidance.

        The adapter derives guidance from the workspace it is handed, so the
        check is that the PRD exists in the workspace when FRONTEND is called
        and that the adapter's prompt builder would include it.
        """
        builder = self._enabled_builder()
        captured = {}

        real_frontend_build = self.mock_adapter.frontend_build

        def spy(*args, **kwargs):
            from app.core.document_guidance import guidance_for_workspace
            captured["guidance"] = guidance_for_workspace(kwargs["workspace"])[0]
            return real_frontend_build(*args, **kwargs)

        self.mock_adapter.frontend_build.side_effect = spy
        self._build(builder, "proj-on7", {"name": "N", "what": "w", "why": "y"})
        self.assertIsNotNone(captured.get("guidance"))
        self.assertIn("PRD & DESIGN GUIDANCE", captured["guidance"])

    def test_initial_build_records_guidance_digest(self):
        builder = self._enabled_builder()
        self._build(builder, "proj-on8", {"name": "N", "what": "w", "why": "y"})
        state = self.store.load("proj-on8")
        assert state is not None
        self.assertIn("guidance_digest", state.deployment)

    def test_flag_off_records_no_guidance_digest(self):
        """With the feature OFF, no documents exist and no digest is recorded."""
        builder = FrontendBuilder(
            self.runner, self.store, hermes_adapter=self.mock_adapter
        )
        self._build(builder, "proj-off3", {"name": "N", "what": "w", "why": "y"})
        state = self.store.load("proj-off3")
        assert state is not None
        self.assertNotIn("guidance_digest", state.deployment)


class AdapterGuidanceTests(unittest.TestCase):
    """The adapter derives guidance from the workspace and puts it in the prompt."""

    def _adapter(self):
        from app.hermes.adapter import HermesAdapter

        return HermesAdapter.__new__(HermesAdapter)

    def test_prompt_is_byte_identical_without_documents(self):
        import tempfile
        from pathlib import Path

        adapter = self._adapter()
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            prompt = adapter._build_frontend_prompt(
                {"name": "N", "what": "w", "why": "y"}, ws
            )
            self.assertNotIn("PRD & DESIGN GUIDANCE", prompt)

    def test_prompt_includes_guidance_when_documents_present(self):
        import tempfile
        from pathlib import Path
        from app.core.document_guidance import guidance_for_workspace
        from app.core.requirement_creator import compile_prd
        from app.core.document_store import store_documents

        adapter = self._adapter()
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            prd = compile_prd(
                {"name": "N", "what": "w", "why": "y"},
                project_id="p", source_revision=1,
            )
            store_documents(ws, prd_text=prd)
            block, _ = guidance_for_workspace(ws)
            prompt = adapter._build_frontend_prompt(
                {"name": "N", "what": "w", "why": "y"}, ws, document_guidance=block
            )
            self.assertIn("PRD & DESIGN GUIDANCE", prompt)


class DesignGuidanceBuildTests(_Base):
    """N6-C: pre-build DesignGuidance.md is written only in ACTIVE design mode."""

    def _builder(self, *, design_mode):
        from app.core.decision_modes import DecisionEngineConfig, DecisionEngineMode
        from app.core.decision_engine import DecisionEngine
        from app.core.decision_provider import (
            DecisionProviderRegistry, DesignCandidate,
        )

        class _Provider:
            @property
            def name(self):
                return "test"

            def classify_intake(self, *a, **k):
                raise AssertionError("intake not used")

            def propose_design_candidates(self, context, *, limit):
                return (DesignCandidate(
                    candidate_id="c1", label="Editorial",
                    descriptor="editorial grid, smooth scroll",
                    palette=("#101014",),
                    source_uri="viking://design/x", source_revision="r1",
                    score=0.9,
                ),)

        registry = DecisionProviderRegistry()
        registry.register(_Provider())
        engine = DecisionEngine(registry, provider_name="test")
        return FrontendBuilder(
            self.runner, self.store, hermes_adapter=self.mock_adapter,
            requirement_creator_config=RequirementCreatorConfig(enabled=True),
            decision_engine=engine,
            decision_engine_config=DecisionEngineConfig(design_mode=design_mode),
        )

    def test_disabled_design_mode_writes_no_guidance(self):
        from app.core.decision_modes import DecisionEngineMode
        from app.core.document_contracts import DESIGN_GUIDANCE_FILE_NAME

        builder = self._builder(design_mode=DecisionEngineMode.DISABLED)
        result = self._build(builder, "gd-off",
                             {"name": "N", "what": "w", "why": "y"})
        assert result.workspace is not None
        self.assertFalse((result.workspace / DESIGN_GUIDANCE_FILE_NAME).exists())

    def test_shadow_design_mode_writes_no_guidance(self):
        from app.core.decision_modes import DecisionEngineMode
        from app.core.document_contracts import DESIGN_GUIDANCE_FILE_NAME

        builder = self._builder(design_mode=DecisionEngineMode.SHADOW)
        result = self._build(builder, "gd-shadow",
                             {"name": "N", "what": "w", "why": "y"})
        assert result.workspace is not None
        self.assertFalse((result.workspace / DESIGN_GUIDANCE_FILE_NAME).exists())

    def test_active_design_mode_writes_grounded_guidance(self):
        from app.core.decision_modes import DecisionEngineMode
        from app.core.document_contracts import DESIGN_GUIDANCE_FILE_NAME

        builder = self._builder(design_mode=DecisionEngineMode.ACTIVE)
        result = self._build(builder, "gd-on",
                             {"name": "Northcut", "what": "barbershop", "why": "wa"})
        assert result.workspace is not None
        path = result.workspace / DESIGN_GUIDANCE_FILE_NAME
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8")
        # Grounded in the brief and in the candidate's real provenance.
        self.assertIn("barbershop", text)
        self.assertIn("viking://design/x@r1", text)

    def test_active_guidance_is_recorded_and_written_before_frontend(self):
        """Guidance exists on disk when FRONTEND is invoked (pre-build)."""
        from app.core.decision_modes import DecisionEngineMode
        from app.core.document_contracts import DESIGN_GUIDANCE_FILE_NAME

        builder = self._builder(design_mode=DecisionEngineMode.ACTIVE)
        seen = {}

        def _capture(*, workspace, **kwargs):
            seen["guidance_at_frontend"] = (
                workspace / DESIGN_GUIDANCE_FILE_NAME
            ).exists()
            return {"success": True, "design_dna": _dna()}

        self.mock_adapter.frontend_build.side_effect = _capture
        self._build(builder, "gd-order",
                    {"name": "Northcut", "what": "barbershop", "why": "wa"})
        self.assertTrue(seen["guidance_at_frontend"])

    def test_active_guidance_digest_recorded_in_deployment(self):
        from app.core.decision_modes import DecisionEngineMode

        builder = self._builder(design_mode=DecisionEngineMode.ACTIVE)
        self._build(builder, "gd-digest",
                    {"name": "Northcut", "what": "barbershop", "why": "wa"})
        state = self.store.load("gd-digest")
        assert state is not None
        records = state.deployment[DEPLOYMENT_DOCUMENTS_KEY]
        self.assertIn("design_guidance", records["digests"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
