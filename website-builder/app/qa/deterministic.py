"""Deterministic functional QA for Website Builder R1 Phase 8.

Application code owns deterministic QA. No subjective judgment here —
that belongs to VISION. Keeps checks small and stdlib-only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from app.qa.findings import DeterministicFindings
from app.qa.screenshot import ScreenshotSet


def check_design_dna(workspace: Path) -> bool:
    """design-dna.json exists and is valid JSON."""
    dna_path = workspace / "design-dna.json"
    if not dna_path.is_file():
        return False
    try:
        with dna_path.open("r", encoding="utf-8") as f:
            json.load(f)
        return True
    except (json.JSONDecodeError, IOError):
        return False


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

    return findings
