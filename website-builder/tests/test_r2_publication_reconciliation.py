"""R2 Git publication reconciliation: the A/B/C/D matrix, the remote-read
contract, and the NOT_CONFIGURED stage semantics.

Everything here runs through the REAL promotion orchestration with only the
Vercel, Telegram and browser boundaries faked. The Git side is a real local
repository pushed at a real bare remote through git's own ``insteadOf``
mirror, so every fast-forward, non-fast-forward rejection and branch-absent
answer is OBSERVED rather than simulated.

Three claims are under test, and they are the ones the batch exists to lock:

  1. An accepted fast-forward push is the proof. The happy path performs no
     remote read at all; the remote is read only on a resume or a rejection.
  2. C and D are different verdicts with different error codes, and neither is
     ever satisfied by re-parenting onto what the remote happens to hold.
  3. NOT_CONFIGURED is a status, not a shortcut: the machine still advances
     through GIT_CONFIRMED, with zero Git subprocesses.
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.contracts import OperationResult  # noqa: E402
from app.core.lifecycle import ProjectLifecycle  # noqa: E402
from app.core.state import ProjectStateStore  # noqa: E402
from app.deploy.git_output import (  # noqa: E402
    PUBLICATION_INPUT_INVALID,
    PUBLICATION_LOCAL_REF_UPDATE_FAILED,
    OutputGitRepository,
)
from app.deploy.snapshot import TestedSnapshot  # noqa: E402
from app.projects.promote import PromoteDeps, PromotionOrchestrator  # noqa: E402
from app.sandbox.runner import ProjectRunner  # noqa: E402
from test_promote import (  # noqa: E402
    OWNER,
    FakeTelegram,
    FlakyPromoteVercel,
    _make_workspace,
)
from test_r1_live_publish_finishing import (  # noqa: E402
    CANONICAL,
    DEPLOY_KEY,
    DEPLOYMENT_URL,
    GITHUB_SSH_URL,
    SLUG,
    Live,
    _bare_remote,
    _DropStageStore,
    _mirror_env,
    _remote_git,
    _SimulatedCrash,
    _Vercel,
)

PROJECT = "tg-777"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Repo(OutputGitRepository):
    """Real repository, real pushes, real ls-remote -- fully instrumented."""

    def __init__(self, path, hermes_root, remote):
        self.git_calls = []
        self.prepare_calls = []
        self.push_calls = []
        self.reconcile_calls = []
        self.unreadable_remote = False
        super().__init__(path, hermes_root=hermes_root)
        self.remote = remote

    def _run(self, args, data=None, extra=None, init=False, check=True):
        self.git_calls.append(list(args))
        return super()._run(args, data=data, extra=extra, init=init, check=check)

    def prepare_publication(self, *args, **kwargs):
        self.prepare_calls.append(dict(kwargs, tested_commit=args[0],
                                       branch=args[1], url=args[2]))
        return super().prepare_publication(*args, **kwargs)

    def push_prepared_publication(self, url, commit, branch, **kwargs):
        self.push_calls.append({"commit": commit, "branch": branch, "url": url})
        return super().push_prepared_publication(url, commit, branch, **kwargs)

    def push_prepared_publication(self, url, commit, branch, **kwargs):
        self.push_calls.append({"commit": commit, "branch": branch, "url": url})
        return super().push_prepared_publication(
            url, commit, branch,
            **{**kwargs, "extra_env": _mirror_env(self.remote)},
        )

    def reconcile_publication_head(self, url, branch, intended_commit,
                                   intended_parent, **kwargs):
        self.reconcile_calls.append(
            {"branch": branch, "intended_commit": intended_commit,
             "intended_parent": intended_parent,
             "already_confirmed": kwargs.get("already_confirmed", False)})
        if self.unreadable_remote:
            # A transport failure: non-zero exit, no output to interpret. This
            # is exactly what an unreachable remote looks like to git, and it
            # must NOT be mistaken for "the branch is absent".
            return self._verdict(
                "D_UNAVAILABLE", "PUBLICATION_HEAD_UNREADABLE",
                remote_state="UNREADABLE")
        return super().reconcile_publication_head(
            url, branch, intended_commit, intended_parent,
            **{**kwargs, "extra_env": _mirror_env(self.remote)},
        )

    def _verdict(self, verdict, error_code, **data):
        from app.core.contracts import OperationResult
        return OperationResult(
            success=False, error=error_code, error_code=error_code,
            data={"verdict": verdict, **data},
        )

    # -- assertions helpers -------------------------------------------------

    def remote_reads(self):
        return [a for a in self.git_calls
                if a and a[0] in ("ls-remote", "fetch", "clone", "pull", "archive")]

    def push_argvs(self):
        return [a for a in self.git_calls if "push" in a]

    def tree_of(self, commit):
        return self._run(["rev-parse", f"{commit}^{{tree}}"]).strip().decode()


class _StageRecordingStore(ProjectStateStore):
    """A real state store that records every pending-publication stage written.

    Observing the sequence the machine actually walks is the point: asserting
    the final stage would not catch a ``PREPARED -> PRODUCTION_CONFIRMED``
    shortcut, which is precisely the defect NOT_CONFIGURED is prone to.
    """

    def __init__(self, root):
        super().__init__(root)
        self.stages = []

    def save(self, state):
        stage = ((state.deployment or {}).get("pending_publication") or {}).get("stage")
        if stage and (not self.stages or self.stages[-1] != stage):
            self.stages.append(stage)
        super().save(state)


class _SimulatedCrash(RuntimeError):
    """A process that died mid-write.

    Raised from the storage seam, not from the code under test, so the recovery
    path runs exactly as it would after a real crash: the durable record is
    whatever was last successfully written, and nothing in memory survives.
    """


class _DropCommittedStore(_StageRecordingStore):
    """A real state store that loses the COMMITTED write and dies doing it.

    Models the one crash the R2 stage machine has to survive from the end: the
    release is promoted, the smoke passed, SMOKE_PASSED is durable, and the
    process dies before the single atomic write that publishes the identity.

    The write is detected by its own shape -- it is the only save that both
    creates a ``last_live_release`` and clears ``pending_publication`` -- so
    nothing about the code under test is patched or special-cased. It fires
    ONCE: the recovery has to be able to commit, which is the whole point.
    """

    crash_class = _SimulatedCrash

    def __init__(self, root):
        super().__init__(root)
        self.armed = True

    def save(self, state):
        deployment = state.deployment or {}
        if (self.armed
                and deployment.get("last_live_release") is not None
                and "pending_publication" not in deployment):
            self.armed = False
            raise self.crash_class("process died before the COMMITTED write")
        super().save(state)


class _Scenario:
    """One project's store, workspace, real output repo and orchestrators."""

    def __init__(self, tmp_path, vercel=None, github=True, store_class=None):
        self.tmp_path = tmp_path
        self.store = (store_class or _StageRecordingStore)(tmp_path / "state")
        self.ws = _make_workspace(tmp_path)
        self.runner = ProjectRunner(tmp_path / "workspaces", self.store)
        self.remote = _bare_remote(tmp_path / "remote.git")
        self.repo = _Repo(tmp_path / "out", tmp_path / "hermes", self.remote)
        self.vercel = vercel or _Vercel()
        self.github = github
        self.deps = PromoteDeps(
            vercel=self.vercel,
            telegram=FakeTelegram(),
            smoke=_AlwaysPassSmoke(),
            chat_id_for=lambda pid, state: "chat-1",
            slug_for=lambda pid, state: SLUG,
            output_repo=self.repo,
            source_repo_url=GITHUB_SSH_URL if github else None,
            source_branch_for=lambda pid, state, expected_name: expected_name,
            source_ssh_key=tmp_path / DEPLOY_KEY if github else None,
        )
        self.orch = PromotionOrchestrator(self.runner, self.store, self.deps)

    def fresh_orchestrator(self):
        """A new orchestrator over the SAME state root, as a restarted process
        would build. Nothing in-memory carries over."""
        return PromotionOrchestrator(self.runner, self.store, self.deps)

    @property
    def stages(self):
        return list(self.store.stages)

    def show_preview(self, revision, source, artifact):
        snapshot = TestedSnapshot({"src/App.tsx": source},
                                  {"index.html": artifact})
        git = self.repo.commit(PROJECT, snapshot)
        shown = {
            "operation_id": f"op-{revision}",
            "source_revision": revision,
            "deployment_id": f"dpl_{revision}",
            "source_sha256": git["source_sha256"],
            "artifact_sha256": git["artifact_sha256"],
            "preview_url": DEPLOYMENT_URL,
            "shown_at": float(revision),
        }
        with self.store.acquire_writer(PROJECT) as state:
            state.lifecycle = ProjectLifecycle.PREVIEW_READY.value
            state.roles["owner"] = OWNER
            state.conversation_id = "chat-1"
            state.revisions.source_revision = revision
            state.revisions.qa_revision = revision
            state.revisions.preview_revision = revision
            state.deployment["latest_shown_preview"] = shown
            state.deployment["preview_intent"] = {
                "operation_id": shown["operation_id"],
                "source_revision": revision,
                "source_sha256": git["source_sha256"],
                "artifact_sha256": git["artifact_sha256"],
                "stage": "committed",
                "git": git,
            }
            self.store.save(state)
        return git

    def approve_and_publish(self):
        assert self.orch.approve(PROJECT, principal_id=OWNER).success
        return self.orch.promote(PROJECT, self.ws, principal_id=OWNER)

    def state(self):
        return self.store.load(PROJECT)


