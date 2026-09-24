"""Phase F — R1 revision crash-window recovery (Bug 7).

Scenario: revision seq N reserved, build/QA succeed, the preview is
SUCCESSFULLY delivered to Telegram, then the process crashes BEFORE the final
`revision_seq` / `pending_revisions[seq].applied` commit.

Required recovery invariant:
  * seq N remains the active unfinished revision
  * no seq N+1 may overtake it
  * the preview must NOT be re-sent
  * restart/re-entry reconciles/finalizes N exactly once
  * revision history remains gap-free

The test performs the REAL prior steps, injects the crash exactly after
durable preview delivery but before final revision application persistence,
then re-enters with a NEW orchestrator over the SAME temp state.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from r1_harness import LocalR1Scenario, patch_qa_boundaries  # noqa: E402


@pytest.fixture(autouse=True)
def _qa_boundaries(monkeypatch):
    patch_qa_boundaries(monkeypatch)


class _CrashAfterPreviewDelivery(Exception):
    """Injected crash signal raised from the post-preview hook."""


def _setup_preview_ready(h: LocalR1Scenario, name: str = "kitsunereading"):
    """Drive a real build+preview so the project is genuinely PREVIEW_READY
    with a delivered preview, then return the project id."""
    h.set_router_decision("CREATE_PROJECT", confidence="high",
                          proposed_new_project_name=name)
    h.set_intake_response(name=name, what="reading rental", why="rent books",
                          readiness="DISCOVERY_READY", clarification_needed=False)
    h.send_user_message(f"bikin website baru namanya {name}")
    pid = h.current_project_id
    h.run_build_and_preview(pid)
    return pid


def _install_crash_hook_after_delivery(h: LocalR1Scenario):
    """Wrap the harness preview orchestrator so a crash is raised EXACTLY
    after a successful preview delivery (durable), modelling a process death
    before the revision finalization write."""
    original_run_owned = h.preview.run_owned

    def crashing_run_owned(project_id, workspace, *, slot_held=False):
        result = original_run_owned(project_id, workspace, slot_held=slot_held)
        if result.success and h._crash_armed:
            h._crash_armed = False
            raise _CrashAfterPreviewDelivery("crash after preview delivery")
        return result

    h.preview.run_owned = crashing_run_owned


class TestRevisionCrashAfterPreviewDelivery:
    def test_crash_after_preview_delivery_then_recovery_finalizes_once(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        pid = _setup_preview_ready(h)
        photos_after_initial = len(h.telegram.photo_calls)
        assert photos_after_initial == 1

        # Arm the crash for the revision's preview delivery.
        h._crash_armed = True
        _install_crash_hook_after_delivery(h)

        # The injected post-delivery exception is reconciled by the owner;
        # no revision is lost and the delivery is not repeated.
        result = h.run_revision("ganti warna jadi lebih lembut")
        assert result.success
        assert result.reconciled

        state = h.store.load(pid)
        assert state.revisions.queued_revision_seq == 1
        assert state.revisions.revision_seq == 1
        pending = [e for e in state.pending_revisions if e.get("seq") == 1]
        assert pending and pending[0]["applied"] is True
        assert len(h.telegram.photo_calls) == photos_after_initial + 1

        # ---- Recovery: new runtime/orchestrator over the SAME temp state. ----
        h.restart()
        result = h.revise.apply(pid, 1, "ganti warna jadi lebih lembut",
                               principal_id=h.principal_id)
        assert not result.success
        assert result.error_code == "REVISION_ALREADY_APPLIED"

        recovered = h.store.load(pid)
        # Exactly once: seq 1 applied, no gap, no seq 2.
        assert recovered.revisions.revision_seq == 1
        assert recovered.revisions.queued_revision_seq == 1
        p = [e for e in recovered.pending_revisions if e.get("seq") == 1]
        assert p and p[0]["applied"] is True
        # Preview NOT re-sent.
        assert len(h.telegram.photo_calls) == photos_after_initial + 1
        # Revision history gap-free.
        seqs = sorted(e["seq"] for e in recovered.pending_revisions)
        assert seqs == list(range(1, len(seqs) + 1))
