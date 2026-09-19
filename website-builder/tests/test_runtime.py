"""Tests for the production runtime composition root and Telegram receive loop.

Mocks external network boundaries only. No real Telegram, Vercel, GitHub,
Web3Forms, Meta, or Hermes network calls.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.channels.dispatch import AuthenticatedTelegramContext, TelegramDispatcher
from app.channels.telegram import TelegramNormalizer
from app.core.state import ProjectStateStore
from app.runtime import (
    ConfigurationError,
    ConversationIntent,
    RuntimeComposition,
    TelegramProviderError,
    TelegramReceiveLoop,
    compose,
    load_runtime_config,
    main,
    preflight_node_toolchain,
    preflight_smoke_support,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env(**overrides):
    """Return a minimal valid environment for runtime config tests."""
    base = {
        "TELEGRAM_BOT_TOKEN": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        "VERCEL_TOKEN": "vercel-token-test",
        "VERCEL_TEAM_ID": "team_test123",
        "HERMES_HOME": "/tmp/test-hermes-home",
        "WEBSITE_BUILDER_WORKSPACE_ROOT": "/tmp/test-workspaces",
        "WEBSITE_BUILDER_STATE_ROOT": "/tmp/test-state",
        "WEBSITE_BUILDER_OUTPUT_REPO": "/tmp/test-output-repo",
    }
    base.update(overrides)
    return base


def _make_config(tmp_path, **overrides):
    """Create a RuntimeConfig with test paths."""
    env = _env(**overrides)
    with patch.dict(os.environ, env, clear=False):
        config = load_runtime_config()
    # Override paths to use tmp_path
    object.__setattr__(config, "hermes_home", tmp_path / "hermes-home")
    object.__setattr__(config, "workspace_root", tmp_path / "workspaces")
    object.__setattr__(config, "state_root", tmp_path / "state")
    object.__setattr__(config, "output_repo_path", tmp_path / "output-repo")
    return config


# ---------------------------------------------------------------------------
# 1. Configuration validation
# ---------------------------------------------------------------------------


class TestConfigurationValidation:
    def test_missing_telegram_token_fails_closed(self):
        with patch.dict(os.environ, _env(TELEGRAM_BOT_TOKEN=""), clear=False):
            with pytest.raises(ConfigurationError, match="TELEGRAM_BOT_TOKEN"):
                load_runtime_config()

    def test_missing_vercel_token_fails_closed(self):
        with patch.dict(os.environ, _env(VERCEL_TOKEN=""), clear=False):
            with pytest.raises(ConfigurationError, match="VERCEL_TOKEN"):
                load_runtime_config()

    def test_missing_vercel_team_id_fails_closed(self):
        with patch.dict(os.environ, _env(VERCEL_TEAM_ID=""), clear=False):
            with pytest.raises(ConfigurationError, match="VERCEL_TEAM_ID"):
                load_runtime_config()

    def test_valid_config_loads(self, tmp_path):
        config = _make_config(tmp_path)
        assert config.telegram_bot_token == "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
        assert config.vercel_token == "vercel-token-test"
        assert config.vercel_team_id == "team_test123"

    def test_secrets_not_in_repr(self, tmp_path):
        config = _make_config(tmp_path)
        text = repr(config)
        assert "123456:ABC" not in text
        assert "vercel-token-test" not in text

    def test_whatsapp_credentials_not_required(self, tmp_path):
        """WhatsApp remains IMPLEMENTED_DORMANT — no WhatsApp env vars needed."""
        env = _env()
        # Explicitly remove any WhatsApp vars that might leak from the test env
        for key in list(os.environ):
            if key.startswith("WHATSAPP_"):
                del os.environ[key]
        with patch.dict(os.environ, env, clear=False):
            config = load_runtime_config()
        assert config is not None


# ---------------------------------------------------------------------------
# 2. Composition constructs expected collaborators
# ---------------------------------------------------------------------------


class TestComposition:
    def test_compose_constructs_all_collaborators(self, tmp_path):
        config = _make_config(tmp_path)
        comp = compose(config)

        assert isinstance(comp, RuntimeComposition)
        assert comp.store is not None
        assert comp.runner is not None
        assert comp.hermes is not None
        assert comp.intake is not None
        assert comp.references is not None
        assert comp.directions is not None
        assert comp.builder is not None
        assert comp.revise is not None
        assert comp.promote is not None
        assert comp.domain is not None
        assert comp.preview is not None
        assert comp.telegram_out is not None
        assert comp.dispatcher is not None

    def test_compose_uses_existing_classes(self, tmp_path):
        config = _make_config(tmp_path)
        comp = compose(config)

        assert isinstance(comp.store, ProjectStateStore)
        assert isinstance(comp.dispatcher, TelegramDispatcher)
        assert comp.runner.state_store is comp.store
        assert comp.builder.runner is comp.runner
        assert comp.revise.runner is comp.runner
        assert comp.promote.runner is comp.runner

    def test_compose_creates_directories(self, tmp_path):
        config = _make_config(tmp_path)
        compose(config)
        assert config.hermes_home.exists()
        assert config.workspace_root.exists()
        assert config.state_root.exists()

    def test_conversations_shares_same_hermes_instance(self, tmp_path):
        """Regression: FAST-first conversation routing must use the SAME
        HermesAdapter instance as the rest of the runtime. A second,
        independently-constructed HermesAdapter silently sets
        conversations.hermes to a different object, or worse, leaves the
        router unable to reach FAST at all in production.
        """
        config = _make_config(tmp_path)
        comp = compose(config)
        assert comp.conversations.hermes is not None
        assert comp.conversations.hermes is comp.hermes

    def test_compose_constructs_exactly_one_hermes_adapter(self, tmp_path):
        """Regression: compose() must never construct a second HermesAdapter
        merely to wire the conversation router.
        """
        config = _make_config(tmp_path)
        with patch("app.runtime.HermesAdapter", wraps=__import__(
            "app.hermes.adapter", fromlist=["HermesAdapter"]
        ).HermesAdapter) as spy:
            compose(config)
            assert spy.call_count == 1

    def test_fast_first_routing_reachable_through_real_composition(self, tmp_path):
        """Regression: the conversation-level FAST routing prompt must be
        reachable through the real `compose()` wiring, not just when a
        ConversationRouter is hand-constructed in isolation (as prior
        FAST-first routing tests did). This is the actual production bug:
        `conversations.hermes` was None in the real runtime, so FAST was
        never consulted for conversation-level routing.
        """
        config = _make_config(tmp_path)
        comp = compose(config)

        with patch.object(
            comp.hermes, "_run_fast_programmatic"
        ) as mock_fast:
            mock_fast.return_value = MagicMock(
                success=True,
                response='{"intent": "LIST_PROJECTS", "target_project_name": null, '
                         '"proposed_new_project_name": null, "confidence": "high"}',
            )
            comp.conversations.route("555", "project apa aja aku ada")
            assert mock_fast.called


# ---------------------------------------------------------------------------
# 3. Telegram getUpdates success
# ---------------------------------------------------------------------------


class TestTelegramGetUpdates:
    def test_get_updates_success(self):
        transport = MagicMock()
        response = MagicMock()
        response.body = json.dumps({
            "ok": True,
            "result": [
                {"update_id": 1, "message": {"from": {"id": 1}, "chat": {"id": 555}, "text": "hello", "date": 1234567890}},
            ],
        }).encode()
        response.status = 200
        transport.request.return_value = response

        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=MagicMock(),
            telegram_out=MagicMock(),
            transport=transport,
        )
        updates = loop._get_updates()
        assert len(updates) == 1
        assert updates[0]["update_id"] == 1

    def test_get_updates_provider_failure(self):
        transport = MagicMock()
        response = MagicMock()
        response.body = json.dumps({
            "ok": False,
            "description": "Unauthorized",
        }).encode()
        response.status = 401
        transport.request.return_value = response

        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=MagicMock(),
            telegram_out=MagicMock(),
            transport=transport,
        )
        with pytest.raises(TelegramProviderError, match="Unauthorized"):
            loop._get_updates()

    def test_get_updates_malformed_result(self):
        transport = MagicMock()
        response = MagicMock()
        response.body = json.dumps({
            "ok": True,
            "result": "not-a-list",
        }).encode()
        response.status = 200
        transport.request.return_value = response

        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=MagicMock(),
            telegram_out=MagicMock(),
            transport=transport,
        )
        with pytest.raises(TelegramProviderError, match="malformed"):
            loop._get_updates()


# ---------------------------------------------------------------------------
# 4. Telegram offset advancement
# ---------------------------------------------------------------------------


class TestTelegramOffset:
    def test_offset_advances_after_processing(self):
        transport = MagicMock()
        response = MagicMock()
        response.body = json.dumps({
            "ok": True,
            "result": [
                {"update_id": 100, "message": {"from": {"id": 1}, "chat": {"id": 555}, "text": "a", "date": 1}},
                {"update_id": 101, "message": {"from": {"id": 1}, "chat": {"id": 555}, "text": "b", "date": 2}},
            ],
        }).encode()
        response.status = 200
        transport.request.return_value = response

        dispatcher = MagicMock()
        dispatcher.store.load.return_value = None
        dispatcher.dispatch.return_value = MagicMock(success=True)

        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher,
            telegram_out=MagicMock(),
            transport=transport,
        )

        # Process one batch
        updates = loop._get_updates()
        for update in updates:
            update_id = update.get("update_id")
            if update_id is not None:
                loop._offset = update_id + 1
            loop._process_update(update)

        assert loop._offset == 102

        # Next call should include offset
        loop._get_updates()
        call_url = transport.request.call_args[0][1]
        assert "offset=102" in call_url


# ---------------------------------------------------------------------------
# 5. Malformed update isolation
# ---------------------------------------------------------------------------


class TestMalformedUpdateIsolation:
    def test_non_message_update_skipped(self):
        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=MagicMock(),
            telegram_out=MagicMock(),
            transport=MagicMock(),
        )
        # Should not raise
        loop._process_update({"update_id": 1, "edited_channel_post": {}})

    def test_missing_identity_skipped(self):
        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=MagicMock(),
            telegram_out=MagicMock(),
            transport=MagicMock(),
        )
        # Should not raise
        loop._process_update({"update_id": 1, "message": {"text": "hello"}})

    def test_exception_in_processing_does_not_crash(self):
        dispatcher = MagicMock()
        dispatcher.store.load.side_effect = RuntimeError("boom")
        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher,
            telegram_out=MagicMock(),
            transport=MagicMock(),
        )
        # Should not raise
        loop._process_update({
            "update_id": 1,
            "message": {"from": {"id": 1}, "chat": {"id": 555}, "text": "hello", "date": 1},
        })


# ---------------------------------------------------------------------------
# 6. Graceful stop behavior
# ---------------------------------------------------------------------------


class TestGracefulStop:
    def test_stop_event_breaks_loop(self):
        transport = MagicMock()
        response = MagicMock()
        response.body = json.dumps({"ok": True, "result": []}).encode()
        response.status = 200
        transport.request.return_value = response

        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=MagicMock(),
            telegram_out=MagicMock(),
            transport=transport,
            poll_timeout=1,
        )

        # Stop after a short delay
        def stopper():
            time.sleep(0.1)
            loop.stop()

        t = threading.Thread(target=stopper)
        t.start()
        loop.run()
        t.join()
        assert loop._stop_event.is_set()


# ---------------------------------------------------------------------------
# 7. Authenticated context derivation
# ---------------------------------------------------------------------------


class TestAuthenticatedContextDerivation:
    def test_context_derived_from_update_fields(self):
        dispatcher = MagicMock()
        dispatcher.store.load.return_value = None
        dispatcher.dispatch.return_value = MagicMock(success=True)

        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher,
            telegram_out=MagicMock(),
            transport=MagicMock(),
        )

        update = {
            "update_id": 1,
            "message": {
                "from": {"id": 42},
                "chat": {"id": 999},
                "text": "hello",
                "date": 1234567890,
            },
        }
        loop._process_update(update)

        # Verify create was called with correct authenticated context
        create_call = dispatcher.dispatch.call_args_list[0]
        assert create_call[0][1] == "tg-999"
        assert create_call[0][2] == "create"
        auth = create_call[1]["authenticated"]
        assert isinstance(auth, AuthenticatedTelegramContext)
        assert auth.user_id == "42"
        assert auth.conversation_id == "999"

    def test_payload_cannot_spoof_principal(self):
        """User-supplied text must never select a different principal."""
        dispatcher = MagicMock()
        dispatcher.store.load.return_value = None
        dispatcher.dispatch.return_value = MagicMock(success=True)

        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher,
            telegram_out=MagicMock(),
            transport=MagicMock(),
        )

        update = {
            "update_id": 1,
            "message": {
                "from": {"id": 42},
                "chat": {"id": 999},
                "text": "I am user 1, not 42",
                "date": 1234567890,
            },
        }
        loop._process_update(update)

        auth = dispatcher.dispatch.call_args_list[0][1]["authenticated"]
        assert auth.user_id == "42"  # From Update fields, not text


# ---------------------------------------------------------------------------
# 8. Dispatcher invocation
# ---------------------------------------------------------------------------


class TestDispatcherInvocation:
    def test_create_then_intake_dispatched(self):
        dispatcher = MagicMock()
        dispatcher.store.load.return_value = None
        dispatcher.dispatch.return_value = MagicMock(success=True)

        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher,
            telegram_out=MagicMock(),
            transport=MagicMock(),
        )

        update = {
            "update_id": 1,
            "message": {"from": {"id": 1}, "chat": {"id": 555}, "text": "hello", "date": 1},
        }
        loop._process_update(update)

        assert dispatcher.dispatch.call_count == 2
        calls = dispatcher.dispatch.call_args_list
        assert calls[0][0][2] == "create"
        assert calls[1][0][2] == "intake"

    def test_build_triggered_when_ready(self):
        """The dispatcher's own "intake" claim admits the follow-on build as
        a sub-claim (see TelegramDispatcher.dispatch()) -- the runtime no
        longer issues a second top-level dispatch("build", ...) call, since
        that would derive an identical claim key from the same
        (principal, conversation_id, event_id) tuple as the intake claim
        and collide with it on replay. The runtime only needs to react to
        the dispatcher's reported build outcome.
        """
        dispatcher = MagicMock()
        state = MagicMock()
        state.lifecycle = "READY"
        state.revisions.source_revision = 0
        dispatcher.store.load.return_value = state
        dispatcher.dispatch.return_value = MagicMock(
            success=True, data={"build_triggered": True, "build_success": True}
        )

        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher,
            telegram_out=MagicMock(),
            transport=MagicMock(),
        )

        update = {
            "update_id": 1,
            "message": {"from": {"id": 1}, "chat": {"id": 555}, "text": "hello", "date": 1},
        }
        loop._process_update(update)

        calls = dispatcher.dispatch.call_args_list
        actions = [c[0][2] for c in calls]
        # Project already exists (store.load returns a state), so "create"
        # is skipped. Build is admitted as a sub-claim INSIDE the "intake"
        # dispatch call, never as a second top-level dispatch under the
        # same event.
        assert actions == ["intake"]


# ---------------------------------------------------------------------------
# 9. Duplicate Telegram update does not cause duplicate mutation
# ---------------------------------------------------------------------------


class TestDuplicateUpdate:
    def test_duplicate_update_idempotent(self, tmp_path):
        """The dispatcher's dispatch_events dedup prevents duplicate mutations."""
        store = ProjectStateStore(tmp_path / "state")
        intake = MagicMock()
        intake.process.return_value = MagicMock(
            readiness=MagicMock(value="NEEDS_CLARIFICATION"),
            brief={},
        )
        intake.apply_to_project.return_value = None

        dispatcher = TelegramDispatcher(store, intake)

        update = {
            "update_id": 1,
            "message": {"from": {"id": 1}, "chat": {"id": 555}, "text": "hello", "date": 1},
        }
        auth = AuthenticatedTelegramContext("1", "555")

        # First dispatch: create + intake
        r1 = dispatcher.dispatch(update, "tg-555", "create", authenticated=auth)
        assert r1.success
        r2 = dispatcher.dispatch(update, "tg-555", "intake", authenticated=auth)
        assert r2.success

        # Second dispatch of SAME update: create returns PROJECT_EXISTS,
        # intake returns duplicate=True
        r3 = dispatcher.dispatch(update, "tg-555", "create", authenticated=auth)
        assert not r3.success  # PROJECT_EXISTS
        r4 = dispatcher.dispatch(update, "tg-555", "intake", authenticated=auth)
        assert r4.success
        assert r4.data.get("duplicate") is True

        # Intake process should only have been called once
        assert intake.process.call_count == 1


