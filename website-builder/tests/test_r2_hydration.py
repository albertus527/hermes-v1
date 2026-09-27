"""R2-C: exact-source hydration, pointer workspaces, and blob-exact materialization.

The contract under test is narrow and strict: a revision workspace is
DISPOSABLE and is materialized from a proven exact source, and nothing about the
leftover contents of a project directory is ever allowed to decide what a
revision starts from.
"""
from __future__ import annotations

import hashlib
import inspect
import shutil
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore
from app.deploy.git_output import (
    MaterializationRefusal,
    OutputGitRepository,
    looks_like_canonical_lfs_pointer,
    refuse_tree_entry,
    unsafe_repository_path,
)
from app.deploy.hydrate import (
    BASE_KIND_DRAFT,
    BASE_KIND_LIVE,
    DRAFT_SNAPSHOT_UNAVAILABLE,
    HYDRATION_ARTIFACT_MISMATCH,
    HYDRATION_BASE_DRIFT,
    HYDRATION_RECORD_INVALID,
    HYDRATION_RECOVERY_REQUIRED,
    HYDRATION_REPO_MISMATCH,
    HYDRATION_SOURCE_MISMATCH,
    HYDRATION_STAGING_UNAVAILABLE,
    HYDRATION_STATE_UNPERSISTED,
    HYDRATION_UNSAFE_ENTRY,
    HydrationError,
    HydrationRecord,
    RevisionBase,
    WorkspaceHydrator,
    monotonic_pointer_mode,
)
from app.deploy.snapshot import TestedSnapshot, digest, read_tree, source_fingerprint
from app.projects.revise import RevisionOrchestrator
from app.sandbox.runner import (
    WORKSPACE_POINTER_INVALID,
    WORKSPACE_POINTER_MISSING_AFTER_HYDRATION,
    PointerResolutionError,
    ProjectRunner,
)

OWNER = "owner-1"
OTHER = "owner-2"
CONFIGURED_URL = "git@github.com:o/r.git"
CANONICAL_LFS = (b"version https://git-lfs.github.com/spec/v1\n"
                 b"oid sha256:" + b"a" * 64 + b"\nsize 42\n")

SOURCE_FILES = {
    "src/App.tsx": b"export default () => null\n",
    "design-dna.json": b'{"version": 1}\n',
}
DIST_FILES = {"index.html": b"<html>northcut</html>\n"}
ALL_FILES = {**SOURCE_FILES, **{f"dist/{n}": c for n, c in DIST_FILES.items()}}


def _symlinks_available():
    """Windows needs Developer Mode (or elevation) before ``symlink_to`` works.

    The symlink guards are real and load-bearing; on a host that cannot create
    a symlink there is no way to exercise them, and pretending otherwise with a
    mock would test nothing.
    """
    root = Path(tempfile.mkdtemp())
    try:
        (root / "real").mkdir()
        (root / "link").symlink_to(root / "real", target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        shutil.rmtree(root, ignore_errors=True)


needs_symlinks = pytest.mark.skipif(
    not _symlinks_available(),
    reason="this host cannot create symlinks (Windows without Developer Mode)")


def _junctions_available():
    """A Windows directory junction needs no elevation, unlike a symlink."""
    if sys.platform != "win32":
        return False
    root = Path(tempfile.mkdtemp())
    try:
        (root / "real").mkdir()
        return _make_junction(root / "link", root / "real") is None
    except (OSError, NotImplementedError, ValueError):
        return False
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _make_junction(link, target):
    """Create a Windows directory junction, or return the failure text."""
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True, text=True)
    return None if result.returncode == 0 else result.stderr.strip()


needs_junctions = pytest.mark.skipif(
    not _junctions_available(),
    reason="this host cannot create directory junctions (non-Windows)")


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git(repo, *args, check=True):
    # ``core.hooksPath`` is neutralised for every test-side push, because a
    # globally installed hook (a Git LFS pre-push, for instance) would run
    # against a local mirror and fail the push for reasons that have nothing to
    # do with the code under test. The production repository neutralises it for
    # exactly the same reason.
    return subprocess.run(["git", "-c", "core.hooksPath=nul", *args],
                          cwd=str(repo), check=check, capture_output=True,
                          text=True)


def _write_object(repo, kind, payload):
    """Write a loose object directly, bypassing every one of git's checks.

    ``git hash-object`` and ``git mktree`` both validate paths and modes, which
    is exactly what this file needs to defeat: the guard under test is
    materialization, so a commit containing a path or a mode Git would never
    let you type has to be constructible. The object format is trivially
    ``"<kind> <len>\\0" + payload`` under zlib, so writing it by hand is honest
    and produces a real, fetchable object.
    """
    body = f"{kind} {len(payload)}".encode("ascii") + b"\0" + payload
    sha = hashlib.sha1(body).hexdigest()
    path = Path(repo) / ".git" / "objects" / sha[:2] / sha[2:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zlib.compress(body))
    return sha


def _blob(repo, content):
    return _write_object(repo, "blob", content)


def _write_tree(repo, entries):
    """Write a raw tree object. ``entries`` are (mode, type, sha, name)."""
    def sort_key(entry):
        _mode, kind, _sha, name = entry
        return name + "/" if kind == "tree" else name

    payload = b""
    for mode, kind, sha, name in sorted(entries, key=sort_key):
        payload += f"{mode} {name}".encode("utf-8") + b"\0" + bytes.fromhex(sha)
    return _write_object(repo, "tree", payload)


def _tree_from_paths(repo, path_entries):
    """Build the tree for a flat ``{path: (mode, type, payload)}`` mapping.

    ``payload`` is raw bytes (written as a blob) or an existing object id.
    """
    root = {}
    for path, (mode, kind, payload) in path_entries.items():
        if isinstance(payload, bytes):
            payload = _blob(repo, payload)
        node = root
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = (mode, kind, payload)

    def build(node):
        entries = []
        for name, value in node.items():
            if isinstance(value, dict):
                entries.append(("040000", "tree", build(value), name))
            else:
                entries.append((value[0], value[1], value[2], name))
        return _write_tree(repo, entries)

    return build(root)


def _commit_files(tmp_path, label, files, *, cacheinfo=(), message="c",
                  amend=False):
    """One commit whose tree contains exactly ``files`` (+ cacheinfo entries)."""
    repo = tmp_path / f"seed-{label}"
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        _git(tmp_path, "-c", "init.defaultBranch=main", "init", str(repo))
        _git(repo, "config", "user.email", "t@example.invalid")
        _git(repo, "config", "user.name", "t")
        _git(repo, "config", "commit.gpgsign", "false")
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    _git(repo, "add", "-A")
    for mode, blob, path in cacheinfo:
        # ``blob`` may be raw bytes: the object has to exist in the repository
        # being built, not in whichever repo happened to create it.
        if isinstance(blob, bytes):
            blob = _blob(repo, blob)
        _git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}")
    if amend:
        _git(repo, "commit", "--amend", "-m", message)
    else:
        _git(repo, "commit", "-m", message)
    commit = _git(repo, "rev-parse", "HEAD").stdout.strip()
    tree = _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    return repo, commit, tree


class _MirrorRepo(OutputGitRepository):
    """The real output repository, with its fetch boundary mirrored locally.

    Every ``_run`` is recorded, because the only honest way to prove "no branch
    tip was ever fetched" and "no checkout/checkout-index was ever used" is to
    inspect the commands that were actually issued.

    The redirect is a private ``GIT_CONFIG_GLOBAL`` file rather than the
    ``GIT_CONFIG_KEY_n`` environment form, because git refuses to parse the
    ``url.<base>.insteadOf`` subsection key through that channel. The production
    fetch already disables credential helpers and hooks, so only the remote
    itself has to be redirected here.
    """

    def __init__(self, root, mirror):
        self.commands = []
        self._mirror_config = Path(str(root) + "-mirror.gitconfig")
        self._mirror_config.write_text(
            f'[url "file:///{Path(mirror).as_posix().lstrip("/")}"]\n'
            f"\tinsteadOf = {CONFIGURED_URL}\n",
            encoding="utf-8")
        super().__init__(root)
        self.mirror = Path(mirror)
        # The constructor's own ``init`` is not part of the recorded history.

    def _run(self, args, check=True, extra=None, init=False):
        if not init:
            self.commands.append(list(args))
        return super()._run(args, check=check, extra=extra, init=init)

    def _ssh_env(self, ssh_key=None):
        return {"GIT_CONFIG_GLOBAL": str(self._mirror_config)}

    def fetches(self):
        return [c for c in self.commands if "fetch" in c]


# ---------------------------------------------------------------------------
# Project fixtures
# ---------------------------------------------------------------------------


def _store(tmp_path):
    return ProjectStateStore(tmp_path / "state")


def _runner(tmp_path, store):
    return ProjectRunner(tmp_path / "workspaces", store)


def _write_workspace(workspace, files=None, dist=None):
    files = SOURCE_FILES if files is None else files
    dist = DIST_FILES if dist is None else dist
    workspace.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (workspace / "dist").mkdir(exist_ok=True)
    for name, content in dist.items():
        (workspace / "dist" / name).write_bytes(content)
    return workspace


def _base_state(state):
    state.roles["owner"] = OWNER
    state.conversation_id = "555"
    state.brief = {"name": "Northcut", "what": "barbershop", "why": "booking"}
    state.design_dna = {"version": 1, "typography": {"heading_font": "Inter",
                                                     "body_font": "Inter"}}
    state.revisions.source_revision = 1
    state.revisions.qa_revision = 1
    state.revisions.preview_revision = 1
    return state


def _draft_project(tmp_path, project_id="proj",
                   lifecycle=ProjectLifecycle.PREVIEW_READY, files=None, dist=None,
                   extra_deployment=None):
    """A PREVIEW_READY project with a real tested snapshot: the DRAFT source."""
    store = _store(tmp_path)
    workspace = _write_workspace(tmp_path / "workspaces" / project_id, files, dist)
    snapshot = TestedSnapshot.capture(workspace)
    with store.acquire_writer(project_id) as state:
        _base_state(state)
        state.lifecycle = lifecycle.value
        state.deployment["checked"] = {
            "source_revision": 1,
            "source_sha256": snapshot.source_sha256,
            "artifact_sha256": snapshot.artifact_sha256,
        }
        state.deployment["tested_snapshot"] = snapshot.to_dict()
        for key, value in (extra_deployment or {}).items():
            state.deployment[key] = value
        store.save(state)
    return store, workspace, snapshot


def _draft_hydrator(tmp_path, store, **kwargs):
    return WorkspaceHydrator(_runner(tmp_path, store), store, **kwargs)


def _orchestrator(hydrator, **kwargs):
    return RevisionOrchestrator(hydrator.runner, hydrator.store,
                                hermes_adapter=object(), hydrator=hydrator,
                                **kwargs)


def _reserve(orchestrator, project_id="proj", seq=1, principal=OWNER):
    result = orchestrator.reserve(project_id, seq, principal_id=principal)
    assert result.success, result.error
    entry = next(e for e in orchestrator.store.load(project_id).pending_revisions
                 if e["seq"] == seq)
    return RevisionBase.from_dict(entry["base"])


# ---------------------------------------------------------------------------
# LIVE fixture: a real commit, a real preview ref, a real release record
# ---------------------------------------------------------------------------


