"""Batch D3a.5 Parts F/G: 21st.dev and React Bits catalog normalization.

Pure normalization tests over supplied payloads. **No network, no subprocess, no
install** -- the payload is handed in already fetched, which is exactly the split
that keeps capability resolution offline.

The properties under test are BEHAVIOUR CONTRACTS:

    * a real upstream entry normalizes to a citable, bounded identity
    * each source validates ids in ITS OWN vocabulary (slug vs PascalCase), so a
      React Bits component cannot be requested as a 21st component
    * declared dependency NAMES are untrusted: unknown ones make a component
      NOT installable and never widen the allowlist
    * a malformed or empty payload yields zero entries plus a static warning --
      never an invented component
    * upstream text is bounded before it is copied
    * truncation is reported, never silent
    * identity lookup is exact, never a prefix match
    * nothing from upstream reaches argv without the registry resolver's say-so
"""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_catalog import (
    MAX_FIELD_CHARS,
    SOURCE_HOSTS,
    WARNING_CATALOG_EMPTY,
    WARNING_CATALOG_MALFORMED,
    component_id_is_valid,
    find_catalog_entry,
    normalize_catalog,
    normalize_catalog_entry,
)
from app.core.design_install import DEPENDENCY_PACKAGES
from app.core.design_registry import (
    SOURCE_REACT_BITS,
    SOURCE_SHADCN_BUILTIN,
    SOURCE_TWENTY_FIRST,
    build_registry_request,
    resolve_registry_locator,
)

#: A payload shaped like what the 21st catalog returns.
TWENTY_FIRST_PAYLOAD = {
    "components": [
        {"id": "aurora-hero", "name": "Aurora Hero", "dependencies": ["gsap"]},
        {"id": "pricing-grid", "name": "Pricing Grid", "dependencies": []},
        {"id": "neon-marquee", "name": "Neon Marquee", "requires": "three, motion"},
    ]
}

#: A payload shaped like what the React Bits catalog returns.
REACT_BITS_PAYLOAD = {
    "components": [
        {"id": "SplitText", "name": "Split Text"},
        {"id": "BlurText", "name": "Blur Text", "dependencies": ["gsap"]},
        {"id": "CountUp", "name": "Count Up", "dependencies": [{"name": "lenis"}]},
    ]
}


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("catalog normalization must not reach out or execute")

    monkeypatch.setattr(subprocess, "Popen", deny)
    monkeypatch.setattr(subprocess, "run", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)


# ---------------------------------------------------------------------------
# Real entries normalize
# ---------------------------------------------------------------------------


def test_a_real_twenty_first_payload_normalizes():
    result = normalize_catalog(SOURCE_TWENTY_FIRST, TWENTY_FIRST_PAYLOAD)

    assert result.ok is True
    ids = [e.component_id for e in result.entries]
    assert ids == ["aurora-hero", "pricing-grid", "neon-marquee"]


def test_a_real_react_bits_payload_normalizes():
    result = normalize_catalog(SOURCE_REACT_BITS, REACT_BITS_PAYLOAD)

    assert result.ok is True
    assert [e.component_id for e in result.entries] == [
        "SplitText",
        "BlurText",
        "CountUp",
    ]


def test_an_entry_is_citable_by_source_and_identity():
    result = normalize_catalog(SOURCE_TWENTY_FIRST, TWENTY_FIRST_PAYLOAD)
    entry = result.entries[0]

    assert entry.identity == "twenty_first:aurora-hero"
    assert entry.provenance_host == SOURCE_HOSTS[SOURCE_TWENTY_FIRST]
    assert entry.to_dict()["provenance"]["path"] == "aurora-hero"


def test_provenance_names_the_canonical_host_only():
    for source in SOURCE_HOSTS:
        result = normalize_catalog(
            source,
            TWENTY_FIRST_PAYLOAD if source == SOURCE_TWENTY_FIRST else REACT_BITS_PAYLOAD,
        )
        for entry in result.entries:
            assert entry.provenance_host == SOURCE_HOSTS[source]


def test_dependency_objects_and_comma_strings_are_both_read():
    """Catalogs are inconsistent; both documented shapes must be handled."""
    result = normalize_catalog(SOURCE_TWENTY_FIRST, TWENTY_FIRST_PAYLOAD)

    marquee = find_catalog_entry(result, "neon-marquee")
    hero = find_catalog_entry(result, "aurora-hero")
    assert marquee.declared_dependency_ids == ("three",), "comma string"
    assert hero.declared_dependency_ids == ("gsap",), "list of strings"


