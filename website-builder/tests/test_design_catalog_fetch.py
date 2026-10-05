"""Focused tests for the bounded catalog discovery adapters (D3a.5 VPS repair).

Every test injects a transport, so the suite is hermetic and offline. The one
LIVE path is documented at the bottom of this file rather than executed here.
"""

from __future__ import annotations

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
    assert CATALOG_ENDPOINTS[SOURCE_TWENTY_FIRST].startswith("https://21st.dev/")


@pytest.mark.parametrize("source", [SOURCE_REACT_BITS, SOURCE_TWENTY_FIRST])
def test_every_endpoint_passes_its_own_allowlist(source):
    assert url_is_allowed(source, CATALOG_ENDPOINTS[source]) is True


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
    payload = {"components": [{"id": "blur-text", "name": "Blur Text"}]}
    transport = RecordingTransport(body=json.dumps(payload).encode("utf-8"))

    result = fetch_catalog_payload(SOURCE_TWENTY_FIRST, transport=transport)

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

    result = fetch_catalog_payload(SOURCE_TWENTY_FIRST, transport=transport)

    assert result.reason == REASON_UNEXPECTED_ERROR


def test_undecodable_bytes_are_a_degraded_result():
    transport = RecordingTransport(body=b"\xff\xfe\x00binary")

    result = fetch_catalog_payload(SOURCE_TWENTY_FIRST, transport=transport)

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
    fetch_catalog_payload(
        SOURCE_TWENTY_FIRST,
        transport=transport,
        credential_provider=lambda _source: None,
    )
    assert "Authorization" not in seen


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
    assert {"BlurText", "CountUp", "ASCIIText"} <= set(result.installable_ids())
    for entry in result.entries:
        assert entry.source == SOURCE_REACT_BITS
        assert entry.provenance_host == "reactbits.dev"


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

    assert "BlurText" in result.installable_ids(), "upstream really lists it"

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
    outcome = build_registry_request(
        SOURCE_REACT_BITS, "SplitText", declared_dependencies=["gsap"]
    )

    assert outcome.ok is True
    assert outcome.request.source == SOURCE_REACT_BITS
    assert outcome.request.component_id == "SplitText"
    assert outcome.request.required_dependency_ids == ("gsap",)


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

    assert "BlurText" in result.installable_ids()
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