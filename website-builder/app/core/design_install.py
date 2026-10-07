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
#:
#: ``gsap_react`` is the ``@gsap/react`` companion that the reviewed React Bits
#: ``SplitText`` component imports (``useGSAP``). It is a FIRST-CLASS
#: application-owned dependency, not a transitive detail: SplitText cannot
#: compile without it, so a registry install that materialized the component
#: while leaving ``@gsap/react`` un-pinned would ship a broken build. It is
#: deliberately NOT derived from a ``@gsap/*`` naming rule -- it is a reviewed
#: row, exactly like ``@types/three``.
DEPENDENCY_PACKAGES: Dict[str, str] = {
    "gsap": "gsap",
    "gsap_react": "@gsap/react",
    "three": "three",
    "lenis": "lenis",
}

#: shadcn is not an npm dependency; it is a component registry invoked by its
#: own CLI. Named separately so no code path can treat it as a package.
REGISTRY_DEPENDENCY = "shadcn"


# ---------------------------------------------------------------------------
# Exact pins -- reproducibility
# ---------------------------------------------------------------------------
#
# A dependency id resolves to a NAME (above) and a VERSION (here), and both are
# application-owned constants. Without the version the "verified installed"
# claim describes something that will not be installed again tomorrow: an
# unpinned `three` added today can float to a different release on the next
# build, and the pin in the manifest would then describe a version nobody has.
#
# Versions are EXACT. No range operator, no ``latest``, no prerelease alias.
# The regexp below is not an input filter -- it is applied to the three constant
# strings above -- so it is a static safety net proving the allowlist has not
# been edited into something that could resolve differently tomorrow.

#: A pinned version is digits and dots only. Deliberately rejects ``^``, ``~``,
#: ``>=``, ``>``, ``||``, ``*``, prerelease suffixes, and whitespace.
_EXACT_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

#: The npm dependency sections this application writes and verifies against.
DEPENDENCY_SECTIONS: Tuple[str, ...] = ("dependencies", "devDependencies")

SECTION_DEPENDENCIES = "dependencies"
SECTION_DEV_DEPENDENCIES = "devDependencies"


@dataclass(frozen=True)
class PackageSpec:
    """One exact, application-owned package pin.

    ``dependency_section`` is part of the spec because it is part of the
    postcondition: a type-only companion belongs in ``devDependencies``, and
    verification that accepts the package in *either* section would let a
    project satisfy a runtime requirement with a dev-only declaration.
    """

    package: str
    version: str
    dependency_section: str = SECTION_DEPENDENCIES

    @property
    def spec(self) -> str:
        """The ``name@version`` string handed to the package manager."""
        return f"{self.package}@{self.version}"

    def is_exact(self) -> bool:
        return bool(_EXACT_VERSION_RE.match(self.version))


def _runtime_spec(dependency_id: str) -> PackageSpec:
    """The runtime :class:`PackageSpec` for an allowlisted dependency id."""
    return PackageSpec(
        package=DEPENDENCY_PACKAGES[dependency_id],
        version=DEPENDENCY_PACKAGE_PINS[dependency_id],
        dependency_section=SECTION_DEPENDENCIES,
    )


#: Exact versions, keyed by the same dependency ids as
#: :data:`DEPENDENCY_PACKAGES`. The two mappings are deliberately separate:
#: ``DEPENDENCY_PACKAGES`` answers "which package", these answer "which
#: version", and a caller that wants an installable spec must consult
#: :func:`required_package_specs` rather than pairing the maps by hand.
#:
#: ``gsap_react`` is pinned to ``2.1.2`` -- the current stable release, verified
#: live against the npm registry (``npm view @gsap/react version``). Its
#: ``peerDependencies`` are ``gsap: ^3.12.5`` and ``react: >=17``; the app-owned
#: ``gsap`` pin (3.15.0) satisfies the first, and the starter ships React 19.2.7,
#: which satisfies the second. See :data:`REVIEWED_REGISTRY_COMPONENTS`.
DEPENDENCY_PACKAGE_PINS: Dict[str, str] = {
    "gsap": "3.15.0",
    "gsap_react": "2.1.2",
    "three": "0.186.1",
    "lenis": "1.3.26",
}


@dataclass(frozen=True)
class CompanionPackage:
    """A package a dependency requires IN ADDITION to its runtime package.

    Exists because ``three`` ships no bundled TypeScript declarations: the
    runtime package alone installs cleanly and then fails the build with
    ``TS7016``. The companion is the difference between "installed" and
    "installed and typechecks", and treating it as optional would let the
    postcondition report a capability the project does not have.
    """

    package: str
    version: str
    dependency_section: str

    def to_spec(self) -> PackageSpec:
        return PackageSpec(
            package=self.package,
            version=self.version,
            dependency_section=self.dependency_section,
        )


#: dependency id -> the companion packages it needs, and ONLY those.
#:
#: **This mapping is the allowlist.** There is deliberately NO general
#: ``@types/<runtime-package>`` derivation: inferring a companion from a package
#: name is precisely the "arbitrary installer" this module forbids, because it
#: would let any future runtime package acquire an arbitrary dev dependency
#: without a human adding the row here. A caller-, model-, or resource-supplied
#: companion name is never forwarded to npm; :func:`required_package_specs`
#: resolves only through this map.
DEPENDENCY_COMPANION_PACKAGES: Dict[str, Tuple[CompanionPackage, ...]] = {
    "three": (
        CompanionPackage(
            package="@types/three",
            version="0.186.0",
            dependency_section=SECTION_DEV_DEPENDENCIES,
        ),
    ),
}


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


def resolve_companion_packages(dependency_id: str) -> Tuple[CompanionPackage, ...]:
    """The closed companion packages for ``dependency_id``.

    Mirrors :func:`resolve_package`'s contract exactly: an unrecognised id
    yields an EMPTY tuple rather than a synthesised companion, and never echoes
    its input. ``gsap`` and ``lenis`` ship their own types and therefore have no
    entry here -- which is the point of the mapping being closed rather than
    derived.
    """
    if not isinstance(dependency_id, str):
        return ()
    return DEPENDENCY_COMPANION_PACKAGES.get(dependency_id, ())


# ---------------------------------------------------------------------------
# Packages the pinned shadcn CLI introduces -- APPLICATION-OWNED, exact-pinned
# ---------------------------------------------------------------------------
#
# The pinned shadcn CLI is a black box that WRITES npm packages directly into a
# project's package.json. Live probe of shadcn@4.21.0: adding the builtin
# `button` writes ``cn@^0.4.0`` and ``radix-ui@^1.7.0``; adding the external
# React Bits `SplitText` writes ``gsap@^3.15.0`` and ``@gsap/react@^2.1.2``.
# Those are DIRECT project dependencies introduced by a design install, so they
# are inside the boundary this batch guards.
#
# They are deliberately NOT added to :data:`DEPENDENCY_PACKAGES`: that table maps
# D2-selectable resource ids, and these are toolchain-introduced packages with no
# manifest resource. Keeping them separate stops the D2 selection surface from
# widening just because the registry needs a helper package.
#
# Every entry is an EXACT application-owned pin, verified live. The registry's own
# range (``^0.4.0``) is only a starting point: it is normalized to the pin below
# after the CLI runs.

#: package -> exact application-owned pin, for packages the registry introduces.
REGISTRY_INTRODUCED_PACKAGE_PINS: Dict[str, str] = {
    "cn": "0.4.0",
    "radix-ui": "1.7.0",
    # The emitted builtin SOURCES import ``lucide-react`` (accordion, checkbox,
    # dialog, select, sheet) but the pinned CLI neither declares nor installs it
    # (verified live against shadcn@4.21.0: ``add dialog`` writes cn+radix-ui and
    # creates only ``dialog.tsx``). The starter's own ``components.json`` already
    # declares ``"iconLibrary": "lucide"``, so reviewing and exact-pinning the
    # icon package is what the application intended -- not a new dependency.
    "lucide-react": "1.52.0",
}


