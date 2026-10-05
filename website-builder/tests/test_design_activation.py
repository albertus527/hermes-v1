"""Batch D3a.5 Part C: the multi-dimensional activation capability model.

Real temporary profile directories with real skill trees. **No network, no
subprocess, no install** -- asserted directly, not merely by convention, because
"capability resolution is offline" is the property that lets this run at startup
on every host without making startup depend on a remote answer.

The properties under test are BEHAVIOUR CONTRACTS:

    * capability resolution opens no socket and starts no process
    * a resource reports SEVERAL simultaneous capabilities, not one enum state
      (21st: free metadata discovery while authenticated retrieval is absent)
    * credential PRESENCE is never confused with credential VALUE
    * a declared-but-unactivated resource cannot acquire capability
    * an optional absent resource never fails the report
    * serialization is deterministic, bounded, and free of paths/secrets
    * the Impeccable engine is resolved through a CLOSED platform mapping and
      verified SEPARATELY from the stable manifest artifacts
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_activation import (
    ACTIVATION_REASONS,
    CREDENTIAL_ENV_NAMES,
    INSTALLABLE_ON_DEMAND,
    REASON_ENGINE_MISSING,
    REASON_NO_LIVE_ADAPTER,
    REASON_NO_REVIEWED_COMPONENT,
    REASON_ENGINE_VERIFIED,
    REASON_LOCALLY_PROVISIONED,
    REASON_SKILL_ARTIFACTS_MISSING,
    REASON_SKILL_NOT_PROVISIONED,
    ResourceActivationCapability,
    activate_design_resources,
    activate_resource,
    credential_present,
    engine_relative_paths,
    resolve_engine_path,
)
from app.core.design_activation import _has_live_discovery_adapter
from app.core.design_resources import (
    DesignResource,
    load_design_resource_manifest,
)

#: The shipped manifest, resolved relative to THIS FILE rather than the repo
#: root. The mutation drivers copy the tree to a temp directory, where the repo
#: root layout does not exist; a path built from ``parents[2]`` would then raise
#: at fixture time and make every mutation look "invalidated" rather than
#: actually exercising its guard.
SHIPPED_MANIFEST = (
    Path(__file__).resolve().parent.parent / "config" / "design_resources.yaml"
)

GUIDANCE = "ui_ux_pro_max"
REFERO = "refero"
IMPECCABLE = "impeccable"
TWENTY_FIRST = "twenty_first"
REACT_BITS = "react_bits"
TRANSITIONS = "transitions_dev"
SHADCN = "shadcn"
THREE = "three"

LINUX = ("linux", "x86_64")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail loudly if capability resolution reaches for a socket.

    This is the load-bearing property of Part C: activation answers "is this
    available HERE", from local state and credential PRESENCE. A registry lookup
    here would make every startup depend on a remote answer and would let a
    remote response decide a capability.
    """

    def deny(*args, **kwargs):
        raise AssertionError("network access is forbidden in activation tests")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)


@pytest.fixture
def manifest():
    return load_design_resource_manifest(SHIPPED_MANIFEST)


@pytest.fixture
def home(tmp_path) -> Path:
    root = tmp_path / "profile"
    (root / "skills").mkdir(parents=True)
    return root


def _make_skill(
    home: Path,
    skill_name: str,
    *,
    files=("SKILL.md",),
    body="content\n",
) -> Path:
    skill_dir = home / "skills" / skill_name
    for entry in files:
        target = skill_dir / entry
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return skill_dir


def _manifest_resource(
    resource_id: str, *, skill_name: str, data_entries=()
) -> DesignResource:
    return DesignResource(
        resource_id=resource_id,
        kind="skill",
        required=False,
        resolution="profile_skill",
        skill_name=skill_name,
        data_entries=tuple(data_entries),
    )


def _activate(resource: DesignResource, home: Path, *, system="linux", machine="x86_64"):
    return activate_resource(home, resource, system=system, machine=machine)


