"""A role named by alias must reach the runtime as a real model id.

Hermes' oneshot path resolves ``model_aliases`` only when no ``--provider`` was
passed. The adapter always passes one, so an aliased role reached the runtime
verbatim: ``agent.model`` became the alias NAME. Every metadata lookup then
matched on a name no catalog contains, so the context window fell back to the
256K default and logged a warning no operator could act on — for a model whose
real id was sitting in the profile's own alias table.

Fixed on the website-builder side, not in ``hermes_cli``: the adapter resolves
the alias and puts a real model id on argv, so the child's own resolution is
never reached for a role. Preflight then fails closed when an alias resolves to
something Hermes has no context metadata for, because that is the same 256K
fallback arriving 45 minutes later instead of now.

No live LLM, browser, or npm is required.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.hermes.adapter import (
    HermesAdapter,
    _configured_context_length,
    _model_has_static_context_metadata,
    _role_model_alias_target,
)


PROFILE = """\
model:
  default: z-ai/glm-5.3-flash
  provider: openrouter
%(context_length)s%(aliases)swebsite_builder:
  models:
    FAST:
      model: %(fast)s
      provider: openrouter
    FRONTEND:
      model: %(frontend)s
      provider: openrouter
    VISION:
      model: %(vision)s
      provider: openrouter
"""

# A catalogued model: "glm-5.3" is a DEFAULT_CONTEXT_LENGTHS key, so anything
# containing it resolves to a real window.
CATALOGUED_ALIAS_TARGET = "z-ai/glm-5.3-flash"
UNCATALOGUED_ALIAS_TARGET = "acme/private-v9"
ALIAS_NAME = "frontend"


def _profile(*, aliases=None, fast=None, frontend=None, vision=None,
             context_length=None):
    """A profile whose roles name models by alias (the default) or literally."""
    alias_block = ""
    if aliases:
        lines = [
            "model_aliases:",
            *[f"  {name}: {{model: {target}}}" for name, target in aliases.items()],
        ]
        alias_block = "\n".join(lines) + "\n"
    role_default = ALIAS_NAME if aliases else None
    return PROFILE % {
        "fast": fast or role_default,
        "frontend": frontend or role_default,
        "vision": vision or role_default,
        "aliases": alias_block,
        "context_length": f"  context_length: {context_length}\n" if context_length else "",
    }


class TestAliasResolution(unittest.TestCase):
    def test_a_catalogued_alias_resolves_to_its_model_id(self):
        cfg = {"model_aliases": {"frontend": {"model": "z-ai/glm-5.3-flash"}}}

        model, was_alias = _role_model_alias_target(cfg, "frontend")

        self.assertEqual(model, "z-ai/glm-5.3-flash")
        self.assertTrue(was_alias)

    def test_lookup_is_case_and_whitespace_insensitive(self):
        """The runtime's own alias table lowercases keys; so must this."""
        cfg = {"model_aliases": {"frontend": {"model": "z-ai/glm-5.3-flash"}}}

        self.assertEqual(_role_model_alias_target(cfg, "  FrontEnd ")[0], "z-ai/glm-5.3-flash")

    def test_a_literal_model_is_never_treated_as_an_alias(self):
        cfg = {"model_aliases": {"frontend": {"model": "z-ai/glm-5.3-flash"}}}

        model, was_alias = _role_model_alias_target(cfg, "z-ai/glm-5.3")

        self.assertEqual(model, "z-ai/glm-5.3")
        self.assertFalse(was_alias)

    def test_an_unusable_alias_table_is_a_no_op_not_a_crash(self):
        for cfg in (
            {},
            {"model_aliases": None},
            {"model_aliases": []},
            {"model_aliases": {"frontend": "z-ai/glm-5.3-flash"}},
            {"model_aliases": {"frontend": {"model": ""}}},
            {"model_aliases": {"frontend": {"model": 7}}},
        ):
            with self.subTest(cfg=cfg):
                self.assertEqual(_role_model_alias_target(cfg, "frontend"),
                                 ("frontend", False))

    def test_the_alias_provider_never_repoints_the_role(self):
        """The role's provider is a separate, deliberate declaration."""
        cfg = {"model_aliases": {"frontend": {"model": "z-ai/glm-5.3-flash",
                                               "provider": "somewhere-else"}}}

        self.assertEqual(_role_model_alias_target(cfg, "frontend")[0], "z-ai/glm-5.3-flash")


