"""R2 release identity: the COMPLETE / LEGACY_PARTIAL contract and the lazy
migration that produces a legacy record.

The claim under test is narrow and load-bearing: ``last_live_release`` is the
authoritative LIVE release, and a record this system did not fully record stays
*unknown* rather than being completed by inference. Every test here is about
what the validator accepts, what it refuses, and what a lazily derived record
is allowed to contain.

Local behavioural tests only: a real state store against a temp root, real
builders, real validators. No network, no credentials, no provider.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.contracts import StaleOperationIntent  # noqa: E402
from app.core.lifecycle import ProjectLifecycle  # noqa: E402
from app.core.state import ProjectStateStore  # noqa: E402
from app.projects.release import (  # noqa: E402
    COMPLETENESS_COMPLETE,
    COMPLETENESS_LEGACY_PARTIAL,
    ERROR_HEAD_CONFLICT,
    ERROR_HEAD_UNREADABLE,
    LEGACY_UNKNOWN_FIELDS,
    MODE_NEW_RELEASE,
    MODE_PREVIOUS_RELEASE,
    PUBLICATION_CONFIRMED,
    PUBLICATION_NOT_CONFIGURED,
    PUBLICATION_PENDING,
    RELEASE_FIELDS,
    STAGE_COMMITTED,
    STAGE_GIT_CONFIRMED,
    STAGE_ORDER,
    STAGE_PREPARED,
    STAGE_PRODUCTION_CONFIRMED,
    STAGE_SMOKE_PASSED,
    UNCONFIGURED_IDENTITY_FIELDS,
    VERDICT_ERROR_CODES,
    ReleaseCoordinator,
    ReleaseRecordError,
    ReleaseStageError,
    VERDICT_C_CONFLICT,
    VERDICT_D_UNAVAILABLE,
    assert_stage_advances,
    build_last_live_release,
    build_pending_publication,
    legacy_live_release,
    next_stage,
    release_is_known,
    resolve_branch_parent,
    validate_last_live_release,
)

PROJECT = "rel-1"
OWNER = "owner-1"
SHA1 = "a" * 40
SHA1_B = "b" * 40
TREE = "c" * 40
SHA256 = "d" * 64
SHA256_B = "e" * 64
CANONICAL = "https://rel1.vercel.app/"
DEPLOYMENT = "https://dpl_rel1.vercel.app"

SMOKE_PASSED = {
    "status": "PASSED",
    "at": 1736.0,
    "target_host": "rel1.vercel.app",
    "target_path": "/",
    "failure_classification": None,
}


@pytest.fixture
def store(tmp_path):
    return ProjectStateStore(tmp_path / "state")


def _configured_publication(**overrides):
    publication = {
        "configured": True,
        "repo": "albertus527/website",
        "branch": "rel1",
        "tested_commit": SHA1,
        "tested_tree": TREE,
        "intended_commit": SHA1_B,
        "intended_parent": None,
        "intended_tree": TREE,
        "status": PUBLICATION_CONFIRMED,
        "confirmed_at": 1735.0,
    }
    publication.update(overrides)
    return publication


def _release(**overrides):
    record = {
        "release_id": "op-1",
        "source_revision": 3,
        "operation_id": "op-1",
        "tested_commit": SHA1,
        "tested_tree": TREE,
        "publication_commit": SHA1_B,
        "publication_tree": TREE,
        "publication_parent": None,
        "publication_branch": "rel1",
        "publication_repo": "albertus527/website",
        "source_sha256": SHA256,
        "artifact_sha256": SHA256_B,
        "deployment_id": "dpl_rel1",
        "production_url": CANONICAL,
        "deployment_url": DEPLOYMENT,
        "smoke": dict(SMOKE_PASSED),
        "committed_at": 1736.5,
        "completeness": COMPLETENESS_COMPLETE,
        "publication_configured": True,
    }
    record.update(overrides)
    return record


def _not_configured_release(**overrides):
    record = _release(
        tested_commit=None, tested_tree=None, publication_commit=None,
        publication_tree=None, publication_parent=None, publication_branch=None,
        publication_repo=None, publication_configured=False,
    )
    record.update(overrides)
    return record


def _pending_at(store, stage, *, configured=True, operation_id="op-1",
                outcome=None, reconciliation_required=False,
                publication_status=None):
    if publication_status is None:
        publication_status = PUBLICATION_CONFIRMED if configured \
            else PUBLICATION_NOT_CONFIGURED
    with store.acquire_writer(PROJECT) as state:
        state.roles["owner"] = OWNER
        state.lifecycle = ProjectLifecycle.PUBLISHING.value
        state.deployment["pending_publication"] = {
            "operation_id": operation_id,
            "source_revision": 3,
            "stage": stage,
            "outcome": outcome,
            "reconciliation_required": reconciliation_required,
            "created_at": 1734.0,
            "updated_at": 1734.0,
            "source_sha256": SHA256,
            "artifact_sha256": SHA256_B,
            "publication": (
                _configured_publication(status=publication_status)
                if configured
                else {
                    "configured": False, "repo": None, "branch": None,
                    "tested_commit": None, "tested_tree": None,
                    "intended_commit": None, "intended_parent": None,
                    "intended_tree": None,
                    "status": publication_status,
                    "confirmed_at": None,
                }
            ),
            "production": {"deployment_id": None, "promoted_deployment_id": None,
                           "confirmed_at": None},
            "smoke": {"status": None, "at": None},
            "last_error_code": None,
        }
        store.save(state)


def _legacy_last_live():
    return {
        "operation_id": "op-0",
        "deployment_id": "dpl_old",
        "production_url": CANONICAL,
        "deployment_url": DEPLOYMENT,
        "source_revision": 1,
        "source_sha256": SHA256,
        "artifact_sha256": SHA256_B,
        "live_at": 1.0,
    }


def _seed_legacy(store, *, repository=None):
    with store.acquire_writer(PROJECT) as state:
        state.lifecycle = ProjectLifecycle.LIVE.value
        state.deployment["last_live_deployment"] = _legacy_last_live()
        state.repository = repository or {}
        store.save(state)


# ---------------------------------------------------------------------------
# The stage graph
# ---------------------------------------------------------------------------


def test_stage_order_is_one_linear_graph():
    assert STAGE_ORDER == (
        STAGE_PREPARED, STAGE_GIT_CONFIRMED, STAGE_PRODUCTION_CONFIRMED,
        STAGE_SMOKE_PASSED, STAGE_COMMITTED,
    )
    assert next_stage(STAGE_PREPARED) == STAGE_GIT_CONFIRMED
    assert next_stage(STAGE_SMOKE_PASSED) == STAGE_COMMITTED


def test_a_stage_is_never_skipped():
    with pytest.raises(ReleaseStageError):
        assert_stage_advances(STAGE_PREPARED, STAGE_PRODUCTION_CONFIRMED)
    with pytest.raises(ReleaseStageError):
        assert_stage_advances(STAGE_PREPARED, STAGE_COMMITTED)


def test_a_stage_never_regresses_or_repeats():
    with pytest.raises(ReleaseStageError):
        assert_stage_advances(STAGE_PRODUCTION_CONFIRMED, STAGE_GIT_CONFIRMED)
    with pytest.raises(ReleaseStageError):
        assert_stage_advances(STAGE_GIT_CONFIRMED, STAGE_GIT_CONFIRMED)


def test_committed_is_terminal():
    with pytest.raises(ReleaseStageError):
        next_stage(STAGE_COMMITTED)


# ---------------------------------------------------------------------------
# COMPLETE validation
# ---------------------------------------------------------------------------


def test_a_complete_configured_release_validates():
    assert validate_last_live_release(
        _release(), MODE_NEW_RELEASE, publication_configured=True) is not None


def test_a_complete_release_reads_its_own_publication_configured_fact():
    """The record is self-describing: a read-side check with no pending record
    to consult resolves the publication fact from the record itself."""
    assert validate_last_live_release(_release(), MODE_PREVIOUS_RELEASE)
    assert validate_last_live_release(
        _not_configured_release(), MODE_PREVIOUS_RELEASE)


def test_publication_configured_may_never_be_guessed_from_a_null_commit():
    """The record states the fact. A record that omits it is refused rather
    than inferred, so ``publication_commit`` is never the evidence."""
    record = _not_configured_release()
    del record["publication_configured"]
    with pytest.raises(ReleaseRecordError):
        validate_last_live_release(record, MODE_PREVIOUS_RELEASE)
    # Even a record that DOES carry a commit needs the fact stated.
    with pytest.raises(ReleaseRecordError):
        validate_last_live_release(
            dict(_release(), publication_configured=None), MODE_PREVIOUS_RELEASE)


def test_a_configured_release_may_not_claim_it_did_not_publish():
    # The declared fact and the identity shape must agree in both directions.
    with pytest.raises(ReleaseRecordError):
        validate_last_live_release(
            _release(publication_configured=False), MODE_PREVIOUS_RELEASE)
    with pytest.raises(ReleaseRecordError):
        validate_last_live_release(
            _not_configured_release(publication_configured=True),
            MODE_PREVIOUS_RELEASE)


@pytest.mark.parametrize("field", [
    "publication_commit", "publication_tree", "publication_repo",
    "publication_branch", "tested_commit", "tested_tree",
])
def test_a_configured_release_may_not_omit_its_publication_identity(field):
    with pytest.raises(ReleaseRecordError):
        validate_last_live_release(
            _release(**{field: None}), MODE_NEW_RELEASE,
            publication_configured=True,
        )


def test_publication_parent_may_be_null_only_for_a_root_publication():
    validate_last_live_release(
        _release(publication_parent=None), MODE_NEW_RELEASE,
        publication_configured=True,
    )
    with pytest.raises(ReleaseRecordError):
        validate_last_live_release(
            _release(publication_parent="not-a-commit"), MODE_NEW_RELEASE,
            publication_configured=True,
        )


@pytest.mark.parametrize("field", ["source_sha256", "artifact_sha256"])
def test_source_and_artifact_identity_is_always_required(field):
    with pytest.raises(ReleaseRecordError):
        validate_last_live_release(
            _release(**{field: None}), MODE_NEW_RELEASE,
            publication_configured=True,
        )


def test_a_release_must_record_a_passed_smoke_against_its_own_host():
    validate_last_live_release(
        _release(), MODE_NEW_RELEASE, publication_configured=True)
    with pytest.raises(ReleaseRecordError):
        validate_last_live_release(
            _release(smoke=dict(SMOKE_PASSED, status="FAILED")),
            MODE_NEW_RELEASE, publication_configured=True,
        )
    # Evidence about a DIFFERENT host describes a different URL.
    with pytest.raises(ReleaseRecordError):
        validate_last_live_release(
            _release(smoke=dict(SMOKE_PASSED, target_host="somewhere.else")),
            MODE_NEW_RELEASE, publication_configured=True,
        )


def test_urls_may_not_smuggle_credentials():
    with pytest.raises(ReleaseRecordError):
        validate_last_live_release(
            _release(production_url="https://user:pw@rel1.vercel.app/"),
            MODE_NEW_RELEASE, publication_configured=True,
        )


def test_release_is_known_reads_completeness_not_presence_of_a_commit():
    # A legacy record may carry a publication commit and still not be a
    # complete identity.
    legacy = legacy_live_release(_legacy_last_live(),
                                 {"publication_commit": SHA1_B})
    assert legacy["publication_commit"] == SHA1_B
    assert not release_is_known(legacy)
    assert release_is_known(_release())
    assert not release_is_known(None)

# ---------------------------------------------------------------------------
# LEGACY_PARTIAL validation and the lazy migration
# ---------------------------------------------------------------------------


def test_legacy_migration_preserves_only_r1_facts(store):
    _seed_legacy(store, repository={
        "provider": "github", "repo": "albertus527/website", "branch": "rel1",
        "tested_commit": SHA1, "publication_commit": SHA1_B,
        "source_revision": 1, "sync_status": "SYNCED", "synced_at": 2.0,
    })

    release = store.load(PROJECT).deployment["last_live_release"]

    assert release["completeness"] == COMPLETENESS_LEGACY_PARTIAL
    # What R1 persisted.
    assert release["operation_id"] == "op-0"
    assert release["deployment_id"] == "dpl_old"
    assert release["production_url"] == CANONICAL
    assert release["publication_commit"] == SHA1_B
    # Everything R1 never persisted stays None -- asserted as None, not merely
    # absent, because an inferred value would be indistinguishable from a
    # proven one at every later read.
    for field in LEGACY_UNKNOWN_FIELDS:
        assert release[field] is None, field
    assert release["smoke"] == {"status": None, "at": None}


def test_legacy_migration_seeds_the_branch_parent_authority(store):
    _seed_legacy(store, repository={"publication_commit": SHA1_B,
                                    "branch": "rel1", "synced_at": 2.0})

    state = store.load(PROJECT)

    assert state.deployment["publication_head"]["commit"] == SHA1_B
    assert state.deployment["publication_head"]["branch"] == "rel1"
    assert resolve_branch_parent(state) == SHA1_B
    # ``state.repository`` is retired: its contents are dropped, not carried.
    assert state.repository == {}


def test_no_invented_live_release_without_a_last_live_deployment(store):
    with store.acquire_writer(PROJECT) as state:
        state.lifecycle = ProjectLifecycle.PREVIEW_READY.value
        state.repository = {"publication_commit": SHA1_B}
        store.save(state)

    state = store.load(PROJECT)

    # No evidence a release ever happened, so no release record is created --
    # not even a partial one.
    assert "last_live_release" not in state.deployment
    # The branch authority IS seeded, because a confirmed publication is real
    # evidence of its own kind: it says what was published, not what is live.
    # Keeping the two apart is the whole point of the R2 record.
    assert state.deployment["publication_head"]["commit"] == SHA1_B
    assert resolve_branch_parent(state) == SHA1_B


def test_a_root_publication_leaves_the_branch_authority_absent(store):
    with store.acquire_writer(PROJECT) as state:
        state.lifecycle = ProjectLifecycle.PREVIEW_READY.value
        state.repository = {}
        store.save(state)

    state = store.load(PROJECT)

    assert "publication_head" not in state.deployment
    assert resolve_branch_parent(state) is None


def test_a_legacy_release_is_never_a_new_release():
    legacy = legacy_live_release(_legacy_last_live(),
                                 {"publication_commit": SHA1_B})
    # Accepted as a previous-release / rollback-target identity...
    validate_last_live_release(legacy, MODE_PREVIOUS_RELEASE)
    # ...and refused as a new one.
    with pytest.raises(ReleaseRecordError):
        validate_last_live_release(legacy, MODE_NEW_RELEASE)


def test_a_legacy_partial_record_may_not_gain_fields():
    legacy = legacy_live_release(_legacy_last_live())
    legacy["publication_tree"] = TREE
    with pytest.raises(ReleaseRecordError):
        validate_last_live_release(legacy, MODE_PREVIOUS_RELEASE)


def test_legacy_partial_is_not_upgraded_by_reading_it(store):
    _seed_legacy(store)

    assert store.load(PROJECT).deployment["last_live_release"]["completeness"] == \
        COMPLETENESS_LEGACY_PARTIAL

    # Any ordinary save re-persists the projection, and the completeness must
    # not drift while doing so.
    with store.acquire_writer(PROJECT) as state:
        state.brief["note"] = "touched"
        store.save(state)

    on_disk = json.loads(
        (store.root / f"{PROJECT}.json").read_text(encoding="utf-8"))
    assert on_disk["deployment"]["last_live_release"]["completeness"] == \
        COMPLETENESS_LEGACY_PARTIAL
    assert store.load(PROJECT).deployment["last_live_release"]["completeness"] == \
        COMPLETENESS_LEGACY_PARTIAL


def test_migration_never_rewrites_an_r2_release_record(store):
    complete = _release()
    with store.acquire_writer(PROJECT) as state:
        state.lifecycle = ProjectLifecycle.LIVE.value
        state.deployment["last_live_deployment"] = _legacy_last_live()
        state.deployment["last_live_release"] = complete
        store.save(state)

    reloaded = store.load(PROJECT)

    assert reloaded.deployment["last_live_release"] == complete
    assert reloaded.deployment["last_live_release"]["completeness"] == \
        COMPLETENESS_COMPLETE


# ---------------------------------------------------------------------------
# commit_release
# ---------------------------------------------------------------------------


def test_commit_release_rejects_a_legacy_partial(store):
    _pending_at(store, STAGE_SMOKE_PASSED)
    coordinator = ReleaseCoordinator(store)
    legacy = legacy_live_release(_legacy_last_live())

    with pytest.raises((StaleOperationIntent, ReleaseRecordError)):
        coordinator.commit_release(
            PROJECT, operation_id="op-1", release=legacy)


def test_commit_release_is_the_single_atomic_live_write(store):
    coordinator = ReleaseCoordinator(store)
    _pending_at(store, STAGE_SMOKE_PASSED)
    record = _release()

    committed = coordinator.commit_release(
        PROJECT, operation_id="op-1", release=record)

    state = store.load(PROJECT)
    assert state.lifecycle == ProjectLifecycle.LIVE.value
    assert state.revisions.live_revision == 3
    assert state.production_url == CANONICAL
    assert state.failure is None
    assert state.deployment["last_live_release"] == committed
    # The compatibility projection is DERIVED from the same record, so the two
    # cannot disagree.
    projection = state.deployment["last_live_deployment"]
    assert projection["operation_id"] == committed["operation_id"]
    assert projection["deployment_id"] == committed["deployment_id"]
    assert projection["production_url"] == committed["production_url"]
    assert projection["deployment_url"] == committed["deployment_url"]
    assert projection["source_revision"] == committed["source_revision"]
    assert projection["source_sha256"] == committed["source_sha256"]
    assert projection["artifact_sha256"] == committed["artifact_sha256"]
    assert projection["live_at"] == committed["committed_at"]
    # The pending record is gone: this operation is finished.
    assert "pending_publication" not in state.deployment


def test_commit_release_requires_the_smoke_passed_stage(store):
    coordinator = ReleaseCoordinator(store)
    _pending_at(store, STAGE_PRODUCTION_CONFIRMED)

    with pytest.raises(ReleaseStageError):
        coordinator.commit_release(
            PROJECT, operation_id="op-1", release=_release())


def test_commit_release_refuses_an_unresolved_publication(store):
    coordinator = ReleaseCoordinator(store)
    _pending_at(store, STAGE_SMOKE_PASSED, reconciliation_required=True)

    with pytest.raises(ReleaseStageError):
        coordinator.commit_release(
            PROJECT, operation_id="op-1", release=_release())


def test_commit_release_refuses_a_failed_publication(store):
    coordinator = ReleaseCoordinator(store)
    _pending_at(store, STAGE_SMOKE_PASSED, publication_status="FAILED")

    with pytest.raises(ReleaseStageError):
        coordinator.commit_release(
            PROJECT, operation_id="op-1", release=_release())


def test_commit_release_refuses_a_publication_that_is_still_pending(store):
    """A COMMITTED release is a statement the Git stage made. PENDING means it
    has not made one yet, so a release committed from it claims a publication
    nothing vouched for."""
    coordinator = ReleaseCoordinator(store)
    _pending_at(store, STAGE_SMOKE_PASSED, publication_status=PUBLICATION_PENDING)

    with pytest.raises(ReleaseStageError):
        coordinator.commit_release(
            PROJECT, operation_id="op-1", release=_release())

    # And the same refusal on a NOT_CONFIGURED record, which must be settled
    # as NOT_CONFIGURED rather than still PENDING.
    _pending_at(store, STAGE_SMOKE_PASSED, configured=False,
                publication_status=PUBLICATION_PENDING)
    with pytest.raises(ReleaseStageError):
        coordinator.commit_release(
            PROJECT, operation_id="op-1", release=_not_configured_release())


def test_commit_release_only_accepts_a_settled_publication_status(store):
    coordinator = ReleaseCoordinator(store)
    for status in (PUBLICATION_CONFIRMED, PUBLICATION_NOT_CONFIGURED):
        _pending_at(store, STAGE_SMOKE_PASSED,
                    configured=status == PUBLICATION_CONFIRMED,
                    publication_status=status)
        record = _release() if status == PUBLICATION_CONFIRMED \
            else _not_configured_release()
        assert coordinator.commit_release(
            PROJECT, operation_id="op-1", release=record)["completeness"] == \
            COMPLETENESS_COMPLETE


def test_commit_release_may_not_rewrite_the_operations_publication_fact(store):
    """The persisted flag is corroboration, never authority: the record must
    agree with the pending publication this write commits."""
    coordinator = ReleaseCoordinator(store)
    _pending_at(store, STAGE_SMOKE_PASSED)

    with pytest.raises(ReleaseRecordError):
        coordinator.commit_release(
            PROJECT, operation_id="op-1",
            release=_release(publication_configured=False),
        )
    # ...and the record cannot claim a NOT_CONFIGURED release for an operation
    # that did publish.
    _pending_at(store, STAGE_SMOKE_PASSED, configured=False,
                publication_status=PUBLICATION_NOT_CONFIGURED)
    with pytest.raises(ReleaseRecordError):
        coordinator.commit_release(
            PROJECT, operation_id="op-1",
            release=_not_configured_release(publication_configured=True),
        )


def test_commit_release_is_bound_to_its_operation(store):
    coordinator = ReleaseCoordinator(store)
    _pending_at(store, STAGE_SMOKE_PASSED, operation_id="someone-elses")

    with pytest.raises(StaleOperationIntent):
        coordinator.commit_release(
            PROJECT, operation_id="op-1", release=_release())


def test_commit_release_must_not_disagree_with_the_confirmed_publication(store):
    coordinator = ReleaseCoordinator(store)
    _pending_at(store, STAGE_SMOKE_PASSED)

    with pytest.raises(ReleaseRecordError):
        coordinator.commit_release(
            PROJECT, operation_id="op-1",
            release=_release(publication_commit="f" * 40),
        )


# ---------------------------------------------------------------------------
# NOT_CONFIGURED
# ---------------------------------------------------------------------------


def test_a_not_configured_release_is_complete_with_null_git_identity():
    record = _not_configured_release()

    validate_last_live_release(
        record, MODE_NEW_RELEASE, publication_configured=False)
    assert record["completeness"] == COMPLETENESS_COMPLETE
    # The record is self-describing, so the read side needs no operation
    # record to corroborate it: a NOT_CONFIGURED release IS a known identity.
    validate_last_live_release(record, MODE_PREVIOUS_RELEASE)
    assert release_is_known(record)
    assert release_is_known(_release())


def test_a_legacy_record_records_publication_uncertainty_rather_than_guessing():
    """R1 wrote a repository record only when it attempted a publication, so
    one proves configuration was possible; an absent one proves nothing."""
    proven = legacy_live_release(
        _legacy_last_live(),
        {"provider": "github", "repo": "albertus527/website", "branch": "rel1",
         "publication_commit": SHA1_B, "sync_status": "SYNCED"})
    assert proven["publication_configured"] is True

    attempted = legacy_live_release(
        _legacy_last_live(),
        {"provider": "github", "repo": "albertus527/website", "branch": "rel1",
         "sync_status": "SOURCE_SYNC_REQUIRED"})
    assert attempted["publication_configured"] is True
    assert attempted["publication_commit"] is None

    unknown = legacy_live_release(_legacy_last_live(), {})
    assert unknown["publication_configured"] is None
    validate_last_live_release(unknown, MODE_PREVIOUS_RELEASE)
    assert not release_is_known(unknown)


def test_a_legacy_record_may_not_claim_publication_was_not_configured():
    """R1 had no way to record that, so a record carrying it fabricates
    certainty the historical state never had."""
    legacy = legacy_live_release(_legacy_last_live(), {})
    legacy["publication_configured"] = False
    with pytest.raises(ReleaseRecordError):
        validate_last_live_release(legacy, MODE_PREVIOUS_RELEASE)


def test_build_last_live_release_reads_the_publication_it_commits():
    record = build_last_live_release(
        operation_id="op-1", source_revision=3, source_sha256=SHA256,
        artifact_sha256=SHA256_B, deployment_id="dpl_rel1",
        production_url=CANONICAL, deployment_url=DEPLOYMENT,
        smoke=dict(SMOKE_PASSED), publication=_configured_publication(),
    )
    assert record["tested_commit"] == SHA1
    assert record["tested_tree"] == TREE
    assert record["publication_commit"] == SHA1_B
    assert record["publication_tree"] == TREE
    assert record["publication_branch"] == "rel1"
    assert record["publication_repo"] == "albertus527/website"
    assert record["publication_configured"] is True
    assert set(record) == set(RELEASE_FIELDS)


def test_build_last_live_release_for_an_unconfigured_publication():
    pending = build_pending_publication(
        operation_id="op-1", source_revision=3, source_sha256=SHA256,
        artifact_sha256=SHA256_B,
    )
    record = build_last_live_release(
        operation_id="op-1", source_revision=3, source_sha256=SHA256,
        artifact_sha256=SHA256_B, deployment_id="dpl_rel1",
        production_url=CANONICAL, deployment_url=DEPLOYMENT,
        smoke=dict(SMOKE_PASSED), publication=pending["publication"],
    )
    assert record["completeness"] == COMPLETENESS_COMPLETE
    # No Git identity AT ALL: nothing was published, and there was no tested
    # snapshot commit to bind. Everything the operation actually did is present.
    for field in UNCONFIGURED_IDENTITY_FIELDS:
        assert record[field] is None, field
    # And it says so, rather than leaving a reader to infer it.
    assert record["publication_configured"] is False
    assert record["source_sha256"] == SHA256
    assert record["artifact_sha256"] == SHA256_B


def test_a_not_configured_pending_publication_carries_no_branch_authority(store):
    coordinator = ReleaseCoordinator(store)
    _pending_at(store, STAGE_PREPARED, configured=False)

    coordinator.confirm_publication(PROJECT, "op-1")

    state = store.load(PROJECT)
    pending = state.deployment["pending_publication"]
    assert pending["stage"] == STAGE_GIT_CONFIRMED
    assert pending["publication"]["status"] == PUBLICATION_NOT_CONFIGURED
    # There is no new branch authority when nothing was published.
    assert "publication_head" not in state.deployment


def test_a_configured_publication_advances_the_branch_authority(store):
    coordinator = ReleaseCoordinator(store)
    _pending_at(store, STAGE_PREPARED)

    coordinator.confirm_publication(PROJECT, "op-1", remote_head=SHA1_B)

    state = store.load(PROJECT)
    assert state.deployment["pending_publication"]["publication"]["status"] == \
        PUBLICATION_CONFIRMED
    assert state.deployment["publication_head"]["commit"] == SHA1_B
    assert resolve_branch_parent(state) == SHA1_B


def test_confirmation_refuses_a_head_that_is_not_the_intended_commit(store):
    coordinator = ReleaseCoordinator(store)
    _pending_at(store, STAGE_PREPARED)

    with pytest.raises(ReleaseRecordError):
        coordinator.confirm_publication(PROJECT, "op-1", remote_head=SHA1)


# ---------------------------------------------------------------------------
# Stage writes are operation-bound
# ---------------------------------------------------------------------------


def test_every_stage_write_is_operation_bound(store):
    coordinator = ReleaseCoordinator(store)
    _pending_at(store, STAGE_PREPARED, operation_id="someone-elses")

    with pytest.raises(StaleOperationIntent):
        coordinator.confirm_publication(PROJECT, "op-1")
    with pytest.raises(StaleOperationIntent):
        coordinator.advance_stage(PROJECT, "op-1", STAGE_PRODUCTION_CONFIRMED)
    with pytest.raises(StaleOperationIntent):
        coordinator.mark_terminal_failure(PROJECT, "op-1", "X")
    with pytest.raises(StaleOperationIntent):
        coordinator.mark_reconciliation_required(PROJECT, "op-1", "X")


def test_a_terminal_failure_keeps_the_stage_it_reached(store):
    coordinator = ReleaseCoordinator(store)
    _pending_at(store, STAGE_PRODUCTION_CONFIRMED)

    coordinator.mark_terminal_failure(
        PROJECT, "op-1", "SMOKE_FAILED", publication_failed=False)

    pending = store.load(PROJECT).deployment["pending_publication"]
    # The stage still says how far the operation actually got.
    assert pending["stage"] == STAGE_PRODUCTION_CONFIRMED
    assert pending["outcome"] == "TERMINAL_FAILED"
    assert pending["last_error_code"] == "SMOKE_FAILED"


def test_reconciliation_required_blocks_a_new_operation(store):
    coordinator = ReleaseCoordinator(store)
    _pending_at(store, STAGE_GIT_CONFIRMED)
    coordinator.mark_reconciliation_required(PROJECT, "op-1", ERROR_HEAD_CONFLICT)

    state = store.load(PROJECT)
    assert coordinator.is_reconciliation_required(state)
    pending = state.deployment["pending_publication"]
    assert pending["outcome"] == "RECONCILIATION_REQUIRED"
    assert pending["last_error_code"] == ERROR_HEAD_CONFLICT


def test_a_successful_confirmation_clears_a_recorded_failure(store):
    """A retry that genuinely confirms must not be blocked by the earlier
    attempt's recorded failure."""
    coordinator = ReleaseCoordinator(store)
    _pending_at(store, STAGE_PREPARED, outcome="TERMINAL_FAILED",
                reconciliation_required=True, publication_status="FAILED")

    coordinator.confirm_publication(PROJECT, "op-1")

    pending = store.load(PROJECT).deployment["pending_publication"]
    assert pending["stage"] == STAGE_GIT_CONFIRMED
    assert pending["outcome"] is None
    assert pending["reconciliation_required"] is False
    assert pending["last_error_code"] is None
    assert pending["publication"]["status"] == PUBLICATION_CONFIRMED