def reviewed_registry_package_pins() -> Dict[str, str]:
    """Every package a registry install may introduce, at its exact pin.

    The union of the toolchain-introduced helpers (``cn``, ``radix-ui``) and the
    allowlisted dependencies an external component may declare (``gsap``,
    ``@gsap/react``, ...). Used to normalize whatever the CLI wrote back to an
    exact application-owned version. A package absent from this map has no
    reviewed pin and cannot be normalized -- the boundary refuses it first.
    """
    pins: Dict[str, str] = dict(REGISTRY_INTRODUCED_PACKAGE_PINS)
    for dependency_id, package in DEPENDENCY_PACKAGES.items():
        pin = DEPENDENCY_PACKAGE_PINS.get(dependency_id)
        if pin is not None:
            pins[package] = pin
    return pins

#: For each reviewed shadcn BUILTIN component, the DIRECT packages the pinned CLI
#: adds to ``dependencies``. Verified live against shadcn@4.21.0 by adding each
#: component to a clean project and diffing package.json. A component absent from
#: this table has no reviewed dependency contract and must not be installed.
REVIEWED_BUILTIN_COMPONENT_DEPENDENCIES: Dict[str, Tuple[str, ...]] = {
    "accordion": ("cn", "radix-ui"),
    "alert": ("cn",),
    "badge": ("cn", "radix-ui"),
    "button": ("cn", "radix-ui"),
    "card": ("cn",),
    "checkbox": ("cn", "radix-ui"),
    "dialog": ("cn", "radix-ui"),
    "input": ("cn",),
    "label": ("cn", "radix-ui"),
    "select": ("cn", "radix-ui"),
    "separator": ("cn", "radix-ui"),
    "sheet": ("cn", "radix-ui"),
    "switch": ("cn", "radix-ui"),
    "tabs": ("cn", "radix-ui"),
    "textarea": ("cn",),
    "tooltip": ("cn", "radix-ui"),
}

#: For each reviewed builtin, the BARE module specifiers its EMITTED SOURCE
#: imports that the pinned CLI does NOT install. Verified live against
#: shadcn@4.21.0: ``add dialog`` emits ``import { XIcon } from "lucide-react"``
#: and ``import { Button } from "@/components/ui/button"`` while the registry item
#: declares only ``cn``/``radix-ui`` and the CLI installs only those. The starter's
#: ``components.json`` already declares ``"iconLibrary": "lucide"``, so the icon
#: package is reviewed and exact-pinned (``REGISTRY_INTRODUCED_PACKAGE_PINS``).
#: A builtin whose emitted source imports a package outside this reviewed set is
#: NOT installable: the postcondition would otherwise report ``installed`` for a
#: component whose build cannot resolve. This is the SAME class as the external
#: component dependency boundary, applied to the builtin source import closure.
REVIEWED_BUILTIN_COMPONENT_IMPORTS: Dict[str, Tuple[str, ...]] = {
    "accordion": ("lucide-react",),
    "alert": (),
    "badge": (),
    "button": (),
    "card": (),
    "checkbox": ("lucide-react",),
    "dialog": ("lucide-react",),
    "input": (),
    "label": (),
    "select": ("lucide-react",),
    "separator": (),
    "sheet": ("lucide-react",),
    "switch": (),
    "tabs": (),
    "textarea": (),
    "tooltip": (),
}

#: Builtins whose emitted SOURCE imports a sibling builtin under
#: ``@/components/ui/<name>`` that the registry item does NOT declare in
#: ``registryDependencies``. Verified live: ``dialog.tsx`` imports
#: ``@/components/ui/button`` but the item declares no nested registry dependency,
#: so ``shadcn add dialog`` creates ONLY ``dialog.tsx`` and the emitted file cannot
#: resolve. Requesting such a component must materialize its reviewed closure too.
REVIEWED_BUILTIN_COMPONENT_NESTED: Dict[str, Tuple[str, ...]] = {
    "dialog": ("button",),
}


def expand_reviewed_component_closure(components: Sequence[str]) -> Tuple[str, ...]:
    """The requested builtins plus every REVIEWED nested builtin they require.

    The closure is taken from the application-owned ``REVIEWED_BUILTIN_COMPONENT_NESTED``
    table only -- never from upstream metadata -- so it cannot be widened by a
    registry that starts declaring new nested components. A component whose
    emitted source imports an unreviewed sibling is refused at the table
    (``REVIEWED_BUILTIN_COMPONENT_IMPORTS``); this function only adds the siblings
    the application has already reviewed.
    """
    resolved: set = set()
    queue = [c for c in (components or ()) if isinstance(c, str)]
    while queue:
        component = queue.pop()
        if component in resolved:
            continue
        resolved.add(component)
        queue.extend(REVIEWED_BUILTIN_COMPONENT_NESTED.get(component, ()))
    return tuple(sorted(resolved))


def expected_registry_packages(components: Sequence[str]) -> Tuple[str, ...]:
    """Every direct package the reviewed builtins may introduce OR import.

    The union of the CLI-written helpers (``cn``, ``radix-ui``) and the packages
    the emitted SOURCE imports (``lucide-react``). This is the set the
    post-install delta guard accepts; anything else fails closed. Returns ``()``
    for an empty list. An unknown component contributes nothing here; callers gate
    on the component allowlist separately, so this only ever describes the
    reviewed set.
    """
    packages: set = set()
    for component in components or ():
        packages.update(REVIEWED_BUILTIN_COMPONENT_DEPENDENCIES.get(component, ()))
        packages.update(REVIEWED_BUILTIN_COMPONENT_IMPORTS.get(component, ()))
    return tuple(sorted(packages))


def required_registry_packages(components: Sequence[str]) -> Tuple[str, ...]:
    """Packages the builtins' emitted SOURCE requires that the CLI does NOT install.

    These must be present at their exact application-owned pin after the install,
    or the emitted component cannot build. Distinct from
    :func:`expected_registry_packages`, which also lists packages the CLI writes
    itself (those are normalized, not required-to-be-installed).
    """
    packages: set = set()
    for component in components or ():
        packages.update(REVIEWED_BUILTIN_COMPONENT_IMPORTS.get(component, ()))
    return tuple(sorted(packages))


# ---------------------------------------------------------------------------
# Emitted-source import boundary -- the INSTALLATION is untrusted
# ---------------------------------------------------------------------------
#
# A component IDENTITY is reviewed (``ALLOWED_SHADCN_COMPONENTS`` / a reviewed
# external contract). Its INSTALLATION is not: the registry decides which file
# it materializes, and that file may import a package the registry never
# declared, the CLI never installed, and this application never reviewed. The
# package.json delta guard cannot see this, because nothing was added to the
# manifest -- the import is a source-level requirement. So the emitted source
# itself is parsed and every BARE package specifier it names must be one this
# application already provides or has reviewed. Anything else fails closed.

#: Bare specifiers every emitted component may import without review: the React
#: runtime the starter always ships.
_ALWAYS_PROVIDED_PACKAGES: Tuple[str, ...] = ("react", "react-dom")

#: A bounded read of an emitted component source. A generated component is a few
#: kilobytes; this ceiling stops a pathological file from being slurped whole.
_MAX_COMPONENT_SOURCE_CHARS = 400_000

_IMPORT_FROM_RE = re.compile(r"""\bfrom\s*["']([^"']+)["']""")
_SIDE_EFFECT_IMPORT_RE = re.compile(r"""\bimport\s*["']([^"']+)["']""")


def bare_package_of(specifier: object) -> Optional[str]:
    """The npm package a module specifier names, or ``None`` for a non-package.

    ``None`` for a relative path (``./x``, ``../x``), an absolute path, or the
    project's own alias (``@/...``). For a package, strips any subpath
    (``gsap/ScrollTrigger`` -> ``gsap``) and preserves a scope
    (``@gsap/react``). Bounded and total: a non-string or empty specifier is
    ``None`` rather than an error.
    """
    if not isinstance(specifier, str) or not specifier:
        return None
    if specifier.startswith(("./", "../", "/", "@/")) or specifier in (".", ".."):
        return None
    parts = specifier.split("/")
    if specifier.startswith("@"):
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return None
        return f"{parts[0]}/{parts[1]}"
    return parts[0] or None


def declared_imports(source: str) -> Tuple[str, ...]:
    """Every BARE package specifier ``source`` imports, sorted and deduped.

    Matches ``import ... from "x"``, ``export ... from "x"`` and the side-effect
    form ``import "x"``. Relative paths and the ``@/`` project alias are not
    packages and are ignored.
    """
    if not isinstance(source, str) or not source:
        return ()
    found: set = set()
    for match in _IMPORT_FROM_RE.finditer(source):
        package = bare_package_of(match.group(1))
        if package:
            found.add(package)
    for match in _SIDE_EFFECT_IMPORT_RE.finditer(source):
        package = bare_package_of(match.group(1))
        if package:
            found.add(package)
    return tuple(sorted(found))


