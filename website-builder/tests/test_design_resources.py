"""Batch D0.1-D0.2: design resource manifest + capability preflight.

Real temporary profiles, real YAML, real filesystem. No network, no subprocess,
no npm install, and no operator home: every fixture is built under ``tmp_path``.

The properties under test are BEHAVIOUR CONTRACTs, not snapshots of the current
manifest contents. Where a test names a resource id it is because that id
carries a distinct semantic obligation (required vs optional, on-demand vs
degraded) — not because the list is expected to stay frozen. Nothing here
asserts a count, so adding or removing a resource does not break the suite.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_capabilities import (
    STATUS_AVAILABLE,
    STATUS_NOT_INSTALLED,
    STATUS_UNAVAILABLE_OPTIONAL,
    STATUS_UNAVAILABLE_REQUIRED,
    capability_statuses,
        entry_is_contained,
        preflight_design_capabilities,
        resolve_design_capabilities,
    )
from app.core.design_resources import (
    DesignResourceManifestError,
    design_profile_skills_dir,
    load_design_resource_manifest,
    parse_design_resource_manifest,
    validate_data_entry,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_MANIFEST = REPO_ROOT / "website-builder" / "config" / "design_resources.yaml"

#: The required design capability. Named rather than counted: it is the resource
#: whose absence must block startup.
REQUIRED_SKILL = "ui_ux_pro_max"

#: Every resource provisioned per project on demand. ``shadcn`` is a registry,
#: the rest are npm packages; all share one resting state, which is the point.
ON_DEMAND_RESOURCES = ("shadcn", "gsap", "three", "lenis")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail loudly if anything in this module reaches for a socket.

    Resolution must be purely local: a resolver that consulted a registry or a
    package index would make startup depend on the network and would report a
    capability "available" based on a remote answer rather than this machine.
    """

    def deny(*args, **kwargs):
        raise AssertionError("network access is forbidden in design capability tests")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture
def manifest():
    return load_design_resource_manifest()


def _write_manifest(path: Path, document: dict) -> Path:
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def _minimal_manifest(**overrides) -> dict:
    """A tiny valid manifest, overridable per test."""
    document = {
        "version": 1,
        "resources": {
            REQUIRED_SKILL: {
                "kind": "skill",
                "required": True,
                "resolution": "profile_skill",
                "skill_name": "ui-ux-pro-max",
                "data_entries": [],
            }
        },
    }
    document.update(overrides)
    return document


