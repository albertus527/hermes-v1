"""Local render lifecycle for Website Builder R1 Phase 8 QA.

Launches the built project's static preview server (the existing Vite
toolchain's ``npm run preview``, already declared in the fixed starter's
``package.json``) on localhost only, using ``ProjectRunner`` for process
tracking and workspace containment. Deterministic startup detection via a
bounded HTTP poll. No new server framework, no public exposure.
"""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.sandbox.runner import ProjectRunner


@dataclass
class RenderHandle:
    """Handle to a running local render server."""

    project_id: str
    port: int
    process: object  # subprocess.Popen — typed loosely to avoid import cycle
    url: str


class RenderError(RuntimeError):
    """Raised when the local render server fails to start."""


def _port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        try:
            s.connect((host, port))
            return False  # something is listening
        except OSError:
            return True


def _wait_for_http_ready(url: str, timeout: float = 30.0, interval: float = 0.25) -> bool:
    """Bounded poll for the render server to answer HTTP requests."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:  # nosec B310 - localhost only
                if resp.status < 500:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(interval)
    return False


class LocalRenderer:
    """Starts/stops one local preview render per project. localhost only."""

    def __init__(self, runner: ProjectRunner):
        self.runner = runner

    def start(
        self,
        project_id: str,
        workspace: Path,
        startup_timeout: float = 30.0,
    ) -> RenderHandle:
        """Start the project's preview server and wait for it to be ready.

        Raises RenderError if the server never becomes ready. Always cleans
        up the spawned process on failure — no orphan process.
        """
        port = self.runner.port_allocator.allocate()
        url = f"http://127.0.0.1:{port}/"

        # The generated project's vite.config.ts sets `preview.host: true`
        # (public bind) so a real deployment/dev-container QA pass can reach
        # it from outside its own network namespace. Phase 8's local QA
        # render only needs localhost access, so explicitly override the
        # host here rather than editing the generated project's vite config
        # (which stays intentionally public-bindable for other flows).
        process = self.runner.start_background(
            project_id,
            [
                "npm", "run", "preview", "--",
                "--port", str(port), "--strictPort",
                "--host", "127.0.0.1",
            ],
            cwd=workspace,
            port=port,
        )

        ready = _wait_for_http_ready(url, timeout=startup_timeout)
        if not ready:
            self.runner.stop_background(process)
            self.runner.port_allocator.release(port)
            raise RenderError(
                f"Local render server for project {project_id!r} did not "
                f"become ready on {url} within {startup_timeout}s"
            )

        return RenderHandle(project_id=project_id, port=port, process=process, url=url)

    def stop(self, handle: RenderHandle) -> None:
        """Stop the render server and release its port. No orphan process."""
        try:
            self.runner.stop_background(handle.process)
        finally:
            self.runner.port_allocator.release(handle.port)
