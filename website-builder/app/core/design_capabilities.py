"""Design capability resolution and preflight (Batch D0.2).

The manifest says what is CONFIGURED. This module answers the separate
question: what is actually AVAILABLE on this machine?

Those are different questions and conflating them is the failure this module
exists to prevent. The D0.2 purpose is to let the runtime distinguish a real,
usable local capability from a skill name or a placeholder directory — so a
capability is reported ``available`` only when it has been inspected on this
filesystem and found present, contained, and readable.

**What "available" means here.** Statically verified local resource layout:
the directory tree and its files are present, contained inside the skill root,
and readable. It explicitly does NOT mean the resource has been *executed* —
in particular it does not mean the UI UX Pro Max search entrypoint has been run
successfully. Executing it is a different layer (the D0 integration smoke
test) and would require subprocess execution, which this module deliberately
never performs.

**How it avoids a false positive.** Availability is proven only by inspecting
the resolved profile directory. A resource whose name appears in a prompt, a
SKILL.md body, a source file, or a row of a CSV dataset proves nothing and is
never treated as evidence. This matters concretely: UI UX Pro Max's data names
shadcn, GSAP and Three.js as guidance, and a resolver that scanned for those
names would cheerfully report them installed.

**Failure semantics.**

* required + unavailable  -> ``ok=False``; startup refuses.
* optional + unavailable  -> ``ok`` unchanged; listed in ``degraded``.
* on-demand + absent      -> ``not_installed``; NOT degradation. See
  :attr:`~app.core.design_resources.DesignResource.is_on_demand`.

Everything returned is bounded: resource ids, statuses, and static sanitized
labels. No file content, no absolute paths per resource, no secrets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from app.core.design_resources import (
    DesignResource,
    DesignResourceManifest,
    DesignResourceManifestError,
    design_profile_skills_dir,
    load_design_resource_manifest,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status vocabulary — closed set
# ---------------------------------------------------------------------------

STATUS_AVAILABLE = "available"
STATUS_UNAVAILABLE_REQUIRED = "unavailable_required"
STATUS_UNAVAILABLE_OPTIONAL = "unavailable_optional"
STATUS_NOT_INSTALLED = "not_installed"

#: Exhaustive. A capability result must never carry a status outside this set:
#: a fifth spelling would be a new, unhandled state, and callers switch on these
#: strings.
CAPABILITY_STATUSES = frozenset(
    {
        STATUS_AVAILABLE,
        STATUS_UNAVAILABLE_REQUIRED,
        STATUS_UNAVAILABLE_OPTIONAL,
        STATUS_NOT_INSTALLED,
    }
)

#: Sanitized failure labels. These are the ONLY strings that may appear as a
#: resource's ``detail``. They describe the class of problem, never the path,
#: never file content, and never anything read out of the file system.
DETAIL_OK = "verified"
DETAIL_SKILL_DIR_MISSING = "skill directory not present in profile"
DETAIL_SKILL_DIR_NOT_A_DIR = "skill path is not a directory"
DETAIL_SKILL_MD_MISSING = "SKILL.md not present"
DETAIL_SKILL_MD_UNREADABLE = "SKILL.md is not a readable non-empty file"
DETAIL_DATA_ENTRY_MISSING = "declared data entry not present"
DETAIL_DATA_ENTRY_ESCAPES = "declared data entry resolves outside the skill root"
DETAIL_DEFERRED = "resolution not wired in this batch"


def _is_readable_file(path: Path) -> bool:
    """True when ``path`` is a readable, non-empty regular file.

    Zero-byte counts as unreadable: a placeholder file is exactly the state the
    capability check exists to catch, and an empty SKILL.md carries no
    instructions at all.
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


@dataclass(frozen=True)
class DesignCapability:
    """One resource's resolved capability state."""

    resource_id: str
    kind: str
    required: bool
    configured: bool
    available: bool
    status: str
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "required": self.required,
            "configured": self.configured,
            "available": self.available,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class DesignCapabilityReport:
    """Bounded, serializable result of capability resolution."""

    ok: bool
    manifest_version: int
    profile_skills_dir: str
    resources: Dict[str, DesignCapability]
    failures: List[str]
    degraded: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "manifest_version": self.manifest_version,
            "profile_skills_dir": self.profile_skills_dir,
            "resources": {
                rid: cap.to_dict() for rid, cap in sorted(self.resources.items())
            },
            "failures": sorted(self.failures),
            "degraded": sorted(self.degraded),
        }

    def summary(self) -> str:
        """One bounded line: resource ids with their statuses. No paths."""
        return ", ".join(
            f"{rid}={cap.status}"
            for rid, cap in sorted(self.resources.items())
        )


