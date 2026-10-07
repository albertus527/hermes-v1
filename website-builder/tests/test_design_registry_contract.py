"""Batch D3a.5 Part B: the reviewed component dependency CONTRACT.

The catalog is a PROPOSAL, the registry response is UNTRUSTED, and the
application-owned reviewed contract is AUTHORITATIVE. These tests prove the
contract is enforced: a component is installable only when its live declared
dependencies match, exactly, what a human reviewed.

Properties under test:

    * SplitText's reviewed contract is ``gsap`` + ``gsap_react``, no nested
      registry dependencies (verified live at /r/SplitText-TS-TW)
    * exact expected dependencies => request allowed
    * missing dependency => refused
    * extra dependency => refused
    * unknown dependency => refused
    * unexpected registryDependency => refused
    * the upstream VERSION constraint is checked against the app pin, never
      installed
    * a component with no reviewed contract is not installable
    * the approved-identity table and the contract table cannot drift

No network, no subprocess.
"""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.core.design_registry as registry
from app.core.design_install import DEPENDENCY_PACKAGE_PINS
from app.core.design_registry import (
    REASON_COMPONENT_UNKNOWN,
    REASON_CONSTRAINT_UNSATISFIED,
    REASON_CONTRACT_MISSING,
    REASON_DEPENDENCY_CONTRACT_MISMATCH,
    REASON_DEPENDENCY_UNKNOWN,
    REASON_REGISTRY_DEPENDENCY_MISMATCH,
    SOURCE_REACT_BITS,
    SOURCE_TWENTY_FIRST,
    build_registry_request,
    dependency_specs_are_satisfied,
    reviewed_component_contract,
    resolve_reviewed_dependency_specs,
)

#: The LIVE registry response for the reviewed component, transcribed verbatim.
LIVE_SPLIT_TEXT_DEPS = ["gsap@^3.13.0", "@gsap/react@^2.1.2"]


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("the registry boundary must not execute anything")

    monkeypatch.setattr(subprocess, "Popen", deny)
    monkeypatch.setattr(subprocess, "run", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)


# ---------------------------------------------------------------------------
# The shipped contract matches the live registry
# ---------------------------------------------------------------------------


def test_split_text_has_a_reviewed_contract():
    contract = reviewed_component_contract(SOURCE_REACT_BITS, "SplitText")

    assert contract is not None
    assert set(contract.expected_dependency_ids) == {"gsap", "gsap_react"}
    assert contract.expected_registry_dependencies == ()


def test_an_unreviewed_component_has_no_contract():
    assert reviewed_component_contract(SOURCE_REACT_BITS, "BlurText") is None
    assert reviewed_component_contract(SOURCE_TWENTY_FIRST, "aurora-hero") is None


def test_the_approved_identity_table_is_derived_from_the_contracts():
    """Approved-identity and reviewed-contract cannot drift."""
    for source in registry.REGISTRY_SOURCES:
        approved = registry._APPROVED_COMPONENTS.get(source, frozenset())
        contracted = {
            component_id
            for (s, component_id) in registry._REVIEWED_COMPONENT_CONTRACTS
            if s == source
        }
        assert set(approved) == contracted, source


def test_every_reviewed_contract_names_allowlisted_dependency_ids():
    from app.core.design_install import DEPENDENCY_PACKAGES

    for contract in registry._REVIEWED_COMPONENT_CONTRACTS.values():
        for dependency_id in contract.expected_dependency_ids:
            assert dependency_id in DEPENDENCY_PACKAGES, dependency_id


# ---------------------------------------------------------------------------
# The contract gate: exact match, or refused
# ---------------------------------------------------------------------------


def test_the_live_declared_dependencies_are_accepted():
    outcome = build_registry_request(
        SOURCE_REACT_BITS,
        "SplitText",
        declared_dependencies=LIVE_SPLIT_TEXT_DEPS,
        declared_registry_dependencies=[],
    )

    assert outcome.ok is True
    assert outcome.request.required_dependency_ids == ("gsap", "gsap_react")


