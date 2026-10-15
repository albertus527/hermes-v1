"""N6-E — the build boundary invalidates a stale post-build ``Design.md``.

The defect this pins (live incident, Forma Atelier): a workspace is REUSED
across builds, and ``Design.md`` is compiled AFTER FRONTEND. On a rebuild the
previous build's ``Design.md`` is still on disk at the FRONTEND invocation, so
it is recorded as a USED guidance input; the post-FRONTEND recompile then
changes its digest and QA fails closed with a false
``GUIDANCE_VERSION_MISMATCH``.

The fix invalidates ONLY that stale ``Design.md`` at the build boundary, before
the FRONTEND guidance snapshot is captured. These tests drive the REAL
``FrontendBuilder.build`` path (FRONTEND and the cheap checks are stubbed, as the
existing Phase-7 orchestration tests do) and prove:

* a FRESH build (no Design.md) still reaches parity — unchanged;
* a REBUILD of a reused workspace now reaches parity;
* the stale ``Design.md`` is gone at the FRONTEND call and recorded ABSENT;
* PRD.md / DesignGuidance.md / design-dna.json / src / other artefacts survive;
* a PRD or DesignGuidance change AFTER the invocation still FAILS parity;
* an in-flight change to a document that WAS used still FAILS parity;
* a symlinked Design.md is refused (never followed), so nothing outside the
  workspace is touched;
* a second, repeated rebuild also passes (the fix is idempotent).

No live model, browser, network, or npm is required.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_guidance import compile_design_guidance  # noqa: E402
from app.core.document_contracts import (  # noqa: E402
    DESIGN_FILE_NAME,
    DESIGN_GUIDANCE_FILE_NAME,
    PRD_FILE_NAME,
)
from app.core.document_store import (  # noqa: E402
    invalidate_design,
    read_document,
    store_design_guidance,
    store_documents,
)
from app.core.guidance_snapshot import (  # noqa: E402
    latest_invocation,
    record_invocation,
    verify_invocation,
)
from app.core.lifecycle import ProjectLifecycle  # noqa: E402
from app.core.requirement_creator import (  # noqa: E402
    RequirementCreatorConfig,
    compile_design,
    compile_prd,
)
from app.core.state import ProjectStateStore  # noqa: E402
from app.projects.build import FrontendBuilder  # noqa: E402
from app.sandbox.runner import ProjectRunner  # noqa: E402

FORMA_BRIEF = {
    "name": "Forma Atelier",
    "what": "Landing page untuk studio arsitektur",
    "why": (
        "Menampilkan studio secara profesional: hero, showcase 3 proyek "
        "arsitektur, tentang studio, layanan, dan kontak"
    ),
}

DNA = {
    "version": 1,
    "brand_personality": "editorial",
    "palette": {"primary": "#101014", "accent": "#a8674a"},
    "typography": {"heading_font": "Fraunces", "body_font": "Inter"},
    "spacing": {"density": "comfortable"},
    "page_inventory": ["home"],
    "layout": {"navigation": "top-bar"},
    "motion": {"enabled": True},
    "primary_cta": {"label": "Hubungi", "destination": None},
    "assets": [],
    "verified_content": {"name": "Forma Atelier"},
    "unresolved_facts": [],
}


def _passing_checks() -> dict:
    return {
        "npm_ci": {"success": True},
        "npm_build": {"success": True},
        "npm_typecheck": {"success": True},
    }


@pytest.fixture(autouse=True)
def _fake_artifact(monkeypatch):
    """Give every workspace the ``dist/index.html`` the build path expects."""
    original = ProjectRunner.create_workspace

    def create(runner, project_id):
        ws = original(runner, project_id)
        (ws / "dist").mkdir(exist_ok=True)
        (ws / "dist" / "index.html").write_bytes(b"<html>fake</html>")
        return ws

    monkeypatch.setattr(ProjectRunner, "create_workspace", create)


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.workspace_root = root / "workspaces"
        self.store = ProjectStateStore(root / "state")
        self.runner = ProjectRunner(self.workspace_root, self.store)
        self.adapter = MagicMock()
        self.adapter.frontend_build.side_effect = self._fake_frontend

    def tearDown(self):
        self._tmp.cleanup()

    def _fake_frontend(self, *, project_id, workspace, build_operation_id=None, **kwargs):
        """Mimic the REAL adapter's FRONTEND choke point.

        The real ``HermesAdapter.frontend_build`` records the guidance snapshot
        immediately before the model runs (N6-E). A bare ``MagicMock`` would skip
        that, hiding the very mechanism under test, so the stub reproduces it
        faithfully — using the same public ``record_invocation`` the adapter uses.
        """
        record_invocation(
            self.store, project_id, workspace,
            invocation_id=(
                f"{project_id}:r{build_operation_id}"
                if build_operation_id is not None else f"{project_id}:adhoc"
            ),
        )
        return {"success": True, "design_dna": DNA}

    def _builder(self):
        return FrontendBuilder(
            self.runner,
            self.store,
            hermes_adapter=self.adapter,
            requirement_creator_config=RequirementCreatorConfig(enabled=True),
        )

    def _queue(self, project_id: str):
        """Drive the project to QUEUED via legal edges only (idempotent).

        The live Forma Atelier "rebuild" was an initial-build FAILURE RECOVERY
        re-entry (intake.py): FAILED -> READY with ``source_revision`` reset to 0
        and ``failure`` cleared, under the canonical (READY, revision 0) contract
        ``prebuild_error`` requires. We reproduce that exact reset here instead of
        bypassing the lifecycle — the state machine is never weakened.
        """
        state = self.store.load(project_id)
        current = state.lifecycle if state else ProjectLifecycle.DISCOVERING.value
        if current == ProjectLifecycle.RUNNING.value:
            self.store.transition_lifecycle(project_id, ProjectLifecycle.FAILED)
            current = ProjectLifecycle.FAILED.value
        if current == ProjectLifecycle.FAILED.value:
            # Initial-build recovery reset (mirrors app/core/intake.py).
            with self.store.acquire_writer(project_id) as st:
                st.revisions.source_revision = 0
                st.failure = None
                self.store.save(st)
            self.store.transition_lifecycle(project_id, ProjectLifecycle.READY)
        elif current != ProjectLifecycle.READY.value:
            self.store.transition_lifecycle(project_id, ProjectLifecycle.READY)
        self.store.transition_lifecycle(project_id, ProjectLifecycle.QUEUED)

    def _run_build(self, builder, project_id: str):
        self._queue(project_id)
        with patch.object(builder, "_run_fixed_checks", return_value=_passing_checks()):
            with patch("app.projects.build.QAOrchestrator") as qa_cls:
                qa = MagicMock()
                qa.run.return_value = MagicMock(success=True, error=None)
                qa_cls.return_value = qa
                return builder.build(project_id, FORMA_BRIEF)

    def _seed_stale_design(self, workspace: Path, revision: int = 1):
        """Write a PREVIOUS build's Design.md into the reused workspace."""
        design = compile_design(
            {"brand_personality": "old", "palette": {"accent": "#000000"},
             "typography": {}, "layout": {}, "version": revision},
            project_id="proj", source_revision=revision,
            design_dna_version=revision,
        )
        store_documents(workspace, design_text=design)
        return design