# ---------------------------------------------------------------------------
# 10. Secrets not exposed in errors/logging
# ---------------------------------------------------------------------------


class TestSecretSafety:
    def test_provider_error_does_not_contain_token(self):
        transport = MagicMock()
        response = MagicMock()
        response.body = json.dumps({
            "ok": False,
            "description": "Unauthorized",
        }).encode()
        response.status = 401
        transport.request.return_value = response

        token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
        loop = TelegramReceiveLoop(
            bot_token=token,
            dispatcher=MagicMock(),
            telegram_out=MagicMock(),
            transport=transport,
        )
        with pytest.raises(TelegramProviderError) as exc_info:
            loop._get_updates()
        assert token not in str(exc_info.value)

    def test_config_error_does_not_contain_secrets(self):
        with patch.dict(os.environ, _env(TELEGRAM_BOT_TOKEN=""), clear=False):
            with pytest.raises(ConfigurationError) as exc_info:
                load_runtime_config()
            assert "123456" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# 11. Main entrypoint
# ---------------------------------------------------------------------------


class TestMainEntrypoint:
    def test_main_fails_closed_on_missing_config(self):
        with patch.dict(os.environ, {}, clear=True):
            # Ensure required vars are absent
            for key in ("TELEGRAM_BOT_TOKEN", "VERCEL_TOKEN", "VERCEL_TEAM_ID"):
                os.environ.pop(key, None)
            assert main() == 1

    def test_main_fails_closed_on_compose_error(self, tmp_path):
        with patch.dict(os.environ, _env(), clear=False):
            with patch("app.runtime.compose", side_effect=RuntimeError("boom")):
                assert main() == 1


