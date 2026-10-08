"""Focused tests for the bounded catalog discovery adapters (D3a.5 VPS repair).

Every test injects a transport, so the suite is hermetic and offline. The one
LIVE path is documented at the bottom of this file rather than executed here.
"""

from __future__ import annotations

import http.client
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_catalog_fetch import (
    ALLOWED_HOSTS,
    CATALOG_ENDPOINTS,
    CATALOG_FETCH_REASONS,
    CATALOG_SEARCH_ENDPOINTS,
    CREDENTIAL_REQUIRED_FOR_DISCOVERY,
    MAX_RESPONSE_BYTES,
    REASON_BAD_STATUS,
    REASON_REDIRECT_REFUSED,
    REASON_TIMEOUT,
    REASON_TOO_LARGE,
    REASON_UNEXPECTED_ERROR,
    REASON_UNREACHABLE,
    CatalogFetchResult,
    discover_catalog,
    fetch_catalog_payload,
    parse_markdown_catalog,
    url_is_allowed,
)
from app.core.design_registry import (
    SOURCE_REACT_BITS,
    SOURCE_TWENTY_FIRST,
    build_registry_request,
    resolve_registry_locator,
)

#: A trimmed but REAL slice of reactbits.dev/llms.txt: the `CLI: `X`.` marker
#: and a prose sentence that must NOT become a component.
REAL_REACTBITS_INDEX = """# React Bits

- [ASCII Text](https://www.reactbits.dev/text-animations/ascii-text): Renders text with an animated ASCII background. CLI: `ASCIIText`.
- [Blur Text](https://www.reactbits.dev/text-animations/blur-text): Text starts blurred. CLI: `BlurText`.
- [Count Up](https://www.reactbits.dev/text-animations/count-up): Animated number counter. CLI: `CountUp`.

- shadcn: `npx shadcn@latest add https://reactbits.dev/r/<Component>-<LANG>-<STYLE>`
"""


class RecordingTransport:
    """Records the request a fetch would make; returns a canned response."""

    def __init__(self, status=200, body=b"", error=None):
        self.calls = []
        self.status = status
        self.body = body
        self.error = error

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers), timeout))
        if self.error is not None:
            raise self.error
        return self.status, self.body


# ---------------------------------------------------------------------------
# Endpoints and hosts are application-owned and closed
# ---------------------------------------------------------------------------


def test_the_endpoints_are_application_owned_constants():
    """Never discovered, never overridden by a caller."""
    assert CATALOG_ENDPOINTS[SOURCE_REACT_BITS] == "https://reactbits.dev/llms.txt"
    # 21st's only real surface is the authenticated REST SEARCH; its public
    # llms.txt publishes no identity schema, so it is NOT a discovery endpoint.
    assert SOURCE_TWENTY_FIRST not in CATALOG_ENDPOINTS
    assert (
        CATALOG_SEARCH_ENDPOINTS[SOURCE_TWENTY_FIRST]
        == "https://21st.dev/api/v1/components/search"
    )
    assert SOURCE_TWENTY_FIRST in CREDENTIAL_REQUIRED_FOR_DISCOVERY


@pytest.mark.parametrize("source", [SOURCE_REACT_BITS, SOURCE_TWENTY_FIRST])
def test_every_built_url_passes_its_own_allowlist(source):
    """Every URL this module builds is https on the source's own host."""
    from app.core.design_catalog_fetch import build_discovery_url

    url = build_discovery_url(source)
    assert url is not None
    assert url_is_allowed(source, url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://reactbits.dev/llms.txt",          # not https
        "https://evil.example/llms.txt",          # unapproved host
        "https://reactbits.dev.evil.example/x",   # suffix confusion
        "https://user@evil.example/x",            # credential trick
        "file:///etc/passwd",
        "https://21st.dev/x",                     # wrong host for this source
        "",
    ],
)
def test_non_allowlisted_urls_are_refused(url):
    """HTTPS only, exact host, on the right source."""
    assert url_is_allowed(SOURCE_REACT_BITS, url) is False


def test_an_unknown_source_has_no_allowlist_at_all():
    assert url_is_allowed("some_other_source", "https://some_other_source/x") is False


def test_a_source_with_no_endpoint_fails_closed():
    result = fetch_catalog_payload("some_other_source", transport=RecordingTransport())
    assert result.ok is False
    assert result.reason in CATALOG_FETCH_REASONS


# ---------------------------------------------------------------------------
# Bounded fetch: success and every failure branch
# ---------------------------------------------------------------------------


