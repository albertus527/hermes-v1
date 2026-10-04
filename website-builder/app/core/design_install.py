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
the project's own ``package.json`` — or, for shadcn, observes every requested
component as a contained regular file under the destination the project's own
reviewed ``components.json`` declares. A selection flag, a successful-looking
command, or a returned exit code are each individually insufficient.

**Failure is explicit and never silently degrades requirements.** A failed
install produces a FAILED state carrying a static reason. There is no fallback
to a different package, no silent skip, and no "close enough" substitute, because
a silent substitution is a user requirement quietly changed by the application.

**shadcn is component-scoped and pinned.** Only the components D2 selected are
requested, never a default bundle, and the CLI is invoked by pinned version
through the project's own package manager, using that manager's real one-off
runner (``npm exec`` / ``pnpm dlx`` / ``yarn dlx``). An unselected shadcn
produces zero invocations, and a project with no approved shadcn configuration
produces zero invocations too — a destination is never guessed and
``shadcn init`` is never run.
"""

from __future__ import annotations

import json
import logging
import subprocess
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
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
#:
#: There is no official shadcn LTS channel, so the policy is latest STABLE plus
#: an exact pin: no prerelease, no canary, no range operator (``^``/``~``). The
#: pin is bumped only after a regression run plus a VPS smoke.
#:
#: 4.x resolves the component destination by reading ``paths`` from the ROOT
#: ``tsconfig.json`` and joining it against the alias. It does not follow
#: ``references`` into ``tsconfig.app.json``. The starter therefore declares the
#: same ``@/* -> ./src/*`` mapping in all three places that matter -- the root
#: tsconfig (what the CLI reads), ``tsconfig.app.json`` (what ``tsc -b``
#: typechecks against) and ``vite.config.ts`` (what the bundler resolves). See
#: ``templates/frontend-starter/tsconfig.json`` for the standing requirement.
SHADCN_CLI_VERSION = "4.21.0"

#: File suffixes a shadcn component may materialize as. shadcn emits TSX for a
#: TypeScript project (components.json ``tsx: true``); ``.ts`` is accepted so a
#: component that is genuinely a single non-JSX module still verifies. Both are
#: exact, application-owned extensions -- never a prefix scan, never "any file
#: whose name contains the component name".
COMPONENT_SUFFIXES: Tuple[str, ...] = (".tsx", ".ts")


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
REASON_COMPONENTS_NOT_VERIFIED = (
    "the registry command succeeded but the requested components are absent from "
    "the approved component directory; the state is not upgraded to installed"
)
REASON_SHADCN_CONFIG_INVALID = (
    "the project has no valid application-owned shadcn configuration, so the "
    "component destination is unknown; no command was attempted"
)
REASON_MANAGER_UNSUPPORTED = (
    "the project's package manager has no supported pinned one-off mechanism"
)
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
    #: Component NAMES observed under the approved component directory. Names
    #: only, drawn from the closed allowlist -- never an absolute path -- so the
    #: receipt stays bounded and leaks no filesystem layout.
    verified_components: Tuple[str, ...] = ()

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
            "verified_components": list(self.verified_components),
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
# The approved component destination -- APPLICATION-OWNED
# ---------------------------------------------------------------------------
#
# `shadcn add` does not choose where a component lands; it reads the project's
# `components.json` and writes under `aliases.ui`. So the destination is a
# property of the PROJECT'S CONFIG, and this module's job is to decide whether
# that config is one we are willing to act on.
#
# Two failure modes this deliberately refuses:
#
#   * Guessing. Hardcoding `src/components/ui` would verify a path the CLI was
#     never told to write, which can both false-pass (a file that happens to sit
#     there) and false-fail (a perfectly valid custom alias).
#   * `shadcn init`. Bootstrapping by running init would let the CLI mutate
#     package.json, the CSS entry and the config itself -- changes well beyond
#     the component the caller asked for. D3a only ever runs `add`.
#
# Absent or unapprovable config therefore FAILS CLOSED: no destination is
# invented and no command is attempted.

#: The config file shadcn reads. Named here so no code path spells it.
SHADCN_CONFIG_FILENAME = "components.json"

#: shadcn CLI styles accepted by this application. Anything else is a config we
#: have not reviewed, so it is not acted on.
_APPROVED_SHADCN_STYLES = frozenset({"new-york"})

#: The root an approved alias is resolved AGAINST. The starter maps `@/*` to
#: `./src/*`, so an alias is relative to the source root, not to the project
#: root: `@/components/ui` names `src/components/ui`. Binding here means a
#: config can only ever direct the CLI into the project source tree that
#: `tsc -b` actually typechecks.
#:
#: shadcn >=4 resolves the alias by reading ``paths`` from the ROOT
#: ``tsconfig.json``; it does not follow ``references``. So this assumption is
#: only sound while the starter declares the mapping in all three places that
#: read it: the root tsconfig (the CLI), ``tsconfig.app.json`` (``tsc -b``) and
#: ``vite.config.ts`` (the bundler). If the root mapping is ever dropped, shadcn
#: writes a literal ``@/components/ui`` directory at the project root instead --
#: which this module then correctly REFUSES to verify, because that path is not
#: under ``src``. The failure is loud, not silent.
_APPROVED_COMPONENT_ROOT = "src"

#: The approved alias must use the starter's `@/*` mapping. The starter declares
#: it in the root ``tsconfig.json`` (``paths``, which is what shadcn >=4 reads),
#: in ``tsconfig.app.json`` (``paths``) and in ``vite.config.ts``
#: (``resolve.alias``), so this is a check that the config agrees with the
#: toolchain, not a preference.
_APPROVED_ALIAS_PREFIX = "@/"

#: Longest alias string accepted. A config that names a 4 KB directory is not a
#: config this application is going to treat as reviewed.
_MAX_ALIAS_LEN = 200


def _read_shadcn_config(project_root: Path) -> Optional[Dict[str, Any]]:
    """The project's ``components.json``, or ``None`` if absent/unreadable."""
    path = Path(project_root) / SHADCN_CONFIG_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        document = json.loads(raw)
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


