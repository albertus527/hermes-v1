"""Real temporary YAML/loaders/resolvers. No live inference or operator homes."""
from __future__ import annotations

import logging
import os
import socket
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.hermes.adapter import HermesAdapter
from app.runtime import RuntimeConfig, main, preflight_role_validation
from hermes_constants import (
    get_hermes_home, reset_hermes_home_override, set_hermes_home_override,
)
from hermes_cli.runtime_provider import resolve_runtime_provider

ROLES = ("FAST", "FRONTEND", "VISION")


def write_profile(home, *, endpoint="website", disabled=(), vision=True):
    home.mkdir(parents=True, exist_ok=True)
    cfg = {
        "model": {"default": "unrelated-default", "provider": "site-fast"},
        "website_builder": {"models": {
            role: {"model": f"test-{role.lower()}", "provider": f"site-{role.lower()}"}
            for role in ROLES
        }},
        "providers": {
            f"site-{role.lower()}": {
                "enabled": role not in disabled,
                "base_url": f"https://{endpoint}.invalid/{role.lower()}/v1",
                "api_key": f"test-only-{endpoint}-{role}",
                "models": {f"test-{role.lower()}": {"supports_vision": vision}},
            }
            for role in ROLES
        },
    }
    save(home, cfg)
    return cfg


def save(home, cfg):
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    # Clear inherited credentials, endpoint overrides, and HOME before resolution.
    with patch.dict(os.environ, {
        "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
        "HERMES_HOME": str(tmp_path / "ambient"),
    }, clear=True):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        token = set_hermes_home_override(tmp_path / "ambient")
        def deny_network(*args, **kwargs):
            raise AssertionError("Network forbidden in role preflight tests")
        monkeypatch.setattr(socket.socket, "connect", deny_network)
        monkeypatch.setattr(socket, "create_connection", deny_network)
        try:
            yield
        finally:
            reset_hermes_home_override(token)


@pytest.fixture
def profile(tmp_path):
    home = tmp_path / "website"
    cfg = write_profile(home)
    return home, cfg, HermesAdapter(store=None, hermes_home=home, repo_root=tmp_path)


def runtime_config(home):
    return RuntimeConfig(
        telegram_bot_token="test-only", hermes_home=home,
        workspace_root=home / "ws", state_root=home / "state",
        output_repo_path=home / "output", vercel_token="test-only",
        vercel_team_id="test-team", vercel_ownership_namespace="test",
    )


def test_all_roles_real_resolution(profile):
    home, cfg, adapter = profile
    report = adapter.validate_role_configuration()
    assert report["ok"], report
    assert set(report["roles"]) == set(ROLES)
    assert not report["errors"]
    assert report["config_path"] == str(home / "config.yaml")
    assert get_hermes_home() != home


@pytest.mark.parametrize("role", ROLES)
def test_each_provider_must_really_resolve(profile, role):
    home, cfg, adapter = profile
    cfg["providers"][f"site-{role.lower()}"]["enabled"] = False
    save(home, cfg)
    report = adapter.validate_role_configuration()
    assert not report["ok"]
    assert report["errors"] == {role: "provider resolution failed"}
    assert set(report["roles"]) == set(ROLES) - {role}


def test_aggregate_resolution_failures(profile):
    home, cfg, adapter = profile
    for entry in cfg["providers"].values():
        entry["enabled"] = False
    save(home, cfg)
    report = adapter.validate_role_configuration()
    assert not report["ok"]
    assert set(report["errors"]) == set(ROLES)
    assert not report["roles"]


@pytest.mark.parametrize("value", [None, "not-a-mapping", {}, {"model": " ", "provider": "site-fast"}])
def test_malformed_selection(profile, value):
    home, cfg, adapter = profile
    cfg["website_builder"]["models"]["FAST"] = value
    save(home, cfg)
    report = adapter.validate_role_configuration()
    assert report["errors"] == {"FAST": "missing or malformed role configuration"}
    assert set(report["roles"]) == {"FRONTEND", "VISION"}


def test_missing_config_fails_closed(tmp_path):
    adapter = HermesAdapter(store=None, hermes_home=tmp_path / "absent")
    report = adapter.validate_role_configuration()
    assert not report["ok"]
    assert set(report["errors"]) == set(ROLES)


