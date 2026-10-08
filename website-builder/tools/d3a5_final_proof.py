#!/usr/bin/env python3
"""D3a.5 FINAL PROOF -- one command, one verdict.

Runs the whole D3a.5 acceptance battery and prints a consolidated PASS/FAIL:

  1. the DEFAULT test suite, with NON-LOOPBACK network blocked at the Python
     level -- so one run proves both "green" AND "offline";
  2. every mutation driver, each of which must report "all N guards killed";
  3. the batch guardrails: branch ``web-design``, ``feature/website`` untouched,
     and no D3b work.

Exit code 0 iff every check passes. Nothing here mutates the working tree: the
drivers copy the tree to a temp dir, and this script only reads.

Run (from ``website-builder``, with the venv active):

    python tools/d3a5_final_proof.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent

#: The five D3a.5 drivers -- the batch's acceptance gate. Each entry is
#: (driver filename, the exact number of guards it must report killed). The
#: count is pinned so a driver cannot silently LOSE a guard: its own summary is
#: "all N guards killed", which stays true if a mutation is deleted, so an
#: external pin is what makes the claim meaningful.
D3A5_DRIVERS: Tuple[Tuple[str, int], ...] = (
    ("mutation_check_d3a5_parta.py", 16),
    ("mutation_check_d3a5_partbc.py", 73),
    ("mutation_check_d3a5_partc.py", 36),
    ("mutation_check_d3a5_partd.py", 36),
    ("mutation_check_d3a5_parti.py", 18),
)

#: The older D0/D1/D2-D3a drivers. Separate work; reported for the record, not
#: part of the D3a.5 gate (their host-masked kills are a known, documented
#: environmental artifact).
LEGACY_DRIVERS: Tuple[Tuple[str, int], ...] = (
    ("mutation_check_d0.py", 9),
    ("mutation_check_d1.py", 33),
    ("mutation_check_d2_d3a.py", 25),
)

#: The default suite deselects exactly the network-needing integration tests.
EXPECTED_DESELECTED = 4

#: The branch this batch lands on, and the branch it must NOT touch.
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


def _pytest_summary(output: str) -> str:
    lines = [ln for ln in output.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def check_suite_offline() -> Check:
    """The default suite must be GREEN while the internet is unreachable."""
    with tempfile.TemporaryDirectory(prefix="d3a5-proof-netblock-") as tmp:
        (Path(tmp) / "sitecustomize.py").write_text(_NETBLOCK, encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = tmp + os.pathsep + env.get("PYTHONPATH", "")
        proc = _run(
            [sys.executable, "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider"],
            cwd=ROOT, env=env,
        )
    tail = _pytest_summary(proc.stdout)
    ok = proc.returncode == 0 and "failed" not in tail and "error" not in tail
    # The 4 network-needing tests must be deselected, not silently run.
    m = re.search(r"(\d+) deselected", tail)
    deselected = int(m.group(1)) if m else 0
    if ok and deselected != EXPECTED_DESELECTED:
        ok = False
        tail += f"  (expected {EXPECTED_DESELECTED} deselected, saw {deselected})"
    return Check("suite green + offline (default selection)", ok, tail)


def check_driver(filename: str, expected: int, *, gate: bool) -> Check:
    """A driver must report 'all <expected> guards killed'."""
    path = ROOT / "tools" / filename
    proc = _run([sys.executable, str(path)], cwd=ROOT)
    out = proc.stdout + proc.stderr
    tail = _pytest_summary(out) or out.strip().splitlines()[-1] if out.strip() else ""
    m = re.search(r"all (\d+) guards killed", out)
    killed = int(m.group(1)) if m else None
    ok = proc.returncode == 0 and killed == expected
    detail = f"{killed}/{expected} guards killed" if killed is not None else "no summary"
    if not ok:
        detail += f"  | {tail[-160:]}"
    label = ("D3a.5 " if gate else "legacy ") + filename
    return Check(label, ok, detail)


def _git(*args: str) -> str:
    return _run(["git", *args], cwd=REPO).stdout.strip()


def check_guardrails() -> List[Check]:
    checks: List[Check] = []
    branch = _git("branch", "--show-current")
    checks.append(Check(
        f"branch is {WORK_BRANCH}", branch == WORK_BRANCH, branch,
    ))
    # feature/website must be untouched vs its recorded baseline.
    baseline = "868ed00e3f24e06f1dcf9944d6d031105dff0646"
    actual = _git("rev-parse", UNTOUCHED_BRANCH)
    checks.append(Check(
        f"{UNTOUCHED_BRANCH} untouched",
        actual == baseline,
        f"{actual} (baseline {baseline})",
    ))
    # No D3b: no d3b driver / module / doc on this branch.
    d3b = []
    for pattern in ("**/mutation_check_d3b*", "**/design_d3b*", "**/*D3B*"):
        d3b.extend(str(p) for p in REPO.glob(pattern))
    checks.append(Check("no D3b artifacts", not d3b, ", ".join(d3b[:5]) or "none"))
    # The working tree must be clean (nothing uncommitted).
    dirty = _git("status", "--porcelain")
    checks.append(Check("working tree clean", not dirty, dirty[:120] or "clean"))
    return checks


def main() -> int:
    print("=" * 72)
    print("D3a.5 FINAL PROOF")
    print("=" * 72, flush=True)

    checks: List[Check] = []

    print("\n[1/3] default suite, non-loopback network BLOCKED ...", flush=True)
    c = check_suite_offline()
    checks.append(c)
    print(f"      {'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}", flush=True)

    print("\n[2/3] mutation drivers ...", flush=True)
    for filename, expected in D3A5_DRIVERS:
        c = check_driver(filename, expected, gate=True)
        checks.append(c)
        print(f"      {'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}", flush=True)
    for filename, expected in LEGACY_DRIVERS:
        c = check_driver(filename, expected, gate=False)
        checks.append(c)
        print(f"      {'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}", flush=True)

    print("\n[3/3] guardrails ...", flush=True)
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
