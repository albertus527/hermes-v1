"""External component catalogs: 21st.dev and React Bits (Batch D3a.5 F/G).

Two upstream catalogs, one shape. Both publish a machine-readable list of real
components and both install through the shadcn CLI, so the *normalization* is
shared even though the identity vocabularies differ:

    21st.dev     component ids are kebab/lowercase slugs
    React Bits   component ids are PascalCase (SplitText, BlurText, CountUp)

This module normalizes a raw catalog payload into an application-owned
:class:`CatalogEntry` and NOTHING more. It deliberately does not install, does
not write files, and does not call the network: a caller supplies an
already-fetched, already-bounded payload. That split is what lets the capability
layer stay offline while the retrieval layer stays bounded.

**Everything upstream is untrusted DATA.** Specifically:

* a component id is only ever a *proposal*. :mod:`app.core.design_registry`
  decides whether an approved locator exists for it, so nothing here can make
  an unreviewed component installable.
* declared dependency NAMES are mapped through the closed allowlist by
  :func:`app.core.design_registry.resolve_dependency_requirements`. An unknown
  name makes the component NOT installable; it never widens the allowlist.
* no field from upstream is ever placed in a command. The only thing that
  reaches argv is the locator the application resolved itself.
* provenance is retained so a consumer can cite what it used, and it is
  normalized to a host plus a path -- never a raw string echoed wholesale.

**No fabrication.** An entry exists only because a real payload listed it. A
missing or malformed payload yields zero entries plus a static warning; the
module has no notion of "a typical 21st component" to fall back on, which is
the fabrication failure this whole batch exists to prevent.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.core.design_registry import (
    PACKAGE_TO_DEPENDENCY_ID,
    RESERVED_COMPONENT_IDS,
    SOURCE_REACT_BITS,
    SOURCE_TWENTY_FIRST,
    resolve_dependency_requirements,
    resolve_registry_locator,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Static warnings -- a CLOSED set
# ---------------------------------------------------------------------------

WARNING_CATALOG_EMPTY = "the catalog payload contained no usable component entries"
WARNING_CATALOG_MALFORMED = "the catalog payload could not be parsed; no entries produced"

#: Bound on how many entries one payload may contribute. Upstream catalogs hold
#: thousands of components; shipping them all would be a context blow-up and an
#: injection surface. Truncation is explicit and reported, never silent.
MAX_CATALOG_ENTRIES = 64

#: Bound on any single field copied out of upstream. A component description is
#: untrusted text of unknown length.
MAX_FIELD_CHARS = 400

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_PASCAL_RE = re.compile(r"^[A-Z][A-Za-z0-9]*$")

#: Route / highlight / category segments that are NOT component identities. The
#: single source of truth is :data:`app.core.design_registry.RESERVED_COMPONENT_IDS`
#: -- it lives in the module that decides INSTALLABILITY, and this module (which
#: imports it) refuses the same set at the identity vocabulary. Re-exported here
#: under the historical private name so existing callers keep working.
_RESERVED_COMPONENT_IDS: Dict[str, frozenset] = RESERVED_COMPONENT_IDS


@dataclass(frozen=True)
class CatalogEntry:
    """One real component, normalized into an application-owned shape.

    ``declared_dependency_ids`` are already mapped through the closed allowlist,
    so they are safe to act on. ``unknown_dependency_ids`` is retained
    deliberately: a consumer must be able to SEE that a component wanted
    something out of policy, because that is why it is not installable. Dropping
    it would make a refusal inexplicable to whoever hits it.
    """

    source: str
    component_id: str
    display_name: str
    provenance_host: str
    provenance_path: str
    declared_dependency_ids: Tuple[str, ...] = ()
    unknown_dependency_ids: Tuple[str, ...] = ()

    @property
    def identity(self) -> str:
        """The stable, citable identity: source plus component."""
        return f"{self.source}:{self.component_id}"

    @property
    def dependencies_in_policy(self) -> bool:
        """Whether every declared dependency is in the closed allowlist."""
        return not self.unknown_dependency_ids

    @property
    def has_approved_locator(self) -> bool:
        """Whether the application has a REVIEWED canonical locator for this.

        The registry decides this, not the catalog: a component upstream lists
        is installable only when this application has reviewed it.
        """
        return resolve_registry_locator(self.source, self.component_id) is not None

    @property
    def installable(self) -> bool:
        """Whether THIS APPLICATION can actually install the component.

        BOTH conditions are required: the declared dependency set must be
        entirely in policy AND an approved canonical locator must exist for
        ``(source, component_id)``. A component upstream lists but this
        application has NOT reviewed is not installable -- claiming otherwise is
        the false positive this property must never repeat. (Deriving
        installability from declared dependencies alone reported every listed
        React Bits component as installable while only the reviewed one was.)
        """
        return self.dependencies_in_policy and self.has_approved_locator

    def to_dict(self) -> Dict[str, Any]:
        """Serializable and bounded. No absolute path, no credential."""
        return {
            "source": self.source,
            "component_id": self.component_id,
            "display_name": self.display_name,
            "provenance": {
                "host": self.provenance_host,
                "path": self.provenance_path,
            },
            "declared_dependency_ids": list(self.declared_dependency_ids),
            "unknown_dependency_ids": list(self.unknown_dependency_ids),
            "dependencies_in_policy": self.dependencies_in_policy,
            "has_approved_locator": self.has_approved_locator,
            "installable": self.installable,
        }


@dataclass(frozen=True)
class CatalogResult:
    """The outcome of normalizing one catalog payload."""

    source: str
    entries: Tuple[CatalogEntry, ...]
    warnings: Tuple[str, ...] = ()
    truncated: bool = False

    def __post_init__(self) -> None:
        # `ok` is `not warnings`, so an ok result MUST carry entries: otherwise
        # `ok=True` would assert a usable catalog that has nothing in it -- the
        # same "success flag not bound to its payload" defect as the container
        # shape. Production always pairs empty entries with a warning; this makes
        # that a property of the TYPE.
        if not self.warnings and not self.entries:
            raise ValueError("an ok catalog result must carry at least one entry")

    @property
    def ok(self) -> bool:
        return not self.warnings

    def installable_ids(self) -> Tuple[str, ...]:
        return tuple(sorted(e.component_id for e in self.entries if e.installable))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "entries": [e.to_dict() for e in self.entries],
            "warnings": list(self.warnings),
            "truncated": self.truncated,
        }


#: The canonical host per source, matching the registry locator table. Recorded
#: here too so provenance cannot name a host the registry would refuse.
SOURCE_HOSTS: Dict[str, str] = {
    SOURCE_TWENTY_FIRST: "21st.dev",
    SOURCE_REACT_BITS: "reactbits.dev",
}


def component_id_is_valid(source: str, component_id: object) -> bool:
    """Whether ``component_id`` matches ``source``'s own identity vocabulary.

    Per-source rather than one shared rule, because the vocabularies genuinely
    differ and accepting either form everywhere would let a React Bits
    component be requested as a 21st component.

    A RESERVED route/highlight segment (``s``, ``popular``, ``newest``,
    ``featured``, ``week``) is refused even though it is slug-shaped: those are
    page ROUTES in 21st's index, not components, and the false-positive parser
    turned exactly them into fabricated identities. Refusing them at the
    vocabulary makes that impossible on EVERY path, not just the parser's.
    """
    if not isinstance(component_id, str) or not component_id or len(component_id) > 64:
        return False
    if component_id in _RESERVED_COMPONENT_IDS.get(source, frozenset()):
        return False
    if source == SOURCE_TWENTY_FIRST:
        return bool(_SLUG_RE.match(component_id))
    if source == SOURCE_REACT_BITS:
        return bool(_PASCAL_RE.match(component_id))
    return False


def _bound_text(value: Any) -> str:
    """Coerce any upstream string field to a bounded, single-paragraph string.

    Upstream text is untrusted and unbounded, so it is truncated here rather
    than at the entry level -- otherwise one enormous description would be
    counted against the entry budget only after being copied in full.
    """
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.split())
    return cleaned[:MAX_FIELD_CHARS]


def _first_str(document: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _dependency_names(document: Mapping[str, Any]) -> Tuple[str, ...]:
    """Extract declared dependency names from the several shapes catalogs use.

    Catalogs are inconsistent -- a list of strings, a list of objects, a
    comma-joined string -- so all three are read. Every value is then treated as
    an untrusted proposal and mapped through the closed allowlist; none is ever
    forwarded to a package manager.
    """
    raw = document.get("dependencies") or document.get("requires") or ()
    names: List[str] = []
    if isinstance(raw, str):
        names.extend(part.strip() for part in raw.split(",") if part.strip())
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, str):
                names.append(item.strip())
            elif isinstance(item, Mapping):
                name = _first_str(item, "name", "package", "dependency")
                if name:
                    names.append(name)
    return tuple(n for n in names if n)


def normalize_catalog_entry(source: str, document: Any) -> Optional[CatalogEntry]:
    """Normalize one upstream component object, or return ``None``.

    Returns ``None`` -- never a partially-invented entry -- when the object has
    no valid identity for this source. An entry without a real component id
    cannot be cited, resolved, or installed, so constructing one would only add
    a plausible-looking row to a report.
    """
    if not isinstance(document, Mapping):
        return None

    component_id = _first_str(document, "id", "component_id", "slug", "name")
    if not component_id_is_valid(source, component_id):
        return None

    known, unknown = resolve_dependency_requirements(_dependency_names(document))

    return CatalogEntry(
        source=source,
        component_id=component_id,
        display_name=_bound_text(_first_str(document, "name", "title")) or component_id,
        provenance_host=SOURCE_HOSTS.get(source, ""),
        provenance_path=component_id,
        declared_dependency_ids=known,
        unknown_dependency_ids=unknown,
    )


#: Per-source, the container KEY under which that source's own list-response
#: nests its component objects. Verified from each source's published contract:
#:
#: * 21st's REST search 200 response is ``{"query","scope","results":[...]}`` --
#:   ``results`` is the ONLY component container the API emits.
#: * React Bits has no JSON list surface; ``parse_markdown_catalog`` yields a
#:   bare list, so it is keyed ``None`` (the bare-list form).
#:
#: Recognising a container key is NOT free: it is what ``ok`` reports. Accepting
#: keys a source never emits makes ``ok=True`` mean "some JSON parsed" while
#: reading as "this source's catalog was read" -- a semantic lie. The shape is
#: therefore per-source and closed.
CATALOG_CONTAINER_KEYS: Dict[str, Optional[str]] = {
    SOURCE_TWENTY_FIRST: "results",
    SOURCE_REACT_BITS: None,  # a bare list
}


def _iter_documents(
    source: str, payload: Any
) -> Tuple[List[Mapping[str, Any]], bool]:
    """Pull component-shaped objects out of the shape ``source`` VERIFIABLY uses.

    Returns ``(documents, ok)``. ``ok`` is False when the payload is not the
    source's recognized container, which is the "cannot be parsed" case --
    distinct from a well-formed payload that simply lists nothing.

    The container shape is the SOURCE'S OWN verified shape, not a generic set of
    plausible keys: a payload keyed ``components`` is NOT a 21st catalog (21st
    emits ``results``), so accepting it would let ``ok`` claim a 21st read that
    never happened.
    """
    expected_key = CATALOG_CONTAINER_KEYS.get(source)

    if isinstance(payload, (str, bytes)):
        try:
            decoded = json.loads(payload)
        except (ValueError, TypeError):
            return [], False
        return _iter_documents(source, decoded)

    if isinstance(payload, Mapping):
        if expected_key is None:
            # This source's surface is a bare list; a mapping is not it.
            return [], False
        value = payload.get(expected_key)
        if isinstance(value, (list, tuple)):
            return [d for d in value if isinstance(d, Mapping)], True
        return [], False

    if isinstance(payload, (list, tuple)):
        if expected_key is not None:
            # This source's surface is a keyed container; a bare list is not it.
            return [], False
        return [d for d in payload if isinstance(d, Mapping)], True

    return [], False


def normalize_catalog(
    source: str, payload: Any, *, limit: int = MAX_CATALOG_ENTRIES
) -> CatalogResult:
    """Normalize a catalog payload into bounded :class:`CatalogEntry` rows.

    Performs no network access and no installation: the payload is supplied by
    the caller, already fetched and already bounded. Truncation is reported in
    ``truncated`` rather than being silent, because a caller that sees 64 of
    12,000 components must be able to tell the difference between "that is
    everything" and "that is what fit".
    """
    if source not in SOURCE_HOSTS:
        return CatalogResult(
            source=source,
            entries=(),
            warnings=(WARNING_CATALOG_MALFORMED,),
        )

    documents, ok = _iter_documents(source, payload)
    if not ok:
        return CatalogResult(
            source=source, entries=(), warnings=(WARNING_CATALOG_MALFORMED,)
        )

    entries: List[CatalogEntry] = []
    seen = set()
    for document in documents:
        entry = normalize_catalog_entry(source, document)
        if entry is None or entry.component_id in seen:
            continue
        seen.add(entry.component_id)
        entries.append(entry)
        if len(entries) >= limit:
            break

    if not entries:
        return CatalogResult(
            source=source, entries=(), warnings=(WARNING_CATALOG_EMPTY,)
        )

    return CatalogResult(
        source=source,
        entries=tuple(entries),
        truncated=len(entries) < len(documents),
    )


def find_catalog_entry(result: CatalogResult, component_id: str) -> Optional[CatalogEntry]:
    """The normalized entry for ``component_id``, or ``None``.

    Exact match only. A prefix or substring match here would let a request for
    ``Split`` install ``SplitText``, which is a different component -- so
    identity is matched as identity.
    """
    if not isinstance(component_id, str):
        return None
    for entry in result.entries:
        if entry.component_id == component_id:
            return entry
    return None


__all__ = [
    "CATALOG_CONTAINER_KEYS",
    "MAX_CATALOG_ENTRIES",
    "_RESERVED_COMPONENT_IDS",
    "MAX_FIELD_CHARS",
    "SOURCE_HOSTS",
    "WARNING_CATALOG_EMPTY",
    "WARNING_CATALOG_MALFORMED",
    "CatalogEntry",
    "CatalogResult",
    "component_id_is_valid",
    "find_catalog_entry",
    "normalize_catalog",
    "normalize_catalog_entry",
]