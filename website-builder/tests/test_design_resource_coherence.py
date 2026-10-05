"""Batch D3a.5 Part L: cross-resource coherence.

Each Part proved its own layer. This suite asks the question none of them can:
do the layers still agree with each other once they are combined?

The specific failure this guards is a DRIFT between the manifest, the adapter
table, the activation model, and the retrieval adapters -- four places that all
describe "what can this resource do" and can silently disagree.

Everything here is offline: no network, no installation, no subprocess OF OURS.

A note on the offline fixture. It bans ``subprocess.run`` and ``Popen``. It does
NOT ban ``subprocess.check_output``, because on Windows the STANDARD LIBRARY's
``platform.system()`` probes the OS version by running ``ver`` through
``check_output``. That is CPython's implementation detail, not a capability
probe of ours, and banning it would make this suite unrunnable on Windows
without proving anything true. The production contract is that
``activate_design_resources`` performs no network access and issues no
subprocess **of its own**; it resolves entirely from local state and credential
PRESENCE. Every test below passes ``system``/``machine`` explicitly, which both
avoids the stdlib probe and makes the suite host-independent.
"""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_activation import (
    ACTIVATION_REASONS,
    ResourceActivationCapability,
    activate_design_resources,
)
from app.core.design_critic import CRITIC_ARGV_SUFFIX, build_critic_argv
from app.core.design_install import (
    DEPENDENCY_COMPANION_PACKAGES,
    PINNED_CLIS,
    required_package_specs,
)
from app.core.design_registry import ALLOWED_SHADCN_COMPONENTS, resolve_registry_locator
from app.core.design_resources import DESIGN_ADAPTERS, load_design_resource_manifest
from app.core.design_retrieval import _RESOURCE_ADAPTER_IDS

#: The five resources D3a.5 exists to activate.
ACTIVATED = ("refero", "twenty_first", "react_bits", "transitions_dev", "impeccable")

#: Of those, the ones reachable with no provisioning at all.
ON_DEMAND = ("twenty_first", "react_bits", "transitions_dev")

#: Those that need the skill provisioned into the profile first.
PROFILE_SKILL = ("refero", "impeccable")

#: A fixed platform, so every test below is host-independent and never triggers
#: the stdlib `ver` probe inside platform.system().
HOST = {"system": "linux", "machine": "x86_64"}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("capability resolution must not open a socket")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)

    def deny_process(*args, **kwargs):
        raise AssertionError("capability resolution must not run a process")

    monkeypatch.setattr(subprocess, "run", deny_process)
    monkeypatch.setattr(subprocess, "Popen", deny_process)


@pytest.fixture(scope="module")
def manifest():
    return load_design_resource_manifest()


def _activate(hermes_home, manifest, **overrides):
    return activate_design_resources(hermes_home, manifest, **HOST, **overrides)


def _provision(hermes_home: Path, manifest, resource_id: str) -> Path:
    """Create the skill a `profile_skill` resource declares, under ``hermes_home``."""
    skill = Path(hermes_home) / "skills" / manifest.get(resource_id).skill_name
    for entry in manifest.get(resource_id).data_entries:
        target = skill / entry
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
    return Path(hermes_home)


def _provision_impeccable(
    hermes_home: Path, manifest, *, system: str, machine: str, with_engine: bool = True
) -> Path:
    """Provision Impeccable's skill plus its cross-platform Node engine.

    ``system``/``machine`` are accepted and ignored: the shipped detector is the
    same Node ESM entrypoint on every platform, so there is no per-OS artifact.
    """
    del system, machine
    from app.core.design_activation import engine_relative_paths

    home = _provision(hermes_home, manifest, "impeccable")
    if with_engine:
        for relative in engine_relative_paths():
            engine = home / "skills" / "impeccable" / relative
            engine.parent.mkdir(parents=True, exist_ok=True)
            engine.write_text("// node entrypoint\n", encoding="utf-8")
    return home


def _provisioned_refero(hermes_home: Path, manifest) -> Path:
    return _provision(hermes_home, manifest, "refero")


# ---------------------------------------------------------------------------
# Every layer sees the same resource set
# ---------------------------------------------------------------------------


def test_the_manifest_and_activation_agree_on_the_resource_set(manifest, tmp_path):
    assert set(_activate(tmp_path, manifest).capabilities) == set(manifest.resources)


def test_every_resource_is_resolved_even_when_absent(manifest, tmp_path):
    """An absent optional resource must be VISIBLE, not silently missing."""
    report = _activate(tmp_path, manifest)

    for resource_id in manifest.resources:
        assert resource_id in report.capabilities


def test_activation_is_local_on_an_empty_profile(manifest, tmp_path):
    report = _activate(tmp_path, manifest)

    assert isinstance(report.ok, bool)
    assert all(
        isinstance(cap, ResourceActivationCapability)
        for cap in report.capabilities.values()
    )