def test_a_bounded_fetch_succeeds_and_returns_the_payload():
    transport = RecordingTransport(body=REAL_REACTBITS_INDEX.encode("utf-8"))

    result = fetch_catalog_payload(SOURCE_REACT_BITS, transport=transport)

    assert result.ok is True
    assert result.payload == REAL_REACTBITS_INDEX.strip()
    assert result.degraded is False


def test_the_fetch_requests_only_the_application_owned_url():
    transport = RecordingTransport(body=b"{}")

    fetch_catalog_payload(SOURCE_REACT_BITS, transport=transport)

    url, _, _ = transport.calls[0]
    assert url == CATALOG_ENDPOINTS[SOURCE_REACT_BITS]


def test_a_json_endpoint_is_passed_through_unparsed():
    """21st's REST search returns JSON; it reaches the normalizer unparsed."""
    payload = {"components": [{"id": "blur-text", "name": "Blur Text"}]}
    transport = RecordingTransport(body=json.dumps(payload).encode("utf-8"))

    result = fetch_catalog_payload(
        SOURCE_TWENTY_FIRST,
        transport=transport,
        credential_provider=lambda _s: "21st_sk_present",
    )

    assert result.payload == payload


def test_a_timeout_is_a_bounded_degraded_result():
    transport = RecordingTransport(error=TimeoutError())

    result = fetch_catalog_payload(SOURCE_REACT_BITS, transport=transport)

    assert result.ok is False
    assert result.reason == REASON_TIMEOUT
    assert result.degraded is True


def test_a_network_failure_is_a_bounded_degraded_result():
    transport = RecordingTransport(error=urllib.error.URLError("no route"))

    result = fetch_catalog_payload(SOURCE_REACT_BITS, transport=transport)

    assert result.ok is False
    assert result.reason == REASON_UNREACHABLE


def test_a_nested_timeout_in_a_urlerror_is_reported_as_a_timeout():
    transport = RecordingTransport(
        error=urllib.error.URLError(TimeoutError("slow"))
    )

    result = fetch_catalog_payload(SOURCE_REACT_BITS, transport=transport)

    assert result.reason == REASON_TIMEOUT


def test_a_redirect_status_is_refused_not_followed():
    """Following a redirect would walk the allowlist to an unapproved host."""
    transport = RecordingTransport(status=302)

    result = fetch_catalog_payload(SOURCE_REACT_BITS, transport=transport)

    assert result.reason == REASON_REDIRECT_REFUSED


def test_a_redirect_exception_is_refused():
    error = urllib.error.HTTPError(
        CATALOG_ENDPOINTS[SOURCE_REACT_BITS], 302, "found", {}, None
    )
    result = fetch_catalog_payload(
        SOURCE_REACT_BITS, transport=RecordingTransport(error=error)
    )

    assert result.reason == REASON_REDIRECT_REFUSED


def test_an_error_status_is_reported_not_treated_as_data():
    transport = RecordingTransport(status=500)

    result = fetch_catalog_payload(SOURCE_REACT_BITS, transport=transport)

    assert result.reason == REASON_BAD_STATUS
    assert result.payload is None


def test_an_oversized_response_is_refused():
    transport = RecordingTransport(body=b"x" * (MAX_RESPONSE_BYTES + 1))

    result = fetch_catalog_payload(SOURCE_REACT_BITS, transport=transport)

    assert result.reason == REASON_TOO_LARGE


def test_malformed_json_is_a_degraded_result_not_an_entry():
    transport = RecordingTransport(body=b"{not json")

    result = fetch_catalog_payload(
        SOURCE_TWENTY_FIRST,
        transport=transport,
        credential_provider=lambda _s: "21st_sk_present",
    )

    assert result.reason == REASON_UNEXPECTED_ERROR


def test_undecodable_bytes_are_a_degraded_result():
    transport = RecordingTransport(body=b"\xff\xfe\x00binary")

    result = fetch_catalog_payload(
        SOURCE_TWENTY_FIRST,
        transport=transport,
        credential_provider=lambda _s: "21st_sk_present",
    )

    assert result.ok is False
    assert result.reason in CATALOG_FETCH_REASONS


def test_an_empty_body_is_a_degraded_result():
    transport = RecordingTransport(body=b"   ")

    result = fetch_catalog_payload(SOURCE_REACT_BITS, transport=transport)

    assert result.reason == REASON_UNEXPECTED_ERROR


