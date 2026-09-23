"""Phase 2 — happy-path baseline for the local R1 simulation harness.

Proves the harness can execute one normal scenario end to end against the REAL
production control flow with only external boundaries faked.
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


def _full_intake(h: LocalR1Scenario, name: str):
    h.set_intake_response(name=name, what="reading rental", why="rent books",
                          readiness="DISCOVERY_READY", clarification_needed=False)


class TestHappyPathBaseline:
    def test_normal_scenario_reaches_preview_ready(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        # Turn 1: descriptive create request, no explicit name.
        h.set_router_decision("CREATE_PROJECT", confidence="high",
                              proposed_new_project_name=None)
        h.send_user_message("aku mau bikin web untuk membaca, soft tone dan bright")

        # The router must ask for a name (no identity derived from the body).
        assert h.registry.active_project_id(h.chat_id) is None
        assert any("nama" in t.lower() for _, t in h.telegram.text_calls)

        # Turn 2: the user answers with the explicit name.
        h._event_seq += 1
        h.set_intake_response(name="kitsunereading", what="reading rental",
                              why="rent books", readiness="DISCOVERY_READY",
                              clarification_needed=False)
        h.send_user_message("kitsunereading")

        entry = h.registry_entry()
        assert entry is not None
        assert entry.display_name == "kitsunereading"

        # Run the real build -> QA -> preview pipeline.
        h.run_build_and_preview()

        state = h.project_state()
        assert state is not None
        assert state.brief.get("name") == "kitsunereading"
        assert state.lifecycle == "PREVIEW_READY"
        assert state.revisions.source_revision == state.revisions.preview_revision

        # Exactly one Vercel project create, one preview delivery.
        assert h.vercel.post_calls == 1
        assert h.vercel.created_projects == ["kitsunereading"]
        assert len(h.telegram.photo_calls) == 1
        assert entry.vercel_slug == "kitsunereading"

    def test_harness_uses_real_persistence(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        h.set_router_decision("CREATE_PROJECT", confidence="high",
                              proposed_new_project_name="kitsunereading")
        h.set_intake_response(name="kitsunereading", what="rental", why="rent",
                              readiness="DISCOVERY_READY", clarification_needed=False)
        h.send_user_message("bikin website baru namanya kitsunereading")

        # Registry file must actually exist on disk (real JSON persistence).
        conv_file = h.state_root / "conversations" / f"{h.chat_id}.json"
        assert conv_file.exists()

        # A brand-new harness over the same root sees the same persisted truth.
        h2 = LocalR1Scenario(tmp_path)
        assert h2.registry.active_project_id(h.chat_id) == h.current_project_id
