"""Typed registry install boundary (Batch D3a.5 Part D).

Two different things look alike and must never share a code path:

    ``shadcn add button``                  a BUILT-IN primitive, named by us
    ``shadcn add https://21st.dev/r/...``  a REMOTE component at some URL

Both go through the shadcn CLI, so a naive implementation would let a raw
external URL reach the argv -- and a URL is attacker-shaped text. Any component
from 21st or React Bits would then be installable by string, with no allowlist
and no review, which is precisely the supply-chain hazard this module exists to
close.

So the two are separated by TYPE, not by convention:

    built-in component  ->  ALLOWED_SHADCN_COMPONENTS, unchanged, untouched
    external component  ->  RegistryInstallRequest, which carries a *resolved
                            locator*, never a caller-supplied URL

The flow is one-directional and cannot be short-circuited:

    normalized identity
        -> application-owned resolver  (REGISTRY_LOCATORS)
        -> validated canonical locator
        -> RegistryInstallRequest
        -> pinned shadcn CLI
        -> contained materialization + postcondition verification

**There is deliberately no generic URL install.** :func:`resolve_registry_locator`
accepts a ``(source, component_id)`` PAIR and looks it up in a closed table; it
has no parameter through which a raw URL could arrive. A model, a corpus, a
prompt, or a registry response therefore cannot become a locator -- they can
only contribute a component identity that the application already knows.

**Required dependencies are closed too.** A component may declare it needs
``gsap``/``three``/``lenis``; anything else makes that component NOT
installable rather than widening the allowlist. Upstream metadata is untrusted
input and is treated as such: it proposes, the application decides.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Sequence, Tuple

from app.core.design_install import (
    ALLOWED_SHADCN_COMPONENTS,
    DEPENDENCY_PACKAGES,
    INSTALL_FAILED,
    REASON_COMPONENT_NOT_ALLOWED,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registry sources -- a CLOSED set
# ---------------------------------------------------------------------------

SOURCE_SHADCN_BUILTIN = "shadcn_builtin"
SOURCE_TWENTY_FIRST = "twenty_first"
SOURCE_REACT_BITS = "react_bits"

REGISTRY_SOURCES: Tuple[str, ...] = (
    SOURCE_SHADCN_BUILTIN,
    SOURCE_TWENTY_FIRST,
    SOURCE_REACT_BITS,
)

#: Upstream host allowlist. A locator whose host is not listed here is not a
#: canonical locator, whatever else is true about it. Recorded explicitly so a
#: future source addition has to name its host rather than inherit one.
REGISTRY_HOSTS: Dict[str, str] = {
    SOURCE_TWENTY_FIRST: "21st.dev",
    SOURCE_REACT_BITS: "reactbits.dev",
}


# ---------------------------------------------------------------------------
# Dependency requirements -- closed, application-owned
# ---------------------------------------------------------------------------
#
# Upstream component metadata names its dependencies as free-form package
# strings. Those are UNTRUSTED INPUT. They are never forwarded to a package
# manager: each is mapped to an allowlisted dependency id, and an id this
# application does not own makes the whole component non-installable.

#: Canonical package name -> dependency id. Only these three exist because only
#: these three are in ``DEPENDENCY_PACKAGES``.
PACKAGE_TO_DEPENDENCY_ID: Dict[str, str] = {
    package: dependency_id
    for dependency_id, package in DEPENDENCY_PACKAGES.items()
}


def resolve_dependency_requirements(
    declared: Sequence[str],
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Map declared package names to dependency ids.

    Returns ``(known, unknown)``. A non-empty ``unknown`` means the component
    cannot be installed: it wants something this application does not own, and
    the only safe response is refusal rather than adding a row to the allowlist
    because a remote catalog asked.
    """
    known: list[str] = []
    unknown: list[str] = []
    for name in declared:
        if not isinstance(name, str):
            unknown.append("<non-string>")
            continue
        dependency_id = PACKAGE_TO_DEPENDENCY_ID.get(name.strip())
        if dependency_id is None:
            unknown.append(name.strip())
        else:
            known.append(dependency_id)
    return tuple(sorted(set(known))), tuple(sorted(set(unknown)))


# ---------------------------------------------------------------------------
# The canonical locator table
# ---------------------------------------------------------------------------
#
# Application-owned and closed. A row exists only where a human has reviewed the
# exact upstream locator for that component. Anything not in this table has NO
# locator and is therefore not installable -- which is the default that keeps an
# unreviewed component out of a build.