# ---------------------------------------------------------------------------
# Per-resource verification
# ---------------------------------------------------------------------------


def entry_is_contained(skill_root: Path, entry_path: Path) -> bool:
    """True when ``entry_path`` resolves strictly inside ``skill_root``.

    The resolved half of ``data_entries`` containment, factored out so it can
    be tested directly rather than only through a symlink (which most Windows
    hosts cannot create without an elevated privilege, and skipping the check
    would leave the guard unexercised exactly where it is most likely to be
    wrong).

    Both sides are resolved, so this catches a symlink INSIDE the skill
    directory that points at ``/etc/passwd``: the entry passes every string
    check — relative, no ``..``, it exists — and lands outside the root here.
    Containment is measured against the RESOLVED root, so linking a shared
    skill into the profile remains legitimate while linking *out of* it does
    not.
    """
    try:
        resolved_root = Path(skill_root).resolve()
        resolved_entry = Path(entry_path).resolve()
        resolved_entry.relative_to(resolved_root)
    except (OSError, ValueError):
        return False
    return True


def _verify_profile_skill(
    skills_dir: Path, resource: DesignResource
) -> "tuple[bool, str]":
    """Verify a ``profile_skill`` resource against the real profile layout.

    Ordered, first-failure-wins. Returns ``(available, sanitized_detail)``.
    """
    assert resource.skill_name is not None  # guaranteed by manifest validation

    skill_dir = skills_dir / resource.skill_name
    if not skill_dir.exists():
        return False, DETAIL_SKILL_DIR_MISSING
    if not skill_dir.is_dir():
        return False, DETAIL_SKILL_DIR_NOT_A_DIR

    if not _is_readable_file(skill_dir / "SKILL.md"):
        if not (skill_dir / "SKILL.md").exists():
            return False, DETAIL_SKILL_MD_MISSING
        return False, DETAIL_SKILL_MD_UNREADABLE

    for entry in resource.data_entries:
        entry_path = skill_dir / entry
        if not entry_path.is_file():
            return False, DETAIL_DATA_ENTRY_MISSING
        if not entry_is_contained(skill_dir, entry_path):
            return False, DETAIL_DATA_ENTRY_ESCAPES

    return True, DETAIL_OK