def unreviewed_imports(
    source: str, *, allowed_packages: Sequence[str]
) -> Tuple[str, ...]:
    """Bare packages ``source`` imports that are NOT in ``allowed_packages``."""
    allowed = set(allowed_packages)
    return tuple(p for p in declared_imports(source) if p not in allowed)


def _reviewed_source_import_packages(*extra: str) -> Tuple[str, ...]:
    """Every package an emitted component source may import without review.

    The reviewed builtin helpers (``cn``, ``radix-ui``), the reviewed source
    imports (``lucide-react``), the packages the starter always ships
    (``react``, ``react-dom``), and any ``extra`` packages the caller has already
    reviewed for this install (e.g. an external component's contract packages).
    A package outside this set is refused.
    """
    packages: set = set(_ALWAYS_PROVIDED_PACKAGES)
    packages.update(extra)
    packages.update(REGISTRY_INTRODUCED_PACKAGE_PINS)
    for component_packages in REVIEWED_BUILTIN_COMPONENT_DEPENDENCIES.values():
        packages.update(component_packages)
    for component_packages in REVIEWED_BUILTIN_COMPONENT_IMPORTS.values():
        packages.update(component_packages)
    return tuple(sorted(packages))


def component_source_text(
    project_root: Path, component_dir: Path, component: str
) -> Optional[str]:
    """The emitted source of ``component`` under ``component_dir``, or ``None``.

    Reads the first contained regular ``<component><suffix>`` file, bounded to
    :data:`_MAX_COMPONENT_SOURCE_CHARS`. Returns ``None`` when nothing is
    readable -- materialization is verified separately, so an absent file is not
    a failure here.
    """
    for suffix in COMPONENT_SUFFIXES:
        candidate = Path(component_dir) / f"{component}{suffix}"
        try:
            if not candidate.is_file() or not is_contained(project_root, candidate):
                continue
            with candidate.open("r", encoding="utf-8", errors="replace") as handle:
                return handle.read(_MAX_COMPONENT_SOURCE_CHARS)
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# Impeccable critic parser runtime -- APPLICATION-OWNED, exact-pinned
# ---------------------------------------------------------------------------
#
# The Impeccable skill-v4.1.0 ``detect.mjs`` runs the FULL static HTML/CSS engine
# only when four npm parser modules are importable; without them it prints
# ``DEGRADED - HTML parser modules unavailable`` and falls back to regex, which
# is an UNDERCOUNT, not a clean bill of health.
#
# The universal artifact ships NO ``package.json`` (verified: 2698 zip entries,
# zero ``package.json``/lockfile), so the parser modules are not bundled. They
# must be provisioned explicitly. Versions below are the upstream ``skill-v4.1.0``
# ``package.json`` ``dependencies`` (verified live), pinned EXACT:
#
#     css-select ^7.0.0  -> 7.0.0
#     css-tree   ^3.2.1  -> 3.2.1
#     domutils   ^4.0.2  -> 4.0.2
#     htmlparser2 ^12.0.0 -> 12.0.0
#
# These are provisioned ONLY during explicit Hermes design-profile setup, NEVER
# during an arbitrary website build. They are deliberately NOT in
# ``DEPENDENCY_PACKAGES`` (that table is the D2 project-on-demand surface), so
# no project build can pull them in.

#: The four parser modules the full static-HTML engine imports, exactly pinned.
IMPECCABLE_PARSER_PACKAGE_PINS: Dict[str, str] = {
    "css-select": "7.0.0",
    "css-tree": "3.2.1",
    "domutils": "4.0.2",
    "htmlparser2": "12.0.0",
}


def impeccable_parser_package_pins() -> Tuple[Tuple[str, str], ...]:
    """The exact ``(package, version)`` pins for the Impeccable parser runtime."""
    return tuple(sorted(IMPECCABLE_PARSER_PACKAGE_PINS.items()))


def required_package_specs(dependency_id: str) -> Tuple[PackageSpec, ...]:
    """Every package spec a dependency needs: its runtime pin plus companions.

    This is what verification counts against and what an install may write. A
    dependency is satisfied only when EVERY spec here is present at its exact
    version in its exact section, so a runtime-only ``three`` is not installed.

    An unrecognised id returns ``()`` -- the same "no package" answer
    :func:`resolve_package` gives, for the same reason.
    """
    package = resolve_package(dependency_id)
    if package is None:
        return ()
    return (PackageSpec(package, DEPENDENCY_PACKAGE_PINS[dependency_id]),) + tuple(
        companion.to_spec()
        for companion in resolve_companion_packages(dependency_id)
    )


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
REASON_PIN_NOT_EXACT = (
    "the application-owned pin for this dependency is not an exact version; "
    "no command was attempted"
)
REASON_COMPANION_NOT_VERIFIED = (
    "the install commands succeeded but a required companion package is absent "
    "from the project manifest, or is not at the exact pinned version in the "
    "exact dependency section; the state is not upgraded to installed"
)
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
REASON_REGISTRY_DEPENDENCY_DRIFT = (
    "the registry install introduced a direct package dependency outside the "
    "application-owned reviewed set; the state is not upgraded to installed"
)
REASON_REGISTRY_PACKAGE_NOT_EXACT = (
    "a registry-introduced package could not be normalized to its exact "
    "application-owned pin; the state is not upgraded to installed"
)
REASON_BUILTIN_COMPONENT_UNREVIEWED = (
    "the requested builtin component has no reviewed dependency contract"
)
REASON_REGISTRY_IMPORT_UNRESOLVED = (
    "a component's emitted source imports a package that could not be verified "
    "present at its exact application-owned pin; the state is not upgraded to "
    "installed"
)
REASON_REGISTRY_SOURCE_IMPORT_UNREVIEWED = (
    "a component's emitted source imports a package outside the application-owned "
    "reviewed set; the state is not upgraded to installed"
)
REASON_MANAGER_UNSUPPORTED = (
    "the project's package manager has no supported pinned one-off mechanism"
)


def _incomplete_reason(dependency_id: str, project_root: Path) -> str:
    """Static reason describing WHY verification did not pass.

    Distinguishes "the runtime package never landed" from "the runtime landed
    but its companion did not", because they are different operational problems
    with the same terminal state. Both are static strings: the branch is chosen
    by inspecting the manifest, and no value read from it is echoed into the
    reason, so a receipt stays bounded and leaks no dependency layout.
    """
    package = resolve_package(dependency_id)
    if package is None:
        return REASON_NOT_VERIFIED
    runtime_spec = required_package_specs(dependency_id)[0]
    if not project_satisfies_spec(project_root, runtime_spec):
        return REASON_NOT_VERIFIED
    # The runtime package is present and correct, so the gap is a companion.
    return REASON_COMPANION_NOT_VERIFIED
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