# ---------------------------------------------------------------------------
# Offline by construction
# ---------------------------------------------------------------------------


def test_activation_runs_no_subprocess(monkeypatch, home, manifest):
    """Activation inspects state; it never executes anything."""

    def boom(*args, **kwargs):
        raise AssertionError("activation must not start a subprocess")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)

    report = activate_design_resources(home, manifest, system="linux", machine="x86_64")

    assert report.capabilities


def test_activation_touches_no_remote_catalog(monkeypatch, home, manifest):
    """No HTTP client is constructed during capability resolution."""
    import urllib.request

    def boom(*args, **kwargs):
        raise AssertionError("activation must not perform HTTP")

    monkeypatch.setattr(urllib.request, "urlopen", boom)

    activate_design_resources(home, manifest, system="linux", machine="x86_64")


# ---------------------------------------------------------------------------
# Multi-dimensional, not one enum state
# ---------------------------------------------------------------------------


def test_a_resource_can_report_several_capabilities_at_once():
    """The whole reason this model exists: an enum would force a lie.

    Read back through ``to_dict`` rather than off the dataclass attributes,
    because the serialized form is what D2 and the diagnostics actually consume
    -- an enum collapse there would be invisible to a caller reading the object.
    """
    capability = ResourceActivationCapability(
        resource_id="example",
        discovery_available=True,
        retrieval_available=True,
        install_available=True,
        reasons=(),
    )

    payload = capability.to_dict()
    assert payload["discovery_available"] is True
    assert payload["retrieval_available"] is True
    assert payload["install_available"] is True
    assert payload["critic_available"] is False, "unset axes stay false, not None"


def test_serialization_carries_every_axis_independently():
    """No axis is derived from another on the way out."""
    for field, kwargs in (
        ("discovery_available", {"discovery_available": True}),
        ("retrieval_available", {"retrieval_available": True}),
        ("install_available", {"install_available": True}),
        ("critic_available", {"critic_available": True}),
    ):
        payload = ResourceActivationCapability(resource_id="x", **kwargs).to_dict()
        assert payload[field] is True, field
        others = [
            axis
            for axis in (
                "discovery_available",
                "retrieval_available",
                "install_available",
                "critic_available",
            )
            if axis != field
        ]
        assert not any(payload[axis] for axis in others), (field, payload)


def test_free_search_and_paid_retrieval_are_separate_axes(home, monkeypatch):
    """21st: metadata search is free, component retrieval is not.

    Both facts are true simultaneously. Forcing one state would discard the
    other and make the capability report wrong about a working resource.
    """
    monkeypatch.delenv("TWENTY_FIRST_API_KEY", raising=False)
    monkeypatch.delenv("TWENTYFIRST_API_KEY", raising=False)

    capability = _activate(_manifest_resource(TWENTY_FIRST, skill_name="x"), home)

    assert capability.discovery_available is True, "free metadata search works"
    assert capability.retrieval_available is False, "paid retrieval is gated"
    assert capability.install_available is False
    assert capability.authentication_required is True
    assert capability.authentication_present is False
    assert capability.degraded is False, "a designed free tier is not a degradation"


def test_a_present_credential_enables_the_authenticated_axis(home, monkeypatch):
    """Presence flips exactly the axes a credential gates -- and no more.

    It does NOT enable INSTALL. Nothing is reviewed for 21st.dev, so its install
    axis is closed by review, not by the credential. The previous revision
    asserted ``install_available is True`` off the back of a credential, which
    claimed an install path for a source with an empty allowlist.
    """
    monkeypatch.setenv("TWENTY_FIRST_API_KEY", "present-not-verified")

    capability = _activate(_manifest_resource(TWENTY_FIRST, skill_name="x"), home)

    assert capability.authentication_present is True
    assert capability.retrieval_available is True
    assert capability.install_available is False, (
        "no 21st component is reviewed, so install must not be claimed"
    )
    assert REASON_NO_REVIEWED_COMPONENT in capability.reasons
    assert capability.degraded is False


