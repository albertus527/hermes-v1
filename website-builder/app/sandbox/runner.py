"""Isolated project runner for Website Builder R1.

One project = one isolated workspace. MAX_WORKERS=1.
No containers-per-project, no distributed concurrency.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from app.core import credentials
from app.core.state import ProjectStateStore


# MAX_WORKERS=1 — one active project mutation at a time
MAX_WORKERS = 1

# Port allocation: small deterministic range
_PORT_BASE = 5100
_PORT_RANGE = 100


class WorkspaceError(ValueError):
    """Raised when workspace validation fails."""


# ---------------------------------------------------------------------------
# Pointer workspaces (R2-C)
# ---------------------------------------------------------------------------
# A revision workspace is DISPOSABLE. It is materialized from a proven exact
# source into ``<project>/.ops/rev-<seq>/`` and only becomes the project's
# workspace when the ``current`` pointer is atomically swapped to name it.
#
# ``current`` holds ONE line: the validated operation token ``rev-<digits>``.
# It never holds a commit, a branch or a path -- so no commit or branch
# identity can ever be inferred from it, and a pointer that somehow holds one
# is a refusal rather than a guess.
#
# The historical witness that a project has crossed the pointer boundary is
# ``deployment["pointer_mode"]``, a monotonic boolean. It is consulted because
# ``deployment["hydration"]`` is a SINGLE record that a later operation may
# supersede: a project whose record was replaced by a new operation's RESERVED
# while its ``current`` file was lost must NOT silently fall back to the legacy
# mutable workspace. That fallback is precisely the source-of-truth behaviour
# this layout exists to remove.

WORKSPACE_POINTER_INVALID = "WORKSPACE_POINTER_INVALID"
WORKSPACE_POINTER_MISSING_AFTER_HYDRATION = "WORKSPACE_POINTER_MISSING_AFTER_HYDRATION"

POINTER_FILENAME = "current"
OPS_DIRNAME = ".ops"
#: Runtime directories that live INSIDE the resolved workspace, wherever that
#: workspace is. In pointer mode they move with the revision.
RUNTIME_DIRNAMES = (".hermes", ".browser", ".runtime")

_OP_TOKEN_RE = re.compile(r"rev-[0-9]+")
_SHA1_RE = re.compile(r"[0-9a-f]{40}")


class PointerResolutionError(WorkspaceError):
    """The ``current`` pointer could not be resolved. Carries its error code."""

    def __init__(self, error_code: str, message: str = ""):
        self.error_code = error_code
        super().__init__(message or error_code)


def validate_operation_token(token) -> str:
    """Only a bare ``rev-<digits>`` operation token is ever accepted."""
    if not isinstance(token, str) or not _OP_TOKEN_RE.fullmatch(token):
        raise PointerResolutionError(
            WORKSPACE_POINTER_INVALID,
            f"Invalid workspace operation token: {token!r}")
    return token


def validate_project_id(project_id: str) -> str:
    """Validate a project ID to prevent path traversal.

    Rules:
    - alphanumeric, hyphens, underscores only
    - no path separators
    - no leading/trailing dots or hyphens
    - max 64 chars
    """
    if not project_id:
        raise WorkspaceError("Project ID cannot be empty")
    if len(project_id) > 64:
        raise WorkspaceError("Project ID too long (max 64 chars)")
    if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9_-]*[a-zA-Z0-9])?", project_id):
        raise WorkspaceError(
            f"Invalid project ID: {project_id!r}. "
            "Must be alphanumeric with hyphens/underscores, "
            "no leading/trailing dots or hyphens."
        )
    if ".." in project_id or "/" in project_id or "\\" in project_id:
        raise WorkspaceError(f"Path traversal detected in project ID: {project_id!r}")
    try:
        ProjectStateStore._validate_id(project_id)
    except ValueError as exc:
        raise WorkspaceError(str(exc)) from exc
    return project_id


def validate_workspace_root(root: Path) -> Path:
    """Validate and resolve the workspace root."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def project_workspace_path(workspace_root: Path, project_id: str) -> Path:
    """Return the validated workspace path for a project.

    Guarantees the path is contained within workspace_root.
    """
    project_id = validate_project_id(project_id)
    root = validate_workspace_root(workspace_root)
    path = (root / project_id).resolve()

    # Ensure containment
    if not path.is_relative_to(root):
        raise WorkspaceError(
            f"Project path {path} escapes workspace root {root}"
        )
    return path


