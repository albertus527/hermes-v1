"""Build-layer integration for the self-contained artifact gate.

Complements ``test_self_contained.py`` (which covers the normalizer + preflight
directly). These tests pin the BUILD-LAYER contract:

  * the gate participates in the fixed cheap-check sequence and its failure is
    classified as a REPAIRABLE source error (so it reuses the existing single
    compile-repair budget rather than opening a second repair system);
  * the gate runs BEFORE the ``checked`` binding is recorded, and the binding
    therefore always describes NORMALIZED bytes;
  * the terminal failure surfaces the stable ``EXTERNAL_RUNTIME_DEPENDENCY``
    contract together with the failing check;
  * the FRONTEND repair instructions carry the per-file diagnostics.

No real network, no npm, no Vercel, no Telegram.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.selfcontained import (  # noqa: E402
    EXTERNAL_RUNTIME_DEPENDENCY,
    PREVIEW_NORMALIZATION_INFRASTRUCTURE,
)
from app.projects.build import (  # noqa: E402
    _CHEAP_CHECK_SEQUENCE,
    FrontendBuilder,
    classify_cheap_check_failure,
)


class _StubRunner:
    """Deterministic ProjectRunner stand-in: real workspace layout + real
    single-worker slot semantics, but no process is ever spawned."""

    def __init__(self, root: Path):
        self.workspace_root = Path(root)
        self.commands: list = []

    def create_workspace(self, project_id: str) -> Path:
        path = self.workspace_root / project_id
        (path / "src").mkdir(parents=True, exist_ok=True)
        (path / "dist").mkdir(parents=True, exist_ok=True)
        return path

    def acquire_project(self, project_id: str) -> bool:
        return True

    def release_project(self, project_id: str) -> None:
        return None

    def run_command(self, project_id, command, cwd=None, **kwargs):
        self.commands.append(list(command))
        return subprocess.CompletedProcess(args=list(command), returncode=0,
                                           stdout="", stderr="")


# ---------------------------------------------------------------------------
# Classification: the gate reuses the EXISTING compile-repair budget
# ---------------------------------------------------------------------------

class TestGateClassification:
    def _checks(self, **overrides):
        checks = {name: {"success": True, "stdout": "", "stderr": ""}
                  for name in _CHEAP_CHECK_SEQUENCE}
        checks.update(overrides)
        return checks

    def test_self_contained_failure_is_repairable_source_error(self):
        checks = self._checks(self_contained={
            "success": False,
            "stdout": "",
            "stderr": EXTERNAL_RUNTIME_DEPENDENCY,
        })
        decision = classify_cheap_check_failure(checks)
        assert decision.eligible is True
        assert decision.failed_check == "self_contained"
        assert decision.classification == "source_error"

    def test_self_contained_infrastructure_failure_is_not_repairable(self):
        checks = self._checks(self_contained={
            "success": False,
            "stdout": "",
            "stderr": PREVIEW_NORMALIZATION_INFRASTRUCTURE,
            "classification": "infrastructure",
        })
        decision = classify_cheap_check_failure(checks)
        assert decision.eligible is False
        assert decision.classification == "infrastructure_failure"

    def test_self_contained_is_not_masked_by_a_passing_build(self):
        """The gate is the FIRST failing check when npm steps all passed."""
        checks = self._checks(self_contained={"success": False, "stdout": "",
                                              "stderr": EXTERNAL_RUNTIME_DEPENDENCY})
        assert classify_cheap_check_failure(checks).failed_check == "self_contained"

    def test_an_ealier_npm_failure_still_wins(self):
        """A real build failure is reported before the artifact gate."""
        checks = self._checks(npm_build={
            "success": False, "stdout": "", "stderr": "error TS2304: x"})
        checks["self_contained"] = {"success": False, "stdout": "",
                                    "stderr": EXTERNAL_RUNTIME_DEPENDENCY}
        assert classify_cheap_check_failure(checks).failed_check == "npm_build"

    def test_no_failure_is_ineligible(self):
        decision = classify_cheap_check_failure(self._checks())
        assert decision.eligible is False
        assert decision.classification == "no_failure"


# ---------------------------------------------------------------------------
# Repair instructions carry the actionable diagnostics
# ---------------------------------------------------------------------------

class TestRepairInstructions:
    def _builder(self, tmp_path) -> FrontendBuilder:
        from app.core.state import ProjectStateStore

        store = ProjectStateStore(tmp_path / "state")
        return FrontendBuilder(_StubRunner(tmp_path / "work"), store)

    def test_instructions_include_gate_output_and_kinds(self, tmp_path):
        builder = self._builder(tmp_path)
        state = type("S", (), {"design_dna": {"version": 1}})()
        checks = {name: {"success": True, "stdout": "", "stderr": ""}
                  for name in _CHEAP_CHECK_SEQUENCE}
        checks["self_contained"] = {
            "success": False,
            "stdout": "",
            "stderr": (EXTERNAL_RUNTIME_DEPENDENCY +
                       "\n- external_script host=cdn.example.com file=index.html"
                       " :: render-critical external script"),
        }
        decision = classify_cheap_check_failure(checks)
        assert decision.failed_check == "self_contained"

        text = builder._build_compile_repair_instructions(state, checks, decision)
        assert "self_contained: FAIL" in text
        assert EXTERNAL_RUNTIME_DEPENDENCY in text
        assert "host=cdn.example.com" in text
        assert "file=index.html" in text


# ---------------------------------------------------------------------------
# Binding ordering: the checked snapshot describes NORMALIZED bytes
# ---------------------------------------------------------------------------

class TestBindingOrdering:
    def test_checked_binding_matches_normalized_bytes(self, tmp_path):
        """The gate must run BEFORE record_checks, so the bound
        ``artifact_sha256`` covers the vendored fonts, not pre-normalization
        bytes."""
        from app.core import selfcontained as module
        from app.core.state import ProjectStateStore
        from tests.test_self_contained import _fixture, _p7_index_html
        from app.deploy.snapshot import TestedSnapshot, source_fingerprint

        store = ProjectStateStore(tmp_path / "state")
        runner = _StubRunner(tmp_path / "work")
        builder = FrontendBuilder(runner, store)

        project_id = "proj"
        workspace = runner.create_workspace(project_id)
        (workspace / "src" / "App.tsx").write_text("export default null\n",
                                                   encoding="utf-8")
        (workspace / "index.html").write_text(_p7_index_html(), encoding="utf-8")
        (workspace / "dist" / "index.html").write_text(_p7_index_html(),
                                                       encoding="utf-8")
        (workspace / "design-dna.json").write_text(json.dumps({"version": 1}),
                                                   encoding="utf-8")

        original = module.normalize_artifact

        def with_fixture(ws, **kwargs):
            kwargs.setdefault("vendored", _fixture())
            return original(ws, **kwargs)

        module.normalize_artifact = with_fixture
        try:
            pre_normalization = source_fingerprint(workspace)
            report = module.normalize_and_check_self_contained(project_id, workspace)
            assert report.ok is True, report.error_text()
            after_normalization = source_fingerprint(workspace)
            assert after_normalization != pre_normalization

            from app.deploy.snapshot import record_checks

            record_checks(store, project_id, workspace, after_normalization)
        finally:
            module.normalize_artifact = original

        state = store.load(project_id)
        checked = state.deployment["checked"]
        assert checked["source_sha256"] == after_normalization
        # ``record_checks`` binds the CORRECT bytes and intentionally clears
        # any previously tested snapshot (QA re-binds it). Prove the bound
        # bytes are the NORMALIZED artifact by re-capturing from disk with the
        # recorded digests.
        snapshot = TestedSnapshot.capture(
            workspace, checked["source_sha256"], checked["artifact_sha256"])
        assert any(name.startswith("fonts/") for name in snapshot.dist)
        assert b"fonts.gstatic.com" not in b"".join(snapshot.dist.values())
        # The pre-normalization fingerprint can no longer verify: the bound
        # artifact moved.
        assert checked["source_sha256"] != pre_normalization
        assert "tested_snapshot" not in state.deployment
