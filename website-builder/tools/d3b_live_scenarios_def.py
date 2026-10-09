"""Live VPS Scenarios D, E, F (no paid calls).

D -- bounded failure: a repair that makes no change must STALL and stop, with
     the attempt limit respected and no publication.
E -- recovery: crash/fault injection around a durable repair boundary, then
     reconcile; the durable attempt count must prevent duplicate/unbounded
     repair.
F -- dependency integrity: compare before/after manifests and project state.

Uses the REAL ImpeccableScanner against a disposable project and the REAL
production ``QAOrchestrator`` path. The FRONTEND adapter is a controlled
adapter that writes NOTHING (Scenario D) or writes a minimal change (Scenario F
checks manifests are untouched).

Run:  python tools/d3b_live_scenarios_def.py
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

from app.core import critic_policy as cp  # noqa: E402
from app.core.critic_repair import ImpeccableScanner  # noqa: E402
from app.core.lifecycle import ProjectLifecycle  # noqa: E402
from app.core.state import ProjectStateStore  # noqa: E402
from app.qa.orchestrator import QAOrchestrator  # noqa: E402
from app.qa.render import LocalRenderer  # noqa: E402
from app.qa.screenshot import ScreenshotCapture  # noqa: E402
from app.sandbox.runner import ProjectRunner  # noqa: E402

_PROVEN_APP_TSX = r"""export default function App() {
  const services = [
    { name: "Signature Cut", price: "$35" },
    { name: "Beard Sculpt", price: "$25" },
    { name: "Cut & Beard", price: "$52" },
  ];
  return (
    <div style={{ fontFamily: "Georgia, serif", background: "#f7f4ef", color: "#1b1b1b", margin: 0 }}>
      <header style={{ padding: "18px 20px", background: "#141414", color: "#f7f4ef", textAlign: "center" }}>
        <strong style={{ fontSize: "20px", letterSpacing: "3px" }}>NORTHCUT</strong>
      </header>

      <section style={{ padding: "64px 24px", textAlign: "center", background: "#1c1a17", color: "#f7f4ef" }}>
        <h1 style={{ fontSize: "40px", lineHeight: 1.15, margin: "0 0 18px" }}>Sharp cuts, done right.</h1>
        <p style={{ fontSize: "18px", lineHeight: 1.6, color: "#d8d0c3", maxWidth: "540px", margin: "0 auto 32px" }}>
          A traditional barbershop in the heart of the city. Walk in, sit down,
          leave sharper. Book your chair online in under a minute.
        </p>
        <a
          href="#book"
          style={{ display: "inline-block", padding: "18px 40px", background: "#c8a15a",
                   color: "#141414", textDecoration: "none", borderRadius: "4px",
                   fontWeight: 700, fontSize: "17px" }}
        >
          Book an appointment
        </a>
      </section>

      <section style={{ padding: "56px 24px", maxWidth: "720px", margin: "0 auto" }}>
        <h2 style={{ fontSize: "30px", margin: "0 0 24px", textAlign: "center" }}>Services</h2>
        {services.map((s) => (
          <div
            key={s.name}
            style={{ display: "flex", justifyContent: "space-between", alignItems: "center",
                     padding: "20px 22px", marginBottom: "14px", background: "#ffffff",
                     border: "1px solid #e2dbcf", borderRadius: "6px",
                     transition: "width 300ms, height 300ms" }}
          >
            <span style={{ fontSize: "19px" }}>{s.name}</span>
            <strong style={{ fontSize: "19px", color: "#8a6a24" }}>{s.price}</strong>
          </div>
        ))}
      </section>

      <section style={{ padding: "0 24px 56px", maxWidth: "720px", margin: "0 auto" }}>
        <h2 style={{ fontSize: "30px", margin: "0 0 20px", textAlign: "center" }}>Opening hours</h2>
        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
          <li style={{ display: "flex", justifyContent: "space-between", padding: "14px 0", borderBottom: "1px solid #e2dbcf" }}>
            <span>Monday – Friday</span><span>9:00 – 19:00</span>
          </li>
          <li style={{ display: "flex", justifyContent: "space-between", padding: "14px 0", borderBottom: "1px solid #e2dbcf" }}>
            <span>Saturday</span><span>9:00 – 17:00</span>
          </li>
          <li style={{ display: "flex", justifyContent: "space-between", padding: "14px 0" }}>
            <span>Sunday</span><span>Closed</span>
          </li>
        </ul>
      </section>

      <section id="book" style={{ padding: "56px 24px", background: "#141414", color: "#f7f4ef", textAlign: "center" }}>
        <h2 style={{ fontSize: "30px", margin: "0 0 14px" }}>Ready when you are</h2>
        <p style={{ color: "#d8d0c3", fontSize: "17px", lineHeight: 1.6, maxWidth: "520px", margin: "0 auto 28px" }}>
          Send us your name and preferred time and we will confirm your chair by
          email within the hour.
        </p>
        <a
          href="mailto:book@northcut.example"
          style={{ display: "inline-block", padding: "18px 40px", background: "#c8a15a",
                   color: "#141414", textDecoration: "none", borderRadius: "4px",
                   fontWeight: 700, fontSize: "17px" }}
        >
          Request a booking
        </a>
      </section>

      <footer style={{ padding: "28px 24px", textAlign: "center", color: "#8a8f98", fontSize: "13px" }}>
        Northcut Barbershop · 14 Market Street · Open six days a week
      </footer>
    </div>
  );
}
"""


WORK = Path("/tmp/d3b-vps-scenarios-def")
HERMES_HOME = Path.home() / ".hermes-website"
FULL_SKILL_SRC = Path("/tmp/impeccable-full")


class NoOpFrontend:
    """A FRONTEND adapter that declares success but writes nothing.

    Used to force a PERSISTENT finding (no improvement) so the loop must STALL.
    """

    def __init__(self):
        self.calls = []

    def frontend_build(self, **kwargs):
        self.calls.append(kwargs.get("build_operation_id"))
        return {"success": True, "design_dna": {"version": 1, "brand_personality": "premium"}}

    def vision_inspect(self, desktop, mobile, brief, design_dna=None):
        return {"pass": True, "blocking": [], "observations": [], "summary": "ok"}


def _full_skill() -> Path:
    skill = WORK / "skills-full" / "impeccable"
    if skill.exists():
        shutil.rmtree(skill.parent)
    skill.parent.mkdir(parents=True)
    shutil.copytree(HERMES_HOME / "skills" / "impeccable", skill)
    shutil.copytree(FULL_SKILL_SRC / "node_modules", skill / "node_modules")
    return skill


def _make_project(name: str) -> Path:
    ws = WORK / "workspaces" / name
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    starter = Path("/home/albertus527/hermes-website/templates/frontend-starter")
    for item in starter.iterdir():
        if item.name in ("dist", ".git"):
            continue
        dest = ws / item.name
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)
    # Build node_modules once (offline, from the starter's own lockfile) so the
    # REAL `npm run build` / `npm run typecheck` in _run_rebuild_checks work.
    _ensure_node_modules(ws)
    (ws / "design-dna.json").write_text(
        json.dumps({"version": 1, "brand_personality": "premium"}), encoding="utf-8"
    )
    # A complete, mobile-first page with exactly one actionable critic finding
    # (layout-transition: animating width/height). This design is VISION-clean
    # (verified live: pass=True, no blocking findings), so the ONLY trigger is
    # the critic finding -- which is what these scenarios must isolate.
    (ws / "src" / "App.tsx").write_text(_PROVEN_APP_TSX, encoding="utf-8")
    return ws


def _ensure_node_modules(ws: Path) -> None:
    """Install node_modules offline (npm ci, warm cache) and BUILD dist/.

    ``npm run preview`` (the QA render server) serves ``dist/``, so a project
    without a build cannot render. ``npm ci`` is lockfile-exact and offline when
    the npm cache is warm; the build is the project's own ``npm run build``.
    """
    import subprocess as _sp

    node_bin = shutil.which("node")
    if node_bin is None:
        return
    bindir = str(Path(node_bin).parent)
    env = dict(os.environ)
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    npm = str(Path(bindir) / "npm")
    try:
        _sp.run([npm, "ci", "--no-audit", "--no-fund"], cwd=str(ws), env=env,
                timeout=900, capture_output=True, text=True, check=True)
        _sp.run([npm, "run", "build"], cwd=str(ws), env=env, timeout=600,
                capture_output=True, text=True, check=True)
    except Exception as exc:  # a build failure is surfaced by the QA render step
        print(f"  [warn] install/build for {ws.name} failed: {type(exc).__name__}")


def _manifest(ws: Path) -> dict:
    """Source manifest: everything except runtime output (node_modules, dist, qa).

    ``qa/`` is application-produced evidence (screenshots), not project source;
    it is excluded exactly as ``app/deploy/snapshot.py`` excludes it.
    """
    out = {}
    for p in sorted(ws.rglob("*")):
        if not p.is_file():
            continue
        parts = set(p.parts)
        if "node_modules" in parts or "dist" in parts or "qa" in parts:
            continue
        out[str(p.relative_to(ws))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _bootstrap(store, pid, ws):
    with store.acquire_writer(pid) as st:
        st.brief = {"name": "Northcut", "what": "barbershop", "why": "online booking"}
        st.design_dna = json.loads((ws / "design-dna.json").read_text())
        store.save(st)
    store.transition_lifecycle(pid, ProjectLifecycle.READY)
    store.transition_lifecycle(pid, ProjectLifecycle.QUEUED)
    store.transition_lifecycle(pid, ProjectLifecycle.RUNNING)


def scenario_d(store, runner, scanner, adapter) -> dict:
    print("\n=== Scenario D: bounded failure (no improvement) ===")
    pid = "scenD"
    ws = _make_project(pid)
    _bootstrap(store, pid, ws)

    pre = scanner.scan(ws)
    print("pre-scan:", pre.state, "findings:", len(pre.findings))

    orch = QAOrchestrator(runner, store, hermes_adapter=adapter, critic_scanner=scanner)
    # Build/typecheck pass; browser QA passes; FRONTEND writes nothing.
    result = orch.run(pid, ws, {"name": "Northcut", "what": "barbershop",
                                "why": "online booking"},
                      json.loads((ws / "design-dna.json").read_text()))
    state = store.load(pid)
    critic = state.deployment.get("critic") or {}
    print("qa success:", result.success)
    print("critic state:", critic.get("state"))
    print("critic outcome:", critic.get("outcome"))
    print("attempts_used:", critic.get("attempts_used"))
    print("lifecycle:", state.lifecycle)
    print("adapter calls (repairs attempted):", len(adapter.calls))
    checks = {
        "loop_stopped": critic.get("outcome") in (
            cp.OUTCOME_STALLED, cp.OUTCOME_EXHAUSTED, cp.OUTCOME_REJECTED,
        ),
        "attempts_within_limit": (critic.get("attempts_used") or 0) <= cp.MAX_CRITIC_REPAIR_ATTEMPTS,
        "not_preview_ready": state.lifecycle != ProjectLifecycle.PREVIEW_READY.value,
        "not_live": state.lifecycle not in (
            ProjectLifecycle.LIVE.value, ProjectLifecycle.PUBLISHING.value,
        ),
        "no_publication_identity": state.production_url is None,
    }
    print("checks:", checks)
    return {"scenario": "D", "critic": critic, "lifecycle": state.lifecycle,
            "checks": checks, "adapter_calls": len(adapter.calls)}


def scenario_e(store, runner, scanner, adapter) -> dict:
    print("\n=== Scenario E: recovery around a durable boundary ===")
    pid = "scenE"
    ws = _make_project(pid)
    _bootstrap(store, pid, ws)

    # Fault injection: the FRONTEND adapter records the durable attempt and then
    # raises, simulating a crash mid-repair (AFTER the attempt is recorded,
    # BEFORE validation). The run must fail closed and leave durable evidence.
    class CrashingFrontend(NoOpFrontend):
        def frontend_build(self, **kwargs):
            super().frontend_build(**kwargs)
            raise RuntimeError("simulated crash mid-repair")

    crashing = CrashingFrontend()
    orch = QAOrchestrator(runner, store, hermes_adapter=crashing, critic_scanner=scanner)
    result = orch.run(pid, ws, {"name": "Northcut", "what": "barbershop",
                                "why": "online booking"},
                      json.loads((ws / "design-dna.json").read_text()))
    state = store.load(pid)
    repair_bag = state.deployment.get("critic_repair") or {}
    print("qa success:", result.success)
    print("lifecycle:", state.lifecycle)
    print("durable attempts_started:", repair_bag.get("attempts_started"))
    print("durable last_attempt:", repair_bag.get("last_attempt"))

    # Reconcile: the durable attempt identity survives, so a re-run cannot
    # silently restart the count. The project is FAILED (fail-closed), which the
    # existing stranded-state reconciler also treats as recoverable.
    from app.core.state import reconcile_stranded_projects
    recovered = reconcile_stranded_projects(store)
    print("reconciler recovered:", recovered)

    # A fresh CriticStage resumed from the durable attempt count cannot exceed
    # the limit.
    from app.qa.critic_stage import CriticStage
    from app.core.critic_policy import RepairBudget
    calls = {"n": 0}

    def _repair(findings, prev, budget, idx):
        calls["n"] += 1
        return True

    stage = CriticStage(
        scanner=scanner,
        repair_fn=_repair,
        rebuild_fn=lambda: (True, True, True),
        browser_qa_fn=lambda: type("A", (), {"final_pass": True, "infrastructure_error": None})(),
        budget=RepairBudget(attempts_used=int(repair_bag.get("attempts_started", 0))),
    )
    resumed = stage.run(ws)
    print("resumed outcome:", resumed.outcome, "attempts_used:", resumed.attempts_used)
    print("resumed repairs attempted:", calls["n"])

    checks = {
        "crash_failed_closed": result.success is False,
        "durable_attempt_recorded": int(repair_bag.get("attempts_started", 0)) >= 1,
        "resume_did_not_reset_count": resumed.attempts_used >= int(repair_bag.get("attempts_started", 0)),
        "resume_bounded": calls["n"] <= cp.MAX_CRITIC_REPAIR_ATTEMPTS,
        "no_duplicate_unbounded": calls["n"] <= max(0, cp.MAX_CRITIC_REPAIR_ATTEMPTS - int(repair_bag.get("attempts_started", 0))) + 1,
    }
    print("checks:", checks)
    return {"scenario": "E", "lifecycle": state.lifecycle,
            "durable": repair_bag, "resumed": resumed.to_dict(), "checks": checks}


def scenario_f(store, runner, scanner, adapter) -> dict:
    print("\n=== Scenario F: dependency integrity ===")
    pid = "scenF"
    ws = _make_project(pid)
    _bootstrap(store, pid, ws)

    before = _manifest(ws)
    toolchain = {k: before[k] for k in before if k in (
        "package.json", "package-lock.json", ".nvmrc", "tsconfig.json",
        "tsconfig.app.json", "tsconfig.node.json", "vite.config.ts",
        "components.json", ".npmrc",
    )}

    orch = QAOrchestrator(runner, store, hermes_adapter=adapter, critic_scanner=scanner)
    orch.run(pid, ws, {"name": "Northcut", "what": "barbershop", "why": "online booking"},
             json.loads((ws / "design-dna.json").read_text()))

    after = _manifest(ws)
    toolchain_after = {k: after[k] for k in after if k in toolchain}
    changed_toolchain = {k: (toolchain[k], toolchain_after.get(k)) for k in toolchain
                         if toolchain[k] != toolchain_after.get(k)}
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    print("toolchain files unchanged:", not changed_toolchain)
    print("added files:", added)
    print("removed files:", removed)

    checks = {
        "no_toolchain_mutation": not changed_toolchain,
        "no_new_files_outside_src": not [a for a in added if not a.startswith("src/")],
        "no_unexpected_lockfile_change": "package-lock.json" not in changed_toolchain,
        "no_removed_source": not removed,
    }
    print("checks:", checks)
    return {"scenario": "F", "before_count": len(before), "after_count": len(after),
            "changed_toolchain": list(changed_toolchain), "added": added,
            "removed": removed, "checks": checks}


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    store = ProjectStateStore(WORK / "state")
    runner = ProjectRunner(WORK / "workspaces", store, hermes_home=HERMES_HOME)
    scanner = ImpeccableScanner(
        skill_root=_full_skill(), node_executable=shutil.which("node"),
        timeout_seconds=120,
    )

    results = []
    results.append(scenario_d(store, runner, scanner, NoOpFrontend()))
    results.append(scenario_e(store, runner, scanner, NoOpFrontend()))
    results.append(scenario_f(store, runner, scanner, NoOpFrontend()))

    json.dump(results, open(WORK / "scenarios_def.json", "w"), indent=2, default=str)
    print("\n=== SUMMARY ===")
    for r in results:
        ok = all(r["checks"].values())
        print(f"  Scenario {r['scenario']}: {'PASS' if ok else 'FAIL'} {r['checks']}")
    print("saved:", WORK / "scenarios_def.json")
    return 0 if all(all(r["checks"].values()) for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