def test_discovery_is_only_reported_when_a_live_adapter_exists(home, monkeypatch):
    """A declared catalog with no adapter behind it is not a capability.

    The VPS-found defect: discovery was reported available purely because a
    normalizer existed. This asserts the axis is now driven by the adapter.
    """
    import app.core.design_activation as activation

    capability = _activate(_manifest_resource(REACT_BITS, skill_name="x"), home)
    assert capability.discovery_available is True, "the adapter exists in this build"

    monkeypatch.setattr(activation, "_has_live_discovery_adapter", lambda: False)
    without = _activate(_manifest_resource(REACT_BITS, skill_name="x"), home)

    assert without.discovery_available is False
    assert without.retrieval_available is False
    assert without.degraded is True
    assert REASON_NO_LIVE_ADAPTER in without.reasons


def test_the_live_adapter_probe_is_local_and_socket_free():
    """Capability resolution must never touch the network to answer."""
    assert _has_live_discovery_adapter() is True

    import app.core.design_catalog_fetch as fetch

    source = Path(fetch.__file__).read_text(encoding="utf-8")
    # The adapter module itself performs I/O, but the PROBE must not call it.
    assert callable(fetch.discover_catalog)


def test_react_bits_reports_install_because_one_component_is_reviewed(home):
    """The install axis tracks review, and SplitText is reviewed."""
    capability = _activate(_manifest_resource(REACT_BITS, skill_name="x"), home)

    assert capability.install_available is True
    assert REASON_NO_REVIEWED_COMPONENT not in capability.reasons


def test_an_unauthenticated_resource_needs_no_credential(home, monkeypatch):
    """React Bits publishes a catalog and registry entries with no credential."""
    for name in CREDENTIAL_ENV_NAMES[REACT_BITS]:
        monkeypatch.delenv(name, raising=False)

    capability = _activate(_manifest_resource(REACT_BITS, skill_name="x"), home)

    assert capability.discovery_available is True
    assert capability.retrieval_available is True
    assert capability.authentication_required is False


def test_usable_means_at_least_one_capability():
    """A resource with every axis false is not usable, and says so."""
    assert ResourceActivationCapability(resource_id="x").usable is False
    assert (
        ResourceActivationCapability(resource_id="x", critic_available=True).usable
        is True
    )


# ---------------------------------------------------------------------------
# Credential presence never means the value
# ---------------------------------------------------------------------------


def test_credential_presence_ignores_blank_values():
    """An empty credential cannot work, so it counts as absent."""
    assert credential_present(("X_KEY",), source={"X_KEY": ""}) is False
    assert credential_present(("X_KEY",), source={"X_KEY": "   "}) is False
    assert credential_present(("X_KEY",), source={}) is False


def test_credential_presence_accepts_any_configured_name():
    assert credential_present(("A", "B"), source={"B": "value"}) is True


def test_the_capability_record_never_carries_a_credential_value(home, monkeypatch):
    """Presence is a boolean; the value must not escape into the report."""
    secret = "sk-super-secret-value"
    monkeypatch.setenv("TWENTY_FIRST_API_KEY", secret)

    capability = _activate(_manifest_resource(TWENTY_FIRST, skill_name="x"), home)
    rendered = json.dumps(capability.to_dict())

    assert secret not in rendered
    assert capability.authentication_present is True


def test_no_credential_name_is_a_deployment_credential():
    """A design corpus has no business holding a release credential."""
    forbidden = ("VERCEL", "GITHUB", "TELEGRAM", "NINEROUTER", "HOSTINGER", "STRIX")
    for resource_id, names in CREDENTIAL_ENV_NAMES.items():
        for name in names:
            assert not any(
                token in name.upper() for token in forbidden
            ), (resource_id, name)


# ---------------------------------------------------------------------------
# Declared is not capable
# ---------------------------------------------------------------------------