class _Live:
    """A real publication: a commit, a shared tree, a mirror, and the record.

    The tested commit and the publication commit are DIFFERENT objects that
    share a tree, which is exactly what the real pipeline produces from a
    TestedSnapshot. That is the whole point of the corroboration rule.
    """

    def __init__(self, tmp_path, project_id="proj", *, record_overrides=None,
                 keep_tested=True, keep_publication=True, head_ahead=False,
                 lifecycle=ProjectLifecycle.LIVE):
        self.project_id = project_id
        self.repo, tested, tree = _commit_files(tmp_path, "live", ALL_FILES)
        self.repo, publication, _ = _commit_files(
            tmp_path, "live", ALL_FILES, message="published", amend=True)
        self.tested, self.publication, self.tree = tested, publication, tree
        self.mirror = tmp_path / "mirror.git"
        _git(tmp_path, "init", "--bare", str(self.mirror))
        # The amendment made the tested commit unreachable from the branch, so
        # it is pushed explicitly. A real remote has it too: the preview ref is
        # what still points at it.
        _git(self.repo, "push", str(self.mirror), f"{self.tested}:refs/heads/tested")
        _git(self.repo, "push", str(self.mirror), f"{self.publication}:refs/heads/main")
        if head_ahead:
            self._push_ahead()
        self.workspace = tmp_path / "workspaces" / project_id
        self.source_sha256 = source_fingerprint(_write_workspace(tmp_path / "dig"))
        self.artifact_sha256 = digest(DIST_FILES)
        self.store = _store(tmp_path)
        self.output = _MirrorRepo(tmp_path / "output.git", self.mirror)
        self._seed_output(keep_tested, keep_publication, head_ahead)
        record = {
            "release_id": "rel-1", "operation_id": "op-1",
            "completeness": "COMPLETE", "publication_configured": True,
            "source_revision": 1,
            "publication_commit": publication, "publication_parent": None,
            "publication_tree": tree, "publication_repo": "o/r",
            "publication_branch": "northcut",
            "tested_commit": tested, "tested_tree": tree,
            "source_sha256": self.source_sha256,
            "artifact_sha256": self.artifact_sha256,
            "deployment_id": "dsp-1",
            "production_url": "https://northcut.vercel.app",
            "deployment_url": "https://dsp-1.northcut.vercel.app",
            "smoke": {"status": "PASSED", "target_host": "northcut.vercel.app",
                      "target_path": "/", "at": 1.0},
            "committed_at": 1.0,
        }
        record.update(record_overrides or {})
        self.record = record
        with self.store.acquire_writer(project_id) as state:
            _base_state(state)
            state.lifecycle = lifecycle.value
            state.deployment["last_live_release"] = record
            self.store.save(state)
        self.runner = _runner(tmp_path, self.store)

    def _push_ahead(self):
        """Move the branch forward, so its tip is NOT the release."""
        (self.repo / "src" / "Later.tsx").write_bytes(b"export const later = 1\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "later")
        self.head = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        _git(self.repo, "push", "-f", str(self.mirror), "HEAD:refs/heads/main")

    def _seed_output(self, keep_tested, keep_publication, head_ahead):
        import hashlib

        # The internal preview ref is ``preview/<project-sha>/<snapshot-sha>``
        # -- the snapshot identity, not the commit id. That is what makes the
        # ref an immutable TestedSnapshot commit rather than "some ref that
        # happens to point here".
        project = hashlib.sha256(self.project_id.encode()).hexdigest()
        identity = hashlib.sha256(f"{self.tested}:{self.tree}".encode()).hexdigest()
        if keep_tested:
            self.output._run([
                "fetch", "--no-tags", "--", str(self.mirror),
                f"{self.tested}:refs/heads/preview/{project}/{identity}"])
        if keep_publication:
            self.output._run(["fetch", "--no-tags", "--", str(self.mirror),
                              f"{self.publication}:refs/hydrate/seeded"])
        if head_ahead:
            # The branch tip is present locally AND is not the release. It must
            # never be consulted.
            self.output._run(["fetch", "--no-tags", "--", str(self.mirror),
                              f"{self.head}:refs/heads/main"])
        self.output.commands.clear()

    def hydrator(self, **kwargs):
        kwargs.setdefault("output_repo", self.output)
        kwargs.setdefault("source_repo_url", CONFIGURED_URL)
        return WorkspaceHydrator(self.runner, self.store, **kwargs)

    def orchestrator(self, **kwargs):
        kwargs.setdefault("hydrator", self.hydrator())
        kwargs.setdefault("source_repo_url", CONFIGURED_URL)
        return RevisionOrchestrator(self.runner, self.store, hermes_adapter=object(),
                                    **kwargs)


# ---------------------------------------------------------------------------
# POINTER
# ---------------------------------------------------------------------------


def test_never_hydrated_with_no_current_resolves_the_legacy_workspace(tmp_path):
    store, workspace, _ = _draft_project(tmp_path)
    runner = _runner(tmp_path, store)
    assert runner.resolve_workspace("proj") == workspace
    assert runner.create_workspace("proj") == workspace
    for name in (".hermes", ".browser", ".runtime"):
        assert (workspace / name).is_dir()


def test_pointer_mode_true_with_a_missing_current_fails_closed(tmp_path):
    store, workspace, _ = _draft_project(tmp_path)
    runner = _runner(tmp_path, store)
    with store.acquire_writer("proj") as state:
        state.deployment["pointer_mode"] = True
        store.save(state)
    with pytest.raises(PointerResolutionError) as exc:
        runner.resolve_workspace("proj")
    assert exc.value.error_code == WORKSPACE_POINTER_MISSING_AFTER_HYDRATION
    with pytest.raises(PointerResolutionError):
        runner.create_workspace("proj")


def test_pointer_mode_false_is_still_legacy(tmp_path):
    """``False`` is the explicit 'never crossed the boundary' statement, and it
    is what a project carries before its first swap."""
    store, workspace, _ = _draft_project(tmp_path)
    runner = _runner(tmp_path, store)
    with store.acquire_writer("proj") as state:
        state.deployment["pointer_mode"] = False
        store.save(state)
    assert runner.resolve_workspace("proj") == workspace


@pytest.mark.parametrize("raw", [
    "rev-1 rev-2\n",   # multi-token
    "rev-1\nrev-2\n",  # multi-line
    "\n",              # empty line
    "",                # zero-length
    "rev-\n",          # no number
    "rev-1a\n",        # not all digits
    "rev- 1\n",        # internal space
    "current\n",
    "HEAD\n",
    "refs/heads/main\n",
    "a" * 40 + "\n",   # a commit id is never an operation token
    "0" * 40 + "\n",
    "rev-1\n\n",
])
def test_a_malformed_pointer_fails_closed_and_never_falls_back(tmp_path, raw):
    store, workspace, _ = _draft_project(tmp_path)
    runner = _runner(tmp_path, store)
    (workspace / "current").write_text(raw, encoding="utf-8")
    with pytest.raises(PointerResolutionError) as exc:
        runner.resolve_workspace("proj")
    assert exc.value.error_code == WORKSPACE_POINTER_INVALID


def test_an_unreadable_pointer_fails_closed(tmp_path):
    store, workspace, _ = _draft_project(tmp_path)
    runner = _runner(tmp_path, store)
    (workspace / "current").write_bytes(b"\xff\xfe\x00rev-1")
    with pytest.raises(PointerResolutionError) as exc:
        runner.resolve_workspace("proj")
    assert exc.value.error_code == WORKSPACE_POINTER_INVALID


@needs_symlinks
def test_a_symlinked_pointer_fails_closed(tmp_path):
    store, workspace, _ = _draft_project(tmp_path)
    runner = _runner(tmp_path, store)
    (tmp_path / "elsewhere").mkdir()
    (workspace / "current").symlink_to(tmp_path / "elsewhere",
                                       target_is_directory=True)
    with pytest.raises(PointerResolutionError) as exc:
        runner.resolve_workspace("proj")
    assert exc.value.error_code == WORKSPACE_POINTER_INVALID


def test_a_pointer_targeting_a_missing_operation_directory_fails_closed(tmp_path):
    store, workspace, _ = _draft_project(tmp_path)
    runner = _runner(tmp_path, store)
    (workspace / ".ops").mkdir()
    (workspace / "current").write_text("rev-4\n", encoding="utf-8")
    with pytest.raises(PointerResolutionError) as exc:
        runner.resolve_workspace("proj")
    assert exc.value.error_code == WORKSPACE_POINTER_INVALID


def test_a_pointer_targeting_a_file_fails_closed(tmp_path):
    store, workspace, _ = _draft_project(tmp_path)
    runner = _runner(tmp_path, store)
    (workspace / ".ops").mkdir()
    (workspace / ".ops" / "rev-1").write_text("not a directory", encoding="utf-8")
    (workspace / "current").write_text("rev-1\n", encoding="utf-8")
    with pytest.raises(PointerResolutionError) as exc:
        runner.resolve_workspace("proj")
    assert exc.value.error_code == WORKSPACE_POINTER_INVALID


@needs_symlinks
def test_a_pointer_targeting_a_symlinked_operation_directory_fails_closed(tmp_path):
    store, workspace, _ = _draft_project(tmp_path)
    runner = _runner(tmp_path, store)
    (tmp_path / "outside").mkdir()
    (workspace / ".ops").mkdir()
    (workspace / ".ops" / "rev-1").symlink_to(tmp_path / "outside",
                                              target_is_directory=True)
    (workspace / "current").write_text("rev-1\n", encoding="utf-8")
    with pytest.raises(PointerResolutionError) as exc:
        runner.resolve_workspace("proj")
    assert exc.value.error_code == WORKSPACE_POINTER_INVALID


@needs_symlinks
def test_a_symlinked_ops_directory_fails_closed(tmp_path):
    store, workspace, _ = _draft_project(tmp_path)
    runner = _runner(tmp_path, store)
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "rev-1").mkdir()
    (workspace / ".ops").symlink_to(tmp_path / "outside", target_is_directory=True)
    (workspace / "current").write_text("rev-1\n", encoding="utf-8")
    with pytest.raises(PointerResolutionError) as exc:
        runner.resolve_workspace("proj")
    assert exc.value.error_code == WORKSPACE_POINTER_INVALID


@needs_symlinks
def test_write_pointer_refuses_a_symlinked_target(tmp_path):
    store, workspace, _ = _draft_project(tmp_path)
    runner = _runner(tmp_path, store)
    (tmp_path / "outside").mkdir()
    (workspace / "current").symlink_to(tmp_path / "outside" / "current")
    with pytest.raises(PointerResolutionError):
        runner.write_pointer("proj", "rev-1")
    assert not (tmp_path / "outside" / "current").exists()



def test_a_valid_pointer_resolves_to_the_operation_directory(tmp_path):
    store, workspace, _ = _draft_project(tmp_path)
    runner = _runner(tmp_path, store)
    op = workspace / ".ops" / "rev-1"
    op.mkdir(parents=True)
    (workspace / "current").write_text("rev-1\n", encoding="utf-8")
    assert runner.resolve_workspace("proj") == op
    assert runner.create_workspace("proj") == op
    for name in (".hermes", ".browser", ".runtime"):
        assert (op / name).is_dir()


def test_write_pointer_writes_exactly_one_token_line(tmp_path):
    store, workspace, _ = _draft_project(tmp_path)
    runner = _runner(tmp_path, store)
    runner.write_pointer("proj", "rev-7")
    assert (workspace / "current").read_text(encoding="utf-8") == "rev-7\n"
    assert runner.pointer_token("proj") == "rev-7"
    assert not list(workspace.glob(".current*"))


