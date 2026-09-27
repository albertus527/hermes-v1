"""R2-C: canonical-source admission.

A LIVE revision may only start from a source whose identity is PROVEN. These
tests pin the locked precedence, the per-kind refusals, the R1 -> R2 evidence
rules, and the zero-mutation property of a refusal.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore
from app.projects.release import (
    CANONICAL_SOURCE_COMMIT_MISSING,
    CANONICAL_SOURCE_LEGACY_IDENTITY,
    CANONICAL_SOURCE_NO_LIVE_RELEASE,
    CANONICAL_SOURCE_NOT_PUBLISHED,
    CANONICAL_SOURCE_PARENT_UNRESOLVED,
    CANONICAL_SOURCE_REPO_MISMATCH,
    CANONICAL_SOURCE_REPO_UNRESOLVED,
    CANONICAL_SOURCE_SYNC_REQUIRED,
    COMPLETENESS_COMPLETE,
    COMPLETENESS_LEGACY_PARTIAL,
    MODE_NEW_RELEASE,
    PUBLICATION_CONFIRMED,
    RELEASE_FIELDS,
    ReleaseCoordinator,
    ReleaseStageError,
    STAGE_GIT_CONFIRMED,
    STAGE_SMOKE_PASSED,
    VERDICT_PUBLICATION_NOT_CONFIGURED,
    VERDICT_PUBLICATION_PARENT_UNRESOLVED,
    VERDICT_READY,
    VERDICT_REMOTE_IDENTITY_UNRESOLVED,
    build_last_live_release,
    canonical_source_repo_verdict,
    canonical_source_verdict,
    validate_last_live_release,
)
from app.projects.revise import RevisionOrchestrator
from app.sandbox.runner import ProjectRunner

OWNER = "owner-1"
SHA = "a" * 40
SHA2 = "b" * 40
TREE = "c" * 40
DIGEST = "d" * 64


def _complete_record(**overrides):
    """A COMPLETE, configured, well-formed R2 release identity -- a record
    Batch B's own validator accepts unchanged."""
    record = {
        "release_id": "rel-1",
        "operation_id": "op-1",
        "completeness": COMPLETENESS_COMPLETE,
        "publication_configured": True,
        "source_revision": 3,
        "publication_commit": SHA,
        "publication_parent": None,
        "publication_tree": TREE,
        "publication_repo": "o/r",
        "publication_branch": "northcut",
        "tested_commit": SHA2,
        "tested_tree": TREE,
        "source_sha256": DIGEST,
        "artifact_sha256": "e" * 64,
        "deployment_id": "dsp-1",
        "production_url": "https://northcut.vercel.app",
        "deployment_url": "https://dsp-1.northcut.vercel.app",
        "smoke": {"status": "PASSED", "target_host": "northcut.vercel.app",
                  "target_path": "/", "at": 1.0},
        "committed_at": 1.0,
    }
    record.update(overrides)
    return record


def _legacy_record(**overrides):
    record = {
        "completeness": COMPLETENESS_LEGACY_PARTIAL,
        "source_revision": 2,
        "tested_commit": SHA2,
        "tested_tree": TREE,
        "source_sha256": DIGEST,
        "artifact_sha256": "e" * 64,
    }
    record.update(overrides)
    return record


def _store(tmp_path, project_id="proj"):
    store = ProjectStateStore(tmp_path / "state")
    with store.acquire_writer(project_id) as state:
        state.roles["owner"] = OWNER
        state.conversation_id = "555"
        state.brief = {"name": "Northcut", "what": "barbershop", "why": "booking"}
        state.design_dna = {"version": 1, "typography": {"heading_font": "Inter",
                                                         "body_font": "Inter"}}
        state.lifecycle = ProjectLifecycle.LIVE.value
        store.save(state)
    return store


def _put_release(store, project_id, record, **deployment):
    with store.acquire_writer(project_id) as state:
        if record is not None:
            state.deployment["last_live_release"] = record
        for key, value in deployment.items():
            state.deployment[key] = value
        store.save(state)


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


