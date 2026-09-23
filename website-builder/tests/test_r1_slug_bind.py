"""Phase C — R1 slug-bind persistence durability (Bug 4).

Once the preview flow begins using a friendly Vercel project identity, that
binding must be durable before it proceeds as if the preview identity is safe.
A bind persistence failure must fail closed, must NOT fall back to the opaque
project name, and must NOT duplicate the Vercel project create on recovery.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from r1_harness import (  # noqa: E402
    LocalR1Scenario,
    patch_qa_boundaries,
    make_preview_ready,
)


@pytest.fixture(autouse=True)
def _qa_boundaries(monkeypatch):
    patch_qa_boundaries(monkeypatch)


def _ready_project(h: LocalR1Scenario, name: str = "kitsunereading") -> str:
    pid = h.seed_project(name)
    ws = h.runner.create_workspace(pid)
    make_preview_ready(h.store, pid, ws, source_revision=1)
    return pid


class TestSlugBindPersistFailure:
    def test_bind_failure_fails_closed_before_deploy_and_delivery(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        pid = _ready_project(h)
        h.vercel.set_vercel_behavior if False else None
        # Pre-approve the friendly project create path.
        h.vercel.get_status = 404
        h.vercel.post_status = 201
        # Inject failure into bind_slug persistence.
        h.bind_slug_raises = RuntimeError("registry write failed")

        ws = h.runner.create_workspace(pid)
        result = h.preview.run_owned(pid, ws)

        assert not result.success
        assert result.error_code == "SLUG_BIND_PERSIST_FAILED"
        # Preview MUST NOT proceed to deployment/delivery.
        assert h.vercel.deploy_calls == 0
        assert h.telegram.photo_calls == []
        # Exactly one remote project create (the friendly one).
        assert h.vercel.post_calls == 1
        # No slug bound locally (the write failed).
        assert h.registry_entry(pid).vercel_slug is None
        # Never fell back to the opaque project name.
        assert h.vercel.created_projects == ["kitsunereading"]

    def test_recovery_reconciles_same_remote_project_no_duplicate_create(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        pid = _ready_project(h)
        h.vercel.get_status = 404
        h.vercel.post_status = 201
        h.bind_slug_raises = RuntimeError("registry write failed")

        ws = h.runner.create_workspace(pid)
        first = h.preview.run_owned(pid, ws)
        assert not first.success
        assert h.vercel.post_calls == 1

        # Recovery: the transient persistence failure is resolved and a NEW
        # preview orchestrator instance runs over the SAME temp state. The
        # second attempt must reconcile the already-created remote project
        # (GET 200 path) WITHOUT a second create.
        h.bind_slug_raises = None
        h.vercel.get_status = 200  # project now exists remotely

        h.restart()
        second = h.preview.run_owned(pid, ws)

        assert second.success, second.error
        # Still exactly ONE create across both attempts.
        assert h.vercel.post_calls == 1
        # Slug now durably bound.
        assert h.registry_entry(pid).vercel_slug == "kitsunereading"
        # Preview delivered exactly once (only on the successful attempt).
        assert len(h.telegram.photo_calls) == 1
        assert h.vercel.deploy_calls == 1
