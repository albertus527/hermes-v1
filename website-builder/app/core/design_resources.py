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

#: Credential environment variable names per design resource -- the ONE table.
#:
#: Two layers consult it and they must agree: the capability layer reads only
#: PRESENCE ("is this resource configured?"), and the catalog fetch adapter reads
#: the VALUE ("place it in an Authorization header"). Keeping the names in one
#: place means what the capability layer reports and what execution uses cannot
#: drift -- the defect this table's central position removes.
#:
#: These are third-party research/CI credentials, never deployment credentials
#: (Vercel/GitHub/Telegram): a design corpus has no business holding a release
#: credential, so none of these names may ever name one.
#:
#: ``twenty_first`` includes the names 21st's OWN agent skill documents
#: (``TWENTYFIRST_TOKEN`` / ``API_KEY_21ST``, from
#: ``21st.dev/.well-known/skills/21st-cli-use/SKILL.md``) so a user who follows
#: upstream's docs is recognised rather than silently reported as unconfigured.
CREDENTIAL_ENV_NAMES: Dict[str, Tuple[str, ...]] = {
    "refero": ("REFERO_API_KEY",),
    "twenty_first": (
        "TWENTYFIRST_TOKEN",
        "API_KEY_21ST",
        "TWENTY_FIRST_API_KEY",
        "TWENTYFIRST_API_KEY",
    ),
    "react_bits": (),
    "transitions_dev": (),
    "impeccable": (),
}


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

#: How a resource is located. This is a CLOSED set and it fails closed: an
#: unrecognized resolution is a typo or an unsupported feature, never a silent
#: fallback to some default behaviour.
#:
#: ``profile_skill`` is verifiable locally today -- the resource is a directory
#: under ``$HERMES_HOME/skills/<skill_name>`` whose ``data_entries`` are checked
#: for existence and containment.
#:
#: ``on_demand_registry`` is a resource that is fetched INTO AN INDIVIDUAL
#: PROJECT when and if that project's design calls for it (the shadcn registry,
#: the 21st.dev / React Bits catalogs). It is not a global dependency, so its
#: absence is the correct resting state.
#:
#: ``deferred`` remains ONLY for a resource that is declared but genuinely not
#: wired. It is a statement about the CODE, not about the host: a wired resource
#: must never carry it, because ``deferred`` suppresses capability verification
#: and a wired-but-deferred resource would report nothing while pretending to be
#: honest about being unwired.
RESOLUTIONS: Tuple[str, ...] = ("profile_skill", "on_demand_registry", "deferred")

#: Resolutions that describe a resource the application actually operates.
#: Everything else in :data:`RESOLUTIONS` is a declaration without an
#: implementation behind it.
IMPLEMENTED_RESOLUTIONS: Tuple[str, ...] = ("profile_skill", "on_demand_registry")