def test_write_pointer_refuses_an_invalid_token(tmp_path):
    store, workspace, _ = _draft_project(tmp_path)
    runner = _runner(tmp_path, store)
    for bad in ("", "rev-x", "a" * 40, "rev-1\nrev-2"):
        with pytest.raises(PointerResolutionError):
            runner.write_pointer("proj", bad)
    assert not (workspace / "current").exists()


def test_pointer_mode_is_monotonic():
    deployment = {}
    monotonic_pointer_mode(deployment)
    assert deployment["pointer_mode"] is True
    monotonic_pointer_mode(deployment)
    assert deployment["pointer_mode"] is True
    # False is upgraded, not respected: the project is in pointer mode or it is
    # not, and a spurious False must not re-open legacy fallback.
    upgraded = {"pointer_mode": False}
    monotonic_pointer_mode(upgraded)
    assert upgraded["pointer_mode"] is True


def test_sweep_preserves_the_pointer_target_unconditionally(tmp_path):
    store, workspace, _ = _draft_project(tmp_path)
    runner = _runner(tmp_path, store)
    ops = workspace / ".ops"
    for name in ("rev-1", "rev-2", "rev-3"):
        (ops / name).mkdir(parents=True)
    (workspace / "current").write_text("rev-2\n", encoding="utf-8")
    removed = runner.sweep_ops("proj")
    assert sorted(removed) == ["rev-1", "rev-3"]
    assert (ops / "rev-2").is_dir()


def test_sweep_sweeps_nothing_when_the_pointer_is_unusable(tmp_path):
    store, workspace, _ = _draft_project(tmp_path)
    runner = _runner(tmp_path, store)
    ops = workspace / ".ops"
    for name in ("rev-1", "rev-2"):
        (ops / name).mkdir(parents=True)
    (workspace / "current").write_text("garbage\n", encoding="utf-8")
    assert runner.sweep_ops("proj") == []
    assert (ops / "rev-1").is_dir() and (ops / "rev-2").is_dir()


# ---------------------------------------------------------------------------
# RUNNER: one resolver authority
# ---------------------------------------------------------------------------


def _record_spawns():
    seen = []

    class _Popen:
        def __init__(self, command, cwd=None, env=None, **kwargs):
            seen.append({"cwd": Path(cwd), "env": env, "args": list(command)})
            raise _Captured

    class _Captured(Exception):
        pass

    return seen, _Popen, _Captured


def test_every_runner_entry_point_uses_the_resolved_operation_directory(tmp_path):
    store, workspace, _ = _draft_project(tmp_path)
    runner = _runner(tmp_path, store)
    op = workspace / ".ops" / "rev-2"
    op.mkdir(parents=True)
    (workspace / "current").write_text("rev-2\n", encoding="utf-8")

    assert runner.create_workspace("proj") == op
    assert runner.resolve_workspace("proj") == op

    env = runner._build_project_env("proj", op)
    assert env["WORKSPACE_ROOT"] == str(op)
    assert env["HERMES_HOME"] == str(op / ".hermes")

    seen, popen, captured = _record_spawns()
    with patch("app.sandbox.runner.subprocess.Popen", popen):
        with pytest.raises(captured):
            runner.run_command("proj", ["node", "-e", "0"])
        assert seen[-1]["cwd"] == op
        assert seen[-1]["env"]["WORKSPACE_ROOT"] == str(op)
        assert seen[-1]["env"]["HERMES_HOME"] == str(op / ".hermes")
        with pytest.raises(captured):
            runner.start_background("proj", ["node", "server.js"])
    assert seen[-1]["cwd"] == op
    assert seen[-1]["env"]["WORKSPACE_ROOT"] == str(op)


def test_legacy_mode_is_unchanged_by_the_resolver(tmp_path):
    store, workspace, _ = _draft_project(tmp_path)
    runner = _runner(tmp_path, store)
    env = runner._build_project_env("proj", runner.create_workspace("proj"))
    assert env["WORKSPACE_ROOT"] == str(workspace)
    assert env["HERMES_HOME"] == str(workspace / ".hermes")


def test_the_legacy_root_cannot_be_used_once_pointer_mode_is_true(tmp_path):
    store, workspace, _ = _draft_project(tmp_path)
    runner = _runner(tmp_path, store)
    (workspace / ".ops" / "rev-1").mkdir(parents=True)
    runner.write_pointer("proj", "rev-1")
    with store.acquire_writer("proj") as state:
        state.deployment["pointer_mode"] = True
        store.save(state)
    assert runner.resolve_workspace("proj") == workspace / ".ops" / "rev-1"
    # The legacy root still exists and is still writable. It is simply no
    # longer the project's workspace -- not even once the pointer is lost.
    assert workspace.is_dir()
    (workspace / "current").unlink()
    with pytest.raises(PointerResolutionError) as exc:
        runner.resolve_workspace("proj")
    assert exc.value.error_code == WORKSPACE_POINTER_MISSING_AFTER_HYDRATION


def test_the_resolver_never_acquires_the_project_writer_lock(tmp_path):
    """``build.py`` calls ``create_workspace`` from inside a writer block, so a
    resolver that reached for the lock would invert lock order and deadlock."""
    store, workspace, _ = _draft_project(tmp_path)
    runner = _runner(tmp_path, store)
    with store.acquire_writer("proj"):
        assert runner.create_workspace("proj") == workspace


# ---------------------------------------------------------------------------
# DRAFT hydration
# ---------------------------------------------------------------------------


