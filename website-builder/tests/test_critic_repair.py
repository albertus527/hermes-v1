"""D3b: the production Impeccable critic scanner seam.

Every test drives the scanner with an INJECTED runner, so nothing executes the
real engine here. The properties under test are the ones the orchestration
layer gates on:

    * a degraded zero-findings scan is DEGRADED, never CLEAN;
    * a scan failure is FAILED, never an empty pass;
    * a missing engine is an absence, never a pass;
    * a full-quality clean scan of the intended project IS a certification;
    * a finding set at full quality is FINDINGS and authoritative.

The REAL engine is exercised in the live VPS qualification, not here (the
default suite must stay offline and deterministic).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import critic_policy as cp
from app.core.critic_repair import ImpeccableScanner, scanner_from_config
from app.core.design_critic import DEGRADED_MARKER, EXIT_CLEAN, EXIT_FINDINGS

NODE = "/usr/bin/node"

REAL_FINDINGS = [
    {
        "antipattern": "low-contrast",
        "name": "Low contrast text",
        "description": "Text does not meet WCAG AA contrast requirements.",
        "severity": "warning",
        "category": "quality",
        "file": "src/App.tsx",
        "line": 10,
        "snippet": "3.2:1 (need 4.5:1)",
    }
]


class RecordingRunner:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.calls = []
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv, **kwargs):
        self.calls.append((tuple(argv), kwargs))
        return SimpleNamespace(stdout=self.stdout, returncode=self.returncode, stderr=self.stderr)


@pytest.fixture(autouse=True)
def _no_real_execution(monkeypatch):
    def deny(*a, **k):
        raise AssertionError("the offline suite never executes an engine")

    monkeypatch.setattr(subprocess, "run", deny)
    monkeypatch.setattr(subprocess, "Popen", deny)


@pytest.fixture
def full_skill(tmp_path) -> Path:
    """A skill root with the engine AND its parser runtime (full quality)."""
    root = tmp_path / "skills" / "impeccable"
    (root / "scripts" / "detector").mkdir(parents=True)
    (root / "scripts" / "detect.mjs").write_text(
        "import { detectCli } from './detector/detect-antipatterns.mjs';\n",
        encoding="utf-8",
    )
    (root / "scripts" / "detector" / "detect-antipatterns.mjs").write_text(
        "export { detectCli };\n", encoding="utf-8"
    )
    for pkg in ("htmlparser2", "css-select", "css-tree", "domutils"):
        (root / "node_modules" / pkg).mkdir(parents=True)
    return root


@pytest.fixture
def workspace(tmp_path) -> Path:
    ws = tmp_path / "project"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "App.tsx").write_text("// real\n", encoding="utf-8")
    return ws


def _scanner(skill_root, runner):
    return ImpeccableScanner(
        skill_root=skill_root, node_executable=NODE, runner=runner
    )


# ---------------------------------------------------------------------------
# Clean, full-quality, intended-project scan = a certification
# ---------------------------------------------------------------------------


def test_a_full_quality_clean_scan_of_the_intended_project_is_clean(full_skill, workspace):
    runner = RecordingRunner(stdout="[]", returncode=EXIT_CLEAN)
    result = _scanner(full_skill, runner).scan(workspace)

    assert result.state == cp.CLEAN
    assert result.authoritative is True
    assert result.intended_project_scanned is True
    assert result.is_clean_certification is True
    assert result.findings == ()


def test_the_scan_runs_with_the_project_as_cwd_and_the_fixed_target(full_skill, workspace):
    runner = RecordingRunner(stdout="[]", returncode=EXIT_CLEAN)
    _scanner(full_skill, runner).scan(workspace)

    argv, kwargs = runner.calls[0]
    # fixed target is the last element, and cwd is the PROJECT (not the skill)
    assert argv[-1] == "."
    assert kwargs["cwd"] == str(workspace)
    assert kwargs["shell"] is False


# ---------------------------------------------------------------------------
# A degraded zero-findings scan is NOT a clean certification
# ---------------------------------------------------------------------------


def test_a_degraded_zero_findings_scan_is_degraded_not_clean(tmp_path, workspace):
    """The batch's central failure: a degraded clean must not certify."""
    root = tmp_path / "skills" / "impeccable"
    (root / "scripts" / "detector").mkdir(parents=True)
    (root / "scripts" / "detect.mjs").write_text("// engine\n", encoding="utf-8")
    (root / "scripts" / "detector" / "detect-antipatterns.mjs").write_text(
        "// facade\n", encoding="utf-8"
    )
    # NO node_modules -> degraded parser runtime.
    runner = RecordingRunner(stdout="[]", returncode=EXIT_CLEAN, stderr=DEGRADED_MARKER)
    result = _scanner(root, runner).scan(workspace)

    assert result.state == cp.DEGRADED
    assert result.authoritative is False
    assert result.is_clean_certification is False
    assert result.degraded is True


