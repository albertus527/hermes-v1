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

#: Of those, the ones reachable with no provisioning AND no credential.
#:
#: 21st is deliberately NOT here: its real machine surface is authenticated
#: (verified live -- HTTP 401 without a Bearer key) and its public llms.txt
#: publishes no component-identity schema, so it needs a credential. Its
#: credential-gated state is asserted separately below.
ON_DEMAND = ("react_bits", "transitions_dev")

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
    hermes_home: Path,
    manifest,
    *,
    system: str,
    machine: str,
    with_engine: bool = True,
    with_parser_runtime: bool = True,
) -> Path:
    """Provision Impeccable's skill plus its cross-platform Node engine.

    ``system``/``machine`` are accepted and ignored: the shipped detector is the
    same Node ESM entrypoint on every platform, so there is no per-OS artifact.

    ``with_parser_runtime`` defaults True here so the helper produces a
    FULL-QUALITY critic (engine + parser runtime). A test that wants the degraded
    state passes ``with_parser_runtime=False``.
    """
    del system, machine
    from app.core.design_activation import (
        PARSER_RUNTIME_PACKAGES,
        engine_relative_paths,
    )

    home = _provision(hermes_home, manifest, "impeccable")
    if with_engine:
        for relative in engine_relative_paths():
            engine = home / "skills" / "impeccable" / relative
            engine.parent.mkdir(parents=True, exist_ok=True)
            engine.write_text("// node entrypoint\n", encoding="utf-8")
    if with_engine and with_parser_runtime:
        modules = home / "skills" / "impeccable" / "node_modules"
        for package in PARSER_RUNTIME_PACKAGES:
            (modules / package).mkdir(parents=True, exist_ok=True)
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


def test_twenty_first_is_credential_gated_not_free_discovery(manifest, tmp_path):
    """21st's discovery AND retrieval both require a credential.

    VERIFIED LIVE: the real machine surface is authenticated (HTTP 401 without a
    Bearer key) and the public index publishes no component-identity schema. So
    with no credential there is no discovery -- a bounded credential-required,
    degraded state. A present credential restores discovery but never install
    (nothing is reviewed for 21st).
    """
    capability = _activate(tmp_path, manifest).capabilities["twenty_first"]

    assert capability.discovery_available is False
    assert capability.retrieval_available is False
    assert capability.authentication_required is True
    assert capability.degraded is True
    assert capability.usable is False


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


# ---------------------------------------------------------------------------
# Capability truth MATCHES the execution requirement (item 2)
# ---------------------------------------------------------------------------
# The capability layer and the execution layer must answer the same question the
# same way: does using this resource need a credential? The adapter owns the
# closed tables (CREDENTIAL_REQUIRED_FOR_DISCOVERY / _RETRIEVAL); the capability
# layer must report exactly that -- no more, no less. These are REGRESSION
# assertions: they fail if either layer drifts from the other.


def _catalog_ids():
    from app.core.design_registry import SOURCE_REACT_BITS, SOURCE_TWENTY_FIRST

    return (SOURCE_TWENTY_FIRST, SOURCE_REACT_BITS)


@pytest.mark.parametrize("source", _catalog_ids())
def test_capability_auth_requirement_matches_the_adapter_table(source, manifest, tmp_path):
    """authentication_required is the adapter's own answer, not a second opinion."""
    from app.core.design_catalog_fetch import credential_requirement

    disc_auth, retr_auth = credential_requirement(source)
    capability = _activate(tmp_path, manifest).capabilities[source]

    assert capability.authentication_required is (disc_auth or retr_auth), source


@pytest.mark.parametrize("source", _catalog_ids())
def test_no_credential_closes_exactly_the_gated_axes(source, manifest, tmp_path):
    """With no credential, each axis matches what its executor actually needs."""
    from app.core.design_catalog_fetch import credential_requirement

    disc_auth, retr_auth = credential_requirement(source)
    capability = _activate(tmp_path, manifest).capabilities[source]

    if disc_auth:
        assert capability.discovery_available is False, source
    if retr_auth:
        assert capability.retrieval_available is False, source


