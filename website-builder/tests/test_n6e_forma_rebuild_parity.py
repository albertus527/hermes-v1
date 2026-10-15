"""N6-D/N6-E verification for the Forma Atelier E2E — controlled fixtures.

Two properties, both independent of any live model/browser/network:

**V3 — non-empty DesignContextPack for the Forma Atelier brief.** The pack for
the real brief, with the authoritative capability report present, must carry the
guidance resource (``resources>0``) rather than degrade to an empty pack. This
pins the FIX-1 wiring (capability report -> selector) on the exact brief the
live E2E uses.

**V4 — N6-E guidance parity on a REUSED workspace.** The pre-existing N6-E tests
only exercise a FRESH workspace, where ``Design.md`` is absent at the FRONTEND
invocation. A live incident on the reused Forma Atelier workspace showed a
different failure: the workspace already carried a ``Design.md`` from a PREVIOUS
build, so the FRONTEND invocation recorded that STALE ``Design.md`` as a used
input, and the post-FRONTEND N2 recompile then changed it -> a false
``GUIDANCE_VERSION_MISMATCH``. These tests pin that behaviour so the smallest
fix can be applied with confidence.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_capabilities import (  # noqa: E402
    DesignCapability,
    DesignCapabilityReport,
    STATUS_AVAILABLE,
)
from app.core.design_context import build_design_context_pack  # noqa: E402
from app.core.design_guidance import (  # noqa: E402
    compile_design_guidance,
    guidance_evidence,
    guidance_selection_input,
)
from app.core.document_store import (  # noqa: E402
    store_design_guidance,
    store_documents,
)
from app.core.guidance_snapshot import (  # noqa: E402
    record_invocation,
    verify_invocation,
)
from app.core.requirement_creator import compile_design, compile_prd  # noqa: E402
from app.core.state import ProjectState, ProjectStateStore  # noqa: E402

# The EXACT brief the Forma Atelier E2E build uses.
FORMA_BRIEF = {
    "name": "Forma Atelier",
    "what": "Landing page untuk studio arsitektur",
    "why": (
        "Menampilkan studio secara profesional: hero, showcase 3 proyek "
        "arsitektur, tentang studio, layanan, dan kontak agar pengunjung "
        "memahami karya dan menghubungi studio"
    ),
}

GUIDANCE = "ui_ux_pro_max"


def _available_report(resource_ids) -> DesignCapabilityReport:
    return DesignCapabilityReport(
        ok=True,
        manifest_version=1,
        profile_skills_dir="/tmp/profile/skills",
        resources={
            rid: DesignCapability(
                resource_id=rid, kind="skill", required=(rid == GUIDANCE),
                configured=True, available=True, status=STATUS_AVAILABLE, detail="",
            )
            for rid in resource_ids
        },
        failures=[],
        degraded=[],
    )


# ---------------------------------------------------------------------------
# V3 — non-empty pack for the Forma Atelier brief
# ---------------------------------------------------------------------------

class FormaAtelierPackTests(unittest.TestCase):
    def _pack(self, *, capability_report):
        evidence = guidance_evidence(FORMA_BRIEF, None)
        selection_input = guidance_selection_input(evidence)
        return build_design_context_pack(
            Path("."),
            design_dna=selection_input,
            query="Forma Atelier Landing page untuk studio arsitektur",
            include_guidance=True,
            retrieve=False,
            capability_report=capability_report,
        )

    def test_pack_is_nonempty_with_capability_report(self):
        pack = self._pack(capability_report=_available_report([GUIDANCE]))
        selected = pack.decisions["selected_resources"]
        self.assertGreater(len(selected), 0, "pack must carry the guidance resource")
        self.assertIn(GUIDANCE, {e["resource_id"] for e in selected})

    def test_pack_degrades_without_capability_report(self):
        """The exact live failure: no report => resources=0."""
        pack = self._pack(capability_report=None)
        self.assertEqual(len(pack.decisions["selected_resources"]), 0)


# ---------------------------------------------------------------------------
# V4 — N6-E parity on a REUSED workspace (the independent defect)
# ---------------------------------------------------------------------------

class _ReusedWorkspaceBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.ws = root / "workspaces" / "proj"
        self.ws.mkdir(parents=True, exist_ok=True)
        self.store = ProjectStateStore(root / "state")
        st = ProjectState(project_id="proj")
        st.revisions.source_revision = 1
        self.store.save(st)

    def tearDown(self):
        self._tmp.cleanup()

    def _store_build_inputs(self, revision: int):
        prd = compile_prd(FORMA_BRIEF, project_id="proj", source_revision=revision)
        store_documents(self.ws, prd_text=prd)
        store_design_guidance(self.ws, compile_design_guidance(
            FORMA_BRIEF, None, project_id="proj", source_revision=revision))

    def _store_post_frontend_design(self, revision: int, *, brand: str, accent: str):
        design = compile_design(
            {"brand_personality": brand, "palette": {"accent": accent},
             "typography": {}, "layout": {}, "version": revision},
            project_id="proj", source_revision=revision,
            design_dna_version=revision,
        )
        store_documents(self.ws, design_text=design)


class ReusedWorkspaceParityTests(_ReusedWorkspaceBase):
    def test_fresh_workspace_parity_passes(self):
        """Baseline: no stale Design.md => parity passes (the existing test)."""
        self._store_build_inputs(1)
        rec = record_invocation(self.store, "proj", self.ws, invocation_id="proj:r1")
        self._store_post_frontend_design(1, brand="editorial", accent="#a8674a")
        self.assertIsNone(verify_invocation(self.ws, rec))

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Documents the PRE-FIX behaviour for the record: WITHOUT the build-"
            "boundary invalidation, a REUSED workspace still carries the previous "
            "build's Design.md at the FRONTEND invocation, so it is recorded as a "
            "USED input; the post-FRONTEND N2 recompile then changes it and parity "
            "fails closed with a false GUIDANCE_VERSION_MISMATCH. The build path "
            "now invalidates that stale document (see "
            "test_n6e_stale_design_invalidation.py); this raw-snapshot sequence "
            "keeps proving the failure mode the fix removes."
        ),
    )
    def test_reused_workspace_rebuild_parity_passes(self):
        """PRE-FIX sequence: a rebuild of a REUSED workspace WITHOUT invalidation.

        The workspace already holds the previous build's Design.md and nothing
        removes it, so the raw N6-E mechanism records it as an input and the
        post-FRONTEND Design.md changes its digest. This is the exact false
        ``GUIDANCE_VERSION_MISMATCH`` the build boundary now prevents; kept as a
        strict xfail so the failure mode stays pinned.
        """
        # ---- BUILD 1: fresh, then Design.md written post-FRONTEND ----
        self._store_build_inputs(1)
        record_invocation(self.store, "proj", self.ws, invocation_id="proj:r1")
        self._store_post_frontend_design(1, brand="editorial", accent="#a8674a")

        # ---- BUILD 2: REUSED workspace (stale Design.md still on disk) ----
        self._store_build_inputs(2)
        rec2 = record_invocation(self.store, "proj", self.ws, invocation_id="proj:r2")
        self._store_post_frontend_design(2, brand="editorial v2", accent="#123456")
        self.assertIsNone(verify_invocation(self.ws, rec2))

    def test_reused_workspace_rebuild_passes_when_design_invalidated(self):
        """The fix is sufficient: invalidating the stale Design.md before the
        FRONTEND invocation (so it is recorded ABSENT, exactly like a fresh
        build) makes parity pass."""
        self._store_build_inputs(1)
        record_invocation(self.store, "proj", self.ws, invocation_id="proj:r1")
        self._store_post_frontend_design(1, brand="editorial", accent="#a8674a")

        # BUILD 2 with the stale Design.md invalidated first, via the real helper.
        self._store_build_inputs(2)
        from app.core.document_store import invalidate_design
        self.assertTrue(invalidate_design(self.ws), "stale design must be removed")
        rec2 = record_invocation(self.store, "proj", self.ws, invocation_id="proj:r2")
        self.assertIsNone(rec2["design_digest"], "invalidated => recorded absent")
        self._store_post_frontend_design(2, brand="editorial v2", accent="#123456")
        self.assertIsNone(verify_invocation(self.ws, rec2),
                          "rebuild parity must pass once the stale design is gone")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