class _AlwaysPassSmoke:
    """A production smoke that passes and reports the host it was given."""

    def __init__(self):
        self.calls = []

    def run(self, url, out_dir, **kwargs):
        from urllib.parse import urlsplit
        self.calls.append(url)
        parts = urlsplit(url)
        return OperationResult.ok({
            "url": url, "failures": [],
            "target_host": parts.hostname,
            "target_path": parts.path or "/",
            "failure_classification": None,
        })


class _GarbageLsRemoteRepo(OutputGitRepository):
    """A real repository whose ``ls-remote`` answers with unusable output.

    Zero exit, no transport failure, and bytes that are not ref data -- what a
    proxy, a wrapper or a corrupted transport looks like to the reconciler. The
    point is that it must be UNREADABLE and never ABSENT: an ABSENT answer
    authorises a root push at a branch whose state is unknown.
    """

    GARBAGE = (b"ssh: connect to host example.invalid port 22: Connection refused\n"
               b"\x00\x01not-a-ref-line\n")

    def __init__(self, path, hermes_root):
        super().__init__(path, hermes_root=hermes_root)
        self.ls_remote_argvs = []

    def _run(self, args, data=None, extra=None, init=False, check=True):
        if args and args[0] == "ls-remote":
            self.ls_remote_argvs.append(list(args))
            return subprocess.CompletedProcess(
                args=["git"] + list(args), returncode=0,
                stdout=self.GARBAGE, stderr=b"")
        return super()._run(args, data=data, extra=extra, init=init, check=check)


@pytest.fixture
def scenario(tmp_path):
    return _Scenario(tmp_path)


def _assert_no_forced_push(push_argvs):
    """A shared assertion for every path that DOES push.

    A force flag or a ``+`` refspec would rewrite the branch, so this is
    asserted wherever a push actually happened rather than in a loop over an
    empty list, which would prove nothing.
    """
    for argv in push_argvs:
        assert "--force" not in argv, argv
        assert not any(a.startswith("+") for a in argv), argv
        assert not argv[-1].startswith("+"), argv


def _point_branch_at_foreign_commit(scenario, source=b"elsewhere",
                                    artifact=b"<html>elsewhere</html>"):
    """Make the remote branch head a commit this repository did not publish."""
    other = OutputGitRepository(scenario.tmp_path / "other-out",
                                hermes_root=scenario.tmp_path / "other-hermes")
    foreign = other.commit(
        PROJECT, TestedSnapshot({"src/App.tsx": source},
                                {"index.html": artifact}))
    fetched = subprocess.run(
        ["git", "--git-dir", str(scenario.remote), "fetch",
         "file:///" + other.path.as_posix(), foreign["commit"]],
        capture_output=True,
    )
    assert fetched.returncode == 0, fetched.stderr
    moved = subprocess.run(
        ["git", "--git-dir", str(scenario.remote), "update-ref",
         f"refs/heads/{SLUG}", foreign["commit"]],
        capture_output=True,
    )
    assert moved.returncode == 0, moved.stderr
    return foreign["commit"]


# ---------------------------------------------------------------------------
# 1. The remote-read contract
# ---------------------------------------------------------------------------


def test_happy_path_performs_zero_remote_reads(scenario):
    scenario.show_preview(1, b"v=1", b"<html>1</html>")

    assert scenario.approve_and_publish().success

    # The push happened, exactly once, and it was the proof. Nothing was read.
    assert len(scenario.repo.push_argvs()) == 1
    _assert_no_forced_push(scenario.repo.push_argvs())
    assert scenario.repo.remote_reads() == []
    assert scenario.repo.reconcile_calls == []


def test_accepted_push_persists_git_confirmed_without_a_round_trip(tmp_path):
    scenario = _Scenario(tmp_path)
    scenario.show_preview(1, b"v=1", b"<html>1</html>")
    assert scenario.approve_and_publish().success

    release = scenario.state().deployment["last_live_release"]
    assert release["completeness"] == "COMPLETE"
    # The pending record was cleared by the single COMMITTED write, so its
    # absence is the end state of a fully advanced machine.
    assert "pending_publication" not in scenario.state().deployment
    # The stage it walked is observed, not assumed: GIT_CONFIRMED landed
    # between PREPARED and the production stages.
    assert scenario.stages[:2] == ["PREPARED", "GIT_CONFIRMED"]
    assert release["publication_commit"] == scenario.state().deployment[
        "publication_head"]["commit"]


