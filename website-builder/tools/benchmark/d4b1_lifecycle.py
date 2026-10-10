#!/usr/bin/env python3
"""D4b.1 -- Mode B lifecycle driver: short-lived isolated Laya workers.

Spawns the real upstream Laya worker (`d4b1_worker.py`) ONCE PER BRIEF with
concurrency = 1, and measures the lifecycle the mission asks for:

  * cold-start overhead (import + checkpoint load + first forward pass);
  * end-to-end latency per brief;
  * peak process-tree memory (sampled from /proc while the worker is alive AND
    confirmed by getrusage(RUSAGE_CHILDREN).ru_maxrss for the reaped child);
  * memory reclaimed after process exit (host MemAvailable before vs after);
  * swap activity (SwapFree delta across the run);
  * CPU impact (host load average during the run);
  * worker exit verification (returncode + PID gone).

It uses representative cold-start samples (default 5 briefs) rather than loading
the checkpoint 50 times, exactly as the mission requires.

    <laya-venv>/bin/python tools/benchmark/d4b1_lifecycle.py --samples 5
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
BENCH_DIR = Path(__file__).resolve().parent

VENV_PY = str(Path.home() / ".website-builder/laya/venv/bin/python")


def meminfo() -> dict:
    d = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            k, _, v = line.partition(":")
            d[k] = int(v.split()[0])
    return d


def loadavg() -> float:
    return float(open("/proc/loadavg").read().split()[0])


def proc_tree_rss_mb(root_pid: int) -> float:
    """Sum RSS (MB) over the worker process and all its descendants."""
    pids = [root_pid]
    seen = set()
    while pids:
        pid = pids.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            children = open(f"/proc/{pid}/task/{pid}/children").read().split()
            pids.extend(int(c) for c in children)
        except Exception:
            pass
    total_kb = 0.0
    for pid in seen:
        try:
            with open(f"/proc/{pid}/status") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        total_kb += int(line.split()[1])
        except Exception:
            pass
    return total_kb / 1024.0


def run_one(brief: str, models: str, sample_rss: bool = True):
    before = meminfo()
    load_before = loadavg()
    # record host load/swap DURING the worker via a light sampler thread
    peak_host_load = [load_before]
    peak_host_swap_used = [0.0]
    t0 = time.monotonic()
    proc = subprocess.Popen(
        [VENV_PY, str(BENCH_DIR / "d4b1_worker.py"), "--brief", brief, "--models", models],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(ROOT),
    )
    peak_rss = 0.0
    # sample the live process tree + host load while it runs
    while proc.poll() is None:
        if sample_rss:
            r = proc_tree_rss_mb(proc.pid)
            if r > peak_rss:
                peak_rss = r
        l = loadavg()
        if l > peak_host_load[-1]:
            peak_host_load.append(l)
        mi = meminfo()
        used = (mi["SwapTotal"] - mi["SwapFree"]) / 1024.0
        if used > peak_host_swap_used[-1]:
            peak_host_swap_used.append(used)
        time.sleep(0.15)
    out, err = proc.communicate()
    e2e_s = time.monotonic() - t0
    # ru_maxrss for reaped children (cumulative max across all waited children, KB on Linux)
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    # the OS reclaims the whole process image on exit; measure host MemAvailable
    # recovery after the child is reaped. Reclaim is proven by the child PID no
    # longer existing AND host available memory returning to ~pre-run level.
    reclaimed = 0.0
    deadline = time.monotonic() + 15.0
    target = before["MemAvailable"] - 64 * 1024  # allow 64 MB slack
    while time.monotonic() < deadline:
        after = meminfo()
        if after["MemAvailable"] >= target:
            break
        time.sleep(0.5)
    after = meminfo()
    pid_gone = not Path(f"/proc/{proc.pid}").exists()
    post_exit_tree_rss = proc_tree_rss_mb(proc.pid) if not pid_gone else 0.0
    reclaimed_mb = (after["MemAvailable"] - before["MemAvailable"]) / 1024.0
    result = None
    try:
        result = json.loads(out.strip().splitlines()[-1]) if out.strip() else None
    except Exception:
        result = None
    return {
        "returncode": proc.returncode,
        "worker_pid_gone_after_exit": pid_gone,
        "post_exit_tree_rss_mb": round(post_exit_tree_rss, 1),
        "e2e_s": round(e2e_s, 3),
        "peak_sampled_rss_mb": round(peak_rss, 1),
        "ru_maxrss_children_mb": round(ru.ru_maxrss / 1024.0, 1),
        "mem_available_before_mb": round(before["MemAvailable"] / 1024.0, 1),
        "mem_available_after_mb": round(after["MemAvailable"] / 1024.0, 1),
        "reclaimed_mb": round(reclaimed_mb, 1),
        "swapfree_before_mb": round(before["SwapFree"] / 1024.0, 1),
        "swapfree_after_mb": round(after["SwapFree"] / 1024.0, 1),
        "peak_host_load_during": max(peak_host_load),
        "peak_host_swap_used_mb_during": max(peak_host_swap_used),
        "load_before": load_before,
        "load_after": loadavg(),
        "worker_result": result,
        "stderr_tail": err.strip().splitlines()[-3:] if err.strip() else [],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument("--models", default=str(Path.home() / ".website-builder/laya/models"))
    ap.add_argument("--out", default=str(BENCH_DIR / "results" / "d4b1_lifecycle.json"))
    args = ap.parse_args()

    data = json.loads((BENCH_DIR / "d4b1_dataset.json").read_text())
    cases = data["cases"]
    # representative sample: spread across the dataset, mix of languages
    step = max(1, len(cases) // args.samples)
    sample = cases[::step][: args.samples]

    runs = []
    for c in sample:
        print(f"[modeB] worker for {c['id']} ({c['language']}) ...", flush=True)
        r = run_one(c["brief"], args.models)
        r["case_id"] = c["id"]
        r["language"] = c["language"]
        runs.append(r)
        print(f"        rc={r['returncode']} e2e={r['e2e_s']}s "
              f"peak={r['peak_sampled_rss_mb']}MB reclaimed={r['reclaimed_mb']}MB", flush=True)

    ok = [r for r in runs if r["returncode"] == 0 and r["worker_result"]]
    import statistics as st
    summary = {
        "mode": "B_short_lived_isolated_worker",
        "concurrency": 1,
        "samples": len(runs),
        "workers_ok": len(ok),
        "cold_start_overhead_s": [r["worker_result"]["cold_load_and_first_forward_s"] for r in ok],
        "import_s": [r["worker_result"]["import_s"] for r in ok],
        "e2e_s": [r["e2e_s"] for r in ok],
        "peak_sampled_rss_mb": [r["peak_sampled_rss_mb"] for r in ok],
        "e2e_p50_s": round(st.median([r["e2e_s"] for r in ok]), 3) if ok else None,
        "peak_rss_max_mb": round(max((r["peak_sampled_rss_mb"] for r in ok), default=0.0), 1),
        "cold_start_p50_s": round(st.median([r["worker_result"]["cold_load_and_first_forward_s"] for r in ok]), 3) if ok else None,
        "reclaimed_mb_mean": round(st.fmean([r["reclaimed_mb"] for r in ok]), 1) if ok else None,
        "all_workers_exited": all(r["worker_pid_gone_after_exit"] for r in ok) if ok else None,
        "max_host_swap_used_mb_during": round(max((r["peak_host_swap_used_mb_during"] for r in ok), default=0.0), 1),
        "max_host_load_during": round(max((r["peak_host_load_during"] for r in ok), default=0.0), 2),
        "runs": runs,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(summary, indent=2, default=str))
    print(f"\nwrote {args.out}")
    print(json.dumps({k: v for k, v in summary.items() if k != "runs"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