# ---------------------------------------------------------------------------
# 12. Natural-conversation intent classification
# ---------------------------------------------------------------------------


class TestIntentClassification:
    def _loop(self, hermes=None, lifecycle="PREVIEW_READY"):
        dispatcher = MagicMock()
        state = MagicMock()
        state.lifecycle = lifecycle
        state.revisions.queued_revision_seq = 0
        state.revisions.source_revision = 1
        state.conversation_id = "555"
        dispatcher.store.load.return_value = state
        dispatcher.dispatch.return_value = MagicMock(success=True)
        return TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher,
            telegram_out=MagicMock(),
            hermes=hermes,
            transport=MagicMock(),
        )

    def test_pre_build_lifecycle_always_intake(self):
        """Pre-build states only offer INTAKE — no FAST call needed."""
        hermes = MagicMock()
        loop = self._loop(hermes=hermes, lifecycle="DISCOVERING")
        intent = loop._classify_intent("make the hero smaller", "DISCOVERING")
        assert intent == ConversationIntent.INTAKE
        hermes._run_fast_programmatic.assert_not_called()

    def test_preview_ready_change_request_classified_revise(self):
        """PREVIEW_READY + change request -> REVISE."""
        hermes = MagicMock()
        hermes._run_fast_programmatic.return_value = MagicMock(
            success=True, response="REVISE"
        )
        loop = self._loop(hermes=hermes, lifecycle="PREVIEW_READY")
        intent = loop._classify_intent("make the hero smaller", "PREVIEW_READY")
        assert intent == ConversationIntent.REVISE

    def test_preview_ready_go_live_classified_publish(self):
        """PREVIEW_READY + explicit go-live -> PUBLISH."""
        hermes = MagicMock()
        hermes._run_fast_programmatic.return_value = MagicMock(
            success=True, response="PUBLISH"
        )
        loop = self._loop(hermes=hermes, lifecycle="PREVIEW_READY")
        intent = loop._classify_intent("oke live", "PREVIEW_READY")
        assert intent == ConversationIntent.PUBLISH

    def test_preview_ready_approve_classified_approve(self):
        """PREVIEW_READY + clear acceptance -> APPROVE."""
        hermes = MagicMock()
        hermes._run_fast_programmatic.return_value = MagicMock(
            success=True, response="APPROVE"
        )
        loop = self._loop(hermes=hermes, lifecycle="PREVIEW_READY")
        intent = loop._classify_intent("looks good", "PREVIEW_READY")
        assert intent == ConversationIntent.APPROVE

    def test_ambiguous_intent_falls_back_to_intake(self):
        """Ambiguous FAST response -> INTAKE."""
        hermes = MagicMock()
        hermes._run_fast_programmatic.return_value = MagicMock(
            success=True, response="MAYBE_REVISE"
        )
        loop = self._loop(hermes=hermes, lifecycle="PREVIEW_READY")
        intent = loop._classify_intent("hmm not sure", "PREVIEW_READY")
        assert intent == ConversationIntent.INTAKE

    def test_malformed_fast_result_falls_back_to_intake(self):
        """Malformed FAST response -> INTAKE."""
        hermes = MagicMock()
        hermes._run_fast_programmatic.return_value = MagicMock(
            success=True, response="not a valid intent at all"
        )
        loop = self._loop(hermes=hermes, lifecycle="PREVIEW_READY")
        intent = loop._classify_intent("hello", "PREVIEW_READY")
        assert intent == ConversationIntent.INTAKE

    def test_fast_failure_falls_back_to_intake(self):
        """FAST failure -> INTAKE."""
        hermes = MagicMock()
        hermes._run_fast_programmatic.return_value = MagicMock(
            success=False, error="provider error"
        )
        loop = self._loop(hermes=hermes, lifecycle="PREVIEW_READY")
        intent = loop._classify_intent("hello", "PREVIEW_READY")
        assert intent == ConversationIntent.INTAKE

    def test_fast_exception_falls_back_to_intake(self):
        """FAST exception -> INTAKE."""
        hermes = MagicMock()
        hermes._run_fast_programmatic.side_effect = RuntimeError("boom")
        loop = self._loop(hermes=hermes, lifecycle="PREVIEW_READY")
        intent = loop._classify_intent("hello", "PREVIEW_READY")
        assert intent == ConversationIntent.INTAKE

    def test_hermes_unavailable_falls_back_to_intake(self):
        """No Hermes adapter -> INTAKE."""
        loop = self._loop(hermes=None, lifecycle="PREVIEW_READY")
        intent = loop._classify_intent("hello", "PREVIEW_READY")
        assert intent == ConversationIntent.INTAKE

    def test_live_allows_revise_but_not_publish(self):
        """LIVE state allows REVISE but not PUBLISH."""
        hermes = MagicMock()
        hermes._run_fast_programmatic.return_value = MagicMock(
            success=True, response="PUBLISH"
        )
        loop = self._loop(hermes=hermes, lifecycle="LIVE")
        intent = loop._classify_intent("publish again", "LIVE")
        # PUBLISH is not in LIVE's valid intents, so it falls back to INTAKE
        assert intent == ConversationIntent.INTAKE