def test_a_declared_but_unactivated_resource_claims_nothing(home):
    """The anti-placeholder property: a manifest row grants no capability."""

    capability = _activate(_manifest_resource("some_new_resource", skill_name="x"), home)

    assert capability.usable is False
    assert capability.to_dict()["reasons"]


def test_an_absent_optional_resource_never_fails_the_report(home, manifest):
    """Optional absence is not a startup blocker."""
    report = activate_design_resources(home, manifest, system="linux", machine="x86_64")

    assert IMPECCABLE in report.capabilities, "absent resources are still reported"
    assert IMPECCABLE not in report.failures


def test_an_on_demand_resource_is_installable_not_degraded(manifest):
    """Its resting state is "not added yet", not a permanent degradation."""
    resource = manifest.get(SHADCN)
    capability = activate_resource(
        Path("."), resource, system="linux", machine="x86_64"
    )

    assert capability.install_available is True
    assert capability.degraded is False
    assert SHADCN in INSTALLABLE_ON_DEMAND


def test_on_demand_resources_share_one_install_axis(manifest):
    """Registry and npm resources report the same shape, by design."""
    for resource_id in sorted(INSTALLABLE_ON_DEMAND):
        capability = activate_resource(
            Path("."), manifest.get(resource_id), system="linux", machine="x86_64"
        )
        assert capability.install_available is True, resource_id
        assert capability.usable is True, resource_id


# ---------------------------------------------------------------------------
# Local skill resources
# ---------------------------------------------------------------------------


def test_an_absent_skill_reports_honest_absence(home):
    capability = _activate(_manifest_resource(REFERO, skill_name="refero-design"), home)

    assert capability.usable is False
    assert capability.locally_provisioned is False
    assert REASON_SKILL_NOT_PROVISIONED in capability.reasons


def test_a_placeholder_skill_directory_is_not_a_capability(home):
    """SKILL.md alone is the placeholder case the check exists to catch."""
    _make_skill(home, "refero-design", files=("SKILL.md",))
    resource = _manifest_resource(
        REFERO,
        skill_name="refero-design",
        data_entries=("references/typography.md", "references/color.md"),
    )

    capability = _activate(resource, home)

    assert capability.usable is False
    assert REASON_SKILL_ARTIFACTS_MISSING in capability.reasons


def test_a_verified_local_skill_is_usable_with_no_credential(home, monkeypatch):
    """Refero's baseline is FREE; the paid tier must not gate it."""
    monkeypatch.delenv("REFERO_API_KEY", raising=False)
    _make_skill(
        home,
        "refero-design",
        files=("SKILL.md", "references/typography.md", "references/color.md"),
    )
    resource = _manifest_resource(
        REFERO,
        skill_name="refero-design",
        data_entries=("references/typography.md", "references/color.md"),
    )

    capability = _activate(resource, home)

    assert capability.discovery_available is True
    assert capability.retrieval_available is True
    assert capability.locally_provisioned is True
    assert capability.authentication_required is False, (
        "the capability being reported -- the free local craft references -- is "
        "not gated by any credential; claiming otherwise reports a blocker that "
        "does not exist"
    )
    assert capability.authentication_optional is True, (
        "the unmet paid tier is still reported, on the axis that means "
        "'optional', not 'required'"
    )
    assert capability.degraded is False, (
        "baseline works with no account; the absent paid tier is not a degradation"
    )


def test_an_empty_skill_artifact_is_not_verified(home):
    """A zero-byte file carries no instructions."""
    skill_dir = home / "skills" / "refero-design"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("real\n", encoding="utf-8")
    (skill_dir / "references" / "typography.md").write_text("", encoding="utf-8")
    resource = _manifest_resource(
        REFERO,
        skill_name="refero-design",
        data_entries=("references/typography.md",),
    )

    capability = _activate(resource, home)

    assert capability.usable is False
    assert REASON_SKILL_ARTIFACTS_MISSING in capability.reasons


