"""N6-E — Per-invocation guidance snapshots and QA/VISION parity.

Proves the N6 requirements that:

* the exact guidance snapshot is recorded at the MOMENT of each FRONTEND
  invocation (invocation id + source revision + document digests), with bounded
  history that never drops the current release's evidence;
* a document created AFTER an invocation (the post-build Design.md) is not
  retroactively represented as input to that invocation;
* QA/VISION verify against the RECORDED snapshot, and a true mismatch fails
  closed — parity is never inferred from the current workspace;
* specification parity is kept separate from design compliance.

No live model, browser, network, or npm is required.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_guidance import compile_design_guidance  # noqa: E402
from app.core.decision_engine import DesignDecision  # noqa: E402
from app.core.decision_provider import DesignCandidate  # noqa: E402
from app.core.document_store import (  # noqa: E402
    store_design_guidance,
    store_documents,
)
from app.core.guidance_snapshot import (  # noqa: E402
    DEPLOYMENT_GUIDANCE_KEY,
    MAX_SNAPSHOT_HISTORY,
    find_invocation,
    latest_invocation,
    record_invocation,
    verify_invocation,
)
from app.core.requirement_creator import compile_design, compile_prd  # noqa: E402
from app.core.state import ProjectState, ProjectStateStore  # noqa: E402

BRIEF = {"name": "Northcut", "what": "barbershop", "why": "book a cut"}


def _candidate():
    return DesignCandidate(
        candidate_id="c1", label="Editorial", descriptor="editorial grid",
        palette=("#101014",), source_uri="viking://design/x", source_revision="r1",
        score=0.9,
    )


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.ws = root / "ws"
        self.ws.mkdir(parents=True, exist_ok=True)
        self.store = ProjectStateStore(root / "state")
        st = ProjectState(project_id="p")
        st.revisions.source_revision = 1
        self.store.save(st)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_prd(self):
        prd = compile_prd(BRIEF, project_id="p", source_revision=1)
        store_documents(self.ws, prd_text=prd)
        return prd

    def _write_guidance(self):
        g = compile_design_guidance(
            BRIEF,
            DesignDecision(selected=_candidate(), ranked=(_candidate(),),
                           abstained=False, abstain_reason="", source="provider",
                           provider="openviking"),
            project_id="p", source_revision=1,
        )
        store_design_guidance(self.ws, g)
        return g


class RecordingTests(_Base):
    def test_records_at_invocation(self):
        self._write_prd()
        rec = record_invocation(self.store, "p", self.ws, invocation_id="p:r1")
        assert rec is not None
        self.assertEqual(rec["invocation_id"], "p:r1")
        self.assertEqual(rec["source_revision"], 1)
        self.assertIsNotNone(rec["prd_digest"])
        self.assertIsNotNone(rec["combined_digest"])

    def test_no_documents_records_nothing(self):
        rec = record_invocation(self.store, "p", self.ws, invocation_id="p:r1")
        self.assertIsNone(rec)
        state = self.store.load("p")
        assert state is not None
        self.assertNotIn(DEPLOYMENT_GUIDANCE_KEY, state.deployment)

    def test_post_build_design_is_not_retroactive_input(self):
        """Design.md written AFTER the invocation is absent from that record."""
        self._write_prd()
        record_invocation(self.store, "p", self.ws, invocation_id="p:r1")
        # Now FRONTEND has produced DNA -> Design.md is compiled.
        d = compile_design(
            {"brand_personality": "warm", "palette": {"primary": "#000"}},
            project_id="p", source_revision=1,
        )
        store_documents(self.ws, design_text=d)
        state = self.store.load("p")
        rec = find_invocation(state, "p:r1")
        assert rec is not None
        # The invocation record must NOT claim Design.md was an input.
        self.assertIsNone(rec["design_digest"])

    def test_history_is_bounded_but_keeps_latest(self):
        self._write_prd()
        for i in range(MAX_SNAPSHOT_HISTORY + 5):
            record_invocation(self.store, "p", self.ws, invocation_id=f"p:r{i}")
        state = self.store.load("p")
        block = state.deployment[DEPLOYMENT_GUIDANCE_KEY]
        self.assertLessEqual(len(block["history"]), MAX_SNAPSHOT_HISTORY)
        # The newest record (current release evidence) is retained AND is what
        # latest_invocation() returns — the oldest must have been pruned.
        self.assertEqual(block["latest_invocation_id"],
                         f"p:r{MAX_SNAPSHOT_HISTORY + 4}")
        newest = latest_invocation(state)
        assert newest is not None
        self.assertEqual(newest["invocation_id"], f"p:r{MAX_SNAPSHOT_HISTORY + 4}")
        # The oldest record was pruned away.
        self.assertIsNone(find_invocation(state, "p:r0"))

    def test_re_record_same_invocation_is_idempotent(self):
        self._write_prd()
        record_invocation(self.store, "p", self.ws, invocation_id="p:r1")
        record_invocation(self.store, "p", self.ws, invocation_id="p:r1")
        state = self.store.load("p")
        block = state.deployment[DEPLOYMENT_GUIDANCE_KEY]
        self.assertEqual(len(block["history"]), 1)


class VerifyTests(_Base):
    def test_match_verifies(self):
        self._write_prd()
        rec = record_invocation(self.store, "p", self.ws, invocation_id="p:r1")
        self.assertIsNone(verify_invocation(self.ws, rec))

    def test_changed_document_fails_closed(self):
        self._write_prd()
        rec = record_invocation(self.store, "p", self.ws, invocation_id="p:r1")
        # A DIFFERENT valid PRD appears after the invocation.
        other = compile_prd(
            {"name": "Northcut", "what": "changed", "why": "book a cut"},
            project_id="p", source_revision=2,
        )
        from app.core.document_contracts import PRD_FILE_NAME
        (self.ws / PRD_FILE_NAME).write_text(other, encoding="utf-8")
        reason = verify_invocation(self.ws, rec)
        self.assertIsNotNone(reason)
        self.assertIn("GUIDANCE_VERSION_MISMATCH", reason)

    def test_missing_used_document_fails_closed(self):
        self._write_prd()
        rec = record_invocation(self.store, "p", self.ws, invocation_id="p:r1")
        from app.core.document_contracts import PRD_FILE_NAME
        (self.ws / PRD_FILE_NAME).unlink()
        reason = verify_invocation(self.ws, rec)
        self.assertIsNotNone(reason)
        self.assertIn("GUIDANCE_VERSION_MISMATCH", reason)

    def test_later_appearance_is_not_a_mismatch(self):
        """The post-build Design.md appearing later is expected, not a mismatch."""
        self._write_prd()
        rec = record_invocation(self.store, "p", self.ws, invocation_id="p:r1")
        d = compile_design(
            {"brand_personality": "warm", "palette": {"primary": "#000"}},
            project_id="p", source_revision=1,
        )
        store_documents(self.ws, design_text=d)
        self.assertIsNone(verify_invocation(self.ws, rec))

    def test_none_record_verifies_trivially(self):
        self.assertIsNone(verify_invocation(self.ws, None))

    def test_guidance_change_is_detected(self):
        self._write_prd()
        self._write_guidance()
        rec = record_invocation(self.store, "p", self.ws, invocation_id="p:r1")
        # Replace the guidance with a different valid document.
        g2 = compile_design_guidance(BRIEF, None, project_id="p", source_revision=1)
        from app.core.document_contracts import DESIGN_GUIDANCE_FILE_NAME
        (self.ws / DESIGN_GUIDANCE_FILE_NAME).write_text(g2, encoding="utf-8")
        reason = verify_invocation(self.ws, rec)
        self.assertIsNotNone(reason)
        self.assertIn("GUIDANCE_VERSION_MISMATCH", reason)


class LatestTests(_Base):
    def test_latest_invocation_returns_newest(self):
        self._write_prd()
        record_invocation(self.store, "p", self.ws, invocation_id="p:r1")
        record_invocation(self.store, "p", self.ws, invocation_id="p:r2")
        state = self.store.load("p")
        rec = latest_invocation(state)
        assert rec is not None
        self.assertEqual(rec["invocation_id"], "p:r2")

    def test_latest_invocation_none_when_empty(self):
        state = self.store.load("p")
        self.assertIsNone(latest_invocation(state))


class VisionParityTests(unittest.TestCase):
    """VISION receives design guidance and keeps the two judgements separate."""

    def _adapter(self):
        from app.hermes.adapter import HermesAdapter

        return HermesAdapter.__new__(HermesAdapter)

    def test_vision_prompt_unchanged_without_guidance(self):
        a = self._adapter()
        p = a._build_vision_prompt(BRIEF, None)
        self.assertNotIn("design compliance", p.lower())

    def test_vision_prompt_includes_guidance_and_separates_judgements(self):
        a = self._adapter()
        g = compile_design_guidance(BRIEF, None, project_id="p", source_revision=1)
        p = a._build_vision_prompt(BRIEF, None, g)
        self.assertIn("PRE-BUILD DesignGuidance", p)
        self.assertIn("specification parity", p.lower())
        self.assertIn("design compliance", p.lower())

    def test_vision_prompt_signature_accepts_guidance_kwarg(self):
        import inspect

        a = self._adapter()
        params = inspect.signature(a.vision_inspect).parameters
        self.assertIn("design_guidance", params)


class FrontendInvocationRecordingTests(unittest.TestCase):
    """N6-E: frontend_build records the snapshot at the invocation."""

    def test_frontend_build_records_the_invocation_snapshot(self):
        import tempfile
        from unittest.mock import MagicMock
        from app.core.state import ProjectState, ProjectStateStore
        from app.core.requirement_creator import compile_prd
        from app.core.document_store import store_documents
        from app.core.guidance_snapshot import (
            latest_invocation,
        )
        from app.hermes.adapter import HermesAdapter, HermesResult

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            ws.mkdir()
            store = ProjectStateStore(root / "state")
            st = ProjectState(project_id="p")
            st.revisions.source_revision = 1
            store.save(st)

            prd = compile_prd(BRIEF, project_id="p", source_revision=1)
            store_documents(ws, prd_text=prd)

            adapter = HermesAdapter.__new__(HermesAdapter)
            adapter.store = store
            adapter.hermes_home = root / "hermes"
            adapter.repo_root = root
            # Bypass the actual model: return a benign result.
            adapter._run_hermes_cli = MagicMock(
                return_value=HermesResult(success=False, error="stub")
            )
            adapter._has_complete_frontend_artifacts = MagicMock(return_value=False)

            adapter.frontend_build(
                project_id="p", brief=BRIEF, workspace=ws,
                build_operation_id="7",
            )

            state = store.load("p")
            rec = latest_invocation(state)
            assert rec is not None, "frontend_build must record a snapshot"
            self.assertEqual(rec["invocation_id"], "p:r7")
            self.assertIsNotNone(rec["prd_digest"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
