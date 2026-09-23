"""PHASE G — realistic full local story + restart/recovery over the R1 harness.

The exact story from the campaign:

  TURN 1: long descriptive reading-rental website request (books + magazines,
          on-site atmosphere, collection info, soft tone, bright design)
          -> system asks for a name; original request context durably preserved
  TURN 2: "cozyreadingspace"
          -> same internal project_id; registry.display_name ==
             brief.name == "cozyreadingspace"; original TURN 1 details survive

  Then: build -> QA -> Vercel project ensure/create with a REAL double
  bootstrap timing (poll 1: BUILDING, poll 2: READY) -> automation bypass
  provisioned ONCE -> protected preview deployed -> desktop + mobile smoke
  pass WITH the bypass -> Telegram preview delivered once -> revision ->
  revised preview -> approve -> promote -> LIVE.

  Restart over the SAME temp state: reuse project, bypass, preview; no
  duplicate project/bootstrap/bypass/preview; Telegram delivered once.
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


TURN_1 = (
    "aku mau bikin web untuk membaca, jadi disitu bisa sewa bahan bacaan kaya "
    "buku dan majalah dan ada info buat bisa baca langsung di tempat kaya vibe "
    "nya gimana dan suasananya gimana, dan koleksi buku yg di punya\n"
    "buat desainnya dibikin soft tone dan bright"
)
TURN_2 = "cozyreadingspace"


def _open_project(h: LocalR1Scenario, *, bootstrap_transient: bool = True) -> str:
    """Drive TURN 1 -> name clarification -> TURN 2 name; return project id.

    The TURN 2 answer completes the brief, which AUTO-TRIGGERS the build
    (existing R1 contract). ``bootstrap_transient`` scripts the bootstrap
    timing BEFORE that auto-build so the first preview attempt observes the
    real "bound but BUILDING" transient state.
    """
    h.set_router_decision("CREATE_PROJECT", confidence="high",
                          proposed_new_project_name=None)
    h.send_user_message(TURN_1)
    assert h.pending_action["action"] == "CREATE_PROJECT"
    assert h.pending_action["awaiting"] == "NAME"
    assert h.pending_action.get("original_request")

    if bootstrap_transient:
        h.vercel.bootstrap_building_then_ready = True
    h.set_intake_response(
        name="cozyreadingspace",
        what="reading rental: books and magazines, read on-site",
        why="rent reading materials and read in place",
        readiness="DISCOVERY_READY", clarification_needed=False,
    )
    h.send_user_message(TURN_2)
    return h.current_project_id


class TestPhaseGFullStory:
    def test_full_story_bootstrap_bypass_preview_live(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        # bootstrap_transient=True (default): the FIRST preview attempt
        # (auto-triggered by the completing TURN 2 answer) observes the real
        # transient BUILDING bootstrap state and fails closed.
        pid = _open_project(h)

        # Same internal id; canonical identity + preserved intent.
        assert pid == "tg-555-p1"
        entry = h.registry_entry(pid)
        state = h.project_state(pid)
        assert entry.display_name == "cozyreadingspace"
        assert state.brief["name"] == "cozyreadingspace"
        assert state.brief.get("what") and state.brief.get("why")
        assert [p.project_id for p in h.conversation_registry.projects] == [pid]

        # The auto-build ran build + QA + a preview attempt that failed closed
        # at the transient BUILDING bootstrap (exactly like the real p7
        # evidence): QA succeeded, but NO real content was deployed/delivered.
        assert h.vercel.deploy_calls == 0
        assert state.deployment.get("latest_shown_preview") is None
        assert len(h.telegram.photo_calls) == 0

        # Retry the preview (reconcile the SAME project) -> bootstrap READY.
        result = h.preview.run_owned(pid, h.runner.create_workspace(pid))
        assert result.success, result.error
        state = h.project_state(pid)
        assert state.deployment.get("latest_shown_preview")

        # Vercel project created once under the confirmed slug.
        assert h.vercel.created_projects == ["cozyreadingspace"]
        assert h.vercel.post_calls == 1
        assert h.current_slug == "cozyreadingspace"

        # Bypass provisioned exactly once and NOT stored in ProjectState.
        assert h.vercel.bypass_generation_calls == 1
        assert h.bypass_store.get("prj_1")
        # The secret is absent from the persisted project state file.
        raw = (h.state_root / f"{pid}.json").read_text(encoding="utf-8")
        assert h.bypass_store.get("prj_1") not in raw
        # ...and absent from the conversation registry too.
        reg_raw = (h.state_root / "conversations" / "555.json").read_text(encoding="utf-8")
        assert h.bypass_store.get("prj_1") not in reg_raw

        # Smoke used the project-specific bypass (preview smoke only).
        assert h.smoke.bypass_secrets[-1] == h.bypass_store.get("prj_1")
        # One preview deployment + one Telegram photo.
        assert h.vercel.deploy_calls == 1
        assert len(h.telegram.photo_calls) == 1

        # Revision -> revised preview -> approve -> promote -> LIVE.
        rev = h.run_revision("ganti warna jadi lebih lembut, tetap soft")
        assert rev.success, getattr(rev, "error", None)
        assert h.vercel.deploy_calls == 2
        assert len(h.telegram.photo_calls) == 2
        # No extra bypass generation on revision (reused).
        assert h.vercel.bypass_generation_calls == 1
        # Still exactly one project create.
        assert h.vercel.post_calls == 1

        assert h.approve().success
        promoted = h.promote_now()
        assert promoted.success, promoted.error
        assert h.project_state(pid).lifecycle == "LIVE"
        assert h.vercel.promote_calls == 1

    def test_exact_side_effect_counts(self, tmp_path):
        """No blind duplicates: exact counts for project create POST, bootstrap
        POST, protection-bypass generation PATCH, preview deploy POST,
        Telegram preview delivery, and promote."""
        h = LocalR1Scenario(tmp_path)
        pid = _open_project(h)  # auto-build -> transient bootstrap fail-closed
        assert h.preview.run_owned(pid, h.runner.create_workspace(pid)).success

        assert h.vercel.post_calls == 1                 # project create POST
        assert h.vercel.bootstrap_calls >= 1            # bootstrap attempt(s)
        assert h.vercel.bypass_generation_calls == 1    # bypass PATCH once
        assert h.vercel.deploy_calls == 1               # preview deploy POST
        assert len(h.telegram.photo_calls) == 1         # Telegram preview once

        rev = h.run_revision("ubah hero jadi lebih terang")
        assert rev.success, getattr(rev, "error", None)
        assert h.vercel.deploy_calls == 2               # one more preview deploy
        assert len(h.telegram.photo_calls) == 2         # one more delivery
        assert h.vercel.bypass_generation_calls == 1    # no rotation
        assert h.vercel.post_calls == 1                 # no duplicate create

        assert h.approve().success
        assert h.promote_now().success
        assert h.vercel.promote_calls == 1              # promote once

    def test_restart_recovery_reuses_everything_no_duplicates(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        pid = _open_project(h)  # auto-build hits the transient bootstrap
        assert h.project_state(pid).lifecycle == "PREVIEW_READY"
        # Retry so the preview actually lands (bootstrap now READY).
        assert h.preview.run_owned(pid, h.runner.create_workspace(pid)).success
        assert h.latest_shown_preview(pid)

        before = {
            "create": h.vercel.post_calls,
            "deploy": h.vercel.deploy_calls,
            "bypass": h.vercel.bypass_generation_calls,
            "photos": len(h.telegram.photo_calls),
        }
        secret = h.bypass_store.get("prj_1")
        shown = h.latest_shown_preview(pid)

        # Simulate a process restart over the SAME temp state, then re-drive
        # the preview (as a retry after a previously failed smoke would).
        h.restart()
        h.set_smoke_result(True)
        result = h.preview.run_owned(pid, h.runner.create_workspace(pid))
        assert result.success

        # Same project reused; no duplicate project create / bootstrap POST /
        # bypass generation / preview deploy; Telegram delivered once (the
        # already-shown preview short-circuits, no duplicate photo).
        assert h.current_project_id == pid
        assert h.vercel.post_calls == before["create"]
        assert h.vercel.deploy_calls == before["deploy"]
        assert h.vercel.bypass_generation_calls == before["bypass"]
        assert len(h.telegram.photo_calls) == before["photos"]
        assert h.bypass_store.get("prj_1") == secret
        assert h.latest_shown_preview(pid) == shown

    def test_retry_after_failed_smoke_does_not_rotate_bypass(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        h.set_smoke_result(False)
        # The completing TURN 2 answer auto-builds; the preview FAILS at smoke.
        pid = _open_project(h, bootstrap_transient=False)
        # A smoke failure leaves the lifecycle at PREVIEW_READY but delivers
        # NO preview (fail closed before Telegram).
        assert h.latest_shown_preview(pid) == {}
        assert len(h.telegram.photo_calls) == 0
        secret_first = h.bypass_store.get("prj_1")
        assert secret_first
        assert h.vercel.bypass_generation_calls == 1

        # Retry with working smoke: same bypass secret reused (no rotation),
        # same preview deployment reconciled (no duplicate deploy).
        h.set_smoke_result(True)
        result = h.preview.run_owned(pid, h.runner.create_workspace(pid))
        assert result.success
        assert h.bypass_store.get("prj_1") == secret_first
        assert h.vercel.bypass_generation_calls == 1
        assert h.vercel.deploy_calls == 1
