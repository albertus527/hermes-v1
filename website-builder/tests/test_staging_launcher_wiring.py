"""Regression tests for the staging launcher/guard argument wiring and the
side-effect-free ``--preflight`` contract.

These are PURE tests (no root, no network, no systemd): they exercise the
scripts' argument-handling logic and the runtime entrypoint's flag dispatch,
so the two defects found during the live E2E are pinned permanently:

  * Fix B defect: ``launch-staging-sandboxed.sh`` passed the operator flag as a
    positional argument to the guard, but the guard only read the
    ``WB_E2E_INNER_ARGS`` environment variable -- so ``--preflight`` was
    silently dropped and the FULL Telegram runtime started instead.
  * Side-effect defect: a dropped flag meant ``--preflight`` actually launched
    the polling loop, created state directories, and consumed Telegram updates.
    The runtime MUST return before any composition/polling for ``--preflight``.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "staging" / "lib"
SANDBOXED = LIB / "launch-staging-sandboxed.sh"
GUARD = LIB / "guard-and-run.sh"
STAGING_HOSTS = LIB / "staging-hosts"


# ---------------------------------------------------------------------------
# Static wiring: the launcher must pass "$@" to the guard, and the guard must
# forward "$@" to the inner launcher (not only an env var).
# ---------------------------------------------------------------------------

def test_sandboxed_launcher_forwards_positional_args_to_guard():
    text = SANDBOXED.read_text(encoding="utf-8")
    # The launcher strips its own ``--detach`` control flag, then forwards the
    # remaining operator args (FORWARD[@]) to the guard. This is what makes
    # ``--preflight`` reach the runtime instead of being dropped.
    assert re.search(r'/bin/bash\s+"\$GUARD"\s+"\$\{FORWARD\[@\]\}"', text), (
        "sandboxed launcher must forward operator args to the guard")
    assert "FORWARD" in text and "--detach" in text, (
        "launcher must strip its own --detach flag before forwarding")


def test_guard_forwards_positional_args_to_inner_launcher():
    text = GUARD.read_text(encoding="utf-8")
    assert re.search(r'runuser -u wbe2e -- /bin/bash\s+"\$INNER"\s+"\$\{ARGS\[@\]\}"', text), (
        "guard must forward ARGS[@] to the inner launcher")


def test_guard_reads_args_from_argv_first_then_env_backcompat():
    text = GUARD.read_text(encoding="utf-8")
    assert 'ARGS=("$@")' in text
    assert "WB_E2E_INNER_ARGS" in text, "guard should retain env back-compat"


def test_sandboxed_launcher_bind_mounts_staging_hosts_sinkhole():
    text = SANDBOXED.read_text(encoding="utf-8")
    assert "BindReadOnlyPaths=\"$STAGING_HOSTS:/etc/hosts\"" in text
    assert "/etc/wb-e2e/staging-hosts" in text


def test_guard_has_failclosed_egress_selfcheck():
    text = GUARD.read_text(encoding="utf-8")
    # must check BOTH directions: vercel blocked AND telegram/npm reachable
    assert "api.vercel.com" in text
    assert "api.telegram.org" in text
    assert "registry.npmjs.org" in text
    assert "GUARD REFUSING" in text


def test_guard_does_not_blanket_drop_all_443():
    """The original defect: a blanket ``tcp dport 443 drop`` (no daddr)."""
    text = GUARD.read_text(encoding="utf-8")
    # Find the nft add rule line(s) and assert each 443 drop is address-scoped.
    for line in text.splitlines():
        if "tcp dport 443 drop" in line:
            assert "ip daddr" in line, (
                "any 443 drop must be scoped to an address, not blanket")


def test_guard_targets_only_vercel_mgmt_range_not_preview_hosts():
    text = GUARD.read_text(encoding="utf-8")
    assert "76.76.21.0/24" in text
    # the sinkhole file must NOT contain an ACTIVE *.vercel.app mapping (preview
    # smoke host must stay reachable). Ignore comments.
    active = [ln for ln in STAGING_HOSTS.read_text(encoding="utf-8").splitlines()
              if ln.strip() and not ln.lstrip().startswith("#")]
    assert not any("vercel.app" in ln for ln in active), (
        "preview deployment hosts must stay reachable; do not sinkhole them")


# ---------------------------------------------------------------------------
# Side-effect-free --preflight: runtime returns BEFORE compose()/polling.
# ---------------------------------------------------------------------------

def _patch_config_ok():
    return patch("app.runtime.load_runtime_config",
                 return_value=MagicMock(smoke_browser_factory=MagicMock()))


def test_preflight_returns_before_compose_and_polling():
    """--preflight must NOT compose (no dirs), NOT build, NOT poll."""
    with _patch_config_ok(), \
         patch("app.runtime.preflight_node_toolchain") as node, \
         patch("app.runtime.preflight_smoke_support") as smoke, \
         patch("app.runtime.preflight_screenshot_support") as shot, \
         patch("app.runtime.compose") as compose, \
         patch("app.runtime.TelegramReceiveLoop") as loop:
        from app.runtime import main
        rc = main(["--preflight"])
    assert rc == 0
    node.assert_called_once()
    smoke.assert_called_once()
    shot.assert_called_once()
    compose.assert_not_called()
    loop.assert_not_called()


def test_preflight_does_not_touch_telegram_transport():
    """--preflight must never construct a Telegram transport / call getUpdates."""
    with _patch_config_ok(), \
         patch("app.runtime.preflight_node_toolchain"), \
         patch("app.runtime.preflight_smoke_support"), \
         patch("app.runtime.preflight_screenshot_support"), \
         patch("app.runtime.compose") as compose, \
         patch("app.runtime.TelegramReceiveLoop") as loop:
        from app.runtime import main
        rc = main(["--preflight"])
    assert rc == 0
    compose.assert_not_called()
    loop.assert_not_called()


def test_unknown_flag_never_falls_through_to_polling():
    """A typo'd flag must be refused before config load / polling."""
    with patch("app.runtime.TelegramReceiveLoop") as loop, \
         patch("app.runtime.compose") as compose:
        from app.runtime import main
        rc = main(["--preflgiht"])  # deliberate typo
    assert rc == 2
    compose.assert_not_called()
    loop.assert_not_called()