@pytest.mark.parametrize("source", _catalog_ids())
def test_a_present_credential_opens_exactly_the_gated_axes(
    source, manifest, tmp_path, monkeypatch
):
    """A configured credential must restore precisely the axes it gates."""
    from app.core.design_catalog_fetch import credential_requirement
    from app.core.design_resources import CREDENTIAL_ENV_NAMES

    names = CREDENTIAL_ENV_NAMES.get(source, ())
    if not names:
        pytest.skip(f"{source} needs no credential")

    for name in names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(names[0], "present-not-verified")

    disc_auth, retr_auth = credential_requirement(source)
    capability = _activate(tmp_path, manifest).capabilities[source]

    assert capability.authentication_present is True
    if disc_auth:
        assert capability.discovery_available is True, source
    if retr_auth:
        assert capability.retrieval_available is True, source


def test_install_is_gated_by_review_not_by_a_credential(manifest, tmp_path, monkeypatch):
    """A credential never confers install: review does.

    Nothing is reviewed for 21st, so install stays closed even with a present
    credential -- the install axis must track the reviewed allowlist, not the key.
    """
    from app.core.design_registry import (
        SOURCE_TWENTY_FIRST,
        approved_registry_components,
    )

    monkeypatch.setenv("TWENTY_FIRST_API_KEY", "present-not-verified")
    capability = _activate(tmp_path, manifest).capabilities["twenty_first"]

    assert approved_registry_components(SOURCE_TWENTY_FIRST) == ()
    assert capability.install_available is False


def test_a_credential_gated_resource_is_degraded_without_its_credential(
    manifest, tmp_path
):
    """The blanket consequence of item 2: an unmet required credential degrades.

    For every resource whose execution needs a credential, the capability layer
    must report the reduction (degraded) when that credential is absent -- not a
    clean, fully-available state.
    """
    from app.core.design_catalog_fetch import credential_requirement

    report = _activate(tmp_path, manifest)
    for resource_id, capability in report.capabilities.items():
        disc_auth, retr_auth = credential_requirement(resource_id)
        if not (disc_auth or retr_auth):
            continue
        if capability.authentication_present:
            continue
        assert capability.authentication_required is True, resource_id
        assert capability.degraded is True, resource_id


def test_every_installable_on_demand_resource_has_a_real_mechanism():
    """install_available=True for an on-demand dep means a mechanism exists.

    The execution requirement differs by kind -- an npm package builds an
    install argv, the registry CLI builds a registry request -- so the capability
    must not claim install without the matching mechanism.
    """
    from app.core.design_activation import INSTALLABLE_ON_DEMAND
    from app.core.design_install import build_install_argv

    for dependency_id in INSTALLABLE_ON_DEMAND:
        if dependency_id == "shadcn":
            # The registry CLI has no npm argv; its mechanism is the registry.
            from app.core.design_registry import ALLOWED_SHADCN_COMPONENTS

            assert ALLOWED_SHADCN_COMPONENTS, dependency_id
            continue
        argv = build_install_argv(("npm", "install"), dependency_id)
        assert argv, dependency_id


# ---------------------------------------------------------------------------
# Part L: the manual LIVE smokes must stay runnable
# ---------------------------------------------------------------------------
# The audit doc records manual live smokes (Part L). They are NOT run in this
# suite -- the default suite is offline (no non-loopback network) -- but the
# COMMANDS must keep naming real symbols and the real CLI pin, or a reader
# following the doc would hit an ImportError or an unpinned CLI. This guard is
# the doc's anti-rot check: it resolves every symbol the documented commands
# import, and pins the CLI version the documented `npm exec` lines hard-code.


#: The audit doc that records the manual live smokes.
LIVE_SMOKE_DOC = (
    Path(__file__).resolve().parents[1] / "docs" / "D3A5_DEPENDENCY_INGRESS_AUDIT.md"
)


def test_the_live_smoke_doc_exists_and_has_the_part_l_section():
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "Part L" in text
    assert "manual LIVE smokes" in text
    # the ordering requirement is the whole point of Part L
    assert "AFTER the unit suite" in text or "after the unit" in text.lower()