def test_a_degraded_scan_with_findings_is_findings_but_not_authoritative(tmp_path, workspace):
    root = tmp_path / "skills" / "impeccable"
    (root / "scripts" / "detector").mkdir(parents=True)
    (root / "scripts" / "detect.mjs").write_text("// engine\n", encoding="utf-8")
    (root / "scripts" / "detector" / "detect-antipatterns.mjs").write_text(
        "// facade\n", encoding="utf-8"
    )
    runner = RecordingRunner(
        stdout=json.dumps(REAL_FINDINGS), returncode=EXIT_FINDINGS, stderr=DEGRADED_MARKER
    )
    result = _scanner(root, runner).scan(workspace)

    assert result.state == cp.FINDINGS
    assert result.authoritative is False
    assert len(result.findings) == 1


# ---------------------------------------------------------------------------
# A scan failure is never an empty pass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("returncode", [1, 3, 127, 255])
def test_a_scan_failure_is_failed_not_clean(full_skill, workspace, returncode):
    runner = RecordingRunner(stdout="", returncode=returncode)
    result = _scanner(full_skill, runner).scan(workspace)

    assert result.state == cp.FAILED
    assert result.authoritative is False
    assert result.findings == ()


def test_unparseable_output_is_failed(full_skill, workspace):
    runner = RecordingRunner(stdout="not json at all", returncode=EXIT_FINDINGS)
    result = _scanner(full_skill, runner).scan(workspace)

    assert result.state == cp.FAILED
    assert result.authoritative is False


def test_a_timeout_is_failed(full_skill, workspace):
    def explode(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    result = _scanner(full_skill, explode).scan(workspace)
    assert result.state == cp.FAILED


# ---------------------------------------------------------------------------
# A missing engine / interpreter is an absence, never a pass
# ---------------------------------------------------------------------------


def test_a_missing_engine_is_degraded_not_clean(tmp_path, workspace):
    result = _scanner(tmp_path / "nope", RecordingRunner(stdout="[]")).scan(workspace)

    assert result.state == cp.DEGRADED
    assert result.authoritative is False
    assert "engine_unavailable" in result.reasons


def test_a_missing_interpreter_is_reported_and_nothing_runs(full_skill, workspace):
    runner = RecordingRunner(stdout="[]")
    scanner = ImpeccableScanner(skill_root=full_skill, node_executable=None, runner=runner)
    result = scanner.scan(workspace)

    assert result.state == cp.DEGRADED
    assert result.authoritative is False
    assert runner.calls == [], "nothing may execute without a named interpreter"


def test_a_missing_workspace_is_not_scanned(full_skill, tmp_path):
    runner = RecordingRunner(stdout="[]")
    result = _scanner(full_skill, runner).scan(tmp_path / "does-not-exist")

    assert result.state == cp.FAILED
    assert result.intended_project_scanned is False
    assert runner.calls == []


def test_no_skill_root_is_not_run(tmp_path):
    result = ImpeccableScanner(skill_root=None, node_executable=NODE).scan(tmp_path)

    assert result.state == cp.NOT_RUN
    assert result.authoritative is False


# ---------------------------------------------------------------------------
# Findings at full quality are authoritative
# ---------------------------------------------------------------------------


def test_findings_at_full_quality_are_authoritative(full_skill, workspace):
    runner = RecordingRunner(
        stdout=json.dumps(REAL_FINDINGS), returncode=EXIT_FINDINGS, stderr=""
    )
    result = _scanner(full_skill, runner).scan(workspace)

    assert result.state == cp.FINDINGS
    assert result.authoritative is True
    assert result.findings[0].rule_id == "low-contrast"


def test_the_result_is_serializable(full_skill, workspace):
    runner = RecordingRunner(
        stdout=json.dumps(REAL_FINDINGS), returncode=EXIT_FINDINGS
    )
    payload = _scanner(full_skill, runner).scan(workspace).to_dict()

    assert payload["state"] == cp.FINDINGS
    assert payload["authoritative"] is True
    assert payload["findings"][0]["rule_id"] == "low-contrast"


# ---------------------------------------------------------------------------
# scanner_from_config
# ---------------------------------------------------------------------------


def test_scanner_from_config_builds_from_a_profile_home(tmp_path):
    scanner = scanner_from_config(hermes_home=tmp_path, node_executable=NODE)
    assert scanner is not None
    assert scanner.skill_root == tmp_path / "skills" / "impeccable"


def test_scanner_from_config_without_a_home_is_none():
    assert scanner_from_config(hermes_home=None, node_executable=NODE) is None