def test_no_release_at_all_is_no_live_release(tmp_path):
    store = _store(tmp_path)
    verdict = canonical_source_verdict(store.load("proj"))
    assert verdict.verdict == "NO_LIVE_RELEASE"
    assert verdict.error_code == CANONICAL_SOURCE_NO_LIVE_RELEASE
    assert not verdict.ready


def test_legacy_sync_evidence_outranks_the_generic_legacy_verdict(tmp_path):
    store = _store(tmp_path)
    _put_release(store, "proj", _legacy_record(),
                 legacy_source_sync={"sync_status": "SOURCE_SYNC_REQUIRED"})
    verdict = canonical_source_verdict(store.load("proj"))
    assert verdict.verdict == "SOURCE_SYNC_REQUIRED"
    assert verdict.error_code == CANONICAL_SOURCE_SYNC_REQUIRED


def test_legacy_partial_without_r1_evidence_falls_back_to_legacy_identity(tmp_path):
    store = _store(tmp_path)
    _put_release(store, "proj", _legacy_record())
    verdict = canonical_source_verdict(store.load("proj"))
    assert verdict.verdict == "LEGACY_RELEASE_IDENTITY"
    assert verdict.error_code == CANONICAL_SOURCE_LEGACY_IDENTITY


def test_unrecognised_sync_status_is_not_read_as_r1_evidence(tmp_path):
    """Only the exact R1 status is that evidence. Anything else -- including a
    status that merely sounds affirmative -- falls through to the general legacy
    verdict instead of being read as a stronger claim."""
    store = _store(tmp_path)
    _put_release(store, "proj", _legacy_record(),
                 legacy_source_sync={"sync_status": "SYNCED"})
    verdict = canonical_source_verdict(store.load("proj"))
    assert verdict.error_code == CANONICAL_SOURCE_LEGACY_IDENTITY


def test_publication_not_configured_outranks_identity_checks(tmp_path):
    store = _store(tmp_path)
    _put_release(store, "proj", _complete_record(publication_configured=False))
    verdict = canonical_source_verdict(store.load("proj"))
    assert verdict.verdict == VERDICT_PUBLICATION_NOT_CONFIGURED
    assert verdict.error_code == CANONICAL_SOURCE_NOT_PUBLISHED


def test_complete_r2_release_is_ready(tmp_path):
    store = _store(tmp_path)
    _put_release(store, "proj", _complete_record())
    verdict = canonical_source_verdict(store.load("proj"))
    assert verdict.verdict == VERDICT_READY
    assert verdict.ready and verdict.error_code is None


# ---------------------------------------------------------------------------
# publication_parent semantics -- root publication is VALID
# ---------------------------------------------------------------------------


def test_root_publication_parent_none_is_valid(tmp_path):
    store = _store(tmp_path)
    _put_release(store, "proj", _complete_record(publication_parent=None))
    verdict = canonical_source_verdict(store.load("proj"))
    assert verdict.ready, verdict.error_code


def test_root_publication_is_also_accepted_by_batch_b_validation(tmp_path):
    """The refusal must come from the same place the release was written, not
    from an admission check that Batch B would disagree with."""
    store = _store(tmp_path)
    _put_release(store, "proj", _complete_record(publication_parent=None))
    validate_last_live_release(
        store.load("proj").deployment["last_live_release"], MODE_NEW_RELEASE)


def test_malformed_non_null_publication_parent_is_unresolved(tmp_path):
    store = _store(tmp_path)
    _put_release(store, "proj", _complete_record(publication_parent="HEAD"))
    verdict = canonical_source_verdict(store.load("proj"))
    assert verdict.verdict == VERDICT_PUBLICATION_PARENT_UNRESOLVED
    assert verdict.error_code == CANONICAL_SOURCE_PARENT_UNRESOLVED


def test_empty_publication_parent_is_unresolved_not_a_root_publication(tmp_path):
    """"Absent" and "present but empty" are different facts. Only ``None``
    means a root publication."""
    store = _store(tmp_path)
    _put_release(store, "proj", _complete_record(publication_parent=""))
    verdict = canonical_source_verdict(store.load("proj"))
    assert verdict.error_code == CANONICAL_SOURCE_PARENT_UNRESOLVED