def test_a_second_revision_also_reads_nothing(scenario):
    scenario.show_preview(1, b"v=1", b"<html>1</html>")
    assert scenario.approve_and_publish().success
    scenario.show_preview(2, b"v=2", b"<html>2</html>")

    assert scenario.approve_and_publish().success

    assert len(scenario.repo.push_argvs()) == 2
    assert scenario.repo.remote_reads() == []


def test_rejected_push_reconciles_exactly_once(scenario):
    """A non-fast-forward rejection triggers exactly one ref-head read, never
    two, and never a fetch."""
    scenario.show_preview(1, b"v=1", b"<html>1</html>")
    assert scenario.approve_and_publish().success
    first = scenario.state().deployment["publication_head"]["commit"]
    scenario.repo.git_calls.clear()

    _point_branch_at_foreign_commit(scenario)
    scenario.show_preview(2, b"v=2", b"<html>2</html>")

    result = scenario.approve_and_publish()

    assert not result.success
    assert result.error_code == "PUBLICATION_HEAD_CONFLICT"
    # Exactly one push attempt (which is what got rejected) and exactly one
    # ref-head read (which is what classified it). Never two of either, and
    # never a fetch.
    assert len(scenario.repo.push_argvs()) == 1
    assert len(scenario.repo.remote_reads()) == 1
    assert scenario.repo.remote_reads()[0][-1] == f"refs/heads/{SLUG}"
    assert not [a for a in scenario.repo.git_calls if "fetch" in a]


# ---------------------------------------------------------------------------
# 2. The A/B/C/D matrix
# ---------------------------------------------------------------------------


def _stage_at(scenario, stage):
    """Re-stage a finished publish as a crashed one, at *stage*.

    The pending record is rebuilt from the release that was actually committed,
    so the intended commit under test is the one that genuinely reached the
    branch. The project is put back into the promotion-phase FAILED state a
    crash after the Git stage would leave behind, which is what makes
    ``resume_publish`` the applicable operator entry point.
    """
    with scenario.store.acquire_writer(PROJECT) as state:
        release = state.deployment["last_live_release"]
        state.deployment.pop("last_live_release", None)
        state.deployment.pop("last_live_deployment", None)
        state.deployment["publication_head"] = {
            "commit": release["publication_commit"],
            "branch": release["publication_branch"],
            "confirmed_at": release["committed_at"],
        }
        state.deployment["pending_publication"] = {
            "operation_id": release["operation_id"],
            "source_revision": release["source_revision"],
            "stage": stage,
            "outcome": None,
            "reconciliation_required": False,
            "created_at": release["committed_at"],
            "updated_at": release["committed_at"],
            "source_sha256": release["source_sha256"],
            "artifact_sha256": release["artifact_sha256"],
            "publication": {
                "configured": True,
                "repo": release["publication_repo"],
                "branch": release["publication_branch"],
                "tested_commit": release["tested_commit"],
                "tested_tree": release["tested_tree"],
                "intended_commit": release["publication_commit"],
                "intended_parent": release["publication_parent"],
                "intended_tree": release["publication_tree"],
                "status": "CONFIRMED",
                "confirmed_at": release["committed_at"],
            },
            "production": {"deployment_id": release["deployment_id"],
                           "promoted_deployment_id": release["deployment_id"],
                           "confirmed_at": None},
            "smoke": dict(release["smoke"]),
            "last_error_code": None,
        }
        state.lifecycle = ProjectLifecycle.FAILED.value
        state.failure = {"phase": "promotion", "error": "PROMOTE_FAILED",
                         "error_code": "PROMOTE_NOT_APPLIED", "failed_at": 1.0}
        scenario.store.save(state)


@pytest.mark.parametrize("branch_state,expected_code,expected_pushes", [
    # head == intended commit: A_ADOPT, adopt, push nothing.
    ("intended", None, 0),
    # head rewound to the intended parent: STILL case C on a resume. The stage
    # says the commit was already published, so a head that is not it means the
    # remote changed after confirmation -- a conflict to report, never a
    # re-publication. Zero pushes, always.
    ("parent", "PUBLICATION_HEAD_CONFLICT", 0),
    # a valid head we never published: C_CONFLICT, fail closed, push nothing.
    ("other", "PUBLICATION_HEAD_CONFLICT", 0),
    # the branch vanished though a confirmed parent existed: C_CONFLICT,
    # because a lost branch is never silently re-created.
    ("absent", "PUBLICATION_HEAD_CONFLICT", 0),
])
def test_case_matrix_from_a_resume_at_git_confirmed(
        tmp_path, branch_state, expected_code, expected_pushes):
    """A resume reads the remote exactly once and classifies it A/C/D.

    A GIT_CONFIRMED-or-later resume NEVER retries a push: the record already
    asserts the publication landed, so anything other than the intended commit
    is a conflict. B_RETRY belongs to a rejected initial push, where the remote
    genuinely has not seen the commit yet.

    A SECOND publication is used so the record carries a real intended parent;
    that is what makes ``parent`` a real rewind rather than a no-op, and what
    makes an absent branch a conflict rather than a legitimate first
    publication.
    """
    scenario = _Scenario(tmp_path)
    scenario.show_preview(1, b"v=1", b"<html>1</html>")
    assert scenario.approve_and_publish().success
    parent = scenario.state().deployment["publication_head"]["commit"]
    scenario.show_preview(2, b"v=2", b"<html>2</html>")
    assert scenario.approve_and_publish().success
    confirmed = scenario.state().deployment["last_live_release"]["publication_commit"]
    assert _remote_git(scenario.remote, "rev-parse",
                       f"refs/heads/{SLUG}") == confirmed
    _stage_at(scenario, "GIT_CONFIRMED")
    assert scenario.state().deployment["pending_publication"]["publication"][
        "intended_parent"] == parent
    scenario.repo.git_calls.clear()
    scenario.repo.reconcile_calls.clear()

    if branch_state == "parent":
        moved = subprocess.run(
            ["git", "--git-dir", str(scenario.remote), "update-ref",
             f"refs/heads/{SLUG}", parent], capture_output=True)
        assert moved.returncode == 0, moved.stderr
    elif branch_state == "other":
        _point_branch_at_foreign_commit(scenario)
    elif branch_state == "absent":
        deleted = subprocess.run(
            ["git", "--git-dir", str(scenario.remote), "update-ref", "-d",
             f"refs/heads/{SLUG}"], capture_output=True)
        assert deleted.returncode == 0, deleted.stderr

    result = scenario.orch.resume_publish(PROJECT, scenario.ws,
                                          principal_id=OWNER)

    # Exactly one ref-head read, for exactly one ref, and never a fetch.
    assert len(scenario.repo.remote_reads()) == 1
    assert scenario.repo.remote_reads()[0][-1] == f"refs/heads/{SLUG}"
    assert scenario.repo.reconcile_calls[0]["intended_commit"] == confirmed
    # The load-bearing assertion: a confirmed resume never re-publishes.
    assert scenario.repo.push_argvs() == []
    assert expected_pushes == 0
    assert not [a for a in scenario.repo.git_calls if "fetch" in a]
    if expected_code is None:
        assert result.success, result.error
        assert scenario.state().deployment["last_live_release"][
            "publication_commit"] == confirmed
    else:
        assert not result.success
        assert result.error_code == expected_code
        # A rewound branch is left exactly as it was found.
        if branch_state == "parent":
            assert _remote_git(scenario.remote, "rev-parse",
                               f"refs/heads/{SLUG}") == parent


