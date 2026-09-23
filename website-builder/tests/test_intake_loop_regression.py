"""Regression tests for the Telegram infinite clarification loop (E2E).

Observed failure sequence (before fix):

  User: "aku mau bikin website buat bengkel mobil namanya warehousebandung ..."
  Bot:   asks WHY (intake field)
  User: "liat layanan bengkel dan alamat dulu"
  Bot:   "Aku belum yakin maksudnya bikin website baru atau lanjut/ubah project
          yang sudah ada."   <- BUG: AMBIGUOUS again, infinite loop

Root causes fixed here:

  1. _fast_classify_turn received no active-project context.  FAST could not
     tell that "liat layanan..." was answering the active project's WHY
     question -- it returned AMBIGUOUS and CLARIFICATION fired again.

  2. When the router asked "Mau kasih nama apa?", no pending_action was
     persisted.  The next turn was re-classified by FAST from scratch, went
     AMBIGUOUS, and produced the same loop.

Fixes:

  1. _fast_classify_turn now receives the active project's display name,
     lifecycle, and next missing intake field.

  2. WAITING_INPUT bias: AMBIGUOUS or non-high CREATE while active project is
     WAITING_INPUT routes to active project instead of CLARIFICATION.

  3. _route_new_project (no name) atomically persists pending_action and
     returns CLARIFICATION.  Next turn consumed before FAST.  pending_action
     cleared ONLY after project identity is safely recorded (crash-safe).

  4. Correct trigger for pending_action: CREATE_PROJECT(high) + no proposed
     name.  AMBIGUOUS alone NEVER sets pending.

Coverage:
  * Multi-turn intake loop (primary reported regression)
  * WAITING_INPUT bias rule (AMBIGUOUS and non-high CREATE)
  * pending_action round-trip: set -> consumed -> cleared (crash-safe order)
  * High-confidence CREATE while WAITING_INPUT still proceeds to NEW_PROJECT
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.channels.dispatch import AuthenticatedTelegramContext, TelegramDispatcher
from app.conversations import ConversationRoute, ConversationRouter
from app.core.intake import IntakeProcessor
from app.core.lifecycle import ProjectLifecycle
from app.core.registry import ConversationRegistryStore
from app.core.state import ProjectStateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CONV = "555"


def _payload(event_id, user=1, chat=CONV, text="hello"):
    return {
        "update_id": event_id,
        "message": {
            "from": {"id": user},
            "chat": {"id": int(chat)},
            "text": text,
            "date": event_id,
        },
    }


def _fast_intake_ready(name="P", what="biz", why="cta"):
    return {
        "scope": "WEBSITE",
        "name": name,
        "what": what,
        "why": why,
        "why_destination": None,
        "ambiguity": None,
        "clarification_needed": False,
        "clarification_question": None,
        "readiness": "DISCOVERY_READY",
    }


def _make(tmp_path):
    """Construct all collaborators without a TelegramReceiveLoop."""
    state_root = tmp_path / "state"
    store = ProjectStateStore(state_root)
    registry = ConversationRegistryStore(state_root / "conversations")
    adapter = MagicMock()
    adapter.fast_interpret.return_value = _fast_intake_ready()
    intake = IntakeProcessor(store, hermes_adapter=adapter)
    builder = MagicMock()
    builder.build.return_value = MagicMock(success=True)
    dispatcher = TelegramDispatcher(
        store, intake, builder=builder, workspace_for=lambda pid: tmp_path / "ws"
    )
    router = ConversationRouter(store, registry, telegram_out=MagicMock())
    auth = AuthenticatedTelegramContext("1", CONV)
    return store, registry, adapter, dispatcher, router, auth


def _create_project(router, dispatcher, store, conv, display_name, auth, event_id):
    """Allocate + materialize a project and return its registry entry."""
    entry = router.materialize_new_project(conv, display_name, event_id=str(event_id))
    dispatcher.dispatch(
        _payload(event_id), entry.project_id, "create", authenticated=auth
    )
    return entry


def _set_lifecycle(store, project_id, target: ProjectLifecycle):
    """Test-only: directly transition project lifecycle."""
    store.transition_lifecycle(project_id, target)


def _set_brief(store, project_id, **fields):
    """Test-only: directly patch brief fields without LLM."""
    with store.acquire_writer(project_id) as state:
        for k, v in fields.items():
            state.brief[k] = v
        store.save(state)


class _FastRouterStub:
    """Router-level FAST stub: _run_fast_programmatic returns a fixed payload."""

    def __init__(self, payload):
        self._response = payload if isinstance(payload, str) else json.dumps(payload)

    def _run_fast_programmatic(self, **_kwargs):
        return MagicMock(success=True, response=self._response)


class _FastRaisesIfCalled:
    """Sentinel: raises if FAST is ever invoked (proves pending gate bypasses it)."""

    def _run_fast_programmatic(self, **_):
        raise AssertionError("FAST must not be called during pending-NAME consumption")


def _router_turn(intent, target=None, proposed=None, confidence="high"):
    return {
        "intent": intent,
        "target_project_name": target,
        "proposed_new_project_name": proposed,
        "confidence": confidence,
    }


def _active_ctx(name, lifecycle="WAITING_INPUT", missing="why"):
    return {
        "active_project_name": name,
        "active_project_lifecycle": lifecycle,
        "next_missing_intake_field": missing,
    }


# ---------------------------------------------------------------------------
# 1. Multi-turn intake loop (primary regression)
# ---------------------------------------------------------------------------


class TestIntakeLoopNoRepeatClarification:
    """Core regression: an intake answer must never loop back to CREATE-vs-MODIFY
    clarification when the active project is in WAITING_INPUT.
    """

    def test_enriched_context_routes_intake_answer_to_active_project(self, tmp_path):
        """FAST receives active project name + lifecycle + missing field.
        With this context it classifies the intake answer as PROJECT_TURN.
        """
        store, registry, _, dispatcher, router, auth = _make(tmp_path)

        entry = _create_project(
            router, dispatcher, store, CONV, "warehousebandung", auth, 1
        )
        _set_lifecycle(store, entry.project_id, ProjectLifecycle.WAITING_INPUT)
        _set_brief(store, entry.project_id, name="warehousebandung", what="bengkel mobil")

        router.hermes = _FastRouterStub(_router_turn("PROJECT_TURN", confidence="high"))

        route = router.route(
            CONV,
            "liat layanan bengkel dan alamat dulu",
            event_id="2",
            active_project_context=_active_ctx("warehousebandung"),
        )

        assert route.route == ConversationRoute.PROJECT
        assert route.project_id == entry.project_id
        # No second project allocated.
        assert len(registry.load(CONV).projects) == 1

    def test_waiting_input_bias_overrides_ambiguous(self, tmp_path):
        """Safety net: FAST misfires with AMBIGUOUS while WAITING_INPUT -> PROJECT."""
        store, registry, _, dispatcher, router, auth = _make(tmp_path)

        entry = _create_project(
            router, dispatcher, store, CONV, "warehousebandung", auth, 1
        )
        _set_lifecycle(store, entry.project_id, ProjectLifecycle.WAITING_INPUT)
        _set_brief(store, entry.project_id, name="warehousebandung", what="bengkel mobil")

        router.hermes = _FastRouterStub(_router_turn("AMBIGUOUS", confidence="high"))

        route = router.route(
            CONV,
            "liat layanan bengkel dan alamat dulu",
            event_id="2",
            active_project_context=_active_ctx("warehousebandung"),
        )

        assert route.route == ConversationRoute.PROJECT
        assert route.project_id == entry.project_id
        assert len(registry.load(CONV).projects) == 1

    def test_waiting_input_bias_overrides_medium_create(self, tmp_path):
        """Safety net: FAST returns CREATE_PROJECT at medium confidence while
        WAITING_INPUT -> bias overrides to PROJECT, no new project allocated.
        """
        store, registry, _, dispatcher, router, auth = _make(tmp_path)

        entry = _create_project(
            router, dispatcher, store, CONV, "warehousebandung", auth, 1
        )
        _set_lifecycle(store, entry.project_id, ProjectLifecycle.WAITING_INPUT)
        _set_brief(store, entry.project_id, name="warehousebandung", what="bengkel mobil")

        router.hermes = _FastRouterStub(
            _router_turn("CREATE_PROJECT", proposed="something", confidence="medium")
        )

        route = router.route(
            CONV,
            "liat layanan bengkel dan alamat dulu",
            event_id="2",
            active_project_context=_active_ctx("warehousebandung"),
        )

        assert route.route == ConversationRoute.PROJECT
        assert route.project_id == entry.project_id
        assert len(registry.load(CONV).projects) == 1

    def test_discovering_lifecycle_does_not_trigger_bias(self, tmp_path):
        """WAITING_INPUT bias is narrowly scoped: DISCOVERING does NOT trigger it."""
        store, registry, _, dispatcher, router, auth = _make(tmp_path)

        _create_project(router, dispatcher, store, CONV, "warehousebandung", auth, 1)
        # Lifecycle remains DISCOVERING (default after create).

        router.hermes = _FastRouterStub(_router_turn("AMBIGUOUS", confidence="high"))

        route = router.route(
            CONV,
            "some ambiguous text",
            event_id="2",
            active_project_context=_active_ctx("warehousebandung", lifecycle="DISCOVERING"),
        )

        # DISCOVERING + AMBIGUOUS -> CLARIFICATION; bias does not fire.
        assert route.route == ConversationRoute.CLARIFICATION


# ---------------------------------------------------------------------------
# 2. pending_action round-trip (crash-safe ordering)
# ---------------------------------------------------------------------------


class TestPendingClarificationSequence:
    """Correct trigger for pending_action: CREATE_PROJECT(high) + proposed=null.
    AMBIGUOUS alone MUST NOT set pending.
    """

    def test_ambiguous_alone_does_not_set_pending_action(self, tmp_path):
        """AMBIGUOUS intent must never set pending_action=CREATE_PROJECT/NAME."""
        store, registry, _, dispatcher, router, auth = _make(tmp_path)
        _create_project(router, dispatcher, store, CONV, "warehousebandung", auth, 1)

        router.hermes = _FastRouterStub(_router_turn("AMBIGUOUS", confidence="high"))
        router.route(CONV, "entah lah pokoknya website baru", event_id="2")

        assert registry.load(CONV).pending_action is None

    def test_create_high_no_name_sets_pending_action(self, tmp_path):
        """CREATE_PROJECT(high) + proposed=null -> pending_action persisted.

        The test text must not contain a token that extract_new_project_name
        can extract (otherwise _route_new_project skips the pending path and
        allocates directly with the extracted token).
        """
        store, registry, _, dispatcher, router, auth = _make(tmp_path)
        _create_project(router, dispatcher, store, CONV, "existing-project", auth, 1)

        router.hermes = _FastRouterStub(
            _router_turn("CREATE_PROJECT", proposed=None, confidence="high")
        )
        route = router.route(
            CONV, "bikin website baru", event_id="2"
        )

        assert route.route == ConversationRoute.CLARIFICATION
        pending = registry.load(CONV).pending_action
        assert pending["action"] == "CREATE_PROJECT"
        assert pending["awaiting"] == "NAME"

    def test_pending_name_consumed_before_fast_and_clears(self, tmp_path):
        """After pending is set, the next turn bypasses FAST, allocates exactly
        one project, and clears pending_action.
        """
        store, registry, _, dispatcher, router, auth = _make(tmp_path)
        _create_project(router, dispatcher, store, CONV, "existing-project", auth, 1)
        n_before = len(registry.load(CONV).projects)

        # Turn 1: router asks for name, pending set.
        # Text must not contain a token that extract_new_project_name extracts
        # (e.g. avoid trailing nouns like "nanti" which the extractor picks up).
        router.hermes = _FastRouterStub(
            _router_turn("CREATE_PROJECT", proposed=None, confidence="high")
        )
        route1 = router.route(
            CONV, "bikin website baru", event_id="2"
        )
        assert route1.route == ConversationRoute.CLARIFICATION
        pending1 = registry.load(CONV).pending_action
        assert pending1["action"] == "CREATE_PROJECT"
        assert pending1["awaiting"] == "NAME"

        # Turn 2: user supplies the name. Sentinel proves FAST is bypassed.
        router.hermes = _FastRaisesIfCalled()
        route2 = router.route(CONV, "warehousebandung", event_id="3")

        assert route2.route == ConversationRoute.NEW_PROJECT
        assert route2.entry is not None
        assert route2.entry.display_name == "warehousebandung"
        assert len(registry.load(CONV).projects) == n_before + 1
        # pending_action cleared after allocation.
        assert registry.load(CONV).pending_action is None
        assert registry.active_project_id(CONV) == route2.entry.project_id

    def test_pending_cleared_only_after_allocation_crash_safe_ordering(self, tmp_path):
        """Directly inject pending_action (simulating previous turn) and confirm
        allocation completes and pending is cleared in the correct order.

        Crash-safety invariant: if clear happened before allocation (old code),
        a crash between them leaves pending=None + no project = BAD.
        Fixed code: allocate first, then clear. A crash between leaves
        pending still set but project exists -- next retry recovers via
        resolve_name (duplicate guard) and clears pending.
        """
        store, registry, _, dispatcher, router, auth = _make(tmp_path)
        registry.set_pending_action(CONV, {"action": "CREATE_PROJECT", "awaiting": "NAME"})
        assert registry.load(CONV).pending_action is not None

        router.hermes = None  # FAST unavailable; pending gate is pre-FAST

        route = router.route(CONV, "warehousebandung", event_id="5")

        assert route.route == ConversationRoute.NEW_PROJECT
        # pending cleared after allocation.
        assert registry.load(CONV).pending_action is None
        # Project exists in registry.
        result = registry.resolve_name(CONV, "warehousebandung")
        assert result.status == "ok"

    def test_full_sequence_intake_answer_never_asks_create_vs_modify(self, tmp_path):
        """Full bug-report sequence (corrected trigger):
          1. Pending set (simulating CREATE_PROJECT+no name turn).
          2. "warehousebandung" -> consumed as name, project allocated.
          3. "liat layanan bengkel dan alamat dulu" with WAITING_INPUT active
             -> PROJECT, no CLARIFICATION fired.
        """
        store, registry, _, dispatcher, router, auth = _make(tmp_path)

        # Turn 1: inject pending directly.
        registry.set_pending_action(CONV, {"action": "CREATE_PROJECT", "awaiting": "NAME"})

        router.hermes = None
        route1 = router.route(CONV, "warehousebandung", event_id="2")
        assert route1.route == ConversationRoute.NEW_PROJECT
        pid = route1.entry.project_id

        # Materialize so _materialized() returns True.
        dispatcher.dispatch(_payload(2), pid, "create", authenticated=auth)

        _set_lifecycle(store, pid, ProjectLifecycle.WAITING_INPUT)
        _set_brief(store, pid, name="warehousebandung", what="bengkel mobil")

        # Turn 2: intake answer with enriched context.
        router.hermes = _FastRouterStub(_router_turn("PROJECT_TURN", confidence="high"))
        route2 = router.route(
            CONV,
            "liat layanan bengkel dan alamat dulu",
            event_id="3",
            active_project_context=_active_ctx("warehousebandung"),
        )

        assert route2.route == ConversationRoute.PROJECT
        assert route2.project_id == pid
        # No CLARIFICATION fired; no second project allocated.
        assert len(registry.load(CONV).projects) == 1


# ---------------------------------------------------------------------------
# 3. High-confidence CREATE while active project is in WAITING_INPUT
# ---------------------------------------------------------------------------


class TestNewProjectWhileActiveIntake:
    """High-confidence CREATE_PROJECT must still proceed even when the active
    project is in WAITING_INPUT.  The bias rule only fires for AMBIGUOUS or
    non-high CREATE.
    """

    def test_high_confidence_create_while_waiting_input_allocates_new_project(
        self, tmp_path
    ):
        """Explicit new website request while warehousebandung is in WAITING_INPUT.
        FAST high-confidence CREATE -> NEW_PROJECT; warehousebandung untouched.
        """
        store, registry, _, dispatcher, router, auth = _make(tmp_path)

        entry = _create_project(
            router, dispatcher, store, CONV, "warehousebandung", auth, 1
        )
        _set_lifecycle(store, entry.project_id, ProjectLifecycle.WAITING_INPUT)
        _set_brief(store, entry.project_id, name="warehousebandung", what="bengkel mobil")

        state_before = store.load(entry.project_id)

        router.hermes = _FastRouterStub(
            _router_turn("CREATE_PROJECT", proposed="toko bunga", confidence="high")
        )
        route = router.route(
            CONV,
            "sekarang bikin website baru untuk toko bunga",
            event_id="2",
            active_project_context=_active_ctx("warehousebandung"),
        )

        # Bias must NOT fire; NEW_PROJECT reached.
        assert route.route == ConversationRoute.NEW_PROJECT
        assert route.entry is not None
        assert route.entry.display_name == "toko bunga"
        assert route.entry.project_id != entry.project_id

        # warehousebandung completely untouched.
        state_after = store.load(entry.project_id)
        assert state_after.lifecycle == state_before.lifecycle
        assert state_after.brief == state_before.brief

        assert len(registry.load(CONV).projects) == 2

    def test_high_confidence_create_no_name_sets_pending_not_bias_not_project(
        self, tmp_path
    ):
        """High-confidence CREATE without a proposed name -> pending set (ask for
        name), NOT overridden to the existing WAITING_INPUT project.
        The bias rule fires only for AMBIGUOUS/non-high CREATE.
        """
        store, registry, _, dispatcher, router, auth = _make(tmp_path)

        entry = _create_project(
            router, dispatcher, store, CONV, "warehousebandung", auth, 1
        )
        _set_lifecycle(store, entry.project_id, ProjectLifecycle.WAITING_INPUT)

        router.hermes = _FastRouterStub(
            _router_turn("CREATE_PROJECT", proposed=None, confidence="high")
        )
        route = router.route(
            CONV,
            "mau bikin website baru",
            event_id="2",
            active_project_context=_active_ctx("warehousebandung"),
        )

        assert route.route == ConversationRoute.CLARIFICATION
        pending = registry.load(CONV).pending_action
        assert pending["action"] == "CREATE_PROJECT"
        assert pending["awaiting"] == "NAME"
        # No extra project allocated.
        assert len(registry.load(CONV).projects) == 1


# ---------------------------------------------------------------------------
# 4. Crash safety of pending NAME consumption
# ---------------------------------------------------------------------------


class TestPendingNameCrashRecovery:
    def test_pending_name_crash_before_materialization_recovers_on_replay(
        self, tmp_path
    ):
        """Regression test for crash safety of pending NAME consumption:
        pending CREATE_PROJECT / awaiting NAME
        -> user sends "warehousebandung" (event_id="5")
        -> project identity allocated + event replay mapping recorded + pending cleared atomically
        -> simulated crash BEFORE ProjectState materialization
        -> Telegram replays same event_id="5"
        -> same project_id recovered (NEW_PROJECT route with recovered id)
        -> project count remains exactly one (no p2 allocated)
        -> pending state does not reopen incorrectly (remains None)
        -> ProjectState then materialized via runtime/dispatcher
        """
        store, registry, _, dispatcher, router, auth = _make(tmp_path)

        # 1. Setup: conversation is waiting for project NAME.
        registry.set_pending_action(
            CONV, {"action": "CREATE_PROJECT", "awaiting": "NAME"}
        )
        assert registry.load(CONV).pending_action == {
            "action": "CREATE_PROJECT",
            "awaiting": "NAME",
        }

        # 2. Turn 1: user sends "warehousebandung" with event_id="5".
        # Router consumes pending NAME, allocates project, records event mapping,
        # and clears pending_action — all in ONE locked atomic save.
        route1 = router.route(CONV, "warehousebandung", event_id="5")
        assert route1.route == ConversationRoute.NEW_PROJECT
        allocated_pid = route1.project_id or route1.entry.project_id
        assert allocated_pid is not None
        assert route1.entry.display_name == "warehousebandung"

        # Verify atomic registry state immediately after allocation:
        reg_after_alloc = registry.load(CONV)
        assert len(reg_after_alloc.projects) == 1
        assert reg_after_alloc.active_project_id == allocated_pid
        assert reg_after_alloc.event_projects.get("5") == allocated_pid
        assert reg_after_alloc.pending_action is None

        # 3. Simulated crash: do NOT call dispatcher.dispatch("create").
        # The ProjectState file does NOT exist on disk.
        assert store.load(allocated_pid) is None

        # 4. Telegram retries the exact same event (event_id="5").
        # Even if FAST is stubbed to return AMBIGUOUS or CREATE_PROJECT,
        # the replay guard must deterministically recover the allocated identity.
        router.hermes = _FastRouterStub(
            _router_turn("AMBIGUOUS", confidence="low")
        )
        route2 = router.route(CONV, "warehousebandung", event_id="5")

        # 5. Verify recovery invariants:
        # - SAME project_id recovered
        assert route2.route == ConversationRoute.NEW_PROJECT
        assert route2.project_id == allocated_pid
        assert route2.entry.project_id == allocated_pid
        assert "Lagi beresin project kamu" in (route2.clarification or "")

        # - Project count remains exactly ONE (no second allocation / no p2)
        reg_after_replay = registry.load(CONV)
        assert len(reg_after_replay.projects) == 1
        assert store.load(f"tg-{CONV}-p2") is None

        # - Pending state does not reopen incorrectly
        assert reg_after_replay.pending_action is None

        # 6. Materialize: dispatcher runs "create" for the recovered project.
        dispatcher.dispatch(
            _payload(5), allocated_pid, "create", authenticated=auth
        )
        assert store.load(allocated_pid) is not None

        # 7. Post-materialization replay: a third delivery of event_id="5"
        # routes to PROJECT (not NEW_PROJECT) since it is now materialized.
        route3 = router.route(CONV, "warehousebandung", event_id="5")
        assert route3.route == ConversationRoute.PROJECT
        assert route3.project_id == allocated_pid
        assert len(registry.load(CONV).projects) == 1

    def test_pending_name_crash_e2e_via_runtime_loop(self, tmp_path):
        """E2E test through TelegramReceiveLoop:
        Telegram sends event 5 with name -> simulated crash before dispatch ->
        Telegram replays event 5 -> loop processes update -> state materialized
        with single project identity."""
        from unittest.mock import MagicMock
        from app.core.state import ProjectStateStore
        from app.core.intake import IntakeProcessor
        from app.channels.dispatch import TelegramDispatcher
        from app.runtime import TelegramReceiveLoop

        state_root = tmp_path / "state"
        store = ProjectStateStore(state_root)
        registry = ConversationRegistryStore(state_root / "conversations")
        adapter = MagicMock()
        fast_ret = MagicMock()
        fast_ret.is_empty = False
        fast_ret.decision = "READY"
        fast_ret.reasoning = "ready"
        fast_ret.extracted_fields = {"name": "warehousebandung"}
        adapter.fast_interpret.return_value = fast_ret
        adapter._run_fast_programmatic.return_value = MagicMock(success=False)
        intake = IntakeProcessor(store, hermes_adapter=adapter)
        builder = MagicMock()
        builder.build.return_value = MagicMock(success=True)
        dispatcher = TelegramDispatcher(
            store, intake, builder=builder,
            workspace_for=lambda pid: tmp_path / "ws"
        )

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

        # Pending set
        registry.set_pending_action(CONV, {"action": "CREATE_PROJECT", "awaiting": "NAME"})

        # First delivery allocates, but simulates crash before dispatch
        route1 = router.route(CONV, "warehousebandung", event_id="5")
        allocated_pid = route1.project_id
        assert store.load(allocated_pid) is None

        # Replay arrives via loop._process_update
        loop._process_update(_payload(5, text="warehousebandung"))

        # State materialized under original identity
        assert store.load(allocated_pid) is not None
        assert len(registry.load(CONV).projects) == 1
        assert registry.load(CONV).pending_action is None

