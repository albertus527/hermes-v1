"""Local behavioral tests: exact-byte snapshot, immutability, staleness rejection."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.deploy.snapshot import TestedSnapshot, digest, read_tree, source_fingerprint


def _make_workspace(tmp_path, index_body=b'<html>v1</html>'):
    ws = tmp_path / 'ws'
    (ws / 'src').mkdir(parents=True)
    (ws / 'dist').mkdir()
    (ws / 'src' / 'App.tsx').write_bytes(b'export default 1;')
    (ws / 'dist' / 'index.html').write_bytes(index_body)
    (ws / 'node_modules').mkdir()
    (ws / 'node_modules' / 'x.js').write_bytes(b'noise')
    (ws / '.git').mkdir()
    (ws / '.git' / 'HEAD').write_bytes(b'noise')
    return ws


def test_read_tree_excludes_runtime_dirs(tmp_path):
    ws = _make_workspace(tmp_path)
    files = read_tree(ws, {'node_modules', 'dist', '.git'})
    assert set(files) == {'src/App.tsx'}


def test_digest_deterministic_and_order_independent():
    a = digest({'b': b'2', 'a': b'1'})
    b = digest({'a': b'1', 'b': b'2'})
    assert a == b


def test_snapshot_binds_exact_source_and_dist_bytes(tmp_path):
    ws = _make_workspace(tmp_path)
    snap = TestedSnapshot.capture(ws)
    assert snap.dist['index.html'] == b'<html>v1</html>'
    assert snap.source['src/App.tsx'] == b'export default 1;'
    assert snap.source_sha256 == source_fingerprint(ws)


def test_snapshot_rejects_changed_source_since_check(tmp_path):
    ws = _make_workspace(tmp_path)
    before = source_fingerprint(ws)
    (ws / 'src' / 'App.tsx').write_bytes(b'export default 2;')
    with pytest.raises(ValueError, match='STALE_SOURCE'):
        TestedSnapshot.capture(ws, expected_source=before)


def test_snapshot_rejects_changed_dist_since_check(tmp_path):
    ws = _make_workspace(tmp_path)
    snap = TestedSnapshot.capture(ws)
    (ws / 'dist' / 'index.html').write_bytes(b'<html>tampered</html>')
    with pytest.raises(ValueError, match='STALE_ARTIFACT'):
        TestedSnapshot.capture(ws, expected_source=snap.source_sha256, expected_artifact=snap.artifact_sha256)


def test_snapshot_missing_dist_index_rejected(tmp_path):
    ws = _make_workspace(tmp_path)
    (ws / 'dist' / 'index.html').unlink()
    with pytest.raises(ValueError):
        TestedSnapshot.capture(ws)


def test_snapshot_roundtrip_dict():
    snap = TestedSnapshot({'a': b'1'}, {'index.html': b'<html/>'})
    restored = TestedSnapshot.from_dict(snap.to_dict())
    assert restored.identity == snap.identity


def test_snapshot_immutable_mapping():
    snap = TestedSnapshot({'a': b'1'}, {'index.html': b'<html/>'})
    with pytest.raises(TypeError):
        snap.source['a'] = b'2'


def test_read_tree_rejects_symlink_root(tmp_path):
    if sys.platform == 'win32':
        pytest.skip('requires symlink privilege on Windows')
    target = tmp_path / 'real'
    target.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(target)
    with pytest.raises(ValueError):
        read_tree(link)
