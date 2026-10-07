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
    INSTALL_FAILED,
    REGISTRY_INTRODUCED_PACKAGE_PINS,
    REASON_REGISTRY_DEPENDENCY_DRIFT,
    REASON_REGISTRY_PACKAGE_NOT_EXACT,
    SECTION_DEPENDENCIES,
    DesignDependencyInstaller,
    approved_component_dir,
    approved_external_component_dir,
    dependency_delta,
    registry_dependency_delta_is_acceptable,
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
    ):
        self.commands: List[List[str]] = []
        self._writes = dependency_writes or {}
        self._materializes = materializes
        self._component_name = component_name
        self._external_dir = external_dir
        self._exitcode = exitcode
        self._honor_exact = honor_exact

    def run_command(self, project_id, command, cwd=None, env=None, timeout=300.0):
        command = list(command)
        self.commands.append(command)
        root = Path(cwd)

        is_install = command[:2] in (["npm", "install"], ["pnpm", "add"], ["yarn", "add"])
        is_add = "add" in command and not is_install

        if is_install:
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

            if self._materializes and self._component_name:
                target = (
                    approved_external_component_dir(root)
                    if self._external_dir
                    else approved_component_dir(root)
                )
                if target is not None:
                    target.mkdir(parents=True, exist_ok=True)
                    (target / f"{self._component_name}.tsx").write_text(
                        "export const X = 1\n", encoding="utf-8"
                    )

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
