"""Regression tests for deployment-local Website Builder config selection."""

from pathlib import Path

import yaml

from app import runtime


def test_default_config_remains_disabled():
    path = Path(__file__).resolve().parents[1] / "config/default.yaml"
    cfg = yaml.safe_load(path.read_text())
    wb = cfg["website_builder"]

    assert wb["openviking"]["enabled"] is False
    assert wb["laya"]["enabled"] is False
    assert wb["laya"]["multilingual_expansion"] is False
    assert wb["laya"]["fast_context_injection"] is False


def test_missing_override_fails_before_credentials(monkeypatch, tmp_path):
    missing = tmp_path / "nonexistent.yaml"

    monkeypatch.setenv("WEBSITE_BUILDER_CONFIG_PATH", str(missing))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)
    monkeypatch.delenv("VERCEL_TEAM_ID", raising=False)

    assert runtime.main(["--preflight"]) == 1


def test_malformed_override_fails_before_credentials(monkeypatch, tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("website_builder: [unclosed\n")

    monkeypatch.setenv("WEBSITE_BUILDER_CONFIG_PATH", str(path))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)
    monkeypatch.delenv("VERCEL_TEAM_ID", raising=False)

    assert runtime.main(["--preflight"]) == 1


def test_invalid_structure_fails_before_credentials(monkeypatch, tmp_path):
    path = tmp_path / "invalid.yaml"
    path.write_text("website_builder: []\n")

    monkeypatch.setenv("WEBSITE_BUILDER_CONFIG_PATH", str(path))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)
    monkeypatch.delenv("VERCEL_TEAM_ID", raising=False)

    assert runtime.main(["--preflight"]) == 1