def test_activation_is_deterministic(manifest, tmp_path):
    assert _activate(tmp_path, manifest).to_dict() == _activate(
        tmp_path, manifest
    ).to_dict()


# ---------------------------------------------------------------------------
# The adapter tables do not disagree
# ---------------------------------------------------------------------------


def test_every_manifest_adapter_exists_in_the_closed_table(manifest):
    for resource_id, resource in manifest.resources.items():
        if resource.adapter:
            assert resource.adapter in DESIGN_ADAPTERS, resource_id


def test_every_declared_retrieval_adapter_is_a_registered_adapter_name():
    """Resolved through the real registry, not a hand-copied list.

    Part J named an adapter that design_retrieval never registered. This check
    is what caught it, so it stays: it asks the source of truth.
    """
    from app.core.design_retrieval import DESIGN_ADAPTERS as REGISTERED

    registered = {adapter.name for adapter in REGISTERED}

    for name in DESIGN_ADAPTERS:
        module_name, _ = DESIGN_ADAPTERS[name]
        if module_name == "app.core.design_retrieval":
            assert name in registered, name


def test_the_per_resource_retrieval_pins_resolve():
    for resource_id in _RESOURCE_ADAPTER_IDS:
        assert _RESOURCE_ADAPTER_IDS[resource_id].name


def test_every_resource_reached_through_retrieval_is_declared(manifest):
    """A resource the retrieval layer can read must also be a manifest resource.

    The reverse of drift: an adapter registered against a resource the manifest
    does not declare is reading something D0 never sanctioned.
    """
    declared = set(manifest.resources)

    assert set(_RESOURCE_ADAPTER_IDS) <= declared


# ---------------------------------------------------------------------------
# Each activated resource reports the capability it actually has
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("resource_id", ON_DEMAND)
def test_each_on_demand_resource_activates_without_provisioning(
    resource_id, manifest, tmp_path
):
    """No account, no local skill, no install -- and still a real capability.

    These are the resources whose whole D3a.5 point is that their ABSENCE is
    not absence of capability.
    """
    capability = _activate(tmp_path, manifest).capabilities[resource_id]

    assert capability.usable is True, f"{resource_id}: {capability.reasons}"


@pytest.mark.parametrize("resource_id", PROFILE_SKILL)
def test_an_unprovisioned_profile_skill_is_honestly_absent(
    resource_id, manifest, tmp_path
):
    """Not a failure of the test -- this is the correct answer.

    A `profile_skill` resource that has not been provisioned into the profile
    genuinely has no capability, and says so with a static reason rather than
    claiming an empty-but-working one.
    """
    capability = _activate(tmp_path, manifest).capabilities[resource_id]

    assert capability.usable is False
    assert capability.reasons


def test_the_free_refero_baseline_needs_no_credential(manifest, tmp_path):
    """Part E's whole point: the local craft references work with no account."""
    home = _provisioned_refero(tmp_path, manifest)
    capability = _activate(home, manifest).capabilities["refero"]

    assert capability.authentication_required is False
    assert capability.authentication_present is False
    assert capability.usable is True
    assert capability.degraded is False


def test_twenty_first_reports_free_discovery_without_claiming_retrieval(
    manifest, tmp_path
):
    """The case that made one boolean impossible."""
    capability = _activate(tmp_path, manifest).capabilities["twenty_first"]

    assert capability.discovery_available is True
    assert capability.retrieval_available is False
    assert capability.usable is True


def test_twenty_first_records_a_present_credential_as_presence_only(
    manifest, tmp_path, monkeypatch
):
    """Presence is reported; the value never is, and usability is unclaimed."""
    monkeypatch.setenv("TWENTY_FIRST_API_KEY", "present-but-unverified")

    capability = _activate(tmp_path, manifest).capabilities["twenty_first"]

    assert capability.authentication_present is True
    assert "present-but-unverified" not in str(capability.to_dict())


def test_transitions_is_installable_without_claiming_it_is_materialized(
    manifest, tmp_path
):
    capability = _activate(tmp_path, manifest).capabilities["transitions_dev"]

    assert capability.install_available is True
    assert capability.degraded is False


def test_impeccable_reports_a_critic_only_when_provisioned(manifest, tmp_path):
    """Unprovisioned, Impeccable has no critic -- and says so, not silence."""
    capability = _activate(tmp_path, manifest).capabilities["impeccable"]

    assert capability.critic_available is False
    assert capability.reasons


def test_impeccable_activates_once_the_skill_and_engine_exist(manifest, tmp_path):
    """The verified engine layout activates, and only if complete."""
    from app.core.design_activation import engine_relative_paths

    relative = engine_relative_paths()
    assert relative, "the engine layout is application-owned and non-empty"

    home = _provision_impeccable(
        tmp_path, manifest, system=HOST["system"], machine=HOST["machine"]
    )

    capability = _activate(home, manifest).capabilities["impeccable"]

    assert capability.critic_available is True
    assert capability.locally_provisioned is True


