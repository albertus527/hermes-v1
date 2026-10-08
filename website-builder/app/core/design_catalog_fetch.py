"""Bounded, application-owned catalog discovery adapters (D3a.5 VPS repair).

Adds the two REAL fetch adapters the capability model already claimed existed.

Why this module exists
----------------------
``app.core.design_catalog`` normalizes a payload that a CALLER supplies, and
``resolve_design_capabilities()`` reported ``discovery_available=True`` for both
21st.dev and React Bits on the strength of that normalizer alone. VPS
qualification found there is **no production network adapter** for either -- so
the capability report described a capability that did not exist. That is the
worst kind of lie here: it says "we can search it" when nothing can.

This module supplies the missing middle, and only the middle:

    real upstream discovery
        -> bounded fetch (this module)
        -> bounded raw payload
        -> normalize_catalog(SOURCE, ...)        (unchanged, still offline)
        -> CatalogResult

``normalize_catalog`` itself is untouched and still performs no I/O: keeping the
network OUT of the normalizer is what lets the capability resolver stay local,
offline and socket-free while discovery is available where it is actually
used.

Verified upstream contracts
---------------------------
21st.dev
    The real machine surface is the authenticated REST API documented by
    ``openapi.json`` (server ``https://21st.dev/api/v1``), the same surface the
    ``21st`` CLI and MCP server call. Component discovery is
    ``GET /api/v1/components/search?q=<term>``, which returns
    ``{"results": [{slug, name, install_ref, ...}]}``. Verified live (2026-10):
    it returns **HTTP 401 without a Bearer key**, so discovery is
    CREDENTIAL-GATED. Its public ``llms.txt`` publishes NO component-identity
    schema -- only route/category links -- so it is not a discovery surface at
    all. This module therefore uses the REST search endpoint and refuses to
    attempt a request when no key is present, rather than parsing route text
    into fabricated identities.

React Bits
    ``llms.txt`` publishes ``https://reactbits.dev/llms.txt`` as the agent-facing
    surface, and documents the shadcn registry form
    ``https://reactbits.dev/r/<Component>-<LANG>-<STYLE>``. No credential is
    required for either.

Security properties (all load-bearing, all enforced below)
----------------------------------------------------------
* **Explicit host allowlist.** A resolved URL's host must be in the per-source
  allowlist. Nothing else is reachable.
* **HTTPS only.** A non-https scheme is refused before any socket is opened.
* **No caller-supplied URL.** The endpoint is a module constant. A model,
  resource, or prompt cannot influence what is fetched -- there is no parameter
  through which a URL arrives.
* **Bounded timeout, bounded response size.** A slow or enormous upstream is a
  degraded result, not a hang or an OOM.
* **Redirects refused.** Any 3xx is a failure; a redirect cannot walk the fetch
  to an unapproved host.
* **No raw upstream URL ever becomes argv.** This module returns catalog
  ENTITIES. Locators are resolved later, by ``design_registry``, from an
  application-owned allowlist.
* **Malformed or failed upstream yields a bounded degraded result**, never a
  fabricated entry. There is no "typical component" fallback anywhere here.
* **No network from the capability resolver.** ``resolve_design_capabilities``
  does not import this module.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlsplit

from app.core.design_catalog import (
    CatalogResult,
    WARNING_CATALOG_EMPTY,
    WARNING_CATALOG_MALFORMED,
    normalize_catalog,
)
from app.core.design_registry import SOURCE_REACT_BITS, SOURCE_TWENTY_FIRST
from app.core.design_resources import CREDENTIAL_ENV_NAMES

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bounded failure reasons -- a CLOSED set of STATIC labels
# ---------------------------------------------------------------------------
#
# Never a URL, a status code, a response body, or an exception message. These
# strings are safe to log and to render into a prompt.

REASON_UNREACHABLE = "the official catalog endpoint was unreachable"
REASON_TIMEOUT = "the official catalog endpoint did not respond in time"
REASON_TOO_LARGE = "the official catalog response exceeded the size bound"
REASON_REDIRECT_REFUSED = "the official catalog endpoint redirected; refusing to follow"
REASON_BAD_STATUS = "the official catalog endpoint returned an unusable status"
#: The contract documents HTTP 401 distinctly from any other non-200. A key can
#: be PRESENT but INVALID/expired (verified live: a bogus bearer returns
#: ``401 invalid_api_key``), so a present credential is not a working one. Named
#: separately so an operator can tell a bad key from a rate limit or a real
#: outage.
REASON_AUTH_REJECTED = "the catalog rejected the configured credential"
#: The contract documents HTTP 429 (rate limited) as its own response. A rate
#: limit is transient and actionable ("retry later"), which is a different
#: remedy from an invalid key or an unusable status, so it is named separately.
REASON_RATE_LIMITED = "the official catalog rate-limited the request"
REASON_CREDENTIAL_REQUIRED = "this catalog needs a credential that is not configured"
REASON_UNEXPECTED_ERROR = "the official catalog could not be read"

CATALOG_FETCH_REASONS = frozenset(
    {
        REASON_UNREACHABLE,
        REASON_TIMEOUT,
        REASON_TOO_LARGE,
        REASON_REDIRECT_REFUSED,
        REASON_BAD_STATUS,
        REASON_AUTH_REJECTED,
        REASON_RATE_LIMITED,
        REASON_CREDENTIAL_REQUIRED,
        REASON_UNEXPECTED_ERROR,
    }
)


# ---------------------------------------------------------------------------
# Endpoints and hosts -- application-owned, CLOSED
# ---------------------------------------------------------------------------

#: Per-source, the ONE documented machine-readable discovery surface that is
#: reachable with a plain GET and no query. React Bits publishes an
#: unauthenticated agent index that enumerates its CLI identifiers.
CATALOG_ENDPOINTS: Dict[str, str] = {
    # React Bits' published agent index, which enumerates the CLI identifiers.
    SOURCE_REACT_BITS: "https://reactbits.dev/llms.txt",
}

#: Sources whose discovery is an authenticated REST SEARCH. The request URL is
#: built from this module constant plus a BOUNDED, PERCENT-ENCODED query term --
#: never from a caller-supplied URL. 21st has no "list everything" surface, so
#: its only real discovery is search.
CATALOG_SEARCH_ENDPOINTS: Dict[str, str] = {
    SOURCE_TWENTY_FIRST: "https://21st.dev/api/v1/components/search",
}

#: Sources whose discovery surface requires a credential. 21st's search API
#: returns HTTP 401 without a Bearer key, so no request is attempted without one
#: -- the honest bounded state is CREDENTIAL-REQUIRED, not a wasted round-trip
#: and not an empty catalog reported as a real one.
CREDENTIAL_REQUIRED_FOR_DISCOVERY: frozenset = frozenset({SOURCE_TWENTY_FIRST})

#: Sources whose RETRIEVAL surface -- fetching a component's real content, which
#: the install path executes via the pinned CLI -- requires a credential.
#: Verified live (2026-10): 21st's registry item
#: (``https://21st.dev/r/<user>/<slug>``) answers **403 Authentication required**
#: and its install API (``/api/v1/components/install/<user>/<slug>``) answers
#: **401** without a key; React Bits' registry item
#: (``https://reactbits.dev/r/<Component>-<LANG>-<STYLE>``) is served openly
#: (HTTP 200 with no credential).
#:
#: This is the ONE place the credential requirement per axis is declared. The
#: capability layer DERIVES its `authentication_required` and its discovery /
#: retrieval gates from these tables rather than from caller-supplied booleans,
#: so what the capability layer reports can never drift from what execution
#: actually requires.
CREDENTIAL_REQUIRED_FOR_RETRIEVAL: frozenset = frozenset({SOURCE_TWENTY_FIRST})


def credential_requirement(source: str) -> Tuple[bool, bool]:
    """``(discovery_requires_credential, retrieval_requires_credential)``.

    The application's own, closed answer for ``source``. The capability layer
    consumes THIS rather than accepting the requirement as a parameter, so the
    reported truth is a derivation from the executable tables, not an assertion
    a call site could get wrong.
    """
    return (
        source in CREDENTIAL_REQUIRED_FOR_DISCOVERY,
        source in CREDENTIAL_REQUIRED_FOR_RETRIEVAL,
    )

#: The scope and page size the application sends. Both are module constants, not
#: caller input, so a request cannot widen what upstream returns. ``public`` is
#: the community catalog; the API's own default is the caller's private scope.
DISCOVERY_SCOPE = "public"
DISCOVERY_LIMIT = 24

#: Bound on a discovery query term. The term is inert DATA -- it is percent-
#: encoded into the query string of a module-owned URL and can never introduce a
#: host, a path, or a second parameter.
MAX_QUERY_CHARS = 64

#: The application-owned term used when a caller does not supply one. It is a
#: SEARCH TERM, not an entry: every result still comes only from upstream, so
#: this cannot fabricate a component.
DEFAULT_DISCOVERY_QUERY = "component"

#: Exact hosts permitted per source. The allowlist is checked against the
#: PARSED host of the URL this module built, so a redirected or substituted
#: host is refused even though the URL itself was a constant.
ALLOWED_HOSTS: Dict[str, frozenset] = {
    SOURCE_TWENTY_FIRST: frozenset({"21st.dev"}),
    SOURCE_REACT_BITS: frozenset({"reactbits.dev"}),
}

#: Hard bounds. A catalog is a directory listing, not a bundle: two seconds and
#: two megabytes are generous for the documented surfaces, and both bounds exist
#: so a hostile or broken upstream cannot stall or exhaust a build.
FETCH_TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class CatalogFetchResult:
    """A bounded discovery outcome: the payload, or why there is none.

    ``payload`` is ``None`` whenever ``reason`` is set, so a caller cannot
    accidentally normalize an error string into entries.
    """

    source: str
    payload: Optional[Any] = None
    reason: Optional[str] = None
    degraded: bool = False

    def __post_init__(self) -> None:
        if self.reason is not None and self.reason not in CATALOG_FETCH_REASONS:
            raise ValueError(f"unregistered catalog fetch reason: {self.reason!r}")
        if self.reason is not None and self.payload is not None:
            raise ValueError("a failed fetch carries no payload")
        # `ok` is `reason is None`, so a successful fetch MUST carry a payload.
        # Enforced at CONSTRUCTION: the invariant is a property of the type, not
        # a comment plus an `assert` a caller must remember (and which `python -O`
        # strips). Without this, a payload-less success silently normalized.
        if self.reason is None and self.payload is None:
            raise ValueError("a successful fetch must carry a payload")

    @property
    def ok(self) -> bool:
        return self.reason is None

    def to_result(self) -> CatalogResult:
        """Normalize into a :class:`CatalogResult`, or a degraded empty one.

        This is the ONLY place the two halves meet, and it is deliberately the
        last step: normalization stays offline, and a failure can never become
        an entry because there is nothing to normalize.
        """
        if not self.ok:
            return CatalogResult(
                source=self.source,
                entries=(),
                warnings=(self.reason or REASON_UNREACHABLE,),
                truncated=False,
            )
        # `ok` implies a payload, enforced by __post_init__, so this is a fact
        # of the type rather than an `assert` (which `python -O` would strip).
        assert self.payload is not None  # nosec - implied by ok, see __post_init__

        # A markdown index (React Bits llms.txt) is parsed into component
        # documents first; a JSON payload (21st's search response) is passed
        # straight to the unchanged normalizer. Either way HTTP stays in this
        # module and normalization stays offline.
        if isinstance(self.payload, str):
            return normalize_catalog(
                self.source, parse_markdown_catalog(self.source, self.payload)
            )
        return normalize_catalog(self.source, self.payload)


def _empty_result(source: str, reason: str) -> CatalogResult:
    return CatalogResult(source=source, entries=(), warnings=(reason,))


def url_is_allowed(source: str, url: str) -> bool:
    """Whether ``url`` is an https URL on one of ``source``'s allowed hosts.

    Checked independently of how ``url`` was produced, so it is a real gate
    rather than a formality. Returns ``False`` on anything malformed.
    """
    allowed = ALLOWED_HOSTS.get(source)
    if not allowed:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and (parts.hostname or "") in allowed


# ---------------------------------------------------------------------------
# The bounded fetch
# ---------------------------------------------------------------------------

#: A transport is ``(url, headers, timeout) -> (status, bytes)``. Injected so
#: tests exercise every failure branch without a network, and so the seam is a
#: seam rather than a mock of the whole module.
Transport = Callable[[str, Dict[str, str], int], Tuple[int, bytes]]

#: A credential provider is ``(source) -> Optional[str]``. Presence matters for
#: capability state; the VALUE is only ever placed in a request header and is
#: never returned, logged, or stored.
CredentialProvider = Callable[[str], Optional[str]]


def _environment_credential(source: str) -> Optional[str]:
    """The DEFAULT credential provider: the resource's configured env value.

    This is the wiring the adapter needs in production. Without a default the
    ``credential_provider`` parameter had to be supplied by a caller -- and no
    production caller did -- so a CONFIGURED key never reached the request while
    the capability layer reported ``discovery_available=True`` on the strength of
    that same key. The reported capability and the executed request disagreed.

    It reads the SAME names the capability layer reads (the one table in
    :mod:`app.core.design_resources`), so presence and use cannot drift. The
    value is returned to be placed in an ``Authorization`` header and is never
    logged, stored, or returned to a caller of this module.
    """
    for name in CREDENTIAL_ENV_NAMES.get(source, ()):
        value = os.environ.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn every redirect into an error instead of following it.

    ``urllib`` follows redirects by default, which would let an approved host
    bounce the fetch to an arbitrary one -- defeating the allowlist. Refusing is
    the only behaviour that keeps the allowlist meaningful.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def _default_transport(
    url: str, headers: Dict[str, str], timeout: int
) -> Tuple[int, bytes]:
    """The real bounded GET. HTTPS only, no redirects, size-capped read."""
    if not url.lower().startswith("https://"):
        raise ValueError("non-https url refused")

    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        _NoRedirect(),
    )
    request = urllib.request.Request(url, headers=headers, method="GET")
    with opener.open(request, timeout=timeout) as response:
        status = getattr(response, "status", None) or response.getcode()
        body = response.read(MAX_RESPONSE_BYTES + 1)
    return int(status), body


def build_discovery_url(source: str, query: Optional[str] = None) -> Optional[str]:
    """The ONE request URL for ``source``, or ``None`` when it cannot be built.

    For a source with a plain index endpoint the URL is the module constant. For
    a source whose only real surface is an authenticated REST SEARCH (21st), the
    URL is that constant plus a BOUNDED, PERCENT-ENCODED query term and the
    application-owned ``scope``/``limit``. The term is inert data: percent
    encoding means it can never introduce a host, a path segment, or a second
    query parameter, and the constants mean a caller cannot widen the request.

    Returns ``None`` for an unknown source, or when the built URL fails its own
    host allowlist (a programming error that must fail closed).
    """
    if source in CATALOG_ENDPOINTS:
        url = CATALOG_ENDPOINTS[source]
    elif source in CATALOG_SEARCH_ENDPOINTS:
        term = query if isinstance(query, str) and query.strip() else DEFAULT_DISCOVERY_QUERY
        term = term.strip()[:MAX_QUERY_CHARS]
        encoded = urllib.parse.quote(term, safe="")
        url = (
            f"{CATALOG_SEARCH_ENDPOINTS[source]}"
            f"?q={encoded}&scope={DISCOVERY_SCOPE}&limit={DISCOVERY_LIMIT}"
        )
    else:
        return None

    if not url_is_allowed(source, url):
        logger.error("A catalog endpoint failed its own host allowlist; refusing.")
        return None
    return url


def fetch_catalog_payload(
    source: str,
    *,
    transport: Optional[Transport] = None,
    credential_provider: Optional[CredentialProvider] = None,
    timeout: int = FETCH_TIMEOUT_SECONDS,
    query: Optional[str] = None,
) -> CatalogFetchResult:
    """Fetch ``source``'s documented catalog, bounded at every step.

    Returns a :class:`CatalogFetchResult`; a failure is a bounded degraded
    result carrying a static reason, never a fabricated payload.

    ``transport`` is a trusted test seam. It is never channel- or
    resource-controlled, and the URL passed to it is built by
    :func:`build_discovery_url` from module constants plus an inert, encoded
    query term -- so a substituted transport still cannot introduce an
    unapproved host, and the allowlist is re-checked on the way out regardless.

    A source whose discovery surface is authenticated (21st) is NOT requested
    without a credential: an unauthenticated GET would only return HTTP 401, so
    the honest bounded state is :data:`REASON_CREDENTIAL_REQUIRED` rather than a
    wasted round-trip reported as an empty catalog.
    """
    if source not in ALLOWED_HOSTS:
        return CatalogFetchResult(source=source, reason=REASON_UNREACHABLE)

    # A caller may inject a provider (tests do); otherwise the adapter reads the
    # configured credential itself, from the SAME names the capability layer
    # checks. No production wiring is required for a present key to be used.
    provider = credential_provider or _environment_credential
    credential = None
    candidate = provider(source)
    if isinstance(candidate, str) and candidate.strip():
        credential = candidate.strip()

    if source in CREDENTIAL_REQUIRED_FOR_DISCOVERY and credential is None:
        # No key -> no request. This is the designed bounded state, not an error.
        return CatalogFetchResult(source=source, reason=REASON_CREDENTIAL_REQUIRED)

    url = build_discovery_url(source, query)
    if url is None:
        return CatalogFetchResult(source=source, reason=REASON_UNREACHABLE)

    headers = {
        "Accept": "application/json, text/plain;q=0.9, */*;q=0.1",
        "Accept-Encoding": "identity",
        "User-Agent": "hermes-website-builder",
    }

    # Presence is the only thing that gates DISCOVERY here; the value never
    # leaves the header, and is never returned, logged, or stored.
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"

    execute = transport or _default_transport

    try:
        status, body = execute(url, headers, timeout)
    except urllib.error.HTTPError as error:
        code = int(getattr(error, "code", 0) or 0)
        return CatalogFetchResult(
            source=source, reason=_reason_for_status(code), degraded=True
        )
    except (socket.timeout, TimeoutError):
        return CatalogFetchResult(source=source, reason=REASON_TIMEOUT, degraded=True)
    except urllib.error.URLError as error:
        nested = getattr(error, "reason", None)
        if isinstance(nested, (socket.timeout, TimeoutError)):
            return CatalogFetchResult(
                source=source, reason=REASON_TIMEOUT, degraded=True
            )
        return CatalogFetchResult(
            source=source, reason=REASON_UNREACHABLE, degraded=True
        )
    except (OSError, ValueError):
        return CatalogFetchResult(
            source=source, reason=REASON_UNREACHABLE, degraded=True
        )
    except Exception:
        # Catch-all so the documented contract holds: a fetch NEVER raises for an
        # upstream problem. This is load-bearing, not belt-and-braces: urllib's
        # transport raises ``http.client.HTTPException`` subclasses
        # (``BadStatusLine`` on a malformed status line, ``IncompleteRead`` on a
        # truncated body, ``LineTooLong``, ``UnknownProtocol``) which are NOT
        # ``OSError`` and so slip past the clause above. Letting one escape would
        # both break the contract and risk the request HEADERS -- including
        # ``Authorization: Bearer <key>`` -- appearing in a caller's traceback.
        # The reason is a static label; nothing from the exception is echoed.
        return CatalogFetchResult(
            source=source, reason=REASON_UNEXPECTED_ERROR, degraded=True
        )

    status = int(status)
    if status != 200:
        return CatalogFetchResult(
            source=source, reason=_reason_for_status(status), degraded=True
        )
    if len(body) > MAX_RESPONSE_BYTES:
        return CatalogFetchResult(
            source=source, reason=REASON_TOO_LARGE, degraded=True
        )

    payload = _decode_payload(body)
    if payload is None:
        return CatalogFetchResult(
            source=source, reason=REASON_UNEXPECTED_ERROR, degraded=True
        )

    return CatalogFetchResult(source=source, payload=payload)


def _reason_for_status(status: int) -> str:
    """The bounded reason for a non-200 HTTP status, per the documented contract.

    The 21st search contract documents three responses: 200, **401**, and
    **429**. 401 means the credential was rejected (present but invalid or
    expired -- verified live: a bogus bearer returns ``401 invalid_api_key``);
    429 means the caller was rate-limited. Both are named separately from the
    generic unusable-status case, because each has a DIFFERENT remedy. Any other
    status, and every redirect, is the generic bounded reason. A status code
    never becomes a reason string.
    """
    if 300 <= status < 400:
        return REASON_REDIRECT_REFUSED
    if status == 401:
        return REASON_AUTH_REJECTED
    if status == 429:
        return REASON_RATE_LIMITED
    return REASON_BAD_STATUS


def _decode_payload(body: bytes) -> Optional[Any]:
    """JSON when it is JSON, else the bounded text the llms.txt surfaces return.

    Both documented endpoints are markdown indexes, so text is the expected case
    rather than a fallback. Decoding is bounded by the size check above and
    never raises: undecodable bytes are a degraded result, not a crash.
    """
    if isinstance(body, (bytes, bytearray)):
        try:
            text = bytes(body).decode("utf-8")
        except UnicodeDecodeError:
            return None
    else:  # pragma: no cover - defensive; transports return bytes
        text = str(body)

    stripped = text.strip()
    if not stripped:
        return None

    if stripped[0] in "[{":
        try:
            return json.loads(stripped)
        except (ValueError, TypeError):
            return None

    return stripped


#: The marker upstream uses for the CLI-installable identifier, then a
#: backticked token. Verified in reactbits.dev/llms.txt, where it is the ONLY
#: documented identity surface.
_CLI_MARKER = re.compile(r"CLI:\s*`([A-Za-z0-9][A-Za-z0-9_-]*)`")

#: 21st has NO markdown component-identity schema, so this module keeps NO regex
#: over its ``/components/<slug>`` paths -- not even an anchored one.
#:
#: 21st documents component PAGES as ``/@author/components/<slug>`` and CATEGORY
#: pages as ``/community/components/s/<tag>``, ``/community/components/popular``,
#: ``/newest``, ``/featured``, ``/week``. A regex over those paths cannot tell a
#: component page from a ROUTE: the previous revision extracted the generic tail
#: ``/components/([a-z0-9-]+)`` and turned the routes into fabricated identities
#: (on the real ``llms.txt``: exactly ``s``, ``popular``, ``newest``,
#: ``featured``, ``week``). Anchoring on ``/@<author>/`` fixes the symptom, but
#: it still scrapes a path this application has no verified schema for, and its
#: only consumer would be a test -- so the extraction is REMOVED, not kept.
#:
#: Verified live (2026-10): 21st's ``llms.txt`` publishes NO machine-readable
#: component-identity schema. Its real machine surface is the authenticated REST
#: API (``/api/v1/components/search``), which returns HTTP 401 without a Bearer
#: key. If a markdown schema is EVER verified, it must be extracted with an
#: ``/@<author>/``-anchored pattern -- never the generic ``/components/<slug>``
#: tail -- and this comment is the record of that rule.

#: Upper bound on how many component lines one index may contribute. Upstream
#: lists hundreds; the normalizer applies its own budget, and this keeps the
#: parse itself cheap on a hostile or enormous document.
MAX_PARSED_IDS = 512


def parse_markdown_catalog(source: str, text: str) -> Any:
    """Turn a bounded llms.txt body into documents for ``normalize_catalog``.

    Returns ``[]`` (a well-formed but empty listing) rather than raising on
    anything unrecognized: an index this application cannot read is a degraded
    empty result, never a fabricated entry.

    Only a VERIFIED identity marker is accepted. React Bits publishes one
    (``CLI: `X` ``); 21st publishes NONE in its documentation index, so its
    routes (``/components/popular``, ``/components/s/hero``, ...) yield zero
    identities instead of fabricated ones. A 200 response whose body carries no
    recognized identity schema is therefore an EMPTY result, not a success.
    """
    from app.core.design_catalog import component_id_is_valid

    if not isinstance(text, str) or not text.strip():
        return []

    if source not in ALLOWED_HOSTS:
        return []

    found = []
    seen = set()

    if source == SOURCE_REACT_BITS:
        # React Bits documents PascalCase CLI identifiers behind an explicit
        # `CLI:` marker. That marker IS the verified schema.
        for match in _CLI_MARKER.finditer(text):
            identity = match.group(1).strip()
            if not component_id_is_valid(source, identity):
                continue
            if identity in seen:
                continue
            seen.add(identity)
            found.append({"id": identity, "name": identity})
            if len(found) >= MAX_PARSED_IDS:
                break
        return found

    # 21st: NO verified component-identity schema exists in the documented
    # surface, so NOTHING is extracted -- there is no regex over its
    # ``/components/<slug>`` paths at all. Route paths are not identities, and a
    # scrape would fabricate them; the honest result is an empty listing. (A
    # future schema must be extracted with an ``/@<author>/``-anchored pattern,
    # never the generic tail -- see the module constant above.)
    return []


def discover_catalog(
    source: str,
    *,
    transport: Optional[Transport] = None,
    credential_provider: Optional[CredentialProvider] = None,
    timeout: int = FETCH_TIMEOUT_SECONDS,
    query: Optional[str] = None,
) -> CatalogResult:
    """Discovery for ``source``, end to end and bounded.

    This is the entry point a caller wants. It never raises for an upstream
    problem: a network failure, a malformed payload, or an oversized response
    all become a degraded :class:`CatalogResult` with zero entries, because a
    plausible-looking component that upstream never listed is the fabrication
    this whole boundary exists to prevent.

    ``query`` is an optional SEARCH TERM for a source whose discovery surface is
    a search (21st). It is inert data -- bounded and percent-encoded into a
    module-owned URL -- and it can never widen the request. Every result still
    comes only from upstream, so no term can fabricate an entry.
    """
    fetched = fetch_catalog_payload(
        source,
        transport=transport,
        credential_provider=credential_provider,
        timeout=timeout,
        query=query,
    )
    return fetched.to_result()


__all__ = [
    "ALLOWED_HOSTS",
    "CATALOG_ENDPOINTS",
    "CATALOG_FETCH_REASONS",
    "CATALOG_SEARCH_ENDPOINTS",
    "CREDENTIAL_REQUIRED_FOR_DISCOVERY",
    "CREDENTIAL_REQUIRED_FOR_RETRIEVAL",
    "credential_requirement",
    "DEFAULT_DISCOVERY_QUERY",
    "DISCOVERY_LIMIT",
    "DISCOVERY_SCOPE",
    "FETCH_TIMEOUT_SECONDS",
    "MAX_QUERY_CHARS",
    "MAX_RESPONSE_BYTES",
    "REASON_AUTH_REJECTED",
    "REASON_BAD_STATUS",
    "REASON_RATE_LIMITED",
    "REASON_CREDENTIAL_REQUIRED",
    "REASON_REDIRECT_REFUSED",
    "REASON_TIMEOUT",
    "REASON_TOO_LARGE",
    "REASON_UNEXPECTED_ERROR",
    "REASON_UNREACHABLE",
    "CatalogFetchResult",
    "build_discovery_url",
    "discover_catalog",
    "fetch_catalog_payload",
    "parse_markdown_catalog",
    "url_is_allowed",
]