def test_a_dependency_object_form_is_read():
    result = normalize_catalog(SOURCE_REACT_BITS, REACT_BITS_PAYLOAD)

    count_up = find_catalog_entry(result, "CountUp")
    assert count_up.declared_dependency_ids == ("lenis",)


# ---------------------------------------------------------------------------
# Per-source identity vocabularies
# ---------------------------------------------------------------------------


def test_each_source_validates_ids_in_its_own_vocabulary():
    """Accepting either form everywhere would cross-wire the two catalogs."""
    assert component_id_is_valid(SOURCE_TWENTY_FIRST, "aurora-hero") is True
    assert component_id_is_valid(SOURCE_TWENTY_FIRST, "AuroraHero") is False

    assert component_id_is_valid(SOURCE_REACT_BITS, "SplitText") is True
    assert component_id_is_valid(SOURCE_REACT_BITS, "split-text") is False


def test_a_cross_vocabulary_id_is_rejected():
    for source, wrong in (
        (SOURCE_TWENTY_FIRST, "SplitText"),
        (SOURCE_REACT_BITS, "aurora-hero"),
    ):
        assert component_id_is_valid(source, wrong) is False
        assert normalize_catalog_entry(source, {"id": wrong}) is None


def test_url_shaped_ids_are_rejected():
    for source in SOURCE_HOSTS:
        for candidate in (
            "https://evil.example/r/x",
            "../../etc/passwd",
            "x; rm -rf /",
            "",
        ):
            assert component_id_is_valid(source, candidate) is False, (source, candidate)


def test_an_unknown_source_is_refused():
    result = normalize_catalog("evil-registry", {"components": [{"id": "x"}]})

    assert result.entries == ()
    assert result.warnings


# ---------------------------------------------------------------------------
# Upstream metadata is untrusted
# ---------------------------------------------------------------------------


def test_an_unknown_dependency_makes_a_component_non_installable():
    """Refusal, not allowlist widening: upstream proposes, we decide."""
    result = normalize_catalog(
        SOURCE_REACT_BITS,
        {"components": [{"id": "SplitText", "dependencies": ["left-pad"]}]},
    )
    entry = result.entries[0]

    assert entry.dependencies_in_policy is False
    assert entry.installable is False
    assert entry.unknown_dependency_ids == ("left-pad",)
    assert result.installable_ids() == (), "no component is installable here"


def test_installable_ids_requires_BOTH_policy_and_a_reviewed_locator():
    """Installable means the application reviewed it, not merely "listed".

    A component with clean dependencies but NO approved locator is a proposal,
    not an installable thing -- the false positive this property must not repeat.
    """
    result = normalize_catalog(
        SOURCE_REACT_BITS,
        {
            "components": [
                # Reviewed: an approved locator exists -> installable.
                {"id": "SplitText"},
                # Listed upstream, clean deps, but NOT reviewed -> not installable.
                {"id": "BlurText"},
                # Unreviewed AND an out-of-policy dependency -> not installable.
                {"id": "CountUp", "dependencies": ["left-pad"]},
            ]
        },
    )

    assert result.installable_ids() == ("SplitText",)


def test_the_dependency_allowlist_is_never_widened():
    import app.core.design_catalog as catalog
    import app.core.design_registry as registry

    before = dict(registry.PACKAGE_TO_DEPENDENCY_ID)
    normalize_catalog(
        SOURCE_TWENTY_FIRST, {"components": [{"id": "x", "dependencies": ["left-pad"]}]}
    )

    assert registry.PACKAGE_TO_DEPENDENCY_ID == before
    assert set(catalog.PACKAGE_TO_DEPENDENCY_ID.values()) <= set(DEPENDENCY_PACKAGES)


def test_upstream_text_is_bounded():
    result = normalize_catalog(
        SOURCE_TWENTY_FIRST,
        {"components": [{"id": "big", "name": "x" * 5000}]},
    )

    assert len(result.entries[0].display_name) <= MAX_FIELD_CHARS


