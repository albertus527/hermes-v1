"""Mutation driver for N6-E — guidance snapshots and QA/VISION parity.

Run:  python tools/mutation_check_n6e.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv", "venv")

TESTS = "tests/test_guidance_snapshot.py"
SNAP = "app/core/guidance_snapshot.py"
ADAPTER = "app/hermes/adapter.py"

MUTATIONS = [
    (
        "a used document that changed is a true mismatch (fail closed)",
        SNAP,
        """        if stored.digest != recorded:
            return (
                f"{ERR_GUIDANCE_VERSION_MISMATCH}: {kind} document changed since "
                f"invocation {record.get('invocation_id')} "
                f"(used {recorded}, now {stored.digest})"
            )""",
        """        if False:
            return (
                f"{ERR_GUIDANCE_VERSION_MISMATCH}: {kind} document changed since "
                f"invocation {record.get('invocation_id')} "
                f"(used {recorded}, now {stored.digest})"
            )""",
    ),
    (
        "a used document that vanished is a true mismatch",
        SNAP,
        """        if stored is None:
            return (
                f"{ERR_GUIDANCE_VERSION_MISMATCH}: {kind} document was used at "
                f"invocation {record.get('invocation_id')} but is now missing"
            )""",
        """        if False:
            return (
                f"{ERR_GUIDANCE_VERSION_MISMATCH}: {kind} document was used at "
                f"invocation {record.get('invocation_id')} but is now missing"
            )""",
    ),
    (
        "a document absent at invocation is not a mismatch (post-build Design.md)",
        SNAP,
        """        recorded = record.get(key)
        if recorded is None:""",
        """        recorded = record.get(key)
        if False:""",
    ),
    (
        "the newest invocation record is retained (bounded history)",
        SNAP,
        """        if len(history) > MAX_SNAPSHOT_HISTORY:
            history = history[-MAX_SNAPSHOT_HISTORY:]""",
        """        if len(history) > MAX_SNAPSHOT_HISTORY:
            history = history[:MAX_SNAPSHOT_HISTORY]""",
    ),
    (
        "the snapshot is recorded at the FRONTEND invocation",
        ADAPTER,
        """        try:
            from app.core.guidance_snapshot import record_invocation

            record_invocation(""",
        """        try:
            from app.core.guidance_snapshot import record_invocation as _ri

            if False:
                _ri(""",
    ),
    (
        "VISION separates design compliance from specification parity",
        ADAPTER,
        """        guidance_block = ""
        if design_guidance:""",
        """        guidance_block = ""
        if False:""",
    ),
]


def run_tests(cwd, test_file):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", test_file, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=str(cwd), capture_output=True, text=True,
    )
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    return result.returncode, lines[-1] if lines else result.stderr.strip()[-140:]


def main():
    code, tail = run_tests(ROOT, TESTS)
    print(f"N6-E baseline: exit={code} {tail}")
    if code != 0:
        return 1

    print()
    unproven = []
    for label, relative, present, replacement in MUTATIONS:
        target = ROOT / relative
        original = target.read_text(encoding="utf-8")
        if present not in original:
            print(f"[ANCHOR-MISS] {label}")
            unproven.append(f"{label}: anchor not found in {relative}")
            continue
        mutated = original.replace(present, replacement, 1)
        if mutated == original:
            print(f"[ANCHOR-MISS] {label} (no-op)")
            unproven.append(f"{label}: mutation changed nothing")
            continue
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "tree"
            shutil.copytree(ROOT, work, ignore=IGNORED)
            (work / relative).write_text(mutated, encoding="utf-8")
            code, tail = run_tests(work, TESTS)
            if code == 0:
                print(f"[SURVIVED]   {label} -- tests still pass without this guard")
                unproven.append(f"{label}: tests still pass without this guard")
            else:
                print(f"[KILLED]     {label} -- {tail}")

    print()
    if unproven:
        print(f"{len(unproven)} guard(s) unproven:")
        for item in unproven:
            print("  -", item)
        return 1
    print(f"all {len(MUTATIONS)} guards killed by the focused tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
