"""Design resource activation capability (Batch D3a.5).

This module answers one question: **for each declared design resource, which
capabilities actually exist on this host right now?**

It exists because the pre-D3a.5 model had a single boolean per resource, and a
single boolean is the wrong shape. The resources have genuinely different and
*simultaneously true* capabilities:

* 21st.dev requires a credential for BOTH its discovery and its retrieval: its
  only machine surface is the authenticated REST API (verified live: HTTP 401
  without a Bearer key) and its public ``llms.txt`` publishes no
  component-identity schema. There is no free tier to report.
* Refero's bundled local craft references are usable with no account at all,
  while its live MCP research is a paid enhancement.
* Impeccable may be locally provisioned with a working critic while being
  entirely irrelevant to a project build.

Forcing those into one enum (``search_available`` XOR ``authenticated`` XOR
``install_available``) forces a lie: whichever single state is chosen discards a
true capability. So this module is deliberately **multi-dimensional**.

**What this module does NOT do.** It is not an installer, not a fetcher, not a
browser, not a second FRONTEND, and not a repair loop. In particular:

    * It performs NO network access, NO subprocess, and NO installation.
    * It only ever inspects local state and *credential presence*.

That last distinction is load-bearing. Credential **presence** (does a variable
exist and is it non-empty?) is a local boolean; the value is a secret and is
never read, copied, logged, or returned. This is why capability resolution can
run at startup on every host without making startup depend on a remote answer --
the property ``tests/test_design_resources.py`` and
``tests/test_design_resource_activation.py`` enforce with socket-ban fixtures.

**Presence never implies usability.** ``authentication_present=True`` says a
credential was found; it does NOT claim the credential works, and it does not
make ``retrieval_available`` true on its own. The per-resource adapter decides
that, and it may still degrade if the credential is rejected downstream. That is
why ``authentication_required`` and ``authentication_present`` are separate
fields rather than one tri-state: "needs a credential" and "has a credential"
answer different questions.

Every ``reasons`` entry is a static, sanitized label from
:data:`ACTIVATION_REASONS`. Nothing read from the filesystem, the environment,
or a remote service is ever placed in a reason, so the report is safe to log and
safe to render into a prompt.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.core.design_registry import (
    SOURCE_REACT_BITS,
    SOURCE_TWENTY_FIRST,
    approved_registry_components,
)
from app.core.design_resources import (
    DesignResource,
    DesignResourceManifest,
    design_profile_skills_dir,
)


def _has_live_discovery_adapter(source: str) -> bool:
    """Whether a REAL, executable discovery adapter exists FOR ``source``.

    Answered from the application's own module surface, NOT from a network
    probe: importing this module must stay local, offline and socket-free, so
    "is there an adapter" has to be a static fact.

    **Per-source, deliberately.** A source's discovery surface may be a plain
    index endpoint (``CATALOG_ENDPOINTS``) OR an authenticated REST search
    (``CATALOG_SEARCH_ENDPOINTS``). A source-agnostic check would confer one
    source's capability on another -- reporting 21st's adapter available because
    React Bits happens to populate a table, or vice versa. The question is
    always "does THIS source have an endpoint this module can build a request
    for", so both tables are consulted for the SAME source.

    ``design_catalog_fetch`` is the adapter. It is IMPORTED HERE on purpose:
    the import is local and pure (stdlib plus the offline normalizer), and it
    makes the dependency explicit -- if that module is removed or fails to
    import, discovery correctly reports itself absent instead of claiming a
    capability that no longer exists.
    """
    try:
        from app.core import design_catalog_fetch as _fetch
    except Exception:  # pragma: no cover - import failure is the answer
        return False
    if not getattr(_fetch, "discover_catalog", None):
        return False
    index_endpoints = getattr(_fetch, "CATALOG_ENDPOINTS", None) or {}
    search_endpoints = getattr(_fetch, "CATALOG_SEARCH_ENDPOINTS", None) or {}
    return source in index_endpoints or source in search_endpoints

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Static, sanitized reasons -- a CLOSED set
# ---------------------------------------------------------------------------
#
# These are the ONLY strings that may appear in ``ResourceActivationCapability.reasons``.
# They describe a CLASS of outcome, never a path, never file content, never an
# environment value, and never a remote response body.

REASON_SKILL_NOT_PROVISIONED = (
    "the resource's local skill has not been provisioned into this profile"
)
REASON_SKILL_ARTIFACTS_MISSING = (
    "the local skill is present but its declared artifacts could not be verified"
)
REASON_LOCALLY_PROVISIONED = "the local skill is provisioned and its artifacts verified"

REASON_AUTH_REQUIRED = (
    "this capability requires a credential that is not configured on this host"
)
REASON_AUTH_PRESENT = "a credential is present; capability is not proven until used"

#: An OPTIONAL enhancement is unmet. The capability being reported is FULLY
#: usable without it -- this is not a blocker and not a degradation, which is
#: exactly why it needs its own label rather than reusing REASON_AUTH_REQUIRED.
REASON_AUTH_OPTIONAL = (
    "an optional paid enhancement is not configured; the free baseline is "
    "unaffected"
)

REASON_NO_OFFICIAL_MECHANISM = (
    "no official machine-consumable mechanism is configured for this resource"
)
REASON_CATALOG_REACHABLE = (
    "the official catalog is reachable without authentication for metadata"
)
REASON_NO_LIVE_ADAPTER = (
    "no live discovery adapter is wired for this resource in this build"
)
REASON_NO_REVIEWED_COMPONENT = (
    "no reviewed component is allowlisted for install from this resource"
)
REASON_CLI_PINNED = "a pinned, application-owned CLI is available for this resource"
REASON_ENGINE_VERIFIED = (
    "the provisioned skill's detector engine is present and verified"
)
REASON_ENGINE_MISSING = (
    "the provisioned skill has no detector engine; no download is attempted"
)
REASON_ENGINE_DEGRADED = (
    "the detector engine is present but its parser runtime is unavailable; the "
    "scan is degraded and NOT an authoritative clean"
)
REASON_ENGINE_FULL_QUALITY = (
    "the detector engine and its parser runtime are present; the scan is "
    "full-quality"
)

#: Exhaustive. A reason outside this set is a bug, and callers switch on these
#: strings; a new spelling would be a new, unhandled state.
ACTIVATION_REASONS: frozenset = frozenset(
    {
        REASON_SKILL_NOT_PROVISIONED,
        REASON_SKILL_ARTIFACTS_MISSING,
        REASON_LOCALLY_PROVISIONED,
        REASON_AUTH_REQUIRED,
        REASON_AUTH_PRESENT,
        REASON_AUTH_OPTIONAL,
        REASON_NO_OFFICIAL_MECHANISM,
        REASON_CATALOG_REACHABLE,
        REASON_NO_LIVE_ADAPTER,
        REASON_NO_REVIEWED_COMPONENT,
        REASON_CLI_PINNED,
        REASON_ENGINE_VERIFIED,
        REASON_ENGINE_MISSING,
        REASON_ENGINE_DEGRADED,
        REASON_ENGINE_FULL_QUALITY,
    }
)


# ---------------------------------------------------------------------------
# Engine resolution -- the VERIFIED upstream contract
# ---------------------------------------------------------------------------
#
# Verified against the official release (see `config/design_resources.yaml`):
# pbakaus/impeccable `skill-v4.1.0` ships exactly one asset, `universal.zip`
# (sha256:a54d837f086ff2036ab0cf2cc2499249362d38fa27d42feb07d6248dfdd64a11),
# whose Hermes layout contains NO `scripts/bin/`, NO `scripts/impeccable`
# launcher, and no per-platform native binary.
#
# The detector is a Node ESM entrypoint, `scripts/detect.mjs`, which re-exports
# `detectCli` from `detector/detect-antipatterns.mjs`. It is therefore
# CROSS-PLATFORM: the same relative path is correct on every supported host, so
# there is no `(system, machine)` mapping to get wrong.
#
# The previous revision resolved `scripts/bin/<os>-<arch>/impeccable` from a
# closed platform table. That layout does not exist in the supported
# distribution, so it reported `critic_available=False` on a fully and correctly
# provisioned skill -- a fabricated contract, which is worse than an honest
# absence because it also sent the manifest looking for a file upstream never
# shipped.
#
# The Node interpreter is resolved from the application-owned pinned CLI table
# (the same mechanism that runs shadcn and transitions-dev), never from an
# arbitrary PATH binary and never from an npm shim inside the project.

#: Skill-root-relative entrypoints, in the order the launcher chain tries them.
#: The first is the shipped Hermes entrypoint; the second is the detector facade
#: it imports. Both must exist, so a partial provisioning fails honestly.
ENGINE_ENTRYPOINTS: Tuple[str, ...] = (
    "scripts/detect.mjs",
    "scripts/detector/detect-antipatterns.mjs",
)


def engine_relative_paths() -> Tuple[str, ...]:
    """Every skill-root-relative path the engine consists of."""
    return ENGINE_ENTRYPOINTS


def resolve_engine_path(skill_root: Path) -> Optional[Path]:
    """The engine entrypoint inside ``skill_root``, or ``None``.

    Every entrypoint must be a contained, existing, non-empty regular file, so a
    half-provisioned skill (docs without a detector, or a detector without its
    facade) is an honest absence rather than a runtime failure. Containment is
    checked on the RESOLVED path: a symlink inside the skill pointing outside it
    is refused before anything executes.
    """
    root = Path(skill_root)
    try:
        resolved_root = root.resolve()
    except (OSError, RuntimeError):
        return None

    for relative in ENGINE_ENTRYPOINTS:
        path = root / relative
        # A missing, EMPTY, or non-regular entrypoint is not an engine. The
        # chain is checked in full so a half-provisioned skill -- docs without
        # a detector, or an entrypoint without the facade it imports -- is an
        # honest absence rather than something that fails at run time.
        if not _is_readable_file(path):
            return None
        try:
            # Containment on the RESOLVED path: a symlink inside the skill
            # pointing outside it is refused before anything executes.
            Path(path).resolve().relative_to(resolved_root)
        except (OSError, ValueError, RuntimeError):
            return None
    return root / ENGINE_ENTRYPOINTS[0]


#: The directory inside the skill where the parser runtime is provisioned. The
#: modules are resolved by Node's normal ``node_modules`` lookup from the skill
#: scripts, so they live at ``<skill>/node_modules``. Provisioning writes them
#: there; a build never does.
PARSER_RUNTIME_DIRNAME = "node_modules"

#: The parser modules the full static-HTML engine imports, verified against the
#: shipped ``detect-html.mjs`` (``import('htmlparser2')`` etc.). Mirrors
#: ``design_install.IMPECCABLE_PARSER_PACKAGE_PINS`` by package name; kept as a
#: tuple here so this module does not import the install layer at load time.
PARSER_RUNTIME_PACKAGES: Tuple[str, ...] = (
    "htmlparser2",
    "css-select",
    "css-tree",
    "domutils",
)


def parser_runtime_is_provisioned(skill_root: Path) -> bool:
    """Whether every parser module is present under the skill's ``node_modules``.

    Presence is checked as a directory per package (``node_modules/<pkg>``); the
    module itself is a ``.mjs``/``.js`` inside it, but a package directory that
    exists is what Node's resolver needs to resolve the bare import. A missing
    directory means the engine would print DEGRADED and fall back to regex, so
    this is the difference between a full-quality scan and an undercount.

    Offline and pure: filesystem inspection only, no import, no execution.
    """
    modules_dir = Path(skill_root) / PARSER_RUNTIME_DIRNAME
    try:
        if not modules_dir.is_dir():
            return False
    except OSError:
        return False
    for package in PARSER_RUNTIME_PACKAGES:
        try:
            if not (modules_dir / package).is_dir():
                return False
        except OSError:
            return False
    return True


def engine_quality(skill_root: Path) -> str:
    """``"full"`` | ``"degraded"`` | ``"missing"`` for the engine at ``skill_root``.

    ``missing`` -- the entrypoints themselves are absent.
    ``degraded`` -- the engine exists but the parser runtime does not, so a scan
    is an undercount and a clean result is NOT authoritative.
    ``full`` -- engine AND parser runtime present; a scan is full-quality.
    """
    if resolve_engine_path(skill_root) is None:
        return "missing"
    if not parser_runtime_is_provisioned(skill_root):
        return "degraded"
    return "full"


# ---------------------------------------------------------------------------
# Credential PRESENCE -- never a credential value
# ---------------------------------------------------------------------------


def credential_present(
    names: Sequence[str], *, source: Optional[Mapping[str, str]] = None
) -> bool:
    """Whether any of ``names`` is configured, WITHOUT reading its value.

    Presence is a local boolean. The value is a secret, so it is never returned,
    logged, or stored; only ``True``/``False`` leaves this function. A variable
    set to the empty string counts as absent, because an empty credential is
    indistinguishable from a missing one to every consumer and treating it as
    present would report a capability that cannot work.
    """
    env = os.environ if source is None else source
    for name in names:
        value = env.get(name)
        if isinstance(value, str) and value.strip():
            return True
    return False


# ---------------------------------------------------------------------------
# The capability record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceActivationCapability:
    """One resource's activation capability, across all dimensions at once.

    Immutable and fully serializable. Every field is an independent axis; none
    of them implies another:

    ``discovery_available``
        Something can be listed/searched (a local corpus, a free catalog, a
        pinned CLI that can enumerate).
    ``retrieval_available``
        Real content can be fetched. Usually a subset of discovery in practice,
        but kept separate because "we can search it" and "we can read it" are
        different promises and upstream may split them. (21st does NOT split
        them: both its discovery and its retrieval require a credential.)
    ``install_available``
        A selected component can be materialized into a project. ``False`` for
        every resource that is inspiration-only.
    ``critic_available``
        A deterministic reviewer can be invoked. Only Impeccable sets this.
    ``authentication_required``
        Using (some of) this resource needs a credential. It does NOT say which
        capability is gated, because usually only retrieval is.
    ``authentication_present``
        A credential was found. It does NOT mean it works.
    ``locally_provisioned``
        A verified local skill exists. Impeccable and Refero use this.
    ``degraded``
        The resource is usable but reduced. ``False`` for a resource that is
        working as designed -- including one whose paid enhancement is absent,
        because the free baseline is intact and reporting that as a degradation
        would train operators to ignore real degradations.

    ``reasons`` are static labels, de-duplicated and sorted, so the
    serialization is deterministic.
    """

    resource_id: str
    discovery_available: bool = False
    retrieval_available: bool = False
    install_available: bool = False
    critic_available: bool = False
    #: The critic can be INVOKED and its findings parsed, but it is running the
    #: DEGRADED (regex-fallback) path, so its findings are an UNDERCOUNT and a
    #: clean result is NOT authoritative. Distinct from ``critic_available``:
    #: a degraded critic is callable but must never be treated as a full-quality
    #: pass. Impeccable sets this when the parser runtime is absent.
    critic_degraded: bool = False
    authentication_required: bool = False
    #: An OPTIONAL, separately-gated enhancement is unmet. Distinct from
    #: `authentication_required`: that one says the capability being reported is
    #: BLOCKED, while this says the reported capability is fully usable and
    #: something extra simply is not switched on. Conflating the two is what
    #: made Refero's free local baseline report itself as blocked.
    authentication_optional: bool = False
    authentication_present: bool = False
    locally_provisioned: bool = False
    degraded: bool = False
    reasons: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        unknown = [r for r in self.reasons if r not in ACTIVATION_REASONS]
        if unknown:
            raise ValueError(f"unregistered activation reason(s): {sorted(unknown)}")

    @property
    def usable(self) -> bool:
        """Whether ANY capability is present.

        The honest answer to "is there anything here at all", distinct from
        ``available`` in the D0 sense (which meant one resource-specific thing).
        Used by D2 to decide whether a reference is reachable at all.
        """
        return bool(
            self.discovery_available
            or self.retrieval_available
            or self.install_available
            or self.critic_available
        )

    def to_dict(self) -> Dict[str, Any]:
        """Deterministic, bounded serialization. No paths, no secrets."""
        return {
            "resource_id": self.resource_id,
            "discovery_available": self.discovery_available,
            "retrieval_available": self.retrieval_available,
            "install_available": self.install_available,
            "critic_available": self.critic_available,
            "critic_degraded": self.critic_degraded,
            "authentication_required": self.authentication_required,
            "authentication_optional": self.authentication_optional,
            "authentication_present": self.authentication_present,
            "locally_provisioned": self.locally_provisioned,
            "degraded": self.degraded,
            "usable": self.usable,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class DesignActivationReport:
    """Bounded activation state for every declared resource."""

    ok: bool
    capabilities: Dict[str, ResourceActivationCapability]
    failures: List[str]
    degraded: List[str]

    def get(self, resource_id: str) -> Optional[ResourceActivationCapability]:
        return self.capabilities.get(resource_id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "capabilities": {
                rid: cap.to_dict() for rid, cap in sorted(self.capabilities.items())
            },
            "failures": sorted(self.failures),
            "degraded": sorted(self.degraded),
        }

    def summary(self) -> str:
        """One bounded line. Ids and booleans only."""
        return ", ".join(
            f"{rid}={'usable' if cap.usable else 'absent'}"
            for rid, cap in sorted(self.capabilities.items())
        )


# ---------------------------------------------------------------------------
# Local skill verification
# ---------------------------------------------------------------------------


def _is_readable_file(path: Path) -> bool:
    """True when ``path`` is a readable, non-empty regular file.

    Zero bytes counts as unreadable: a placeholder file is precisely the state a
    capability check exists to catch.
    """
    try:
        if not path.is_file():
            return False
        if path.stat().st_size <= 0:
            return False
        with path.open("rb"):
            return True
    except OSError:
        return False


def verify_local_skill(
    hermes_home: Path, resource: DesignResource
) -> Tuple[bool, bool]:
    """``(present, artifacts_verified)`` for a profile-skill resource.

    Two distinct questions, deliberately not collapsed:

    * **present** -- the skill directory and a non-empty ``SKILL.md`` exist.
      A skill whose only artifact is ``SKILL.md`` is a placeholder directory.
    * **artifacts_verified** -- every declared ``data_entries`` path exists as a
      contained regular file. Re-verified at read time rather than trusting a
      preflight that ran in another process against possibly different
      filesystem state.

    Containment is checked on the RESOLVED paths, so a symlink inside the skill
    directory pointing outside it fails here rather than being followed.
    """
    if resource.skill_name is None:
        return False, False

    skill_dir = design_profile_skills_dir(hermes_home) / resource.skill_name
    if not skill_dir.is_dir():
        return False, False
    if not _is_readable_file(skill_dir / "SKILL.md"):
        return True, False

    for entry in resource.data_entries:
        entry_path = skill_dir / entry
        if not _is_readable_file(entry_path):
            return True, False
        try:
            Path(entry_path).resolve().relative_to(Path(skill_dir).resolve())
        except (OSError, ValueError):
            return True, False
    return True, True


def _dedupe(reasons: Sequence[str]) -> Tuple[str, ...]:
    return tuple(sorted(set(reasons)))


# ---------------------------------------------------------------------------
# Per-resource activation
# ---------------------------------------------------------------------------
#
# A table of per-id resolvers, each returning a capability. The generic path is
# the honest default: a declared-but-unactivated resource reports nothing
# available and says so. That is the anti-placeholder property -- a resource
# cannot acquire capability by existing in the manifest.

#: On-demand resources that have a real install mechanism. ``shadcn`` is a
#: registry invoked through the pinned CLI; the other three are exact-pinned npm
#: dependencies. Their capability is INSTALL availability, which D2 selection
#: then gates per project.
INSTALLABLE_ON_DEMAND: frozenset = frozenset({"shadcn", "gsap", "three", "lenis"})

#: Credential environment variable names per resource. EXACTLY these names are
#: consulted, and only their PRESENCE is read.
#:
#: These are third-party research/CI credentials, never deployment credentials
#: (Vercel/GitHub/Telegram): a design corpus has no business holding a
#: release credential, so none of these names may ever name one.
CREDENTIAL_ENV_NAMES: Dict[str, Tuple[str, ...]] = {
    "refero": ("REFERO_API_KEY",),
    "twenty_first": ("TWENTY_FIRST_API_KEY", "TWENTYFIRST_API_KEY"),
    "react_bits": (),
    "transitions_dev": (),
    "impeccable": (),
}


def _absent(resource_id: str, reasons: Sequence[str]) -> ResourceActivationCapability:
    return ResourceActivationCapability(
        resource_id=resource_id, reasons=_dedupe(reasons)
    )


def _local_skill_capability(
    hermes_home: Path,
    resource: DesignResource,
    *,
    resource_id: str,
    paid_credential: Sequence[str] = (),
) -> ResourceActivationCapability:
    """A resource whose BASELINE capability is a provisioned local skill.

    Used by Refero. The distinction that matters: the local craft references
    are the PRIMARY, FREE capability, and any paid live-research tier is an
    OPTIONAL enhancement. So a missing credential leaves the baseline fully
    usable and is NOT a degradation -- reporting it as one would teach
    operators to ignore degradations.
    """
    present, verified = verify_local_skill(hermes_home, resource)

    reasons: List[str] = []
    if not present:
        reasons.append(REASON_SKILL_NOT_PROVISIONED)
        return _absent(resource_id, reasons)
    if not verified:
        reasons.append(REASON_SKILL_ARTIFACTS_MISSING)
        return _absent(resource_id, reasons)

    reasons.append(REASON_LOCALLY_PROVISIONED)

    # The paid enhancement is reported honestly on its OWN axis and never gates
    # the baseline above. `authentication_required` stays False here: the
    # capability being reported -- the local craft references -- needs no
    # credential, and claiming otherwise reports a blocker that does not exist.
    auth_present = credential_present(paid_credential) if paid_credential else False
    if paid_credential:
        reasons.append(
            REASON_AUTH_PRESENT if auth_present else REASON_AUTH_OPTIONAL
        )

    return ResourceActivationCapability(
        resource_id=resource_id,
        discovery_available=True,
        retrieval_available=True,
        install_available=False,
        critic_available=False,
        authentication_required=False,
        authentication_optional=bool(paid_credential) and not auth_present,
        authentication_present=auth_present,
        locally_provisioned=True,
        degraded=False,
        reasons=_dedupe(reasons),
    )


def _catalog_capability(
    resource_id: str,
    *,
    metadata_without_auth: bool,
    retrieval_requires_auth: bool,
    credential_names: Sequence[str],
    install_available: bool,
    adapter_exists: bool,
    reviewed_components: int = 0,
) -> ResourceActivationCapability:
    """A remote catalog resource (21st, React Bits).

    ``metadata_without_auth`` states whether the catalog can be LISTED with no
    credential. It must be the truth about the PRODUCTION adapter, not the
    marketing tier: 21st's real machine surface is an authenticated REST API
    (verified live -- ``/api/v1/components/search`` returns HTTP 401 without a
    Bearer key) and its public ``llms.txt`` publishes no component-identity
    schema, so 21st's discovery is credential-gated. React Bits publishes an
    unauthenticated agent index with an explicit ``CLI:`` marker, so its
    discovery needs no credential.

    Discovery therefore follows the credential WHENEVER the mechanism requires
    one: a resource whose only real surface is authenticated must NOT report
    ``discovery_available=True`` on the strength of a public page that yields no
    identities. The honest state is a bounded credential-required/degraded one.
    """
    auth_required = retrieval_requires_auth
    auth_present = credential_present(credential_names) if credential_names else False

    reasons: List[str] = []
    if not adapter_exists:
        # The declaration alone cannot confer discovery. Without a live
        # adapter nothing can list this catalog, so reporting it would
        # repeat the exact lie this repair removes.
        reasons.append(REASON_NO_LIVE_ADAPTER)
    elif metadata_without_auth:
        reasons.append(REASON_CATALOG_REACHABLE)
    if auth_required:
        reasons.append(
            REASON_AUTH_PRESENT if auth_present else REASON_AUTH_REQUIRED
        )
    if not metadata_without_auth and not auth_required and adapter_exists:
        reasons.append(REASON_NO_OFFICIAL_MECHANISM)
    if install_available and reviewed_components <= 0:
        # An install capability with nothing reviewed is not a capability.
        reasons.append(REASON_NO_REVIEWED_COMPONENT)

    # Discovery is available when the adapter exists AND either the catalog is
    # listable without a credential OR a credential is present. A credentialed
    # mechanism with no credential is a bounded credential-required state, NOT a
    # free-discovery claim.
    discovery = adapter_exists and (metadata_without_auth or auth_present)
    install = install_available and reviewed_components > 0

    return ResourceActivationCapability(
        resource_id=resource_id,
        discovery_available=discovery,
        # Retrieval follows the credential only when it is actually required.
        retrieval_available=discovery and (
            not auth_required or auth_present
        ),
        install_available=install and (not auth_required or auth_present),
        critic_available=False,
        authentication_required=auth_required,
        authentication_present=auth_present,
        locally_provisioned=False,
        # Free metadata working is the designed state; a MISSING adapter OR an
        # unmet required credential is a genuine reduction in what this build
        # can do.
        degraded=not discovery,
        reasons=_dedupe(reasons),
    )


def _pinned_cli_capability(resource_id: str) -> ResourceActivationCapability:
    """A resource whose mechanism is a pinned, application-owned CLI.

    Transitions.dev. No credential for the free tier, so both retrieval and
    install are available; ``install_available`` here means the mechanism
    exists, and D2 selection plus slug normalization still gate any invocation.
    """
    return ResourceActivationCapability(
        resource_id=resource_id,
        discovery_available=True,
        retrieval_available=True,
        install_available=True,
        critic_available=False,
        authentication_required=False,
        authentication_present=False,
        locally_provisioned=False,
        degraded=False,
        reasons=(REASON_CLI_PINNED,),
    )


def _impeccable_capability(
    hermes_home: Path,
    resource: DesignResource,
    *,
    resource_id: str,
    system: str,
    machine: str,
) -> ResourceActivationCapability:
    """Impeccable: provisioned skill PLUS a verified Node detector engine.

    The critic is available only when BOTH hold:

    1. the STABLE skill artifacts verify (pinned by the manifest, cross-platform
       and OS-agnostic), and
    2. the shipped Node detector entrypoints exist as contained regular files.

    The engine is CROSS-PLATFORM, so ``system``/``machine`` are accepted for call
    signature compatibility and deliberately NOT consulted: there is no
    per-platform artifact to resolve, and inventing one would report a correctly
    provisioned skill as unavailable on every host that differs from the one the
    mapping happened to be written on.

    A missing engine yields ``critic_available=False`` with a static reason, and
    explicitly does NOT download anything: no clone, no artifact fetch, no npm
    shim inside the project, no fallback to an arbitrary PATH binary. An absent
    engine is a provisioning state an operator fixes by provisioning the skill,
    and reaching for an interpreter instead would mean executing something that
    was never reviewed.
    """
    del system, machine  # cross-platform engine; nothing to resolve per-OS

    present, verified = verify_local_skill(hermes_home, resource)
    if not present:
        return _absent(resource_id, [REASON_SKILL_NOT_PROVISIONED])
    if not verified:
        return _absent(resource_id, [REASON_SKILL_ARTIFACTS_MISSING])

    skill_root = design_profile_skills_dir(hermes_home) / str(resource.skill_name)
    quality = engine_quality(skill_root)

    if quality == "missing":
        return ResourceActivationCapability(
            resource_id=resource_id,
            discovery_available=True,
            retrieval_available=False,
            locally_provisioned=True,
            reasons=_dedupe(
                [REASON_LOCALLY_PROVISIONED, REASON_ENGINE_MISSING]
            ),
        )

    if quality == "degraded":
        # The engine runs, but its parser runtime is absent, so the detector
        # prints DEGRADED and falls back to regex -- an UNDERCOUNT. The critic
        # is CALLABLE, so `critic_available=True`, but `critic_degraded=True`
        # marks it as NOT authoritative: a clean result here must never be
        # treated as a full-quality pass.
        return ResourceActivationCapability(
            resource_id=resource_id,
            discovery_available=True,
            retrieval_available=True,
            critic_available=True,
            critic_degraded=True,
            install_available=False,
            authentication_required=False,
            authentication_present=False,
            locally_provisioned=True,
            degraded=True,
            reasons=_dedupe(
                [REASON_LOCALLY_PROVISIONED, REASON_ENGINE_DEGRADED]
            ),
        )

    return ResourceActivationCapability(
        resource_id=resource_id,
        discovery_available=True,
        retrieval_available=True,
        critic_available=True,
        critic_degraded=False,
        # Impeccable is a reviewer, never something to install into a project.
        install_available=False,
        authentication_required=False,
        authentication_present=False,
        locally_provisioned=True,
        degraded=False,
        reasons=_dedupe(
            [
                REASON_LOCALLY_PROVISIONED,
                REASON_ENGINE_VERIFIED,
                REASON_ENGINE_FULL_QUALITY,
            ]
        ),
    )


def _on_demand_capability(
    resource_id: str, *, install_available: bool
) -> ResourceActivationCapability:
    """A project-on-demand dependency (``shadcn``, ``gsap``, ``three``, ``lenis``).

    These have NO content to retrieve and no local provisioning requirement: the
    question for them is not "is it available here" but "may it be added to a
    project, and by what mechanism".

    So they report ``install_available`` (the mechanism exists) with every other
    axis false and ``degraded=False``. Reporting them as absent-and-degraded
    would be wrong twice over: it is not a degradation, and a permanent
    unactionable degradation trains operators to ignore real ones -- exactly the
    failure ``not_installed`` was introduced in D0 to avoid.
    """
    return ResourceActivationCapability(
        resource_id=resource_id,
        install_available=install_available,
        reasons=(),
    )


def activate_resource(
    hermes_home: Path,
    resource: DesignResource,
    *,
    system: str,
    machine: str,
) -> ResourceActivationCapability:
    """Resolve ONE resource's activation capability.

    Offline by construction: the only inputs are the filesystem, the manifest
    declaration, and credential PRESENCE. ``system``/``machine`` are passed in
    rather than read here so the platform mapping is a pure function and tests
    need not patch the host.
    """
    resource_id = resource.resource_id

    # Project-on-demand dependencies are decided first: they share a resting
    # state across kinds, and mis-routing one into the content paths would
    # report "no official mechanism" for a resource that has a working one.
    if resource.is_on_demand:
        return _on_demand_capability(
            resource_id, install_available=resource_id in INSTALLABLE_ON_DEMAND
        )

    if resource_id == "ui_ux_pro_max":
        # The required local design-guidance skill. No paid tier, so the whole
        # capability reduces to "is the provisioned skill verified".
        return _local_skill_capability(hermes_home, resource, resource_id=resource_id)

    if resource_id == "refero":
        return _local_skill_capability(
            hermes_home,
            resource,
            resource_id=resource_id,
            paid_credential=CREDENTIAL_ENV_NAMES["refero"],
        )
    if resource_id == "impeccable":
            return _impeccable_capability(
                hermes_home, resource, resource_id=resource_id, system=system, machine=machine
            )
    if resource_id == "twenty_first":
        return _catalog_capability(
            resource_id,
            # VERIFIED LIVE (2026-10): 21st's real machine surface is the
            # authenticated REST API (`GET /api/v1/components/search` -> HTTP 401
            # without a Bearer key), and its public `llms.txt` publishes NO
            # component-identity schema (only route/category links). So there is
            # no unauthenticated component catalog to list: discovery is
            # credential-gated, exactly like retrieval. The fetch adapter uses
            # that REST search endpoint and sends no request without a key.
            # Reporting free discovery would repeat the exact lie this repair
            # removes.
            metadata_without_auth=False,
            retrieval_requires_auth=True,
            credential_names=CREDENTIAL_ENV_NAMES["twenty_first"],
            install_available=True,
            adapter_exists=_has_live_discovery_adapter(SOURCE_TWENTY_FIRST),
            # Nothing is reviewed for 21st, so no component is installable.
            reviewed_components=len(
                approved_registry_components(SOURCE_TWENTY_FIRST)
            ),
        )
    if resource_id == "react_bits":
        # Upstream publishes an agent index and a shadcn registry entry per
        # component; no credential is required for either.
        return _catalog_capability(
            resource_id,
            metadata_without_auth=True,
            retrieval_requires_auth=False,
            credential_names=(),
            install_available=True,
            adapter_exists=_has_live_discovery_adapter(SOURCE_REACT_BITS),
            reviewed_components=len(
                approved_registry_components(SOURCE_REACT_BITS)
            ),
        )
    if resource_id == "transitions_dev":
        return _pinned_cli_capability(resource_id)

    # Generic path: declared but not activated. Honest absence, never a
    # capability inferred from the declaration itself.
    return _absent(resource_id, [REASON_NO_OFFICIAL_MECHANISM])


def activate_design_resources(
    hermes_home: Path,
    manifest: DesignResourceManifest,
    *,
    system: Optional[str] = None,
    machine: Optional[str] = None,
) -> DesignActivationReport:
    """Resolve activation capability for every declared resource.

    ``hermes_home`` and ``manifest`` are supplied by the caller; this module
    never reads ``HERMES_HOME`` itself and never loads the shipped manifest on
    its own, mirroring ``resolve_design_capabilities``.

    Performs no network access, no subprocess, and no installation. Every
    declared resource is resolved -- including one that is absent -- so a
    reduced capability is visible in diagnostics instead of silently missing.

    An absent OPTIONAL resource is never a startup failure. Only a REQUIRED
    resource with no capability at all is a failure, and ``ok`` is the single
    boolean the startup path reads.
    """
    if system is None or machine is None:
        import platform as _platform

        system = _platform.system() if system is None else system
        machine = _platform.machine() if machine is None else machine

    capabilities: Dict[str, ResourceActivationCapability] = {}
    failures: List[str] = []
    degraded: List[str] = []

    for resource_id, resource in sorted(manifest.resources.items()):
        capability = activate_resource(
            hermes_home, resource, system=system, machine=machine
        )
        capabilities[resource_id] = capability

        if capability.usable:
            continue
        if resource.required:
            failures.append(resource_id)
        elif capability.degraded:
            degraded.append(resource_id)

    return DesignActivationReport(
        ok=not failures,
        capabilities=capabilities,
        failures=failures,
        degraded=degraded,
    )


__all__ = [
        "ACTIVATION_REASONS",
        "CREDENTIAL_ENV_NAMES",
        "ENGINE_ENTRYPOINTS",
        "INSTALLABLE_ON_DEMAND",
        "PARSER_RUNTIME_DIRNAME",
        "PARSER_RUNTIME_PACKAGES",
        "DesignActivationReport",
        "ResourceActivationCapability",
        "activate_design_resources",
        "activate_resource",
        "credential_present",
        "engine_quality",
        "engine_relative_paths",
        "parser_runtime_is_provisioned",
        "resolve_engine_path",
        "verify_local_skill",
]