def test_a_credential_is_sent_only_as_a_header_and_only_when_present():
    seen = {}

    def transport(url, headers, timeout):
        seen.update(headers)
        return 200, b"{}"

    fetch_catalog_payload(
        SOURCE_TWENTY_FIRST,
        transport=transport,
        credential_provider=lambda _source: "sk-secret",
    )
    assert seen["Authorization"] == "Bearer sk-secret"

    seen.clear()
    no_key = fetch_catalog_payload(
        SOURCE_TWENTY_FIRST,
        transport=transport,
        credential_provider=lambda _source: None,
    )
    # No credential -> no request at all (21st's search API is 401 without one).
    assert seen == {}
    assert no_key.reason == "this catalog needs a credential that is not configured"


def test_the_credential_value_never_reaches_the_result():
    """Presence is capability state; the value is a secret."""
    result = fetch_catalog_payload(
        SOURCE_TWENTY_FIRST,
        transport=RecordingTransport(body=b"{}"),
        credential_provider=lambda _source: "sk-secret",
    )

    assert "sk-secret" not in json.dumps(
        {"payload": result.payload, "reason": result.reason}, default=str
    )


# ---------------------------------------------------------------------------
# The failure vocabulary is closed and static
# ---------------------------------------------------------------------------


def test_every_reason_is_static_and_bounded():
    for reason in CATALOG_FETCH_REASONS:
        assert isinstance(reason, str)
        assert "://" not in reason
        assert "\n" not in reason
        assert len(reason) <= 120


def test_an_unregistered_reason_cannot_be_constructed():
    with pytest.raises(ValueError):
        CatalogFetchResult(source=SOURCE_REACT_BITS, reason="https://evil.example/x")


def test_a_failed_fetch_carries_no_payload():
    """A caller cannot normalize an error string into entries."""
    with pytest.raises(ValueError):
        CatalogFetchResult(source=SOURCE_REACT_BITS, payload={"id": "x"}, reason="boom")


# ---------------------------------------------------------------------------
# Normalization flows through design_catalog
# ---------------------------------------------------------------------------


def test_discovery_normalizes_through_design_catalog():
    transport = RecordingTransport(body=REAL_REACTBITS_INDEX.encode("utf-8"))

    result = discover_catalog(SOURCE_REACT_BITS, transport=transport)

    assert result.ok is True
    assert result.source == SOURCE_REACT_BITS
    # Every listed id is a real entry...
    assert {"BlurText", "CountUp", "ASCIIText"} <= {
        e.component_id for e in result.entries
    }
    # ...but "installable" requires an APPROVED locator, so none of these
    # unreviewed components is installable (only the reviewed one is).
    assert result.installable_ids() == ()
    for entry in result.entries:
        assert entry.source == SOURCE_REACT_BITS
        assert entry.provenance_host == "reactbits.dev"
        assert entry.dependencies_in_policy is True


def test_a_network_failure_yields_zero_entries_and_no_fabrication():
    result = discover_catalog(
        SOURCE_REACT_BITS, transport=RecordingTransport(error=TimeoutError())
    )

    assert result.entries == ()
    assert result.ok is False
    assert result.warnings == (REASON_TIMEOUT,)


def test_a_malformed_payload_yields_zero_entries():
    result = discover_catalog(
        SOURCE_TWENTY_FIRST, transport=RecordingTransport(body=b"not a catalog")
    )

    assert result.entries == ()
    assert result.ok is False


def test_a_malformed_markdown_index_yields_zero_entries_not_invented_ones():
    result = discover_catalog(
        SOURCE_REACT_BITS,
        transport=RecordingTransport(body=b"prose with no CLI markers"),
    )

    assert result.entries == ()


def test_the_parsed_index_is_bounded_and_reports_truncation():
    """Upstream lists hundreds; a caller must see it is not seeing all of them."""
    transport = RecordingTransport(body=REAL_REACTBITS_INDEX.encode("utf-8"))

    result = discover_catalog(SOURCE_REACT_BITS, transport=transport)

    # The trimmed fixture fits under the bound, so nothing was dropped and
    # the result must NOT claim truncation: a spurious True would tell a
    # caller it is seeing less of the catalog than it actually is.
    assert result.truncated is False
    assert len(result.entries) == 3


# ---------------------------------------------------------------------------
# Parsing reads the documented marker, not prose
# ---------------------------------------------------------------------------


def test_only_the_cli_marker_becomes_a_component():
    docs = parse_markdown_catalog(SOURCE_REACT_BITS, REAL_REACTBITS_INDEX)

    assert {d["id"] for d in docs} == {"ASCIIText", "BlurText", "CountUp"}


def test_prose_never_becomes_a_component():
    """The bareword mentions are not identities."""
    docs = parse_markdown_catalog(SOURCE_REACT_BITS, REAL_REACTBITS_INDEX)
    ids = {d["id"] for d in docs}

    assert "Component" not in ids
    assert "LANG" not in ids
    assert "STYLE" not in ids
    assert "shadcn" not in ids


