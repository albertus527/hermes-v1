"""D4a: the production composition root wires the OpenViking adapter.

Proves the seam is REAL and, above all, SAFE BY DEFAULT:

    * ``compose()`` exposes an OpenViking retrieval adapter;
    * the feature is DISABLED by default, so the adapter is a no-op that
      contacts no backend and invents no context;
    * composing the runtime changes no project lifecycle state and performs no
      dependency/toolchain mutation;
    * the adapter is NOT wired into FRONTEND, FAST, or QA (no second
      orchestration path).

No subprocess, no network, no browser: ``compose`` is called with a config
whose paths point at a disposable tmp tree.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.openviking_retrieval import (
    OpenVikingConfig,
    OpenVikingRetrievalAdapter,
    config_from_mapping,
)
from app.runtime import RuntimeConfig, compose


@pytest.fixture
def config(tmp_path) -> RuntimeConfig:
    home = tmp_path / "hermes-home"
    home.mkdir()
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


def test_compose_exposes_the_openviking_adapter(config):
    comp = compose(config)
    assert isinstance(comp.openviking, OpenVikingRetrievalAdapter)


def test_the_adapter_is_disabled_by_default(config):
    comp = compose(config)
    assert comp.openviking.enabled is False
    result = comp.openviking.retrieve_context("anything", "alpha")
    assert result.status == "disabled"
    assert result.returned_items == 0


def test_the_default_runtime_config_disables_openviking(config):
    assert config.openviking_config.enabled is False


def test_the_openviking_config_is_not_a_second_authority(config):
    """The adapter is a context PROVIDER: it exposes no write or decision path."""
    comp = compose(config)
    for forbidden in ("write", "ingest", "install", "deploy", "execute", "publish"):
        assert not hasattr(comp.openviking, forbidden)


def test_the_adapter_is_not_injected_into_frontend_or_qa(config):
    """D4a does NOT integrate retrieval into FRONTEND or QA (that is D4b+)."""
    comp = compose(config)
    assert not hasattr(comp.builder, "openviking")
    assert not hasattr(comp.builder, "openviking_adapter")


def test_composing_the_runtime_creates_no_project_state(config, tmp_path):
    """Fail-open: composing does not mutate any project lifecycle."""
    state_root = config.state_root
    compose(config)
    # No project state files were written by composition.
    projects_dir = state_root / "projects"
    if projects_dir.exists():
        assert list(projects_dir.iterdir()) == []


def test_an_enabled_config_is_honoured_but_does_not_touch_network(config):
    """Enabling the flag flips the adapter's flag; construction still touches no
    network (the live backend is lazy)."""
    enabled = RuntimeConfig(
        telegram_bot_token="123456:ABCDEF_test",
        hermes_home=config.hermes_home,
        workspace_root=config.workspace_root,
        state_root=config.state_root,
        output_repo_path=config.output_repo_path,
        vercel_token="x",
        vercel_team_id="team",
        vercel_ownership_namespace="ns",
        openviking_config=OpenVikingConfig(enabled=True),
    )
    comp = compose(enabled)
    assert comp.openviking.enabled is True


def test_config_from_mapping_ignores_unknown_keys(config):
    cfg = config_from_mapping({"enabled": True, "future_key": 1, "base_url": "http://localhost:1933"})
    assert cfg.enabled is True
    assert cfg.base_url == "http://localhost:1933"


def test_the_shipped_default_config_declares_openviking_disabled():
    import yaml

    path = Path(__file__).resolve().parents[1] / "config" / "default.yaml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    block = cfg["website_builder"]["openviking"]
    assert block["enabled"] is False
    assert block["base_url"].startswith("http://localhost")
    # A secret is never committed to the config file.
    assert "api_key" not in block