def _make_skill(
    skills_dir: Path,
    name: str,
    *,
    skill_md: str | None = "---\nname: x\ndescription: y\n---\n\nBody.\n",
    data_entries: dict | None = None,
) -> Path:
    """Create a profile skill directory with the real D0 layout."""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    if skill_md is not None:
        (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    for rel, content in (data_entries or {}).items():
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return skill_dir


@pytest.fixture
def profile(tmp_path):
    """An isolated profile home. Nothing outside tmp_path is ever touched."""
    home = tmp_path / "hermes-website"
    home.mkdir()
    return home


# ---------------------------------------------------------------------------
# 1. Manifest loads
# ---------------------------------------------------------------------------


def test_shipped_manifest_loads():
    manifest = load_design_resource_manifest(SHIPPED_MANIFEST)
    assert manifest.version == 1
    assert manifest.resources, "manifest must declare resources"


@pytest.mark.parametrize(
    "resource_id,kind,required",
    [
        ("ui_ux_pro_max", "skill", True),
        ("impeccable", "skill", False),
        ("refero", "reference", False),
        ("shadcn", "registry", False),
        ("twenty_first", "reference", False),
        ("react_bits", "reference", False),
        ("transitions_dev", "reference", False),
        ("gsap", "npm_optional", False),
        ("three", "npm_optional", False),
        ("lenis", "npm_optional", False),
    ],
)
def test_declared_resource_semantics(manifest, resource_id, kind, required):
    """Each resource declares the kind and requirement D0.1 specified."""
    resource = manifest.get(resource_id)
    assert resource.kind == kind
    assert resource.required is required


def test_required_skill_pins_real_search_entrypoint_and_dataset(manifest):
    """A required skill must verify more than SKILL.md alone.

    A skill whose only verified artifact is SKILL.md is the placeholder
    directory this batch exists to distinguish from a usable capability.
    """
    entries = set(manifest.get(REQUIRED_SKILL).data_entries)
    assert entries, "required skill must pin its canonical local data"
    assert any("search" in entry for entry in entries)
    assert any(entry.endswith(".csv") for entry in entries)


def test_no_resource_is_uniquely_required_beyond_the_known_one(manifest):
    """Exactly one resource is required at this stage.

    Asserting the COUNT would break every time a resource is promoted, which is
    a routine decision. Asserting the identity is the actual contract: the one
    resource that blocks startup is a deliberate, reviewed choice.
    """
    assert set(manifest.required_ids) == {REQUIRED_SKILL}


# ---------------------------------------------------------------------------
# 2. Validation is fail-closed
# ---------------------------------------------------------------------------


def test_duplicate_key_is_rejected(tmp_path):
    """yaml.safe_load keeps the LAST duplicate silently.

    A manifest declaring the same resource twice with different requirements
    would otherwise load without complaint and apply whichever came last —
    on the exact field that decides whether a missing capability blocks
    startup.
    """
    path = tmp_path / "dupe.yaml"
    path.write_text(
        "version: 1\n"
        "resources:\n"
        "  a_resource:\n"
        "    kind: reference\n"
        "    required: false\n"
        "    resolution: deferred\n"
        "  a_resource:\n"
        "    kind: reference\n"
        "    required: true\n"
        "    resolution: deferred\n",
        encoding="utf-8",
    )
    with pytest.raises(DesignResourceManifestError):
        load_design_resource_manifest(path)


@pytest.mark.parametrize("kind", ["", "library", "SKILL", "npm", None])
def test_unknown_kind_is_rejected(kind):
    document = _minimal_manifest(
        resources={"thing": {"kind": kind, "required": False, "resolution": "deferred"}}
    )
    with pytest.raises(DesignResourceManifestError):
        parse_design_resource_manifest(document)


def test_unknown_resolution_is_rejected():
    document = _minimal_manifest(
        resources={"thing": {"kind": "reference", "required": False, "resolution": "vibes"}}
    )
    with pytest.raises(DesignResourceManifestError):
        parse_design_resource_manifest(document)


@pytest.mark.parametrize(
    "resource",
    [
        {"kind": "reference", "required": False, "resolution": "deferred", "surprise": 1},
        {"kind": "skill", "required": False, "resolution": "profile_skill"},  # no skill_name
        {"kind": "reference", "required": "yes", "resolution": "deferred"},  # not bool
        {"kind": "reference", "required": False, "resolution": "deferred", "skill_name": "x"},
        {"kind": "registry", "required": False, "resolution": "deferred"},  # no install_mode
        {"kind": "npm_optional", "required": False, "resolution": "deferred", "install_mode": "always"},
        {"kind": "reference", "required": False, "resolution": "deferred", "install_mode": "project_on_demand"},
    ],
)
def test_malformed_resource_declarations_are_rejected(resource):
    document = _minimal_manifest(resources={"thing": resource})
    with pytest.raises(DesignResourceManifestError):
        parse_design_resource_manifest(document)


def test_a_skill_deferred_without_wiring_is_still_a_valid_declaration():
    """A deferred skill may declare its name for future wiring.

    ``resolution`` says WHERE a resource is looked up, not whether the
    declaration is well-formed. Refusing ``kind: skill`` + ``deferred`` would
    make it impossible to declare a skill before wiring its resolution — which
    is exactly the state this manifest holds every non-profile skill in.
    """
    document = _minimal_manifest(
        resources={
            "thing": {
                "kind": "skill",
                "required": False,
                "resolution": "deferred",
                "skill_name": "thing",
            }
        }
    )
    manifest = parse_design_resource_manifest(document)
    assert manifest.get("thing").skill_name == "thing"
    assert manifest.get("thing").is_profile_skill is False


def test_unknown_top_level_key_is_rejected():
    document = _minimal_manifest(surprise=True)
    with pytest.raises(DesignResourceManifestError):
        parse_design_resource_manifest(document)


def test_empty_or_wrong_version_manifest_is_rejected():
    with pytest.raises(DesignResourceManifestError):
        parse_design_resource_manifest({"version": 2, "resources": {"a": {}}})
    with pytest.raises(DesignResourceManifestError):
        parse_design_resource_manifest({"version": 1, "resources": {}})
    with pytest.raises(DesignResourceManifestError):
        parse_design_resource_manifest({"version": 1})


def test_missing_manifest_file_fails_closed(tmp_path):
    """There is no 'empty manifest' fallback.

    Proceeding with zero declared resources would turn every required
    capability into a vacuous pass — the exact silent success this layer
    exists to prevent.
    """
    with pytest.raises(DesignResourceManifestError):
        load_design_resource_manifest(tmp_path / "absent.yaml")


# ---------------------------------------------------------------------------
# 3-4. Required vs optional semantics
# ---------------------------------------------------------------------------


def test_required_missing_skill_fails_preflight(profile):
    manifest = parse_design_resource_manifest(_minimal_manifest())
    report = resolve_design_capabilities(profile, manifest)
    assert not report.ok
    assert report.failures == [REQUIRED_SKILL]
    assert report.resources[REQUIRED_SKILL].status == STATUS_UNAVAILABLE_REQUIRED
    assert report.resources[REQUIRED_SKILL].available is False


def test_optional_missing_skill_is_degraded_not_failed(profile):
    document = _minimal_manifest(
        resources={
            "a_required": {
                "kind": "skill",
                "required": True,
                "resolution": "profile_skill",
                "skill_name": "present-skill",
            },
            "an_optional": {
                "kind": "skill",
                "required": False,
                "resolution": "profile_skill",
                "skill_name": "absent-skill",
            },
        }
    )
    manifest = parse_design_resource_manifest(document)
    _make_skill(design_profile_skills_dir(profile), "present-skill")

    report = resolve_design_capabilities(profile, manifest)
    assert report.ok, "an absent optional must never fail preflight"
    assert report.failures == []
    assert report.degraded == ["an_optional"]
    assert report.resources["an_optional"].status == STATUS_UNAVAILABLE_OPTIONAL


def test_preflight_is_not_fatal_for_optionals(profile, caplog):
    _make_skill(design_profile_skills_dir(profile), "present-skill")
    document = _minimal_manifest(
        resources={
            "present": {
                "kind": "skill",
                "required": True,
                "resolution": "profile_skill",
                "skill_name": "present-skill",
            },
            "absent": {
                "kind": "skill",
                "required": False,
                "resolution": "profile_skill",
                "skill_name": "absent-skill",
            },
        }
    )
    manifest = parse_design_resource_manifest(document)

    with caplog.at_level("WARNING"):
        report = preflight_design_capabilities(profile, manifest)

    assert report.ok
    assert "absent" in report.degraded


# ---------------------------------------------------------------------------
# 5. Real layout resolves in a temp fixture
# ---------------------------------------------------------------------------


def test_real_ui_ux_pro_max_layout_resolves_available(tmp_path):
    """The verified production layout, built for real under tmp_path."""
    home = tmp_path / "profile-home"
    _make_skill(
        design_profile_skills_dir(home),
        "ui-ux-pro-max",
        data_entries={
            "scripts/search.py": "def search():\n    return []\n",
            "data/styles.csv": "name,css\nbutton,\"padding: 1rem\"\n",
        },
    )

    report = resolve_design_capabilities(home, load_design_resource_manifest())
    capability = report.resources[REQUIRED_SKILL]

    assert capability.available is True
    assert capability.status == STATUS_AVAILABLE
    assert report.ok
    assert report.failures == []


def test_resolution_does_not_depend_on_a_literal_home_path(tmp_path, monkeypatch):
    """Two differently-named roots must resolve identically.

    A resolver that reached for ``Path.home()`` or a baked-in user directory
    would verify a different directory than the runtime loads from — and the
    bug would be invisible on the machine where the baked path happens to be
    correct.
    """
    manifest = load_design_resource_manifest()
    results = []
    for home_name in ("operator-a", "totally-different-name-b"):
        home = tmp_path / home_name / "nested" / "profile"
        _make_skill(
            design_profile_skills_dir(home),
            "ui-ux-pro-max",
            data_entries={
                "scripts/search.py": "x = 1\n",
                "data/styles.csv": "a,b\n1,2\n",
            },
        )
        results.append(resolve_design_capabilities(home, manifest))

    first, second = results
    assert first.ok and second.ok
    assert first.resources[REQUIRED_SKILL].status == second.resources[REQUIRED_SKILL].status == STATUS_AVAILABLE
    assert set(first.resources) == set(second.resources)
    assert first.degraded == second.degraded


def test_profile_skills_dir_is_root_relative(profile):
    assert design_profile_skills_dir(profile) == profile / "skills"


# ---------------------------------------------------------------------------
# 6-7. Missing / unreadable / malformed skill layouts
# ---------------------------------------------------------------------------


def test_missing_skill_md_is_unavailable(profile):
    _make_skill(design_profile_skills_dir(profile), "ui-ux-pro-max", skill_md=None)
    report = resolve_design_capabilities(profile, load_design_resource_manifest())
    assert report.resources[REQUIRED_SKILL].available is False
    assert not report.ok


def test_empty_skill_md_is_unavailable(profile):
    """A zero-byte SKILL.md carries no instructions — same as none at all."""
    _make_skill(
        design_profile_skills_dir(profile), "ui-ux-pro-max", skill_md=""
    )
    report = resolve_design_capabilities(profile, load_design_resource_manifest())
    assert report.resources[REQUIRED_SKILL].available is False


def test_skill_md_that_is_a_directory_is_unavailable(profile):
    """Malformed layout: the path exists but is not a readable file."""
    skill_dir = design_profile_skills_dir(profile) / "ui-ux-pro-max"
    (skill_dir / "SKILL.md").mkdir(parents=True)
    report = resolve_design_capabilities(profile, load_design_resource_manifest())
    assert report.resources[REQUIRED_SKILL].available is False
    assert not report.ok


def test_skill_path_that_is_a_file_is_unavailable(profile):
    skills_dir = design_profile_skills_dir(profile)
    skills_dir.mkdir(parents=True)
    (skills_dir / "ui-ux-pro-max").write_text("not a directory", encoding="utf-8")
    report = resolve_design_capabilities(profile, load_design_resource_manifest())
    assert report.resources[REQUIRED_SKILL].available is False


def test_missing_declared_data_entry_is_unavailable(profile):
    """Proves the data check is real rather than vacuously satisfied."""
    _make_skill(
        design_profile_skills_dir(profile),
        "ui-ux-pro-max",
        data_entries={"scripts/search.py": "x = 1\n"},
    )
    report = resolve_design_capabilities(profile, load_design_resource_manifest())
    capability = report.resources[REQUIRED_SKILL]
    assert capability.available is False
    assert not report.ok


# ---------------------------------------------------------------------------
# 8. data_entries containment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        "../escape.py",
        "scripts/../../escape.py",
        "scripts\\..\\..\\escape.py",
        "../../etc/passwd",
        "..\\..\\windows\\system32",
    ],
)
def test_traversal_entries_are_rejected_at_load(entry):
    with pytest.raises(DesignResourceManifestError):
        validate_data_entry(entry)


