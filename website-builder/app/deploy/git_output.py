"""Separate, application-owned bare output repository. Exact blob plumbing only."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path

from app.deploy.snapshot import TestedSnapshot

# A friendly publication branch is a single lower-case Git ref path component,
# never the internal ``preview/<hash>/<hash>`` preview ref. Vercel project
# names (which are what the branch is derived from) are the same shape.
_FRIENDLY_BRANCH_RE = re.compile(r'[a-z0-9][a-z0-9._-]{0,99}')

# A publication commit is a deterministic, human-readable record of one LIVE
# publication. It carries no secret and no unbounded content, and it is built
# from fixed values only, so the same (tree, parent, inputs) always yields the
# same commit SHA. That is what makes a retry after a failed push safe: the
# recreated commit is byte-identical to the one that failed.
def _publication_message(project_branch, source_revision, tested_commit,
                         source_sha256, artifact_sha256):
    return (
        'Website Builder LIVE publication\n'
        '\n'
        f'Project: {project_branch}\n'
        f'Live revision: {source_revision}\n'
        f'Tested snapshot commit: {tested_commit}\n'
        f'Source sha256: {source_sha256}\n'
        f'Artifact sha256: {artifact_sha256}\n'
    ).encode('utf-8')


class OutputGitRepository:
    def __init__(self, path, hermes_root=None):
        self.path = Path(path).absolute()
        hermes = Path(hermes_root or Path(__file__).resolve().parents[3]).resolve()
        resolved = self.path.resolve()
        if resolved == hermes or resolved.is_relative_to(hermes) or hermes.is_relative_to(resolved):
            raise ValueError('Output repository must be separate from Hermes')
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise ValueError('Linked output repository')
        self.path = resolved
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.mkdir()
        marker = self.path / 'website-builder-output'
        if not marker.exists():
            if any(self.path.iterdir()):
                raise ValueError('Refusing unowned output repository')
            self._run(['init', '--bare', '--template=', str(self.path)], init=True)
            marker.write_text('website-builder-output-v1\n', encoding='ascii')
        if marker.is_symlink() or marker.read_text() != 'website-builder-output-v1\n':
            raise ValueError('Invalid output ownership')
        if self._run(['rev-parse', '--is-bare-repository']).strip() != b'true':
            raise ValueError('Output must be bare')

    def _run(self, args, data=None, extra=None, init=False, check=True):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
        env.update({'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                    'GIT_TERMINAL_PROMPT': '0', 'GIT_AUTHOR_NAME': 'Website Builder',
                    'GIT_AUTHOR_EMAIL': 'builder@localhost', 'GIT_COMMITTER_NAME': 'Website Builder',
                    'GIT_COMMITTER_EMAIL': 'builder@localhost',
                    'GIT_AUTHOR_DATE': '2000-01-01T00:00:00+0000',
                    'GIT_COMMITTER_DATE': '2000-01-01T00:00:00+0000'})
        env.update(extra or {})
        command = ['git', '-c', 'core.longpaths=true', '-c', 'core.hooksPath=' + os.devnull]
        if not init:
            command += ['--git-dir=' + str(self.path)]
        result = subprocess.run(command + args, input=data, capture_output=True,
                                env=env, timeout=60, check=check)
        # With check=False the caller needs the exit status and the porcelain
        # report, so the CompletedProcess is returned verbatim. It is the one
        # place a non-raising git invocation is exposed: every other call
        # still fails loudly on a non-zero exit.
        return result if not check else result.stdout

    def commit(self, project_id, snapshot: TestedSnapshot):
        project = hashlib.sha256(project_id.encode()).hexdigest()
        branch = 'preview/' + project + '/' + snapshot.identity
        files = dict(snapshot.source)
        files.update({'dist/' + name: value for name, value in snapshot.dist.items()})
        # Private index; never checkout, add, filters, hooks, or Hermes index.
        with tempfile.TemporaryDirectory(dir=self.path.parent) as tmp:
            extra = {'GIT_INDEX_FILE': str(Path(tmp) / 'index')}
            self._run(['read-tree', '--empty'], extra=extra)
            entries = []
            for name, value in sorted(files.items()):
                if (name.startswith('/') or any(p in ('', '.', '..', '.git') for p in name.split('/'))
                        or any(c in name for c in ('\\', ':', '\0'))):
                    raise ValueError('Invalid Git snapshot path')
                blob = self._run(['hash-object', '-w', '--stdin'], value).strip()
                entries.append(b'100644 ' + blob + b'\t' + name.encode() + b'\0')
            self._run(['update-index', '-z', '--index-info'], b''.join(entries), extra)
            tree = self._run(['write-tree'], extra=extra).strip().decode()
        commit = self._run(['commit-tree', tree], ('Tested preview ' + snapshot.identity + '\n').encode()).strip().decode()
        self._run(['update-ref', 'refs/heads/' + branch, commit])
        return {'path': str(self.path), 'branch': branch, 'commit': commit,
                'source_sha256': snapshot.source_sha256, 'artifact_sha256': snapshot.artifact_sha256}

    def push_github(self, identity, url, *, enabled=False):
        """Explicit runtime opt-in. Never reads a remote from generated source."""
        if not enabled or not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git', url):
            raise ValueError('Explicit GitHub push authorization required')
        branch = identity['branch']
        if not re.fullmatch(r'preview/[a-f0-9]{64}/[a-f0-9]{64}', branch):
            raise ValueError('Invalid output branch')
        commit = self._run(['rev-parse', 'refs/heads/' + branch]).strip().decode()
        if commit != identity['commit']:
            raise ValueError('Stale output identity')
        self._run(['-c', 'credential.helper=', 'push', '--', url,
                   commit + ':refs/heads/' + branch])

    # ------------------------------------------------------------------
    # Friendly-branch publication (separate from push_github above)
    # ------------------------------------------------------------------
    #
    # ``push_github`` publishes the IMMUTABLE internal preview ref
    # ``preview/<project-sha>/<snapshot-sha>`` and stays exactly as it was.
    # This method is a different contract and does not share its URL form,
    # its ref shape, or its push shape.
    #
    # The friendly ``<project-slug>`` branch is a human-readable PUBLICATION
    # HISTORY, not a mirror of the latest build, so the branch is never
    # force-moved. Each LIVE publication adds its own commit whose TREE is
    # the exact tree of the approved TestedSnapshot commit:
    #
    #     publication_commit.tree  ==  tested_snapshot_commit.tree
    #
    # and whose parent is the previous publication commit, so every published
    # revision stays in the ancestry of the next one. The tested commit
    # itself remains immutable and authoritative internally; the two commits
    # are related by tree equality, never by identity.
    #
    # The tree object is TAKEN FROM the tested commit. Files are never
    # rebuilt from the workspace, ``git add .`` is never run, no index or
    # checkout is used, the remote is never read from generated project
    # files, and the Hermes repository is never touched.
    #
    # The parent comes from the caller (the last SYNCED publication recorded
    # in project state), NOT from the local ref and NOT from a remote read.
    # That is what keeps the branch honest: a revision whose push failed
    # never enters the friendly branch's ancestry.

    _PUSH_REJECTION_MARKERS = ('(non-fast-forward)', '(fetch first)')

    def _ssh_env(self, ssh_key):
        """GIT_SSH_COMMAND for a deploy-key push.

        Injected through the existing ``extra`` env hook, which is applied
        AFTER the ``GIT_*`` environment strip, so the key path survives. The
        key itself is never placed in argv, in a URL, in git config, or in
        any state. ``BatchMode=yes`` is mandatory: without it a missing key
        or an unknown host makes ssh prompt, which would hang the single
        project worker instead of failing the publication.
        """
        if not ssh_key:
            return None
        return {'GIT_SSH_COMMAND': (
            'ssh -o BatchMode=yes -o IdentitiesOnly=yes -i ' + str(ssh_key)
        )}

    def _has_commit(self, revision):
        result = self._run(['cat-file', '-e', str(revision) + '^{commit}'], check=False)
        return result.returncode == 0

    def _is_tested_snapshot_commit(self, commit):
        """True only when ``commit`` is the head of an internal
        ``preview/<project-sha>/<snapshot-sha>`` ref.

        That is what proves the commit being published is an immutable
        TestedSnapshot commit produced by ``commit()`` and not some other
        object that happened to be reachable.
        """
        result = self._run(
            ['for-each-ref', '--format=%(refname)', '--points-at', commit,
             'refs/heads/preview/'],
            check=False,
        )
        if result.returncode != 0:
            return False
        for line in result.stdout.decode('utf-8', 'replace').splitlines():
            if re.fullmatch(r'refs/heads/preview/[a-f0-9]{64}/[a-f0-9]{64}', line.strip()):
                return True
        return False

    def _build_publication_commit(self, tree, branch, parent, *, source_revision,
                                  tested_commit, source_sha256, artifact_sha256):
        """One deterministic publication commit for one LIVE revision.

        Deterministic on purpose: author/committer identity and dates are
        pinned in ``_run`` and the message is built from fixed values, so a
        retry after a failed push recreates the byte-identical commit (and
        therefore the same SHA) instead of forking a competing publication.
        """
        args = ['commit-tree', tree]
        if parent:
            args += ['-p', parent]
        message = _publication_message(
            branch, source_revision, tested_commit, source_sha256, artifact_sha256)
        return self._run(args, message).strip().decode()

    def _push_publication(self, url, commit, branch, extra):
        """One plain fast-forward push of the publication commit.

        Deliberately no ``--force``, no ``--force-with-lease`` and no ``+``
        refspec: the branch is advanced, never rewritten. Returns ``True`` on
        a confirmed push, or re-raises.
        """
        self._run(['-c', 'credential.helper=', 'push', '--porcelain', '--', url,
                   commit + ':refs/heads/' + branch], extra=extra)

    @staticmethod
    def _rejected_as_non_fast_forward(result):
        """True when a push was refused because the remote branch moved.

        Read from the machine-readable ``--porcelain`` report on stdout. A
        refusal for any other reason (auth, permissions, a pre-receive hook,
        a remote-side rejection) is NOT a fast-forward disagreement and must
        not trigger a re-parent: re-pushing in those cases would just repeat
        the same failure.
        """
        out = (result.stdout or b'').decode('utf-8', 'replace')
        for line in out.splitlines():
            # Porcelain ref lines are tab-delimited: "!<TAB><ref><TAB><summary>".
            fields = line.split('\t')
            if not fields or fields[0].strip() != '!':
                continue
            if any(marker in line for marker in OutputGitRepository._PUSH_REJECTION_MARKERS):
                return True
        return False

    def _remote_head(self, url, branch, extra):
        """The remote branch head SHA, or None when the branch is absent or
        unreadable.

        This is a REF-HEAD reconcile only: it returns a 40-character SHA and
        no file content. It is never a fetch, clone, pull, archive or any
        other read of source, and it never feeds a workspace.
        """
        result = self._run(
            ['ls-remote', '--heads', '--', url, 'refs/heads/' + branch],
            check=False, extra=extra,
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.decode('utf-8', 'replace').splitlines():
            fields = line.split()
            if len(fields) != 2 or fields[1] != 'refs/heads/' + branch:
                continue
            sha = fields[0]
            if re.fullmatch(r'[0-9a-f]{40}', sha):
                return sha
        return None

    def publish_project_branch(self, tested_commit, branch, url, *, source_revision=None,
                               source_sha256=None, artifact_sha256=None,
                               previous_publication_commit=None, ssh_key=None,
                               extra_env=None):
        """Publish one LIVE revision to the friendly ``<project-slug>`` branch.

        Publishes the EXACT tested snapshot commit that was approved and
        promoted -- never mutable workspace contents, never an untested
        build. Returns the two identities plus the shared tree:

            {'branch', 'repo', 'tested_commit', 'publication_commit', 'tree',
             'parent', 'reparented'}

        ``previous_publication_commit`` is the parent for the new publication
        commit, or None for the first publication (a root commit). It must be
        a commit this repository already holds: a parent we do not have is a
        hard error, never a reason to fetch.

        ``extra_env`` is additional git configuration for the push subprocess
        (applied after the ``GIT_*`` environment strip), e.g. an
        ``insteadOf`` mirror of the configured remote. It is never populated
        from generated project files and never carries a credential.
        """
        if not isinstance(url, str) or not re.fullmatch(
                r'git@github\.com:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\.git', url):
            raise ValueError('Explicit GitHub SSH remote required')
        repo = re.fullmatch(
            r'git@github\.com:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\.git', url)
        repo_name = repo.group(1) + '/' + repo.group(2)
        if (not isinstance(branch, str)
                or not _FRIENDLY_BRANCH_RE.fullmatch(branch)
                or branch.startswith('preview/')):
            # The internal preview/<hash>/<hash> ref is never the user-facing
            # branch, and a branch that could escape refs/heads is refused
            # outright rather than sanitized.
            raise ValueError('Invalid friendly publication branch')
        if not re.fullmatch(r'[0-9a-f]{40}', str(tested_commit or '')):
            raise ValueError('Invalid tested commit')
        if not self._has_commit(tested_commit):
            raise ValueError('Unknown tested commit')
        if not self._is_tested_snapshot_commit(tested_commit):
            # Refuse to publish anything that is not an immutable
            # TestedSnapshot commit, whatever it happens to be.
            raise ValueError('Tested commit is not a tested snapshot ref')

        parent = previous_publication_commit
        if parent is not None:
            if not re.fullmatch(r'[0-9a-f]{40}', str(parent)):
                raise ValueError('Invalid previous publication commit')
            if not self._has_commit(parent):
                # No fetch, ever: an unavailable parent is an operator
                # decision, not something to paper over.
                raise ValueError('Unknown previous publication commit')

        # The exact tree of the approved tested commit, reused verbatim.
        tree = self._run(['rev-parse', str(tested_commit) + '^{tree}']).strip().decode()
        extra = self._ssh_env(ssh_key)
        if extra_env:
            extra = dict(extra or {}, **extra_env)
        publication = self._build_publication_commit(
            tree, branch, parent, source_revision=source_revision,
            tested_commit=tested_commit, source_sha256=source_sha256,
            artifact_sha256=artifact_sha256)
        # Cheap, and the whole point of the contract: the published commit's
        # tree is the tested commit's tree.
        if self._run(['rev-parse', publication + '^{tree}']).strip().decode() != tree:
            raise ValueError('Publication commit tree does not match tested tree')

        reparented = False
        try:
            self._push_publication(url, publication, branch, extra)
        except subprocess.CalledProcessError as exc:
            if not self._rejected_as_non_fast_forward(exc):
                raise
            # Bounded reconcile for the one case a fast-forward push cannot
            # self-heal: the remote accepted an earlier publication and the
            # local record of it was lost (a crash between the push and the
            # caller's state write). Re-parent onto the remote head and push
            # once more. The remote is never rewritten, only extended, and
            # only with a commit that holds the same tested tree.
            remote_head = self._remote_head(url, branch, extra)
            if remote_head is None or remote_head == parent:
                raise
            if not self._has_commit(remote_head):
                # An unknown remote head cannot be extended from here, and
                # fetching is R2 behavior. Fail closed.
                raise ValueError('Remote publication head is not available locally')
            publication = self._build_publication_commit(
                tree, branch, remote_head, source_revision=source_revision,
                tested_commit=tested_commit, source_sha256=source_sha256,
                artifact_sha256=artifact_sha256)
            if self._run(['rev-parse', publication + '^{tree}']).strip().decode() != tree:
                raise ValueError('Publication commit tree does not match tested tree')
            self._push_publication(url, publication, branch, extra)
            parent, reparented = remote_head, True

        # Object retention and a local human-visible mirror of the branch.
        # This ref is NOT the parent authority: the caller persists the
        # publication commit and passes it back as the parent next time.
        self._run(['update-ref', 'refs/heads/' + branch, publication])
        return {
            'branch': branch,
            'repo': repo_name,
            'tested_commit': tested_commit,
            'publication_commit': publication,
            'tree': tree,
            'parent': parent,
            'reparented': reparented,
        }