@dataclass(frozen=True)
class _RegistryBoundaryResult:
    """Internal outcome of the registry dependency-boundary check.

    Private: this is not part of the module's public contract. It carries only a
    static ``reason`` and an optional receipt -- never the offending package
    names, which stay internal so no untrusted string can reach a public reason.
    """

    ok: bool
    reason: str = ""
    receipt: Optional[CommandReceipt] = None


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

    Retained as the loose membership question ("is this name mentioned
    anywhere"), used where the section genuinely does not matter.
    :func:`project_satisfies_spec` is what decides an INSTALL postcondition.
    """
    document = _read_project_manifest(project_root)
    if document is None or not package:
        return False
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        block = document.get(section)
        if isinstance(block, Mapping) and package in block:
            return True
    return False


def project_satisfies_spec(project_root: Path, spec: PackageSpec) -> bool:
    """Whether the project manifest satisfies ``spec`` EXACTLY.

    Conjunctive on three axes, all three load-bearing:

    * **presence** in the section the spec declares,
    * **exact version** match, and
    * **exact section**.

    The section check is why a type-only companion in ``devDependencies`` is not
    interchangeable with a runtime dependency, and why accepting the package in
    ``optionalDependencies`` (which ``npm install --save-optional`` would write)
    would not satisfy a runtime requirement.
    """
    document = _read_project_manifest(project_root)
    if document is None or not spec.package:
        return False

    block = document.get(spec.dependency_section)
    if not isinstance(block, Mapping):
        return False

    declared = block.get(spec.package)
    if not isinstance(declared, str):
        return False
    return declared.strip() == spec.version


def project_satisfies_dependency(project_root: Path, dependency_id: str) -> bool:
    """Whether EVERY required spec for ``dependency_id`` is satisfied.

    This is the ONLY definition of "installed" for an npm dependency. A
    dependency with a companion therefore cannot be reported installed from the
    runtime package alone, which is exactly the condition that produced
    ``TS7016`` in the field: ``three`` installed, no declarations, build broken.
    """
    specs = required_package_specs(dependency_id)
    if not specs:
        return False
    return all(project_satisfies_spec(project_root, spec) for spec in specs)


# ---------------------------------------------------------------------------
# Direct-dependency snapshot / delta -- the registry boundary
# ---------------------------------------------------------------------------
#
# The pinned shadcn CLI writes packages into package.json itself, so "the CLI
# exited 0" says nothing about WHICH packages it introduced. These helpers make
# the DIRECT dependency delta observable and comparable against an
# application-owned reviewed set. They deliberately do NOT model the full
# transitive tree (npm's territory): only the direct project-level declarations a
# design install causes.

#: Every dependency section a snapshot covers. Wider than the two this module
#: writes, because the guard must notice a package smuggled into
#: ``optionalDependencies`` or ``peerDependencies`` by a registry.
SNAPSHOT_SECTIONS: Tuple[str, ...] = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
)


def snapshot_direct_dependencies(project_root: Path) -> Dict[str, Dict[str, str]]:
    """A section -> {package: declared} snapshot of the project's direct deps.

    A missing or unreadable manifest yields an empty snapshot, so a delta
    computed against it is the whole manifest -- the conservative direction.
    """
    document = _read_project_manifest(project_root)
    snapshot: Dict[str, Dict[str, str]] = {section: {} for section in SNAPSHOT_SECTIONS}
    if document is None:
        return snapshot
    for section in SNAPSHOT_SECTIONS:
        block = document.get(section)
        if isinstance(block, Mapping):
            snapshot[section] = {
                str(name): str(version)
                for name, version in block.items()
                if isinstance(name, str) and isinstance(version, str)
            }
    return snapshot


def dependency_delta(
    before: Mapping[str, Mapping[str, str]],
    after: Mapping[str, Mapping[str, str]],
) -> Dict[str, Dict[str, str]]:
    """The packages present in ``after`` that are new or changed vs ``before``.

    Returns ``section -> {package: new_version}``. A package that moved sections
    (added to a section it was not in before) counts as a delta in the new
    section, which is how a runtime package smuggled into ``devDependencies``
    becomes visible.
    """
    delta: Dict[str, Dict[str, str]] = {}
    for section in SNAPSHOT_SECTIONS:
        before_block = before.get(section, {})
        after_block = after.get(section, {})
        changed = {
            name: version
            for name, version in after_block.items()
            if before_block.get(name) != version
        }
        if changed:
            delta[section] = changed
    return delta


def removed_direct_dependencies(
    before: Mapping[str, Mapping[str, str]],
    after: Mapping[str, Mapping[str, str]],
) -> Dict[str, Tuple[str, ...]]:
    """Direct dependencies present in ``before`` and ABSENT from ``after``.

    ``dependency_delta`` only reports what ``after`` CONTAINS, so a package the
    install silently REMOVED -- or moved out of a section entirely -- is
    invisible to it. A registry install may only ADD its reviewed packages; it
    must never delete a pre-existing project dependency. Returns
    ``section -> (package, ...)`` for every removal.
    """
    removed: Dict[str, Tuple[str, ...]] = {}
    for section in SNAPSHOT_SECTIONS:
        before_block = before.get(section, {})
        after_block = after.get(section, {})
        gone = tuple(sorted(name for name in before_block if name not in after_block))
        if gone:
            removed[section] = gone
    return removed


def registry_dependency_delta_is_acceptable(
    before: Mapping[str, Mapping[str, str]],
    after: Mapping[str, Mapping[str, str]],
    *,
    allowed_packages: Sequence[str],
) -> Tuple[bool, Tuple[str, ...]]:
    """Whether a registry install's direct-dependency delta is inside policy.

    Returns ``(ok, offending_packages)``. ``offending_packages`` names only
    packages OUTSIDE the reviewed set -- never a raw upstream string, and never
    more than the offending names -- so a caller can log a bounded, actionable
    reason without echoing untrusted metadata. A package is acceptable only when
    it appears in ``allowed_packages`` AND only in the runtime ``dependencies``
    section (a registry introducing a package into a dev/optional/peer section is
    not the reviewed shape).

    A package REMOVED from a pre-existing section is also offending: a registry
    install may add its reviewed packages, never delete a project dependency.
    """
    delta = dependency_delta(before, after)
    allowed = set(allowed_packages)
    offending: set = set()
    for section, packages in delta.items():
        for name in packages:
            if name not in allowed:
                offending.add(name)
            elif section != SECTION_DEPENDENCIES:
                # Right package, wrong section: a registry must not move a
                # runtime helper into devDependencies/optionalDependencies.
                offending.add(name)
    # A removal is invisible to ``dependency_delta``; catch it here so a silent
    # deletion of a project dependency fails the install.
    for packages in removed_direct_dependencies(before, after).values():
        offending.update(packages)
    return (not offending), tuple(sorted(offending))


#: The package-manager argv that normalizes a floating range to an exact pin.
#: The registry writes ``cn@^0.4.0``; this rewrites it to ``cn@0.4.0`` with the
#: manager's own ``--save-exact`` so the final manifest matches the
#: application-owned pin. Reuses the SAME manager-specific shape as
#: :func:`_runtime_install_argv`, so there is one place that knows how to pin.
def build_registry_normalization_argv(
    manager: Sequence[str], packages: Sequence[str]
) -> Tuple[Tuple[str, ...], ...]:
    """One exact-pin install command per registry-introduced ``package``.

    Returns a tuple of argv lists, each installing one package at its
    application-owned pin with ``--save-exact``. A package with no reviewed pin
    yields NO command for it (the caller refuses on the pin table separately), so
    an unknown package cannot be normalized into existence.
    """
    commands: list = []
    for package in packages:
        pin = reviewed_registry_package_pins().get(package)
        if pin is None:
            continue
        commands.append(
            _runtime_install_argv(
                tuple(manager),
                PackageSpec(package=package, version=pin),
            )
        )
    return tuple(commands)


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
    return _approved_alias_dir(project_root, "ui")


def approved_external_component_dir(project_root: Path) -> Optional[Path]:
    """The approved destination for an EXTERNAL registry component.

    shadcn writes a remote ``registry-item`` under ``aliases.components`` (not
    ``aliases.ui``). Verified live: adding
    ``https://reactbits.dev/r/SplitText-TS-TW`` to the starter writes
    ``src/components/SplitText.tsx``. So an external component's destination is a
    DIFFERENT reviewed alias from the builtin one, and verifying it against
    ``aliases.ui`` would fail closed on a correct install (or, worse, pass on the
    wrong directory).

    Same fail-closed contract as :func:`approved_component_dir`: an unapproved
    config yields ``None`` and the caller runs nothing.
    """
    return _approved_alias_dir(project_root, "components")


def _approved_alias_dir(project_root: Path, alias_key: str) -> Optional[Path]:
    """Resolve a reviewed ``aliases.<alias_key>`` directory inside the project.

    Shared by the builtin (``ui``) and external (``components``) destinations so
    the containment rule exists in exactly one place. ``utils`` is still required
    present because the shadcn schema requires it and every emitted component
    imports it.
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
    if not _alias_is_approved(aliases.get(alias_key)) or not _alias_is_approved(
        aliases.get("utils")
    ):
        return None

    alias = str(aliases[alias_key])
    relative = PurePosixPath(alias[len(_APPROVED_ALIAS_PREFIX):])
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


def verify_external_component_materialized(
    project_root: Path, component_id: str, component_dir: Optional[Path]
) -> Tuple[str, ...]:
    """Whether an external component materialized under the approved directory.

    Same all-or-nothing contract as :func:`verify_components_materialized`, but
    the component identity is a REVIEWED external component (PascalCase, e.g.
    ``SplitText``) rather than a shadcn builtin, so the ``ALLOWED_SHADCN_COMPONENTS``
    membership check does not apply. The identity is validated as a well-formed
    component id, and the file must be ``<component_id><suffix>`` -- a contained
    regular file inside the project. Returns ``(component_id,)`` or ``()``.
    """
    if component_dir is None or not component_id:
        return ()
    # Reuse the registry module's identity rule so a URL fragment can never be
    # verified as a materialized file. Imported lazily to avoid an import cycle
    # (design_registry imports design_install).
    from app.core.design_registry import component_id_is_well_formed

    if not component_id_is_well_formed(component_id):
        return ()
    for suffix in COMPONENT_SUFFIXES:
        candidate = Path(component_dir) / f"{component_id}{suffix}"
        try:
            if candidate.is_file() and is_contained(project_root, candidate):
                return (component_id,)
        except OSError:
            continue
    return ()



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


# ---------------------------------------------------------------------------
# Pinned CLIs -- application-owned, closed
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PinnedCli:
    """One allowlisted, exactly-pinned command-line tool.

    ``package`` and ``binary`` are separate because they are NOT always the
    same string: npm runs the spec but the binary name may differ from the
    package name. Keeping both explicit stops a future entry from accidentally
    assuming they match.
    """

    package: str
    version: str
    binary: str

    @property
    def spec(self) -> str:
        """The exact ``package@version`` spec handed to the package manager."""
        return f"{self.package}@{self.version}"

    def is_exact(self) -> bool:
        return bool(_EXACT_VERSION_RE.match(self.version))


#: The ONLY command-line tools this application will ever execute in a build.
#:
#: Closed by construction, for the same reason
#: :data:`DEPENDENCY_COMPANION_PACKAGES` is closed: the map is the allowlist.
#: A caller supplies a ``cli_id`` and gets whatever this table says, or nothing.
#: No caller, model, prompt, or resource text can add a row or override a
#: version, because nothing in the codebase writes to this mapping after
#: import.
#:
#: ``impeccable`` is deliberately ABSENT. Its npm package is a binary shim that
#: resolves or DOWNLOADS an opaque platform executable into ``~/.impeccable``,
#: which would put a downloaded binary inside the containment model of a project
#: build. Impeccable is invoked from its explicitly provisioned profile skill's
#: bundled engine instead (see ``app.core.design_activation``).
#:
#: Versions were verified against the live npm registry as current stable.
PINNED_CLIS: Dict[str, PinnedCli] = {
    "shadcn": PinnedCli(
        package="shadcn", version=SHADCN_CLI_VERSION, binary="shadcn"
    ),
    "transitions_dev": PinnedCli(
        package="transitions-dev", version="0.3.0", binary="transitions-dev"
    ),
}


def pinned_cli(cli_id: str) -> Optional[PinnedCli]:
    """The :class:`PinnedCli` for ``cli_id``, or ``None`` if not allowlisted.

    The only function that turns a name into an executable package. Mirrors
    :func:`resolve_package`'s contract: an unrecognised id yields ``None``
    rather than something derived from the input.
    """
    if not isinstance(cli_id, str):
        return None
    return PINNED_CLIS.get(cli_id)


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


def build_pinned_cli_prefix(
    manager: Sequence[str], cli_id: str, project_root: Path
) -> Optional[Tuple[str, ...]]:
    """The manager-specific one-off runner argv for one allowlisted pinned CLI.

    Each package manager has a DIFFERENT one-off mechanism, and the pre-B code
    assumed they all shared one by appending a bare ``dlx``. That produced
    ``npm dlx ...``, and ``npm`` has no ``dlx`` subcommand -- npm's one-off
    mechanism is ``npm exec`` (``npx`` is its alias).

    **This is a pinned-CLI primitive, not an installer.** The package and
    version come only from :data:`PINNED_CLIS`, keyed by a caller-supplied
    ``cli_id`` that must already be in that closed map. There is deliberately
    no ``install_anything(package_name, version)`` form: a caller that could
    pass a package string would be able to execute arbitrary code inside a
    build, which is the entire class this module exists to prevent. A missing
    or unknown ``cli_id`` returns ``None`` and the caller runs nothing.

    Returning ``None`` means "this toolchain has no mechanism I will drive", and
    the caller runs NO command at all. That is deliberate for Yarn Classic:
    rather than silently falling back to ``npx``/``npm``, which would run a CLI
    the project never opted into, an unsupported toolchain fails closed.
    """
    pinned = PINNED_CLIS.get(cli_id) if isinstance(cli_id, str) else None
    if pinned is None:
        return None

    argv = tuple(manager)
    if not argv:
        return None
    name = argv[0]
    spec = pinned.spec
    binary = pinned.binary

    if name == "npm":
        # `npm exec --package=<spec> -- <binary>`. The trailing `--` is
        # REQUIRED: without it npm re-parses later switches as its own and
        # would swallow the CLI's own flags (e.g. shadcn's `--yes`). `--yes`
        # suppresses npm's own install prompt so a supervised run cannot block
        # on it.
        return argv + ("exec", "--yes", f"--package={spec}", "--", binary)
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


def registry_invocation_prefix(
    manager: Sequence[str], *, project_root: Path, version: str
) -> Optional[Tuple[str, ...]]:
    """The one-off runner argv for the pinned **shadcn** CLI.

    Retained as a named seam because the component path calls it directly with
    an explicit version, and because the D2/D3a mutation driver anchors on it.
    The runner construction itself now lives in
    :func:`build_pinned_cli_prefix`, so the per-manager argv shape is defined in
    exactly one place.

    ``version`` must equal :data:`SHADCN_CLI_VERSION`. A caller passing any
    other value would reintroduce a floating pin, so a mismatch fails closed
    rather than being interpolated into the argv.
    """
    if version != SHADCN_CLI_VERSION:
        logger.error("Refusing a registry invocation with a non-pinned shadcn version.")
        return None
    return build_pinned_cli_prefix(manager, REGISTRY_DEPENDENCY, project_root)


def build_install_argv(
    manager: Sequence[str], dependency_id: str
) -> Tuple[str, ...]:
    """The install argv for one allowlisted dependency's RUNTIME package.

    Every spec is exact (``name@version``). ``--save-exact`` is deliberate: a
    dependency added without a pin can float to a new version on a later build,
    which makes a "verified installed" claim describe something that will not be
    installed again tomorrow.

    This returns the runtime command ONLY. Companions are a separate command
    because they land in a DIFFERENT section of ``package.json``, and that
    section is part of the postcondition -- see
    :func:`build_companion_install_argv` and :func:`required_package_specs`.
    """
    argv = tuple(manager)
    runtime = PackageSpec(
        package=DEPENDENCY_PACKAGES[dependency_id],
        version=DEPENDENCY_PACKAGE_PINS[dependency_id],
    )
    return _runtime_install_argv(argv, runtime)


def build_companion_install_argv(
    manager: Sequence[str], dependency_id: str
) -> Tuple[Tuple[str, ...], ...]:
    """One install command per companion of ``dependency_id``.

    Returns an EMPTY tuple when the dependency has no companions, so a caller
    can iterate the result and run zero commands without testing anything --
    that is how "gsap and lenis install no companion" becomes a property of
    control flow rather than a flag someone has to remember to check.

    Each command is a flat argv list. There is deliberately no ``&&`` chaining
    and no shell string: commands are executed as argument vectors, so a joined
    string would pass ``&&`` to the package manager as a literal argument.
    Failure of one companion therefore stops the sequence at the caller rather
    than being papered over.
    """
    return tuple(
        _companion_install_argv(tuple(manager), companion.to_spec())
        for companion in resolve_companion_packages(dependency_id)
    )


def _runtime_install_argv(
    argv: Sequence[str], spec: PackageSpec
) -> Tuple[str, ...]:
    """Manager-specific argv installing one runtime spec with ``--save-exact``."""
    argv = tuple(argv)
    if argv and argv[0] == "npm":
        return argv + ("install", spec.spec, "--save-exact", "--no-audit", "--no-fund")
    if argv and argv[0] == "pnpm":
        return argv + ("add", "--save-exact", spec.spec)
    if argv and argv[0] == "yarn":
        return argv + ("add", "--exact", spec.spec)
    return argv + ("install", spec.spec, "--save-exact")


def _companion_install_argv(
    argv: Sequence[str], spec: PackageSpec
) -> Tuple[str, ...]:
    """Manager-specific argv installing one companion into the dev section."""
    argv = tuple(argv)
    if argv and argv[0] == "npm":
        return argv + (
            "install",
            spec.spec,
            "--save-dev",
            "--save-exact",
            "--no-audit",
            "--no-fund",
        )
    if argv and argv[0] == "pnpm":
        return argv + ("add", "--save-dev", "--save-exact", spec.spec)
    if argv and argv[0] == "yarn":
        return argv + ("add", "--dev", "--exact", spec.spec)
    return argv + ("install", spec.spec, "--save-dev", "--save-exact")


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
        manager, run one bounded runtime install plus one bounded command per
        companion, then VERIFY every required spec against the project manifest.

        ``installed`` is assigned only by that verification. For a dependency
        with a companion (``three`` -> ``@types/three``) the runtime package
        alone is NOT sufficient: the project would install cleanly and then fail
        ``tsc`` with ``TS7016``, which is precisely the condition this batch
        closes.
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

        runtime_spec = required_package_specs(dependency_id)[0]
        if not runtime_spec.is_exact():
            # The pin table has been edited into something that could resolve
            # differently tomorrow. Refuse rather than install a range.
            logger.error("Allowlisted package pin is not an exact version.")
            return InstallOutcome(
                dependency_id=dependency_id,
                state=INSTALL_FAILED,
                package=package,
                reason=REASON_PIN_NOT_EXACT,
            )

        if project_satisfies_dependency(self.project_root, dependency_id):
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

        receipts: List[CommandReceipt] = []

        process, timed_out = self._run(
            build_install_argv(manager, dependency_id), INSTALL_TIMEOUT_SECONDS
        )
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
        receipts.append(CommandReceipt.from_process(process, cwd_label="<project>"))

        # Companions are a SEPARATE command per companion, each into the dev
        # section. No companion means no command at all -- the loop body never
        # executes, which is how "gsap/lenis install no companion" is enforced
        # by control flow rather than by a conditional someone could forget.
        for argv in build_companion_install_argv(manager, dependency_id):
            companion_process, companion_timed_out = self._run(
                argv, INSTALL_TIMEOUT_SECONDS
            )
            if companion_timed_out:
                return InstallOutcome(
                    dependency_id=dependency_id,
                    state=INSTALL_FAILED,
                    package=package,
                    reason=REASON_TIMEOUT,
                    receipt=CommandReceipt.from_process(
                        companion_process, cwd_label="<project>"
                    )
                    if companion_process is not None
                    else receipts[-1],
                )
            if companion_process is None or companion_process.returncode != 0:
                receipt = (
                    CommandReceipt.from_process(
                        companion_process, cwd_label="<project>"
                    )
                    if companion_process is not None
                    else receipts[-1]
                )
                return InstallOutcome(
                    dependency_id=dependency_id,
                    state=INSTALL_FAILED,
                    package=package,
                    reason=REASON_INSTALL_FAILED,
                    receipt=receipt,
                )
            receipts.append(
                CommandReceipt.from_process(companion_process, cwd_label="<project>")
            )

        # Every command succeeded. That is NOT yet an installed claim.
        verified = project_satisfies_dependency(self.project_root, dependency_id)
        receipt = receipts[-1]
        if not verified:
            return InstallOutcome(
                dependency_id=dependency_id,
                state=INSTALL_FAILED,
                package=package,
                reason=_incomplete_reason(dependency_id, self.project_root),
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

        # Expand to the REVIEWED nested closure. Some builtins' emitted source
        # imports a sibling builtin the registry item does not declare (live:
        # ``dialog`` imports ``@/components/ui/button``), so requesting only
        # ``dialog`` would materialize a file that cannot resolve. The closure is
        # application-owned -- never taken from upstream metadata -- so a registry
        # cannot widen it. Every requested and nested component is still gated by
        # the allowlist above.
        effective = expand_reviewed_component_closure(allowed)

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

        argv = build_registry_argv(prefix, components=effective)
        # Snapshot BEFORE the CLI runs. The pinned CLI writes packages into
        # package.json itself, so the postcondition must compare against this
        # application-owned baseline rather than trusting a zero exit code.
        before = snapshot_direct_dependencies(self.project_root)
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

        # 1. The direct-dependency delta must be inside the reviewed set, and
        #    the reviewed packages must be normalized to exact application pins.
        boundary = self._enforce_registry_dependency_boundary(
            before, expected_registry_packages(effective), manager
        )
        if not boundary.ok:
            return (
                InstallOutcome(
                    dependency_id=REGISTRY_DEPENDENCY,
                    state=INSTALL_FAILED,
                    package=None,
                    reason=boundary.reason,
                    receipt=boundary.receipt or receipt,
                    verified_components=(),
                ),
                allowed,
                rejected,
            )

        # 1b. Packages the emitted SOURCE imports but the CLI does NOT install
        #     (live: ``lucide-react``) must be present at their exact application
        #     pin, or the emitted component cannot build. Installed through the
        #     SAME exact-pin mechanism as any other dependency -- never a
        #     registry-supplied range -- and refused if a required package has no
        #     reviewed pin.
        imports_ok, imports_receipt = self._ensure_required_registry_imports(
            effective, manager
        )
        if not imports_ok:
            return (
                InstallOutcome(
                    dependency_id=REGISTRY_DEPENDENCY,
                    state=INSTALL_FAILED,
                    package=None,
                    reason=REASON_REGISTRY_IMPORT_UNRESOLVED,
                    receipt=imports_receipt or boundary.receipt or receipt,
                    verified_components=(),
                ),
                allowed,
                rejected,
            )

        # 2. Every requested component -- AND every reviewed nested component the
        #    closure pulled in -- must be materialized under the approved
        #    directory. A partial install is not a success.
        verified = verify_components_materialized(
            self.project_root, effective, component_dir
        )
        if not verified:
            return (
                InstallOutcome(
                    dependency_id=REGISTRY_DEPENDENCY,
                    state=INSTALL_FAILED,
                    package=None,
                    reason=REASON_COMPONENTS_NOT_VERIFIED,
                    receipt=imports_receipt or boundary.receipt or receipt,
                    verified_components=(),
                ),
                allowed,
                rejected,
            )

        # 3. The INSTALLATION is untrusted. The emitted source may import a
        #    package the registry never declared (so the manifest delta is
        #    clean) and the CLI never installed. Every bare package the emitted
        #    source imports must be one this application provides or reviewed;
        #    otherwise the build cannot resolve and the state is NOT installed.
        source_ok = self._enforce_registry_source_import_boundary(
            effective, component_dir
        )
        if not source_ok:
            return (
                InstallOutcome(
                    dependency_id=REGISTRY_DEPENDENCY,
                    state=INSTALL_FAILED,
                    package=None,
                    reason=REASON_REGISTRY_SOURCE_IMPORT_UNREVIEWED,
                    receipt=imports_receipt or boundary.receipt or receipt,
                    verified_components=(),
                ),
                allowed,
                rejected,
            )

        # 4. Whole-operation removal check. Steps 1-3 compare the CLI's own
        #    delta and the emitted sources, but the NORMALIZATION installs in
        #    step 1 (and step 1b) also run a package manager, which may prune a
        #    pre-existing project dependency. Nothing added after ``before`` may
        #    have REMOVED a package that ``before`` declared: a registry install
        #    adds reviewed packages, it never deletes the project's own deps.
        if self._removal_after_install(before):
            logger.warning(
                "Refusing a registry install that removed a pre-existing "
                "project dependency."
            )
            return (
                InstallOutcome(
                    dependency_id=REGISTRY_DEPENDENCY,
                    state=INSTALL_FAILED,
                    package=None,
                    reason=REASON_REGISTRY_DEPENDENCY_DRIFT,
                    receipt=imports_receipt or boundary.receipt or receipt,
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
                receipt=imports_receipt or boundary.receipt or receipt,
                verified_components=verified,
            ),
            allowed,
            rejected,
        )

    def _removal_after_install(
        self, before: Mapping[str, Mapping[str, str]]
    ) -> bool:
        """Whether any package ``before`` declared is now ABSENT.

        The last line of defence for the whole operation: every prior check
        compares ADDITIONS. A package manager run by normalization, or a CLI
        that rewrote the manifest, could silently DROP a project dependency; that
        is a mutation of the project's own dependency state and must fail closed.
        """
        after = snapshot_direct_dependencies(self.project_root)
        return bool(removed_direct_dependencies(before, after))

    def _enforce_registry_source_import_boundary(
        self,
        components: Sequence[str],
        component_dir: Optional[Path],
        *,
        extra_packages: Sequence[str] = (),
    ) -> bool:
        """Whether every bare package the emitted sources import is reviewed.

        The identity is trusted; the installation is not. ``component_dir`` is
        the project's own reviewed destination, so the files read here are the
        ones the CLI just wrote. Any bare package outside
        ``_reviewed_source_import_packages()`` fails closed -- a package the
        registry introduced only at the source level (never declared, never
        installed) is exactly what the manifest delta guard cannot see.
        """
        if component_dir is None or not components:
            return True
        allowed = _reviewed_source_import_packages(*extra_packages)
        for component in components:
            source = component_source_text(self.project_root, component_dir, component)
            if source is None:
                # Materialization is verified separately; an unreadable file is
                # not this boundary's failure.
                continue
            offending = unreviewed_imports(source, allowed_packages=allowed)
            if offending:
                logger.warning(
                    "Refusing a registry component whose emitted source imports "
                    "%d unreviewed package(s).",
                    len(offending),
                )
                return False
        return True

    def _ensure_required_registry_imports(
        self,
        components: Sequence[str],
        manager: Sequence[str],
    ) -> Tuple[bool, Optional[CommandReceipt]]:
        """Install/verify the reviewed packages a builtin's SOURCE imports.

        The pinned CLI writes ``cn``/``radix-ui`` but does NOT install
        ``lucide-react``, which several emitted builtins import. Each such package
        is installed through the SAME exact application-owned pin path
        (:func:`build_registry_normalization_argv`) and then re-verified in the
        project manifest -- never a registry range. A required package with no
        reviewed pin fails closed rather than being installed at whatever the
        upstream suggested.
        """
        required = required_registry_packages(components)
        if not required:
            return (True, None)

        pins = reviewed_registry_package_pins()
        if any(package not in pins for package in required):
            logger.error(
                "A component's emitted source requires a package with no "
                "application-owned exact pin."
            )
            return (False, None)

        # Already present at the exact pin? Nothing to do.
        manifest = snapshot_direct_dependencies(self.project_root)
        installed = manifest.get(SECTION_DEPENDENCIES, {})
        missing = tuple(
            package for package in required if installed.get(package) != pins[package]
        )
        if not missing:
            return (True, None)

        last_receipt: Optional[CommandReceipt] = None
        for argv in build_registry_normalization_argv(manager, missing):
            process, timed_out = self._run(argv, INSTALL_TIMEOUT_SECONDS)
            if timed_out:
                return (False, last_receipt)
            if process is None or process.returncode != 0:
                receipt = (
                    CommandReceipt.from_process(process, cwd_label="<project>")
                    if process is not None
                    else None
                )
                return (False, receipt)
            last_receipt = CommandReceipt.from_process(process, cwd_label="<project>")

        final = snapshot_direct_dependencies(self.project_root)
        installed = final.get(SECTION_DEPENDENCIES, {})
        if any(installed.get(package) != pins[package] for package in required):
            logger.error(
                "A required component import is not at its exact "
                "application-owned pin after normalization."
            )
            return (False, last_receipt)
        return (True, last_receipt)

    def _enforce_registry_dependency_boundary(
        self,
        before: Mapping[str, Mapping[str, str]],
        allowed_packages: Sequence[str],
        manager: Sequence[str],
    ) -> "_RegistryBoundaryResult":
        """Verify the CLI's direct-dependency delta, then normalize to exact pins.

        This is the guard that makes "the CLI succeeded" insufficient. It:

        1. snapshots the manifest AFTER the CLI,
        2. computes the DIRECT dependency delta (all four sections),
        3. refuses any package outside ``allowed_packages`` or in a non-runtime
           section,
        4. normalizes the reviewed packages that actually appeared to their exact
           application-owned pins (the CLI writes ranges; Hermes installs exact),
        5. re-verifies the final manifest holds the exact pins.

        It never echoes an untrusted package name into a reason; the offending
        names stay in the bounded internal tuple.
        """
        after = snapshot_direct_dependencies(self.project_root)
        acceptable, offending = registry_dependency_delta_is_acceptable(
            before, after, allowed_packages=allowed_packages
        )
        if not acceptable:
            logger.warning(
                "Refusing a registry install that introduced %d unreviewed "
                "direct dependency(ies).",
                len(offending),
            )
            return _RegistryBoundaryResult(
                ok=False, reason=REASON_REGISTRY_DEPENDENCY_DRIFT
            )

        delta = dependency_delta(before, after)
        introduced = tuple(sorted(delta.get(SECTION_DEPENDENCIES, {})))
        if not introduced:
            # Nothing changed in the runtime set: no normalization needed.
            return _RegistryBoundaryResult(ok=True)

        # A package that appeared must have a reviewed exact pin. A reviewed
        # package with no pin is a configuration error, not a pass.
        pins = reviewed_registry_package_pins()
        missing_pin = [
            package for package in introduced if package not in pins
        ]
        if missing_pin:
            logger.error(
                "A registry-introduced package has no application-owned exact pin."
            )
            return _RegistryBoundaryResult(
                ok=False, reason=REASON_REGISTRY_PACKAGE_NOT_EXACT
            )

        # Already exact? Then nothing to normalize.
        already_exact = all(
            after.get(SECTION_DEPENDENCIES, {}).get(package) == pins[package]
            for package in introduced
        )
        if already_exact:
            return _RegistryBoundaryResult(ok=True)

        last_receipt: Optional[CommandReceipt] = None
        for argv in build_registry_normalization_argv(manager, introduced):
            normalize_process, normalize_timed_out = self._run(
                argv, INSTALL_TIMEOUT_SECONDS
            )
            if normalize_timed_out:
                return _RegistryBoundaryResult(
                    ok=False, reason=REASON_TIMEOUT
                )
            if normalize_process is None or normalize_process.returncode != 0:
                receipt = (
                    CommandReceipt.from_process(
                        normalize_process, cwd_label="<project>"
                    )
                    if normalize_process is not None
                    else None
                )
                return _RegistryBoundaryResult(
                    ok=False, reason=REASON_INSTALL_FAILED, receipt=receipt
                )
            last_receipt = CommandReceipt.from_process(
                normalize_process, cwd_label="<project>"
            )

        # Re-verify: the final manifest must hold the EXACT application pins.
        final = snapshot_direct_dependencies(self.project_root)
        for package in introduced:
            if final.get(SECTION_DEPENDENCIES, {}).get(package) != pins[package]:
                logger.error(
                    "A registry-introduced package is not at its exact "
                    "application-owned pin after normalization."
                )
                return _RegistryBoundaryResult(
                    ok=False,
                    reason=REASON_REGISTRY_PACKAGE_NOT_EXACT,
                    receipt=last_receipt,
                )
        return _RegistryBoundaryResult(ok=True, receipt=last_receipt)

    # -- external registry components -----------------------------------

    def install_external_component(self, request) -> InstallOutcome:
        """Install ONE reviewed external registry component, bounded at every step.

        ``request`` is a :class:`app.core.design_registry.RegistryInstallRequest`.
        The locator inside it is application-owned (never a caller URL), and the
        reviewed contract's expected dependency set is what the post-install
        manifest delta is checked against -- so a registry that silently adds a
        package makes this FAIL rather than installing it.

        The architecture is the safe one this batch requires:

            snapshot direct deps
            -> pinned shadcn `add <locator>`
            -> verify the direct-dependency delta is the reviewed set
            -> normalize any registry-introduced range to an exact app pin
            -> verify the component materialized under ``aliases.components``

        A refused/absent request produces NO command.
        """
        if request is None or getattr(request, "is_builtin", True):
            return InstallOutcome(
                dependency_id=REGISTRY_DEPENDENCY,
                state=INSTALL_FAILED,
                package=None,
                reason=REASON_COMPONENT_NOT_ALLOWED,
            )

        locator = getattr(request, "registry_locator_id", "")
        if not locator:
            return InstallOutcome(
                dependency_id=REGISTRY_DEPENDENCY,
                state=INSTALL_FAILED,
                package=None,
                reason=REASON_COMPONENT_NOT_ALLOWED,
            )

        # The accepted package set is derived from the APPLICATION-OWNED reviewed
        # contract for this (source, component) -- NOT from a field on the passed
        # object. A duck-typed stand-in with a widened
        # ``required_dependency_ids`` therefore cannot expand what the boundary
        # accepts: the contract is the single source of truth, resolved here
        # rather than trusted from the argument.
        from app.core.design_registry import reviewed_component_contract

        source = getattr(request, "source", "")
        component_id = getattr(request, "component_id", "")
        contract = reviewed_component_contract(source, component_id)
        if contract is None:
            return InstallOutcome(
                dependency_id=REGISTRY_DEPENDENCY,
                state=INSTALL_FAILED,
                package=None,
                reason=REASON_COMPONENT_NOT_ALLOWED,
            )
        allowed_packages = tuple(
            sorted(
                DEPENDENCY_PACKAGES[dependency_id]
                for dependency_id in contract.expected_dependency_ids
                if dependency_id in DEPENDENCY_PACKAGES
            )
        )

        component_dir = approved_external_component_dir(self.project_root)
        if component_dir is None:
            return InstallOutcome(
                dependency_id=REGISTRY_DEPENDENCY,
                state=INSTALL_FAILED,
                package=None,
                reason=REASON_SHADCN_CONFIG_INVALID,
            )

        manager = detect_package_manager(self.project_root)
        if manager is None:
            return InstallOutcome(
                dependency_id=REGISTRY_DEPENDENCY,
                state=INSTALL_FAILED,
                package=None,
                reason=REASON_NO_PACKAGE_MANAGER,
            )

        prefix = registry_invocation_prefix(
            manager, project_root=self.project_root, version=SHADCN_CLI_VERSION
        )
        if prefix is None:
            return InstallOutcome(
                dependency_id=REGISTRY_DEPENDENCY,
                state=INSTALL_FAILED,
                package=None,
                reason=REASON_MANAGER_UNSUPPORTED,
            )

        argv = tuple(prefix) + ("add", locator, "--yes", "--overwrite")
        before = snapshot_direct_dependencies(self.project_root)
        process, timed_out = self._run(argv, REGISTRY_TIMEOUT_SECONDS)
        if timed_out:
            return InstallOutcome(
                dependency_id=REGISTRY_DEPENDENCY,
                state=INSTALL_FAILED,
                package=None,
                reason=REASON_TIMEOUT,
            )
        if process is None or process.returncode != 0:
            receipt = (
                CommandReceipt.from_process(process, cwd_label="<project>")
                if process is not None
                else None
            )
            return InstallOutcome(
                dependency_id=REGISTRY_DEPENDENCY,
                state=INSTALL_FAILED,
                package=None,
                reason=REASON_INSTALL_FAILED,
                receipt=receipt,
            )

        receipt = CommandReceipt.from_process(process, cwd_label="<project>")

        boundary = self._enforce_registry_dependency_boundary(
            before, allowed_packages, manager
        )
        if not boundary.ok:
            return InstallOutcome(
                dependency_id=REGISTRY_DEPENDENCY,
                state=INSTALL_FAILED,
                package=None,
                reason=boundary.reason,
                receipt=boundary.receipt or receipt,
                verified_components=(),
            )

        verified = verify_external_component_materialized(
            self.project_root, request.component_id, component_dir
        )
        if not verified:
            return InstallOutcome(
                dependency_id=REGISTRY_DEPENDENCY,
                state=INSTALL_FAILED,
                package=None,
                reason=REASON_COMPONENTS_NOT_VERIFIED,
                receipt=boundary.receipt or receipt,
                verified_components=(),
            )

        # The INSTALLATION is untrusted: the emitted source may import a package
        # the registry never declared and the CLI never installed. Every bare
        # package it imports must be reviewed; otherwise the build cannot resolve.
        if not self._enforce_registry_source_import_boundary(
            (request.component_id,), component_dir, extra_packages=allowed_packages
        ):
            return InstallOutcome(
                dependency_id=REGISTRY_DEPENDENCY,
                state=INSTALL_FAILED,
                package=None,
                reason=REASON_REGISTRY_SOURCE_IMPORT_UNREVIEWED,
                receipt=boundary.receipt or receipt,
                verified_components=(),
            )

        # Whole-operation removal check: normalization installs also run a
        # package manager, which must not drop a pre-existing project dependency.
        if self._removal_after_install(before):
            return InstallOutcome(
                dependency_id=REGISTRY_DEPENDENCY,
                state=INSTALL_FAILED,
                package=None,
                reason=REASON_REGISTRY_DEPENDENCY_DRIFT,
                receipt=boundary.receipt or receipt,
                verified_components=(),
            )

        return InstallOutcome(
            dependency_id=REGISTRY_DEPENDENCY,
            state="installed",
            package=None,
            reason=REASON_ALREADY_INSTALLED,
            receipt=boundary.receipt or receipt,
            verified_components=verified,
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
    "DEPENDENCY_COMPANION_PACKAGES",
    "DEPENDENCY_PACKAGE_PINS",
    "DEPENDENCY_PACKAGES",
    "DEPENDENCY_SECTIONS",
    "INSTALL_FAILED",
    "PINNED_CLIS",
    "PinnedCli",
    "INSTALL_STATES",
    "REASON_ALREADY_INSTALLED",
    "REASON_COMPANION_NOT_VERIFIED",
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
    "REASON_PIN_NOT_EXACT",
    "REASON_PROJECT_INVALID",
    "REASON_SHADCN_CONFIG_INVALID",
    "REASON_REGISTRY_DEPENDENCY_DRIFT",
    "REASON_REGISTRY_PACKAGE_NOT_EXACT",
    "REASON_REGISTRY_IMPORT_UNRESOLVED",
    "REASON_REGISTRY_SOURCE_IMPORT_UNREVIEWED",
    "REASON_BUILTIN_COMPONENT_UNREVIEWED",
    "REASON_TIMEOUT",
    "SECTION_DEPENDENCIES",
    "SECTION_DEV_DEPENDENCIES",
    "SNAPSHOT_SECTIONS",
    "REGISTRY_DEPENDENCY",
    "REGISTRY_INTRODUCED_PACKAGE_PINS",
    "REVIEWED_BUILTIN_COMPONENT_DEPENDENCIES",
    "REVIEWED_BUILTIN_COMPONENT_IMPORTS",
    "REVIEWED_BUILTIN_COMPONENT_NESTED",
    "SHADCN_CLI_VERSION",
    "SHADCN_CONFIG_FILENAME",
    "TERMINAL_INSTALL_STATES",
    "CompanionPackage",
    "CommandReceipt",
    "InstallOutcome",
    "PackageSpec",
    "allowlisted_dependencies",
    "allowed_shadcn_components",
    "approved_component_dir",
    "approved_external_component_dir",
    "bare_package_of",
    "component_source_text",
    "declared_imports",
    "dependency_delta",
    "expand_reviewed_component_closure",
    "unreviewed_imports",
    "expected_registry_packages",
    "is_contained",
    "package_name_is_well_formed",
    "project_declares_dependency",
    "project_satisfies_dependency",
    "project_satisfies_spec",
    "registry_dependency_delta_is_acceptable",
    "removed_direct_dependencies",
    "required_registry_packages",
    "reviewed_registry_package_pins",
    "snapshot_direct_dependencies",
    "verify_external_component_materialized",
    "InstallReport",
    "DesignDependencyInstaller",
    "LOCKFILES",
    "YARN_BERRY_CONFIG",
    "INSTALL_TIMEOUT_SECONDS",
    "REGISTRY_TIMEOUT_SECONDS",
    "build_companion_install_argv",
    "build_install_argv",
    "build_pinned_cli_prefix",
    "build_registry_argv",
    "build_registry_normalization_argv",
    "detect_package_manager",
    "filter_allowed_components",
    "pinned_cli",
    "registry_invocation_prefix",
    "required_package_specs",
    "resolve_companion_packages",
    "resolve_package",
    "verify_components_materialized",
]