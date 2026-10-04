"""Batch D3a.5 Part B: the generic pinned-CLI primitive.

Real temporary project directories. **No subprocess runs and no network call is
made** -- these tests are about the argv the application WOULD build.

The properties under test are BEHAVIOUR CONTRACTS:

    * every CLI in the table is EXACTLY pinned, with no range operator
    * the package/version come only from the closed table, never from a caller
    * an unknown cli_id produces NO argv at all
    * the three supported managers each get their REAL one-off mechanism
      (npm ``exec``, pnpm ``dlx``, yarn Berry ``dlx``)
    * yarn Classic and any unsupported manager fail closed
    * this is NOT a general installer: there is no API that accepts a package
      string from the caller

That last one is the security claim of this batch, so it is asserted two ways:
structurally (the signature takes a ``cli_id``, not a package), and
behaviourally (a package-shaped string produces ``None``).
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_install import (
    PINNED_CLIS,
    SHADCN_CLI_VERSION,
    YARN_BERRY_CONFIG,
    build_pinned_cli_prefix,
    pinned_cli,
    registry_invocation_prefix,
)

SHADCN = "shadcn"
TRANSITIONS = "transitions_dev"
IMPECCABLE = "impeccable"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail loudly if anything here reaches for a socket.

    A pinned CLI is resolved from a static table. If resolving one needed the
    network, this would be a registry lookup inside a build -- the exact failure
    the pin exists to prevent.
    """

    def deny(*args, **kwargs):
        raise AssertionError("network access is forbidden in pinned-CLI tests")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)


@pytest.fixture
def berry_project(tmp_path) -> Path:
    """A project that opts into Yarn Berry's one-off runner."""
    (tmp_path / YARN_BERRY_CONFIG).write_text("", encoding="utf-8")
    return tmp_path


@pytest.fixture
def classic_project(tmp_path) -> Path:
    """A Yarn Classic project: ``yarn.lock`` present, no Berry config."""
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# The table is closed and exactly pinned
# ---------------------------------------------------------------------------


def test_every_pinned_cli_is_exactly_versioned():
    """No range operator, no floating latest, in any row."""
    for cli_id, pinned in PINNED_CLIS.items():
        assert pinned.is_exact(), (cli_id, pinned.version)
        for forbidden in ("^", "~", ">", "<", "=", "*", "||", " ", "beta", "rc", "next"):
            assert forbidden not in pinned.version, (cli_id, pinned.version, forbidden)


def test_every_pinned_cli_has_a_package_and_a_binary():
    """Both are explicit; neither is derived from the other at call time."""
    for cli_id, pinned in PINNED_CLIS.items():
        assert pinned.package, cli_id
        assert pinned.binary, cli_id
        assert pinned.spec == f"{pinned.package}@{pinned.version}"


def test_shadcn_is_pinned_to_the_reviewed_version():
    """The registry CLI pin is unchanged by the refactor."""
    assert PINNED_CLIS[SHADCN].version == SHADCN_CLI_VERSION


def test_impeccable_is_deliberately_not_a_pinned_cli():
    """Its npm package is a binary-download shim; it must never reach here.

    Including it would mean a build could download an opaque platform
    executable into the user home, outside the project containment model.
    Impeccable is invoked from its provisioned profile skill instead.
    """
    assert IMPECCABLE not in PINNED_CLIS
    assert pinned_cli(IMPECCABLE) is None
    assert build_pinned_cli_prefix(("npm",), IMPECCABLE, Path(".")) is None


# ---------------------------------------------------------------------------
# The table is the allowlist
# ---------------------------------------------------------------------------


def test_an_unknown_cli_id_produces_no_argv():
    """The primitive is keyed by id, never by package name."""
    for candidate in (
        "totally-unknown",
        "left-pad",
        "shadcn@latest",
        "npm install evil",
        "../../etc/passwd",
        "",
    ):
        assert pinned_cli(candidate) is None, candidate
        assert build_pinned_cli_prefix(("npm",), candidate, Path(".")) is None, candidate


def test_the_resolver_never_echoes_its_input():
    """Returning the input would recreate arbitrary execution."""
    for candidate in ("evil", "npx", "shadcn"):
        if candidate not in PINNED_CLIS:
            assert pinned_cli(candidate) != candidate


def test_a_package_string_cannot_be_smuggled_through_the_id():
    """A caller naming a package directly gets nothing.

    This is the "not a general installer" claim asserted behaviourally: there
    is no path by which a package string becomes an executable spec.
    """
    assert build_pinned_cli_prefix(("npm",), "shadcn@4.21.0", Path(".")) is None
    assert build_pinned_cli_prefix(("npm",), "transitions-dev@0.3.0", Path(".")) is None