# ---------------------------------------------------------------------------
# The fix, on the REAL build path
# ---------------------------------------------------------------------------

class StaleDesignInvalidatedOnRebuildTests(_Base):
    def test_rebuild_removes_stale_design_before_frontend(self):
        """On a reused workspace the stale Design.md must be gone at FRONTEND."""
        builder = self._builder()
        pid = "proj-rebuild"
        ws = self.runner.resolve_workspace(pid)
        ws.mkdir(parents=True, exist_ok=True)
        self._seed_stale_design(ws)

        seen = {}

        def _capture(*, project_id, workspace, build_operation_id=None, **kwargs):
            seen["design_at_frontend"] = (workspace / DESIGN_FILE_NAME).exists()
            seen["guidance"] = read_document(workspace, "design")
            return self._fake_frontend(
                project_id=project_id, workspace=workspace,
                build_operation_id=build_operation_id, **kwargs,
            )

        self.adapter.frontend_build.side_effect = _capture
        result = self._run_build(builder, pid)
        self.assertTrue(result.success)
        self.assertFalse(
            seen["design_at_frontend"],
            "stale Design.md must be invalidated BEFORE the FRONTEND invocation",
        )
        self.assertIsNone(seen["guidance"], "no stale design guidance is handed to FRONTEND")

    def test_rebuild_reaches_guidance_parity(self):
        """The recorded snapshot must verify at QA time (parity PASS)."""
        builder = self._builder()
        pid = "proj-parity"
        ws = self.runner.resolve_workspace(pid)
        ws.mkdir(parents=True, exist_ok=True)
        self._seed_stale_design(ws)

        result = self._run_build(builder, pid)
        self.assertTrue(result.success)

        state = self.store.load(pid)
        assert state is not None
        recorded = latest_invocation(state)
        self.assertIsNotNone(recorded)
        # The stale design was absent at invocation => recorded None.
        self.assertIsNone(recorded["design_digest"])
        # And the workspace NOW holds the freshly compiled Design.md, which is
        # the expected later appearance — parity holds.
        self.assertIsNotNone(read_document(ws, "design"))
        self.assertIsNone(verify_invocation(ws, recorded))

    def test_fresh_build_still_reaches_parity(self):
        """A fresh workspace (no Design.md) is unchanged: parity passes."""
        builder = self._builder()
        pid = "proj-fresh"
        result = self._run_build(builder, pid)
        self.assertTrue(result.success)
        state = self.store.load(pid)
        assert state is not None
        recorded = latest_invocation(state)
        self.assertIsNotNone(recorded)
        self.assertIsNone(recorded["design_digest"])
        self.assertIsNone(verify_invocation(result.workspace, recorded))

    def test_repeated_rebuild_stays_parity_green(self):
        """A second rebuild is idempotent: still no stale design at FRONTEND."""
        builder = self._builder()
        pid = "proj-twice"
        ws = self.runner.resolve_workspace(pid)
        ws.mkdir(parents=True, exist_ok=True)
        self._seed_stale_design(ws)

        first = self._run_build(builder, pid)
        self.assertTrue(first.success)
        # Second build reuses the workspace that now holds build-1's Design.md.
        second = self._run_build(builder, pid)
        self.assertTrue(second.success)

        state = self.store.load(pid)
        assert state is not None
        recorded = latest_invocation(state)
        self.assertIsNone(recorded["design_digest"])
        self.assertIsNone(verify_invocation(second.workspace, recorded))


