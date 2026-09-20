"""Phase 10 tests: ordered revision requests against persisted Design DNA.

Tests do NOT require a live LLM, browser, or npm. QAOrchestrator and
PreviewOrchestrator boundaries are stubbed; FRONTEND is a mock adapter.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.contracts import OperationResult
from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore
from app.projects.revise import RevisionOrchestrator
from app.sandbox.runner import ProjectRunner


OWNER = "owner-1"
STRANGER = "stranger-9"


def _make_workspace(tmpdir: Path, project_id: str) -> Path:
    ws = tmpdir / "workspaces" / project_id
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "App.tsx").write_text("// content", encoding="utf-8")
    (ws / "design-dna.json").write_text('{"version": 1}', encoding="utf-8")
    return ws


class RevisionOrchestratorTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmpdir_obj.name)
        self.workspace_root = self.tmpdir / "workspaces"
        self.state_root = self.tmpdir / "state"
        self.store = ProjectStateStore(self.state_root)
        self.runner = ProjectRunner(self.workspace_root, self.store)
        self.mock_adapter = MagicMock()
        self.mock_preview = MagicMock()
        self.mock_preview.run_owned.return_value = OperationResult.ok({"preview_url": "https://x.vercel.app"})
        self.orchestrator = RevisionOrchestrator(
            self.runner, self.store,
            hermes_adapter=self.mock_adapter,
            preview_orchestrator=self.mock_preview,
        )

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    def _preview_ready(self, project_id: str) -> Path:
        ws = _make_workspace(self.tmpdir, project_id)
        with self.store.acquire_writer(project_id) as state:
            state.brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}
            state.design_dna = {"version": 1, "typography": {"heading_font": "Inter", "body_font": "Inter"}}
            state.lifecycle = ProjectLifecycle.RUNNING.value
            state.lifecycle = ProjectLifecycle.PREVIEW_READY.value
            state.roles["owner"] = OWNER
            self.store.save(state)
        return ws


class TestReservation(RevisionOrchestratorTestBase):
    def test_first_reservation_succeeds_and_sets_revision_requested(self):
        self._preview_ready("proj-a")
        result = self.orchestrator.reserve("proj-a", 1, principal_id=OWNER)
        self.assertTrue(result.success)
        state = self.store.load("proj-a")
        self.assertEqual(state.lifecycle, ProjectLifecycle.REVISION_REQUESTED.value)
        self.assertEqual(state.revisions.queued_revision_seq, 1)

    def test_out_of_order_reservation_rejected(self):
        self._preview_ready("proj-b")
        result = self.orchestrator.reserve("proj-b", 2, principal_id=OWNER)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "OUT_OF_ORDER_REVISION")

    def test_duplicate_reservation_rejected(self):
        """A reservation for an ALREADY-APPLIED seq must still be rejected.

        F6 note: re-reserving the *current, un-applied* queued seq is now a
        safe idempotent re-drive (adoption), not a duplicate. A duplicate of
        an *applied* reservation must remain rejected.
        """
        self._preview_ready("proj-c")
        self.orchestrator.reserve("proj-c", 1, principal_id=OWNER)
        # Mark the reservation applied so seq=1 is now behind queued_seq.
        with self.store.acquire_writer("proj-c") as state:
            for entry in state.pending_revisions:
                if entry.get("seq") == 1:
                    entry["applied"] = True
            self.store.save(state)
        result = self.orchestrator.reserve("proj-c", 1, principal_id=OWNER)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "OUT_OF_ORDER_REVISION")

    def test_redrive_same_pending_reservation_is_idempotent(self):
        """(F6-a/b) crash-after-reserve re-drive: re-reserving the SAME
        pending seq with the SAME principal adopts the reservation instead of
        appending a new one or bumping the sequence."""
        self._preview_ready("proj-cx")
        first = self.orchestrator.reserve("proj-cx", 1, principal_id=OWNER)
        self.assertTrue(first.success)
        # Simulate crash-between-reserve-and-apply: state is REVISION_REQUESTED
        # with an un-applied reservation for seq=1.
        second = self.orchestrator.reserve("proj-cx", 1, principal_id=OWNER)
        self.assertTrue(second.success)
        self.assertTrue(second.data.get("redriven"))
        state = self.store.load("proj-cx")
        self.assertEqual(state.revisions.queued_revision_seq, 1)
        # Only ONE pending reservation exists -- no duplicate was appended.
        self.assertEqual(
            sum(1 for e in state.pending_revisions if e.get("seq") == 1), 1
        )

    def test_redrive_rejects_different_principal(self):
        """(F6-f) a pending reservation cannot be adopted by a different
        principal -- ownership is preserved."""
        self._preview_ready("proj-cy")
        self.orchestrator.reserve("proj-cy", 1, principal_id=OWNER)
        result = self.orchestrator.reserve("proj-cy", 1, principal_id=STRANGER)
        self.assertFalse(result.success)

    def test_out_of_order_seq_still_rejected_after_redrive(self):
        """(F6-e) skipping ahead to seq=2 while seq=1 is still pending is
        rejected -- monotonic ordering is preserved. The project stays parked
        in REVISION_REQUESTED until seq=1 is applied, so a skip-ahead is
        rejected (fail-closed) and never bumps the sequence."""
        self._preview_ready("proj-cz")
        self.orchestrator.reserve("proj-cz", 1, principal_id=OWNER)
        result = self.orchestrator.reserve("proj-cz", 2, principal_id=OWNER)
        self.assertFalse(result.success)
        self.assertIn(result.error_code, (
            "OUT_OF_ORDER_REVISION", "REVISION_NOT_ALLOWED_IN_LIFECYCLE"))
        state = self.store.load("proj-cz")
        self.assertEqual(state.revisions.queued_revision_seq, 1)

    def test_reservation_rejected_outside_allowed_lifecycle(self):
        with self.store.acquire_writer("proj-d") as state:
            state.lifecycle = ProjectLifecycle.DISCOVERING.value
            state.roles["owner"] = OWNER
            self.store.save(state)
        result = self.orchestrator.reserve("proj-d", 1, principal_id=OWNER)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "REVISION_NOT_ALLOWED_IN_LIFECYCLE")

    def test_second_reservation_blocked_until_first_applied(self):
        self._preview_ready("proj-e")
        self.orchestrator.reserve("proj-e", 1, principal_id=OWNER)
        # Still REVISION_REQUESTED, not PREVIEW_READY/LIVE -> blocked
        result = self.orchestrator.reserve("proj-e", 2, principal_id=OWNER)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "REVISION_NOT_ALLOWED_IN_LIFECYCLE")

    def test_reservation_rejected_for_unauthorized_principal(self):
        self._preview_ready("proj-a2")
        result = self.orchestrator.reserve("proj-a2", 1, principal_id=STRANGER)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "UNAUTHORIZED_ROLE")
        state = self.store.load("proj-a2")
        self.assertEqual(state.revisions.queued_revision_seq, 0)
        self.assertEqual(state.lifecycle, ProjectLifecycle.PREVIEW_READY.value)

    def test_reservation_rejected_without_any_principal(self):
        self._preview_ready("proj-a3")
        result = self.orchestrator.reserve("proj-a3", 1)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "UNAUTHORIZED_ROLE")


class TestApply(RevisionOrchestratorTestBase):
    def _passing_qa(self):
        return patch(
            "app.projects.revise.QAOrchestrator",
            return_value=MagicMock(run=MagicMock(return_value=MagicMock(success=True, error=None))),
        )

    def test_revision_preserves_combined_build_context(self):
        from app.core.references import ReferenceItem, persisted_reference_instructions
        from app.projects.directions import direction_build_instructions
        from app.core.contact_form import decide_contact_method, compose_contact_form_instructions
        for key in (None, "private-access-key"):
            pid = "context-key" if key else "context-link"
            ws = self._preview_ready(pid)
            with self.store.acquire_writer(pid) as state:
                state.design_references = {"UX": {"item": ReferenceItem(
                    "UX", "upload", "a" * 64, "image/png", 100).to_dict(), "evidence": "Clear hierarchy"}}
                state.selected_direction = {"label": "Warm", "descriptor": "Quiet", "palette": {}}
                state.brief["why_destination"] = "https://wa.me/12345678901"
                self.store.save(state)
            state = self.store.load(pid)
            expected = "\n\n".join((
                persisted_reference_instructions(state),
                direction_build_instructions(state.selected_direction),
                compose_contact_form_instructions(decide_contact_method(key, state.brief["why_destination"])),
                self.orchestrator._build_revision_instructions(state.design_dna, "smaller hero"),
            ))
            revision = RevisionOrchestrator(self.runner, self.store, self.mock_adapter,
                                            self.mock_preview, web3forms_access_key=key)
            assert revision.reserve(pid, 1, principal_id=OWNER).success
            self.mock_adapter.frontend_build.return_value = {"success": True, "design_dna": {
                "version": 2, "reference_synthesis": {"UX": "Original hierarchy"}}}
            with self._passing_qa():
                result = revision.apply(pid, 1, "smaller hero", workspace=ws, principal_id=OWNER)
            assert result.success, result.error
            instructions = self.mock_adapter.frontend_build.call_args.kwargs["design_dna_instructions"]
            assert instructions == expected
            if key:
                assert key not in instructions
                assert "{{WEB3FORMS_ACCESS_KEY}}" not in instructions
                assert "https://wa.me/12345678901" in instructions

    def test_revision_rejects_missing_returned_reference_synthesis(self):
        from app.core.references import ReferenceItem
        ws = self._preview_ready("missing-synthesis")
        with self.store.acquire_writer("missing-synthesis") as state:
            state.design_references = {"UX": {"item": ReferenceItem(
                "UX", "upload", "a" * 64, "image/png", 100).to_dict(), "evidence": "Clear hierarchy"}}
            state.design_dna["reference_synthesis"] = {"UX": "Old valid synthesis"}
            self.store.save(state)
        old_dna = self.store.load("missing-synthesis").design_dna
        assert self.orchestrator.reserve("missing-synthesis", 1, principal_id=OWNER).success
        self.mock_adapter.frontend_build.return_value = {"success": True}
        with self._passing_qa() as qa:
            result = self.orchestrator.apply("missing-synthesis", 1, "change", workspace=ws, principal_id=OWNER)
        assert result.error_code == "REFERENCE_SYNTHESIS_INVALID"
        qa.assert_not_called()
        self.mock_preview.run_owned.assert_not_called()
        state = self.store.load("missing-synthesis")
        assert state.lifecycle == "FAILED"
        assert state.design_dna == old_dna
        assert state.revisions.revision_seq == 0

    def test_apply_without_reservation_fails_closed(self):
        ws = self._preview_ready("proj-f")
        result = self.orchestrator.apply("proj-f", 1, "make header red", workspace=ws, principal_id=OWNER)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "REVISION_NOT_RESERVED")

    def test_apply_rejected_for_unauthorized_principal(self):
        ws = self._preview_ready("proj-f2")
        self.orchestrator.reserve("proj-f2", 1, principal_id=OWNER)
        result = self.orchestrator.apply("proj-f2", 1, "make header red", workspace=ws, principal_id=STRANGER)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "UNAUTHORIZED_ROLE")
        state = self.store.load("proj-f2")
        self.assertEqual(state.revisions.revision_seq, 0)
        self.mock_adapter.frontend_build.assert_not_called()

    def test_apply_success_advances_revision_seq_and_invalidates_qa(self):
        ws = self._preview_ready("proj-g")
        self.orchestrator.reserve("proj-g", 1, principal_id=OWNER)
        with self.store.acquire_writer("proj-g") as state:
            state.deployment["checked"] = {"source_revision": 5}
            state.deployment["tested_snapshot"] = {"x": 1}
            self.store.save(state)

        self.mock_adapter.frontend_build.return_value = {
            "success": True,
            "design_dna": {"version": 2, "typography": {"heading_font": "Inter", "body_font": "Inter"}},
        }
        with self._passing_qa():
            result = self.orchestrator.apply("proj-g", 1, "make header red", workspace=ws, principal_id=OWNER)

        self.assertTrue(result.success, result.error)
        state = self.store.load("proj-g")
        self.assertEqual(state.revisions.revision_seq, 1)
        self.assertEqual(state.revisions.source_revision, 1)
        self.assertNotIn("checked", state.deployment)
        self.assertNotIn("tested_snapshot", state.deployment)
        self.mock_preview.run_owned.assert_called_once()

    def test_apply_rejects_typography_violation(self):
        ws = self._preview_ready("proj-h")
        self.orchestrator.reserve("proj-h", 1, principal_id=OWNER)
        self.mock_adapter.frontend_build.return_value = {
            "success": True,
            "design_dna": {
                "version": 2,
                "typography": {"heading_font": "Inter", "body_font": "Georgia"},
            },
        }
        # Add a 3rd family via an extra top-level field is not part of the
        # contract; simulate violation using 3 distinct values across the
        # two allowed keys is impossible (only 2 keys) so instead assert
        # the allowed 2-family case passes and a > 2 family case (patched
        # helper) fails.
        with patch("app.projects.revise.validate_typography", return_value=False):
            with self._passing_qa():
                result = self.orchestrator.apply("proj-h", 1, "add a 3rd font", workspace=ws, principal_id=OWNER)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "DESIGN_DNA_TYPOGRAPHY_VIOLATION")
        state = self.store.load("proj-h")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)

    def test_apply_twice_with_same_seq_is_rejected(self):
        ws = self._preview_ready("proj-i")
        self.orchestrator.reserve("proj-i", 1, principal_id=OWNER)
        self.mock_adapter.frontend_build.return_value = {
            "success": True,
            "design_dna": {"version": 2, "typography": {"heading_font": "Inter", "body_font": "Inter"}},
        }
        with self._passing_qa():
            first = self.orchestrator.apply("proj-i", 1, "req", workspace=ws, principal_id=OWNER)
        self.assertTrue(first.success)

        second = self.orchestrator.apply("proj-i", 1, "req again", workspace=ws, principal_id=OWNER)
        self.assertFalse(second.success)
        self.assertEqual(second.error_code, "REVISION_ALREADY_APPLIED")

    def test_frontend_failure_marks_project_failed(self):
        ws = self._preview_ready("proj-j")
        self.orchestrator.reserve("proj-j", 1, principal_id=OWNER)
        self.mock_adapter.frontend_build.return_value = {
            "success": False,
            "error": "FRONTEND exploded",
        }
        result = self.orchestrator.apply("proj-j", 1, "req", workspace=ws, principal_id=OWNER)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "FRONTEND_REVISION_FAILED")
        state = self.store.load("proj-j")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)

    def test_qa_failure_does_not_advance_revision_seq(self):
        ws = self._preview_ready("proj-k")
        self.orchestrator.reserve("proj-k", 1, principal_id=OWNER)
        self.mock_adapter.frontend_build.return_value = {
            "success": True,
            "design_dna": {"version": 2, "typography": {"heading_font": "Inter", "body_font": "Inter"}},
        }
        with patch(
            "app.projects.revise.QAOrchestrator",
            return_value=MagicMock(run=MagicMock(return_value=MagicMock(success=False, error="QA blocked"))),
        ):
            result = self.orchestrator.apply("proj-k", 1, "req", workspace=ws, principal_id=OWNER)
        self.assertFalse(result.success)
        state = self.store.load("proj-k")
        self.assertEqual(state.revisions.revision_seq, 0)

    def test_worker_slot_busy_fails_closed(self):
        ws = self._preview_ready("proj-l")
        self.orchestrator.reserve("proj-l", 1, principal_id=OWNER)
        self.runner.acquire_project("other-project")
        result = self.orchestrator.apply("proj-l", 1, "req", workspace=ws, principal_id=OWNER)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "WORKER_BUSY")


if __name__ == "__main__":
    unittest.main()
