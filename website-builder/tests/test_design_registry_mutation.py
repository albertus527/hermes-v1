"""Batch D3a.5 Part C: registry install must not bypass the dependency boundary.

The pinned shadcn CLI writes npm packages DIRECTLY into a project's
``package.json``. Live proof against shadcn@4.21.0:

    add https://reactbits.dev/r/SplitText-TS-TW
        -> gsap@^3.15.0, @gsap/react@^2.1.2
    add button
        -> cn@^0.4.0, radix-ui@^1.7.0

So "the CLI exited 0" says NOTHING about which packages it introduced. These
tests prove the boundary: snapshot before, verify the DIRECT dependency delta
after, normalize registry ranges to exact application-owned pins, and refuse any
unreviewed package -- in ANY section.

No real subprocess: the runner EMULATES the CLI's manifest writes.
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
    INSTALL_FAILED,
    REGISTRY_INTRODUCED_PACKAGE_PINS,
    REASON_COMPONENTS_NOT_VERIFIED,
    REASON_REGISTRY_DEPENDENCY_DRIFT,
    REASON_REGISTRY_IMPORT_UNRESOLVED,
    REASON_REGISTRY_PACKAGE_NOT_EXACT,
    REASON_REGISTRY_SOURCE_IMPORT_UNREVIEWED,
    SECTION_DEPENDENCIES,
    DesignDependencyInstaller,
    approved_component_dir,
    approved_external_component_dir,
    bare_package_of,
    declared_imports,
    dependency_delta,
    expand_reviewed_component_closure,
    unreviewed_imports,
    registry_dependency_delta_is_acceptable,
    required_registry_packages,
    reviewed_registry_package_pins,
    snapshot_direct_dependencies,
)

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


class RegistryRunner:
    """Emulates the shadcn CLI: writes component files AND package.json deps.

    ``dependency_writes`` maps section -> {package: version} written verbatim
    (the CLI's own floating range, e.g. ``^3.15.0``). ``materializes`` controls
    whether the component file lands. ``normalize_exact`` makes the emulated
    install of an exact spec (``--save-exact``) write the exact version, as a
    real npm install would.
    """

    def __init__(
        self,
        *,
        dependency_writes: dict | None = None,
        materializes: bool = True,
        component_name: str | None = None,
        external_dir: bool = False,
        exitcode: int = 0,
        honor_exact: bool = True,
        materialize_all_in_argv: bool = False,
        install_fails_for: str | None = None,
        source_imports: Sequence[str] = (),
    ):
        self.commands: List[List[str]] = []
        self._writes = dependency_writes or {}
        self._materializes = materializes
        self._component_name = component_name
        self._external_dir = external_dir
        self._exitcode = exitcode
        self._honor_exact = honor_exact
        self._materialize_all_in_argv = materialize_all_in_argv
        self._install_fails_for = install_fails_for
        self._source_imports = tuple(source_imports)

    def run_command(self, project_id, command, cwd=None, env=None, timeout=300.0):
        command = list(command)
        self.commands.append(command)
        root = Path(cwd)

        is_install = command[:2] in (["npm", "install"], ["pnpm", "add"], ["yarn", "add"])
        is_add = "add" in command and not is_install

        if is_install:
            if self._install_fails_for is not None and any(
                self._install_fails_for in arg for arg in command
            ):
                return subprocess.CompletedProcess(
                    args=command, returncode=1, stdout="", stderr=""
                )
            # Normalization install: write the EXACT spec's version.
            manifest = root / "package.json"
            doc = json.loads(manifest.read_text(encoding="utf-8"))
            section = "devDependencies" if "--save-dev" in command else "dependencies"
            for arg in command:
                if arg.startswith("-") or "@" not in arg:
                    continue
                name, _, version = arg.rpartition("@")
                if not name:
                    continue
                if self._honor_exact:
                    doc.setdefault(section, {})[name] = version
                manifest.write_text(json.dumps(doc), encoding="utf-8")
                break
        elif is_add:
            manifest = root / "package.json"
            doc = json.loads(manifest.read_text(encoding="utf-8"))
            for section, packages in self._writes.items():
                for name, version in packages.items():
                    doc.setdefault(section, {})[name] = version
            manifest.write_text(json.dumps(doc), encoding="utf-8")

            if self._materializes:
                if self._materialize_all_in_argv:
                    # The real CLI materializes every component named in its
                    # argv, so an emulation that wants to exercise the reviewed
                    # closure has to do the same.
                    names = [
                        a for a in command if a in ALLOWED_SHADCN_COMPONENTS
                    ]
                elif self._component_name:
                    names = [self._component_name]
                else:
                    names = []
                if names:
                    target = (
                        approved_external_component_dir(root)
                        if self._external_dir
                        else approved_component_dir(root)
                    )
                    if target is not None:
                        target.mkdir(parents=True, exist_ok=True)
                        for name in names:
                            (target / f"{name}.tsx").write_text(
                                self._component_source_text(), encoding="utf-8"
                            )

        return subprocess.CompletedProcess(
            args=command, returncode=self._exitcode, stdout="ok", stderr=""
        )

    def _component_source_text(self) -> str:
        """The emitted source, with the imports this test wants to exercise.

        The default matches a real reviewed builtin (imports ``cn``/``radix-ui``
        plus any ``source_imports`` the test asks for), so the source-import
        boundary can be driven the way a drifted registry would drive it.
        """
        lines = ['import * as React from "react"', 'import { cn } from "cn"']
        for spec in self._source_imports:
            lines.append(f'import {{ X }} from "{spec}"')
        return "\n".join(lines) + "\nexport const X = 1\n"


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
    (root / "components.json").write_text(
        json.dumps(APPROVED_COMPONENTS_JSON), encoding="utf-8"
    )
    return root


def _installer(project: Path, runner) -> DesignDependencyInstaller:
    return DesignDependencyInstaller(runner, "proj-1", project)


# ---------------------------------------------------------------------------
# The snapshot / delta primitives
# ---------------------------------------------------------------------------


def test_a_snapshot_covers_every_dependency_section(project):
    snap = snapshot_direct_dependencies(project)

    assert set(snap) == {
        "dependencies",
        "devDependencies",
        "optionalDependencies",
        "peerDependencies",
    }
    assert snap["dependencies"]["react"] == "19.2.7"


def test_a_delta_reports_new_and_changed_packages():
    before = {"dependencies": {"react": "19.2.7"}}
    after = {"dependencies": {"react": "19.2.7", "gsap": "^3.15.0"}}

    delta = dependency_delta(before, after)

    assert delta == {"dependencies": {"gsap": "^3.15.0"}}


def test_a_package_moved_into_another_section_is_a_delta():
    before = {"dependencies": {}, "devDependencies": {"x": "1.0.0"}}
    after = {"dependencies": {"x": "1.0.0"}, "devDependencies": {}}

    delta = dependency_delta(before, after)

    assert "x" in delta["dependencies"]


def test_an_unreviewed_package_in_the_delta_is_rejected():
    before = {"dependencies": {}}
    after = {"dependencies": {"evil-package": "1.0.0"}}

    ok, offending = registry_dependency_delta_is_acceptable(
        before, after, allowed_packages=["cn"]
    )

    assert ok is False
    assert offending == ("evil-package",)


def test_a_reviewed_package_in_the_wrong_section_is_rejected():
    before = {"dependencies": {}}
    after = {"devDependencies": {"cn": "0.4.0"}}

    ok, offending = registry_dependency_delta_is_acceptable(
        before, after, allowed_packages=["cn"]
    )

    assert ok is False
    assert offending == ("cn",)


def test_a_reviewed_package_in_the_runtime_section_is_accepted():
    before = {"dependencies": {}}
    after = {"dependencies": {"cn": "0.4.0"}}

    ok, offending = registry_dependency_delta_is_acceptable(
        before, after, allowed_packages=["cn"]
    )

    assert ok is True
    assert offending == ()


def test_the_normalization_pins_are_exact():
    pins = reviewed_registry_package_pins()

    for package, version in REGISTRY_INTRODUCED_PACKAGE_PINS.items():
        assert pins[package] == version
    # The allowlisted deps an external component may declare are pinned too.
    assert pins["gsap"] == "3.15.0"
    assert pins["@gsap/react"] == "2.1.2"


# ---------------------------------------------------------------------------
# End to end: the builtin path
# ---------------------------------------------------------------------------


def test_a_builtin_install_introducing_only_reviewed_packages_is_accepted(project):
    """button introduces cn + radix-ui; both are reviewed, then normalized."""
    runner = RegistryRunner(
        dependency_writes={"dependencies": {"cn": "^0.4.0", "radix-ui": "^1.7.0"}},
        materializes=True,
        component_name="button",
    )

    outcome, installed, _ = _installer(project, runner).install_components(["button"])

    assert outcome.state == "installed", outcome.reason
    # The final manifest holds EXACT pins, not the CLI's ranges.
    doc = json.loads((project / "package.json").read_text(encoding="utf-8"))
    assert doc["dependencies"]["cn"] == "0.4.0"
    assert doc["dependencies"]["radix-ui"] == "1.7.0"


def test_a_builtin_install_introducing_an_unreviewed_package_is_refused(project):
    """A registry that writes a package outside the reviewed set fails closed."""
    runner = RegistryRunner(
        dependency_writes={"dependencies": {"cn": "^0.4.0", "surprise-pkg": "^9.9.9"}},
        materializes=True,
        component_name="button",
    )

    outcome, _, _ = _installer(project, runner).install_components(["button"])

    assert outcome.state == INSTALL_FAILED
    assert outcome.reason == REASON_REGISTRY_DEPENDENCY_DRIFT
    assert outcome.installed is False


def test_a_builtin_install_writing_into_the_wrong_section_is_refused(project):
    runner = RegistryRunner(
        dependency_writes={"devDependencies": {"cn": "^0.4.0"}},
        materializes=True,
        component_name="button",
    )

    outcome, _, _ = _installer(project, runner).install_components(["button"])

    assert outcome.state == INSTALL_FAILED
    assert outcome.reason == REASON_REGISTRY_DEPENDENCY_DRIFT


def test_a_builtin_install_with_no_dependency_change_is_accepted(project):
    """A component that introduces no packages (already present) still verifies."""
    runner = RegistryRunner(
        dependency_writes={}, materializes=True, component_name="card"
    )

    outcome, _, _ = _installer(project, runner).install_components(["card"])

    assert outcome.state == "installed", outcome.reason


# ---------------------------------------------------------------------------
# End to end: the external registry path
# ---------------------------------------------------------------------------


class _FakeRequest:
    """A minimal stand-in for RegistryInstallRequest for the installer tests."""

    def __init__(self, *, component_id, locator, dependency_ids, is_builtin=False):
        self.source = "react_bits"
        self.component_id = component_id
        self.registry_locator_id = locator
        self.required_dependency_ids = tuple(dependency_ids)
        self.is_builtin = is_builtin


def _split_text_request():
    return _FakeRequest(
        component_id="SplitText",
        locator="https://reactbits.dev/r/SplitText-TS-TW",
        dependency_ids=("gsap", "gsap_react"),
    )


def test_an_external_install_with_reviewed_packages_is_accepted(project):
    runner = RegistryRunner(
        dependency_writes={
            "dependencies": {"gsap": "^3.15.0", "@gsap/react": "^2.1.2"}
        },
        materializes=True,
        component_name="SplitText",
        external_dir=True,
    )

    outcome = _installer(project, runner).install_external_component(
        _split_text_request()
    )

    assert outcome.state == "installed", outcome.reason
    doc = json.loads((project / "package.json").read_text(encoding="utf-8"))
    assert doc["dependencies"]["gsap"] == "3.15.0"
    assert doc["dependencies"]["@gsap/react"] == "2.1.2"


def test_an_external_install_introducing_an_extra_package_is_refused(project):
    runner = RegistryRunner(
        dependency_writes={
            "dependencies": {
                "gsap": "^3.15.0",
                "@gsap/react": "^2.1.2",
                "tracking-lib": "^1.0.0",
            }
        },
        materializes=True,
        component_name="SplitText",
        external_dir=True,
    )

    outcome = _installer(project, runner).install_external_component(
        _split_text_request()
    )

    assert outcome.state == INSTALL_FAILED
    assert outcome.reason == REASON_REGISTRY_DEPENDENCY_DRIFT


def test_an_external_install_without_materialization_is_not_installed(project):
    runner = RegistryRunner(
        dependency_writes={"dependencies": {"gsap": "^3.15.0"}},
        materializes=False,
        component_name="SplitText",
        external_dir=True,
    )

    outcome = _installer(project, runner).install_external_component(
        _split_text_request()
    )

    assert outcome.state == INSTALL_FAILED
    assert outcome.installed is False


def test_a_builtin_request_never_takes_the_external_path(project):
    """Defence in depth: a builtin request cannot reach the external install."""
    request = _FakeRequest(
        component_id="button", locator="", dependency_ids=(), is_builtin=True
    )
    runner = RegistryRunner()

    outcome = _installer(project, runner).install_external_component(request)

    assert outcome.state == INSTALL_FAILED
    assert runner.commands == [], "a builtin must produce no external command"


def test_the_upstream_range_is_normalized_to_the_exact_pin(project):
    """The CLI writes ^3.15.0; the final manifest must hold 3.15.0."""
    runner = RegistryRunner(
        dependency_writes={"dependencies": {"gsap": "^3.15.0"}},
        materializes=True,
        component_name="SplitText",
        external_dir=True,
    )

    _installer(project, runner).install_external_component(_split_text_request())

    doc = json.loads((project / "package.json").read_text(encoding="utf-8"))
    assert doc["dependencies"]["gsap"] == "3.15.0"
    assert "^" not in doc["dependencies"]["gsap"]


def test_a_duck_typed_request_cannot_widen_the_accepted_set(project):
    """The accepted package set comes from the CONTRACT, not the request object.

    A stand-in whose ``required_dependency_ids`` claims extra allowlisted ids
    must not cause the boundary to accept a package outside the reviewed
    contract. Here the object claims ``three`` (allowlisted), but the reviewed
    SplitText contract does not include it -- so a registry that writes ``three``
    is refused.
    """
    runner = RegistryRunner(
        dependency_writes={
            "dependencies": {
                "gsap": "^3.15.0",
                "@gsap/react": "^2.1.2",
                "three": "^0.186.1",
            }
        },
        materializes=True,
        component_name="SplitText",
        external_dir=True,
    )
    widened = _FakeRequest(
        component_id="SplitText",
        locator="https://reactbits.dev/r/SplitText-TS-TW",
        dependency_ids=("gsap", "gsap_react", "three"),
    )

    outcome = _installer(project, runner).install_external_component(widened)

    assert outcome.state == INSTALL_FAILED
    assert outcome.reason == REASON_REGISTRY_DEPENDENCY_DRIFT


def test_an_external_request_for_an_unknown_contract_runs_nothing(project):
    """A request whose (source, component) has no contract is refused pre-run."""
    runner = RegistryRunner()
    bogus = _FakeRequest(
        component_id="NeverReviewed",
        locator="https://reactbits.dev/r/NeverReviewed-TS-TW",
        dependency_ids=(),
    )

    outcome = _installer(project, runner).install_external_component(bogus)

    assert outcome.state == INSTALL_FAILED
    assert runner.commands == []


# ---------------------------------------------------------------------------
# Part D (built-in shadcn): the emitted SOURCE import closure
# ---------------------------------------------------------------------------
#
# Live proof against shadcn@4.21.0, real starter copy: ``shadcn add dialog``
# writes cn+radix-ui, creates ONLY dialog.tsx -- and dialog.tsx imports
# ``lucide-react`` (never installed) and ``@/components/ui/button`` (never
# created). ``npx tsc -b`` then fails with TS2307 for both. Same class as the
# SplitText @gsap/react gap: a component reported ``installed`` whose build
# cannot resolve. These tests prove the closure is now enforced.


def test_lucide_react_is_a_reviewed_exact_pin():
    """The icon package several builtins import is application-owned, exact."""
    assert REGISTRY_INTRODUCED_PACKAGE_PINS["lucide-react"] == "1.52.0"
    assert reviewed_registry_package_pins()["lucide-react"] == "1.52.0"


def test_the_builtin_import_table_matches_the_reviewed_components():
    from app.core.design_install import REVIEWED_BUILTIN_COMPONENT_IMPORTS

    assert set(REVIEWED_BUILTIN_COMPONENT_IMPORTS) == set(ALLOWED_SHADCN_COMPONENTS)


def test_an_import_package_without_a_pin_is_a_configuration_error():
    """Every package the emitted source imports must have a reviewed exact pin."""
    from app.core.design_install import REVIEWED_BUILTIN_COMPONENT_IMPORTS

    pins = reviewed_registry_package_pins()
    for component, packages in REVIEWED_BUILTIN_COMPONENT_IMPORTS.items():
        for package in packages:
            assert package in pins, (component, package)


def test_the_dialog_closure_pulls_in_its_nested_button():
    """dialog's emitted source imports @/components/ui/button, undeclared."""
    assert expand_reviewed_component_closure(["dialog"]) == ("button", "dialog")
    assert expand_reviewed_component_closure(["button"]) == ("button",)
    assert expand_reviewed_component_closure([]) == ()


def test_the_required_import_set_is_only_what_the_cli_does_not_install():
    """lucide-react is required; cn/radix-ui are CLI-written (normalized only)."""
    assert required_registry_packages(["dialog"]) == ("lucide-react",)
    assert required_registry_packages(["button"]) == ()
    assert required_registry_packages(["accordion", "checkbox"]) == ("lucide-react",)


def test_a_lucide_builtin_install_installs_the_missing_import(project):
    """The whole point: ``dialog`` must end up with lucide-react present EXACT."""
    runner = RegistryRunner(
        dependency_writes={"dependencies": {"cn": "^0.4.0", "radix-ui": "^1.7.0"}},
        materializes=True,
        materialize_all_in_argv=True,
    )

    outcome, installed, _ = _installer(project, runner).install_components(["dialog"])

    assert outcome.state == "installed", outcome.reason
    # The requested component AND its reviewed nested closure materialized.
    assert installed == ("dialog",)
    assert (project / "src/components/ui/dialog.tsx").is_file()
    assert (project / "src/components/ui/button.tsx").is_file()
    doc = json.loads((project / "package.json").read_text(encoding="utf-8"))
    assert doc["dependencies"]["lucide-react"] == "1.52.0", (
        "the emitted source's import must be installed at the exact app pin"
    )
    assert "^" not in doc["dependencies"]["lucide-react"]
    # And the CLI-written helpers are still normalized to their exact pins.
    assert doc["dependencies"]["cn"] == "0.4.0"
    assert doc["dependencies"]["radix-ui"] == "1.7.0"


def test_a_lucide_builtin_whose_import_install_fails_is_not_installed(project):
    """If the exact-pin install of the required import fails, refuse."""
    runner = RegistryRunner(
        dependency_writes={"dependencies": {"cn": "^0.4.0", "radix-ui": "^1.7.0"}},
        materializes=True,
        materialize_all_in_argv=True,
        install_fails_for="lucide-react",
    )

    outcome, _, _ = _installer(project, runner).install_components(["dialog"])

    assert outcome.state == INSTALL_FAILED
    assert outcome.reason == REASON_REGISTRY_IMPORT_UNRESOLVED
    assert outcome.installed is False


def test_a_nested_component_that_never_materializes_is_not_installed(project):
    """dialog imports button; if only dialog lands, the install is incomplete."""
    runner = RegistryRunner(
        dependency_writes={"dependencies": {"cn": "^0.4.0", "radix-ui": "^1.7.0"}},
        materializes=True,
        component_name="dialog",  # ONLY dialog -- button never written
    )

    outcome, _, _ = _installer(project, runner).install_components(["dialog"])

    assert outcome.state == INSTALL_FAILED
    assert outcome.reason == REASON_COMPONENTS_NOT_VERIFIED


def test_a_plain_builtin_needs_no_extra_import(project):
    """A builtin whose source imports nothing beyond cn/radix-ui is unchanged."""
    runner = RegistryRunner(
        dependency_writes={"dependencies": {"cn": "^0.4.0", "radix-ui": "^1.7.0"}},
        materializes=True,
        component_name="button",
    )

    outcome, _, _ = _installer(project, runner).install_components(["button"])

    assert outcome.state == "installed", outcome.reason
    doc = json.loads((project / "package.json").read_text(encoding="utf-8"))
    assert "lucide-react" not in doc.get("dependencies", {})


def test_the_nested_closure_cannot_be_widened_by_upstream(project):
    """The closure comes from the app table, not from any registry metadata.

    A component with no reviewed nested entry contributes no extra argv
    component, even if a registry started declaring one.
    """
    runner = RegistryRunner(
        dependency_writes={"dependencies": {"cn": "^0.4.0"}},
        materializes=True,
        materialize_all_in_argv=True,
    )

    _installer(project, runner).install_components(["card"])

    argv = next(c for c in runner.commands if "add" in c)
    components = [a for a in argv if a in ALLOWED_SHADCN_COMPONENTS]
    assert components == ["card"], "no unreviewed nested component may be added"


def test_a_required_import_with_no_reviewed_pin_fails_closed(project, monkeypatch):
    """Defence in depth: if a reviewed import lost its pin, refuse -- never
    install at an upstream-suggested version.

    ``required_registry_packages`` names a package; if that package is absent
    from ``reviewed_registry_package_pins`` the install must fail BEFORE running
    any command rather than falling back to whatever upstream declared.
    """
    from app.core.design_install import reviewed_registry_package_pins

    original = reviewed_registry_package_pins()
    monkeypatch.setattr(
        "app.core.design_install.reviewed_registry_package_pins",
        lambda: {k: v for k, v in original.items() if k != "lucide-react"},
    )

    runner = RegistryRunner(
        dependency_writes={"dependencies": {"cn": "^0.4.0", "radix-ui": "^1.7.0"}},
        materializes=True,
        materialize_all_in_argv=True,
    )

    outcome, _, _ = _installer(project, runner).install_components(["dialog"])

    assert outcome.state == INSTALL_FAILED
    assert outcome.reason == REASON_REGISTRY_IMPORT_UNRESOLVED
    # No normalization/install argv was run for the unpinned package.
    assert not any("lucide-react" in arg for c in runner.commands for arg in c)


# ---------------------------------------------------------------------------
# Emitted-SOURCE import boundary: the INSTALLATION is untrusted
# ---------------------------------------------------------------------------
#
# The identity is reviewed; the installation is not. A registry may materialize
# a file that imports a package it never declared, so the manifest delta guard
# sees nothing. These tests prove the emitted source itself is checked.


def test_bare_package_of_distinguishes_packages_from_paths():
    assert bare_package_of("react") == "react"
    assert bare_package_of("gsap/ScrollTrigger") == "gsap"
    assert bare_package_of("@gsap/react") == "@gsap/react"
    assert bare_package_of("@/components/ui/button") is None
    assert bare_package_of("./x") is None
    assert bare_package_of("../x") is None
    assert bare_package_of("/abs") is None
    assert bare_package_of("") is None
    assert bare_package_of(None) is None
    assert bare_package_of("@") is None
    assert bare_package_of("@scope") is None


def test_declared_imports_finds_every_form():
    src = (
        'import * as React from "react"\n'
        'import { gsap } from "gsap"\n'
        'import { ScrollTrigger } from "gsap/ScrollTrigger"\n'
        'import { useGSAP } from "@gsap/react"\n'
        'import { Button } from "@/components/ui/button"\n'
        'import "./styles.css"\n'
        'import "side-effect-pkg"\n'
        'export { x } from "re-exported-pkg"\n'
    )
    assert declared_imports(src) == (
        "@gsap/react", "gsap", "re-exported-pkg", "react", "side-effect-pkg",
    )


def test_unreviewed_imports_names_only_what_is_not_allowed():
    src = 'import { gsap } from "gsap"\nimport { X } from "evil-lib"\n'
    assert unreviewed_imports(src, allowed_packages=["gsap"]) == ("evil-lib",)
    assert unreviewed_imports(src, allowed_packages=["gsap", "evil-lib"]) == ()


def test_an_approved_builtin_whose_source_drifts_is_refused(project):
    """button is reviewed; its emitted source importing an unreviewed package
    must fail closed even though package.json is unchanged."""
    runner = RegistryRunner(
        dependency_writes={"dependencies": {"cn": "^0.4.0", "radix-ui": "^1.7.0"}},
        materializes=True,
        component_name="button",
        source_imports=("brand-new-unreviewed-pkg",),
    )

    outcome, _, _ = _installer(project, runner).install_components(["button"])

    assert outcome.state == INSTALL_FAILED
    assert outcome.reason == REASON_REGISTRY_SOURCE_IMPORT_UNREVIEWED
    assert outcome.installed is False


def test_a_clean_builtin_source_is_accepted(project):
    """The reviewed source (cn/radix-ui/lucide-react only) is accepted."""
    runner = RegistryRunner(
        dependency_writes={"dependencies": {"cn": "^0.4.0", "radix-ui": "^1.7.0"}},
        materializes=True,
        component_name="button",
    )

    outcome, _, _ = _installer(project, runner).install_components(["button"])

    assert outcome.state == "installed", outcome.reason


def test_a_lucide_builtin_source_that_imports_lucide_is_accepted(project):
    """lucide-react is reviewed, so a source importing it is fine."""
    runner = RegistryRunner(
        dependency_writes={"dependencies": {"cn": "^0.4.0", "radix-ui": "^1.7.0"}},
        materializes=True,
        materialize_all_in_argv=True,
        source_imports=("lucide-react",),
    )

    outcome, _, _ = _installer(project, runner).install_components(["dialog"])

    assert outcome.state == "installed", outcome.reason


def test_an_external_component_source_drift_is_refused(project):
    """SplitText's contract packages are allowed; a new import is not."""
    runner = RegistryRunner(
        dependency_writes={"dependencies": {"gsap": "^3.15.0", "@gsap/react": "^2.1.2"}},
        materializes=True,
        component_name="SplitText",
        external_dir=True,
        source_imports=("evil-tracking-lib",),
    )

    outcome = _installer(project, runner).install_external_component(_split_text_request())

    assert outcome.state == INSTALL_FAILED
    assert outcome.reason == REASON_REGISTRY_SOURCE_IMPORT_UNREVIEWED


def test_an_external_component_with_only_contract_imports_is_accepted(project):
    """SplitText importing only its reviewed contract packages is accepted."""
    runner = RegistryRunner(
        dependency_writes={"dependencies": {"gsap": "^3.15.0", "@gsap/react": "^2.1.2"}},
        materializes=True,
        component_name="SplitText",
        external_dir=True,
        source_imports=("gsap", "@gsap/react"),
    )

    outcome = _installer(project, runner).install_external_component(_split_text_request())

    assert outcome.state == "installed", outcome.reason


# ---------------------------------------------------------------------------
# Removal mutation: an install must never DELETE a project dependency
# ---------------------------------------------------------------------------
#
# `dependency_delta` reports what `after` CONTAINS, so a REMOVAL is invisible to
# it. A registry install may ADD its reviewed packages; it must never remove a
# pre-existing project dependency. These tests prove removals are caught.


def test_removed_direct_dependencies_reports_every_removal():
    from app.core.design_install import removed_direct_dependencies

    before = {"dependencies": {"react": "19.2.7", "cn": "0.4.0"},
              "devDependencies": {"vite": "8.2.0"}}
    after = {"dependencies": {"cn": "0.4.0"}, "devDependencies": {}}

    removed = removed_direct_dependencies(before, after)

    assert removed["dependencies"] == ("react",)
    assert removed["devDependencies"] == ("vite",)


def test_a_removal_is_rejected_by_the_delta_guard():
    before = {"dependencies": {"react": "19.2.7", "cn": "0.4.0"}}
    after = {"dependencies": {"cn": "0.4.0"}}

    ok, offending = registry_dependency_delta_is_acceptable(
        before, after, allowed_packages=["cn", "radix-ui"]
    )

    assert ok is False
    assert offending == ("react",)


def test_an_install_that_removes_a_project_dependency_is_refused(project):
    """The CLI adds its reviewed packages but drops a pre-existing dep."""
    runner = RegistryRunner(
        dependency_writes={"dependencies": {"cn": "^0.4.0", "radix-ui": "^1.7.0"}},
        materializes=True,
        component_name="button",
    )
    # Make the emulated CLI also remove `react`.
    _orig = runner.run_command

    def run_and_drop(pid, command, cwd=None, env=None, timeout=300.0):
        result = _orig(pid, command, cwd=cwd, env=env, timeout=timeout)
        if "add" in list(command):
            manifest = Path(cwd) / "package.json"
            doc = json.loads(manifest.read_text(encoding="utf-8"))
            doc.get("dependencies", {}).pop("react", None)
            manifest.write_text(json.dumps(doc), encoding="utf-8")
        return result

    runner.run_command = run_and_drop

    outcome, _, _ = _installer(project, runner).install_components(["button"])

    assert outcome.state == INSTALL_FAILED
    assert outcome.reason == REASON_REGISTRY_DEPENDENCY_DRIFT
    assert outcome.installed is False


def test_an_install_that_only_adds_reviewed_packages_is_accepted(project):
    """The no-removal path is unchanged: adding reviewed packages still passes."""
    runner = RegistryRunner(
        dependency_writes={"dependencies": {"cn": "^0.4.0", "radix-ui": "^1.7.0"}},
        materializes=True,
        component_name="button",
    )

    outcome, _, _ = _installer(project, runner).install_components(["button"])

    assert outcome.state == "installed", outcome.reason


def test_a_removal_during_normalization_is_refused(project):
    """Normalization runs a package manager AFTER the delta check; if it drops a
    project dependency, only the whole-operation removal check can see it."""
    runner = RegistryRunner(
        dependency_writes={"dependencies": {"cn": "^0.4.0"}},
        materializes=True,
        component_name="button",
    )
    _orig = runner.run_command

    def run_and_drop_on_normalize(pid, command, cwd=None, env=None, timeout=300.0):
        result = _orig(pid, command, cwd=cwd, env=env, timeout=timeout)
        if list(command)[:2] in (["npm", "install"], ["pnpm", "add"], ["yarn", "add"]):
            manifest = Path(cwd) / "package.json"
            doc = json.loads(manifest.read_text(encoding="utf-8"))
            doc.get("dependencies", {}).pop("react", None)
            manifest.write_text(json.dumps(doc), encoding="utf-8")
        return result

    runner.run_command = run_and_drop_on_normalize

    outcome, _, _ = _installer(project, runner).install_components(["button"])

    assert outcome.state == INSTALL_FAILED
    assert outcome.reason == REASON_REGISTRY_DEPENDENCY_DRIFT
    assert outcome.installed is False