# ---------------------------------------------------------------------------
# Non-guidance artefacts survive the invalidation
# ---------------------------------------------------------------------------

class ArtefactsPreservedTests(_Base):
    def test_prd_guidance_dna_and_source_survive(self):
        builder = self._builder()
        pid = "proj-artefacts"
        ws = self.runner.resolve_workspace(pid)
        ws.mkdir(parents=True, exist_ok=True)
        self._seed_stale_design(ws)
        # Pre-existing, unrelated artefacts that MUST survive: a PRD, a
        # DesignGuidance.md, design-dna.json and src/.
        store_documents(ws, prd_text=compile_prd(
            FORMA_BRIEF, project_id=pid, source_revision=1))
        store_design_guidance(ws, compile_design_guidance(
            FORMA_BRIEF, None, project_id=pid, source_revision=1))
        (ws / "design-dna.json").write_text('{"version": 1}', encoding="utf-8")
        (ws / "src").mkdir(exist_ok=True)
        (ws / "src" / "App.tsx").write_text("export default function App(){}", encoding="utf-8")

        result = self._run_build(builder, pid)
        self.assertTrue(result.success)
        self.assertTrue((ws / "design-dna.json").exists(), "design-dna.json must survive")
        self.assertTrue((ws / "src" / "App.tsx").exists(), "src/ must survive")
        # The PRD + DesignGuidance are recompiled pre-FRONTEND and stay.
        self.assertIsNotNone(read_document(ws, "prd"))
        self.assertIsNotNone(read_document(ws, "design_guidance"))
        # And the fresh post-FRONTEND Design.md is present again.
        self.assertIsNotNone(read_document(ws, "design"))

    def test_only_design_is_removed_not_the_other_documents(self):
        """``invalidate_design`` touches exactly one file."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            store_documents(
                ws,
                prd_text=compile_prd(FORMA_BRIEF, project_id="p", source_revision=1),
                design_text=compile_design(
                    {"brand_personality": "x", "palette": {}, "typography": {},
                     "layout": {}, "version": 1},
                    project_id="p", source_revision=1, design_dna_version=1,
                ),
            )
            store_design_guidance(ws, compile_design_guidance(
                FORMA_BRIEF, None, project_id="p", source_revision=1))
            self.assertTrue(invalidate_design(ws))
            self.assertFalse((ws / DESIGN_FILE_NAME).exists())
            self.assertTrue((ws / PRD_FILE_NAME).exists())
            self.assertTrue((ws / DESIGN_GUIDANCE_FILE_NAME).exists())

    def test_invalidate_is_a_noop_when_design_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(invalidate_design(Path(tmp)))

    def test_symlinked_design_is_refused_and_outside_target_untouched(self):
        """A symlinked Design.md is never followed or unlinked."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            ws.mkdir()
            outside = root / "outside.md"
            outside.write_text("secret", encoding="utf-8")
            (ws / DESIGN_FILE_NAME).symlink_to(outside)
            with self.assertRaises(ValueError):
                invalidate_design(ws)
            self.assertTrue(outside.exists(), "the symlink target must be untouched")
            self.assertEqual(outside.read_text(encoding="utf-8"), "secret")


