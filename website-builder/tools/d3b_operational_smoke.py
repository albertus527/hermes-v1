"""D3b POST-ACCEPTANCE OPERATIONAL SMOKE -- the real end-to-end path.

This is the operational hardening smoke test. Unlike ``d3b_live_scenario_b.py``
(which used a DISPOSABLE full-quality skill root and a DISPOSABLE profile), this
drives the REAL, CORRECTED operational configuration end to end:

    operational Website Builder configuration
      -> approved generation profile (real ~/.hermes-website, clean .env)
      -> real FRONTEND execution (configured provider)
      -> build + typecheck
      -> browser / VISION QA
      -> full-quality Impeccable scan (the ACTUAL provisioned skill)
      -> bounded repair (a real actionable finding)
      -> revalidation
      -> PREVIEW_READY or an explicit bounded failure

It is a PAID run (a real FRONTEND + VISION model call) and is therefore NOT part
of the offline suite. It is bounded to one repair (attempt cap 2).

Run (from ``website-builder``, with the venv active and Node 26.5.0 selected):

    python tools/d3b_operational_smoke.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT))

from app.core import app_env  # noqa: E402
from app.core import credentials  # noqa: E402
from app.core.critic_repair import ImpeccableScanner  # noqa: E402
from app.core.lifecycle import ProjectLifecycle  # noqa: E402
from app.core.state import ProjectStateStore  # noqa: E402
from app.hermes.adapter import HermesAdapter  # noqa: E402
from app.projects.build import (  # noqa: E402
    _capture_toolchain_hashes,
    _verify_toolchain_untouched,
)
from app.qa.orchestrator import QAOrchestrator  # noqa: E402
from app.sandbox.runner import ProjectRunner  # noqa: E402

WORK = Path("/tmp/d3b-operational-smoke")
PROJECT_ID = "smokeproj"
SKILL_ROOT = Path.home() / ".hermes-website" / "skills" / "impeccable"


def _manifest(workspace: Path) -> dict:
    """SHA-256 of every project file, excluding build/QA/runtime artifacts."""
    out = {}
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        parts = set(path.parts)
        if parts & {"node_modules", "dist", "qa", ".browser", ".hermes", ".runtime"}:
            continue
        out[str(path.relative_to(workspace))] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return out


def main() -> int:
    # ---- Operational configuration: load the APPLICATION's own env file. -----
    loaded = app_env.load_application_env()
    print("operational env loaded (names):", loaded)
    profile = app_env.resolve_profile_home()
    print("generation profile:", profile)

    # The generation profile must be clean; the guard that refused before now
    # passes, because the operational credential lives in the application env.
    credentials.assert_profile_dotenv_clean(profile)
    credentials.assert_profile_home_clean(profile)
    print("profile guards: PASS (clean generation profile)")

    if not SKILL_ROOT.is_dir():
        print("FATAL: provisioned skill missing:", SKILL_ROOT)
        return 2

    workspace = WORK / "workspaces" / PROJECT_ID
    state_root = WORK / "state"
    store = ProjectStateStore(state_root)

    with store.acquire_writer(PROJECT_ID) as state:
        state.brief = {"name": "Northcut", "what": "barbershop", "why": "online booking"}
        state.design_dna = json.loads((workspace / "design-dna.json").read_text())
        store.save(state)

    store.transition_lifecycle(PROJECT_ID, ProjectLifecycle.READY)
    store.transition_lifecycle(PROJECT_ID, ProjectLifecycle.QUEUED)
    store.transition_lifecycle(PROJECT_ID, ProjectLifecycle.RUNNING)

    runner = ProjectRunner(WORK / "workspaces", store, hermes_home=profile)
    adapter = HermesAdapter(store=store, hermes_home=profile)
    scanner = ImpeccableScanner(
        skill_root=SKILL_ROOT,
        node_executable=shutil.which("node"),
        timeout_seconds=120,
    )

    toolchain_hashes = _capture_toolchain_hashes(workspace)
    before_manifest = _manifest(workspace)

    print("\n=== pre-repair scan (REAL provisioned skill) ===")
    pre = scanner.scan(workspace)
    print("state:", pre.state, "| authoritative:", pre.authoritative,
          "| intended_project_scanned:", pre.intended_project_scanned,
          "| degraded:", pre.degraded, "| findings:", len(pre.findings))
    for f in pre.findings:
        print(f"  - [{f.severity}] {f.rule_id}: {f.finding[:70]}")

    orch = QAOrchestrator(
        runner, store, hermes_adapter=adapter, critic_scanner=scanner,
        toolchain_verify=lambda ws: _verify_toolchain_untouched(ws, toolchain_hashes),
    )

    print("\n=== running the production QA + critic repair path ===")
    result = orch.run(
        PROJECT_ID, workspace,
        {"name": "Northcut", "what": "barbershop", "why": "online booking"},
        json.loads((workspace / "design-dna.json").read_text()),
    )

    state = store.load(PROJECT_ID)
    critic = state.deployment.get("critic") or {}
    print("\n=== result ===")
    print("success:", result.success)
    print("repair_attempts:", result.repair_attempts)
    print("error:", result.error)
    print("lifecycle:", state.lifecycle)
    print("critic state:", critic.get("state"))
    print("critic outcome:", critic.get("outcome"))
    print("critic authoritative:", critic.get("authoritative"))
    print("critic attempts_used:", critic.get("attempts_used"))

    print("\n=== post-repair scan (REAL provisioned skill) ===")
    post = scanner.scan(workspace)
    print("state:", post.state, "| authoritative:", post.authoritative,
          "| intended_project_scanned:", post.intended_project_scanned,
          "| degraded:", post.degraded, "| findings:", len(post.findings))
    for f in post.findings:
        print(f"  - [{f.severity}] {f.rule_id}: {f.finding[:70]}")

    # ---- dependency integrity -------------------------------------------------
    after_manifest = _manifest(workspace)
    changed = sorted(
        set(before_manifest) | set(after_manifest)
    )
    changed = [p for p in changed if before_manifest.get(p) != after_manifest.get(p)]
    removed = sorted(set(before_manifest) - set(after_manifest))
    added = sorted(set(after_manifest) - set(before_manifest))
    toolchain_violation = _verify_toolchain_untouched(workspace, toolchain_hashes)

    print("\n=== dependency / toolchain integrity ===")
    print("changed project files:", changed)
    print("added files:", added)
    print("removed files:", removed)
    print("toolchain violation:", toolchain_violation)

    receipt = {
        "operational_env_names": list(loaded),
        "profile": str(profile),
        "skill_root": str(SKILL_ROOT),
        "pre_scan": pre.to_dict(),
        "post_scan": post.to_dict(),
        "result": {
            "success": result.success,
            "repair_attempts": result.repair_attempts,
            "error": result.error,
            "lifecycle": state.lifecycle,
            "critic": critic,
        },
        "changed_files": changed,
        "added_files": added,
        "removed_files": removed,
        "toolchain_violation": toolchain_violation,
    }
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print("\nreceipt:", WORK / "receipt.json")

    ok = (
        result.success
        and pre.authoritative and pre.state == "FINDINGS"
        and post.authoritative
        and critic.get("outcome") in ("REPAIRED", "ACCEPTED")
        and toolchain_violation is None
        and state.lifecycle == ProjectLifecycle.PREVIEW_READY.value
    )
    print("\nSMOKE VERDICT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