#: The CLOSED set of adapters a resource may name, each mapped to the
#: ``(module, attribute)`` that implements it.
#:
#: This spans BOTH surfaces a design resource can be reached through, which is
#: why it is not simply the retrieval registry:
#:
#: * **retrieval** reads guidance into a prompt -- the csv/reference/markdown
#:   adapters in :mod:`app.core.design_retrieval`;
#: * **critic** runs a bounded scan -- :mod:`app.core.design_critic`;
#: * **catalog** normalizes a remote component listing --
#:   :mod:`app.core.design_catalog`;
#: * **cli** materializes recipes -- :mod:`app.core.design_transitions`;
#: * **registry** types a component install -- :mod:`app.core.design_registry`;
#: * **package** installs an exact-pinned npm dependency --
#:   :mod:`app.core.design_install`.
#:
#: The value is checked at load time by importing the module and reading the
#: attribute, so a renamed or deleted implementation breaks the manifest load
#: rather than silently leaving a resource claiming wiring it does not have.
DESIGN_ADAPTERS: Dict[str, Tuple[str, str]] = {
    # retrieval surfaces. These names are the `DesignAdapter.name` values in
    # design_retrieval.DESIGN_ADAPTERS, and the table entry is what
    # design_retrieval._adapter_by_name resolves against -- so a renamed adapter
    # breaks the manifest load rather than silently detaching it.
    "critic": ("app.core.design_retrieval", "_adapter_by_name"),
    "guidance": ("app.core.design_retrieval", "_adapter_by_name"),
    "reference_markdown": ("app.core.design_retrieval", "_adapter_by_name"),
    # bounded critic scan
    "critic_scan": ("app.core.design_critic", "run_critic_scan"),
    # remote component catalogs
    "twenty_first": ("app.core.design_catalog", "normalize_catalog"),
    "react_bits": ("app.core.design_catalog", "normalize_catalog"),
    # recipe materialization
    "transitions_dev": ("app.core.design_transitions", "build_add_argv"),
    # typed component installs
    "shadcn": ("app.core.design_registry", "build_registry_request"),
    # exact-pinned npm dependencies
    "npm_package": ("app.core.design_install", "build_install_argv"),
}

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
    "adapter",
    "companion",
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
    adapter: Optional[str] = None
    companion: Optional[str] = None
    data_entries: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_implemented(self) -> bool:
            """True when this declaration describes something the code operates.

            The counterpart to ``deferred``: a resource may be perfectly well
            formed and still be a declaration with no implementation behind it.
            """
            return self.resolution in IMPLEMENTED_RESOLUTIONS

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
            "adapter": self.adapter,
            "companion": self.companion,
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

    @property
    def deferred_ids(self) -> List[str]:
        """Declared-but-unwired resources.

        A REVIEW LEDGER, not a health check. After D3a.5 this set is expected to
        be empty: every resource below is reached through a named adapter.
        Anything appearing here needs a wiring story.
        """
        return sorted(
            r.resource_id for r in self.resources.values() if not r.is_implemented
        )

        @property
        def deferred_ids(self) -> List[str]:
            """Declared-but-unwired resources.

            This is a REVIEW LEDGER, not a health check. After D3a.5 the set is
            expected to be empty for the five activated resources; anything here
            still needs a wiring story.
            """
            return sorted(
                r.resource_id for r in self.resources.values() if not r.is_implemented
            )


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

    # `adapter` names the surface this resource is reached through. Its
    # EXISTENCE is not required here: parse_design_resource_manifest() validates
    # the schema, and the shipped file's truth claims are checked separately by
    # validate_adapter_claims() -- called from load_design_resource_manifest().
    #
    # What IS refused here is the self-contradictory case. `deferred` asserts
    # that nothing operates the resource; an adapter beside it is a claim the
    # manifest contradicts itself about, and no amount of later validation makes
    # that coherent.
    adapter = raw.get("adapter")
    if adapter is not None and not isinstance(adapter, str):
        raise DesignResourceManifestError(
            f"resource {resource_id!r} must declare 'adapter' as a string"
        )
    if adapter is not None and resolution == "deferred":
        raise DesignResourceManifestError(
            f"resource {resource_id!r} is resolution 'deferred' and must not "
            f"declare 'adapter'"
        )

    # A companion package is a CLOSED, reviewed entry -- never derived from a
    # generic naming rule at load time. The name is the key into
    # design_install.DEPENDENCY_COMPANION_PACKAGES.
    companion = raw.get("companion")
    if companion is not None and (
        not isinstance(companion, str) or not companion.strip()
    ):
        raise DesignResourceManifestError(
            f"resource {resource_id!r} must declare 'companion' as a "
            f"non-empty string"
        )

    skill_name = raw.get("skill_name")
    if kind == "skill":
        if not isinstance(skill_name, str) or not skill_name.strip():
            raise DesignResourceManifestError(
                f"resource {resource_id!r} of kind 'skill' must declare 'skill_name'"
            )
    elif skill_name is not None:
        raise DesignResourceManifestError(
            f"resource {resource_id!r} of kind {kind!r} must not declare "
            f"'skill_name'"
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
            adapter=adapter,
            companion=companion,
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

    # Schema only. Whether the shipped manifest's claims are TRUE is checked by
    # load_design_resource_manifest() below, so a hand-built document exercising
    # an unrelated rule does not have to restate which adapter operates it.
    return DesignResourceManifest(
        version=version, data_entries_max=max_entries, resources=resources
    )


def validate_adapter_claims(manifest: DesignResourceManifest) -> None:
    """Every claimed adapter must exist; every companion must be in the table.

    This is the check that makes ``adapter:`` worth having. Without it the
    manifest could name an adapter nobody registered -- reintroducing exactly
    the placeholder D3a.5 removes, but now with a field that LOOKS like
    evidence of wiring.

    A missing adapter raises rather than warning. A manifest that claims a
    capability nothing implements is a build-time error, not a degraded host:
    the operator has to fix the file, and silently degrading would let the
    mistake ship.

    Imports are local and lazy so the manifest module stays importable without
    dragging the retrieval/catalog/registry/install layers into a file that only
    needs to be parseable.
    """
    import importlib

    for resource_id, resource in sorted(manifest.resources.items()):
        adapter = resource.adapter
        if adapter is not None:
            target = DESIGN_ADAPTERS.get(adapter)
            if target is None:
                raise DesignResourceManifestError(
                    f"resource {resource_id!r} claims unknown adapter "
                    f"{adapter!r}; known adapters: {sorted(DESIGN_ADAPTERS)}"
                )
            module_name, attribute = target
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                raise DesignResourceManifestError(
                    f"resource {resource_id!r} claims adapter {adapter!r}, whose "
                    f"module {module_name!r} cannot be imported"
                ) from None
            if not hasattr(module, attribute):
                raise DesignResourceManifestError(
                    f"resource {resource_id!r} claims adapter {adapter!r}, but "
                    f"{module_name!r} has no attribute {attribute!r}"
                )

        companion = resource.companion
        if companion is not None:
            from app.core.design_install import DEPENDENCY_COMPANION_PACKAGES

            if companion not in DEPENDENCY_COMPANION_PACKAGES:
                raise DesignResourceManifestError(
                    f"resource {resource_id!r} claims companion package "
                    f"{companion!r}, which is not in the closed companion table; "
                    f"known: {sorted(DEPENDENCY_COMPANION_PACKAGES)}"
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

    manifest = parse_design_resource_manifest(raw)

    # The SHIPPED manifest is held to a stricter standard than an ad-hoc one.
    # Only when no path was supplied are we looking at the file whose claims
    # this batch actually made; an explicit `path` is a caller asking to inspect
    # some manifest, which is exactly how drift tests and custom manifests are
    # exercised, and restating the shipped wiring contract there would make an
    # otherwise-valid custom manifest unparseable.
    if path is None:
        for resource in manifest.resources.values():
            if resource.is_implemented and not resource.adapter:
                raise DesignResourceManifestError(
                    f"shipped resource {resource.resource_id!r} is resolution "
                    f"{resource.resolution!r} and must name the 'adapter' that "
                    f"operates it"
                )
    validate_adapter_claims(manifest)
    return manifest
