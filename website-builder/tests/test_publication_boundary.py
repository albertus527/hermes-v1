"""Regression tests for the scoped publication safety fixes (Fix A + Fix B).

WHY
---
A live staging E2E (Forma Atelier, project ``tg-6329821361-p1``) sent an owner
message whose text was literally ``approve``. Because approval IS publication in
this codebase, the runtime recorded the approval, emitted the progress
acknowledgement ``"✅ Preview approved. Publishing..."`` and entered the promote
flow -- despite the environment being authorized for Preview ONLY. The promote
failed closed (identity lookup 403), so nothing was promoted and the second
message was the honest "publish could not be verified" copy. Two defects:

  * Fix A -- identity consistency. The staging ``app_id_for`` / ``friendly_slug``
    gating added by the earlier preview fix was wired into ``preview_deps`` only.
    ``promote_deps`` and ``domain_deps`` still derived the WRONG app id and
    consulted the friendly slug, so a promotion could never resolve the pinned
    opaque project. The same gating must apply to all three.
  * Fix B -- explicit publication boundary. There was no switch that made an
    APPROVE record the approval WITHOUT attempting any publish/promotion side
    effect, and the "Publishing..." acknowledgement fired unconditionally.

These tests assert the boundary end-to-end through the REAL
TelegramReceiveLoop -> TelegramDispatcher -> PromotionOrchestrator chain, plus a
real ``compose()`` wiring test for the identity consistency, and a ``main()``
level test for the operator resume path. Offline only: no network, no Hermes, no
Vercel, no browser.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.channels.dispatch import (  # noqa: E402
    AuthenticatedTelegramContext,
    TelegramDispatcher,
)
from app.core.contracts import OperationResult  # noqa: E402
from app.core.lifecycle import ProjectLifecycle  # noqa: E402
from app.core.state import ProjectStateStore  # noqa: E402
from app.projects.promote import PromoteDeps, PromotionOrchestrator  # noqa: E402
from app.runtime import (  # noqa: E402
    _APPROVAL_ACK_APPROVED_ONLY,
    _APPROVAL_ACK_PUBLISHING,
    TelegramReceiveLoop,
)

from test_promote import (  # noqa: E402
    OWNER,
    FakeSmoke,
    FakeTelegram,
    FakeVercel,
    _approved_state,
    _make_workspace,
)

BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
CHAT = "555"
PROJECT_ID = "tg-555"
PUBLISHING_MARKER = "Publishing"
APPROVED_ONLY_MARKER = "belum live"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _payload(event_id, text, user="1", chat=CHAT):
    return {
        "update_id": event_id,
        "message": {
            "from": {"id": int(user)},
            "chat": {"id": int(chat)},
            "text": text,
            "date": event_id,
        },
    }


def _preview_ready_project(store, project_id=PROJECT_ID, source_revision=1):
    _approved_state(store, project_id, source_revision=source_revision)
    with store.acquire_writer(project_id) as state:
        state.roles["owner"] = "telegram:1"
        # The real runtime resolves the chat from the project's own
        # conversation_id, so the acknowledgement path needs it set.
        state.conversation_id = CHAT
        store.save(state)


def _make(tmp_path, *, publication_enabled, vercel=None):
    """Build the REAL receive loop -> dispatcher -> promotion chain.

    Only the Telegram transport is faked; the Vercel boundary is the shared
    ``FakeVercel`` double, which records every ``promote_deployment`` call.
    """
    store = ProjectStateStore(tmp_path / "state")
    hermes = MagicMock()
    hermes._run_fast_programmatic.return_value = MagicMock(
        success=True, response="APPROVE",
    )
    vercel = vercel or FakeVercel()
    out = FakeTelegram()
    promote = PromotionOrchestrator(
        MagicMock(), store,
        PromoteDeps(
            vercel=vercel, telegram=out, smoke=FakeSmoke(),
            chat_id_for=lambda pid, st: CHAT,
        ),
    )
    dispatcher = TelegramDispatcher(
        store, MagicMock(), promote=promote,
        workspace_for=lambda pid: _make_workspace(tmp_path),
        publication_enabled=publication_enabled,
    )
    loop = TelegramReceiveLoop(bot_token=BOT_TOKEN, dispatcher=dispatcher,
                               telegram_out=out, hermes=hermes)
    return loop, store, out, promote, vercel


def _acks(out):
    return [t for _c, t in out.sent if t.startswith("✅ Preview approved")]


# ---------------------------------------------------------------------------
# Fix B -- staging APPROVE records approval, attempts NO promotion
# ---------------------------------------------------------------------------

class TestStagingApproveRecordsOnly:

    def test_approve_stores_approval_and_never_promotes(self, tmp_path):
        loop, store, out, promote, vercel = _make(
            tmp_path, publication_enabled=False)
        _preview_ready_project(store)

        loop._process_update(_payload(401, "approve"))

        state = store.load(PROJECT_ID)
        # The approval IS durably recorded (existing lifecycle/snapshot
        # contracts permit it)...
        assert state.revisions.approved_revision == 1
        assert state.deployment["approval"]["operation_id"] == "op-1"
        assert state.deployment["approval"]["deployment_id"] == "dpl_1"
        # ...but NOTHING was published and the preview is untouched.
        assert state.lifecycle == ProjectLifecycle.PREVIEW_READY.value
        assert state.revisions.live_revision == 0
        assert state.production_url is None
        # No remote promote was ever attempted.
        assert vercel.promote_calls == []

    def test_staging_approve_never_says_publishing(self, tmp_path):
        loop, store, out, promote, vercel = _make(
            tmp_path, publication_enabled=False)
        _preview_ready_project(store)

        loop._process_update(_payload(402, "approve"))

        acks = _acks(out)
        assert len(acks) == 1, "an approval must be acknowledged once"
        assert PUBLISHING_MARKER not in acks[0], (
            "staging must NEVER emit the 'Publishing...' acknowledgement")
        assert acks[0] == _APPROVAL_ACK_APPROVED_ONLY
        # And no LIVE copy was ever sent.
        assert [t for _c, t in out.sent if "Live" in t] == []

    def test_partial_approval_without_publication_state(self, tmp_path):
        """Accurate handling of the approval-without-publication state."""
        loop, store, out, promote, vercel = _make(
            tmp_path, publication_enabled=False)
        _preview_ready_project(store)

        loop._process_update(_payload(403, "approve"))

        state = store.load(PROJECT_ID)
        # APPROVED but explicitly NOT PUBLISHING / NOT PUBLISHED / NOT LIVE.
        assert state.revisions.approved_revision == 1
        assert state.revisions.live_revision == 0
        assert state.lifecycle not in (
            ProjectLifecycle.PUBLISHING.value,
            ProjectLifecycle.LIVE.value,
        )
        assert state.deployment.get("promotion_intent") is None
        assert vercel.promote_calls == []

    def test_repeated_approve_is_idempotent_and_never_promotes(self, tmp_path):
        loop, store, out, promote, vercel = _make(
            tmp_path, publication_enabled=False)
        _preview_ready_project(store)

        loop._process_update(_payload(404, "approve"))
        # A genuinely different update that re-approves the SAME identity.
        loop._process_update(_payload(405, "oke approve"))

        state = store.load(PROJECT_ID)
        assert state.revisions.approved_revision == 1
        assert state.revisions.live_revision == 0
        assert vercel.promote_calls == []
        # At-most-once acknowledgement per approved identity.
        assert len(_acks(out)) == 1

    def test_replayed_update_is_idempotent(self, tmp_path):
        loop, store, out, promote, vercel = _make(
            tmp_path, publication_enabled=False)
        _preview_ready_project(store)

        loop._process_update(_payload(406, "approve"))
        loop._process_update(_payload(406, "approve"))  # exact replay

        assert store.load(PROJECT_ID).revisions.live_revision == 0
        assert vercel.promote_calls == []
        assert len(_acks(out)) == 1


# ---------------------------------------------------------------------------
# Fix B -- direct PUBLISH blocked BEFORE any remote call
# ---------------------------------------------------------------------------

class TestDirectPublishBlocked:

    def _dispatcher(self, store, promote, tmp_path, *, publication_enabled):
        return TelegramDispatcher(
            store, None, promote=promote,
            workspace_for=lambda pid: tmp_path / "ws",
            publication_enabled=publication_enabled,
        )

    def _publish(self, store, promote, tmp_path, *, publication_enabled,
                 update_id=1):
        d = self._dispatcher(store, promote, tmp_path,
                             publication_enabled=publication_enabled)
        d.dispatch(_payload(update_id, "oke live"), PROJECT_ID, "create",
                   authenticated=AuthenticatedTelegramContext("1", CHAT))
        _preview_ready_project(store)
        return d.dispatch(
            _payload(update_id + 1, "oke live"), PROJECT_ID, "publish",
            authenticated=AuthenticatedTelegramContext("1", CHAT))

    def test_direct_publish_blocked_before_remote_calls(self, tmp_path):
        store = ProjectStateStore(tmp_path / "state")
        promote = MagicMock()
        promote.approve.return_value = OperationResult.ok({"operation_id": "op-1"})
        promote.promote.return_value = OperationResult.ok(
            {"production_url": "https://prod.vercel.app"})

        result = self._publish(store, promote, tmp_path,
                               publication_enabled=False)

        assert not result.success
        assert result.error_code == "PUBLISH_BLOCKED"
        # Blocked BEFORE any remote call.
        promote.approve.assert_not_called()
        promote.promote.assert_not_called()

    def test_direct_publish_allowed_when_publication_enabled(self, tmp_path):
        store = ProjectStateStore(tmp_path / "state")
        promote = MagicMock()
        promote.approve.return_value = OperationResult.ok({"operation_id": "op-1"})
        promote.promote.return_value = OperationResult.ok(
            {"production_url": "https://prod.vercel.app"})

        result = self._publish(store, promote, tmp_path,
                               publication_enabled=True)

        assert result.success
        promote.approve.assert_called_once()
        promote.promote.assert_called_once()


# ---------------------------------------------------------------------------
# Fix B -- production default behaviour is UNCHANGED
# ---------------------------------------------------------------------------

class TestProductionDefaultUnchanged:

    def test_production_approve_publishes_and_says_publishing(self, tmp_path):
        loop, store, out, promote, vercel = _make(
            tmp_path, publication_enabled=True)
        _preview_ready_project(store)

        loop._process_update(_payload(501, "approve"))

        state = store.load(PROJECT_ID)
        assert state.lifecycle == ProjectLifecycle.LIVE.value
        assert state.revisions.live_revision == 1
        assert state.production_url == "https://wb.vercel.app/"
        assert vercel.promote_calls == ["dpl_1"]
        acks = _acks(out)
        assert acks == [_APPROVAL_ACK_PUBLISHING]
        assert [t for _c, t in out.sent if "Live" in t] == [
            "🚀 Live: https://wb.vercel.app/"]

    def test_default_dispatcher_publication_enabled(self, tmp_path):
        d = TelegramDispatcher(ProjectStateStore(tmp_path / "state"), None)
        assert d.publication_enabled is True


# ---------------------------------------------------------------------------
# Fix A -- project identity consistency across preview / promote / domain
# ---------------------------------------------------------------------------

class TestIdentityConsistencyAcrossDeps:

    @staticmethod
    def _config(tmp_path, *, friendly_slug, publication_enabled):
        from app.runtime import RuntimeConfig
        cfg = RuntimeConfig(
            telegram_bot_token=BOT_TOKEN,
            hermes_home=tmp_path / "hermes-home",
            workspace_root=tmp_path / "workspaces",
            state_root=tmp_path / "state",
            output_repo_path=tmp_path / "output-repo",
            vercel_token="v",
            vercel_team_id="team_1",
            vercel_ownership_namespace="ns",
        )
        object.__setattr__(cfg, "friendly_slug", friendly_slug)
        object.__setattr__(cfg, "publication_enabled", publication_enabled)
        return cfg

    def test_staging_deps_all_use_conversation_app_id_and_no_slug(self, tmp_path):
        from app.runtime import compose

        cfg = self._config(tmp_path, friendly_slug=False,
                           publication_enabled=False)
        comp = compose(cfg)

        pid = "tg-6329821361-p1"
        # preview, promote and domain must agree on ONE identity: the
        # conversation-level opaque app id, with NO friendly slug consulted.
        assert comp.preview.deps.app_id_for(pid) == "tg-6329821361"
        assert comp.promote.deps.app_id_for(pid) == "tg-6329821361"
        assert comp.domain.deps.app_id_for(pid) == "tg-6329821361"
        assert comp.preview.deps.slug_for is None
        assert comp.promote.deps.slug_for is None
        assert comp.domain.deps.slug_for is None
        # The publication boundary is carried onto the dispatcher.
        assert comp.dispatcher.publication_enabled is False

    def test_production_deps_keep_project_id_and_slug_resolver(self, tmp_path):
        from app.runtime import compose

        cfg = self._config(tmp_path, friendly_slug=True,
                           publication_enabled=True)
        comp = compose(cfg)

        pid = "tg-6329821361-p1"
        assert comp.preview.deps.app_id_for(pid) == pid
        assert comp.promote.deps.app_id_for(pid) == pid
        assert comp.domain.deps.app_id_for(pid) == pid
        # Production still resolves the bound friendly slug.
        assert comp.preview.deps.slug_for is not None
        assert comp.promote.deps.slug_for is not None
        assert comp.domain.deps.slug_for is not None
        assert comp.dispatcher.publication_enabled is True


# ---------------------------------------------------------------------------
# Fix A / fail-closed -- invalid ownership or identity never promotes
# ---------------------------------------------------------------------------

class TestFailClosedOnInvalidIdentity:

    def test_unauthorized_approve_fails_closed_and_never_promotes(self, tmp_path):
        loop, store, out, promote, vercel = _make(
            tmp_path, publication_enabled=True)
        _preview_ready_project(store)
        # Replace the owner with a DIFFERENT principal: the authenticated
        # telegram:1 is no longer authorized to approve.
        with store.acquire_writer(PROJECT_ID) as state:
            state.roles["owner"] = "telegram:999"
            store.save(state)

        loop._process_update(_payload(601, "approve"))

        state = store.load(PROJECT_ID)
        assert state.revisions.approved_revision == 0
        assert state.revisions.live_revision == 0
        assert vercel.promote_calls == []
        assert _acks(out) == []

    def test_no_shown_preview_fails_closed(self, tmp_path):
        loop, store, out, promote, vercel = _make(
            tmp_path, publication_enabled=True)
        # PREVIEW_READY with an owner, but nothing ever shown.
        with store.acquire_writer(PROJECT_ID) as state:
            state.lifecycle = ProjectLifecycle.PREVIEW_READY.value
            state.roles["owner"] = "telegram:1"
            state.revisions.source_revision = 1
            state.revisions.preview_revision = 1
            store.save(state)

        loop._process_update(_payload(602, "approve"))

        assert store.load(PROJECT_ID).revisions.approved_revision == 0
        assert vercel.promote_calls == []


# ---------------------------------------------------------------------------
# Fix B -- operator resume / reconcile publication path is blocked
# ---------------------------------------------------------------------------

class TestResumePublicationBlocked:

    @staticmethod
    def _run_main(argv, *, publication_enabled):
        cfg = MagicMock(smoke_browser_factory=MagicMock())
        cfg.publication_enabled = publication_enabled
        with patch("app.runtime.load_runtime_config", return_value=cfg), \
             patch("app.runtime.preflight_node_toolchain"), \
             patch("app.runtime.preflight_smoke_support"), \
             patch("app.runtime.preflight_screenshot_support"), \
             patch("app.runtime.preflight_role_validation", return_value=True), \
             patch("app.runtime.preflight_design_capabilities",
                   return_value=MagicMock(ok=True)), \
             patch("app.runtime.compose", return_value=MagicMock()), \
             patch("app.runtime.reconcile_stranded_projects", return_value={}), \
             patch("app.runtime._run_reconcile_publish",
                   return_value=0) as resume, \
             patch("app.runtime.TelegramReceiveLoop") as loop:
            from app.runtime import main
            rc = main(argv)
        return rc, resume, loop

    def test_resume_blocked_when_publication_disabled(self):
        rc, resume, loop = self._run_main(
            ["--reconcile-publish", PROJECT_ID, "--as", "telegram:1"],
            publication_enabled=False)
        assert rc == 1
        resume.assert_not_called()
        loop.assert_not_called()

    def test_resume_reaches_operator_path_when_enabled(self):
        rc, resume, loop = self._run_main(
            ["--reconcile-publish", PROJECT_ID, "--as", "telegram:1"],
            publication_enabled=True)
        assert rc == 0
        resume.assert_called_once()
        loop.assert_not_called()


# ---------------------------------------------------------------------------
# Fix B -- preview-only recovery is unaffected
# ---------------------------------------------------------------------------

class TestPreviewRecoveryUnaffected:

    def test_reconcile_preview_still_runs_with_publication_disabled(self, tmp_path):
        store = ProjectStateStore(tmp_path / "state")
        _approved_state(store, PROJECT_ID)
        with store.acquire_writer(PROJECT_ID) as state:
            state.roles["owner"] = "telegram:1"
            state.conversation_id = CHAT
            # reconcile_preview requires a tested snapshot to be present.
            state.deployment["tested_snapshot"] = {"dist": {"index.html": "x"}}
            store.save(state)
        preview = MagicMock()
        preview.run_owned.return_value = OperationResult.ok(
            {"preview_url": "https://x.vercel.app"})
        d = TelegramDispatcher(
            store, None, preview=preview,
            workspace_for=lambda pid: tmp_path / "ws",
            publication_enabled=False,
        )
        d.dispatch(_payload(701, "lihat preview"), PROJECT_ID, "create",
                   authenticated=AuthenticatedTelegramContext("1", CHAT))

        result = d.dispatch(
            _payload(702, "lihat preview"), PROJECT_ID, "reconcile_preview",
            authenticated=AuthenticatedTelegramContext("1", CHAT))

        assert result.success
        preview.run_owned.assert_called_once()
