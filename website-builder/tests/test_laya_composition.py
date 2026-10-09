"""D4b: the production composition root wires Laya at the FAST intake seam.

Proves the seam is REAL and SAFE BY DEFAULT:

    * ``compose()`` injects a Laya preparer into the intake processor;
    * Laya is DISABLED by default, so it is a no-op that contacts no backend;
    * even with Laya enabled, no retrieval happens unless OpenViking is enabled;
    * the preparer is a CONTEXT PROVIDER: it exposes no write/decision path and
      is NOT injected into FRONTEND or QA (no second orchestration path);
    * composing the runtime mutates no project lifecycle state.

No subprocess, no network, no browser.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.laya_context import LayaConfig, LayaContextPreparer
from app.core.openviking_retrieval import OpenVikingConfig
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


def test_compose_injects_the_laya_preparer(config):
    comp = compose(config)
    assert isinstance(comp.intake.laya, LayaContextPreparer)


def test_laya_is_disabled_by_default(config):
    comp = compose(config)
    assert comp.intake.laya.enabled is False
    result = comp.intake.laya.prepare_context("minimalist typography", "p1")
    assert result.status == "skipped"
    assert result.items == ()


def test_the_default_runtime_config_disables_laya(config):
    assert config.laya_config.enabled is False


def test_laya_enabled_alone_still_skips_without_openviking(config, tmp_path):
    """Enabling Laya without OpenViking must NOT contact a backend."""
    enabled = RuntimeConfig(
        telegram_bot_token="123456:ABCDEF_test",
        hermes_home=config.hermes_home,
        workspace_root=config.workspace_root,
        state_root=config.state_root,
        output_repo_path=config.output_repo_path,
        vercel_token="x",
        vercel_team_id="team",
        vercel_ownership_namespace="ns",
        openviking_config=OpenVikingConfig(enabled=False),
        laya_config=LayaConfig(enabled=True),
    )
    comp = compose(enabled)
    assert comp.intake.laya.enabled is False
    result = comp.intake.laya.prepare_context("minimalist typography", "p1")
    assert result.status == "skipped"


def test_the_laya_preparer_is_not_a_second_authority(config):
    comp = compose(config)
    for forbidden in ("write", "ingest", "install", "deploy", "execute",
                      "publish", "save"):
        assert not hasattr(comp.intake.laya, forbidden)


def test_the_laya_preparer_is_not_injected_into_frontend_or_qa(config):
    comp = compose(config)
    assert not hasattr(comp.builder, "laya")
    assert not hasattr(comp.builder, "laya_preparer")


def test_composing_with_laya_creates_no_project_state(config):
    compose(config)
    projects_dir = config.state_root / "projects"
    if projects_dir.exists():
        assert list(projects_dir.iterdir()) == []


def test_the_shipped_default_config_declares_laya_disabled():
    import yaml

    path = Path(__file__).resolve().parents[1] / "config" / "default.yaml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    block = cfg["website_builder"]["laya"]
    assert block["enabled"] is False
    assert "api_key" not in block