def test_the_critic_is_available_on_a_platform_with_no_upstream_binary(
    manifest, tmp_path
):
    """Regression guard for the VPS-found defect.

    The official skill-v4.1.0 release ships NO ``scripts/bin/`` and no native
    binary. A platform-specific engine mapping therefore reported a correctly
    provisioned skill as unavailable on every host. There is no unmapped
    platform now, because there is no per-platform artifact at all.
    """
    for system, machine in (("Linux", "pdp11"), ("Plan9", "vax"), ("Windows", "arm64")):
        home = _provision_impeccable(
            tmp_path / f"{system}-{machine}",
            manifest,
            system=system,
            machine=machine,
        )
        capability = activate_design_resources(
            home, manifest, system=system, machine=machine
        ).capabilities["impeccable"]

        assert capability.critic_available is True, (system, machine)


def test_a_skill_without_the_engine_reports_no_critic(manifest, tmp_path):
    """Honest absence: provisioned, but no engine, and no download attempted."""
    home = _provision_impeccable(
        tmp_path, manifest, system=HOST["system"], machine=HOST["machine"],
        with_engine=False,
    )

    capability = _activate(home, manifest).capabilities["impeccable"]

    assert capability.critic_available is False
    assert capability.locally_provisioned is True
    assert capability.reasons


# ---------------------------------------------------------------------------
# Pins are consistent across every table that holds them
# ---------------------------------------------------------------------------


def test_the_transitions_cli_is_pinned_and_registered():
    assert "transitions_dev" in PINNED_CLIS
    assert PINNED_CLIS["transitions_dev"].spec == "transitions-dev@0.3.0"


def test_the_impeccable_engine_is_not_a_pinned_npm_cli():
    """The npm package is a binary-download shim; the skill ships the engine."""
    assert "impeccable" not in PINNED_CLIS


def test_the_critic_argv_names_no_package():
    """The engine is the skill's own Node entrypoint, never `npm exec`."""
    argv = build_critic_argv("/usr/bin/node", Path("/skills/impeccable/scripts/detect.mjs"))

    assert "npm" not in argv
    assert "npx" not in argv
    assert argv[0] == "/usr/bin/node"
    assert argv[2:] == CRITIC_ARGV_SUFFIX


def test_three_requires_its_companion_package():
    """The Part A guarantee, asserted as a relationship.

    `three` pulls `@types/three` into devDependencies at exact pins -- that is
    what closed TS7016. Both must appear, and the companion must land in the
    DEV section, because a types package in `dependencies` would ship to
    production and a bare `three` alone would not compile.
    """
    specs = required_package_specs("three")
    by_package = {spec.package: spec for spec in specs}

    assert by_package["three"].dependency_section == "dependencies"
    companion = by_package["@types/three"]
    assert companion.dependency_section == "devDependencies"
    assert companion.version
    assert "three" in DEPENDENCY_COMPANION_PACKAGES


def test_the_companion_table_is_closed_and_small():
    """A generic @types/<pkg> rule would invent a package per dependency."""
    assert set(DEPENDENCY_COMPANION_PACKAGES) == {"three"}


def test_the_builtin_component_allowlist_is_closed():
    """The external and builtin paths are separate by construction."""
    assert "button" in ALLOWED_SHADCN_COMPONENTS
    assert resolve_registry_locator("shadcn_external", "button") is None


# ---------------------------------------------------------------------------
# Nothing leaks
# ---------------------------------------------------------------------------


def test_the_activation_report_serializes_without_the_home_path(manifest, tmp_path):
    rendered = str(_activate(tmp_path, manifest).to_dict())

    assert str(tmp_path) not in rendered


def test_a_credential_value_never_reaches_the_report(manifest, tmp_path, monkeypatch):
    monkeypatch.setenv("TWENTY_FIRST_API_KEY", "sk-super-secret-value")

    assert "sk-super-secret-value" not in str(_activate(tmp_path, manifest).to_dict())


def test_credential_presence_is_reported_as_a_boolean_only(manifest, tmp_path, monkeypatch):
    monkeypatch.setenv("TWENTY_FIRST_API_KEY", "sk-super-secret-value")

    capability = _activate(tmp_path, manifest).capabilities["twenty_first"]
    payload = capability.to_dict()

    assert payload["authentication_present"] is True
    assert "sk-super-secret-value" not in str(payload)


def test_every_activation_reason_is_from_the_closed_vocabulary(manifest, tmp_path):
    for capability in _activate(tmp_path, manifest).capabilities.values():
        for reason in capability.reasons:
            assert reason in ACTIVATION_REASONS


def test_the_missing_required_skill_is_reported_not_hidden(manifest, tmp_path):
    """`ui_ux_pro_max` is required and absent: visible, not swallowed."""
    report = _activate(tmp_path, manifest)

    assert "ui_ux_pro_max" in report.degraded or "ui_ux_pro_max" in report.failures