# ---------------------------------------------------------------------------
# 13. Intent routing through dispatcher
# ---------------------------------------------------------------------------


class TestIntentRouting:
    def _loop_with_state(self, lifecycle="PREVIEW_READY", queued_seq=0):
        dispatcher = MagicMock()
        state = MagicMock()
        state.lifecycle = lifecycle
        state.revisions.queued_revision_seq = queued_seq
        state.revisions.source_revision = 1
        state.conversation_id = "555"
        dispatcher.store.load.return_value = state
        dispatcher.dispatch.return_value = MagicMock(success=True)
        return dispatcher, state

    def test_revise_dispatches_with_correct_seq(self):
        """REVISE intent dispatches revise with seq = queued_revision_seq + 1."""
        dispatcher, state = self._loop_with_state(queued_seq=2)
        hermes = MagicMock()
        hermes._run_fast_programmatic.return_value = MagicMock(
            success=True, response="REVISE"
        )
        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher,
            telegram_out=MagicMock(),
            hermes=hermes,
            transport=MagicMock(),
        )
        update = {
            "update_id": 1,
            "message": {"from": {"id": 1}, "chat": {"id": 555}, "text": "make hero smaller", "date": 1},
        }
        loop._process_update(update)

        calls = dispatcher.dispatch.call_args_list
        revise_calls = [c for c in calls if c[0][2] == "revise"]
        assert len(revise_calls) == 1
        assert revise_calls[0][1]["seq"] == 3  # queued_seq(2) + 1

    def test_approve_dispatches_approve_action(self):
        """APPROVE intent dispatches approve action."""
        dispatcher, state = self._loop_with_state()
        hermes = MagicMock()
        hermes._run_fast_programmatic.return_value = MagicMock(
            success=True, response="APPROVE"
        )
        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher,
            telegram_out=MagicMock(),
            hermes=hermes,
            transport=MagicMock(),
        )
        update = {
            "update_id": 1,
            "message": {"from": {"id": 1}, "chat": {"id": 555}, "text": "looks good", "date": 1},
        }
        loop._process_update(update)

        calls = dispatcher.dispatch.call_args_list
        approve_calls = [c for c in calls if c[0][2] == "approve"]
        assert len(approve_calls) == 1

    def test_publish_dispatches_single_publish_action(self):
        """PUBLISH intent dispatches exactly one publish action."""
        dispatcher, state = self._loop_with_state()
        hermes = MagicMock()
        hermes._run_fast_programmatic.return_value = MagicMock(
            success=True, response="PUBLISH"
        )
        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher,
            telegram_out=MagicMock(),
            hermes=hermes,
            transport=MagicMock(),
        )
        update = {
            "update_id": 1,
            "message": {"from": {"id": 1}, "chat": {"id": 555}, "text": "oke live", "date": 1},
        }
        loop._process_update(update)

        calls = dispatcher.dispatch.call_args_list
        actions = [c[0][2] for c in calls]
        assert "publish" in actions
        assert "approve" not in actions
        assert actions.count("publish") == 1

    def test_publish_failure_sends_error_reply(self):
        """PUBLISH dispatch failure sends error reply to user."""
        dispatcher, state = self._loop_with_state()
        dispatcher.dispatch.return_value = MagicMock(
            success=False, error_code="STALE_APPROVAL"
        )
        hermes = MagicMock()
        hermes._run_fast_programmatic.return_value = MagicMock(
            success=True, response="PUBLISH"
        )
        telegram_out = MagicMock()
        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher,
            telegram_out=telegram_out,
            hermes=hermes,
            transport=MagicMock(),
        )
        update = {
            "update_id": 1,
            "message": {"from": {"id": 1}, "chat": {"id": 555}, "text": "oke live", "date": 1},
        }
        loop._process_update(update)

        calls = dispatcher.dispatch.call_args_list
        actions = [c[0][2] for c in calls]
        assert "publish" in actions
        assert "approve" not in actions
        telegram_out.send_text.assert_called_once_with(
            "555", "The approval is stale. Please review the latest preview."
        )

    def test_intake_dispatches_intake_action(self):
        """INTAKE intent dispatches intake action."""
        dispatcher, state = self._loop_with_state(lifecycle="DISCOVERING")
        hermes = MagicMock()
        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher,
            telegram_out=MagicMock(),
            hermes=hermes,
            transport=MagicMock(),
        )
        update = {
            "update_id": 1,
            "message": {"from": {"id": 1}, "chat": {"id": 555}, "text": "hello", "date": 1},
        }
        loop._process_update(update)

        calls = dispatcher.dispatch.call_args_list
        actions = [c[0][2] for c in calls]
        assert "intake" in actions

    def test_unsupported_intent_cannot_become_arbitrary_action(self):
        """FAST returning an unsupported intent string cannot become a dispatcher action."""
        dispatcher, state = self._loop_with_state()
        hermes = MagicMock()
        hermes._run_fast_programmatic.return_value = MagicMock(
            success=True, response="DELETE_EVERYTHING"
        )
        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher,
            telegram_out=MagicMock(),
            hermes=hermes,
            transport=MagicMock(),
        )
        update = {
            "update_id": 1,
            "message": {"from": {"id": 1}, "chat": {"id": 555}, "text": "delete everything", "date": 1},
        }
        loop._process_update(update)

        calls = dispatcher.dispatch.call_args_list
        actions = [c[0][2] for c in calls]
        # Unsupported intent falls back to INTAKE
        assert "intake" in actions
        assert "delete" not in actions
        assert "publish" not in actions