def test_every_symbol_the_documented_smokes_import_exists():
    """Each `from app.core... import ...` the doc's commands use must resolve."""
    import importlib
    import re

    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")
    # Join shell line-continuations, then find import statements. A name list
    # ends at a `;`, a quote, or the end of the joined text.
    joined = text.replace("\\\n", " ").replace("\n", " ")
    pattern = re.compile(r"from\s+(app\.core\.[\w.]+)\s+import\s+([\w,\s]+?)\s*(?:;|\"|$)")

    def _clean(raw: str) -> list:
        # strip `as <alias>` from each name and drop empties
        out = []
        for chunk in raw.split(","):
            name = chunk.strip().split(" as ")[0].strip()
            if name:
                out.append(name)
        return out

    pairs = [(m.group(1), _clean(m.group(2))) for m in pattern.finditer(joined)]

    assert pairs, "the doc must document at least one app.core import"
    for module, names in pairs:
        resolved = importlib.import_module(module)
        for name in names:
            assert hasattr(resolved, name), f"{module} has no {name}"


def test_the_documented_cli_pin_matches_the_application_pin():
    """The doc hard-codes `transitions-dev@0.3.0`; it must match PINNED_CLIS."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")
    pinned = PINNED_CLIS["transitions_dev"]

    assert f"{pinned.package}@{pinned.version}" in text
    # and the binary the doc invokes is the pinned binary
    assert f"-- {pinned.binary} list" in text
    assert f"-- {pinned.binary} add" in text


def test_the_documented_delta_verifier_symbols_exist():
    """The L.4 command imports the reusable verifier -- it must exist."""
    from app.core.design_install import (
        snapshot_direct_dependency_state,
        verify_direct_dependency_delta,
    )

    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")
    assert "snapshot_direct_dependency_state" in text
    assert "verify_direct_dependency_delta" in text
    assert callable(snapshot_direct_dependency_state)
    assert callable(verify_direct_dependency_delta)


def test_the_live_smoke_doc_has_no_vacuous_self_comparison():
    """The no-delta proof must be a check that CAN fail.

    `git diff --no-index package.json package.json` compares a file to ITSELF:
    it exits 0 with empty output no matter what changed. Documenting it as the
    "unchanged" proof would be a check whose enforcement is weaker than the
    property it names -- the exact defect class this batch fixes.
    """
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "git diff --no-index package.json package.json" not in text
    # the real check compares against a SEPARATE baseline
    assert "cmp -s" in text
    assert "BASELINE" in text


def test_the_live_smoke_uses_a_fresh_disposable_dir():
    """`mkdir -p` reuses state; the doc must use a fresh dir per run."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "mktemp -d" in text
    # the old re-usable path must be gone
    assert "mkdir -p /tmp/partL" not in text


def test_the_live_smoke_doc_requires_the_venv():
    """pytest lives only in the venv; the doc must say so explicitly."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "source .venv/bin/activate" in text
    assert "REQUIRED" in text


def test_the_live_smoke_doc_does_not_hard_code_a_test_count():
    """A hard-coded count self-drifts on every test added; keep it qualitative."""
    import re

    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")
    # no `# <N> passed, <N> skipped` comment beside the pytest line
    assert not re.search(r"pytest tests/ -q\s+#\s*\d+ passed", text)


def test_the_documented_21st_discovery_command_is_present_and_exact():
    """The VPS command for 21st real discovery must document the real surface.

    The credential requirement is load-bearing: 21st's only machine surface is the
    authenticated REST search. The documented command must name it, and must not
    reintroduce the "free tier" claim the batch removed.
    """
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "21st REAL discovery" in text
    assert "/api/v1/components/search" in text
    # the real credential names, not invented ones
    assert "API_KEY_21ST" in text
    assert "TWENTYFIRST_TOKEN" in text
    # the removed false claim must not come back
    assert "free tier" not in text.lower() or "no free tier" in text.lower()


def test_the_documented_discovery_never_echoes_the_credential():
    """The documented command prints presence, never the value."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    # presence is printed; the value is only ever put in a header
    assert "credential_present" in text
    assert "Authorization: Bearer" in text
    # no documented command interpolates the secret into a print
    for line in text.splitlines():
        if "print(" in line and "$API_KEY_21ST" in line:
            raise AssertionError(f"a documented print echoes the secret: {line!r}")


