"""Integration tests for the unified publish dispatch action.

Uses the REAL TelegramDispatcher and ProjectStateStore to prove:
1. One PUBLISH Telegram update calls approve() exactly once
2. promote() exactly once
3. Only one dispatch_events entry is created
4. Replay of the same update does not re-approve or re-promote
5. Approval failure prevents promote()
6. Stale preview protection still works
7. Unauthorized publish still fails
8. EVENT_ACTION_MISMATCH behavior remains intact
9. Ordinary explicit dispatcher publish behavior remains correct
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.channels.dispatch import AuthenticatedTelegramContext, TelegramDispatcher
from app.core.contracts import OperationResult
from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(tmp_path):
    return ProjectStateStore(tmp_path / "state")


def _make_dispatcher(store, promote, workspace_root):
    return TelegramDispatcher(
        store,
        None,
        promote=promote,
        workspace_for=lambda pid: workspace_root / pid,
    )


def _publish_payload(update_id=1, user_id=1, chat_id=555, text="oke live"):
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": user_id},
            "chat": {"id": chat_id},
            "text": text,
            "date": 1,
        },
    }


def _auth(user_id="1", chat_id="555"):
    return AuthenticatedTelegramContext(user_id, chat_id)


def _setup_preview_ready(store, project_id, owner="telegram:1"):
    with store.acquire_writer(project_id) as state:
        state.lifecycle = ProjectLifecycle.PREVIEW_READY.value
        state.roles["owner"] = owner
        state.revisions.source_revision = 1
        state.revisions.qa_revision = 1
        state.revisions.preview_revision = 1
        state.deployment["latest_shown_preview"] = {
            "operation_id": "op-1",
            "source_revision": 1,
            "preview_url": "https://test.vercel.app",
            "deployment_id": "dpl_1",
            "source_sha256": "a" * 64,
            "artifact_sha256": "b" * 64,
        }
        store.save(state)


def _setup_stale_preview(store, project_id, owner="telegram:1"):
    """Set up a project where the shown preview is stale relative to revisions."""
    with store.acquire_writer(project_id) as state:
        state.lifecycle = ProjectLifecycle.PREVIEW_READY.value
        state.roles["owner"] = owner
        state.revisions.source_revision = 2
        state.revisions.qa_revision = 2
        state.revisions.preview_revision = 2
        state.deployment["latest_shown_preview"] = {
            "operation_id": "op-1",
            "source_revision": 1,
            "preview_url": "https://test.vercel.app",
            "deployment_id": "dpl_1",
            "source_sha256": "a" * 64,
            "artifact_sha256": "b" * 64,
        }
        store.save(state)


# ---------------------------------------------------------------------------
# 1. One PUBLISH update calls approve() exactly once and promote() exactly once
# ---------------------------------------------------------------------------


class TestPublishSingleDispatch:
    def test_approve_and_promote_each_called_once(self, tmp_path):
        store = _make_store(tmp_path)
        promote = MagicMock()
        promote.approve.return_value = OperationResult.ok({"operation_id": "op-1"})
        promote.promote.return_value = OperationResult.ok({"production_url": "https://prod.vercel.app"})
        dispatcher = _make_dispatcher(store, promote, tmp_path / "workspaces")

        dispatcher.dispatch(_publish_payload(), "tg-555", "create", authenticated=_auth())
        _setup_preview_ready(store, "tg-555")

        result = dispatcher.dispatch(_publish_payload(), "tg-555", "publish", authenticated=_auth())

        assert result.success
        assert promote.approve.call_count == 1
        assert promote.promote.call_count == 1

    def test_only_one_dispatch_event_created(self, tmp_path):
        store = _make_store(tmp_path)
        promote = MagicMock()
        promote.approve.return_value = OperationResult.ok({"operation_id": "op-1"})
        promote.promote.return_value = OperationResult.ok({"production_url": "https://prod.vercel.app"})
        dispatcher = _make_dispatcher(store, promote, tmp_path / "workspaces")

        dispatcher.dispatch(_publish_payload(), "tg-555", "create", authenticated=_auth())
        _setup_preview_ready(store, "tg-555")

        dispatcher.dispatch(_publish_payload(), "tg-555", "publish", authenticated=_auth())

        state = store.load("tg-555")
        assert len(state.dispatch_events) == 1
        event = next(iter(state.dispatch_events.values()))
        assert event["action"] == "publish"
        assert event["status"] == "DONE"


# ---------------------------------------------------------------------------
# 2. Replay of the same update does not re-approve or re-promote
# ---------------------------------------------------------------------------


class TestPublishReplayIdempotency:
    def test_replay_does_not_reapprove_or_repromote(self, tmp_path):
        store = _make_store(tmp_path)
        promote = MagicMock()
        promote.approve.return_value = OperationResult.ok({"operation_id": "op-1"})
        promote.promote.return_value = OperationResult.ok({"production_url": "https://prod.vercel.app"})
        dispatcher = _make_dispatcher(store, promote, tmp_path / "workspaces")

        dispatcher.dispatch(_publish_payload(), "tg-555", "create", authenticated=_auth())
        _setup_preview_ready(store, "tg-555")

        r1 = dispatcher.dispatch(_publish_payload(), "tg-555", "publish", authenticated=_auth())
        assert r1.success

        r2 = dispatcher.dispatch(_publish_payload(), "tg-555", "publish", authenticated=_auth())
        assert r2.success
        assert r2.data.get("duplicate") is True

        assert promote.approve.call_count == 1
        assert promote.promote.call_count == 1

        state = store.load("tg-555")
        assert len(state.dispatch_events) == 1


# ---------------------------------------------------------------------------
# 3. Approval failure prevents promote()
# ---------------------------------------------------------------------------


class TestPublishApprovalFailure:
    def test_approval_failure_prevents_promote(self, tmp_path):
        store = _make_store(tmp_path)
        promote = MagicMock()
        promote.approve.return_value = OperationResult.fail("STALE_APPROVAL", error_code="STALE_APPROVAL")
        dispatcher = _make_dispatcher(store, promote, tmp_path / "workspaces")

        dispatcher.dispatch(_publish_payload(), "tg-555", "create", authenticated=_auth())
        _setup_preview_ready(store, "tg-555")

        result = dispatcher.dispatch(_publish_payload(), "tg-555", "publish", authenticated=_auth())

        assert not result.success
        assert result.error_code == "STALE_APPROVAL"
        assert promote.approve.call_count == 1
        assert promote.promote.call_count == 0

        state = store.load("tg-555")
        event = next(iter(state.dispatch_events.values()))
        assert event["status"] == "FAILED"


# ---------------------------------------------------------------------------
# 4. Stale preview protection still works
# ---------------------------------------------------------------------------


class TestPublishStalePreviewProtection:
    def test_stale_preview_blocked(self, tmp_path):
        store = _make_store(tmp_path)
        promote = MagicMock()
        promote.approve.return_value = OperationResult.fail("STALE_QA_BINDING", error_code="STALE_QA_BINDING")
        dispatcher = _make_dispatcher(store, promote, tmp_path / "workspaces")

        dispatcher.dispatch(_publish_payload(), "tg-555", "create", authenticated=_auth())
        _setup_stale_preview(store, "tg-555")

        result = dispatcher.dispatch(_publish_payload(), "tg-555", "publish", authenticated=_auth())

        assert not result.success
        assert result.error_code == "STALE_QA_BINDING"
        assert promote.promote.call_count == 0


# ---------------------------------------------------------------------------
# 5. Unauthorized publish still fails
# ---------------------------------------------------------------------------


class TestPublishUnauthorized:
    def test_unauthorized_publish_rejected(self, tmp_path):
        store = _make_store(tmp_path)
        promote = MagicMock()
        dispatcher = _make_dispatcher(store, promote, tmp_path / "workspaces")

        dispatcher.dispatch(_publish_payload(), "tg-555", "create", authenticated=_auth())
        _setup_preview_ready(store, "tg-555", owner="telegram:999")

        stranger = _auth(user_id="111", chat_id="222")
        result = dispatcher.dispatch(_publish_payload(user_id=111, chat_id=222), "tg-555", "publish", authenticated=stranger)

        assert not result.success
        assert result.error_code == "UNAUTHORIZED_ROLE"
        assert promote.approve.call_count == 0
        assert promote.promote.call_count == 0


# ---------------------------------------------------------------------------
# 6. EVENT_ACTION_MISMATCH behavior remains intact
# ---------------------------------------------------------------------------


class TestPublishEventActionMismatch:
    def test_same_event_different_action_rejected(self, tmp_path):
        store = _make_store(tmp_path)
        promote = MagicMock()
        promote.approve.return_value = OperationResult.ok({"operation_id": "op-1"})
        promote.promote.return_value = OperationResult.ok({"production_url": "https://prod.vercel.app"})
        dispatcher = _make_dispatcher(store, promote, tmp_path / "workspaces")

        dispatcher.dispatch(_publish_payload(), "tg-555", "create", authenticated=_auth())
        _setup_preview_ready(store, "tg-555")

        # First dispatch: publish
        r1 = dispatcher.dispatch(_publish_payload(), "tg-555", "publish", authenticated=_auth())
        assert r1.success

        # Same event, different action
        r2 = dispatcher.dispatch(_publish_payload(), "tg-555", "approve", authenticated=_auth())
        assert not r2.success
        assert r2.error_code == "EVENT_ACTION_MISMATCH"


# ---------------------------------------------------------------------------
# 7. Ordinary explicit dispatcher publish behavior remains correct
# ---------------------------------------------------------------------------


class TestExplicitDispatcherPublish:
    def test_explicit_publish_calls_approve_then_promote(self, tmp_path):
        """Direct dispatch("publish") performs approve-then-promote atomically."""
        store = _make_store(tmp_path)
        promote = MagicMock()
        promote.approve.return_value = OperationResult.ok({"operation_id": "op-1"})
        promote.promote.return_value = OperationResult.ok({"production_url": "https://prod.vercel.app"})
        dispatcher = _make_dispatcher(store, promote, tmp_path / "workspaces")

        dispatcher.dispatch(_publish_payload(), "tg-555", "create", authenticated=_auth())
        _setup_preview_ready(store, "tg-555")

        result = dispatcher.dispatch(_publish_payload(), "tg-555", "publish", authenticated=_auth())

        assert result.success
        assert promote.approve.call_count == 1
        assert promote.promote.call_count == 1
        # Verify approve was called before promote
        assert promote.method_calls[0][0] == "approve"
        assert promote.method_calls[1][0] == "promote"

    def test_explicit_publish_with_workspace(self, tmp_path):
        """Publish passes the correct workspace to promote()."""
        store = _make_store(tmp_path)
        promote = MagicMock()
        promote.approve.return_value = OperationResult.ok({"operation_id": "op-1"})
        promote.promote.return_value = OperationResult.ok({"production_url": "https://prod.vercel.app"})
        workspace_root = tmp_path / "workspaces"
        dispatcher = _make_dispatcher(store, promote, workspace_root)

        dispatcher.dispatch(_publish_payload(), "tg-555", "create", authenticated=_auth())
        _setup_preview_ready(store, "tg-555")

        dispatcher.dispatch(_publish_payload(), "tg-555", "publish", authenticated=_auth())

        promote.promote.assert_called_once_with("tg-555", workspace_root / "tg-555", principal_id="telegram:1")