class TestStaticContextMetadata(unittest.TestCase):
    def test_a_versioned_glm_id_is_recognised(self):
        self.assertTrue(_model_has_static_context_metadata("z-ai/glm-5.3-flash"))
        self.assertTrue(_model_has_static_context_metadata("glm-5.3"))

    def test_an_alias_name_is_not_a_model(self):
        """The p20 condition: a real alias NAME sizes nothing."""
        self.assertFalse(_model_has_static_context_metadata("frontend"))

    def test_an_explicit_context_length_counts_as_known_metadata(self):
        self.assertTrue(_configured_context_length({"model": {"context_length": 1_048_576}}))
        for cfg in (
            {},
            {"model": None},
            {"model": "z-ai/glm-5.3"},
            {"model": {"context_length": 0}},
            {"model": {"context_length": "1048576"}},
            {"model": {"context_length": True}},
        ):
            with self.subTest(cfg=cfg):
                self.assertFalse(_configured_context_length(cfg))


class TestRoleLaunchUsesTheResolvedModel(unittest.TestCase):
    """The argv must carry a model id, not the alias the operator wrote."""

    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmpdir_obj.name)
        self.home = self.tmpdir / ".hermes-website"
        (self.home / "skills").mkdir(parents=True)
        self.adapter = HermesAdapter(
            _store(self.tmpdir), hermes_home=self.home, repo_root=self.tmpdir / "repo"
        )

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    def _argv(self, profile):
        (self.home / "config.yaml").write_text(profile, encoding="utf-8")
        captured = {}

        def _run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            return _CompletedProcess()

        with patch.object(self.adapter, "sync_skills_to_profile"), \
                patch("app.hermes.adapter.subprocess.run", side_effect=_run), \
                patch("app.hermes.adapter.credentials.assert_no_privileged"), \
                patch("app.hermes.adapter.credentials.assert_profile_dotenv_clean"), \
                patch("app.hermes.adapter.credentials.assert_profile_home_clean"), \
                patch("app.hermes.adapter.credentials.agent_env", return_value={}):
            self.adapter._run_hermes_cli(prompt="p", role="FRONTEND")

        return captured["cmd"]

    def _flag(self, cmd, name):
        return cmd[cmd.index(name) + 1]

    def test_an_aliased_role_puts_a_real_model_id_on_argv(self):
        cmd = self._argv(_profile(aliases={ALIAS_NAME: CATALOGUED_ALIAS_TARGET}))

        self.assertEqual(self._flag(cmd, "--model"), CATALOGUED_ALIAS_TARGET)
        self.assertEqual(self._flag(cmd, "--provider"), "openrouter")

    def test_a_literal_role_model_is_passed_through_unchanged(self):
        cmd = self._argv(_profile(frontend="z-ai/glm-5.3"))

        self.assertEqual(self._flag(cmd, "--model"), "z-ai/glm-5.3")


class TestPreflightFailsClosedOnAliasMetadata(unittest.TestCase):
    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmpdir_obj.name)
        self.home = self.tmpdir / ".hermes-website"
        (self.home / "skills").mkdir(parents=True)
        self.adapter = HermesAdapter(
            _store(self.tmpdir), hermes_home=self.home, repo_root=self.tmpdir / "repo"
        )

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    def _write(self, **kwargs):
        (self.home / "config.yaml").write_text(_profile(**kwargs), encoding="utf-8")

    def _preflight(self):
        runtime = {
            "api_key": "k", "provider": "openrouter", "requested_provider": "openrouter",
            "base_url": "https://example.invalid", "api_mode": "chat_completions",
        }
        with patch("app.hermes.adapter.resolve_runtime_provider", return_value=runtime), \
                patch.object(HermesAdapter, "_role_vision_support", return_value=True):
            return self.adapter._validate_role_configuration()

    def test_an_uncatalogued_alias_target_fails_closed(self):
        """The p20 condition, caught before the run instead of during it."""
        self._write(aliases={ALIAS_NAME: UNCATALOGUED_ALIAS_TARGET})

        report = self._preflight()

        self.assertFalse(report["ok"], report)
        self.assertIn("FRONTEND", report["errors"])
        self.assertIn("context metadata", report["errors"]["FRONTEND"])

    def test_a_catalogued_alias_target_passes(self):
        self._write(aliases={ALIAS_NAME: CATALOGUED_ALIAS_TARGET})

        report = self._preflight()

        self.assertTrue(report["ok"], report)
        self.assertEqual(report["roles"]["FRONTEND"]["resolved_model"],
                         CATALOGUED_ALIAS_TARGET)

    def test_an_explicit_context_length_is_the_escape_hatch(self):
        """What the runtime's own fallback warning tells the operator to set."""
        self._write(aliases={ALIAS_NAME: UNCATALOGUED_ALIAS_TARGET},
                    context_length=1_048_576)

        report = self._preflight()

        self.assertTrue(report["ok"], report)

    def test_a_literal_uncatalogued_model_is_not_gated(self):
        """Out of scope by design: an unknown custom model is legitimate config."""
        self._write(frontend=UNCATALOGUED_ALIAS_TARGET, fast="z-ai/glm-5.3",
                    vision="z-ai/glm-5.3")

        report = self._preflight()

        self.assertTrue(report["ok"], report)