def test_a_missing_dependency_is_refused():
    """The exact bug: only ``gsap`` declared, ``@gsap/react`` silently lost."""
    outcome = build_registry_request(
        SOURCE_REACT_BITS, "SplitText", declared_dependencies=["gsap@^3.13.0"]
    )

    assert outcome.ok is False
    assert outcome.reason == REASON_DEPENDENCY_CONTRACT_MISMATCH
    assert outcome.request is None


def test_an_extra_dependency_is_refused():
    """Upstream adding a package must make the component non-installable."""
    outcome = build_registry_request(
        SOURCE_REACT_BITS,
        "SplitText",
        declared_dependencies=LIVE_SPLIT_TEXT_DEPS + ["some-new-package@^1.0.0"],
    )

    assert outcome.ok is False
    # The extra package is not allowlisted, so it is caught as UNKNOWN first --
    # either way the component is refused, which is the invariant.
    assert outcome.reason in (
        REASON_DEPENDENCY_UNKNOWN,
        REASON_DEPENDENCY_CONTRACT_MISMATCH,
    )


def test_an_unknown_dependency_is_refused():
    outcome = build_registry_request(
        SOURCE_REACT_BITS, "SplitText", declared_dependencies=["left-pad@^1.0.0"]
    )

    assert outcome.ok is False
    assert outcome.reason == REASON_DEPENDENCY_UNKNOWN


def test_an_unexpected_registry_dependency_is_refused():
    outcome = build_registry_request(
        SOURCE_REACT_BITS,
        "SplitText",
        declared_dependencies=LIVE_SPLIT_TEXT_DEPS,
        declared_registry_dependencies=["button"],
    )

    assert outcome.ok is False
    assert outcome.reason == REASON_REGISTRY_DEPENDENCY_MISMATCH


def test_a_component_with_no_contract_is_not_installable(monkeypatch):
    """Approving an identity without a contract does not make it installable."""
    monkeypatch.setattr(
        registry,
        "_APPROVED_COMPONENTS",
        {SOURCE_REACT_BITS: frozenset({"UnreviewedButApproved"})},
    )

    outcome = build_registry_request(SOURCE_REACT_BITS, "UnreviewedButApproved")

    assert outcome.ok is False
    assert outcome.reason == REASON_CONTRACT_MISSING


# ---------------------------------------------------------------------------
# The constructor binds the dependency set to the contract too
# ---------------------------------------------------------------------------
#
# The type is public. A caller that constructs a request DIRECTLY must not be
# able to widen the accepted dependency set beyond the reviewed contract -- that
# is the same class of bug, one layer down.


def test_a_direct_request_cannot_carry_a_superset_of_contract_dependencies():
    from app.core.design_registry import RegistryInstallRequest

    with pytest.raises(ValueError):
        RegistryInstallRequest(
            source=SOURCE_REACT_BITS,
            component_id="SplitText",
            registry_locator_id="https://reactbits.dev/r/SplitText-TS-TW",
            # gsap + gsap_react is the reviewed set; adding `three` widens it.
            required_dependency_ids=("gsap", "gsap_react", "three"),
        )


def test_a_direct_request_cannot_carry_a_subset_of_contract_dependencies():
    from app.core.design_registry import RegistryInstallRequest

    with pytest.raises(ValueError):
        RegistryInstallRequest(
            source=SOURCE_REACT_BITS,
            component_id="SplitText",
            registry_locator_id="https://reactbits.dev/r/SplitText-TS-TW",
            required_dependency_ids=("gsap",),
        )


def test_a_direct_request_matching_the_contract_is_accepted():
    from app.core.design_registry import RegistryInstallRequest

    request = RegistryInstallRequest(
        source=SOURCE_REACT_BITS,
        component_id="SplitText",
        registry_locator_id="https://reactbits.dev/r/SplitText-TS-TW",
        required_dependency_ids=("gsap", "gsap_react"),
    )

    assert request.required_dependency_ids == ("gsap", "gsap_react")