def test_a_missing_display_name_falls_back_to_the_identity():
    result = normalize_catalog(SOURCE_TWENTY_FIRST, {"components": [{"id": "anon"}]})

    assert result.entries[0].display_name == "anon"


# ---------------------------------------------------------------------------
# Nothing is invented
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        None,
        42,
        {"unexpected": "shape"},
        {"components": "not-a-list"},
        "not json at all",
    ],
)
def test_a_malformed_payload_yields_no_entries(payload):
    result = normalize_catalog(SOURCE_TWENTY_FIRST, payload)

    assert result.entries == ()
    assert result.warnings == (WARNING_CATALOG_MALFORMED,)


def test_an_empty_payload_yields_no_entries():
    result = normalize_catalog(SOURCE_TWENTY_FIRST, {"components": []})

    assert result.entries == ()
    assert result.warnings == (WARNING_CATALOG_EMPTY,)


def test_entries_without_a_valid_identity_are_dropped_not_invented():
    result = normalize_catalog(
        SOURCE_TWENTY_FIRST,
        {"components": [{"name": "no id here"}, "a string", {"id": "real-one"}]},
    )

    assert [e.component_id for e in result.entries] == ["real-one"]


def test_duplicate_ids_collapse():
    result = normalize_catalog(
        SOURCE_TWENTY_FIRST,
        {"components": [{"id": "same"}, {"id": "same"}]},
    )

    assert len(result.entries) == 1


# ---------------------------------------------------------------------------
# Bounds and determinism
# ---------------------------------------------------------------------------


def test_the_entry_limit_is_enforced_and_reported():
    payload = {"components": [{"id": f"item-{i}"} for i in range(200)]}

    result = normalize_catalog(SOURCE_TWENTY_FIRST, payload, limit=10)

    assert len(result.entries) == 10
    assert result.truncated is True, "truncation must be visible, not silent"


def test_an_untruncated_result_says_so():
    result = normalize_catalog(SOURCE_TWENTY_FIRST, TWENTY_FIRST_PAYLOAD)

    assert result.truncated is False


def test_a_json_string_payload_is_parsed():
    import json

    result = normalize_catalog(
        SOURCE_TWENTY_FIRST, json.dumps(TWENTY_FIRST_PAYLOAD)
    )

    assert result.ok is True
    assert len(result.entries) == 3


def test_normalization_is_deterministic():
    first = normalize_catalog(SOURCE_REACT_BITS, REACT_BITS_PAYLOAD)
    second = normalize_catalog(SOURCE_REACT_BITS, REACT_BITS_PAYLOAD)

    assert [e.to_dict() for e in first.entries] == [e.to_dict() for e in second.entries]


# ---------------------------------------------------------------------------
# Identity lookup is exact
# ---------------------------------------------------------------------------


def test_lookup_is_exact_not_a_prefix():
    """A request for `Split` must not install `SplitText`."""
    result = normalize_catalog(SOURCE_REACT_BITS, REACT_BITS_PAYLOAD)

    assert find_catalog_entry(result, "Split") is None
    assert find_catalog_entry(result, "SplitText") is not None


def test_lookup_rejects_non_string_input():
    result = normalize_catalog(SOURCE_REACT_BITS, REACT_BITS_PAYLOAD)

    assert find_catalog_entry(result, None) is None
    assert find_catalog_entry(result, 7) is None


# ---------------------------------------------------------------------------
# Nothing reaches argv without the registry resolver
# ---------------------------------------------------------------------------


def test_a_catalog_entry_alone_cannot_become_an_install_request():
    """Normalizing a component does not authorize installing it."""
    result = normalize_catalog(SOURCE_TWENTY_FIRST, TWENTY_FIRST_PAYLOAD)
    entry = result.entries[0]

    outcome = build_registry_request(entry.source, entry.component_id)

    assert outcome.ok is False, (
        "an unreviewed catalog component must still need an approved locator"
    )
    assert outcome.request is None


