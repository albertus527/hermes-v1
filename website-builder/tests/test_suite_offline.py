"""No DEFAULT-suite test may depend on the internet.

`pytest` (the default selection, i.e. everything not marked ``integration``) must
run offline. A test that shells out to a package manager -- directly OR through a
module-level helper -- or opens a non-loopback socket, makes the whole suite
depend on the network, and a warm local cache silently hides that until a cold
host runs it.

The static check walks each non-integration test's body PLUS every module-level
helper it transitively calls, because the real offender hides one hop away: the
test calls ``_run(workspace, "ci")`` and ``_run`` is what invokes ``NPM`` (bound
from ``shutil.which("npm")`` at module scope). A body-only scan misses it.

Tests that legitimately need the network must carry ``@pytest.mark.integration``
(the repo's own marker, deselected by ``addopts = "-m 'not integration'"``).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TESTS_DIR = Path(__file__).resolve().parent

#: CLIs that reach the network when invoked.
NETWORK_CLIS = frozenset(
    {"npm", "npx", "pnpm", "yarn", "pip", "pip3", "curl", "wget", "gh"}
)

#: Attribute chains that ARE a network call when invoked.
_NET_CALL_NAMES = frozenset(
    {
        "urlopen",
        "requests.get", "requests.post", "requests.put", "requests.delete",
        "requests.head", "requests.request",
        "httpx.get", "httpx.post", "httpx.put", "httpx.delete", "httpx.head",
        "httpx.request", "httpx.Client", "httpx.AsyncClient",
        "socket.create_connection",
    }
)

#: A loopback literal, so an obviously-local call is not flagged.
_LOOPBACK_RE = re.compile(r"(127\.0\.0\.1|::1|localhost)")


def _dotted_name(node: ast.AST) -> str:
    """``a.b.c`` for a Name/Attribute chain, else ``''``."""
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _called_names(node: ast.AST) -> Set[str]:
    """Every bare function name called inside ``node``."""
    names: Set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _module_network_cli_names(tree: ast.Module) -> Set[str]:
    """Module names bound to a network CLI, e.g. ``NPM = shutil.which('npm')``."""
    names: Set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        if _dotted_name(call.func) != "shutil.which" or not call.args:
            continue
        arg = call.args[0]
        if isinstance(arg, ast.Constant) and arg.value in NETWORK_CLIS:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _argv_names_network_cli(call: ast.Call, cli_names: Set[str]) -> bool:
    """Whether a ``subprocess`` call's argv names a network CLI.

    The argv is the first positional argument. It may name the CLI literally
    (``"npm"``) or through a module constant (``NPM``).
    """
    if not call.args:
        return False
    for sub in ast.walk(call.args[0]):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if sub.value in NETWORK_CLIS:
                return True
        if isinstance(sub, ast.Name) and sub.id in cli_names:
            return True
    return False


def _segment_reaches_network(source: str, node: ast.AST, cli_names: Set[str]) -> bool:
    """Whether a single function/segment resolves or runs a network CLI, or calls
    a network entry point against a non-loopback host."""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        dotted = _dotted_name(sub.func)
        # shutil.which("npm")
        if dotted == "shutil.which" and sub.args:
            arg = sub.args[0]
            if isinstance(arg, ast.Constant) and arg.value in NETWORK_CLIS:
                return True
        # subprocess.run([NPM, ...])
        if dotted.startswith("subprocess."):
            if _argv_names_network_cli(sub, cli_names):
                return True
        # urlopen(...) / requests.get(...) / httpx... / socket.create_connection(...)
        if dotted in _NET_CALL_NAMES:
            call_src = ast.get_source_segment(source, sub) or ""
            if not _LOOPBACK_RE.search(call_src):
                return True
    return False


def _integration_marker_aliases(tree: ast.Module) -> Set[str]:
    """Module-level names bound to ``pytest.mark.integration``.

    A file may write ``requires_network = pytest.mark.integration`` and use it as
    a decorator; the alias must be recognised, or every test it marks looks
    unmarked.
    """
    aliases: Set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        src = ast.dump(node.value)
        if "integration" not in src:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                aliases.add(target.id)
    return aliases


def _function_is_integration(node: ast.AST, aliases: Set[str] = set()) -> bool:
    """Whether ``node`` carries an ``@pytest.mark.integration`` decorator."""
    for dec in getattr(node, "decorator_list", []):
        if isinstance(dec, ast.Name) and dec.id in aliases:
            return True
        if isinstance(dec, ast.Attribute) and dec.attr == "integration":
            return True
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
            if dec.func.attr == "integration":
                return True
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name):
            if dec.func.id in aliases:
                return True
    return False


def _module_is_integration(tree: ast.Module) -> bool:
    """Whether the whole module is ``pytestmark``-ed integration."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "pytestmark":
                    if "integration" in ast.dump(node.value):
                        return True
    return False


