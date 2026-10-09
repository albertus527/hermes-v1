"""Scenario B (live VPS): a REAL FRONTEND critic repair through the production path.

This drives the REAL ``QAOrchestrator`` with a REAL ``HermesAdapter`` (the
configured FRONTEND provider) and a REAL ``ImpeccableScanner`` (full-quality
skill root), against a disposable project. It is a PAID call; the batch limits
it to ONE initial repair scenario with a hard attempt cap of 2.

Not part of the offline suite. Run explicitly:

    python tools/d3b_live_scenario_b.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT))

from app.core.critic_repair import ImpeccableScanner  # noqa: E402
from app.core.lifecycle import ProjectLifecycle  # noqa: E402
from app.core.state import ProjectStateStore  # noqa: E402
from app.hermes.adapter import HermesAdapter  # noqa: E402
from app.qa.orchestrator import QAOrchestrator  # noqa: E402
from app.sandbox.runner import ProjectRunner  # noqa: E402

WORK = Path("/tmp/d3b-vps-scenarioB")
HERMES_HOME = WORK / "hermes-home"


def _full_quality_skill() -> Path:
    """A disposable full-quality skill root (never mutates the profile)."""
    skill = WORK / "skills-full" / "impeccable"
    if skill.exists():
        shutil.rmtree(skill.parent)
    skill.parent.mkdir(parents=True)
    shutil.copytree(HERMES_HOME / "skills" / "impeccable", skill)
    shutil.copytree(Path("/tmp/impeccable-full") / "node_modules", skill / "node_modules")
    return skill


def main() -> int:
    ws = WORK / "workspaces" / "liveproj"
    state_root = WORK / "state"
    store = ProjectStateStore(state_root)

    pid = "liveproj"
    with store.acquire_writer(pid) as state:
        state.brief = {"name": "Northcut", "what": "barbershop",
                       "why": "online booking"}
        state.design_dna = json.loads((ws / "design-dna.json").read_text())
        store.save(state)

    # Advance the project to RUNNING (the post-Phase-7 state).
    store.transition_lifecycle(pid, ProjectLifecycle.READY)
    store.transition_lifecycle(pid, ProjectLifecycle.QUEUED)
    store.transition_lifecycle(pid, ProjectLifecycle.RUNNING)

    runner = ProjectRunner(WORK / "workspaces", store, hermes_home=HERMES_HOME)
    adapter = HermesAdapter(store=store, hermes_home=HERMES_HOME)
    scanner = ImpeccableScanner(
        skill_root=_full_quality_skill(),
        node_executable=shutil.which("node"),
        timeout_seconds=120,
    )

    print("=== Scenario B: pre-repair scan ===")
    pre = scanner.scan(ws)
    print("state:", pre.state, "authoritative:", pre.authoritative,
          "findings:", len(pre.findings))
    for f in pre.findings:
        print(f"  - [{f.severity}] {f.rule_id}: {f.finding[:70]}")

    orch = QAOrchestrator(
        runner, store, hermes_adapter=adapter, critic_scanner=scanner,
    )

    print("\n=== Scenario B: running the production QA + critic repair path ===")
    result = orch.run(pid, ws, {"name": "Northcut", "what": "barbershop",
                                "why": "online booking"},
                      json.loads((ws / "design-dna.json").read_text()))

    print("\n=== Scenario B: result ===")
    print("success:", result.success)
    print("repair_attempts:", result.repair_attempts)
    print("error:", result.error)
    state = store.load(pid)
    print("lifecycle:", state.lifecycle)
    critic = state.deployment.get("critic") or {}
    print("critic state:", critic.get("state"))
    print("critic outcome:", critic.get("outcome"))
    print("critic authoritative:", critic.get("authoritative"))
    print("critic attempts_used:", critic.get("attempts_used"))
    print("critic history:", json.dumps(critic.get("history"), indent=2)[:800])

    # Scenario F: capture the AFTER manifest for dependency-integrity comparison.
    def _manifest():
        import hashlib as _h
        out = {}
        for p in sorted(ws.rglob("*")):
            if p.is_file() and "node_modules" not in p.parts and "dist" not in p.parts:
                out[str(p.relative_to(ws))] = _h.sha256(p.read_bytes()).hexdigest()
        return out

    json.dump({"pre_scan": pre.to_dict(), "result": {
        "success": result.success, "repair_attempts": result.repair_attempts,
        "error": result.error, "lifecycle": state.lifecycle, "critic": critic,
    }, "after_files": _manifest()}, open(WORK / "scenarioB.json", "w"), indent=2)

    print("\n=== Scenario B: post-repair scan ===")
    post = scanner.scan(ws)
    print("state:", post.state, "authoritative:", post.authoritative,
          "findings:", len(post.findings))
    for f in post.findings:
        print(f"  - [{f.severity}] {f.rule_id}: {f.finding[:70]}")
    json.dump(post.to_dict(), open(WORK / "scenarioB_post.json", "w"), indent=2)
    print("\nsaved:", WORK / "scenarioB.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