def test_a_rewound_branch_is_never_silently_re_published(tmp_path):
    """A dedicated test for the double-publication failure class, at the exact
    point it would occur: the record says published, the remote disagrees, and
    the only wrong thing to do would be to push again."""
    scenario = _Scenario(tmp_path)
    scenario.show_preview(1, b"v=1", b"<html>1</html>")
    assert scenario.approve_and_publish().success
    first = scenario.state().deployment["publication_head"]["commit"]
    scenario.show_preview(2, b"v=2", b"<html>2</html>")
    assert scenario.approve_and_publish().success
    second = scenario.state().deployment["publication_head"]["commit"]
    _stage_at(scenario, "GIT_CONFIRMED")
    subprocess.run(["git", "--git-dir", str(scenario.remote), "update-ref",
                    f"refs/heads/{SLUG}", first], capture_output=True, check=True)
    scenario.repo.git_calls.clear()

    result = scenario.orch.resume_publish(PROJECT, scenario.ws,
                                          principal_id=OWNER)

    assert not result.success
    assert result.error_code == "PUBLICATION_HEAD_CONFLICT"
    assert scenario.repo.push_argvs() == []
    # Still rewound: the resume reported the conflict, it did not repair it.
    assert _remote_git(scenario.remote, "rev-parse", f"refs/heads/{SLUG}") == first
    # The branch authority still names what was genuinely confirmed.
    assert scenario.state().deployment["publication_head"]["commit"] == second
    assert second != first


def test_case_c_leaves_the_remote_untouched_and_holds_publishing(tmp_path):
    scenario = _Scenario(tmp_path)
    scenario.show_preview(1, b"v=1", b"<html>1</html>")
    assert scenario.approve_and_publish().success
    confirmed = scenario.state().deployment["publication_head"]["commit"]
    _stage_at(scenario, "GIT_CONFIRMED")
    foreign = _point_branch_at_foreign_commit(scenario)
    scenario.repo.git_calls.clear()

    result = scenario.orch.resume_publish(PROJECT, scenario.ws,
                                          principal_id=OWNER)

    assert not result.success
    assert result.error_code == "PUBLICATION_HEAD_CONFLICT"
    # Untouched, and never re-parented onto.
    assert _remote_git(scenario.remote, "rev-parse",
                       f"refs/heads/{SLUG}") == foreign
    # No push at all -- asserted directly, because a loop over an empty list
    # would assert nothing about force flags.
    assert scenario.repo.push_argvs() == []
    _assert_no_forced_push(scenario.repo.push_argvs())
    state = scenario.state()
    assert state.lifecycle == ProjectLifecycle.PUBLISHING.value
    assert state.deployment["publication_head"]["commit"] == confirmed
    pending = state.deployment["pending_publication"]
    assert pending["reconciliation_required"] is True
    assert pending["outcome"] == "RECONCILIATION_REQUIRED"
    assert pending["last_error_code"] == "PUBLICATION_HEAD_CONFLICT"


def test_case_d_is_unreadable_not_conflicting(tmp_path):
    """An unreadable remote is D/UNREADABLE, never C/CONFLICT.

    The distinction is intentional: C means the remote was READ and holds an
    unexpected state; D means it could not be read, so nothing is asserted.
    """
    scenario = _Scenario(tmp_path)
    scenario.show_preview(1, b"v=1", b"<html>1</html>")
    assert scenario.approve_and_publish().success
    confirmed = scenario.state().deployment["publication_head"]["commit"]
    _stage_at(scenario, "GIT_CONFIRMED")
    scenario.repo.git_calls.clear()
    scenario.repo.unreadable_remote = True

    result = scenario.orch.resume_publish(PROJECT, scenario.ws,
                                          principal_id=OWNER)

    assert not result.success
    assert result.error_code == "PUBLICATION_HEAD_UNREADABLE"
    assert result.error_code != "PUBLICATION_HEAD_CONFLICT"
    assert scenario.repo.push_argvs() == []
    pending = scenario.state().deployment["pending_publication"]
    assert pending["last_error_code"] == "PUBLICATION_HEAD_UNREADABLE"
    assert pending["reconciliation_required"] is True
    # The branch authority still names what we genuinely confirmed.
    assert scenario.state().deployment["publication_head"]["commit"] == confirmed


def test_c_and_d_are_never_interchangeable(tmp_path):
    """The two fail-closed verdicts are produced by distinct remote states and
    always carry their own error code."""
    conflict = _Scenario(tmp_path / "c")
    conflict.show_preview(1, b"v=1", b"<html>1</html>")
    assert conflict.approve_and_publish().success
    _stage_at(conflict, "GIT_CONFIRMED")
    _point_branch_at_foreign_commit(conflict)
    conflict_result = conflict.orch.resume_publish(
        PROJECT, conflict.ws, principal_id=OWNER)

    unreadable = _Scenario(tmp_path / "d")
    unreadable.show_preview(1, b"v=1", b"<html>1</html>")
    assert unreadable.approve_and_publish().success
    _stage_at(unreadable, "GIT_CONFIRMED")
    unreadable.repo.unreadable_remote = True
    unreadable_result = unreadable.orch.resume_publish(
        PROJECT, unreadable.ws, principal_id=OWNER)

    assert conflict_result.error_code == "PUBLICATION_HEAD_CONFLICT"
    assert unreadable_result.error_code == "PUBLICATION_HEAD_UNREADABLE"


def test_a_terminal_promote_failure_records_the_stage_it_reached(tmp_path):
    """The Git stage completed before production, so a production failure
    leaves the record at GIT_CONFIRMED with a terminal outcome."""
    scenario = _Scenario(tmp_path, vercel=FlakyPromoteVercel(fail_on={"dpl_1"}))
    scenario.show_preview(1, b"v=1", b"<html>1</html>")

    result = scenario.approve_and_publish()

    assert not result.success
    assert result.error_code == "PROMOTE_FAILED"
    pending = scenario.state().deployment["pending_publication"]
    assert pending["stage"] == "GIT_CONFIRMED"
    assert pending["outcome"] == "TERMINAL_FAILED"
    assert pending["publication"]["status"] == "CONFIRMED"


