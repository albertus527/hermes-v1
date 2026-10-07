"""Batch D3a.5 Part D: the typed registry install boundary.

Pure-function tests. **No subprocess, no network, no install** -- these are about
what the application is willing to turn into a command, which is the security
question this batch answers.

The properties under test are BEHAVIOUR CONTRACTS:

    * the shadcn builtin allowlist is UNCHANGED and never weakened
    * a raw external URL can never become an installable locator
    * a locator is produced only by the closed, application-owned table
    * an unreviewed component has no locator and is therefore not installable
    * upstream dependency NAMES are untrusted: an unknown one refuses the
      COMPONENT rather than widening the allowlist
    * a request cannot be constructed with a mismatched locator, a locator on a
      builtin, or a non-allowlisted dependency
    * the flow cannot be short-circuited from identity to argv
"""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_install import ALLOWED_SHADCN_COMPONENTS, DEPENDENCY_PACKAGES
from app.core.design_registry import (
    REGISTRY_HOSTS,
    REGISTRY_SOURCES,
    REASON_BUILTIN_CARRIES_DEPENDENCIES,
    REASON_COMPONENT_NOT_BUILTIN,
    REASON_COMPONENT_UNKNOWN,
    REASON_DEPENDENCY_UNKNOWN,
    REASON_SOURCE_UNKNOWN,
    SOURCE_REACT_BITS,
    SOURCE_SHADCN_BUILTIN,
    SOURCE_TWENTY_FIRST,
    RegistryInstallRequest,
    approved_registry_components,
    build_registry_argv,
    build_registry_request,
    component_id_is_well_formed,
    resolve_dependency_requirements,
    resolve_registry_locator,
)

#: A reviewed component, added here as a test-only approval so the suite can
#: exercise the approved path without the shipped table being pre-populated.
#: The shipped table starts EMPTY on purpose: an allowlist that mirrors the whole
#: upstream catalog stops being a review and becomes a copy.
APPROVED_21ST = "AuroraHero"
APPROVED_REACT_BITS = "SplitText"


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    """This layer decides intent; it never acts on it."""

    def deny(*args, **kwargs):
        raise AssertionError("the registry boundary must not execute anything")

    monkeypatch.setattr(subprocess, "Popen", deny)
    monkeypatch.setattr(subprocess, "run", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)


@pytest.fixture
def approved(monkeypatch):
    """Approve one component per external source for the duration of a test.

    The shipped table starts empty on purpose. Approving here -- via a mutation
    the monkeypatch fixture restores -- lets the suite exercise the approved
    path without the production allowlist being pre-populated with names no
    human has reviewed.

    An approved IDENTITY is no longer sufficient on its own: the reviewed
    dependency CONTRACT is also required, because a name allowlist cannot detect
    an added/removed dependency. So this fixture installs BOTH -- a matching
    contract whose expected dependencies are the ones the tests declare.
    """
    import app.core.design_registry as registry

    monkeypatch.setattr(
        registry,
        "_APPROVED_COMPONENTS",
        {
            SOURCE_TWENTY_FIRST: frozenset({APPROVED_21ST}),
            SOURCE_REACT_BITS: frozenset({APPROVED_REACT_BITS}),
        },
    )
    monkeypatch.setattr(
        registry,
        "_REVIEWED_COMPONENT_CONTRACTS",
        {
            (SOURCE_TWENTY_FIRST, APPROVED_21ST): registry.ReviewedComponentContract(
                source=SOURCE_TWENTY_FIRST,
                component_id=APPROVED_21ST,
                expected_dependency_ids=(),
                expected_registry_dependencies=(),
            ),
            (SOURCE_REACT_BITS, APPROVED_REACT_BITS): registry.ReviewedComponentContract(
                source=SOURCE_REACT_BITS,
                component_id=APPROVED_REACT_BITS,
                expected_dependency_ids=(),
                expected_registry_dependencies=(),
            ),
        },
    )
    return registry


# ---------------------------------------------------------------------------
# The builtin allowlist is untouched
# ---------------------------------------------------------------------------


def test_the_builtin_allowlist_is_unweakened():
    """shadcn built-ins remain exactly the reviewed primitive set.

    External components must never widen this: they route through
    ``RegistryInstallRequest`` instead, precisely so this tuple cannot grow.
    """
    assert "button" in ALLOWED_SHADCN_COMPONENTS
    assert "card" in ALLOWED_SHADCN_COMPONENTS
    assert APPROVED_21ST not in ALLOWED_SHADCN_COMPONENTS
    assert APPROVED_REACT_BITS not in ALLOWED_SHADCN_COMPONENTS