def test_a_direct_request_for_a_component_without_a_contract_is_refused():
    from app.core.design_registry import RegistryInstallRequest

    with pytest.raises(ValueError):
        RegistryInstallRequest(
            source=SOURCE_TWENTY_FIRST,
            component_id="aurora-hero",
            registry_locator_id="https://21st.dev/r/aurora-hero",
            required_dependency_ids=(),
        )


# ---------------------------------------------------------------------------
# The builtin allowlist and the reviewed-dependency table stay coherent
# ---------------------------------------------------------------------------


def test_every_allowed_builtin_has_a_reviewed_dependency_row():
    """A builtin with no reviewed row would install with an empty expected set,
    so the delta guard would refuse any package it legitimately introduces --
    and, worse, an unreviewed builtin could slip in silently. The two sets must
    be identical."""
    from app.core.design_install import (
        ALLOWED_SHADCN_COMPONENTS,
        REVIEWED_BUILTIN_COMPONENT_DEPENDENCIES,
    )

    assert set(ALLOWED_SHADCN_COMPONENTS) == set(
        REVIEWED_BUILTIN_COMPONENT_DEPENDENCIES
    )


def test_reviewed_builtin_packages_are_all_exact_pinned():
    from app.core.design_install import (
        REGISTRY_INTRODUCED_PACKAGE_PINS,
        REVIEWED_BUILTIN_COMPONENT_DEPENDENCIES,
    )

    for component, packages in REVIEWED_BUILTIN_COMPONENT_DEPENDENCIES.items():
        for package in packages:
            assert package in REGISTRY_INTRODUCED_PACKAGE_PINS, (component, package)


# ---------------------------------------------------------------------------
# The version constraint is CHECKED, never installed
# ---------------------------------------------------------------------------


def test_the_app_pin_satisfies_the_live_constraints():
    assert dependency_specs_are_satisfied(LIVE_SPLIT_TEXT_DEPS) is True


def test_a_constraint_the_pin_cannot_satisfy_is_refused():
    """A future upstream bump outside the pin's range refuses the component."""
    outcome = build_registry_request(
        SOURCE_REACT_BITS,
        "SplitText",
        declared_dependencies=["gsap@^4.0.0", "@gsap/react@^2.1.2"],
    )

    assert outcome.ok is False
    assert outcome.reason == REASON_CONSTRAINT_UNSATISFIED


def test_the_declared_constraint_is_never_forwarded_as_a_version():
    """Only the parsed identity and constraint leave the parser; the request
    carries dependency IDS, never a range."""
    outcome = build_registry_request(
        SOURCE_REACT_BITS,
        "SplitText",
        declared_dependencies=LIVE_SPLIT_TEXT_DEPS,
    )

    rendered = str(outcome.to_dict())
    assert "^" not in rendered, "no range may appear in a request"
    assert "3.13.0" not in rendered
    assert outcome.request.required_dependency_ids == ("gsap", "gsap_react")


def test_reviewed_dependency_specs_separate_identity_from_constraint():
    specs = resolve_reviewed_dependency_specs(LIVE_SPLIT_TEXT_DEPS)

    assert dict(specs) == {"gsap": "^3.13.0", "gsap_react": "^2.1.2"}
    # And the pins the app owns satisfy them.
    for dependency_id, constraint in specs:
        assert DEPENDENCY_PACKAGE_PINS[dependency_id]


# ---------------------------------------------------------------------------
# A raw URL still cannot become a component identity or argv
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "candidate",
    [
        "https://reactbits.dev/r/SplitText-TS-TW",
        "https://evil.example/r/x",
        "../../etc/passwd",
        "x; rm -rf /",
    ],
)
def test_a_raw_url_is_never_a_component_identity(candidate):
    outcome = build_registry_request(SOURCE_REACT_BITS, candidate)

    assert outcome.ok is False
    assert outcome.reason == REASON_COMPONENT_UNKNOWN
