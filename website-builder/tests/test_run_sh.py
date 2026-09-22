"""H-9: ``website-builder/run.sh`` interpreter selection determinism.

These tests exercise the REAL script through bash with a PATH shim that
replaces the interpreters (and ``cd``/``nvm``) with recording stubs. No
network, no pip, no real venv creation, and no real app launch occur.

Scenarios:
  A. website-builder/.venv/bin/python exists and is executable -> chosen
  B. no .venv, a system interpreter satisfies the dependency probe -> chosen
  C. no usable interpreter -> non-zero exit with a clear message
  D. invoked from outside website-builder cwd -> correct project path/venv
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

WEBSITE_BUILDER = Path(__file__).resolve().parents[1]
RUN_SH = WEBSITE_BUILDER / "run.sh"

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required for run.sh tests")


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _make_shim_dir(shim_dir: Path, probe_log: Path, *, probe_result: str,
                   record_exec: str | None = None) -> None:
    """Create stub interpreters recording probe + launch invocations.

    ``probe_result`` is the exit code of the stdlib dependency probe
    (``python -c "import playwright.sync_api, yaml"``) for the fallback
    interpreters. The venv interpreter always "succeeds" so that scenario A is
    not conflated with a broken venv.
    """
    # Bare-name fallback interpreters: succeed on -m app (recorded), honour the
    # configured probe result on -c.
    for name in ("python3", "python"):
        _write_executable(
            shim_dir / name,
            "#!/usr/bin/env bash\n"
            "if [ \"$1\" = \"-c\" ]; then\n"
            f"  echo \"{name} -c \\\"$2\\\"\" >> '{probe_log}'\n"
            f"  exit {probe_result}\n"
            "fi\n"
            f"  echo \"{name} $*\" >> '{record_exec}'\n"
            "exit 0\n",
        )


def _run_run_sh(shim_dir: Path, cwd: Path, env_overrides: dict | None = None):
    env = dict(os.environ)
    # Only the shims + minimal toolchain are visible: guarantees the discovery
    # fallback loop sees our stub python3/python and nothing else.
    env["PATH"] = f"{shim_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"
    env.pop("WB_PYTHON", None)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [BASH, str(RUN_SH), "--selector-test"],
        cwd=str(cwd), env=env, capture_output=True, text=True,
    )


def _install_venv_python(probe_log: Path, record_exec: Path) -> Path:
    venv_python = WEBSITE_BUILDER / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    _write_executable(
        venv_python,
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"-c\" ]; then\n"
        f"  echo \".venv-python -c \\\"$2\\\"\" >> '{probe_log}'\n"
        "  exit 0\n"
        "fi\n"
        f"  echo \".venv-python $*\" >> '{record_exec}'\n"
        "exit 0\n",
    )
    return venv_python


@pytest.fixture
def venv_cleanup():
    """Ensure a real project .venv (if any) is restored after a test."""
    venv_dir = WEBSITE_BUILDER / ".venv"
    existed = venv_dir.exists()
    yield
    # Remove only the stub we created; never touch a pre-existing real venv.
    if not existed and venv_dir.exists():
        shutil.rmtree(venv_dir, ignore_errors=True)


def test_run_sh_prefers_project_venv(tmp_path, venv_cleanup):
    """A: .venv/bin/python exists and is executable -> run.sh chooses it."""
    shim = tmp_path / "bin"
    shim.mkdir()
    probe_log = tmp_path / "probe.log"
    record_exec = tmp_path / "exec.log"
    probe_log.write_text("")
    record_exec.write_text("")
    venv_python = _install_venv_python(probe_log, record_exec)
    # Fallback interpreters exist but must NOT be selected.
    _make_shim_dir(shim, probe_log, probe_result="1", record_exec=str(record_exec))

    result = _run_run_sh(shim, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    execs = record_exec.read_text()
    assert ".venv-python -m app --selector-test" in execs
    assert "python3 -m app" not in execs
    assert not any(line.startswith("python -m app") for line in execs.splitlines())
    assert str(venv_python)  # sanity: the path we expect to be chosen


def test_run_sh_falls_back_to_usable_system_interpreter(tmp_path, venv_cleanup):
    """B: no .venv, system interpreter satisfies the dependency probe."""
    shim = tmp_path / "bin"
    shim.mkdir()
    probe_log = tmp_path / "probe.log"
    record_exec = tmp_path / "exec.log"
    probe_log.write_text("")
    record_exec.write_text("")
    _make_shim_dir(shim, probe_log, probe_result="0", record_exec=str(record_exec))

    result = _run_run_sh(shim, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    execs = record_exec.read_text()
    assert "python3 -m app --selector-test" in execs
    # The probe was actually used to decide.
    assert "import playwright.sync_api, yaml" in probe_log.read_text()


def test_run_sh_fails_closed_without_usable_interpreter(tmp_path, venv_cleanup):
    """C: no usable interpreter -> non-zero exit with a clear message."""
    shim = tmp_path / "bin"
    shim.mkdir()
    probe_log = tmp_path / "probe.log"
    record_exec = tmp_path / "exec.log"
    probe_log.write_text("")
    record_exec.write_text("")
    # Both fallbacks fail the dependency probe.
    _make_shim_dir(shim, probe_log, probe_result="1", record_exec=str(record_exec))

    result = _run_run_sh(shim, cwd=tmp_path)

    assert result.returncode != 0
    assert "no usable Python interpreter" in result.stderr
    assert "playwright.sync_api" in result.stderr
    # It must never have launched the app.
    assert "-m app" not in record_exec.read_text()


def test_run_sh_resolves_paths_when_invoked_outside_project(tmp_path, venv_cleanup):
    """D: invoked from an arbitrary cwd -> still resolves the project venv."""
    shim = tmp_path / "bin"
    shim.mkdir()
    probe_log = tmp_path / "probe.log"
    record_exec = tmp_path / "exec.log"
    probe_log.write_text("")
    record_exec.write_text("")
    _install_venv_python(probe_log, record_exec)
    _make_shim_dir(shim, probe_log, probe_result="1", record_exec=str(record_exec))

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    result = _run_run_sh(shim, cwd=outside)

    assert result.returncode == 0, result.stderr
    execs = record_exec.read_text()
    # The project .venv was discovered from an unrelated cwd -> SCRIPT_DIR
    # resolution is correct and independent of the caller's cwd.
    assert ".venv-python -m app --selector-test" in execs
    assert str(WEBSITE_BUILDER) not in str(outside)


def test_run_sh_prefers_wb_python_env_override(tmp_path, venv_cleanup):
    """Extra: WB_PYTHON is honoured before the PATH-name fallbacks."""
    shim = tmp_path / "bin"
    shim.mkdir()
    probe_log = tmp_path / "probe.log"
    record_exec = tmp_path / "exec.log"
    probe_log.write_text("")
    record_exec.write_text("")
    custom = tmp_path / "custom-python"
    _write_executable(
        custom,
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"-c\" ]; then\n"
        f"  echo \"custom -c \\\"$2\\\"\" >> '{probe_log}'\n"
        "  exit 0\n"
        "fi\n"
        f"  echo \"custom $*\" >> '{record_exec}'\n"
        "exit 0\n",
    )
    _make_shim_dir(shim, probe_log, probe_result="0", record_exec=str(record_exec))

    result = _run_run_sh(shim, cwd=tmp_path, env_overrides={"WB_PYTHON": str(custom)})

    assert result.returncode == 0, result.stderr
    execs = record_exec.read_text()
    assert "custom -m app --selector-test" in execs
    assert "python3 -m app" not in execs
