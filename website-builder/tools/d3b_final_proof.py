#!/usr/bin/env python3
"""D3b FINAL PROOF -- one command, one verdict.

Runs the D3b acceptance battery and prints a consolidated PASS/FAIL:

  1. the focused D3b tests (policy, scanner, stage, production integration,
     composition root);
  2. the DEFAULT offline suite, with non-loopback network blocked at the Python
     level -- so one run proves both "green" AND "offline";
  3. the applicable D3a.5 regression tests (the dependency-ingress and critic
     seams D3b must not have broken);
  4. the D3a.5 mutation drivers, each of which must report "all N guards killed";
  5. the D3b mutation driver with its PINNED guard count;
  6. the branch and scope guardrails.

The D3a.5 *runner* (``tools/d3a5_final_proof.py``) is deliberately NOT invoked:
its historical "no D3b artifacts" phase-boundary assertion is correct for that
phase and D3b now exists, so calling it would fail for the RIGHT reason. This
runner preserves D3a.5's regression and mutation coverage directly instead of
weakening that assertion.

Exit code 0 iff every check passes. Nothing here mutates the working tree: the
drivers copy the tree to a temp dir, and this script only reads.

Run (from ``website-builder``, with the venv active):

    python tools/d3b_final_proof.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent

#: The focused D3b tests. These are the implementation gate.
D3B_TEST_FILES: Tuple[str, ...] = (
    "tests/test_critic_policy.py",
    "tests/test_critic_repair.py",
    "tests/test_critic_stage.py",
    "tests/test_critic_integration.py",
    "tests/test_critic_composition.py",
)

#: Applicable D3a.5 regression tests: the critic seam D3b builds on, the
#: dependency-ingress suite, the activation/coherence suites, and the QA/build
#: suites D3b integrated with. They must stay green with D3b present.
D3A5_REGRESSION_TESTS: Tuple[str, ...] = (
    "tests/test_design_critic.py",
    "tests/test_design_activation.py",
    "tests/test_design_resource_activation.py",
    "tests/test_design_dependency_pins.py",
    "tests/test_design_registry.py",
    "tests/test_design_registry_contract.py",
    "tests/test_design_install.py",
    "tests/test_qa.py",
    "tests/test_build.py",
)

#: The five D3a.5 mutation drivers -- (filename, exact pinned guard count).
D3A5_DRIVERS: Tuple[Tuple[str, int], ...] = (
    ("mutation_check_d3a5_parta.py", 16),
    ("mutation_check_d3a5_partbc.py", 73),
    ("mutation_check_d3a5_partc.py", 39),
    ("mutation_check_d3a5_partd.py", 36),
    ("mutation_check_d3a5_parti.py", 18),
)

#: The D3b mutation driver and its PINNED guard count. The count is pinned so
#: the driver cannot silently LOSE a guard: its own summary is "all N guards
#: killed", which stays true if a mutation is deleted.
D3B_DRIVER = "mutation_check_d3b.py"
D3B_DRIVER_GUARDS = 14

#: The default suite deselects exactly the network-needing integration tests.
EXPECTED_DESELECTED = 4

WORK_BRANCH = "web-design"
UNTOUCHED_BRANCH = "feature/website"

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


def _run(cmd: List[str], *, cwd: Path, env: dict | None = None,
         timeout: int = 1800) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout,
    )


def _summary(output: str) -> str:
    lines = [ln for ln in output.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def check_focused() -> Check:
    proc = _run(
        [sys.executable, "-m", "pytest", *D3B_TEST_FILES, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
    )
    tail = _summary(proc.stdout)
    ok = proc.returncode == 0 and "failed" not in tail and "error" not in tail
    return Check("focused D3b tests", ok, tail)


def check_d3a5_regression() -> Check:
    proc = _run(
        [sys.executable, "-m", "pytest", *D3A5_REGRESSION_TESTS, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
    )
    tail = _summary(proc.stdout)
    ok = proc.returncode == 0 and "failed" not in tail and "error" not in tail
    return Check("D3a.5 regression tests", ok, tail)


def check_suite_offline() -> Check:
    """The default suite must be GREEN while the internet is unreachable."""
    with tempfile.TemporaryDirectory(prefix="d3b-proof-netblock-") as tmp:
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


def check_driver(filename: str, expected: int, *, gate: bool) -> Check:
    proc = _run([sys.executable, str(ROOT / "tools" / filename)], cwd=ROOT)
    out = proc.stdout + proc.stderr
    m = re.search(r"all (\d+) guards killed", out)
    killed = int(m.group(1)) if m else None
    ok = proc.returncode == 0 and killed == expected
    detail = f"{killed}/{expected} guards killed" if killed is not None else "no summary"
    if not ok:
        detail += f"  | {_summary(out)[-160:]}"
    label = ("D3a.5 " if gate else "D3b ") + filename
    return Check(label, ok, detail)


def _git(*args: str) -> str:
    return _run(["git", *args], cwd=REPO).stdout.strip()


def check_guardrails() -> List[Check]:
    checks: List[Check] = []
    branch = _git("branch", "--show-current")
    checks.append(Check(f"branch is {WORK_BRANCH}", branch == WORK_BRANCH, branch))

    baseline = "868ed00e3f24e06f1dcf9944d6d031105dff0646"
    actual = _git("rev-parse", UNTOUCHED_BRANCH)
    checks.append(Check(
        f"{UNTOUCHED_BRANCH} untouched", actual == baseline,
        f"{actual} (baseline {baseline})",
    ))

    # The D3b artifacts exist and the D3a.5 critic seam is preserved.
    required = [
        "app/core/critic_policy.py",
        "app/core/critic_repair.py",
        "app/qa/critic_stage.py",
        "tools/mutation_check_d3b.py",
        "tools/d3b_final_proof.py",
        "app/core/design_critic.py",  # the D3a.5 seam is untouched
    ]
    missing = [p for p in required if not (ROOT / p).is_file()]
    checks.append(Check("D3b + preserved D3a.5 artifacts present", not missing,
                        ", ".join(missing) or "all present"))

    # The critic contract is unchanged: the fixed target and the absence of a
    # repair verb in the engine argv.
    critic = (ROOT / "app/core/design_critic.py").read_text(encoding="utf-8")
    suffix_ok = 'CRITIC_ENGINE_ARGV_SUFFIX: Tuple[str, ...] = ("detect", "--json", "--quiet", ".")' in critic
    checks.append(Check("critic argv still fixed with the '.' target", suffix_ok,
                        "suffix constant present" if suffix_ok else "suffix changed"))

    # No unrelated branch was introduced.
    branches = _git("branch", "--list", "--format=%(refname:short)")
    checks.append(Check(
        "no unexpected working branch",
        all(b in {"web-design", "feature/website", "main", "master"} or b.startswith(("d3", "r2"))
            for b in branches.splitlines() if b),
        ", ".join(branches.splitlines()) or "(none)",
    ))
    return checks


def main() -> int:
    print("=" * 72)
    print("D3b FINAL PROOF")
    print("=" * 72, flush=True)

    checks: List[Check] = []

    print("\n[1/6] focused D3b tests ...", flush=True)
    c = check_focused()
    checks.append(c)
    print(f"      {'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}", flush=True)

    print("\n[2/6] full offline suite (non-loopback network BLOCKED) ...", flush=True)
    c = check_suite_offline()
    checks.append(c)
    print(f"      {'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}", flush=True)

    print("\n[3/6] D3a.5 regression tests ...", flush=True)
    c = check_d3a5_regression()
    checks.append(c)
    print(f"      {'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}", flush=True)

    print("\n[4/6] D3a.5 mutation drivers ...", flush=True)
    for filename, expected in D3A5_DRIVERS:
        c = check_driver(filename, expected, gate=True)
        checks.append(c)
        print(f"      {'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}", flush=True)

    print("\n[5/6] D3b mutation driver ...", flush=True)
    c = check_driver(D3B_DRIVER, D3B_DRIVER_GUARDS, gate=False)
    checks.append(c)
    print(f"      {'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}", flush=True)

    print("\n[6/6] guardrails ...", flush=True)
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