def _resolve_resource(
    skills_dir: Path, resource: DesignResource
) -> DesignCapability:
    """Resolve one declared resource to its capability state."""
    # Declared but not wired: report honestly rather than probing. A deferred
    # reference is never "unavailable" — nothing was ever going to look for it.
    if not resource.is_profile_skill:
        status = (
            STATUS_NOT_INSTALLED if resource.is_on_demand else STATUS_UNAVAILABLE_OPTIONAL
        )
        return DesignCapability(
            resource_id=resource.resource_id,
            kind=resource.kind,
            required=resource.required,
            configured=True,
            available=False,
            status=status,
            detail=DETAIL_DEFERRED,
        )

    available, detail = _verify_profile_skill(skills_dir, resource)

    if available:
        status = STATUS_AVAILABLE
    elif resource.required:
        status = STATUS_UNAVAILABLE_REQUIRED
    elif resource.is_on_demand:
        status = STATUS_NOT_INSTALLED
    else:
        status = STATUS_UNAVAILABLE_OPTIONAL

    return DesignCapability(
        resource_id=resource.resource_id,
        kind=resource.kind,
        required=resource.required,
        configured=True,
        available=available,
        status=status,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def resolve_design_capabilities(
    hermes_home: Path,
    manifest: Optional[DesignResourceManifest] = None,
) -> DesignCapabilityReport:
    """Resolve every declared design resource for the profile at ``hermes_home``.

    ``hermes_home`` is supplied by the caller (``RuntimeConfig.hermes_home``,
    already ``expanduser``-ed and env/config-resolved). This module never reads
    ``HERMES_HOME`` from the environment itself and never constructs a home path:
    resolving the profile is the runtime's job, and a resolver that guesses its
    own would disagree with the runtime the first time they diverged.

    Every declared resource is resolved, whether or not it is required, so a
    degraded optional is visible in diagnostics instead of silently absent.
    """
    if manifest is None:
        manifest = load_design_resource_manifest()

    skills_dir = design_profile_skills_dir(hermes_home)

    capabilities: Dict[str, DesignCapability] = {}
    failures: List[str] = []
    degraded: List[str] = []

    for resource_id, resource in sorted(manifest.resources.items()):
        capability = _resolve_resource(skills_dir, resource)
        capabilities[resource_id] = capability

        if capability.available:
            continue
        if capability.required:
            failures.append(resource_id)
        # An on-demand resource's absence is its resting state, not a
        # degradation. Listing it here would report a condition the operator
        # cannot act on and cannot fix at bootstrap.
        elif capability.status == STATUS_UNAVAILABLE_OPTIONAL:
            degraded.append(resource_id)

    return DesignCapabilityReport(
        ok=not failures,
        manifest_version=manifest.version,
        profile_skills_dir=str(skills_dir),
        resources=capabilities,
        failures=failures,
        degraded=degraded,
    )


def preflight_design_capabilities(
    hermes_home: Path,
    manifest: Optional[DesignResourceManifest] = None,
) -> DesignCapabilityReport:
    """Resolve design capabilities and log a bounded summary.

    Returns the report rather than a bare bool so callers that need the detail
    (the startup log, diagnostics) do not have to resolve twice. The bool
    contract the startup path uses is ``report.ok``.

    Fails closed for REQUIRED capabilities only. An optional resource that is
    absent is reported and logged, never fatal.
    """
    try:
        report = resolve_design_capabilities(hermes_home, manifest)
    except DesignResourceManifestError as exc:
        # An unreadable manifest is a configuration failure, not a missing
        # capability: we cannot know what is required, so we cannot claim to be
        # satisfied. Fail closed, and say only the sanitized reason.
        logger.error(
            "Design capability preflight cannot evaluate the resource manifest "
            "- refusing to start.  %s",
            exc,
        )
        raise

    if report.ok:
        logger.info(
            "Design capability preflight OK. Profile skills directory: %s. %s",
            report.profile_skills_dir,
            report.summary(),
        )
    else:
        logger.error(
            "A REQUIRED design capability is unavailable - refusing to start."
        )
        for resource_id in sorted(report.failures):
            capability = report.resources[resource_id]
            logger.error(
                "  required design resource %s (%s): %s",
                resource_id,
                capability.kind,
                capability.detail,
            )
        logger.error("Profile skills directory: %s", report.profile_skills_dir)

    if report.degraded:
        logger.warning(
            "Optional design capabilities are degraded (not fatal): %s",
            ", ".join(sorted(report.degraded)),
        )

    return report


def capability_statuses(manifest: DesignResourceManifest) -> Dict[str, str]:
    """Expected status per resource WITHOUT touching the filesystem.

    Derived purely from the declaration: every non-profile skill is either
    on-demand (``not_installed``) or a deferred reference
    (``unavailable_optional``). Useful for asserting the resolver's
    classification logic itself, independently of any host layout.
    """
    statuses: Dict[str, str] = {}
    for resource_id, resource in manifest.resources.items():
        if resource.is_profile_skill:
            statuses[resource_id] = STATUS_UNAVAILABLE_REQUIRED if resource.required else STATUS_UNAVAILABLE_OPTIONAL
        elif resource.is_on_demand:
            statuses[resource_id] = STATUS_NOT_INSTALLED
        else:
            statuses[resource_id] = STATUS_UNAVAILABLE_OPTIONAL
    return statuses


__all__ = [
    "CAPABILITY_STATUSES",
    "DesignCapability",
    "DesignCapabilityReport",
    "STATUS_AVAILABLE",
    "STATUS_NOT_INSTALLED",
    "STATUS_UNAVAILABLE_OPTIONAL",
    "STATUS_UNAVAILABLE_REQUIRED",
    "capability_statuses",
    "preflight_design_capabilities",
    "resolve_design_capabilities",
]