# ---------------------------------------------------------------------------
# COMPLETE identity refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", None, "not-a-sha", "z" * 40, SHA[:39]])
def test_missing_or_malformed_publication_commit_is_commit_missing(tmp_path, bad):
    store = _store(tmp_path)
    _put_release(store, "proj", _complete_record(publication_commit=bad))
    verdict = canonical_source_verdict(store.load("proj"))
    assert verdict.verdict == "NO_PUBLICATION_COMMIT"
    assert verdict.error_code == CANONICAL_SOURCE_COMMIT_MISSING


@pytest.mark.parametrize("field", ["publication_repo", "publication_branch"])
@pytest.mark.parametrize("bad", ["", None, "a" * 200, "line\nbreak"])
def test_malformed_remote_identity_is_repo_unresolved(tmp_path, field, bad):
    store = _store(tmp_path)
    _put_release(store, "proj", _complete_record(**{field: bad}))
    verdict = canonical_source_verdict(store.load("proj"))
    assert verdict.verdict == VERDICT_REMOTE_IDENTITY_UNRESOLVED
    assert verdict.error_code == CANONICAL_SOURCE_REPO_UNRESOLVED


@pytest.mark.parametrize("bad", ["Northcut", "has space", "preview/abc", "refs/heads/x"])
def test_a_non_friendly_publication_branch_is_unresolved(tmp_path, bad):
    """The internal ``preview/<hash>/<hash>`` ref shape is never a publication
    branch, and neither is a raw ref path or a differently-cased name."""
    store = _store(tmp_path)
    _put_release(store, "proj", _complete_record(publication_branch=bad))
    verdict = canonical_source_verdict(store.load("proj"))
    assert verdict.error_code == CANONICAL_SOURCE_REPO_UNRESOLVED


def test_unexpected_completeness_is_never_read_as_the_strictest(tmp_path):
    store = _store(tmp_path)
    _put_release(store, "proj", _complete_record(completeness="SOMETHING_NEW"))
    verdict = canonical_source_verdict(store.load("proj"))
    assert verdict.verdict == "NO_PUBLICATION_COMMIT"
    assert verdict.error_code == CANONICAL_SOURCE_COMMIT_MISSING


def test_admission_does_not_loosen_complete_release_validation(tmp_path):
    """Admission is a second reader of the same fields, never a relaxation of
    the writer. A record Batch B would reject is not admitted."""
    store = _store(tmp_path)
    _put_release(store, "proj", _complete_record(publication_parent="HEAD"))
    with pytest.raises(ValueError):
        validate_last_live_release(
            store.load("proj").deployment["last_live_release"], MODE_NEW_RELEASE)


def test_verdict_is_a_pure_read(tmp_path):
    store = _store(tmp_path)
    _put_release(store, "proj", _complete_record())
    before = store._project_path("proj").read_bytes()
    canonical_source_verdict(store.load("proj"))
    canonical_source_verdict(store.load("proj"))
    assert store._project_path("proj").read_bytes() == before


# ---------------------------------------------------------------------------
# R1 evidence vs a genuine COMPLETE R2 release
# ---------------------------------------------------------------------------


def test_stale_r1_evidence_does_not_refuse_a_complete_r2_release(tmp_path):
    """The R1 sync evidence is read from the same ``deployment`` bag, so a
    project that was R1 and later earned a genuine R2 release still carries
    it. The ``LEGACY_PARTIAL`` branch gate is what keeps that from refusing a
    perfectly good R2 release."""
    store = _store(tmp_path)
    _put_release(store, "proj", _complete_record(),
                 legacy_source_sync={"sync_status": "SOURCE_SYNC_REQUIRED"})
    verdict = canonical_source_verdict(store.load("proj"))
    assert verdict.ready, verdict.error_code


# ---------------------------------------------------------------------------
# Repository identity
# ---------------------------------------------------------------------------


def test_repo_verdict_accepts_a_matching_configured_remote(tmp_path):
    store = _store(tmp_path)
    _put_release(store, "proj", _complete_record())
    verdict = canonical_source_repo_verdict(store.load("proj"), "o/r")
    assert verdict.ready


