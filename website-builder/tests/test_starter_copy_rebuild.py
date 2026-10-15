"""``FrontendBuilder._copy_starter`` is rebuild-safe and symlink-safe.

``templates/frontend-starter/`` is merged into the project workspace on EVERY
build, and a project is legitimately rebuilt on top of a workspace a previous
build already populated. The pre-fix merge used ``shutil.copytree(...,
dirs_exist_ok=True)`` / ``shutil.copy2`` directly, which has two real defects
this module pins down:

  1. **Read-only destination.** ``shutil.copy2`` opens the destination with
     ``'wb'`` and raises ``PermissionError`` when a previously copied starter
     file is read-only. Reproduced live on the staging rebuild: the staging
     source refresh left the pinned source ``chmod a-w``, so the first build's
     copied starter inherited read-only modes (``444``) and the second build
     failed with ``UNEXPECTED_BUILD_ERROR`` BEFORE N6/FRONTEND ever ran.
  2. **Symlink follow.** ``copy2``/``copytree`` follow a symlink planted at the
     destination, so a link inside the workspace can redirect the write (and a
     ``'wb'`` open) to a file OUTSIDE the workspace.

The fix replaces the destination instead of opening it for write, never follows
a destination symlink, confines every write to the workspace, and preserves
unrelated generated files (PRD.md, Design.md, FRONTEND source). These tests
assert all of that, plus the happy path and a repeated rebuild.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.projects.build import (  # noqa: E402
    FrontendBuilder,
    _STARTER_PATH,
)
from app.sandbox.runner import WorkspaceError  # noqa: E402


def _builder() -> FrontendBuilder:
    """A FrontendBuilder bound to the REAL starter, with no runner/adapter.

    ``_copy_starter`` touches only ``self.starter_path`` and the passed
    workspace, so the rest of the constructor is irrelevant here.
    """
    builder = FrontendBuilder.__new__(FrontendBuilder)
    builder.starter_path = Path(_STARTER_PATH)
    return builder


def _freeze_tree(root: Path) -> None:
    """Make every real file read-only and every real dir non-writable.

    Mirrors the staging failure exactly: a starter copied from a ``chmod a-w``
    source leaves read-only files behind for the next build.
    """
    for current, dirs, files in os.walk(root, topdown=False):
        for name in files:
            path = Path(current) / name
            if not path.is_symlink():
                os.chmod(path, 0o444)
        for name in dirs:
            path = Path(current) / name
            if not path.is_symlink():
                os.chmod(path, 0o555)


def _restore_tree(root: Path) -> None:
    for current, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(current) / name
            try:
                os.chmod(path, 0o755)
            except OSError:
                pass


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    yield ws
    _restore_tree(ws)


# ---------------------------------------------------------------------------
# Happy path + repeated rebuild
# ---------------------------------------------------------------------------


def test_first_copy_places_starter(workspace: Path):
    _builder()._copy_starter(workspace)
    assert (workspace / "package.json").is_file()
    assert (workspace / "index.html").is_file()
    assert (workspace / "src" / "App.tsx").is_file()


def test_rebuild_over_read_only_starter_files(workspace: Path):
    """THE blocker: a second build over read-only starter files must succeed."""
    builder = _builder()
    builder._copy_starter(workspace)
    _freeze_tree(workspace)
    # Sanity: the destination really is read-only.
    assert stat.S_IMODE(os.stat(workspace / "tsconfig.json").st_mode) == 0o444

    # Pre-fix this raised PermissionError.
    builder._copy_starter(workspace)

    assert (workspace / "tsconfig.json").read_text(encoding="utf-8").startswith("{")


def test_repeated_rebuild_is_idempotent(workspace: Path):
    builder = _builder()
    builder._copy_starter(workspace)
    for _ in range(3):
        builder._copy_starter(workspace)
    assert (workspace / "vite.config.ts").read_text(encoding="utf-8") == (
        _STARTER_PATH / "vite.config.ts"
    ).read_text(encoding="utf-8")


def test_rebuild_restores_a_mutated_starter_file(workspace: Path):
    builder = _builder()
    builder._copy_starter(workspace)
    (workspace / "vite.config.ts").write_text("MUTATED", encoding="utf-8")
    builder._copy_starter(workspace)
    assert (workspace / "vite.config.ts").read_text(encoding="utf-8") == (
        _STARTER_PATH / "vite.config.ts"
    ).read_text(encoding="utf-8")


def test_rebuild_preserves_unrelated_generated_files(workspace: Path):
    """PRD.md / Design.md / generated source are NOT starter entries."""
    builder = _builder()
    builder._copy_starter(workspace)
    (workspace / "PRD.md").write_text("PRD-SURVIVES", encoding="utf-8")
    (workspace / "Design.md").write_text("DESIGN-SURVIVES", encoding="utf-8")
    (workspace / "src" / "components").mkdir()
    (workspace / "src" / "components" / "Hero.tsx").write_text("HERO", encoding="utf-8")

    builder._copy_starter(workspace)

    assert (workspace / "PRD.md").read_text(encoding="utf-8") == "PRD-SURVIVES"
    assert (workspace / "Design.md").read_text(encoding="utf-8") == "DESIGN-SURVIVES"
    assert (workspace / "src" / "components" / "Hero.tsx").read_text(encoding="utf-8") == "HERO"


def test_executable_mode_is_preserved(workspace: Path):
    """copy2 semantics: the source's permission bits survive the merge."""
    builder = _builder()
    src = _STARTER_PATH / "src" / "main.tsx"
    original = stat.S_IMODE(os.stat(src).st_mode)
    builder._copy_starter(workspace)
    assert stat.S_IMODE(os.stat(workspace / "src" / "main.tsx").st_mode) == original


