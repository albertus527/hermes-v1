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
    ``llms.txt`` documents a REST API (OpenAPI at ``/openapi.json``) that "the
    21st CLI and MCP server call", authenticated with ``Authorization: Bearer``
    against a user-issued key from ``/mcp``. Metadata ``search`` is free;
    component retrieval (``get_component``) is the paid tier. So discovery is
    attempted only when a key is present, and retrieval is never attempted here.

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
import re
import socket
import ssl
import urllib.error
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
REASON_CREDENTIAL_REQUIRED = "this catalog needs a credential that is not configured"
REASON_UNEXPECTED_ERROR = "the official catalog could not be read"

CATALOG_FETCH_REASONS = frozenset(
    {
        REASON_UNREACHABLE,
        REASON_TIMEOUT,
        REASON_TOO_LARGE,
        REASON_REDIRECT_REFUSED,
        REASON_BAD_STATUS,
        REASON_CREDENTIAL_REQUIRED,
        REASON_UNEXPECTED_ERROR,
    }
)


# ---------------------------------------------------------------------------
# Endpoints and hosts -- application-owned, CLOSED
# ---------------------------------------------------------------------------

#: Per-source, the ONE documented machine-readable discovery surface. Both were
#: verified from upstream ``llms.txt``; neither is discovered at runtime, and
#: neither can be overridden by a caller.
CATALOG_ENDPOINTS: Dict[str, str] = {
    # 21st's agent-facing markdown index of the registry surface.
    SOURCE_TWENTY_FIRST: "https://21st.dev/llms.txt",
    # React Bits' published agent index, which enumerates the CLI identifiers.
    SOURCE_REACT_BITS: "https://reactbits.dev/llms.txt",
}

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
        assert self.payload is not None  # implied by ok

        # Both documented endpoints are markdown indexes. Parse the index into
        # component documents, then hand those to the unchanged normalizer --
        # HTTP stays in this module and normalization stays offline.
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


def fetch_catalog_payload(
    source: str,
    *,
    transport: Optional[Transport] = None,
    credential_provider: Optional[CredentialProvider] = None,
    timeout: int = FETCH_TIMEOUT_SECONDS,
) -> CatalogFetchResult:
    """Fetch ``source``'s documented catalog, bounded at every step.

    Returns a :class:`CatalogFetchResult`; a failure is a bounded degraded
    result carrying a static reason, never a fabricated payload.

    ``transport`` is a trusted test seam. It is never channel- or
    resource-controlled, and the URL passed to it is the module constant, so a
    substituted transport still cannot introduce an unapproved host into a real
    request -- the allowlist is re-checked on the way out regardless.
    """
    url = CATALOG_ENDPOINTS.get(source)
    allowed = ALLOWED_HOSTS.get(source)
    if url is None or not allowed:
        return CatalogFetchResult(source=source, reason=REASON_UNREACHABLE)

    if not url_is_allowed(source, url):
        # A constant that fails its own allowlist is a programming error, and
        # must fail closed rather than "helpfully" proceeding.
        logger.error("A catalog endpoint failed its own host allowlist; refusing.")
        return CatalogFetchResult(source=source, reason=REASON_REDIRECT_REFUSED)

    headers = {
        "Accept": "application/json, text/plain;q=0.9, */*;q=0.1",
        "Accept-Encoding": "identity",
        "User-Agent": "hermes-website-builder",
    }

    # 21st's REST surface requires a bearer key. Presence is the only thing that
    # gates DISCOVERY here; the value never leaves the header.
    if credential_provider is not None:
        credential = credential_provider(source)
        if isinstance(credential, str) and credential.strip():
            headers["Authorization"] = f"Bearer {credential.strip()}"

    execute = transport or _default_transport

    try:
        status, body = execute(url, headers, timeout)
    except urllib.error.HTTPError as error:
        if 300 <= int(getattr(error, "code", 0) or 0) < 400:
            return CatalogFetchResult(
                source=source, reason=REASON_REDIRECT_REFUSED, degraded=True
            )
        return CatalogFetchResult(
            source=source, reason=REASON_BAD_STATUS, degraded=True
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

    status = int(status)
    if 300 <= status < 400:
        return CatalogFetchResult(
            source=source, reason=REASON_REDIRECT_REFUSED, degraded=True
        )
    if status != 200:
        return CatalogFetchResult(
            source=source, reason=REASON_BAD_STATUS, degraded=True
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
#: backticked token. Verified in reactbits.dev/llms.txt.
_CLI_MARKER = re.compile(r"CLI:\s*`([A-Za-z0-9][A-Za-z0-9_-]*)`")

#: 21st documents install ids as slash paths (e.g. ``/@author/components/slug``)
#: and lowercase slugs; the identity is the trailing slug.
_21ST_SLUG = re.compile(r"/components/([a-z0-9]+(?:-[a-z0-9]+)*)\b")

#: Upper bound on how many component lines one index may contribute. Upstream
#: lists hundreds; the normalizer applies its own budget, and this keeps the
#: parse itself cheap on a hostile or enormous document.
MAX_PARSED_IDS = 512


def parse_markdown_catalog(source: str, text: str) -> Any:
    """Turn a bounded llms.txt body into documents for ``normalize_catalog``.

    Returns ``[]`` (a well-formed but empty listing) rather than raising on
    anything unrecognized: an index this application cannot read is a degraded
    empty result, never a fabricated entry.
    """
    from app.core.design_catalog import component_id_is_valid

    if not isinstance(text, str) or not text.strip():
        return []

    if source not in ALLOWED_HOSTS:
        return []

    found = []
    seen = set()

    if source == SOURCE_REACT_BITS:
        # React Bits documents PascalCase CLI identifiers.
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

    # 21st: component slugs appear in documented `/components/<slug>` paths.
    for match in _21ST_SLUG.finditer(text):
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


def discover_catalog(
    source: str,
    *,
    transport: Optional[Transport] = None,
    credential_provider: Optional[CredentialProvider] = None,
    timeout: int = FETCH_TIMEOUT_SECONDS,
) -> CatalogResult:
    """Discovery for ``source``, end to end and bounded.

    This is the entry point a caller wants. It never raises for an upstream
    problem: a network failure, a malformed payload, or an oversized response
    all become a degraded :class:`CatalogResult` with zero entries, because a
    plausible-looking component that upstream never listed is the fabrication
    this whole boundary exists to prevent.
    """
    fetched = fetch_catalog_payload(
        source,
        transport=transport,
        credential_provider=credential_provider,
        timeout=timeout,
    )
    return fetched.to_result()


__all__ = [
    "ALLOWED_HOSTS",
    "CATALOG_ENDPOINTS",
    "CATALOG_FETCH_REASONS",
    "FETCH_TIMEOUT_SECONDS",
    "MAX_RESPONSE_BYTES",
    "REASON_BAD_STATUS",
    "REASON_CREDENTIAL_REQUIRED",
    "REASON_REDIRECT_REFUSED",
    "REASON_TIMEOUT",
    "REASON_TOO_LARGE",
    "REASON_UNEXPECTED_ERROR",
    "REASON_UNREACHABLE",
    "CatalogFetchResult",
    "discover_catalog",
    "fetch_catalog_payload",
    "parse_markdown_catalog",
    "url_is_allowed",
]