def test_repo_verdict_refuses_a_different_configured_remote(tmp_path):
    store = _store(tmp_path)
    _put_release(store, "proj", _complete_record())
    verdict = canonical_source_repo_verdict(store.load("proj"), "other/repo")
    assert verdict.verdict == "REPO_MISMATCH"
    assert verdict.error_code == CANONICAL_SOURCE_REPO_MISMATCH


def test_repo_verdict_is_a_no_op_without_a_configured_remote(tmp_path):
    """Not being able to compare is not a mismatch. Inventing a refusal here
    would duplicate the hydrate-time repository-identity check in a place that
    cannot see the configuration."""
    store = _store(tmp_path)
    _put_release(store, "proj", _complete_record())
    assert canonical_source_repo_verdict(store.load("proj"), None).ready
    assert canonical_source_repo_verdict(store.load("proj"), "").ready


# ---------------------------------------------------------------------------
# legacy_source_sync projection (migration) and D8' cleanup (commit_release)
# ---------------------------------------------------------------------------


def test_migration_projects_a_non_empty_r1_sync_status(tmp_path):
    store = _store(tmp_path)
    with store.acquire_writer("proj") as state:
        state.repository = {"sync_status": "SOURCE_SYNC_REQUIRED", "branch": "x"}
        store.save(state)
    loaded = store.load("proj")
    assert loaded.deployment["legacy_source_sync"] == {"sync_status": "SOURCE_SYNC_REQUIRED"}
    # The retired bag really is emptied, and the branch is not carried along.
    assert loaded.repository == {}


@pytest.mark.parametrize("bag", [{}, {"branch": "x"}, {"sync_status": ""}, None])
def test_migration_writes_nothing_when_the_r1_evidence_is_absent(tmp_path, bag):
    """Absent stays unknown. There is no R1 way to record "no sync status", so
    nothing is projected -- and ``False`` is never invented to fill the gap."""
    store = _store(tmp_path)
    with store.acquire_writer("proj") as state:
        state.repository = bag
        store.save(state)
    assert "legacy_source_sync" not in store.load("proj").deployment


def test_migration_never_overwrites_an_existing_projection(tmp_path):
    store = _store(tmp_path)
    with store.acquire_writer("proj") as state:
        state.deployment["legacy_source_sync"] = {"sync_status": "NEWER_TRUTH"}
        state.repository = {"sync_status": "SOURCE_SYNC_REQUIRED"}
        store.save(state)
    assert store.load("proj").deployment["legacy_source_sync"] == {"sync_status": "NEWER_TRUTH"}


def test_migration_never_touches_pointer_mode(tmp_path):
    store = _store(tmp_path)
    with store.acquire_writer("proj") as state:
        state.deployment["pointer_mode"] = True
        state.repository = {"sync_status": "SOURCE_SYNC_REQUIRED"}
        store.save(state)
    assert store.load("proj").deployment["pointer_mode"] is True


def test_pointer_mode_false_survives_a_migration_untouched(tmp_path):
    store = _store(tmp_path)
    with store.acquire_writer("proj") as state:
        state.deployment["pointer_mode"] = False
        state.repository = {"sync_status": "SOURCE_SYNC_REQUIRED"}
        store.save(state)
    assert store.load("proj").deployment["pointer_mode"] is False


OPERATION = "op-1"
PUBLICATION = {
    "configured": True,
    "status": PUBLICATION_CONFIRMED,
    "repo": "o/r",
    "branch": "northcut",
    "tested_commit": SHA2,
    "tested_tree": TREE,
    "intended_commit": SHA,
    "intended_parent": None,
    "intended_tree": TREE,
    "confirmed_at": 1.0,
}
SMOKE = {"status": "PASSED", "target_host": "northcut.vercel.app",
         "target_path": "/", "at": 1.0}