def _network_tests_in(path: Path) -> List[Tuple[str, int]]:
    """``(name, lineno)`` for every non-integration test that reaches the net.

    Follows module-level helpers: a test that calls ``_run`` inherits ``_run``'s
    network reach.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    if _module_is_integration(tree):
        return []

    cli_names = _module_network_cli_names(tree)
    aliases = _integration_marker_aliases(tree)

    # module-level function name -> node, for the call-graph walk.
    helpers: Dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            helpers[node.name] = node

    def reaches(node: ast.AST, seen: Set[str]) -> bool:
        if _segment_reaches_network(source, node, cli_names):
            return True
        for called in _called_names(node):
            if called in seen or called not in helpers:
                continue
            seen.add(called)
            if reaches(helpers[called], seen):
                return True
        return False

    offenders: List[Tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_") or _function_is_integration(node, aliases):
            continue
        if reaches(node, set()):
            offenders.append((node.name, node.lineno))
    return offenders


def _iter_test_files() -> List[Path]:
    """Every suite test file EXCEPT this guard's own file.

    This module plants network calls inside STRING literals to prove the scanner
    works; those strings are not real calls, and a scanner cannot tell a written
    sample from live code, so the guard excludes itself.
    """
    me = Path(__file__).resolve().name
    return [p for p in sorted(TESTS_DIR.glob("test_*.py")) if p.name != me]


def test_no_default_suite_test_reaches_the_network():
    """Every network-touching test must be marked ``integration``."""
    offenders: Dict[str, List[Tuple[str, int]]] = {}
    for path in _iter_test_files():
        hits = _network_tests_in(path)
        if hits:
            offenders[path.name] = hits

    assert not offenders, (
        "these tests run by default and appear to reach the network; mark them "
        f"@pytest.mark.integration: {offenders}"
    )


def test_the_guard_flags_a_network_touching_test(tmp_path):
    """Self-proof: the scanner detects a direct offender."""
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "import shutil\n"
        "import subprocess\n\n\n"
        "def test_installs_from_registry(tmp_path):\n"
        "    npm = shutil.which('npm')\n"
        "    subprocess.run([npm, 'ci'], cwd=tmp_path)\n",
        encoding="utf-8",
    )

    assert [name for name, _ in _network_tests_in(sample)] == [
        "test_installs_from_registry"
    ]


def test_the_guard_follows_a_module_level_helper(tmp_path):
    """Self-proof for the REAL shape: the CLI lives in a helper, not the test."""
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "import shutil\n"
        "import subprocess\n\n"
        "NPM = shutil.which('npm')\n\n\n"
        "def _run(workspace, *args):\n"
        "    return subprocess.run([NPM, *args], cwd=workspace)\n\n\n"
        "def test_builds(tmp_path):\n"
        "    _run(tmp_path, 'run', 'build')\n",
        encoding="utf-8",
    )

    assert [name for name, _ in _network_tests_in(sample)] == ["test_builds"]


def test_the_guard_accepts_an_integration_marked_test(tmp_path):
    """A network test IS allowed once marked ``integration``."""
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "import shutil\n"
        "import subprocess\n"
        "import pytest\n\n"
        "NPM = shutil.which('npm')\n\n\n"
        "def _run(workspace, *args):\n"
        "    return subprocess.run([NPM, *args], cwd=workspace)\n\n\n"
        "@pytest.mark.integration\n"
        "def test_builds(tmp_path):\n"
        "    _run(tmp_path, 'run', 'build')\n",
        encoding="utf-8",
    )

    assert _network_tests_in(sample) == []


def test_the_guard_does_not_flag_a_test_that_BLOCKS_the_network(tmp_path):
    """A test that monkeypatches the network away must NOT be flagged.

    ``monkeypatch.setattr(urllib.request, "urlopen", boom)`` is a call to
    ``setattr``, not to ``urlopen`` -- the AST check must tell them apart, or
    every offline-enforcing test would look like an offender.
    """
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "import urllib.request\n\n\n"
        "def test_touches_no_remote(monkeypatch):\n"
        "    def boom(*a, **k):\n"
        "        raise AssertionError('no HTTP')\n"
        "    monkeypatch.setattr(urllib.request, 'urlopen', boom)\n"
        "    assert boom\n",
        encoding="utf-8",
    )

    assert _network_tests_in(sample) == []


def test_the_guard_scans_the_full_suite():
    """The guard cannot pass by scanning nothing."""
    files = _iter_test_files()

    assert len(files) > 50, f"expected the full suite, saw {len(files)} files"
    assert "test_starter_toolchain.py" in {p.name for p in files}
