"""Local behavioral tests: separate bare output repo, exact blob commit, no push by default."""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.deploy.git_output import OutputGitRepository
from app.deploy.snapshot import TestedSnapshot


def _snapshot():
    return TestedSnapshot({'src/App.tsx': b'x=1', 'design-dna.json': b'{}'},
                          {'index.html': b'<html>hi</html>', 'assets/a.js': b'console.log(1)'})


def test_output_repo_rejects_hermes_root(tmp_path):
    hermes_root = tmp_path / 'hermes'
    hermes_root.mkdir()
    with pytest.raises(ValueError):
        OutputGitRepository(hermes_root / 'out', hermes_root=hermes_root)


def test_output_repo_init_bare_and_marked(tmp_path):
    repo = OutputGitRepository(tmp_path / 'out', hermes_root=tmp_path / 'hermes')
    assert (repo.path / 'website-builder-output').exists()
    assert repo._run(['rev-parse', '--is-bare-repository']).strip() == b'true'


def test_output_repo_refuses_unowned_nonempty_dir(tmp_path):
    target = tmp_path / 'out'
    target.mkdir()
    (target / 'something').write_text('x')
    with pytest.raises(ValueError):
        OutputGitRepository(target, hermes_root=tmp_path / 'hermes')


def test_commit_writes_exact_bytes_deterministic_branch(tmp_path):
    repo = OutputGitRepository(tmp_path / 'out', hermes_root=tmp_path / 'hermes')
    snap = _snapshot()
    identity = repo.commit('project-1', snap)
    assert identity['branch'].startswith('preview/')
    assert identity['branch'].endswith(snap.identity)

    show = subprocess.run(
        ['git', '--git-dir=' + str(repo.path), 'show',
         identity['commit'] + ':dist/index.html'],
        capture_output=True, check=True,
    )
    assert show.stdout == b'<html>hi</html>'

    show_src = subprocess.run(
        ['git', '--git-dir=' + str(repo.path), 'show',
         identity['commit'] + ':src/App.tsx'],
        capture_output=True, check=True,
    )
    assert show_src.stdout == b'x=1'


def test_commit_same_identity_reuses_branch_deterministically(tmp_path):
    repo = OutputGitRepository(tmp_path / 'out', hermes_root=tmp_path / 'hermes')
    snap = _snapshot()
    first = repo.commit('project-1', snap)
    second = repo.commit('project-1', snap)
    assert first['commit'] == second['commit']
    assert first['branch'] == second['branch']


def test_commit_different_projects_do_not_collide(tmp_path):
    repo = OutputGitRepository(tmp_path / 'out', hermes_root=tmp_path / 'hermes')
    snap = _snapshot()
    a = repo.commit('project-a', snap)
    b = repo.commit('project-b', snap)
    assert a['branch'] != b['branch']


def test_push_github_requires_explicit_opt_in(tmp_path):
    repo = OutputGitRepository(tmp_path / 'out', hermes_root=tmp_path / 'hermes')
    identity = repo.commit('project-1', _snapshot())
    with pytest.raises(ValueError):
        repo.push_github(identity, 'https://github.com/example/repo.git', enabled=False)


def test_push_github_rejects_invalid_url_even_when_enabled(tmp_path):
    repo = OutputGitRepository(tmp_path / 'out', hermes_root=tmp_path / 'hermes')
    identity = repo.commit('project-1', _snapshot())
    with pytest.raises(ValueError):
        repo.push_github(identity, 'git@github.com:example/repo.git', enabled=True)


# ---------------------------------------------------------------------------
# Live-incident regressions: OUTPUT_COMMIT_FAILED from a non-writable parent.
# ---------------------------------------------------------------------------
# The staging harness keeps its root directory root-owned 0755 on purpose. The
# output repository inside it is writable by the runtime user, but the PARENT
# is not — and ``commit`` used to create its scratch index in that parent, so
# the very first object write raised EACCES. These tests pin the corrected
# behaviour without weakening the isolation: the parent stays non-writable.

def _writable_repo_under_readonly_parent(tmp_path):
    """A bare repo whose parent is deliberately non-writable (0755-root shape)."""
    parent = tmp_path / 'staging-root'
    parent.mkdir()
    repo_path = parent / 'output-repo'
    repo_path.mkdir()
    repo = OutputGitRepository(repo_path, hermes_root=tmp_path / 'hermes')
    import os
    os.chmod(parent, 0o555)
    return repo, parent


