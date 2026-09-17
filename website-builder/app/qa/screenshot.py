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
    ]
    browser_args = os.environ.get("AGENT_BROWSER_ARGS", "").strip()
    if browser_args:
        open_argv.extend(["--args", " ".join(shlex.split(browser_args))])

    try:
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

            # Viewport is a runtime command, not an `open` flag. Apply it to
            # the same session before capturing either desktop or mobile evidence.
            viewport_result = subprocess.run(
                [
                    browser_cmd,
                    "--session", session_name,
                    "--json",
                    "set", "viewport", str(width), str(height),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if viewport_result.returncode != 0:
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
        except subprocess.TimeoutExpired:
            # Contract: this function never raises — a hung `open`/`set
            # viewport`/`screenshot` call is a controlled capture failure,
            # not an exception that should crash the QA run and bypass the
            # bounded FRONTEND repair loop.
            return False
    finally:
        # Cleanup must never itself raise past this function (a hung/failed
        # `close` would otherwise mask the real capture result above).
        try:
            subprocess.run(
                [browser_cmd, "--session", session_name, "--json", "close"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except subprocess.TimeoutExpired:
            pass


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


def _png_dimensions(path: Path) -> tuple:
    """Read (width, height) from a PNG's IHDR chunk — stdlib only.

    Raises ScreenshotError if the file is not a decodable PNG header.
    """
    import struct

    try:
        with open(path, "rb") as f:
            header = f.read(24)
    except OSError as exc:
        raise ScreenshotError(f"cannot read screenshot evidence {path}: {exc}") from exc
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ScreenshotError(f"screenshot evidence is not a valid PNG: {path}")
    return struct.unpack(">II", header[16:24])


def validate_screenshot_dimensions(screenshots: ScreenshotSet) -> None:
    """Evidence-integrity guard: actual PNG pixels must match the viewports.

    Verifies the ACTUAL pixel dimensions of captured screenshots against the
    intended viewports (desktop 1440, mobile 390) by parsing each PNG's IHDR
    chunk — not filenames or metadata. The tg-6329821361 incident produced
    valid-looking PNGs at the browser's default 1280px width for BOTH
    viewports because the viewport command was never applied; this guard
    makes that class of broken evidence impossible to hand to VISION.

    Raises ScreenshotError on any mismatch. Missing files are skipped here —
    they are reported by the deterministic screenshot-presence checks.
    """
    for path, expected_width, label in (
        (screenshots.desktop, DESKTOP_VIEWPORT[0], "desktop"),
        (screenshots.mobile, MOBILE_VIEWPORT[0], "mobile"),
    ):
        if path is None or not path.exists():
            continue
        width, _height = _png_dimensions(path)
        if width != expected_width:
            raise ScreenshotError(
                f"screenshot evidence integrity failure: {label} capture is "
                f"{width}px wide, expected {expected_width}px ({path}) — "
                f"viewport was not applied; evidence rejected before VISION"
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
