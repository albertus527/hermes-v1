"""Desktop/mobile screenshot capture for Website Builder R1 Phase 8 QA.

Uses the existing repo-wide browser automation CLI (``agent-browser`` — the
same headless-Chromium tool Hermes' own ``browser_tool.py`` shells out to)
directly via subprocess, with an injectable ``capture_fn`` seam for tests.
No new browser automation framework, no Playwright/Selenium dependency
added to this project.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# Fixed viewport sizes per canonical Phase 8 spec.
DESKTOP_VIEWPORT = (1440, 900)
MOBILE_VIEWPORT = (390, 844)

# Type for an injectable capture function: (url, width, height, out_path) -> bool
CaptureFn = Callable[[str, int, int, Path], bool]


class ScreenshotError(RuntimeError):
    """Raised when a screenshot capture fails."""


def _default_capture_fn(url: str, width: int, height: int, out_path: Path) -> bool:
    """Default capture using the ``agent-browser`` CLI, if installed.

    Returns False (never raises) on any failure — the caller decides how to
    treat a missing/failed capture as a deterministic QA finding.

    Optional env-controlled browser-args seam: when ``AGENT_BROWSER_ARGS``
    is set (e.g. ``--no-sandbox`` on sandbox-less VPS hosts), its value is
    passed through to ``agent-browser``'s existing ``--args`` launch option.
    Unset or empty preserves the original argv exactly.
    """
    browser_cmd = shutil.which("agent-browser")
    if not browser_cmd:
        return False

    out_path.parent.mkdir(parents=True, exist_ok=True)
    session_name = f"qa-{width}x{height}-{abs(hash((url, width, height))) % 100000}"

    open_argv = [
        browser_cmd,
        "--session", session_name,
        "--json",
        "open", url,
        "--width", str(width),
        "--height", str(height),
    ]
    browser_args = os.environ.get("AGENT_BROWSER_ARGS", "").strip()
    if browser_args:
        open_argv.extend(["--args", " ".join(shlex.split(browser_args))])

    try:
        open_result = subprocess.run(
            open_argv,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        # A failed `open` must never fall through to a screenshot: without
        # this check a nonzero-exit open (bad URL, browser launch failure,
        # missing sandbox flags, ...) could still produce a "successful"
        # screenshot of a blank/error page, silently corrupting QA evidence.
        if open_result.returncode != 0:
            return False

        result = subprocess.run(
            [
                browser_cmd,
                "--session", session_name,
                "--json",
                "screenshot", "--full", str(out_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return result.returncode == 0 and out_path.exists()
    finally:
        subprocess.run(
            [browser_cmd, "--session", session_name, "--json", "close"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )


@dataclass
class ScreenshotSet:
    """Desktop + mobile screenshot pair for one QA attempt."""

    desktop: Optional[Path]
    mobile: Optional[Path]

    @property
    def complete(self) -> bool:
        return (
            self.desktop is not None
            and self.desktop.exists()
            and self.mobile is not None
            and self.mobile.exists()
        )


class ScreenshotCapture:
    """Captures desktop + mobile screenshots into a project-local QA dir."""

    def __init__(self, capture_fn: Optional[CaptureFn] = None):
        self.capture_fn = capture_fn or _default_capture_fn

    def capture(self, url: str, qa_dir: Path, attempt: int) -> ScreenshotSet:
        """Capture desktop + mobile screenshots for one QA attempt.

        Stores under ``qa_dir/attempt-<N>/{desktop,mobile}.png``. Never
        raises — a failed capture yields ``None`` for that screenshot so
        the caller records it as a deterministic finding.
        """
        attempt_dir = qa_dir / f"attempt-{attempt}"
        attempt_dir.mkdir(parents=True, exist_ok=True)

        desktop_path = attempt_dir / "desktop.png"
        mobile_path = attempt_dir / "mobile.png"

        desktop_ok = self.capture_fn(
            url, DESKTOP_VIEWPORT[0], DESKTOP_VIEWPORT[1], desktop_path
        )
        mobile_ok = self.capture_fn(
            url, MOBILE_VIEWPORT[0], MOBILE_VIEWPORT[1], mobile_path
        )

        return ScreenshotSet(
            desktop=desktop_path if desktop_ok and desktop_path.exists() else None,
            mobile=mobile_path if mobile_ok and mobile_path.exists() else None,
        )