def _alias_is_approved(alias: object) -> bool:
    """Whether a shadcn alias names a reviewed directory inside the source root.

    The alias is validated as a SHAPE only. It must use the starter's `@/*`
    mapping and must be a plain relative path with no traversal, drive letter or
    leading separator. Resolving it against the source root (and requiring
    containment) is :func:`approved_component_dir`'s job, so a single place
    decides where the path actually lands.
    """
    if not isinstance(alias, str):
        return False
    if not alias or len(alias) > _MAX_ALIAS_LEN:
        return False
    if not alias.startswith(_APPROVED_ALIAS_PREFIX):
        # Rejects an absolute path ("/tmp/x"), a Windows drive path ("C:/x"), a
        # traversal ("@/../.."), and a bare relative path the toolchain has no
        # mapping for -- all without ever joining it to anything.
        return False
    relative = alias[len(_APPROVED_ALIAS_PREFIX):]
    if not relative:
        return False
    # PurePosixPath, deliberately: the alias is written in the shadcn config with
    # forward slashes on every platform, and parsing it with the native flavour
    # would treat "/" as a separator on POSIX but still yield host-dependent
    # parts on Windows. The alias is not a host path until we join it below.
    parts = PurePosixPath(relative).parts
    if not parts:
        return False
    # `..` anywhere is a traversal; "." segments are noise but harmless.
    if any(part == ".." for part in parts):
        return False
    if parts[0] in ("", "/"):
        return False
    return True


def approved_component_dir(project_root: Path) -> Optional[Path]:
    """The approved component directory declared by the project's shadcn config.

    Returns the resolved absolute directory, or ``None`` when the project has no
    config this application is willing to act on -- which callers MUST treat as
    "do not invoke the CLI", because without a destination there is nothing to
    verify against afterwards.

    The returned directory is validated twice: once as a *shape* (an alias we
    approve) and once as a *location* (resolves inside the project, with no
    symlinked segment escaping it). Only then is it usable.
    """
    document = _read_shadcn_config(project_root)
    if document is None:
        return None

    style = document.get("style")
    if style not in _APPROVED_SHADCN_STYLES:
        return None

    tailwind = document.get("tailwind")
    if not isinstance(tailwind, Mapping) or not isinstance(
        tailwind.get("css"), str
    ):
        # shadcn's own schema requires tailwind.{config,css,baseColor,
        # cssVariables}; a config missing the CSS entry is not one we reviewed.
        return None

    aliases = document.get("aliases")
    if not isinstance(aliases, Mapping):
        return None
    # `utils` is required by the shadcn schema and is the import every emitted
    # component depends on, so its absence means the config cannot produce a
    # compiling project.
    if not _alias_is_approved(aliases.get("ui")) or not _alias_is_approved(
        aliases.get("utils")
    ):
        return None

    ui_alias = str(aliases["ui"])
    relative = PurePosixPath(ui_alias[len(_APPROVED_ALIAS_PREFIX):])
    root = Path(project_root)
    # The alias is bound against the SOURCE root, because that is what the
    # starter's `@/*` mapping points at. Joining against the project root would
    # resolve `@/components/ui` to `<project>/components/ui`, a directory the
    # CLI would never write to and `tsc -b` would never check.
    candidate = root / _APPROVED_COMPONENT_ROOT / relative

    # Require containment in BOTH the project and the source root. `is_contained`
    # resolves, so a component root that is a symlink pointing outside the
    # workspace is rejected here rather than after files are written through it.
    if not is_contained(root, candidate) or not is_contained(
        root / _APPROVED_COMPONENT_ROOT, candidate
    ):
        logger.warning(
            "Refused a shadcn component directory that resolved outside the project."
        )
        return None
    return candidate


