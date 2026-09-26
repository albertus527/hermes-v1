"""Desktop/mobile screenshot capture for Website Builder R1 Phase 8 QA.

Uses the existing repo-wide browser automation CLI (``agent-browser`` — the
same headless-Chromium tool Hermes' own ``browser_tool.py`` shells out to)
directly via subprocess, with an injectable ``capture_fn`` seam for tests.
No new browser automation framework, no Playwright/Selenium dependency
added to this project.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

# Fixed viewport sizes per canonical Phase 8 spec.
DESKTOP_VIEWPORT = (1440, 900)
MOBILE_VIEWPORT = (390, 844)
# Maximum horizontal space a browser may reserve for a vertical scrollbar.
# Used only for ``documentElement.clientWidth``, which is innerWidth minus this
# reservation; innerWidth/innerHeight remain exact.
SCROLLBAR_ALLOWANCE_PX = 32


class ScreenshotError(RuntimeError):
    """Raised when a screenshot capture fails."""


def _validated_metric_value(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScreenshotError(f"browser metric {name} is missing or non-numeric")
    if not math.isfinite(value) or value < 0 or int(value) != value:
        raise ScreenshotError(f"browser metric {name} is not a finite non-negative integer")
    return int(value)


@dataclass(frozen=True)
class BrowserMetrics:
    """Trusted live-browser measurements captured immediately before evidence."""

    inner_width: int
    inner_height: int
    document_client_width: int
    document_scroll_width: int
    body_scroll_width: int

    @classmethod
    def from_payload(cls, payload: object) -> "BrowserMetrics":
        """Parse and strictly validate the five metrics from ``eval``."""
        if not isinstance(payload, dict):
            raise ScreenshotError("browser metric probe returned a non-object result")
        return cls(
            inner_width=_validated_metric_value(payload.get("innerWidth"), "innerWidth"),
            inner_height=_validated_metric_value(payload.get("innerHeight"), "innerHeight"),
            document_client_width=_validated_metric_value(
                payload.get("documentClientWidth"), "documentClientWidth"
            ),
            document_scroll_width=_validated_metric_value(
                payload.get("documentScrollWidth"), "documentScrollWidth"
            ),
            body_scroll_width=_validated_metric_value(
                payload.get("bodyScrollWidth"), "bodyScrollWidth"
            ),
        )

    def validate(self) -> "BrowserMetrics":
        """Revalidate a value object before trusting it for QA decisions."""
        return BrowserMetrics.from_payload({
            "innerWidth": self.inner_width,
            "innerHeight": self.inner_height,
            "documentClientWidth": self.document_client_width,
            "documentScrollWidth": self.document_scroll_width,
            "bodyScrollWidth": self.body_scroll_width,
        })


@dataclass(frozen=True)
class CaptureResult:
    """Structured result of one browser capture attempt.

    ``status`` is one of ``captured``, ``capture_failed``, or
    ``metric_probe_failed``. Metrics are present only for ``captured``.
    """

    path: Optional[Path]
    metrics: Optional[BrowserMetrics]
    status: str
    error: str = ""

    @property
    def captured(self) -> bool:
        return self.status == "captured"

    @classmethod
    def failure(cls, status: str, error: str) -> "CaptureResult":
        if status not in {"capture_failed", "metric_probe_failed"}:
            raise ValueError(f"invalid capture failure status: {status}")
        return cls(path=None, metrics=None, status=status, error=error)


# Type for an injectable capture function. A failed capture is represented
# structurally and is never collapsed to a boolean because metric-probe
# failures are capture-integrity errors, not repairable page defects.
CaptureFn = Callable[[str, int, int, Path], CaptureResult]

_METRICS_EXPRESSION = (
    "JSON.stringify({"
    "innerWidth: window.innerWidth,"
    "innerHeight: window.innerHeight,"
    "documentClientWidth: document.documentElement.clientWidth,"
    "documentScrollWidth: document.documentElement.scrollWidth,"
    "bodyScrollWidth: document.body.scrollWidth"
    "})"
)


def _run_browser_command(argv: List[str], timeout: int) -> subprocess.CompletedProcess:
    """Run one ``agent-browser`` command, capturing its JSON stdout.

    ``agent-browser`` is an npm ``.CMD`` shim on Windows, and a cold Chromium
    launch inherits the parent's stdout handle. Under ``capture_output=True``
    the launched grandchild keeps the pipe's write end open, so the CLI has
    already printed its JSON and exited but ``communicate()`` never observes
    EOF — the call then dies on ``timeout`` and every capture is reported as
    a failure. Buffering through a temporary file gives the child an ordinary
    file handle to inherit, so the wait ends as soon as the CLI exits.
    """
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as sink:
        try:
            result = subprocess.run(
                argv,
                stdout=sink,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            sink.seek(0)
            partial = sink.read()
            raise subprocess.TimeoutExpired(argv, timeout, output=partial) from exc
        sink.seek(0)
        return subprocess.CompletedProcess(argv, result.returncode, sink.read(), "")


def _probe_browser_metrics(result: subprocess.CompletedProcess) -> BrowserMetrics:
    """Parse agent-browser's outer JSON envelope and its nested eval result."""
    if result.returncode != 0:
        raise ScreenshotError(f"browser metric probe exited with status {result.returncode}")
    try:
        envelope = json.loads(result.stdout or "")
    except (json.JSONDecodeError, TypeError) as exc:
        raise ScreenshotError(f"browser metric probe returned invalid JSON: {exc}") from exc
    if not isinstance(envelope, dict) or envelope.get("success") is not True:
        raise ScreenshotError("browser metric probe did not report success")
    data = envelope.get("data")
    if not isinstance(data, dict) or "result" not in data:
        raise ScreenshotError("browser metric probe response is missing data.result")
    payload = data["result"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ScreenshotError(f"browser metric data.result is invalid JSON: {exc}") from exc
    return BrowserMetrics.from_payload(payload)


def _default_capture_fn(
    url: str, width: int, height: int, out_path: Path
) -> CaptureResult:
    """Capture a full-page PNG and trusted metrics with one browser session.

    The required order is ``open`` -> ``set viewport`` -> ``eval`` ->
    unchanged full-page ``screenshot`` -> ``close``. Optional
    ``AGENT_BROWSER_ARGS`` launch arguments remain attached to ``open``.
    """
    browser_cmd = shutil.which("agent-browser")
    if not browser_cmd:
        return CaptureResult.failure("capture_failed", "agent-browser CLI is not installed")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    session_name = f"qa-{width}x{height}-{abs(hash((url, width, height))) % 100000}"
    open_argv = [browser_cmd, "--session", session_name, "--json", "open", url]
    browser_args = os.environ.get("AGENT_BROWSER_ARGS", "").strip()
    if browser_args:
        open_argv.extend(["--args", " ".join(shlex.split(browser_args))])

    try:
        try:
            open_result = _run_browser_command(open_argv, 30)
            if open_result.returncode != 0:
                return CaptureResult.failure(
                    "capture_failed", f"agent-browser open exited with status {open_result.returncode}"
                )

            viewport_result = _run_browser_command(
                [
                    browser_cmd, "--session", session_name, "--json",
                    "set", "viewport", str(width), str(height),
                ],
                30,
            )
            if viewport_result.returncode != 0:
                return CaptureResult.failure(
                    "capture_failed",
                    f"agent-browser set viewport exited with status {viewport_result.returncode}",
                )

            try:
                metrics = _probe_browser_metrics(
                    _run_browser_command(
                        [
                            browser_cmd, "--session", session_name, "--json",
                            "eval", _METRICS_EXPRESSION,
                        ],
                        30,
                    )
                )
            except ScreenshotError as exc:
                return CaptureResult.failure("metric_probe_failed", str(exc))

            result = _run_browser_command(
                [
                    browser_cmd, "--session", session_name, "--json",
                    "screenshot", "--full", str(out_path),
                ],
                30,
            )
            if result.returncode != 0 or not out_path.exists():
                return CaptureResult.failure(
                    "capture_failed",
                    f"agent-browser screenshot exited with status {result.returncode} or produced no file",
                )
            return CaptureResult(path=out_path, metrics=metrics, status="captured")
        except subprocess.TimeoutExpired as exc:
            return CaptureResult.failure("capture_failed", f"agent-browser command timed out: {exc}")
    finally:
        # Cleanup never raises past this function and cannot mask the result.
        try:
            _run_browser_command(
                [browser_cmd, "--session", session_name, "--json", "close"], 10
            )
        except subprocess.TimeoutExpired:
            pass


@dataclass
class ScreenshotSet:
    """Desktop + mobile screenshot pair and live metrics for one QA attempt."""

    desktop: Optional[Path]
    mobile: Optional[Path]
    desktop_metrics: Optional[BrowserMetrics] = None
    mobile_metrics: Optional[BrowserMetrics] = None

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
    """Validate live viewport metrics and preserve full-page evidence.

    The live inner/client widths prove the requested viewport was applied;
    PNG IHDR parsing remains evidence-presence and signature validation. A
    full-page PNG may be wider than the viewport when the document overflows,
    so it must be at least (not exactly) the requested width.
    """
    for path, metrics, viewport, label in (
        (screenshots.desktop, screenshots.desktop_metrics, DESKTOP_VIEWPORT, "desktop"),
        (screenshots.mobile, screenshots.mobile_metrics, MOBILE_VIEWPORT, "mobile"),
    ):
        if path is None or not path.exists():
            continue
        if metrics is None:
            raise ScreenshotError(
                f"screenshot evidence integrity failure: {label} live browser metrics are missing"
            )
        try:
            metrics = metrics.validate()
        except (AttributeError, TypeError, ScreenshotError) as exc:
            detail = str(exc) if isinstance(exc, ScreenshotError) else f"{type(exc).__name__}: {exc}"
            raise ScreenshotError(
                f"screenshot evidence integrity failure: {label} live browser metrics are malformed: {detail}"
            ) from exc
        expected_width, expected_height = viewport
        actual_viewport = {
            "innerWidth": metrics.inner_width,
            "innerHeight": metrics.inner_height,
            "documentElement.clientWidth": metrics.document_client_width,
        }
        # innerWidth/innerHeight are what ``set viewport`` sets, and the
        # browser guarantees them exactly -> exact equality is correct.
        for metric_name, expected in (
            ("innerWidth", expected_width),
            ("innerHeight", expected_height),
        ):
            if actual_viewport[metric_name] != expected:
                raise ScreenshotError(
                    f"screenshot evidence integrity failure: {label} {metric_name} is "
                    f"{actual_viewport[metric_name]}, expected {expected}; evidence rejected before VISION"
                )
        # documentElement.clientWidth is the CONTENT width: it is innerWidth
        # minus any space the browser reserves for a vertical scrollbar. A
        # marketing page is virtually always taller than the viewport, so a
        # browser that reserves scrollbar space reports clientWidth < viewport
        # on a capture that is perfectly valid. Requiring exact equality here
        # rejected correct evidence as an infrastructure failure, which does not
        # consume a repair attempt and lands the project in FAILED with nothing
        # actionable. Allow a scrollbar's worth of slack; still reject a real
        # mismatch (a viewport that was not applied).
        client_width = actual_viewport["documentElement.clientWidth"]
        if not (expected_width - SCROLLBAR_ALLOWANCE_PX <= client_width <= expected_width):
            raise ScreenshotError(
                f"screenshot evidence integrity failure: {label} "
                f"documentElement.clientWidth is {client_width}, expected "
                f"{expected_width} (within {SCROLLBAR_ALLOWANCE_PX}px scrollbar "
                f"tolerance); evidence rejected before VISION"
            )

        png_width, _png_height = _png_dimensions(path)
        if png_width < expected_width:
            raise ScreenshotError(
                f"screenshot evidence integrity failure: {label} full-page PNG is "
                f"{png_width}px wide, narrower than the {expected_width}px requested viewport"
            )


class ScreenshotCapture:
    """Captures desktop + mobile screenshots into a project-local QA dir."""

    def __init__(self, capture_fn: Optional[CaptureFn] = None):
        self.capture_fn = capture_fn or _default_capture_fn

    def capture(self, url: str, qa_dir: Path, attempt: int) -> ScreenshotSet:
        """Capture desktop + mobile evidence and live browser metrics."""
        attempt_dir = qa_dir / f"attempt-{attempt}"
        attempt_dir.mkdir(parents=True, exist_ok=True)

        desktop = self.capture_fn(
            url, DESKTOP_VIEWPORT[0], DESKTOP_VIEWPORT[1], attempt_dir / "desktop.png"
        )
        mobile = self.capture_fn(
            url, MOBILE_VIEWPORT[0], MOBILE_VIEWPORT[1], attempt_dir / "mobile.png"
        )

        # A structured browser/CLI failure is capture infrastructure, not a
        # missing page artifact. Raise before VISION and before repair budget.
        for result in (desktop, mobile):
            if not result.captured:
                raise ScreenshotError(
                    f"{result.status}: {result.error or 'browser capture failed'}"
                )
            if result.metrics is None:
                raise ScreenshotError(
                    "metric_probe_failed: successful capture has no trusted browser metrics"
                )
            if result.path is None or not result.path.exists():
                raise ScreenshotError(
                    f"capture_failed: successful capture reported no evidence file: {result.path}"
                )

        return ScreenshotSet(
            desktop=desktop.path,
            mobile=mobile.path,
            desktop_metrics=desktop.metrics,
            mobile_metrics=mobile.metrics,
        )