# Root bypasses mode bits, so the EACCES shape cannot be reproduced as root.
# The tests still run (and pass) as root but would not have caught the original
# defect there; the strict permission assertions are skipped in that case.
_require_nonroot = pytest.mark.skipif(
    hasattr(__import__('os'), 'geteuid') and __import__('os').geteuid() == 0,
    reason='mode bits are not enforced for root; cannot reproduce EACCES',
)


@_require_nonroot
def test_commit_succeeds_when_parent_is_not_writable(tmp_path):
    """The scratch index must live in the repo, never in the parent."""
    import os
    repo, parent = _writable_repo_under_readonly_parent(tmp_path)
    try:
        assert not os.access(parent, os.W_OK)
        assert os.access(repo.path, os.W_OK)
        identity = repo.commit('project-1', _snapshot())
        show = subprocess.run(
            ['git', '--git-dir=' + str(repo.path), 'show',
             identity['commit'] + ':dist/index.html'],
            capture_output=True, check=True,
        )
        assert show.stdout == b'<html>hi</html>'
    finally:
        os.chmod(parent, 0o755)


def test_commit_leaves_no_scratch_dir_behind(tmp_path):
    """Success cleans up the transient index directory inside the repo."""
    repo, parent = _writable_repo_under_readonly_parent(tmp_path)
    try:
        repo.commit('project-1', _snapshot())
        leftovers = [p.name for p in repo.path.iterdir()
                     if p.name not in ('objects', 'refs', 'HEAD', 'config',
                                       'description', 'hooks', 'info', 'website-builder-output')]
        assert leftovers == [], f'unexpected leftovers: {leftovers}'
    finally:
        import os
        os.chmod(parent, 0o755)


def test_commit_failure_still_cleans_up_scratch_dir(tmp_path):
    """A rejected snapshot path raises AND removes the scratch directory."""
    repo, parent = _writable_repo_under_readonly_parent(tmp_path)
    try:
        before = {p.name for p in repo.path.iterdir()}
        bad = TestedSnapshot({'../escape.tsx': b'x=1'}, {'index.html': b'hi'})
        with pytest.raises(ValueError):
            repo.commit('project-1', bad)
        after = {p.name for p in repo.path.iterdir()}
        assert after == before, f'leaked scratch dir: {after - before}'
    finally:
        import os
        os.chmod(parent, 0o755)


def test_scratch_index_is_confined_to_the_repository(tmp_path):
    """No temp entry may appear in the (non-writable) parent after a commit."""
    repo, parent = _writable_repo_under_readonly_parent(tmp_path)
    try:
        repo.commit('project-1', _snapshot())
        leaked = [p.name for p in parent.iterdir() if p.name.startswith('tmp')]
        assert leaked == [], f'leaked into parent: {leaked}'
    finally:
        import os
        os.chmod(parent, 0o755)


def test_commit_refuses_symlinked_output_repository(tmp_path):
    """Path confinement: a symlinked output repo is refused, not followed."""
    real = tmp_path / 'real-out'
    real.mkdir()
    link = tmp_path / 'linked-out'
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError):
        OutputGitRepository(link, hermes_root=tmp_path / 'hermes')


def test_commit_rejects_path_traversal_in_snapshot(tmp_path):
    """Snapshot file names may not escape the tree (existing invariant, pinned)."""
    repo = OutputGitRepository(tmp_path / 'out', hermes_root=tmp_path / 'hermes')
    for bad_name in ('../x.tsx', '/abs.tsx', 'a/../../b.tsx', 'a/.git/config'):
        bad = TestedSnapshot({bad_name: b'x'}, {'index.html': b'hi'})
        with pytest.raises(ValueError):
            repo.commit('project-1', bad)


def test_scratch_dir_is_created_inside_the_repository(tmp_path, monkeypatch):
    """Root-proof shape pin: TemporaryDirectory(dir=...) must be repo.path.

    This asserts the CONTRACT directly (independent of uid), so the regression
    is caught even where mode bits cannot be enforced (running as root).
    """
    import tempfile as _tempfile

    repo = OutputGitRepository(tmp_path / 'out', hermes_root=tmp_path / 'hermes')
    seen = {}
    real = _tempfile.TemporaryDirectory

    def spy(*args, **kwargs):
        seen['dir'] = kwargs.get('dir')
        return real(*args, **kwargs)

    monkeypatch.setattr('app.deploy.git_output.tempfile.TemporaryDirectory', spy)
    repo.commit('project-1', _snapshot())
    assert seen['dir'] == repo.path, f"scratch dir must be repo.path, got {seen['dir']}"
