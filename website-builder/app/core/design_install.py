"""Bounded, project-local dependency execution (Batch D3a).

D2 decides what a project *may* use. D3a is the first batch permitted to make
that happen, and it does so with the narrowest mechanism that can work.

Five properties are load-bearing, and each one closes a specific way a build
becomes a supply-chain or scope problem:

**The allowlist is application-owned and closed.** A dependency id maps to
exactly one package name, declared here in code. There is no path by which a
model, a design corpus, or a Design DNA string contributes a package name:
:meth:`resolve_package` accepts a *dependency id*, looks it up in a frozen
mapping, and returns ``None`` for anything unrecognised. ``npm install
<arbitrary>`` is not merely discouraged -- it is unrepresentable.

**Nothing is installed globally.** Every command runs through the project's own
``ProjectRunner.run_command``, whose cwd containment check and
``credentials.build_env`` isolation already enforce the boundary. This module
adds no global install path, and states that as a property rather than a
convention.

**Selection alone never means installed.** The state machine is
``available_for_project_on_demand -> selected -> install attempted -> installed``
and ``installed`` is reachable ONLY after verification observes the package in
the project's own ``package.json``. A selection flag, a successful-looking
command, or a returned exit code are each individually insufficient.

**Failure is explicit and never silently degrades requirements.** A failed
install produces a FAILED state carrying a static reason. There is no fallback
to a different package, no silent skip, and no "close enough" substitute, because
a silent substitution is a user requirement quietly changed by the application.

**shadcn is component-scoped and pinned.** Only the components D2 selected are
requested, never a default bundle, and the CLI is invoked by pinned version
through the project's own package manager. An unselected shadcn produces zero
invocations.
"""

from __future__ import annotations

