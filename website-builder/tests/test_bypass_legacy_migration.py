"""R1 -> R2 migration of the Vercel bypass secret store out of the profile.

R1 wrote every project's automation-bypass secret under the generation profile
home; R2 relocated the store into the application's state root and added
``assert_profile_home_clean``, which refuses a profile that still holds one.
Together those two changes left an un-migrated R1 install unable to start — the
startup guard runs before anything could move the file.

These tests pin the migration that closes that hole: real files, real
``BypassSecretStore`` writes, no mocks except where a test injects a fault.
The two properties under test throughout are (a) no provisioned secret is ever
lost, and (b) anything the migration cannot verify fails closed with the legacy
directory left in place.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.credentials import (  # noqa: E402
    PRIVILEGED_SECRET_SUBPATHS,
    assert_profile_home_clean,
)
from app.core import secrets as secrets_module  # noqa: E402
from app.core.secrets import (  # noqa: E402
    BYPASS_STORE_DIRNAME,
    BypassSecretStore,
    LegacyBypassMigrationError,
    file_mode,
    migrate_legacy_bypass_secrets,
)

# Canary value: must never reach a log record or an exception message.
MIGRATION_SECRET = "CANARY_MIGRATION_BYPASS_SECRET"
SECOND_SECRET = "CANARY_MIGRATION_BYPASS_SECRET_2"
PRIMARY_SECRET = "CANARY_PRIMARY_BYPASS_SECRET"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _layout(tmp_path):
    """Profile home and state root, kept apart the way production keeps them."""
    home = tmp_path / ".hermes-website"
    home.mkdir(parents=True, exist_ok=True)
    return home, tmp_path / ".website-builder" / "state"


def _legacy_dir(home):
    return home / BYPASS_STORE_DIRNAME


def _primary(state):
    return BypassSecretStore(state / BYPASS_STORE_DIRNAME)


def _seed_valid(home, *project_ids):
    """Write authentic R1-shaped legacy files through a real store."""
    store = BypassSecretStore(_legacy_dir(home))
    for index, project_id in enumerate(project_ids):
        store.set(project_id, MIGRATION_SECRET if index == 0 else SECOND_SECRET)


def _write_entry(home, name, body):
    path = _legacy_dir(home) / name
    path.write_text(body, encoding="utf-8")
    return path


def _quarantines(state):
    return sorted(state.glob(f"{BYPASS_STORE_DIRNAME}-migrated-*"))


class _ModuleShim:
    """A stand-in for a module: the real one plus patched callables.

    Installed as a module attribute (``secrets.os`` / ``secrets.time``) rather
    than by patching the real module, so a patch applied here cannot leak into
    unrelated code — patching ``os.replace`` itself would also break the
    atomic write inside ``BypassSecretStore.set``.
    """

    def __init__(self, real_module, **overrides):
        self._real = real_module
        for name, func in overrides.items():
            setattr(self, name, func)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _patch_retire_only(monkeypatch, legacy, on_retire):
    """Affect only the rename that retires *legacy*.

    *on_retire* receives the (src, dst) of that rename and must either perform
    it or raise; every other ``os.replace`` (i.e. every secret write) is
    delegated to the real one.
    """
    real_replace = os.replace

    def replace(src, dst):
        if Path(src) == legacy:
            return on_retire(src, dst)
        return real_replace(src, dst)

    monkeypatch.setattr(secrets_module, "os", _ModuleShim(os, replace=replace))


def _force_symlink(monkeypatch, path, target, *, as_directory=False):
    """Make *path* a symlink to *target*, simulating the check if forbidden.

    Returns True when a real link was created. Windows without developer mode
    (or with the privilege withheld) cannot create one, and the guard must
    still be exercised on every host — so a real placeholder is left in its
    place and the *check* is stubbed rather than the test skipped.
    """
    try:
        path.symlink_to(target, target_is_directory=as_directory)
        return True
    except (OSError, NotImplementedError):
        if as_directory:
            path.mkdir()
        else:
            path.write_text('{"secret": "%s"}' % MIGRATION_SECRET, encoding="utf-8")
        real_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path, "is_symlink",
            lambda self: True if self == path else real_is_symlink(self),
        )
        return False


def _refuse_iterdir(legacy):
    """A ``Path.iterdir`` replacement that cannot scan *legacy*."""
    real_iterdir = Path.iterdir

    def refuse(self, *args, **kwargs):
        if self == legacy:
            raise FileNotFoundError(str(self))
        return real_iterdir(self, *args, **kwargs)

    return refuse


# ---------------------------------------------------------------------------
# No-op / clean install
# ---------------------------------------------------------------------------


def test_clean_install_without_legacy_dir_is_a_noop(tmp_path):
    home, state = _layout(tmp_path)
    (home / "skills").mkdir()
    assert migrate_legacy_bypass_secrets(home, state) is None
    # Nothing was created anywhere: a clean profile must stay untouched.
    assert not state.exists()
    assert sorted(p.name for p in home.iterdir()) == ["skills"]
    assert_profile_home_clean(home)


def test_a_legacy_path_that_is_not_a_directory_is_left_to_the_profile_guard(tmp_path):
    """A file (or any non-directory) at the store path is not ours to move.

    The migration does not delete it — it has no idea what it is — so the
    profile guard still refuses and the operator keeps an actionable failure
    rather than silent data loss.
    """
    home, state = _layout(tmp_path)
    stranger = home / BYPASS_STORE_DIRNAME
    stranger.write_text("not a store", encoding="utf-8")
    assert migrate_legacy_bypass_secrets(home, state) is None
    assert stranger.read_text(encoding="utf-8") == "not a store"
    with pytest.raises(ValueError, match="privileged secret store"):
        assert_profile_home_clean(home)


# ---------------------------------------------------------------------------
# The state root must be outside the profile home
# ---------------------------------------------------------------------------


def _tree_snapshot(root):
    return sorted(
        str(p.relative_to(root)) for p in root.rglob("*")
    )


@pytest.mark.parametrize("shape", ["equal", "nested", "deeply-nested"])
def test_state_root_inside_the_profile_home_fails_closed(tmp_path, shape, caplog):
    """A state root at or under HERMES_HOME is refused before anything moves.

    ``assert_profile_home_clean`` only inspects the profile home's direct
    children, so ``HERMES_HOME/state/vercel-bypass`` is invisible to it — which
    is why the code that MOVES the secret has to refuse the destination itself.
    Migrating here would hand the credential back to the generation plane and
    leave the guard passing.
    """
    home, _ = _layout(tmp_path)
    _seed_valid(home, "prj_canary")
    before = _tree_snapshot(home)
    state = {
        "equal": home,
        "nested": home / "state",
        "deeply-nested": home / "a" / "b" / "c" / "state",
    }[shape]

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LegacyBypassMigrationError) as excinfo:
            migrate_legacy_bypass_secrets(home, state)

    # No mutation anywhere under the profile, and nothing retired.
    assert _tree_snapshot(home) == before
    assert _quarantines(state) == []
    assert MIGRATION_SECRET not in str(excinfo.value)
    assert MIGRATION_SECRET not in caplog.text


def test_state_root_refusal_names_the_paths_and_the_fix(tmp_path):
    """The operator gets an actionable, value-free pointer — not "OSError"."""
    home, _ = _layout(tmp_path)
    _seed_valid(home, "prj_canary")
    state = home / "state"

    with pytest.raises(LegacyBypassMigrationError) as excinfo:
        migrate_legacy_bypass_secrets(home, state)

    message = str(excinfo.value)
    assert "outside the profile home" in message
    assert str(home.resolve()) in message
    assert str(state.resolve()) in message
    assert "WEBSITE_BUILDER_STATE_ROOT" in message
    assert MIGRATION_SECRET not in message
    # The guard itself is untouched: the profile still refuses the store.
    with pytest.raises(ValueError, match="privileged secret store"):
        assert_profile_home_clean(home)


def test_state_root_outside_the_profile_home_is_accepted(tmp_path):
    """Outside the profile the migration proceeds — including a name that only
    shares a string prefix with it, which must not be mistaken for nesting."""
    home, _ = _layout(tmp_path)
    _seed_valid(home, "prj_canary")
    sibling = home.parent / (home.name + "-state")
    sibling.mkdir()

    report = migrate_legacy_bypass_secrets(home, sibling)

    assert report["migrated"] == ["prj_canary"]
    assert _primary(sibling).get("prj_canary") == MIGRATION_SECRET
    assert not _legacy_dir(home).exists()
    assert_profile_home_clean(home)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_single_valid_legacy_secret_is_migrated(tmp_path):
    home, state = _layout(tmp_path)
    _seed_valid(home, "prj_canary")
    report = migrate_legacy_bypass_secrets(home, state)

    target = state / BYPASS_STORE_DIRNAME / "prj_canary.json"
    assert report["migrated"] == ["prj_canary"]
    assert report["already_current"] == []
    assert report["temp_leftovers"] == 0
    assert Path(report["quarantined_to"]).is_dir()

    assert target.is_file()
    assert _primary(state).get("prj_canary") == MIGRATION_SECRET
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload == {"project_id": "prj_canary", "secret": MIGRATION_SECRET}

    if os.name == "posix":
        assert file_mode(target) == 0o600
        assert file_mode(target.parent) == 0o700

    assert not _legacy_dir(home).exists()
    assert _quarantines(state) == [Path(report["quarantined_to"])]
    assert (Path(report["quarantined_to"]) / "prj_canary.json").is_file()
    if os.name == "posix":
        assert file_mode(Path(report["quarantined_to"])) == 0o700


def test_primary_wins_and_legacy_is_retired(tmp_path):
    home, state = _layout(tmp_path)
    _seed_valid(home, "prj_canary")
    primary = _primary(state)
    primary.set("prj_canary", PRIMARY_SECRET)
    before = primary.path_for("prj_canary").read_bytes()

    report = migrate_legacy_bypass_secrets(home, state)

    assert report["already_current"] == ["prj_canary"]
    assert report["migrated"] == []
    # Not rewritten, not merged, not compared value-for-value.
    assert primary.path_for("prj_canary").read_bytes() == before
    assert primary.get("prj_canary") == PRIMARY_SECRET
    assert not _legacy_dir(home).exists()
    assert Path(report["quarantined_to"]).is_dir()


def test_repeated_startup_is_idempotent(tmp_path):
    home, state = _layout(tmp_path)
    _seed_valid(home, "prj_a", "prj_b")
    first = migrate_legacy_bypass_secrets(home, state)
    assert sorted(first["migrated"]) == ["prj_a", "prj_b"]

    primary_dir = state / BYPASS_STORE_DIRNAME
    snapshot = {p.name: p.read_bytes() for p in sorted(primary_dir.iterdir())}

    second = migrate_legacy_bypass_secrets(home, state)

    assert second is None
    assert {p.name: p.read_bytes() for p in sorted(primary_dir.iterdir())} == snapshot
    # Exactly one retirement, even across starts.
    assert len(_quarantines(state)) == 1


def test_new_writes_land_only_in_state_root(tmp_path):
    home, state = _layout(tmp_path)
    _seed_valid(home, "prj_canary")
    migrate_legacy_bypass_secrets(home, state)

    primary = _primary(state)
    primary.set("prj_new", SECOND_SECRET)

    assert (state / BYPASS_STORE_DIRNAME / "prj_new.json").is_file()
    assert primary.get("prj_new") == SECOND_SECRET
    assert primary.get("prj_canary") == MIGRATION_SECRET
    # The profile is empty of the store entirely, so there is nowhere left for
    # a write to land there.
    assert not _legacy_dir(home).exists()
    assert assert_profile_home_clean(home) is None


# ---------------------------------------------------------------------------
# The profile guard: satisfied by moving, never by whitelisting
# ---------------------------------------------------------------------------


def test_assert_profile_home_clean_passes_after_migration(tmp_path):
    home, state = _layout(tmp_path)
    _seed_valid(home, "prj_canary")
    with pytest.raises(ValueError, match="privileged secret store"):
        assert_profile_home_clean(home)

    migrate_legacy_bypass_secrets(home, state)

    assert_profile_home_clean(home)


def test_migrated_dirname_is_exactly_the_one_the_guard_refuses(tmp_path):
    """The guard was not weakened — the directory simply moved."""
    assert BYPASS_STORE_DIRNAME == "vercel-bypass"
    assert BYPASS_STORE_DIRNAME in PRIVILEGED_SECRET_SUBPATHS

    profile = tmp_path / "profile"
    (profile / BYPASS_STORE_DIRNAME).mkdir(parents=True)
    with pytest.raises(ValueError, match="privileged secret store"):
        assert_profile_home_clean(profile)


# ---------------------------------------------------------------------------
# Fail-closed: malformed / unexpected legacy content
# ---------------------------------------------------------------------------


def test_malformed_legacy_json_fails_closed_with_no_data_loss(tmp_path):
    home, state = _layout(tmp_path)
    _seed_valid(home, "prj_a", "prj_b")
    corrupt = _legacy_dir(home) / "prj_b.json"
    corrupt.write_text('{"project_id": "prj_b", "secret": ', encoding="utf-8")
    before = corrupt.read_bytes()

    with pytest.raises(LegacyBypassMigrationError, match="not valid JSON"):
        migrate_legacy_bypass_secrets(home, state)

    assert corrupt.read_bytes() == before
    assert not (state / BYPASS_STORE_DIRNAME / "prj_b.json").exists()
    assert _legacy_dir(home).is_dir()
    assert _quarantines(state) == []


@pytest.mark.parametrize("kind", [
    "subdirectory", "traversal-name", "space-in-name", "empty-stem",
    "not-json", "symlink",
])
def test_invalid_project_id_or_entry_name_fails_closed(tmp_path, monkeypatch, kind):
    home, state = _layout(tmp_path)
    _seed_valid(home, "prj_valid")
    remove, own_valid_ids = _add_unexpected_entry(home, monkeypatch, kind)

    with pytest.raises(LegacyBypassMigrationError):
        migrate_legacy_bypass_secrets(home, state)

    # Nothing was retired and the operator still owns every file.
    assert _legacy_dir(home).is_dir()
    assert _quarantines(state) == []

    # Once the operator resolves the unknown entry, the same install migrates.
    remove()
    report = migrate_legacy_bypass_secrets(home, state)
    assert sorted(report["migrated"] + report["already_current"]) == sorted(
        ["prj_valid"] + own_valid_ids,
    )
    assert _primary(state).get("prj_valid") == MIGRATION_SECRET


def _add_unexpected_entry(home, monkeypatch, kind):
    """Add one entry the store could never have written.

    Returns ``(remove_it, extra_valid_ids)`` — the entries this helper added
    that the store would legitimately accept once the offender is gone.
    """
    legacy = _legacy_dir(home)
    secret_body = json.dumps({"project_id": "prj_canary", "secret": MIGRATION_SECRET})
    if kind == "subdirectory":
        nested = legacy / "sub"
        nested.mkdir()
        (nested / "evil.json").write_text(secret_body, encoding="utf-8")
        return lambda: shutil.rmtree(nested), []
    if kind == "traversal-name":
        path = _write_entry(home, "..%2fescape.json", secret_body)
        return lambda: path.unlink(), []
    if kind == "space-in-name":
        path = _write_entry(home, "bad name.json", secret_body)
        return lambda: path.unlink(), []
    if kind == "empty-stem":
        path = _write_entry(home, ".json", secret_body)
        return lambda: path.unlink(), []
    if kind == "not-json":
        path = _write_entry(home, "notes.txt", MIGRATION_SECRET)
        return lambda: path.unlink(), []
    if kind == "symlink":
        target = _write_entry(home, "prj_real.json", json.dumps({"secret": MIGRATION_SECRET}))
        _force_symlink(monkeypatch, legacy / "prj_linked.json", target)
        return lambda: (legacy / "prj_linked.json").unlink(), ["prj_real"]
    raise AssertionError(kind)


def test_legacy_json_without_a_usable_secret_fails_closed(tmp_path):
    home, state = _layout(tmp_path)
    _seed_valid(home, "prj_valid")
    _write_entry(home, "prj_blank.json", '{"project_id": "prj_blank", "secret": ""}')
    _write_entry(home, "prj_list.json", f'["{MIGRATION_SECRET}"]')
    with pytest.raises(LegacyBypassMigrationError, match="no usable secret"):
        migrate_legacy_bypass_secrets(home, state)
    assert _legacy_dir(home).is_dir()
    assert _quarantines(state) == []


def test_legacy_file_claiming_another_project_fails_closed(tmp_path):
    home, state = _layout(tmp_path)
    _seed_valid(home, "prj_valid")
    _write_entry(
        home, "prj_mislabelled.json",
        json.dumps({"project_id": "prj_somewhere_else", "secret": MIGRATION_SECRET}),
    )
    with pytest.raises(LegacyBypassMigrationError, match="different project id"):
        migrate_legacy_bypass_secrets(home, state)
    assert not (state / BYPASS_STORE_DIRNAME / "prj_mislabelled.json").exists()
    assert _legacy_dir(home).is_dir()


def test_corrupt_primary_with_valid_legacy_fails_closed(tmp_path):
    """D3: never silently pick a winner between two disagreeing copies."""
    home, state = _layout(tmp_path)
    _seed_valid(home, "prj_canary")
    primary = _primary(state)
    target = primary.path_for("prj_canary")
    target.write_text("{ truncated", encoding="utf-8")
    legacy_copy = _legacy_dir(home) / "prj_canary.json"

    with pytest.raises(LegacyBypassMigrationError) as excinfo:
        migrate_legacy_bypass_secrets(home, state)

    message = str(excinfo.value)
    assert "prj_canary" in message
    assert str(target) in message
    assert str(legacy_copy) in message
    # The operator's conflicting file is left exactly as it was.
    assert target.read_text(encoding="utf-8") == "{ truncated"
    assert _legacy_dir(home).is_dir()
    assert _quarantines(state) == []


# ---------------------------------------------------------------------------
# Fail-closed: interruption
# ---------------------------------------------------------------------------


def test_interrupted_copy_leaves_no_partially_trusted_state(tmp_path):
    home, state = _layout(tmp_path)
    _seed_valid(home, "prj_a", "prj_b")
    real_set = BypassSecretStore.set
    calls = []

    def flaky(self, project_id, secret):
        calls.append(project_id)
        if len(calls) > 1:
            raise OSError(errno.EIO, "simulated disk failure")
        return real_set(self, project_id, secret)

    with patch.object(BypassSecretStore, "set", flaky):
        with pytest.raises(LegacyBypassMigrationError, match="could not be written"):
            migrate_legacy_bypass_secrets(home, state)

    # The entry that did land is valid and usable — a half-written file would
    # be worse than a missing one.
    assert _primary(state).get("prj_a") == MIGRATION_SECRET
    assert _primary(state).get("prj_b") is None
    assert _legacy_dir(home).is_dir()
    assert BypassSecretStore(_legacy_dir(home)).get("prj_b") == SECOND_SECRET
    assert _quarantines(state) == []

    # Re-running is the recovery: no repair command exists.
    report = migrate_legacy_bypass_secrets(home, state)
    assert report["migrated"] == ["prj_b"]
    assert report["already_current"] == ["prj_a"]
    assert not _legacy_dir(home).exists()
    assert _primary(state).get("prj_b") == SECOND_SECRET


def test_a_write_that_does_not_read_back_fails_closed(tmp_path):
    home, state = _layout(tmp_path)
    _seed_valid(home, "prj_canary")
    real_set = BypassSecretStore.set

    def tampering(self, project_id, secret):
        return real_set(self, project_id, "SOMETHING-ELSE")

    with patch.object(BypassSecretStore, "set", tampering):
        with pytest.raises(LegacyBypassMigrationError, match="did not read back"):
            migrate_legacy_bypass_secrets(home, state)

    assert _legacy_dir(home).is_dir()
    assert _quarantines(state) == []


def test_recursively_nested_legacy_json_fails_closed_without_a_traceback(
    tmp_path, caplog,
):
    """Deeply nested JSON is a malformed file, not a startup crash.

    ``json.load`` raises ``RecursionError`` on a pathologically nested
    document, and the startup caller only handles ``ValueError`` — so without
    the strict reader catching it the operator gets a traceback instead of the
    actionable, value-free refusal every other malformed file gets.
    """
    home, state = _layout(tmp_path)
    _seed_valid(home, "prj_valid")
    deep = _write_entry(home, "prj_deep.json", "[" * 20000 + "]" * 20000)
    before = deep.read_bytes()

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LegacyBypassMigrationError, match="nested too deeply"):
            migrate_legacy_bypass_secrets(home, state)

    assert deep.read_bytes() == before
    assert _legacy_dir(home).is_dir()
    assert _quarantines(state) == []
    primary_dir = state / BYPASS_STORE_DIRNAME
    assert not primary_dir.exists() or list(primary_dir.iterdir()) == []
    assert MIGRATION_SECRET not in caplog.text


def test_interrupted_retire_leaves_the_legacy_dir_retryable(tmp_path, monkeypatch):
    home, state = _layout(tmp_path)
    _seed_valid(home, "prj_a", "prj_b")
    legacy = _legacy_dir(home)
    attempts = []

    def deny(src, dst):
        attempts.append(dst)
        raise PermissionError(errno.EACCES, "denied by test", str(src))

    _patch_retire_only(monkeypatch, legacy, deny)
    with pytest.raises(LegacyBypassMigrationError, match="could not be moved aside"):
        migrate_legacy_bypass_secrets(home, state)

    # A permission failure is not a name clash, so it is not retried.
    assert len(attempts) == 1
    assert legacy.is_dir()
    assert BypassSecretStore(legacy).get("prj_b") == SECOND_SECRET
    # The primary is already complete, so the retry only retires.
    assert _primary(state).get("prj_a") == MIGRATION_SECRET

    monkeypatch.undo()
    report = migrate_legacy_bypass_secrets(home, state)
    assert report["migrated"] == []
    assert sorted(report["already_current"]) == ["prj_a", "prj_b"]
    assert not legacy.exists()
    assert len(_quarantines(state)) == 1


def test_cross_device_retirement_fails_closed_with_an_actionable_error(
    tmp_path, monkeypatch, caplog,
):
    """EXDEV is refused, explained, and NOT mistaken for a name collision.

    The retirement is a single ``os.replace``, which cannot span filesystems.
    The migration deliberately has no copy+delete fallback: the operator gets a
    refusal naming the cause and the fix, the legacy directory stays where it
    is, and the entries already copied into the primary stay valid.
    """
    home, state = _layout(tmp_path)
    _seed_valid(home, "prj_a", "prj_b")
    legacy = _legacy_dir(home)
    before = {p.name: p.read_bytes() for p in sorted(legacy.iterdir())}
    attempts = []

    def cross_device(src, dst):
        attempts.append(dst)
        raise OSError(errno.EXDEV, os.strerror(errno.EXDEV), str(src))

    _patch_retire_only(monkeypatch, legacy, cross_device)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LegacyBypassMigrationError) as excinfo:
            migrate_legacy_bypass_secrets(home, state)

    message = str(excinfo.value)
    # Distinguishable from the generic collision path: named cause, named fix,
    # and the errno — not "fix the filesystem".
    assert "different filesystems" in message
    assert "EXDEV" in message and str(errno.EXDEV) in message
    assert "WEBSITE_BUILDER_STATE_ROOT" in message
    assert "fix the filesystem" not in message
    assert "could not be moved aside" not in message
    # Not retried: a destination conflict rolls to the next suffix, EXDEV stops.
    assert len(attempts) == 1

    # Nothing deleted, nothing retired, primary work preserved.
    assert legacy.is_dir()
    assert {p.name: p.read_bytes() for p in sorted(legacy.iterdir())} == before
    assert _quarantines(state) == []
    assert sorted(_primary(state).get(pid) for pid in ("prj_a", "prj_b")) == sorted(
        [MIGRATION_SECRET, SECOND_SECRET],
    )
    assert MIGRATION_SECRET not in message
    assert SECOND_SECRET not in caplog.text

    # Correcting the path layout is the whole recovery.
    monkeypatch.undo()
    report = migrate_legacy_bypass_secrets(home, state)
    assert sorted(report["already_current"]) == ["prj_a", "prj_b"]
    assert not legacy.exists()


def test_retire_takes_the_next_free_quarantine_name(tmp_path, monkeypatch):
    """Two migrations in the same second must not overwrite each other."""
    stamp = "19700101T000000Z"
    monkeypatch.setattr(
        secrets_module, "time", _ModuleShim(time, strftime=lambda fmt, t=None: stamp),
    )
    home, state = _layout(tmp_path)
    _seed_valid(home, "prj_a")
    taken = state / f"{BYPASS_STORE_DIRNAME}-migrated-{stamp}"
    taken.mkdir(parents=True)
    (taken / "keep.txt").write_text("an earlier migration", encoding="utf-8")

    report = migrate_legacy_bypass_secrets(home, state)

    quarantine = Path(report["quarantined_to"])
    assert quarantine.name == f"{BYPASS_STORE_DIRNAME}-migrated-{stamp}-1"
    assert (taken / "keep.txt").read_text(encoding="utf-8") == "an earlier migration"
    assert (quarantine / "prj_a.json").is_file()


def test_concurrent_migration_that_lost_the_race_is_a_success_noop(tmp_path, monkeypatch):
    home, state = _layout(tmp_path)
    _seed_valid(home, "prj_a", "prj_b")
    legacy = _legacy_dir(home)

    def peer_won(src, dst):
        # The other process completed the move and its directory is gone.
        shutil.rmtree(legacy)
        raise FileNotFoundError(str(src))

    _patch_retire_only(monkeypatch, legacy, peer_won)
    assert migrate_legacy_bypass_secrets(home, state) is None

    # The peer wrote identical primary content, so nothing is lost.
    assert _primary(state).get("prj_a") == MIGRATION_SECRET
    assert _primary(state).get("prj_b") == SECOND_SECRET
    assert not legacy.exists()


def test_legacy_dir_vanishing_during_the_scan_is_a_success_noop(tmp_path, monkeypatch):
    """A peer that finished the move between the stat and the scan is fine."""
    home, state = _layout(tmp_path)
    _seed_valid(home, "prj_a")
    legacy = _legacy_dir(home)
    real_is_dir, real_exists = Path.is_dir, Path.exists
    with patch.object(
        Path, "is_dir", lambda s: True if s == legacy else real_is_dir(s),
    ), patch.object(
        Path, "exists", lambda s: False if s == legacy else real_exists(s),
    ), patch.object(Path, "iterdir", _refuse_iterdir(legacy)):
        assert migrate_legacy_bypass_secrets(home, state) is None
    assert legacy.is_dir()  # the on-disk state is unchanged by the attempt


def test_legacy_dir_that_cannot_be_scanned_fails_closed(tmp_path):
    """A scan failure that is NOT a vanished directory is not a success."""
    home, state = _layout(tmp_path)
    _seed_valid(home, "prj_a")
    legacy = _legacy_dir(home)
    with patch.object(Path, "iterdir", _refuse_iterdir(legacy)):
        with pytest.raises(LegacyBypassMigrationError, match="disappeared"):
            migrate_legacy_bypass_secrets(home, state)
    assert legacy.is_dir()
    assert not (state / BYPASS_STORE_DIRNAME).exists()
    assert _quarantines(state) == []


def test_legacy_symlink_directory_fails_closed(tmp_path, monkeypatch):
    """Path safety: a symlinked store directory is not ours to move."""
    home, state = _layout(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _force_symlink(monkeypatch, home / BYPASS_STORE_DIRNAME, elsewhere)
    with pytest.raises(LegacyBypassMigrationError, match="symbolic link"):
        migrate_legacy_bypass_secrets(home, state)
    # Nothing was read from it and nothing was moved.
    assert not (elsewhere / "prj_a.json").exists()
    assert not (state / BYPASS_STORE_DIRNAME).exists()


# ---------------------------------------------------------------------------
# Temp leftovers and secret hygiene
# ---------------------------------------------------------------------------


def test_temp_leftover_is_retained_in_quarantine(tmp_path, caplog):
    home, state = _layout(tmp_path)
    _seed_valid(home, "prj_a")
    leftover = _legacy_dir(home) / "tmpZZZZZZ.tmp"
    leftover.write_text(MIGRATION_SECRET, encoding="utf-8")

    with caplog.at_level(logging.DEBUG):
        report = migrate_legacy_bypass_secrets(home, state)

    assert report["temp_leftovers"] == 1
    assert report["migrated"] == ["prj_a"]
    quarantine = Path(report["quarantined_to"])
    # Carried, never parsed and never deleted.
    assert (quarantine / "tmpZZZZZZ.tmp").read_text(encoding="utf-8") == MIGRATION_SECRET
    assert (quarantine / "prj_a.json").is_file()
    assert _primary(state).get("prj_a") == MIGRATION_SECRET
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings and "1 temporary file(s)" in warnings[0].getMessage()
    assert MIGRATION_SECRET not in caplog.text


def _scenario_primary_wins(home, state, monkeypatch):
    """The already_current branch, with two different real secret values.

    Both canaries are live here — the legacy value and the primary value that
    wins over it — so a leak on this branch is catchable.
    """
    _seed_valid(home, "prj_canary")            # legacy: MIGRATION_SECRET
    primary = _primary(state)
    primary.set("prj_canary", PRIMARY_SECRET)   # primary wins: a different value
    before = primary.path_for("prj_canary").read_bytes()

    report = migrate_legacy_bypass_secrets(home, state)

    # The branch really was taken, the primary was not rewritten, and the
    # legacy store was still retired normally.
    assert report["already_current"] == ["prj_canary"]
    assert report["migrated"] == []
    assert primary.path_for("prj_canary").read_bytes() == before
    assert primary.get("prj_canary") == PRIMARY_SECRET
    assert not _legacy_dir(home).exists()
    assert Path(report["quarantined_to"]).is_dir()


def _scenario_success(home, state, monkeypatch):
    _seed_valid(home, "prj_a", "prj_b")
    migrate_legacy_bypass_secrets(home, state)


def _scenario_temp_leftover(home, state, monkeypatch):
    _seed_valid(home, "prj_a")
    _write_entry(home, "tmpZZZZZZ.tmp", MIGRATION_SECRET)
    migrate_legacy_bypass_secrets(home, state)


def _scenario_malformed_json(home, state, monkeypatch):
    _seed_valid(home, "prj_a")
    _write_entry(home, "prj_b.json", '{"secret": "%s"' % MIGRATION_SECRET)
    migrate_legacy_bypass_secrets(home, state)


def _scenario_not_an_object(home, state, monkeypatch):
    _seed_valid(home, "prj_a")
    _write_entry(home, "prj_b.json", '["%s"]' % MIGRATION_SECRET)
    migrate_legacy_bypass_secrets(home, state)


def _scenario_blank_secret(home, state, monkeypatch):
    _seed_valid(home, "prj_a")
    _write_entry(home, "prj_b.json", '{"secret": ""}')
    migrate_legacy_bypass_secrets(home, state)


def _scenario_bad_entry_name(home, state, monkeypatch):
    _seed_valid(home, "prj_a")
    _write_entry(home, "notes.txt", MIGRATION_SECRET)
    migrate_legacy_bypass_secrets(home, state)


def _scenario_bad_project_id(home, state, monkeypatch):
    _seed_valid(home, "prj_a")
    _write_entry(home, "..%2fescape.json", json.dumps({"secret": MIGRATION_SECRET}))
    migrate_legacy_bypass_secrets(home, state)


def _scenario_corrupt_primary(home, state, monkeypatch):
    _seed_valid(home, "prj_canary")
    primary = _primary(state)
    primary.path_for("prj_canary").write_text("{ broken", encoding="utf-8")
    migrate_legacy_bypass_secrets(home, state)


def _scenario_unreadable_primary(home, state, monkeypatch):
    _seed_valid(home, "prj_canary")
    primary = _primary(state)
    primary.path_for("prj_canary").write_text(
        json.dumps({"project_id": "prj_canary", "secret": ""}), encoding="utf-8",
    )
    migrate_legacy_bypass_secrets(home, state)


def _scenario_readback_mismatch(home, state, monkeypatch):
    _seed_valid(home, "prj_a")
    real_set = BypassSecretStore.set

    def tampering(self, project_id, secret):
        return real_set(self, project_id, SECOND_SECRET)

    with patch.object(BypassSecretStore, "set", tampering):
        migrate_legacy_bypass_secrets(home, state)


def _scenario_write_failure(home, state, monkeypatch):
    _seed_valid(home, "prj_a")

    def failing(self, project_id, secret):
        raise OSError(errno.EIO, "simulated disk failure")

    with patch.object(BypassSecretStore, "set", failing):
        migrate_legacy_bypass_secrets(home, state)


def _scenario_recursively_nested(home, state, monkeypatch):
    _seed_valid(home, "prj_a")
    _write_entry(home, "prj_deep.json", "[" * 20000 + "]" * 20000)
    migrate_legacy_bypass_secrets(home, state)


def _scenario_directory_entry(home, state, monkeypatch):
    _seed_valid(home, "prj_a")
    (home / BYPASS_STORE_DIRNAME / "sub").mkdir()
    migrate_legacy_bypass_secrets(home, state)


def _scenario_symlink_entry(home, state, monkeypatch):
    _seed_valid(home, "prj_a")
    target = _write_entry(home, "prj_real.json", json.dumps({"secret": MIGRATION_SECRET}))
    _force_symlink(monkeypatch, home / BYPASS_STORE_DIRNAME / "prj_linked.json", target)
    migrate_legacy_bypass_secrets(home, state)


# (name, scenario, must_fail_closed)
_SCENARIOS = [
    ("success", _scenario_success, False),
    ("primary-wins", _scenario_primary_wins, False),
    ("temp-leftover", _scenario_temp_leftover, False),
    ("malformed-json", _scenario_malformed_json, True),
    ("not-an-object", _scenario_not_an_object, True),
    ("blank-secret", _scenario_blank_secret, True),
    ("bad-entry-name", _scenario_bad_entry_name, True),
    ("bad-project-id", _scenario_bad_project_id, True),
    ("corrupt-primary", _scenario_corrupt_primary, True),
    ("unreadable-primary", _scenario_unreadable_primary, True),
    ("readback-mismatch", _scenario_readback_mismatch, True),
    ("write-failure", _scenario_write_failure, True),
    ("recursively-nested", _scenario_recursively_nested, True),
    ("directory-entry", _scenario_directory_entry, True),
    ("symlink-entry", _scenario_symlink_entry, True),
]


def test_no_secret_value_appears_in_logs_or_errors(tmp_path, monkeypatch, caplog):
    """The canary must not reach any log record or any failure message.

    Every branch runs here — successful, malformed, conflicting, interrupted
    — because a leak only has to happen once.
    """
    raised = {}
    with caplog.at_level(logging.DEBUG):
        for name, scenario, _must_fail in _SCENARIOS:
            home = tmp_path / name / ".hermes-website"
            state = tmp_path / name / ".website-builder" / "state"
            home.mkdir(parents=True)
            try:
                scenario(home, state, monkeypatch)
            except LegacyBypassMigrationError as exc:
                raised[name] = str(exc)

    # The branches that must fail closed actually did, so the blob below is
    # not just the happy path re-checked.
    assert {name for name, _, must_fail in _SCENARIOS if must_fail} == set(raised)

    blob = caplog.text + "\n" + "\n".join(raised.values())
    assert MIGRATION_SECRET not in blob
    assert SECOND_SECRET not in blob
    assert PRIMARY_SECRET not in blob