def _pending_publication(store, project_id, publication=None):
    """A pending publication already at SMOKE_PASSED, ready to commit."""
    publication = dict(PUBLICATION if publication is None else publication)
    with store.acquire_writer(project_id) as state:
        state.lifecycle = ProjectLifecycle.PUBLISHING.value
        state.deployment["pending_publication"] = {
            "operation_id": OPERATION,
            "source_revision": 3,
            "stage": STAGE_SMOKE_PASSED,
            "outcome": None,
            "reconciliation_required": False,
            "publication": publication,
            "smoke": dict(SMOKE),
        }
        state.deployment["legacy_source_sync"] = {"sync_status": "SOURCE_SYNC_REQUIRED"}
        store.save(state)
    return publication


def test_successful_commit_removes_the_r1_evidence(tmp_path):
    store = _store(tmp_path)
    publication = _pending_publication(store, "proj")
    release = build_last_live_release(
        operation_id=OPERATION, source_revision=3, source_sha256=DIGEST,
        artifact_sha256="e" * 64, deployment_id="dsp-1",
        production_url="https://northcut.vercel.app",
        deployment_url="https://dsp-1.northcut.vercel.app",
        smoke=dict(SMOKE), publication=publication,
    )
    ReleaseCoordinator(store).commit_release("proj", operation_id=OPERATION,
                                             release=release)
    loaded = store.load("proj")
    assert "legacy_source_sync" not in loaded.deployment
    assert loaded.lifecycle == ProjectLifecycle.LIVE.value
    assert loaded.deployment["last_live_release"]["completeness"] == COMPLETENESS_COMPLETE
    # And the release it just wrote is immediately admissible as a source.
    assert canonical_source_verdict(loaded).ready


@pytest.mark.parametrize("break_release", [
    {"publication_parent": "HEAD"},
    {"publication_tree": "f" * 40},
    {"tested_commit": "not-a-sha"},
    {"source_sha256": "not-a-digest"},
])
def test_refused_release_record_preserves_the_r1_evidence(tmp_path, break_release):
    store = _store(tmp_path)
    publication = _pending_publication(store, "proj")
    release = build_last_live_release(
        operation_id=OPERATION, source_revision=3, source_sha256=DIGEST,
        artifact_sha256="e" * 64, deployment_id="dsp-1",
        production_url="https://northcut.vercel.app",
        deployment_url="https://dsp-1.northcut.vercel.app",
        smoke=dict(SMOKE), publication=publication,
    )
    release.update(break_release)
    with pytest.raises(ValueError):
        ReleaseCoordinator(store).commit_release("proj", operation_id=OPERATION,
                                                 release=release)
    assert store.load("proj").deployment["legacy_source_sync"] == {
        "sync_status": "SOURCE_SYNC_REQUIRED"}


def test_refused_commit_stage_preserves_the_r1_evidence(tmp_path):
    """A refusal that happens before the record is even judged -- the operation
    is not at SMOKE_PASSED -- must also leave the evidence alone."""
    store = _store(tmp_path)
    publication = _pending_publication(store, "proj")
    with store.acquire_writer("proj") as state:
        state.deployment["pending_publication"]["stage"] = STAGE_GIT_CONFIRMED
        store.save(state)
    release = build_last_live_release(
        operation_id=OPERATION, source_revision=3, source_sha256=DIGEST,
        artifact_sha256="e" * 64, deployment_id="dsp-1",
        production_url="https://northcut.vercel.app",
        deployment_url="https://dsp-1.northcut.vercel.app",
        smoke=dict(SMOKE), publication=publication,
    )
    with pytest.raises(ReleaseStageError):
        ReleaseCoordinator(store).commit_release("proj", operation_id=OPERATION,
                                                 release=release)
    assert store.load("proj").deployment["legacy_source_sync"] == {
        "sync_status": "SOURCE_SYNC_REQUIRED"}


def test_batch_b_release_fields_invariant_is_unchanged():
    """Batch B owns the record shape. Batch C added no field to it."""
    assert "hydration" not in RELEASE_FIELDS
    assert "pointer_mode" not in RELEASE_FIELDS
    assert "legacy_source_sync" not in RELEASE_FIELDS
    for field in ("publication_commit", "publication_parent", "publication_tree",
                  "publication_repo", "publication_branch", "tested_commit",
                  "tested_tree", "source_sha256", "artifact_sha256"):
        assert field in RELEASE_FIELDS


