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

from app.core.state import ProjectStateStore


# MAX_WORKERS=1 — one active project mutation at a time
MAX_WORKERS = 1

# Port allocation: small deterministic range
_PORT_BASE = 5100
_PORT_RANGE = 100


class WorkspaceError(ValueError):
    """Raised when workspace validation fails."""


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


class PortAllocator:
    """Small deterministic port allocator. Not a service."""

    def __init__(self, base: int = _PORT_BASE, range_size: int = _PORT_RANGE):
        self.base = base
        self.range_size = range_size
        self._allocated: Set[int] = set()

    def allocate(self) -> int:
        """Allocate the next available port in the deterministic range."""
        for offset in range(self.range_size):
            port = self.base + offset
            if port not in self._allocated:
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
        """Create an isolated workspace for a project."""
        path = project_workspace_path(self.workspace_root, project_id)
        path.mkdir(parents=True, exist_ok=True)

        # Create subdirectories
        (path / ".hermes").mkdir(exist_ok=True)
        (path / ".browser").mkdir(exist_ok=True)
        (path / ".runtime").mkdir(exist_ok=True)

        return path

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

        The command runs with the project workspace as cwd and with
        project-local environment isolation.
        """
        workspace = project_workspace_path(self.workspace_root, project_id)
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
        - domain provider credentials
        - unrelated platform secrets

        They DO receive:
        - HERMES_HOME (project-local)
        - PROJECT_ID
        - WORKSPACE_ROOT
        - PATH, HOME, and other basic system vars
        """
        env = os.environ.copy()

        # Strip platform credentials
        credential_prefixes = (
            "TELEGRAM_",
            "WHATSAPP_",
            "VERCEL_",
            "GITHUB_",
            "OPENROUTER_",
            "OPENAI_",
            "ANTHROPIC_",
            "NINEROUTER_",
            "9ROUTER_",
            "DOMAIN_",
            "NAMECHEAP_",
            "GODADDY_",
            "CLOUDFLARE_",
        )
        for key in list(env.keys()):
            if key.startswith(credential_prefixes):
                del env[key]

        # Set project-specific vars
        env["HERMES_HOME"] = str(self.hermes_home)
        env["PROJECT_ID"] = project_id
        env["WORKSPACE_ROOT"] = str(workspace)

        if extra:
            env.update(extra)

        return env

    def build_hermes_env(
        self,
        project_id: str,
        extra: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        """Build environment for Hermes platform processes.

        Hermes invocation MAY receive platform credentials because it needs
        them for model/provider resolution. This is separate from the
        generated-project environment.
        """
        env = os.environ.copy()
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
        workspace = project_workspace_path(self.workspace_root, project_id)
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
            )
            return result.returncode == 0 and not result.stdout.strip()
        except Exception:
            # If git is unavailable or fails, we cannot prove cleanliness
            return False
