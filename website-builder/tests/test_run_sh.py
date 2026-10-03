"""H-9: ``website-builder/run.sh`` interpreter selection determinism.

These tests exercise the REAL script through bash with a PATH shim that
replaces the interpreters (and ``cd``/``nvm``) with recording stubs. No
network, no pip, no real venv creation, and no real app launch occur.

Sandboxing
----------
The script under test is a byte-identical copy of the production ``run.sh``
placed in a temporary project root under ``tmp_path``. ``run.sh`` derives
``SCRIPT_DIR`` from ``BASH_SOURCE``, so interpreter selection is a pure
function of the script's own location plus ``PATH``/``WB_PYTHON`` -- a temp
copy therefore exercises exactly the same selection code.

Nothing is written inside the real project directory. The developer-owned
``website-builder/.venv`` is never created, renamed, overwritten or deleted,
so there is no restore step a crash could skip, and no lock is required.
Every writable path is handed to a helper explicitly and asserted to stay
inside the sandbox root; no helper derives a writable path from the canonical
``WEBSITE_BUILDER`` location.

Scenarios:
  A. sandbox .venv/bin/python exists and is executable -> chosen
  B. no .venv, a PATH interpreter satisfies the dependency probe -> chosen
  C. no usable interpreter -> non-zero exit with a clear message
  D. invoked from outside the sandbox project cwd -> correct project venv
  E. WB_PYTHON wins over the PATH fallback
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

WEBSITE_BUILDER = Path(__file__).resolve().parents[1]
RUN_SH = WEBSITE_BUILDER / "run.sh"

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required for run.sh tests")

_ARGV0_SEP = " |argv0="


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _path_parts(value: str) -> tuple[str, ...]:
    """Split a recorded argv0 into parts, accepting POSIX and Windows shapes.

    Under Git Bash / MSYS a path handed to the script round-trips as
    ``/c/Users/...`` while ``tmp_path`` is a native ``C:\\Users\\...``, so
    assertions below compare component *tails* rather than raw strings.
    """
    return tuple(part for part in re.split(r"[/\\]", value) if part)


def _inside(child: Path, parent: Path) -> bool:
    """True when ``child`` is ``parent`` itself or lives beneath it."""
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _sandbox_venv(project: Path) -> Path:
    """The writable venv location for this sandbox project, and nothing else.

    ``project`` is always the ``tmp_path`` root, so this cannot resolve to the
    canonical developer-owned ``website-builder/.venv``. The containment
    assertion is a guard against a future refactor passing the real project in
    by accident: if it ever fires, the suite would be writing into developer
    state and must fail rather than proceed.
    """
    venv_dir = project / ".venv"
    assert _inside(venv_dir, project), f"venv path escaped the sandbox: {venv_dir}"
    return venv_dir


def _launched_argv0(execs: str) -> str:
    """Return the ``argv0`` of the single ``-m app`` launch recorded in ``execs``."""
    launches = [line for line in execs.splitlines() if " -m app" in line]
    assert len(launches) == 1, f"expected exactly one app launch, got {launches!r}"
    marker = _ARGV0_SEP
    assert marker in launches[0], f"launch did not record argv0: {launches[0]!r}"
    return launches[0].split(marker, 1)[1].strip()


def _make_shim_dir(shim_dir: Path, probe_log: Path, *, probe_result: str,
                   record_exec: str | None = None) -> None:
    """Create stub interpreters recording probe + launch invocations.

    ``probe_result`` is the exit code of the stdlib dependency probe
    (``python -c "import playwright.sync_api, yaml"``) for the fallback
    interpreters. The venv interpreter always "succeeds" so that scenario A is
    not conflated with a broken venv.

    Each record line ends with ``|argv0=$0`` so a test can assert *which* file
    actually executed, not merely which stub label claimed to.
    """
    for name in ("python3", "python"):
        _write_executable(
            shim_dir / name,
            "#!/usr/bin/env bash\n"
            "if [ \"$1\" = \"-c\" ]; then\n"
            f"  echo \"{name} -c \\\"$2\\\"{_ARGV0_SEP}$0\" >> '{probe_log}'\n"
            f"  exit {probe_result}\n"
            "fi\n"
            f"  echo \"{name} $* |argv0=$0\" >> '{record_exec}'\n"
            "exit 0\n",
        )


def _run_run_sh(script: Path, shim_dir: Path, cwd: Path,
                env_overrides: dict | None = None):
    env = dict(os.environ)
    # Only the shims + minimal toolchain are visible: guarantees the discovery
    # fallback loop sees our stub python3/python and nothing else.
    env["PATH"] = f"{shim_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"
    env.pop("WB_PYTHON", None)
    # Neutralise the Node/NVM block. It sits outside the H-9 selection logic
    # under test, but sourcing a real ~/.nvm/nvm.sh would mutate PATH and make
    # the run depend on the host's shell profile.
    env["NVM_DIR"] = str(shim_dir.parent / "empty-nvm-home")
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [BASH, str(script), "--selector-test"],
        cwd=str(cwd), env=env, capture_output=True, text=True,
    )


def _install_venv_python(project: Path, probe_log: Path, record_exec: Path) -> Path:
    """Create a fake project-venv interpreter inside the sandbox project.

    ``project`` is required explicitly: there is no implicit derivation from
    the canonical project location.
    """
    venv_python = _sandbox_venv(project) / "bin" / "python"
    # Refuse to write through a symlink. Writing here would follow the link to
    # its target -- on Linux .venv/bin/python resolves to /usr/bin/python3.12,
    # so a naive write_text() would try to overwrite the system interpreter.
    assert not venv_python.is_symlink(), (
        f"refusing to write through symlink: {venv_python}"
    )
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    _write_executable(
        venv_python,
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"-c\" ]; then\n"
        f"  echo \".venv-python -c \\\"$2\\\"{_ARGV0_SEP}$0\" >> '{probe_log}'\n"
        "  exit 0\n"
        "fi\n"
        f"  echo \".venv-python $* |argv0=$0\" >> '{record_exec}'\n"
        "exit 0\n",
    )
    return venv_python


class _Logs:
    """Probe/exec logs living under the sandbox root."""

    def __init__(self, project: Path):
        self.probe = project / "probe.log"
        self.exec_log = project / "exec.log"
        self.probe.write_text("")
        self.exec_log.write_text("")


class _VenvScenario:
    """A sandbox project plus everything needed to observe interpreter choice."""

    def __init__(self, project: Path, venv_python: Path | None):
        self.project = project
        self.script = project / "run.sh"
        self.venv_python = venv_python
        self._logs = _Logs(project)

    @property
    def probe_log(self) -> Path:
        return self._logs.probe

    @property
    def record_exec(self) -> Path:
        return self._logs.exec_log

    @property
    def shim_dir(self) -> Path:
        shim = self.project / "shim-bin"
        shim.mkdir(exist_ok=True)
        return shim

    def execs(self) -> str:
        return self.record_exec.read_text()

    def probes(self) -> str:
        return self.probe_log.read_text()


@pytest.fixture
def sandbox_project(tmp_path):
    """A temp project root holding a byte-identical copy of run.sh.

    The copy is asserted byte-identical inline: the selection logic under test
    runs unchanged, while SCRIPT_DIR resolves into tmp_path so no test can
    touch the real project tree.
    """
    project = tmp_path / "project"
    project.mkdir()
    script = project / "run.sh"
    script.write_bytes(RUN_SH.read_bytes())
    shutil.copymode(RUN_SH, script)
    assert script.read_bytes() == RUN_SH.read_bytes(), "run.sh copy drifted"
    return project


@pytest.fixture
def fake_project_venv(sandbox_project):
    """Sandbox project with a fake, non-symlink ``.venv/bin/python`` present."""
    scenario = _VenvScenario(sandbox_project, venv_python=None)
    scenario.venv_python = _install_venv_python(
        sandbox_project, scenario.probe_log, scenario.record_exec
    )
    return scenario


@pytest.fixture
def absent_project_venv(sandbox_project):
    """Sandbox project with no ``.venv`` at all, so PATH/WB_PYTHON decide."""
    scenario = _VenvScenario(sandbox_project, venv_python=None)
    assert not _sandbox_venv(sandbox_project).exists(), "sandbox .venv must be absent"
    return scenario


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def test_run_sh_prefers_project_venv(fake_project_venv):
    """A: sandbox .venv/bin/python exists and is executable -> run.sh chooses it."""
    scenario = fake_project_venv
    shim = scenario.shim_dir
    # Fallback interpreters exist but must NOT be selected.
    _make_shim_dir(shim, scenario.probe_log, probe_result="1",
                   record_exec=str(scenario.record_exec))

    result = _run_run_sh(scenario.script, shim, cwd=scenario.project)

    assert result.returncode == 0, result.stderr
    execs = scenario.execs()
    assert ".venv-python -m app --selector-test" in execs
    assert "python3 -m app" not in execs
    assert not any(line.startswith("python -m app") for line in execs.splitlines())
    # The file that actually executed was the sandbox fake venv interpreter,
    # not merely a stub that claimed the ".venv-python" label.
    assert _path_parts(_launched_argv0(execs))[-3:] == (".venv", "bin", "python")


def test_run_sh_falls_back_to_usable_system_interpreter(absent_project_venv):
    """B: no .venv, a PATH interpreter satisfies the dependency probe."""
    scenario = absent_project_venv
    shim = scenario.shim_dir
    _make_shim_dir(shim, scenario.probe_log, probe_result="0",
                   record_exec=str(scenario.record_exec))

    result = _run_run_sh(scenario.script, shim, cwd=scenario.project)

    assert result.returncode == 0, result.stderr
    execs = scenario.execs()
    assert "python3 -m app --selector-test" in execs
    # The probe was actually used to decide.
    assert "import playwright.sync_api, yaml" in scenario.probes()
    assert _path_parts(_launched_argv0(execs))[-1] == "python3"


def test_run_sh_fails_closed_without_usable_interpreter(absent_project_venv):
    """C: no usable interpreter -> non-zero exit with a clear message."""
    scenario = absent_project_venv
    shim = scenario.shim_dir
    # Both fallbacks fail the dependency probe.
    _make_shim_dir(shim, scenario.probe_log, probe_result="1",
                   record_exec=str(scenario.record_exec))

    result = _run_run_sh(scenario.script, shim, cwd=scenario.project)

    assert result.returncode != 0
    assert "no usable Python interpreter" in result.stderr
    assert "playwright.sync_api" in result.stderr
    # It must never have launched the app.
    assert "-m app" not in scenario.execs()


def test_run_sh_resolves_paths_when_invoked_outside_project(fake_project_venv):
    """D: invoked from an arbitrary cwd -> still resolves the project venv."""
    scenario = fake_project_venv
    shim = scenario.shim_dir
    _make_shim_dir(shim, scenario.probe_log, probe_result="1",
                   record_exec=str(scenario.record_exec))

    outside = scenario.project.parent / "elsewhere"
    outside.mkdir()
    result = _run_run_sh(scenario.script, shim, cwd=outside)

    assert result.returncode == 0, result.stderr
    execs = scenario.execs()
    # The sandbox venv was discovered from an unrelated cwd -> SCRIPT_DIR
    # resolution is correct and independent of the caller's cwd.
    assert ".venv-python -m app --selector-test" in execs
    assert _path_parts(_launched_argv0(execs))[-3:] == (".venv", "bin", "python")
    # Nothing was resolved against the caller's cwd.
    assert "elsewhere" not in execs


def test_run_sh_prefers_wb_python_env_override(absent_project_venv):
    """E: WB_PYTHON is honoured ahead of the PATH-name fallbacks."""
    scenario = absent_project_venv
    shim = scenario.shim_dir
    custom = scenario.project / "custom-python"
    _write_executable(
        custom,
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"-c\" ]; then\n"
        f"  echo \"custom -c \\\"$2\\\"{_ARGV0_SEP}$0\" >> '{scenario.probe_log}'\n"
        "  exit 0\n"
        "fi\n"
        f"  echo \"custom $* |argv0=$0\" >> '{scenario.record_exec}'\n"
        "exit 0\n",
    )
    _make_shim_dir(shim, scenario.probe_log, probe_result="0",
                   record_exec=str(scenario.record_exec))

    result = _run_run_sh(scenario.script, shim, cwd=scenario.project,
                         env_overrides={"WB_PYTHON": str(custom)})

    assert result.returncode == 0, result.stderr
    execs = scenario.execs()
    assert "custom -m app --selector-test" in execs
    assert "python3 -m app" not in execs
    assert _path_parts(_launched_argv0(execs))[-1] == "custom-python"


# ---------------------------------------------------------------------------
# Isolation regressions
# ---------------------------------------------------------------------------

def test_fake_venv_interpreter_is_a_real_file_inside_the_sandbox(fake_project_venv):
    """The fake venv interpreter is a real in-project file, never a symlink."""
    scenario = fake_project_venv
    venv_python = scenario.venv_python
    assert venv_python is not None
    assert venv_python.is_file()
    assert not venv_python.is_symlink()
    assert _inside(venv_python, scenario.project)
    assert not _inside(venv_python, WEBSITE_BUILDER)


def test_fallback_scenarios_run_with_project_venv_absent(absent_project_venv):
    """B/C/E really run with the sandbox project .venv absent."""
    scenario = absent_project_venv
    assert not (scenario.project / ".venv").exists()
    assert not (scenario.project / ".venv").is_symlink()


def test_symlinked_venv_python_is_never_written_through(tmp_path):
    """A symlinked .venv/bin/python is refused and its target left untouched."""
    project = tmp_path / "project"
    project.mkdir()

    sentinel = tmp_path / "sentinel-python"
    sentinel.write_bytes(b"REAL-INTERPRETER-DO-NOT-TOUCH\n")

    venv_python = _sandbox_venv(project) / "bin" / "python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    try:
        venv_python.symlink_to(sentinel)
    except OSError:
        pytest.skip("symlink creation not permitted on this host")

    before = sentinel.read_bytes()
    with pytest.raises(AssertionError, match="refusing to write through symlink"):
        _install_venv_python(project, tmp_path / "probe.log", tmp_path / "exec.log")

    assert sentinel.read_bytes() == before
    assert venv_python.is_symlink()