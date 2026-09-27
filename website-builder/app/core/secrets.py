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
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import tempfile
import threading
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Vercel project IDs are opaque ``prj_...`` tokens. Validate strictly so a
# malformed value can never escape the store directory (path traversal).
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_ENV_FALLBACK = "VERCEL_AUTOMATION_BYPASS_SECRET"


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
