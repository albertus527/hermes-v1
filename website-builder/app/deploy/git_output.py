"""Separate, application-owned bare output repository. Exact blob plumbing only."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path

from app.deploy.snapshot import TestedSnapshot


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

    def _run(self, args, data=None, extra=None, init=False):
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
                                env=env, timeout=60, check=True)
        return result.stdout

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
