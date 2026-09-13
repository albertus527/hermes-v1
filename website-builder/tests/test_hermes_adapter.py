"""Tests for the Hermes adapter boundary.

Verifies the thin adapter uses the existing Hermes seams correctly.
"""

from __future__ import annotations

import json
import shutil
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

    def test_fast_prompt_encodes_name_what_why_sufficiency(self):
        """The FAST prompt contract encodes the NAME+WHAT+WHY readiness rule.

        This is the deterministic prompt/contract boundary: Hermes owns the
        semantic interpretation, but the application owns the contract text the
        interpreter is given. The prompt must state that NAME+WHAT+WHY is
        sufficient for DISCOVERY_READY and that missing CTA/contact/business
        facts are unresolved (never fabricated) and non-blocking.
        """
        prompt = self.adapter._build_fast_prompt(
            "Northcut, barbershop, biar orang booking WA"
        )

        # Sufficiency bar: NAME + WHAT + WHY proceeds.
        self.assertIn("NAME + WHAT + WHY", prompt)
        self.assertIn("DISCOVERY_READY", prompt)
        # Missing CTA/contact/business facts are non-blocking.
        self.assertIn("NOT blocking", prompt)
        self.assertIn("clarification_needed", prompt)
        # No-fabrication rule preserved.
        self.assertIn("Never fabricate", prompt)
        self.assertIn("why_destination", prompt)

    def test_fast_parse_northcut_semantic_contract(self):
        """Northcut FAST response parses to the DISCOVERY_READY contract.

        Regression lock for the real runtime case:
          "Northcut, barbershop, biar orang booking WA"
        NAME+WHAT+WHY are materially present; the WhatsApp number is an
        unresolved downstream fact, NOT a blocking clarification.
        """
        response = (
            '{"scope":"WEBSITE","name":"Northcut","what":"barbershop",'
            '"why":"let visitors book via WhatsApp","why_destination":null,'
            '"ambiguity":null,"clarification_needed":false,'
            '"clarification_question":null,"readiness":"DISCOVERY_READY"}'
        )
        result = self.adapter._parse_fast_response(response)

        self.assertEqual(result["scope"], "WEBSITE")
        self.assertEqual(result["name"], "Northcut")
        self.assertEqual(result["what"], "barbershop")
        self.assertEqual(result["why"], "let visitors book via WhatsApp")
        self.assertIsNone(result["why_destination"])
        self.assertFalse(result["clarification_needed"])
        self.assertIsNone(result["clarification_question"])
        self.assertEqual(result["readiness"], "DISCOVERY_READY")
        self.assertEqual(result["source"], "hermes_fast")

    def test_fast_parse_missing_what_why_still_needs_clarification(self):
        """A genuinely incomplete brief (missing WHAT/WHY) stays blocking."""
        response = (
            '{"scope":"WEBSITE","name":"Northcut","what":null,"why":null,'
            '"why_destination":null,"ambiguity":null,"clarification_needed":true,'
            '"clarification_question":"What is Northcut?",'
            '"readiness":"NEEDS_CLARIFICATION"}'
        )
        result = self.adapter._parse_fast_response(response)

        self.assertEqual(result["name"], "Northcut")
        self.assertIsNone(result["what"])
        self.assertIsNone(result["why"])
        self.assertTrue(result["clarification_needed"])
        self.assertEqual(result["clarification_question"], "What is Northcut?")
        self.assertEqual(result["readiness"], "NEEDS_CLARIFICATION")

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

    def test_frontend_build_requests_extended_timeout(self):
        """FRONTEND build requests the longer 900s timeout.

        Real FRONTEND design + implementation calls exceed the generic 300s
        default; the build must explicitly request the extended budget.
        """
        workspace = Path(self.tmpdir) / "workspaces" / "proj-timeout"
        workspace.mkdir(parents=True)

        with patch.object(self.adapter, "_run_hermes_cli") as mock_cli:
            mock_cli.return_value = HermesResult(
                success=True,
                response='{"success": true}',
            )
            self.adapter.frontend_build(
                project_id="proj-timeout",
                brief={"name": "Northcut", "what": "barbershop", "why": "booking"},
                workspace=workspace,
            )

        call_kwargs = mock_cli.call_args[1]
        self.assertEqual(call_kwargs["timeout_seconds"], 900)

    def test_run_hermes_cli_default_timeout_is_300(self):
        """Generic _run_hermes_cli retains the 300s default timeout."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout='{"success": true}', stderr=""
            )
            self.adapter._run_hermes_cli("test prompt")

        self.assertEqual(mock_run.call_args[1]["timeout"], 300)

    def test_run_hermes_cli_timeout_seconds_override(self):
        """_run_hermes_cli passes an explicit timeout_seconds to subprocess."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout='{"success": true}', stderr=""
            )
            self.adapter._run_hermes_cli("test prompt", timeout_seconds=900)

        self.assertEqual(mock_run.call_args[1]["timeout"], 900)

    def test_run_hermes_cli_timeout_error_reports_seconds(self):
        """Timeout failure message reflects the configured timeout."""
        import subprocess as _sp

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _sp.TimeoutExpired(cmd=["x"], timeout=900)
            result = self.adapter._run_hermes_cli("test prompt", timeout_seconds=900)

        self.assertFalse(result.success)
        self.assertIn("900s", result.error)
        self.assertEqual(result.exit_code, 124)

    def test_frontend_prompt_requires_source_implementation_after_design_dna(self):
        """FRONTEND prompt bounds design discovery and requires src/ implementation.

        Regression lock for the Phase 7 timeout: the prompt must make Design DNA
        a bounded step, not the finish line — design discovery is bounded, and
        the starter placeholder must actually be replaced before stopping.
        """
        import re

        workspace = Path(self.tmpdir) / "workspaces" / "proj-prompt"
        prompt = self.adapter._build_frontend_prompt(
            {"name": "Northcut", "what": "barbershop", "why": "booking"},
            workspace,
        )
        # Normalize whitespace so assertions are robust to line wrapping.
        flat = re.sub(r"\s+", " ", prompt)

        # Bounded design discovery.
        self.assertIn("Keep design discovery bounded", flat)
        self.assertIn("Choose a coherent direction quickly", flat)
        self.assertIn("design-dna.json alone does not complete this task", flat)
        # Immediate implementation after Design DNA.
        self.assertIn("IMMEDIATELY after writing design-dna.json", flat)
        self.assertIn("immediately implement the website", flat)
        # Placeholder replacement is the completion bar.
        self.assertIn(
            "starter placeholder in src/ has actually been replaced with the "
            "website implementation",
            flat,
        )
        self.assertIn("Do not stop after producing Design DNA", flat)
        # Application-owned npm checks preserved.
        self.assertIn("Do NOT run npm ci, npm run build, or npm run typecheck", flat)

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

    def test_oneshot_prompt_immediately_follows_z_flag(self):
        """Regression: the oneshot prompt is the immediate argument to -z.

        Hermes argparse defines `-z PROMPT` / `--oneshot PROMPT`, so the
        prompt MUST directly follow the flag. Previously the prompt was
        appended at the very end of argv, after --toolsets/--skills, which
        made argparse consume the next option as the oneshot value and fail
        with "argument -z/--oneshot: expected one argument".
        """
        prompt = "Build the Northcut barbershop landing page."
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout='{"success": true}', stderr=""
            )
            self.adapter._run_hermes_cli(
                prompt,
                model="test-model",
                provider="test-provider",
                toolsets=["file", "terminal", "skills"],
                skills=[
                    "website-builder-environment",
                    "website-builder-product-scope",
                    "website-builder-design-dna",
                ],
            )

        argv = mock_run.call_args[0][0]

        # The prompt is the immediate argument to -z.
        z_index = argv.index("-z")
        self.assertEqual(argv[z_index + 1], prompt)

        # Toolset/skill/provider/model flags remain present and correct.
        self.assertIn("--toolsets", argv)
        self.assertEqual(
            argv[argv.index("--toolsets") + 1], "file,terminal,skills"
        )
        self.assertEqual(argv.count("--skills"), 3)
        self.assertIn("website-builder-environment", argv)
        self.assertIn("website-builder-product-scope", argv)
        self.assertIn("website-builder-design-dna", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "test-model")
        self.assertEqual(argv[argv.index("--provider") + 1], "test-provider")

        # The prompt appears exactly once, right after -z (not at the tail).
        self.assertEqual(argv.count(prompt), 1)
        self.assertNotEqual(argv[-1], prompt)


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