def test_an_approved_component_flows_through_the_registry_boundary():
    """The full path: catalog entry -> approved locator + contract -> typed request."""
    import app.core.design_registry as registry

    result = normalize_catalog(SOURCE_TWENTY_FIRST, TWENTY_FIRST_PAYLOAD)
    entry = result.entries[0]

    # Approval now requires an identity AND a reviewed contract. The catalog
    # entry declares no dependencies here, so the contract expects none.
    registry._APPROVED_COMPONENTS[SOURCE_TWENTY_FIRST] = frozenset(
        {entry.component_id}
    )
    registry._REVIEWED_COMPONENT_CONTRACTS[
        (SOURCE_TWENTY_FIRST, entry.component_id)
    ] = registry.ReviewedComponentContract(
        source=SOURCE_TWENTY_FIRST,
        component_id=entry.component_id,
        expected_dependency_ids=tuple(entry.declared_dependency_ids),
        expected_registry_dependencies=(),
    )
    try:
        outcome = build_registry_request(
            entry.source,
            entry.component_id,
            declared_dependencies=list(entry.declared_dependency_ids),
        )
        assert outcome.ok is True
        assert outcome.request.registry_locator_id.startswith("https://21st.dev/")

        # And the locator is the application's, not upstream's.
        assert resolve_registry_locator(
            entry.source, entry.component_id
        ) == outcome.request.registry_locator_id
    finally:
        registry._APPROVED_COMPONENTS[SOURCE_TWENTY_FIRST] = frozenset()
        registry._REVIEWED_COMPONENT_CONTRACTS.pop(
            (SOURCE_TWENTY_FIRST, entry.component_id), None
        )


def test_a_non_installable_component_is_refused_even_when_approved():
    """Approval does not excuse an out-of-policy dependency."""
    import app.core.design_registry as registry

    result = normalize_catalog(
        SOURCE_TWENTY_FIRST,
        {"components": [{"id": "risky", "dependencies": ["left-pad"]}]},
    )
    entry = result.entries[0]

    registry._APPROVED_COMPONENTS[SOURCE_TWENTY_FIRST] = frozenset({"risky"})
    try:
        outcome = build_registry_request(
            entry.source, entry.component_id, declared_dependencies=["left-pad"]
        )
        assert outcome.ok is False
        assert outcome.request is None
    finally:
        registry._APPROVED_COMPONENTS[SOURCE_TWENTY_FIRST] = frozenset()


def test_a_shadcn_builtin_source_is_not_a_catalog_source():
    assert SOURCE_SHADCN_BUILTIN not in SOURCE_HOSTS


# ---------------------------------------------------------------------------
# installable means REVIEWED, not merely listed (the false positive)
# ---------------------------------------------------------------------------
#
# A catalog entry is installable only when BOTH hold: its declared dependency
# set is entirely in policy AND the application has an APPROVED canonical
# locator for (source, component_id). Deriving installability from declared
# dependencies alone reported every listed component as installable while only
# the reviewed one actually was.


def test_a_listed_but_unreviewed_component_is_not_installable():
    """The exact false positive: clean deps, no approved locator."""
    result = normalize_catalog(
        SOURCE_REACT_BITS, {"components": [{"id": "BlurText"}]}
    )
    entry = result.entries[0]

    assert entry.dependencies_in_policy is True
    assert entry.has_approved_locator is False
    assert entry.installable is False
    assert result.installable_ids() == ()


def test_a_reviewed_component_with_clean_deps_is_installable():
    result = normalize_catalog(
        SOURCE_REACT_BITS, {"components": [{"id": "SplitText"}]}
    )
    entry = result.entries[0]

    assert entry.has_approved_locator is True
    assert entry.installable is True
    assert result.installable_ids() == ("SplitText",)


def test_a_reviewed_component_with_an_out_of_policy_dep_is_not_installable():
    """Review is necessary but not sufficient: dependencies must be in policy."""
    result = normalize_catalog(
        SOURCE_REACT_BITS,
        {"components": [{"id": "SplitText", "dependencies": ["left-pad"]}]},
    )
    entry = result.entries[0]

    assert entry.has_approved_locator is True
    assert entry.dependencies_in_policy is False
    assert entry.installable is False


def test_installable_never_disagrees_with_the_registry():
    """For every entry, .installable must equal what the registry would allow."""
    from app.core.design_registry import resolve_registry_locator

    for source, payload in (
        (SOURCE_REACT_BITS, {"components": [{"id": "SplitText"}, {"id": "BlurText"}]}),
        (SOURCE_TWENTY_FIRST, {"components": [{"id": "hero-section"}]}),
    ):
        for entry in normalize_catalog(source, payload).entries:
            expected = (
                not entry.unknown_dependency_ids
                and resolve_registry_locator(source, entry.component_id) is not None
            )
            assert entry.installable is expected, entry.component_id


