"""PHASE E — Vercel automation-bypass auto-provisioning + secret storage.

Covers the required bypass tests 1, 2, 3, 5, 6, 8, 9, 11, 12 (the smoke-facing
ones — 4, 7, 10, 13 — live in the Phase F test module since they exercise the
PreviewSmokeTester integration).
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


class FakeVercelBypass:
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