def test_vision_real_config_override(profile):
    home, cfg, adapter = profile
    cfg["providers"]["site-vision"]["models"]["test-vision"]["supports_vision"] = False
    save(home, cfg)
    report = adapter.validate_role_configuration()
    assert not report["ok"]
    assert set(report["errors"]) == {"VISION"}
    assert "image input" in report["errors"]["VISION"]


@pytest.mark.parametrize("role", ROLES)
def test_historical_initial_scope_only_leaks_then_fixed(profile, tmp_path, role):
    home, cfg, adapter = profile
    ambient = tmp_path / "ambient"
    write_profile(ambient, endpoint="poison")
    # Authentic old sequence: KEEP the initial scoped loader. Only resolution
    # happens after that context closes. Same provider names, different route.
    selected = adapter._role_selection(adapter._load_role_config(), role)
    assert selected == (f"test-{role.lower()}", f"site-{role.lower()}")
    assert get_hermes_home() == ambient
    old_runtime = resolve_runtime_provider(requested=selected[1], target_model=selected[0])
    assert old_runtime["base_url"] == f"https://poison.invalid/{role.lower()}/v1"
    assert old_runtime["api_key"] == f"test-only-poison-{role}"

    # Exercise the fixed production method, replacing ONLY the inference agent.
    # Real config, resolver, skill loader, session DB, capability lookup remain.
    agent = MagicMock()
    agent.run_conversation.return_value = {"final_response": "resolved"}
    with patch("app.hermes.adapter.AIAgent", return_value=agent) as constructor:
        result = adapter._run_fast_programmatic(
            "test", role=role, require_vision=role == "VISION",
        )
    assert result.success, result.error
    kwargs = constructor.call_args.kwargs
    assert kwargs["base_url"] == f"https://website.invalid/{role.lower()}/v1"
    assert kwargs["api_key"] == f"test-only-website-{role}"
    assert kwargs["model"] == selected[0]
    assert kwargs["requested_provider"] == selected[1]
    assert kwargs["enabled_toolsets"] == []
    agent.close.assert_called_once()
    assert get_hermes_home() == ambient


def test_preflight_scope_not_ambient(profile, tmp_path):
    home, cfg, adapter = profile
    write_profile(tmp_path / "ambient", disabled=ROLES, vision=False)
    assert adapter.validate_role_configuration()["ok"]
    assert preflight_role_validation(runtime_config(home))
    assert get_hermes_home() == tmp_path / "ambient"


@pytest.mark.parametrize("role", ROLES)
def test_main_refuses_unresolvable_role_before_composition(profile, monkeypatch, role):
    home, cfg, adapter = profile
    cfg["providers"][f"site-{role.lower()}"]["enabled"] = False
    save(home, cfg)
    for name, value in {
        "HERMES_HOME": str(home),
        "TELEGRAM_BOT_TOKEN": "test-only",
        "VERCEL_TOKEN": "test-only",
        "VERCEL_TEAM_ID": "test-team",
        "WEBSITE_BUILDER_WORKSPACE_ROOT": str(home / "ws"),
        "WEBSITE_BUILDER_STATE_ROOT": str(home / "state"),
        "WEBSITE_BUILDER_OUTPUT_REPO": str(home / "output"),
    }.items():
        monkeypatch.setenv(name, value)
    # Only the external receive loop is replaced. Composition would create
    # directories; refusal must happen before even those local side effects.
    with patch("app.runtime.TelegramReceiveLoop") as loop:
        assert main() == 1
    loop.assert_not_called()
    assert not (home / "state").exists()
    assert not (home / "ws").exists()


@pytest.mark.parametrize("disabled", [False, True])
def test_static_logs_never_echo_config_or_exception(profile, caplog, disabled):
    home, cfg, adapter = profile
    sentinel = "secret-in-provider-name"
    cfg["providers"][sentinel] = cfg["providers"].pop("site-fast")
    cfg["providers"][sentinel]["enabled"] = not disabled
    cfg["website_builder"]["models"]["FAST"] = {
        "provider": sentinel, "model": "secret-in-model-name",
    }
    save(home, cfg)
    with caplog.at_level(logging.DEBUG):
        assert preflight_role_validation(runtime_config(home)) is (not disabled)
    assert sentinel not in caplog.text
    assert "secret-in-model-name" not in caplog.text
    assert "test-only-website" not in caplog.text
    app_text = "\n".join(
        r.getMessage() for r in caplog.records if r.name == "app.runtime"
    )
    if disabled:
        assert "provider resolution failed" in app_text
        assert str(home / "config.yaml") in app_text
        assert "website_builder.models" in app_text