def test_the_serialized_entry_reports_both_conditions():
    result = normalize_catalog(SOURCE_REACT_BITS, {"components": [{"id": "BlurText"}]})
    payload = result.entries[0].to_dict()

    assert payload["dependencies_in_policy"] is True
    assert payload["has_approved_locator"] is False
    assert payload["installable"] is False


# ---------------------------------------------------------------------------
# Reserved 21st route/highlight segments are NEVER component identities
# ---------------------------------------------------------------------------
#
# 21st's public index publishes CATEGORY pages (`/community/components/s/<tag>`)
# and HIGHLIGHT pages (`/community/components/popular|newest|featured|week`).
# Those segments are page ROUTES, not components. The false-positive parser
# turned exactly them into fabricated identities. They are now refused at the
# identity vocabulary itself, so no path can reintroduce them.

RESERVED_21ST_SEGMENTS = ("s", "popular", "newest", "featured", "week")


@pytest.mark.parametrize("segment", RESERVED_21ST_SEGMENTS)
def test_a_reserved_21st_route_segment_is_not_a_component_id(segment):
    assert component_id_is_valid(SOURCE_TWENTY_FIRST, segment) is False


@pytest.mark.parametrize("segment", RESERVED_21ST_SEGMENTS)
def test_a_reserved_segment_cannot_become_a_catalog_entry(segment):
    """Not just the parser: a JSON payload claiming one invents nothing."""
    result = normalize_catalog(
        SOURCE_TWENTY_FIRST, {"components": [{"id": segment}]}
    )

    assert result.entries == ()
    assert result.installable_ids() == ()


def test_a_reserved_segment_is_not_a_dependency_or_installable_thing():
    from app.core.design_registry import (
        build_registry_request,
        resolve_registry_locator,
    )

    for segment in RESERVED_21ST_SEGMENTS:
        assert resolve_registry_locator(SOURCE_TWENTY_FIRST, segment) is None
        assert build_registry_request(SOURCE_TWENTY_FIRST, segment).ok is False


def test_a_slug_shaped_non_reserved_id_is_still_valid():
    """The refusal is exactly the reserved set, not all short slugs."""
    for ok in ("aurora-hero", "hero", "pricing-section", "ai-chat"):
        assert component_id_is_valid(SOURCE_TWENTY_FIRST, ok) is True, ok


def test_the_reserved_set_is_source_scoped():
    """React Bits has no such routes, and PascalCase can't collide anyway."""
    for segment in RESERVED_21ST_SEGMENTS:
        assert component_id_is_valid(SOURCE_REACT_BITS, segment) is False  # not PascalCase
    assert component_id_is_valid(SOURCE_REACT_BITS, "SplitText") is True


def test_the_reserved_set_is_exactly_the_route_derived_segments():
    """Derive the reserved set from the DOCUMENTED route shapes.

    21st's index uses two route shapes that are NOT components:
      * ``/community/components/s/<tag>``  -> the ``s`` category prefix
      * ``/community/components/<highlight>`` -> terminal highlight segments
    The reserved table must equal exactly those, so it cannot silently drift.
    """
    import re

    from app.core.design_catalog import _RESERVED_COMPONENT_IDS

    routes = (
        "https://21st.dev/community/components/s/hero\n"
        "https://21st.dev/community/components/s/card\n"
        "https://21st.dev/community/components/popular\n"
        "https://21st.dev/community/components/newest\n"
        "https://21st.dev/community/components/featured\n"
        "https://21st.dev/community/components/week\n"
    )
    derived = set(re.findall(r"/community/components/([a-z0-9-]+)/", routes))   # prefix
    derived |= set(
        re.findall(r"/community/components/([a-z0-9-]+)(?=[\s]|$)", routes)     # terminal
    )
    assert _RESERVED_COMPONENT_IDS[SOURCE_TWENTY_FIRST] == frozenset(derived)


def test_no_route_derived_segment_is_installable():
    """Every route-derived segment resolves to no approved locator."""
    from app.core.design_registry import resolve_registry_locator

    for segment in RESERVED_21ST_SEGMENTS:
        assert resolve_registry_locator(SOURCE_TWENTY_FIRST, segment) is None