def test_a_builtin_request_needs_no_locator(approved):
    outcome = build_registry_request(SOURCE_SHADCN_BUILTIN, "button")

    assert outcome.ok is True
    assert outcome.request.is_builtin is True
    assert outcome.request.registry_locator_id == ""


def test_an_external_component_is_not_a_builtin(approved):
    outcome = build_registry_request(SOURCE_SHADCN_BUILTIN, APPROVED_21ST)

    assert outcome.ok is False
    assert outcome.reason == REASON_COMPONENT_NOT_BUILTIN


def test_a_builtin_cannot_carry_a_locator(approved):
    """A locator on the built-in path is the conflation this type prevents."""
    with pytest.raises(ValueError):
        RegistryInstallRequest(
            source=SOURCE_SHADCN_BUILTIN,
            component_id="button",
            registry_locator_id="https://evil.example/r/x",
        )


# ---------------------------------------------------------------------------
# A raw URL can never become a locator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "candidate",
    [
        "https://21st.dev/r/AuroraHero",
        "https://evil.example/r/AuroraHero",
        "//evil.example/r/x",
        "../../etc/passwd",
        "21st.dev/r/x",
        "x; rm -rf /",
        "x$(whoami)",
        "x`id`",
        "file:///etc/passwd",
    ],
)
def test_a_url_is_never_a_component_identity(candidate):
    """URL-shaped text fails the ID check, so it can never be substituted."""
    assert component_id_is_well_formed(candidate) is False
    assert (
        resolve_registry_locator(SOURCE_TWENTY_FIRST, candidate) is None
    ), candidate


def test_no_function_accepts_a_url(candidate_source=SOURCE_TWENTY_FIRST):
    """The resolver's signature takes an IDENTITY, never a locator."""
    outcome = build_registry_request(candidate_source, "https://evil.example/r/x")

    assert outcome.ok is False
    assert outcome.reason == REASON_COMPONENT_UNKNOWN


def test_an_unapproved_component_has_no_locator():
    """The default is no locator, which is what keeps review meaningful."""
    assert resolve_registry_locator(SOURCE_TWENTY_FIRST, APPROVED_21ST) is None
    assert approved_registry_components(SOURCE_TWENTY_FIRST) == ()


def test_an_approved_component_resolves_to_its_canonical_locator(approved):
    locator = resolve_registry_locator(SOURCE_TWENTY_FIRST, APPROVED_21ST)

    assert locator is not None
    assert locator.startswith(f"https://{REGISTRY_HOSTS[SOURCE_TWENTY_FIRST]}/")
    assert locator.endswith(APPROVED_21ST)


def test_a_locator_cannot_be_substituted_for_another_component(approved):
    """Mismatched locator/component pairs are rejected at construction."""
    outcome = build_registry_request(SOURCE_TWENTY_FIRST, APPROVED_21ST)

    with pytest.raises(ValueError):
        RegistryInstallRequest(
            source=SOURCE_TWENTY_FIRST,
            component_id=APPROVED_21ST,
            registry_locator_id="https://21st.dev/r/SomethingElse",
        )


def test_a_foreign_host_locator_is_rejected(approved):
    """Right shape, wrong host: still refused."""
    with pytest.raises(ValueError):
        RegistryInstallRequest(
            source=SOURCE_TWENTY_FIRST,
            component_id=APPROVED_21ST,
            registry_locator_id="https://evil.example/r/AuroraHero",
        )


def test_a_locator_must_belong_to_its_source(monkeypatch):
    """A template pointed at the wrong host is refused, not trusted.

    Without this, editing one entry of ``_LOCATOR_TEMPLATES`` (or a component id
    that happens to carry a host) would silently redirect an install to an
    arbitrary registry. The host check is the only thing standing between a
    reviewed source and an unreviewed destination, so it is asserted directly
    rather than only through the request path.
    """
    import app.core.design_registry as registry

    # A reviewed component whose template now points somewhere else.
    monkeypatch.setattr(
        registry,
        "_APPROVED_COMPONENTS",
        {SOURCE_TWENTY_FIRST: frozenset({APPROVED_21ST})},
    )
    monkeypatch.setattr(
        registry,
        "_LOCATOR_TEMPLATES",
        {SOURCE_TWENTY_FIRST: "https://evil.example/r/{component}"},
    )

    assert resolve_registry_locator(SOURCE_TWENTY_FIRST, APPROVED_21ST) is None