@pytest.mark.parametrize(
    "entry",
    ["/etc/passwd", "/usr/local/lib", "C:\\Windows\\win.ini", "c:/windows/win.ini", "~", "~/secrets", ".", "./", "  "],
)
def test_non_relative_entries_are_rejected_at_load(entry):
    with pytest.raises(DesignResourceManifestError):
        validate_data_entry(entry)


@pytest.mark.parametrize("entry", [None, 1, [], {"a": "b"}])
def test_non_string_entries_are_rejected_at_load(entry):
    with pytest.raises(DesignResourceManifestError):
        validate_data_entry(entry)


def test_containment_predicate_rejects_entries_outside_the_skill_root(tmp_path):
    """Exercised directly, so it holds on hosts that cannot create symlinks.

    The symlink test below needs an elevated privilege on Windows and skips
    without it. Leaving the guard unexercised exactly where the escape risk is
    live would be the wrong trade, so the predicate is also tested against real
    out-of-root paths that need no special privilege.
    """
    skill_root = tmp_path / "skill"
    (skill_root / "data").mkdir(parents=True)
    inside = skill_root / "data" / "styles.csv"
    inside.write_text("a,b\n1,2\n", encoding="utf-8")
    outside = tmp_path / "secrets" / "passwd"
    outside.parent.mkdir(parents=True)
    outside.write_text("root:x:0:0\n", encoding="utf-8")

    assert entry_is_contained(skill_root, inside) is True
    assert entry_is_contained(skill_root, outside) is False
    # A prefix-sharing sibling must not pass: "skill-evil" starts with "skill".
    sibling = tmp_path / "skill-evil" / "data.csv"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("a,b\n1,2\n", encoding="utf-8")
    assert entry_is_contained(skill_root, sibling) is False
    # The root itself is not strictly inside itself.
    assert entry_is_contained(skill_root, skill_root) is True


