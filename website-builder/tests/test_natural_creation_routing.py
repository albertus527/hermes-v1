"""Regression tests for natural-language NEW_PROJECT routing.

A real Telegram E2E exposed a HIGH wrong-project routing bug: with an
existing READY project (tarotreaderjogja) active, the natural creation
message

    "aku mau bikin website tentang cafe, nama web nya thedailybake
     desainnya di sesuaiin aja"

was routed as INTAKE into the legacy project, silently mutating its brief
with a hybrid cafe/tarot state. Root causes:

1. Deterministic NEW_PROJECT detection required the literal word
   "baru"/"new".
2. ``ConversationRouter._fast_target_name()`` asked FAST for an intent but
   discarded it, so FAST could not rescue a natural creation request.

These tests pin the fix:

* natural creation (ID + EN, no "baru") routes NEW_PROJECT with a fresh
  immutable ID, leaving the old project's state byte-for-byte untouched;
* revision / select / correction turns never route NEW_PROJECT;
* nameless natural creation asks for a name and allocates nothing;
* malformed FAST output never creates a project;
* FAST NEW_PROJECT naming an existing project clarifies, never duplicates.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.channels.dispatch import AuthenticatedTelegramContext, TelegramDispatcher
from app.conversations import ConversationRoute, ConversationRouter
from app.core.intake import IntakeProcessor
from app.core.registry import ConversationRegistryStore
from app.core.state import ProjectStateStore


# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_conversations.py conventions)
# ---------------------------------------------------------------------------


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


def _fast_ready(scope="WEBSITE", name="P", what="biz", why="cta"):
    return {
        "scope": scope,
        "name": name,
        "what": what,
        "why": why,
        "why_destination": None,
        "ambiguity": None,
        "clarification_needed": False,
        "clarification_question": None,
        "readiness": "DISCOVERY_READY",
    }


def _make(tmp_path, chat="555"):
    state_root = tmp_path / "state"
    store = ProjectStateStore(state_root)
    registry = ConversationRegistryStore(state_root / "conversations")
    adapter = MagicMock()
    intake = IntakeProcessor(store, hermes_adapter=adapter)
    builder = MagicMock()
    builder.build.return_value = MagicMock(success=True)
    dispatcher = TelegramDispatcher(store, intake, builder=builder,
                                    workspace_for=lambda pid: tmp_path / "ws")
    router = ConversationRouter(store, registry, telegram_out=MagicMock())
    auth = AuthenticatedTelegramContext("1", str(chat))
    return store, registry, adapter, builder, dispatcher, router, auth


def _create_via_router(router, dispatcher, payload, conversation_id, display_name,
                       auth, event_id=None):
    entry = router.materialize_new_project(
        conversation_id, display_name, event_id=event_id or payload["update_id"]
    )
    dispatcher.dispatch(payload, entry.project_id, "create", authenticated=auth)
    return entry


def _snapshot(state):
    """Byte-stable snapshot of the mutable parts of a project state."""
    return {
        "lifecycle": state.lifecycle,
        "brief": dict(state.brief),
        "requirements_version": state.revisions.requirements_version,
        "source_revision": state.revisions.source_revision,
        "processed_events": sorted(state.processed_events),
        "dispatch_events": sorted(state.dispatch_events),
    }


class _FastStub:
    """Hermes stub whose _run_fast_programmatic returns a fixed response."""

    def __init__(self, response):
        self._response = response
        self.calls = 0

    def _run_fast_programmatic(self, **kwargs):
        self.calls += 1
        return MagicMock(success=True, response=self._response)


# ---------------------------------------------------------------------------
# 1. Real-incident repro: READY tarot project + natural cafe creation
# ---------------------------------------------------------------------------


class TestRealIncidentNaturalCreation:
    def test_ready_tarot_project_plus_cafe_request_routes_new_project(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready(
            name="tarotreaderjogja", what="Sesi tarot", why="booking"
        )
        tarot = _create_via_router(router, dispatcher, _payload(1), "555",
                                   "tarotreaderjogja", auth)
        dispatcher.dispatch(_payload(2, text="tarot jogja, sesi tarot, booking"),
                            tarot.project_id, "intake", authenticated=auth)
        before = _snapshot(store.load(tarot.project_id))

        route = router.route(
            "555",
            "aku mau bikin website tentang cafe, nama web nya thedailybake "
            "desainnya di sesuaiin aja",
            event_id="3",
        )

        assert route.route == ConversationRoute.NEW_PROJECT
        assert route.entry.display_name == "thedailybake"
        assert route.entry.project_id != tarot.project_id

        # Materialize and confirm a NEW immutable identity, and that the old
        # project is byte-for-byte untouched.
        cafe = _create_via_router(router, dispatcher, _payload(3), "555",
                                  route.entry.display_name, auth, event_id="3")
        assert cafe.project_id != tarot.project_id
        after = _snapshot(store.load(tarot.project_id))
        assert after == before
        assert registry.active_project_id("555") == cafe.project_id

    @pytest.mark.parametrize(
        "text,expected_name",
        [
            ("buat website coffee shop namanya kopikita", "kopikita"),
            ("bikin web untuk portfolio, nama webnya albertfolio", "albertfolio"),
            ("aku mau bikin website tentang cafe, nama web nya thedailybake",
             "thedailybake"),
        ],
    )
    def test_indonesian_natural_creation_variants(self, tmp_path, text, expected_name):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)

        route = router.route("555", text, event_id="2")
        assert route.route == ConversationRoute.NEW_PROJECT
        assert route.entry.display_name == expected_name


# ---------------------------------------------------------------------------
# 2. English natural creation -> NEW_PROJECT
# ---------------------------------------------------------------------------


class TestEnglishNaturalCreation:
    def test_create_a_website_called_routes_new_project(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)

        route = router.route("555", "create a website called Daily Bake", event_id="2")
        assert route.route == ConversationRoute.NEW_PROJECT
        assert route.entry.display_name == "Daily Bake"

    def test_make_a_website_for_my_cafe_routes_new_project(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)

        route = router.route("555", "make a website for my cafe, called kopiku",
                             event_id="2")
        assert route.route == ConversationRoute.NEW_PROJECT
        assert route.entry.display_name == "kopiku"


# ---------------------------------------------------------------------------
# 3. Revision/select/correction turns must NEVER route NEW_PROJECT
# ---------------------------------------------------------------------------


class TestNonCreationTurnsStayProject:
    @pytest.mark.parametrize(
        "text",
        [
            "revisi webbandung",
            "ubah hero webjogja",
            "hero-nya kecilin",
            "lanjut webjogja",
        ],
    )
    def test_revision_and_select_never_new_project(self, tmp_path, text):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        p1 = _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        p2 = _create_via_router(router, dispatcher, _payload(2), "555", "webjogja", auth)
        n_projects = len(registry.load("555").projects)

        route = router.route("555", text, event_id="3")
        assert route.route == ConversationRoute.PROJECT
        # No project was allocated.
        assert len(registry.load("555").projects) == n_projects

    def test_name_correction_of_existing_project_never_new_project(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        tarot = _create_via_router(router, dispatcher, _payload(1), "555",
                                   "tarotreaderjogja", auth)
        n_projects = len(registry.load("555").projects)

        route = router.route(
            "555", "sebenarnya nama tarotreaderjogja jadi Tarot Jogja", event_id="2"
        )
        assert route.route != ConversationRoute.NEW_PROJECT
        assert len(registry.load("555").projects) == n_projects


# ---------------------------------------------------------------------------
# 4. Nameless natural creation -> ask for a name, allocate nothing
# ---------------------------------------------------------------------------


class TestNamelessNaturalCreation:
    def test_aku_mau_bikin_website_cafe_asks_for_name(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        n_projects = len(registry.load("555").projects)

        route = router.route("555", "aku mau bikin website cafe", event_id="2")
        assert route.route == ConversationRoute.CLARIFICATION
        assert route.clarification is not None
        assert "nama" in route.clarification.lower()
        # Nothing allocated while waiting for the name.
        assert len(registry.load("555").projects) == n_projects
        assert store.load("tg-555-p2") is None


# ---------------------------------------------------------------------------
# 5. Malformed/ambiguous FAST output must never create a project
# ---------------------------------------------------------------------------


class TestMalformedFastNeverCreates:
    @pytest.mark.parametrize(
        "response",
        [
            "not json at all",
            "```json\n{}\n```",
            '{"intent": "EXPLODE", "target_project_name": null}',
            '["INTAKE", "webbandung"]',
            '{"intent": 42}',
            "",
        ],
    )
    def test_malformed_fast_falls_back_to_active_project(self, tmp_path, response):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        p1 = _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        router.hermes = _FastStub(response)
        n_projects = len(registry.load("555").projects)

        # Contains project cues ("web") so FAST would be consulted, but no
        # creation phrasing — so the ONLY way a new project could appear is a
        # bad FAST decision leaking through.
        route = router.route("555", "gimana progress web aku?", event_id="2")
        assert route.route in (ConversationRoute.PROJECT, ConversationRoute.CLARIFICATION)
        assert route.route != ConversationRoute.NEW_PROJECT
        assert len(registry.load("555").projects) == n_projects
        assert store.load("tg-555-p2") is None

    def test_fast_exception_never_creates(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        hermes = MagicMock()
        hermes._run_fast_programmatic.side_effect = RuntimeError("boom")
        router.hermes = hermes
        n_projects = len(registry.load("555").projects)

        route = router.route("555", "gimana progress web aku?", event_id="2")
        assert route.route != ConversationRoute.NEW_PROJECT
        assert len(registry.load("555").projects) == n_projects


# ---------------------------------------------------------------------------
# 6. FAST NEW_PROJECT naming an existing project -> clarify, never duplicate
# ---------------------------------------------------------------------------


class TestFastNewProjectNamingExisting:
    def test_fast_new_project_with_existing_name_clarifies(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        p1 = _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        router.hermes = _FastStub(
            '{"intent": "NEW_PROJECT", "target_project_name": "webbandung"}'
        )
        n_projects = len(registry.load("555").projects)

        # "web" cues FAST; deterministic natural creation must NOT fire here
        # (no create verb) so the FAST path is what is exercised.
        route = router.route("555", "web aku yang itu gimana ya", event_id="2")
        assert route.route != ConversationRoute.NEW_PROJECT
        assert len(registry.load("555").projects) == n_projects
        assert store.load("tg-555-p2") is None

    def test_fast_new_project_without_name_creates_via_app_code(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        p1 = _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        # FAST correctly identifies intent; app code still allocates the ID.
        router.hermes = _FastStub(
            '{"intent": "NEW_PROJECT", "target_project_name": null}'
        )

        route = router.route("555", "buat website coffee shop namanya kopikita",
                             event_id="2")
        assert route.route == ConversationRoute.NEW_PROJECT
        # Name extracted deterministically, not from FAST.
        assert route.entry.display_name == "kopikita"
        assert route.entry.project_id != p1.project_id

    def test_fast_intent_alone_never_allocates_when_deterministic_name_missing(
        self, tmp_path
    ):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        router.hermes = _FastStub(
            '{"intent": "NEW_PROJECT", "target_project_name": null}'
        )
        n_projects = len(registry.load("555").projects)

        route = router.route("555", "aku mau bikin website cafe", event_id="2")
        assert route.route == ConversationRoute.CLARIFICATION
        assert "nama" in (route.clarification or "").lower()
        assert len(registry.load("555").projects) == n_projects