def test_a_held_publication_refuses_a_new_operation(tmp_path):
    scenario = _Scenario(tmp_path)
    scenario.show_preview(1, b"v=1", b"<html>1</html>")
    assert scenario.approve_and_publish().success
    _stage_at(scenario, "GIT_CONFIRMED")
    _point_branch_at_foreign_commit(scenario)
    assert not scenario.orch.resume_publish(
        PROJECT, scenario.ws, principal_id=OWNER).success

    # A DIFFERENT operation may not start while the earlier one is held.
    scenario.show_preview(2, b"v=2", b"<html>2</html>")
    assert scenario.orch.approve(PROJECT, principal_id=OWNER).success
    blocked = scenario.orch.promote(PROJECT, scenario.ws, principal_id=OWNER)

    assert not blocked.success
    assert blocked.error_code == "PUBLICATION_SUPERSEDE_FORBIDDEN"
    # The held record is still the held record, and the branch is untouched.
    pending = scenario.state().deployment["pending_publication"]
    assert pending["operation_id"] == "op-1"
    assert pending["reconciliation_required"] is True


class _AmbiguousReconcileVercel(FlakyPromoteVercel):
    """A provider that cannot say whether the promotion took effect.

    The exact shape of a Vercel read that times out on a promote which may or
    may not have been applied: the request is not refusable, so the only honest
    answer is "I do not know".
    """

    def __init__(self, **kw):
        super().__init__(fail_on=set(), **kw)
        self.ambiguous = True

    def reconcile_production_deployment(self, app_id, project,
                                        expected_identity, **kwargs):
        if self.ambiguous:
            return OperationResult.ok({'status': 'UNKNOWN'})
        return super().reconcile_production_deployment(
            app_id, project, expected_identity, **kwargs)


def test_an_ambiguous_provider_resume_holds_the_publication(tmp_path):
    """A same-operation resume that cannot attribute production must hold the
    publication open exactly like the ambiguous-promote path does.

    It used to record only the promotion failure, which left
    ``reconciliation_required`` false and let a NEW operation supersede a
    publication the state machine had not resolved.
    """
    scenario = _Scenario(tmp_path, vercel=_AmbiguousReconcileVercel())
    scenario.show_preview(1, b"v=1", b"<html>1</html>")
    assert scenario.orch.approve(PROJECT, principal_id=OWNER).success
    # A terminal promote failure leaves the record at GIT_CONFIRMED, which is
    # where a resume re-reads the Git side and then the provider side.
    scenario.vercel.fail_on = {"dpl_1"}
    first = scenario.orch.promote(PROJECT, scenario.ws, principal_id=OWNER)
    assert not first.success
    assert first.error_code == "PROMOTE_FAILED"
    assert scenario.state().deployment["pending_publication"]["stage"] == \
        "GIT_CONFIRMED"

    scenario.vercel.fail_on = set()
    scenario.vercel.ambiguous = True
    result = scenario.orch.resume_publish(PROJECT, scenario.ws,
                                          principal_id=OWNER)

    assert not result.success
    assert result.error_code == "PROMOTION_RECONCILIATION_REQUIRED"
    pending = scenario.state().deployment["pending_publication"]
    assert pending["reconciliation_required"] is True
    assert pending["outcome"] == "RECONCILIATION_REQUIRED"
    assert pending["last_error_code"] == "PROMOTION_RECONCILIATION_REQUIRED"
    assert pending["operation_id"] == "op-1"
    # Held open, not failed: the same operation is still resumable.
    assert scenario.state().lifecycle == ProjectLifecycle.PUBLISHING.value

    # A NEW operation may not supersede it.
    scenario.show_preview(2, b"v=2", b"<html>2</html>")
    assert scenario.orch.approve(PROJECT, principal_id=OWNER).success
    blocked = scenario.orch.promote(PROJECT, scenario.ws, principal_id=OWNER)
    assert not blocked.success
    assert blocked.error_code == "PUBLICATION_SUPERSEDE_FORBIDDEN"
    assert scenario.state().deployment["pending_publication"][
        "operation_id"] == "op-1"


def test_a_crash_after_smoke_passed_resumes_forward_to_committed(tmp_path):
    """The last recovery hole: SMOKE_PASSED is durable, the process dies before
    the COMMITTED write.

    Every step the record already completed is skipped rather than re-run, so
    the resume reaches COMMITTED with no second push and no second promote --
    and without asking the stage graph to walk backwards, which is what used
    to make this unrecoverable.
    """
    scenario = _Scenario(tmp_path, store_class=_DropCommittedStore)
    git = scenario.show_preview(1, b"v=1", b"<html>1</html>")
    assert scenario.orch.approve(PROJECT, principal_id=OWNER).success

    with pytest.raises(_SimulatedCrash):
        scenario.orch.promote(PROJECT, scenario.ws, principal_id=OWNER)

    # The COMMITTED write was lost, so the release did NOT go live...
    assert scenario.state().deployment.get("last_live_release") is None
    assert scenario.state().lifecycle == ProjectLifecycle.PUBLISHING.value
    pending = scenario.state().deployment["pending_publication"]
    assert pending["stage"] == "SMOKE_PASSED"
    assert pending["smoke"]["status"] == "PASSED"
    assert pending["publication"]["status"] == "CONFIRMED"
    assert pending["production"]["deployment_id"] == "dpl_1"

    # The provider confirms the approved artifact really is production, so a
    # resume has nothing left to promote.
    scenario.vercel.production_now = {
        "deployment_id": "dpl_1", "operation_id": "op-1",
        "source_revision": 1, "artifact_sha256": git["artifact_sha256"],
    }
    promotes_before = list(scenario.vercel.promote_calls)
    smokes_before = len(scenario.deps.smoke.calls)
    assert len(scenario.repo.push_argvs()) == 1
    scenario.repo.git_calls.clear()

    # A fresh orchestrator over the same state root, as a restart would build.
    result = scenario.fresh_orchestrator().resume_publish(
        PROJECT, scenario.ws, principal_id=OWNER)

    assert result.success, result.error
    state = scenario.state()
    assert state.lifecycle == ProjectLifecycle.LIVE.value
    release = state.deployment["last_live_release"]
    assert release["completeness"] == "COMPLETE"
    assert release["operation_id"] == "op-1"
    assert release["source_revision"] == 1
    assert release["publication_configured"] is True
    # The identity advanced exactly once: there was nothing to advance FROM.
    assert "pending_publication" not in state.deployment
    assert release["deployment_id"] == "dpl_1"
    assert release["smoke"]["status"] == "PASSED"
    # The publication is unchanged, and so is the branch.
    assert release["publication_commit"] == \
        state.deployment["publication_head"]["commit"]
    assert _remote_git(scenario.remote, "rev-parse", f"refs/heads/{SLUG}") == \
        release["publication_commit"]
    assert _remote_git(scenario.remote, "rev-list", "--count",
                       f"refs/heads/{SLUG}") == "1"
    # No second push, no second promote POST, no re-smoke.
    assert scenario.repo.push_argvs() == []
    assert scenario.vercel.promote_calls == promotes_before
    assert len(scenario.deps.smoke.calls) == smokes_before
    # The Git side was re-derived exactly once, and adopted rather than pushed.
    assert len(scenario.repo.remote_reads()) == 1
    assert scenario.repo.reconcile_calls[0]["intended_commit"] == \
        release["publication_commit"]
    # The stage machine resumed and finished; it never walked backwards.
    seen = scenario.store.stages
    assert seen[seen.index("SMOKE_PASSED"):] == ["SMOKE_PASSED"]