def test_the_documented_discovery_states_the_proposal_not_install_rule():
    """A discovered component is a proposal; only review makes it installable."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "PROPOSAL, not an install" in text
    assert "approved_registry_components" in text


def test_the_documented_react_bits_command_states_no_credential():
    """React Bits is the contrast case: discovery AND retrieval need no credential."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "React Bits — real discovery + retrieval, NO credential" in text
    assert "llms.txt" in text
    assert "reactbits.dev/r/" in text


def test_the_documented_react_bits_command_states_discovery_is_not_installability():
    """64 discovered, exactly 1 installable -- the reviewed contract is the gate."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "Discovery is not installability" in text
    assert "installable_ids" in text
    assert "the component has no approved canonical locator" in text


def test_the_documented_react_bits_command_states_the_upstream_range_is_checked_not_installed():
    """The upstream range is CHECKED; the app installs its own exact pins."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "only CHECKED, never installed" in text
    assert "outside the closed allowlist" in text


def test_the_documented_fetch_layer_command_states_bounds_are_enforced():
    """The fetch-layer command must prove BOUNDS, not just show constants."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "catalog FETCH layer" in text
    assert "MAX_RESPONSE_BYTES" in text
    # the ENFORCEMENT claim itself, not merely the constant's name
    assert "size bound is ENFORCED (not just declared)" in text
    # the edge check (at cap vs over cap) is the point
    assert "at cap" in text and "over cap" in text
    # redirect refusal and the allowlist re-check
    assert "redirects refused" in text.lower() or "redirect" in text.lower()
    assert "url_is_allowed" in text


def test_the_documented_fetch_layer_command_uses_the_real_transport():
    """A fetch-layer proof that only used injected transports would prove nothing."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "_default_transport" in text
    assert "no injection" in text.lower()


def test_the_documented_registry_json_command_is_present():
    """The command fetches the exact locator the app authorizes."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "fetch the `SplitText-TS-TW` registry JSON" in text
    assert "https://reactbits.dev/r/SplitText-TS-TW" in text
    assert "registryDependencies" in text


def test_the_documented_registry_json_command_states_who_fetches_what():
    """The app does NOT fetch the JSON; the pinned CLI does. Stated, not implied."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "The app does NOT read this JSON" in text
    assert "post-hoc" in text


def test_the_documented_registry_json_command_notes_the_variant_suffix_is_required():
    """The bare /r/SplitText is HTML; the -TS-TW variant is the registry item."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "-TS-TW suffix is required" in text or "suffix is required" in text
    assert "bare `/r/SplitText`" in text or "bare /r/SplitText" in text


def test_the_documented_registry_json_command_states_the_external_path_is_not_wired():
    """Honest boundary: the external path is reachable but not yet in execute_selection."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "execute_selection" in text
    assert "not yet wired" in text


def test_the_documented_contract_validation_command_is_present():
    """The command validates the reviewed contract AND the trust boundary."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "validate the reviewed dependency contract" in text
    assert "trusted_registry_boundary" in text
    assert "REFUSED" in text


def test_the_documented_contract_command_states_the_boundary_is_consulted_both_paths():
    """The claim is that BOTH executing paths refuse an incoherent boundary."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "install_components" in text
    assert "install_external_component" in text
    assert "no command" in text.lower()


def test_the_documented_disposable_starter_command_is_present():
    """The command installs into a DISPOSABLE copy and proves the result builds."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "install into a DISPOSABLE frontend starter" in text
    assert "mktemp -d" in text
    assert "frontend-starter" in text
    assert "npm run build" in text


def test_the_documented_disposable_starter_command_states_the_refusal_is_load_bearing():
    """The command must show WHY the boundary matters, not just that it refuses."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "the refusal is load-bearing" in text
    assert "TS2307" in text


def test_the_documented_final_exact_state_command_is_present():
    """The command proves a PRE-EXISTING floating range is normalized to the pin."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "verify the FINAL package.json exact direct-dependency state" in text
    assert 'doc["dependencies"]["cn"] = "^0.4.0"' in text
    assert "final cn: 0.4.0" in text


def test_the_documented_final_exact_state_command_states_the_governed_set_rule():
    """The doc must name the property, not just show the command."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "governed" in text
    assert "not just the CLI's delta" in text


