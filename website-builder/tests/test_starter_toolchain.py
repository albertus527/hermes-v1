"""The frontend starter's own toolchain is a production dependency.

``templates/frontend-starter/`` is copied verbatim into every generated project
(``app.projects.build._STARTER_PATH``), and its ``npm ci`` / ``npm run build`` /
``npm run typecheck`` are the three cheap checks that gate a real build. A
defect in the starter's own TypeScript project therefore fails every build,
which is exactly what a VPS D3a smoke observed: the ``@`` alias patch made
``vite.config.ts`` import ``node:url`` and read ``import.meta.url``, but the
Node-side project declared ``"types": []`` and the manifest declared no
``@types/node``, so ``tsc -b`` — which ``npm run build`` runs first — rejected
the config file and the build never reached Vite.

What is asserted here:

  * the manifest carries an EXACTLY pinned ``@types/node`` (never a range), and
    that pin's major matches the ``engines.node`` contract the starter
    enforces through ``.npmrc``'s ``engine-strict=true``;
  * ``package-lock.json`` pins the same version, so ``npm ci`` is reproducible;
  * the Node-side TypeScript project includes the ``node`` types it now needs;
  * the browser-side TypeScript project does NOT — the Vite/browser type
    boundary is unchanged, which is what keeps generated application source
    honest about what it can touch;
  * the real toolchain succeeds end to end in a copy of the real starter.

A note on how load-bearing each half is. On a machine with no ancestor
``@types/node``, the three command assertions fail without the manifest pin.
On a developer machine that happens to have one (or with Vite's own
``/// <reference types="node" />`` resolving transitively), they can pass with
the package absent — so the manifest, lockfile and ``tsconfig`` assertions are
what actually kill the regression, and they are host-independent by
construction. The command assertions exist to prove the toolchain really
builds, not to be the mutation killer.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import credentials

REPO_ROOT = Path(__file__).resolve().parents[2]
STARTER = REPO_ROOT / "templates" / "frontend-starter"

NODE_TYPES = "@types/node"

# A range would let a future `npm install` move the type surface under a
# project that pins its runtime to a single Node line. Only these characters
# turn an exact pin into a floating one.
_RANGE_CHARACTERS = "^~><=*|xX -"


def _manifest() -> dict:
    return json.loads((STARTER / "package.json").read_text(encoding="utf-8"))


def _lockfile() -> dict:
    return json.loads((STARTER / "package-lock.json").read_text(encoding="utf-8"))


def _tsconfig(name: str) -> dict:
    # The starter's tsconfigs are JSONC: ``/* Bundler mode */``-style section
    # markers. Strip comments LINE-wise rather than with a regex over the whole
    # document — a naive ``/\\*.*?\\*/`` would happily match the ``/*`` inside
    # the path mapping ``"@/*": ["./src/*"]`` and eat real content.
    kept: list[str] = []
    in_block = False
    for line in (STARTER / name).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if in_block:
            if "*/" in stripped:
                in_block = False
            continue
        if stripped.startswith("/*"):
            in_block = "*/" not in stripped
            continue
        if stripped.startswith("//"):
            continue
        kept.append(line)
    return json.loads("\n".join(kept))


def _pinned_node_types() -> str:
    return _manifest()["devDependencies"][NODE_TYPES]


def _engine_bounds() -> tuple[int, int]:
    """``engines.node`` as a ``(lowest_major, exclusive_ceiling_major)`` pair.

    The starter declares a RANGE, not a single major, so the pin's job is to
    describe a runtime inside that range — not to sit below its floor or at
    its ceiling.
    """
    declared = _manifest()["engines"]["node"]
    numbers = [int(n) for n in re.findall(r"\d+", declared)]
    assert len(numbers) >= 2, f"unreadable engines.node range: {declared!r}"
    return numbers[0], numbers[1]


# ---------------------------------------------------------------------------
# Manifest: an exact pin, on the Node line the starter declares
# ---------------------------------------------------------------------------


def test_starter_declares_node_types_as_a_dev_dependency():
    """Node types are build-time only — they must not reach the runtime bundle."""
    declared = _manifest()["devDependencies"]
    assert NODE_TYPES in declared
    assert NODE_TYPES not in _manifest().get("dependencies", {})


def test_starter_pins_node_types_exactly():
    """An exact pin, not a range.

    Every other entry in this manifest is exact for the same reason: the
    generated project is copied somewhere else and rebuilt later, and a
    floating range is an unreproducible build.
    """
    pin = _pinned_node_types()
    assert not [c for c in pin if c in _RANGE_CHARACTERS], (
        f"{NODE_TYPES} is pinned to {pin!r}, which is a range"
    )
    # A plain semver: digits and dots only.
    assert pin.replace(".", "").isdigit(), pin


def test_pinned_node_types_major_matches_the_declared_engine_range():
    """The type surface must describe the Node the starter actually runs on.

    ``@types/node`` versions track Node majors. Pinning the wrong major either
    declares APIs the pinned runtime does not have (build passes, runtime
    throws) or withholds ones it does (build fails on a correct config).
    """
    engines = _manifest()["engines"]["node"]
    floor, ceiling = _engine_bounds()
    major = int(_pinned_node_types().split(".")[0])
    assert floor <= major < ceiling, (
        f"{NODE_TYPES} major {major} falls outside the starter's engine range "
        f"{engines}"
    )


def test_node_types_pin_matches_the_nvmrc_runtime_line():
    """The pin's minor line is the one ``.nvmrc`` selects."""
    nvmrc = (STARTER / ".nvmrc").read_text(encoding="utf-8").strip()
    runtime_major, runtime_minor = (int(p) for p in nvmrc.split(".")[:2])
    major, minor = (int(part) for part in _pinned_node_types().split(".")[:2])
    assert (major, minor) == (runtime_major, runtime_minor), (
        f"{NODE_TYPES} {major}.{minor} does not match the .nvmrc runtime "
        f"{nvmrc}"
    )