def verify_components_materialized(
    project_root: Path, components: Sequence[str], component_dir: Optional[Path]
) -> Tuple[str, ...]:
    """Which of ``components`` are materialized under the approved directory.

    Returns the components that are present, or ``()`` if ANY is missing: a
    partial install is not a success, because the caller will run
    ``npm run build`` immediately afterwards and a missing component fails it.

    ``component_dir`` is the ONLY directory consulted. It comes from
    :func:`approved_component_dir`, i.e. the project's own reviewed config, and
    is never derived from model text, resource text, or the command's stdout.

    Each candidate must satisfy ALL of:
      * a regular file (``is_file()`` -- a directory or dangling symlink fails),
      * named exactly ``<component><suffix>`` for an approved suffix,
      * contained in ``project_root`` after resolution (no symlink escape).
    """
    if component_dir is None or not components:
        return ()

    verified: List[str] = []
    for component in components:
        if not any(
            _component_is_materialized(project_root, component_dir, component, suffix)
            for suffix in COMPONENT_SUFFIXES
        ):
            # All-or-nothing: report nothing rather than the subset that
            # happened to land, so a partial install cannot be read as success.
            return ()
        verified.append(component)
    return tuple(sorted(verified))


def _component_is_materialized(
    project_root: Path, component_dir: Path, component: str, suffix: str
) -> bool:
    """Whether one component exists as a contained regular file."""
    if not component or component not in ALLOWED_SHADCN_COMPONENTS:
        # Defence in depth. Callers already pass an allowlisted tuple; this makes
        # it impossible for a rejected component to be verified even if a future
        # caller forgets to filter first.
        return False
    candidate = Path(component_dir) / f"{component}{suffix}"
    try:
        if not candidate.is_file():
            return False
    except OSError:
        return False
    return is_contained(project_root, candidate)



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

#: Yarn Berry's marker file. Berry is the only yarn that ships ``yarn dlx``;
#: Yarn Classic (which writes ``.yarnrc``) has no such subcommand. Presence of
#: this file is therefore the whole "is dlx supported by this toolchain" test,
#: decided from the project rather than by running ``yarn --version``.
YARN_BERRY_CONFIG = ".yarnrc.yml"


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


