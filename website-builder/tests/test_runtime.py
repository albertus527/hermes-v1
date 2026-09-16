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
    RuntimeComposition,
    TelegramProviderError,
    TelegramReceiveLoop,
    compose,
    load_runtime_config,
    main,
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
        dispatcher = MagicMock()
        state = MagicMock()
        state.lifecycle = "READY"
        state.revisions.source_revision = 0
        dispatcher.store.load.return_value = state
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

        calls = dispatcher.dispatch.call_args_list
        actions = [c[0][2] for c in calls]
        assert "build" in actions


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