#: source -> canonical path suffix template. The placeholders are
#: ``{component}`` (a validated component identity) and ``{variant}`` (an
#: application-owned registry variant, where the source has variants). Both are
#: validated before substitution by :func:`_format_locator`.
#:
#: **React Bits carries a variant, and that is verified, not assumed.** Upstream's
#: own agent documentation states the shadcn install form is
#: ``https://reactbits.dev/r/<Component>-<LANG>-<STYLE>`` with ``<LANG>`` in
#: ``JS|TW``... concretely ``JS``/``TS`` and ``<STYLE>`` in ``CSS|TW``. Probed
#: live: ``/r/SplitText-JS-CSS`` returns a real ``registry-item`` JSON document,
#: while the bare ``/r/SplitText`` returns the marketing HTML page -- an HTML
#: document handed to ``shadcn add`` would not be a registry item at all.
_LOCATOR_TEMPLATES: Dict[str, str] = {
    SOURCE_TWENTY_FIRST: "https://21st.dev/r/{component}",
    SOURCE_REACT_BITS: "https://reactbits.dev/r/{component}-{variant}",
}

#: Registry variants React Bits publishes, as the two-part ``LANG``/``STYLE``
#: suffix its own docs specify. Application-owned and closed: a variant is never
#: taken from a remote payload, because the payload proposes and this table
#: decides. ``TS`` + ``TW`` (TypeScript + Tailwind) is the default this
#: application installs, matching the shadcn/Tailwind toolchain the generated
#: sites use.
REACT_BITS_VARIANT = "TS-TW"

#: Reviewed components per source. Kept small and explicit on purpose: an
#: allowlist that grew to mirror the whole upstream catalog would stop being a
#: review and become a copy.
#:
#: Exactly ONE React Bits component is reviewed so far, ``SplitText``. Its
#: canonical locator ``https://reactbits.dev/r/SplitText-TS-TW`` was probed and
#: confirmed to serve a real shadcn ``registry-item`` document whose declared
#: dependencies are ``gsap`` and ``@gsap/react``. Both are in the closed
#: dependency allowlist, which is why this component -- and only reviewed
#: components like it -- is installable.
_APPROVED_COMPONENTS: Dict[str, frozenset] = {
    SOURCE_TWENTY_FIRST: frozenset(),
    SOURCE_REACT_BITS: frozenset({"SplitText"}),
}

#: Component identities are PascalCase (React Bits' own convention, e.g.
#: ``SplitText``) or kebab/lowercase slugs. Anything containing a path
#: separator, a scheme, or a host is rejected -- those are URL fragments
#: masquerading as an identity.
_COMPONENT_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:[A-Z][a-z0-9]*)*$")

#: Slug form for the lowercase/kebab identifiers some registries use.
_COMPONENT_SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


def component_id_is_well_formed(component_id: object) -> bool:
    """Whether ``component_id`` is a bare identity, not a URL fragment.

    Deliberately strict and deliberately NOT a URL check: a value like
    ``https://evil.example/r/x`` fails here because it contains ``:``, ``/`` and
    ``.``, so it can never be treated as an identity and substituted into a
    locator.
    """
    if not isinstance(component_id, str) or not component_id or len(component_id) > 64:
        return False
    return bool(_COMPONENT_ID_RE.match(component_id) or _COMPONENT_SLUG_RE.match(component_id))


def _format_locator(source: str, component_id: str) -> Optional[str]:
    """Substitute ``component_id`` into ``source``'s template, or return ``None``.

    The component is validated BEFORE substitution, and the result is
    re-validated as a whole against the source's host. Both halves matter: the
    first stops traversal at the identity, the second stops a template that was
    edited to point somewhere else.

    ``{variant}`` is filled from :data:`REACT_BITS_VARIANT`, an
    application-owned constant -- never from the caller and never from a remote
    payload. A remote catalog proposing a different variant does not get to
    choose one; it cannot reach this function with a URL at all.
    """
    template = _LOCATOR_TEMPLATES.get(source)
    if template is None or not component_id_is_well_formed(component_id):
        return None

    try:
        locator = template.format(component=component_id, variant=REACT_BITS_VARIANT)
    except (KeyError, IndexError):
        logger.error("A registry locator template has an unexpected placeholder; refusing.")
        return None

    expected_host = REGISTRY_HOSTS.get(source)
    if not expected_host or not locator.startswith(f"https://{expected_host}/"):
        logger.error("A registry locator resolved outside its source's host; refusing.")
        return None

    # The tail must be EXACTLY what this source's documented form is, and the
    # expected string is rebuilt from host + tail rather than read back out of
    # the template. A template edited to append a suffix of its own would
    # otherwise make this check agree with itself.
    expected_tail = (
        f"{component_id}-{REACT_BITS_VARIANT}"
        if source == SOURCE_REACT_BITS
        else component_id
    )
    if locator != f"https://{expected_host}/r/{expected_tail}":
        logger.error(
            "A registry locator did not match its source's canonical form; "
            "refusing."
        )
        return None
    return locator