# ---------------------------------------------------------------------------
# Impeccable: stable artifacts AND a separately-verified platform engine
# ---------------------------------------------------------------------------


def _impeccable_resource() -> DesignResource:
    """Cross-platform, OS-agnostic manifest artifacts only."""
    return _manifest_resource(
        IMPECCABLE,
        skill_name="impeccable",
        data_entries=("SKILL.md", "reference/critique.md", "scripts/detect.mjs"),
    )


def _provision_impeccable(home: Path, *, with_engine: bool, system="linux", machine="x86_64"):
    """Provision the REAL verified layout: SKILL.md, reference, scripts/detect.mjs.

    ``system``/``machine`` are accepted and ignored: the shipped detector is a
    cross-platform Node ESM entrypoint, so there is no per-platform artifact.
    """
    del system, machine
    _make_skill(
        home,
        "impeccable",
        files=("SKILL.md", "reference/critique.md", "scripts/detect.mjs"),
    )
    if not with_engine:
        return
    for relative in engine_relative_paths():
        engine = home / "skills" / "impeccable" / relative
        engine.parent.mkdir(parents=True, exist_ok=True)
        engine.write_text("// node entrypoint", encoding="utf-8")


def test_critic_needs_the_stable_artifacts(home):
    """Stable artifacts alone do not make a critic.

    Note the two states are genuinely different and both are reported
    honestly: an unprovisioned skill reports ``locally_provisioned=False``,
    while a provisioned skill with no engine for this platform reports
    ``locally_provisioned=True`` with ``critic_available=False``. Collapsing
    them would hide whether the fix is "provision the skill" or "add the
    engine" -- two different operator actions.
    """
    _provision_impeccable(home, with_engine=False)

    capability = _activate(_impeccable_resource(), home)

    assert capability.critic_available is False
    assert capability.locally_provisioned is True, "the skill itself IS provisioned"
    assert capability.discovery_available is True
    assert capability.install_available is False


def test_critic_needs_the_current_platform_engine(home):
    """Stable artifacts alone are not enough: the engine is the critic."""
    _provision_impeccable(home, with_engine=False)

    capability = _activate(_impeccable_resource(), home)

    assert REASON_ENGINE_MISSING in capability.reasons
    assert REASON_ENGINE_VERIFIED not in capability.reasons


def test_critic_is_available_only_when_both_verify(home):
    _provision_impeccable(home, with_engine=True)

    capability = _activate(_impeccable_resource(), home)

    assert capability.critic_available is True
    assert capability.locally_provisioned is True
    assert capability.install_available is False, (
        "Impeccable is a reviewer, never something installed into a project"
    )
    assert REASON_ENGINE_VERIFIED in capability.reasons


def test_a_missing_engine_never_downloads(home, monkeypatch):
    """No npm fallback, no launcher download, no user-home write."""
    import urllib.request

    def boom(*args, **kwargs):
        raise AssertionError("a missing engine must not trigger a download")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    _provision_impeccable(home, with_engine=False)

    capability = _activate(_impeccable_resource(), home)

    assert capability.critic_available is False
    assert capability.authentication_required is False


def test_the_engine_is_present_on_every_platform(home):
    """The shipped detector is cross-platform, so no host is special.

    Regression guard for the VPS-found defect: the official ``skill-v4.1.0``
    release contains NO ``scripts/bin/`` and no native binary, so the previous
    per-platform mapping reported a correctly provisioned skill as unavailable.
    """
    _provision_impeccable(home, with_engine=True, system="linux", machine="x86_64")

    for system, machine in (
        ("linux", "x86_64"),
        ("linux", "arm64"),
        ("darwin", "arm64"),
        ("darwin", "x86_64"),
        ("windows", "x86_64"),
        ("windows", "arm64"),
        ("plan9", "vax"),
    ):
        capability = _activate(
            _impeccable_resource(), home, system=system, machine=machine
        )
        assert capability.critic_available is True, (system, machine)