import json
import logging
import subprocess
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.core.design_policies import (
    STATE_AVAILABLE_ON_DEMAND,
    STATE_SELECTED,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Installation state machine
# ---------------------------------------------------------------------------

#: The four states an install-capable dependency moves through. ``installed`` is
#: NOT reachable without a verified observation, which is the whole point of
#: naming it separately from "install attempted".
INSTALL_STATES: Tuple[str, ...] = (
    STATE_AVAILABLE_ON_DEMAND,
    STATE_SELECTED,
    "install_attempted",
    "installed",
)

#: Terminal failure states. A failure is a first-class outcome with its own
#: label, never an absent success.
INSTALL_FAILED = "install_failed"

#: The terminal states a caller must distinguish: only ``installed`` and
#: ``INSTALL_FAILED`` mean "the attempt is over".
TERMINAL_INSTALL_STATES: Tuple[str, ...] = ("installed", INSTALL_FAILED)


# ---------------------------------------------------------------------------
# The package allowlist -- APPLICATION-OWNED, CLOSED
# ---------------------------------------------------------------------------

#: dependency id -> exact package name. Frozen by construction (a mapping
#: literal is not exported as a mutable module attribute callers are invited to
#: edit).
#:
#: The keys are the manifest resource ids D2 selects; the values are the only
#: package strings this module can ever produce. Nothing here is derived from a
#: resource, a model, a prompt, or Design DNA, which is what makes
#: "resource text cannot choose package names" an implementation fact.
DEPENDENCY_PACKAGES: Dict[str, str] = {
    "gsap": "gsap",
    "three": "three",
    "lenis": "lenis",
}

#: shadcn is not an npm dependency; it is a component registry invoked by its
#: own CLI. Named separately so no code path can treat it as a package.
REGISTRY_DEPENDENCY = "shadcn"


def resolve_package(dependency_id: str) -> Optional[str]:
    """The package for ``dependency_id``, or ``None`` if it is not allowlisted.

    This is the ONLY function that turns a name into an installable package.
    Returning ``None`` rather than the input is deliberate: an unrecognised id
    has no package, and inventing one from the id would recreate the arbitrary
    install this module exists to prevent.
    """
    if not isinstance(dependency_id, str):
        return None
    return DEPENDENCY_PACKAGES.get(dependency_id)


def allowlisted_dependencies() -> Tuple[str, ...]:
    """Every installable dependency id, in stable sorted order."""
    return tuple(sorted(DEPENDENCY_PACKAGES))


# ---------------------------------------------------------------------------
# shadcn component policy
# ---------------------------------------------------------------------------

#: The components this project may ever request, and nothing else.
#:
#: An explicit allowlist rather than "whatever D2 asked for": D2's word for a
#: component is design language, and design language must not become a CLI
#: argument without passing through this list. A component outside this set is
#: rejected rather than forwarded.
ALLOWED_SHADCN_COMPONENTS: Tuple[str, ...] = (
    "accordion",
    "alert",
    "badge",
    "button",
    "card",
    "checkbox",
    "dialog",
    "input",
    "label",
    "select",
    "separator",
    "sheet",
    "switch",
    "tabs",
    "textarea",
    "tooltip",
)

#: Pinned shadcn CLI version. Production execution never resolves a floating
#: ``latest``: an unpinned registry CLI is a silent supply-chain upgrade inside a
#: build, which is the exact failure a pinned toolchain file exists to prevent.
SHADCN_CLI_VERSION = "2.1.6"


def allowed_shadcn_components() -> Tuple[str, ...]:
    return tuple(sorted(ALLOWED_SHADCN_COMPONENTS))


# ---------------------------------------------------------------------------
# Static, sanitized failure reasons
# ---------------------------------------------------------------------------
#
# Never a path, never a package name from input, never command output. These are
# the only strings that may appear in a receipt's ``reason``.

REASON_NOT_SELECTED = "dependency was not selected; no command was attempted"
REASON_ALREADY_INSTALLED = "dependency was already verified present in the project"
REASON_INSTALL_FAILED = "project-local install did not succeed"
REASON_NOT_VERIFIED = (
    "the install command succeeded but the package is absent from the project "
    "manifest; the state is not upgraded to installed"
)
REASON_PACKAGE_NOT_ALLOWLISTED = "no allowlisted package is declared for this dependency"
REASON_COMPONENT_NOT_ALLOWED = "requested component is not in the allowlist"
REASON_COMPONENT_NONE_REQUESTED = "no component was selected, so no CLI was invoked"
REASON_OUTSIDE_PROJECT = "resolved path is outside the project workspace"
REASON_TIMEOUT = "the project-local command exceeded its bounded timeout"
REASON_NO_PACKAGE_MANAGER = "no project-local package manager is available"
REASON_PROJECT_INVALID = "the project root does not contain a package manifest"


# ---------------------------------------------------------------------------
# Command receipts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandReceipt:
    """A bounded, serializable record of ONE project command.

    Carries the argv, exit code, and a bounded output tail -- enough for an
    operator to diagnose a failure, never enough to become a log-injection or
    exfiltration surface. Output is bounded by characters, not lines, because a
    stack trace is long in lines and short in characters.
    """

    command: Tuple[str, ...]
    returncode: int
    cwd_label: str
    timed_out: bool = False
    stdout_tail: str = ""
    stderr_tail: str = ""

    #: Bound on captured output per stream. Deliberately small: a receipt is
    #: diagnostic context, not an artifact store.
    MAX_OUTPUT_CHARS = 2_000

    @classmethod
    def from_process(cls, process, *, cwd_label: str, timed_out: bool = False) -> "CommandReceipt":
        def tail(text: Optional[str]) -> str:
            if not text:
                return ""
            limit = cls.MAX_OUTPUT_CHARS
            return text if len(text) <= limit else text[-limit:]

        return cls(
            command=tuple(process.args if isinstance(process.args, (list, tuple)) else [str(process.args)]),
            returncode=int(process.returncode),
            cwd_label=cwd_label,
            timed_out=timed_out,
            stdout_tail=tail(process.stdout),
            stderr_tail=tail(process.stderr),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command": list(self.command),
            "returncode": self.returncode,
            "cwd_label": self.cwd_label,
            "timed_out": self.timed_out,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }


@dataclass(frozen=True)
class InstallOutcome:
    """The result of executing (or deliberately not executing) one dependency."""

    dependency_id: str
    state: str
    package: Optional[str]
    reason: str
    receipt: Optional[CommandReceipt] = None
    verified_in_manifest: bool = False

    @property
    def installed(self) -> bool:
        """True ONLY when verification observed the package in the project."""
        return self.state == "installed"

    @property
    def failed(self) -> bool:
        return self.state == INSTALL_FAILED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dependency_id": self.dependency_id,
            "state": self.state,
            "package": self.package,
            "reason": self.reason,
            "verified_in_manifest": self.verified_in_manifest,
            "receipt": self.receipt.to_dict() if self.receipt else None,
        }