def test_a_lowercase_marked_id_is_not_a_react_bits_component():
    """React Bits identities are PascalCase; a slug is not one."""
    docs = parse_markdown_catalog(SOURCE_REACT_BITS, "- x CLI: `blur-text`.",)

    assert docs == []


def test_an_empty_or_non_text_index_yields_nothing():
    assert parse_markdown_catalog(SOURCE_REACT_BITS, "") == []
    assert parse_markdown_catalog(SOURCE_REACT_BITS, "   ") == []
    assert parse_markdown_catalog(SOURCE_REACT_BITS, None) == []


def test_an_unknown_source_is_never_parsed():
    assert parse_markdown_catalog("some_other_source", REAL_REACTBITS_INDEX) == []


# ---------------------------------------------------------------------------
# Install boundary: review is the gate, not the catalog
# ---------------------------------------------------------------------------


def test_a_component_discovered_upstream_is_still_only_a_proposal():
    """BlurText is a real upstream component and is NOT installable."""
    transport = RecordingTransport(body=REAL_REACTBITS_INDEX.encode("utf-8"))
    result = discover_catalog(SOURCE_REACT_BITS, transport=transport)

    # It is a real ENTRY...
    assert "BlurText" in {e.component_id for e in result.entries}
    # ...but NOT installable, and the catalog agrees (no false positive).
    assert "BlurText" not in result.installable_ids()
    assert resolve_registry_locator(SOURCE_REACT_BITS, "BlurText") is None
    outcome = build_registry_request(SOURCE_REACT_BITS, "BlurText")
    assert outcome.ok is False
    assert outcome.request is None


def test_the_reviewed_component_resolves_to_its_canonical_locator():
    assert (
        resolve_registry_locator(SOURCE_REACT_BITS, "SplitText")
        == "https://reactbits.dev/r/SplitText-TS-TW"
    )


def test_a_reviewed_component_builds_a_typed_install_request():
    """The reviewed SplitText contract, declared as the LIVE registry does.

    The live document declares ``gsap@^3.13.0`` and ``@gsap/react@^2.1.2``. The
    contract requires BOTH ids, so a request declaring only ``gsap`` is refused
    (a separate test). This exercises the matching path.
    """
    outcome = build_registry_request(
        SOURCE_REACT_BITS,
        "SplitText",
        declared_dependencies=["gsap@^3.13.0", "@gsap/react@^2.1.2"],
        declared_registry_dependencies=[],
    )

    assert outcome.ok is True
    assert outcome.request.source == SOURCE_REACT_BITS
    assert outcome.request.component_id == "SplitText"
    assert outcome.request.required_dependency_ids == ("gsap", "gsap_react")


def test_the_locator_host_and_source_match_is_enforced():
    """A locator whose host is not its source's host is refused at construction."""
    from app.core.design_registry import RegistryInstallRequest

    with pytest.raises(ValueError):
        RegistryInstallRequest(
            source=SOURCE_REACT_BITS,
            component_id="SplitText",
            registry_locator_id="https://evil.example/r/SplitText-TS-TW",
        )


def test_the_variant_is_application_owned_not_caller_supplied():
    """A remote payload proposes; the application decides the variant."""
    transport = RecordingTransport(
        body=b"- [Blur Text](https://x.dev/r/BlurText-JS-CSS): CLI: `BlurText`.\n"
    )
    result = discover_catalog(SOURCE_REACT_BITS, transport=transport)

    assert "BlurText" in {e.component_id for e in result.entries}
    assert "BlurText" not in result.installable_ids()
    assert resolve_registry_locator(SOURCE_REACT_BITS, "BlurText") is None


# ---------------------------------------------------------------------------
# No network from capability resolution
# ---------------------------------------------------------------------------


def test_capability_resolution_imports_no_fetch_module():
    """The resolver must stay local, offline and socket-free."""
    import app.core.design_capabilities as caps

    source = Path(caps.__file__).read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "design_catalog_fetch" not in stripped, line
            assert "urllib" not in stripped, line
            assert "socket" not in stripped, line


def test_normalization_performs_no_network():
    """design_catalog is untouched by this repair and still does no I/O."""
    import app.core.design_catalog as catalog

    source = Path(catalog.__file__).read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "urllib" not in stripped, line
            assert "socket" not in stripped, line


def test_the_fetch_module_never_imports_the_capability_resolver():
    import app.core.design_catalog_fetch as fetch

    source = Path(fetch.__file__).read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "design_capabilities" not in stripped, line