# ---------------------------------------------------------------------------
# 14. Duplicate update idempotency for revision and publication
# ---------------------------------------------------------------------------


class TestDuplicateRevisionAndPublication:
    def test_duplicate_revise_update_idempotent(self, tmp_path):
        """Duplicate Telegram update does not cause duplicate revision."""
        store = ProjectStateStore(tmp_path / "state")
        revise = MagicMock()
        revise.reserve.return_value = MagicMock(success=True)
        revise.apply.return_value = MagicMock(success=True)

        dispatcher = TelegramDispatcher(store, None, revise=revise)

        update = {
            "update_id": 1,
            "message": {"from": {"id": 1}, "chat": {"id": 555}, "text": "make hero smaller", "date": 1},
        }
        auth = AuthenticatedTelegramContext("1", "555")

        # Create project first
        dispatcher.dispatch(update, "tg-555", "create", authenticated=auth)

        # Set lifecycle to PREVIEW_READY
        with store.acquire_writer("tg-555") as state:
            state.lifecycle = "PREVIEW_READY"
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

        # First revise dispatch
        r1 = dispatcher.dispatch(update, "tg-555", "revise", authenticated=auth, seq=1)
        assert r1.success

        # Duplicate revise dispatch of SAME update
        r2 = dispatcher.dispatch(update, "tg-555", "revise", authenticated=auth, seq=1)
        assert r2.success
        assert r2.data.get("duplicate") is True

        # Reserve should only have been called once
        assert revise.reserve.call_count == 1
        assert revise.apply.call_count == 1

    def test_duplicate_publish_update_idempotent(self, tmp_path):
        """Duplicate Telegram update does not cause duplicate publication."""
        store = ProjectStateStore(tmp_path / "state")
        promote = MagicMock()
        promote.approve.return_value = MagicMock(success=True)
        promote.promote.return_value = MagicMock(success=True)

        dispatcher = TelegramDispatcher(store, None, promote=promote, workspace_for=lambda pid: tmp_path / "ws")

        update = {
            "update_id": 1,
            "message": {"from": {"id": 1}, "chat": {"id": 555}, "text": "oke live", "date": 1},
        }
        auth = AuthenticatedTelegramContext("1", "555")

        # Create project first
        dispatcher.dispatch(update, "tg-555", "create", authenticated=auth)

        # Set lifecycle to PREVIEW_READY with shown preview
        with store.acquire_writer("tg-555") as state:
            state.lifecycle = "PREVIEW_READY"
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

        # First publish dispatch
        r1 = dispatcher.dispatch(update, "tg-555", "publish", authenticated=auth)
        assert r1.success

        # Duplicate publish dispatch of SAME update
        r2 = dispatcher.dispatch(update, "tg-555", "publish", authenticated=auth)
        assert r2.success
        assert r2.data.get("duplicate") is True

        # Approve and promote should each have been called exactly once
        assert promote.approve.call_count == 1
        assert promote.promote.call_count == 1


