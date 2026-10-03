"""A FRONTEND run that declares success must have produced the artifacts.

FRONTEND is told, in its prompt, that "creating design-dna.json alone does not
complete this task" and that the run is incomplete until the starter
placeholder in ``src/`` has been replaced. Nothing checked that. The only place
the artifacts were consulted was the TIMEOUT-recovery branch, so a run which
exited 0 having written ``design-dna.json`` and nothing else was accepted and
carried on into deterministic checks, QA and preview — on a placeholder site.

These tests pin the postcondition only. They add no timing and no supervision
behaviour, which is deliberate: whether a run that declares NOTHING should also
be rejected is a separate question with a different blast radius, because that
is the artifact-recovery path.

No live LLM, browser, or npm is required.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.state import ProjectStateStore
from app.hermes.adapter import (
    FRONTEND_IMPLEMENTATION_MISSING,
    HermesAdapter,
    HermesResult,
)


DECLARED_SUCCESS = json.dumps(
    {"success": True, "design_dna_path": "design-dna.json", "error": ""}
)

DESIGN_DNA = {
    "version": 1,
    "brand_personality": "premium, modern, approachable",
    "typography": {"heading_font": "Inter", "body_font": "Source Sans 3"},
}

STARTER_APP = "// starter placeholder\n"
IMPLEMENTED_APP = "export default function App() { return <main>Northcut</main> }\n"


def _ok_run(response: str) -> HermesResult:
    return HermesResult(
        success=True,
        response=response,
        exit_code=0,
        error=None,
        error_code=None,
        timed_out=False,
        invocation={"invocation_id": "inv-1"},
    )


class TestDeclaredSuccessPostcondition(unittest.TestCase):
    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmpdir_obj.name)
        self.repo_root = self.tmpdir / "repo"
        starter = self.repo_root / "templates" / "frontend-starter" / "src"
        starter.mkdir(parents=True)
        (starter / "App.tsx").write_text(STARTER_APP, encoding="utf-8")
        self.adapter = HermesAdapter(
            ProjectStateStore(self.tmpdir / "state"),
            hermes_home=self.tmpdir / ".hermes-website",
            repo_root=self.repo_root,
        )
        self.workspace = self.tmpdir / "ws"
        (self.workspace / "src").mkdir(parents=True)
        self.app_path = self.workspace / "src" / "App.tsx"
        self.app_path.write_text(STARTER_APP, encoding="utf-8")

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    def _write_dna(self):
        (self.workspace / "design-dna.json").write_text(
            json.dumps(DESIGN_DNA), encoding="utf-8"
        )

    def _build(self, response: str = DECLARED_SUCCESS):
        with patch.object(self.adapter, "_run_hermes_cli", return_value=_ok_run(response)):
            return self.adapter.frontend_build(
                project_id="proj",
                brief={"name": "Northcut", "what": "barbershop", "why": "booking WA"},
                workspace=self.workspace,
            )

    # --- the defect ---------------------------------------------------------

    def test_design_dna_alone_is_not_a_success(self):
        """Design DNA and nothing else: the p20 shape, reported as a success."""
        self._write_dna()

        result = self._build()

        self.assertFalse(result["success"], result)
        self.assertEqual(result["error_code"], FRONTEND_IMPLEMENTATION_MISSING)
        self.assertIn("src/App.tsx", result["error"])
        self.assertTrue((result["error"] or "").strip())

    def test_a_success_with_no_artifacts_at_all_is_rejected(self):
        result = self._build()

        self.assertFalse(result["success"], result)
        self.assertEqual(result["error_code"], FRONTEND_IMPLEMENTATION_MISSING)

    def test_a_rejected_success_still_carries_its_diagnostics(self):
        """The invocation record must survive, or the run is unexplainable."""
        self._write_dna()

        result = self._build()

        self.assertEqual(result["invocation"], {"invocation_id": "inv-1"})
        self.assertEqual(result["design_dna"], DESIGN_DNA)

    # --- preservation -------------------------------------------------------

    def test_a_declared_success_with_complete_artifacts_is_accepted(self):
        self._write_dna()
        self.app_path.write_text(IMPLEMENTED_APP, encoding="utf-8")

        result = self._build()

        self.assertTrue(result["success"], result)
        self.assertIsNone(result["error"])
        self.assertNotIn("error_code", result)
        self.assertEqual(result["design_dna"], DESIGN_DNA)

    def test_a_nested_dna_artifact_still_counts_as_complete(self):
        """The completeness check shares the loader, so shapes cannot disagree."""
        (self.workspace / "design-dna.json").write_text(
            json.dumps({"design_dna": DESIGN_DNA}), encoding="utf-8"
        )
        self.app_path.write_text(IMPLEMENTED_APP, encoding="utf-8")

        result = self._build()

        self.assertTrue(result["success"], result)
        self.assertEqual(result["design_dna"], DESIGN_DNA)

    def test_an_unreadable_dna_file_is_not_a_complete_artifact(self):
        (self.workspace / "design-dna.json").write_text("{not json", encoding="utf-8")
        self.app_path.write_text(IMPLEMENTED_APP, encoding="utf-8")

        result = self._build()

        self.assertFalse(result["success"], result)
        self.assertEqual(result["error_code"], FRONTEND_IMPLEMENTATION_MISSING)

    def test_a_summary_that_declares_nothing_is_left_to_the_artifacts(self):
        """No declaration is the artifact-recovery case; it is not our verdict.

        This is the one path where an incomplete workspace is still accepted,
        and that is pre-existing behaviour on purpose: the timeout-recovery
        branch calls the parser with an empty response precisely so the disk
        decides.
        """
        self.app_path.write_text(IMPLEMENTED_APP, encoding="utf-8")

        result = self._build(response="")

        self.assertTrue(result["success"], result)

    def test_a_declared_failure_is_reported_as_itself(self):
        self._write_dna()
        self.app_path.write_text(IMPLEMENTED_APP, encoding="utf-8")

        result = self._build(
            json.dumps({"success": False, "error": "brand facts missing"})
        )

        self.assertFalse(result["success"], result)
        self.assertEqual(result["error"], "brand facts missing")
        self.assertNotEqual(result.get("error_code"), FRONTEND_IMPLEMENTATION_MISSING)

    def test_completeness_has_exactly_one_definition(self):
        """The postcondition must reuse the existing check, not re-derive it."""
        calls = []
        original = self.adapter._has_complete_frontend_artifacts

        def _spy(workspace):
            calls.append(workspace)
            return original(workspace)

        self._write_dna()
        with patch.object(
            self.adapter, "_has_complete_frontend_artifacts", side_effect=_spy
        ), patch.object(self.adapter, "_run_hermes_cli", return_value=_ok_run(DECLARED_SUCCESS)):
            self.adapter.frontend_build(
                project_id="proj",
                brief={"name": "N", "what": "b", "why": "w"},
                workspace=self.workspace,
            )

        self.assertEqual(calls, [self.workspace])


class TestCompletenessAgainstTheRealStarter(unittest.TestCase):
    """The postcondition's reference is the shipped template, not a fixture.

    ``_has_complete_frontend_artifacts`` returns False when the starter file is
    not where it expects — which would make every FRONTEND success a
    FRONTEND_IMPLEMENTATION_MISSING. That is a total product break, and every
    other test here uses a synthetic starter, so nothing would have caught it.
    This asserts a packaging invariant (the template exists where the adapter
    reads it), not the shape of its contents.
    """

    def _repo_adapter(self, repo_root):
        return HermesAdapter(
            ProjectStateStore(repo_root / "unused-state"),
            hermes_home=repo_root / "unused-home",
            repo_root=repo_root,
        )

    def test_the_shipped_starter_is_where_the_adapter_looks_for_it(self):
        repo_root = Path(__file__).resolve().parents[2]
        adapter = self._repo_adapter(repo_root)
        starter = adapter.repo_root / "templates" / "frontend-starter" / "src" / "App.tsx"

        self.assertTrue(
            starter.is_file(),
            f"the starter placeholder is missing at {starter}; every FRONTEND "
            f"success would be rejected as {FRONTEND_IMPLEMENTATION_MISSING}",
        )

    def test_the_unchanged_real_starter_is_not_a_complete_build(self):
        repo_root = Path(__file__).resolve().parents[2]
        adapter = self._repo_adapter(repo_root)
        starter = repo_root / "templates" / "frontend-starter" / "src" / "App.tsx"

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "src").mkdir()
            shutil.copy(starter, workspace / "src" / "App.tsx")
            (workspace / "design-dna.json").write_text(
                json.dumps(DESIGN_DNA), encoding="utf-8"
            )
            # Byte-identical to the real placeholder: what p20 left behind.
            self.assertFalse(adapter._has_complete_frontend_artifacts(workspace))

            (workspace / "src" / "App.tsx").write_text(IMPLEMENTED_APP, encoding="utf-8")
            self.assertTrue(adapter._has_complete_frontend_artifacts(workspace))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()