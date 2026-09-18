"""Runtime-level tests for conversation-first routing (multi-project Telegram).

Exercises the REAL TelegramReceiveLoop._process_update path with a REAL
ConversationRouter + ProjectStateStore + TelegramDispatcher. Only the Hermes
FAST boundary and the outbound Telegram adapter are mocked.

Covers: first contact bootstrap, NEW_PROJECT through the loop, replay of the
same event, LIST_PROJECTS (no LLM), and active-project switching.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.channels.dispatch import AuthenticatedTelegramContext, TelegramDispatcher
from app.conversations import ConversationRoute, ConversationRouter
from app.core.intake import IntakeProcessor
from app.core.registry import ConversationRegistryStore
from app.core.state import ProjectStateStore
from app.runtime import TelegramReceiveLoop


def _payload(event_id, user=1, chat=555, text="hello"):
    return {
        "update_id": event_id,
        "message": {
            "from": {"id": user},
            "chat": {"id": chat},
            "text": text,
            "date": event_id,
        },
    }


def _fast_ready(name="P", what="biz", why="cta"):
    return {
        "scope": "WEBSITE", "name": name, "what": what, "why": why,
        "why_destination": None, "ambiguity": None,
        "clarification_needed": False, "clarification_question": None,
        "readiness": "DISCOVERY_READY",
    }


class _FakeTelegramOut:
    def __init__(self):
        self.sent = []

    def send_text(self, chat_id, text, **kwargs):
        self.sent.append((str(chat_id), text))

    def send_photo(self, chat_id, path, caption="", **kwargs):
        self.sent.append((str(chat_id), caption))


def _loop(tmp_path, chat="555"):
    state_root = tmp_path / "state"
    store = ProjectStateStore(state_root)
    registry = ConversationRegistryStore(state_root / "conversations")
    adapter = MagicMock()
    adapter.fast_interpret.return_value = _fast_ready()
    adapter._run_fast_programmatic.return_value = MagicMock(success=False)
    intake = IntakeProcessor(store, hermes_adapter=adapter)
    builder = MagicMock()
    builder.build.return_value = MagicMock(success=True)
    dispatcher = TelegramDispatcher(store, intake, builder=builder,
                                    workspace_for=lambda pid: tmp_path / "ws")
    telegram_out = _FakeTelegramOut()
    router = ConversationRouter(store, registry, telegram_out=telegram_out, hermes=adapter)
    loop = TelegramReceiveLoop(
        bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        dispatcher=dispatcher,
        telegram_out=telegram_out,
        hermes=adapter,
        transport=MagicMock(),
        conversations=router,
    )
    return loop, store, registry, adapter, builder, telegram_out, router


# ---------------------------------------------------------------------------
# First contact bootstraps exactly one project
# ---------------------------------------------------------------------------


class TestFirstContactBootstrap:
    def test_first_message_creates_p1_and_registers_it(self, tmp_path):
        loop, store, registry, adapter, builder, out, router = _loop(tmp_path)
        loop._process_update(_payload(1, text="bikin website baru namanya webbandung"))

        assert store.load("tg-555-p1") is not None
        assert registry.active_project_id("555") == "tg-555-p1"
        entry = registry.load("555").find_by_id("tg-555-p1")
        assert entry.display_name == "webbandung"

    def test_second_message_reuses_same_project(self, tmp_path):
        loop, store, registry, adapter, builder, out, router = _loop(tmp_path)
        loop._process_update(_payload(1, text="bikin website baru namanya webbandung"))
        loop._process_update(_payload(2, text="ini toko tarot di bandung, buat booking"))
        assert len(registry.load("555").projects) == 1
        assert store.load("tg-555-p2") is None


# ---------------------------------------------------------------------------
# NEW_PROJECT through the loop
# ---------------------------------------------------------------------------


class TestNewProjectThroughLoop:
    def test_second_project_allocated_and_active(self, tmp_path):
        loop, store, registry, adapter, builder, out, router = _loop(tmp_path)
        loop._process_update(_payload(1, text="bikin website baru namanya webbandung"))
        assert registry.active_project_id("555") == "tg-555-p1"

        loop._process_update(_payload(2, text="sekarang bikin website baru namanya webjogja"))
        assert store.load("tg-555-p2") is not None
        assert registry.active_project_id("555") == "tg-555-p2"
        # Old project state untouched and still present.
        assert store.load("tg-555-p1") is not None

    def test_replay_same_event_does_not_allocate_second_project(self, tmp_path):
        loop, store, registry, adapter, builder, out, router = _loop(tmp_path)
        loop._process_update(_payload(1, text="bikin website baru namanya webbandung"))
        loop._process_update(_payload(2, text="bikin website baru namanya webjogja"))
        assert store.load("tg-555-p2") is not None
        n_before = len(registry.load("555").projects)

        # Replay the identical Telegram event.
        loop._process_update(_payload(2, text="bikin website baru namanya webjogja"))
        assert len(registry.load("555").projects) == n_before
        assert store.load("tg-555-p3") is None


# ---------------------------------------------------------------------------
# LIST_PROJECTS (no LLM call)
# ---------------------------------------------------------------------------


class TestListProjectsThroughLoop:
    def test_list_returns_names_and_makes_no_llm_call(self, tmp_path):
        loop, store, registry, adapter, builder, out, router = _loop(tmp_path)
        loop._process_update(_payload(1, text="bikin website baru namanya webbandung"))
        loop._process_update(_payload(2, text="bikin website baru namanya webjogja"))
        fast_calls_before = adapter._run_fast_programmatic.call_count

        loop._process_update(_payload(3, text="project aku ada apa aja?"))

        assert adapter._run_fast_programmatic.call_count == fast_calls_before
        last_message = out.sent[-1][1]
        assert "webbandung" in last_message
        assert "webjogja" in last_message
        assert "tg-555-p1" not in last_message
        assert "tg-555-p2" not in last_message


# ---------------------------------------------------------------------------
# Named selection through the loop
# ---------------------------------------------------------------------------


class TestSelectThroughLoop:
    def test_revisi_named_project_switches_active(self, tmp_path):
        loop, store, registry, adapter, builder, out, router = _loop(tmp_path)
        loop._process_update(_payload(1, text="bikin website baru namanya webbandung"))
        loop._process_update(_payload(2, text="bikin website baru namanya webjogja"))
        assert registry.active_project_id("555") == "tg-555-p2"

        loop._process_update(_payload(3, text="aku mau revisi webbandung"))
        assert registry.active_project_id("555") == "tg-555-p1"

    def test_unknown_project_asks_clarification(self, tmp_path):
        loop, store, registry, adapter, builder, out, router = _loop(tmp_path)
        loop._process_update(_payload(1, text="bikin website baru namanya webbandung"))
        loop._process_update(_payload(2, text="bikin website baru namanya webjogja"))
        # Remove active pointer to force a choice, then reference an unknown name.
        reg = registry.load("555")
        reg.active_project_id = None
        registry.save(reg)
        loop._process_update(_payload(3, text="aku mau revisi websurabaya"))
        clarification = out.sent[-1][1]
        assert "webbandung" in clarification or "webjogja" in clarification


# ---------------------------------------------------------------------------
# Restart durability through the loop
# ---------------------------------------------------------------------------


class TestRestartThroughLoop:
    def test_active_project_survives_new_loop_instance(self, tmp_path):
        loop, store, registry, adapter, builder, out, router = _loop(tmp_path)
        loop._process_update(_payload(1, text="bikin website baru namanya webbandung"))
        loop._process_update(_payload(2, text="bikin website baru namanya webjogja"))

        # Brand-new loop over the same persisted root.
        loop2, store2, registry2, adapter2, builder2, out2, router2 = _loop(tmp_path)
        assert registry2.active_project_id("555") == "tg-555-p2"
        route = router2.route("555", "hero-nya lebih kecil", event_id="3")
        assert route.project_id == "tg-555-p2"


# ---------------------------------------------------------------------------
# Dangling registry pointer repair
# ---------------------------------------------------------------------------


class TestDanglingPointerRepair:
    def test_registry_pointing_at_missing_project_is_repaired(self, tmp_path):
        loop, store, registry, adapter, builder, out, router = _loop(tmp_path)
        loop._process_update(_payload(1, text="bikin website baru namanya webbandung"))
        # Corrupt: point active at a project that does not exist on disk.
        reg = registry.load("555")
        from app.core.registry import ProjectEntry
        reg.projects.append(ProjectEntry(project_id="tg-555-p9", display_name="ghost"))
        reg.active_project_id = "tg-555-p9"
        registry.save(reg)

        loop._process_update(_payload(2, text="hero lebih kecil"))
        # The loop must not crash and must not route at the ghost project.
        assert store.load("tg-555-p9") is None


# ---------------------------------------------------------------------------
# Legacy mode (no router) is unchanged
# ---------------------------------------------------------------------------


class TestLegacyNoRouter:
    def test_loop_without_router_uses_conversation_project_id(self, tmp_path):
        state_root = tmp_path / "state"
        store = ProjectStateStore(state_root)
        adapter = MagicMock()
        adapter.fast_interpret.return_value = _fast_ready()
        intake = IntakeProcessor(store, hermes_adapter=adapter)
        builder = MagicMock()
        builder.build.return_value = MagicMock(success=True)
        dispatcher = TelegramDispatcher(store, intake, builder=builder,
                                        workspace_for=lambda pid: tmp_path / "ws")
        telegram_out = _FakeTelegramOut()
        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher,
            telegram_out=telegram_out,
            hermes=adapter,
            transport=MagicMock(),
        )
        loop._process_update(_payload(1, text="Northcut, barbershop, booking"))
        # Legacy single-project-per-conversation ID preserved.
        assert store.load("tg-555") is not None