def test_a_template_that_appends_its_own_suffix_is_refused(monkeypatch):
    """The locator must end with exactly the component it was resolved for."""
    import app.core.design_registry as registry

    monkeypatch.setattr(
        registry,
        "_APPROVED_COMPONENTS",
        {SOURCE_TWENTY_FIRST: frozenset({APPROVED_21ST})},
    )
    monkeypatch.setattr(
        registry,
        "_LOCATOR_TEMPLATES",
        {SOURCE_TWENTY_FIRST: "https://21st.dev/r/{component}/something-else"},
    )

    assert resolve_registry_locator(SOURCE_TWENTY_FIRST, APPROVED_21ST) is None


def test_a_builtin_source_never_resolves_a_locator(monkeypatch, approved):
    """``shadcn_builtin`` components are named, never located.

    Even a component that IS approved in the external table must not resolve a
    locator under the builtin source: the two paths stay disjoint so an external
    URL can never enter the builtin flow.
    """
    assert resolve_registry_locator(SOURCE_SHADCN_BUILTIN, APPROVED_21ST) is None
    assert resolve_registry_locator(SOURCE_SHADCN_BUILTIN, "button") is None


def test_sources_are_closed():
    for source in REGISTRY_SOURCES:
        assert REGISTRY_HOSTS.get(source) or source == SOURCE_SHADCN_BUILTIN

    assert build_registry_request("evil-registry", "x").reason == REASON_SOURCE_UNKNOWN
    assert resolve_registry_locator("evil-registry", "x") is None
    assert resolve_registry_locator(None, "x") is None
    assert approved_registry_components("evil-registry") == ()


# ---------------------------------------------------------------------------
# Upstream dependency names are untrusted
# ---------------------------------------------------------------------------


def test_known_packages_map_to_dependency_ids():
    known, unknown = resolve_dependency_requirements(["gsap", "three"])

    assert unknown == ()
    assert set(known) == {"gsap", "three"}


def test_an_unknown_package_makes_the_component_non_installable(approved):
    """Refusal, not allowlist widening -- upstream proposes, we decide."""
    outcome = build_registry_request(
        SOURCE_TWENTY_FIRST, APPROVED_21ST, declared_dependencies=["left-pad"]
    )

    assert outcome.ok is False
    assert outcome.reason == REASON_DEPENDENCY_UNKNOWN
    assert outcome.request is None


def test_the_allowlist_is_never_extended_by_upstream_metadata(approved):
    import app.core.design_registry as registry

    before = dict(registry.PACKAGE_TO_DEPENDENCY_ID)
    build_registry_request(
        SOURCE_TWENTY_FIRST, APPROVED_21ST, declared_dependencies=["left-pad"]
    )

    assert registry.PACKAGE_TO_DEPENDENCY_ID == before


def test_a_request_cannot_carry_a_non_allowlisted_dependency(approved):
    with pytest.raises(ValueError):
        RegistryInstallRequest(
            source=SOURCE_TWENTY_FIRST,
            component_id=APPROVED_21ST,
            registry_locator_id=resolve_registry_locator(
                SOURCE_TWENTY_FIRST, APPROVED_21ST
            ),
            required_dependency_ids=("left-pad",),
        )


def test_a_builtin_request_cannot_carry_dependency_ids_at_all():
    """A builtin's direct dependencies are reviewed per component in
    ``REVIEWED_BUILTIN_COMPONENT_DEPENDENCIES`` (plus emitted-source imports);
    the request carries none. Accepting ids here would let a caller assert a
    dependency contract for a builtin the reviewed table never declared."""
    # Allowlisted ids are refused too: the shape itself is wrong for a builtin.
    for deps in (("gsap",), ("gsap", "three"), ("gsap_react",)):
        with pytest.raises(ValueError):
            RegistryInstallRequest(
                source=SOURCE_SHADCN_BUILTIN,
                component_id="button",
                registry_locator_id="",
                required_dependency_ids=deps,
            )
    # And the empty-tuple shape the application actually produces is accepted.
    request = RegistryInstallRequest(
        source=SOURCE_SHADCN_BUILTIN,
        component_id="button",
        registry_locator_id="",
        required_dependency_ids=(),
    )
    assert request.required_dependency_ids == ()
    assert request.is_builtin is True


def test_a_builtin_request_cannot_carry_a_non_allowlisted_dependency():
    """The allowlist check is load-bearing for the BUILTIN path.

    The reviewed-contract check applies only to external components, so a
    builtin is guarded by the allowlist membership check alone. This proves that
    guard still fires: a builtin request carrying an un-allowlisted id is
    refused even though no contract governs builtins.
    """
    with pytest.raises(ValueError):
        RegistryInstallRequest(
            source=SOURCE_SHADCN_BUILTIN,
            component_id="button",
            registry_locator_id="",
            required_dependency_ids=("left-pad",),
        )


