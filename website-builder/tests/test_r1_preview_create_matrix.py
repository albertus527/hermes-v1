"""Phase E (runtime level) — Vercel create classification through the REAL
PreviewOrchestrator, driven by the local R1 simulation harness.

Complements the adapter-level matrix by proving the preview control flow
reacts correctly to each classification and never duplicates a create.
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


def _ready(h: LocalR1Scenario, name: str = "kitsunereading"):
    pid = h.seed_project(name)
    ws = h.runner.create_workspace(pid)
    make_preview_ready(h.store, pid, ws, source_revision=1)
    return pid, ws


class TestPreviewCreateClassificationMatrix:
    def test_a_404_then_201_succeeds(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        pid, ws = _ready(h)
        h.vercel.get_status = 404
        h.vercel.post_status = 201
        result = h.preview.run_owned(pid, ws)
        assert result.success
        assert h.vercel.post_calls == 1
        assert h.vercel.created_projects == ["kitsunereading"]

    def test_b_400_is_confirmed_failure_not_ambiguous(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        pid, ws = _ready(h)
        h.vercel.get_status = 404
        h.vercel.post_status = 400
        result = h.preview.run_owned(pid, ws)
        assert not result.success
        assert result.error_code == "PROJECT_CREATE_REJECTED"
        assert h.vercel.post_calls == 1
        # No deploy, no delivery.
        assert h.vercel.deploy_calls == 0
        assert h.telegram.photo_calls == []

    def test_c_409_is_confirmed_collision_not_ambiguous(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        pid, ws = _ready(h)
        h.vercel.get_status = 404
        h.vercel.post_status = 409
        result = h.preview.run_owned(pid, ws)
        assert not result.success
        assert result.error_code == "SLUG_COLLISION"
        assert h.vercel.post_calls == 1

    def test_d_post_timeout_is_ambiguous_no_blind_second_post(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        pid, ws = _ready(h)
        h.vercel.get_status = 404
        h.vercel.post_raises = TimeoutError("timeout after send")
        result = h.preview.run_owned(pid, ws)
        assert not result.success
        assert result.error_code == "AMBIGUOUS_PROJECT_CREATE"
        assert h.vercel.post_calls == 1
        assert h.telegram.photo_calls == []

    def test_e_ambiguous_lookup_finds_owned_project_on_retry(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        pid, ws = _ready(h)
        # Attempt 1: ambiguous timeout.
        h.vercel.get_status = 404
        h.vercel.post_raises = TimeoutError("timeout after send")
        first = h.preview.run_owned(pid, ws)
        assert not first.success
        assert h.vercel.post_calls == 1

        # Recovery: the project now exists remotely and is ours. A NEW
        # orchestrator over the SAME state reconciles WITHOUT a second create.
        h.restart()
        h.vercel.post_raises = None
        h.vercel.get_status = 200
        second = h.preview.run_owned(pid, ws)
        assert second.success, second.error
        assert h.vercel.post_calls == 1
        assert len(h.telegram.photo_calls) == 1

    def test_f_lookup_still_inconclusive_fails_closed_no_second_create(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        pid, ws = _ready(h)
        h.vercel.get_status = 500  # neither 404 nor a valid 200
        result = h.preview.run_owned(pid, ws)
        assert not result.success
        # Never create blindly when we could not prove absence.
        assert h.vercel.post_calls == 0
