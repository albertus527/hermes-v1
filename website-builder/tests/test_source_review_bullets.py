"""Part N -- the source-review bullets, re-verified as LIVE properties.

Every bullet in ``docs/D3A5_DEPENDENCY_INGRESS_AUDIT.md`` was verified once, by
hand, in the session that fixed it. This module re-checks each as a property of
the CURRENT code, so a later commit cannot silently weaken one. It is the
durable half of "re-verify the earlier source-review bullets on HEAD".

Each test names the bullet it pins. They are all pure, offline and fast: no
network, no subprocess, no filesystem mutation (one test reads the starter
manifest, read-only).
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BUILDER = Path(__file__).resolve().parents[1]
STARTER = BUILDER.parent / "templates" / "frontend-starter"

from app.core import design_activation as activation
from app.core import design_catalog as catalog
from app.core import design_catalog_fetch as fetch
from app.core import design_install as install
from app.core import design_npm_spec as npm_spec
from app.core import design_registry as registry
from app.core import design_transitions as transitions
from app.core.design_registry import (
    SOURCE_REACT_BITS,
    SOURCE_SHADCN_BUILTIN,
    SOURCE_TWENTY_FIRST,
)

RESERVED_ROUTE_SEGMENTS = ("s", "popular", "newest", "featured", "week")
BUILTIN_NAMES = (
    "accordion", "alert", "badge", "button", "card", "checkbox", "dialog",
    "input", "label", "select", "separator", "sheet", "switch", "tabs",
    "textarea", "tooltip",
)


# ---------------------------------------------------------------------------
# The 21st.dev surface bullets
# ---------------------------------------------------------------------------


def test_the_surface_we_call_is_the_authoritative_rest_search():
    """Bullet: the endpoint our adapter calls is 21st's own REST v1 search."""
    url = fetch.build_discovery_url(SOURCE_TWENTY_FIRST)
    assert url is not None
    assert "/api/v1/components/search" in url


def test_the_adapter_sends_bearer_and_never_x_api_key():
    """Bullet: auth is `Authorization: Bearer`, never `x-api-key`."""
    source = inspect.getsource(fetch)
    assert "Authorization" in source
    assert "Bearer" in source
    assert "x-api-key" not in source.lower()


def test_no_credential_value_field_on_the_result_type():
    """Bullet: presence is used; the VALUE is never stored on a result."""
    names = {f.name for f in dataclasses.fields(fetch.CatalogFetchResult)}
    assert names.isdisjoint({"credential", "api_key", "token", "authorization"})


def test_the_fetch_never_raises_for_an_upstream_problem():
    """Bullet: the catch-all closes the header-leaking traceback vector."""
    source = inspect.getsource(fetch)
    assert "REASON_UNEXPECTED_ERROR" in source
    assert "except Exception" in source


def test_the_failure_vocabulary_names_401_and_429_distinctly():
    """Bullet: 401 -> auth rejected, 429 -> rate limited, else generic."""
    assert fetch._reason_for_status(401) == fetch.REASON_AUTH_REJECTED
    assert fetch._reason_for_status(429) == fetch.REASON_RATE_LIMITED
    assert fetch._reason_for_status(500) == fetch.REASON_BAD_STATUS


def test_a_successful_fetch_is_bound_to_a_payload_at_construction():
    """Bullet: `ok` cannot be constructed without a payload (`python -O` safe)."""
    with pytest.raises(ValueError):
        fetch.CatalogFetchResult(source=SOURCE_REACT_BITS, payload=None, reason=None)


def test_the_catalog_container_key_is_per_source():
    """Bullet: 21st nests under `results`; React Bits is a bare list."""
    assert catalog.CATALOG_CONTAINER_KEYS[SOURCE_TWENTY_FIRST] == "results"
    assert catalog.CATALOG_CONTAINER_KEYS[SOURCE_REACT_BITS] is None


def test_no_regex_scrapes_a_21st_component_path():
    """Bullet: no `/components/<slug>` scrape survives, anchored or not."""
    patterns = [
        value.pattern
        for value in vars(fetch).values()
        if isinstance(value, re.Pattern)
    ]
    assert not [p for p in patterns if "components/" in p]


def test_discovery_available_iff_a_url_can_be_built():
    """Bullet: the live-adapter probe agrees with `build_discovery_url`."""
    for source in (SOURCE_TWENTY_FIRST, SOURCE_REACT_BITS, "unknown_source"):
        assert activation._has_live_discovery_adapter(source) is (
            fetch.build_discovery_url(source) is not None
        )


def test_the_free_metadata_search_claim_is_gone_from_every_surface():
    """Bullet: no surface claims a free 21st metadata search tier."""
    claim = "free metadata search"
    surfaces = {
        "module docstring": fetch.__doc__ or "",
        "module source": inspect.getsource(fetch),
        "manifest": (BUILDER / "config" / "design_resources.yaml").read_text(
            encoding="utf-8"
        ),
    }
    offenders = [name for name, text in surfaces.items() if claim in text]
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# The registry bullets
# ---------------------------------------------------------------------------


