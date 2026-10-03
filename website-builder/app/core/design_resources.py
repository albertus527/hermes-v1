"""Design resource manifest â€” the ONE configuration surface for design resources.

This module owns two things and nothing else:

1. :func:`design_profile_skills_dir` â€” the single definition of the
   profile-local skills directory (``$HERMES_HOME/skills``). Every other module
   that needs it derives it from here rather than re-spelling it, so the rule
   exists in exactly one place.

2. :func:`load_design_resource_manifest` â€” a strict, fail-closed reader for
   ``config/design_resources.yaml``.

Why a strict reader rather than a plain ``yaml.safe_load``:

``yaml.safe_load`` silently keeps the LAST value when a mapping repeats a key.
A manifest that declares ``gsap`` twice -- once ``required: true``, once
``required: false`` -- would therefore load without complaint and quietly
apply the wrong requirement. Configuration that decides whether a missing
capability blocks startup cannot tolerate silent disagreement with itself, so
duplicate keys are detected and rejected here.

Every validation failure raises :class:`DesignResourceManifestError` with a
static, sanitized message. This module is configuration, not secrets: no value
read from the manifest is ever secret, but messages are still static labels so
a start-up log line can never echo file content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import yaml


class DesignResourceManifestError(ValueError):
    """The design resource manifest is missing, unreadable, or invalid.

    Fail-closed by construction: a manifest that cannot be understood exactly is
    never partially applied. Callers cannot distinguish "resource absent" from
    "resource misconfigured" by accident, because misconfiguration never
    returns.
    """


#: The only manifest schema this build understands.
MANIFEST_VERSION = 1

#: Resource kinds. A closed set: an unrecognized kind is a typo or an
#: unsupported feature, and both must fail loudly rather than fall through to
#: some default resolution behaviour.
RESOURCE_KINDS: Tuple[str, ...] = ("skill", "reference", "registry", "npm_optional")

#: How a resource is located. ``profile_skill`` is verifiable locally today;
#: ``deferred`` means the resource is declared but not yet wired, so capability
#: verification deliberately does not attempt it.
RESOLUTIONS: Tuple[str, ...] = ("profile_skill", "deferred")

#: The only install mode in this batch. Resources carrying it are provisioned
#: per project on demand and are NOT global dependencies.
INSTALL_MODES: Tuple[str, ...] = ("project_on_demand",)

#: Hard bound on how many data entries one resource may pin. A manifest that
#: enumerates a whole directory is a snapshot, not a capability contract.
DEFAULT_DATA_ENTRIES_MAX = 32

#: Resource ids are lowercase-underscore. Constrained so a manifest key can be
#: used verbatim in a log line and a diagnostics dict without escaping.
_RESOURCE_ID_RE = re.compile(r"^[a-z0-9_]+$")

#: A data entry may not escape the skill root. Checked under BOTH separators so
#: a Windows-authored entry (``..\\..\\etc``) cannot bypass a POSIX-only check.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")

#: Absolute or home-anchored prefixes are rejected before any filesystem work.
_FORBIDDEN_ENTRY_PREFIXES = ("/", "~")


_TOP_LEVEL_KEYS: Tuple[str, ...] = ("version", "data_entries_max", "resources")

#: Per-resource keys. ``skill_name``/``install_mode``/``data_entries`` are
#: conditional, not free-form; the validator enforces which are permitted for a
#: given kind.
_RESOURCE_KEYS: Tuple[str, ...] = (
    "kind",
    "required",
    "resolution",
    "skill_name",
    "install_mode",
    "data_entries",
)


def design_profile_skills_dir(hermes_home: Path) -> Path:
    """The profile-local skills directory for ``hermes_home``.

    Hermes discovers skills under ``$HERMES_HOME/skills`` regardless of the
    process working directory, which is what makes them visible to a FRONTEND
    run executing inside an isolated external workspace.

    This is the ONLY place that rule is written down.
    :meth:`app.hermes.adapter.HermesAdapter._profile_skills_dir` delegates here
    rather than constructing the path again.
    """
    return Path(hermes_home).expanduser() / "skills"


# ---------------------------------------------------------------------------
# Strict YAML construction
# ---------------------------------------------------------------------------


class _StrictLoader(yaml.SafeLoader):
    """``SafeLoader`` that rejects duplicate mapping keys.

    Subclasses the SAFE loader deliberately â€” this only ever constructs
    standard Python types from the manifest, never arbitrary objects.
    """


def _no_duplicate_keys(loader: yaml.Loader, node: yaml.MappingNode, deep: bool = False):
    mapping: Dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise DesignResourceManifestError(
                f"duplicate key in design resource manifest: {key!r}"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys
)


# ---------------------------------------------------------------------------
# data_entries containment
# ---------------------------------------------------------------------------


def validate_data_entry(entry: Any) -> str:
    """Return ``entry`` if it is a safe skill-root-relative path, else raise.

    Syntactic half of containment. Runs at LOAD time so a hostile or careless
    entry never reaches the filesystem at all. The resolved half â€” proving the
    path lands inside the skill directory once symlinks are followed â€” lives in
    :mod:`app.core.design_capabilities`, because it needs the skill directory to
    exist. Both halves are required: this one rejects the obvious traversal,
    that one rejects the alias.
    """
    if not isinstance(entry, str):
        raise DesignResourceManifestError("data_entries entries must be strings")
    if not entry.strip():
        raise DesignResourceManifestError("data_entries entries must be non-empty")
    if _WINDOWS_DRIVE_RE.match(entry):
        raise DesignResourceManifestError(
            f"data_entries entry must be relative, got a drive-anchored path: {entry!r}"
        )
    normalized = entry.replace("\\", "/")
    if normalized.startswith(_FORBIDDEN_ENTRY_PREFIXES):
        raise DesignResourceManifestError(
            f"data_entries entry must be relative to the skill root: {entry!r}"
        )
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts:
        raise DesignResourceManifestError(
            f"data_entries entry does not name a file: {entry!r}"
        )
    if ".." in parts:
        raise DesignResourceManifestError(
            f"data_entries entry must not traverse outside the skill root: {entry!r}"
        )
    return entry


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DesignResource:
    """One declared design resource.

    A declaration of intent, never a claim of availability. ``required`` and
    ``install_mode`` say what SHOULD be true; proving what IS true is
    :mod:`app.core.design_capabilities`' job.
    """

    resource_id: str
    kind: str
    required: bool
    resolution: str
    skill_name: Optional[str] = None
    install_mode: Optional[str] = None
    data_entries: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_profile_skill(self) -> bool:
        """True when this resource is resolved from the profile skills dir."""
        return self.resolution == "profile_skill"

    @property
    def is_on_demand(self) -> bool:
        """True when absence is this resource's correct resting state.

        Keyed off ``install_mode``, deliberately NOT off ``kind``: a registry
        resource (``shadcn``) is provisioned per project exactly as an on-demand
        npm package is, so it shares the same resting state. Scoping this to
        ``npm_optional`` would report a permanent, unactionable degradation for
        every registry resource on every run.
        """
        return self.install_mode == "project_on_demand"

    def to_dict(self) -> Dict[str, Any]:
        """Serializable declaration. Names and modes only â€” no paths."""
        return {
            "kind": self.kind,
            "required": self.required,
            "resolution": self.resolution,
            "skill_name": self.skill_name,
            "install_mode": self.install_mode,
            "data_entries": list(self.data_entries),
        }


@dataclass(frozen=True)
class DesignResourceManifest:
    """The validated manifest, indexed by resource id."""

    version: int
    data_entries_max: int
    resources: Dict[str, DesignResource]

    def get(self, resource_id: str) -> DesignResource:
        """Return a declared resource.

        Raises :class:`DesignResourceManifestError` for an undeclared id rather
        than returning ``None``: capability code asks for resources by name, and
        a typo there is a bug that should be loud, not a silent "unavailable"
        that reads like a healthy host.
        """
        try:
            return self.resources[resource_id]
        except KeyError:
            raise DesignResourceManifestError(
                f"undeclared design resource: {resource_id!r}"
            ) from None

    @property
    def required_ids(self) -> List[str]:
        return sorted(r.resource_id for r in self.resources.values() if r.required)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_resource(resource_id: Any, raw: Any) -> DesignResource:
    if not isinstance(resource_id, str) or not _RESOURCE_ID_RE.match(resource_id):
        raise DesignResourceManifestError(
            f"resource ids must be lowercase-underscore, got {resource_id!r}"
        )
    if not isinstance(raw, Mapping):
        raise DesignResourceManifestError(
            f"resource {resource_id!r} must be a mapping"
        )

    unknown = set(raw) - set(_RESOURCE_KEYS)
    if unknown:
        raise DesignResourceManifestError(
            f"resource {resource_id!r} has unknown key(s): {sorted(unknown)}"
        )

    kind = raw.get("kind")
    if kind not in RESOURCE_KINDS:
        raise DesignResourceManifestError(
            f"resource {resource_id!r} has unknown kind {kind!r}; "
            f"expected one of {list(RESOURCE_KINDS)}"
        )

    required = raw.get("required")
    if not isinstance(required, bool):
        raise DesignResourceManifestError(
            f"resource {resource_id!r} must declare a boolean 'required'"
        )

    resolution = raw.get("resolution")
    if resolution not in RESOLUTIONS:
        raise DesignResourceManifestError(
            f"resource {resource_id!r} has unknown resolution {resolution!r}; "
            f"expected one of {list(RESOLUTIONS)}"
        )

    skill_name = raw.get("skill_name")
    if kind == "skill":
        if not isinstance(skill_name, str) or not skill_name.strip():
            raise DesignResourceManifestError(
                f"resource {resource_id!r} of kind 'skill' must declare 'skill_name'"
            )
    elif skill_name is not None:
        raise DesignResourceManifestError(
            f"resource {resource_id!r} of kind {kind!r} must not declare 'skill_name'"
        )

    install_mode = raw.get("install_mode")
    if kind in ("registry", "npm_optional"):
        if install_mode not in INSTALL_MODES:
            raise DesignResourceManifestError(
                f"resource {resource_id!r} of kind {kind!r} must declare "
                f"'install_mode' as one of {list(INSTALL_MODES)}"
            )
    elif install_mode is not None:
        raise DesignResourceManifestError(
            f"resource {resource_id!r} of kind {kind!r} must not declare 'install_mode'"
        )

    # A skill that is not resolved from the profile cannot have its data
    # entries verified, so declaring them would be a claim nothing checks.
    if resolution != "profile_skill" and raw.get("data_entries"):
        raise DesignResourceManifestError(
            f"resource {resource_id!r} may only declare 'data_entries' when "
            f"resolution is 'profile_skill'"
        )

    raw_entries = raw.get("data_entries")
    if raw_entries is None:
        raw_entries = []
    if not isinstance(raw_entries, list):
        raise DesignResourceManifestError(
            f"resource {resource_id!r} 'data_entries' must be a list"
        )

    return DesignResource(
        resource_id=resource_id,
        kind=kind,
        required=required,
        resolution=resolution,
        skill_name=skill_name,
        install_mode=install_mode,
        data_entries=tuple(validate_data_entry(entry) for entry in raw_entries),
    )


def parse_design_resource_manifest(
    raw: Any, *, data_entries_max: Optional[int] = None
) -> DesignResourceManifest:
    """Validate an already-parsed manifest document into a manifest.

    Separated from :func:`load_design_resource_manifest` so validation is
    testable against hand-built documents without touching the filesystem.

    ``data_entries_max`` is a test seam for overriding the bound. When it is
    omitted the document's own ``data_entries_max`` governs, falling back to
    :data:`DEFAULT_DATA_ENTRIES_MAX`. Reading the bound from the document here
    rather than only at load time is what makes the declared key meaningful on
    every path â€” a bound honoured by one entry point and ignored by another
    means the manifest author cannot reason about their own file.
    """
    if not isinstance(raw, Mapping):
        raise DesignResourceManifestError("design resource manifest must be a mapping")

    unknown = set(raw) - set(_TOP_LEVEL_KEYS)
    if unknown:
        raise DesignResourceManifestError(
            f"design resource manifest has unknown top-level key(s): {sorted(unknown)}"
        )

    version = raw.get("version")
    if version != MANIFEST_VERSION:
        raise DesignResourceManifestError(
            f"unsupported design resource manifest version {version!r}; "
            f"expected {MANIFEST_VERSION}"
        )

    declared_max = raw.get("data_entries_max")
    if declared_max is not None and (
        not isinstance(declared_max, int)
        or isinstance(declared_max, bool)
        or declared_max <= 0
    ):
        raise DesignResourceManifestError(
            "design resource manifest 'data_entries_max' must be a positive integer"
        )

    if data_entries_max is None:
        max_entries = declared_max or DEFAULT_DATA_ENTRIES_MAX
    else:
        max_entries = data_entries_max

    raw_resources = raw.get("resources")
    if not isinstance(raw_resources, Mapping) or not raw_resources:
        raise DesignResourceManifestError(
            "design resource manifest must declare a non-empty 'resources' mapping"
        )

    resources: Dict[str, DesignResource] = {}
    for resource_id, declaration in raw_resources.items():
        resource = _parse_resource(resource_id, declaration)
        if len(resource.data_entries) > max_entries:
            raise DesignResourceManifestError(
                f"resource {resource_id!r} pins {len(resource.data_entries)} "
                f"data_entries, exceeding the maximum of {max_entries}"
            )
        resources[resource.resource_id] = resource

    return DesignResourceManifest(
        version=version, data_entries_max=max_entries, resources=resources
    )


def load_design_resource_manifest(
    path: Path | None = None,
) -> DesignResourceManifest:
    """Load and validate ``config/design_resources.yaml``.

    ``path`` is a test seam; production passes nothing and gets the shipped
    file. Raises :class:`DesignResourceManifestError` for an absent, unreadable,
    or invalid manifest â€” there is no "empty manifest" fallback, because
    silently proceeding with zero declared resources would turn every required
    capability into a vacuous pass.
    """
    if path is None:
        path = Path(__file__).resolve().parents[2] / "config" / "design_resources.yaml"
    path = Path(path)

    if not path.is_file():
        raise DesignResourceManifestError(
            f"design resource manifest not found at {path}"
        )
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)
    except DesignResourceManifestError:
        raise
    except (OSError, yaml.YAMLError) as exc:
            raise DesignResourceManifestError(
                f"design resource manifest at {path} could not be parsed: {type(exc).__name__}"
            ) from None

    return parse_design_resource_manifest(raw)