class TestPreflightResolvesAliasesBeforeReachingTheRuntime(unittest.TestCase):
    """Preflight must prove the model the RUNTIME will launch, not the alias.

    Both launch paths — the supervised CLI boundary and the programmatic
    ``_run_fast_programmatic`` — resolve ``model_aliases`` before the model
    reaches ``resolve_runtime_provider`` or the vision capability lookup.
    Preflight computed the same resolved id for its metadata gate but then
    handed the RAW ALIAS to both of those seams, so the preflight proof was
    about a name no catalog contains while the run used a real model id. That
    divergence is a wrong answer in both directions: preflight could fail a
    resolvable model, or pass one whose real model cannot resolve or lacks
    image input.
    """

    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmpdir_obj.name)
        self.home = self.tmpdir / ".hermes-website"
        (self.home / "skills").mkdir(parents=True)
        self.adapter = HermesAdapter(
            _store(self.tmpdir), hermes_home=self.home, repo_root=self.tmpdir / "repo"
        )

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    def _write(self, **kwargs):
        (self.home / "config.yaml").write_text(_profile(**kwargs), encoding="utf-8")

    def _runtime(self):
        return {
            "api_key": "k", "provider": "openrouter", "requested_provider": "openrouter",
            "base_url": "https://example.invalid", "api_mode": "chat_completions",
        }

    def _preflight(self):
        """Run preflight, recording every model each seam actually received."""
        resolved: list = []
        vision: list = []

        def _resolve(**kwargs):
            resolved.append(kwargs.get("target_model"))
            return self._runtime()

        def _vision(_runtime_arg, model, _cfg):
            vision.append(model)
            return True

        with patch("app.hermes.adapter.resolve_runtime_provider", side_effect=_resolve), \
                patch.object(HermesAdapter, "_role_vision_support", side_effect=_vision):
            report = self.adapter._validate_role_configuration()

        return report, resolved, vision

    def test_no_role_reaches_runtime_resolution_with_the_literal_alias(self):
        self._write(aliases={ALIAS_NAME: CATALOGUED_ALIAS_TARGET})

        report, resolved, _ = self._preflight()

        self.assertTrue(report["ok"], report)
        self.assertTrue(resolved, "preflight resolved no role at all")
        for target in resolved:
            with self.subTest(target=target):
                self.assertNotEqual(target, ALIAS_NAME)
                self.assertEqual(target, CATALOGUED_ALIAS_TARGET)

    def test_aliased_vision_capability_lookup_receives_the_resolved_model(self):
        self._write(aliases={ALIAS_NAME: CATALOGUED_ALIAS_TARGET})

        _, _, vision = self._preflight()

        self.assertTrue(vision, "VISION capability lookup never ran")
        for model in vision:
            with self.subTest(model=model):
                self.assertEqual(model, CATALOGUED_ALIAS_TARGET)
                self.assertNotEqual(model, ALIAS_NAME)

    def test_a_literal_role_model_still_reaches_the_runtime_verbatim(self):
        """The fix must not resolve or rewrite a role that names a model."""
        self._write(frontend="z-ai/glm-5.3", fast="z-ai/glm-5.3", vision="z-ai/glm-5.3")

        _, resolved, vision = self._preflight()

        self.assertEqual(resolved, ["z-ai/glm-5.3"] * 3)
        self.assertEqual(vision, ["z-ai/glm-5.3"])

    def test_configured_alias_is_retained_for_diagnostics(self):
        """`model` is config identity and must still read as the operator wrote it."""
        self._write(aliases={ALIAS_NAME: CATALOGUED_ALIAS_TARGET})

        report, _, _ = self._preflight()

        self.assertEqual(report["roles"]["FRONTEND"]["model"], ALIAS_NAME)
        self.assertEqual(report["roles"]["FRONTEND"]["resolved_model"],
                         CATALOGUED_ALIAS_TARGET)