# ---------------------------------------------------------------------------
# 15. Lifecycle guards remain authoritative
# ---------------------------------------------------------------------------


class TestLifecycleGuards:
    def test_revise_blocked_in_wrong_lifecycle(self):
        """REVISE intent in DISCOVERING lifecycle falls back to INTAKE."""
        dispatcher = MagicMock()
        state = MagicMock()
        state.lifecycle = "DISCOVERING"
        state.revisions.queued_revision_seq = 0
        state.conversation_id = "555"
        dispatcher.store.load.return_value = state
        dispatcher.dispatch.return_value = MagicMock(success=True)

        hermes = MagicMock()
        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher,
            telegram_out=MagicMock(),
            hermes=hermes,
            transport=MagicMock(),
        )
        update = {
            "update_id": 1,
            "message": {"from": {"id": 1}, "chat": {"id": 555}, "text": "make hero smaller", "date": 1},
        }
        loop._process_update(update)

        calls = dispatcher.dispatch.call_args_list
        actions = [c[0][2] for c in calls]
        # DISCOVERING only allows INTAKE, so revise is never dispatched
        assert "revise" not in actions
        assert "intake" in actions

    def test_publish_blocked_in_wrong_lifecycle(self):
        """PUBLISH intent in RUNNING lifecycle falls back to INTAKE."""
        dispatcher = MagicMock()
        state = MagicMock()
        state.lifecycle = "RUNNING"
        state.revisions.queued_revision_seq = 0
        state.conversation_id = "555"
        dispatcher.store.load.return_value = state
        dispatcher.dispatch.return_value = MagicMock(success=True)

        hermes = MagicMock()
        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher,
            telegram_out=MagicMock(),
            hermes=hermes,
            transport=MagicMock(),
        )
        update = {
            "update_id": 1,
            "message": {"from": {"id": 1}, "chat": {"id": 555}, "text": "oke live", "date": 1},
        }
        loop._process_update(update)

        calls = dispatcher.dispatch.call_args_list
        actions = [c[0][2] for c in calls]
        # RUNNING only allows INTAKE, so publish is never dispatched
        assert "publish" not in actions
        assert "intake" in actions