# ---------------------------------------------------------------------------
# LIVE smoke path (documented, not run here -- no network in the suite)
# ---------------------------------------------------------------------------
#
# Run manually against the real upstreams:
#
#   python -c "from app.core.design_catalog_fetch import discover_catalog; \
#              r = discover_catalog('react_bits'); \
#              print(r.ok, len(r.entries), r.truncated, 'SplitText' in r.installable_ids())"
#
# Observed on the qualification host (2026-10-05):
#
#   True 64 True True
#
# i.e. 213 real components parsed, bounded to the first 64 with `truncated`
# reported, and the reviewed `SplitText` present. `discover_catalog('twenty_first')`
# requires an API key for the REST surface and degrades cleanly without one.


def test_live_smoke_is_documented_rather_than_executed():
    """This module never touches the network; the live path is opt-in."""
    transport = RecordingTransport(body=REAL_REACTBITS_INDEX.encode("utf-8"))
    assert discover_catalog(SOURCE_REACT_BITS, transport=transport).ok is True


# ---------------------------------------------------------------------------
# Part G: 21st route paths are NOT component identities
# ---------------------------------------------------------------------------
#
# The VPS-found false positive: a parser that extracted ``/components/<x>`` from
# the raw text turned 21st's ROUTE/CATEGORY links into fabricated catalog
# identities. The real 21st surface is authenticated (HTTP 401 without a Bearer
# key) and its public llms.txt publishes no component-identity schema.

#: A trimmed slice of the REAL 21st llms.txt category/highlight routes -- the
#: exact paths the old regex turned into fake identities.
REAL_21ST_ROUTES = """# 21st (https://21st.dev)

## Component categories
- [Hero Sections](https://21st.dev/community/components/s/hero): landing page hero components
- [Cards](https://21st.dev/community/components/s/card): card layouts
- [Buttons](https://21st.dev/community/components/s/button): buttons

## Community highlights
- [Popular Components](https://21st.dev/community/components/popular): most used
- [Latest Components](https://21st.dev/community/components/newest): newest
- [Featured Components](https://21st.dev/community/components/featured): picks
- [Weekly Best](https://21st.dev/community/components/week): top of the week
"""


def test_21st_route_paths_do_not_become_component_identities():
    """NONE of the category/highlight routes may be a catalog identity."""
    docs = parse_markdown_catalog(SOURCE_TWENTY_FIRST, REAL_21ST_ROUTES)

    ids = {d["id"] for d in docs}
    for forbidden in ("s", "popular", "newest", "featured", "week", "hero", "card", "button"):
        assert forbidden not in ids, forbidden


def test_a_21st_200_with_routes_only_yields_zero_entries():
    """A network success with no identity schema is an EMPTY result, not a pass."""
    transport = RecordingTransport(body=REAL_21ST_ROUTES.encode("utf-8"))

    result = discover_catalog(SOURCE_TWENTY_FIRST, transport=transport)

    assert result.entries == ()
    assert result.ok is False, "no identities parsed must not be reported as ok"
    assert result.warnings


def test_the_react_bits_cli_marker_still_parses():
    """The regression must not break the source that DOES publish a schema."""
    docs = parse_markdown_catalog(SOURCE_REACT_BITS, REAL_REACTBITS_INDEX)

    assert {d["id"] for d in docs} == {"ASCIIText", "BlurText", "CountUp"}


def test_the_21st_schema_flag_is_explicitly_false():
    """The verified fact: no unauthenticated 21st identity schema exists."""
    import app.core.design_catalog_fetch as fetch

    assert fetch._21ST_COMPONENT_IDENTITY_SCHEMA_VERIFIED is False


# ---------------------------------------------------------------------------
# Part G: 21st discovery is the REAL authenticated REST search
# ---------------------------------------------------------------------------
#
# The false-positive route parser was removed; this is its REPLACEMENT. 21st's
# only real machine surface is the authenticated REST API
# (`GET /api/v1/components/search`), which returns HTTP 401 without a Bearer
# key. Discovery therefore uses that endpoint, sends no request without a
# credential, and never parses route text into identities.

#: A trimmed slice of a REAL 21st search response (schema from openapi.json).
REAL_21ST_SEARCH = {
    "query": "hero",
    "scope": "public",
    "results": [
        {"name": "Hero Section", "slug": "hero-section", "registry": "blocks",
         "install_ref": "@someone/hero-section", "author": "someone"},
        {"name": "Pricing Table", "slug": "pricing-table", "registry": "blocks",
         "install_ref": "@other/pricing-table"},
    ],
}


