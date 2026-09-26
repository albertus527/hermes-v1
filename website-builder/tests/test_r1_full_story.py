"""Full local R1 end-to-end story through the REAL production control flow.

Proves the complete happy path plus revision/promote:
  1. user asks for a reading-rental website with no explicit business name
  2. system asks name
  3. user says "kitsunereading"
  4. same internal project retained
  5. requirements complete
  6. build succeeds
  7. fake Vercel creates project "kitsunereading" exactly once
  8. preview deploy succeeds
  9. smoke succeeds
 10. Telegram preview delivered once
 11. user sends revision
 12. revision applies once
 13. revised preview delivered once
 14. user approves
 15. fake promote succeeds
 16. production smoke succeeds
 17. project reaches LIVE
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


LONG_DESCRIPTION = (
    "aku mau bikin web untuk membaca, jadi disitu bisa sewa bahan bacaan, "
    "soft tone dan bright"
)


def _full_story(h: LocalR1Scenario) -> str:
    # 1-2. Descriptive request with no explicit business name -> ask name.
    h.set_router_decision("CREATE_PROJECT", confidence="high",
                          proposed_new_project_name=None)
    h.send_user_message(LONG_DESCRIPTION)
    assert h.registry.load_or_create(h.chat_id).projects == []
    assert any("nama" in t.lower() for _, t in h.telegram.text_calls)

    # 3-5. The user answers with the explicit name.
    h.set_intake_response(name="kitsunereading", what="reading rental",
                          why="rent books", readiness="DISCOVERY_READY",
                          clarification_needed=False)
    h.send_user_message("kitsunereading")
    pid = h.current_project_id
    assert pid is not None
    entry = h.registry_entry(pid)
    assert entry.display_name == "kitsunereading"

    # 6-10. Real build -> QA -> preview (fake Vercel/smoke/Telegram).
    h.run_build_and_preview(pid)
    return pid


class TestFullLocalStory:
    def test_complete_story_reaches_live_with_canonical_consistency(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        pid = _full_story(h)

        state = h.project_state(pid)
        assert state.lifecycle == "PREVIEW_READY"
        # 7-8: exactly one Vercel project create and one deploy.
        assert h.vercel.post_calls == 1
        assert h.vercel.created_projects == ["kitsunereading"]
        assert h.vercel.deploy_calls == 1
        # 9-10: smoke ran and exactly one preview photo was delivered.
        assert len(h.smoke.calls) == 1
        assert len(h.telegram.photo_calls) == 2
        assert state.revisions.preview_revision == state.revisions.source_revision

        # 11-13. A revision applies exactly once and delivers exactly one new
        # preview (two screenshots per delivery, so 2 -> 4).
        rev = h.run_revision("ganti warna jadi lebih lembut, tetap soft")
        assert rev.success, getattr(rev, "error", None)
        state = h.project_state(pid)
        assert state.revisions.revision_seq == 1
        assert state.revisions.source_revision == 2
        assert state.revisions.preview_revision == 2
        assert len(h.telegram.photo_calls) == 4
        # Still exactly one project create.
        assert h.vercel.post_calls == 1

        # 14. Approve the current preview.
        approval = h.approve()
        assert approval.success, approval.error
        state = h.project_state(pid)
        assert state.revisions.approved_revision == 2

        # 15-17. Promote -> production smoke -> LIVE.
        promoted = h.promote_now()
        assert promoted.success, promoted.error
        state = h.project_state(pid)
        assert state.lifecycle == "LIVE"
        assert h.vercel.promote_calls == 1
        # Each of the two previews ran a preview smoke; promotion ran one
        # production smoke. Total 3.
        assert len(h.smoke.calls) == 3
        assert h.smoke.calls[-1] == "https://prod.vercel.app"

        # Final consistency.
        entry = h.registry_entry(pid)
        assert entry.display_name == "kitsunereading"
        assert entry.vercel_slug == "kitsunereading"
        assert state.brief["name"] == "kitsunereading"
        r = state.revisions
        assert (r.source_revision == r.qa_revision == r.preview_revision
                == r.approved_revision == r.live_revision == 2)
        assert state.production_url == "https://prod.vercel.app"
        # No duplicate project create / deployment / Telegram preview.
        assert h.vercel.post_calls == 1
        assert h.vercel.deploy_calls == 2  # one per preview-bearing revision
        assert len(h.telegram.photo_calls) == 4
        # No skipped revision seq.
        seqs = sorted(e["seq"] for e in state.pending_revisions)
        assert seqs == list(range(1, len(seqs) + 1))

    def test_story_survives_restart_between_steps(self, tmp_path):
        """The whole story is durable across a process restart."""
        h = LocalR1Scenario(tmp_path)
        pid = _full_story(h)
        # Restart before revising.
        h.restart()
        rev = h.run_revision("hero lebih kecil")
        assert rev.success
        h.restart()
        assert h.approve().success
        h.restart()
        assert h.promote_now().success
        assert h.project_state(pid).lifecycle == "LIVE"
        assert h.vercel.post_calls == 1