# ---------------------------------------------------------------------------
# Fail-closed: real mismatches must STILL fail
# ---------------------------------------------------------------------------

class GenuineMismatchStillFailsTests(_Base):
    def test_prd_change_after_invocation_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            store = ProjectStateStore(Path(tmp) / "state")
            from app.core.state import ProjectState
            st = ProjectState(project_id="p")
            st.revisions.source_revision = 1
            store.save(st)
            store_documents(ws, prd_text=compile_prd(
                FORMA_BRIEF, project_id="p", source_revision=1))
            rec = record_invocation(store, "p", ws, invocation_id="p:r1")
            self.assertIsNotNone(rec["prd_digest"])
            # PRD changes AFTER the invocation -> true mismatch.
            store_documents(ws, prd_text=compile_prd(
                {"name": "Different", "what": "other", "why": "changed"},
                project_id="p", source_revision=1))
            reason = verify_invocation(ws, rec)
            self.assertIsNotNone(reason)
            self.assertIn("GUIDANCE_VERSION_MISMATCH", reason)

    def test_design_guidance_change_after_invocation_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            store = ProjectStateStore(Path(tmp) / "state")
            from app.core.state import ProjectState
            st = ProjectState(project_id="p")
            st.revisions.source_revision = 1
            store.save(st)
            store_design_guidance(ws, compile_design_guidance(
                FORMA_BRIEF, None, project_id="p", source_revision=1))
            rec = record_invocation(store, "p", ws, invocation_id="p:r1")
            self.assertIsNotNone(rec["design_guidance_digest"])
            store_design_guidance(ws, compile_design_guidance(
                {"name": "Other", "what": "x", "why": "y"},
                None, project_id="p", source_revision=1))
            reason = verify_invocation(ws, rec)
            self.assertIsNotNone(reason)
            self.assertIn("GUIDANCE_VERSION_MISMATCH", reason)

    def test_used_design_removed_after_invocation_fails(self):
        """A Design.md that WAS used and is then removed is a mismatch."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            store = ProjectStateStore(Path(tmp) / "state")
            from app.core.state import ProjectState
            st = ProjectState(project_id="p")
            st.revisions.source_revision = 1
            store.save(st)
            store_documents(ws, design_text=compile_design(
                {"brand_personality": "x", "palette": {}, "typography": {},
                 "layout": {}, "version": 1},
                project_id="p", source_revision=1, design_dna_version=1,
            ))
            rec = record_invocation(store, "p", ws, invocation_id="p:r1")
            self.assertIsNotNone(rec["design_digest"])
            (ws / DESIGN_FILE_NAME).unlink()
            reason = verify_invocation(ws, rec)
            self.assertIsNotNone(reason)
            self.assertIn("GUIDANCE_VERSION_MISMATCH", reason)

    def test_absent_at_invocation_later_appearance_is_parity(self):
        """The normal flow: absent Design.md appears later — NOT a mismatch."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            store = ProjectStateStore(Path(tmp) / "state")
            from app.core.state import ProjectState
            st = ProjectState(project_id="p")
            st.revisions.source_revision = 1
            store.save(st)
            store_documents(ws, prd_text=compile_prd(
                FORMA_BRIEF, project_id="p", source_revision=1))
            rec = record_invocation(store, "p", ws, invocation_id="p:r1")
            self.assertIsNone(rec["design_digest"])
            store_documents(ws, design_text=compile_design(
                {"brand_personality": "x", "palette": {}, "typography": {},
                 "layout": {}, "version": 1},
                project_id="p", source_revision=1, design_dna_version=1,
            ))
            self.assertIsNone(verify_invocation(ws, rec))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
