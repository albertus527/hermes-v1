"""Batch D3a.5 Part F: the bounded npm package-spec parser.

Pure-function tests. No network, no subprocess, no install. This parser is the
narrow contract that lets a registry's ``gsap@^3.13.0`` be understood WITHOUT
letting arbitrary npm specs become installable.

The properties under test are BEHAVIOUR CONTRACTS:

    * the four approved grammar shapes parse correctly
    * the package identity and the declared constraint are separated
    * hostile / unsupported specs FAIL CLOSED (aliases, git/ssh/http URLs,
      file:/link:/workspace:, dist-tags, wildcards, malformed scoped specs)
    * the raw rejected input is never echoed
    * the declared constraint is a CONSTRAINT, checked against the app pin --
      never forwarded to a package manager
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_npm_spec import (
    REJECTED_SPEC_LABEL,
    NpmPackageSpec,
    constraint_is_satisfied,
    parse_npm_package_spec,
)

# ---------------------------------------------------------------------------
# The approved grammar parses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec, expected_name, expected_constraint",
    [
        ("gsap", "gsap", ""),
        ("gsap@^3.13.0", "gsap", "^3.13.0"),
        ("gsap@3.15.0", "gsap", "3.15.0"),
        ("@gsap/react", "@gsap/react", ""),
        ("@gsap/react@^2.1.2", "@gsap/react", "^2.1.2"),
        ("@types/three@0.186.0", "@types/three", "0.186.0"),
        ("lenis@~1.3.0", "lenis", "~1.3.0"),
        ("three@>=0.186.0", "three", ">=0.186.0"),
    ],
)
def test_the_approved_grammar_parses(spec, expected_name, expected_constraint):
    parsed = parse_npm_package_spec(spec)

    assert parsed is not None, spec
    assert parsed.package_name == expected_name
    assert parsed.declared_constraint == expected_constraint


def test_a_bare_scoped_name_keeps_its_scope_sigil():
    """The scope ``@`` is not the version separator."""
    parsed = parse_npm_package_spec("@gsap/react")

    assert parsed is not None
    assert parsed.package_name == "@gsap/react"
    assert parsed.is_bare is True


def test_a_versioned_scoped_name_splits_at_the_second_at():
    parsed = parse_npm_package_spec("@gsap/react@^2.1.2")

    assert parsed.package_name == "@gsap/react"
    assert parsed.declared_constraint == "^2.1.2"


def test_the_result_is_serializable_without_the_raw_spec():
    parsed = parse_npm_package_spec("gsap@^3.13.0")

    payload = parsed.to_dict()
    assert payload == {"package_name": "gsap", "declared_constraint": "^3.13.0"}


# ---------------------------------------------------------------------------
# Hostile / unsupported specs FAIL CLOSED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        # dist-tags / wildcards
        "foo@latest",
        "foo@next",
        "foo@*",
        "foo@x",
        "gsap@",
        "@gsap/react@",
        # aliases and protocols
        "npm:other-package",
        "foo@npm:bar",
        "foo@github:user/repo",
        "foo@https://evil.example/x",
        "foo@http://evil.example/x",
        "foo@git+https://evil.example/x.git",
        "foo@git+ssh://git@evil.example/x.git",
        "foo@file:../x",
        "foo@file:/etc/passwd",
        "foo@link:../x",
        "foo@workspace:*",
        "@scope/pkg@file:../x",
        # malformed scoped specs
        "@scope/@1.2.3",
        "@",
        "@/x",
        "@scope",
        "@scope/",
        # path / traversal / shell fragments
        "../../etc/passwd",
        "x; rm -rf /",
        "x$(whoami)",
        "x`id`",
        "gsap three",
        "",
        "   ",
        # non-strings
        None,
        7,
        ["gsap"],
        {"name": "gsap"},
    ],
)
def test_unsupported_and_hostile_specs_are_refused(spec):
    assert parse_npm_package_spec(spec) is None, spec


def test_a_malformed_constraint_is_refused():
    for spec in ("gsap@^", "gsap@>>1", "gsap@1.2.3.4.5", "gsap@^abc", "gsap@1.x"):
        assert parse_npm_package_spec(spec) is None, spec


def test_the_raw_rejected_input_is_never_echoed():
    """A rejected spec must not leak into a bounded label."""
    assert "evil" not in REJECTED_SPEC_LABEL
    assert "://" not in REJECTED_SPEC_LABEL
    assert "latest" not in REJECTED_SPEC_LABEL


# ---------------------------------------------------------------------------
# The declared constraint is CHECKED, never installed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "constraint, version, expected",
    [
        ("^3.13.0", "3.15.0", True),
        ("^3.13.0", "3.13.0", True),
        ("^3.13.0", "4.0.0", False),
        ("^3.13.0", "3.12.9", False),
        ("^2.1.2", "2.1.2", True),
        ("^2.1.2", "3.0.0", False),
        ("~1.3.0", "1.3.26", True),
        ("~1.3.0", "1.4.0", False),
        ("3.15.0", "3.15.0", True),
        ("3.15.0", "3.15.1", False),
        (">=0.186.0", "0.186.1", True),
        (">=0.186.0 <1.0.0", "0.186.1", True),
        (">=0.186.0 <1.0.0", "1.0.0", False),
        ("^0.4.0", "0.4.0", True),
        ("^0.4.0", "0.5.0", False),
    ],
)
def test_the_pin_is_checked_against_the_declared_constraint(constraint, version, expected):
    assert constraint_is_satisfied(constraint, version) is expected


def test_a_malformed_constraint_is_not_satisfied():
    """A parse gap fails closed -- never a false pass."""
    assert constraint_is_satisfied("latest", "3.15.0") is False
    assert constraint_is_satisfied("", "3.15.0") is False
    assert constraint_is_satisfied("^3.13.0", "not-a-version") is False


def test_a_caret_zero_zero_range_is_bounded_correctly():
    """^0.0.x pins the patch, per semver."""
    assert constraint_is_satisfied("^0.0.3", "0.0.3") is True
    assert constraint_is_satisfied("^0.0.3", "0.0.4") is False