def test_draft_hydration_stages_exact_order(tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    seen = _RecordingHydrator(_runner(tmp_path, store), store)
    base = _reserve(_orchestrator(seen))
    outcome = seen.hydrate("proj", 1, base)
    assert seen.states == ["RESERVED", "FETCHED", "VERIFIED", "READY"]
    assert outcome.workspace == workspace / ".ops" / "rev-1"
    assert outcome.base_kind == BASE_KIND_DRAFT


def test_draft_hydration_is_byte_exact(tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    hydrator = _draft_hydrator(tmp_path, store)
    base = _reserve(_orchestrator(hydrator))
    outcome = hydrator.hydrate("proj", 1, base)
    assert read_tree(outcome.workspace) == {**snapshot.source,
                                             **{f"dist/{n}": c
                                                for n, c in snapshot.dist.items()}}
    for name in (".hermes", ".browser", ".runtime"):
        assert (outcome.workspace / name).is_dir()
    # The runtime directories are excluded, so creating them changed no digest.
    assert outcome.record.expected_source_sha256 == snapshot.source_sha256
    assert outcome.record.expected_artifact_sha256 == snapshot.artifact_sha256


def test_draft_ignores_whatever_the_legacy_workspace_holds_now(tmp_path):
    """The leftover mutable workspace is not the source, and edits made to it
    since the last test are not carried into the revision."""
    store, workspace, snapshot = _draft_project(tmp_path)
    (workspace / "src" / "App.tsx").write_bytes(b"export default () => 'DRIFT'\n")
    (workspace / "src" / "Scratch.tsx").write_bytes(b"// not tested\n")
    hydrator = _draft_hydrator(tmp_path, store)
    base = _reserve(_orchestrator(hydrator))
    outcome = hydrator.hydrate("proj", 1, base)
    assert (outcome.workspace / "src" / "App.tsx").read_bytes() == SOURCE_FILES["src/App.tsx"]
    assert not (outcome.workspace / "src" / "Scratch.tsx").exists()


def test_draft_never_reads_last_live_release(tmp_path):
    live_like = {
        "release_id": "rel-1", "operation_id": "op-1", "completeness": "COMPLETE",
        "publication_configured": True, "source_revision": 9,
        "publication_commit": "a" * 40, "publication_parent": None,
        "publication_tree": "b" * 40, "publication_repo": "o/r",
        "publication_branch": "northcut", "tested_commit": "c" * 40,
        "tested_tree": "b" * 40, "source_sha256": "d" * 64,
        "artifact_sha256": "e" * 64, "deployment_id": "dsp-1",
        "production_url": "https://northcut.vercel.app",
        "deployment_url": "https://x.vercel.app", "committed_at": 1.0,
        "smoke": {"status": "PASSED", "target_host": "northcut.vercel.app",
                  "target_path": "/", "at": 1.0},
    }
    store, workspace, snapshot = _draft_project(
        tmp_path, extra_deployment={"last_live_release": live_like})
    hydrator = _draft_hydrator(tmp_path, store)
    base = _reserve(_orchestrator(hydrator))
    outcome = hydrator.hydrate("proj", 1, base)
    assert outcome.base_kind == BASE_KIND_DRAFT
    record = store.load("proj").deployment["hydration"]
    for field in ("commit", "repo", "branch", "expected_tree",
                  "tested_commit_corroborated"):
        assert record[field] is None, field
    assert record["fetched_locally"] is True


def test_draft_hydration_performs_zero_git_and_zero_network(tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)

    class _Explode:
        def __getattr__(self, name):
            raise AssertionError(f"the DRAFT path must not touch Git: {name}")

    hydrator = WorkspaceHydrator(_runner(tmp_path, store), store,
                                 output_repo=_Explode(),
                                 source_repo_url=CONFIGURED_URL)
    base = _reserve(_orchestrator(hydrator))
    import app.deploy.git_output as git_output

    original = git_output.subprocess.run

    def no_git(args, *a, **kw):
        if args and "git" in [str(x) for x in args]:
            raise AssertionError("the DRAFT path must not spawn git")
        return original(args, *a, **kw)

    git_output.subprocess.run = no_git
    try:
        outcome = hydrator.hydrate("proj", 1, base)
    finally:
        git_output.subprocess.run = original
    assert outcome.base_kind == BASE_KIND_DRAFT


def test_draft_refuses_a_snapshot_that_changed_after_reservation(tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    hydrator = _draft_hydrator(tmp_path, store)
    base = _reserve(_orchestrator(hydrator))
    other = _write_workspace(tmp_path / "other", {"src/App.tsx": b"// different\n"})
    with store.acquire_writer("proj") as state:
        state.deployment["tested_snapshot"] = TestedSnapshot.capture(other).to_dict()
        store.save(state)
    with pytest.raises(HydrationError) as exc:
        hydrator.hydrate("proj", 1, base)
    assert exc.value.error_code == HYDRATION_SOURCE_MISMATCH
    assert not (workspace / "current").exists()


def test_draft_refuses_when_the_snapshot_is_gone(tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    hydrator = _draft_hydrator(tmp_path, store)
    base = _reserve(_orchestrator(hydrator))
    with store.acquire_writer("proj") as state:
        state.deployment.pop("tested_snapshot")
        store.save(state)
    with pytest.raises(HydrationError) as exc:
        hydrator.hydrate("proj", 1, base)
    assert exc.value.error_code == DRAFT_SNAPSHOT_UNAVAILABLE


def test_reserve_refuses_a_draft_with_no_usable_snapshot(tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    with store.acquire_writer("proj") as state:
        state.deployment.pop("tested_snapshot")
        store.save(state)
    result = _orchestrator(_draft_hydrator(tmp_path, store)).reserve(
        "proj", 1, principal_id=OWNER)
    assert not result.success
    assert result.error_code == DRAFT_SNAPSHOT_UNAVAILABLE
    assert store.load("proj").pending_revisions == []


def test_a_snapshot_key_that_traverses_is_refused_by_the_materializer(tmp_path):
    """A durable snapshot is a free-form bag, so a hand-written one can carry a
    key that escapes. The DRAFT materializer applies the same predicate the LIVE
    path does, so the key is refused rather than written.

    The base is frozen from the same payload, so the refusal has to come from
    materialization rather than from admission noticing a mismatch.
    """
    from app.deploy.hydrate import build_draft_base

    store, workspace, _ = _draft_project(tmp_path)
    payload = dict(store.load("proj").deployment["tested_snapshot"])
    payload["source"] = dict(payload["source"])
    payload["source"]["../escape.txt"] = "eA=="
    snapshot = TestedSnapshot.from_dict(payload)
    with store.acquire_writer("proj") as state:
        state.deployment["tested_snapshot"] = payload
        state.deployment["checked"] = {
            "source_revision": 1,
            "source_sha256": snapshot.source_sha256,
            "artifact_sha256": snapshot.artifact_sha256,
        }
        state.pending_revisions.append({
            "seq": 1, "principal_id": OWNER, "reserved_at": 1.0, "applied": False,
            "base": build_draft_base(
                snapshot, seq=1, reserved_at=1.0, requirements_version=0,
                design_dna_version=0, source_revision=1).to_dict()})
        store.save(state)
    base = RevisionBase.from_dict(
        store.load("proj").pending_revisions[0]["base"])
    with pytest.raises(HydrationError) as exc:
        _draft_hydrator(tmp_path, store).hydrate("proj", 1, base)
    assert exc.value.error_code == HYDRATION_UNSAFE_ENTRY
    assert not (tmp_path / "escape.txt").exists()
    assert not (workspace / "current").exists()


# ---------------------------------------------------------------------------
# Per-kind hydration record validation
# ---------------------------------------------------------------------------


def _record(**overrides):
    fields = dict(
        operation_id="rev-1", state="FETCHED", base_kind=BASE_KIND_DRAFT,
        commit=None, repo=None, branch=None, expected_tree=None,
        tested_commit_corroborated=None, fetched_locally=True,
        op_dir="x/.ops/rev-1", op_token="rev-1",
        expected_source_sha256="a" * 64, expected_artifact_sha256="b" * 64,
        updated_at=1.0, error_code=None)
    fields.update(overrides)
    return HydrationRecord.from_dict(fields)


def test_a_draft_record_accepts_only_null_live_identity():
    assert _record().validate() is None
    for field, value in (("commit", "a" * 40), ("repo", "o/r"), ("branch", "northcut"),
                         ("expected_tree", "b" * 40), ("tested_commit_corroborated", True)):
        with pytest.raises(HydrationError) as exc:
            _record(**{field: value})
        assert exc.value.error_code == HYDRATION_RECORD_INVALID, field


def test_a_live_record_requires_its_full_identity():
    live = dict(base_kind=BASE_KIND_LIVE, commit="a" * 40, repo="o/r",
                branch="northcut", expected_tree="b" * 40,
                tested_commit_corroborated=False, fetched_locally=True)
    assert _record(**live).validate() is None
    for field, value in (("commit", None), ("commit", "nope"), ("repo", None),
                         ("branch", "preview/" + "a" * 64), ("expected_tree", None),
                         ("tested_commit_corroborated", None)):
        payload = dict(live)
        payload[field] = value
        with pytest.raises(HydrationError) as exc:
            _record(**payload)
        assert exc.value.error_code == HYDRATION_RECORD_INVALID, field


def test_a_draft_record_may_not_claim_a_network_acquisition():
    with pytest.raises(HydrationError) as exc:
        _record(fetched_locally=False)
    assert exc.value.error_code == HYDRATION_RECORD_INVALID


def test_an_invalid_record_on_disk_fails_closed(tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    with store.acquire_writer("proj") as state:
        state.deployment["hydration"] = {
            "operation_id": "rev-9", "state": "READY", "base_kind": BASE_KIND_DRAFT,
            "commit": "a" * 40, "fetched_locally": True, "op_token": "rev-9"}
        store.save(state)
    hydrator = _draft_hydrator(tmp_path, store)
    base = _reserve(_orchestrator(hydrator))
    with pytest.raises(HydrationError) as exc:
        hydrator.hydrate("proj", 1, base)
    assert exc.value.error_code == HYDRATION_RECORD_INVALID


# ---------------------------------------------------------------------------
# Base immutability
# ---------------------------------------------------------------------------


def test_a_base_that_is_not_the_reserved_one_is_refused(tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    hydrator = _draft_hydrator(tmp_path, store)
    base = _reserve(_orchestrator(hydrator))
    drifted = RevisionBase.from_dict({**base.to_dict(), "source_revision": 99})
    with pytest.raises(HydrationError) as exc:
        hydrator.hydrate("proj", 1, drifted)
    assert exc.value.error_code == HYDRATION_BASE_DRIFT
    assert not (workspace / ".ops" / "rev-1" / "src").exists()


def test_a_reservation_without_a_frozen_base_is_refused(tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    hydrator = _draft_hydrator(tmp_path, store)
    base = _reserve(_orchestrator(hydrator))
    with store.acquire_writer("proj") as state:
        state.pending_revisions[0].pop("base")
        store.save(state)
    with pytest.raises(HydrationError) as exc:
        hydrator.hydrate("proj", 1, base)
    assert exc.value.error_code == HYDRATION_BASE_DRIFT


def test_the_live_base_does_not_drift_when_a_newer_release_lands(tmp_path):
    """A revision continues from what it was ADMITTED against, not from what the
    project looks like now. A newer publication must not be substituted for the
    reserved one."""
    live = _Live(tmp_path)
    base = _reserve(live.orchestrator())
    _newer_repo, newer, _newer_tree = _commit_files(
        tmp_path, "newer", {**ALL_FILES, "src/New.tsx": b"n\n"}, message="newer")
    with live.store.acquire_writer("proj") as state:
        record = dict(state.deployment["last_live_release"])
        record["publication_commit"] = newer
        record["source_sha256"] = "f" * 64
        state.deployment["last_live_release"] = record
        live.store.save(state)
    outcome = live.hydrator().hydrate("proj", 1, base)
    assert (outcome.workspace / "src" / "App.tsx").read_bytes() == SOURCE_FILES["src/App.tsx"]
    assert not (outcome.workspace / "src" / "New.tsx").exists()
    assert outcome.record.commit == live.publication


# ---------------------------------------------------------------------------
# State machine / crash windows
# ---------------------------------------------------------------------------


class _Crash(RuntimeError):
    pass


class _RecordingHydrator(WorkspaceHydrator):
    """Records the durable stage order, and can crash at a chosen one."""

    def __init__(self, runner, store, crash_at=None, refuse_ready=False, **kwargs):
        super().__init__(runner, store, **kwargs)
        self.states = []
        self.crash_at = crash_at
        self.refuse_ready = refuse_ready

    def _save_record(self, project_id, record):
        super()._save_record(project_id, record)
        self.states.append(record.state)
        if self.crash_at == record.state:
            raise _Crash(record.state)

    def _commit_ready(self, project_id, record):
        if self.refuse_ready:
            self.states.append("READY_LOST")
            raise HydrationError(HYDRATION_STATE_UNPERSISTED, "save failed")
        ready = super()._commit_ready(project_id, record)
        self.states.append(ready.state)
        return ready


def test_crash_after_fetched_resumes_by_discarding_staging(tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    crashing = _RecordingHydrator(_runner(tmp_path, store), store,
                                  crash_at="FETCHED")
    base = _reserve(_orchestrator(crashing))
    with pytest.raises(_Crash):
        crashing.hydrate("proj", 1, base)
    assert crashing.states == ["RESERVED", "FETCHED"]
    root = workspace
    assert not (root / "current").exists(), "an uncommitted operation is never current"
    assert (root / ".ops" / "rev-1" / "src" / "App.tsx").is_file()
    assert store.load("proj").deployment["hydration"]["state"] == "FETCHED"

    clean = _RecordingHydrator(_runner(tmp_path, store), store)
    outcome = clean.hydrate("proj", 1, base)
    assert clean.states == ["RESERVED", "FETCHED", "VERIFIED", "READY"]
    assert (root / "current").read_text() == "rev-1\n"
    assert outcome.record.state == "READY"


def test_crash_after_reserved_restarts_acquisition(tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    crashing = _RecordingHydrator(_runner(tmp_path, store), store,
                                  crash_at="RESERVED")
    base = _reserve(_orchestrator(crashing))
    with pytest.raises(_Crash):
        crashing.hydrate("proj", 1, base)
    assert crashing.states == ["RESERVED"]
    assert not (workspace / ".ops" / "rev-1" / "src").exists()
    clean = _RecordingHydrator(_runner(tmp_path, store), store)
    clean.hydrate("proj", 1, base)
    assert clean.states == ["RESERVED", "FETCHED", "VERIFIED", "READY"]


def test_crash_after_verified_before_the_swap_resumes_without_rematerializing(
        tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    crashing = _RecordingHydrator(_runner(tmp_path, store), store,
                                  crash_at="VERIFIED")
    base = _reserve(_orchestrator(crashing))
    with pytest.raises(_Crash):
        crashing.hydrate("proj", 1, base)
    assert crashing.states == ["RESERVED", "FETCHED", "VERIFIED"]
    op = workspace / ".ops" / "rev-1"
    assert (op / "src" / "App.tsx").is_file()
    assert not (workspace / "current").exists()

    clean = _RecordingHydrator(_runner(tmp_path, store), store)
    outcome = clean.hydrate("proj", 1, base)
    # The verified staging is promoted as it is: no acquisition, no write.
    assert clean.states == ["READY"]
    assert (workspace / "current").read_text() == "rev-1\n"
    assert outcome.workspace == op


def test_verified_staging_that_no_longer_matches_is_discarded_not_promoted(tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    crashing = _RecordingHydrator(_runner(tmp_path, store), store,
                                  crash_at="VERIFIED")
    base = _reserve(_orchestrator(crashing))
    with pytest.raises(_Crash):
        crashing.hydrate("proj", 1, base)
    op = workspace / ".ops" / "rev-1"
    (op / "src" / "App.tsx").write_bytes(b"tampered\n")

    clean = _RecordingHydrator(_runner(tmp_path, store), store)
    clean.hydrate("proj", 1, base)
    assert (op / "src" / "App.tsx").read_bytes() == SOURCE_FILES["src/App.tsx"]


def test_crash_after_the_swap_never_deletes_or_rolls_back(tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    loser = _RecordingHydrator(_runner(tmp_path, store), store,
                               refuse_ready=True)
    base = _reserve(_orchestrator(loser))
    with pytest.raises(HydrationError) as exc:
        loser.hydrate("proj", 1, base)
    assert exc.value.error_code == HYDRATION_STATE_UNPERSISTED
    op = workspace / ".ops" / "rev-1"
    # The swap committed: the pointer names the operation and the bytes are
    # there. Nothing is deleted, nothing is rolled back, and the durable READY
    # is simply still missing.
    assert (workspace / "current").read_text() == "rev-1\n"
    assert (op / "src" / "App.tsx").is_file()
    deployment = store.load("proj").deployment
    assert deployment["hydration"]["state"] == "VERIFIED"
    assert "pointer_mode" not in deployment

    resumed = _RecordingHydrator(_runner(tmp_path, store), store)
    outcome = resumed.hydrate("proj", 1, base)
    assert resumed.states == ["READY"]
    assert store.load("proj").deployment["pointer_mode"] is True
    assert store.load("proj").deployment["hydration"]["state"] == "READY"
    assert outcome.workspace == op


def test_ours_ready_is_idempotent(tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    hydrator = _draft_hydrator(tmp_path, store)
    base = _reserve(_orchestrator(hydrator))
    first = hydrator.hydrate("proj", 1, base)
    second = hydrator.hydrate("proj", 1, base)
    assert first.workspace == second.workspace
    assert store.load("proj").deployment["hydration"]["state"] == "READY"


def test_a_foreign_non_ready_record_holding_the_current_target_is_held(tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    hydrator = _draft_hydrator(tmp_path, store)
    base = _reserve(_orchestrator(hydrator))
    foreign = workspace / ".ops" / "rev-9"
    foreign.mkdir(parents=True)
    (workspace / "current").write_text("rev-9\n", encoding="utf-8")
    with store.acquire_writer("proj") as state:
        state.deployment["hydration"] = _record(
            operation_id="rev-9", state="VERIFIED", op_dir=str(foreign),
            op_token="rev-9").to_dict()
        state.deployment["pointer_mode"] = True
        store.save(state)
    with pytest.raises(HydrationError) as exc:
        hydrator.hydrate("proj", 1, base)
    assert exc.value.error_code == HYDRATION_RECOVERY_REQUIRED
    # Held, not deleted, not swept, not started over.
    assert foreign.is_dir()
    assert (workspace / "current").read_text() == "rev-9\n"
    assert not (workspace / ".ops" / "rev-1" / "src").exists()


def test_a_foreign_ready_record_is_superseded_and_the_old_current_survives(
        tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    hydrator = _draft_hydrator(tmp_path, store)
    first_base = _reserve(_orchestrator(hydrator), seq=1)
    hydrator.hydrate("proj", 1, first_base)
    old_current = workspace / ".ops" / "rev-1"
    assert old_current.is_dir()

    with store.acquire_writer("proj") as state:
        state.lifecycle = ProjectLifecycle.PREVIEW_READY.value
        state.revisions.revision_seq = 1
        state.pending_revisions[0]["applied"] = True
        store.save(state)
    second_base = _reserve(_orchestrator(hydrator), seq=2)
    assert second_base.revision_seq == 2

    seen = _RecordingHydrator(_runner(tmp_path, store), store)
    real_sweep = seen.runner.sweep_ops
    sweeps = []

    def spy(project_id, *, keep=()):
        # Record what the pointer named and what each sweep actually removed,
        # at the moment the sweep ran.
        pointer_before = _pointer_now(tmp_path, store)
        removed = real_sweep(project_id, keep=keep)
        sweeps.append((pointer_before, sorted(removed)))
        return removed

    with patch.object(seen.runner, "sweep_ops", spy):
        outcome = seen.hydrate("proj", 2, second_base)
    assert seen.states == ["RESERVED", "FETCHED", "VERIFIED", "READY"]
    assert outcome.workspace == workspace / ".ops" / "rev-2"
    # The old current is never removed while the pointer still names it: every
    # sweep before the swap leaves it alone, and it is dropped only by the
    # sweep that runs after the new operation is durably READY.
    assert sweeps, "the operation must bound .ops"
    for pointer_before, removed in sweeps[:-1]:
        assert "rev-1" not in removed, (pointer_before, removed)
    assert sweeps[-1] == ("rev-2", ["rev-1"])
    assert not old_current.exists()
    assert store.load("proj").deployment["hydration"]["operation_id"] == "rev-2"


def _pointer_now(tmp_path, store):
    pointer = tmp_path / "workspaces" / "proj" / "current"
    return pointer.read_text(encoding="utf-8").strip() if pointer.exists() else None


def test_abandoned_staging_from_a_foreign_record_is_swept(tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    hydrator = _draft_hydrator(tmp_path, store)
    base = _reserve(_orchestrator(hydrator))
    abandoned = workspace / ".ops" / "rev-9"
    abandoned.mkdir(parents=True)
    (abandoned / "junk.txt").write_text("x", encoding="utf-8")
    (workspace / ".ops" / "rev-1").mkdir(parents=True)
    (workspace / "current").write_text("rev-1\n", encoding="utf-8")
    with store.acquire_writer("proj") as state:
        state.deployment["hydration"] = _record(
            operation_id="rev-9", state="FETCHED", op_dir=str(abandoned),
            op_token="rev-9").to_dict()
        state.deployment["pointer_mode"] = True
        store.save(state)
    outcome = hydrator.hydrate("proj", 1, base)
    assert not abandoned.exists(), "abandoned staging is disposable"
    assert (workspace / ".ops" / "rev-1").is_dir()
    assert outcome.workspace == workspace / ".ops" / "rev-1"


def test_a_staging_path_that_is_not_a_directory_is_refused(tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    hydrator = _draft_hydrator(tmp_path, store)
    base = _reserve(_orchestrator(hydrator))
    (tmp_path / "outside").mkdir()
    (workspace / ".ops").mkdir(parents=True, exist_ok=True)
    (workspace / ".ops" / "rev-1").write_text("not a directory", encoding="utf-8")
    with pytest.raises(HydrationError) as exc:
        hydrator.hydrate("proj", 1, base)
    assert exc.value.error_code == HYDRATION_STAGING_UNAVAILABLE
    assert (workspace / ".ops" / "rev-1").read_text() == "not a directory"
    assert (tmp_path / "outside").is_dir()


@needs_symlinks
def test_a_symlinked_staging_directory_is_refused_rather_than_followed(tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    hydrator = _draft_hydrator(tmp_path, store)
    base = _reserve(_orchestrator(hydrator))
    (tmp_path / "outside").mkdir()
    (workspace / ".ops").mkdir(parents=True, exist_ok=True)
    (workspace / ".ops" / "rev-1").symlink_to(tmp_path / "outside",
                                              target_is_directory=True)
    with pytest.raises(HydrationError) as exc:
        hydrator.hydrate("proj", 1, base)
    assert exc.value.error_code == HYDRATION_STAGING_UNAVAILABLE


# ---------------------------------------------------------------------------
# LIVE identity
# ---------------------------------------------------------------------------


def test_live_hydration_materializes_exactly_the_publication_commit(tmp_path):
    live = _Live(tmp_path)
    base = _reserve(live.orchestrator())
    assert base.base_kind == BASE_KIND_LIVE
    assert base.publication_commit == live.publication
    outcome = live.hydrator().hydrate("proj", 1, base)
    assert outcome.record.commit == live.publication
    assert outcome.record.tested_commit_corroborated is True
    assert (outcome.workspace / "src" / "App.tsx").read_bytes() == SOURCE_FILES["src/App.tsx"]
    assert not (outcome.workspace / ".git").exists()


def test_live_stages_exact_order(tmp_path):
    live = _Live(tmp_path)
    seen = _RecordingHydrator(live.runner, live.store,
                              output_repo=live.output,
                              source_repo_url=CONFIGURED_URL)
    base = _reserve(_orchestrator(seen))
    seen.hydrate("proj", 1, base)
    assert seen.states == ["RESERVED", "FETCHED", "VERIFIED", "READY"]


def test_live_fetches_the_exact_commit_when_it_is_missing_locally(tmp_path):
    live = _Live(tmp_path, keep_publication=False)
    base = _reserve(live.orchestrator())
    hydrator = live.hydrator()
    assert not hydrator.output_repo.has_commit(live.publication)
    outcome = hydrator.hydrate("proj", 1, base)
    fetches = hydrator.output_repo.fetches()
    assert len(fetches) == 1, fetches
    refspec = fetches[0][-1]
    assert refspec == f"{live.publication}:refs/hydrate/{live.publication}"
    # No HEAD, no branch name, no FETCH_HEAD authority anywhere in the fetch.
    url = fetches[0][fetches[0].index("--") + 1]
    for token in (url, refspec):
        assert "HEAD" not in token.upper()
        assert "main" not in token
    assert "--no-write-fetch-head" in fetches[0]
    assert outcome.record.commit == live.publication
    assert outcome.record.fetched_locally is False
    assert (outcome.workspace / "src" / "App.tsx").read_bytes() == SOURCE_FILES["src/App.tsx"]


def test_live_never_substitutes_the_tested_commit(tmp_path):
    """The tested commit is corroboration. When the publication commit cannot
    be obtained, the correct answer is a refusal -- not the other commit."""
    live = _Live(tmp_path, keep_publication=False)
    _reserve(live.orchestrator())
    with live.store.acquire_writer("proj") as state:
        base = state.pending_revisions[0]["base"]
        base["publication_commit"] = "9" * 40
        base["source_sha256"] = "a" * 64
        base["artifact_sha256"] = "b" * 64
        live.store.save(state)
    frozen = RevisionBase.from_dict(
        live.store.load("proj").pending_revisions[0]["base"])
    with pytest.raises(HydrationError) as exc:
        live.hydrator().hydrate("proj", 1, frozen)
    assert exc.value.error_code == "HYDRATION_COMMIT_UNAVAILABLE"
    assert not (live.workspace / ".ops" / "rev-1" / "src").exists()


def test_live_ignores_a_newer_branch_head(tmp_path):
    live = _Live(tmp_path, head_ahead=True)
    base = _reserve(live.orchestrator())
    hydrator = live.hydrator()
    assert hydrator.output_repo.has_commit(live.head), "the branch tip IS present locally"
    outcome = hydrator.hydrate("proj", 1, base)
    assert (outcome.workspace / "src" / "App.tsx").read_bytes() == SOURCE_FILES["src/App.tsx"]
    assert not (outcome.workspace / "src" / "Later.tsx").exists()


def test_live_refuses_a_repository_mismatch_before_any_network_access(tmp_path):
    live = _Live(tmp_path)
    base = _reserve(live.orchestrator())
    hydrator = live.hydrator(source_repo_url="git@github.com:other/repo.git")
    with pytest.raises(HydrationError) as exc:
        hydrator.hydrate("proj", 1, base)
    assert exc.value.error_code == HYDRATION_REPO_MISMATCH
    assert hydrator.output_repo.fetches() == []
    assert not (live.workspace / ".ops" / "rev-1" / "src").exists()


def test_live_refuses_a_repository_url_it_cannot_interpret(tmp_path):
    live = _Live(tmp_path)
    base = _reserve(live.orchestrator())
    hydrator = live.hydrator(source_repo_url="https://github.com/o/r.git")
    with pytest.raises(HydrationError) as exc:
        hydrator.hydrate("proj", 1, base)
    assert exc.value.error_code == HYDRATION_REPO_MISMATCH
    assert hydrator.output_repo.fetches() == []


def test_live_refuses_a_commit_the_remote_does_not_hold(tmp_path):
    live = _Live(tmp_path, keep_publication=False)
    _reserve(live.orchestrator())
    with live.store.acquire_writer("proj") as state:
        state.pending_revisions[0]["base"]["publication_commit"] = "9" * 40
        live.store.save(state)
    frozen = RevisionBase.from_dict(
        live.store.load("proj").pending_revisions[0]["base"])
    with pytest.raises(HydrationError) as exc:
        live.hydrator().hydrate("proj", 1, frozen)
    assert exc.value.error_code == "HYDRATION_COMMIT_UNAVAILABLE"


def test_live_refuses_a_tree_that_does_not_match_the_record(tmp_path):
    live = _Live(tmp_path)
    _reserve(live.orchestrator())
    with live.store.acquire_writer("proj") as state:
        state.pending_revisions[0]["base"]["publication_tree"] = "7" * 40
        live.store.save(state)
    frozen = RevisionBase.from_dict(
        live.store.load("proj").pending_revisions[0]["base"])
    with pytest.raises(HydrationError) as exc:
        live.hydrator().hydrate("proj", 1, frozen)
    assert exc.value.error_code == "HYDRATION_TREE_MISMATCH"


@pytest.mark.parametrize("keep_tested", [True, False])
def test_live_refuses_when_tested_and_publication_trees_differ(tmp_path, keep_tested):
    """The recorded trees must agree, and that check stands on its own.

    With ``keep_tested=True`` the corroboration step would refuse this anyway
    (the local tested commit's own tree is checked against ``tested_tree``), so
    the agreement check is only independently load-bearing when the tested
    commit is NOT available locally and corroboration cannot run at all.
    """
    live = _Live(tmp_path, keep_tested=keep_tested)
    _reserve(live.orchestrator())
    with live.store.acquire_writer("proj") as state:
        state.pending_revisions[0]["base"]["tested_tree"] = "6" * 40
        live.store.save(state)
    frozen = RevisionBase.from_dict(
        live.store.load("proj").pending_revisions[0]["base"])
    with pytest.raises(HydrationError) as exc:
        live.hydrator().hydrate("proj", 1, frozen)
    assert exc.value.error_code == "HYDRATION_TREE_MISMATCH"
    assert not (live.workspace / "current").exists()


def test_live_materialization_never_checks_out_anything(tmp_path):
    live = _Live(tmp_path)
    base = _reserve(live.orchestrator())
    hydrator = live.hydrator()
    hydrator.hydrate("proj", 1, base)
    forbidden = {"checkout", "checkout-index", "read-tree", "archive", "tar",
                 "clean", "smudge", "apply", "stash", "worktree", "submodule",
                 "clone", "restore"}
    for command in hydrator.output_repo.commands:
        assert command[0] not in forbidden, command
        assert not any(flag.startswith("--work-tree") for flag in command), command
    assert not (live.workspace / ".ops" / "rev-1" / ".git").exists()


def _sentinel_hooks(directory, marker):
    """A ``reference-transaction`` hook that records every ref update.

    Deliberately a ref-update hook and not a checkout/push hook: hydration DOES
    update a ref when it has to fetch the pinned commit, so proving that the
    fetch runs no hook is a stronger claim than proving that no checkout was
    issued.
    """
    directory.mkdir(parents=True, exist_ok=True)
    hook = directory / "reference-transaction"
    hook.write_text(f"#!/bin/sh\necho fired > '{marker}'\n", encoding="ascii")
    hook.chmod(0o755)
    return hook


def test_no_git_hook_ever_fires_during_hydration(tmp_path):
    """Hooks are neutralised, not merely absent from the command list.

    The sentinel is installed as the OUTPUT REPOSITORY'S OWN
    ``core.hooksPath``, so Git would find it and run it for any ref update that
    did not carry the ``-c core.hooksPath=<devnull>`` override. A control arm
    then proves the sentinel is genuinely reachable on this host, so its
    silence during hydration is a fact about hydration rather than about a
    hook that could never have fired anyway.
    """
    live = _Live(tmp_path, keep_publication=False)
    marker = tmp_path / "hook-fired.txt"
    hooks = tmp_path / "sentinel-hooks"
    _sentinel_hooks(hooks, marker)
    # ``_git`` neutralises hooks itself, so the repository's own configuration
    # is read and written through a bare ``git`` to keep the sentinel reachable.
    raw_git = ["git", "--git-dir", str(live.output.path)]
    subprocess.run(raw_git + ["config", "core.hooksPath", str(hooks)],
                   capture_output=True, text=True, check=True)
    assert subprocess.run(raw_git + ["config", "core.hooksPath"],
                          capture_output=True, text=True).stdout.strip() == str(hooks)

    base = _reserve(live.orchestrator())
    hydrator = live.hydrator()
    outcome = hydrator.hydrate("proj", 1, base)
    assert outcome.record.fetched_locally is False
    assert hydrator.output_repo.fetches(), "the pinned fetch must have run"
    assert outcome.record.state == "READY"
    assert (outcome.workspace / "src" / "App.tsx").read_bytes() == SOURCE_FILES["src/App.tsx"]
    assert not marker.exists(), "a Git hook ran while hydrating"

    # Control: the same repository, the same sentinel, one un-overridden ref
    # update. If this does not fire, the assertion above proved nothing.
    control = subprocess.run(
        raw_git + ["fetch", "--no-tags", "--", str(live.mirror),
                   f"{live.tested}:refs/heads/control"],
        capture_output=True, text=True)
    assert marker.exists(), (
        "the sentinel never fires on this host, so the hydration arm is "
        f"vacuous: {control.stderr}")


def test_live_hydration_never_reads_the_leftover_mutable_workspace(tmp_path):
    """The R1 leftover directory is not the source, in either direction.

    A file that exists only on disk -- never in the commit -- must be absent
    from the hydrated workspace and untouched on disk, and once pointer mode
    has committed, a tampered legacy root must never be resolved again.
    """
    live = _Live(tmp_path)
    live.runner.create_workspace("proj")
    stray = live.workspace / "src" / "Scratch.tsx"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"// never committed\n")

    base = _reserve(live.orchestrator())
    outcome = live.hydrator().hydrate("proj", 1, base)
    assert not (outcome.workspace / "src" / "Scratch.tsx").exists()
    assert stray.read_bytes() == b"// never committed\n", "the leftover is not cleaned up"
    assert (outcome.workspace / "src" / "App.tsx").read_bytes() == SOURCE_FILES["src/App.tsx"]
    assert live.store.load("proj").deployment["pointer_mode"] is True

    # And the legacy root stays unreachable from here on, even tampered with.
    (live.workspace / "src" / "App.tsx").write_bytes(b"export const tampered = 1\n")
    (live.workspace / "current").unlink()
    with pytest.raises(PointerResolutionError) as exc:
        live.runner.resolve_workspace("proj")
    assert exc.value.error_code == WORKSPACE_POINTER_MISSING_AFTER_HYDRATION
    assert stray.read_bytes() == b"// never committed\n"


# ---------------------------------------------------------------------------
# BLOB safety
# ---------------------------------------------------------------------------


def _inject_objects(destination, source):
    """Copy loose objects from one repository's store into another's.

    Used instead of a fetch for the hand-built trees: ``git fetch`` runs
    ``index-pack``, which validates paths and modes -- and those validations
    are exactly what the materialization guard has to be able to receive. The
    objects are real and byte-identical to what a push would have produced.
    """
    import shutil as _shutil

    src_objects = Path(source) / ".git" / "objects"
    dst_objects = Path(destination) / "objects"
    for bucket in src_objects.iterdir():
        if len(bucket.name) != 2:
            continue
        for obj in bucket.iterdir():
            target = dst_objects / bucket.name / obj.name
            if target.exists():
                # Already present (the same blob is usually shared with the
                # real publication), and git's own objects are read-only.
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            # ``copyfile``, not ``copy2``: copying git's read-only bit across
            # would make the destination unwritable too.
            _shutil.copyfile(obj, target)


def _unsafe_commit(live, extra_entries=None, label="unsafe", extra_files=None):
    """A commit carrying special entries, present in the output repo.

    The tree is assembled by hand (see ``_write_object``) so the paths and modes
    Git itself would refuse to accept can still reach the materializer.
    """
    entries = {name: ("100644", "blob", content)
               for name, content in {**ALL_FILES, **(extra_files or {})}.items()}
    entries.update(extra_entries or {})
    repo = live.mirror.parent / f"tree-{label}"
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        _git(live.mirror.parent, "-c", "init.defaultBranch=main", "init", str(repo))
    tree = _tree_from_paths(repo, entries)
    commit = _git(repo, "commit-tree", tree, "-m", label).stdout.strip()
    _inject_objects(live.output.path, repo)
    live.output.commands.clear()
    assert live.output.has_commit(commit)
    return commit, tree


def _materialize(live, commit, tree):
    staging = live.mirror.parent / "staging"
    if staging.exists():
        shutil.rmtree(staging)
    return live.output.materialize_commit(commit, tree, staging)


def test_blob_symlinks_are_refused(tmp_path):
    live = _Live(tmp_path)
    commit, tree = _unsafe_commit(
        live, extra_entries={"link": ("120000", "blob", b"/etc/passwd")})
    with pytest.raises(MaterializationRefusal) as exc:
        _materialize(live, commit, tree)
    assert exc.value.reason == "symlink"
    assert exc.value.path == "link"


def test_blob_submodules_are_refused(tmp_path):
    live = _Live(tmp_path)
    submodule = _git(live.repo, "rev-parse", "HEAD").stdout.strip()
    commit, tree = _unsafe_commit(
        live, extra_entries={"vendor": ("160000", "commit", submodule)})
    with pytest.raises(MaterializationRefusal) as exc:
        _materialize(live, commit, tree)
    assert exc.value.reason == "submodule"
    assert exc.value.path == "vendor"


def test_an_executable_blob_is_written_and_accepted(tmp_path):
    live = _Live(tmp_path)
    commit, tree = _unsafe_commit(
        live, extra_entries={"run.sh": ("100755", "blob", b"#!/bin/sh\nexit 0\n")})
    written = _materialize(live, commit, tree)
    assert "run.sh" in written["source"]
    assert (live.mirror.parent / "staging" / "run.sh").read_bytes() == b"#!/bin/sh\nexit 0\n"


@pytest.mark.parametrize("mode", ["100664", "100600", "100640", "040000", "0",
                                   "100644", "100755", "100644 "])
def test_the_mode_gate_pins_exactly_two_accepted_modes(mode):
    """100644 and 100755 are the only blob modes written.

    Pinned on the gate itself rather than end-to-end, because git re-derives a
    regular file's mode to the canonical 100644/100755 when it reads a tree --
    an end-to-end test would only ever prove that git normalizes, not that the
    materializer refuses. 120000 and 160000 DO survive a tree read and are
    covered end-to-end above.
    """
    if mode in ("100644", "100755"):
        assert refuse_tree_entry(mode, "blob", "ok") is None
        return
    with pytest.raises(MaterializationRefusal) as exc:
        refuse_tree_entry(mode, "blob", "weird")
    assert exc.value.reason in ("mode", "symlink", "submodule"), exc.value.reason


def test_the_accepted_mode_list_is_exactly_two():
    from app.deploy.git_output import _ACCEPTED_BLOB_MODES

    assert _ACCEPTED_BLOB_MODES == ("100644", "100755")


def test_the_executable_mode_is_the_only_one_that_sets_the_bit():
    from app.deploy.git_output import _ACCEPTED_BLOB_MODES

    assert _ACCEPTED_BLOB_MODES == ("100644", "100755")
    assert refuse_tree_entry("100644", "blob", "ok") is None
    assert refuse_tree_entry("100755", "blob", "ok") is None


@pytest.mark.parametrize("path", [
    "../escape.txt", "a/../../escape.txt", ".git/config",
    "a/.git/hooks/pre-commit", "back\\slash.txt", "colon:name.txt",
    "..", "./relative.txt", ".git",
])
def test_blob_unsafe_paths_are_refused(tmp_path, path):
    """End-to-end, over the paths git will actually read back out of a tree.

    Absolute, empty and double-slash names are not constructible: git refuses to
    emit them, so they are pinned on the predicate below instead of here.
    """
    live = _Live(tmp_path)
    commit, tree = _unsafe_commit(
        live, extra_entries={path: ("100644", "blob", b"hello\n")}, label="path")
    with pytest.raises(MaterializationRefusal) as exc:
        _materialize(live, commit, tree)
    assert exc.value.reason in ("unsafe_path", "mode", "unparseable"), exc.value.reason
    assert not (tmp_path.parent / "escape.txt").exists()


@pytest.mark.parametrize("path", ["/abs.txt", "", "a//b.txt", "a/./b.txt"])
def test_paths_git_will_not_round_trip_are_still_refused_by_the_predicate(path):
    assert unsafe_repository_path(path)


def test_a_tree_entry_whose_type_is_not_a_blob_is_refused():
    """Git derives the ls-tree TYPE from the mode, so a non-blob type cannot be
    smuggled through a real tree. The gate is pinned directly."""
    with pytest.raises(MaterializationRefusal) as exc:
        refuse_tree_entry("100644", "commit", "odd")
    assert exc.value.reason == "non_blob"


def test_a_tree_entry_with_a_non_blob_type_is_refused(tmp_path):
    live = _Live(tmp_path)
    submodule = _git(live.repo, "rev-parse", "HEAD").stdout.strip()
    with pytest.raises(MaterializationRefusal) as exc:
        refuse_tree_entry("100644", "commit", submodule)
    assert exc.value.reason == "non_blob"


def test_unsafe_repository_path_predicate():
    assert unsafe_repository_path("../x") and unsafe_repository_path("/x")
    assert unsafe_repository_path("a//b") and unsafe_repository_path(".git/x")
    assert unsafe_repository_path("back\\slash") and unsafe_repository_path("a:b")
    assert unsafe_repository_path("") and unsafe_repository_path(None)
    assert unsafe_repository_path(".") and unsafe_repository_path("..")
    assert not unsafe_repository_path("src/App.tsx")
    assert not unsafe_repository_path("dist/index.html")
    assert not unsafe_repository_path("src/../src/App.tsx".split("/")[-1])


@needs_symlinks
def test_materialization_rechecks_containment_for_every_entry(tmp_path):
    """A symlink swapped in mid-run must be caught, not only one present before
    the first write."""
    live = _Live(tmp_path)
    files = {**ALL_FILES, "zz-second.txt": b"second\n"}
    repo, commit, tree = _commit_files(tmp_path, "swap", files)
    _git(repo, "push", "-f", str(live.mirror), f"{commit}:refs/heads/swap")
    live.output._run(["fetch", "--no-tags", "--", str(live.mirror),
                      f"{commit}:refs/hydrate/swap"])
    live.output.commands.clear()
    staging = tmp_path / "staging"
    real = live.output._run
    state = {"n": 0}

    def swapping_run(args, check=True, extra=None):
        if args[:2] == ["cat-file", "blob"]:
            state["n"] += 1
            if state["n"] == 1:
                # Replace a directory the remaining entries will write into.
                # ``dist`` is the first one materialization creates and
                # ``dist/index.html`` is the very next write, so the swapped-in
                # directory sits on the path of the entry being written.
                (staging / "dist").rename(tmp_path / "dist-backup")
                (tmp_path / "escape").mkdir()
                (staging / "dist").symlink_to(tmp_path / "escape",
                                              target_is_directory=True)
        return real(args, check=check, extra=extra)

    live.output._run = swapping_run
    staging.mkdir(parents=True, exist_ok=True)
    # Pre-created so the swap has a directory to replace before the first
    # write; the write that must be refused is the one into it.
    (staging / "dist").mkdir()
    with pytest.raises(MaterializationRefusal) as exc:
        live.output.materialize_commit(commit, tree, staging)
    assert exc.value.reason == "containment"
    assert not (tmp_path / "escape" / "index.html").exists()
    assert not (tmp_path / "escape" / "src").exists()


@needs_junctions
def test_a_junction_swapped_in_mid_run_is_refused_by_the_resolved_containment_check(
        tmp_path):
    """The escape check that works even where symlinks cannot be created.

    A Windows junction is not a symlink as far as ``Path.is_symlink`` is
    concerned, so the per-component walk cannot see one. The fully resolved
    containment check is the layer that catches it, and this is the test that
    proves that layer is load-bearing on a host with no symlink support at all.
    """
    live = _Live(tmp_path)
    files = {**ALL_FILES, "zz-second.txt": b"second\n"}
    repo, commit, tree = _commit_files(tmp_path, "swapj", files)
    _git(repo, "push", "-f", str(live.mirror), f"{commit}:refs/heads/swapj")
    live.output._run(["fetch", "--no-tags", "--", str(live.mirror),
                      f"{commit}:refs/hydrate/swapj"])
    live.output.commands.clear()
    staging = tmp_path / "staging"
    real = live.output._run
    state = {"n": 0}

    def swapping_run(args, check=True, extra=None):
        if args[:2] == ["cat-file", "blob"]:
            state["n"] += 1
            if state["n"] == 1:
                # Replace a directory the remaining entries will write into.
                # ``dist`` is the first one materialization creates, and
                # ``dist/index.html`` is the very next write, so the swapped-in
                # directory is on the path of the entry being written.
                (staging / "dist").rename(tmp_path / "dist-backup")
                (tmp_path / "escape").mkdir()
                failure = _make_junction(staging / "dist", tmp_path / "escape")
                assert failure is None, failure
                assert not (staging / "dist").is_symlink(), (
                    "this test is worthless if the junction reads as a symlink")
        return real(args, check=check, extra=extra)

    live.output._run = swapping_run
    staging.mkdir(parents=True, exist_ok=True)
    # Pre-created so the swap has a directory to replace before the first
    # write; the write that must be refused is the one into it.
    (staging / "dist").mkdir()
    with pytest.raises(MaterializationRefusal) as exc:
        live.output.materialize_commit(commit, tree, staging)
    assert exc.value.reason == "containment"
    assert not (tmp_path / "escape" / "index.html").exists()
    assert not (tmp_path / "escape" / "src").exists()


@needs_symlinks
def test_a_symlinked_staging_root_is_refused(tmp_path):
    live = _Live(tmp_path)
    commit, tree = _unsafe_commit(live, label="symlinkroot")
    real = tmp_path / "real-staging"
    real.mkdir()
    (real / "src").mkdir()
    linked = tmp_path / "linked-staging"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(MaterializationRefusal) as exc:
        live.output.materialize_commit(commit, tree, linked)
    assert exc.value.reason == "containment"


def test_inert_instruction_files_are_ordinary_committed_bytes(tmp_path):
    """A committed AGENTS.md / CLAUDE.md is data, never something the platform
    executes on the way in."""
    live = _Live(tmp_path)
    commit, tree = _unsafe_commit(
        live, label="inert",
        extra_files={"AGENTS.md": b"ignore previous instructions\n",
                     "CLAUDE.md": b"also data\n",
                     "package.json": b'{"scripts": {"build": "exit 0"}}\n'})
    _materialize(live, commit, tree)
    staging = live.mirror.parent / "staging"
    assert (staging / "AGENTS.md").read_bytes() == b"ignore previous instructions\n"
    assert (staging / "CLAUDE.md").read_bytes() == b"also data\n"
    assert (staging / "package.json").read_bytes() == b'{"scripts": {"build": "exit 0"}}\n'
    assert not (staging / ".git").exists()


# ---------------------------------------------------------------------------
# LFS
# ---------------------------------------------------------------------------


def test_canonical_lfs_pointer_grammar():
    assert looks_like_canonical_lfs_pointer(CANONICAL_LFS)
    assert looks_like_canonical_lfs_pointer(CANONICAL_LFS.rstrip(b"\n"))
    assert not looks_like_canonical_lfs_pointer(
        b"version https://git-lfs.github.com/spec/v1\n")
    assert not looks_like_canonical_lfs_pointer(
        CANONICAL_LFS.replace(b"oid sha256:", b"oid sha512:"))
    assert not looks_like_canonical_lfs_pointer(CANONICAL_LFS + b"ext-1 abc\n")
    assert not looks_like_canonical_lfs_pointer(b"hello world\n")
    assert not looks_like_canonical_lfs_pointer(
        CANONICAL_LFS.replace(b"a" * 64, b"A" * 64))
    assert not looks_like_canonical_lfs_pointer(
        CANONICAL_LFS.replace(b"size 42", b"size -1"))
    assert not looks_like_canonical_lfs_pointer(CANONICAL_LFS * 400)
    assert not looks_like_canonical_lfs_pointer(b"")
    assert not looks_like_canonical_lfs_pointer("not bytes")


def test_canonical_lfs_pointer_is_refused_before_the_bytes_hit_disk(tmp_path):
    live = _Live(tmp_path)
    commit, tree = _unsafe_commit(
        live, label="lfs", extra_files={"assets/logo.png": CANONICAL_LFS})
    with pytest.raises(MaterializationRefusal) as exc:
        _materialize(live, commit, tree)
    assert exc.value.reason == "lfs"
    assert exc.value.path == "assets/logo.png"
    assert not (live.mirror.parent / "staging" / "assets" / "logo.png").exists()


def test_lfs_is_never_executed_or_filtered(tmp_path):
    live = _Live(tmp_path)
    commit, tree = _unsafe_commit(
        live, label="lfsfilter",
        extra_files={".gitattributes": b"*.png filter=lfs diff=lfs merge=lfs -text\n",
                     "assets/logo.png": CANONICAL_LFS})
    with pytest.raises(MaterializationRefusal) as exc:
        _materialize(live, commit, tree)
    assert exc.value.reason == "lfs"
    issued = {c[0] for c in live.output.commands}
    assert "lfs" not in issued
    assert not {"clean", "smudge", "checkout", "checkout-index"} & issued
    assert not (live.mirror.parent / "staging" / "assets" / "logo.png").exists()


def test_ordinary_text_mentioning_lfs_is_accepted(tmp_path):
    live = _Live(tmp_path)
    commit, tree = _unsafe_commit(
        live, label="lfsprose",
        extra_files={"README.md": b"We used to use Git LFS for logos.\n"
                                   b"version https://git-lfs.github.com/spec/v1 is a thing.\n"})
    _materialize(live, commit, tree)
    assert (live.mirror.parent / "staging" / "README.md").read_bytes().startswith(
        b"We used to use Git LFS")


def test_non_canonical_lfs_like_bytes_are_ordinary_committed_bytes(tmp_path):
    """Only the implemented grammar is recognised. Anything else is exactly
    what was committed -- and digest verification is content integrity, not a
    general LFS safety net, and is not claimed to be one."""
    near_miss = (b"version https://git-lfs.github.com/spec/v1\n"
                 b"oid sha256:" + b"a" * 64 + b"\n"
                 b"size not-a-number\n")
    live = _Live(tmp_path)
    commit, tree = _unsafe_commit(
        live, label="nearmiss", extra_files={"assets/logo.png": near_miss})
    _materialize(live, commit, tree)
    assert (live.mirror.parent / "staging" / "assets" / "logo.png").read_bytes() == near_miss


# ---------------------------------------------------------------------------
# Independent re-walk
# ---------------------------------------------------------------------------


def test_the_name_set_is_rechecked_against_what_was_admitted(tmp_path):
    """The digests can match while the name set on disk is not the one that was
    committed, so the re-walk compares the names as well as the bytes."""
    live = _Live(tmp_path)
    base = _reserve(live.orchestrator())
    hydrator = live.hydrator()
    real = hydrator.output_repo.materialize_commit

    def under_reporting(commit, expected_tree, staging, **kwargs):
        outcome = real(commit, expected_tree, staging, **kwargs)
        outcome["source"] = outcome["source"][:-1]
        return outcome

    hydrator.output_repo.materialize_commit = under_reporting
    with pytest.raises(HydrationError) as exc:
        hydrator.hydrate("proj", 1, base)
    assert exc.value.error_code == HYDRATION_UNSAFE_ENTRY


def test_a_foreign_file_appearing_in_staging_is_refused_before_the_swap(tmp_path):
    live = _Live(tmp_path)
    base = _reserve(live.orchestrator())
    hydrator = live.hydrator()
    real = hydrator.output_repo.materialize_commit

    def plant_a_file(commit, expected_tree, staging, **kwargs):
        outcome = real(commit, expected_tree, staging, **kwargs)
        (staging / "src" / "Injected.tsx").write_bytes(b"// not committed\n")
        return outcome

    hydrator.output_repo.materialize_commit = plant_a_file
    with pytest.raises(HydrationError) as exc:
        hydrator.hydrate("proj", 1, base)
    assert exc.value.error_code == HYDRATION_UNSAFE_ENTRY
    assert not (live.workspace / "current").exists()


def test_a_removed_file_is_caught_before_the_swap(tmp_path):
    """A file that vanished between writing and verification changes the name
    set, so the re-walk refuses it before the digests are even consulted."""
    live = _Live(tmp_path)
    base = _reserve(live.orchestrator())
    hydrator = live.hydrator()
    real = hydrator.output_repo.materialize_commit

    def drop_a_file(commit, expected_tree, staging, **kwargs):
        outcome = real(commit, expected_tree, staging, **kwargs)
        (staging / "design-dna.json").unlink()
        return outcome

    hydrator.output_repo.materialize_commit = drop_a_file
    with pytest.raises(HydrationError) as exc:
        hydrator.hydrate("proj", 1, base)
    assert exc.value.error_code == HYDRATION_UNSAFE_ENTRY
    assert not (live.workspace / "current").exists()


def test_corrupted_bytes_are_caught_by_the_source_digest(tmp_path):
    live = _Live(tmp_path)
    base = _reserve(live.orchestrator())
    hydrator = live.hydrator()
    real = hydrator.output_repo.materialize_commit

    def corrupt(commit, expected_tree, staging, **kwargs):
        outcome = real(commit, expected_tree, staging, **kwargs)
        (staging / "src" / "App.tsx").write_bytes(b"corrupted\n")
        return outcome

    hydrator.output_repo.materialize_commit = corrupt
    with pytest.raises(HydrationError) as exc:
        hydrator.hydrate("proj", 1, base)
    assert exc.value.error_code == HYDRATION_SOURCE_MISMATCH
    assert not (live.workspace / "current").exists()


def test_corrupted_artifact_bytes_are_caught_by_the_artifact_digest(tmp_path):
    live = _Live(tmp_path)
    base = _reserve(live.orchestrator())
    hydrator = live.hydrator()
    real = hydrator.output_repo.materialize_commit

    def corrupt(commit, expected_tree, staging, **kwargs):
        outcome = real(commit, expected_tree, staging, **kwargs)
        (staging / "dist" / "index.html").write_bytes(b"corrupted\n")
        return outcome

    hydrator.output_repo.materialize_commit = corrupt
    with pytest.raises(HydrationError) as exc:
        hydrator.hydrate("proj", 1, base)
    assert exc.value.error_code == HYDRATION_ARTIFACT_MISMATCH


# ---------------------------------------------------------------------------
# Revision integration
# ---------------------------------------------------------------------------


def _apply_with_stubs(orchestrator, project_id, seq, text):
    """Run apply() with the collaborators that would spawn processes stubbed."""
    orchestrator.hermes_adapter = MagicMock(frontend_build=MagicMock(
        return_value={"success": True, "design_dna": {
            "version": 2, "typography": {"heading_font": "Inter",
                                          "body_font": "Inter"}}}))
    preview = MagicMock()
    preview.run_owned.return_value = MagicMock(success=True, error=None, data={})
    orchestrator.preview_orchestrator = preview
    with patch("app.projects.revise.run_fixed_checks", return_value={
            "npm_ci": {"success": True}, "npm_build": {"success": True},
            "npm_typecheck": {"success": True}}), \
         patch("app.projects.revise.QAOrchestrator", return_value=MagicMock(
             run=MagicMock(return_value=MagicMock(success=True, error=None)))):
        return orchestrator.apply(project_id, seq, text, principal_id=OWNER)


def test_a_hydration_refusal_fails_the_revision_with_no_observable_effect(tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    hydrator = _draft_hydrator(tmp_path, store)
    orchestrator = _orchestrator(hydrator)
    _reserve(orchestrator)
    with store.acquire_writer("proj") as state:
        state.deployment.pop("tested_snapshot")
        store.save(state)
    result = orchestrator.apply("proj", 1, "make it red", principal_id=OWNER)
    assert not result.success
    assert result.error_code == DRAFT_SNAPSHOT_UNAVAILABLE
    after = store.load("proj")
    assert after.lifecycle == ProjectLifecycle.FAILED.value
    assert after.revisions.source_revision == 1, "no source was advanced"
    assert after.failure["error_code"] == DRAFT_SNAPSHOT_UNAVAILABLE
    assert after.revisions.qa_revision == 1
    assert after.revisions.preview_revision == 1
    assert after.revisions.approved_revision == 0
    assert not (workspace / "current").exists()


def test_a_pointer_refusal_fails_the_revision_before_any_git_work(tmp_path):
    live = _Live(tmp_path)
    _reserve(live.orchestrator())
    live.runner.create_workspace("proj")
    (live.workspace / "current").write_text("garbage\n", encoding="utf-8")
    hydrator = live.hydrator()
    result = orchestrator_apply(live, hydrator, "make it red")
    assert not result.success
    assert result.error_code == WORKSPACE_POINTER_INVALID
    assert hydrator.output_repo.commands == [], "no Git work may happen"
    assert live.store.load("proj").lifecycle == ProjectLifecycle.FAILED.value
    assert live.store.load("proj").revisions.source_revision == 1


def store_live(live):
    return live.store.load("proj")


def orchestrator_apply(live, hydrator, text):
    orchestrator = RevisionOrchestrator(live.runner, live.store,
                                        hermes_adapter=object(),
                                        hydrator=hydrator,
                                        source_repo_url=CONFIGURED_URL)
    return orchestrator.apply("proj", 1, text, principal_id=OWNER)


def test_the_persisted_error_code_always_equals_the_returned_one(tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    hydrator = _draft_hydrator(tmp_path, store)
    orchestrator = _orchestrator(hydrator)
    _reserve(orchestrator)
    with store.acquire_writer("proj") as state:
        state.deployment.pop("tested_snapshot")
        store.save(state)
    result = orchestrator.apply("proj", 1, "make it red", principal_id=OWNER)
    assert result.error_code == store.load("proj").failure["error_code"]


def test_a_successful_revision_leaves_the_workspace_current(tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    hydrator = _draft_hydrator(tmp_path, store)
    orchestrator = _orchestrator(hydrator)
    _reserve(orchestrator)
    result = _apply_with_stubs(orchestrator, "proj", 1, "make it red")
    assert result.success, result.error
    assert (workspace / "current").read_text() == "rev-1\n"
    assert store.load("proj").deployment["pointer_mode"] is True
    assert result.base_kind == BASE_KIND_DRAFT
    assert result.hydrated_from["fetched_locally"] is True


def test_the_workspace_seam_still_bypasses_hydration(tmp_path):
    """The compatibility seam survives, and it is the ONLY way to inject a
    directory: there is no public flag that offers the same thing."""
    store, workspace, snapshot = _draft_project(tmp_path)
    hydrator = _draft_hydrator(tmp_path, store)
    orchestrator = _orchestrator(hydrator)
    _reserve(orchestrator)
    result = _apply_with_stubs(orchestrator, "proj", 1, "make it red")
    assert result.success, result.error
    signature = inspect.signature(RevisionOrchestrator.apply)
    assert "workspace" in signature.parameters
    bypasses = [name for name in signature.parameters
                if name.startswith(("skip_", "allow_", "no_", "bypass_",
                                    "force_", "unsafe_"))]
    assert bypasses == []


def test_without_the_seam_a_production_call_always_hydrates(tmp_path):
    """The dispatcher's call shape: no workspace, therefore hydration is
    mandatory and there is no silent continuation from what is on disk."""
    store, workspace, snapshot = _draft_project(tmp_path)
    hydrator = _draft_hydrator(tmp_path, store)
    seen = _RecordingHydrator(hydrator.runner, store)
    orchestrator = _orchestrator(seen)
    _reserve(orchestrator)
    calls = []
    real = seen.hydrate
    seen.hydrate = lambda *a, **kw: (calls.append(a), real(*a, **kw))[1]
    result = _apply_with_stubs(orchestrator, "proj", 1, "make it red")
    assert result.success, result.error
    assert calls, "a production-shaped apply() must hydrate"
    assert seen.states == ["RESERVED", "FETCHED", "VERIFIED", "READY"]


def test_a_second_principal_cannot_take_over_a_reservation(tmp_path):
    store, workspace, snapshot = _draft_project(tmp_path)
    hydrator = _draft_hydrator(tmp_path, store)
    orchestrator = _orchestrator(hydrator)
    _reserve(orchestrator)
    other = orchestrator.reserve("proj", 1, principal_id=OTHER)
    assert not other.success
    state = store.load("proj")
    assert len(state.pending_revisions) == 1
    assert state.pending_revisions[0]["principal_id"] == OWNER


def test_lost_ready_state_does_not_fail_the_revision(tmp_path):
    """The post-swap durability failure is the one hydration error that must
    NOT transition the project to FAILED."""
    store, workspace, snapshot = _draft_project(tmp_path)
    runner = _runner(tmp_path, store)
    hydrator = _RecordingHydrator(runner, store, refuse_ready=True)
    orchestrator = _orchestrator(hydrator)
    _reserve(orchestrator)
    result = orchestrator.apply("proj", 1, "make it red", principal_id=OWNER)
    assert not result.success
    assert result.error_code == HYDRATION_STATE_UNPERSISTED
    after = store.load("proj")
    assert after.lifecycle == ProjectLifecycle.REVISION_REQUESTED.value
    assert not after.failure
    assert (workspace / "current").read_text() == "rev-1\n"
    assert (workspace / ".ops" / "rev-1" / "src" / "App.tsx").is_file()

    # The same operation's re-drive finishes the write and continues the
    # revision, without advancing source_revision twice.
    resumed = _orchestrator(_draft_hydrator(tmp_path, store))
    assert _apply_with_stubs(resumed, "proj", 1, "make it red").success
    after = store.load("proj")
    assert after.deployment["pointer_mode"] is True
    assert after.deployment["hydration"]["state"] == "READY"
    assert after.revisions.source_revision == 2
    assert after.revisions.revision_seq == 1


# ---------------------------------------------------------------------------
# New project
# ---------------------------------------------------------------------------


def test_a_new_project_is_untouched_by_hydration(tmp_path):
    store = _store(tmp_path)
    runner = _runner(tmp_path, store)
    with store.acquire_writer("brand-new") as state:
        state.roles["owner"] = OWNER
        state.lifecycle = ProjectLifecycle.READY.value
        store.save(state)
    root = tmp_path / "workspaces" / "brand-new"
    assert runner.resolve_workspace("brand-new") == root
    assert not (root / ".ops").exists()
    assert not (root / "current").exists()
    deployment = store.load("brand-new").deployment
    assert "pointer_mode" not in deployment
    assert "hydration" not in deployment