def test_symlink_escaping_the_skill_root_is_unavailable(tmp_path):
    """The resolved half of containment.

    A symlink INSIDE the skill directory passes every string check — it is
    relative, has no '..', and exists — and lands outside the root. Only
    resolving it catches the alias.
    """
    home = tmp_path / "profile-home"
    skills_dir = design_profile_skills_dir(home)
    skill_dir = _make_skill(skills_dir, "ui-ux-pro-max")

    outside = tmp_path / "outside" / "styles.csv"
    outside.parent.mkdir(parents=True)
    outside.write_text("name,css\nbutton,padding\n", encoding="utf-8")

    entry = skill_dir / "data" / "styles.csv"
    entry.parent.mkdir(parents=True, exist_ok=True)
    try:
        entry.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this host")

    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (skill_dir / "scripts" / "search.py").write_text("x = 1\n", encoding="utf-8")

    report = resolve_design_capabilities(home, load_design_resource_manifest())
    capability = report.resources[REQUIRED_SKILL]
    assert capability.available is False
    assert not report.ok


def test_entries_beyond_data_entries_max_are_rejected():
    document = _minimal_manifest()
    document["data_entries_max"] = 2
    document["resources"][REQUIRED_SKILL]["data_entries"] = [
        "a.py",
        "b.py",
        "c.py",
    ]
    with pytest.raises(DesignResourceManifestError):
        parse_design_resource_manifest(document)