# ---------------------------------------------------------------------------
# Lockfile: `npm ci` must reproduce the same pin
# ---------------------------------------------------------------------------


def test_lockfile_root_mirrors_the_manifest_pin():
    """`npm ci` installs from the lockfile, so its root entry must agree."""
    root = _lockfile()["packages"][""]
    assert root["devDependencies"][NODE_TYPES] == _pinned_node_types()


def test_lockfile_resolves_node_types_to_that_exact_version():
    """The resolved tree entry pins the version AND carries an integrity hash."""
    entry = _lockfile()["packages"][f"node_modules/{NODE_TYPES}"]
    assert entry["version"] == _pinned_node_types()
    assert entry.get("integrity", "").startswith("sha512-")
    assert entry.get("dev") is True


# ---------------------------------------------------------------------------
# TypeScript project boundary
# ---------------------------------------------------------------------------


def test_node_side_project_includes_node_types():
    """``vite.config.ts`` runs in Node: it needs the Node type surface.

    Without this the config's own ``import.meta.url`` / ``node:url`` usage is
    untyped and ``tsc -b`` rejects the file — which fails ``npm run build``
    before Vite ever starts.
    """
    types = _tsconfig("tsconfig.node.json")["compilerOptions"].get("types", [])
    assert "node" in types


def test_node_side_project_does_not_pull_in_the_browser_types():
    """The Node-side project stays Node-only; the alias's own imports must
    resolve without dragging in a DOM lib it has no business having."""
    types = _tsconfig("tsconfig.node.json")["compilerOptions"].get("types", [])
    assert "vite/client" not in types