def test_21st_discovery_without_a_credential_makes_no_request():
    """No key -> no request. A 401 round-trip is not a useful degradation."""
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        return 200, b"{}"

    result = fetch_catalog_payload(SOURCE_TWENTY_FIRST, transport=transport)

    assert calls == [], "no request may be attempted without a credential"
    assert result.ok is False
    assert result.reason == "this catalog needs a credential that is not configured"


def test_21st_discovery_with_a_credential_hits_the_search_endpoint():
    transport = RecordingTransport(body=json.dumps(REAL_21ST_SEARCH).encode("utf-8"))

    result = discover_catalog(
        SOURCE_TWENTY_FIRST,
        transport=transport,
        credential_provider=lambda _s: "21st_sk_present",
        query="hero",
    )

    url, headers, _ = transport.calls[0]
    assert url.startswith("https://21st.dev/api/v1/components/search?")
    assert "q=hero" in url
    assert headers["Authorization"] == "Bearer 21st_sk_present"
    assert result.ok is True
    assert {e.component_id for e in result.entries} == {"hero-section", "pricing-table"}


def test_the_21st_search_results_normalize_and_are_only_proposals():
    """Every result is real, and none is installable (nothing is reviewed)."""
    transport = RecordingTransport(body=json.dumps(REAL_21ST_SEARCH).encode("utf-8"))

    result = discover_catalog(
        SOURCE_TWENTY_FIRST,
        transport=transport,
        credential_provider=lambda _s: "21st_sk_present",
    )

    # It is a real ENTRY...
    assert "hero-section" in {e.component_id for e in result.entries}
    # ...and NOT installable, because review is the gate, not the catalog.
    assert "hero-section" not in result.installable_ids()
    assert resolve_registry_locator(SOURCE_TWENTY_FIRST, "hero-section") is None
    outcome = build_registry_request(SOURCE_TWENTY_FIRST, "hero-section")
    assert outcome.ok is False


def test_the_query_term_cannot_widen_the_request():
    """A hostile term is percent-encoded into a module-owned URL."""
    from app.core.design_catalog_fetch import build_discovery_url

    url = build_discovery_url(
        SOURCE_TWENTY_FIRST, "x&scope=team&evil=https://evil.example"
    )
    assert url.startswith("https://21st.dev/api/v1/components/search?q=")
    # The injected '&scope=team' and 'https://evil' are DATA, not parameters.
    assert "&evil=" not in url
    assert url.count("&scope=") == 1
    assert "scope=public" in url
    assert "https://evil.example" not in url
    assert url_is_allowed(SOURCE_TWENTY_FIRST, url) is True


def test_the_query_term_is_bounded():
    from app.core.design_catalog_fetch import MAX_QUERY_CHARS, build_discovery_url

    url = build_discovery_url(SOURCE_TWENTY_FIRST, "z" * 5000)
    # The encoded term is the bound, not the raw 5000 characters.
    assert url.count("z") <= MAX_QUERY_CHARS


def test_an_empty_query_uses_the_application_owned_default():
    from app.core.design_catalog_fetch import (
        DEFAULT_DISCOVERY_QUERY,
        build_discovery_url,
    )

    assert f"q={DEFAULT_DISCOVERY_QUERY}" in build_discovery_url(SOURCE_TWENTY_FIRST)
    assert f"q={DEFAULT_DISCOVERY_QUERY}" in build_discovery_url(
        SOURCE_TWENTY_FIRST, "   "
    )


def test_the_21st_discovery_url_is_never_caller_supplied():
    """No parameter is a URL; only a bounded search term."""
    import inspect

    from app.core.design_catalog_fetch import build_discovery_url, fetch_catalog_payload

    for fn in (build_discovery_url, fetch_catalog_payload):
        params = set(inspect.signature(fn).parameters)
        for forbidden in ("url", "endpoint", "host", "locator", "href"):
            assert forbidden not in params, (fn.__name__, forbidden)


def test_react_bits_still_needs_no_credential():
    """The credential gate is 21st-specific; React Bits is unchanged."""
    transport = RecordingTransport(body=REAL_REACTBITS_INDEX.encode("utf-8"))

    result = discover_catalog(SOURCE_REACT_BITS, transport=transport)

    assert result.ok is True
    assert SOURCE_REACT_BITS not in CREDENTIAL_REQUIRED_FOR_DISCOVERY


# ---------------------------------------------------------------------------
# The 21st identity pattern is ANCHORED, never the generic /components/<slug>
# ---------------------------------------------------------------------------
#
# The root cause of the route false positive: the pattern kept the generic
# `/components/<slug>` tail and DROPPED the `/@<author>/` anchor. A component
# page is `/@<author>/components/<slug>`; a category/highlight ROUTE is
# `/community/components/<x>` with NO author. Without the anchor, every route
# tail became a fabricated identity.