# ---------------------------------------------------------------------------
# Symlink / path-escape attempts
# ---------------------------------------------------------------------------


def test_destination_symlink_is_not_followed(workspace: Path, tmp_path: Path):
    """A symlink at a starter destination must not redirect the write."""
    victim = tmp_path / "victim.txt"
    victim.write_text("ORIGINAL-SECRET", encoding="utf-8")
    os.symlink(victim, workspace / "package.json")

    _builder()._copy_starter(workspace)

    assert victim.read_text(encoding="utf-8") == "ORIGINAL-SECRET"
    assert not (workspace / "package.json").is_symlink()
    assert (workspace / "package.json").read_text(encoding="utf-8").startswith("{")


def test_symlinked_directory_escape_is_neutralized(workspace: Path, tmp_path: Path):
    """A ``src`` symlink pointing outside must be replaced, not followed.

    The escape target is NEVER written through: the link is unlinked and a real
    starter directory is created in its place, so the outside tree is untouched.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "main.tsx").write_text("OUTSIDE-SECRET", encoding="utf-8")
    os.symlink(outside, workspace / "src")

    _builder()._copy_starter(workspace)

    # The outside tree is byte-for-byte untouched and gained no starter files.
    assert (outside / "main.tsx").read_text(encoding="utf-8") == "OUTSIDE-SECRET"
    assert sorted(os.listdir(outside)) == ["main.tsx"]
    # The planted link is gone; a real starter directory replaced it.
    assert not (workspace / "src").is_symlink()
    assert (workspace / "src" / "App.tsx").is_file()


def test_assert_confined_rejects_an_escaping_parent(tmp_path: Path):
    """The confinement guard itself refuses a destination outside the workspace."""
    from app.projects.build import _assert_confined

    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    with pytest.raises(WorkspaceError):
        _assert_confined(outside / "package.json", workspace)
    # The workspace root and its descendants are accepted.
    _assert_confined(workspace / "package.json", workspace)
    _assert_confined(workspace, workspace)


def test_symlinked_file_inside_workspace_is_replaced_not_followed(workspace: Path):
    """A symlink that stays inside the workspace is still unlinked, not read."""
    real = workspace / "real-target.json"
    real.write_text("INNER", encoding="utf-8")
    os.symlink(real, workspace / "package.json")

    _builder()._copy_starter(workspace)

    # The link is gone (replaced by a real file); the target is untouched.
    assert not (workspace / "package.json").is_symlink()
    assert real.read_text(encoding="utf-8") == "INNER"
