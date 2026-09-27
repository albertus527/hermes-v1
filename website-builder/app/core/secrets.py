"""PHASE E — tiny local secret store for per-project Vercel automation bypass.

The Vercel Deployment Protection "automation bypass" secret is a PROJECT-
SPECIFIC credential used only to let the smoke browser through a protected
``*.vercel.app`` preview. It MUST NOT be persisted in ProjectState,
conversation registry, deployment/OperationResult metadata, Telegram,
screenshots, logs, Git, or test snapshots.

This module is the smallest possible local secret store — NOT a generic vault:

  * separate from all normal project/conversation state,
  * separate from every Hermes profile home (see ``BypassSecretStore`` — a
    secret under ``$HERMES_HOME`` is readable by the generation agent),
  * keyed by the IMMUTABLE Vercel project ID (e.g. ``prj_...``),
  * one JSON file per project under ``<state_root>/vercel-bypass/``,
  * file mode 0600 and parent directory 0700 where supported,
  * atomic writes (temp file + fsync + os.replace),
  * the secret value is never included in ``repr`` / logs.

No keyring, no KMS, no encryption layer, no rotation framework. Backward
compatibility: an ``VERCEL_AUTOMATION_BYPASS_SECRET`` env var is honoured as an
optional fallback/override, but a project-specific stored secret always wins.

This module also owns the R1 -> R2 upgrade path for that store: an install that
provisioned secrets under its generation profile home has to be able to MOVE
them, because the profile guard refuses to start against a profile that still
holds one. See :func:`migrate_legacy_bypass_secrets`.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Vercel project IDs are opaque ``prj_...`` tokens. Validate strictly so a
# malformed value can never escape the store directory (path traversal).
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_ENV_FALLBACK = "VERCEL_AUTOMATION_BYPASS_SECRET"

# The single on-disk name of the store, used wherever a root is joined to it:
# the production wiring, the R1 -> R2 migration, and (asserted by test) the
# credential policy's forbidden profile subpaths. One name, so the relocation
# and the guard that enforces it cannot drift apart.
BYPASS_STORE_DIRNAME = "vercel-bypass"


class LegacyBypassMigrationError(ValueError):
    """The legacy Vercel bypass store could not be migrated safely.

    A ``ValueError`` subclass on purpose: the two existing fail-closed callers
    (startup preflight and the spawn seam's profile guard) already handle
    ``ValueError`` as "this profile is unsafe, refuse", and this error means
    exactly that. Tests use the concrete type to tell the migration apart from
    an unrelated ``ValueError``.
    """


class BypassSecretStore:
    """Filesystem-backed per-project automation-bypass secret store.

    Layout: ``<root>/vercel-bypass/<project_id>.json`` with the secret under a
    single ``secret`` key. The file (and, where supported, its parent
    directory) is restricted to the owner.

    ROOT PLACEMENT IS A SECURITY BOUNDARY, not a tidiness choice.

    This store must NOT live under a Hermes profile home. The generation agent
    (FRONTEND) is launched with ``HERMES_HOME`` pointing at exactly that
    directory, holds ``file`` and ``terminal`` tools, runs under
    ``HERMES_YOLO_MODE=1`` so there is no human approval gate on a command,
    and is told the profile path verbatim by the
    ``website-builder-environment`` skill. A credential stored there is
    therefore readable by a generation role no matter how carefully the
    process environment was scoped — environment isolation is irrelevant to a
    file. ``<root>`` is the application's own state root, which is never placed
    in any child environment.

    ``legacy_roots`` keeps pre-existing stores readable so a relocation never
    silently discards a provisioned secret. Reads consult the primary root
    first; writes always land in the primary.

    Once startup has run, ``legacy_roots`` is a BELT-AND-BRACES read path
    rather than the migration mechanism: preflight calls
    :func:`migrate_legacy_bypass_secrets`, which moves the pre-R2 store into
    the primary root and renames the original aside. The read path stays for
    a store that reappears (an operator restoring a profile backup, a state
    root pointed at a fresh machine) so a provisioned secret still resolves
    instead of silently vanishing.
    """

    def __init__(self, root: Path, *, legacy_roots: Optional[Iterable[Path]] = None):
        self.root = Path(root)
        self._legacy_roots = [Path(r) for r in (legacy_roots or ())]
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ------------------------------------------------------------------
    # Paths / permissions
    # ------------------------------------------------------------------

    def _dir(self) -> Path:
        d = self.root
        d.mkdir(parents=True, exist_ok=True)
        _restrict_mode(d, 0o700)
        return d

    def _path(self, project_id: str) -> Path:
        if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
            raise ValueError("Invalid Vercel project id")
        return self._dir() / f"{project_id}.json"

    def path_for(self, project_id: str) -> Path:
        """Return where *project_id*'s secret file lives (or would live).

        The public form of the same resolution the read/write paths use, so
        the legacy migration can ask "does the primary already hold this
        project?" without duplicating the id validation that keeps a
        traversal-shaped name from escaping the store directory. Raises
        ``ValueError`` for an id the store would refuse to serve anyway.
        """
        return self._path(project_id)

    def _legacy_paths(self, project_id: str) -> list:
        if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
            return []
        return [r / f"{project_id}.json" for r in self._legacy_roots]

    def _lock_for(self, project_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(project_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[project_id] = lock
            return lock

    # ------------------------------------------------------------------
    # Read / write
    # ------------------------------------------------------------------

    @staticmethod
    def _read_secret(path: Path) -> Optional[str]:
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            logger.warning("Unreadable bypass secret file for project (redacted)")
            return None
        if not isinstance(payload, dict):
            return None
        secret = payload.get("secret")
        return secret if isinstance(secret, str) and secret else None

    def get(self, project_id: str) -> Optional[str]:
        """Return the stored secret for *project_id* or None. Never logs it."""
        try:
            paths = [self._path(project_id)]
        except ValueError:
            return None
        paths.extend(self._legacy_paths(project_id))
        for path in paths:
            secret = self._read_secret(path)
            if secret:
                return secret
        return None

    def set(self, project_id: str, secret: str) -> None:
        """Atomically persist ``secret`` for ``project_id`` (mode 0600)."""
        if not isinstance(secret, str) or not secret:
            raise ValueError("Invalid bypass secret")
        with self._lock_for(project_id):
            path = self._path(project_id)
            fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            try:
                try:
                    os.fchmod(fd, 0o600)
                except (AttributeError, OSError):
                    # Windows / unsupported: best effort. The parent dir is
                    # still restricted where supported.
                    pass
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"project_id": project_id, "secret": secret}, f)
                    f.flush()
                    os.fsync(f.fileno())
                _restrict_mode(Path(tmp_path), 0o600)
                os.replace(tmp_path, path)
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)

    # ------------------------------------------------------------------
    # Resolution (stored -> env fallback)
    # ------------------------------------------------------------------

    def resolve(self, project_id: Optional[str]) -> Optional[str]:
        """Resolve the bypass secret for a Vercel project.

        Priority (PHASE E contract):
          1. project-specific STORED secret,
          2. ``VERCEL_AUTOMATION_BYPASS_SECRET`` env fallback,
          3. None (no bypass).
        """
        if project_id:
            stored = self.get(project_id)
            if stored:
                return stored
        env_value = os.environ.get(_ENV_FALLBACK, "").strip()
        return env_value or None

    def has_stored(self, project_id: str) -> bool:
        return bool(self.get(project_id))


def _restrict_mode(path: Path, mode: int) -> None:
    """Best-effort chmod; silently no-ops on platforms without POSIX modes."""
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError):
        pass


def file_mode(path: Path) -> Optional[int]:
    """Return the low permission bits of ``path`` (test/introspection helper)."""
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# R1 -> R2 legacy store migration
# ---------------------------------------------------------------------------


def _strict_read(path: Path, project_id: str) -> str:
    """Return the secret in *path*, or raise — never guess, never skip.

    Deliberately NOT :meth:`BypassSecretStore.get`. That method is the normal
    read path, where a malformed file degrades to "no bypass" and is retried
    on the next provisioning round. A migration is the opposite case: the
    legacy file is the last copy of a provisioned secret, so anything this
    cannot fully understand must abort the whole migration with the legacy
    directory left in place — a value this operator can look at — rather than
    be skipped and then renamed away.

    Requires a JSON object carrying a non-empty string ``secret``; a
    ``project_id`` key, when present, must equal *project_id* (the filename
    stem). Every message names the path and the project id and never the
    value, the file body, or the parse error's own text.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError:
        raise LegacyBypassMigrationError(
            f"Vercel bypass secret file could not be read: {path}"
        ) from None
    except ValueError:
        # json.JSONDecodeError and UnicodeDecodeError. The exception text is
        # deliberately dropped: a decoder message can quote the offending
        # input.
        raise LegacyBypassMigrationError(
            f"Vercel bypass secret file is not valid JSON: {path}"
        ) from None
    except RecursionError:
        # A pathologically nested document trips the decoder's recursion guard.
        # It is a malformed file like any other, so it fails closed through the
        # same sanitized path instead of escaping as a traceback out of startup
        # (the caller only handles ValueError).
        raise LegacyBypassMigrationError(
            f"Vercel bypass secret file is nested too deeply to parse: {path}"
        ) from None
    if not isinstance(payload, dict):
        raise LegacyBypassMigrationError(
            f"Vercel bypass secret file does not contain a JSON object: {path}"
        )
    secret = payload.get("secret")
    if not isinstance(secret, str) or not secret:
        raise LegacyBypassMigrationError(
            f"Vercel bypass secret file for project {project_id} carries no "
            f"usable secret: {path}"
        )
    declared = payload.get("project_id")
    if declared is not None and declared != project_id:
        raise LegacyBypassMigrationError(
            f"Vercel bypass secret file for project {project_id} declares a "
            f"different project id: {path}"
        )
    return secret


def _is_destination_conflict(exc: OSError) -> bool:
    """True when *exc* means "the rename target already exists"."""
    if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
        return True
    return getattr(exc, "winerror", None) in (80, 183)  # FILE_EXISTS / ALREADY_EXISTS


def _quarantine_legacy_dir(legacy_dir: Path, state_root: Path) -> Optional[Path]:
    """Rename *legacy_dir* aside under *state_root*; return where it landed.

    One ``os.replace`` — atomic within a filesystem, and the same call the
    store already uses for every secret write, so there is no new durability
    assumption. Across filesystems the rename is refused (``EXDEV``) rather
    than emulated: see the handler below. Nothing is ever deleted: the retired
    directory keeps the file contents an operator may still need, under a
    timestamped name that is not a registered secret-store path.

    Returns None when a concurrent start already performed the move, which is
    tolerated (both processes compute identical primary content, and the
    winner's quarantine is just as complete).
    """
    try:
        state_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LegacyBypassMigrationError(
            f"The Website Builder state root could not be created at "
            f"{state_root} ({type(exc).__name__}). The legacy Vercel bypass "
            "store is untouched; fix the filesystem and start again."
        ) from None
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = f"{BYPASS_STORE_DIRNAME}-migrated-{stamp}"
    for suffix in ("", *(f"-{n}" for n in range(1, 10))):
        target = state_root / (base + suffix)
        if target.exists():
            continue
        try:
            os.replace(legacy_dir, target)
        except FileNotFoundError:
            if not legacy_dir.exists():
                return None  # A peer start completed this migration first.
            raise LegacyBypassMigrationError(
                f"The legacy Vercel bypass store at {legacy_dir} could not be "
                f"moved aside to {target}, and it is still in place. Nothing "
                "was deleted; fix the filesystem and start again."
            ) from None
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                # Cross-device. os.replace cannot span filesystems and there is
                # deliberately no copy+delete fallback: retiring the store
                # non-atomically would trade a recoverable refusal for a window
                # where the only copy of a secret is neither place. The legacy
                # directory stays exactly where it is and the entries already
                # copied into the current store stay valid, so the operator
                # only has to fix the path layout and start again.
                raise LegacyBypassMigrationError(
                    f"The legacy Vercel bypass store at {legacy_dir} and the "
                    f"Website Builder state root {state_root} are on different "
                    f"filesystems, so the legacy store cannot be atomically "
                    f"retired into it (errno {exc.errno}, EXDEV). Nothing was "
                    "deleted: the legacy directory is untouched and the secrets "
                    "already copied into the current store are valid. Point "
                    "WEBSITE_BUILDER_STATE_ROOT (or state_root in config.yaml) "
                    "at a directory on the same filesystem as the profile home "
                    "for this one-time migration, then start again."
                ) from None
            if _is_destination_conflict(exc):
                continue
            raise LegacyBypassMigrationError(
                f"The legacy Vercel bypass store at {legacy_dir} could not be "
                f"moved aside to {target} ({type(exc).__name__}). Nothing was "
                "deleted; fix the filesystem and start again."
            ) from None
        return target
    raise LegacyBypassMigrationError(
        f"The legacy Vercel bypass store at {legacy_dir} could not be retired: "
        f"no free quarantine name under {state_root} after 10 attempts. The "
        "legacy directory is untouched; remove older "
        f"{base}* directories and start again."
    )


def _assert_state_root_outside_profile(hermes_home: Path, state_root: Path) -> None:
    """Fail closed unless the state root is strictly outside the profile home.

    The Batch A invariant is that NO privileged bypass store may exist anywhere
    underneath ``HERMES_HOME`` — the one directory the generation plane is
    pointed at, holding file and terminal tools under ``HERMES_YOLO_MODE=1``.
    ``assert_profile_home_clean`` enforces that for the profile home's DIRECT
    children, which is the right shape for a *scan*: it must stay O(entries)
    over a profile that holds sessions and caches, and it must not grow a
    recursive walk. That leaves a gap this function has to close instead,
    because it is the code that MOVES secret material: a state root that is the
    profile home, or anywhere under it, would relocate a provisioned secret
    into a directory the guard cannot see — ``HERMES_HOME/state/vercel-bypass``
    passes a direct-children check — or rename the store aside under a name
    the guard does not recognise while the secret stays behind.

    Both sides are resolved first, so a symlinked state root cannot smuggle the
    store back under the profile, and a symlinked profile home is compared
    against where it actually points rather than where it is named.

    Paths only; never a secret or a file body.
    """
    home = Path(hermes_home).expanduser().resolve()
    state = Path(state_root).expanduser().resolve()
    if state == home or state.is_relative_to(home):
        raise LegacyBypassMigrationError(
            f"The Website Builder state root {state} is the generation profile "
            f"home {home} or lives inside it. Migrating the Vercel bypass "
            "secret store there would place a credential under a directory the "
            "generation agent can read, which is exactly what the store's root "
            "placement forbids. Nothing was changed. Point "
            "WEBSITE_BUILDER_STATE_ROOT (or state_root in config.yaml) at a "
            "directory outside the profile home — the defaults are "
            "~/.website-builder/state against the ~/.hermes-website profile — "
            "and start again."
        )


def migrate_legacy_bypass_secrets(
    hermes_home: Path, state_root: Path,
) -> Optional[dict]:
    """Move a pre-R2 ``$HERMES_HOME/vercel-bypass`` store into *state_root*.

    Why this has to exist. R1 wrote every project's Vercel automation-bypass
    secret under the generation profile home, which R2 correctly refused to
    keep doing: ``HERMES_HOME`` *is* that directory for the FRONTEND child,
    which holds file and terminal tools under ``HERMES_YOLO_MODE=1``. R2
    relocated the store to the application's own state root and added
    ``assert_profile_home_clean`` to refuse a profile that still holds one.
    Those two changes together leave an un-migrated R1 install unable to
    start at all: the guard runs at startup preflight, before anything could
    move the file, so the operator's only apparent recourse is deleting
    provisioned secrets. This function is that upgrade path.

    Deliberately narrow, and deliberately paranoid:

    * The state root is proven to sit outside the profile home first
      (:func:`_assert_state_root_outside_profile`), before a single byte is
      written or renamed.
    * No ``hermes_home`` entry other than the store directory is touched.
    * The store is moved, never deleted. A timestamped copy stays under the
      state root for the operator to remove once they are satisfied; the
      original name is gone, so nothing resolves from the profile any more.
    * A ``.tmp`` leftover (this store's own ``mkstemp`` crash artifact) is
      carried along and reported by count. Anything else unexpected — a
      subdirectory, a symlink, a non-``.json`` name, a ``.json`` whose stem is
      not a valid project id, unparsable JSON, a missing/blank secret — fails
      the migration with the legacy directory untouched.
    * Where the primary store already holds a project, the primary wins
      untouched and is only verified as readable. An unreadable primary next
      to a readable legacy copy is a conflict the operator must resolve, not
      something to silently overwrite or repair.
    * Every value written is read back and compared before the legacy
      directory is retired.
    * Messages and logs carry ids, counts and paths only — never a secret, a
      file body, or a decoder message that could quote one.

    Crash safety. The only irreversible step is the single rename that retires
    the legacy directory, and it runs only after every entry has been
    copied-and-verified into the primary. Everything before it is re-derivable
    and repeatable: each write is atomic (temp + fsync + ``os.replace``), and
    a re-run takes the ``already_current`` branch for entries that landed, so
    an interrupted migration is retried by the next start rather than needing
    a repair command. A failure at any point leaves the legacy directory in
    place, so no secret can be lost by trying again.

    Returns None when there is nothing to do (a clean install, or a start that
    already migrated) — including when a concurrent start won the race, since
    that peer wrote the same primary content. Otherwise returns a report of
    ids and counts only::

        {"migrated": [...], "already_current": [...],
         "quarantined_to": Path, "temp_leftovers": int}

    Raises:
        LegacyBypassMigrationError: the legacy store could not be migrated
            with certainty (including a state root that is not outside the
            profile home). The legacy directory is still in place.
    """
    # The invariant, before anything else: this function moves credential
    # material, so the destination must be provably outside the profile home.
    _assert_state_root_outside_profile(hermes_home, state_root)

    legacy_dir = Path(hermes_home) / BYPASS_STORE_DIRNAME
    if legacy_dir.is_symlink():
        # Path safety: a symlinked store directory is not the store this
        # function knows how to reason about (it could point anywhere, and its
        # contents are not necessarily secrets we may relocate).
        raise LegacyBypassMigrationError(
            "The legacy Vercel bypass store path is a symbolic link: "
            f"{legacy_dir}. Refusing to migrate through a link; inspect it and "
            "replace it with the real directory."
        )
    if not legacy_dir.is_dir():
        return None  # Clean install, or already migrated.

    try:
        entries = sorted(legacy_dir.iterdir())
    except FileNotFoundError:
        if not legacy_dir.exists():
            return None  # A peer start completed this migration first.
        raise LegacyBypassMigrationError(
            f"The legacy Vercel bypass store at {legacy_dir} disappeared while "
            "it was being read and is still in place; start again."
        ) from None
    except OSError as exc:
        raise LegacyBypassMigrationError(
            f"The legacy Vercel bypass store at {legacy_dir} could not be "
            f"listed ({type(exc).__name__}). Nothing was changed; start again."
        ) from None

    # The primary must be self-sufficient AFTER the move, so it is built with
    # no legacy root: if a read still resolved from the profile afterwards,
    # the relocation would not actually have moved anything.
    primary = BypassSecretStore(Path(state_root) / BYPASS_STORE_DIRNAME)

    migrated: list = []
    already_current: list = []
    temp_leftovers = 0

    for entry in entries:
        if entry.is_dir() or entry.is_symlink():
            raise LegacyBypassMigrationError(
                f"The legacy Vercel bypass store at {legacy_dir} holds a "
                f"directory or link ({entry.name}) that the store never "
                "creates. Refusing to migrate an unknown layout; nothing was "
                "changed."
            )
        if entry.suffix == ".tmp":
            # This store's own mkstemp artifact, left by a crash mid-write.
            # It holds no committed secret and cannot be attributed to a
            # project, so it travels with the directory rather than being
            # parsed or deleted. Counted only: naming it would leak nothing,
            # but a count is all the operator needs.
            temp_leftovers += 1
            continue
        if entry.suffix != ".json":
            raise LegacyBypassMigrationError(
                f"The legacy Vercel bypass store at {legacy_dir} holds an "
                f"unexpected file ({entry.name}) that is not a project secret. "
                "Refusing to migrate an unknown layout; nothing was changed."
            )
        project_id = entry.stem
        try:
            target = primary.path_for(project_id)
        except ValueError:
            raise LegacyBypassMigrationError(
                f"The legacy Vercel bypass store at {legacy_dir} holds a file "
                f"({entry.name}) whose name is not a valid Vercel project id. "
                "Refusing to migrate; nothing was changed."
            ) from None
        secret = _strict_read(entry, project_id)

        if target.exists():
            try:
                _strict_read(target, project_id)
            except LegacyBypassMigrationError:
                raise LegacyBypassMigrationError(
                    f"The current Vercel bypass store holds an unreadable entry "
                    f"for project {project_id} at {target} while a legacy copy "
                    f"exists at {entry}. Refusing to choose between them: "
                    "restore or remove one of the two files and start again."
                ) from None
            already_current.append(project_id)
            continue

        try:
            primary.set(project_id, secret)
        except OSError as exc:
            raise LegacyBypassMigrationError(
                f"The Vercel bypass secret for project {project_id} could not "
                f"be written to the current store at {target} "
                f"({type(exc).__name__}). Nothing was deleted; fix the "
                "filesystem and start again."
            ) from None
        if _strict_read(target, project_id) != secret:
            raise LegacyBypassMigrationError(
                f"The Vercel bypass secret for project {project_id} did not "
                f"read back identically after being written to {target}. "
                "Refusing to retire the legacy store; nothing was deleted."
            )
        migrated.append(project_id)

    if temp_leftovers:
        logger.warning(
            "The legacy Vercel bypass store holds %d temporary file(s) from an "
            "interrupted write. They are retained, unread, in the quarantine "
            "directory; the operator can delete that directory once the "
            "migration is confirmed.",
            temp_leftovers,
        )

    quarantine = _quarantine_legacy_dir(legacy_dir, Path(state_root))
    if quarantine is None:
        return None  # Peer start already retired the legacy directory.
    _restrict_mode(quarantine, 0o700)

    logger.info(
        "Migrated the legacy Vercel bypass secret store out of the generation "
        "profile: %d project(s) migrated, %d already current, %d temp "
        "leftover(s) retained in quarantine. Legacy path: %s. Quarantine: %s",
        len(migrated), len(already_current), temp_leftovers, legacy_dir, quarantine,
    )
    return {
        "migrated": migrated,
        "already_current": already_current,
        "quarantined_to": quarantine,
        "temp_leftovers": temp_leftovers,
    }