def test_deferred_resource_cannot_declare_data_entries():
    """Nothing would verify them — declaring them would be an unchecked claim."""
    document = _minimal_manifest(
        resources={
            "thing": {
                "kind": "skill",
                "required": False,
                "resolution": "deferred",
                "skill_name": "thing",
                "data_entries": ["data.csv"],
            }
        }
    )
    with pytest.raises(DesignResourceManifestError):
        parse_design_resource_manifest(document)


# ---------------------------------------------------------------------------
# 9. Names are never evidence of availability
# ---------------------------------------------------------------------------


def test_names_in_skill_data_never_provision_on_demand_resources(tmp_path):
    """UI UX Pro Max's data NAMES shadcn/gsap/three/lenis as guidance.

    A resolver that scanned for those names would report them installed. They
    are on-demand project resources; absent is their correct resting state and
    must never read as available.
    """
    home = tmp_path / "profile-home"
    _make_skill(
        design_profile_skills_dir(home),
        "ui-ux-pro-max",
        skill_md=(
            "---\nname: ui-ux-pro-max\ndescription: d\n---\n\n"
            "Use shadcn, gsap, three and lenis for animation.\n"
        ),
        data_entries={
            "scripts/search.py": "x = 1\n",
            "data/styles.csv": (
                "name,library\n"
                "fade-in,gsap\n"
                "parallax,three\n"
                "smooth-scroll,lenis\n"
                "button,shadcn\n"
            ),
        },
    )

    report = resolve_design_capabilities(home, load_design_resource_manifest())

    assert report.resources[REQUIRED_SKILL].status == STATUS_AVAILABLE
    for resource_id in ON_DEMAND_RESOURCES:
        capability = report.resources[resource_id]
        assert capability.available is False, f"{resource_id} must not be inferred installed"
        assert capability.status == STATUS_NOT_INSTALLED