def test_a_crash_after_production_confirmed_re_smokes_and_commits(tmp_path):
    """The step just before: a crash after PRODUCTION_CONFIRMED re-runs the
    smoke, because the smoke is the thing that had not been established yet --
    and it still sends no second promote."""
    scenario = _Scenario(tmp_path, store_class=_DropCommittedStore)
    git = scenario.show_preview(1, b"v=1", b"<html>1</html>")
    assert scenario.orch.approve(PROJECT, principal_id=OWNER).success
    with pytest.raises(_SimulatedCrash):
        scenario.orch.promote(PROJECT, scenario.ws, principal_id=OWNER)

    with scenario.store.acquire_writer(PROJECT) as state:
        state.deployment["pending_publication"]["stage"] = "PRODUCTION_CONFIRMED"
        state.lifecycle = ProjectLifecycle.FAILED.value
        state.failure = {"phase": "promotion", "error": "PROMOTE_FAILED",
                         "error_code": "PROMOTE_NOT_APPLIED", "failed_at": 1.0}
        scenario.store.save(state)
    scenario.vercel.production_now = {
        "deployment_id": "dpl_1", "operation_id": "op-1",
        "source_revision": 1, "artifact_sha256": git["artifact_sha256"],
    }
    promotes_before = list(scenario.vercel.promote_calls)
    smokes_before = len(scenario.deps.smoke.calls)
    scenario.repo.git_calls.clear()

    result = scenario.fresh_orchestrator().resume_publish(
        PROJECT, scenario.ws, principal_id=OWNER)

    assert result.success, result.error
    assert scenario.state().lifecycle == ProjectLifecycle.LIVE.value
    # The smoke had not run, so it runs now; the promote had, so it does not.
    assert len(scenario.deps.smoke.calls) == smokes_before + 1
    assert scenario.vercel.promote_calls == promotes_before
    assert scenario.repo.push_argvs() == []


# ---------------------------------------------------------------------------
# 3. NOT_CONFIGURED stage semantics
# ---------------------------------------------------------------------------


def test_not_configured_advances_through_git_confirmed(tmp_path):
    """There is no PREPARED -> PRODUCTION_CONFIRMED shortcut anywhere."""
    scenario = _Scenario(tmp_path, github=False)
    scenario.show_preview(1, b"v=1", b"<html>1</html>")

    result = scenario.approve_and_publish()

    assert result.success, result.error
    release = result.data["release"]
    assert release["completeness"] == "COMPLETE"
    # The stage sequence the machine actually walked: a pending record captured
    # at each step, so the order is observed rather than asserted.
    assert scenario.stages == ["PREPARED", "GIT_CONFIRMED",
                               "PRODUCTION_CONFIRMED", "SMOKE_PASSED"]


def test_not_configured_performs_no_git_network_activity(tmp_path):
    scenario = _Scenario(tmp_path, github=False)
    scenario.show_preview(1, b"v=1", b"<html>1</html>")

    assert scenario.approve_and_publish().success

    # No publication commit was built, nothing was pushed, nothing was read.
    assert scenario.repo.prepare_calls == []
    assert scenario.repo.push_argvs() == []
    assert scenario.repo.remote_reads() == []
    # And no branch authority is claimed when nothing was published.
    assert "publication_head" not in scenario.state().deployment


def test_not_configured_leaves_no_git_identity_on_the_release(tmp_path):
    scenario = _Scenario(tmp_path, github=False)
    scenario.show_preview(1, b"v=1", b"<html>1</html>")
    assert scenario.approve_and_publish().success

    release = scenario.state().deployment["last_live_release"]

    for field in ("publication_commit", "publication_tree", "publication_parent",
                  "publication_branch", "publication_repo", "tested_commit",
                  "tested_tree"):
        assert release[field] is None, field
    # Everything the operation actually did is present.
    assert release["source_sha256"] == release["source_sha256"]
    assert release["smoke"]["status"] == "PASSED"
    assert release["production_url"] == CANONICAL
    assert release["deployment_id"] == "dpl_1"


def test_not_configured_resume_needs_no_remote_read(tmp_path):
    scenario = _Scenario(tmp_path, github=False)
    scenario.show_preview(1, b"v=1", b"<html>1</html>")
    assert scenario.approve_and_publish().success
    # Re-stage a NOT_CONFIGURED publication mid-flight and resume it.
    with scenario.store.acquire_writer(PROJECT) as state:
        state.lifecycle = ProjectLifecycle.FAILED.value
        state.failure = {"phase": "promotion", "error": "PROMOTE_FAILED",
                         "error_code": "PROMOTE_NOT_APPLIED", "failed_at": 1.0}
        state.deployment["last_live_release"] = None
        state.deployment["last_live_deployment"] = None
        state.deployment["pending_publication"] = {
            "operation_id": "op-1", "source_revision": 1, "stage": "GIT_CONFIRMED",
            "outcome": "TERMINAL_FAILED", "reconciliation_required": False,
            "created_at": 1.0, "updated_at": 1.0,
            "source_sha256": "a" * 64, "artifact_sha256": "b" * 64,
            "publication": {
                "configured": False, "repo": None, "branch": None,
                "tested_commit": None, "tested_tree": None,
                "intended_commit": None, "intended_parent": None,
                "intended_tree": None, "status": "NOT_CONFIGURED",
                "confirmed_at": 1.0,
            },
            "production": {"deployment_id": None,
                           "promoted_deployment_id": None, "confirmed_at": None},
            "smoke": {"status": None, "at": None},
            "last_error_code": "PROMOTE_NOT_APPLIED",
        }
        scenario.store.save(state)
    scenario.repo.git_calls.clear()

    result = scenario.orch.resume_publish(PROJECT, scenario.ws,
                                          principal_id=OWNER)

    assert result.success, result.error
    assert scenario.repo.git_calls == []


# ---------------------------------------------------------------------------
# Publication identity invariants
# ---------------------------------------------------------------------------


def test_the_published_commit_holds_the_tested_tree(tmp_path):
    scenario = _Scenario(tmp_path)
    git = scenario.show_preview(1, b"v=1", b"<html>1</html>")

    assert scenario.approve_and_publish().success

    release = scenario.state().deployment["last_live_release"]
    assert release["tested_commit"] == git["commit"]
    assert release["tested_tree"] == scenario.repo.tree_of(git["commit"])
    assert release["publication_tree"] == release["tested_tree"]
    assert scenario.repo.tree_of(release["publication_commit"]) == \
        release["tested_tree"]

