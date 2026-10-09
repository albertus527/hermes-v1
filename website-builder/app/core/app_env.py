"""Application-owned OPERATIONAL environment for Website Builder.

WHY THIS EXISTS
---------------
The Website Builder ORCHESTRATION layer needs a small set of OPERATIONAL
credentials that are NOT model credentials and must never reach a generation
child:

* ``TELEGRAM_BOT_TOKEN`` — the bot the receive loop speaks through;
* ``VERCEL_TOKEN`` / ``VERCEL_TEAM_ID`` — the deployment adapter's credentials.

The generation profile is a SEPARATE trust boundary. The ``hermes -z`` child is
launched with ``HERMES_HOME`` pointing at that profile, and it re-runs
``load_hermes_dotenv()``, which loads ``$HERMES_HOME/.env`` unfiltered with
``override=True`` INSIDE the child — after the parent's environment scoping. A
privileged credential written there lands in a process that holds file and
terminal tools under ``HERMES_YOLO_MODE=1``. R2's
:func:`app.core.credentials.assert_profile_dotenv_clean` therefore refuses to
spawn against a profile whose ``.env`` declares one.

That guard is correct. The fix is to stop putting operational credentials in
the generation profile's ``.env`` at all, and give the APPLICATION its own,
separate source for them. This module is that source.

PLACEMENT IS A SECURITY BOUNDARY
--------------------------------
The operational env file lives under the application STATE ROOT
(``~/.website-builder/state/app.env`` by default), exactly like
:class:`app.core.secrets.BypassSecretStore` — NEVER under a Hermes profile home.
The generation child is pointed at the profile home; a file there is readable
no matter how the process environment was scoped. A file under the state root
is outside that boundary, which is the same invariant the bypass store relies
on.

VALUES NEVER LEAVE THE PROCESS ENVIRONMENT
------------------------------------------
Loaded values go into the application process environment only (the same place
the runtime already reads ``TELEGRAM_BOT_TOKEN`` from). Nothing here is logged
with a value: :func:`load_application_env` returns variable NAMES only, and the
path guard reports paths, never contents.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, MutableMapping, Optional, Tuple

logger = logging.getLogger(__name__)

#: On-disk name of the application operational env file, joined to the state
#: root wherever a path is computed. One name, so the production wiring and the
#: guard that keeps it out of the profile cannot drift apart.
APPLICATION_ENV_FILENAME = "app.env"

#: The default state root, mirroring ``config/default.yaml``
#: (``website_builder.state_root``) and ``app.runtime.load_runtime_config``.
DEFAULT_STATE_ROOT = "~/.website-builder/state"

#: The default generation profile home, mirroring ``config/default.yaml``
#: (``website_builder.hermes_home``) and ``app.runtime.load_runtime_config``.
DEFAULT_HERMES_HOME = "~/.hermes-website"


def _expand_user_path(value: str) -> Optional[Path]:
    """Expand a ``~`` path ONLY when ``$HOME`` is set.

    ``Path.expanduser`` silently falls back to ``pwd`` when ``HOME`` is unset,
    which would let an environment with no ``HOME`` (a hermetic test, a bare
    container) resolve the machine's real state root and pick up an operational
    env file it was never handed. Treating a missing ``HOME`` as "no path" keeps
    the loader hermetic: it only ever reads the file the environment actually
    names.
    """
    text = value.strip()
    if not text:
        return None
    if text.startswith("~") and not os.environ.get("HOME"):
        return None
    return Path(text).expanduser()


def resolve_state_root(config_path: Optional[Path] = None) -> Path:
    """Resolve the application state root with the runtime's own precedence.

    ``WEBSITE_BUILDER_STATE_ROOT`` env override, then ``config.yaml``'s
    ``website_builder.state_root``, then :data:`DEFAULT_STATE_ROOT`. This mirrors
    :func:`app.runtime.load_runtime_config` so the loader and the runtime never
    disagree about where application state lives. Non-secret path only.
    """
    value = os.environ.get("WEBSITE_BUILDER_STATE_ROOT", "").strip()
    if value:
        resolved = _expand_user_path(value)
        if resolved is not None:
            return resolved
    try:
        import yaml

        path = Path(config_path) if config_path else (
            Path(__file__).resolve().parent.parent / "config" / "default.yaml"
        )
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                cfg = yaml.safe_load(handle) or {}
            configured = (cfg.get("website_builder") or {}).get("state_root")
            if isinstance(configured, str) and configured.strip():
                resolved = _expand_user_path(configured)
                if resolved is not None:
                    return resolved
    except Exception:  # pragma: no cover - malformed config falls back
        pass
    default = _expand_user_path(DEFAULT_STATE_ROOT)
    return default if default is not None else Path(DEFAULT_STATE_ROOT)


def resolve_profile_home(config_path: Optional[Path] = None) -> Path:
    """Resolve the generation profile home with the runtime's precedence.

    ``HERMES_HOME`` env override, then ``config.yaml``'s
    ``website_builder.hermes_home``, then :data:`DEFAULT_HERMES_HOME`. Used ONLY
    to prove the operational env file is not inside the generation profile.
    """
    value = os.environ.get("HERMES_HOME", "").strip()
    if value:
        resolved = _expand_user_path(value)
        if resolved is not None:
            return resolved
    try:
        import yaml

        path = Path(config_path) if config_path else (
            Path(__file__).resolve().parent.parent / "config" / "default.yaml"
        )
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                cfg = yaml.safe_load(handle) or {}
            configured = (cfg.get("website_builder") or {}).get("hermes_home")
            if isinstance(configured, str) and configured.strip():
                resolved = _expand_user_path(configured)
                if resolved is not None:
                    return resolved
    except Exception:  # pragma: no cover - malformed config falls back
        pass
    default = _expand_user_path(DEFAULT_HERMES_HOME)
    return default if default is not None else Path(DEFAULT_HERMES_HOME)


def application_env_path(state_root: Optional[Path] = None) -> Path:
    """The application operational env file for *state_root*."""
    root = Path(state_root).expanduser() if state_root else resolve_state_root()
    return root / APPLICATION_ENV_FILENAME


def _parse_env_file(path: Path) -> Dict[str, str]:
    """Parse a ``KEY=VALUE`` file into a dict. Values are never logged.

    Reuses Hermes' canonical dotenv parser (quote/escape semantics identical to
    every other reader) when it is importable, so a credential containing ``"``
    or ``\\`` parses correctly. Falls back to a minimal parser only if the
    Hermes tree is unavailable.
    """
    try:
        from agent.secret_scope import load_env_file

        return dict(load_env_file(path))
    except Exception:  # pragma: no cover - Hermes tree unavailable
        pass

    secrets: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return secrets
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        secrets[key] = value
    return secrets


def assert_application_env_outside_profile(
    env_path: Path, profile_home: Path
) -> None:
    """Fail closed if the operational env file is inside the generation profile.

    The invariant is the one :class:`BypassSecretStore` relies on: a credential
    under ``HERMES_HOME`` is readable by the generation agent, so the
    application's operational env must live outside it. Both sides are resolved
    first so a symlinked path cannot smuggle the file back under the profile.
    """
    env_resolved = Path(env_path).expanduser().resolve()
    home_resolved = Path(profile_home).expanduser().resolve()
    if env_resolved == home_resolved or env_resolved.is_relative_to(home_resolved):
        raise ValueError(
            f"The Website Builder operational env file {env_resolved} is the "
            f"generation profile home {home_resolved} or lives inside it. A "
            "credential there is readable by the generation agent, which is "
            "exactly what the profile guard exists to prevent. Keep it under "
            "the application state root instead."
        )


def load_application_env(
    *,
    path: Optional[Path] = None,
    state_root: Optional[Path] = None,
    profile_home: Optional[Path] = None,
    environ: Optional[MutableMapping[str, str]] = None,
) -> Tuple[str, ...]:
    """Load the application's operational env into the process environment.

    Resolution: an explicit *path*, else *state_root* / ``app.env``, else the
    resolved default state root / ``app.env``.

    Behaviour:

    * A missing file is not an error — a deployment that supplies its
      operational credentials via systemd ``Environment=`` or a shell export
      legitimately has no file. Returns ``()``.
    * A symlinked file is refused (it could point anywhere).
    * The file must resolve OUTSIDE the generation profile home
      (:func:`assert_application_env_outside_profile`), so this loader can never
      become a second way to put a privileged credential into the profile.
    * Values are applied with ``override=True`` into *environ* (the process
      environment by default): the application's own operational file is
      authoritative for the application's own operational credentials.

    Returns the sorted variable NAMES loaded. Values are never returned, logged,
    or serialised.
    """
    env_file = Path(path) if path else application_env_path(state_root)
    env_file = env_file.expanduser()

    if not env_file.is_file():
        return ()
    if env_file.is_symlink():
        raise ValueError(
            f"The Website Builder operational env file is a symbolic link: "
            f"{env_file}. Refusing to load through a link."
        )

    home = Path(profile_home) if profile_home else resolve_profile_home()
    assert_application_env_outside_profile(env_file, home)

    secrets = _parse_env_file(env_file)
    target = os.environ if environ is None else environ
    for name, value in secrets.items():
        target[name] = value
    return tuple(sorted(secrets))


__all__ = [
    "APPLICATION_ENV_FILENAME",
    "DEFAULT_HERMES_HOME",
    "DEFAULT_STATE_ROOT",
    "application_env_path",
    "assert_application_env_outside_profile",
    "load_application_env",
    "resolve_profile_home",
    "resolve_state_root",
]