def test_declared_dependencies_are_deduplicated_and_sorted():
    known, unknown = resolve_dependency_requirements(["three", "gsap", "three"])

    assert known == ("gsap", "three")
    assert unknown == ()


def test_a_non_string_dependency_is_treated_as_unknown():
    _, unknown = resolve_dependency_requirements([None, 7])

    assert unknown, "a malformed declaration must not be silently dropped"


def test_every_mapped_package_is_really_allowlisted():
    """The mapping cannot drift away from the real dependency allowlist."""
    from app.core.design_registry import PACKAGE_TO_DEPENDENCY_ID

    assert set(PACKAGE_TO_DEPENDENCY_ID.values()) <= set(DEPENDENCY_PACKAGES)
    assert set(PACKAGE_TO_DEPENDENCY_ID) == set(DEPENDENCY_PACKAGES.values())


# ---------------------------------------------------------------------------
# The flow cannot be short-circuited
# ---------------------------------------------------------------------------


def test_a_request_yields_components_only_after_approval(approved):
    outcome = build_registry_request(SOURCE_REACT_BITS, APPROVED_REACT_BITS)

    assert outcome.ok is True
    argv = build_registry_argv(outcome.request)
    assert argv, "an approved external component must produce a component argument"
    assert argv[0].startswith(f"https://{REGISTRY_HOSTS[SOURCE_REACT_BITS]}/")


def test_a_builtin_yields_no_locator_argument(approved):
    """Built-ins are named, not located -- the two paths stay distinct."""
    outcome = build_registry_request(SOURCE_SHADCN_BUILTIN, "card")

    assert build_registry_argv(outcome.request) == ()


def test_an_unapproved_component_yields_no_request_at_all():
    outcome = build_registry_request(SOURCE_TWENTY_FIRST, "NeverReviewed")

    assert outcome.request is None
    assert build_registry_argv(outcome.request) == ()


def test_the_outcome_is_serializable_without_a_credential(approved):
    import json

    outcome = build_registry_request(SOURCE_TWENTY_FIRST, APPROVED_21ST)
    rendered = json.dumps(outcome.to_dict())

    assert APPROVED_21ST in rendered


def test_no_credential_or_path_appears_in_a_request(approved):
    import json

    outcome = build_registry_request(
        SOURCE_TWENTY_FIRST, APPROVED_21ST, declared_dependencies=["gsap"]
    )
    rendered = json.dumps(outcome.to_dict())

    for forbidden in ("/home/", "C:\\", "token", "secret", "api_key"):
        assert forbidden not in rendered.lower(), forbidden


# ---------------------------------------------------------------------------
# Builtins are NOT subject to React Bits' (or any external) reviewed contract
# ---------------------------------------------------------------------------


def test_a_builtin_carrying_declared_dependencies_is_refused_not_crashed():
    """A builtin's direct dependencies are reviewed PER COMPONENT, never declared
    by a caller. Supplying them must be a bounded refusal, not an uncaught
    ValueError from the request constructor."""
    outcome = build_registry_request(
        SOURCE_SHADCN_BUILTIN,
        "button",
        declared_dependencies=["gsap", "@gsap/react"],
    )

    assert outcome.ok is False
    assert outcome.request is None
    assert outcome.reason == REASON_BUILTIN_CARRIES_DEPENDENCIES


def test_a_clean_builtin_request_carries_no_dependencies():
    outcome = build_registry_request(SOURCE_SHADCN_BUILTIN, "dialog")

    assert outcome.ok is True
    assert outcome.request.required_dependency_ids == ()
    assert outcome.request.is_builtin is True
    assert outcome.dependency_ids == ()


def test_no_builtin_is_keyed_into_the_external_contract_table():
    """The React Bits reviewed contract must never cover a shadcn builtin."""
    import app.core.design_install as install
    import app.core.design_registry as registry

    contracted = {component for (_source, component) in registry._REVIEWED_COMPONENT_CONTRACTS}
    assert contracted.isdisjoint(set(install.ALLOWED_SHADCN_COMPONENTS))
    # And a builtin has no reviewed external contract, by source or identity.
    for component in install.ALLOWED_SHADCN_COMPONENTS:
        assert registry.reviewed_component_contract(SOURCE_REACT_BITS, component) is None
        assert registry.reviewed_component_contract(SOURCE_SHADCN_BUILTIN, component) is None


def test_a_builtin_never_resolves_to_a_react_bits_locator():
    for component in ("button", "dialog", "card"):
        assert resolve_registry_locator(SOURCE_REACT_BITS, component) is None