def test_preflight_flag_is_known():
    from app.runtime import _KNOWN_FLAGS
    assert "--preflight" in _KNOWN_FLAGS


# ---------------------------------------------------------------------------
# QA screenshot preflight gate (agent-browser) — pins the first-live-E2E defect.
# A runtime that passes the Playwright smoke check but lacks agent-browser must
# FAIL CLOSED at startup, not after a full FRONTEND build.
# ---------------------------------------------------------------------------

def test_screenshot_preflight_missing_cli_raises():
    with patch("shutil.which", return_value=None):
        from app.runtime import preflight_screenshot_support
        with pytest.raises(RuntimeError, match="agent-browser"):
            preflight_screenshot_support()


def test_screenshot_preflight_nonzero_exit_raises():
    fake = MagicMock(returncode=1, stdout="", stderr="boom")
    with patch("shutil.which", return_value="/usr/bin/agent-browser"), \
         patch("subprocess.run", return_value=fake):
        from app.runtime import preflight_screenshot_support
        with pytest.raises(RuntimeError):
            preflight_screenshot_support()


def test_screenshot_preflight_empty_version_raises():
    fake = MagicMock(returncode=0, stdout="   ", stderr="")
    with patch("shutil.which", return_value="/usr/bin/agent-browser"), \
         patch("subprocess.run", return_value=fake):
        from app.runtime import preflight_screenshot_support
        with pytest.raises(RuntimeError):
            preflight_screenshot_support()


def test_screenshot_preflight_ok():
    fake = MagicMock(returncode=0, stdout="agent-browser 0.37.1\n", stderr="")
    with patch("shutil.which", return_value="/usr/bin/agent-browser"), \
         patch("subprocess.run", return_value=fake) as run:
        from app.runtime import preflight_screenshot_support
        preflight_screenshot_support()  # must not raise
    # The CLI is invoked with --version only; no browser launch, no network.
    argv = run.call_args[0][0]
    assert argv[0] == "/usr/bin/agent-browser"
    assert argv[1] == "--version"


def test_main_fails_closed_when_screenshot_preflight_raises():
    """If agent-browser is missing, main() returns 1 BEFORE polling/compose."""
    with _patch_config_ok(), \
         patch("app.runtime.preflight_node_toolchain"), \
         patch("app.runtime.preflight_smoke_support"), \
         patch("app.runtime.preflight_screenshot_support",
               side_effect=RuntimeError("agent-browser missing")), \
         patch("app.runtime.compose") as compose, \
         patch("app.runtime.TelegramReceiveLoop") as loop:
        from app.runtime import main
        rc = main([])
    assert rc == 1
    compose.assert_not_called()
    loop.assert_not_called()


# ---------------------------------------------------------------------------
# Launcher wiring: AGENT_BROWSER_ARGS must be exported for the capture path.
# ---------------------------------------------------------------------------

LAUNCH_INNER = LIB / "launch-staging.sh"


def test_launcher_exports_agent_browser_args():
    text = LAUNCH_INNER.read_text(encoding="utf-8")
    assert "AGENT_BROWSER_ARGS" in text
    assert "--no-sandbox" in text


def test_launcher_node_bin_precedes_system_path():
    """agent-browser must resolve from the STAGING node prefix, not system."""
    text = LAUNCH_INNER.read_text(encoding="utf-8")
    assert 'export PATH="$NODE_BIN:$VENV/bin:/usr/bin:/bin"' in text


# ---------------------------------------------------------------------------
# Root-owned artifact deployment parity (the repo copy must be installable).
# ---------------------------------------------------------------------------

def test_lib_files_exist_and_are_scripts():
    assert SANDBOXED.is_file()
    assert GUARD.is_file()
    assert STAGING_HOSTS.is_file()
    assert LAUNCH_INNER.is_file()
    assert SANDBOXED.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
    assert GUARD.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
    assert LAUNCH_INNER.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
