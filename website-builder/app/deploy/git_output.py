"""Separate, application-owned bare output repository. Exact blob plumbing only."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path

from app.core import credentials
from app.core.contracts import OperationResult
from app.deploy.snapshot import TestedSnapshot

# A friendly publication branch is a single lower-case Git ref path component,
# never the internal ``preview/<hash>/<hash>`` preview ref. Vercel project
# names (which are what the branch is derived from) are the same shape.
_FRIENDLY_BRANCH_RE = re.compile(r'[a-z0-9][a-z0-9._-]{0,99}')

# The ONE accepted form of the operator-configured source remote. Publication
# and hydration both derive the repository NAME from it, so a release recorded
# against a different remote is a mismatch rather than a second code path.
_GITHUB_SSH_RE = re.compile(r'git@github\.com:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\.git')

_SHA1_RE = re.compile(r'[0-9a-f]{40}')
_MODE_RE = re.compile(rb'[0-7]{6}')
_BLOB_SHA_RE = re.compile(rb'[0-9a-f]{40}')

#: Blob modes hydration will write. Everything else is refused, each with its
#: own reason: 120000 is a symlink, 160000 is a gitlink (submodule), and any
#: other mode is unsupported. There is no "skip it and carry on".
_ACCEPTED_BLOB_MODES = ('100644', '100755')

#: Git-LFS pointer recognition is a bounded, EXPLICIT grammar check (D29) and
#: nothing more. A canonical pointer is exactly three LF-terminated ASCII
#: lines: the spec header, the ``oid``, the ``size``. Bytes that do not match
#: that structure are ordinary committed bytes -- this is NOT a general LFS
#: detector and never claims to be one. Digest verification at VERIFIED is a
#: CONTENT-INTEGRITY check, not a fallback LFS safety net: if non-canonical
#: LFS-like bytes were themselves what was tested and published, the recorded
#: digest matches them and hydration legitimately succeeds.
_LFS_VERSION_LINE = b'version https://git-lfs.github.com/spec/v1'
_LFS_POINTER_MAX_BYTES = 1024
_LFS_OID_RE = re.compile(rb'oid sha256:[0-9a-f]{64}\Z')
_LFS_SIZE_RE = re.compile(rb'size [0-9]+\Z')


class MaterializationRefusal(RuntimeError):
    """One refused tree entry (or refused commit), with its own reason.

    ``reason`` is a stable slug the hydrator maps to a user-facing code. The
    offending ``path`` is the safe repository path, never a host path.
    """

    def __init__(self, reason: str, path=None):
        self.reason = reason
        self.path = path
        super().__init__(reason if path is None else f'{reason}: {path}')


def repository_name_for_url(url):
    """``owner/name`` derived from the configured SSH remote, else ``None``."""
    if not isinstance(url, str):
        return None
    match = _GITHUB_SSH_RE.fullmatch(url)
    return f'{match.group(1)}/{match.group(2)}' if match else None


def verify_repository_identity(url, expected_repo):
    """Offline: the configured remote must name the recorded repository.

    Deliberately hoisted ahead of any fetch so a mismatch is refused without
    touching the network, and never repaired by fetching from the wrong place.
    """
    name = repository_name_for_url(url)
    if name is None:
        raise MaterializationRefusal('repo_identity')
    if isinstance(expected_repo, str) and expected_repo and name != expected_repo:
        raise MaterializationRefusal('repo_mismatch', expected_repo)
    return name


def looks_like_canonical_lfs_pointer(data: bytes) -> bool:
    """True only for the canonical Git-LFS pointer grammar implemented here."""
    if not isinstance(data, (bytes, bytearray)):
        return False
    if len(data) > _LFS_POINTER_MAX_BYTES:
        return False
    if not data.startswith(_LFS_VERSION_LINE + b'\n'):
        return False
    body = bytes(data)[len(_LFS_VERSION_LINE) + 1:]
    lines = body.split(b'\n')
    if lines and lines[-1] == b'':
        lines.pop()
    if len(lines) != 2:
        return False
    return bool(_LFS_OID_RE.match(lines[0]) and _LFS_SIZE_RE.match(lines[1]))


def unsafe_repository_path(name) -> bool:
    """The same predicate ``commit()`` applies to a snapshot path.

    Traversal, absolute paths, empty/``.``/``..``/``.git`` components,
    backslashes, colons and NULs are all refused. A path is never sanitized --
    it is accepted or it is refused.
    """
    if not isinstance(name, str) or not name:
        return True
    if name.startswith('/'):
        return True
    if any(c in name for c in ('\\', ':', '\0')):
        return True
    return any(p in ('', '.', '..', '.git') for p in name.split('/'))


def _parse_ls_tree(raw: bytes):
    """Strictly parse ``ls-tree -r -z`` output.

    The record shape is ``<mode> SP <type> SP <sha> TAB <path> NUL``. Anything
    that does not parse exactly is refused rather than skipped, because a
    parser that skips what it cannot read is a parser that silently
    materializes less than the commit contains.
    """
    records = raw.split(b'\0')
    if records and records[-1] == b'':
        records.pop()
    entries = []
    for record in records:
        if not record:
            raise MaterializationRefusal('unparseable')
        head, separator, path = record.partition(b'\t')
        if not separator or not path:
            raise MaterializationRefusal('unparseable')
        try:
            path_text = path.decode('utf-8')
        except UnicodeDecodeError:
            raise MaterializationRefusal('unparseable') from None
        fields = head.split(b' ')
        if len(fields) != 3:
            raise MaterializationRefusal('unparseable', path_text)
        mode, kind, blob = fields
        if not _MODE_RE.fullmatch(mode) or not _BLOB_SHA_RE.fullmatch(blob):
            raise MaterializationRefusal('unparseable', path_text)
        try:
            kind_text = kind.decode('ascii')
        except UnicodeDecodeError:
            raise MaterializationRefusal('unparseable', path_text)
        entries.append((mode.decode('ascii'), kind_text, blob.decode('ascii'),
                        path_text))
    return entries


def refuse_tree_entry(mode: str, kind: str, path: str) -> None:
    """The blob-type / mode gate. Each refusal names its own reason."""
    if mode == '120000':
        raise MaterializationRefusal('symlink', path)
    if mode == '160000':
        raise MaterializationRefusal('submodule', path)
    if mode not in _ACCEPTED_BLOB_MODES:
        raise MaterializationRefusal('mode', path)
    if kind != 'blob':
        raise MaterializationRefusal('non_blob', path)
    if unsafe_repository_path(path):
        raise MaterializationRefusal('unsafe_path', path)


def assert_write_target(staging: Path, destination: Path) -> None:
    """Re-checked before EVERY write, not once per run.

    The staging directory itself must not be a symlink, no component between
    the staging root and the destination may be an existing symlink, and the
    fully resolved destination must still be inside the resolved staging root.
    A single up-front check would be satisfied by a tree that changed shape
    while it was being written.
    """
    if staging.is_symlink():
        raise MaterializationRefusal('containment')
    try:
        relative = destination.relative_to(staging)
    except ValueError:
        raise MaterializationRefusal('containment') from None
    current = staging
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise MaterializationRefusal('containment')
    try:
        resolved = destination.resolve(strict=False)
        root = staging.resolve(strict=False)
    except OSError:
        raise MaterializationRefusal('containment') from None
    if resolved != root and root not in resolved.parents:
        raise MaterializationRefusal('containment')

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


#: A LOCAL publication-input failure: the intended commit or parent this
#: operation persisted is not a commit id.
#:
#: This is deliberately NOT ``PUBLICATION_HEAD_CONFLICT``. C and D classify
#: what the REMOTE holds -- C is "we read it and it is not ours", D is "we could
#: not read it". A malformed local identity means nothing was read at all, and
#: labelling it C sends an operator to inspect GitHub instead of the state
#: record that is actually corrupt.
PUBLICATION_INPUT_INVALID = 'PUBLICATION_INPUT_INVALID'

#: The remote accepted the push, but the LOCAL mirror ref could not be written.
#: The branch is durable history and is never rolled back for this: the record
#: stays at PREPARED, and the retry re-pushes, is rejected as a non-fast-forward
#: (the remote already holds the intended commit), and adopts as case A.
PUBLICATION_LOCAL_REF_UPDATE_FAILED = 'PUBLICATION_LOCAL_REF_UPDATE_FAILED'


def _verdict(verdict: str, error_code: str, **data) -> OperationResult:
    """One classified reconciliation failure carrying its verdict.

    C and D stay distinct all the way out: ``C_CONFLICT`` means the remote was
    read and holds an unexpected state, ``D_UNAVAILABLE`` means it could not be
    read. ``OperationResult.fail`` has no ``data`` parameter, so the result is
    constructed directly rather than losing the verdict on the way.
    """
    return OperationResult(
        success=False, error=error_code, error_code=error_code,
        data={'verdict': verdict, **data},
    )


def _local_input_invalid(reason: str) -> OperationResult:
    """A refusal to reconcile because the LOCAL identity is unusable.

    Carries no verdict: nothing was read, so there is nothing to classify
    against the A/B/C/D matrix. Constructed directly for the same reason
    ``_verdict`` is -- ``OperationResult.fail`` takes no ``data``.
    """
    return OperationResult(
        success=False, error=reason, error_code=PUBLICATION_INPUT_INVALID,
        data={'verdict': None, 'local_reason': reason},
    )


def _push_rejected(rejected_as: str) -> OperationResult:
    """One classified push failure. Only the rejection KIND is ever exposed."""
    return OperationResult(
        success=False, error='PUBLICATION_PUSH_REJECTED',
        error_code='PUBLICATION_PUSH_FAILED', data={'rejected_as': rejected_as},
    )


def _local_ref_update_failed() -> OperationResult:
    """A local mirror-ref write that failed after an ACCEPTED remote push.

    Reported as a push failure so ``GIT_CONFIRMED`` is not persisted, which is
    honest: the durable record of the confirmation is exactly what was lost. It
    stays recoverable because the retry reaches case A, and the remote branch is
    never rolled back to make the local state look tidy.
    """
    return OperationResult(
        success=False, error=PUBLICATION_LOCAL_REF_UPDATE_FAILED,
        error_code=PUBLICATION_LOCAL_REF_UPDATE_FAILED,
        data={'rejected_as': 'OTHER'},
    )


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

    def _run(self, args, data=None, extra=None, init=False, check=True, credentials_env=None):
        # R2-B1: the base environment is role-scoped. This is the Git
        # adapter, so it may receive ONLY Git/SSH configuration plus benign
        # system variables — never a Vercel/Hostinger/Strix deployment
        # credential, and never a model credential. The previous
        # ``os.environ`` pass-through handed every deploy token in the parent
        # process to every git invocation.
        env = credentials.git_env(source=credentials_env)
        # Unset any inherited GIT_* first, so the deterministic values below
        # are authoritative. Unchanged behaviour; only the base env is scoped.
        for key in [k for k in list(env) if k.upper().startswith('GIT_')]:
            del env[key]
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
    # The parent comes from the caller (``deployment.publication_head`` in
    # project state, the last publication commit CONFIRMED on the remote
    # branch), NOT from the local ref and NOT from a remote read. That is what
    # keeps the branch honest: a revision whose push failed never enters the
    # friendly branch's ancestry.
    #
    # Publication is three explicit steps, and only the middle one touches the
    # remote on the happy path:
    #
    #   1. ``prepare_publication``  -- local only; builds the one intended
    #      commit and proves its tree equals the tested tree.
    #   2. ``push_prepared_publication`` -- one plain non-force push. An
    #      ACCEPTED push is the proof: it can only have succeeded as a
    #      fast-forward from the parent we built against.
    #   3. ``reconcile_publication_head`` -- the ONLY remote read, used on
    #      resume/recovery and on a rejected push to classify A/B/C/D and fail
    #      closed on anything that is not provably ours.
    #
    # There is deliberately no re-parent path. A conflicting remote head is a
    # conflict to report, not a commit to build on top of.

    _PUSH_REJECTION_MARKERS = ('(non-fast-forward)', '(fetch first)')

    def _ssh_env(self, ssh_key):
        """GIT_SSH_COMMAND for a deploy-key push.

        Injected through the existing ``extra`` env hook, which is applied
        AFTER the ``GIT_*`` environment strip, so the key path survives. The
        key itself is never placed in argv, in a URL, in git config, or in
        any state. ``BatchMode=yes`` is mandatory: without it a missing key
        or an unknown host makes ssh prompt, which would hang the single
        project worker instead of failing the publication.

        R2-B1: the Git adapter remains the ONLY place a deploy key is
        referenced. The key is a PATH; the material is never read, exported
        into any generation-role environment, or made model-visible.
        """
        if not ssh_key:
            return None
        command = credentials.git_env(ssh_key=ssh_key, source={})['GIT_SSH_COMMAND']
        return {'GIT_SSH_COMMAND': command}

    def _has_commit(self, revision):
        result = self._run(['cat-file', '-e', str(revision) + '^{commit}'], check=False)
        return result.returncode == 0

    # -- hydration reads (R2-C) --------------------------------------------

    def has_commit(self, commit):
        """Whether the object is present locally. Never touches the network."""
        if not _SHA1_RE.fullmatch(str(commit or '')):
            return False
        return self._has_commit(commit)

    def has_tested_snapshot_commit(self, commit):
        """Whether ``commit`` is the head of an immutable preview ref."""
        if not _SHA1_RE.fullmatch(str(commit or '')):
            return False
        return self._is_tested_snapshot_commit(commit)

    def commit_tree(self, commit):
        """The exact tree id of ``commit``. Raises if the object is unknown."""
        return self._run(['rev-parse', str(commit) + '^{tree}']).strip().decode()

    def fetch_pinned_commit(self, commit, url, *, ssh_key=None, extra_env=None):
        """Fetch EXACTLY ``commit`` into a private namespaced hydration ref.

        The refspec is the literal 40-hex object id, so what lands is the
        recorded publication commit and nothing else. There is deliberately no
        branch, no tag, no wildcard and no ``FETCH_HEAD`` fallback: a branch
        tip that has moved since the release is not the release, and resolving
        it "close enough" is exactly the source-of-truth drift this fetch
        exists to prevent.

        Returns the namespaced ref name. A failure raises; the caller decides
        what an absent object means.
        """
        if not _SHA1_RE.fullmatch(str(commit or '')):
            raise MaterializationRefusal('commit')
        extra = self._ssh_env(ssh_key)
        if extra_env:
            extra = dict(extra or {}, **extra_env)
        ref = 'refs/hydrate/' + commit
        self._run(['-c', 'credential.helper=', 'fetch', '--no-tags',
                   '--no-write-fetch-head', '--', url, commit + ':' + ref],
                  extra=extra)
        return ref

    def materialize_commit(self, commit, expected_tree, staging, *, extra_env=None):
        """Write ``commit``'s exact blobs into ``staging``. No worktree at all.

        Never ``checkout``, ``checkout-index``, ``read-tree`` as a worktree
        materializer, ``archive``, ``tar``, a filter, clean/smudge or a hook.
        Every file is fetched as a raw blob object and written by Python, so
        the bytes on disk are the bytes in the commit and nothing else can
        influence them.

        Returns ``{'source': [...], 'dist': [...]}`` -- the repository-relative
        names written under each kind, which the caller re-walks from the real
        filesystem before the workspace is allowed to become current.
        """
        if not _SHA1_RE.fullmatch(str(commit or '')):
            raise MaterializationRefusal('commit')
        if not _SHA1_RE.fullmatch(str(expected_tree or '')):
            raise MaterializationRefusal('tree')
        if self.commit_tree(commit) != expected_tree:
            raise MaterializationRefusal('tree_mismatch')
        entries = _parse_ls_tree(
            self._run(['ls-tree', '-r', '-z', str(commit)], extra=extra_env))
        staging = Path(staging)
        staging.mkdir(parents=True, exist_ok=True)
        source, dist = [], []
        for mode, kind, blob, path in entries:
            refuse_tree_entry(mode, kind, path)
            destination = staging / path
            assert_write_target(staging, destination)
            data = self._run(['cat-file', 'blob', blob], extra=extra_env)
            # LFS is unsupported: recognised before the write, so the bytes
            # never reach disk. No Git LFS process, no filter, no clean/smudge
            # and no LFS object fetch is ever attempted.
            if looks_like_canonical_lfs_pointer(data):
                raise MaterializationRefusal('lfs', path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            # The executable bit comes from the mode and only from 100755;
            # every other accepted mode is written plain.
            os.chmod(destination, 0o755 if mode == '100755' else 0o644)
            if path.startswith('dist/'):
                dist.append(path[len('dist/'):])
            else:
                source.append(path)
        return {'source': sorted(source), 'dist': sorted(dist)}

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
        """Classify the remote branch head. Never returns a bare ``None``.

        Returns one of:

            ``("HEAD", sha)``       -- the branch exists; ``sha`` is its head.
            ``("ABSENT", None)``    -- the remote answered, and the answer was
                                       trustworthy, and the branch is not there.
            ``("UNREADABLE", None)``-- the remote could not be read at all
                                       (transport/auth failure) OR it answered
                                       with output we cannot parse as ref data.

        Collapsing the last two into a single ``None`` is the R1 defect this
        split fixes: "the branch is gone" and "we could not ask" license
        completely different actions (a root push vs. fail closed).

        The parse is strict, and the strictness is load-bearing. ``ls-remote``
        with a ref pattern prints nothing at all when the pattern matches
        nothing, so *empty* output is a genuine, trustworthy ABSENT. Output that
        is present but not shaped like ``<40-hex><TAB-or-space><ref>`` is
        something else entirely -- a proxy, a wrapper, a corrupted transport --
        and it is UNREADABLE, because a reader that cannot understand the reply
        has learned nothing about the branch. Treating it as ABSENT would let an
        unreadable remote authorise either a conflict verdict (we read it, it is
        wrong) or, with no parent yet, a root push at a branch of unknown state.

        This is a REF-HEAD reconcile only: it returns a 40-character SHA and
        no file content. It is never a fetch, clone, pull, archive or any
        other read of source, and it never feeds a workspace.
        """
        result = self._run(
            ['ls-remote', '--heads', '--', url, 'refs/heads/' + branch],
            check=False, extra=extra,
        )
        if result.returncode != 0:
            return ('UNREADABLE', None)
        malformed = False
        for line in result.stdout.decode('utf-8', 'replace').splitlines():
            if not line.strip():
                continue
            fields = line.split()
            if len(fields) != 2 or not re.fullmatch(r'[0-9a-f]{40}', fields[0]):
                # Present, but not ref data. Nothing about the branch can be
                # concluded from it, so nothing is concluded.
                malformed = True
                continue
            if fields[1] == 'refs/heads/' + branch:
                return ('HEAD', fields[0])
        if malformed:
            return ('UNREADABLE', None)
        # The remote answered with ref data (or with nothing at all) and named
        # no such branch: that is a trustworthy ABSENT.
        return ('ABSENT', None)

    def _publication_inputs(self, tested_commit, branch, url, previous_publication_commit):
        """Validate every publication input and return the shared identity.

        No network, no push, no ref update: this is the validation half of
        publication, factored out so ``prepare_publication`` is the only entry
        point that decides whether an intended publication is well-formed.
        """
        if not isinstance(url, str) or not _GITHUB_SSH_RE.fullmatch(url):
            raise ValueError('Explicit GitHub SSH remote required')
        repo = _GITHUB_SSH_RE.fullmatch(url)
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
        return repo_name, parent

    def prepare_publication(self, tested_commit, branch, url, *, source_revision=None,
                            source_sha256=None, artifact_sha256=None,
                            previous_publication_commit=None, ssh_key=None,
                            extra_env=None):
        """Build and return the ONE intended publication commit, with no side
        effects beyond writing that commit object into the local repository.

        The commit is deterministic (pinned identity/dates in ``_run``, fixed
        message in ``_publication_message``), so preparing the same inputs
        twice always yields the same SHA. That is what makes case A below
        possible: after a crash that lost the ``GIT_CONFIRMED`` write, the
        retried operation rebuilds the *identical* commit and the remote is
        found to already hold it, rather than forking a competing publication.

        Returns:

            {'branch', 'repo', 'tested_commit', 'tested_tree', 'tree',
             'parent', 'publication_commit'}

        No network access happens here: no push, no ``ls-remote``, no fetch.
        """
        repo_name, parent = self._publication_inputs(
            tested_commit, branch, url, previous_publication_commit)
        # The exact tree of the approved tested commit, reused verbatim.
        tree = self._run(['rev-parse', str(tested_commit) + '^{tree}']).strip().decode()
        publication = self._build_publication_commit(
            tree, branch, parent, source_revision=source_revision,
            tested_commit=tested_commit, source_sha256=source_sha256,
            artifact_sha256=artifact_sha256)
        # Cheap, and the whole point of the contract: the publication commit's
        # tree is the tested commit's tree.
        if self._run(['rev-parse', publication + '^{tree}']).strip().decode() != tree:
            raise ValueError('Publication commit tree does not match tested tree')
        return {
            'branch': branch,
            'repo': repo_name,
            'tested_commit': tested_commit,
            'tested_tree': tree,
            'publication_commit': publication,
            'tree': tree,
            'parent': parent,
        }

    def push_prepared_publication(self, url, commit, branch, *, ssh_key=None,
                                  extra_env=None):
        """Push one already-prepared publication commit. Returns an
        ``OperationResult`` whose ``data["rejected_as"]`` is ``None`` on
        success, ``"NON_FAST_FORWARD"`` when the branch moved, or
        ``"OTHER"`` for every other refusal.

        On success this performs NO remote read. A plain fast-forward push
        already proves the remote's previous head was exactly the parent we
        built against, so a verification round-trip would be a redundant
        network call and a second source of truth for a settled fact.
        """
        extra = self._ssh_env(ssh_key)
        if extra_env:
            extra = dict(extra or {}, **extra_env)
        try:
            self._push_publication(url, commit, branch, extra)
        except subprocess.CalledProcessError as exc:
            rejected_as = (
                'NON_FAST_FORWARD' if self._rejected_as_non_fast_forward(exc) else 'OTHER'
            )
            return _push_rejected(rejected_as)
        except Exception:
            # A transport failure that is not a clean non-zero exit (an ssh
            # connection error, a missing key, a timeout). Classified, not
            # propagated: the caller decides terminal-vs-reconcile from the
            # classification, and only the rejection KIND is ever surfaced --
            # a git failure's message can embed the remote and local paths.
            return _push_rejected('OTHER')
        # Object retention and a local human-visible mirror of the branch.
        # This ref is NOT the parent authority: the caller persists the
        # publication commit in ``publication_head`` and passes it back as the
        # parent next time.
        #
        # A failure here is classified, never propagated. The push has already
        # been accepted, so the only thing lost is a local convenience ref and
        # the caller's ability to persist GIT_CONFIRMED on this attempt; the
        # remote branch is durable history and is NEVER rolled back to make the
        # local state look consistent. The retry re-pushes, is refused as a
        # non-fast-forward, and adopts the existing commit as case A.
        try:
            self._run(['update-ref', 'refs/heads/' + branch, commit])
        except Exception:
            return _local_ref_update_failed()
        return OperationResult.ok({'rejected_as': None, 'publication_commit': commit})

    def reconcile_publication_head(self, url, branch, intended_commit, intended_parent,
                                   *, ssh_key=None, extra_env=None,
                                   already_confirmed=False):
        """Classify the remote branch head against one intended publication.

        Read-only, and the ONLY entry point that reads the remote. Runs on
        resume/recovery and on a rejected push -- never on the ordinary
        successful path.

        Returns an ``OperationResult`` with ``data['verdict']``:

            ``A_ADOPT``       the remote already holds ``intended_commit``;
                              the publication is confirmed, no push is needed.
            ``B_RETRY``       the remote still holds ``intended_parent`` (or
                              the branch is absent and there is no parent), so
                              the exact intended commit is a clean fast-forward.
            ``C_CONFLICT``    the remote holds something else entirely.
            ``D_UNAVAILABLE`` the remote could not be read or trusted.

        C and D are deliberately distinct: C means we READ the remote and it
        holds an unexpected state, D means we could not read it and therefore
        assert nothing. They map to distinct error codes
        (``PUBLICATION_HEAD_CONFLICT`` / ``PUBLICATION_HEAD_UNREADABLE``) and
        must never be merged.

        ``already_confirmed`` narrows the matrix to the RESUME semantics, and
        the narrowing is a safety property, not a convenience. It says the
        persisted stage already asserts the push landed, so anything other than
        ``intended_commit`` means the remote changed AFTER confirmation -- which
        is a conflict to report, never a commit to re-establish. B_RETRY is
        therefore never returned in that mode: re-pushing a branch a previous
        process already published is exactly the second publication the stage
        machine exists to prevent. B_RETRY remains for reconciliation while
        still at PREPARED, after a rejected initial push, where the remote
        genuinely has not seen the commit yet.
        """
        if not re.fullmatch(r'[0-9a-f]{40}', str(intended_commit or '')):
            # LOCAL, not remote: nothing is read, and nothing is asserted about
            # the branch. Raising here is what used to happen, and it escaped
            # the whole stage machine as a TypeError.
            return _local_input_invalid('Invalid intended publication commit')
        if intended_parent is not None and not re.fullmatch(
                r'[0-9a-f]{40}', str(intended_parent)):
            return _local_input_invalid('Invalid intended publication parent')
        extra = self._ssh_env(ssh_key)
        if extra_env:
            extra = dict(extra or {}, **extra_env)
        state, head = self._remote_head(url, branch, extra)
        if state == 'UNREADABLE':
            return _verdict(
                'D_UNAVAILABLE', 'PUBLICATION_HEAD_UNREADABLE',
                remote_state=state,
            )
        if state == 'HEAD':
            if head == intended_commit:
                return OperationResult.ok(
                    {'verdict': 'A_ADOPT', 'remote_head': head, 'remote_state': state})
            if already_confirmed:
                # The remote moved after this operation was told the commit was
                # there. Reported, never re-published.
                return _verdict(
                    'C_CONFLICT', 'PUBLICATION_HEAD_CONFLICT',
                    remote_head=head, remote_state=state,
                )
            if intended_parent is not None and head == intended_parent:
                return OperationResult.ok(
                    {'verdict': 'B_RETRY', 'remote_head': head, 'remote_state': state})
            # A valid head we did not intend. Not adoptable, not retryable,
            # and never a reason to re-parent onto it.
            return _verdict(
                'C_CONFLICT', 'PUBLICATION_HEAD_CONFLICT',
                remote_head=head, remote_state=state,
            )
        # ABSENT. A first-ever publication (no parent) is a clean root push;
        # an absent branch that was supposed to exist means the remote lost
        # confirmed history, which we never silently re-create.
        if already_confirmed:
            return _verdict(
                'C_CONFLICT', 'PUBLICATION_HEAD_CONFLICT',
                remote_head=None, remote_state='ABSENT',
            )
        if intended_parent is None:
            return OperationResult.ok(
                {'verdict': 'B_RETRY', 'remote_head': None, 'remote_state': 'ABSENT'})
        return _verdict(
            'C_CONFLICT', 'PUBLICATION_HEAD_CONFLICT',
            remote_head=None, remote_state='ABSENT',
        )