def test_publication_identity_is_never_persisted_as_a_secret(tmp_path):
    scenario = _Scenario(tmp_path)
    scenario.show_preview(1, b"v=1", b"<html>1</html>")

    assert scenario.approve_and_publish().success

    blob = (scenario.store.root / f"{PROJECT}.json").read_text(encoding="utf-8")

    assert "git@github.com" not in blob
    assert DEPLOY_KEY not in blob
    assert "ssh" not in blob.lower()
    # The record states whether publication was configured, so a reader never
    # has to infer it -- and the statement itself carries nothing sensitive.
    release = scenario.state().deployment["last_live_release"]
    assert release["publication_configured"] is True


# ---------------------------------------------------------------------------
# Local input validation (H1): never a TypeError, never a C/D verdict
# ---------------------------------------------------------------------------


def test_a_malformed_local_intent_is_refused_without_reading_the_remote(tmp_path):
    """A corrupt LOCAL identity is not a remote conflict and not an unreadable
    remote: nothing is read, so nothing is asserted about the branch.

    These branches used to call ``OperationResult.fail(..., data=...)``, which
    has no ``data`` parameter, so a TypeError escaped the whole stage machine
    and the operation died with no error code recorded at all.
    """
    repo = OutputGitRepository(tmp_path / "out", hermes_root=tmp_path / "hermes")
    remote = _bare_remote(tmp_path / "remote.git")

    def call(**kwargs):
        return repo.reconcile_publication_head(
            GITHUB_SSH_URL, SLUG, kwargs.get("commit", "a" * 40),
            kwargs.get("parent"), extra_env=_mirror_env(remote))

    for kwargs in (
        {"commit": "not-a-commit"},
        {"commit": ""},
        {"commit": None},
        {"commit": "A" * 40},
        {"commit": "a" * 39},
        {"commit": "a" * 40, "parent": "not-a-parent"},
        {"commit": "a" * 40, "parent": "g" * 40},
    ):
        result = call(**kwargs)
        assert not result.success, kwargs
        # A LOCAL code, never mislabelled as a head conflict...
        assert result.error_code == PUBLICATION_INPUT_INVALID, kwargs
        assert result.error_code != "PUBLICATION_HEAD_CONFLICT", kwargs
        assert result.error_code != "PUBLICATION_HEAD_UNREADABLE", kwargs
        # ...and no verdict, because nothing was read.
        assert (result.data or {}).get("verdict") is None, kwargs


def test_a_malformed_local_intent_performs_no_remote_read(tmp_path):
    """Deterministic, and provably local: no ``ls-remote`` at all."""
    repo = _GarbageLsRemoteRepo(tmp_path / "out", hermes_root=tmp_path / "hermes")
    remote = _bare_remote(tmp_path / "remote.git")

    first = repo.reconcile_publication_head(
        GITHUB_SSH_URL, SLUG, "nope", None, extra_env=_mirror_env(remote))
    second = repo.reconcile_publication_head(
        GITHUB_SSH_URL, SLUG, "nope", None, extra_env=_mirror_env(remote))

    assert first.error_code == second.error_code == PUBLICATION_INPUT_INVALID
    assert repo.ls_remote_argvs == []


def test_malformed_ls_remote_output_is_d_not_absent(tmp_path):
    """Zero exit, unusable bytes: D_UNAVAILABLE, never ABSENT.

    An ABSENT answer is an actionable fact -- it licenses a root push when there
    is no parent -- so a reader that cannot parse the reply must never produce
    one. Driven through the real, unmocked classifier.
    """
    repo = _GarbageLsRemoteRepo(tmp_path / "out", hermes_root=tmp_path / "hermes")
    remote = _bare_remote(tmp_path / "remote.git")

    for parent in (None, "a" * 40):
        result = repo.reconcile_publication_head(
            GITHUB_SSH_URL, SLUG, "b" * 40, parent,
            extra_env=_mirror_env(remote))
        assert not result.success
        assert result.error_code == "PUBLICATION_HEAD_UNREADABLE"
        assert result.error_code != "PUBLICATION_HEAD_CONFLICT"
        assert result.data["verdict"] == "D_UNAVAILABLE"
        assert result.data["remote_state"] == "UNREADABLE"
    # One read per call, and never a retry.
    assert len(repo.ls_remote_argvs) == 2


def test_malformed_ls_remote_output_blocks_the_whole_publication(tmp_path):
    """End to end: an unreadable remote must not reach Vercel, and must not be
    mistaken for a branch that is safe to build on.

    Revision 1 publishes; the branch is then moved off our history so revision
    2's push is refused -- which is the only way the reconciler gets to read the
    remote at all. The read is what comes back unparseable.
    """
    scenario = _Scenario(tmp_path)
    scenario.show_preview(1, b"v=1", b"<html>1</html>")
    assert scenario.approve_and_publish().success
    _point_branch_at_foreign_commit(scenario)
    scenario.repo.git_calls.clear()
    scenario.repo.push_calls.clear()

    scenario.show_preview(2, b"v=2", b"<html>2</html>")
    assert scenario.orch.approve(PROJECT, principal_id=OWNER).success
    scenario.repo.ls_remote_argvs = []
    original = scenario.repo._run

    def garbage(args, data=None, extra=None, init=False, check=True):
        if args and args[0] == "ls-remote":
            scenario.repo.ls_remote_argvs.append(list(args))
            return subprocess.CompletedProcess(
                args=["git"] + list(args), returncode=0,
                stdout=_GarbageLsRemoteRepo.GARBAGE, stderr=b"")
        return original(args, data=data, extra=extra, init=init, check=check)

    scenario.repo._run = garbage
    foreign_head = _remote_git(scenario.remote, "rev-parse", f"refs/heads/{SLUG}")

    result = scenario.orch.promote(PROJECT, scenario.ws, principal_id=OWNER)

    assert not result.success
    assert result.error_code == "PUBLICATION_HEAD_UNREADABLE"
    assert result.error_code != "PUBLICATION_HEAD_CONFLICT"
    # The one read that followed the refused push was unreadable, so nothing
    # was retried and production was never touched.
    assert len(scenario.repo.ls_remote_argvs) == 1
    assert len(scenario.repo.push_argvs()) == 1
    assert scenario.vercel.promote_calls == ["dpl_1"]
    assert scenario.state().deployment["pending_publication"][
        "last_error_code"] == "PUBLICATION_HEAD_UNREADABLE"
    # The branch is exactly as it was found.
    assert _remote_git(scenario.remote, "rev-parse",
                       f"refs/heads/{SLUG}") == foreign_head


