"""Deterministic functional QA for Website Builder R1 Phase 8.

Application code owns deterministic QA. No subjective judgment here —
that belongs to VISION. Keeps checks small and stdlib-only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from app.core.design_dna import load_persisted_design_dna
from app.qa.findings import DeterministicFindings
from app.qa.screenshot import DESKTOP_VIEWPORT, MOBILE_VIEWPORT, ScreenshotSet


def check_design_dna(workspace: Path) -> bool:
    """design-dna.json is readable as a Design DNA document.

    Same reader every other consumer uses, so QA cannot pass a document the
    build rejected — or fail one the build accepted — by disagreeing about what
    the file contains.
    """
    return load_persisted_design_dna(workspace / "design-dna.json") is not None


def check_source_present(workspace: Path) -> bool:
    """Generated source (src/App.tsx) is present."""
    return (workspace / "src" / "App.tsx").is_file()


def run_deterministic_checks(
    workspace: Path,
    screenshots: ScreenshotSet,
    render_ok: bool,
    build_ok: bool,
    typecheck_ok: bool,
) -> DeterministicFindings:
    """Run all deterministic Phase 8 checks and collect blocking failures.

    Build/typecheck are passed in (already run by the caller through
    ProjectRunner) rather than re-run here — each cheap check must still
    run at most once per QA attempt.
    """
    findings = DeterministicFindings(
        render_ok=render_ok,
        desktop_screenshot_ok=screenshots.desktop is not None,
        mobile_screenshot_ok=screenshots.mobile is not None,
        design_dna_valid=check_design_dna(workspace),
        source_present=check_source_present(workspace),
        build_ok=build_ok,
        typecheck_ok=typecheck_ok,
    )

    if not findings.render_ok:
        findings.failures.append("Local render server failed to start")
    if not findings.desktop_screenshot_ok:
        findings.failures.append("Desktop screenshot missing")
    if not findings.mobile_screenshot_ok:
        findings.failures.append("Mobile screenshot missing")
    if not findings.design_dna_valid:
        findings.failures.append("design-dna.json missing or invalid")
    if not findings.source_present:
        findings.failures.append("Generated source (src/App.tsx) missing")
    if not findings.build_ok:
        findings.failures.append("npm run build failed")
    if not findings.typecheck_ok:
        findings.failures.append("npm run typecheck failed")

    # Browser metrics are captured before the full-page PNG, so overflow is
    # deterministic page-layout evidence rather than subjective VISION output.
    for label, metrics, viewport_width in (
        ("desktop", screenshots.desktop_metrics, DESKTOP_VIEWPORT[0]),
        ("mobile", screenshots.mobile_metrics, MOBILE_VIEWPORT[0]),
    ):
        if metrics is None:
            continue
        largest_width = max(metrics.document_scroll_width, metrics.body_scroll_width)
        if largest_width > metrics.document_client_width:
            findings.failures.append(
                f"{label} horizontal overflow: {largest_width}px document width "
                f"exceeds {viewport_width}px viewport"
            )

    return findings