# ---------------------------------------------------------------------------
# reserve() admission
# ---------------------------------------------------------------------------


def _orchestrator(tmp_path, store, **kwargs):
    runner = ProjectRunner(tmp_path / "workspaces", store)
    return RevisionOrchestrator(runner, store, hermes_adapter=object(),
                                **kwargs)


def test_reserve_admits_a_ready_live_release(tmp_path):
    store = _store(tmp_path)
    _put_release(store, "proj", _complete_record())
    result = _orchestrator(tmp_path, store).reserve("proj", 1, principal_id=OWNER)
    assert result.success
    base = store.load("proj").pending_revisions[0]["base"]
    assert base["base_kind"] == "LIVE"
    assert base["publication_commit"] == SHA
    assert base["revision_seq"] == 1


@pytest.mark.parametrize(
    "record,expected",
    [
        (None, CANONICAL_SOURCE_NO_LIVE_RELEASE),
        (_legacy_record(), CANONICAL_SOURCE_LEGACY_IDENTITY),
        (_complete_record(publication_configured=False), CANONICAL_SOURCE_NOT_PUBLISHED),
        (_complete_record(publication_commit=""), CANONICAL_SOURCE_COMMIT_MISSING),
        (_complete_record(publication_parent="HEAD"), CANONICAL_SOURCE_PARENT_UNRESOLVED),
        (_complete_record(publication_repo=""), CANONICAL_SOURCE_REPO_UNRESOLVED),
        (_complete_record(publication_branch=""), CANONICAL_SOURCE_REPO_UNRESOLVED),
    ],
)
def test_reserve_refuses_a_non_admitted_live_source_with_zero_mutation(
        tmp_path, record, expected):
    store = _store(tmp_path)
    _put_release(store, "proj", record)
    before = store._project_path("proj").read_bytes()
    result = _orchestrator(tmp_path, store).reserve("proj", 1, principal_id=OWNER)
    assert not result.success
    assert result.error_code == expected
    # No sequence bump, no lifecycle change, no reservation append, no cache
    # write: durable state is byte-equivalent.
    assert store._project_path("proj").read_bytes() == before
    after = store.load("proj")
    assert after.revisions.queued_revision_seq == 0
    assert after.lifecycle == ProjectLifecycle.LIVE.value
    assert after.pending_revisions == []


def test_reserve_refuses_a_repository_identity_mismatch_before_any_mutation(tmp_path):
    store = _store(tmp_path)
    _put_release(store, "proj", _complete_record(publication_repo="other/repo"))
    before = store._project_path("proj").read_bytes()
    result = _orchestrator(tmp_path, store, source_repo_url="git@github.com:o/r.git") \
        .reserve("proj", 1, principal_id=OWNER)
    assert not result.success
    assert result.error_code == CANONICAL_SOURCE_REPO_MISMATCH
    assert store._project_path("proj").read_bytes() == before


def test_reserve_freezes_the_base_and_a_redrive_never_recomputes_it(tmp_path):
    store = _store(tmp_path)
    _put_release(store, "proj", _complete_record())
    orchestrator = _orchestrator(tmp_path, store)
    assert orchestrator.reserve("proj", 1, principal_id=OWNER).success
    frozen = store.load("proj").pending_revisions[0]["base"]

    # A newer release lands, then the reserved one is corrupted. The base the
    # reservation holds is still what a re-drive must use.
    _put_release(store, "proj", _complete_record(publication_commit="f" * 40))
    assert orchestrator.reserve("proj", 1, principal_id=OWNER).success
    assert store.load("proj").pending_revisions[0]["base"] == frozen
    assert len(store.load("proj").pending_revisions) == 1


def test_draft_reserve_requires_a_usable_tested_snapshot(tmp_path):
    store = _store(tmp_path)
    with store.acquire_writer("proj") as state:
        state.lifecycle = ProjectLifecycle.PREVIEW_READY.value
        store.save(state)
    result = _orchestrator(tmp_path, store).reserve("proj", 1, principal_id=OWNER)
    assert not result.success
    assert result.error_code == "DRAFT_SNAPSHOT_UNAVAILABLE"
    assert store.load("proj").pending_revisions == []
