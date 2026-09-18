"""Website Builder R1 — conversation/project model regression tests.

Covers the eighteen scenarios from the R1 spec:

 1. first project creation in a new conversation
 2. second project creation in same conversation
 3. old project remains unchanged
 4. active project switches to new project
 5. restart preserves active project
 6. duplicate NEW_PROJECT event does not create another project
 7. duplicate normalized name is not silently created
 8. "revisi webbandung" resolves correct project
 9. subsequent revision without project name uses active project
10. switching from webbandung to webjogja works
11. unknown project asks clarification
12. ambiguous project does not guess
13. project listing exposes names, not IDs
14. current READY fresh intake does not attempt READY -> READY
15. current READY intake claim ends DONE, not CLAIMED
16. existing discovery flow remains unchanged
17. preview follow-up contains persisted display name and real preview URL
18. old project dispatch/deployment/revision state remains intact
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.channels.dispatch import AuthenticatedTelegramContext, TelegramDispatcher
from app.channels.telegram import NormalizedMessage
from app.conversations import ConversationRoute, ConversationRouter
from app.core.intake import IntakeProcessor
from app.core.lifecycle import ProjectLifecycle
from app.core.registry import (
    ConversationRegistry,
    ConversationRegistryStore,
    DuplicateProjectName,
    normalize_project_name,
    project_id_for,
)
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


def _fast(scope, name=None, what=None, why=None, why_destination=None,
          clarification_needed=False, clarification_question=None):
    return {
        "scope": scope,
        "name": name,
        "what": what,
        "why": why,
        "why_destination": why_destination,
        "ambiguity": None,
        "clarification_needed": clarification_needed,
        "clarification_question": clarification_question,
        "readiness": "NEEDS_CLARIFICATION" if clarification_needed else "DISCOVERY_READY",
    }


def _fast_ready(scope="WEBSITE", name="P", what="biz", why="cta"):
    return _fast(scope, name=name, what=what, why=why, clarification_needed=False)


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


def _create_via_router(router, dispatcher, payload, conversation_id, display_name, auth,
                       event_id=None):
    entry = router.materialize_new_project(
        conversation_id, display_name, event_id=event_id or payload["update_id"]
    )
    dispatcher.dispatch(payload, entry.project_id, "create", authenticated=auth)
    return entry


# ---------------------------------------------------------------------------
# 1. First project creation in a new conversation
# ---------------------------------------------------------------------------


class TestFirstProjectCreation:
    def test_first_project_allocates_p1_and_is_active(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()

        route = router.route("555", "bikin website baru namanya webbandung",
                             event_id="1")
        assert route.route == ConversationRoute.NEW_PROJECT
        assert route.entry.display_name == "webbandung"

        entry = _create_via_router(router, dispatcher, _payload(1), "555",
                                   route.entry.display_name, auth)
        assert entry.project_id == "tg-555-p1"
        assert registry.active_project_id("555") == "tg-555-p1"
        assert store.load("tg-555-p1") is not None
        assert store.load("tg-555-p1").owner_id is not None


# ---------------------------------------------------------------------------
# 2. Second project creation in same conversation
# ---------------------------------------------------------------------------


class TestSecondProjectCreation:
    def test_second_project_allocates_p2_and_becomes_active(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()

        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        route = router.route("555", "bikin website baru namanya webjogja", event_id="2")
        assert route.route == ConversationRoute.NEW_PROJECT
        p2 = _create_via_router(router, dispatcher, _payload(2), "555",
                                route.entry.display_name, auth)
        assert p2.project_id == "tg-555-p2"
        assert registry.active_project_id("555") == "tg-555-p2"
        # Both projects exist with distinct state.
        assert store.load("tg-555-p1") is not None
        assert store.load("tg-555-p2") is not None


# ---------------------------------------------------------------------------
# 3. Old project remains unchanged
# ---------------------------------------------------------------------------


class TestOldProjectPreserved:
    def test_new_project_does_not_touch_old_state(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready(name="Bandung", what="tarot", why="booking")
        p1 = _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)

        # Mutate p1 through intake to build a real history.
        dispatcher.dispatch(_payload(2, text="Bandung, tarot, booking"), p1.project_id,
                            "intake", authenticated=auth)
        before = store.load(p1.project_id)
        before_brief = dict(before.brief)
        before_lifecycle = before.lifecycle
        before_requirements = before.revisions.requirements_version
        before_events = sorted(before.processed_events)

        # Create p2.
        adapter.fast_interpret.return_value = _fast_ready(name="Jogja", what="coffee", why="order")
        p2 = _create_via_router(router, dispatcher, _payload(3), "555", "webjogja", auth)
        dispatcher.dispatch(_payload(3, text="Jogja, coffee, order"), p2.project_id,
                            "intake", authenticated=auth)

        after = store.load(p1.project_id)
        assert after.brief == before_brief
        assert after.lifecycle == before_lifecycle
        assert after.revisions.requirements_version == before_requirements
        assert sorted(after.processed_events) == before_events
        assert after.dispatch_events == before.dispatch_events


# ---------------------------------------------------------------------------
# 4. Active project switches to new project
# ---------------------------------------------------------------------------


class TestActiveSwitches:
    def test_active_pointer_moves_to_newly_created_project(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        assert registry.active_project_id("555") == "tg-555-p1"
        _create_via_router(router, dispatcher, _payload(2), "555", "webjogja", auth)
        assert registry.active_project_id("555") == "tg-555-p2"


# ---------------------------------------------------------------------------
# 5. Restart preserves active project
# ---------------------------------------------------------------------------


class TestRestartPreservesActive:
    def test_registry_persists_active_across_store_instances(self, tmp_path):
        state_root = tmp_path / "state"
        store1 = ProjectStateStore(state_root)
        reg1 = ConversationRegistryStore(state_root / "conversations")
        router1 = ConversationRouter(store1, reg1, telegram_out=MagicMock())
        auth = AuthenticatedTelegramContext("1", "555")
        dispatcher = TelegramDispatcher(store1, MagicMock(), builder=MagicMock())
        entry = _create_via_router(router1, dispatcher, _payload(1), "555", "webbandung",
                                   auth)
        assert reg1.active_project_id("555") == entry.project_id

        # Fresh store objects over the same persisted root (process restart).
        store2 = ProjectStateStore(state_root)
        reg2 = ConversationRegistryStore(state_root / "conversations")
        assert reg2.active_project_id("555") == entry.project_id
        loaded = reg2.load("555")
        assert loaded.find_by_id(entry.project_id).display_name == "webbandung"


# ---------------------------------------------------------------------------
# 6. Duplicate NEW_PROJECT event does not create another project
# ---------------------------------------------------------------------------


class TestDuplicateNewProjectEvent:
    def test_replay_same_event_resolves_to_same_project(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        payload = _payload(42, text="bikin website baru namanya webjogja")

        route1 = router.route("555", payload["message"]["text"], event_id="42")
        assert route1.route == ConversationRoute.NEW_PROJECT
        p_created = _create_via_router(router, dispatcher, payload, "555",
                                       route1.entry.display_name, auth, event_id="42")
        assert p_created.project_id == "tg-555-p1"
        n_projects_after_first = len(registry.load("555").projects)

        # Replay the SAME Telegram event.
        route2 = router.route("555", payload["message"]["text"], event_id="42")
        assert route2.route == ConversationRoute.PROJECT
        assert route2.project_id == p_created.project_id
        assert len(registry.load("555").projects) == n_projects_after_first
        # No p2 was silently allocated by the replay.
        assert store.load("tg-555-p2") is None


# ---------------------------------------------------------------------------
# 7. Duplicate normalized name is not silently created
# ---------------------------------------------------------------------------


class TestDuplicateNameRejected:
    def test_same_normalized_name_selects_existing_no_silent_project(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        _create_via_router(router, dispatcher, _payload(1), "555", "WebBandung", auth)

        route = router.route("555", "bikin website baru namanya webbandung", event_id="2")
        # Duplicate name resolves to the existing project, never silently creates p2.
        assert route.route == ConversationRoute.PROJECT
        assert route.project_id == "tg-555-p1"
        assert route.clarification is not None
        assert "webbandung" in route.clarification.lower() or "WebBandung" in route.clarification
        assert len(registry.load("555").projects) == 1
        assert store.load("tg-555-p2") is None


# ---------------------------------------------------------------------------
# 8. "revisi webbandung" resolves correct project
# ---------------------------------------------------------------------------


class TestReviseResolvesNamedProject:
    def test_revisi_named_project_routes_to_that_project(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        p1 = _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        p2 = _create_via_router(router, dispatcher, _payload(2), "555", "webjogja", auth)
        # Active is p2 after creation.
        assert registry.active_project_id("555") == p2.project_id

        route = router.route("555", "aku mau revisi webbandung", event_id="3")
        assert route.route == ConversationRoute.PROJECT
        assert route.project_id == p1.project_id
        # The router resolves WHICH project the turn targets; it no longer
        # decides REVISE-vs-INTAKE itself — that decision is deferred
        # entirely to runtime.py's TelegramReceiveLoop._classify_intent
        # (project-lifecycle-gated FAST classifier), so router and runtime
        # never make conflicting semantic decisions about the same turn.
        assert route.forced_intent is None
        # Router focused the named project for subsequent turns.
        assert registry.active_project_id("555") == p1.project_id


# ---------------------------------------------------------------------------
# 9. Subsequent revision without project name uses active project
# ---------------------------------------------------------------------------


class TestSubsequentTurnUsesActive:
    def test_no_name_after_revisi_targets_active_project(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        p1 = _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        p2 = _create_via_router(router, dispatcher, _payload(2), "555", "webjogja", auth)
        router.route("555", "aku mau revisi webbandung", event_id="3")

        # Follow-up small-talk turn with no project name -> active p1.
        follow_route = router.route("555", "hero-nya lebih kecil", event_id="4")
        assert follow_route.route == ConversationRoute.PROJECT
        assert follow_route.project_id == p1.project_id
        assert follow_route.forced_intent is None


# ---------------------------------------------------------------------------
# 10. Switching from webbandung to webjogja works
# ---------------------------------------------------------------------------


class TestSwitchProject:
    def test_sekarang_webjogja_switches_active(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        p1 = _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        p2 = _create_via_router(router, dispatcher, _payload(2), "555", "webjogja", auth)
        # Go back to p1 first.
        router.route("555", "aku mau revisi webbandung", event_id="3")
        assert registry.active_project_id("555") == p1.project_id
        # Now switch.
        route = router.route("555", "sekarang webjogja", event_id="4")
        assert route.route == ConversationRoute.PROJECT
        assert route.project_id == p2.project_id
        assert registry.active_project_id("555") == p2.project_id


# ---------------------------------------------------------------------------
# 11. Unknown project asks clarification
# ---------------------------------------------------------------------------


class TestUnknownProjectClarifies:
    def test_unknown_name_with_no_active_context_asks_for_choice(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        _create_via_router(router, dispatcher, _payload(2), "555", "webjogja", auth)
        # Clear the active pointer to simulate a fresh, ambiguous context.
        reg = registry.load("555")
        reg.active_project_id = None
        registry.save(reg)

        adapter._run_fast_programmatic = MagicMock(return_value=MagicMock(
            success=True,
            response='{"intent": "SELECT_PROJECT", "target_project_name": "websurabaya"}',
        ))
        router.hermes = adapter
        route = router.route("555", "aku mau revisi websurabaya", event_id="3")
        assert route.route == ConversationRoute.CLARIFICATION
        assert route.clarification is not None
        # Ambiguous context lists the known projects so the user can pick one.
        assert "webbandung" in route.clarification
        assert "webjogja" in route.clarification


# ---------------------------------------------------------------------------
# 12. Ambiguous project does not guess
# ---------------------------------------------------------------------------


class TestAmbiguousProjectNoGuess:
    def test_alias_collision_is_ambiguous_never_guessed(self, tmp_path):
        store_root = tmp_path / "state"
        store = ProjectStateStore(store_root)
        registry = ConversationRegistryStore(store_root / "conversations")
        router = ConversationRouter(store, registry, telegram_out=MagicMock())
        auth = AuthenticatedTelegramContext("1", "555")
        dispatcher = TelegramDispatcher(store, MagicMock(), builder=MagicMock())
        a = _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        b = _create_via_router(router, dispatcher, _payload(2), "555", "Bandung Tarot", auth)
        # Give the second project an alias that collides with the first's name.
        registry.add_alias("555", b.project_id, "webbandung")

        # Direct registry resolution reports ambiguous.
        resolution = registry.resolve_name("555", "webbandung")
        assert resolution.status == "ambiguous"
        assert len(resolution.candidates) == 2


# ---------------------------------------------------------------------------
# 13. Project listing exposes names, not IDs
# ---------------------------------------------------------------------------


class TestListProjects:
    def test_list_shows_human_names_and_status_no_internal_ids(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        p1 = _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        p2 = _create_via_router(router, dispatcher, _payload(2), "555", "webjogja", auth)

        # Give p1 a meaningfully different status than p2.
        with store.acquire_writer(p1.project_id) as st:
            st.lifecycle = ProjectLifecycle.PREVIEW_READY.value
            store.save(st)

        route = router.route("555", "project aku ada apa aja?", event_id="3")
        assert route.route == ConversationRoute.LIST_PROJECTS
        listing = route.reply
        assert "webbandung" in listing
        assert "webjogja" in listing
        assert "Preview ready" in listing
        assert "Draft" in listing
        assert "tg-555-p1" not in listing
        assert "tg-555-p2" not in listing
        assert "project_id" not in listing


# ---------------------------------------------------------------------------
# 14. READY fresh intake does not attempt READY -> READY
# ---------------------------------------------------------------------------


class TestReadyReadyNoLifecycleError:
    def test_ready_state_semantically_complete_intake_no_error(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        p1 = _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)

        # Drive p1 through discovery to READY. Suppress the auto-build so the
        # lifecycle settles at READY (the point of this regression scenario).
        original_builder = dispatcher.builder
        dispatcher.builder = None
        try:
            dispatcher.dispatch(_payload(2, text="Bandung, tarot, booking"), p1.project_id,
                                "intake", authenticated=auth)
        finally:
            dispatcher.builder = original_builder
        assert store.load(p1.project_id).lifecycle == "READY"

        # A FRESH intake event that is again semantically complete must not
        # attempt READY -> READY (LifecycleError). Keep auto-build suppressed
        # here too so the assertion isolates the intake lifecycle behavior.
        adapter.fast_interpret.return_value = _fast_ready(name="Bandung v2", what="tarot", why="booking")
        dispatcher.builder = None
        try:
            result = dispatcher.dispatch(
                _payload(3, text="Bandung v2, tarot, booking"), p1.project_id,
                "intake", authenticated=auth,
            )
        finally:
            dispatcher.builder = original_builder
        assert result.success
        assert result.error_code != "EVENT_RECONCILIATION_REQUIRED"
        assert store.load(p1.project_id).lifecycle == "READY"


# ---------------------------------------------------------------------------
# 15. READY intake claim ends DONE, not CLAIMED
# ---------------------------------------------------------------------------


class TestReadyIntakeClaimDone:
    def test_dispatch_claim_completes_done(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        p1 = _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        original_builder = dispatcher.builder
        dispatcher.builder = None
        try:
            dispatcher.dispatch(_payload(2, text="Bandung, tarot, booking"), p1.project_id,
                                "intake", authenticated=auth)
        finally:
            dispatcher.builder = original_builder
        assert store.load(p1.project_id).lifecycle == "READY"

        adapter.fast_interpret.return_value = _fast_ready(name="Bandung v2", what="tarot", why="booking")
        result = dispatcher.dispatch(
            _payload(3, text="Bandung v2, tarot, booking"), p1.project_id,
            "intake", authenticated=auth,
        )
        assert result.success
        state = store.load(p1.project_id)
        top_claims = [c for c in state.dispatch_events.values() if c["action"] == "intake"]
        assert len(top_claims) == 2
        assert all(c["status"] == "DONE" for c in top_claims)


# ---------------------------------------------------------------------------
# 16. Existing discovery flow remains unchanged
# ---------------------------------------------------------------------------


class TestDiscoveryFlowUnchanged:
    def test_three_turn_discovery_to_build_still_works(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        p1 = _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)

        adapter.fast_interpret.return_value = _fast(
            "WEBSITE", name="Northcut", clarification_needed=True,
            clarification_question="What is Northcut?",
        )
        r1 = dispatcher.dispatch(_payload(2, text="Northcut"), p1.project_id,
                                 "intake", authenticated=auth)
        assert r1.data["readiness"] == "NEEDS_CLARIFICATION"
        assert r1.data["clarification_question"] == "What is Northcut?"
        assert builder.build.call_count == 0

        adapter.fast_interpret.return_value = _fast(
            "WEBSITE", name="Northcut", what="barbershop",
            clarification_needed=True,
            clarification_question="What should visitors do on Northcut?",
        )
        r2 = dispatcher.dispatch(_payload(3, text="barbershop"), p1.project_id,
                                 "intake", authenticated=auth)
        assert r2.data["readiness"] == "NEEDS_CLARIFICATION"

        adapter.fast_interpret.return_value = _fast(
            "WEBSITE", name="Northcut", what="barbershop", why="booking WA",
            clarification_needed=False,
        )
        r3 = dispatcher.dispatch(_payload(4, text="booking WA"), p1.project_id,
                                 "intake", authenticated=auth)
        assert r3.data["readiness"] == "DISCOVERY_READY"
        assert builder.build.call_count == 1
        assert store.load(p1.project_id).lifecycle == "QUEUED"


# ---------------------------------------------------------------------------
# 17. Preview follow-up contains persisted display name and real preview URL
# ---------------------------------------------------------------------------


class TestPreviewFollowUp:
    def test_follow_up_sent_after_delivery_with_name_and_url(self, tmp_path):
        from app.deploy.preview import PreviewDeps, PreviewOrchestrator
        from app.deploy.git_output import OutputGitRepository
        from app.deploy.snapshot import TestedSnapshot, source_fingerprint
        from tests.test_preview_orchestrator import (
            FakeSmoke, FakeTelegram, FakeVercel, _make_workspace, _preview_ready_state,
        )

        store = ProjectStateStore(tmp_path / "state")
        ws = _make_workspace(tmp_path)
        _preview_ready_state(store, "proj", ws)
        telegram = FakeTelegram()
        deps = PreviewDeps(
            vercel=FakeVercel(),
            telegram=telegram,
            smoke=FakeSmoke(),
            output_repo=OutputGitRepository(tmp_path / "out", hermes_root=tmp_path / "hermes"),
            chat_id_for=lambda pid, state: "123",
            display_name_for=lambda pid, state: "webbandung",
        )
        result = PreviewOrchestrator(store, deps).run_owned("proj", ws)
        assert result.success

        text_sends = [m for m in telegram.sent if m[0] == "text"]
        # The preview delivery text + the natural follow-up.
        assert len(text_sends) == 2
        follow = text_sends[1][2]
        assert "webbandung" in follow
        assert "https://tested.vercel.app" in follow
        assert "Mau revisi" in follow
        assert "tg-555" not in follow


# ---------------------------------------------------------------------------
# 18. Old project dispatch/deployment/revision state intact
# ---------------------------------------------------------------------------


class TestOldProjectAllStateIntact:
    def test_dispatch_deploy_revision_state_all_preserved(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        p1 = _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)

        # Give p1 realistic history across every category.
        with store.acquire_writer(p1.project_id) as st:
            st.brief = {"name": "Bandung", "what": "tarot", "why": "booking"}
            st.lifecycle = ProjectLifecycle.PREVIEW_READY.value
            st.revisions.source_revision = 2
            st.revisions.qa_revision = 2
            st.revisions.preview_revision = 2
            st.design_dna = {"palette": ["#111"]}
            st.deployment["latest_shown_preview"] = {
                "operation_id": "op-1", "preview_url": "https://bandung.vercel.app",
            }
            st.deployment["approval"] = {"bound": True}
            st.domain.domain = "bandung.example"
            st.production_url = "https://bandung.example"
            st.processed_events.add("evt-1")
            st.dispatch_events["k1"] = {"action": "publish", "status": "DONE"}
            store.save(st)
        before = store.load(p1.project_id).to_dict()

        # Create p2 and do a full intake there.
        adapter.fast_interpret.return_value = _fast_ready(name="Jogja", what="coffee", why="order")
        p2 = _create_via_router(router, dispatcher, _payload(2), "555", "webjogja", auth)
        dispatcher.dispatch(_payload(2, text="Jogja, coffee, order"), p2.project_id,
                            "intake", authenticated=auth)

        after = store.load(p1.project_id).to_dict()
        for key in ("brief", "lifecycle", "design_dna", "deployment",
                    "production_url", "dispatch_events", "processed_events"):
            assert after[key] == before[key]
        assert after["domain"]["domain"] == "bandung.example"
        assert after["revisions"]["source_revision"] == 2


# ---------------------------------------------------------------------------
# Name normalization — tested, deterministic, conservative
# ---------------------------------------------------------------------------


class TestNormalization:
    @pytest.mark.parametrize("raw,expected", [
        ("webbandung", "webbandung"),
        ("  WebBandung  ", "webbandung"),
        ("web bandung", "web-bandung"),
        ("web/bandung!", "web-bandung"),
        ("WEB-BANDUNG", "web-bandung"),
        ("---", None),
        ("", None),
        ("   ", None),
        (None, None),
    ])
    def test_normalize(self, raw, expected):
        assert normalize_project_name(raw) == expected

    def test_project_id_format(self):
        assert project_id_for("555", 1) == "tg-555-p1"
        assert project_id_for("555", 12) == "tg-555-p12"


# ---------------------------------------------------------------------------
# Registry-level name resolution semantics
# ---------------------------------------------------------------------------


class TestRegistryResolution:
    def _registry(self, tmp_path):
        store_root = tmp_path / "state"
        store = ProjectStateStore(store_root)
        registry = ConversationRegistryStore(store_root / "conversations")
        router = ConversationRouter(store, registry, telegram_out=MagicMock())
        auth = AuthenticatedTelegramContext("1", "555")
        dispatcher = TelegramDispatcher(store, MagicMock(), builder=MagicMock())
        return store, registry, router, dispatcher, auth

    def test_alias_resolves_to_same_project(self, tmp_path):
        store, registry, router, dispatcher, auth = self._registry(tmp_path)
        p = _create_via_router(router, dispatcher, _payload(1), "555", "Bandung Tarot", auth)
        registry.add_alias("555", p.project_id, "webbandung")
        resolution = registry.resolve_name("555", "webbandung")
        assert resolution.status == "ok"
        assert resolution.entry.project_id == p.project_id

    def test_store_allocate_project_rejects_duplicate_normalized_name(self, tmp_path):
        store, registry, router, dispatcher, auth = self._registry(tmp_path)
        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        # Exact normalized collision (case/whitespace-insensitive) is rejected.
        with pytest.raises(DuplicateProjectName):
            registry.allocate_project("555", "  WEBBANDUNG ")

    def test_distinct_normalized_forms_are_allowed(self, tmp_path):
        # Conservative matching: "Web Bandung" -> "web-bandung" is NOT the
        # same project as "webbandung" — no fuzzy merging, both may exist.
        store, registry, router, dispatcher, auth = self._registry(tmp_path)
        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        entry = registry.allocate_project("555", "Web Bandung")
        assert entry.project_id != "tg-555-p1"
        assert entry.display_name == "Web Bandung"

    def test_active_pointer_unknown_project_rejected(self, tmp_path):
        store, registry, router, dispatcher, auth = self._registry(tmp_path)
        _create_via_router(router, dispatcher, _payload(1), "555", "webbandung", auth)
        with pytest.raises(ValueError):
            registry.set_active("555", "tg-555-p999")


# ---------------------------------------------------------------------------
# 19. Legacy (pre-router) project adoption — tg-<conversation_id>
# ---------------------------------------------------------------------------


class TestLegacyProjectAdoption:
    """A pre-router R1 conversation already owns ``tg-<conversation_id>`` on
    disk with NO registry metadata. Enabling the router must adopt that
    project into the registry (not orphan/hide/reset/replace it), preserve
    its complete ProjectState byte-for-byte, and never create a duplicate.
    """

    def _legacy_state(self, store, conversation_id="555"):
        """Write a realistic pre-router legacy project directly to disk."""
        legacy_id = f"tg-{conversation_id}"
        with store.acquire_writer(legacy_id) as st:
            st.owner_id = "telegram:1"
            st.roles["owner"] = "telegram:1"
            st.channel = "telegram"
            st.conversation_id = conversation_id
            st.brief = {"name": "Bandung Tarot", "what": "tarot reading",
                        "why": "booking WA"}
            st.lifecycle = ProjectLifecycle.PREVIEW_READY.value
            st.revisions.source_revision = 3
            st.revisions.qa_revision = 3
            st.revisions.preview_revision = 3
            st.design_dna = {"palette": ["#222"], "typography": "serif"}
            st.deployment["latest_shown_preview"] = {
                "operation_id": "op-legacy", "preview_url": "https://bandung.vercel.app",
                "follow_up_sent": True,
            }
            st.domain.domain = "bandung.example"
            st.production_url = "https://bandung.example"
            st.processed_events.add("evt-legacy-1")
            st.dispatch_events["legacy-k"] = {"action": "publish", "status": "DONE"}
            store.save(st)
        return legacy_id

    def test_legacy_project_adopted_not_replaced(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        legacy_id = self._legacy_state(store)
        before = store.load(legacy_id).to_dict()

        # First router turn against the conversation.
        route = router.route("555", "hero-nya lebih kecil", event_id="99")
        assert route.route == ConversationRoute.PROJECT
        assert route.project_id == legacy_id

        # Registry adopted the legacy identity; no fresh p1 was allocated.
        reg = registry.load("555")
        ids = [p.project_id for p in reg.projects]
        assert legacy_id in ids
        assert "tg-555-p1" not in ids
        assert reg.active_project_id == legacy_id

        # Display name comes from the existing brief (user-visible name),
        # never an internal id.
        entry = reg.find_by_id(legacy_id)
        assert entry.display_name == "Bandung Tarot"
        assert "tg-555" not in entry.display_name

        # The complete ProjectState is preserved byte-for-byte.
        after = store.load(legacy_id).to_dict()
        assert after == before

    def test_legacy_adoption_idempotent_across_restart(self, tmp_path):
        state_root = tmp_path / "state"
        store1 = ProjectStateStore(state_root)
        reg1 = ConversationRegistryStore(state_root / "conversations")
        router1 = ConversationRouter(store1, reg1, telegram_out=MagicMock())
        legacy_id = self._legacy_state(store1)
        before = store1.load(legacy_id).to_dict()

        route1 = router1.route("555", "apa kabar", event_id="100")
        assert route1.project_id == legacy_id

        # Fresh stores over the same root (process restart). Adoption must
        # not create a second entry nor mutate project state.
        store2 = ProjectStateStore(state_root)
        reg2 = ConversationRegistryStore(state_root / "conversations")
        router2 = ConversationRouter(store2, reg2, telegram_out=MagicMock())
        route2 = router2.route("555", "lanjut", event_id="101")
        assert route2.project_id == legacy_id
        reg = reg2.load("555")
        ids = [p.project_id for p in reg.projects]
        assert ids.count(legacy_id) == 1
        assert len(reg.projects) == 1
        assert store2.load(legacy_id).to_dict() == before

    def test_legacy_list_projects_shows_adopted_name(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        legacy_id = self._legacy_state(store)
        route = router.route("555", "project aku ada apa aja?", event_id="102")
        assert route.route == ConversationRoute.LIST_PROJECTS
        listing = route.reply
        assert "Bandung Tarot" in listing
        assert "Preview ready" in listing
        assert "tg-555" not in listing

    def test_new_project_after_legacy_adoption_allocates_p1(self, tmp_path):
        # Adopting a legacy project must NOT consume the p1 sequence — the
        # router's first explicitly-requested new project still gets p1.
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        legacy_id = self._legacy_state(store)
        router.route("555", "apa kabar", event_id="100")  # adopt
        route = router.route("555", "bikin website baru namanya webjogja",
                             event_id="103")
        assert route.route == ConversationRoute.NEW_PROJECT
        entry = _create_via_router(router, dispatcher, _payload(103), "555",
                                   route.entry.display_name, auth)
        assert entry.project_id == "tg-555-p1"
        assert store.load(legacy_id).lifecycle == "PREVIEW_READY"


# ---------------------------------------------------------------------------
# 20. NEW_PROJECT hard-crash recovery — registry persisted, ProjectState not
# ---------------------------------------------------------------------------


class TestNewProjectCrashWindowRecovery:
    """Simulate a process death between registry persistence (entry +
    event->project mapping) and ProjectState creation. On replay of the
    SAME Telegram event, the originally allocated identity must be recovered
    — never advanced to a second logical project identity."""

    def _simulate_crash_after_registry(self, router, registry, conversation_id,
                                       display_name, event_id):
        """Allocate + record the event mapping, then STOP (no ProjectState)."""
        entry = registry.allocate_project(conversation_id, display_name)
        registry.set_active(conversation_id, entry.project_id)
        registry.record_event(conversation_id, event_id, entry.project_id)
        # Simulated crash: NO dispatcher.dispatch("create") — the project
        # state file does not exist on disk.
        return entry

    def test_replay_recovers_same_project_identity(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()

        crashed = self._simulate_crash_after_registry(
            router, registry, "555", "webbandung", "7"
        )
        assert store.load(crashed.project_id) is None

        # Process restart + Telegram retries the SAME event.
        route = router.route("555", "bikin website baru namanya webbandung",
                             event_id="7")
        assert route.route == ConversationRoute.NEW_PROJECT
        assert route.project_id == crashed.project_id  # SAME identity, not p2

        # The runtime recovery path uses route.project_id directly — no
        # second allocation.
        entry = router.materialize_new_project(
            "555", route.entry.display_name, event_id="7"
        )
        assert entry.project_id == crashed.project_id
        dispatcher.dispatch(_payload(7), entry.project_id, "create", authenticated=auth)
        assert store.load(crashed.project_id) is not None
        # Exactly ONE logical project exists; no p2 was ever allocated.
        assert len(registry.load("555").projects) == 1
        assert store.load("tg-555-p2") is None

    def test_replay_via_runtime_loop_recovers_identity(self, tmp_path):
        """End-to-end: runtime._handle_new_project honors route.project_id."""
        from app.runtime import TelegramReceiveLoop

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

        class _Out:
            def __init__(self): self.sent = []
            def send_text(self, chat_id, text, **kw): self.sent.append((str(chat_id), text))
            def send_photo(self, chat_id, path, caption="", **kw): self.sent.append((str(chat_id), caption))

        out = _Out()
        router = ConversationRouter(store, registry, telegram_out=out, hermes=adapter)
        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher, telegram_out=out, hermes=adapter,
            transport=MagicMock(), conversations=router,
        )

        # Simulate the crash: registry persisted the allocation for event 9,
        # no ProjectState was created.
        crashed_entry = registry.allocate_project("555", "webbandung")
        registry.set_active("555", crashed_entry.project_id)
        registry.record_event("555", "9", crashed_entry.project_id)
        assert store.load(crashed_entry.project_id) is None

        # Restarted loop replays event 9.
        loop._process_update(_payload(9, text="bikin website baru namanya webbandung"))

        # The SAME identity was recovered and materialized; no second project.
        assert store.load(crashed_entry.project_id) is not None
        assert store.load("tg-555-p2") is None
        assert len(registry.load("555").projects) == 1
        assert registry.active_project_id("555") == crashed_entry.project_id


# ---------------------------------------------------------------------------
# 21. Friendly display name preserved (normalization is lookup-only)
# ---------------------------------------------------------------------------


class TestDisplayNamePreserved:
    def test_display_name_not_normalized_in_user_output(self, tmp_path):
        store, registry, adapter, builder, dispatcher, router, auth = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast_ready()
        p = _create_via_router(router, dispatcher, _payload(1), "555",
                               "Bandung Tarot", auth)
        # Lookup/uniqueness uses the normalized form …
        resolution = registry.resolve_name("555", "bandung-tarot")
        assert resolution.status == "ok"
        assert resolution.entry.project_id == p.project_id
        # … but user-facing output preserves the intended display name.
        route = router.route("555", "project aku ada apa aja?", event_id="2")
        listing = route.reply
        assert "Bandung Tarot" in listing
        assert "bandung-tarot" not in listing
        assert registry.display_name_for("555", p.project_id) == "Bandung Tarot"


# ---------------------------------------------------------------------------
# 22. Preview follow-up crash window — fail-closed UX delivery semantics
# ---------------------------------------------------------------------------
#
# The follow-up is UX-only. Its durable state machine is:
#   absent/None -> definitely not attempted (safe to retry)
#   PENDING     -> attempted, outcome UNKNOWN (fail closed; never resend)
#   SENT        -> confirmed delivered (never resend)
#
# These tests pin every window that matters.


class TestPreviewFollowUpCrashWindow:
    """Crash/replay semantics of the post-delivery UX follow-up."""

    def _make(self, tmp_path, telegram=None):
        from app.deploy.preview import PreviewDeps, PreviewOrchestrator
        from app.deploy.git_output import OutputGitRepository
        from tests.test_preview_orchestrator import (
            FakeSmoke, FakeTelegram, FakeVercel, _make_workspace, _preview_ready_state,
        )

        store = ProjectStateStore(tmp_path / "state")
        ws = _make_workspace(tmp_path)
        _preview_ready_state(store, "proj", ws)
        telegram = telegram or FakeTelegram()
        deps = PreviewDeps(
            vercel=FakeVercel(), telegram=telegram, smoke=FakeSmoke(),
            output_repo=OutputGitRepository(tmp_path / "out",
                                            hermes_root=tmp_path / "hermes"),
            chat_id_for=lambda pid, state: "123",
            display_name_for=lambda pid, state: "webbandung",
        )
        return store, ws, telegram, PreviewOrchestrator(store, deps)

    def _follow_texts(self, telegram):
        return [m[2] for m in telegram.sent
                if m[0] == "text" and "Mau revisi" in m[2]]

    def test_normal_run_marks_follow_up_sent_and_never_duplicates(self, tmp_path):
        from app.deploy.preview import FOLLOW_UP_SENT

        store, ws, telegram, orch = self._make(tmp_path)
        assert orch.run_owned("proj", ws).success
        shown = store.load("proj").deployment["latest_shown_preview"]
        assert shown["follow_up_state"] == FOLLOW_UP_SENT
        assert len(self._follow_texts(telegram)) == 1

        # Every replay of the same delivered operation is a no-op.
        orch.run_owned("proj", ws)
        orch.run_owned("proj", ws)
        assert len(self._follow_texts(telegram)) == 1

    def test_crash_after_preview_shown_and_before_follow_up_is_retryable(self, tmp_path):
        """Crash window #3 from the audit: preview marked shown, follow-up
        not yet attempted. Since NOTHING was sent, the retry may safely
        deliver it — the durable state says "definitely not attempted"."""
        store, ws, telegram, orch = self._make(tmp_path)

        # Simulate the crash: run the full flow, then roll the row back to the
        # exact state it had at step 7 (preview shown, follow-up not attempted).
        assert orch.run_owned("proj", ws).success
        import copy
        with store.acquire_writer("proj") as st:
            st.deployment["latest_shown_preview"]["follow_up_state"] = None
            st.deployment["latest_shown_preview"].pop("follow_up_sent", None)
            # The preview delivery itself happened; the follow-up did not.
            store.save(st)

        telegram2 = type(telegram)()
        orch.deps = type(orch.deps)(
            vercel=orch.deps.vercel, telegram=telegram2, smoke=orch.deps.smoke,
            output_repo=orch.deps.output_repo, chat_id_for=orch.deps.chat_id_for,
            display_name_for=orch.deps.display_name_for,
        )

        result = orch.run_owned("proj", ws)
        assert result.success
        follow = self._follow_texts(telegram2)
        assert len(follow) == 1
        assert "webbandung" in follow[0]
        assert "Mau revisi" in follow[0]
        from app.deploy.preview import FOLLOW_UP_SENT as SENT
        assert store.load("proj").deployment["latest_shown_preview"]["follow_up_state"] == SENT

    def test_ambiguous_send_is_not_retried_fail_closed(self, tmp_path):
        """A send that RAISED is ambiguous (may have landed). The retry must
        NOT resend, and the state stays PENDING so an operator can see the
        unresolved attempt. The preview itself stays successful."""
        from app.deploy.preview import FOLLOW_UP_PENDING

        store, ws, telegram, orch = self._make(tmp_path)
        original_send_text = telegram.send_text
        calls = {"n": 0}

        def flaky_send_text(chat_id, text):
            calls["n"] += 1
            if "Mau revisi" in text:
                raise RuntimeError("simulated ambiguous transport failure")
            return original_send_text(chat_id, text)

        telegram.send_text = flaky_send_text

        # Preview delivery MUST still succeed despite the follow-up failure.
        result = orch.run_owned("proj", ws)
        assert result.success
        assert result.data["preview_url"] == "https://tested.vercel.app"
        shown = store.load("proj").deployment["latest_shown_preview"]
        assert shown["follow_up_state"] == FOLLOW_UP_PENDING

        # Retry with a WORKING telegram: must NOT resend (ambiguous send).
        telegram_ok = type(telegram)()
        orch.deps = type(orch.deps)(
            vercel=orch.deps.vercel, telegram=telegram_ok, smoke=orch.deps.smoke,
            output_repo=orch.deps.output_repo, chat_id_for=orch.deps.chat_id_for,
            display_name_for=orch.deps.display_name_for,
        )
        result2 = orch.run_owned("proj", ws)
        assert result2.success
        assert self._follow_texts(telegram_ok) == []
        # Still PENDING — explicitly unresolved, never silently "sent".
        assert store.load("proj").deployment["latest_shown_preview"]["follow_up_state"] == FOLLOW_UP_PENDING

    def test_adapter_reports_failure_is_also_ambiguous(self, tmp_path):
        """A `success=False` result (not an exception) is equally ambiguous —
        the request may still have reached Telegram. No resend."""
        from app.core.contracts import OperationResult
        from app.deploy.preview import (
            FOLLOW_UP_PENDING, PreviewDeps, PreviewOrchestrator,
        )
        from app.deploy.git_output import OutputGitRepository
        from tests.test_preview_orchestrator import (
            FakeSmoke, FakeVercel, _make_workspace, _preview_ready_state,
        )

        class FailingFollowUpTelegram:
            """Delivery text succeeds; follow-up text reports failure."""
            def __init__(self):
                self.sent = []

            def send_photo(self, chat_id, path, caption=''):
                self.sent.append(('photo', chat_id, path))
                return OperationResult.ok({'message_id': 1})

            def send_text(self, chat_id, text):
                self.sent.append(('text', chat_id, text))
                if "Mau revisi" in text:
                    return OperationResult.fail("SEND_FAILED", error_code="SEND_FAILED")
                return OperationResult.ok({'message_id': 2})

        store = ProjectStateStore(tmp_path / "state")
        ws = _make_workspace(tmp_path)
        _preview_ready_state(store, "proj", ws)
        telegram = FailingFollowUpTelegram()
        deps = PreviewDeps(
            vercel=FakeVercel(), telegram=telegram, smoke=FakeSmoke(),
            output_repo=OutputGitRepository(tmp_path / "out",
                                            hermes_root=tmp_path / "hermes"),
            chat_id_for=lambda pid, state: "123",
            display_name_for=lambda pid, state: "webbandung",
        )
        orch = PreviewOrchestrator(store, deps)

        result = orch.run_owned("proj", ws)
        assert result.success  # follow-up failure never fails the preview
        assert store.load("proj").deployment["latest_shown_preview"]["follow_up_state"] == FOLLOW_UP_PENDING

        # Same fail-closed retry: no resend.
        telegram_ok = type(telegram)()
        orch.deps = type(deps)(
            vercel=deps.vercel, telegram=telegram_ok, smoke=deps.smoke,
            output_repo=deps.output_repo, chat_id_for=deps.chat_id_for,
            display_name_for=deps.display_name_for,
        )
        assert orch.run_owned("proj", ws).success
        assert [m for m in telegram_ok.sent if "Mau revisi" in m[2]] == []

    def test_local_pre_send_failure_is_retryable(self, tmp_path):
        """A definite pre-send LOCAL failure (here: display-name resolution
        unavailable) leaves the state untouched, so nothing was sent and the
        retry may deliver once the local dependency is present."""
        from app.deploy.preview import PreviewDeps, PreviewOrchestrator
        from app.deploy.git_output import OutputGitRepository
        from tests.test_preview_orchestrator import (
            FakeSmoke, FakeTelegram, FakeVercel, _make_workspace, _preview_ready_state,
        )

        store = ProjectStateStore(tmp_path / "state")
        ws = _make_workspace(tmp_path)
        _preview_ready_state(store, "proj", ws)
        telegram = FakeTelegram()

        def broken_name(pid, state):
            raise RuntimeError("registry temporarily unavailable")

        deps = PreviewDeps(
            vercel=FakeVercel(), telegram=telegram, smoke=FakeSmoke(),
            output_repo=OutputGitRepository(tmp_path / "out",
                                            hermes_root=tmp_path / "hermes"),
            chat_id_for=lambda pid, state: "123",
            display_name_for=broken_name,
        )
        orch = PreviewOrchestrator(store, deps)

        result = orch.run_owned("proj", ws)
        assert result.success  # local follow-up failure never fails the preview
        assert store.load("proj").deployment["latest_shown_preview"]["follow_up_state"] is None
        assert self._follow_texts(telegram) == []

        # Dependency recovers; retry delivers exactly once.
        orch.deps = type(deps)(
            vercel=deps.vercel, telegram=telegram, smoke=deps.smoke,
            output_repo=deps.output_repo,
            chat_id_for=deps.chat_id_for,
            display_name_for=lambda pid, state: "webbandung",
        )
        assert orch.run_owned("proj", ws).success
        assert len(self._follow_texts(telegram)) == 1