class TestLaunchPathsAreUnaffectedByThePreflightFix(unittest.TestCase):
    """Preflight is a gate, not a launch seam. Both launch paths must not move."""

    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmpdir_obj.name)
        self.home = self.tmpdir / ".hermes-website"
        (self.home / "skills").mkdir(parents=True)
        self.adapter = HermesAdapter(
            _store(self.tmpdir), hermes_home=self.home, repo_root=self.tmpdir / "repo"
        )

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    def test_the_cli_boundary_argv_is_unchanged_for_an_aliased_role(self):
        (self.home / "config.yaml").write_text(
            _profile(aliases={ALIAS_NAME: CATALOGUED_ALIAS_TARGET}), encoding="utf-8"
        )
        captured = {}

        def _run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            return _CompletedProcess()

        with patch.object(self.adapter, "sync_skills_to_profile"), \
                patch("app.hermes.adapter.subprocess.run", side_effect=_run), \
                patch("app.hermes.adapter.credentials.assert_no_privileged"), \
                patch("app.hermes.adapter.credentials.assert_profile_dotenv_clean"), \
                patch("app.hermes.adapter.credentials.assert_profile_home_clean"), \
                patch("app.hermes.adapter.credentials.agent_env", return_value={}):
            self.adapter._run_hermes_cli(prompt="p", role="FRONTEND")

        cmd = captured["cmd"]
        self.assertEqual(cmd[cmd.index("--model") + 1], CATALOGUED_ALIAS_TARGET)
        self.assertEqual(cmd[cmd.index("--provider") + 1], "openrouter")

    def test_the_programmatic_path_still_resolves_an_alias_for_the_agent(self):
        """FAST/VISION launch behaviour is independent of preflight."""
        (self.home / "config.yaml").write_text(
            _profile(aliases={ALIAS_NAME: CATALOGUED_ALIAS_TARGET}), encoding="utf-8"
        )

        with patch.object(self.adapter, "sync_skills_to_profile"), \
                patch("app.hermes.adapter.resolve_runtime_provider",
                      return_value=self._runtime()), \
                patch.object(HermesAdapter, "_role_vision_support", return_value=True), \
                patch("app.hermes.adapter.AIAgent") as constructor:
            constructor.return_value.run_conversation.return_value = {
                "final_response": "ok"
            }
            result = self.adapter._run_fast_programmatic("test", role="FAST")

        self.assertTrue(result.success, result.error)
        self.assertEqual(constructor.call_args.kwargs["model"], CATALOGUED_ALIAS_TARGET)
        self.assertEqual(constructor.call_args.kwargs["enabled_toolsets"], [])

    def test_the_programmatic_vision_gate_still_receives_the_resolved_model(self):
        (self.home / "config.yaml").write_text(
            _profile(aliases={ALIAS_NAME: CATALOGUED_ALIAS_TARGET}), encoding="utf-8"
        )
        seen: list = []

        def _vision(_runtime_arg, model, _cfg):
            seen.append(model)
            return True

        with patch.object(self.adapter, "sync_skills_to_profile"), \
                patch("app.hermes.adapter.resolve_runtime_provider",
                      return_value=self._runtime()), \
                patch.object(HermesAdapter, "_role_vision_support", side_effect=_vision), \
                patch("app.hermes.adapter.AIAgent") as constructor:
            constructor.return_value.run_conversation.return_value = {
                "final_response": "ok"
            }
            result = self.adapter._run_fast_programmatic(
                "test", role="VISION", require_vision=True
            )

        self.assertTrue(result.success, result.error)
        self.assertEqual(seen, [CATALOGUED_ALIAS_TARGET])

    def _runtime(self):
        return {
            "api_key": "k", "provider": "openrouter", "requested_provider": "openrouter",
            "base_url": "https://example.invalid", "api_mode": "chat_completions",
        }


def _store(tmpdir):
    from app.core.state import ProjectStateStore

    return ProjectStateStore(tmpdir / "state")


class _CompletedProcess:
    returncode = 0
    stdout = '{"success": true}'
    stderr = ""


if __name__ == "__main__":  # pragma: no cover
    unittest.main()