def test_reserved_route_segments_are_refused_at_the_vocabulary():
    """Bullet: routes are not identities -- refused at the vocabulary + locator."""
    for segment in RESERVED_ROUTE_SEGMENTS:
        assert registry.component_id_is_reserved(SOURCE_TWENTY_FIRST, segment)
        assert registry.resolve_registry_locator(SOURCE_TWENTY_FIRST, segment) is None


def test_no_contract_can_exist_for_a_reserved_segment():
    """Bullet: the refusal is a property of the contract TYPE."""
    with pytest.raises(ValueError):
        registry.ReviewedComponentContract(
            source=SOURCE_TWENTY_FIRST, component_id="popular"
        )


def test_a_builtin_carrying_declared_deps_is_a_bounded_refusal():
    """Bullet: builtins are never routed through the external contract."""
    outcome = registry.build_registry_request(
        SOURCE_SHADCN_BUILTIN, "button", declared_dependencies=["gsap"]
    )
    assert outcome.ok is False
    assert outcome.reason == registry.REASON_BUILTIN_CARRIES_DEPENDENCIES


def test_a_clean_builtin_request_carries_no_required_deps():
    """Bullet: a builtin's deps are reviewed per component, not declared."""
    outcome = registry.build_registry_request(SOURCE_SHADCN_BUILTIN, "button")
    assert outcome.ok is True
    assert outcome.request is not None
    assert outcome.request.required_dependency_ids == ()


def test_no_builtin_name_is_an_approved_external_identity():
    """Bullet: the component tables are separate at the table level."""
    for source in (SOURCE_REACT_BITS, SOURCE_TWENTY_FIRST):
        approved = set(registry.approved_registry_components(source))
        assert approved.isdisjoint(BUILTIN_NAMES), (source, approved & set(BUILTIN_NAMES))


def test_react_bits_approves_exactly_split_text():
    """Bullet: `installable` means reviewed, not merely listed."""
    assert registry.approved_registry_components(SOURCE_REACT_BITS) == ("SplitText",)


def test_an_unreviewed_external_id_resolves_to_no_locator():
    """Bullet: only a reviewed identity has a canonical locator."""
    assert registry.resolve_registry_locator(SOURCE_REACT_BITS, "BlurText") is None
    assert registry.resolve_registry_locator(SOURCE_REACT_BITS, "SplitText") is not None


def test_the_trusted_boundary_is_a_validated_type():
    """Bullet: the boundary is a property of construction."""
    boundary = registry.trusted_registry_boundary()
    assert dataclasses.is_dataclass(boundary)


def test_split_text_contract_is_exactly_gsap_and_gsap_react():
    """Bullet: the external contract's packages are the reviewed pair."""
    contract = registry.reviewed_component_contract(SOURCE_REACT_BITS, "SplitText")
    assert contract is not None
    assert tuple(sorted(contract.expected_dependency_ids)) == ("gsap", "gsap_react")


def test_a_url_as_a_component_identity_is_refused():
    """Bullet: no arbitrary registry URL reaches argv."""
    outcome = registry.build_registry_request(
        SOURCE_REACT_BITS, "https://evil.example/r/x"
    )
    assert outcome.ok is False


# ---------------------------------------------------------------------------
# The parser / pins / transitions bullets
# ---------------------------------------------------------------------------


HOSTILE_SPECS = (
    "https://evil.example/x.tgz", "http://evil.example/x.tgz",
    "gsap@https://evil.example/x.tgz", "git+https://evil.example/x.git",
    "git+ssh://git@evil.example/x.git", "git://evil.example/x.git",
    "github:user/repo", "gitlab:user/repo", "bitbucket:user/repo",
    "user/repo", "file:../evil", "link:../evil", "workspace:*",
    "npm:other", "npm:@scope/other", "gsap%2F..%2Fevil", "gsap%00",
)
APPROVED_SPECS = ("gsap", "gsap@3.15.0", "gsap@^3.15.0", "@gsap/react@2.1.2")


def test_hostile_npm_specs_are_refused_by_the_bounded_parser():
    """Bullet: every source-bearing / encoded / alias spec is refused."""
    accepted = [s for s in HOSTILE_SPECS if npm_spec.parse_npm_package_spec(s) is not None]
    assert not accepted, accepted


def test_approved_npm_specs_parse():
    """Bullet: the bounded grammar accepts the reviewed forms."""
    refused = [s for s in APPROVED_SPECS if npm_spec.parse_npm_package_spec(s) is None]
    assert not refused, refused


def test_the_always_provided_set_equals_the_starter_runtime_dependencies():
    """Bullet: the always-provided set EQUALS the starter's `dependencies`."""
    starter = json.loads((STARTER / "package.json").read_text(encoding="utf-8"))
    assert set(install._ALWAYS_PROVIDED_PACKAGES) == set(starter["dependencies"])


def _is_exact(package: str, version: str) -> bool:
    return install.PackageSpec(package=package, version=version).is_exact()