def registry_invocation_prefix(
    manager: Sequence[str], *, project_root: Path, version: str
) -> Optional[Tuple[str, ...]]:
    """The manager-specific one-off runner argv for the pinned registry CLI.

    Each package manager has a DIFFERENT one-off mechanism, and the old code
    assumed they all shared one by appending a bare ``dlx``. That produced
    ``npm dlx ...``, and ``npm`` has no ``dlx`` subcommand -- npm's one-off
    mechanism is ``npm exec`` (``npx`` is its alias).

    Returning ``None`` means "this toolchain has no mechanism I will drive", and
    the caller runs NO command at all. That is deliberate for Yarn Classic:
    rather than silently falling back to ``npx``/``npm``, which would run a CLI
    the project never opted into, an unsupported toolchain fails closed.
    """
    argv = tuple(manager)
    if not argv:
        return None
    name = argv[0]
    spec = f"shadcn@{version}"

    if name == "npm":
        # `npm exec --package=<spec> -- shadcn`. The trailing `--` is REQUIRED:
        # without it npm re-parses later switches as its own and would swallow
        # shadcn's `--yes` / `--overwrite`. `--yes` suppresses npm's own
        # install prompt so a supervised run cannot block on it.
        return argv + ("exec", "--yes", f"--package={spec}", "--", "shadcn")
    if name == "pnpm":
        # `pnpm dlx` is a real subcommand that takes the pinned spec positionally.
        return argv + ("dlx", spec)
    if name == "yarn":
        # Only Yarn Berry (v2+) has `dlx`. Classic does not, and falling back to
        # npx/npm would execute a CLI outside this project's toolchain contract.
        if (Path(project_root) / YARN_BERRY_CONFIG).exists():
            return argv + ("dlx", spec)
        logger.warning("Yarn Classic has no pinned one-off runner; refusing to invoke.")
        return None
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
    prefix: Sequence[str], *, components: Sequence[str]
) -> Tuple[str, ...]:
    """The shadcn ``add`` argv for an explicit, allowlisted component list.

    ``prefix`` is the manager-specific one-off runner from
    :func:`registry_invocation_prefix` (already carrying the pinned version), so
    the version is pinned in exactly one place and this function cannot
    reintroduce a floating ``latest``.

    ``--yes`` keeps the invocation non-interactive; without it the CLI can block
    on a prompt inside a supervised run. ``--overwrite`` makes a re-run
    deterministic instead of failing on an existing file.
    """
    ordered = sorted(set(components))
    return tuple(prefix) + ("add",) + tuple(ordered) + ("--yes", "--overwrite")


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

        Three independent ways to run nothing at all, all checked BEFORE any
        command is built: no components, a config this application will not act
        on, or a toolchain with no supported one-off runner.

        ``installed`` is assigned ONLY after every requested component is observed
        as a contained regular file under the destination the project's own
        reviewed ``components.json`` declares. A zero exit code, however
        confident, is not sufficient.
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

        # Fail closed BEFORE any command: without an approved destination there
        # is nothing to verify against afterwards, so invoking the CLI would buy
        # an uncheckable "success".
        component_dir = approved_component_dir(self.project_root)
        if component_dir is None:
            return (
                InstallOutcome(
                    dependency_id=REGISTRY_DEPENDENCY,
                    state=INSTALL_FAILED,
                    package=None,
                    reason=REASON_SHADCN_CONFIG_INVALID,
                ),
                allowed,
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

        prefix = registry_invocation_prefix(
            manager, project_root=self.project_root, version=SHADCN_CLI_VERSION
        )
        if prefix is None:
            return (
                InstallOutcome(
                    dependency_id=REGISTRY_DEPENDENCY,
                    state=INSTALL_FAILED,
                    package=None,
                    reason=REASON_MANAGER_UNSUPPORTED,
                ),
                allowed,
                rejected,
            )

        argv = build_registry_argv(prefix, components=allowed)
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

        # The command succeeded. That is NOT yet an installed claim.
        receipt = CommandReceipt.from_process(process, cwd_label="<project>")
        verified = verify_components_materialized(
            self.project_root, allowed, component_dir
        )
        if not verified:
            return (
                InstallOutcome(
                    dependency_id=REGISTRY_DEPENDENCY,
                    state=INSTALL_FAILED,
                    package=None,
                    reason=REASON_COMPONENTS_NOT_VERIFIED,
                    receipt=receipt,
                    verified_components=(),
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
                receipt=receipt,
                verified_components=verified,
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
    "COMPONENT_SUFFIXES",
    "DEPENDENCY_PACKAGES",
    "INSTALL_FAILED",
    "INSTALL_STATES",
    "REASON_ALREADY_INSTALLED",
    "REASON_COMPONENT_NOT_ALLOWED",
    "REASON_COMPONENT_NONE_REQUESTED",
    "REASON_COMPONENTS_NOT_VERIFIED",
    "REASON_INSTALL_FAILED",
    "REASON_MANAGER_UNSUPPORTED",
    "REASON_NOT_SELECTED",
    "REASON_NOT_VERIFIED",
    "REASON_NO_PACKAGE_MANAGER",
    "REASON_OUTSIDE_PROJECT",
    "REASON_PACKAGE_NOT_ALLOWLISTED",
    "REASON_PROJECT_INVALID",
    "REASON_SHADCN_CONFIG_INVALID",
    "REASON_TIMEOUT",
    "REGISTRY_DEPENDENCY",
    "SHADCN_CLI_VERSION",
    "SHADCN_CONFIG_FILENAME",
    "TERMINAL_INSTALL_STATES",
    "CommandReceipt",
    "InstallOutcome",
    "allowlisted_dependencies",
    "allowed_shadcn_components",
    "approved_component_dir",
    "is_contained",
    "package_name_is_well_formed",
    "project_declares_dependency",
    "InstallReport",
    "DesignDependencyInstaller",
    "LOCKFILES",
    "YARN_BERRY_CONFIG",
    "INSTALL_TIMEOUT_SECONDS",
    "REGISTRY_TIMEOUT_SECONDS",
    "build_install_argv",
    "build_registry_argv",
    "detect_package_manager",
    "filter_allowed_components",
    "registry_invocation_prefix",
    "resolve_package",
    "verify_components_materialized",
]