@dataclass
class ProcessInfo:
    """Tracked child process."""

    pid: int
    command: List[str]
    started_at: float = field(default_factory=time.time)
    port: Optional[int] = None


class ProcessTracker:
    """Track and safely clean project child processes."""

    def __init__(self):
        self._processes: Dict[int, ProcessInfo] = {}
        self._lock = __import__("threading").Lock()

    def register(self, pid: int, command: List[str], port: Optional[int] = None) -> None:
        with self._lock:
            self._processes[pid] = ProcessInfo(pid=pid, command=command, port=port)

    def unregister(self, pid: int) -> None:
        with self._lock:
            self._processes.pop(pid, None)

    def cleanup(self, pid: int) -> bool:
        """Terminate a tracked process. Returns True if terminated."""
        with self._lock:
            info = self._processes.pop(pid, None)
        if info is None:
            return False
        try:
            if sys.platform == "win32":
                os.kill(pid, signal.SIGTERM)
            else:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    def cleanup_all(self) -> int:
        """Terminate all tracked processes. Returns count terminated."""
        with self._lock:
            pids = list(self._processes.keys())
        count = 0
        for pid in pids:
            if self.cleanup(pid):
                count += 1
        return count

    def active_ports(self) -> Set[int]:
        with self._lock:
            return {p.port for p in self._processes.values() if p.port is not None}


def _port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        try:
            s.connect((host, port))
            return False  # something is listening
        except OSError:
            return True


class PortAllocator:
    """Small deterministic port allocator. Not a service."""

    def __init__(self, base: int = _PORT_BASE, range_size: int = _PORT_RANGE, is_free_fn=None):
        self.base = base
        self.range_size = range_size
        self._allocated: Set[int] = set()
        self.is_free_fn = is_free_fn or _port_is_free

    def allocate(self) -> int:
        """Allocate the next available port in the deterministic range."""
        for offset in range(self.range_size):
            port = self.base + offset
            if port not in self._allocated:
                if self.is_free_fn is not None and not self.is_free_fn(port):
                    continue
                self._allocated.add(port)
                return port
        raise RuntimeError("No available ports in deterministic range")

    def release(self, port: int) -> None:
        self._allocated.discard(port)

    def is_allocated(self, port: int) -> bool:
        return port in self._allocated


