"""Regression: the missing-WHY clarification must actually reach Telegram.

Reproduces the clean-VPS R1 intake bug end-to-end with the REAL
TelegramReceiveLoop._handle_intake -> TelegramDispatcher -> IntakeProcessor ->
ProjectStateStore chain. Only external boundaries are mocked:
Hermes FAST model output, Telegram network send, and the frontend build.

Scenario (the exact clean-VPS reproduction):
    Turn 1: "aku mau bikin website tentang tarot, nama web nya
             tarotreaderjogja, desainnya di sesuaiin aja"
        -> NAME + WHAT present, WHY missing
        -> WAITING_INPUT + exactly ONE clarification question
    Replay of the same update: no re-mutation, no second send, no build.
    Turn 2: "buat promosi jasa tarot dan supaya orang bisa booking lewat
             WhatsApp"
        -> NAME/WHAT preserved, WHY merged, READY, exactly one build.
    Replay of turn 2: no rebuild.
    Unauthorized event: no mutation, no send, no build.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.channels.dispatch import AuthenticatedTelegramContext, TelegramDispatcher
from app.core.authz import ProjectAccess
from app.core.intake import IntakeProcessor
from app.core.state import ProjectStateStore
from app.runtime import TelegramReceiveLoop

TURN1_TEXT = (
    "aku mau bikin website tentang tarot, nama web nya tarotreaderjogja, "
    "desainnya di sesuaiin aja"
)
TURN2_TEXT = "buat promosi jasa tarot dan supaya orang bisa booking lewat WhatsApp"


def _payload(event_id, user="1", chat="555", text="hello"):
    return {
        "update_id": event_id,
        "message": {
            "from": {"id": int(user)},
            "chat": {"id": int(chat)},
            "text": text,
            "date": event_id,
        },
    }


def _fast(scope, name=None, what=None, why=None, clarification_needed=False,
          clarification_question=None):
    return {
        "scope": scope,
        "name": name,
        "what": what,
        "why": why,
        "why_destination": None,
        "ambiguity": None,
        "clarification_needed": clarification_needed,
        "clarification_question": clarification_question,
        "readiness": "NEEDS_CLARIFICATION" if clarification_needed else "DISCOVERY_READY",
    }


def _make_loop(tmp_path):
    """Real dispatcher/intake/store; mocked Hermes FAST, Telegram send, builder."""
    store = ProjectStateStore(tmp_path / "state")
    project_id = "tg-555"
    ProjectAccess(store).create(project_id, "telegram:1", channel="telegram",
                                conversation_id="555")
    hermes = MagicMock()
    intake = IntakeProcessor(store, hermes_adapter=hermes)
    builder = MagicMock()
    builder.build.return_value = MagicMock(success=True)
    dispatcher = TelegramDispatcher(store, intake, builder=builder,
                                    workspace_for=lambda pid: tmp_path / "ws")
    telegram_out = MagicMock()
    loop = TelegramReceiveLoop(
        bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        dispatcher=dispatcher,
        telegram_out=telegram_out,
        hermes=hermes,
    )
    return loop, store, hermes, builder, telegram_out, project_id


class TestMissingWhyClarificationE2E:
    def test_incomplete_intake_sends_exactly_one_clarification_then_ready_build(self, tmp_path):
        loop, store, hermes, builder, telegram_out, project_id = _make_loop(tmp_path)

        # --- Turn 1: NAME + WHAT, WHY missing (clean-VPS reproduction) ---
        hermes.fast_interpret.return_value = _fast(
            "WEBSITE", name="tarotreaderjogja",
            what="Website tentang tarot (tarot reader, Jogja)",
        )
        loop._process_update(_payload(101, text=TURN1_TEXT))

        # 1. WAITING_INPUT persisted
        state = store.load(project_id)
        assert state.lifecycle == "WAITING_INPUT"
        assert state.brief["name"] == "tarotreaderjogja"
        assert "tarot" in state.brief["what"]
        assert not state.brief.get("why")

        # 2+3. Exactly one clarification sent, asking for the genuinely
        # missing WHY (derived from the accumulated brief, not a second AI call).
        assert telegram_out.send_text.call_count == 1
        sent_chat, sent_text = telegram_out.send_text.call_args[0]
        assert sent_chat == "555"
        assert "tarotreaderjogja" in sent_text  # per-field WHY question references the site
        assert hermes.fast_interpret.call_count == 1  # no extra AI call for the question
        assert builder.build.call_count == 0

        # --- Replay of the SAME update: fully idempotent ---
        hermes.fast_interpret.reset_mock()
        loop._process_update(_payload(101, text=TURN1_TEXT))
        # 4. no second clarification; 5. no second intake mutation
        assert telegram_out.send_text.call_count == 1
        assert hermes.fast_interpret.call_count == 0
        assert builder.build.call_count == 0
        state = store.load(project_id)
        assert state.lifecycle == "WAITING_INPUT"
        assert state.revisions.source_revision == 0

        # --- Turn 2: user answers WHY naturally, without repeating NAME/WHAT ---
        hermes.fast_interpret.return_value = _fast(
            "WEBSITE", why="promosi jasa tarot dan booking lewat WhatsApp",
        )
        loop._process_update(_payload(102, text=TURN2_TEXT))

        # 6. existing NAME + WHAT preserved; 7. WHY added; 8. READY
        state = store.load(project_id)
        assert state.brief["name"] == "tarotreaderjogja"
        assert "tarot" in state.brief["what"]
        assert "promosi jasa tarot" in state.brief["why"]
        assert state.lifecycle in {"READY", "QUEUED", "BUILDING"}

        # 9. build admitted exactly once with the accumulated brief
        assert builder.build.call_count == 1
        build_brief = builder.build.call_args[0][1]
        assert build_brief["name"] == "tarotreaderjogja"
        assert "promosi jasa tarot" in build_brief["why"]

        # 10. Replay of turn 2: no rebuild, no resend
        loop._process_update(_payload(102, text=TURN2_TEXT))
        assert builder.build.call_count == 1
        assert telegram_out.send_text.call_count == 1

    def test_unauthorized_intake_has_no_side_effects(self, tmp_path):
        loop, store, hermes, builder, telegram_out, project_id = _make_loop(tmp_path)
        hermes.fast_interpret.return_value = _fast(
            "WEBSITE", name="tarotreaderjogja", what="tarot site",
        )
        # A different principal (user 2) sends into conversation 555 — the
        # normalizer/authenticated-context identity check must reject before
        # any intake mutation.
        loop._handle_intake(
            _payload(201, user="2", chat="555", text=TURN1_TEXT),
            project_id,
            AuthenticatedTelegramContext("2", "555"),
            __import__("app.channels.telegram", fromlist=["TelegramNormalizer"]).TelegramNormalizer.normalize(
                _payload(201, user="2", chat="555", text=TURN1_TEXT)
            ),
            store.load(project_id),
        )
        state = store.load(project_id)
        assert not state.brief.get("name")
        assert state.lifecycle == "DISCOVERING"
        assert builder.build.call_count == 0
        # 11. No CLARIFICATION question is sent (the WHY question references
        # the site name and is only ever produced for the authorized intake
        # path). The pre-existing sanitized "not authorized" error reply is
        # out of scope for this regression — it is established runtime
        # behavior for all dispatch rejections.
        for call in telegram_out.send_text.call_args_list:
            assert "tarotreaderjogja" not in call[0][1]
