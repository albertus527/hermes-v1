"""Batch D3a: bounded, project-local dependency execution.

Real temporary project workspaces, a real recording runner, real
``package.json`` files. **No subprocess runs and no network call is made**: the
runner is a recorder, so every assertion is about the argv the application WOULD
run, the state it would report, and -- critically -- about what it refuses to
run at all.

The properties under test are BEHAVIOUR CONTRACTS:

    * ``installed`` requires an observed manifest change, never a command exit
    * a failed install never reports ``installed``
    * a package name is unreachable unless it is allowlisted in code
    * an unselected dependency produces NO command
    * nothing is installed globally

The allowlist tests are load-bearing. "A model must not be able to request
``npm install <arbitrary>``" is only true if resolving an arbitrary string is
impossible, so those tests feed model-shaped and corpus-shaped strings into the
resolver and assert ``None``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_install import (
    ALLOWED_SHADCN_COMPONENTS,
    DEPENDENCY_PACKAGES,
    INSTALL_FAILED,
    INSTALL_STATES,
    REGISTRY_DEPENDENCY,
    SHADCN_CLI_VERSION,
    TERMINAL_INSTALL_STATES,
    DesignDependencyInstaller,
    build_install_argv,
    build_registry_argv,
    detect_package_manager,
    filter_allowed_components,
    is_contained,
    package_name_is_well_formed,
    project_declares_dependency,
    resolve_package,
)
from app.core.design_resources import load_design_resource_manifest
from app.core.design_selection import select_design_resources

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_MANIFEST = REPO_ROOT / "website-builder" / "config" / "design_resources.yaml"

GSAP = "gsap"
THREE = "three"
LENIS = "lenis"
SHADCN = "shadcn"


class RecordingRunner:
    """Stands in for ``ProjectRunner`` and records every argv.

    Deliberately NOT a real runner: these tests are about the command the
    application chooses, so a real subprocess would prove less and cost a network
    round trip. Containment and credential isolation are properties of the real
    ``ProjectRunner`` (covered by the R2 credential-isolation suite); what is
    asserted here is that D3a uses that runner and builds argv itself.
    """

    def __init__(self, *, exitcode: int = 0, writes_manifest: bool = True):
        self.commands: List[List[str]] = []
        self.cwds: List[Path] = []
        self._exitcode = exitcode
        self._writes_manifest = writes_manifest

    def run_command(self, project_id, command, cwd=None, env=None, timeout=300.0):
        self.commands.append(list(command))
        self.cwds.append(Path(cwd) if cwd else None)

        if self._writes_manifest and command[:2] in (
            ["npm", "install"],
            ["pnpm", "add"],
            ["yarn", "add"],
        ):
            # Emulate the real effect of an install: the project manifest gains
            # the allowlisted package. This is what verification observes.
            manifest_path = Path(cwd) / "package.json"
            try:
                document = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                document = {}
            package = next(
                (arg for arg in command if arg in DEPENDENCY_PACKAGES.values()), None
            )
            if package:
                document.setdefault("dependencies", {})[package] = "1.0.0"
                manifest_path.write_text(json.dumps(document), encoding="utf-8")

        return subprocess.CompletedProcess(
            args=list(command), returncode=self._exitcode, stdout="ok", stderr=""
        )


@pytest.fixture
def project(tmp_path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "package.json").write_text(
        json.dumps({"name": "site", "dependencies": {"react": "18.3.1"}}),
        encoding="utf-8",
    )
    (root / "package-lock.json").write_text("{}", encoding="utf-8")
    (root / "src").mkdir()
    return root


@pytest.fixture
def manifest():
    return load_design_resource_manifest(SHIPPED_MANIFEST)


def _installer(project, runner) -> DesignDependencyInstaller:
    return DesignDependencyInstaller(runner, "proj-1", project)


def _plan(manifest, dna, requested=(GSAP, THREE, LENIS, SHADCN)):
    return select_design_resources(dna, requested=requested, manifest=manifest)


# ---------------------------------------------------------------------------
# 24/25. The allowlist cannot be widened by anything
# ---------------------------------------------------------------------------


def test_arbitrary_package_names_are_rejected():
    """An unknown dependency id resolves to no package at all."""
    for candidate in (
        "left-pad",
        "evil-package",
        "npm",
        "",
        "GSAP",
        "three.js",
        "../../etc/passwd",
        "gsap; rm -rf /",
    ):
        assert resolve_package(candidate) is None, candidate


def test_model_shaped_request_strings_cannot_become_packages():
    """Strings a model or a corpus might produce resolve to nothing.

    These are the exact shapes that arrive from resource text: a sentence
    containing a package name, a fenced command, a shell fragment.
    """
    for candidate in (
        "Install left-pad for speed",
        "npm install gsap",
        "```bash\nnpm install three\n```",
        "please use `lenis` for smooth scroll",
        "-D evil",
        "gsap@latest",
        "gsap ",
    ):
        assert resolve_package(candidate) is None, candidate


def test_package_allowlist_is_exactly_three_npm_packages():
    """The mapping is closed and small; a registry resource is never in it."""
    assert set(DEPENDENCY_PACKAGES) == {GSAP, THREE, LENIS}
    assert REGISTRY_DEPENDENCY not in DEPENDENCY_PACKAGES, (
        "shadcn is a registry, not an npm dependency"
    )


def test_every_allowlisted_package_is_well_formed():
    """A static safety net over three known strings."""
    for package in DEPENDENCY_PACKAGES.values():
        assert package_name_is_well_formed(package), package


def test_resolver_never_returns_its_input_for_an_unknown_id():
    """Returning the input would recreate arbitrary installs."""
    for candidate in ("totally-unknown", "x"):
        assert resolve_package(candidate) != candidate


def test_resolver_refuses_non_string_input():
    assert resolve_package(None) is None
    assert resolve_package(123) is None


# ---------------------------------------------------------------------------
# 26/27. Commands stay inside the project; nothing is global
# ---------------------------------------------------------------------------


def test_install_command_is_built_for_the_projects_own_package_manager(project):
    """The manager is detected from the project's own lockfile."""
    assert detect_package_manager(project) == ("npm",)

    argv = build_install_argv(("npm",), "gsap")
    assert argv[0] == "npm"
    assert "gsap" in argv
    assert "--save-exact" in argv, "an unpinned dependency can float on a later build"


