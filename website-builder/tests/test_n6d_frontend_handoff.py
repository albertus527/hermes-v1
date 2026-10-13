"""N6-D — FRONTEND receives PRD + pre-build DesignGuidance; design pack activated.

Proves the N6 requirements that:

* the FRONTEND guidance block carries the PRE-BUILD ``DesignGuidance.md``
  distinctly labelled from the POST-BUILD ``Design.md``;
* the bounded D2/D3a design context pack is built (activated) only in ACTIVE
  design mode, and a pack failure never blocks or changes a build;
* the digest folds in the pre-build guidance so QA can distinguish versions.

No live model, browser, network, or npm is required.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.decision_engine import DesignDecision  # noqa: E402
from app.core.decision_provider import DesignCandidate  # noqa: E402
from app.core.design_guidance import compile_design_guidance  # noqa: E402
from app.core.document_guidance import (  # noqa: E402
    build_document_guidance,
    guidance_digest,
    guidance_for_workspace,
    guidance_snapshot,
)
from app.core.document_store import (  # noqa: E402
    store_design_guidance,
    store_documents,
)
from app.core.requirement_creator import compile_design, compile_prd  # noqa: E402

BRIEF = {"name": "Northcut", "what": "barbershop", "why": "book a cut"}


def _candidate():
    return DesignCandidate(
        candidate_id="c1", label="Editorial", descriptor="editorial grid",
        palette=("#101014",), source_uri="viking://design/x", source_revision="r1",
        score=0.9,
    )


def _decision(selected):
    return DesignDecision(
        selected=selected, ranked=(selected,) if selected else (),
        abstained=selected is None, abstain_reason="" if selected else "none",
        source="provider", provider="openviking",
    )


class GuidanceBlockTests(unittest.TestCase):
    def test_prebuild_guidance_is_labelled_distinctly(self):
        g = compile_design_guidance(BRIEF, _decision(_candidate()),
                                    project_id="p", source_revision=1)
        block = build_document_guidance(None, None, g)
        self.assertIn("pre-build", block.lower())
        self.assertIn("application-decided", block)

    def test_post_build_design_is_labelled_distinctly(self):
        d = compile_design(
            {"brand_personality": "warm", "palette": {"primary": "#000"}},
            project_id="p", source_revision=1,
        )
        block = build_document_guidance(None, d, None)
        self.assertIn("post-build DNA", block)

    def test_both_can_coexist_and_are_distinguishable(self):
        g = compile_design_guidance(BRIEF, _decision(_candidate()),
                                    project_id="p", source_revision=1)
        d = compile_design(
            {"brand_personality": "warm", "palette": {"primary": "#000"}},
            project_id="p", source_revision=1,
        )
        block = build_document_guidance(None, d, g)
        self.assertIn("pre-build", block)
        self.assertIn("post-build DNA", block)

    def test_empty_when_no_documents(self):
        self.assertEqual(build_document_guidance(None, None, None), "")


class DigestTests(unittest.TestCase):
    def test_guidance_digest_distinguishes_prebuild_guidance(self):
        a = guidance_digest("prd", "design", "guidance-A")
        b = guidance_digest("prd", "design", "guidance-B")
        self.assertNotEqual(a, b)

    def test_guidance_digest_changes_with_guidance_only(self):
        base = guidance_digest("prd", "design", None)
        withg = guidance_digest("prd", "design", "guidance")
        self.assertNotEqual(base, withg)


class WorkspaceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_snapshot_includes_prebuild_guidance_digest(self):
        prd = compile_prd(BRIEF, project_id="p", source_revision=1)
        g = compile_design_guidance(BRIEF, _decision(_candidate()),
                                    project_id="p", source_revision=1)
        store_documents(self.ws, prd_text=prd)
        store_design_guidance(self.ws, g)
        snap = guidance_snapshot(self.ws)
        assert snap is not None
        self.assertIsNotNone(snap["prd_digest"])
        self.assertIsNotNone(snap["design_guidance_digest"])
        self.assertIsNone(snap["design_digest"])
        self.assertIsNotNone(snap["combined_digest"])

    def test_snapshot_none_when_no_documents(self):
        self.assertIsNone(guidance_snapshot(self.ws))

    def test_guidance_for_workspace_includes_prebuild_guidance(self):
        g = compile_design_guidance(BRIEF, _decision(_candidate()),
                                    project_id="p", source_revision=1)
        store_design_guidance(self.ws, g)
        block, digest = guidance_for_workspace(self.ws)
        assert block is not None and digest is not None
        self.assertIn("pre-build", block.lower())
        self.assertIn("viking://design/x@r1", block)


class PackActivationTests(unittest.TestCase):
    """The design context pack is built only in ACTIVE design mode."""

    def _builder(self, *, design_mode, hermes_home):
        from app.core.decision_modes import DecisionEngineConfig
        from app.core.decision_engine import DecisionEngine
        from app.core.decision_provider import DecisionProviderRegistry
        from app.core.requirement_creator import RequirementCreatorConfig
        from app.projects.build import FrontendBuilder
        from app.sandbox.runner import ProjectRunner
        from app.core.state import ProjectStateStore
        from unittest.mock import MagicMock

        class _Provider:
            @property
            def name(self):
                return "test"

            def classify_intake(self, *a, **k):
                raise AssertionError

            def propose_design_candidates(self, context, *, limit):
                return (_candidate(),)

        registry = DecisionProviderRegistry()
        registry.register(_Provider())
        engine = DecisionEngine(registry, provider_name="test")
        store = ProjectStateStore(self.state_root)
        runner = ProjectRunner(self.workspace_root, store)
        adapter = MagicMock()
        adapter.hermes_home = hermes_home
        builder = FrontendBuilder(
            runner, store, hermes_adapter=adapter,
            requirement_creator_config=RequirementCreatorConfig(enabled=True),
            decision_engine=engine,
            decision_engine_config=DecisionEngineConfig(design_mode=design_mode),
        )
        return builder

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.workspace_root = root / "ws"
        self.state_root = root / "state"
        self.hermes_home = root / "hermes"
        self.hermes_home.mkdir(parents=True, exist_ok=True)
        # A real project so source_revision >= 1 (the document contract requires
        # a positive revision).
        from app.core.state import ProjectState, ProjectStateStore
        store = ProjectStateStore(self.state_root)
        st = ProjectState(project_id="p")
        st.revisions.source_revision = 1
        store.save(st)

    def tearDown(self):
        self._tmp.cleanup()

    def test_disabled_mode_builds_no_pack(self):
        from app.core.decision_modes import DecisionEngineMode

        builder = self._builder(design_mode=DecisionEngineMode.DISABLED,
                                hermes_home=self.hermes_home)
        text, pack = builder._prepare_prebuild_design("p", Path(self.workspace_root), BRIEF)
        self.assertIsNone(text)
        self.assertIsNone(pack)

    def test_shadow_mode_builds_no_pack(self):
        from app.core.decision_modes import DecisionEngineMode

        builder = self._builder(design_mode=DecisionEngineMode.SHADOW,
                                hermes_home=self.hermes_home)
        text, pack = builder._prepare_prebuild_design("p", Path(self.workspace_root), BRIEF)
        self.assertIsNone(text)
        self.assertIsNone(pack)

    def test_active_mode_builds_a_pack(self):
        from app.core.decision_modes import DecisionEngineMode

        builder = self._builder(design_mode=DecisionEngineMode.ACTIVE,
                                hermes_home=self.hermes_home)
        text, pack = builder._prepare_prebuild_design("p", Path(self.workspace_root), BRIEF)
        self.assertIsNotNone(text)
        self.assertIsNotNone(pack)
        # The pack is the real D2/D3a object and serialises with a version.
        self.assertTrue(hasattr(pack, "to_dict"))

    def test_missing_hermes_home_degrades_pack_to_none(self):
        from app.core.decision_modes import DecisionEngineMode

        builder = self._builder(design_mode=DecisionEngineMode.ACTIVE,
                                hermes_home=None)
        text, pack = builder._prepare_prebuild_design("p", Path(self.workspace_root), BRIEF)
        # Guidance still compiles; the pack is simply absent (an enhancement).
        self.assertIsNotNone(text)
        self.assertIsNone(pack)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