def resolve_registry_locator(source: str, component_id: str) -> Optional[str]:
    """The canonical locator for ``(source, component_id)``, or ``None``.

    **This is the only function that can produce an installable locator**, and
    it has no parameter through which a URL could be passed. Callers supply an
    identity; whether that identity is approved here is the security decision.
    """
    if not isinstance(source, str) or source not in REGISTRY_SOURCES:
        return None

    if source == SOURCE_SHADCN_BUILTIN:
        # Built-ins never carry a locator: they are named directly and remain
        # governed by ALLOWED_SHADCN_COMPONENTS.
        return None

    if component_id not in _APPROVED_COMPONENTS.get(source, frozenset()):
        return None
    return _format_locator(source, component_id)


def approved_registry_components(source: str) -> Tuple[str, ...]:
    """Every reviewed component for ``source``, in stable order."""
    if not isinstance(source, str):
        return ()
    return tuple(sorted(_APPROVED_COMPONENTS.get(source, frozenset())))


# ---------------------------------------------------------------------------
# The typed request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegistryInstallRequest:
    """One reviewed, application-owned intent to materialize a component.

    Constructed ONLY from a resolved locator, and the constructor rejects
    anything else -- including a locator whose host does not match its declared
    source, and a raw URL handed in where an identity belongs. That makes
    "a raw external URL cannot become a registry install" a property of
    construction rather than of every call site remembering to check.

    ``required_dependency_ids`` has already been mapped through the closed
    allowlist by :func:`resolve_dependency_requirements`; an unknown
    requirement is refused at the module boundary instead, so it can never
    appear here.
    """

    source: str
    component_id: str
    registry_locator_id: str
    required_dependency_ids: Tuple[str, ...] = ()

    @property
    def is_builtin(self) -> bool:
        return self.source == SOURCE_SHADCN_BUILTIN

    def __post_init__(self) -> None:
        if self.source not in REGISTRY_SOURCES:
            raise ValueError(f"unknown registry source: {self.source!r}")
        if not isinstance(self.component_id, str) or not self.component_id:
            raise ValueError("registry requests require a component id")

        if self.is_builtin:
            # Built-in components are named, not located. A locator here would
            # mean an external URL had been routed through the built-in path,
            # which is exactly the conflation this type exists to prevent.
            if self.registry_locator_id:
                raise ValueError("a shadcn builtin must not carry a registry locator")
            if self.component_id not in ALLOWED_SHADCN_COMPONENTS:
                raise ValueError(
                    f"component is not an approved shadcn builtin: {self.component_id!r}"
                )
        else:
            if not isinstance(self.registry_locator_id, str) or not self.registry_locator_id:
                raise ValueError("an external registry request requires a locator")
            if resolve_registry_locator(self.source, self.component_id) != self.registry_locator_id:
                raise ValueError(
                    "registry locator does not match the application's canonical "
                    "locator for this source and component"
                )

        for dependency_id in self.required_dependency_ids:
            if dependency_id not in DEPENDENCY_PACKAGES:
                raise ValueError(
                    f"required dependency is not allowlisted: {dependency_id!r}"
                )

    def to_dict(self) -> Dict[str, object]:
        """Serializable. A locator is application-owned, not caller data, so it
        is safe to include; no credential or path appears."""
        return {
            "source": self.source,
            "component_id": self.component_id,
            "registry_locator_id": self.registry_locator_id,
            "required_dependency_ids": list(self.required_dependency_ids),
        }


@dataclass(frozen=True)
class RegistryRequestOutcome:
    """The result of turning a request into something installable."""

    ok: bool
    request: Optional[RegistryInstallRequest]
    #: Application-owned reason. Static text only -- never a locator, never a
    #: component id from untrusted metadata.
    reason: str
    dependency_ids: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, object]:
        return {
            "ok": self.ok,
            "request": self.request.to_dict() if self.request else None,
            "reason": self.reason,
            "dependency_ids": list(self.dependency_ids),
        }