def test_ensure_stage_accepts_a_stage_already_reached(store):
    coordinator = ReleaseCoordinator(store)
    _pending_at(store, STAGE_PRODUCTION_CONFIRMED)

    record = coordinator.ensure_stage(
        PROJECT, "op-1", STAGE_PRODUCTION_CONFIRMED,
        production={"deployment_id": "dpl_rel1"},
    )

    assert record["stage"] == STAGE_PRODUCTION_CONFIRMED
    assert record["production"]["deployment_id"] == "dpl_rel1"


def test_ensure_stage_still_refuses_a_regression(store):
    coordinator = ReleaseCoordinator(store)
    _pending_at(store, STAGE_SMOKE_PASSED)

    with pytest.raises(ReleaseStageError):
        coordinator.ensure_stage(
            PROJECT, "op-1", STAGE_PRODUCTION_CONFIRMED)


# ---------------------------------------------------------------------------
# The C/D mapping is a single source of truth
# ---------------------------------------------------------------------------


def test_c_and_d_never_collapse():
    assert VERDICT_ERROR_CODES[VERDICT_C_CONFLICT] == ERROR_HEAD_CONFLICT
    assert VERDICT_ERROR_CODES[VERDICT_D_UNAVAILABLE] == ERROR_HEAD_UNREADABLE
    assert ERROR_HEAD_CONFLICT != ERROR_HEAD_UNREADABLE
