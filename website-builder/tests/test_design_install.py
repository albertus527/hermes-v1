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
from typing import List, Sequence

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_install import (
    ALLOWED_SHADCN_COMPONENTS,
    DEPENDENCY_PACKAGES,
    INSTALL_FAILED,
    INSTALL_STATES,
    REGISTRY_DEPENDENCY,
    REASON_COMPONENTS_NOT_VERIFIED,
    REASON_MANAGER_UNSUPPORTED,
    REASON_SHADCN_CONFIG_INVALID,
    SHADCN_CLI_VERSION,
    TERMINAL_INSTALL_STATES,
    YARN_BERRY_CONFIG,
    DesignDependencyInstaller,
    approved_component_dir,
    build_install_argv,
    build_registry_argv,
    detect_package_manager,
    filter_allowed_components,
    is_contained,
    package_name_is_well_formed,
    project_declares_dependency,
    registry_invocation_prefix,
    resolve_package,
    verify_components_materialized,
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

    ``materializes_components`` emulates what the registry CLI actually does to
    the workspace: write a component file under the directory the project's
    ``components.json`` declares. When it is False the command "succeeds" while
    changing nothing, which is precisely the case that must not be reported as
    installed.
    """

    def __init__(
        self,
        *,
        exitcode: int = 0,
        writes_manifest: bool = True,
        materializes_components: bool = False,
        only_components: Sequence[str] = (),
        stdout: str = "ok",
    ):
        self.commands: List[List[str]] = []
        self.cwds: List[Path] = []
        self._exitcode = exitcode
        self._writes_manifest = writes_manifest
        self._materializes_components = materializes_components
        self._only_components = tuple(only_components)
        self._stdout = stdout

    def _component_dir(self, cwd) -> Path:
        """Where this project's OWN config says components belong."""
        from app.core.design_install import approved_component_dir

        resolved = approved_component_dir(Path(cwd))
        assert resolved is not None, "fixture requires an approved components.json"
        return resolved

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

        if self._materializes_components and "add" in command:
            target_dir = self._component_dir(cwd)
            target_dir.mkdir(parents=True, exist_ok=True)
            names = self._only_components or tuple(
                arg
                for arg in command
                if arg in DEPENDENCY_COMPAGES_AND_COMPONENTS
            )
            for name in names:
                (target_dir / f"{name}.tsx").write_text(
                    f"export function {name.replace('-', '_')}() {{return null}}",
                    encoding="utf-8",
                )

        return subprocess.CompletedProcess(
            args=list(command), returncode=self._exitcode, stdout=self._stdout, stderr=""
        )


#: Every name that may appear as a component in an `add` argv. Used by the
#: recorder to tell component arguments apart from flags.
DEPENDENCY_COMPAGES_AND_COMPONENTS = frozenset(ALLOWED_SHADCN_COMPONENTS)


#: The pinned, reviewed shadcn config for the Vite/React/TS/Tailwind starter.
#: Kept as a module constant so a test that mutates it must do so on a copy.
APPROVED_COMPONENTS_JSON = {
    "$schema": "https://ui.shadcn.com/schema.json",
    "style": "new-york",
    "rsc": False,
    "tsx": True,
    "tailwind": {
        "config": "",
        "css": "src/index.css",
        "baseColor": "neutral",
        "cssVariables": True,
        "prefix": "",
    },
    "aliases": {
        "components": "@/components",
        "ui": "@/components/ui",
        "lib": "@/lib",
        "utils": "@/lib/utils",
        "hooks": "@/hooks",
    },
    "iconLibrary": "lucide",
}


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
    # A project ships with the reviewed shadcn config, exactly as the starter
    # does. Tests that exercise the fail-closed path remove or corrupt it.
    (root / "components.json").write_text(
        json.dumps(APPROVED_COMPONENTS_JSON), encoding="utf-8"
    )
    return root


def _write_config(project: Path, document) -> None:
    """Overwrite the project's shadcn config with ``document`` (raw)."""
    (project / "components.json").write_text(document, encoding="utf-8")


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
    runner = RecordingRunner(materializes_components=True)
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
    runner = RecordingRunner(materializes_components=True)
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
    runner = RecordingRunner(materializes_components=True)
    _installer(project, runner).install_components(["button"])

    command = runner.commands[-1]
    assert command[0] == "npm", "the project's own package manager is used"
    # npm carries the pin as a --package spec rather than a bare positional, so
    # assert the pin is present in the form THIS manager actually receives.
    assert any(
        part.endswith(f"shadcn@{SHADCN_CLI_VERSION}") for part in command
    ), command
    assert "latest" not in command, "production never resolves a floating latest"


# ---------------------------------------------------------------------------
# 28b. Manager-specific one-off invocation
# ---------------------------------------------------------------------------
#
# The reported bug: `+ ("dlx", ...)` was appended for EVERY manager, producing
# `npm dlx ...`. npm has no `dlx` subcommand -- its one-off mechanism is
# `npm exec`. Each manager gets its own, and the argv is asserted EXACTLY.


def _registry_argv(manager, project_root, components=("button", "card")):
    prefix = registry_invocation_prefix(
        manager, project_root=project_root, version=SHADCN_CLI_VERSION
    )
    if prefix is None:
        return None
    return build_registry_argv(prefix, components=list(components))


def test_npm_registry_argv_is_exact():
    """npm has no ``dlx``; its one-off runner is ``npm exec``.

    The ``--`` separator is load-bearing: without it npm re-parses later switches
    as its own and would swallow shadcn's ``--yes`` / ``--overwrite``.
    """
    argv = _registry_argv(("npm",), Path("."))

    assert argv == (
        "npm",
        "exec",
        "--yes",
        f"--package=shadcn@{SHADCN_CLI_VERSION}",
        "--",
        "shadcn",
        "add",
        "button",
        "card",
        "--yes",
        "--overwrite",
    ), argv
    assert "dlx" not in argv, "npm has no dlx subcommand"


def test_pnpm_registry_argv_is_exact(project):
    (project / "package-lock.json").unlink()
    (project / "pnpm-lock.yaml").write_text("", encoding="utf-8")

    assert _registry_argv(("pnpm",), project) == (
        "pnpm",
        "dlx",
        f"shadcn@{SHADCN_CLI_VERSION}",
        "add",
        "button",
        "card",
        "--yes",
        "--overwrite",
    )


def test_yarn_berry_registry_argv_is_exact(project):
    """Berry ships `yarn dlx`; its presence is proven by .yarnrc.yml."""
    (project / "package-lock.json").unlink()
    (project / "yarn.lock").write_text("", encoding="utf-8")
    (project / YARN_BERRY_CONFIG).write_text("", encoding="utf-8")

    assert _registry_argv(("yarn",), project) == (
        "yarn",
        "dlx",
        f"shadcn@{SHADCN_CLI_VERSION}",
        "add",
        "button",
        "card",
        "--yes",
        "--overwrite",
    )


def test_yarn_classic_fails_closed_instead_of_falling_back(project):
    """Yarn Classic has no `dlx`.

    Falling back to npx/npm would run a CLI outside the project's toolchain
    contract, so the refusal is the behaviour under test.
    """
    (project / "package-lock.json").unlink()
    (project / "yarn.lock").write_text("", encoding="utf-8")
    (project / ".yarnrc").write_text("", encoding="utf-8")

    assert _registry_argv(("yarn",), project) is None

    runner = RecordingRunner(materializes_components=True)
    outcome, _, _ = _installer(project, runner).install_components(["button"])

    assert runner.commands == [], "an unsupported toolchain must run nothing"
    assert outcome.state == INSTALL_FAILED
    assert outcome.reason == REASON_MANAGER_UNSUPPORTED


def test_unknown_package_manager_has_no_invocation():
    # The version is a passthrough the function never reads on these paths --
    # the refusal is decided by the manager name alone. Passing the real pin
    # keeps the test honest about which version production uses.
    assert registry_invocation_prefix((), project_root=Path("."), version=SHADCN_CLI_VERSION) is None
    assert registry_invocation_prefix(("bun",), project_root=Path("."), version=SHADCN_CLI_VERSION) is None


def test_no_manager_argv_uses_a_floating_latest_or_a_global_flag(project):
    for manager in (("npm",), ("pnpm",), ("yarn",)):
        argv = _registry_argv(manager, project)
        if argv is None:
            continue
        assert "latest" not in argv
        assert "-g" not in argv
        assert "--global" not in argv
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
# 28c. The approved component destination is application-owned
# ---------------------------------------------------------------------------
#
# `shadcn add` writes where `components.json` says, so the destination is a
# property of the project's config. These tests pin the fail-closed behaviour:
# with no config this application is willing to act on, D3a runs NO command.


def test_missing_components_json_runs_no_command_and_fails_closed(project):
    """No config => no destination => no invocation, and no guessed path."""
    (project / "components.json").unlink()

    runner = RecordingRunner(materializes_components=True)
    outcome, _, _ = _installer(project, runner).install_components(["button"])

    assert runner.commands == [], (
        "a component destination we cannot verify must not be written to"
    )
    assert outcome.state == INSTALL_FAILED
    assert outcome.reason == REASON_SHADCN_CONFIG_INVALID
    assert outcome.receipt is None


@pytest.mark.parametrize(
    "document, why",
    [
        ("{not json", "malformed JSON"),
        ("[]", "not an object"),
        (json.dumps({"style": "lemon", "tailwind": {"css": "src/index.css"},
                     "aliases": {"ui": "@/components/ui", "utils": "@/lib/utils"}}),
         "unreviewed style"),
        (json.dumps({"style": "new-york",
                     "aliases": {"ui": "@/components/ui", "utils": "@/lib/utils"}}),
         "no tailwind block"),
        (json.dumps({"style": "new-york", "tailwind": {"config": "t.config.js"},
                     "aliases": {"ui": "@/components/ui", "utils": "@/lib/utils"}}),
         "tailwind without a css entry"),
        (json.dumps({"style": "new-york",
                     "tailwind": {"css": "src/index.css"},
                     "aliases": {"ui": "/etc", "utils": "@/lib/utils"}}),
         "absolute alias"),
        (json.dumps({"style": "new-york",
                     "tailwind": {"css": "src/index.css"},
                     "aliases": {"ui": "@/../..", "utils": "@/lib/utils"}}),
         "traversal alias"),
        (json.dumps({"style": "new-york",
                     "tailwind": {"css": "src/index.css"},
                     "aliases": {"ui": "components/ui"}}),
         "alias not using the @/ mapping"),
        (json.dumps({"style": "new-york",
                     "tailwind": {"css": "src/index.css"},
                     "aliases": {"ui": "@/components/ui"}}),
         "no utils alias"),
    ],
)
def test_unapproved_components_json_fails_closed(project, document, why):
    """Every shape we will not act on produces zero invocations."""
    _write_config(project, document)

    runner = RecordingRunner(materializes_components=True)
    outcome, _, _ = _installer(project, runner).install_components(["button"])

    assert approved_component_dir(project) is None, why
    assert runner.commands == [], why
    assert outcome.state == INSTALL_FAILED, why
    assert outcome.reason == REASON_SHADCN_CONFIG_INVALID, why


def test_approved_config_yields_the_configured_component_root(project):
    """The destination comes from the project's own config, not a constant."""
    resolved = approved_component_dir(project)

    assert resolved is not None
    assert resolved.relative_to(project).as_posix() == "src/components/ui", (
        "@/components/ui is bound against the starter's @/* -> ./src/* mapping"
    )


def test_component_root_follows_a_different_approved_alias(project):
    """A reviewed alias that differs from our default is honoured, not ignored.

    This is why the destination is read from the config at all: hardcoding
    src/components/ui would verify the wrong directory here.
    """
    document = dict(APPROVED_COMPONENTS_JSON)
    document["aliases"] = dict(APPROVED_COMPONENTS_JSON["aliases"], ui="@/components/primitives")
    _write_config(project, json.dumps(document))

    resolved = approved_component_dir(project)

    assert resolved is not None
    assert resolved.relative_to(project).as_posix() == "src/components/primitives"


def test_component_root_that_escapes_the_project_is_refused(project):
    """A symlinked component root pointing outside the workspace is refused.

    Guarded because creating a symlink needs a privilege Windows does not grant
    by default; without it the assertion would error rather than fail informatively.
    """
    outside = project.parent / "outside-ui"
    outside.mkdir()
    link = project / "src" / "components"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not permitted on this host")

    assert approved_component_dir(project) is None


# ---------------------------------------------------------------------------
# 28d. installed requires an observed component on disk
# ---------------------------------------------------------------------------


def test_shadcn_success_without_materialization_is_not_installed(project):
    """Exit code 0 is NOT sufficient. This is the load-bearing failure mode."""
    runner = RecordingRunner(materializes_components=False)
    outcome, _, _ = _installer(project, runner).install_components(["button", "card"])

    assert runner.commands, "the CLI should have been invoked"
    assert outcome.state == INSTALL_FAILED
    assert outcome.installed is False
    assert outcome.verified_components == ()
    assert outcome.reason == REASON_COMPONENTS_NOT_VERIFIED


def test_stdout_claiming_success_does_not_verify_anything(project):
    """The postcondition reads the filesystem, never the command's output."""
    runner = RecordingRunner(
        materializes_components=False,
        stdout="Success! Added 2 components: button, card",
    )
    outcome, _, _ = _installer(project, runner).install_components(["button", "card"])

    assert outcome.state == INSTALL_FAILED
    assert outcome.reason == REASON_COMPONENTS_NOT_VERIFIED


def test_partial_materialization_is_not_installed(project):
    """A half-written component set fails: it would fail `npm run build`."""
    runner = RecordingRunner(materializes_components=True, only_components=("button",))
    outcome, _, _ = _installer(project, runner).install_components(["button", "card"])

    assert (project / "src/components/ui/button.tsx").is_file()
    assert outcome.state == INSTALL_FAILED
    assert outcome.verified_components == ()
    assert outcome.reason == REASON_COMPONENTS_NOT_VERIFIED


def test_all_requested_components_present_is_installed(project):
    runner = RecordingRunner(materializes_components=True)
    outcome, installed, _ = _installer(project, runner).install_components(
        ["button", "card"]
    )

    assert outcome.state == "installed"
    assert outcome.installed is True
    assert outcome.verified_components == ("button", "card")
    assert installed == ("button", "card")


def test_a_directory_named_like_a_component_does_not_verify(project):
    """``is_file()``, not ``exists()``: a directory is not a component."""
    (project / "src/components/ui").mkdir(parents=True)
    (project / "src/components/ui/button.tsx").mkdir()

    runner = RecordingRunner(materializes_components=False)
    outcome, _, _ = _installer(project, runner).install_components(["button"])

    assert outcome.state == INSTALL_FAILED
    assert outcome.reason == REASON_COMPONENTS_NOT_VERIFIED


def test_rejected_component_on_disk_is_never_accepted(project):
    """A rejected name cannot satisfy verification, and its absence cannot fail it.

    Two directions matter: a file the application never asked for must not make
    the run look complete, and must not make it look incomplete either.
    """
    ui = project / "src/components/ui"
    ui.mkdir(parents=True)
    (ui / "button.tsx").write_text("export const Button = () => null", encoding="utf-8")
    (ui / "evil-1.tsx").write_text("export const Evil = () => null", encoding="utf-8")

    runner = RecordingRunner(materializes_components=False)
    outcome, installed, rejected = _installer(project, runner).install_components(
        ["button", "evil-1"]
    )

    assert installed == ("button",)
    assert rejected == ("evil-1",)
    assert outcome.state == "installed"
    assert outcome.verified_components == ("button",), (
        "verification must be scoped to the allowed components only"
    )


def test_verifier_refuses_a_component_outside_the_allowlist(project):
    """Defence in depth: the verifier never trusts its caller."""
    component_dir = approved_component_dir(project)
    component_dir.mkdir(parents=True)
    (component_dir / "evil-1.tsx").write_text("x", encoding="utf-8")

    assert verify_components_materialized(project, ["evil-1"], component_dir) == ()


def test_verifier_rejects_a_component_dir_outside_the_project(project, tmp_path):
    """A component dir outside the workspace verifies nothing, even if populated."""
    outside = tmp_path / "outside"
    (outside / "ui").mkdir(parents=True)
    (outside / "ui" / "button.tsx").write_text("x", encoding="utf-8")

    assert verify_components_materialized(project, ["button"], outside / "ui") == ()
    assert verify_components_materialized(project, ["button"], None) == ()


def test_component_receipt_carries_names_not_paths(project):
    """Bounded, and free of absolute paths."""
    runner = RecordingRunner(materializes_components=True)
    outcome, _, _ = _installer(project, runner).install_components(["button"])

    payload = json.dumps(outcome.to_dict())

    assert outcome.verified_components == ("button",)
    assert str(project) not in payload, "a receipt must not leak the workspace path"
    assert "/tmp" not in payload and "src/components/ui" not in payload


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
