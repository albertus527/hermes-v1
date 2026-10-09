"""Regression tests for the application-owned operational env loader.

These pin the credential-boundary properties of ``app.core.app_env``:

* the operational env file lives under the application STATE ROOT, never under
  the generation profile home (the same invariant ``BypassSecretStore`` uses);
* loading it applies values to the process environment ONLY — the generation
  profile's own ``.env`` stays free of privileged credentials, so the R2 profile
  guard no longer refuses to spawn;
* a symlinked file is refused;
* a file that resolves inside the generation profile home is refused;
* a missing file is not an error (systemd/shell-supplied credentials remain
  valid), and the loader reads NAMES only into its return value;
* the loader is hermetic when ``HOME`` is unset (it never reads the machine's
  real state root by accident).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core import app_env


def _write_env(path: Path, mapping) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{k}={v}\n" for k, v in mapping.items()), encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_application_env_path_is_under_the_state_root(tmp_path):
    path = app_env.application_env_path(tmp_path / "state")
    assert path == tmp_path / "state" / app_env.APPLICATION_ENV_FILENAME


def test_state_root_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSITE_BUILDER_STATE_ROOT", str(tmp_path / "custom"))
    assert app_env.resolve_state_root() == tmp_path / "custom"


def test_resolution_is_hermetic_without_home(monkeypatch):
    """No ``HOME`` => a ``~`` default is NOT silently expanded via pwd."""
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("WEBSITE_BUILDER_STATE_ROOT", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    # Falls back to the literal default path, not the machine's real home.
    assert str(app_env.resolve_state_root()).startswith("~")
    assert str(app_env.resolve_profile_home()).startswith("~")


# ---------------------------------------------------------------------------
# The profile-boundary invariant
# ---------------------------------------------------------------------------


def test_assert_refuses_an_env_file_inside_the_profile(tmp_path):
    profile = tmp_path / ".hermes-website"
    env_file = profile / "app.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("TELEGRAM_BOT_TOKEN=x\n", encoding="utf-8")

    with pytest.raises(ValueError, match="generation profile"):
        app_env.assert_application_env_outside_profile(env_file, profile)


def test_assert_refuses_an_env_file_equal_to_the_profile(tmp_path):
    profile = tmp_path / ".hermes-website"
    profile.mkdir()
    with pytest.raises(ValueError, match="generation profile"):
        app_env.assert_application_env_outside_profile(profile, profile)


def test_assert_accepts_a_sibling_state_root(tmp_path):
    profile = tmp_path / ".hermes-website"
    profile.mkdir()
    state = tmp_path / ".website-builder" / "state"
    state.mkdir(parents=True)
    app_env.assert_application_env_outside_profile(
        state / "app.env", profile
    )


def test_load_refuses_a_symlinked_env_file(tmp_path):
    profile = tmp_path / ".hermes-website"
    profile.mkdir()
    real = tmp_path / "real.env"
    real.write_text("TELEGRAM_BOT_TOKEN=x\n", encoding="utf-8")
    link = tmp_path / "state" / "app.env"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)

    with pytest.raises(ValueError, match="symbolic link"):
        app_env.load_application_env(path=link, profile_home=profile)


def test_load_refuses_a_file_inside_the_profile(tmp_path):
    profile = tmp_path / ".hermes-website"
    env_file = _write_env(profile / "app.env", {"TELEGRAM_BOT_TOKEN": "x"})
    with pytest.raises(ValueError, match="generation profile"):
        app_env.load_application_env(path=env_file, profile_home=profile)


# ---------------------------------------------------------------------------
# Loading behaviour
# ---------------------------------------------------------------------------


def test_missing_file_is_not_an_error(tmp_path):
    profile = tmp_path / ".hermes-website"
    profile.mkdir()
    assert app_env.load_application_env(
        path=tmp_path / "state" / "app.env", profile_home=profile
    ) == ()


def test_load_applies_values_and_returns_names_only(tmp_path):
    profile = tmp_path / ".hermes-website"
    profile.mkdir()
    env_file = _write_env(
        tmp_path / "state" / "app.env",
        {"TELEGRAM_BOT_TOKEN": "canary-tg", "VERCEL_TOKEN": "canary-vercel"},
    )
    target: dict = {}
    names = app_env.load_application_env(
        path=env_file, profile_home=profile, environ=target
    )

    assert names == ("TELEGRAM_BOT_TOKEN", "VERCEL_TOKEN")
    assert target["TELEGRAM_BOT_TOKEN"] == "canary-tg"
    # The RETURN value carries names, never values.
    assert "canary-tg" not in "".join(names)


def test_load_overrides_an_existing_process_value(tmp_path):
    """The application's own file is authoritative for its own credentials."""
    profile = tmp_path / ".hermes-website"
    profile.mkdir()
    env_file = _write_env(
        tmp_path / "state" / "app.env", {"TELEGRAM_BOT_TOKEN": "from-file"}
    )
    target = {"TELEGRAM_BOT_TOKEN": "stale"}
    app_env.load_application_env(path=env_file, profile_home=profile, environ=target)
    assert target["TELEGRAM_BOT_TOKEN"] == "from-file"


# ---------------------------------------------------------------------------
# The end-to-end property: a clean profile spawns, a contaminated one does not
# ---------------------------------------------------------------------------


def test_operational_split_lets_the_generation_profile_stay_clean(tmp_path):
    """The whole point of the split: the profile ``.env`` carries no privileged
    credential, so ``assert_profile_dotenv_clean`` passes and a generation child
    can be spawned — while the application still gets its operational token from
    its OWN file."""
    from app.core import credentials

    profile = tmp_path / ".hermes-website"
    profile.mkdir()
    # Generation profile holds ONLY a model credential.
    _write_env(profile / ".env", {"OPENROUTER_API_KEY": "model-canary"})
    # Application operational env holds the channel credential, OUTSIDE profile.
    op_env = _write_env(
        tmp_path / ".website-builder" / "state" / "app.env",
        {"TELEGRAM_BOT_TOKEN": "operational-canary"},
    )

    # The generation profile is clean.
    credentials.assert_profile_dotenv_clean(profile)

    # The application loads its own credential.
    target: dict = {}
    names = app_env.load_application_env(
        path=op_env, profile_home=profile, environ=target
    )
    assert "TELEGRAM_BOT_TOKEN" in names
    assert target["TELEGRAM_BOT_TOKEN"] == "operational-canary"

    # ...and the operational credential never reaches a generation role env.
    gen_env = credentials.agent_env("FRONTEND", source=target)
    assert "TELEGRAM_BOT_TOKEN" not in {k.upper() for k in gen_env}
    credentials.assert_no_privileged(gen_env)
