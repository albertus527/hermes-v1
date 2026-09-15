"""Behavioral dispatch claims, races and prebuild admission; offline only."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import MagicMock

import pytest
from app.channels.dispatch import TelegramDispatcher, AuthenticatedTelegramContext
from app.core.contracts import OperationResult
from app.core.state import ProjectStateStore
from app.projects.directions import DirectionsOrchestrator


def payload(user=1, chat=555, event=1):
    return {"update_id": event, "message": {"from": {"id": user}, "chat": {"id": chat}, "text": "content"}}


def owned(tmp_path):
    store = ProjectStateStore(tmp_path / "state")
    with store.acquire_writer("app") as state:
        state.roles = {"owner": "telegram:1", "reviewers": ["telegram:2"], "viewers": []}
        state.brief = {"name": "Persisted", "what": "shop", "why": "visit"}
        store.save(state)
    return store


def test_concurrent_and_restart_claim_precedes_effect(tmp_path):
    store = owned(tmp_path)
    entered, release = Event(), Event()
    refs = MagicMock()
    def effect(*args, **kwargs):
        assert next(iter(ProjectStateStore(store.root).load("app").dispatch_events.values()))["status"] == "CLAIMED"
        entered.set()
        assert release.wait(5)
        raise TimeoutError("ambiguous effect")
    refs.add_upload.side_effect = effect
    def call():
        return TelegramDispatcher(ProjectStateStore(store.root), None, reference_intake=refs).dispatch(
            payload(), "app", "reference_upload", authenticated=AuthenticatedTelegramContext("1", "555"))
    with ThreadPoolExecutor(2) as pool:
        future = pool.submit(call)
        try:
            assert entered.wait(5)
            assert call().error_code == "EVENT_RECONCILIATION_REQUIRED"
        finally:
            release.set()
        assert future.result().error_code == "EVENT_RECONCILIATION_REQUIRED"
    assert call().error_code == "EVENT_RECONCILIATION_REQUIRED"
    assert refs.add_upload.call_count == 1


def test_claim_scope_and_action_cannot_be_repurposed(tmp_path):
    store = owned(tmp_path)
    refs = MagicMock()
    refs.add_upload.return_value = OperationResult.ok()
    dispatch = TelegramDispatcher(store, None, reference_intake=refs)
    for user, chat in [(1, 555), (2, 555), (1, 777)]:
        result = dispatch.dispatch(payload(user, chat), "app", "reference_upload",
            authenticated=AuthenticatedTelegramContext(str(user), str(chat)))
        assert result.success
    assert refs.add_upload.call_count == 3
    assert dispatch.dispatch(payload(), "app", "reference_url",
        authenticated=AuthenticatedTelegramContext("1", "555")).error_code == "EVENT_ACTION_MISMATCH"
    refs.add_url.assert_not_called()


@pytest.mark.parametrize("lifecycle", ["QUEUED", "RUNNING", "PREVIEW_READY", "REVISION_REQUESTED", "PUBLISHING", "LIVE", "FAILED", "PAUSED", "CANCELED", "WAITING_INPUT"])
def test_directions_reject_non_prebuild_lifecycle(tmp_path, lifecycle):
    store = owned(tmp_path)
    with store.acquire_writer("app") as state:
        state.lifecycle = lifecycle
        state.design_directions = [{"label": "A"}, {"label": "B"}]
        store.save(state)
    adapter = MagicMock()
    directions = DirectionsOrchestrator(store, adapter)
    before = store._project_path("app").read_bytes()
    assert directions.propose("app", {}, tmp_path, principal_id="telegram:1").error_code == "DIRECTIONS_NOT_ALLOWED_IN_LIFECYCLE"
    assert directions.choose_direction("app", 0, principal_id="telegram:1").error_code == "DIRECTIONS_NOT_ALLOWED_IN_LIFECYCLE"
    assert store._project_path("app").read_bytes() == before
    assert not adapter.mock_calls


@pytest.mark.parametrize("reference", [False, True])
def test_persisted_bypass_not_external_brief(tmp_path, reference):
    store = owned(tmp_path)
    with store.acquire_writer("app") as state:
        if reference:
            state.design_references = {"UX": {"evidence": "navigation"}}
        else:
            state.brief["design_authority_delegated"] = True
        store.save(state)
    adapter = MagicMock()
    result = DirectionsOrchestrator(store, adapter).propose("app", {}, tmp_path, principal_id="telegram:1")
    assert result.success and result.directions == []
    assert not adapter.mock_calls


def test_proposal_uses_persisted_brief_not_external_delegation(tmp_path):
    store = owned(tmp_path)
    adapter = MagicMock()
    adapter.frontend_propose_directions.return_value = {"success": True, "directions": [{"label": "A"}, {"label": "B"}]}
    result = DirectionsOrchestrator(store, adapter).propose("app", {"design_authority_delegated": True, "name": "forged"}, tmp_path, principal_id="telegram:1")
    assert result.success
    assert adapter.frontend_propose_directions.call_args.kwargs["brief"] == store.load("app").brief