class TestSkillSyncToProfile(unittest.TestCase):
    """Test that repo-local skills are synced to $HERMES_HOME/skills/."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

        # Create a fake repo with the three skill directories.
        self.repo_root = Path(self.tmpdir) / "repo"
        self.repo_skills = self.repo_root / ".hermes" / "skills"
        for name in (
            "website-builder-environment",
            "website-builder-product-scope",
            "website-builder-design-dna",
        ):
            skill_dir = self.repo_skills / name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: Test skill {name}.\n---\n\n# {name}\n",
                encoding="utf-8",
            )

        self.hermes_home = Path(self.tmpdir) / ".hermes-website"
        self.store = ProjectStateStore(Path(self.tmpdir) / "state")
        self.adapter = HermesAdapter(
            self.store,
            hermes_home=self.hermes_home,
            repo_root=self.repo_root,
        )

    def _profile_skill_dir(self, name: str) -> Path:
        return self.hermes_home / "skills" / name

    def test_sync_creates_profile_skills(self):
        """Skills are copied from repo to $HERMES_HOME/skills/."""
        self.adapter.sync_skills_to_profile()

        for name in self.adapter._PROFILE_SKILL_NAMES:
            skill_md = self._profile_skill_dir(name) / "SKILL.md"
            self.assertTrue(skill_md.is_file(), f"Missing: {skill_md}")
            content = skill_md.read_text(encoding="utf-8")
            self.assertIn(f"name: {name}", content)

    def test_sync_is_idempotent(self):
        """Second sync does not rewrite files (fingerprints match)."""
        self.adapter.sync_skills_to_profile()

        # Record mtimes after first sync.
        mtimes = {}
        for name in self.adapter._PROFILE_SKILL_NAMES:
            skill_md = self._profile_skill_dir(name) / "SKILL.md"
            mtimes[name] = skill_md.stat().st_mtime_ns

        # Second sync — should be a no-op.
        self.adapter.sync_skills_to_profile()

        for name in self.adapter._PROFILE_SKILL_NAMES:
            skill_md = self._profile_skill_dir(name) / "SKILL.md"
            self.assertEqual(
                skill_md.stat().st_mtime_ns,
                mtimes[name],
                f"{name} was rewritten on idempotent sync",
            )

    def test_sync_updates_stale_profile_skill(self):
        """Stale profile skill is overwritten when repo source changes."""
        self.adapter.sync_skills_to_profile()

        # Mutate the repo source.
        env_md = self.repo_skills / "website-builder-environment" / "SKILL.md"
        env_md.write_text(
            "---\nname: website-builder-environment\ndescription: Updated.\n---\n\n# Updated\n",
            encoding="utf-8",
        )

        self.adapter.sync_skills_to_profile()

        synced = self._profile_skill_dir("website-builder-environment") / "SKILL.md"
        self.assertIn("Updated", synced.read_text(encoding="utf-8"))

    def test_sync_preserves_existing_profile_skills(self):
        """Pre-existing profile-local skills (e.g. ui-ux-pro-max) are untouched."""
        # Simulate ui-ux-pro-max already installed in the profile.
        ux_dir = self.hermes_home / "skills" / "ui-ux-pro-max"
        ux_dir.mkdir(parents=True)
        (ux_dir / "SKILL.md").write_text(
            "---\nname: ui-ux-pro-max\ndescription: Design guidance.\n---\n\n# UI UX Pro Max\n",
            encoding="utf-8",
        )

        self.adapter.sync_skills_to_profile()

        # ui-ux-pro-max must still exist, unmodified.
        self.assertTrue((ux_dir / "SKILL.md").is_file())
        self.assertIn(
            "UI UX Pro Max",
            (ux_dir / "SKILL.md").read_text(encoding="utf-8"),
        )

    def test_sync_skips_missing_repo_source(self):
        """Missing repo skill dirs are skipped with a warning, not an error."""
        # Remove one repo skill.
        shutil.rmtree(self.repo_skills / "website-builder-design-dna")

        # Should not raise.
        self.adapter.sync_skills_to_profile()

        # The other two should still be synced.
        self.assertTrue(
            (self._profile_skill_dir("website-builder-environment") / "SKILL.md").is_file()
        )
        self.assertTrue(
            (self._profile_skill_dir("website-builder-product-scope") / "SKILL.md").is_file()
        )
        # The missing one should NOT exist.
        self.assertFalse(
            self._profile_skill_dir("website-builder-design-dna").exists()
        )

    def test_run_hermes_cli_calls_sync(self):
        """_run_hermes_cli triggers skill sync before spawning Hermes."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            self.adapter._run_hermes_cli("test")

        for name in self.adapter._PROFILE_SKILL_NAMES:
            self.assertTrue(
                (self._profile_skill_dir(name) / "SKILL.md").is_file(),
                f"{name} not synced by _run_hermes_cli",
            )

    def test_run_fast_programmatic_calls_sync(self):
        """_run_fast_programmatic triggers skill sync before agent construction."""
        with patch("app.hermes.adapter.AIAgent") as mock_agent_cls:
            self.adapter  # ensure adapter exists
            # Patch the runtime helpers so _run_fast_programmatic doesn't fail.
            cfg = {"model": {"default": "m", "provider": "p"}}
            with (
                patch("app.hermes.adapter.load_config", return_value=cfg),
                patch(
                    "app.hermes.adapter.resolve_runtime_provider",
                    return_value={
                        "api_key": "k",
                        "base_url": "https://x",
                        "provider": "p",
                        "requested_provider": "p",
                        "api_mode": "chat_completions",
                        "credential_pool": None,
                    },
                ),
                patch("app.hermes.adapter.get_fallback_chain", return_value=[]),
                patch(
                    "app.hermes.adapter._build_preloaded_skills_prompt",
                    return_value=None,
                ),
                patch(
                    "app.hermes.adapter._create_session_db_for_oneshot",
                    return_value=None,
                ),
            ):
                agent_instance = MagicMock()
                agent_instance.run_conversation.return_value = {
                    "final_response": "{}"
                }
                mock_agent_cls.return_value = agent_instance
                self.adapter._run_fast_programmatic("test")

        for name in self.adapter._PROFILE_SKILL_NAMES:
            self.assertTrue(
                (self._profile_skill_dir(name) / "SKILL.md").is_file(),
                f"{name} not synced by _run_fast_programmatic",
            )

    def test_dir_fingerprint_deterministic(self):
        """Fingerprint is stable for identical content."""
        fp1 = HermesAdapter._dir_fingerprint(
            self.repo_skills / "website-builder-environment"
        )
        fp2 = HermesAdapter._dir_fingerprint(
            self.repo_skills / "website-builder-environment"
        )
        self.assertEqual(fp1, fp2)
        self.assertTrue(len(fp1) > 0)

    def test_dir_fingerprint_empty_for_missing_dir(self):
        """Fingerprint of a non-existent directory is empty string."""
        fp = HermesAdapter._dir_fingerprint(Path(self.tmpdir) / "nonexistent")
        self.assertEqual(fp, "")


if __name__ == "__main__":
    unittest.main()
