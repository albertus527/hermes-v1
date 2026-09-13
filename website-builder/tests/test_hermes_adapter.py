"""Tests for the Hermes adapter boundary.

Verifies the thin adapter uses the existing Hermes seams correctly.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.state import ProjectStateStore
from app.hermes.adapter import HermesAdapter, HermesResult


class TestHermesAdapter(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = ProjectStateStore(Path(self.tmpdir) / "state")
        self.adapter = HermesAdapter(
            self.store,
            hermes_home=Path(self.tmpdir) / ".hermes-website",
            repo_root=Path(self.tmpdir) / "repo",
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_adapter_preserves_hermes_home(self):
        """Adapter uses Website-specific HERMES_HOME."""
        self.assertEqual(
            self.adapter.hermes_home, Path(self.tmpdir) / ".hermes-website"
        )

    def _patch_fast_runtime(self, mock_agent_cls):
        """Patch the real FAST execution seam and return the mocks.

        The FAST path constructs AIAgent directly via module-level Hermes
        helpers. These patches intercept the actual module attributes the
        implementation reads — not a re-imported alias.
        """
        # A config that DOES configure default CLI toolsets, to prove they
        # cannot leak into FAST.
        cfg_with_toolsets = {
            "model": {"default": "test-model", "provider": "test-provider"},
            "tools": {"cli": {"enabled": ["terminal", "file", "web", "browser"]}},
        }

        agent_instance = MagicMock()
        agent_instance.run_conversation.return_value = {
            "final_response": '{"scope":"WEBSITE","name":"N","what":"w","why":"y",'
            '"why_destination":null,"clarification_needed":false,'
            '"readiness":"DISCOVERY_READY"}'
        }
        mock_agent_cls.return_value = agent_instance

        patches = {
            "load_config": patch(
                "app.hermes.adapter.load_config", return_value=cfg_with_toolsets
            ),
            "resolve_runtime_provider": patch(
                "app.hermes.adapter.resolve_runtime_provider",
                return_value={
                    "api_key": "k",
                    "base_url": "https://x",
                    "provider": "test-provider",
                    "requested_provider": "test-provider",
                    "api_mode": "chat_completions",
                    "credential_pool": None,
                },
            ),
            "get_fallback_chain": patch(
                "app.hermes.adapter.get_fallback_chain", return_value=[]
            ),
            "detect_provider_for_model": patch(
                "app.hermes.adapter.detect_provider_for_model",
                return_value=("test-provider", "test-model"),
            ),
            "build_skills": patch(
                "app.hermes.adapter._build_preloaded_skills_prompt",
                return_value=None,
            ),
            "session_db": patch(
                "app.hermes.adapter._create_session_db_for_oneshot",
                return_value=None,
            ),
        }
        mocks = {name: p.start() for name, p in patches.items()}
        for p in patches.values():
            self.addCleanup(p.stop)
        mocks["agent_instance"] = agent_instance
        return mocks

    def test_fast_uses_programmatic_boundary(self):
        """FAST uses the programmatic boundary for zero-tool guarantee."""
        with patch.object(self.adapter, "_run_fast_programmatic") as mock_prog:
            mock_prog.return_value = HermesResult(
                success=True,
                response='{"scope":"WEBSITE","name":"Northcut","what":"barbershop","why":"booking","why_destination":null,"clarification_needed":false,"readiness":"DISCOVERY_READY"}',
            )
            result = self.adapter.fast_interpret("Bikin Northcut.")

        self.assertEqual(result["source"], "hermes_fast")
        mock_prog.assert_called_once()

    def test_fast_constructs_agent_with_empty_toolsets(self):
        """FAST ultimately constructs/runs an agent with enabled_toolsets=[]."""
        with patch("app.hermes.adapter.AIAgent") as mock_agent_cls:
            self._patch_fast_runtime(mock_agent_cls)
            result = self.adapter._run_fast_programmatic("test prompt")

        self.assertTrue(result.success, result.error)
        mock_agent_cls.assert_called_once()
        call_kwargs = mock_agent_cls.call_args[1]
        # THE zero-tools invariant: a literal empty list, NOT None.
        self.assertEqual(call_kwargs["enabled_toolsets"], [])
        self.assertIsNotNone(call_kwargs["enabled_toolsets"])
        agent_instance = mock_agent_cls.return_value
        agent_instance.run_conversation.assert_called_once_with("test prompt")

    def test_fast_config_cli_toolsets_cannot_leak(self):
        """Configured/default CLI toolsets cannot leak into FAST.

        load_config returns a config whose tools.cli.enabled lists toolsets;
        FAST must still build the agent with enabled_toolsets=[] and must
        never consult the platform CLI toolset resolver.
        """
        with patch("app.hermes.adapter.AIAgent") as mock_agent_cls:
            self._patch_fast_runtime(mock_agent_cls)
            with patch(
                "hermes_cli.tools_config._get_platform_tools"
            ) as mock_platform_tools:
                result = self.adapter._run_fast_programmatic("test")

        self.assertTrue(result.success, result.error)
        # FAST must not call the CLI platform-toolset resolver at all.
        mock_platform_tools.assert_not_called()
        # And the agent must still be built with zero toolsets.
        self.assertEqual(mock_agent_cls.call_args[1]["enabled_toolsets"], [])

    def test_fast_fallback_on_hermes_failure(self):
        """FAST falls back to deterministic heuristic when Hermes fails."""
        with patch.object(self.adapter, "_run_fast_programmatic") as mock_prog:
            mock_prog.return_value = HermesResult(
                success=False, error="Connection failed", exit_code=1
            )
            result = self.adapter.fast_interpret("Bikin Northcut, barbershop.")

        self.assertEqual(result["source"], "fallback_heuristic")
        self.assertEqual(result["name"], "Northcut")
        self.assertEqual(result["what"], "barbershop")

    def test_fast_never_fabricates_url(self):
        """FAST fallback never fabricates a WhatsApp URL from WHY text."""
        with patch.object(self.adapter, "_run_fast_programmatic") as mock_prog:
            mock_prog.return_value = HermesResult(success=False, error="fail")
            result = self.adapter.fast_interpret(
                "Northcut, barbershop, biar orang booking WA."
            )

        self.assertIsNone(result["why_destination"])
        self.assertNotIn("wa.me", result.get("why", ""))

    def test_fast_failure_boundary_on_import_error(self):
        """Hermes import failure returns HermesResult failure."""
        with patch("app.hermes.adapter._HERMES_IMPORT_ERROR", "no module"):
            result = self.adapter._run_fast_programmatic("test")

        self.assertFalse(result.success)
        self.assertIn("Failed to import Hermes oneshot", result.error)

    def test_fast_failure_boundary_on_provider_error(self):
        """Provider resolution failures return HermesResult failure."""
        with patch("app.hermes.adapter.AIAgent") as mock_agent_cls:
            self._patch_fast_runtime(mock_agent_cls)
            with patch(
                "app.hermes.adapter.resolve_runtime_provider",
                side_effect=RuntimeError("provider fail"),
            ):
                result = self.adapter._run_fast_programmatic("test")

        self.assertFalse(result.success)
        self.assertIn("provider fail", result.error)

    def test_fast_failure_boundary_on_skill_error(self):
        """Skill resolution failures return HermesResult failure."""
        with patch("app.hermes.adapter.AIAgent") as mock_agent_cls:
            self._patch_fast_runtime(mock_agent_cls)
            with patch(
                "app.hermes.adapter._build_preloaded_skills_prompt",
                side_effect=ValueError("Unknown skill(s): nope"),
            ):
                result = self.adapter._run_fast_programmatic(
                    "test", skills=["nope"]
                )

        self.assertFalse(result.success)
        self.assertIn("Unknown skill", result.error)

    def test_fast_failure_boundary_on_agent_construction_error(self):
        """Agent construction failures return HermesResult failure."""
        with patch("app.hermes.adapter.AIAgent") as mock_agent_cls:
            self._patch_fast_runtime(mock_agent_cls)
            mock_agent_cls.side_effect = RuntimeError("construction fail")
            result = self.adapter._run_fast_programmatic("test")

        self.assertFalse(result.success)
        self.assertIn("construction fail", result.error)

    def test_fast_failure_boundary_on_model_execution_error(self):
        """Model execution failures return HermesResult failure."""
        with patch("app.hermes.adapter.AIAgent") as mock_agent_cls:
            mocks = self._patch_fast_runtime(mock_agent_cls)
            mocks["agent_instance"].run_conversation.side_effect = RuntimeError(
                "model execution fail"
            )
            result = self.adapter._run_fast_programmatic("test")

        self.assertFalse(result.success)
        self.assertIn("model execution fail", result.error)

    def test_frontend_build_uses_cli_boundary(self):
        """FRONTEND uses the scripted CLI boundary with explicit toolsets."""
        workspace = Path(self.tmpdir) / "workspaces" / "proj-1"
        workspace.mkdir(parents=True)

        with patch.object(self.adapter, "_run_hermes_cli") as mock_cli:
            mock_cli.return_value = HermesResult(
                success=True,
                response='{"success": true}',
            )
            result = self.adapter.frontend_build(
                project_id="proj-1",
                brief={"name": "Northcut", "what": "barbershop", "why": "booking"},
                workspace=workspace,
            )

        self.assertTrue(result["success"])
        call_kwargs = mock_cli.call_args[1]
        self.assertEqual(call_kwargs["toolsets"], ["file", "terminal", "skills"])
        self.assertEqual(call_kwargs["cwd"], workspace)

    def test_frontend_build_loads_design_dna(self):
        """FRONTEND build loads Design DNA from workspace."""
        workspace = Path(self.tmpdir) / "workspaces" / "proj-2"
        workspace.mkdir(parents=True)

        dna = {"version": 1, "brand_personality": "premium"}
        dna_path = workspace / "design-dna.json"
        with dna_path.open("w") as f:
            json.dump(dna, f)

        with patch.object(self.adapter, "_run_hermes_cli") as mock_cli:
            mock_cli.return_value = HermesResult(
                success=True,
                response='{"success": true}',
            )
            result = self.adapter.frontend_build(
                project_id="proj-2",
                brief={"name": "Northcut"},
                workspace=workspace,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["design_dna"]["brand_personality"], "premium")

    def test_no_usage_file_flag(self):
        """Hermes invocation does NOT use --usage-file -."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout='{"scope":"WEBSITE"}', stderr=""
            )
            self.adapter._run_hermes_cli("test prompt")

        cmd = mock_run.call_args[0][0]
        self.assertNotIn("--usage-file", cmd)

    def test_stdout_is_final_response(self):
        """stdout from hermes -z is treated as the final response text."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="The final response text", stderr=""
            )
            result = self.adapter._run_hermes_cli("test prompt")

        self.assertEqual(result.response, "The final response text")
        self.assertTrue(result.success)


class TestSkillResolution(unittest.TestCase):
    """Test that skill names passed by FAST/FRONTEND are resolvable."""

    def test_fast_skill_names_are_valid(self):
        """FAST skill names follow Hermes skill naming conventions."""
        adapter = HermesAdapter(ProjectStateStore(Path(tempfile.mkdtemp())))
        # The skill names should be hyphenated lowercase strings
        fast_skills = ["website-builder-environment", "website-builder-product-scope"]
        for skill in fast_skills:
            self.assertTrue(skill.replace("-", "").isalnum())
            self.assertEqual(skill, skill.lower())

    def test_frontend_skill_names_are_valid(self):
        """FRONTEND skill names follow Hermes skill naming conventions."""
        frontend_skills = [
            "website-builder-environment",
            "website-builder-product-scope",
            "website-builder-design-dna",
        ]
        for skill in frontend_skills:
            self.assertTrue(skill.replace("-", "").isalnum())
            self.assertEqual(skill, skill.lower())

    def test_ui_ux_pro_max_not_duplicated(self):
        """UI UX Pro Max is reused, not duplicated in Website Builder skills."""
        skills_dir = Path(__file__).parent.parent / "skills"
        skill_names = [d.name for d in skills_dir.iterdir() if d.is_dir()]
        # UI UX Pro Max should NOT be in website-builder skills
        self.assertNotIn("ui-ux-pro-max", skill_names)
        self.assertNotIn("ui_ux_pro_max", skill_names)


if __name__ == "__main__":
    unittest.main()
