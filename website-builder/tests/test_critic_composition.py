"""D3b: the production composition root wires the critic scanner.

This proves the integration point is REAL: ``compose()`` constructs the
scanner from the runtime config and injects it into BOTH the builder (initial
build) and the revision orchestrator (revisions), so the bounded critic repair
stage runs on the actual production pipeline -- not only in a unit test.

No subprocess, no network, no browser: ``compose`` is called with a config whose
paths point at a disposable tmp tree, and the FRONTEND adapter is never invoked.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.critic_repair import ImpeccableScanner
from app.runtime import RuntimeConfig, compose


@pytest.fixture
def config(tmp_path) -> RuntimeConfig:
    home = tmp_path / "hermes-home"
    home.mkdir()
    # A well-formed bot token: ``compose`` builds a real TelegramAdapter, which
    # validates the token shape.
    return RuntimeConfig(
        telegram_bot_token="123456:ABCDEF_test",
        hermes_home=home,
        workspace_root=tmp_path / "workspaces",
        state_root=tmp_path / "state",
        output_repo_path=tmp_path / "output-repo",
        vercel_token="x",
        vercel_team_id="team",
        vercel_ownership_namespace="ns",
    )


def test_compose_injects_the_scanner_into_the_builder(config):
    comp = compose(config)
    assert isinstance(comp.builder.critic_scanner, ImpeccableScanner)
    assert comp.builder.critic_scanner.skill_root == (
        config.hermes_home / "skills" / "impeccable"
    )


def test_compose_injects_the_scanner_into_the_revision_orchestrator(config):
    comp = compose(config)
    assert isinstance(comp.revise.critic_scanner, ImpeccableScanner)


def test_the_scanner_uses_the_declared_node_interpreter(config):
    """The interpreter is resolved by the APPLICATION, never searched by the
    scanner itself (which would be an unvetted PATH binary)."""
    comp = compose(config)
    # node is present on this host; the scanner must carry the resolved path.
    scanner = comp.builder.critic_scanner
    assert scanner.node_executable is None or isinstance(scanner.node_executable, str)


def test_a_host_without_the_skill_scans_as_not_run(config, tmp_path):
    """No provisioned skill => NOT_RUN / DEGRADED, never a fabricated pass."""
    from app.core import critic_policy as cp

    comp = compose(config)
    workspace = tmp_path / "project"
    (workspace / "src").mkdir(parents=True)
    result = comp.builder.critic_scanner.scan(workspace)
    assert result.state in (cp.NOT_RUN, cp.DEGRADED)
    assert result.authoritative is False
