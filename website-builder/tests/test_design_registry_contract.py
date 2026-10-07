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


def test_the_builtin_registry_packages_are_a_separate_application_owned_set():
    """The pinned official shadcn builtin registry dependencies are a SEPARATE
    application-owned set from the D2-selectable dependencies.

    Three sets that must never be conflated:

      A. DEPENDENCY_PACKAGES      -- D2-selectable ids a design decision may pick
      B. builtin registry writes  -- what the pinned shadcn CLI writes (cn, radix-ui)
      C. builtin registry imports -- what the CLI does NOT install (lucide-react)

    B ∪ C is exactly REGISTRY_INTRODUCED_PACKAGE_PINS (its own table, not A), and
    A shares no package NAME with B ∪ C. So a registry helper can never become
    D2-selectable, and a D2 dependency can never be silently treated as a
    registry-introduced helper.
    """
    from app.core.design_install import (
        DEPENDENCY_PACKAGES,
        REGISTRY_INTRODUCED_PACKAGE_PINS,
        REVIEWED_BUILTIN_COMPONENT_DEPENDENCIES,
        REVIEWED_BUILTIN_COMPONENT_IMPORTS,
    )

    d2_packages = set(DEPENDENCY_PACKAGES.values())

    cli_written = set()
    for packages in REVIEWED_BUILTIN_COMPONENT_DEPENDENCIES.values():
        cli_written.update(packages)
    source_imported = set()
    for packages in REVIEWED_BUILTIN_COMPONENT_IMPORTS.values():
        source_imported.update(packages)
    registry_introduced = cli_written | source_imported

    # B ∪ C is its own table, and equals exactly the registry-introduced set.
    assert registry_introduced == set(REGISTRY_INTRODUCED_PACKAGE_PINS)
    # No package NAME is shared between the D2 set and the registry set.
    assert d2_packages.isdisjoint(registry_introduced)
    # And the ids are distinct namespaces: a registry helper is not a D2 id.
    for helper in registry_introduced:
        assert helper not in DEPENDENCY_PACKAGES
    # Concretely today.
    assert cli_written == {"cn", "radix-ui"}
    assert source_imported == {"lucide-react"}
    assert d2_packages == {"gsap", "@gsap/react", "three", "lenis"}


def test_the_pinned_cli_introduces_exactly_two_direct_packages():
    """The application-owned allowlist for DIRECT packages the pinned shadcn CLI
    writes into package.json. Verified live against shadcn@4.21.0: every builtin
    writes only ``cn`` and/or ``radix-ui``. Pinning the EXACT union means a new
    upstream package cannot appear without a code change + re-verification."""
    from app.core.design_install import (
        REGISTRY_INTRODUCED_PACKAGE_PINS,
        REVIEWED_BUILTIN_COMPONENT_DEPENDENCIES,
    )

    union = set()
    for packages in REVIEWED_BUILTIN_COMPONENT_DEPENDENCIES.values():
        union.update(packages)

    assert union == {"cn", "radix-ui"}, union
    for package in union:
        assert REGISTRY_INTRODUCED_PACKAGE_PINS[package] in ("0.4.0", "1.7.0")


def test_the_registry_introduced_pin_table_is_closed_and_exact():
    """The closed set of every package a registry install may introduce --
    the CLI's own writes (cn, radix-ui) plus the source imports Hermes installs
    (lucide-react) -- each at an EXACT pin. A future entry must be reviewed and
    pinned, never a range or a floating tag."""
    from app.core.design_install import (
        REGISTRY_INTRODUCED_PACKAGE_PINS,
        REVIEWED_BUILTIN_COMPONENT_DEPENDENCIES,
        REVIEWED_BUILTIN_COMPONENT_IMPORTS,
        _EXACT_VERSION_RE,
    )

    assert set(REGISTRY_INTRODUCED_PACKAGE_PINS) == {
        "cn",
        "radix-ui",
        "lucide-react",
    }
    for package, version in REGISTRY_INTRODUCED_PACKAGE_PINS.items():
        assert _EXACT_VERSION_RE.match(version), (package, version)

    cli_written = set()
    for packages in REVIEWED_BUILTIN_COMPONENT_DEPENDENCIES.values():
        cli_written.update(packages)
    source_imported = set()
    for packages in REVIEWED_BUILTIN_COMPONENT_IMPORTS.values():
        source_imported.update(packages)

    # Every reviewed direct package is covered by the pin table.
    assert cli_written <= set(REGISTRY_INTRODUCED_PACKAGE_PINS)
    assert source_imported <= set(REGISTRY_INTRODUCED_PACKAGE_PINS)
    # And the pin table adds nothing beyond what is actually reviewed.
    assert set(REGISTRY_INTRODUCED_PACKAGE_PINS) == cli_written | source_imported


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


# ---------------------------------------------------------------------------
# The trusted registry boundary, as ONE structural property
# ---------------------------------------------------------------------------
#
# TRUSTED  (application-owned, in code): the registry sources, hosts, locator
#          templates, approved identities, reviewed contracts, the builtin
#          allowlist, the pinned CLI, and every exact pin.
# UNTRUSTED (the registry response): the materialized file, the declared
#          dependency fields, the ranges written into package.json, and the
#          emitted source's imports.
#
# The boundary is: EVERY untrusted value is compared against a trusted table,
# and nothing untrusted can reach argv or the manifest without that check.


