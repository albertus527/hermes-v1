#!/usr/bin/env python3
"""D4b.2 FINAL PROOF -- one command, one verdict.

Extends the accepted D4b proof battery with the D4b.2 multilingual-expansion
additions:

  1. the focused D4b.2 tests (multilingual query expansion, offline);
  2. the focused D4b tests (the accepted context-preparation layer, unchanged);
  3. the focused D4a/D4a.1 tests (the adapter Laya consumes, unchanged);
  4. the DEFAULT offline suite, with non-loopback network blocked at the Python
     level -- so one run proves both "green" AND "offline";
  5. the applicable D3a.5 and D3b regression tests;
  6. the D3a.5/D3b/D4a/D4a.1/D4b mutation drivers (unchanged guard counts);
  7. the NEW D4b.2 mutation driver;
  8. the branch, scope, and security guardrails, INCLUDING the D4b.2
     English-neutrality invariant (the expansion must not change an English
     brief's query plan).

Exit code 0 iff every check passes. Nothing here mutates the working tree: the
drivers copy the tree to a temp dir, and this script only reads.

Run (from ``website-builder``, with the venv active):

    python tools/d4b2_final_proof.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent

D4B2_TEST_FILES: Tuple[str, ...] = ("tests/test_d4b2_multilingual.py",)

D4B_TEST_FILES: Tuple[str, ...] = (
    "tests/test_laya_context.py",
    "tests/test_laya_integration.py",
    "tests/test_laya_composition.py",
)

D4A_TEST_FILES: Tuple[str, ...] = (
    "tests/test_openviking_library.py",
    "tests/test_openviking_retrieval.py",
    "tests/test_openviking_composition.py",
    "tests/test_openviking_live.py",
)

D3_REGRESSION_TESTS: Tuple[str, ...] = (
    "tests/test_design_critic.py",
    "tests/test_design_activation.py",
    "tests/test_design_resource_activation.py",
    "tests/test_design_dependency_pins.py",
    "tests/test_design_registry.py",
    "tests/test_design_registry_contract.py",
    "tests/test_design_install.py",
    "tests/test_design_refero.py",
    "tests/test_design_resources.py",
    "tests/test_design_resource_coherence.py",
    "tests/test_design_selection.py",
    "tests/test_design_dna_contract.py",
    "tests/test_qa.py",
    "tests/test_build.py",
    "tests/test_runtime.py",
    "tests/test_critic_policy.py",
    "tests/test_critic_repair.py",
    "tests/test_critic_stage.py",
    "tests/test_critic_integration.py",
    "tests/test_critic_composition.py",
)

D3A5_DRIVERS: Tuple[Tuple[str, int], ...] = (
    ("mutation_check_d3a5_parta.py", 16),
    ("mutation_check_d3a5_partbc.py", 73),
    ("mutation_check_d3a5_partc.py", 39),
    ("mutation_check_d3a5_partd.py", 36),
    ("mutation_check_d3a5_parti.py", 18),
)

D3B_DRIVER = ("mutation_check_d3b.py", 14)
D4A_DRIVER = ("mutation_check_d4a.py", 17)
D4A1_DRIVER = ("mutation_check_d4a1.py", 9)
D4B_DRIVER = ("mutation_check_d4b.py", 10)
D4B2_DRIVER = ("mutation_check_d4b2.py", 7)

EXPECTED_DESELECTED = 4

WORK_BRANCH = "web-design"
UNTOUCHED_BRANCH = "feature/website"
UNTOUCHED_BASELINE = "868ed00e3f24e06f1dcf9944d6d031105dff0646"

NO_LAYER_IMPORTS: Tuple[str, ...] = (
    "app/projects/build.py",
    "app/projects/revise.py",
    "app/qa/orchestrator.py",
    "app/qa/critic_stage.py",
    "app/core/design_context.py",
    "app/core/composition.py",
)

REQUIRED_ARTIFACTS: Tuple[str, ...] = (
    "app/core/laya_context.py",
    "tools/mutation_check_d4b.py",
    "tools/mutation_check_d4b2.py",
    "tools/d4b2_final_proof.py",
    "tools/benchmark/d4b2_experiment.py",
    "tools/benchmark/d4b2_heldout.json",
    "docs/D4B2_MULTILINGUAL_RETRIEVAL_BENCHMARK.md",
    "docs/D4B2_MULTILINGUAL_RETRIEVAL_ACCEPTANCE.md",
    "docs/D4B_LAYA_CONTEXT_PREPARATION_ACCEPTANCE.md",
    # Preserved D4a artifacts.
    "app/core/openviking_library.py",
    "app/core/openviking_retrieval.py",
    "app/core/openviking_live.py",
)

_NETBLOCK = '''
import ipaddress
import socket as _socket

_LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}

def _is_loopback(host):
    if host in _LOOPBACK:
        return True
    try:
        return ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        return False

_orig_connect = _socket.socket.connect
_orig_getaddrinfo = _socket.getaddrinfo

def _connect(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if not _is_loopback(host):
        raise RuntimeError(f"NETWORK BLOCKED (non-loopback): {host}")
    return _orig_connect(self, address)

def _getaddrinfo(host, *a, **k):
    if not _is_loopback(host):
        raise RuntimeError(f"DNS BLOCKED (non-loopback): {host}")
    return _orig_getaddrinfo(host, *a, **k)

_socket.socket.connect = _connect
_socket.socket.connect_ex = _connect
_socket.getaddrinfo = _getaddrinfo

try:
    import urllib.request as _ur
    _orig_urlopen = _ur.urlopen

    def _urlopen(url, *a, **k):
        host = getattr(url, "host", None) or str(url).split("//")[-1].split("/")[0]
        if not _is_loopback(host.split(":")[0]):
            raise RuntimeError(f"HTTP BLOCKED (non-loopback): {host}")
        return _orig_urlopen(url, *a, **k)

    _ur.urlopen = _urlopen
except Exception:
    pass
'''


class Check:
    def __init__(self, name: str, ok: bool, detail: str = ""):
        self.name = name
        self.ok = ok
        self.detail = detail


def _run(cmd, *, cwd, env=None, timeout=1800):
    return subprocess.run(
        cmd, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout,
    )


def _summary(output: str) -> str:
    lines = [ln for ln in output.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def check_focused(name: str, files: Tuple[str, ...]) -> Check:
    proc = _run(
        [sys.executable, "-m", "pytest", *files, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
    )
    tail = _summary(proc.stdout)
    ok = proc.returncode == 0 and "failed" not in tail and "error" not in tail
    return Check(name, ok, tail)


def check_d3_regression() -> Check:
    proc = _run(
        [sys.executable, "-m", "pytest", *D3_REGRESSION_TESTS, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
    )
    tail = _summary(proc.stdout)
    ok = proc.returncode == 0 and "failed" not in tail and "error" not in tail
    return Check("D3a.5/D3b regression tests", ok, tail)


def check_suite_offline() -> Check:
    with tempfile.TemporaryDirectory(prefix="d4b2-proof-netblock-") as tmp:
        (Path(tmp) / "sitecustomize.py").write_text(_NETBLOCK, encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = tmp + os.pathsep + env.get("PYTHONPATH", "")
        proc = _run(
            [sys.executable, "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider"],
            cwd=ROOT, env=env,
        )
    tail = _summary(proc.stdout)
    ok = proc.returncode == 0 and "failed" not in tail and "error" not in tail
    m = re.search(r"(\d+) deselected", tail)
    deselected = int(m.group(1)) if m else 0
    if ok and deselected != EXPECTED_DESELECTED:
        ok = False
        tail += f"  (expected {EXPECTED_DESELECTED} deselected, saw {deselected})"
    return Check("full offline suite (network blocked)", ok, tail)


def check_driver(filename: str, expected: int, *, label: str) -> Check:
    proc = _run([sys.executable, str(ROOT / "tools" / filename)], cwd=ROOT, timeout=1500)
    out = proc.stdout + proc.stderr
    m = re.search(r"all (\d+) guards killed", out)
    killed = int(m.group(1)) if m else None
    ok = proc.returncode == 0 and killed == expected
    detail = f"{killed}/{expected} guards killed" if killed is not None else "no summary"
    if not ok:
        detail += f"  | {_summary(out)[-160:]}"
    return Check(f"{label} {filename}", ok, detail)


def _git(*args: str) -> str:
    return _run(["git", *args], cwd=REPO).stdout.strip()


def check_english_neutrality() -> Check:
    """D4b.2 invariant: the expansion must not change an English query plan."""
    script = (
        "import json, sys; sys.path.insert(0, '.');"
        "from app.core import laya_context as lc;"
        "off = lc.LayaConfig(enabled=True, multilingual_expansion=False);"
        "on = lc.LayaConfig(enabled=True, multilingual_expansion=True);"
        "briefs = ["
        "'Build an editorial online magazine about slow food culture.',"
        "'A corporate consulting firm website with clear service pages.',"
        "'Portfolio site for a photographer, gallery-first layout.',"
        "'SaaS landing page with pricing table and feature cards.',"
        "'A minimalist architecture studio website with generous whitespace.',"
        "'Design an admin dashboard with data tables, nav, and modal dialogs.',"
        "'A motion-led brand launch microsite with scroll reveals.',"
        "'A restaurant website with a menu grid and a brand layout.',"
        "'A coffee shop site with opening hours, a jam-packed gallery.',"
        "];"
        "bad = [b for b in briefs if lc.plan_queries(b, None, on) != lc.plan_queries(b, None, off)];"
        "print('ENGLISH_NEUTRAL' if not bad else 'CHANGED:' + repr(bad))"
    )
    proc = _run([sys.executable, "-c", script], cwd=ROOT)
    out = (proc.stdout + proc.stderr).strip()
    return Check("English query plan unchanged by the expansion",
                 proc.returncode == 0 and out.endswith("ENGLISH_NEUTRAL"),
                 out[-160:] or "no output")


def check_guardrails() -> List[Check]:
    checks: List[Check] = []
    branch = _git("branch", "--show-current")
    checks.append(Check(f"branch is {WORK_BRANCH}", branch == WORK_BRANCH, branch))

    actual = _git("rev-parse", UNTOUCHED_BRANCH)
    checks.append(Check(
        f"{UNTOUCHED_BRANCH} untouched", actual == UNTOUCHED_BASELINE,
        f"{actual} (baseline {UNTOUCHED_BASELINE})",
    ))

    missing = [p for p in REQUIRED_ARTIFACTS if not (ROOT / p).is_file()]
    checks.append(Check("D4b.2 + D4b + preserved D4a artifacts present", not missing,
                        ", ".join(missing) or "all present"))

    leaked: List[str] = []
    for rel in NO_LAYER_IMPORTS:
        path = ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8").lower()
        if "openviking" in text or "laya" in text:
            leaked.append(rel)
    checks.append(Check(
        "no OpenViking/Laya wiring into FRONTEND/QA/Design-DNA",
        not leaked, ", ".join(leaked) or "none",
    ))

    laya_src = (ROOT / "app/core/laya_context.py").read_text(encoding="utf-8")
    forbidden_tokens = ("hermes plugins", "hermes memory", "subprocess", "os.system")
    offenders = [t for t in forbidden_tokens if t in laya_src]
    checks.append(Check(
        "no global install / memory-plugin invocation in Laya",
        not offenders, ", ".join(offenders) or "none",
    ))

    cfg = (ROOT / "config/default.yaml").read_text(encoding="utf-8")
    disabled = re.search(r"laya:\s*\n\s*enabled:\s*false", cfg) is not None
    checks.append(Check("laya disabled by default in config", disabled,
                        "enabled: false present" if disabled else "not found"))

    # D4b.2: the multilingual expansion is DISABLED in the shipped config.
    me_disabled = re.search(r"multilingual_expansion:\s*false", cfg) is not None
    checks.append(Check("multilingual expansion disabled by default in config",
                        me_disabled, "multilingual_expansion: false present" if me_disabled else "not found"))

    checks.append(check_english_neutrality())
    return checks


def main() -> int:
    print("=" * 72)
    print("D4b.2 FINAL PROOF")
    print("=" * 72, flush=True)

    checks: List[Check] = []

    print("\n[1/8] focused D4b.2 tests ...", flush=True)
    c = check_focused("focused D4b.2 tests", D4B2_TEST_FILES)
    checks.append(c)
    print(f"      {'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}", flush=True)

    print("\n[2/8] focused D4b tests ...", flush=True)
    c = check_focused("focused D4b tests", D4B_TEST_FILES)
    checks.append(c)
    print(f"      {'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}", flush=True)

    print("\n[3/8] focused D4a/D4a.1 tests ...", flush=True)
    c = check_focused("focused D4a/D4a.1 tests", D4A_TEST_FILES)
    checks.append(c)
    print(f"      {'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}", flush=True)

    print("\n[4/8] full offline suite (non-loopback network BLOCKED) ...", flush=True)
    c = check_suite_offline()
    checks.append(c)
    print(f"      {'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}", flush=True)

    print("\n[5/8] D3a.5/D3b regression tests ...", flush=True)
    c = check_d3_regression()
    checks.append(c)
    print(f"      {'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}", flush=True)

    print("\n[6/8] D3a.5 + D3b + D4a + D4a.1 + D4b mutation drivers ...", flush=True)
    for filename, expected in D3A5_DRIVERS:
        c = check_driver(filename, expected, label="D3a.5")
        checks.append(c)
        print(f"      {'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}", flush=True)
    for filename, expected, label in (
        (D3B_DRIVER[0], D3B_DRIVER[1], "D3b"),
        (D4A_DRIVER[0], D4A_DRIVER[1], "D4a"),
        (D4A1_DRIVER[0], D4A1_DRIVER[1], "D4a.1"),
        (D4B_DRIVER[0], D4B_DRIVER[1], "D4b"),
    ):
        c = check_driver(filename, expected, label=label)
        checks.append(c)
        print(f"      {'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}", flush=True)

    print("\n[7/8] D4b.2 mutation driver ...", flush=True)
    c = check_driver(D4B2_DRIVER[0], D4B2_DRIVER[1], label="D4b.2")
    checks.append(c)
    print(f"      {'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}", flush=True)

    print("\n[8/8] guardrails ...", flush=True)
    for c in check_guardrails():
        checks.append(c)
        print(f"      {'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}", flush=True)

    failed = [c for c in checks if not c.ok]
    print("\n" + "=" * 72)
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed")
    for c in failed:
        print(f"  FAILED: {c.name} -- {c.detail}")
    verdict = "PASS" if not failed else "FAIL"
    print(f"VERDICT: {verdict}")
    print("=" * 72)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
