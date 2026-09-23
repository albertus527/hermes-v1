"""PHASE E — Vercel automation-bypass auto-provisioning + secret storage.

Covers the required bypass tests 1, 2, 3, 5, 6, 8, 9, 11, 12 (the smoke-facing
ones — 4, 7, 10, 13 — live in the Phase F test module since they exercise the
PreviewSmokeTester integration), plus the LIVE-EVIDENCE recovery/reconciliation
path (remote map-key secret adopted without any PATCH).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.contracts import OperationResult  # noqa: E402
from app.core.secrets import BypassSecretStore, file_mode  # noqa: E402
from app.deploy.bypass import BypassProvisioner  # noqa: E402

SECRET = "bypass-secret-value-xyz"
# LIVE-EVIDENCE secret: the protectionBypass MAP KEY (^[a-zA-Z0-9]{32}$).
LIVE_SECRET = "abcdefghijklmnopqrstuvwxyz123456"


class FakeVercelBypass:
    """Provisioner-level double with NO read-only reconciliation surface."""

    def __init__(self, *, result: OperationResult = None, raises=None):
        self.calls = 0
        self.result = result or OperationResult.ok(
            {"project_id": "prj_1", "secret": SECRET}
        )
        self.raises = raises

    def ensure_protection_bypass(self, app_id, project, *, expected_name=None):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.result


class FakeRecoverableVercel:
    """Read-only reconciliation surface (like the real adapter).

    ``remote`` describes what a READ currently sees:
      * a secret string -> exactly ONE existing bypass entry (its MAP KEY);
      * ``None``        -> proven absence;
      * ``'AMBIGUOUS'`` -> cannot read/decide (must fail closed);
      * ``'MALFORMED'`` -> malformed remote shape (must fail closed).
    ``generate_secret`` is what a PATCH would return.
    """

    def __init__(self, remote=None, *, generate_secret=LIVE_SECRET,
                 generate_raises=None):
        self.remote = remote
        self.generate_secret = generate_secret
        self.generate_raises = generate_raises
        self.read_calls = 0
        self.generate_calls = 0  # alias used by some assertions
        self.ensure_calls = 0

    def reconcile_protection_bypass(self, app_id, project, *, expected_name=None):
        self.read_calls += 1
        if self.remote == "AMBIGUOUS":
            return OperationResult.fail(
                "AMBIGUOUS_BYPASS_PROVISION", error_code="AMBIGUOUS_BYPASS_PROVISION"
            )
        if self.remote == "MALFORMED":
            return OperationResult.fail(
                "BYPASS_RECONCILIATION_REQUIRED",
                error_code="BYPASS_RECONCILIATION_REQUIRED",
            )
        if self.remote is None:
            # Authoritative proven absence (recoverable scan contract).
            return OperationResult.ok({"exists": False})
        return OperationResult.ok({"exists": True, "secret": self.remote})

    def ensure_protection_bypass(self, app_id, project, *, expected_name=None):
        self.ensure_calls += 1
        self.generate_calls += 1
        if self.generate_raises is not None:
            raise self.generate_raises
        return OperationResult.ok({"project_id": "prj_1", "secret": self.generate_secret})


def _project(pid="prj_1"):
    return {"id": pid, "name": "cozyreadingspace", "accountId": "team", "env": []}


def _store(tmp_path) -> BypassSecretStore:
    return BypassSecretStore(Path(tmp_path) / "hermes-home" / "vercel-bypass")


def test_01_new_project_generates_once_and_stores(tmp_path):
    vercel = FakeVercelBypass()
    store = _store(tmp_path)
    prov = BypassProvisioner(vercel, store)

    result = prov.ensure("app", _project())
    assert result.success
    assert result.data["secret"] == SECRET
    assert result.data["source"] == "generated"
    assert vercel.calls == 1
    assert store.get("prj_1") == SECRET


def test_02_existing_project_with_stored_secret_does_not_regenerate(tmp_path):
    vercel = FakeVercelBypass()
    store = _store(tmp_path)
    store.set("prj_1", SECRET)
    prov = BypassProvisioner(vercel, store)

    result = prov.ensure("app", _project())
    assert result.success
    assert result.data["source"] == "stored"
    assert vercel.calls == 0  # no regeneration


def test_03_retry_after_failed_smoke_does_not_rotate(tmp_path):
    vercel = FakeVercelBypass()
    store = _store(tmp_path)
    prov = BypassProvisioner(vercel, store)

    first = prov.ensure("app", _project())
    assert first.data["secret"] == SECRET
    # A "retry" (smoke previously failed) simply re-ensures.
    second = prov.ensure("app", _project())
    assert second.data["source"] == "stored"
    assert second.data["secret"] == SECRET
    assert vercel.calls == 1  # generated exactly once


def test_05_generation_forbidden_sanitized_no_secret(tmp_path):
    vercel = FakeVercelBypass(
        result=OperationResult.fail(
            "BYPASS_PROVISION_FORBIDDEN", error_code="BYPASS_PROVISION_FORBIDDEN"
        )
    )
    store = _store(tmp_path)
    prov = BypassProvisioner(vercel, store)

    result = prov.ensure("app", _project())
    assert not result.success
    assert result.error_code == "BYPASS_PROVISION_FORBIDDEN"
    assert store.get("prj_1") is None
    assert SECRET not in str(result)


def test_06_generation_transport_failure_no_blind_repeat(tmp_path):
    vercel = FakeVercelBypass(
        result=OperationResult.fail(
            "AMBIGUOUS_BYPASS_PROVISION", error_code="AMBIGUOUS_BYPASS_PROVISION"
        )
    )
    store = _store(tmp_path)
    prov = BypassProvisioner(vercel, store)

    result = prov.ensure("app", _project())
    assert not result.success
    assert result.error_code == "AMBIGUOUS_BYPASS_PROVISION"
    assert vercel.calls == 1  # exactly ONE attempt, never a blind retry loop
    assert store.get("prj_1") is None


def test_08_env_fallback_only(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setenv("VERCEL_AUTOMATION_BYPASS_SECRET", "env-secret")
    prov = BypassProvisioner(FakeVercelBypass(), store)
    assert prov.resolve("prj_none") == "env-secret"


def test_09_stored_secret_wins_over_env_fallback(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.set("prj_1", SECRET)
    monkeypatch.setenv("VERCEL_AUTOMATION_BYPASS_SECRET", "env-secret")
    prov = BypassProvisioner(FakeVercelBypass(), store)
    assert prov.resolve("prj_1") == SECRET
    # No stored secret for a different project -> env fallback applies.
    assert prov.resolve("prj_other") == "env-secret"


def test_11_secret_never_in_repr_or_debug(tmp_path):
    store = _store(tmp_path)
    store.set("prj_1", SECRET)
    # repr of the store object must not contain the secret.
    assert SECRET not in repr(store)
    # Stored file contains the secret but the store's public repr does not.


def test_12_filesystem_permissions(tmp_path):
    store = _store(tmp_path)
    store.set("prj_1", SECRET)
    path = Path(tmp_path) / "hermes-home" / "vercel-bypass" / "prj_1.json"
    assert path.exists()
    mode = file_mode(path)
    if mode is not None and os.name == "posix":
        assert mode == 0o600
    dir_mode = file_mode(path.parent)
    if dir_mode is not None and os.name == "posix":
        assert dir_mode == 0o700
    payload = json.loads(path.read_text())
    assert payload["secret"] == SECRET


def test_reprovision_forces_new_generation(tmp_path):
    vercel = FakeVercelBypass(
        result=OperationResult.ok({"project_id": "prj_1", "secret": "rotated"})
    )
    store = _store(tmp_path)
    store.set("prj_1", SECRET)
    prov = BypassProvisioner(vercel, store)

    result = prov.ensure("app", _project(), reprovision=True)
    assert result.success
    assert result.data["secret"] == "rotated"
    assert vercel.calls == 1
    assert store.get("prj_1") == "rotated"


def test_missing_project_id_fails_closed(tmp_path):
    prov = BypassProvisioner(FakeVercelBypass(), _store(tmp_path))
    result = prov.ensure("app", {"name": "no-id"})
    assert not result.success
    assert result.error_code == "BYPASS_PROVISION_UNAVAILABLE"


# ---------------------------------------------------------------------------
# LIVE-EVIDENCE recovery / reconciliation (local store empty)
# ---------------------------------------------------------------------------

def test_R01_reconciles_existing_remote_bypass_without_regenerating(tmp_path):
    """p7-style recovery: the remote project already has exactly one bypass
    entry, but the local store is empty (a prior parse failed after creation).
    Adopt the remote MAP KEY -- source 'reconciled' -- and NEVER PATCH."""
    vercel = FakeRecoverableVercel(remote=LIVE_SECRET)
    store = _store(tmp_path)
    prov = BypassProvisioner(vercel, store)

    result = prov.ensure("app", _project())
    assert result.success, result.error
    assert result.data["secret"] == LIVE_SECRET
    assert result.data["source"] == "reconciled"
    assert store.get("prj_1") == LIVE_SECRET
    assert vercel.ensure_calls == 0  # NO generation PATCH whatsoever


def test_R01b_reconciled_secret_is_reused_on_the_next_run(tmp_path):
    vercel = FakeRecoverableVercel(remote=LIVE_SECRET)
    store = _store(tmp_path)
    prov = BypassProvisioner(vercel, store)

    first = prov.ensure("app", _project())
    assert first.data["source"] == "reconciled"
    reads = vercel.read_calls

    # A later run (fresh provisioner over the same store) reuses CASE A.
    prov2 = BypassProvisioner(vercel, store)
    second = prov2.ensure("app", _project())
    assert second.success
    assert second.data["source"] == "stored"
    assert second.data["secret"] == LIVE_SECRET
    assert vercel.read_calls == reads  # no extra read either
    assert vercel.ensure_calls == 0


def test_R02_no_remote_bypass_generates_exactly_once(tmp_path):
    """A proven-absent remote bypass is the ONLY state that authorizes a
    single generation PATCH."""
    vercel = FakeRecoverableVercel(remote=None, generate_secret=LIVE_SECRET)
    store = _store(tmp_path)
    prov = BypassProvisioner(vercel, store)

    result = prov.ensure("app", _project())
    assert result.success
    assert result.data["source"] == "generated"
    assert result.data["secret"] == LIVE_SECRET
    assert vercel.ensure_calls == 1  # exactly one generate
    assert store.get("prj_1") == LIVE_SECRET


def test_R02b_no_read_surface_still_generates_once(tmp_path):
    """Legacy adapters without a read surface keep the exact one-shot
    generation behaviour (no reconciliation read, no blind repeat)."""
    vercel = FakeVercelBypass()
    store = _store(tmp_path)
    prov = BypassProvisioner(vercel, store)
    result = prov.ensure("app", _project())
    assert result.success
    assert result.data["source"] == "generated"
    assert vercel.calls == 1


def test_R03_stored_local_secret_short_circuits_before_any_read(tmp_path):
    """Case 3: an existing stored secret reuses WITHOUT any read/generate."""
    vercel = FakeRecoverableVercel(remote=LIVE_SECRET)
    store = _store(tmp_path)
    store.set("prj_1", SECRET)
    prov = BypassProvisioner(vercel, store)

    result = prov.ensure("app", _project())
    assert result.success
    assert result.data["source"] == "stored"
    assert result.data["secret"] == SECRET
    assert vercel.read_calls == 0
    assert vercel.ensure_calls == 0


def test_R05_ambiguous_remote_read_fails_closed_no_generation(tmp_path):
    vercel = FakeRecoverableVercel(remote="AMBIGUOUS")
    store = _store(tmp_path)
    prov = BypassProvisioner(vercel, store)
    result = prov.ensure("app", _project())
    assert not result.success
    assert result.error_code == "AMBIGUOUS_BYPASS_PROVISION"
    assert vercel.ensure_calls == 0
    assert store.get("prj_1") is None


def test_R06_malformed_remote_shape_fails_closed_no_generation(tmp_path):
    vercel = FakeRecoverableVercel(remote="MALFORMED")
    store = _store(tmp_path)
    prov = BypassProvisioner(vercel, store)
    result = prov.ensure("app", _project())
    assert not result.success
    assert result.error_code == "BYPASS_RECONCILIATION_REQUIRED"
    assert vercel.ensure_calls == 0
    assert store.get("prj_1") is None


def test_R_read_exception_fails_closed_no_generation(tmp_path):
    class Exploding(FakeRecoverableVercel):
        def reconcile_protection_bypass(self, app_id, project, *, expected_name=None):
            raise TimeoutError("connection reset")

    vercel = Exploding(remote=LIVE_SECRET)
    store = _store(tmp_path)
    prov = BypassProvisioner(vercel, store)
    result = prov.ensure("app", _project())
    assert not result.success
    assert result.error_code == "BYPASS_RECONCILIATION_REQUIRED"
    assert vercel.ensure_calls == 0


def test_R_reprovision_forces_generation_ignoring_remote(tmp_path):
    vercel = FakeRecoverableVercel(remote=LIVE_SECRET, generate_secret="rotated" * 4)
    store = _store(tmp_path)
    store.set("prj_1", SECRET)
    prov = BypassProvisioner(vercel, store)

    result = prov.ensure("app", _project(), reprovision=True)
    assert result.success
    assert result.data["secret"] == "rotated" * 4
    assert result.data["source"] == "generated"
    assert vercel.read_calls == 0  # explicit rotation never reads first
    assert vercel.ensure_calls == 1
    assert store.get("prj_1") == "rotated" * 4


def test_R07_reconciled_secret_never_in_state_errors_or_repr(tmp_path):
    vercel = FakeRecoverableVercel(remote=LIVE_SECRET)
    store = _store(tmp_path)
    prov = BypassProvisioner(vercel, store)

    ok = prov.ensure("app", _project())
    assert ok.data["secret"] == LIVE_SECRET
    # Success legitimately carries the secret in ``data`` (the caller persists
    # it); the sanitized failure repr and the store repr never embed it.
    assert LIVE_SECRET not in repr(store)

    bad = BypassProvisioner(
        FakeRecoverableVercel(remote="MALFORMED"), _store(tmp_path / "other")
    ).ensure("app", _project())
    assert not bad.success
    assert LIVE_SECRET not in str(bad)
    assert LIVE_SECRET not in repr(bad)