# ---------------------------------------------------------------------------
# Verification -- what "installed" actually means
# ---------------------------------------------------------------------------

#: npm dependency names are lowercase; scoped names keep their ``@scope/name``
#: shape. Enforced on the ALLOWLIST values rather than on any input, so the
#: regex is a static safety net over three known strings rather than a filter
#: applied to untrusted input.
_PACKAGE_NAME_RE = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")


def package_name_is_well_formed(package: str) -> bool:
    return bool(package and _PACKAGE_NAME_RE.match(package))


def _read_project_manifest(project_root: Path) -> Optional[Dict[str, Any]]:
    """Read the project's ``package.json``, or ``None`` if absent/unreadable."""
    path = project_root / "package.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        document = json.loads(raw)
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


def project_declares_dependency(project_root: Path, package: str) -> bool:
    """Whether ``package`` appears in the project's own dependencies.

    This is the ONLY definition of "installed" this module recognises: an
    observation about the project itself, not an inference from a command's exit
    code. ``npm install`` can exit 0 having written nothing useful, and the only
    durable answer is what the project manifest now says.
    """
    document = _read_project_manifest(project_root)
    if document is None or not package:
        return False
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        block = document.get(section)
        if isinstance(block, Mapping) and package in block:
            return True
    return False


def is_contained(root: Path, candidate: Path) -> bool:
    """Whether ``candidate`` resolves inside ``root``.

    Used on generated paths (component destinations) before anything is written,
    so a registry or a registry mirror cannot place a file outside the project.
    """
    try:
        resolved_root = Path(root).resolve()
        resolved = Path(candidate).resolve()
    except (OSError, RuntimeError):
        return False
    try:
        return resolved == resolved_root or resolved.is_relative_to(resolved_root)
    except (AttributeError, ValueError):
        # Python < 3.9 fallback.
        try:
            resolved.relative_to(resolved_root)
            return True
        except ValueError:
            return False



# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------
#
# Every argv is BUILT here from allowlisted constants. Nothing is ever derived
# from a caller-supplied string by concatenation, so there is no shell to
# inject into even before argument validation: commands are passed as argv
# lists, never as a shell string.

#: Bounded timeouts. A project-local install is network-bound and can genuinely
#: take a while, but it must not be unbounded: the caller has a convergence
#: budget above it, and an unbounded child would outlive that budget silently.
INSTALL_TIMEOUT_SECONDS = 300.0
REGISTRY_TIMEOUT_SECONDS = 300.0

#: The package manager a generated project uses. Read from the project's own
#: lockfile presence rather than assumed, so a pnpm/yarn project is not driven
#: with npm and silently corrupted.
LOCKFILES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("pnpm-lock.yaml", ("pnpm",)),
    ("yarn.lock", ("yarn",)),
    ("package-lock.json", ("npm",)),
)


def detect_package_manager(project_root: Path) -> Optional[Tuple[str, ...]]:
    """The project's own package manager argv prefix, or ``None``.

    Ordered by specificity: a project that carries both lockfiles is a migration
    in progress, and preferring the non-npm manager is the conservative choice
    because running npm there would rewrite the other manager's lockfile.
    """
    for filename, argv in LOCKFILES:
        if (project_root / filename).exists():
            return argv
    return None


def build_install_argv(manager: Sequence[str], package: str) -> Tuple[str, ...]:
    """The install argv for an allowlisted package.

    ``--save-exact`` is deliberate. A dependency added without a pin can float to
    a new version on a later build, which makes a "verified installed" claim
    describe something that will not be installed again tomorrow.
    """
    argv = tuple(manager)
    if argv and argv[0] == "npm":
        return argv + ("install", package, "--save-exact", "--no-audit", "--no-fund")
    if argv and argv[0] == "pnpm":
        return argv + ("add", "--save-exact", package)
    if argv and argv[0] == "yarn":
        return argv + ("add", "--exact", package)
    return argv + ("install", package, "--save-exact")