class ProjectRunner:
    """Isolated project runner for Website Builder R1.

    MAX_WORKERS=1: one active project mutation at a time.
    """

    def __init__(
        self,
        workspace_root: Path,
        state_store: ProjectStateStore,
        hermes_home: Optional[Path] = None,
    ):
        self.workspace_root = validate_workspace_root(workspace_root)
        self.state_store = state_store
        self.hermes_home = hermes_home or Path.home() / ".hermes-website"
        self.process_tracker = ProcessTracker()
        self.port_allocator = PortAllocator()
        self._active_project: Optional[str] = None
        self._active_lock = __import__("threading").Lock()

    def create_workspace(self, project_id: str) -> Path:
        """Create an isolated workspace for a project.

        Resolve FIRST, then create. Creating the legacy root and discovering
        the operation directory afterwards would leave a second, plausible
        looking workspace behind that every other runtime helper would keep
        using -- which is the exact split-brain this resolver exists to
        prevent. The three runtime directories are created inside whatever
        directory was resolved, so a pointer-mode project binds
        ``WORKSPACE_ROOT``/``HERMES_HOME`` to its current operation workspace.
        """
        path = self.resolve_workspace(project_id)
        path.mkdir(parents=True, exist_ok=True)

        # Create subdirectories
        for name in RUNTIME_DIRNAMES:
            (path / name).mkdir(exist_ok=True)

        return path

    # ------------------------------------------------------------------
    # Pointer-aware workspace resolution (the single authority)
    # ------------------------------------------------------------------

    def project_root(self, project_id: str) -> Path:
        """The project's own directory -- the legacy mutable workspace."""
        return project_workspace_path(self.workspace_root, project_id)

    def ops_dir(self, project_id: str) -> Path:
        """The operations directory. Never created, never followed blindly."""
        return self.project_root(project_id) / OPS_DIRNAME

    def pointer_mode(self, project_id: str) -> bool:
        """Whether this project has ever committed a pointer swap.

        A plain state read, deliberately NOT the writer lock. ``build.py``
        calls ``create_workspace`` from inside a writer block, so a resolver
        that reached for the lock would invert lock order and deadlock. The
        read is a bare file read plus the in-memory migration, which is safe to
        nest. A project with no state file has no ``pointer_mode`` and has
        therefore never crossed the boundary.
        """
        state = self.state_store.load(project_id)
        if state is None:
            return False
        return (state.deployment or {}).get("pointer_mode") is True

    def pointer_token(self, project_id: str) -> Optional[str]:
        """The validated operation token named by ``current``, or ``None``.

        ``None`` means the file is genuinely absent. Anything present but
        unusable -- empty, multi-line, multi-token, a bare commit id, a
        symlink, or a name that is not ``rev-<digits>`` -- is a refusal. There
        is no "best effort" reading of a pointer, because a misread pointer
        would send every runtime helper into a different workspace than the
        one the durable state describes.
        """
        pointer = self.project_root(project_id) / POINTER_FILENAME
        if pointer.is_symlink():
            raise PointerResolutionError(
                WORKSPACE_POINTER_INVALID, "Workspace pointer is a symlink")
        if not pointer.exists():
            return None
        try:
            raw = pointer.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise PointerResolutionError(
                WORKSPACE_POINTER_INVALID, "Workspace pointer is unreadable") from None
        lines = raw.split("\n")
        if raw.endswith("\n"):
            lines.pop()
        if len(lines) != 1:
            raise PointerResolutionError(
                WORKSPACE_POINTER_INVALID,
                "Workspace pointer must name exactly one operation")
        token = lines[0]
        if not token:
            raise PointerResolutionError(
                WORKSPACE_POINTER_INVALID, "Workspace pointer is empty")
        if _SHA1_RE.fullmatch(token):
            raise PointerResolutionError(
                WORKSPACE_POINTER_INVALID,
                "Workspace pointer must not hold a commit id")
        return validate_operation_token(token)

    def resolve_workspace(self, project_id: str) -> Path:
        """The ONE directory this project's runtime may use. Never guesses.

        The four rules, in order:

        1. ``current`` absent AND ``pointer_mode`` is not ``True`` -> the
           legacy project directory (the R1 / never-hydrated case).
        2. ``current`` absent AND ``pointer_mode`` is ``True`` -> fail closed.
           A swap has committed at least once, so the legacy mutable workspace
           must never be resurrected, whatever ``hydration`` currently says.
        3. ``current`` names a valid token -> that operation directory, with
           containment and symlink escape checked.
        4. ``current`` exists but is unusable -> fail closed, never legacy.
        """
        root = self.project_root(project_id)
        token = self.pointer_token(project_id)
        if token is None:
            if self.pointer_mode(project_id):
                raise PointerResolutionError(
                    WORKSPACE_POINTER_MISSING_AFTER_HYDRATION,
                    "Workspace pointer is missing after hydration")
            return root
        ops = root / OPS_DIRNAME
        if ops.is_symlink():
            raise PointerResolutionError(
                WORKSPACE_POINTER_INVALID, "Operations directory is a symlink")
        operation = ops / token
        if operation.is_symlink():
            raise PointerResolutionError(
                WORKSPACE_POINTER_INVALID, "Operation directory is a symlink")
        if not operation.is_dir():
            raise PointerResolutionError(
                WORKSPACE_POINTER_INVALID, "Operation directory does not exist")
        try:
            resolved = operation.resolve()
            base = root.resolve()
        except OSError:
            raise PointerResolutionError(
                WORKSPACE_POINTER_INVALID,
                "Operation directory could not be resolved") from None
        if resolved != base and base not in resolved.parents:
            raise PointerResolutionError(
                WORKSPACE_POINTER_INVALID,
                "Operation directory escapes the project workspace")
        return operation

    def op_dir_for(self, project_id: str, seq: int) -> Path:
        """Where one revision's staging workspace is materialized.

        The name is derived from the reserved ``seq``, so two operations can
        never share a directory: a leftover directory under this name belongs
        to this exact reservation and is disposable.
        """
        token = validate_operation_token(f"rev-{seq}")
        ops = self.ops_dir(project_id)
        if ops.is_symlink():
            raise PointerResolutionError(
                WORKSPACE_POINTER_INVALID, "Operations directory is a symlink")
        ops.mkdir(parents=True, exist_ok=True)
        return ops / token

    def write_pointer(self, project_id: str, token: str) -> None:
        """Atomically make ``token`` the project's current workspace.

        Refuses a symlinked ``current`` and a symlinked ``.ops``: writing
        through either would let an attacker redirect the project's entire
        workspace, and a pointer that cannot be written safely is a refusal
        rather than a best-effort update.
        """
        token = validate_operation_token(token)
        root = self.project_root(project_id)
        ops = root / OPS_DIRNAME
        if ops.is_symlink():
            raise PointerResolutionError(
                WORKSPACE_POINTER_INVALID, "Operations directory is a symlink")
        pointer = root / POINTER_FILENAME
        if pointer.is_symlink():
            raise PointerResolutionError(
                WORKSPACE_POINTER_INVALID, "Workspace pointer is a symlink")
        ops.mkdir(parents=True, exist_ok=True)
        staging = root / f".{POINTER_FILENAME}.{token}.tmp"
        try:
            staging.write_text(token + "\n", encoding="ascii")
            os.replace(staging, pointer)
        finally:
            if staging.exists():
                staging.unlink()

    def sweep_ops(self, project_id: str, *, keep: tuple = ()) -> list:
        """Delete operation directories that no longer serve the project.

        The pointer target is preserved UNCONDITIONALLY -- independently of
        the hydration record, of which operation is in flight, and of what any
        caller passes in ``keep``. That is what lets a new operation run its
        whole RESERVED/FETCHED/VERIFIED sequence while the previous current
        workspace stays alive and usable, and it stays usable right up to the
        instant the new swap commits.
        """
        preserved = {str(name) for name in keep if isinstance(name, str)}
        try:
            token = self.pointer_token(project_id)
        except PointerResolutionError:
            # An unusable pointer is not a reason to delete directories: the
            # one it was pointing at may well be the only copy of the
            # workspace. Sweeping nothing is the fail-closed direction.
            return []
        if token:
            preserved.add(token)
        ops = self.ops_dir(project_id)
        if ops.is_symlink() or not ops.is_dir():
            return []
        removed = []
        for child in sorted(ops.iterdir()):
            if child.name in preserved:
                continue
            if child.is_symlink():
                # Unlink the link itself; never follow it out of .ops.
                child.unlink()
                removed.append(child.name)
            elif child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
                removed.append(child.name)
        return removed

    def acquire_project(self, project_id: str) -> bool:
        """Acquire the single active project slot. MAX_WORKERS=1."""
        with self._active_lock:
            if self._active_project is not None:
                return False
            self._active_project = project_id
            return True

    def release_project(self, project_id: str) -> None:
        """Release the single active project slot."""
        with self._active_lock:
            if self._active_project == project_id:
                self._active_project = None

    def run_command(
        self,
        project_id: str,
        command: List[str],
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: float = 300.0,
    ) -> subprocess.CompletedProcess:
        """Run a command inside the project's isolated workspace.

        The command runs with the project's RESOLVED workspace as cwd -- which
        in pointer mode is the current operation directory, not the project
        root -- and with project-local environment isolation. Both the cwd
        containment check and ``WORKSPACE_ROOT``/``HERMES_HOME`` therefore bind
        to the same directory every other helper uses.
        """
        workspace = self.resolve_workspace(project_id)
        if cwd is None:
            cwd = workspace

        # Ensure cwd is within workspace
        cwd = Path(cwd).resolve()
        if not cwd.is_relative_to(workspace):
            raise WorkspaceError(
                f"Command cwd {cwd} escapes project workspace {workspace}"
            )

        # Build isolated environment for generated-project processes
        # These must NOT receive platform credentials
        run_env = self._build_project_env(project_id, workspace, env)

        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=run_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.process_tracker.register(process.pid, command)

        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return subprocess.CompletedProcess(
                args=command,
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        finally:
            self.process_tracker.unregister(process.pid)

    def _build_project_env(
        self,
        project_id: str,
        workspace: Path,
        extra: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        """Build environment for generated-project processes.

        Generated website processes (npm, build, etc.) must NOT inherit:
        - model API keys
        - 9Router API keys (NINEROUTER_*)
        - Telegram tokens
        - Vercel credentials
        - GitHub / Hostinger / Strix deployment credentials
        - domain provider credentials
        - unrelated platform secrets

        They DO receive:
        - HERMES_HOME (project-local)
        - PROJECT_ID
        - WORKSPACE_ROOT
        - PATH, HOME, and other basic system vars

        R2-B1: the allowlist itself now lives in ``app.core.credentials`` so
        every role boundary derives from ONE audited list instead of
        drifting per-caller copies. The content is unchanged — this method
        previously carried the same allowlist inline, and still delegates to
        that single definition.
        """
        return credentials.build_env(project_id, workspace, extra)

    def build_hermes_env(
        self,
        project_id: str,
        extra: Optional[Dict[str, str]] = None,
        provider: Optional[str] = None,
    ) -> Dict[str, str]:
        """Build environment for a Hermes generation-agent invocation.

        R2-B1: this is a GENERATION role boundary, so it is role-scoped like
        FAST/FRONTEND/VISION. It receives the model credential it needs and
        benign system variables — but never a privileged credential, even
        though a generation role legitimately uses model credentials. The
        previous ``os.environ.copy()`` handed the child every deploy token in
        the parent process; that is the leak this closes.

        *provider* optionally narrows the model credential to that provider's
        own names. A caller that has already resolved the FRONTEND role's
        provider should pass it; one that has not gets the full provider set,
        unchanged from R2-B1.
        """
        env = credentials.agent_env("FRONTEND", provider=provider)
        env["HERMES_HOME"] = str(self.hermes_home)
        env["PROJECT_ID"] = project_id
        if extra:
            env.update(extra)
        return env

    def start_background(
        self,
        project_id: str,
        command: List[str],
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        port: Optional[int] = None,
    ) -> subprocess.Popen:
        """Start a long-running background process inside the project workspace.

        Minimal Phase 8 integration seam: used for the local render server
        (e.g. ``npm run preview``). The process is tracked so it can be
        deterministically stopped via ``stop_background`` or
        ``cleanup``/``cleanup_all``. Does NOT block waiting for the process
        to exit — callers own readiness polling.
        """
        workspace = self.resolve_workspace(project_id)
        if cwd is None:
            cwd = workspace
        cwd = Path(cwd).resolve()
        if not cwd.is_relative_to(workspace):
            raise WorkspaceError(
                f"Command cwd {cwd} escapes project workspace {workspace}"
            )

        run_env = self._build_project_env(project_id, workspace, env)

        popen_kwargs: Dict[str, object] = {}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=run_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **popen_kwargs,
        )
        self.process_tracker.register(process.pid, command, port=port)
        return process

    def stop_background(self, process: subprocess.Popen, timeout: float = 5.0) -> None:
        """Stop a background process started via ``start_background``."""
        self.process_tracker.cleanup(process.pid)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=timeout)
            except Exception:
                pass
        except Exception:
            pass

    def cleanup(self, project_id: str) -> None:
        """Clean up project resources."""
        self.process_tracker.cleanup_all()
        self.release_project(project_id)

    def is_source_repo_clean(self, repo_path: Path) -> bool:
        """Check if the source repository has uncommitted changes.

        Uses `git status --porcelain` as the smallest reliable existing
        Git mechanism. Returns True if clean (no modifications).
        """
        repo_path = Path(repo_path).resolve()
        if not repo_path.exists():
            return False

        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=10,
                # R2-B1: this is a Git operation, so it gets the Git
                # adapter's scoped environment (benign system variables plus
                # Git/SSH configuration) — not the full parent environment.
                env=credentials.git_env(),
            )
            return result.returncode == 0 and not result.stdout.strip()
        except Exception:
            # If git is unavailable or fails, we cannot prove cleanliness
            return False