def test_a_non_string_cli_id_produces_no_argv():
    for candidate in (None, 123, b"shadcn", ["shadcn"]):
        assert pinned_cli(candidate) is None
        assert build_pinned_cli_prefix(("npm",), candidate, Path(".")) is None


# ---------------------------------------------------------------------------
# Per-manager argv
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "manager,expected_head",
    [
        (("npm",), ("npm", "exec", "--yes")),
        (("pnpm",), ("pnpm", "dlx")),
    ],
)
def test_supported_managers_get_their_real_one_off_runner(
    tmp_path, manager, expected_head
):
    prefix = build_pinned_cli_prefix(manager, SHADCN, tmp_path)

    assert prefix is not None
    assert prefix[: len(expected_head)] == expected_head


def test_npm_uses_a_separator_before_the_binary():
    """Without ``--`` npm would swallow the CLI's own switches."""
    prefix = build_pinned_cli_prefix(("npm",), SHADCN, Path("."))

    assert "--" in prefix
    separator = prefix.index("--")
    assert prefix[separator + 1] == SHADCN, "the binary must follow the separator"
    assert f"--package={PINNED_CLIS[SHADCN].spec}" in prefix


def test_yarn_berry_gets_dlx(berry_project):
    prefix = build_pinned_cli_prefix(("yarn",), SHADCN, berry_project)

    assert prefix == ("yarn", "dlx", PINNED_CLIS[SHADCN].spec)


def test_yarn_classic_fails_closed(classic_project):
    """No fallback to npx/npm: that would run a CLI the project never opted into."""
    assert build_pinned_cli_prefix(("yarn",), SHADCN, classic_project) is None


def test_an_unsupported_manager_fails_closed(tmp_path):
    for manager in (("bun",), ("npx",), ("node",), (), ("corepack",)):
        assert build_pinned_cli_prefix(manager, SHADCN, tmp_path) is None, manager


# ---------------------------------------------------------------------------
# The shadcn seam still works and is still pinned
# ---------------------------------------------------------------------------


def test_the_registry_seam_agrees_with_the_generic_primitive(tmp_path):
    """``registry_invocation_prefix`` is a named alias, not a second code path."""
    assert registry_invocation_prefix(
        ("npm",), project_root=tmp_path, version=SHADCN_CLI_VERSION
    ) == build_pinned_cli_prefix(("npm",), SHADCN, tmp_path)


def test_the_registry_seam_refuses_a_non_pinned_version(tmp_path):
    """A caller-supplied version must not reintroduce a floating pin."""
    for version in ("latest", "4.0.0", "", "^4.21.0"):
        assert registry_invocation_prefix(
            ("npm",), project_root=tmp_path, version=version
        ) is None, version


def test_transitions_is_pinned_and_reachable():
    """The second CLI is available through the same closed primitive.

    Asserted across both runner shapes, because npm carries the spec in a
    ``--package=`` switch while pnpm carries it positionally -- a test that only
    checked one manager would not prove the pin reaches every invocation form.
    """
    npm_prefix = build_pinned_cli_prefix(("npm",), TRANSITIONS, Path("."))
    pnpm_prefix = build_pinned_cli_prefix(("pnpm",), TRANSITIONS, Path("."))

    assert npm_prefix is not None and pnpm_prefix is not None
    # npm names the binary explicitly after the `--` separator, because the
    # spec is passed as a switch rather than positionally.
    assert f"--package={PINNED_CLIS[TRANSITIONS].spec}" in npm_prefix
    assert npm_prefix[-1] == PINNED_CLIS[TRANSITIONS].binary
    # pnpm takes the spec positionally and derives the binary from it.
    assert pnpm_prefix == ("pnpm", "dlx", PINNED_CLIS[TRANSITIONS].spec)


# ---------------------------------------------------------------------------
# No argv is ever a shell string
# ---------------------------------------------------------------------------


def test_every_prefix_is_a_flat_tuple_of_strings():
    """No shell metacharacters, no joining, no shell=True anywhere."""
    for manager in (("npm",), ("pnpm",)):
        for cli_id in PINNED_CLIS:
            prefix = build_pinned_cli_prefix(manager, cli_id, Path("."))
            assert isinstance(prefix, tuple)
            for part in prefix:
                assert isinstance(part, str), part
                assert not any(ch in part for ch in ";&|<>`$\n"), part
