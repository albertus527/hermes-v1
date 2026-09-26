"""Tests for the Hermes adapter boundary.

Verifies the thin adapter uses the existing Hermes seams correctly.
"""

from __future__ import annotations

import json
import shutil
import subprocess
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
            "import_error": patch("app.hermes.adapter._HERMES_IMPORT_ERROR", None),
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

    def test_configured_roles_execute_real_adapter(self):
        import sys
        from types import ModuleType
        routing = ModuleType("agent.image_routing")
        parts = [{"type": "text", "text": "references"},
                 {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
        routing.build_native_content_parts = MagicMock(return_value=(parts, []))
        routing._lookup_supports_vision = MagicMock(return_value=True)
        with patch("app.hermes.adapter.AIAgent") as agent_cls, patch.dict(sys.modules, {"agent.image_routing": routing}):
            mocks = self._patch_fast_runtime(agent_cls)
            mocks["load_config"].return_value = {
                "website_builder": {"models": {
                    role: {"model": role.lower() + "-model", "provider": "router"}
                    for role in ("FAST", "FRONTEND", "VISION")}},
                "model": {"default": "wrong-default"}}
            agent = mocks["agent_instance"]
            self.assertEqual(self.adapter.fast_interpret("brief")["source"], "hermes_fast")
            self.assertEqual(agent_cls.call_args.kwargs["model"], "fast-model")
            agent.run_conversation.return_value = {"final_response": json.dumps({"directions": [
                {"label": label, "descriptor": "calm spacing", "palette": {"primary": "#112233"}}
                for label in ("Calm", "Bold")]})}
            self.assertTrue(self.adapter.frontend_propose_directions({}, Path(self.tmpdir))["success"])
            self.assertEqual(agent_cls.call_args.kwargs["model"], "frontend-model")
            agent.run_conversation.return_value = {"final_response": '{"characteristics":{"UX":"clear navigation"}}'}
            self.assertTrue(self.adapter.vision_extract_references({"UX": Path("reference.png")})["success"])
            agent.run_conversation.assert_called_with(parts)
            self.assertEqual(agent_cls.call_args.kwargs["model"], "vision-model")
            self.assertEqual(agent_cls.call_args.kwargs["enabled_toolsets"], [])
            self.assertIsNone(agent_cls.call_args.kwargs["fallback_model"])
            mocks["resolve_runtime_provider"].assert_called_with(requested="router", target_model="vision-model", explicit_base_url=None)
            routing._lookup_supports_vision.return_value = None
            agent_cls.reset_mock()
            self.assertFalse(self.adapter.vision_extract_references({"UX": Path("reference.png")})["success"])
            agent_cls.assert_not_called()

    def test_reference_evidence_rejects_missing_extra_and_nonstring(self):
        for raw in ({}, {"UX": 1}, {"UX": " "}, {"UX": "ok", "COLOR": "extra"}):
            self.assertFalse(self.adapter._parse_reference_extraction_response(
                json.dumps({"characteristics": raw}), ["UX"])["success"])

    def test_missing_role_configuration_never_constructs_agent(self):
        with patch("app.hermes.adapter.AIAgent") as agent_cls:
            self._patch_fast_runtime(agent_cls)
            result = self.adapter.frontend_propose_directions({}, Path(self.tmpdir))
            self.assertFalse(result["success"])
            agent_cls.assert_not_called()

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

    def test_fast_parse_failure_falls_back_to_user_text_not_model_prose(self):
        """L-1: on FAST JSON parse failure the heuristic fallback derives the
        brief from the ORIGINAL USER TEXT, never from FAST's own response prose.

        Regression lock: previously _parse_fast_response passed the model's
        response string into _fallback_fast_interpret, so model-explanatory
        prose could populate user-brief fields (fabrication). Now the fallback
        re-derives from the user's message.
        """
        # FAST returns non-JSON prose that *mentions* a name the user never said.
        response = 'I think the user wants a site called MegaCorp Holdings Inc.'
        result = self.adapter._parse_fast_response(
            response, source_text="Northcut, barbershop, biar orang booking WA"
        )
        self.assertEqual(result["source"], "fallback_heuristic")
        # The brief must reflect the user's text, not the model's prose.
        self.assertEqual(result["name"], "Northcut")
        self.assertEqual(result["what"], "barbershop")
        self.assertNotIn("MegaCorp", json.dumps(result))

    def test_vision_wrong_schema_json_fails_closed(self):
        """H-1: a structurally valid JSON object with NONE of the contract keys
        must be treated as a VISION infrastructure failure (error set), never a
        clean 'no blocking findings' pass that would let an un-inspected page
        reach PREVIEW_READY."""
        for bad in ("{}", '{"summary":"looks ok"}', '{"error":"rate limited"}'):
            parsed = self.adapter._parse_vision_response(bad)
            self.assertFalse(parsed["pass"], bad)
            self.assertTrue(parsed.get("error"), bad)
            self.assertEqual(parsed["blocking"], [], bad)

    def test_vision_valid_schemas_still_parse(self):
        """Sanity: genuine new/legacy vision schemas still parse to a pass."""
        ok_new = '{"blocking": [], "observations": ["nice palette"], "summary": "ok"}'
        parsed = self.adapter._parse_vision_response(ok_new)
        self.assertTrue(parsed["pass"])
        self.assertIsNone(parsed.get("error"))
        ok_legacy = '{"critical": [], "major": [], "minor": ["tweak spacing"]}'
        parsed = self.adapter._parse_vision_response(ok_legacy)
        self.assertTrue(parsed["pass"])
        self.assertIsNone(parsed.get("error"))

    def test_vision_legacy_blocking_findings_still_fail(self):
        """Legacy critical/major findings remain blocking (not silently dropped)."""
        parsed = self.adapter._parse_vision_response('{"critical": ["broken hero"]}')
        self.assertFalse(parsed["pass"])
        self.assertIn("broken hero", parsed["blocking"])

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

    def test_frontend_build_is_supervised_not_wall_clock_limited(self):
        """FRONTEND runs under the activity-aware watchdog, not a fixed timer.

        Regression lock for the p10 failure: a fixed 900s wall clock killed a
        build that was still actively working. FRONTEND must now be supervised
        on activity, and must not request a fixed timeout at all.
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
        self.assertTrue(call_kwargs["supervise"])
        self.assertNotIn("timeout_seconds", call_kwargs)
        self.assertEqual(call_kwargs["project_id"], "proj-timeout")
        # Unchanged capability: FRONTEND keeps its tools and profile skills.
        self.assertEqual(call_kwargs["toolsets"], ["file", "terminal", "skills"])
        self.assertEqual(
            call_kwargs["skills"],
            [
                "website-builder-environment",
                "website-builder-product-scope",
                "website-builder-design-dna",
            ],
        )

    def test_frontend_build_forwards_build_operation_id(self):
        """The build operation id reaches the supervised invocation.

        Two invocations of the same project must be distinguishable, or one
        could refresh the other's watchdog.
        """
        workspace = Path(self.tmpdir) / "workspaces" / "proj-op"
        workspace.mkdir(parents=True)

        with patch.object(self.adapter, "_run_hermes_cli") as mock_cli:
            mock_cli.return_value = HermesResult(success=True, response="{}")
            self.adapter.frontend_build(
                project_id="proj-op",
                brief={"name": "Northcut"},
                workspace=workspace,
                build_operation_id="7",
            )

        self.assertEqual(mock_cli.call_args[1]["build_operation_id"], "7")

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


class TestFrontendTimeoutRecovery(unittest.TestCase):
    """Regression tests for Phase 7 timeout-recovery behavior."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = ProjectStateStore(Path(self.tmpdir) / "state")
        self.adapter = HermesAdapter(
            self.store,
            hermes_home=Path(self.tmpdir) / ".hermes-website",
            repo_root=Path(self.tmpdir) / "repo",
        )
        self.workspace = Path(self.tmpdir) / "workspaces" / "proj-timeout"
        self.workspace.mkdir(parents=True)

        # Create a fake repo starter so _has_complete_frontend_artifacts can
        # compare against it.
        starter_src = (
            self.adapter.repo_root
            / "templates"
            / "frontend-starter"
            / "src"
        )
        starter_src.mkdir(parents=True)
        self.starter_app = starter_src / "App.tsx"
        self.starter_app.write_text(
            "// starter placeholder\n",
            encoding="utf-8",
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_workspace_app(self, content: str) -> None:
        src = self.workspace / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "App.tsx").write_text(content, encoding="utf-8")

    def _write_workspace_dna(self, data: dict) -> None:
        (self.workspace / "design-dna.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

    def _invoke(self, result: HermesResult) -> dict:
        with patch.object(self.adapter, "_run_hermes_cli") as mock_cli:
            mock_cli.return_value = result
            return self.adapter.frontend_build(
                project_id="proj-timeout",
                brief={"name": "Northcut"},
                workspace=self.workspace,
            )

    def test_every_supervision_timeout_recovers_from_complete_artifacts(self):
        """Idle, hard-fuse, and degraded-legacy timeouts all recover.

        The artifact-recovery path is keyed on "this was a supervision
        timeout", not on one specific code, so replacing the fixed wall clock
        did not narrow it.
        """
        for code in (
            "FRONTEND_IDLE_TIMEOUT",
            "FRONTEND_HARD_TIMEOUT",
            "FRONTEND_LEGACY_TIMEOUT",
        ):
            with self.subTest(code=code):
                self.setUp()
                try:
                    self._write_workspace_dna({"version": 1, "brand_personality": "premium"})
                    self._write_workspace_app("// generated implementation\n")
                    result = self._invoke(
                        HermesResult(
                            success=False,
                            error=f"FRONTEND invocation {code}",
                            exit_code=124,
                            error_code=code,
                            timed_out=True,
                        )
                    )
                    self.assertTrue(result["success"], code)
                    self.assertEqual(
                        result["design_dna"]["brand_personality"], "premium"
                    )
                finally:
                    self.tearDown()

    def test_normal_nonzero_exit_does_not_recover_from_artifacts(self):
        """A plain nonzero exit is a different failure class from a timeout.

        Complete artifacts on disk must not launder a genuine FRONTEND failure
        into a success; only a supervision timeout may do that.
        """
        self._write_workspace_dna({"version": 1, "brand_personality": "premium"})
        self._write_workspace_app("// generated implementation\n")

        result = self._invoke(
            HermesResult(
                success=False,
                error="provider returned 500",
                exit_code=1,
            )
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "provider returned 500")

    def test_timeout_with_incomplete_artifacts_fails_with_its_code(self):
        """Incomplete artifacts + timeout -> failure carrying the timeout code."""
        self._write_workspace_app("// generated implementation\n")  # no design-dna.json

        result = self._invoke(
            HermesResult(
                success=False,
                error="FRONTEND invocation FRONTEND_HARD_TIMEOUT after 2700.0s",
                exit_code=124,
                error_code="FRONTEND_HARD_TIMEOUT",
                timed_out=True,
            )
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "FRONTEND_HARD_TIMEOUT")

    def test_timeout_recovery_success(self):
        """Timeout + valid artifacts -> recoverable FRONTEND completion."""
        self._write_workspace_dna({"version": 1, "brand_personality": "premium"})
        self._write_workspace_app("// generated implementation\n")

        with patch.object(self.adapter, "_run_hermes_cli") as mock_cli:
            mock_cli.return_value = HermesResult(
                success=False,
                error="FRONTEND invocation FRONTEND_IDLE_TIMEOUT after 180.0s",
                exit_code=124,
                error_code="FRONTEND_IDLE_TIMEOUT",
                timed_out=True,
            )
            result = self.adapter.frontend_build(
                project_id="proj-timeout",
                brief={"name": "Northcut"},
                workspace=self.workspace,
            )

        self.assertTrue(result["success"])
        self.assertIsNotNone(result["design_dna"])
        self.assertEqual(result["design_dna"]["brand_personality"], "premium")
        self.assertIsNone(result["error"])

    def test_timeout_recovery_missing_design_dna(self):
        """Timeout + missing design-dna.json -> FAILED."""
        self._write_workspace_app("// generated implementation\n")

        with patch.object(self.adapter, "_run_hermes_cli") as mock_cli:
            mock_cli.return_value = HermesResult(
                success=False,
                error="FRONTEND invocation FRONTEND_IDLE_TIMEOUT after 180.0s",
                exit_code=124,
                error_code="FRONTEND_IDLE_TIMEOUT",
                timed_out=True,
            )
            result = self.adapter.frontend_build(
                project_id="proj-timeout",
                brief={"name": "Northcut"},
                workspace=self.workspace,
            )

        self.assertFalse(result["success"])
        self.assertIn("FRONTEND_IDLE_TIMEOUT", result["error"])
        self.assertEqual(result["error_code"], "FRONTEND_IDLE_TIMEOUT")

    def test_timeout_recovery_unchanged_starter(self):
        """Timeout + App.tsx identical to starter -> FAILED."""
        self._write_workspace_dna({"version": 1})
        self._write_workspace_app("// starter placeholder\n")

        with patch.object(self.adapter, "_run_hermes_cli") as mock_cli:
            mock_cli.return_value = HermesResult(
                success=False,
                error="FRONTEND invocation FRONTEND_IDLE_TIMEOUT after 180.0s",
                exit_code=124,
                error_code="FRONTEND_IDLE_TIMEOUT",
                timed_out=True,
            )
            result = self.adapter.frontend_build(
                project_id="proj-timeout",
                brief={"name": "Northcut"},
                workspace=self.workspace,
            )

        self.assertFalse(result["success"])
        self.assertIn("FRONTEND_IDLE_TIMEOUT", result["error"])
        self.assertEqual(result["error_code"], "FRONTEND_IDLE_TIMEOUT")

    def test_timeout_recovery_invalid_design_dna_json(self):
        """Timeout + malformed design-dna.json -> FAILED."""
        (self.workspace / "design-dna.json").write_text(
            "{invalid json", encoding="utf-8"
        )
        self._write_workspace_app("// generated implementation\n")

        with patch.object(self.adapter, "_run_hermes_cli") as mock_cli:
            mock_cli.return_value = HermesResult(
                success=False,
                error="FRONTEND invocation FRONTEND_IDLE_TIMEOUT after 180.0s",
                exit_code=124,
                error_code="FRONTEND_IDLE_TIMEOUT",
                timed_out=True,
            )
            result = self.adapter.frontend_build(
                project_id="proj-timeout",
                brief={"name": "Northcut"},
                workspace=self.workspace,
            )

        self.assertFalse(result["success"])
        self.assertIn("FRONTEND_IDLE_TIMEOUT", result["error"])
        self.assertEqual(result["error_code"], "FRONTEND_IDLE_TIMEOUT")

    def test_non_timeout_failure_not_recovered(self):
        """Non-timeout Hermes failure must NOT enter timeout recovery."""
        self._write_workspace_dna({"version": 1})
        self._write_workspace_app("// generated implementation\n")

        with patch.object(self.adapter, "_run_hermes_cli") as mock_cli:
            mock_cli.return_value = HermesResult(
                success=False,
                error="Authentication failed",
                exit_code=1,
            )
            result = self.adapter.frontend_build(
                project_id="proj-auth-fail",
                brief={"name": "Northcut"},
                workspace=self.workspace,
            )

        self.assertFalse(result["success"])
        self.assertIn("Authentication failed", result["error"])

    def test_normal_success_path_unchanged(self):
        """Normal Hermes success path remains unchanged."""
        self._write_workspace_dna({"version": 1, "brand_personality": "minimal"})
        self._write_workspace_app("// generated implementation\n")

        with patch.object(self.adapter, "_run_hermes_cli") as mock_cli:
            mock_cli.return_value = HermesResult(
                success=True,
                response='{"success": true}',
            )
            result = self.adapter.frontend_build(
                project_id="proj-normal",
                brief={"name": "Northcut"},
                workspace=self.workspace,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["design_dna"]["brand_personality"], "minimal")
        self.assertIsNone(result["error"])


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


class TestFrontendForensicWiring(unittest.TestCase):
    """The adapter hands the supervisor everything the receipt needs.

    Observation only: this wiring changes no prompt, toolset, skill, timeout, or
    artifact-recovery behaviour, and it is the reason the next 45-minute run
    can be classified after the fact.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = ProjectStateStore(Path(self.tmpdir) / "state")
        self.adapter = HermesAdapter(
            self.store,
            hermes_home=Path(self.tmpdir) / ".hermes-website",
            repo_root=Path(self.tmpdir) / "repo",
        )
        self.workspace = Path(self.tmpdir) / "workspaces" / "proj-forensics"
        (self.workspace / "src").mkdir(parents=True)
        self.starter_app = (
            self.adapter.repo_root / "templates" / "frontend-starter" / "src" / "App.tsx"
        )
        self.starter_app.parent.mkdir(parents=True)
        self.starter_app.write_text("// starter placeholder\n", encoding="utf-8")
        self.captured: dict = {}

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _supervise(self, run_result=None):
        """Call the supervised boundary with a stubbed supervisor."""
        from app.hermes import watchdog as wd

        self.captured.clear()

        def _fake_supervise(cmd, **kwargs):
            self.captured.update(kwargs)
            return run_result or wd.SupervisedRun(
                returncode=0, stdout="{}", stderr="", outcome=None, diagnostics={}
            )

        with patch.object(wd, "supervise_frontend_run", _fake_supervise):
            self.adapter._run_hermes_cli_supervised(
                ["python", "-m", "hermes_cli.main", "-z", "p"],
                cwd=self.workspace,
                env={},
                project_id="proj-forensics",
                build_operation_id="op-7",
            )
        return self.captured

    def test_supervisor_receives_receipt_destination_and_workspace(self):
        """The destination is a sibling of runs/, and the workspace is the cwd."""
        from app.hermes import watchdog as wd

        captured = self._supervise()

        self.assertEqual(
            captured["diagnostics_dir"],
            self.adapter.hermes_home / "diagnostics" / "proj-forensics",
        )
        # A sibling of runs/, not a child: runs_dir is removed by the caller.
        self.assertNotIn(
            self.adapter.hermes_home / "runs", captured["diagnostics_dir"].parents
        )
        self.assertEqual(captured["workspace"], self.workspace)
        self.assertEqual(captured["progress_path"].name, "progress.jsonl")

    def test_receipt_directory_is_per_project(self):
        """Receipts are pruned per project, so the destination must be per project.

        Two projects must not share a directory, or one project's oldest
        receipts would be pruned away by the other's activity.
        """
        from app.hermes import watchdog as wd

        seen = []
        with patch.object(
            wd,
            "supervise_frontend_run",
            lambda cmd, **kw: seen.append(kw["diagnostics_dir"])
            or wd.SupervisedRun(0, "", ""),
        ):
            for project_id in ("p-one", "p-two"):
                self.adapter._run_hermes_cli_supervised(
                    ["x"], cwd=self.workspace, env={}, project_id=project_id,
                    build_operation_id="1",
                )

        self.assertEqual(len(set(seen)), 2)
        self.assertEqual(
            seen,
            [
                self.adapter.hermes_home / "diagnostics" / "p-one",
                self.adapter.hermes_home / "diagnostics" / "p-two",
            ],
        )

    def test_artifacts_probe_reflects_real_completeness(self):
        """The probe is the existing check, not a second opinion about it."""
        from app.hermes import watchdog as wd

        captured = self._supervise()
        probe = captured["artifacts_probe"]
        self.assertTrue(callable(probe))

        # Nothing produced yet.
        self.assertFalse(probe())

        # Complete artifacts: real Design DNA, and App.tsx no longer the starter.
        (self.workspace / "design-dna.json").write_text('{"v": 1}', encoding="utf-8")
        (self.workspace / "src" / "App.tsx").write_text(
            "export const App = () => null;", encoding="utf-8"
        )
        self.assertTrue(probe())

        # The untouched starter is exactly what the real check rejects.
        (self.workspace / "src" / "App.tsx").write_text(
            "// starter placeholder\n", encoding="utf-8"
        )
        self.assertFalse(probe())

    def test_artifacts_probe_is_fail_safe(self):
        """A raising completeness check must not break supervision."""
        from app.hermes import watchdog as wd

        captured = self._supervise()
        with patch.object(
            self.adapter,
            "_has_complete_frontend_artifacts",
            side_effect=RuntimeError("boom"),
        ):
            self.assertFalse(captured["artifacts_probe"]())
        self.assertEqual(
            captured["diagnostics_dir"],
            self.adapter.hermes_home / "diagnostics" / "proj-forensics",
        )

    def test_degraded_legacy_timeout_still_records_channel_state(self):
        """When the watchdog is unavailable, the fallback still leaves a record."""
        from app.hermes import watchdog as wd

        with patch.object(
            wd, "supervise_frontend_run", side_effect=wd.WatchdogUnavailable("no kill")
        ):
            with patch.object(
                wd, "resolve_watchdog_policy"
            ) as policy:
                policy.return_value = wd.WatchdogPolicy(legacy_wallclock_seconds=1.0)
                with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("x", 1)):
                    result = self.adapter._run_hermes_cli_supervised(
                        ["x"], cwd=self.workspace, env={},
                        project_id="proj-forensics", build_operation_id="op-7",
                    )

        self.assertTrue(result.timed_out)
        self.assertEqual(result.error_code, "FRONTEND_LEGACY_TIMEOUT")
        self.assertFalse(result.invocation["channel_confirmed"])
        self.assertEqual(result.invocation["outcome"], "FRONTEND_LEGACY_TIMEOUT")
        # The user-facing reply is unchanged: no forensics in the error text.
        self.assertEqual(
            result.error, "FRONTEND invocation FRONTEND_LEGACY_TIMEOUT"
        )

    def test_legacy_path_without_a_probe_is_untouched(self):
        """Non-FRONTEND roles and pre-watchdog callers pass no probe at all."""
        from app.hermes import watchdog as wd

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("x", 1)):
            result = self.adapter._run_hermes_cli_legacy(
                ["x"], self.workspace, {}, timeout_seconds=1
            )

        self.assertTrue(result.timed_out)
        self.assertIsNone(result.invocation)
        self.assertEqual(
            result.error, "FRONTEND invocation FRONTEND_LEGACY_TIMEOUT"
        )


if __name__ == "__main__":
    unittest.main()
