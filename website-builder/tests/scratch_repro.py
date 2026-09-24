"""Scratch reproduction: smoke failure -> subsequent Telegram turns loop."""
import json

import pytest

from tests.r1_harness import LocalR1Scenario, patch_qa_boundaries


def _full_brief():
    return {"name": "tokobunga", "what": "toko bunga online", "why": "jualan bunga"}


def test_repro_smoke_failure_loop(tmp_path, monkeypatch):
    patch_qa_boundaries(monkeypatch)
    sc = LocalR1Scenario(tmp_path)
    # Seed a fresh FE result so build produces a site.
    sc.set_frontend_result({"success": True})
    pid = sc.seed_project("tokobunga", brief=_full_brief())
    sc.vercel.deploy_calls = 0

    # Turn 1: build (auto) -> QA -> preview -> smoke FAILS
    sc.set_smoke_result(False)
    sc.run_build_and_preview(pid)
    sc.project_state(pid)
    st = sc.store.load(pid)
    print("AFTER BUILD lifecycle=", st.lifecycle)
    print("latest_shown_preview=", st.deployment.get("latest_shown_preview"))
    print("preview_intent=", json.dumps(st.deployment.get("preview_intent"), default=str))
    print("tested_snapshot?", bool(st.deployment.get("tested_snapshot")))
    print("dispatch_events=", {k: v.get("status") for k, v in st.dispatch_events.items()})

    # Turn 2: user sends a message; smoke still failing
    sc.send_user_message("halo")
    st2 = sc.store.load(pid)
    print("AFTER TURN2 lifecycle=", st2.lifecycle)
    print("deploy_calls=", sc.vercel.deploy_calls)
    print("telegram=", sc.telegram_calls)
    print("msg tail:", [t[1][:60] for t in sc.telegram.text_calls][-3:])

    # Turn 3
    sc.send_user_message("gimana?")
    st3 = sc.store.load(pid)
    print("AFTER TURN3 lifecycle=", st3.lifecycle)
    print("deploy_calls=", sc.vercel.deploy_calls)
    print("msg tail:", [t[1][:80] for t in sc.telegram.text_calls][-3:])