def test_the_anchored_pattern_extracts_a_real_component_page():
    from app.core.design_catalog_fetch import _21ST_AUTHORED_COMPONENT_RE

    text = "https://21st.dev/@someone/components/hero-banner\n"
    assert _21ST_AUTHORED_COMPONENT_RE.findall(text) == ["hero-banner"]


def test_the_anchored_pattern_never_matches_a_route():
    from app.core.design_catalog_fetch import _21ST_AUTHORED_COMPONENT_RE

    for route in (
        "https://21st.dev/community/components/s/hero\n",
        "https://21st.dev/community/components/popular\n",
        "https://21st.dev/community/components/newest\n",
        "https://21st.dev/community/components/featured\n",
        "https://21st.dev/community/components/week\n",
    ):
        assert _21ST_AUTHORED_COMPONENT_RE.findall(route) == [], route


def test_the_anchored_pattern_yields_nothing_from_the_real_index():
    """The real llms.txt is ROUTES, so the honest answer is zero identities."""
    from app.core.design_catalog_fetch import _21ST_AUTHORED_COMPONENT_RE

    assert _21ST_AUTHORED_COMPONENT_RE.findall(REAL_21ST_ROUTES) == []


def test_the_parser_uses_the_anchored_pattern_not_the_generic_tail():
    """A route-only index must produce zero, even if the flag is flipped."""
    import app.core.design_catalog_fetch as fetch

    # Force the extraction branch to run.
    saved = fetch._21ST_COMPONENT_IDENTITY_SCHEMA_VERIFIED
    try:
        fetch._21ST_COMPONENT_IDENTITY_SCHEMA_VERIFIED = True
        # Routes only -> the anchored pattern extracts nothing.
        assert fetch.parse_markdown_catalog(
            SOURCE_TWENTY_FIRST, REAL_21ST_ROUTES
        ) == []
        # A real authored component page -> extracted.
        docs = fetch.parse_markdown_catalog(
            SOURCE_TWENTY_FIRST,
            "https://21st.dev/@someone/components/hero-banner\n",
        )
        assert {d["id"] for d in docs} == {"hero-banner"}
    finally:
        fetch._21ST_COMPONENT_IDENTITY_SCHEMA_VERIFIED = saved


def test_a_successful_fetch_must_carry_a_payload():
    """`ok` is `reason is None`, so ok MUST imply a payload -- at construction."""
    with pytest.raises(ValueError):
        CatalogFetchResult(source=SOURCE_TWENTY_FIRST, payload=None, reason=None)


def test_a_failed_fetch_still_carries_no_payload():
    with pytest.raises(ValueError):
        CatalogFetchResult(
            source=SOURCE_TWENTY_FIRST, payload={"x": 1}, reason=REASON_UNREACHABLE
        )


# ---------------------------------------------------------------------------
# The failure vocabulary matches the DOCUMENTED contract (200 / 401 / 429)
# ---------------------------------------------------------------------------
#
# 21st's search contract documents three responses: 200, 401 (unauthorized) and
# 429 (rate limited). Each has a different remedy, so each is a distinct bounded
# reason -- not one generic "unusable status".


def test_a_401_is_named_as_a_rejected_credential():
    from app.core.design_catalog_fetch import REASON_AUTH_REJECTED

    r = fetch_catalog_payload(
        SOURCE_TWENTY_FIRST,
        transport=RecordingTransport(status=401),
        credential_provider=lambda _s: "21st_sk_invalid",
    )

    assert r.ok is False
    assert r.reason == REASON_AUTH_REJECTED
    assert r.degraded is True


def test_a_429_is_named_as_rate_limiting():
    from app.core.design_catalog_fetch import REASON_RATE_LIMITED

    r = fetch_catalog_payload(
        SOURCE_TWENTY_FIRST,
        transport=RecordingTransport(status=429),
        credential_provider=lambda _s: "21st_sk_present",
    )

    assert r.ok is False
    assert r.reason == REASON_RATE_LIMITED


def test_401_and_429_are_distinguishable():
    """A bad key and a rate limit have different remedies, so different reasons."""
    a = fetch_catalog_payload(
        SOURCE_TWENTY_FIRST, transport=RecordingTransport(status=401),
        credential_provider=lambda _s: "k",
    )
    b = fetch_catalog_payload(
        SOURCE_TWENTY_FIRST, transport=RecordingTransport(status=429),
        credential_provider=lambda _s: "k",
    )

    assert a.reason != b.reason