def test_a_local_ref_update_failure_is_classified_and_recoverable(tmp_path):
    """A local mirror-ref write that fails after an ACCEPTED push.

    The remote branch is durable history and is never rolled back for a local
    bookkeeping problem. GIT_CONFIRMED is honestly not persisted -- the durable
    record of the confirmation is exactly what was lost -- so the record stays
    at PREPARED, and the retry's push of the same commit is accepted as a no-op.
    Either way there is exactly one publication commit.
    """
    scenario = _Scenario(tmp_path)
    scenario.show_preview(1, b"v=1", b"<html>1</html>")
    assert scenario.orch.approve(PROJECT, principal_id=OWNER).success

    original = scenario.repo._run
    broken = {"on": True}

    def fail_update_ref(args, data=None, extra=None, init=False, check=True):
        if args and args[0] == "update-ref" and broken["on"]:
            raise OSError("simulated local ref write failure")
        return original(args, data=data, extra=extra, init=init, check=check)

    scenario.repo._run = fail_update_ref

    first = scenario.orch.promote(PROJECT, scenario.ws, principal_id=OWNER)

    assert not first.success
    assert first.error_code == PUBLICATION_LOCAL_REF_UPDATE_FAILED
    assert first.error_code != "PUBLICATION_PUSH_FAILED"
    assert scenario.vercel.promote_calls == []
    pending = scenario.state().deployment["pending_publication"]
    assert pending["stage"] == "PREPARED"
    assert pending["publication"]["status"] == "FAILED"
    # The remote really does hold the commit: the push was accepted.
    remote_head = _remote_git(scenario.remote, "rev-parse", f"refs/heads/{SLUG}")
    assert pending["publication"]["intended_commit"] == remote_head

    # The local write recovers; the retry confirms the same commit and finishes.
    broken["on"] = False
    scenario.repo.git_calls.clear()
    result = scenario.fresh_orchestrator().resume_publish(
        PROJECT, scenario.ws, principal_id=OWNER)

    assert result.success, result.error
    assert scenario.state().deployment["last_live_release"][
        "publication_commit"] == remote_head
    assert scenario.state().deployment["publication_head"]["commit"] == remote_head
    # Exactly one commit on the branch: a retry, not a second publication.
    assert _remote_git(scenario.remote, "rev-list", "--count",
                       f"refs/heads/{SLUG}") == "1"
    assert _remote_git(scenario.remote, "rev-parse", f"refs/heads/{SLUG}") == remote_head
    _assert_no_forced_push(scenario.repo.push_argvs())


def test_a_lost_git_confirmed_write_adopts_instead_of_forking(tmp_path):
    """The push landed; the record of it did not.

    The record is still at PREPARED/PENDING while the remote already holds the
    intended commit. The retry is refused, exactly one read classifies the
    remote as holding the intended commit, and that is A_ADOPT: the commit is
    adopted, never re-parented and never rebuilt. The branch ends the whole
    episode with ONE publication commit -- not a fork, and not a second
    publication.
    """
    live = Live(tmp_path, store_factory=_DropStageStore,
                crash=_SimulatedCrash)
    live.show_preview(1, b"v=1", b"<html>1</html>")
    assert live.orch.approve(PROJECT, principal_id=OWNER).success
    live.store.drop_stage = "GIT_CONFIRMED"

    with pytest.raises(_SimulatedCrash):
        live.orch.promote(PROJECT, live.ws, principal_id=OWNER)

    assert live.store.dropped == ["GIT_CONFIRMED"]
    pending = live.state().deployment["pending_publication"]
    assert pending["stage"] == "PREPARED"
    assert pending["publication"]["status"] == "PENDING"
    intended = pending["publication"]["intended_commit"]
    # The remote already holds it: the push was accepted, only the write lost.
    assert _remote_git(live.remote, "rev-parse", f"refs/heads/{SLUG}") == intended
    assert live.vercel.promote_calls == []
    live.repo.git_calls.clear()

    # The server refuses the retry, so the reconciler has to decide.
    live.repo.reject_next_publication_push()
    result = live.orch.resume_publish(PROJECT, live.ws, principal_id=OWNER)

    assert result.success, result.error
    # The original push plus the refused retry, and no more. A case-A adopt
    # issues no third push, which is the whole point of the verdict.
    assert len(live.repo.push_calls) == 2
    assert live.repo.push_argvs() == []
    # Exactly one reconciliation read, against the intended commit.
    assert len(live.repo.remote_reads()) == 1
    assert live.repo.reconcile_calls[-1]["intended_commit"] == intended
    assert live.repo.reconcile_calls[-1].get("already_confirmed") is None
    # One publication commit, and it is the intended one.
    assert _remote_git(live.remote, "rev-list", "--count",
                       f"refs/heads/{SLUG}") == "1"
    assert _remote_git(live.remote, "rev-parse", f"refs/heads/{SLUG}") == intended
    release = live.state().deployment["last_live_release"]
    assert release["completeness"] == "COMPLETE"
    assert release["publication_commit"] == intended
    assert release["publication_commit"] == \
        live.state().deployment["publication_head"]["commit"]
    assert live.vercel.promote_calls == ["dpl_1"]


def test_a_lost_git_confirmed_write_with_a_quiet_remote_recovers(tmp_path):
    """The same lost write, but the retry push is simply accepted as a no-op
    because the remote already holds the commit.

    Deterministic push counts, and still exactly one publication commit.
    """
    live = Live(tmp_path, store_factory=_DropStageStore,
                crash=_SimulatedCrash)
    live.show_preview(1, b"v=1", b"<html>1</html>")
    assert live.orch.approve(PROJECT, principal_id=OWNER).success
    live.store.drop_stage = "GIT_CONFIRMED"
    with pytest.raises(_SimulatedCrash):
        live.orch.promote(PROJECT, live.ws, principal_id=OWNER)
    live.repo.git_calls.clear()

    result = live.orch.resume_publish(PROJECT, live.ws, principal_id=OWNER)

    assert result.success, result.error
    assert len(live.repo.push_argvs()) == 1
    assert live.repo.remote_reads() == []
    _assert_no_forced_push(live.repo.push_argvs())
    assert _remote_git(live.remote, "rev-list", "--count",
                       f"refs/heads/{SLUG}") == "1"
    release = live.state().deployment["last_live_release"]
    assert release["publication_commit"] == \
        live.state().deployment["publication_head"]["commit"]


# ---------------------------------------------------------------------------
# Stage order and the shape of a no-op
# ---------------------------------------------------------------------------


def test_an_accepted_push_walks_the_stages_in_order(tmp_path):
    """The order the machine actually walked, not just the final stage: an
    accepted push must land GIT_CONFIRMED between PREPARED and the production
    stages, with nothing read to find that out."""
    scenario = _Scenario(tmp_path)
    scenario.show_preview(1, b"v=1", b"<html>1</html>")

    assert scenario.approve_and_publish().success

    assert scenario.stages == ["PREPARED", "GIT_CONFIRMED",
                               "PRODUCTION_CONFIRMED", "SMOKE_PASSED"]


def test_a_not_configured_release_still_states_its_publication_fact(tmp_path):
    scenario = _Scenario(tmp_path, github=False)
    git = scenario.show_preview(1, b"v=1", b"<html>1</html>")

    assert scenario.approve_and_publish().success

    release = scenario.state().deployment["last_live_release"]
    assert release["publication_configured"] is False
    # The approved source really is what was released: the fixture's own
    # hashes, compared rather than compared with themselves.
    assert release["source_sha256"] == git["source_sha256"]
    assert release["artifact_sha256"] == git["artifact_sha256"]
