"""Batch D3a.5 Part A: exact pins, companion types, conjunctive verification.

Real temporary project workspaces with real ``package.json`` files. **No
subprocess runs and no network call is made**: the runner is a recorder, so
every assertion is about the argv the application WOULD run and the state it
WOULD report.

The properties under test are BEHAVIOUR CONTRACTS:

    * every optional dependency installs at an EXACT application-owned version
    * ``three`` carries a companion ``@types/three`` at its own exact pin
    * ``installed`` is CONJUNCTIVE: runtime AND companion, exact version,
      exact dependency section
    * a runtime-only Three is NOT installed (this is the TS7016 field failure)
    * the companion map is CLOSED: no ``@types/<runtime>`` derivation, and no
      caller-, model- or resource-supplied companion is ever forwarded to npm
    * a dependency with no companion runs no companion command

Nothing here asserts a specific catalogue beyond the pins the application
declares, and nothing asserts a count: adding a dependency or changing a pin is
expected. What is asserted is the RELATION between the tables and the
postcondition -- that verification counts exactly what the maps declare.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_install import (
    DEPENDENCY_COMPANION_PACKAGES,
    DEPENDENCY_PACKAGE_PINS,
    DEPENDENCY_PACKAGES,
    INSTALL_FAILED,
    REASON_COMPANION_NOT_VERIFIED,
    REASON_INSTALL_FAILED,
    REASON_PIN_NOT_EXACT,
    SECTION_DEPENDENCIES,
    SECTION_DEV_DEPENDENCIES,
    DesignDependencyInstaller,
    PackageSpec,
    build_companion_install_argv,
    build_install_argv,
    is_contained,
    project_declares_dependency,
    project_satisfies_dependency,
    project_satisfies_spec,
    required_package_specs,
    resolve_companion_packages,
    resolve_package,
)

GSAP = "gsap"
THREE = "three"
LENIS = "lenis"

#: The companion Three needs because it ships no bundled TypeScript
#: declarations. Named here so a test can talk about it without importing the
#: table it is asserting the contents of.
THREE_TYPES = "@types/three"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class RecordingRunner:
    """Records argv and emulates a real install writing the manifest.

    The emulation respects the SAME exactness the production postcondition
    enforces: it writes the version from the argv spec into the section the
    argv selected. A fixture that wrote a plausible-looking ``1.0.0`` would let
    these tests pass while the real postcondition correctly rejected the state.

    ``runtime_only`` reproduces the field failure: the runtime package lands
    but its companion does not.
    """

    def __init__(
        self,
        *,
        exitcode: int = 0,
        skip_companion: bool = False,
        companion_exitcode: Optional[int] = None,
    ):
        self.commands: List[List[str]] = []
        self.cwds: List[Path] = []
        self._exitcode = exitcode
        self._skip_companion = skip_companion
        #: Exit code for a COMPANION command only. Models the field case where
        #: the runtime install succeeds and the @types/three install fails --
        #: the state that must not be reported as installed.
        self._companion_exitcode = companion_exitcode

    def run_command(self, project_id, command, cwd=None, env=None, timeout=300.0):
        command = list(command)
        self.commands.append(command)
        self.cwds.append(Path(cwd) if cwd else None)

        is_companion = "--save-dev" in command or "--dev" in command

        if is_companion and self._companion_exitcode is not None:
            return subprocess.CompletedProcess(
                args=command,
                returncode=self._companion_exitcode,
                stdout="",
                stderr="companion install failed",
            )

        if command[:2] in (["npm", "install"], ["pnpm", "add"], ["yarn", "add"]):
            if self._skip_companion and "--save-dev" in command:
                return subprocess.CompletedProcess(
                    args=command, returncode=0, stdout="", stderr=""
                )
            path = Path(cwd) / "package.json"
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                document = {}
            section = (
                SECTION_DEV_DEPENDENCIES
                if "--save-dev" in command or "--dev" in command
                else SECTION_DEPENDENCIES
            )
            known = set(DEPENDENCY_PACKAGES.values()) | {
                companion.package
                for companions in DEPENDENCY_COMPANION_PACKAGES.values()
                for companion in companions
            }
            for arg in command:
                if "@" not in arg or arg.startswith("-"):
                    continue
                # rpartition, not partition: a scoped name like
                # "@types/three" contains its own "@", so splitting at
                # the FIRST one would yield an empty package name and
                # silently write nothing.
                name, _, version = arg.rpartition("@")
                if name in known:
                    document.setdefault(section, {})[name] = version
                    break
            path.write_text(json.dumps(document), encoding="utf-8")

        return subprocess.CompletedProcess(
            args=command, returncode=self._exitcode, stdout="ok", stderr=""
        )


@pytest.fixture
def project(tmp_path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "package.json").write_text(
        json.dumps({"name": "site", "dependencies": {"react": "19.2.7"}}),
        encoding="utf-8",
    )
    (root / "package-lock.json").write_text("{}", encoding="utf-8")
    (root / "src").mkdir()
    return root


def _declare(project: Path, package: str, version: str, section: str) -> None:
    path = project / "package.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document.setdefault(section, {})[package] = version
    path.write_text(json.dumps(document), encoding="utf-8")


def _installer(project: Path, runner) -> DesignDependencyInstaller:
    return DesignDependencyInstaller(runner, "proj-1", project)


# ---------------------------------------------------------------------------
# 1-4. Exact pins
# ---------------------------------------------------------------------------


def test_every_allowlisted_dependency_has_an_exact_pin():
    """No dependency may install without an application-owned exact version.

    Asserted as a RELATION between the two tables rather than as literal
    version strings: a pin bump is an expected maintenance edit, while a
    missing pin -- or a range -- would let tomorrow's build install something
    today's verification never saw.
    """
    assert set(DEPENDENCY_PACKAGE_PINS) == set(DEPENDENCY_PACKAGES), (
        "every allowlisted dependency must have a pin, and no pin may name a "
        "dependency that is not allowlisted"
    )
    for dependency_id, version in DEPENDENCY_PACKAGE_PINS.items():
        assert PackageSpec("x", version).is_exact(), (dependency_id, version)


def test_no_pin_carries_a_range_operator():
    """``^``/``~``/``>=``/``latest``/prerelease must be impossible in the table."""
    for dependency_id, version in DEPENDENCY_PACKAGE_PINS.items():
        for forbidden in ("^", "~", ">", "<", "=", "*", "||", " ", "x", "beta", "rc", "next"):
            assert forbidden not in version, (dependency_id, version, forbidden)


def test_three_has_an_exact_companion_types_pin():
    """Three needs @types/three at its own exact pin, in the dev section."""
    companions = resolve_companion_packages(THREE)

    assert len(companions) == 1
    companion = companions[0]
    assert companion.package == THREE_TYPES
    assert companion.to_spec().is_exact()
    assert companion.dependency_section == SECTION_DEV_DEPENDENCIES, (
        "a type-only companion belongs in devDependencies; putting it in the "
        "runtime set would claim it ships"
    )


def test_dependencies_that_ship_their_own_types_have_no_companion():
    """gsap and lenis are self-typed, so the closed map has no row for them."""
    for dependency_id in (GSAP, LENIS):
        assert resolve_companion_packages(dependency_id) == (), dependency_id
        assert build_companion_install_argv(("npm",), dependency_id) == ()


# ---------------------------------------------------------------------------
# 5. Runtime-only Three is NOT installed
# ---------------------------------------------------------------------------


def test_runtime_only_three_is_not_installed(project):
    """The TS7016 field failure, reproduced and prevented.

    ``three`` installs cleanly and the build then fails with "Could not find a
    declaration file for module 'three'". Reporting ``installed`` here would
    claim a capability the project does not have.
    """
    _declare(project, THREE, DEPENDENCY_PACKAGE_PINS[THREE], SECTION_DEPENDENCIES)

    assert not project_satisfies_dependency(project, THREE), (
        "a runtime-only Three must not satisfy the postcondition"
    )


def test_a_three_install_whose_companion_never_lands_is_not_installed(project):
    """End-to-end: the runtime command succeeded, the companion wrote nothing."""
    runner = RecordingRunner(skip_companion=True)

    outcome = _installer(project, runner).install_dependency(THREE)

    assert outcome.state == INSTALL_FAILED
    assert outcome.installed is False
    assert outcome.verified_in_manifest is False
    assert outcome.reason == REASON_COMPANION_NOT_VERIFIED, (
        "the runtime package IS present; the gap is specifically the companion"
    )
    # Both commands were genuinely issued; the verdict came from the manifest.
    assert len(runner.commands) == 2, "runtime plus one companion command"


def test_a_failing_companion_install_reports_a_command_failure(project):
    """The runtime package lands; the @types/three install fails.

    This is the most dangerous shape of the failure: the project now HAS
    ``three``, so a loose membership check would call it installed and the
    build would fail at ``tsc`` with TS7016. Only the conjunctive postcondition
    catches it.

    The reason matters as much as the state. A failed COMMAND is a different
    operational diagnosis from a missing manifest entry -- one means the
    install broke, the other means it silently did nothing -- and the receipt
    only exists on the command-failure path, so an operator loses the diagnostic
    output if the two are conflated.
    """
    runner = RecordingRunner(companion_exitcode=1)

    outcome = _installer(project, runner).install_dependency(THREE)

    assert outcome.state == INSTALL_FAILED
    assert outcome.installed is False
    assert outcome.verified_in_manifest is False
    assert outcome.reason == REASON_INSTALL_FAILED, (
        "a failed companion command must be diagnosed as a command failure, "
        "not as a missing manifest entry"
    )
    assert outcome.receipt is not None, "a failure must carry a diagnostic receipt"
    # The runtime really did land -- that is what makes this the load-bearing case.
    assert project_declares_dependency(project, THREE)
    assert not project_satisfies_dependency(project, THREE)
    # The sequence stopped at the failure rather than continuing.
    assert len(runner.commands) == 2


# ---------------------------------------------------------------------------
# 6. Both exact => installed
# ---------------------------------------------------------------------------


def test_three_with_exact_runtime_and_companion_is_installed(project):
    runner = RecordingRunner()

    outcome = _installer(project, runner).install_dependency(THREE)

    assert outcome.state == "installed"
    assert outcome.verified_in_manifest is True
    assert project_satisfies_dependency(project, THREE)
    document = json.loads((project / "package.json").read_text(encoding="utf-8"))
    assert document[SECTION_DEPENDENCIES][THREE] == DEPENDENCY_PACKAGE_PINS[THREE]
    assert document[SECTION_DEV_DEPENDENCIES][THREE_TYPES] == DEPENDENCY_COMPANION_PACKAGES[THREE][0].version


def test_the_companion_installs_into_the_dev_section_not_the_runtime_one(project):
    """Section is part of the postcondition, not a packaging detail."""
    runner = RecordingRunner()

    _installer(project, runner).install_dependency(THREE)

    document = json.loads((project / "package.json").read_text(encoding="utf-8"))
    assert THREE_TYPES not in document[SECTION_DEPENDENCIES]


# ---------------------------------------------------------------------------
# 7-8. Wrong version / wrong section both fail
# ---------------------------------------------------------------------------


def test_companion_at_the_wrong_version_is_not_installed(project):
    """A near-miss version must not satisfy an exact postcondition.

    Asserted against the predicate rather than through a recording run: a
    recorder that emulates a successful install would legitimately REPAIR the
    manifest, and then "installed" is the correct answer to a different question.
    What matters here is that the pre-existing near-miss state is not accepted.
    """
    _declare(project, THREE, DEPENDENCY_PACKAGE_PINS[THREE], SECTION_DEPENDENCIES)
    _declare(project, THREE_TYPES, "0.185.0", SECTION_DEV_DEPENDENCIES)

    assert not project_satisfies_dependency(project, THREE)
    assert not project_satisfies_spec(
        project,
        required_package_specs(THREE)[1],
    ), "a near-miss companion version must not satisfy the spec"


def test_companion_in_the_wrong_dependency_section_is_not_installed(project):
    """The right version in the wrong section must not satisfy the spec."""
    _declare(project, THREE, DEPENDENCY_PACKAGE_PINS[THREE], SECTION_DEPENDENCIES)
    _declare(
        project,
        THREE_TYPES,
        DEPENDENCY_COMPANION_PACKAGES[THREE][0].version,
        SECTION_DEPENDENCIES,
    )

    assert not project_satisfies_dependency(project, THREE)


def test_runtime_in_the_wrong_section_is_not_installed(project):
    """A dev-only runtime declaration must not satisfy a runtime spec."""
    _declare(
        project, THREE, DEPENDENCY_PACKAGE_PINS[THREE], SECTION_DEV_DEPENDENCIES
    )
    _declare(
        project,
        THREE_TYPES,
        DEPENDENCY_COMPANION_PACKAGES[THREE][0].version,
        SECTION_DEV_DEPENDENCIES,
    )

    assert not project_satisfies_dependency(project, THREE)


def test_a_declared_but_wrong_section_state_still_triggers_a_reinstall(project):
    """End-to-end: an unsatisfied state causes commands, then verification passes.

    Proves the repair path actually runs rather than short-circuiting, and that
    the pre-existing wrong-section entries are not what satisfies the verdict.
    """
    _declare(project, THREE, DEPENDENCY_PACKAGE_PINS[THREE], SECTION_DEV_DEPENDENCIES)
    runner = RecordingRunner()

    outcome = _installer(project, runner).install_dependency(THREE)

    assert runner.commands, "an unsatisfied state must trigger the install path"
    assert outcome.state == "installed"
    document = json.loads((project / "package.json").read_text(encoding="utf-8"))
    assert document[SECTION_DEPENDENCIES][THREE] == DEPENDENCY_PACKAGE_PINS[THREE]


# ---------------------------------------------------------------------------
# 9-11. Companion closure
# ---------------------------------------------------------------------------


def test_the_companion_map_is_closed_to_known_dependencies():
    """No map row may name a dependency that is not itself allowlisted."""
    for dependency_id in DEPENDENCY_COMPANION_PACKAGES:
        assert resolve_package(dependency_id) is not None, (
            "a companion row for an unknown dependency would let an undeclared "
            "id reach npm through the companion path"
        )


def test_an_unknown_dependency_resolves_no_companions_and_no_specs():
    """No id, model-shaped string, or corpus-shaped string acquires a package."""
    for candidate in (
        "totally-unknown",
        "@types/react",
        "three@latest",
        "npm install @types/evil",
        "../../etc",
        "",
    ):
        assert resolve_companion_packages(candidate) == (), candidate
        assert required_package_specs(candidate) == (), candidate
        assert resolve_package(candidate) is None, candidate


def test_no_generic_types_derivation_exists():
    """A runtime package must not acquire a companion by naming convention.

    ``@types/<runtime-name>`` is the rule this batch explicitly refuses: it would
    let any future runtime package acquire an arbitrary dev dependency without a
    human adding the row to the closed map.

    Asserted structurally rather than by reading source. Two facts together
    rule the derivation out:

      1. ``resolve_companion_packages`` consults ONLY the closed map -- so a
         name absent from the map yields an empty tuple, whatever it looks like;
      2. every companion that IS returned came from a map row, so it cannot have
         been invented from the runtime package's name.

    Exercised with a package name whose conventional ``@types/`` twin is NOT in
    the map: if derivation existed, that twin would appear.
    """
    # gsap ships its own types and has no map row, so @types/gsap must NOT appear.
    assert "@types/gsap" not in {
        c.package for c in resolve_companion_packages(GSAP)
    }
    assert resolve_companion_packages(GSAP) == ()

    # Every companion returned anywhere in the table traces to a declared row.
    declared = {
        companion.package
        for companions in DEPENDENCY_COMPANION_PACKAGES.values()
        for companion in companions
    }
    for dependency_id in DEPENDENCY_PACKAGES:
        returned = {c.package for c in resolve_companion_packages(dependency_id)}
        assert returned <= declared, dependency_id

    # A dependency whose conventional twin is absent proves no derivation: the
    # map is the only path, so an unlisted twin cannot appear.
    conventional = {
        f"@types/{package}" for package in DEPENDENCY_PACKAGES.values()
    }
    assert conventional - declared, (
        "expected at least one runtime package with no @types twin, otherwise "
        "this test cannot distinguish derivation from declaration"
    )
    for dependency_id, package in DEPENDENCY_PACKAGES.items():
        if f"@types/{package}" in declared:
            continue
        assert f"@types/{package}" not in {
            c.package for c in resolve_companion_packages(dependency_id)
        }


def test_specs_are_derived_only_from_the_closed_tables():
    """Required specs = runtime pin + declared companions, nothing else."""
    for dependency_id in DEPENDENCY_PACKAGES:
        specs = required_package_specs(dependency_id)
        assert specs[0].package == DEPENDENCY_PACKAGES[dependency_id]
        assert specs[0].version == DEPENDENCY_PACKAGE_PINS[dependency_id]
        expected_companions = len(resolve_companion_packages(dependency_id))
        assert len(specs) == 1 + expected_companions
        for spec in specs:
            assert spec.is_exact()


# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------


def test_every_installed_spec_is_exact_and_named():
    """No argv may carry a bare package name where a pin is required."""
    for dependency_id in DEPENDENCY_PACKAGES:
        argv = build_install_argv(("npm",), dependency_id)
        assert f"{DEPENDENCY_PACKAGES[dependency_id]}@{DEPENDENCY_PACKAGE_PINS[dependency_id]}" in argv
        for companion in resolve_companion_packages(dependency_id):
            companion_argv = build_companion_install_argv(("npm",), dependency_id)
            assert any(companion.to_spec().spec in command for command in companion_argv)


def test_every_manager_emits_an_exact_save_flag():
    """Unpinned in ANY supported manager is the same reproducibility failure.

    The flag is spelled per manager (``--save-exact`` vs yarn's ``--exact``);
    what must hold across all three is that SOME exact-save flag is present, so
    no manager can quietly write a caret or tilde range.
    """
    exact_flags = {"--save-exact", "--exact"}

    for manager in (("npm",), ("pnpm",), ("yarn",)):
        argv = build_install_argv(manager, THREE)
        assert exact_flags & set(argv), (manager, argv)
        for companion_argv in build_companion_install_argv(manager, THREE):
            assert exact_flags & set(companion_argv), (manager, companion_argv)


def test_commands_stay_inside_the_project(project):
    runner = RecordingRunner()

    _installer(project, runner).install_dependency(THREE)

    assert runner.cwds
    for cwd in runner.cwds:
        assert is_contained(project, cwd)


def test_nothing_is_installed_globally(project):
    """Structural answer to a question a dependency ladder invites."""
    runner = RecordingRunner()

    _installer(project, runner).install_dependency(THREE)

    assert DesignDependencyInstaller.installed_globally is False
    for command in runner.commands:
        assert "-g" not in command
        assert "--global" not in command
        assert "--location=global" not in command


def test_an_inexact_pin_runs_no_command(project, monkeypatch):
    """If the pin table is edited to a range, refuse rather than install it."""
    import app.core.design_install as install

    monkeypatch.setitem(install.DEPENDENCY_PACKAGE_PINS, GSAP, "^3.0.0")
    runner = RecordingRunner()

    outcome = _installer(project, runner).install_dependency(GSAP)

    assert outcome.state == INSTALL_FAILED
    assert outcome.reason == REASON_PIN_NOT_EXACT
    assert runner.commands == [], "a non-exact pin must mean no command at all"


# ---------------------------------------------------------------------------
# Part E: GSAP / Three / Lenis + companions -- structural coherence
# ---------------------------------------------------------------------------
#
# A future edit must not be able to add a package without its pin, or a pin
# without its package. These tests assert the RELATION between the maps, so a
# pin bump is fine but a missing pin, an orphan pin, a colliding companion, or a
# wrong section fails loudly.


def test_package_and_pin_maps_have_identical_key_domains():
    """No package without a pin; no pin without a package."""
    assert set(DEPENDENCY_PACKAGES) == set(DEPENDENCY_PACKAGE_PINS)


def test_every_pin_is_exact():
    for dependency_id, version in DEPENDENCY_PACKAGE_PINS.items():
        assert PackageSpec("x", version).is_exact(), (dependency_id, version)


def test_every_runtime_package_lands_in_dependencies():
    """A runtime package is a runtime declaration, never a dev one."""
    for dependency_id in DEPENDENCY_PACKAGES:
        specs = required_package_specs(dependency_id)
        runtime = specs[0]
        assert runtime.package == DEPENDENCY_PACKAGES[dependency_id]
        assert runtime.dependency_section == SECTION_DEPENDENCIES, dependency_id


def test_every_type_companion_lands_in_devdependencies():
    """A type-only companion must not ship to production."""
    for dependency_id, companions in DEPENDENCY_COMPANION_PACKAGES.items():
        for companion in companions:
            assert companion.dependency_section == SECTION_DEV_DEPENDENCIES, (
                dependency_id,
                companion.package,
            )


def test_a_companion_cannot_collide_with_a_runtime_package():
    """A companion name may not also be an allowlisted runtime package."""
    runtime_packages = set(DEPENDENCY_PACKAGES.values())
    for dependency_id, companions in DEPENDENCY_COMPANION_PACKAGES.items():
        for companion in companions:
            assert companion.package not in runtime_packages, (
                dependency_id,
                companion.package,
            )


def test_every_companion_has_an_exact_pin():
    for dependency_id, companions in DEPENDENCY_COMPANION_PACKAGES.items():
        for companion in companions:
            assert companion.to_spec().is_exact(), (dependency_id, companion.package)


def test_every_companion_belongs_to_a_known_dependency():
    """A companion row for an unknown dependency would never be installed."""
    for dependency_id in DEPENDENCY_COMPANION_PACKAGES:
        assert dependency_id in DEPENDENCY_PACKAGES, dependency_id


def test_project_satisfies_dependency_requires_all_companions(project):
    """The conjunctive postcondition: runtime alone is never enough."""
    # three's runtime alone:
    _declare(project, THREE, DEPENDENCY_PACKAGE_PINS[THREE], SECTION_DEPENDENCIES)
    assert project_satisfies_dependency(project, THREE) is False

    # add the companion exactly:
    companion = DEPENDENCY_COMPANION_PACKAGES[THREE][0]
    _declare(
        project,
        companion.package,
        companion.version,
        SECTION_DEV_DEPENDENCIES,
    )
    assert project_satisfies_dependency(project, THREE) is True


def test_no_registry_constraint_can_override_an_exact_pin():
    """A registry-declared range never becomes the installed version."""
    from app.core.design_registry import (
        dependency_specs_are_satisfied,
        resolve_reviewed_dependency_specs,
    )

    # The live SplitText declaration.
    declared = ["gsap@^3.13.0", "@gsap/react@^2.1.2"]

    # The pins the app owns satisfy the constraints...
    assert dependency_specs_are_satisfied(declared) is True
    # ...and the spec's version is retained as a CONSTRAINT only.
    resolved = dict(resolve_reviewed_dependency_specs(declared))
    assert resolved["gsap"] == "^3.13.0"
    # The exact pin is what installs, not the constraint.
    assert DEPENDENCY_PACKAGE_PINS["gsap"] == "3.15.0"
    assert DEPENDENCY_PACKAGE_PINS["gsap_react"] == "2.1.2"


def test_gsap_and_lenis_have_no_companion_but_gsap_react_is_its_own_runtime():
    """The reviewed @gsap/react is a runtime dependency, not a companion."""
    assert resolve_companion_packages(GSAP) == ()
    assert resolve_companion_packages(LENIS) == ()
    # @gsap/react is its own allowlisted runtime id.
    assert DEPENDENCY_PACKAGES["gsap_react"] == "@gsap/react"
    assert required_package_specs("gsap_react")[0].dependency_section == (
        SECTION_DEPENDENCIES
    )


# ---------------------------------------------------------------------------
# Part H: the Impeccable parser runtime pins
# ---------------------------------------------------------------------------


def test_impeccable_parser_pins_are_exact():
    """The four parser modules are exactly pinned, never ranged."""
    from app.core.design_install import (
        IMPECCABLE_PARSER_PACKAGE_PINS,
        impeccable_parser_package_pins,
    )

    assert set(IMPECCABLE_PARSER_PACKAGE_PINS) == {
        "htmlparser2",
        "css-select",
        "css-tree",
        "domutils",
    }
    for package, version in impeccable_parser_package_pins():
        assert PackageSpec(package, version).is_exact(), (package, version)


def test_impeccable_parser_pins_are_not_project_dependencies():
    """They are provisioned at profile setup, never pulled into a build."""
    from app.core.design_install import IMPECCABLE_PARSER_PACKAGE_PINS

    runtime_packages = set(DEPENDENCY_PACKAGES.values())
    for package in IMPECCABLE_PARSER_PACKAGE_PINS:
        assert package not in runtime_packages, package


def test_impeccable_parser_pins_match_the_activation_runtime_list():
    """The pin table and the capability check must name the SAME modules."""
    from app.core.design_activation import PARSER_RUNTIME_PACKAGES
    from app.core.design_install import IMPECCABLE_PARSER_PACKAGE_PINS

    assert set(IMPECCABLE_PARSER_PACKAGE_PINS) == set(PARSER_RUNTIME_PACKAGES)