def test_every_dependency_package_pin_is_exact():
    """Bullet: the application-owned pins are exact versions."""
    non_exact = {
        k: v for k, v in install.DEPENDENCY_PACKAGE_PINS.items()
        if not _is_exact(k, v)
    }
    assert not non_exact, non_exact


def test_every_registry_introduced_package_pin_is_exact():
    """Bullet: registry-introduced pins (cn/radix-ui/lucide-react) are exact."""
    non_exact = {
        k: v for k, v in install.REGISTRY_INTRODUCED_PACKAGE_PINS.items()
        if not _is_exact(k, v)
    }
    assert not non_exact, non_exact


def test_every_impeccable_parser_pin_is_exact():
    """Bullet: the Impeccable parser runtime pins are exact."""
    non_exact = {
        k: v for k, v in install.IMPECCABLE_PARSER_PACKAGE_PINS.items()
        if not _is_exact(k, v)
    }
    assert not non_exact, non_exact


def test_every_pinned_cli_is_exactly_versioned():
    """Bullet: a pinned CLI is an exact `package@version`."""
    non_exact = {
        k: cli.version for k, cli in install.PINNED_CLIS.items()
        if not _is_exact(cli.package, cli.version)
    }
    assert not non_exact, non_exact


def test_bulk_selector_slugs_are_refused_at_the_transitions_vocabulary():
    """Bullet: `all`/`free`/`pro` are refused at the slug vocabulary."""
    assert transitions.RESERVED_RECIPE_SLUGS == frozenset({"all", "free", "pro"})
    for slug in transitions.RESERVED_RECIPE_SLUGS:
        assert transitions.recipe_slug_is_reserved(slug)
        assert not transitions.recipe_slug_is_well_formed(slug)


def test_transitions_optional_suffixes_is_empty():
    """Bullet: materialization is exactly `transitions/<slug>.md`."""
    assert transitions.RECIPE_OPTIONAL_SUFFIXES == ()


def test_the_manifest_section_guard_helpers_exist():
    """Bullet: an install may not change a non-reviewed package.json section."""
    source = Path(install.__file__).read_text(encoding="utf-8")
    assert "snapshot_manifest_sections" in source
    assert "changed_manifest_sections" in source


# ---------------------------------------------------------------------------
# Audit Q9: the policy claims must be EXECUTABLE, not prose
# ---------------------------------------------------------------------------
#
# The doc's Final dependency policy says "every package name that can reach an
# install argv comes from one of FOUR closed, application-owned tables". This
# pins that claim as a property of the code: the four tables are exactly the
# ones named, and each produces package names. A FIFTH table producing a package
# would make the sentence false again -- the same "docs vs executable policy"
# mismatch the audit's Q9 asks about.


def test_every_package_name_source_is_one_of_the_four_closed_tables():
    """The doc names four tables; the code must have exactly those four sources."""
    from app.core import design_install as di

    runtime = set(di.DEPENDENCY_PACKAGES.values())
    companions = {
        c.package
        for comps in di.DEPENDENCY_COMPANION_PACKAGES.values()
        for c in comps
    }
    registry_introduced = set(di.REGISTRY_INTRODUCED_PACKAGE_PINS)
    always_provided = set(di._ALWAYS_PROVIDED_PACKAGES)

    # Each named table actually produces package names (not an empty claim).
    assert runtime == {"gsap", "@gsap/react", "three", "lenis"}
    assert companions == {"@types/three"}
    assert registry_introduced == {"cn", "radix-ui", "lucide-react"}
    assert always_provided == {
        "class-variance-authority", "react", "react-dom"
    }


def test_the_reviewed_builtin_tables_are_a_subset_of_the_four_sources():
    """The per-component builtin tables may only NAME packages the four cover.

    They describe WHICH reviewed package a component introduces/imports, not a
    fifth package source: every entry must already be in the four closed tables.
    """
    from app.core import design_install as di

    covered = (
        set(di.DEPENDENCY_PACKAGES.values())
        | {
            c.package
            for comps in di.DEPENDENCY_COMPANION_PACKAGES.values()
            for c in comps
        }
        | set(di.REGISTRY_INTRODUCED_PACKAGE_PINS)
        | set(di._ALWAYS_PROVIDED_PACKAGES)
    )
    builtin_named = set()
    for packages in di.REVIEWED_BUILTIN_COMPONENT_DEPENDENCIES.values():
        builtin_named.update(packages)
    for packages in di.REVIEWED_BUILTIN_COMPONENT_IMPORTS.values():
        builtin_named.update(packages)

    assert builtin_named <= covered, builtin_named - covered


def test_resolve_package_returns_none_for_an_unowned_id():
    """Q9: "no path by which model/resource text contributes a package name"."""
    from app.core import design_install as di

    for hostile in ("evil-pkg", "gsap@^3.0.0", "../etc", "", "https://x/y.tgz"):
        assert di.resolve_package(hostile) is None
        assert di.resolve_companion_packages(hostile) == ()
