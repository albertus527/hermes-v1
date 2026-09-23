"""Failure-injection matrix for the local R1 simulation harness.

Each row is one scenario from the required matrix, driven through the REAL
production control flow with only external boundaries faked. Rows that need a
specific prior step (crash/recovery) perform the REAL prior steps, inject the
failure at the target boundary, then build a NEW runtime over the SAME temp
state and verify recovery.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from r1_harness import (  # noqa: E402
    LocalR1Scenario,
    patch_qa_boundaries,
    make_preview_ready,
)


@pytest.fixture(autouse=True)
def _qa_boundaries(monkeypatch):
    patch_qa_boundaries(monkeypatch)


LONG_DESCRIPTION = (
    "aku mau bikin web untuk membaca, jadi disitu bisa sewa bahan bacaan, "
    "soft tone dan bright"
)


def _seed_preview_ready(h, name="kitsunereading"):
    pid = h.seed_project(name)
    ws = h.runner.create_workspace(pid)
    make_preview_ready(h.store, pid, ws, source_revision=1)
    return pid, ws


# --- 1. long description without explicit name -----------------------------
def test_matrix_01_long_description_asks_name(tmp_path):
    h = LocalR1Scenario(tmp_path)
    h.set_router_decision("CREATE_PROJECT", confidence="high",
                          proposed_new_project_name=None)
    h.send_user_message(LONG_DESCRIPTION)
    assert h.conversation_registry.projects == []
    assert h.pending_action == {"action": "CREATE_PROJECT", "awaiting": "NAME"}
    assert h.vercel_calls["create_post"] == 0


# --- 2. clarification answer ------------------------------------------------
def test_matrix_02_clarification_answer_propagates(tmp_path):
    h = LocalR1Scenario(tmp_path)
    h.set_router_decision("CREATE_PROJECT", confidence="high",
                          proposed_new_project_name=None)
    h.send_user_message(LONG_DESCRIPTION)
    h.set_intake_response(name="kitsunereading", what="r", why="r",
                          readiness="DISCOVERY_READY", clarification_needed=False)
    h.send_user_message("kitsunereading")
    reg = h.conversation_registry
    assert [p.project_id for p in reg.projects] == ["tg-555-p1"]
    assert reg.display_name_for("tg-555-p1") == "kitsunereading"


# --- 3. registry name collision --------------------------------------------
def test_matrix_03_registry_name_collision(tmp_path):
    h = LocalR1Scenario(tmp_path)
    h.seed_project("kitsunereading")
    before = h.project_state().to_dict()
    h.set_router_decision("CREATE_PROJECT", confidence="high",
                          proposed_new_project_name="kitsunereading")
    h.send_user_message("buat website baru namanya kitsunereading")
    reg = h.conversation_registry
    assert len(reg.projects) == 1
    assert h.project_state().to_dict() == before
    assert any("sudah punya project" in t for _, t in h.telegram.text_calls)


# --- 4/5/6. Vercel create 201 / 400 / 409 ----------------------------------
def test_matrix_04_vercel_create_201_success(tmp_path):
    h = LocalR1Scenario(tmp_path)
    pid, ws = _seed_preview_ready(h)
    h.set_vercel_behavior(get_status=404, post_status=201)
    assert h.preview.run_owned(pid, ws).success
    assert h.vercel_calls["create_post"] == 1


def test_matrix_05_vercel_create_400_confirmed_failure(tmp_path):
    h = LocalR1Scenario(tmp_path)
    pid, ws = _seed_preview_ready(h)
    h.set_vercel_behavior(get_status=404, post_status=400)
    result = h.preview.run_owned(pid, ws)
    assert not result.success and result.error_code == "PROJECT_CREATE_REJECTED"
    assert h.vercel_calls["create_post"] == 1


def test_matrix_06_vercel_create_409_proven_collision(tmp_path):
    h = LocalR1Scenario(tmp_path)
    pid, ws = _seed_preview_ready(h)
    h.set_vercel_behavior(get_status=404, post_status=409)
    result = h.preview.run_owned(pid, ws)
    assert not result.success and result.error_code == "SLUG_COLLISION"
    assert h.vercel_calls["create_post"] == 1


# --- 7. Vercel create timeout after possible acceptance --------------------
def test_matrix_07_vercel_create_timeout_ambiguous(tmp_path):
    h = LocalR1Scenario(tmp_path)
    pid, ws = _seed_preview_ready(h)
    h.set_vercel_behavior(get_status=404, post_raises=TimeoutError("timeout"))
    result = h.preview.run_owned(pid, ws)
    assert not result.success and result.error_code == "AMBIGUOUS_PROJECT_CREATE"
    assert h.vercel_calls["create_post"] == 1


# --- 8. Vercel project lookup found ---------------------------------------
def test_matrix_08_lookup_found_after_ambiguity(tmp_path):
    h = LocalR1Scenario(tmp_path)
    pid, ws = _seed_preview_ready(h)
    h.set_vercel_behavior(get_status=404, post_raises=TimeoutError("t"))
    assert not h.preview.run_owned(pid, ws).success
    h.restart()
    h.set_vercel_behavior(post_raises=None, get_status=200)
    assert h.preview.run_owned(pid, ws).success
    assert h.vercel_calls["create_post"] == 1


# --- 9. Vercel lookup absent ----------------------------------------------
def test_matrix_09_lookup_absent_fails_closed(tmp_path):
    h = LocalR1Scenario(tmp_path)
    pid, ws = _seed_preview_ready(h)
    # Project create succeeds, but the deployment readiness lookup reports a
    # terminal failure -> preview fails closed (no delivery).
    h.set_vercel_behavior(get_status=404, post_status=201)
    h.vercel.lookup_deployment = "error"
    result = h.preview.run_owned(pid, ws)
    assert not result.success
    assert h.telegram_calls["photo"] == 0


# --- 10. Vercel lookup malformed/inconclusive -----------------------------
def test_matrix_10_lookup_malformed_no_blind_create(tmp_path):
    h = LocalR1Scenario(tmp_path)
    pid, ws = _seed_preview_ready(h)
    h.set_vercel_behavior(get_status=500)  # neither 404 nor a valid 200
    result = h.preview.run_owned(pid, ws)
    assert not result.success
    assert h.vercel_calls["create_post"] == 0


# --- 11. bind_slug persistence failure ------------------------------------
def test_matrix_11_bind_slug_failure_fails_closed(tmp_path):
    h = LocalR1Scenario(tmp_path)
    pid, ws = _seed_preview_ready(h)
    h.set_vercel_behavior(get_status=404, post_status=201)
    h.bind_slug_raises = RuntimeError("write failed")
    result = h.preview.run_owned(pid, ws)
    assert not result.success and result.error_code == "SLUG_BIND_PERSIST_FAILED"
    assert h.vercel_calls["deploy_post"] == 0
    assert h.telegram_calls["photo"] == 0


# --- 12. duplicate CREATE using existing name -----------------------------
def test_matrix_12_duplicate_create_no_switch_no_mutation(tmp_path):
    h = LocalR1Scenario(tmp_path)
    h.seed_project("kitsunereading")
    before = h.project_state().to_dict()
    h.set_router_decision("CREATE_PROJECT", confidence="high",
                          proposed_new_project_name="kitsunereading")
    h.send_user_message("bikin website baru namanya kitsunereading")
    assert len(h.conversation_registry.projects) == 1
    assert h.project_state().to_dict() == before
    assert h.vercel_calls["create_post"] == 0


# --- 13. pending CREATE_PROJECT + WAITING_INPUT overlap -------------------
def test_matrix_13_pending_name_vs_waiting_input(tmp_path):
    h = LocalR1Scenario(tmp_path)
    pid = h.seed_project("alpha")
    with h.store.acquire_writer(pid) as state:
        state.brief = {}
        state.lifecycle = "WAITING_INPUT"
        h.store.save(state)
    h.registry.set_pending_action(h.chat_id, {"action": "CREATE_PROJECT",
                                              "awaiting": "NAME"})
    h.set_intake_response(name="Alpha Reading", what="rental", why="rent",
                          readiness="DISCOVERY_READY", clarification_needed=False)
    h.send_user_message("tempat sewa buku bacaan di bandung")
    assert len(h.conversation_registry.projects) == 1
    assert h.project_state(pid).brief.get("name") == "Alpha Reading"


# --- 14/15. revision crash after preview SENT + restart recovery ----------
def test_matrix_14_15_revision_crash_after_preview_then_restart(tmp_path):
    h = LocalR1Scenario(tmp_path)
    h.set_router_decision("CREATE_PROJECT", confidence="high",
                          proposed_new_project_name="kitsunereading")
    h.set_intake_response(name="kitsunereading", what="r", why="r",
                          readiness="DISCOVERY_READY", clarification_needed=False)
    h.send_user_message("bikin website baru namanya kitsunereading")
    pid = h.current_project_id
    h.run_build_and_preview(pid)
    photos0 = h.telegram_calls["photo"]

    original = h.preview.run_owned
    armed = {"v": True}

    def hook(project_id, workspace, *, slot_held=False):
        r = original(project_id, workspace, slot_held=slot_held)
        if r.success and armed["v"]:
            armed["v"] = False
            raise RuntimeError("crash after delivery")
        return r

    h.preview.run_owned = hook
    with pytest.raises(RuntimeError):
        h.run_revision("ganti warna")

    # Crash window durable state.
    assert h.revision_counters(pid)["revision_seq"] == 0
    assert h.revision_counters(pid)["queued_revision_seq"] == 1
    assert h.telegram_calls["photo"] == photos0 + 1

    # Restart over the SAME state and recover exactly once, no re-send.
    h.restart()
    recovered = h.revise.apply(pid, 1, "ganti warna", principal_id=h.principal_id)
    assert recovered.success
    assert h.revision_counters(pid)["revision_seq"] == 1
    assert h.telegram_calls["photo"] == photos0 + 1
    seqs = sorted(e["seq"] for e in h.pending_revisions(pid))
    assert seqs == list(range(1, len(seqs) + 1))


# --- 16. normal happy path after all fixes --------------------------------
def test_matrix_16_happy_path_after_fixes(tmp_path):
    h = LocalR1Scenario(tmp_path)
    h.set_router_decision("CREATE_PROJECT", confidence="high",
                          proposed_new_project_name="kitsunereading")
    h.set_intake_response(name="kitsunereading", what="r", why="r",
                          readiness="DISCOVERY_READY", clarification_needed=False)
    h.send_user_message("bikin website baru namanya kitsunereading")
    pid = h.current_project_id
    h.run_build_and_preview(pid)
    assert h.current_lifecycle == "PREVIEW_READY"
    assert h.vercel_calls["create_post"] == 1
    assert h.telegram_calls["photo"] == 1
    assert h.current_slug == "kitsunereading"
    assert h.latest_shown_preview(pid)["source_revision"] == \
        h.revision_counters(pid)["source_revision"]