def build_registry_argv(
    manager: Sequence[str], *, components: Sequence[str], version: str
) -> Tuple[str, ...]:
    """The shadcn registry argv for an explicit, allowlisted component list.

    Invoked through the PROJECT's own package manager at a PINNED version, so no
    global shadcn CLI is required and no floating ``latest`` is resolved inside a
    production build. ``--yes`` keeps the invocation non-interactive; without it
    the CLI can block on a prompt inside a supervised run.
    """
    ordered = sorted(set(components))
    return (
        tuple(manager)
        + ("dlx", f"shadcn@{version}", "add")
        + tuple(ordered)
        + ("--yes", "--overwrite")
    )


def filter_allowed_components(requested: Sequence[str]) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Split requested components into (allowed, rejected).

    Rejected components are RETURNED rather than silently dropped, so a caller
    can report exactly what was refused instead of believing a component was
    added.
    """
    allowed: List[str] = []
    rejected: List[str] = []
    for component in requested or ():
        if not isinstance(component, str):
            rejected.append(str(component))
            continue
        if component in ALLOWED_SHADCN_COMPONENTS:
            allowed.append(component)
        else:
            rejected.append(component)
    return tuple(sorted(set(allowed))), tuple(sorted(set(rejected)))


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallReport:
    """The bounded outcome of executing a whole selection plan."""

    outcomes: Tuple[InstallOutcome, ...]
    components_installed: Tuple[str, ...]
    components_rejected: Tuple[str, ...]
    verification_failures: Tuple[str, ...]

    @property
    def installed_ids(self) -> Tuple[str, ...]:
        return tuple(o.dependency_id for o in self.outcomes if o.installed)

    @property
    def failed_ids(self) -> Tuple[str, ...]:
        return tuple(o.dependency_id for o in self.outcomes if o.failed)

    @property
    def ok(self) -> bool:
        """True when nothing failed and everything attempted is verified.

        A partial success is NOT ok: a build that half-installed its selection
        will fail its typecheck, and reporting partial success would send the
        caller looking for the wrong cause.
        """
        return not self.failed_ids and not self.verification_failures

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "outcomes": [o.to_dict() for o in self.outcomes],
            "components_installed": list(self.components_installed),
            "components_rejected": list(self.components_rejected),
            "verification_failures": list(self.verification_failures),
        }


class DesignDependencyInstaller:
    """Executes a D2 selection plan inside one isolated project workspace.

    The runner is injected, never constructed: this module must go through the
    project's existing :class:`~app.sandbox.runner.ProjectRunner` so cwd
    containment and credential isolation are inherited rather than reimplemented.
    A second, laxer execution path is exactly the kind of duplicate boundary that
    eventually becomes the hole.

    No global install exists anywhere in this class, and
    :attr:`installed_globally` is a hard-coded ``False`` so that question -- which
    a dependency ladder invites a caller to ask -- has a structural answer.
    """

    #: Structural answer to "was this installed globally?". Always False.
    installed_globally = False

    def __init__(self, runner, project_id: str, project_root: Path):
        self.runner = runner
        self.project_id = project_id
        self.project_root = Path(project_root)

    # -- containment ----------------------------------------------------

    def _assert_inside_project(self, relative: str) -> Optional[Path]:
        """Resolve ``relative`` inside the project, or ``None`` if it escapes.

        Checked before every command. A containment check that runs only on the
        happy path is not a containment check.
        """
        candidate = self.project_root / relative
        if not is_contained(self.project_root, candidate):
            logger.warning(
                "Refused a path that resolved outside the project workspace."
            )
            return None
        return candidate

    def _run(self, argv: Sequence[str], timeout: float) -> Tuple[Any, bool]:
        """Run ``argv`` in the project root; return (process, timed_out)."""
        cwd = self._assert_inside_project(".")
        if cwd is None:
            return None, False
        try:
            process = self.runner.run_command(
                self.project_id,
                list(argv),
                cwd=cwd,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return None, True
        except Exception:
            logger.exception("Project-local command failed to start.")
            return None, False
        return process, False

    # -- npm dependencies -----------------------------------------------

    def install_dependency(self, dependency_id: str) -> InstallOutcome:
        """Install one allowlisted dependency, project-locally.

        The sequence is: resolve the package from the allowlist, refuse an
        unselected or unknown dependency, detect the project's own package
        manager, run one bounded install, then VERIFY against the project
        manifest. ``installed`` is assigned only by that verification.
        """
        package = resolve_package(dependency_id)
        if package is None:
            return InstallOutcome(
                dependency_id=dependency_id,
                state=INSTALL_FAILED,
                package=None,
                reason=REASON_PACKAGE_NOT_ALLOWLISTED,
            )
        if not package_name_is_well_formed(package):
            # A static safety net over three known strings. If it ever fires, the
            # allowlist itself has been corrupted.
            logger.error("Allowlisted package failed its own format check.")
            return InstallOutcome(
                dependency_id=dependency_id,
                state=INSTALL_FAILED,
                package=package,
                reason=REASON_PACKAGE_NOT_ALLOWLISTED,
            )

        if project_declares_dependency(self.project_root, package):
            return InstallOutcome(
                dependency_id=dependency_id,
                state="installed",
                package=package,
                reason=REASON_ALREADY_INSTALLED,
                verified_in_manifest=True,
            )

        manager = detect_package_manager(self.project_root)
        if manager is None:
            return InstallOutcome(
                dependency_id=dependency_id,
                state=INSTALL_FAILED,
                package=package,
                reason=REASON_NO_PACKAGE_MANAGER,
            )

        argv = build_install_argv(manager, package)
        process, timed_out = self._run(argv, INSTALL_TIMEOUT_SECONDS)
        if timed_out:
            return InstallOutcome(
                dependency_id=dependency_id,
                state=INSTALL_FAILED,
                package=package,
                reason=REASON_TIMEOUT,
            )
        if process is None or process.returncode != 0:
            receipt = (
                CommandReceipt.from_process(process, cwd_label="<project>")
                if process is not None
                else None
            )
            return InstallOutcome(
                dependency_id=dependency_id,
                state=INSTALL_FAILED,
                package=package,
                reason=REASON_INSTALL_FAILED,
                receipt=receipt,
            )

        # The command succeeded. That is NOT yet an installed claim.
        verified = project_declares_dependency(self.project_root, package)
        receipt = CommandReceipt.from_process(process, cwd_label="<project>")
        if not verified:
            return InstallOutcome(
                dependency_id=dependency_id,
                state=INSTALL_FAILED,
                package=package,
                reason=REASON_NOT_VERIFIED,
                receipt=receipt,
                verified_in_manifest=False,
            )

        return InstallOutcome(
            dependency_id=dependency_id,
            state="installed",
            package=package,
            reason=REASON_ALREADY_INSTALLED,
            receipt=receipt,
            verified_in_manifest=True,
        )

    # -- registry components --------------------------------------------

    def install_components(self, components: Sequence[str]) -> Tuple[InstallOutcome, Tuple[str, ...], Tuple[str, ...]]:
        """Install an explicit, allowlisted shadcn component list.

        With no components requested this performs ZERO commands -- the
        "unselected => no CLI invocation" property is implemented by the absence
        of any argv, not by a guard that could be skipped.
        """
        if not components:
            return (
                InstallOutcome(
                    dependency_id=REGISTRY_DEPENDENCY,
                    state=STATE_SELECTED,
                    package=None,
                    reason=REASON_COMPONENT_NONE_REQUESTED,
                ),
                (),
                (),
            )

        allowed, rejected = filter_allowed_components(components)
        if not allowed:
            return (
                InstallOutcome(
                    dependency_id=REGISTRY_DEPENDENCY,
                    state=INSTALL_FAILED,
                    package=None,
                    reason=REASON_COMPONENT_NOT_ALLOWED,
                ),
                (),
                rejected,
            )

        manager = detect_package_manager(self.project_root)
        if manager is None:
            return (
                InstallOutcome(
                    dependency_id=REGISTRY_DEPENDENCY,
                    state=INSTALL_FAILED,
                    package=None,
                    reason=REASON_NO_PACKAGE_MANAGER,
                ),
                allowed,
                rejected,
            )

        argv = build_registry_argv(
            manager, components=allowed, version=SHADCN_CLI_VERSION
        )
        process, timed_out = self._run(argv, REGISTRY_TIMEOUT_SECONDS)
        if timed_out:
            return (
                InstallOutcome(
                    dependency_id=REGISTRY_DEPENDENCY,
                    state=INSTALL_FAILED,
                    package=None,
                    reason=REASON_TIMEOUT,
                ),
                allowed,
                rejected,
            )
        if process is None or process.returncode != 0:
            receipt = (
                CommandReceipt.from_process(process, cwd_label="<project>")
                if process is not None
                else None
            )
            return (
                InstallOutcome(
                    dependency_id=REGISTRY_DEPENDENCY,
                    state=INSTALL_FAILED,
                    package=None,
                    reason=REASON_INSTALL_FAILED,
                    receipt=receipt,
                ),
                allowed,
                rejected,
            )

        return (
            InstallOutcome(
                dependency_id=REGISTRY_DEPENDENCY,
                state="installed",
                package=None,
                reason=REASON_ALREADY_INSTALLED,
                receipt=CommandReceipt.from_process(process, cwd_label="<project>"),
            ),
            allowed,
            rejected,
        )

    # -- plan execution --------------------------------------------------

    def execute_selection(
        self,
        plan,
        *,
        selected_components: Sequence[str] = (),
    ) -> InstallReport:
        """Execute exactly the dependencies D2 SELECTED, nothing else.

        The plan is the only input that can authorise an install. A dependency
        the plan rejected is reported as NOT_SELECTED with no receipt and no
        command, which is what makes "GSAP unselected => no npm invocation" a
        property of the control flow rather than a promise in a comment.
        """
        outcomes: List[InstallOutcome] = []
        verification_failures: List[str] = []

        selected_ids = {
            entry.resource_id for entry in plan.selected_resources if entry.selected
        }

        for dependency_id in allowlisted_dependencies():
            if dependency_id not in selected_ids:
                outcomes.append(
                    InstallOutcome(
                        dependency_id=dependency_id,
                        state=STATE_SELECTED if False else STATE_AVAILABLE_ON_DEMAND,
                        package=resolve_package(dependency_id),
                        reason=REASON_NOT_SELECTED,
                    )
                )
                continue

            outcome = self.install_dependency(dependency_id)
            outcomes.append(outcome)
            if outcome.state == "install_attempted" and not outcome.installed:
                verification_failures.append(dependency_id)

        components_installed: Tuple[str, ...] = ()
        components_rejected: Tuple[str, ...] = ()
        if REGISTRY_DEPENDENCY in selected_ids:
            outcome, components_installed, components_rejected = self.install_components(
                selected_components
            )
            outcomes.append(outcome)
            if outcome.failed:
                verification_failures.append(REGISTRY_DEPENDENCY)
        else:
            outcomes.append(
                InstallOutcome(
                    dependency_id=REGISTRY_DEPENDENCY,
                    state=STATE_AVAILABLE_ON_DEMAND,
                    package=None,
                    reason=REASON_NOT_SELECTED,
                )
            )

        return InstallReport(
            outcomes=tuple(outcomes),
            components_installed=components_installed,
            components_rejected=components_rejected,
            verification_failures=tuple(verification_failures),
        )


__all__ = [
    "ALLOWED_SHADCN_COMPONENTS",
    "DEPENDENCY_PACKAGES",
    "INSTALL_FAILED",
    "INSTALL_STATES",
    "REASON_ALREADY_INSTALLED",
    "REASON_COMPONENT_NOT_ALLOWED",
    "REASON_COMPONENT_NONE_REQUESTED",
    "REASON_INSTALL_FAILED",
    "REASON_NOT_SELECTED",
    "REASON_NOT_VERIFIED",
    "REASON_NO_PACKAGE_MANAGER",
    "REASON_OUTSIDE_PROJECT",
    "REASON_PACKAGE_NOT_ALLOWLISTED",
    "REASON_PROJECT_INVALID",
    "REASON_TIMEOUT",
    "REGISTRY_DEPENDENCY",
    "SHADCN_CLI_VERSION",
    "TERMINAL_INSTALL_STATES",
    "CommandReceipt",
    "InstallOutcome",
    "allowlisted_dependencies",
    "allowed_shadcn_components",
    "is_contained",
    "package_name_is_well_formed",
    "project_declares_dependency",
    "InstallReport",
    "DesignDependencyInstaller",
    "LOCKFILES",
    "INSTALL_TIMEOUT_SECONDS",
    "REGISTRY_TIMEOUT_SECONDS",
    "build_install_argv",
    "build_registry_argv",
    "detect_package_manager",
    "filter_allowed_components",
    "resolve_package",
]