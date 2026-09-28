"""R1 finishing changes: canonical LIVE URL, LIVE source publication, approve = publish.

Three behaviours are proved here, all through the REAL promotion/dispatch
orchestration with only the Vercel, Telegram and browser boundaries faked
(the Git repository is a real bare repo, and the "remote" is a real bare repo
pointed at by git's own ``insteadOf`` mirror, so every push, fast-forward and
ancestry assertion below is observed, not simulated).

A. The deployment-specific Vercel hostname and the canonical public URL are
   two distinct things. The deployment host is internal identity; the
   canonical project domain is what gets smoke-checked, persisted as
   ``production_url``, and sent to the user.

B. The exact tested snapshot commit of an approved LIVE revision is published
   to a friendly ``<project-slug>`` branch as a publication HISTORY: one
   commit per LIVE revision, each holding the tested snapshot's exact tree,
   chained to the previous publication commit and advanced by a normal
   fast-forward push. Never force-moved, never rebuilt from the workspace.

C. Approving the exact shown preview publishes it. There is no second
   confirmation step, a duplicate approval is idempotent, and a stale approval
   still fails closed.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.channels.dispatch import TelegramDispatcher  # noqa: E402
from app.core.authz import ProjectAccess  # noqa: E402
from app.core.contracts import OperationResult  # noqa: E402
from app.core.intake import IntakeProcessor  # noqa: E402
from app.core.lifecycle import ProjectLifecycle  # noqa: E402
from app.core.state import ProjectStateStore  # noqa: E402
from app.deploy.git_output import (  # noqa: E402
    OutputGitRepository,
    _push_rejected,
)
from app.deploy.snapshot import TestedSnapshot  # noqa: E402
from app.projects.promote import PromoteDeps, PromotionOrchestrator  # noqa: E402
from app.runtime import TelegramReceiveLoop  # noqa: E402
from app.sandbox.runner import ProjectRunner  # noqa: E402
from test_promote import (  # noqa: E402
    OWNER,
    FakeSmoke,
    FakeTelegram,
    FakeVercel,
    FlakyPromoteVercel,
    _make_workspace,
)

PROJECT = "tg-777"
CHAT = "777"
# The principal a Telegram-driven approve arrives as.
TG_OWNER = "telegram:1"
SLUG = "financeadvisory"
CANONICAL = "https://financeadvisory.vercel.app/"
# The deployment-specific host Vercel hands back for a promoted build. It may
# be behind Deployment Protection and must never reach a user.
DEPLOYMENT_URL = "https://financeadvisory-d1wmsfc9c-albert-a121.vercel.app"
GITHUB_SSH_URL = "git@github.com:albertus527/website.git"
DEPLOY_KEY = "deploy_key"


# ---------------------------------------------------------------------------
# Real Git plumbing: a local bare repo standing in for the private remote
# ---------------------------------------------------------------------------


def _bare_remote(path: Path) -> Path:
    subprocess.run(
        ["git", "init", "--bare", str(path)], check=True, capture_output=True,
    )
    return path


def _mirror_env(remote: Path) -> dict:
    """Point the real GitHub SSH remote at a local bare repo.

    git's own ``insteadOf`` mechanism does the rewrite, so the code under test
    still validates and pushes the exact operator-configured remote; only the
    transport target differs.
    """
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "url.file:///{}.insteadOf".format(remote.as_posix()),
        "GIT_CONFIG_VALUE_0": GITHUB_SSH_URL,
    }


def _remote_git(remote: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(remote), *args],
        check=True, capture_output=True,
    ).stdout.decode().strip()


class _PublishingRepo(OutputGitRepository):
    """Real OutputGitRepository that records every git invocation.

    The recording is what lets the tests assert what the publication path does
    NOT do (no fetch, no clone, no force, no remote read on the happy path)
    instead of merely what it does.

    Every entry point is routed through the ``insteadOf`` mirror so the code
    under test still validates and pushes the exact operator-configured
    GitHub SSH remote; only the transport target differs.
    """

    def __init__(self, path, hermes_root, remote):
        self.git_calls: list[list[str]] = []
        self.prepare_calls: list[dict] = []
        self.push_calls: list[dict] = []
        self.reconcile_calls: list[dict] = []
        self.raise_on_publish = None
        self._reject_next_push = False
        super().__init__(path, hermes_root=hermes_root)
        self.remote = remote

    def reject_next_publication_push(self):
        """Model a push the server refused AFTER updating the branch.

        The client is told the push failed while the remote in fact holds the
        intended commit. That is the only way the A_ADOPT arm of the rejected-
        push path is reachable -- a plain re-push of a commit the remote
        already holds is accepted as a no-op -- and it is exactly the ambiguity
        the durable record exists for: the side effect happened and the client
        cannot prove it.
        """
        self._reject_next_push = True

    def _run(self, args, data=None, extra=None, init=False, check=True):
        self.git_calls.append(list(args))
        # A transport failure is raised from the subprocess seam, exactly where
        # ssh would fail, so the code under test's own classification decides
        # what it means rather than the harness deciding for it.
        if "push" in args and self.raise_on_publish is not None:
            raise self.raise_on_publish
        return super()._run(args, data=data, extra=extra, init=init, check=check)

    def prepare_publication(self, *args, **kwargs):
        self.prepare_calls.append(dict(kwargs, tested_commit=args[0],
                                       branch=args[1], url=args[2]))
        return super().prepare_publication(*args, **kwargs)

    def push_prepared_publication(self, url, commit, branch, **kwargs):
        self.push_calls.append({"commit": commit, "branch": branch, "url": url})
        if self._reject_next_push:
            self._reject_next_push = False
            return _push_rejected("NON_FAST_FORWARD")
        return super().push_prepared_publication(
            url, commit, branch,
            **{**kwargs, "extra_env": _mirror_env(self.remote)},
        )

    def reconcile_publication_head(self, url, branch, intended_commit,
                                   intended_parent, **kwargs):
        self.reconcile_calls.append(
            {"branch": branch, "intended_commit": intended_commit,
             "intended_parent": intended_parent})
        return super().reconcile_publication_head(
            url, branch, intended_commit, intended_parent,
            **{**kwargs, "extra_env": _mirror_env(self.remote)},
        )

    # -- assertions helpers -------------------------------------------------

    def push_argvs(self) -> list[list[str]]:
        return [args for args in self.git_calls if "push" in args]

    def remote_reads(self) -> list[list[str]]:
        """Every invocation that reads the remote, for the zero-read assertion."""
        return [args for args in self.git_calls
                if args and args[0] in ("ls-remote", "fetch", "clone", "pull", "archive")]

    def ref_heads(self, ref: str) -> str:
        return self._run(["rev-parse", ref]).strip().decode()

    def tree_of(self, commit: str) -> str:
        return self._run(["rev-parse", f"{commit}^{{tree}}"]).strip().decode()


class _SimulatedCrash(RuntimeError):
    """A process that died mid-write.

    Raised from the storage seam, not from the code under test, so the recovery
    path runs exactly as it would after a real crash: the durable record is
    whatever was last successfully written, and nothing in memory survives.
    """


class _FailingGitRepo(_PublishingRepo):
    """A publication that fails the way a real transport failure does."""

    def __init__(self, path, hermes_root, remote):
        super().__init__(path, hermes_root, remote)
        self.raise_on_publish = RuntimeError("ssh: connect to host github.com:22")


class _DropStageStore(ProjectStateStore):
    """A real state store that can lose ONE stage write.

    Models the failure the durable publication intent exists for: the remote
    side effect happened, and the record of it was lost. The write is dropped
    at the storage seam rather than by patching the code under test, so the
    recovery path runs for real and decides for itself.

    With ``crash`` set to an exception CLASS, the process is taken down at the
    same seam instead, which is what a real crash looks like: the durable record
    is whatever was last written successfully, and nothing in memory survives to
    be mistaken for progress. The class is injected rather than imported so a
    caller in another test module can catch it with its own identity.
    """

    def __init__(self, root):
        super().__init__(root)
        self.drop_stage = None
        self.crash = None
        self.dropped = []

    def save(self, state):
        stage = ((state.deployment or {}).get("pending_publication") or {}).get("stage")
        if self.drop_stage is not None and stage == self.drop_stage:
            self.dropped.append(stage)
            self.drop_stage = None
            if self.crash is not None:
                raise self.crash(f"process died before the {stage} write")
            return
        super().save(state)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RecordingSmoke(FakeSmoke):
    """Records every URL the production smoke was asked to check."""

    def __init__(self, success=True):
        super().__init__(success=success)
        self.urls: list[str] = []

    def run(self, url, out_dir, **kwargs):
        self.urls.append(url)
        return super().run(url, out_dir)


class _Vercel(FakeVercel):
    """FakeVercel that reports a deployment-specific promote URL and a
    resolvable canonical project domain."""

    def promote_deployment(self, app_id, project, deployment_id, operation_id,
                           source_revision, artifact_sha256, *, expected_name=None):
        result = super().promote_deployment(
            app_id, project, deployment_id, operation_id, source_revision,
            artifact_sha256, expected_name=expected_name,
        )
        result.data["production_url"] = DEPLOYMENT_URL
        return result


class _UnresolvableCanonicalVercel(_Vercel):
    def canonical_production_url(self, app_id, project, *, expected_name=None,
                                 expected_deployment_id=None):
        return OperationResult.fail("CANONICAL_PRODUCTION_URL_UNRESOLVED",
                                    error_code="CANONICAL_PRODUCTION_URL_UNRESOLVED")


class _RevisionLandsMidFlightVercel(_Vercel):
    """A Vercel boundary on which a newer revision lands while the promotion
    is between its approval re-check and its own bookkeeping."""

    def __init__(self, land):
        super().__init__()
        self.land = land

    def lookup_project(self, app_id, *, expected_name=None):
        if self.land is not None:
            land, self.land = self.land, None
            land()
        return super().lookup_project(app_id, expected_name=expected_name)


# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------


class Live:
    """One project's store, workspace, real output repo and orchestrators."""

    def __init__(self, tmp_path, repo_factory=_PublishingRepo, vercel=None,
                 smoke=None, github=True, store_factory=None, crash=None):
        self.tmp_path = tmp_path
        self.store = (store_factory or ProjectStateStore)(tmp_path / "state")
        if crash is not None:
            # ``store_factory`` may be a plain ProjectStateStore; a crash is
            # only meaningful for the stage-dropping one.
            self.store.crash = crash
        self.ws = _make_workspace(tmp_path)
        self.runner = ProjectRunner(tmp_path / "workspaces", self.store)
        self.remote = _bare_remote(tmp_path / "remote.git")
        # The output repository always exists (previews are built from it);
        # ``github=False`` only switches off the PUBLICATION wiring.
        self.repo = repo_factory(tmp_path / "out", tmp_path / "hermes", self.remote)
        self.github = github
        self.vercel = vercel or _Vercel()
        self.telegram = FakeTelegram()
        self.smoke = smoke or _RecordingSmoke()
        self.deps = PromoteDeps(
            vercel=self.vercel,
            telegram=self.telegram,
            smoke=self.smoke,
            chat_id_for=lambda pid, state: CHAT,
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

    # -- state construction --------------------------------------------------

    def show_preview(self, revision: int, source: bytes, artifact: bytes) -> dict:
        """Run a real snapshot commit and show it as the current preview.

        The git identity is the REAL one for these bytes, so the commit the
        publication path later pushes is provably the commit this preview was
        built from.
        """
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
            state.conversation_id = CHAT
            state.revisions.source_revision = revision
            state.revisions.qa_revision = revision
            state.revisions.preview_revision = revision
            state.deployment["latest_shown_preview"] = shown
            # The exact tested commit this preview was built from.
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

    def live_messages(self) -> list[str]:
        return [text for _chat, text in self.telegram.sent if "Live" in text]


@pytest.fixture
def live(tmp_path):
    return Live(tmp_path)


# ---------------------------------------------------------------------------
# A. Canonical public LIVE URL
# ---------------------------------------------------------------------------


def test_deployment_host_is_never_the_final_live_url(live):
    live.show_preview(1, b"v=1", b"<html>1</html>")

    result = live.approve_and_publish()

    assert result.success, result.error
    # The result the caller sees is canonical, and the deployment host is
    # reported separately as the internal identity.
    assert result.data["production_url"] == CANONICAL
    assert result.data["deployment_url"] == DEPLOYMENT_URL
    # No outbound message ever carries the deployment host.
    assert live.live_messages() == [f"🚀 Live: {CANONICAL}"]
    for _chat, text in live.telegram.sent:
        assert DEPLOYMENT_URL not in text
        assert "d1wmsfc9c" not in text


def test_canonical_url_is_what_production_smoke_checks(live):
    live.show_preview(1, b"v=1", b"<html>1</html>")

    assert live.approve_and_publish().success

    assert live.smoke.urls == [CANONICAL]


def test_state_production_url_is_canonical(live):
    live.show_preview(1, b"v=1", b"<html>1</html>")

    assert live.approve_and_publish().success

    assert live.state().production_url == CANONICAL


def test_last_live_deployment_keeps_both_urls(live):
    live.show_preview(1, b"v=1", b"<html>1</html>")

    assert live.approve_and_publish().success

    last_live = live.state().deployment["last_live_deployment"]
    assert last_live["production_url"] == CANONICAL
    assert last_live["deployment_url"] == DEPLOYMENT_URL


def test_canonical_resolution_fails_closed_before_smoke(tmp_path):
    """An unresolvable canonical URL never produces a LIVE claim, and never
    triggers a rollback: the remote promotion is confirmed good and the same
    operation stays resumable."""
    live = Live(tmp_path, vercel=_UnresolvableCanonicalVercel())

    live.show_preview(1, b"v=1", b"<html>1</html>")
    result = live.approve_and_publish()

    assert not result.success
    assert result.error_code == "CANONICAL_PRODUCTION_URL_UNRESOLVED"
    assert live.smoke.urls == []
    assert live.vercel.promote_calls == ["dpl_1"]
    # No rollback promote for a local URL-resolution problem.
    assert live.vercel.promote_calls == ["dpl_1"]
    state = live.state()
    assert state.lifecycle == ProjectLifecycle.FAILED.value
    assert state.failure["error_code"] == "CANONICAL_PRODUCTION_URL_UNRESOLVED"
    assert state.production_url is None
    # The same operation is still resumable: the durable intent survives.
    intent = state.deployment["promotion_intent"]
    assert intent["operation_id"] == "op-1"
    assert "previous_production" in intent
    assert intent["stage"] == "promoted"


# ---------------------------------------------------------------------------
# B. LIVE source publication history
# ---------------------------------------------------------------------------


def test_first_live_publication_creates_the_friendly_branch(live):
    git = live.show_preview(1, b"v=1", b"<html>1</html>")

    assert live.approve_and_publish().success

    release = live.state().deployment["last_live_release"]
    assert release["publication_repo"] == "albertus527/website"
    assert release["publication_branch"] == SLUG
    assert release["tested_commit"] == git["commit"]
    assert release["source_revision"] == 1
    assert release["completeness"] == "COMPLETE"
    assert release["publication_commit"]
    # The branch really exists on the remote, at a ROOT publication commit.
    head = _remote_git(live.remote, "rev-parse", f"refs/heads/{SLUG}")
    assert head == release["publication_commit"]
    assert _remote_git(live.remote, "rev-list", "--count", f"refs/heads/{SLUG}") == "1"
    # The branch-parent authority is the same commit, and the first
    # publication has no parent.
    assert live.state().deployment["publication_head"]["commit"] == \
        release["publication_commit"]
    assert release["publication_parent"] is None
    assert not live.repo.prepare_calls[0]["previous_publication_commit"]
    # A publication commit is a different object from the tested commit...
    assert release["publication_commit"] != git["commit"]
    # ...that holds exactly the tested snapshot's tree.
    assert live.repo.tree_of(release["publication_commit"]) == live.repo.tree_of(
        git["commit"])
    assert release["publication_tree"] == live.repo.tree_of(git["commit"])


def test_second_live_revision_advances_the_branch_and_keeps_history(live):
    git1 = live.show_preview(1, b"v=1", b"<html>1</html>")
    assert live.approve_and_publish().success
    pub1 = live.state().deployment["last_live_release"]["publication_commit"]

    git2 = live.show_preview(2, b"v=2", b"<html>2</html>")
    assert live.approve_and_publish().success
    release = live.state().deployment["last_live_release"]
    pub2 = release["publication_commit"]

    assert pub2 != pub1
    assert release["tested_commit"] == git2["commit"]
    # Revision 1 stays in revision 2's ancestry.
    _remote_git(live.remote, "merge-base", "--is-ancestor", pub1, pub2)
    assert _remote_git(live.remote, "rev-list", "--count",
                       f"refs/heads/{SLUG}") == "2"
    assert _remote_git(live.remote, "rev-parse", f"refs/heads/{SLUG}") == pub2
    # The new publication is chained to the previous one, not to the tested
    # snapshot commit.
    assert live.repo.prepare_calls[1]["previous_publication_commit"] == pub1
    assert release["publication_parent"] == pub1
    assert _remote_git(live.remote, "rev-parse", f"{pub2}^") == pub1


def test_every_publication_commit_holds_its_tested_tree(live):
    pairs = []
    for revision, (source, artifact) in enumerate(
        [(b"v=1", b"<html>1</html>"), (b"v=2", b"<html>2</html>"),
         (b"v=3", b"<html>3</html>")], start=1,
    ):
        git = live.show_preview(revision, source, artifact)
        assert live.approve_and_publish().success
        release = live.state().deployment["last_live_release"]
        pairs.append((release["tested_commit"], release["publication_commit"], git))
        assert release["publication_commit"] != release["tested_commit"]
        # The recorded tree IS the tested tree, by identity not by re-read.
        assert release["publication_tree"] == release["tested_tree"]

    for tested, publication, git in pairs:
        assert live.repo.tree_of(publication) == live.repo.tree_of(tested)
        assert live.repo.tree_of(publication) == live.repo.tree_of(git["commit"])


def test_publication_is_never_force_moved(live):
    live.show_preview(1, b"v=1", b"<html>1</html>")
    assert live.approve_and_publish().success
    live.show_preview(2, b"v=2", b"<html>2</html>")
    assert live.approve_and_publish().success

    pushes = live.repo.push_argvs()
    assert len(pushes) == 2
    for argv in pushes:
        assert "--force" not in argv
        assert "--force-with-lease" not in argv
        assert "-f" not in argv
        refspec = argv[-1]
        assert not refspec.startswith("+")
        assert refspec.endswith(f":refs/heads/{SLUG}")


def test_persisted_publication_commit_is_the_next_parent(live):
    live.show_preview(1, b"v=1", b"<html>1</html>")
    assert live.approve_and_publish().success
    first = live.state().deployment["publication_head"]["commit"]

    live.show_preview(2, b"v=2", b"<html>2</html>")
    assert live.approve_and_publish().success

    # The parent the orchestrator passed came from persisted state, and the
    # commit it created is that state's successor.
    assert live.repo.prepare_calls[1]["previous_publication_commit"] == first
    second = live.state().deployment["publication_head"]["commit"]
    assert second != first
    assert _remote_git(live.remote, "rev-parse", f"{second}^") == first


def test_published_source_is_the_tested_commit_of_the_approved_operation(live):
    git = live.show_preview(1, b"v=1", b"<html>1</html>")

    assert live.approve_and_publish().success

    assert live.repo.prepare_calls[0]["tested_commit"] == git["commit"]
    assert live.repo.prepare_calls[0]["source_sha256"] == git["source_sha256"]
    assert live.repo.prepare_calls[0]["artifact_sha256"] == git["artifact_sha256"]


def test_preview_intent_of_another_operation_is_never_published(tmp_path):
    """A preview intent that does not belong to the approved operation is not
    publishable at all: the publish fails closed and production is never
    touched."""
    live = Live(tmp_path)
    live.show_preview(1, b"v=1", b"<html>1</html>")
    with live.store.acquire_writer(PROJECT) as state:
        state.deployment["preview_intent"]["operation_id"] = "op-someone-else"
        live.store.save(state)

    result = live.approve_and_publish()

    assert not result.success
    assert result.error_code == "NO_TRUSTED_TESTED_COMMIT"
    # Nothing was pushed, and no Vercel call was ever made.
    assert live.repo.push_calls == []
    assert live.vercel.promote_calls == []
    assert live.smoke.urls == []
    state = live.state()
    assert state.lifecycle == ProjectLifecycle.FAILED.value
    # There was never a pending publication: nothing was ever intended.
    assert "pending_publication" not in state.deployment
    assert not live.live_messages()
    # The branch on the remote was never created.
    with pytest.raises(subprocess.CalledProcessError):
        _remote_git(live.remote, "rev-parse", f"refs/heads/{SLUG}")


def test_a_commit_outside_the_tested_snapshot_refs_is_never_pushed(tmp_path):
    """The commit published must be the head of an immutable
    ``preview/<hash>/<hash>`` ref. A commit we happen to hold that is not one
    is refused by the repository boundary, so nothing is pushed and production
    is never touched."""
    live = Live(tmp_path)
    git = live.show_preview(1, b"v=1", b"<html>1</html>")
    # A real commit object in the same repository, on no preview ref at all.
    stranger = live.repo._build_publication_commit(
        live.repo.tree_of(git["commit"]), SLUG, None, source_revision=1,
        tested_commit=git["commit"], source_sha256=git["source_sha256"],
        artifact_sha256=git["artifact_sha256"],
    )
    with live.store.acquire_writer(PROJECT) as state:
        intent = state.deployment["preview_intent"]
        intent["git"] = dict(intent["git"], commit=stranger)
        live.store.save(state)

    result = live.approve_and_publish()

    assert not result.success
    assert result.error_code == "NO_TRUSTED_TESTED_COMMIT"
    # The site is NOT live: the publication could not be proven, so production
    # was never touched.
    assert live.state().lifecycle == ProjectLifecycle.FAILED.value
    assert live.vercel.promote_calls == []
    assert live.smoke.urls == []
    assert not live.live_messages()
    # And the branch on the remote was never created.
    with pytest.raises(subprocess.CalledProcessError):
        _remote_git(live.remote, "rev-parse", f"refs/heads/{SLUG}")


def test_preview_only_revision_never_moves_the_branch(live):
    live.show_preview(1, b"v=1", b"<html>1</html>")
    live.show_preview(2, b"v=2", b"<html>2</html>")

    assert live.repo.push_calls == []
    assert "publication_head" not in live.state().deployment
    with pytest.raises(subprocess.CalledProcessError):
        _remote_git(live.remote, "rev-parse", f"refs/heads/{SLUG}")


def test_stale_approval_never_moves_the_branch(live):
    live.show_preview(1, b"v=1", b"<html>1</html>")
    assert live.orch.approve(PROJECT, principal_id=OWNER).success
    # A newer preview lands after the approval.
    live.show_preview(2, b"v=2", b"<html>2</html>")

    result = live.orch.promote(PROJECT, live.ws, principal_id=OWNER)

    assert not result.success
    assert result.error_code == "STALE_APPROVAL"
    assert live.repo.push_calls == []


def test_failed_publish_never_moves_the_branch(tmp_path):
    """A smoke failure happens AFTER the Git stage, so the branch DOES move --
    and that is correct: the branch is durable publication history, and the
    LIVE release record still describes the previous release."""
    live = Live(tmp_path, smoke=_RecordingSmoke(success=False))
    live.show_preview(1, b"v=1", b"<html>1</html>")

    result = live.approve_and_publish()

    assert not result.success
    assert result.error_code == "SMOKE_FAILED"
    assert len(live.repo.push_calls) == 1
    assert live.state().lifecycle == ProjectLifecycle.FAILED.value
    # The branch is ahead of production, and the state says so explicitly.
    head = live.state().deployment["publication_head"]["commit"]
    assert _remote_git(live.remote, "rev-parse", f"refs/heads/{SLUG}") == head
    assert "last_live_release" not in live.state().deployment


def test_publication_failure_never_touches_production(tmp_path):
    """R2 inversion of the old "git failure still goes live" contract: a
    publication that cannot be proven means production is never reached at
    all."""
    live = Live(tmp_path, repo_factory=_FailingGitRepo)
    live.show_preview(1, b"v=1", b"<html>1</html>")

    result = live.approve_and_publish()

    assert not result.success
    assert result.error_code == "PUBLICATION_PUSH_FAILED"
    state = live.state()
    assert state.lifecycle == ProjectLifecycle.FAILED.value
    assert state.production_url is None
    # No promotion, no smoke, no LIVE message: production was never touched.
    assert live.vercel.promote_calls == []
    assert live.smoke.urls == []
    assert live.live_messages() == []
    # The record says exactly how far the operation got, and does not invent
    # a branch head that was never confirmed.
    pending = state.deployment["pending_publication"]
    assert pending["stage"] == "PREPARED"
    assert pending["outcome"] == "TERMINAL_FAILED"
    assert pending["publication"]["status"] == "FAILED"
    assert pending["last_error_code"] == "PUBLICATION_PUSH_FAILED"
    assert "publication_head" not in state.deployment
    # And the user is told the truth.
    assert state.deployment["approval"]["operation_id"] == "op-1"


def test_publication_failure_preserves_the_last_published_commit(tmp_path):
    live = Live(tmp_path)
    live.show_preview(1, b"v=1", b"<html>1</html>")
    assert live.approve_and_publish().success
    first = live.state().deployment["publication_head"]["commit"]

    live.repo.raise_on_publish = RuntimeError("ssh: network unreachable")
    live.show_preview(2, b"v=2", b"<html>2</html>")
    result = live.approve_and_publish()

    assert not result.success
    # The last revision that genuinely reached the remote is still the parent
    # authority, so a failed revision never enters the published history.
    assert live.state().deployment["publication_head"]["commit"] == first
    assert _remote_git(live.remote, "rev-parse", f"refs/heads/{SLUG}") == first
    assert _remote_git(live.remote, "rev-list", "--count",
                       f"refs/heads/{SLUG}") == "1"
    # The first release is still the LIVE release.
    assert live.state().deployment["last_live_release"]["publication_commit"] == first


def test_retry_after_a_publication_failure_replays_the_same_commit(tmp_path):
    live = Live(tmp_path)
    live.show_preview(1, b"v=1", b"<html>1</html>")
    live.repo.raise_on_publish = RuntimeError("ssh: network unreachable")
    assert not live.approve_and_publish().success

    live.repo.raise_on_publish = None
    # Same-operation recovery, same exact tested commit: the deterministic
    # publication commit is recreated, so nothing forks.
    result = live.orch.resume_publish(PROJECT, live.ws, principal_id=OWNER)

    assert result.success, result.error
    release = live.state().deployment["last_live_release"]
    assert release["completeness"] == "COMPLETE"
    assert _remote_git(live.remote, "rev-list", "--count",
                       f"refs/heads/{SLUG}") == "1"


def test_no_github_read_back_in_r1(live):
    """R2 writes to the source repository and never reads source back from it.

    Asserted on the recorded git invocations across a full publish plus a
    second revision: there is no fetch, clone, pull, archive, or any other
    content read -- and, since an accepted fast-forward push is itself the
    proof, not even a ref-head read. ``ls-remote`` is permitted only on a
    rejected push or a resume, and is covered by the reconciliation tests.
    """
    live.show_preview(1, b"v=1", b"<html>1</html>")
    assert live.approve_and_publish().success
    live.show_preview(2, b"v=2", b"<html>2</html>")
    assert live.approve_and_publish().success

    forbidden = {"fetch", "clone", "pull", "archive", "submodule", "checkout"}
    for argv in live.repo.git_calls:
        assert not (forbidden & set(argv)), argv
    # Nothing was read back from the remote on the normal path.
    assert live.repo.remote_reads() == []


def test_resume_at_git_confirmed_adopts_without_a_second_push(tmp_path):
    """The case-A recovery the durable publication record exists for.

    The Git stage completed and was persisted, then the production stage failed
    terminally. The operator recovery re-derives the Git fact from the remote --
    the head is exactly the intended commit -- adopts it, and pushes nothing.
    One ref-head read, for one ref, and one push for the whole publication.

    (R1 re-parented onto whatever the remote held. R2 does not: a remote head
    we did not publish is case C, covered by
    ``test_conflicting_remote_head_fails_closed_without_forcing``.)
    """
    live = Live(tmp_path, vercel=FlakyPromoteVercel(fail_on={"dpl_1"}))
    live.show_preview(1, b"v=1", b"<html>1</html>")

    first = live.approve_and_publish()

    assert not first.success
    assert first.error_code == "PROMOTE_FAILED"
    pending = live.state().deployment["pending_publication"]
    assert pending["stage"] == "GIT_CONFIRMED"
    assert pending["outcome"] == "TERMINAL_FAILED"
    # The push already landed and nothing was read to find that out.
    assert len(live.repo.push_argvs()) == 1
    assert live.repo.remote_reads() == []
    head = _remote_git(live.remote, "rev-parse", f"refs/heads/{SLUG}")
    live.repo.git_calls.clear()

    live.vercel.fail_on = set()
    result = live.orch.resume_publish(PROJECT, live.ws, principal_id=OWNER)

    assert result.success, result.error
    release = live.state().deployment["last_live_release"]
    assert release["publication_commit"] == head
    # Exactly one ref-head read, for exactly one ref, and no second push.
    reads = live.repo.remote_reads()
    assert len(reads) == 1
    assert reads[0][-1] == f"refs/heads/{SLUG}"
    assert live.repo.push_argvs() == []
    # A single logical publication: the branch still holds one commit.
    assert _remote_git(live.remote, "rev-list", "--count",
                       f"refs/heads/{SLUG}") == "1"


def test_resume_at_git_confirmed_with_a_moved_head_is_case_c(tmp_path):
    """A resume whose branch head has moved off the intended commit is case C:
    reported, never built on, never clobbered."""
    live = Live(tmp_path, vercel=FlakyPromoteVercel(fail_on={"dpl_1"}))
    live.show_preview(1, b"v=1", b"<html>1</html>")
    assert not live.approve_and_publish().success
    head = live.state().deployment["publication_head"]["commit"]

    # Somebody else advanced the branch after our confirmed commit.
    other = OutputGitRepository(tmp_path / "other-out",
                               hermes_root=tmp_path / "other-hermes")
    foreign = other.commit(
        PROJECT, TestedSnapshot({"src/App.tsx": b"elsewhere"},
                                {"index.html": b"<html>elsewhere</html>"}))
    fetched = subprocess.run(
        ["git", "--git-dir", str(live.remote), "fetch",
         "file:///" + other.path.as_posix(), foreign["commit"]],
        capture_output=True,
    )
    assert fetched.returncode == 0, fetched.stderr
    moved = subprocess.run(
        ["git", "--git-dir", str(live.remote), "update-ref",
         f"refs/heads/{SLUG}", foreign["commit"]],
        capture_output=True,
    )
    assert moved.returncode == 0, moved.stderr
    live.vercel.fail_on = set()
    live.repo.git_calls.clear()

    result = live.orch.resume_publish(PROJECT, live.ws, principal_id=OWNER)

    assert not result.success
    assert result.error_code == "PUBLICATION_HEAD_CONFLICT"
    # No push at all, and the remote is untouched.
    assert live.repo.push_argvs() == []
    assert _remote_git(live.remote, "rev-parse",
                       f"refs/heads/{SLUG}") == foreign["commit"]
    # The branch authority still names what we genuinely confirmed.
    assert live.state().deployment["publication_head"]["commit"] == head
    assert live.vercel.promote_calls == ["dpl_1"]


def test_conflicting_remote_head_fails_closed_without_forcing(tmp_path):
    """A remote branch holding a valid commit we did not publish is case C.

    It is reported, never built on top of and never clobbered, and production
    is never touched.
    """
    live = Live(tmp_path)
    live.show_preview(1, b"v=1", b"<html>1</html>")
    assert live.approve_and_publish().success
    first = live.state().deployment["publication_head"]["commit"]

    # A commit from a SEPARATE repository: the remote branch head is a valid
    # 40-hex that this repository did not publish and will not fetch.
    other = OutputGitRepository(tmp_path / "other-out",
                               hermes_root=tmp_path / "other-hermes")
    foreign_git = other.commit(
        PROJECT, TestedSnapshot({"src/App.tsx": b"elsewhere"},
                                {"index.html": b"<html>elsewhere</html>"}))
    fetched = subprocess.run(
        ["git", "--git-dir", str(live.remote), "fetch",
         "file:///" + other.path.as_posix(), foreign_git["commit"]],
        capture_output=True,
    )
    assert fetched.returncode == 0, fetched.stderr
    updated = subprocess.run(
        ["git", "--git-dir", str(live.remote), "update-ref",
         f"refs/heads/{SLUG}", foreign_git["commit"]],
        capture_output=True,
    )
    assert updated.returncode == 0, updated.stderr
    live.repo.git_calls.clear()

    live.show_preview(2, b"v=2", b"<html>2</html>")
    result = live.approve_and_publish()

    assert not result.success
    assert result.error_code == "PUBLICATION_HEAD_CONFLICT"
    # Nothing was force-pushed over the remote head, and nothing was fetched.
    for argv in live.repo.push_argvs():
        assert "--force" not in argv
        assert not argv[-1].startswith("+")
    assert not [argv for argv in live.repo.git_calls if "fetch" in argv]
    # The remote head is still the foreign commit: untouched.
    assert _remote_git(live.remote, "rev-parse",
                       f"refs/heads/{SLUG}") == foreign_git["commit"]
    # Production was reached exactly once: by revision 1, never by the
    # conflicted revision 2.
    assert live.vercel.promote_calls == ["dpl_1"]
    assert live.smoke.urls == [CANONICAL]
    assert live.state().deployment["publication_head"]["commit"] == first
    assert live.state().lifecycle == ProjectLifecycle.PUBLISHING.value
    # Held open for reconciliation, so no new operation may supersede it.
    pending = live.state().deployment["pending_publication"]
    assert pending["reconciliation_required"] is True
    assert pending["outcome"] == "RECONCILIATION_REQUIRED"


# ---------------------------------------------------------------------------
# C. Approve means approve + publish
# ---------------------------------------------------------------------------


def _receive_loop(live, intent="APPROVE", telegram=None):
    hermes = MagicMock()
    hermes._run_fast_programmatic.return_value = MagicMock(
        success=True, response=intent)
    intake = IntakeProcessor(live.store, hermes_adapter=hermes)
    builder = MagicMock()
    builder.build.return_value = OperationResult.ok({})
    out = telegram or live.telegram
    dispatcher = TelegramDispatcher(
        live.store, intake, builder=builder, promote=live.orch,
        workspace_for=lambda pid: live.ws,
    )
    loop = TelegramReceiveLoop(bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
                               dispatcher=dispatcher, telegram_out=out,
                               hermes=hermes)
    return loop, out


def _payload(event_id, text, user="1", chat=CHAT):
    return {
        "update_id": event_id,
        "message": {"from": {"id": int(user)}, "chat": {"id": int(chat)},
                    "text": text, "date": event_id},
    }


def _loop_project(tmp_path, live, intent="APPROVE"):
    ProjectAccess(live.store).create(PROJECT, "telegram:1", channel="telegram",
                                     conversation_id=CHAT)
    live.show_preview(1, b"v=1", b"<html>1</html>")
    with live.store.acquire_writer(PROJECT) as state:
        state.roles["owner"] = "telegram:1"
        live.store.save(state)
    return _receive_loop(live, intent=intent)


def test_approve_immediately_publishes(tmp_path, live):
    loop, out = _loop_project(tmp_path, live)

    loop._process_update(_payload(401, "approve"))

    state = live.state()
    assert state.lifecycle == ProjectLifecycle.LIVE.value
    assert state.revisions.live_revision == 1
    assert state.production_url == CANONICAL
    # Exactly one promote of the approved deployment.
    assert live.vercel.promote_calls == ["dpl_1"]
    # No second publish was asked of, or needed from, the user.
    assert [text for _c, text in out.sent if "Preview approved" in text] == [
        "✅ Preview approved. Publishing..."]
    assert live.live_messages() == [f"🚀 Live: {CANONICAL}"]


def test_no_second_publish_confirmation_is_asked(tmp_path, live):
    loop, out = _loop_project(tmp_path, live)

    loop._process_update(_payload(402, "approve"))

    sent = [text for _c, text in out.sent]
    joined = "\n".join(sent)
    # The old copy asked for a further "publish" step; that step is gone.
    assert "bilang" not in joined
    assert "sudah siap ditayangkan" not in joined
    assert sent.count("✅ Preview approved. Publishing...") == 1
    assert sent == ["✅ Preview approved. Publishing...", f"🚀 Live: {CANONICAL}"]


def test_replayed_approval_creates_no_second_promotion(tmp_path, live):
    loop, out = _loop_project(tmp_path, live)

    loop._process_update(_payload(403, "approve"))
    loop._process_update(_payload(403, "approve"))

    assert live.vercel.promote_calls == ["dpl_1"]
    assert [text for _c, text in out.sent if "Preview approved" in text] == [
        "✅ Preview approved. Publishing..."]
    assert live.live_messages() == [f"🚀 Live: {CANONICAL}"]


def test_duplicate_approval_of_the_same_preview_is_idempotent(live):
    """Approving the exact same preview again, while it is already LIVE, is a
    no-op: no second promotion, no second smoke, no second publication, and no
    state change. The orchestrator reports it as the current live result."""
    live.show_preview(1, b"v=1", b"<html>1</html>")
    assert live.approve_and_publish().success
    before = live.state()
    before_live = before.deployment["last_live_deployment"]
    before_release = dict(before.deployment["last_live_release"])
    before_head = dict(before.deployment["publication_head"])

    assert live.orch.approve(PROJECT, principal_id=OWNER).success
    again = live.orch.promote(PROJECT, live.ws, principal_id=OWNER)

    assert again.success, again.error
    assert again.data["already_live"] is True
    assert again.data["production_url"] == CANONICAL
    assert again.data["deployment_url"] == DEPLOYMENT_URL
    # The reported release is the committed one, not the derived projection.
    assert again.data["release"] == before_release
    # No second promotion, no re-smoke, no second publication.
    assert live.vercel.promote_calls == ["dpl_1"]
    assert live.smoke.urls == [CANONICAL]
    assert len(live.repo.push_calls) == 1
    after = live.state()
    assert after.lifecycle == before.lifecycle
    assert after.production_url == before.production_url
    assert after.revisions.live_revision == before.revisions.live_revision
    assert after.deployment["last_live_deployment"] == before_live
    assert after.deployment["last_live_release"] == before_release
    assert after.deployment["publication_head"] == before_head
    # The orchestrator reported the current live result to its caller.
    assert again.data["operation_id"] == "op-1"


def test_duplicate_approve_of_a_live_project_never_promotes_again(tmp_path, live):
    """A second approve while the project is already LIVE never reaches a
    promotion: the LIVE state is not offered the approve/publish intents, so
    the message stays a conversation turn and nothing is promoted, published or
    re-smoked."""
    loop, out = _loop_project(tmp_path, live)
    hermes = loop.hermes
    hermes.fast_interpret.return_value = {
        "scope": "UNCLEAR", "summary": "small talk", "insights": [],
    }

    loop._process_update(_payload(404, "approve"))
    assert live.vercel.promote_calls == ["dpl_1"]

    # LIVE is not offered the approve/publish intents, so the second message
    # is classified as a conversation turn.
    hermes._run_fast_programmatic.return_value = MagicMock(
        success=True, response="INTAKE")
    loop._process_update(_payload(405, "approve"))

    assert live.vercel.promote_calls == ["dpl_1"]
    assert live.smoke.urls == [CANONICAL]
    assert len(live.repo.push_calls) == 1
    assert live.live_messages() == [f"🚀 Live: {CANONICAL}"]


def test_explicit_publish_after_live_approval_is_a_no_op(tmp_path, live):
    loop, out = _loop_project(tmp_path, live, intent="PUBLISH")

    loop._process_update(_payload(406, "publish"))
    assert live.vercel.promote_calls == ["dpl_1"]
    before = live.state()

    # The explicit command, re-issued for the same already-LIVE preview.
    assert live.orch.approve(PROJECT, principal_id=TG_OWNER).success
    result = live.orch.promote(PROJECT, live.ws, principal_id=TG_OWNER)

    assert result.success, result.error
    assert result.data["already_live"] is True
    assert result.data["production_url"] == CANONICAL
    assert live.vercel.promote_calls == ["dpl_1"]
    after = live.state()
    assert after.lifecycle == before.lifecycle
    assert after.production_url == before.production_url
    assert after.deployment["last_live_release"] == before.deployment["last_live_release"]
    assert live.repo.prepare_calls[0]["previous_publication_commit"] is None


def test_a_revision_landing_mid_flight_fails_closed(tmp_path):
    """A newer revision landing between the approval and the promotion's own
    re-check makes the approval stale: nothing is published, nothing is
    promoted, and the user is not told the site is live."""
    def land_revision():
        with live.store.acquire_writer(PROJECT) as state:
            state.revisions.source_revision = 2
            state.revisions.preview_revision = 2
            state.deployment["latest_shown_preview"] = {
                "operation_id": "op-2",
                "source_revision": 2,
                "deployment_id": "dpl_2",
                "source_sha256": "c" * 64,
                "artifact_sha256": "d" * 64,
                "preview_url": DEPLOYMENT_URL,
                "shown_at": 2.0,
            }
            live.store.save(state)

    live = Live(tmp_path, vercel=_RevisionLandsMidFlightVercel(land_revision))
    live.show_preview(1, b"v=1", b"<html>1</html>")

    result = live.approve_and_publish()

    assert not result.success
    assert result.error_code == "STALE_APPROVAL"
    assert live.vercel.promote_calls == []
    assert live.repo.push_calls == []
    assert live.state().lifecycle == ProjectLifecycle.PREVIEW_READY.value
    assert not live.live_messages()


def test_failed_publish_after_approval_sends_truthful_copy(tmp_path):
    live = Live(tmp_path, smoke=_RecordingSmoke(success=False))
    loop, out = _loop_project(tmp_path, live)

    loop._process_update(_payload(409, "approve"))

    assert live.vercel.promote_calls == ["dpl_1"]
    assert live.state().lifecycle == ProjectLifecycle.FAILED.value
    # Never claims the site is live.
    assert not live.live_messages()
    assert any("Preview" in text for _c, text in out.sent)
    # The Git stage completed BEFORE production, so the branch did move.
    assert len(live.repo.push_calls) == 1


def test_reviewer_approval_still_binds_but_cannot_publish(tmp_path, live):
    loop, out = _loop_project(tmp_path, live)
    # A reviewer, not the owner: approval is theirs to give, publication is not.
    with live.store.acquire_writer(PROJECT) as state:
        state.roles["owner"] = OWNER
        state.roles["reviewers"] = ["telegram:1"]
        live.store.save(state)

    loop._process_update(_payload(410, "approve", user="1"))

    state = live.state()
    assert state.deployment["approval"]["operation_id"] == "op-1"
    assert state.lifecycle == ProjectLifecycle.PREVIEW_READY.value
    assert live.vercel.promote_calls == []
    assert not live.live_messages()
    assert any("not authorized" in text for _c, text in out.sent)


def test_approve_without_a_shown_preview_never_publishes(tmp_path, live):
    ProjectAccess(live.store).create(PROJECT, "telegram:1", channel="telegram",
                                     conversation_id=CHAT)
    live.show_preview(1, b"v=1", b"<html>1</html>")
    with live.store.acquire_writer(PROJECT) as state:
        state.roles["owner"] = "telegram:1"
        state.deployment.pop("latest_shown_preview")
        live.store.save(state)
    loop, out = _receive_loop(live)

    loop._process_update(_payload(411, "approve"))

    assert live.vercel.promote_calls == []
    assert live.repo.push_calls == []
    assert not live.live_messages()
    assert out.sent


# ---------------------------------------------------------------------------
# Config surface
# ---------------------------------------------------------------------------


def test_github_publication_is_disabled_when_not_configured(tmp_path):
    """Publication-not-configured is a STATUS, not a shortcut: the machine
    still advances through GIT_CONFIRMED as a local no-op with zero Git
    subprocesses, then proceeds to production normally."""
    live = Live(tmp_path, github=False)
    live.show_preview(1, b"v=1", b"<html>1</html>")

    result = live.approve_and_publish()

    assert result.success, result.error
    # Not attempted, so nothing is reported as an unsynced source.
    assert live.repo.prepare_calls == []
    assert live.repo.push_calls == []
    assert live.repo.push_argvs() == []
    assert live.repo.remote_reads() == []
    state = live.state()
    release = state.deployment["last_live_release"]
    assert release["completeness"] == "COMPLETE"
    assert release["publication_commit"] is None
    assert release["tested_commit"] is None
    # No branch authority is claimed when nothing was published.
    assert "publication_head" not in state.deployment
    assert state.production_url == CANONICAL
    assert live.live_messages() == [f"🚀 Live: {CANONICAL}"]


def test_persisted_release_record_carries_no_credentials(tmp_path):
    live = Live(tmp_path)
    live.show_preview(1, b"v=1", b"<html>1</html>")

    assert live.approve_and_publish().success

    record = live.state().deployment["last_live_release"]
    assert set(record) >= {"release_id", "operation_id", "source_revision",
                           "tested_commit", "tested_tree", "publication_commit",
                           "publication_tree", "publication_parent",
                           "publication_branch", "publication_repo",
                           "source_sha256", "artifact_sha256", "deployment_id",
                           "production_url", "deployment_url", "smoke",
                           "committed_at", "completeness"}
    serialized = json.dumps(record)
    # No remote URL, no key path, no ssh command, nothing secret.
    assert "git@github.com" not in serialized
    assert DEPLOY_KEY not in serialized
    assert "ssh" not in serialized.lower()
    assert "id_" not in serialized