def test_app_side_project_does_not_include_node_types():
    """Browser source keeps its existing Vite/browser boundary.

    Handing generated application source the Node globals would let a
    component reach for ``process``/``Buffer`` and typecheck here, then fail
    in the browser. The Node types belong to the config project alone.
    """
    types = _tsconfig("tsconfig.app.json")["compilerOptions"].get("types", [])
    assert "node" not in types
    assert "vite/client" in types, "the browser side must keep its Vite types"


def test_app_side_project_keeps_its_dom_lib():
    """The browser lib set is untouched — this change added nothing to it."""
    libs = _tsconfig("tsconfig.app.json")["compilerOptions"]["lib"]
    assert "DOM" in libs and "DOM.Iterable" in libs
    assert not any(entry.startswith("node") for entry in libs)


# ---------------------------------------------------------------------------
# The real toolchain, on a copy of the real starter
# ---------------------------------------------------------------------------

NPM = shutil.which("npm")
requires_npm = pytest.mark.skipif(NPM is None, reason="npm is not installed")

COMMAND_TIMEOUT = 900


def _npm_env(workspace: Path) -> dict:
    """The production generated-project shell environment.

    Not ``os.environ``: ``npm ci`` executes dependency lifecycle scripts, so
    this goes through the same ``build_env`` boundary the application uses for
    its cheap checks. If the strictest env could not run the real toolchain,
    neither could a real build.
    """
    return credentials.build_env("proj-starter-toolchain", workspace)


def _run(workspace: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [NPM, *args],
        cwd=str(workspace),
        env=_npm_env(workspace),
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT,
    )


def _diagnose(result: subprocess.CompletedProcess) -> str:
    return (
        f"command: {result.args}\nexit: {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


@pytest.fixture(scope="module")
def installed_starter(tmp_path_factory) -> Path:
    """A byte-for-byte copy of the starter with its dependencies installed.

    One install shared by the three command assertions: ``npm ci`` is the slow,
    network-touching step, and re-running it per test would make the suite
    pay for it three times over.
    """
    workspace = tmp_path_factory.mktemp("starter") / "project"
    shutil.copytree(STARTER, workspace, ignore=shutil.ignore_patterns("node_modules", "dist"))
    result = _run(workspace, "ci")
    if result.returncode != 0:
        pytest.fail("npm ci failed in the copied starter:\n" + _diagnose(result))
    return workspace


@requires_npm
def test_npm_ci_installs_the_pinned_node_types_into_the_project(
    installed_starter,
):
    """The pin is INSTALLED, at exactly the pinned version.

    This is the assertion that keeps the regression dead on a machine with an
    ancestor ``@types/node``: no package in the starter's own tree means the
    starter does not declare what it needs, regardless of what a parent
    directory happens to provide.
    """
    installed = json.loads(
        (installed_starter / "node_modules" / NODE_TYPES / "package.json")
        .read_text(encoding="utf-8")
    )
    assert installed["version"] == _pinned_node_types()


@requires_npm
def test_starter_typecheck_succeeds(installed_starter):
    """`npm run typecheck` (``tsc -b``) accepts both TypeScript projects."""
    result = _run(installed_starter, "run", "typecheck")
    assert result.returncode == 0, _diagnose(result)


@requires_npm
def test_starter_build_succeeds(installed_starter):
    """`npm run build` (``tsc -b && vite build``) produces the dist artifact."""
    result = _run(installed_starter, "run", "build")
    assert result.returncode == 0, _diagnose(result)
    assert (installed_starter / "dist" / "index.html").is_file()


@requires_npm
def test_starter_build_runs_after_a_clean_typecheck(installed_starter):
    """A clean tree builds — no cached ``.tsbuildinfo`` short-circuit.

    ``tsc -b`` is incremental, so a build that follows a successful typecheck
    can pass on cache alone. Dropping the build info forces the real
    diagnostic run that the VPS smoke hit.
    """
    shutil.rmtree(installed_starter / "node_modules" / ".tmp", ignore_errors=True)
    result = _run(installed_starter, "run", "typecheck")
    assert result.returncode == 0, _diagnose(result)