def test_the_documented_typecheck_command_is_present():
    """The command typechecks a disposable project after a reviewed install."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "`typecheck` a generated project after a reviewed install" in text
    assert "npm run typecheck" in text
    assert '"typecheck": "tsc -b"' in text


def test_the_documented_typecheck_command_states_the_starter_dep_fix():
    """The doc must record the class-variance-authority false-refusal defect."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "class-variance-authority" in text
    assert "5 of the 16" in text
    assert "was absent from the always-provided set" in text


def test_the_documented_build_command_is_present():
    """The command builds a disposable project after a reviewed install."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "`build` a generated project after a reviewed install" in text
    assert '"build": "tsc -b && vite build"' in text
    assert "npm run build" in text


def test_the_documented_build_command_states_the_shadow_config_fix():
    """The doc must record the vite.config shadowing defect and its fix."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "shadow" in text.lower()
    assert "FORBIDDEN_TOOLCHAIN_FILES" in text
    assert "TOOLCHAIN_MUTATION_REJECTED" in text


def test_the_documented_transitions_command_is_present():
    """The command materializes one bounded recipe with the pinned CLI."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "Transitions `add card-resize`" in text
    assert "transitions-dev add card-resize" in text
    assert "transitions-dev@0.3.0" in text


def test_the_documented_transitions_command_states_the_reserved_slug_fix():
    """The doc must record that the bulk selectors are refused structurally."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "refused at the slug VOCABULARY (`RESERVED_RECIPE_SLUGS`)" in text
    assert "add all" in text
    assert "32" in text


def test_the_doc_records_the_linux_containment_family_and_shadowed_tests():
    """The 26/26 containment family and the two shadowed tests must be recorded."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "26/26" in text
    assert "defined" in text and "three times" in text
    assert "test_suite_hygiene.py" in text


def test_the_doc_records_the_offline_suite_constraint():
    """The doc must record that the default suite is offline + the npm fix."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "must never depend on the internet" in text
    assert "test_suite_offline.py" in text
    assert "@pytest.mark.integration" in text


def test_the_doc_has_the_final_proof_part_m_section():
    """Part M must document the one-command acceptance gate."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "## Part M — FINAL PROOF" in text
    assert "d3a5_final_proof.py" in text
    assert "VERDICT" in text
    # The pinned counts must be the real ones (a dropped guard changes a count).
    assert "`parta 16`, `partbc 73`, `partc 38`" in text
    # The observed tally must be the real one (13 = 1 suite + 8 drivers + 4 rails).
    assert "`13/13 checks passed`" in text
    # The observed driver tally must match the pinned counts too -- this is the
    # line that went stale when partc grew to 38 and the suite to 3604.
    assert "drivers `16 / 73 / 38 / 36 / 18`" in text


def test_the_doc_does_not_claim_a_socket_ban_the_suite_does_not_have():
    """The doc must NOT claim the suite 'bans sockets' -- it does not.

    The suite is offline (no non-loopback network), enforced by
    ``tests/test_suite_offline.py``. But the port-allocation tests bind loopback
    sockets (``127.0.0.1:0``) and the guard deliberately allows loopback, so a
    blanket 'bans sockets' claim is false. Pin the honest wording.
    """
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "The suite bans sockets" not in text
    assert "socket-free by construction" not in text
    # The honest phrasing must be present instead.
    assert "no non-loopback network" in text
    assert "loopback" in text


def test_the_doc_records_the_stale_claim_sweep():
    """The sweep section must exist and record what it corrected."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "### A stale-claim sweep of the batch diff" in text
    assert "three stale claims" in text


def test_the_doc_has_the_source_review_bullets_part_n_section():
    """Part N must document the live re-verification of every bullet."""
    text = LIVE_SMOKE_DOC.read_text(encoding="utf-8")

    assert "## Part N — the source-review bullets, re-verified as LIVE properties" in text
    assert "test_source_review_bullets.py" in text
    assert "30 tests" in text