# ---------------------------------------------------------------------------
# 16. WhatsApp remains dormant
# ---------------------------------------------------------------------------


class TestWhatsAppDormant:
    def test_whatsapp_not_instantiated_in_compose(self, tmp_path):
        """WhatsApp adapter is not constructed by the runtime composition."""
        config = _make_config(tmp_path)
        comp = compose(config)
        # No whatsapp attribute on composition
        assert not hasattr(comp, "whatsapp")
        assert not hasattr(comp, "whatsapp_out")


# ---------------------------------------------------------------------------
# 17. Toolchain and Smoke Preflight Checks (HIGH-1, HIGH-2)
# ---------------------------------------------------------------------------


class TestPreflightChecks:
    def test_preflight_node_toolchain_success_on_system(self):
        """Current system node satisfies the requirement >=26 <27."""
        preflight_node_toolchain()

    def test_preflight_node_toolchain_fails_on_old_node(self):
        """Node 22 (EBADENGINE on VPS) fails closed with descriptive error."""
        with patch("subprocess.run") as mock_run:
            def fake_run(cmd, *args, **kwargs):
                name = Path(str(cmd[0])).stem.lower()
                if name == "node":
                    return MagicMock(returncode=0, stdout="v22.23.2\n")
                if name == "npm":
                    return MagicMock(returncode=0, stdout="10.9.8\n")
                return MagicMock(returncode=0, stdout="")
            mock_run.side_effect = fake_run
            with pytest.raises(RuntimeError) as exc_info:
                preflight_node_toolchain()
            assert "EBADENGINE" in str(exc_info.value) or "Node version v22.23.2 does not satisfy" in str(exc_info.value)

    def test_preflight_node_toolchain_fails_if_node_missing(self):
        """Missing node executable fails closed."""
        with patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError) as exc_info:
                preflight_node_toolchain()
            assert "Node.js executable ('node') not found" in str(exc_info.value)

    def test_preflight_smoke_support_success(self):
        """Callable browser factory passes preflight."""
        mock_factory = MagicMock()
        mock_ctx = MagicMock()
        mock_page = MagicMock()
        mock_ctx.new_page.return_value = mock_page
        mock_factory.return_value = mock_ctx
        preflight_smoke_support(mock_factory)
        mock_factory.assert_called_once()
        mock_page.close.assert_called_once()
        mock_ctx.close.assert_called_once()

    def test_preflight_smoke_support_fails_closed_when_factory_none(self):
        """None factory fails closed with descriptive RuntimeError."""
        with pytest.raises(RuntimeError) as exc_info:
            preflight_smoke_support(None)
        assert "Playwright browser factory is unavailable" in str(exc_info.value)

    def test_main_preflight_flag_exits_cleanly(self):
        """python -m app --preflight runs preflight and exits 0."""
        with patch("app.runtime.preflight_node_toolchain") as mock_node, \
             patch("app.runtime.preflight_smoke_support") as mock_smoke, \
             patch("app.runtime.load_runtime_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(smoke_browser_factory=MagicMock())
            exit_code = main(["--preflight"])
            assert exit_code == 0
            mock_node.assert_called_once()
            mock_smoke.assert_called_once()


# ---------------------------------------------------------------------------
# 18. Smoke Tester Composition Wiring (HIGH-2)
# ---------------------------------------------------------------------------


class TestSmokeTesterWiring:
    def test_smoke_tester_wired_into_deps(self, tmp_path):
        """PreviewDeps and PromoteDeps receive a PreviewSmokeTester with a callable run."""
        mock_factory = MagicMock()
        config = _make_config(tmp_path)
        object.__setattr__(config, "smoke_browser_factory", mock_factory)
        comp = compose(config)
        assert callable(comp.preview.deps.smoke.run)
        assert callable(comp.promote.deps.smoke.run)


# ---------------------------------------------------------------------------
# 19. Preview Reconciliation in Telegram Receive Loop (HIGH-4)
# ---------------------------------------------------------------------------


class TestPreviewReconciliation:
    def test_reconcile_preview_triggered_when_preview_ready_without_card(self):
        """When PREVIEW_READY has tested_snapshot but no latest_shown_preview, reconcile."""
        dispatcher = MagicMock()
        state = MagicMock()
        state.lifecycle = "PREVIEW_READY"
        state.deployment = {"tested_snapshot": {"dist_hash": "abc"}}
        state.latest_shown_preview = None
        state.conversation_id = "555"
        dispatcher.store.load.return_value = state
        dispatcher.dispatch.return_value = MagicMock(success=True)

        loop = TelegramReceiveLoop(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            dispatcher=dispatcher,
            telegram_out=MagicMock(),
            hermes=MagicMock(),
            transport=MagicMock(),
        )
        update = {
            "update_id": 1,
            "message": {"from": {"id": 1}, "chat": {"id": 555}, "text": "halo", "date": 1},
        }
        loop._process_update(update)

        calls = dispatcher.dispatch.call_args_list
        actions = [c[0][2] for c in calls]
        assert "reconcile_preview" in actions
