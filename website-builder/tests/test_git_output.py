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