def test_no_untrusted_identity_can_reach_argv():
    """A raw URL / path / shell fragment is never a component identity, so it
    can never produce argv."""
    from app.core.design_registry import build_registry_argv

    for hostile in [
        "https://evil.example/r/x",
        "https://reactbits.dev/r/SplitText-TS-TW",  # even the CORRECT url
        "//evil.example/r/x",
        "file:///etc/passwd",
        "../../etc/passwd",
        "x;rm -rf /",
        "x`id`",
    ]:
        outcome = build_registry_request(SOURCE_REACT_BITS, hostile)
        assert outcome.ok is False, hostile
        # No argv is produced (None request -> empty tuple): "unapproved =>
        # nothing runs" is structural, not a caller's if-check.
        assert not build_registry_argv(outcome.request), hostile


def test_a_trusted_identity_with_a_hostile_declaration_is_refused():
    """A reviewed identity does not make its DECLARED dependencies trusted."""
    for declared in (
        ["gsap", "npm:evil"],
        ["gsap", "https://evil/x.tgz"],
        ["gsap", "gsap@latest"],
        ["gsap", "gsap@file:../x"],
    ):
        outcome = build_registry_request(
            SOURCE_REACT_BITS, "SplitText", declared_dependencies=declared
        )
        assert outcome.ok is False, declared


def test_a_trusted_identity_with_an_unexpected_nested_dep_is_refused():
    for nested in (["evil-component"], ["https://evil/r/x"]):
        outcome = build_registry_request(
            SOURCE_REACT_BITS,
            "SplitText",
            declared_dependencies=["gsap", "@gsap/react"],
            declared_registry_dependencies=nested,
        )
        assert outcome.ok is False, nested


def test_the_trusted_tables_are_the_only_source_of_truth():
    """The trusted inputs are code literals; the untrusted response is only ever
    compared against them."""
    import app.core.design_registry as registry
    import app.core.design_install as install

    # trusted: closed, in code
    assert registry.REGISTRY_SOURCES == ("shadcn_builtin", "twenty_first", "react_bits")
    assert registry.REGISTRY_HOSTS == {"twenty_first": "21st.dev", "react_bits": "reactbits.dev"}
    assert set(registry._LOCATOR_TEMPLATES) == {"twenty_first", "react_bits"}
    assert install.ALLOWED_SHADCN_COMPONENTS
    assert install.PINNED_CLIS["shadcn"].is_exact()
    for version in install.REGISTRY_INTRODUCED_PACKAGE_PINS.values():
        assert install._EXACT_VERSION_RE.match(version)
    # the ONLY reviewed external contract today
    assert set(registry._REVIEWED_COMPONENT_CONTRACTS) == {("react_bits", "SplitText")}


# ---------------------------------------------------------------------------
# The trusted boundary as a TYPE (D3a idiom: fail-closed at construction)
# ---------------------------------------------------------------------------


def test_the_live_trusted_boundary_is_coherent():
    from app.core.design_registry import trusted_registry_boundary

    boundary = trusted_registry_boundary()

    assert boundary.sources == ("shadcn_builtin", "twenty_first", "react_bits")
    assert boundary.hosts == {"twenty_first": "21st.dev", "react_bits": "reactbits.dev"}
    assert boundary.approved_components == {
        "shadcn_builtin": (), "twenty_first": (), "react_bits": ("SplitText",),
    }
    assert boundary.introduced_packages() == ("cn", "lucide-react", "radix-ui")
    assert boundary.shadcn_cli_spec == "shadcn@4.21.0"
    assert len(boundary.builtin_components) == 16


def test_an_incoherent_trusted_boundary_cannot_be_constructed():
    """A drift in any trusted table fails at CONSTRUCTION, the D3a shape."""
    import dataclasses

    from app.core.design_registry import (
        SOURCE_REACT_BITS,
        TrustedRegistryBoundary,
        trusted_registry_boundary,
    )

    base = trusted_registry_boundary()

    def rebuild(**over):
        fields = {f.name: getattr(base, f.name) for f in dataclasses.fields(base)}
        fields.update(over)
        return TrustedRegistryBoundary(**fields)

    # a host for an unknown source
    with pytest.raises(ValueError):
        rebuild(hosts={**base.hosts, "evil": "evil.example"})
    # a non-builtin source with no host (remove its locator template too, so the
    # template/host coherence check cannot be the guard that fires instead)
    with pytest.raises(ValueError):
        rebuild(
            hosts={SOURCE_REACT_BITS: "reactbits.dev"},
            locator_templates={SOURCE_REACT_BITS: base.locator_templates[SOURCE_REACT_BITS]},
        )
    # a builtin dependency row removed
    with pytest.raises(ValueError):
        rebuild(builtin_packages={k: v for k, v in base.builtin_packages.items() if k != "button"})
    # a nested builtin outside the allowlist
    with pytest.raises(ValueError):
        rebuild(builtin_nested={"dialog": ("evil-widget",)})
    # a non-exact introduced pin
    with pytest.raises(ValueError):
        rebuild(introduced_pins={**base.introduced_pins, "cn": "^0.4.0"})
    # a package the builtins introduce with no pin
    with pytest.raises(ValueError):
        rebuild(introduced_pins={k: v for k, v in base.introduced_pins.items() if k != "lucide-react"})
    # a floating pinned CLI
    with pytest.raises(ValueError):
        rebuild(shadcn_cli_spec="shadcn@^4.21.0")


def test_the_boundary_serializes_without_untrusted_data():
    import json

    from app.core.design_registry import trusted_registry_boundary

    payload = json.dumps(trusted_registry_boundary().to_dict())

    assert "http" not in payload and "/r/" not in payload
    assert "token" not in payload and "secret" not in payload
