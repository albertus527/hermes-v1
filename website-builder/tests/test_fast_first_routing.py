"""Regression tests for FAST-first conversation routing.

Architecture under test (see app/conversations.py):

    User message
        -> ConversationRouter._fast_classify_turn (FAST, EVERY turn, no
           keyword pre-filter gating whether FAST gets to interpret)
        -> CREATE_PROJECT | PROJECT_TURN | LIST_PROJECTS | AMBIGUOUS
           + target_project_name / proposed_new_project_name / confidence
        -> deterministic registry/project resolution (this module)
        -> if PROJECT_TURN: runtime.py::_classify_intent decides
           INTAKE | REVISE | APPROVE | PUBLISH (NOT decided here)

The deterministic phrase/regex heuristics (``_NEW_PROJECT_PHRASES``,
``_looks_like_natural_creation``, ``_REVISE_VERBS``, ...) are exercised only
when FAST is unavailable/fails — see test_natural_creation_routing.py and
test_conversations.py for that fallback contract, which remains unchanged.

These tests specifically prove semantic flexibility: NONE of the phrasings
below contain the old cue words / hardcoded trigger phrases, yet must still
route correctly because FAST — not a keyword list — is the primary
authority.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.channels.dispatch import AuthenticatedTelegramContext, TelegramDispatcher
from app.conversations import ConversationRoute, ConversationRouter
from app.core.intake import IntakeProcessor
from app.core.registry import ConversationRegistryStore
from app.core.state import ProjectStateStore


# ---------------------------------------------------------------------------
# Helpers
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
    dispatcher = TelegramDispatcher(
        store, intake, builder=builder, workspace_for=lambda pid: tmp_path / "ws"
    )
    router = ConversationRouter(store, registry, telegram_out=MagicMock())
    auth = AuthenticatedTelegramContext("1", str(chat))
    return store, registry, adapter, builder, dispatcher, router, auth


def _create_via_router(router, dispatcher, payload, conversation_id, display_name, auth,
                        event_id=None):
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
    """Hermes stub whose _run_fast_programmatic returns a fixed JSON payload."""

    def __init__(self, payload):
        self._response = payload if isinstance(payload, str) else json.dumps(payload)
        self.calls = 0

    def _run_fast_programmatic(self, **kwargs):
        self.calls += 1
        return MagicMock(success=True, response=self._response)


def _turn(intent, target=None, proposed=None, confidence="high"):
    return {
        "intent": intent,
        "target_project_name": target,
        "proposed_new_project_name": proposed,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# 1. No cue words at all -> FAST still resolves SELECT/continue semantics
# ---------------------------------------------------------------------------


class TestNoKeywordProjectSelection:
    def test_yang_jogja_dulu_selects_project_via_fast(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        p1 = _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        p2 = _create_via_router(router, dispatcher, _payload(2), "555", "webjogja", auth)
        # Active is p2 after creation; force it back to p1 so the switch is
        # observable.
        registry.set_active("555", p1.project_id)

        router.hermes = _FastStub(_turn("PROJECT_TURN", target="webjogja"))
        route = router.route("555", "yang Jogja dulu", event_id="3")

        assert route.route == ConversationRoute.PROJECT
        assert route.project_id == p2.project_id
        assert registry.active_project_id("555") == p2.project_id
        # The router does NOT decide REVISE/INTAKE itself.
        assert route.forced_intent is None

    def test_balik_ke_daily_bake_selects_project_via_fast(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        p1 = _create_via_router(router, dispatcher, _payload(1), "555", "Daily Bake", auth)
        p2 = _create_via_router(router, dispatcher, _payload(2), "555", "webjogja", auth)

        router.hermes = _FastStub(_turn("PROJECT_TURN", target="Daily Bake"))
        route = router.route("555", "balik ke Daily Bake", event_id="3")

        assert route.route == ConversationRoute.PROJECT
        assert route.project_id == p1.project_id
        assert registry.active_project_id("555") == p1.project_id


# ---------------------------------------------------------------------------
# 2. No cue words -> LIST_PROJECTS via FAST
# ---------------------------------------------------------------------------


class TestNoKeywordListProjects:
    def test_aku_pernah_bikin_apa_aja_lists_via_fast(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)

        router.hermes = _FastStub(_turn("LIST_PROJECTS"))
        route = router.route("555", "aku pernah bikin apa aja di sini?", event_id="2")

        assert route.route == ConversationRoute.LIST_PROJECTS
        assert route.reply is not None
        assert "webbandung" in route.reply


# ---------------------------------------------------------------------------
# 3. No cue words -> PROJECT_TURN, revision decision deferred to runtime.py
# ---------------------------------------------------------------------------


class TestNoKeywordProjectTurnDefersRevision:
    def test_yang_kemarin_terlalu_rame_stays_project_turn(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        p1 = _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)

        router.hermes = _FastStub(_turn("PROJECT_TURN", target=None))
        route = router.route("555", "yang kemarin terlalu rame", event_id="2")

        assert route.route == ConversationRoute.PROJECT
        assert route.project_id == p1.project_id
        # The router never decides REVISE — that is runtime.py's job via
        # _classify_intent, gated by the project's actual lifecycle.
        assert route.forced_intent is None


# ---------------------------------------------------------------------------
# 4. CREATE mixed with design-change wording still routes CREATE_PROJECT
# ---------------------------------------------------------------------------


class TestMixedCreateWording:
    def test_boleh_mulai_sesuatu_buat_bakery_routes_create(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)

        router.hermes = _FastStub(
            _turn("CREATE_PROJECT", proposed="Butter Room")
        )
        route = router.route(
            "555",
            "boleh bantu buat tempat online untuk usaha kue saya? namanya Butter Room",
            event_id="2",
        )

        assert route.route == ConversationRoute.NEW_PROJECT
        assert route.entry.display_name == "Butter Room"

    def test_ready_project_plus_design_change_wording_still_creates(self, tmp_path):
        """Real-incident shape: an active READY project + creation wording
        that also contains design-adjustment language ("disesuaiin aja")
        must still route CREATE_PROJECT, not flip into UPDATE of the old
        project."""
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready(
            name="tarotreaderjogja", what="Sesi tarot", why="booking"
        )
        tarot = _create_via_router(
            router, dispatcher, _payload(1), "555", "tarotreaderjogja", auth
        )
        dispatcher.dispatch(
            _payload(2, text="tarot jogja, sesi tarot, booking"),
            tarot.project_id, "intake", authenticated=auth,
        )
        before = _snapshot(store.load(tarot.project_id))

        router.hermes = _FastStub(
            _turn("CREATE_PROJECT", proposed="thedailybake")
        )
        route = router.route(
            "555",
            "aku mau bikin website tentang cafe, nama web nya thedailybake "
            "desainnya di sesuaiin aja",
            event_id="3",
        )

        assert route.route == ConversationRoute.NEW_PROJECT
        assert route.entry.display_name == "thedailybake"
        assert route.entry.project_id != tarot.project_id
        after = _snapshot(store.load(tarot.project_id))
        assert after == before


# ---------------------------------------------------------------------------
# 5. UPDATE never creates a new project
# ---------------------------------------------------------------------------


class TestUpdateNeverCreates:
    @pytest.mark.parametrize(
        "text",
        [
            "webbandung hero nya kegedean",
            "yang kemarin tombol bookingnya kurang keliatan",
            "Daily Bake terasa terlalu ramai, dibuat lebih clean",
            "change the typography on Morning Yard",
            "bagian atas yang Jogja terlalu tinggi",
        ],
    )
    def test_update_phrasings_never_allocate_project(self, tmp_path, text):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        n_projects = len(registry.load("555").projects)

        router.hermes = _FastStub(_turn("PROJECT_TURN", target=None))
        route = router.route("555", text, event_id="2")

        assert route.route != ConversationRoute.NEW_PROJECT
        assert len(registry.load("555").projects) == n_projects


# ---------------------------------------------------------------------------
# 6. Confidence gating: risk-based, not a blanket threshold
# ---------------------------------------------------------------------------


class TestConfidenceGating:
    @pytest.mark.parametrize("confidence", ["medium", "low"])
    def test_create_project_non_high_confidence_clarifies(self, tmp_path, confidence):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        n_projects = len(registry.load("555").projects)

        router.hermes = _FastStub(
            _turn("CREATE_PROJECT", proposed="Something", confidence=confidence)
        )
        route = router.route("555", "mungkin bikin sesuatu yang baru kali ya", event_id="2")

        # CREATE vs continuing an existing project must never be guessed
        # below high confidence.
        assert route.route == ConversationRoute.CLARIFICATION
        assert len(registry.load("555").projects) == n_projects

    def test_project_turn_medium_confidence_proceeds(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        p1 = _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)

        router.hermes = _FastStub(
            _turn("PROJECT_TURN", target="webbandung", confidence="medium")
        )
        route = router.route("555", "webbandung gimana progressnya", event_id="2")

        assert route.route == ConversationRoute.PROJECT
        assert route.project_id == p1.project_id

    def test_project_turn_low_confidence_clarifies(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)

        router.hermes = _FastStub(
            _turn("PROJECT_TURN", target=None, confidence="low")
        )
        route = router.route("555", "hmm itu gimana ya", event_id="2")

        assert route.route == ConversationRoute.CLARIFICATION

    def test_list_projects_medium_confidence_proceeds(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)

        router.hermes = _FastStub(_turn("LIST_PROJECTS", confidence="medium"))
        route = router.route("555", "coba tunjukin website yang udah dikerjain", event_id="2")

        assert route.route == ConversationRoute.LIST_PROJECTS

    def test_ambiguous_intent_always_clarifies(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        n_projects = len(registry.load("555").projects)

        router.hermes = _FastStub(_turn("AMBIGUOUS", confidence="high"))
        route = router.route("555", "entah lah pokoknya website", event_id="2")

        assert route.route == ConversationRoute.CLARIFICATION
        assert len(registry.load("555").projects) == n_projects


# ---------------------------------------------------------------------------
# 7. Malformed/low-confidence-equivalent FAST output never mutates the
#    wrong project — falls back to the conservative deterministic path.
# ---------------------------------------------------------------------------


class TestMalformedFastFallsBackSafely:
    @pytest.mark.parametrize(
        "response",
        [
            "not json at all",
            "```json\n{}\n```",
            '{"intent": "EXPLODE"}',
            '["CREATE_PROJECT", "webbandung"]',
            '{"intent": 42}',
            "",
        ],
    )
    def test_malformed_fast_response_falls_back_to_active_project(self, tmp_path, response):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        p1 = _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        n_projects = len(registry.load("555").projects)

        router.hermes = _FastStub(response)
        route = router.route("555", "gimana progress web aku?", event_id="2")

        assert route.route in (ConversationRoute.PROJECT, ConversationRoute.CLARIFICATION)
        assert route.route != ConversationRoute.NEW_PROJECT
        assert len(registry.load("555").projects) == n_projects

    def test_fast_exception_falls_back_safely(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        n_projects = len(registry.load("555").projects)

        hermes = MagicMock()
        hermes._run_fast_programmatic.side_effect = RuntimeError("boom")
        router.hermes = hermes

        route = router.route("555", "gimana progress web aku?", event_id="2")

        assert route.route != ConversationRoute.NEW_PROJECT
        assert len(registry.load("555").projects) == n_projects

    def test_unresolvable_target_name_for_project_turn_falls_back(self, tmp_path):
        """A PROJECT_TURN naming a project that does not exist in the
        registry discards the WHOLE FAST decision — never guessed."""
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        p1 = _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)

        router.hermes = _FastStub(_turn("PROJECT_TURN", target="not-a-real-project"))
        route = router.route("555", "lanjutin yang itu", event_id="2")

        # Falls through to the deterministic fallback rather than trusting
        # an unresolvable name; must never crash or silently invent a name.
        assert route.route in (ConversationRoute.PROJECT, ConversationRoute.CLARIFICATION)


# ---------------------------------------------------------------------------
# 8. CREATE_PROJECT naming an existing project -> clarify, never duplicate
# ---------------------------------------------------------------------------


class TestFastCreateNamingExistingProjectClarifies:
    def test_create_intent_with_existing_target_name_clarifies(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        n_projects = len(registry.load("555").projects)

        router.hermes = _FastStub(
            _turn("CREATE_PROJECT", target="webbandung", proposed=None)
        )
        route = router.route("555", "bikin lagi ah kayak webbandung", event_id="2")

        assert route.route == ConversationRoute.CLARIFICATION
        assert len(registry.load("555").projects) == n_projects


# ---------------------------------------------------------------------------
# 9. Fallback (FAST down) never auto-creates on uncertain wording; only very
#    conservative, obvious phrasing is recognized in degraded mode.
# ---------------------------------------------------------------------------


class TestFallbackDegradedModeConservative:
    def test_fast_unavailable_ambiguous_wording_never_creates(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        n_projects = len(registry.load("555").projects)
        assert router.hermes is None  # FAST unavailable

        # No create verb + noun combo, no explicit trigger phrase: the
        # conservative fallback must not guess CREATE.
        route = router.route("555", "gimana progressnya sekarang?", event_id="2")

        assert route.route != ConversationRoute.NEW_PROJECT
        assert len(registry.load("555").projects) == n_projects

    def test_fast_unavailable_still_recognizes_explicit_trigger_phrase(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        assert router.hermes is None

        route = router.route("555", "bikin website baru namanya webjogja", event_id="2")

        assert route.route == ConversationRoute.NEW_PROJECT
        assert route.entry.display_name == "webjogja"