@pytest.mark.parametrize("role", ROLES)
def test_profile_dotenv_beats_ambient_credentials(profile, monkeypatch, role):
    from agent.secret_scope import current_secret_scope

    home, cfg, adapter = profile
    entry = cfg["providers"][f"site-{role.lower()}"]
    entry.pop("api_key")
    entry["key_env"] = "SITE_TEST_KEY"
    save(home, cfg)
    (home / ".env").write_text("SITE_TEST_KEY=profile-only-secret\n", encoding="utf-8")
    monkeypatch.setenv("SITE_TEST_KEY", "ambient-poison-secret")
    before = current_secret_scope()
    assert adapter.validate_role_configuration()["ok"]
    with patch("app.hermes.adapter.AIAgent") as constructor:
        constructor.return_value.run_conversation.return_value = {"final_response": "ok"}
        result = adapter._run_fast_programmatic("test", role=role)
    assert result.success, result.error
    assert constructor.call_args.kwargs["api_key"] == "profile-only-secret"
    assert current_secret_scope() is before
    assert os.environ["SITE_TEST_KEY"] == "ambient-poison-secret"


def test_missing_profile_secret_never_borrows_ambient(profile, monkeypatch):
    home, cfg, adapter = profile
    cfg["providers"]["site-fast"].pop("api_key")
    cfg["providers"]["site-fast"]["key_env"] = "SITE_TEST_KEY"
    save(home, cfg)
    monkeypatch.setenv("SITE_TEST_KEY", "ambient-poison-secret")
    with adapter._hermes_home_scope():
        runtime = resolve_runtime_provider(requested="site-fast", target_model="test-fast")
    assert runtime["api_key"] == "no-key-required"


@pytest.mark.parametrize("default_support, role_support", [(True, False), (False, True)])
def test_vision_uses_role_not_default_capability(profile, default_support, role_support):
    home, cfg, adapter = profile
    cfg["model"]["supports_vision"] = default_support
    cfg["providers"]["site-vision"]["models"]["test-vision"]["supports_vision"] = role_support
    save(home, cfg)
    assert adapter.validate_role_configuration()["ok"] is role_support
    with patch("app.hermes.adapter.AIAgent") as constructor:
        constructor.return_value.run_conversation.return_value = {"final_response": "ok"}
        result = adapter._run_fast_programmatic("test", role="VISION", require_vision=True)
    assert result.success is role_support
    assert constructor.called is role_support


def test_empty_openrouter_credentials_fail_preflight_and_runtime(profile):
    home, cfg, adapter = profile
    cfg["website_builder"]["models"]["FAST"]["provider"] = "openrouter"
    save(home, cfg)
    assert adapter.validate_role_configuration()["errors"] == {"FAST": "provider resolution failed"}
    with patch("app.hermes.adapter.AIAgent") as constructor:
        result = adapter._run_fast_programmatic("test", role="FAST")
    assert not result.success
    constructor.assert_not_called()


def test_diagnostic_filter_covers_auth_logger(profile, caplog):
    from hermes_cli.auth import _xai_validate_inference_base_url
    from concurrent.futures import ThreadPoolExecutor

    home, cfg, adapter = profile
    original = adapter._validate_role_configuration
    auth_logger = logging.getLogger("hermes_cli.auth")
    original_filters = list(auth_logger.filters)

    def validate():
        _xai_validate_inference_base_url("http://secret-url.invalid/token", fallback="https://api.x.ai")
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(auth_logger.warning, "other-thread-diagnostic").result()
        return original()

    with caplog.at_level(logging.DEBUG), patch.object(adapter, "_validate_role_configuration", validate):
        assert adapter.validate_role_configuration()["ok"]
    assert "secret-url" not in caplog.text
    assert "other-thread-diagnostic" in caplog.text
    assert "preflight dependency diagnostic" in caplog.text
    assert auth_logger.filters == original_filters


def test_malformed_yaml_fails_closed_without_stderr_leak(profile, capsys):
    home, cfg, adapter = profile
    secret_line = "secret-in-malformed-yaml-source"
    (home / "config.yaml").write_text(f"model: [unterminated\n# {secret_line}\n", encoding="utf-8")
    report = adapter.validate_role_configuration()
    assert not report["ok"]
    assert set(report["errors"]) == set(ROLES)
    captured = capsys.readouterr()
    assert secret_line not in captured.err
    assert secret_line not in captured.out