REASON_REQUEST_OK = "registry request resolved to an application-owned locator"
REASON_SOURCE_UNKNOWN = "the registry source is not one this application integrates"
REASON_COMPONENT_UNKNOWN = (
    "the component has no approved canonical locator for this source"
)
REASON_DEPENDENCY_UNKNOWN = (
    "the component requires a dependency outside the closed allowlist"
)
REASON_COMPONENT_NOT_BUILTIN = "the component is not an approved shadcn builtin"


def build_registry_request(
    source: str, component_id: str, *, declared_dependencies: Sequence[str] = ()
) -> RegistryRequestOutcome:
    """Turn ``(source, component_id)`` into a validated install request.

    The single entry point. It is deliberately the ONLY place that assembles a
    :class:`RegistryInstallRequest`, so the checks below cannot be skipped by a
    caller constructing one directly -- the constructor re-verifies them, and
    this function is what maps external dependency NAMES to allowlisted ids.

    ``declared_dependencies`` arrives from upstream metadata and is therefore
    untrusted: an unknown name refuses the whole component rather than being
    added to the allowlist.
    """
    if not isinstance(source, str) or source not in REGISTRY_SOURCES:
        return RegistryRequestOutcome(ok=False, request=None, reason=REASON_SOURCE_UNKNOWN)

    if not component_id_is_well_formed(component_id):
        return RegistryRequestOutcome(
            ok=False, request=None, reason=REASON_COMPONENT_UNKNOWN
        )

    dependency_ids, unknown = resolve_dependency_requirements(declared_dependencies)
    if unknown:
        # Refuse the COMPONENT, not just the dependency: shipping half a
        # component's requirements is worse than shipping none of it.
        logger.warning(
            "Refusing a registry component requiring non-allowlisted dependencies."
        )
        return RegistryRequestOutcome(
            ok=False, request=None, reason=REASON_DEPENDENCY_UNKNOWN
        )

    if source == SOURCE_SHADCN_BUILTIN:
        if component_id not in ALLOWED_SHADCN_COMPONENTS:
            return RegistryRequestOutcome(
                ok=False, request=None, reason=REASON_COMPONENT_NOT_BUILTIN
            )
        return RegistryRequestOutcome(
            ok=True,
            request=RegistryInstallRequest(
                source=source,
                component_id=component_id,
                registry_locator_id="",
                required_dependency_ids=dependency_ids,
            ),
            reason=REASON_REQUEST_OK,
            dependency_ids=dependency_ids,
        )

    locator = resolve_registry_locator(source, component_id)
    if locator is None:
        return RegistryRequestOutcome(
            ok=False, request=None, reason=REASON_COMPONENT_UNKNOWN
        )

    return RegistryRequestOutcome(
        ok=True,
        request=RegistryInstallRequest(
            source=source,
            component_id=component_id,
            registry_locator_id=locator,
            required_dependency_ids=dependency_ids,
        ),
        reason=REASON_REQUEST_OK,
        dependency_ids=dependency_ids,
    )


def build_registry_argv(request: Optional[RegistryInstallRequest]) -> Tuple[str, ...]:
    """The component arguments for a request, without any manager prefix.

    Kept separate from argv construction because the prefix is manager- and
    CLI-specific (:func:`app.core.design_install.build_pinned_cli_prefix`) while
    the component list is a property of the request.

    Returns ``()`` for a builtin (which is named directly and stays governed by
    ``ALLOWED_SHADCN_COMPONENTS``) AND for a missing request. The ``None`` case
    matters: a refused component must produce no argv through this helper, so
    "unapproved => nothing runs" holds even if a caller forgets to check
    ``outcome.ok`` first. Making that a crash instead would push every call site
    toward an ``if`` that could be skipped.
    """
    if request is None or request.is_builtin or not request.registry_locator_id:
        return ()
    return (request.registry_locator_id,)


__all__ = [
    "REGISTRY_HOSTS",
    "REGISTRY_SOURCES",
    "REASON_COMPONENT_NOT_BUILTIN",
    "REASON_COMPONENT_UNKNOWN",
    "REASON_DEPENDENCY_UNKNOWN",
    "REASON_REQUEST_OK",
    "REASON_SOURCE_UNKNOWN",
    "SOURCE_REACT_BITS",
    "SOURCE_SHADCN_BUILTIN",
    "SOURCE_TWENTY_FIRST",
    "RegistryInstallRequest",
    "RegistryRequestOutcome",
    "approved_registry_components",
    "build_registry_argv",
    "build_registry_request",
    "component_id_is_well_formed",
    "resolve_dependency_requirements",
    "resolve_registry_locator",
]