def test_an_incomplete_engine_layout_does_not_activate(home):
    """A half-provisioned skill is an honest absence, not a partial critic.

    The declared artifacts all verify, so the skill IS provisioned -- but the
    detector facade its entrypoint imports is missing, so no critic is claimed
    and the reason names the engine rather than the skill.
    """
    _make_skill(
        home,
        "impeccable",
        files=("SKILL.md", "reference/critique.md", "scripts/detect.mjs"),
    )

    capability = _activate(_impeccable_resource(), home)

    assert capability.critic_available is False
    assert capability.retrieval_available is False
    assert capability.locally_provisioned is True
    assert REASON_ENGINE_MISSING in capability.reasons


def test_a_missing_engine_is_reported_without_downloading(home, monkeypatch):
    """No clone, no artifact fetch, no npm shim, no PATH fallback."""
    _make_skill(
        home,
        "impeccable",
        files=("SKILL.md", "reference/critique.md", "scripts/detect.mjs"),
    )
    for module in ("socket", "urllib.request", "http.client"):
        monkeypatch.setitem(sys.modules, module, None)

    capability = _activate(_impeccable_resource(), home)

    assert capability.critic_available is False
    assert capability.locally_provisioned is True
    assert REASON_ENGINE_MISSING in capability.reasons


def test_the_engine_path_is_the_verified_cross_platform_entrypoint(home):
    """The resolved layout is the one upstream actually ships."""
    assert engine_relative_paths() == (
        "scripts/detect.mjs",
        "scripts/detector/detect-antipatterns.mjs",
    )
    assert resolve_engine_path(home / "skills" / "impeccable") is None


def test_the_manifest_never_names_a_platform_specific_artifact():
    """A platform literal in the manifest would break every other platform."""
    resource = _impeccable_resource()
    for entry in resource.data_entries:
        assert "bin/" not in entry, entry
        assert not any(
            token in entry
            for token in ("darwin-arm64", "linux-x64", "windows-x64", "linux-arm64")
        ), entry


def test_the_shipped_manifest_pins_the_verified_engine_entrypoint(manifest):
    """The manifest names the real Node entrypoint, not a launcher that is absent."""
    resource = manifest.resources[IMPECCABLE]
    assert "scripts/detect.mjs" in resource.data_entries
    assert "scripts/impeccable" not in resource.data_entries

# Bounded, deterministic serialization
# ---------------------------------------------------------------------------


def test_serialization_is_deterministic(home, manifest):
    """Same input, byte-identical output -- otherwise logs and diffs lie."""
    first = activate_design_resources(home, manifest, system="linux", machine="x86_64")
    second = activate_design_resources(home, manifest, system="linux", machine="x86_64")

    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(
        second.to_dict(), sort_keys=True
    )


def test_serialization_carries_no_path_and_no_secret(home, manifest, monkeypatch):
    monkeypatch.setenv("TWENTY_FIRST_API_KEY", "sk-secret")

    report = activate_design_resources(home, manifest, system="linux", machine="x86_64")
    rendered = json.dumps(report.to_dict())

    assert "sk-secret" not in rendered
    assert str(home) not in rendered


def test_every_reason_belongs_to_the_closed_set(home, manifest):
    """A reason outside the set is an unhandled state for callers."""
    report = activate_design_resources(home, manifest, system="linux", machine="x86_64")

    for capability in report.capabilities.values():
        for reason in capability.reasons:
            assert reason in ACTIVATION_REASONS, reason


def test_an_unregistered_reason_is_rejected():
    """The closed set is enforced at construction, not by convention."""
    with pytest.raises(ValueError):
        ResourceActivationCapability(resource_id="x", reasons=("something new",))


def test_the_report_covers_every_declared_resource(home, manifest):
    """A resource silently dropped from the report is invisible to operators."""
    report = activate_design_resources(home, manifest, system="linux", machine="x86_64")

    assert set(report.capabilities) == set(manifest.resources)