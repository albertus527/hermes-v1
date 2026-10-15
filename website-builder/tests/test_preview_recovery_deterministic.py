"""Regression tests for the deterministic preview-recovery fix (Fix 1 + Fix 2a).

WHY
---
A live staging E2E showed preview recovery was NOT deterministic:

  * Turn "halo" was routed by FAST to CLARIFICATION, so the pre-turn
    reconcile branch (which lived INSIDE the per-project turn, AFTER routing)
    never ran -- recovery silently depended on FAST's interpretation.
  * A later "show me the preview" turn DID reach reconcile, but packaging then
    failed because the runtime asked for the FRIENDLY slug ``forma-atelier``
    while the staging broker allowlists exactly ONE opaque project name.

Fix 1 makes preview reconciliation run BEFORE FAST routing / intent
classification, driven by the AUTHENTICATED conversation's registry, and makes
the recovery turn TERMINAL (no INTAKE/REVISE/APPROVE/PUBLISH on the same
message). Fix 2a adds a ``friendly_slug`` config gate (default True, production
unchanged) that, in staging, disables the friendly slug AND addresses the
project by its conversation-level opaque identity.

Offline only: no network, no Hermes, no Vercel, no browser.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.channels.dispatch import AuthenticatedTelegramContext, TelegramDispatcher
from app.conversations import ConversationRouter
from app.core.contracts import OperationResult
from app.core.registry import ConversationRegistryStore
from app.core.state import ProjectStateStore
from app.runtime import (
    TelegramReceiveLoop,
    _coerce_bool,
    _conversation_app_id,
)

from tests.r1_harness import LocalR1Scenario, make_preview_ready


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _preview_ready(store: ProjectStateStore, project_id: str, *, owner="telegram:1"):
    with store.acquire_writer(project_id) as state:
        state.lifecycle = "PREVIEW_READY"
        state.roles = {"owner": owner, "reviewers": [], "viewers": []}
        state.revisions.source_revision = 1
        state.revisions.qa_revision = 1
        state.revisions.preview_revision = 0
        state.deployment = {"tested_snapshot": {"dist_hash": "abc"}}
        state.conversation_id = "555"
        store.save(state)


def _update(event_id=1, text="halo"):
    return {
        "update_id": event_id,
        "message": {"from": {"id": 1}, "chat": {"id": 555}, "text": text,
                    "date": event_id},
    }


class _RecordingPreview:
    """Reconcile double: records calls; optionally succeeds + marks shown."""

    def __init__(self, store=None, project_id=None, succeed=True, error_code=None):
        self._store = store
        self._project_id = project_id
        self._succeed = succeed
        self._error_code = error_code
        self.calls = []

    def run_owned(self, project_id, workspace, slot_held=False):
        self.calls.append(project_id)
        if not self._succeed:
            code = self._error_code or "SMOKE_FAILED"
            return OperationResult.fail(code, error_code=code)
        if self._store is not None:
            pid = self._project_id or project_id
            with self._store.acquire_writer(pid) as state:
                state.deployment["latest_shown_preview"] = {
                    "operation_id": "op", "source_revision": 1,
                    "preview_url": "https://x.vercel.app", "deployment_id": "dpl",
                    "source_sha256": "s", "artifact_sha256": "a",
                    "shown_at": 1.0, "follow_up_state": None,
                }
                state.revisions.preview_revision = 1
                self._store.save(state)
        return OperationResult.ok({"preview_url": "https://x.vercel.app"})


def _routed_loop(tmp_path, *, preview, hermes=None, router_decision=None,
                 telegram_out=None):
    """Build a real TelegramReceiveLoop wired to a conversation router."""
    store = ProjectStateStore(tmp_path / "state")
    _preview_ready(store, "tg-555-p1")
    registry = ConversationRegistryStore(tmp_path / "state" / "conversations")
    registry.adopt_project("555", "tg-555-p1", "webbandung")
    registry.set_active("555", "tg-555-p1")

    intake = MagicMock()
    builder = MagicMock()
    dispatcher = TelegramDispatcher(
        store, intake, builder=builder, preview=preview,
        workspace_for=lambda pid: tmp_path / "ws",
    )
    telegram_out = telegram_out or MagicMock()
    telegram_out.send_text.return_value = MagicMock(success=True)

    if hermes is None:
        hermes = MagicMock()
        payload = router_decision or {
            "intent": "PROJECT_TURN", "target_project_name": "webbandung",
            "proposed_new_project_name": None, "confidence": "high",
        }
        import json as _json
        hermes._run_fast_programmatic.return_value = MagicMock(
            success=True, response=_json.dumps(payload),
        )
    router = ConversationRouter(store, registry, telegram_out=telegram_out,
                                hermes=hermes)
    loop = TelegramReceiveLoop(
        bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        dispatcher=dispatcher, telegram_out=telegram_out, hermes=hermes,
        transport=MagicMock(), conversations=router,
    )
    return loop, store, intake, hermes


# ---------------------------------------------------------------------------
# Fix 1 -- deterministic pre-routing recovery
# ---------------------------------------------------------------------------

class TestDeterministicPreRoutingRecovery:
    def test_ambiguous_message_triggers_reconcile_before_fast_and_ends_turn(self, tmp_path):
        """'halo' must reconcile BEFORE FAST, and FAST must not be consulted.

        Regression for the live failure where FAST classified 'halo' as
        CLARIFICATION and the reconcile branch never ran.
        """
        preview = _RecordingPreview()
        loop, store, intake, hermes = _routed_loop(tmp_path, preview=preview)

        loop._process_update(_update(text="halo"))

        # Reconcile ran FIRST, deterministically, without FAST.
        assert preview.calls == ["tg-555-p1"]
        hermes._run_fast_programmatic.assert_not_called()
        # The turn is TERMINAL: no intake / build / revise / publish.
        intake.process.assert_not_called()

    def test_publish_like_text_does_not_publish_in_recovery_turn(self, tmp_path):
        """Publish-like text must reconcile, never reach approve/publish."""
        preview = _RecordingPreview()
        loop, store, intake, hermes = _routed_loop(tmp_path, preview=preview)

        loop._process_update(_update(text="publish ya sekarang"))

        assert preview.calls == ["tg-555-p1"]
        hermes._run_fast_programmatic.assert_not_called()
        intake.process.assert_not_called()
        # Lifecycle untouched by any publish/approve path.
        assert store.load("tg-555-p1").lifecycle == "PREVIEW_READY"

    def test_revise_like_text_does_not_revise_in_recovery_turn(self, tmp_path):
        """Revise-like text must reconcile, never reach the revise path."""
        preview = _RecordingPreview()
        loop, store, intake, hermes = _routed_loop(tmp_path, preview=preview)

        loop._process_update(_update(text="tolong ubah warnanya jadi hijau"))

        assert preview.calls == ["tg-555-p1"]
        hermes._run_fast_programmatic.assert_not_called()
        state = store.load("tg-555-p1")
        assert state.revisions.queued_revision_seq == 0
        assert state.revisions.source_revision == 1

    def test_successful_reconcile_is_idempotent_and_next_message_routes_normally(self, tmp_path):
        """Once the preview is shown, subsequent messages use normal routing."""
        store = ProjectStateStore(tmp_path / "state")
        _preview_ready(store, "tg-555-p1")
        registry = ConversationRegistryStore(tmp_path / "state" / "conversations")
        registry.adopt_project("555", "tg-555-p1", "webbandung")
        registry.set_active("555", "tg-555-p1")

        preview = _RecordingPreview(store, "tg-555-p1")
        intake = MagicMock()
        dispatcher = TelegramDispatcher(
            store, intake, preview=preview,
            workspace_for=lambda pid: tmp_path / "ws",
        )
        telegram_out = MagicMock()
        telegram_out.send_text.return_value = MagicMock(success=True)
        hermes = MagicMock()
        import json as _json
        hermes._run_fast_programmatic.return_value = MagicMock(
            success=True,
            response=_json.dumps({"intent": "PROJECT_TURN",
                                  "target_project_name": "webbandung",
                                  "proposed_new_project_name": None,
                                  "confidence": "high"}),
        )
        router = ConversationRouter(store, registry, telegram_out=telegram_out,
                                    hermes=hermes)
        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher, telegram_out=telegram_out, hermes=hermes,
            transport=MagicMock(), conversations=router,
        )

        loop._process_update(_update(event_id=1, text="halo"))
        assert len(preview.calls) == 1
        assert "latest_shown_preview" in store.load("tg-555-p1").deployment

        # Second message: preview already shown -> normal routing (FAST runs,
        # intake reached), and reconcile does NOT run again.
        loop._process_update(_update(event_id=2, text="halo lagi"))
        assert len(preview.calls) == 1
        hermes._run_fast_programmatic.assert_called()
        intake.process.assert_called_once()

    def test_hard_failure_is_fail_closed_without_infinite_retry(self, tmp_path):
        """A hard reconcile error stops the turn and never loops forever."""
        preview = _RecordingPreview(
            succeed=False, error_code="PROJECT_RECONCILIATION_REQUIRED")
        loop, store, intake, hermes = _routed_loop(tmp_path, preview=preview)

        loop._process_update(_update(event_id=1, text="halo"))
        # Fail-closed: no FAST, no intake, state unchanged.
        hermes._run_fast_programmatic.assert_not_called()
        intake.process.assert_not_called()
        assert store.load("tg-555-p1").lifecycle == "PREVIEW_READY"
        assert "latest_shown_preview" not in store.load("tg-555-p1").deployment

    def test_no_active_project_defers_to_normal_routing(self, tmp_path):
        """No active project -> recovery is a no-op; routing proceeds."""
        store = ProjectStateStore(tmp_path / "state")
        registry = ConversationRegistryStore(tmp_path / "state" / "conversations")
        preview = _RecordingPreview()
        intake = MagicMock()
        dispatcher = TelegramDispatcher(
            store, intake, preview=preview,
            workspace_for=lambda pid: tmp_path / "ws",
        )
        telegram_out = MagicMock()
        telegram_out.send_text.return_value = MagicMock(success=True)
        hermes = MagicMock()
        import json as _json
        hermes._run_fast_programmatic.return_value = MagicMock(
            success=True,
            response=_json.dumps({"intent": "CREATE_PROJECT",
                                  "target_project_name": None,
                                  "proposed_new_project_name": "webbandung",
                                  "confidence": "high"}),
        )
        router = ConversationRouter(store, registry, telegram_out=telegram_out,
                                    hermes=hermes)
        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher, telegram_out=telegram_out, hermes=hermes,
            transport=MagicMock(), conversations=router,
        )

        loop._process_update(_update(text="halo"))
        assert preview.calls == []
        hermes._run_fast_programmatic.assert_called()

    def test_non_preview_ready_project_uses_normal_routing(self, tmp_path):
        """A DISCOVERING project is untouched by the recovery gate."""
        store = ProjectStateStore(tmp_path / "state")
        with store.acquire_writer("tg-555-p1") as state:
            state.lifecycle = "DISCOVERING"
            state.roles = {"owner": "telegram:1", "reviewers": [], "viewers": []}
            state.conversation_id = "555"
            store.save(state)
        registry = ConversationRegistryStore(tmp_path / "state" / "conversations")
        registry.adopt_project("555", "tg-555-p1", "webbandung")
        registry.set_active("555", "tg-555-p1")

        preview = _RecordingPreview()
        intake = MagicMock()
        dispatcher = TelegramDispatcher(
            store, intake, preview=preview,
            workspace_for=lambda pid: tmp_path / "ws",
        )
        telegram_out = MagicMock()
        telegram_out.send_text.return_value = MagicMock(success=True)
        hermes = MagicMock()
        import json as _json
        hermes._run_fast_programmatic.return_value = MagicMock(
            success=True,
            response=_json.dumps({"intent": "PROJECT_TURN",
                                  "target_project_name": "webbandung",
                                  "proposed_new_project_name": None,
                                  "confidence": "high"}),
        )
        router = ConversationRouter(store, registry, telegram_out=telegram_out,
                                    hermes=hermes)
        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher, telegram_out=telegram_out, hermes=hermes,
            transport=MagicMock(), conversations=router,
        )

        loop._process_update(_update(text="halo"))
        assert preview.calls == []
        assert store.load("tg-555-p1").lifecycle == "DISCOVERING"


class TestLegacyPathTerminalRecovery:
    def test_legacy_recovery_is_terminal(self, tmp_path):
        """Legacy (no-router) path: reconcile runs, turn ends, no intake."""
        store = ProjectStateStore(tmp_path / "state")
        _preview_ready(store, "tg-555")
        preview = _RecordingPreview(store, "tg-555")
        intake = MagicMock()
        dispatcher = TelegramDispatcher(
            store, intake, preview=preview,
            workspace_for=lambda pid: tmp_path / "ws",
        )
        telegram_out = MagicMock()
        telegram_out.send_text.return_value = MagicMock(success=True)
        hermes = MagicMock()
        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher, telegram_out=telegram_out, hermes=hermes,
            transport=MagicMock(), conversations=None,
        )

        loop._process_update(_update(text="halo"))
        assert preview.calls == ["tg-555"]
        intake.process.assert_not_called()
        hermes._run_fast_programmatic.assert_not_called()
        assert "latest_shown_preview" in store.load("tg-555").deployment


# ---------------------------------------------------------------------------
# Fix 1 -- bounded / anti-wedge behaviour preserved
# ---------------------------------------------------------------------------

class TestBoundedRecoveryPreserved:
    def test_smoke_blocked_operation_does_not_re_reconcile(self, tmp_path):
        """A blocked operation makes the gate False -> normal routing resumes."""
        store = ProjectStateStore(tmp_path / "state")
        _preview_ready(store, "tg-555-p1")
        with store.acquire_writer("tg-555-p1") as state:
            state.deployment["preview_intent"] = {
                "operation_id": "op", "smoke_blocked": True,
                "failure_status_outcome": "SENT",
                "smoke": {"failure_classification": "artifact_defect"},
            }
            store.save(state)

        preview = _RecordingPreview()
        intake = MagicMock()
        dispatcher = TelegramDispatcher(
            store, intake, preview=preview,
            workspace_for=lambda pid: tmp_path / "ws",
        )
        telegram_out = MagicMock()
        telegram_out.send_text.return_value = MagicMock(success=True)
        hermes = MagicMock()
        import json as _json
        hermes._run_fast_programmatic.return_value = MagicMock(
            success=True,
            response=_json.dumps({"intent": "PROJECT_TURN",
                                  "target_project_name": None,
                                  "proposed_new_project_name": None,
                                  "confidence": "high"}),
        )
        registry = ConversationRegistryStore(tmp_path / "state" / "conversations")
        registry.adopt_project("555", "tg-555-p1", "webbandung")
        registry.set_active("555", "tg-555-p1")
        router = ConversationRouter(store, registry, telegram_out=telegram_out,
                                    hermes=hermes)
        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher, telegram_out=telegram_out, hermes=hermes,
            transport=MagicMock(), conversations=router,
        )

        loop._process_update(_update(text="halo"))
        # Anti-wedge: the blocked op is NOT re-reconciled; routing proceeds.
        assert preview.calls == []
        hermes._run_fast_programmatic.assert_called()


# ---------------------------------------------------------------------------
# Fix 2a -- friendly_slug config + opaque staging identity
# ---------------------------------------------------------------------------

class TestFriendlySlugConfig:
    def test_coerce_bool_defaults_and_values(self):
        assert _coerce_bool(None, True) is True
        assert _coerce_bool("", True) is True
        assert _coerce_bool(None, False) is False
        assert _coerce_bool("false", True) is False
        assert _coerce_bool("0", True) is False
        assert _coerce_bool("true", False) is True
        assert _coerce_bool("yes", False) is True
        assert _coerce_bool(True, False) is True

    def test_default_config_keeps_friendly_slug_enabled(self, tmp_path):
        """Production default: friendly_slug True -> app id is the project id."""
        from app.runtime import RuntimeConfig
        cfg = RuntimeConfig(
            telegram_bot_token="t", hermes_home=tmp_path, workspace_root=tmp_path,
            state_root=tmp_path, output_repo_path=tmp_path, vercel_token="v",
            vercel_team_id="team_1", vercel_ownership_namespace="ns",
        )
        assert cfg.friendly_slug is True

    def test_conversation_app_id_strips_r2_suffix_only(self):
        assert _conversation_app_id("tg-6329821361-p1") == "tg-6329821361"
        assert _conversation_app_id("tg-6329821361-p26") == "tg-6329821361"
        # A non-R2 identity is returned unchanged (no accidental stripping).
        assert _conversation_app_id("tg-6329821361") == "tg-6329821361"
        assert _conversation_app_id("tg-555-px") == "tg-555-px"

    def test_staging_opaque_identity_matches_pinned_project(self):
        """The derived opaque name equals the pinned broker allowlist name."""
        NS = "wb-e2e-staging"
        PIN = "wb-eee91031b73b169972eec68c5929ed2a5567df91"
        app_id = _conversation_app_id("tg-6329821361-p1")
        derived = "wb-" + hashlib.sha256(
            (NS + "\0" + app_id).encode()).hexdigest()[:40]
        assert derived == PIN

    def test_foreign_project_name_is_not_the_pinned_one(self):
        """A DIFFERENT app id derives a DIFFERENT (non-allowlisted) name."""
        NS = "wb-e2e-staging"
        PIN = "wb-eee91031b73b169972eec68c5929ed2a5567df91"
        other = "wb-" + hashlib.sha256(
            (NS + "\0" + "tg-9999999999").encode()).hexdigest()[:40]
        assert other != PIN


class TestPreviewDepsFriendlySlugGate:
    """Fix 2a: the friendly-slug gate decides which project identity is used.

    Uses a REAL tested_snapshot (harness ``make_preview_ready``) so the
    orchestrator reaches identity selection, plus a spy Vercel boundary that
    records the ``expected_name`` it is asked for and then fails closed --
    stopping the run immediately after identity selection.
    """

    class _SpyVercel:
        def __init__(self):
            self.asked = None

        def lookup_project(self, app_id, *, expected_name=None):
            self.asked = (app_id, expected_name)
            return OperationResult.fail(
                "PROJECT_RECONCILIATION_REQUIRED",
                error_code="PROJECT_RECONCILIATION_REQUIRED")

        def ensure_project(self, app_id):
            self.asked = (app_id, None)
            return OperationResult.fail(
                "PROJECT_RECONCILIATION_REQUIRED",
                error_code="PROJECT_RECONCILIATION_REQUIRED")

        def ensure_project_with_slug(self, app_id, slug):
            self.asked = (app_id, slug)
            return OperationResult.fail(
                "PROJECT_RECONCILIATION_REQUIRED",
                error_code="PROJECT_RECONCILIATION_REQUIRED")

        def project_name_for(self, app_id):
            return "wb-" + hashlib.sha256(
                ("wb-e2e-staging\0" + app_id).encode()).hexdigest()[:40]

    @staticmethod
    def _ready(tmp_path, pid="tg-555-p1"):
        store = ProjectStateStore(tmp_path / "state")
        ws = tmp_path / "ws"
        ws.mkdir(parents=True, exist_ok=True)
        make_preview_ready(store, pid, ws)
        # A stale friendly candidate + attempted marker: exactly the staging
        # shape that must NOT be resurrected when friendly_slug is disabled.
        with store.acquire_writer(pid) as state:
            state.deployment["preview_intent"] = {
                "slug_candidate": "forma-atelier",
                "expected_name": "forma-atelier",
                "project_attempted": True,
            }
            store.save(state)
        return store, ws

    def test_friendly_slug_false_ignores_persisted_slug_candidate(self, tmp_path):
        from app.deploy.preview import PreviewDeps, PreviewOrchestrator

        store, ws = self._ready(tmp_path)
        spy = self._SpyVercel()
        output_repo = MagicMock()
        output_repo.commit.return_value = {"commit": "abc"}
        deps = PreviewDeps(
            vercel=spy, telegram=MagicMock(), smoke=MagicMock(),
            output_repo=output_repo, chat_id_for=lambda pid, st: "555",
            app_id_for=lambda pid: "tg-555",
            friendly_slug=False,
            slug_for=lambda pid, st: "forma-atelier",  # must be ignored
        )
        orch = PreviewOrchestrator(store, deps, runner=MagicMock())
        orch.run_owned("tg-555-p1", ws)
        assert spy.asked is not None
        # Staging: the friendly name must NEVER be used.
        assert spy.asked[1] is None
        assert spy.asked[0] == "tg-555"

    def test_friendly_slug_true_preserves_slug_behaviour(self, tmp_path):
        from app.deploy.preview import PreviewDeps, PreviewOrchestrator

        store, ws = self._ready(tmp_path)
        spy = self._SpyVercel()
        output_repo = MagicMock()
        output_repo.commit.return_value = {"commit": "abc"}
        deps = PreviewDeps(
            vercel=spy, telegram=MagicMock(), smoke=MagicMock(),
            output_repo=output_repo, chat_id_for=lambda pid, st: "555",
            friendly_slug=True,
            slug_for=lambda pid, st: "forma-atelier",
        )
        orch = PreviewOrchestrator(store, deps, runner=MagicMock())
        orch.run_owned("tg-555-p1", ws)
        assert spy.asked is not None
        # Production: the friendly slug is still used.
        assert spy.asked[1] == "forma-atelier"


# ---------------------------------------------------------------------------
# No FRONTEND / model calls during preview-only recovery
# ---------------------------------------------------------------------------

class TestNoFrontendDuringRecovery:
    def test_recovery_makes_no_frontend_or_vision_calls(self, tmp_path):
        """The recovery turn never invokes FRONTEND/VISION or a scope model."""
        from tests.r1_harness import FakeHermes

        preview = _RecordingPreview()
        hermes = FakeHermes()
        loop, store, intake, _ = _routed_loop(
            tmp_path, preview=preview, hermes=hermes)

        loop._process_update(_update(text="lihat preview dong"))

        # No model call at all in the recovery turn (no FAST, no VISION).
        assert hermes.fast_calls == []
        assert hermes.frontend_calls == 0
        # No intake/build.
        intake.process.assert_not_called()