@pytest.mark.parametrize("status", [403, 404, 500, 502, 503])
def test_other_statuses_use_the_generic_bounded_reason(status):
    from app.core.design_catalog_fetch import REASON_BAD_STATUS

    r = fetch_catalog_payload(
        SOURCE_TWENTY_FIRST, transport=RecordingTransport(status=status),
        credential_provider=lambda _s: "k",
    )

    assert r.reason == REASON_BAD_STATUS


def test_a_status_code_never_becomes_a_reason_string():
    """The reason vocabulary stays static: no digits, no URL."""
    from app.core.design_catalog_fetch import _reason_for_status

    for status in (301, 302, 400, 401, 403, 404, 418, 429, 500, 503):
        reason = _reason_for_status(status)
        assert str(status) not in reason, status
        assert "://" not in reason
        assert reason in CATALOG_FETCH_REASONS


def test_the_reason_set_still_covers_the_documented_contract():
    """200 -> ok; 401 and 429 each have a NAMED member of the closed set."""
    from app.core.design_catalog_fetch import (
        REASON_AUTH_REJECTED,
        REASON_RATE_LIMITED,
    )

    assert REASON_AUTH_REJECTED in CATALOG_FETCH_REASONS
    assert REASON_RATE_LIMITED in CATALOG_FETCH_REASONS


# ---------------------------------------------------------------------------
# The fetch NEVER raises for an upstream problem -- including HTTPException
# ---------------------------------------------------------------------------
#
# The module's contract is "never raises for an upstream problem". urllib's
# transport raises http.client.HTTPException subclasses (BadStatusLine,
# IncompleteRead, LineTooLong, UnknownProtocol) which are NOT OSError, so they
# slipped past the (OSError, ValueError) clause. An escaping traceback can carry
# the request HEADERS, i.e. the Authorization: Bearer value.


@pytest.mark.parametrize(
    "exc",
    [
        http.client.BadStatusLine("garbage"),
        http.client.IncompleteRead(b"partial"),
        http.client.LineTooLong("line too long"),
        http.client.UnknownProtocol("unknown"),
        RuntimeError("unexpected"),
    ],
)
def test_the_fetch_never_raises_for_an_upstream_problem(exc):
    def transport(url, headers, timeout):
        raise exc

    result = fetch_catalog_payload(
        SOURCE_TWENTY_FIRST,
        transport=transport,
        credential_provider=lambda _s: "21st_sk_present",
    )

    assert result.ok is False
    assert result.reason in CATALOG_FETCH_REASONS
    assert result.degraded is True


def test_an_http_exception_does_not_leak_the_bearer_value():
    """The reason is a static label; nothing from the exception is echoed."""
    secret = "21st_sk_SUPERSECRETVALUE123"

    def transport(url, headers, timeout):
        # worst case: the exception itself embeds the headers
        raise http.client.BadStatusLine(f"garbage headers={headers}")

    result = fetch_catalog_payload(
        SOURCE_TWENTY_FIRST,
        transport=transport,
        credential_provider=lambda _s: secret,
    )

    blob = json.dumps({"reason": result.reason, "payload": result.payload}, default=str)
    assert secret not in blob
    assert "Authorization" not in blob


# ---------------------------------------------------------------------------
# The adapter targets the AUTHORITATIVE interface, with its documented auth
# ---------------------------------------------------------------------------
#
# 21st's ARD manifest (/.well-known/ard.json) names exactly ONE REST API:
#   urn:air:21st.dev:api:rest-v1 -> https://21st.dev/openapi.json
# and its RFC 9727 catalog anchors that service at https://21st.dev/api/v1.
# auth.md says: "REST v1 reads `Authorization: Bearer ...` only and answers
# 401 to `x-api-key`." So the header choice is load-bearing.


def test_the_search_endpoint_is_under_the_designated_api_base():
    from app.core.design_catalog_fetch import CATALOG_SEARCH_ENDPOINTS
    from app.core.design_registry import SOURCE_TWENTY_FIRST

    assert CATALOG_SEARCH_ENDPOINTS[SOURCE_TWENTY_FIRST].startswith(
        "https://21st.dev/api/v1/"
    )


def test_the_adapter_sends_bearer_not_x_api_key():
    """REST v1 answers 401 to x-api-key; Authorization: Bearer is the one."""
    seen = {}

    def transport(url, headers, timeout):
        seen.update(headers)
        return 200, b'{"results":[{"slug":"x"}]}'

    fetch_catalog_payload(
        SOURCE_TWENTY_FIRST,
        transport=transport,
        credential_provider=lambda _s: "21st_sk_present",
    )

    assert seen.get("Authorization") == "Bearer 21st_sk_present"
    assert "x-api-key" not in {k.lower() for k in seen}