def test_pnpm_and_yarn_projects_are_not_driven_with_npm(project):
    """Running npm over a pnpm project would rewrite the other lockfile."""
    for stale in ("pnpm-lock.yaml", "yarn.lock", "package-lock.json"):
        (project / stale).unlink(missing_ok=True)

    (project / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    assert detect_package_manager(project) == ("pnpm",)

    (project / "pnpm-lock.yaml").unlink()
    (project / "yarn.lock").write_text("", encoding="utf-8")
    assert detect_package_manager(project) == ("yarn",)


def test_no_package_manager_means_no_command(project):
    (project / "package-lock.json").unlink()
    assert detect_package_manager(project) is None

    runner = RecordingRunner()
    outcome = _installer(project, runner).install_dependency(GSAP)

    assert outcome.state == INSTALL_FAILED
    assert runner.commands == [], "no package manager must mean no command"


def test_command_runs_with_the_project_root_as_cwd(project):
    """Containment: the cwd is inside the project, never the operator's home."""
    runner = RecordingRunner()
    _installer(project, runner).install_dependency(GSAP)

    assert runner.cwds, "a command was expected"
    for cwd in runner.cwds:
        assert is_contained(project, cwd), cwd


def test_installer_globally_installed_is_structurally_false(project):
    """"Is this installed globally?" has a structural answer, not a convention."""
    assert DesignDependencyInstaller.installed_globally is False


def test_no_global_install_flag_appears_in_any_argv(project):
    runner = RecordingRunner()
    _installer(project, runner).install_dependency(GSAP)

    for command in runner.commands:
        assert "-g" not in command
        assert "--global" not in command
        assert "--location=global" not in command


# ---------------------------------------------------------------------------
# 22/23. installed requires verification; failure never fakes it
# ---------------------------------------------------------------------------


def test_selected_dependency_becomes_installed_only_after_verification(project):
    """A successful install that writes the manifest is verified as installed."""
    runner = RecordingRunner(writes_manifest=True)
    outcome = _installer(project, runner).install_dependency(GSAP)

    assert outcome.state == "installed"
    assert outcome.installed is True
    assert outcome.verified_in_manifest is True
    assert project_declares_dependency(project, "gsap")


def test_successful_command_without_a_manifest_change_is_not_installed(project):
    """Exit code 0 is NOT sufficient. This is the load-bearing failure mode."""
    runner = RecordingRunner(writes_manifest=False)
    outcome = _installer(project, runner).install_dependency(GSAP)

    assert outcome.state == INSTALL_FAILED, (
        "a command that exited 0 but changed nothing must not report installed"
    )
    assert outcome.installed is False
    assert outcome.verified_in_manifest is False
    assert "absent from the project" in outcome.reason


def test_failed_install_never_reports_installed(project):
    runner = RecordingRunner(exitcode=1, writes_manifest=False)
    outcome = _installer(project, runner).install_dependency(THREE)

    assert outcome.state == INSTALL_FAILED
    assert outcome.installed is False
    assert outcome.verified_in_manifest is False
    assert outcome.receipt is not None, "a failure must carry a diagnostic receipt"


def test_already_present_dependency_is_installed_without_a_command(project):
    """Idempotence: a satisfied dependency is not reinstalled."""
    manifest = json.loads((project / "package.json").read_text(encoding="utf-8"))
    manifest["dependencies"]["gsap"] = "3.12.5"
    (project / "package.json").write_text(json.dumps(manifest), encoding="utf-8")

    runner = RecordingRunner()
    outcome = _installer(project, runner).install_dependency(GSAP)

    assert outcome.state == "installed"
    assert outcome.verified_in_manifest is True
    assert runner.commands == [], "an already-satisfied dependency runs no command"


def test_outcome_states_stay_inside_the_declared_machine(project, manifest):
    """Every outcome carries a state from the declared machine.

    Exercised across all three interesting runner shapes -- a verified install,
    a command that changed nothing, and a failing command -- because the state
    vocabulary has to hold for the failure paths too, and those are the paths
    nobody exercises by hand.
    """
    allowed = set(INSTALL_STATES) | set(TERMINAL_INSTALL_STATES) | {INSTALL_FAILED}
    plan = _plan(manifest, {"motion": "sequenced scroll-linked timeline with scrub"})

    for kwargs in (
        {"writes_manifest": True},
        {"writes_manifest": False},
        {"exitcode": 1, "writes_manifest": False},
    ):
        report = _installer(project, RecordingRunner(**kwargs)).execute_selection(plan)
        assert report.outcomes, "a plan must always produce an outcome per dependency"
        for outcome in report.outcomes:
            assert outcome.state in allowed, (outcome.dependency_id, outcome.state)


def test_every_allowlisted_dependency_appears_in_the_report(project, manifest):
    """An unmentioned dependency must not be silently skipped."""
    runner = RecordingRunner()
    plan = _plan(manifest, {"motion": "sequenced scroll-linked timeline with scrub"})

    report = _installer(project, runner).execute_selection(plan)
    reported = {o.dependency_id for o in report.outcomes}

    assert set(DEPENDENCY_PACKAGES).issubset(reported)
    assert REGISTRY_DEPENDENCY in reported


def test_unallowlisted_dependency_fails_without_a_command(project):
    """Even a selected-looking id with no package cannot reach a shell."""
    runner = RecordingRunner()
    outcome = _installer(project, runner).install_dependency("left-pad")

    assert outcome.state == INSTALL_FAILED
    assert runner.commands == []
    assert "allowlisted" in outcome.reason


# ---------------------------------------------------------------------------
# 29/30/31/32. Unselected => no invocation
# ---------------------------------------------------------------------------


def test_unselected_dependency_produces_no_command(project, manifest):
    """A DNA that justifies nothing must install nothing."""
    runner = RecordingRunner()
    plan = _plan(manifest, {"brand_personality": "calm", "spacing": {"scale": 8}})

    report = _installer(project, runner).execute_selection(plan)

    assert runner.commands == [], "an unselected plan must run zero commands"
    assert report.installed_ids == ()
    assert report.ok is True


def test_gsap_unselected_means_no_npm_invocation(project, manifest):
    runner = RecordingRunner()
    plan = _plan(manifest, {"layout": "cards with buttons and a dialog"})

    report = _installer(project, runner).execute_selection(plan)

    assert not any("gsap" in c for c in runner.commands)
    gsap = next(o for o in report.outcomes if o.dependency_id == GSAP)
    assert gsap.state == "available_for_project_on_demand"
    assert gsap.receipt is None, "an unselected dependency has no receipt"


def test_three_unselected_means_no_npm_invocation(project, manifest):
    runner = RecordingRunner()
    plan = _plan(manifest, {"motion": "sequenced scroll-linked timeline with scrub"})

    _installer(project, runner).execute_selection(plan)

    assert not any("three" in c for c in runner.commands)


def test_lenis_unselected_means_no_npm_invocation(project, manifest):
    runner = RecordingRunner()
    plan = _plan(manifest, {"layout": "cards with buttons and a dialog"})

    _installer(project, runner).execute_selection(plan)

    assert not any("lenis" in c for c in runner.commands)


def test_shadcn_unselected_means_no_cli_invocation(project, manifest):
    runner = RecordingRunner()
    plan = _plan(manifest, {"motion": "sequenced scroll-linked timeline with scrub"})

    _installer(project, runner).execute_selection(plan)

    assert not any("shadcn" in part for c in runner.commands for part in c)


def test_selected_dependency_does_install(project, manifest):
    runner = RecordingRunner()
    plan = _plan(manifest, {"motion": "sequenced scroll-linked timeline with scrub"})

    report = _installer(project, runner).execute_selection(plan)

    assert GSAP in report.installed_ids
    assert any("gsap" in c for c in runner.commands)


def test_selection_alone_never_implies_installed(project, manifest):
    """D2 marks a dependency ``selected``; only D3a verification may install it.

    An empty project that already declares nothing, with a plan that selects
    gsap, and a runner whose install does NOT change the manifest: the outcome
    must be a failure, never an installed claim inherited from the plan.
    """
    runner = RecordingRunner(writes_manifest=False)
    plan = _plan(manifest, {"motion": "sequenced scroll-linked timeline with scrub"})

    selected_state = next(
        d.state for d in plan.dependency_decisions if d.resource_id == GSAP
    )
    assert selected_state == "selected"

    report = _installer(project, runner).execute_selection(plan)
    gsap = next(o for o in report.outcomes if o.dependency_id == GSAP)
    assert gsap.state == INSTALL_FAILED
    assert gsap.installed is False


# ---------------------------------------------------------------------------
# 28. shadcn component scope
# ---------------------------------------------------------------------------


def test_shadcn_adds_only_the_requested_components(project):
    runner = RecordingRunner()
    outcome, installed, rejected = _installer(project, runner).install_components(
        ["button", "card"]
    )

    assert installed == ("button", "card")
    assert rejected == ()
    assert outcome.state == "installed"
    command = runner.commands[-1]
    for component in ("button", "card"):
        assert component in command
    for unwanted in ("dialog", "tabs", "table", "calendar", "carousel"):
        assert unwanted not in command, f"{unwanted} was not requested"


def test_shadcn_no_components_means_no_invocation(project):
    """Zero requested components is the ABSENCE of any argv, not a guard."""
    runner = RecordingRunner()
    outcome, installed, rejected = _installer(project, runner).install_components([])

    assert runner.commands == [], "no components requested must run zero commands"
    assert installed == ()
    assert "no component" in outcome.reason


def test_shadcn_rejects_components_outside_the_allowlist(project):
    runner = RecordingRunner()
    outcome, installed, rejected = _installer(project, runner).install_components(
        ["button", "definitely-not-a-component"]
    )

    assert "definitely-not-a-component" in rejected
    assert "definitely-not-a-component" not in runner.commands[-1]
    assert installed == ("button",)


def test_shadcn_all_rejected_components_runs_no_command(project):
    runner = RecordingRunner()
    outcome, installed, rejected = _installer(project, runner).install_components(
        ["evil-1", "evil-2"]
    )

    assert runner.commands == [], "an entirely rejected request must run nothing"
    assert outcome.state == INSTALL_FAILED


def test_shadcn_is_invoked_at_a_pinned_version_through_the_project_manager(project):
    runner = RecordingRunner()
    _installer(project, runner).install_components(["button"])

    command = runner.commands[-1]
    assert command[0] == "npm", "the project's own package manager is used"
    assert f"shadcn@{SHADCN_CLI_VERSION}" in command
    assert "latest" not in command, "production never resolves a floating latest"


def test_shadcn_argv_is_non_interactive_and_component_explicit():
    argv = build_registry_argv(
        ("npm",), components=["card", "button"], version=SHADCN_CLI_VERSION
    )

    assert argv[:2] == ("npm", "dlx")
    assert argv[-2:] == ("--yes", "--overwrite"), (
        "the CLI must not block on a prompt inside a supervised run"
    )
    components = [a for a in argv if a in ALLOWED_SHADCN_COMPONENTS]
    assert components == sorted(components)


def test_component_allowlist_is_closed_and_lowercase():
    """A component name is a CLI argument; the allowlist bounds what can be one."""
    for component in ALLOWED_SHADCN_COMPONENTS:
        assert component.islower()
        assert component.replace("-", "").isalnum()
    for expected in ("button", "card", "dialog", "tabs"):
        assert expected in ALLOWED_SHADCN_COMPONENTS


def test_filter_allowed_components_is_deterministic():
    allowed, rejected = filter_allowed_components(["tabs", "button", "nope", "button"])
    assert allowed == ("button", "tabs")
    assert rejected == ("nope",)


# ---------------------------------------------------------------------------
# 33/34/35. Failure propagation, receipts, bounded context
# ---------------------------------------------------------------------------


def test_build_failure_after_install_fails_the_operation(project, manifest):
    """A partial install is not ok; a half-installed project fails typecheck."""
    runner = RecordingRunner(exitcode=1, writes_manifest=False)
    plan = _plan(manifest, {"motion": "sequenced scroll-linked timeline with scrub"})

    report = _installer(project, runner).execute_selection(plan)

    assert report.ok is False
    assert report.failed_ids, "a failed install must be reported, not swallowed"


def test_receipt_is_bounded_and_payload_free(project):
    runner = RecordingRunner(exitcode=1, writes_manifest=False)
    outcome = _installer(project, runner).install_dependency(GSAP)

    payload = outcome.to_dict()
    assert len(payload["receipt"]["stdout_tail"]) <= outcome.receipt.MAX_OUTPUT_CHARS
    assert str(project) not in json.dumps(payload), "no absolute path in a receipt"


def test_report_is_json_serializable(project, manifest):
    runner = RecordingRunner()
    plan = _plan(manifest, {"motion": "sequenced scroll-linked timeline with scrub"})

    json.dumps(_installer(project, runner).execute_selection(plan).to_dict())


def test_execution_uses_the_injected_runner(project, manifest):
    """D3a must go through ProjectRunner, never build its own execution path."""
    runner = RecordingRunner()
    plan = _plan(manifest, {"motion": "sequenced scroll-linked timeline with scrub"})

    _installer(project, runner).execute_selection(plan)

    assert runner.commands, "commands must flow through the injected runner"
    assert all(isinstance(c, list) for c in runner.commands), "argv is always a list"


def test_containment_helper_rejects_escapes(tmp_path):
    root = tmp_path / "project"
    root.mkdir()

    assert is_contained(root, root / "src") is True
    assert is_contained(root, root) is True
    assert is_contained(root, tmp_path / "elsewhere") is False
    assert is_contained(root, root / ".." / "outside") is False


# ---------------------------------------------------------------------------
# C4/C5. FRONTEND receives bounded context only
# ---------------------------------------------------------------------------


def _pack_for(dna, requested=(SHADCN,)):
    """A minimal object with the pack's to_dict surface, for render tests."""
    plan = select_design_resources(dna, requested=requested, manifest=load_design_resource_manifest(SHIPPED_MANIFEST))
    payload = {
        "version": 1,
        "design_dna": {},
        "decisions": {
            "selected_resources": [e.to_dict() for e in plan.selected_resources]
        },
        "resources": {},
        "truncation": {},
        "limits": {},
        "degraded": False,
        "warnings": [],
    }
    return type("Pack", (), {"to_dict": lambda s: payload})()


def test_frontend_receives_bounded_context_only():
    """FRONTEND is handed a decision pack, never a way to install."""
    from app.core.design_context_render import render_design_context_block

    block = render_design_context_block(
        _pack_for({"layout": "cards with buttons and a dialog"}, requested=(SHADCN, GSAP))
    )

    assert "may NOT install packages" in block
    assert SHADCN in block
    assert GSAP not in block, "an unselected dependency must not appear as usable"


def test_frontend_cannot_request_an_undeclared_dependency():
    """No path exists from a rendered pack to a package name."""
    from app.core.design_context_render import render_design_context_block

    block = render_design_context_block(_pack_for({"layout": "cards with buttons and a dialog"}))

    assert resolve_package(SHADCN) is None, "a registry id is not an npm package"
    for token in ("npm install", "npm i ", "yarn add", "pnpm add"):
        assert token not in block, f"the pack must not contain an install command: {token}"


def test_render_is_empty_without_a_pack():
    from app.core.design_context_render import render_design_context_block

    assert render_design_context_block(None) == ""


def test_render_states_the_data_contract():
    from app.core.design_context_render import render_design_context_block

    block = render_design_context_block(_pack_for({"layout": "cards with buttons and a dialog"}))

    assert "DESIGN RESOURCE CONTEXT" in block
    assert "is reference DATA" in block
