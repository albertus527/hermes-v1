"""Exact byte provenance. No Git ignore rules, filters, or mutable path references."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

# Application/runtime output is not build source. Everything else is hashed,
# including dotfiles, lockfiles, public assets, config and Design DNA.
EXCLUDED = {'.git', 'node_modules', 'dist', 'qa', '.hermes', '.browser', '.runtime'}


def digest(files):
    h = hashlib.sha256()
    for name, content in sorted(files.items()):
        key = name.encode('utf-8')
        h.update(len(key).to_bytes(8, 'big') + key)
        h.update(len(content).to_bytes(8, 'big') + content)
    return h.hexdigest()


def read_tree(root: Path, excluded=()):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Missing or linked source/artifact root')
    files = {}
    total = 0
    def walk(directory, prefix=''):
        nonlocal total
        for path in sorted(directory.iterdir()):
            if not prefix and path.name in excluded:
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Linked snapshot entry')
            name = prefix + path.name
            if any(c in name for c in ('\\', ':', '\0')) or any(
                    p.lower() == '.git' for p in name.split('/')):
                raise ValueError('Unsafe snapshot entry')
            if stat.S_ISDIR(info.st_mode):
                walk(path, name + '/')
            elif stat.S_ISREG(info.st_mode):
                if info.st_size > 32 * 1024 * 1024:
                    raise ValueError('Snapshot file too large')
                content = path.read_bytes()
                after = path.stat()
                if (info.st_ino, info.st_size, info.st_mtime_ns) != (
                        after.st_ino, after.st_size, after.st_mtime_ns):
                    raise ValueError('Snapshot changed during read')
                total += len(content)
                if total > 128 * 1024 * 1024 or len(files) >= 10000:
                    raise ValueError('Snapshot too large')
                files[name] = content
            else:
                raise ValueError('Non-regular snapshot entry')
    walk(root)
    return files


def source_fingerprint(workspace):
    return digest(read_tree(workspace, EXCLUDED))


@dataclass(frozen=True)
class TestedSnapshot:
    source: object
    dist: object

    def __post_init__(self):
        object.__setattr__(self, 'source', MappingProxyType(dict(self.source)))
        object.__setattr__(self, 'dist', MappingProxyType(dict(self.dist)))
        if not self.source or 'index.html' not in self.dist:
            raise ValueError('Incomplete snapshot')
        if not all(isinstance(v, bytes) for v in (*self.source.values(), *self.dist.values())):
            raise ValueError('Snapshot requires bytes')

    @property
    def source_sha256(self):
        return digest(self.source)

    @property
    def artifact_sha256(self):
        return digest(self.dist)

    @property
    def identity(self):
        return hashlib.sha256((self.source_sha256 + self.artifact_sha256).encode()).hexdigest()

    @classmethod
    def capture(cls, workspace, expected_source=None, expected_artifact=None):
        snapshot = cls(read_tree(workspace, EXCLUDED), read_tree(Path(workspace) / 'dist'))
        if expected_source is not None and snapshot.source_sha256 != expected_source:
            raise ValueError('STALE_SOURCE')
        if expected_artifact is not None and snapshot.artifact_sha256 != expected_artifact:
            raise ValueError('STALE_ARTIFACT')
        snapshot.verify(workspace)
        return snapshot

    def verify(self, workspace):
        if source_fingerprint(workspace) != self.source_sha256:
            raise ValueError('STALE_SOURCE')
        if digest(read_tree(Path(workspace) / 'dist')) != self.artifact_sha256:
            raise ValueError('STALE_ARTIFACT')

    def to_dict(self):
        return {kind: {name: base64.b64encode(value).decode('ascii')
                       for name, value in getattr(self, kind).items()}
                for kind in ('source', 'dist')}

    @classmethod
    def from_dict(cls, data):
        return cls(**{kind: {name: base64.b64decode(value, validate=True)
                            for name, value in data[kind].items()}
                      for kind in ('source', 'dist')})


def record_checks(store, project_id, workspace, before):
    """Bind successful build/typecheck to their exact input and output bytes."""
    snapshot = TestedSnapshot.capture(workspace, before)
    with store.acquire_writer(project_id) as state:
        state.deployment['checked'] = {
            'source_revision': state.revisions.source_revision,
            'source_sha256': snapshot.source_sha256,
            'artifact_sha256': snapshot.artifact_sha256,
        }
        state.deployment.pop('tested_snapshot', None)
        store.save(state)
    return snapshot