def test_every_on_demand_resource_reports_not_installed_when_absent(manifest):
    """Status is keyed off the install_mode DECLARATION, not the kind.

    ``shadcn`` is a registry, not an npm package, and is provisioned per project
    exactly as the npm ones are — so it shares their resting state. Scoping
    ``not_installed`` to ``npm_optional`` would report a permanent, unactionable
    degradation for every registry resource on every single run.
    """
    for resource_id in ON_DEMAND_RESOURCES:
        resource = manifest.get(resource_id)
        assert resource.install_mode == "project_on_demand"
        assert resource.is_on_demand

    statuses = capability_statuses(manifest)
    for resource_id in ON_DEMAND_RESOURCES:
        assert statuses[resource_id] == STATUS_NOT_INSTALLED
        assert statuses[resource_id] != STATUS_UNAVAILABLE_OPTIONAL


def test_on_demand_resources_never_reach_the_degraded_list(tmp_path):
    home = tmp_path / "profile-home"
    _make_skill(
        design_profile_skills_dir(home),
        "ui-ux-pro-max",
        data_entries={"scripts/search.py": "x = 1\n", "data/styles.csv": "a,b\n1,2\n"},
    )
    report = resolve_design_capabilities(home, load_design_resource_manifest())
    for resource_id in ON_DEMAND_RESOURCES:
        assert resource_id not in report.degraded


# ---------------------------------------------------------------------------
# 10. Bounded diagnostics / no payload leakage
# ---------------------------------------------------------------------------


def test_capability_result_is_bounded_and_payload_free(tmp_path):
    """No file content, and no absolute paths except the one skills dir."""
    secret = "SECRET-DESIGN-DATASET-CONTENT"
    home = tmp_path / "profile-home"
    _make_skill(
        design_profile_skills_dir(home),
        "ui-ux-pro-max",
        data_entries={
            "scripts/search.py": f"# {secret}\n",
            "data/styles.csv": f"a,b\n{secret},2\n",
        },
    )
    report = resolve_design_capabilities(home, load_design_resource_manifest())
    rendered = yaml.safe_dump(report.to_dict())

    assert secret not in rendered
    for capability in report.resources.values():
        assert "\\" not in capability.detail and "/" not in capability.detail
        assert str(home) not in capability.detail
    assert set(report.to_dict()["resources"][REQUIRED_SKILL]) == {
        "kind",
        "required",
        "configured",
        "available",
        "status",
        "detail",
    }


def test_manifest_does_not_embed_user_specific_paths():
    """No operator home may be baked into the shared manifest."""
    text = SHIPPED_MANIFEST.read_text(encoding="utf-8")
    assert "/home/" not in text
    assert "C:\\Users\\" not in text
    assert "albertus527" not in text


# ---------------------------------------------------------------------------
# 11. Runtime wiring
# ---------------------------------------------------------------------------


def test_runtime_does_not_start_when_a_required_capability_is_missing(monkeypatch):
    """The preflight must be wired, not merely implemented."""
    from app import runtime as runtime_module

    home = Path("C:/nonexistent-profile-home-for-preflight-test")
    monkeypatch.setattr(
        runtime_module.HermesAdapter, "validate_role_configuration",
        lambda self: {"ok": True, "roles": {}, "errors": {}, "config_path": "x"},
    )
    monkeypatch.setattr(
        runtime_module, "preflight_node_toolchain", lambda *a, **k: None
    )
    monkeypatch.setattr(
        runtime_module, "preflight_smoke_support", lambda *a, **k: None
    )

    config = runtime_module.RuntimeConfig(
        telegram_bot_token="t",
        hermes_home=home,
        workspace_root=home / "ws",
        state_root=home.parent / "state",
        output_repo_path=home / "out",
        vercel_token="v",
        vercel_team_id="team",
        vercel_ownership_namespace="ns",
    )
    started = []
    monkeypatch.setattr(runtime_module, "compose", lambda cfg: started.append(1))

    assert runtime_module.main([]) == 1
    assert not started, "runtime must not compose when a required capability is absent"


def test_preflight_reports_manifest_failure_rather_than_raising(monkeypatch):
    from app.core.design_capabilities import preflight_design_capabilities as real

    def boom(*args, **kwargs):
        raise DesignResourceManifestError("manifest is invalid")

    monkeypatch.setattr("app.core.design_capabilities.load_design_resource_manifest", boom)
    with pytest.raises(DesignResourceManifestError):
        real(Path("C:/whatever"))