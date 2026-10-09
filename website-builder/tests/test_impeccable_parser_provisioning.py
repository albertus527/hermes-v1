"""Regression tests for the application-owned Impeccable parser provisioner.

These pin the load-bearing properties of
``app.core.design_install.provision_impeccable_parser_runtime`` and its helpers:

* the install argv carries ONLY the exact reviewed pins (no caller/model input);
* nothing is written into a skill root except the runtime ``node_modules``
  (no ``package.json``, no lockfile);
* the runtime directory may not be a symlink or escape the skill root;
* provisioning refuses a skill root with no verified engine, and never invents
  one;
* an install whose exit code is 0 but which leaves the WRONG version on disk is
  NOT reported as provisioned (npm's exit code is never trusted);
* an already-correct runtime is a no-op (deterministic, no command run).

The npm invocation itself is always faked here: the offline suite never touches
the network. A separate, explicitly-run VPS step exercises the real npm.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core import design_install as di


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine_skill(root: Path) -> Path:
    """Create a minimal skill root with the verified engine entrypoints."""
    from app.core.design_activation import engine_relative_paths

    (root / "SKILL.md").parent.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text("---\nname: impeccable\n---\n", encoding="utf-8")
    for relative in engine_relative_paths():
        engine = root / relative
        engine.parent.mkdir(parents=True, exist_ok=True)
        engine.write_text("// node entrypoint\n", encoding="utf-8")
    return root


def _install_fake_runtime(root: Path, versions=None) -> None:
    """Create ``node_modules/<pkg>/package.json`` for the four parser packages."""
    versions = versions or di.IMPECCABLE_PARSER_PACKAGE_PINS
    modules = root / di.IMPECCABLE_PARSER_RUNTIME_DIRNAME
    for package, version in versions.items():
        manifest = modules / package / "package.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"name": package, "version": version}), encoding="utf-8")


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.args = []


# ---------------------------------------------------------------------------
# The install argv is application-owned and exact
# ---------------------------------------------------------------------------


def test_install_argv_uses_only_the_reviewed_exact_pins(tmp_path):
    skill = _make_engine_skill(tmp_path / "impeccable")
    argv = di.build_impeccable_parser_install_argv(skill, npm_executable="/usr/bin/npm")

    assert argv is not None
    assert argv[0] == "/usr/bin/npm"
    specs = [a for a in argv if "@" in a and a.split("@")[0] in di.IMPECCABLE_PARSER_PACKAGE_PINS]
    assert set(specs) == {
        f"{p}@{v}" for p, v in di.IMPECCABLE_PARSER_PACKAGE_PINS.items()
    }
    # The exact pins are the ONLY versioned arguments.
    for package, version in di.IMPECCABLE_PARSER_PACKAGE_PINS.items():
        assert f"{package}@{version}" in argv


def test_install_argv_targets_the_skill_root(tmp_path):
    skill = _make_engine_skill(tmp_path / "impeccable")
    argv = di.build_impeccable_parser_install_argv(skill, npm_executable="/usr/bin/npm")
    idx = argv.index("--prefix")
    assert argv[idx + 1] == str(skill)


def test_install_argv_refuses_ranges_and_never_writes_manifests(tmp_path):
    skill = _make_engine_skill(tmp_path / "impeccable")
    argv = di.build_impeccable_parser_install_argv(skill, npm_executable="/usr/bin/npm")
    # --no-save + --package-lock=false guarantee no package.json/lockfile.
    assert "--no-save" in argv
    assert "--package-lock=false" in argv
    assert "--ignore-scripts" in argv
    # No range/caret/tilde spec ever appears.
    assert not any(("^" in a or "~" in a) for a in argv)


def test_install_argv_is_none_without_npm(tmp_path, monkeypatch):
    skill = _make_engine_skill(tmp_path / "impeccable")
    monkeypatch.setattr(di.shutil, "which", lambda name: None)
    assert di.build_impeccable_parser_install_argv(skill) is None


# ---------------------------------------------------------------------------
# Provisioning behaviour
# ---------------------------------------------------------------------------


def test_provision_refuses_a_missing_skill_root(tmp_path):
    result = di.provision_impeccable_parser_runtime(tmp_path / "nope")
    assert result.ok is False
    assert result.reason == di.REASON_PARSER_SKILL_ROOT_MISSING


def test_provision_refuses_a_skill_with_no_engine(tmp_path):
    root = tmp_path / "impeccable"
    (root / "SKILL.md").parent.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text("x", encoding="utf-8")
    result = di.provision_impeccable_parser_runtime(root)
    assert result.ok is False
    assert result.reason == di.REASON_PARSER_ENGINE_MISSING


def test_provision_refuses_a_symlinked_runtime_dir(tmp_path):
    skill = _make_engine_skill(tmp_path / "impeccable")
    outside = tmp_path / "outside"
    outside.mkdir()
    (skill / "node_modules").symlink_to(outside, target_is_directory=True)

    result = di.provision_impeccable_parser_runtime(skill)
    assert result.ok is False
    assert result.reason == di.REASON_PARSER_RUNTIME_DIR_UNSAFE


def test_provision_is_a_noop_when_already_correct(tmp_path, monkeypatch):
    skill = _make_engine_skill(tmp_path / "impeccable")
    _install_fake_runtime(skill)

    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("must not run npm when the runtime is already correct")

    monkeypatch.setattr(di.subprocess, "run", _boom)
    result = di.provision_impeccable_parser_runtime(skill)
    assert result.ok is True
    assert result.reason == di.REASON_PARSER_ALREADY_PROVISIONED
    assert called["n"] == 0


def test_provision_runs_npm_and_verifies_exact_versions(tmp_path, monkeypatch):
    skill = _make_engine_skill(tmp_path / "impeccable")
    captured = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs.get("cwd")
        captured["shell"] = kwargs.get("shell")
        # The install "succeeds" and materialises the exact pins.
        _install_fake_runtime(skill)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(di.subprocess, "run", _fake_run)
    result = di.provision_impeccable_parser_runtime(skill, npm_executable="/usr/bin/npm")

    assert result.ok is True
    assert result.reason == di.REASON_PARSER_PROVISIONED
    assert captured["shell"] is False
    assert captured["cwd"] == str(skill)
    assert di.parser_runtime_matches_pins(skill)


def test_provision_fails_when_npm_exit_is_zero_but_versions_are_wrong(tmp_path, monkeypatch):
    skill = _make_engine_skill(tmp_path / "impeccable")

    def _fake_run(argv, **kwargs):
        # Exit 0 but leave the WRONG version on disk.
        _install_fake_runtime(skill, versions={
            "css-select": "1.0.0", "css-tree": "3.2.1",
            "domutils": "4.0.2", "htmlparser2": "12.0.0",
        })
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(di.subprocess, "run", _fake_run)
    result = di.provision_impeccable_parser_runtime(skill, npm_executable="/usr/bin/npm")
    assert result.ok is False
    assert result.reason == di.REASON_PARSER_VERSION_MISMATCH


def test_provision_fails_on_nonzero_exit(tmp_path, monkeypatch):
    skill = _make_engine_skill(tmp_path / "impeccable")
    monkeypatch.setattr(
        di.subprocess, "run", lambda *a, **k: _FakeCompleted(returncode=1, stderr="boom")
    )
    result = di.provision_impeccable_parser_runtime(skill, npm_executable="/usr/bin/npm")
    assert result.ok is False
    assert result.reason == di.REASON_PARSER_INSTALL_FAILED
    assert result.returncode == 1


def test_provision_command_runs_with_a_clean_shell_env(tmp_path, monkeypatch):
    """The npm invocation never inherits a model or deploy credential."""
    skill = _make_engine_skill(tmp_path / "impeccable")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "CANARY-TELEGRAM")
    monkeypatch.setenv("VERCEL_TOKEN", "CANARY-VERCEL")
    monkeypatch.setenv("OPENROUTER_API_KEY", "CANARY-MODEL")
    captured = {}

    def _fake_run(argv, **kwargs):
        captured["env"] = kwargs.get("env")
        _install_fake_runtime(skill)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(di.subprocess, "run", _fake_run)
    di.provision_impeccable_parser_runtime(skill, npm_executable="/usr/bin/npm")

    env = captured["env"]
    assert env is not None
    joined = "".join(env.values())
    for canary in ("CANARY-TELEGRAM", "CANARY-VERCEL", "CANARY-MODEL"):
        assert canary not in joined


def test_provision_never_creates_a_manifest_in_the_skill_root(tmp_path, monkeypatch):
    skill = _make_engine_skill(tmp_path / "impeccable")

    def _fake_run(argv, **kwargs):
        _install_fake_runtime(skill)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(di.subprocess, "run", _fake_run)
    di.provision_impeccable_parser_runtime(skill, npm_executable="/usr/bin/npm")

    assert not (skill / "package.json").exists()
    assert not (skill / "package-lock.json").exists()


def test_parser_pins_are_exact_and_match_the_activation_list():
    from app.core.design_activation import PARSER_RUNTIME_PACKAGES

    assert set(di.IMPECCABLE_PARSER_PACKAGE_PINS) == set(PARSER_RUNTIME_PACKAGES)
    for package, version in di.IMPECCABLE_PARSER_PACKAGE_PINS.items():
        assert di.PackageSpec(package, version).is_exact(), (package, version)
