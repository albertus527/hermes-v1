"""D4b.1 mutation driver -- the benchmark harness's load-bearing guards.

Same discipline as the other drivers: revert ONE guard at a time on a THROWAWAY
COPY and prove the focused D4b.1 tests go red. The working tree is never edited.

The guards protect benchmark INTEGRITY (the properties that stop the benchmark
from over-claiming):

   1. the 50-brief dataset-size guard is removed     -> a shrunken set is accepted
   2. the 25/25 language-split guard is removed       -> a skewed set is accepted
   3. the upstream-version pin check is removed       -> an unpinned package is accepted
   4. the "refuse without isolated env" guard removed -> a stand-in masquerades as upstream
   5. the upstream revision pin is shortened          -> an unpinned revision is accepted
   6. the frozen-thresholds flag is dropped           -> post-hoc thresholds are allowed

Run:  python tools/mutation_check_d4b1.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".git", ".venv", "venv", ".pytest_cache",
)

HARNESS = "tools/benchmark/d4b1_benchmark.py"
DATASET = "tools/benchmark/d4b1_dataset.json"
THRESHOLDS = "tools/benchmark/d4b1_thresholds.json"

D4B1_TESTS = ("tests/test_d4b1_benchmark.py",)

MUTATIONS = [
    (
        "the 50-brief dataset-size guard is enforced",
        HARNESS,
        """    assert len(cases) == 50, f"expected 50 cases, found {len(cases)}"
    assert len(set(ids)) == 50, "duplicate case ids\"""",
        """    assert True, f"expected 50 cases, found {len(cases)}"
    assert True, "duplicate case ids\"""",
    ),
    (
        "the 25/25 language split is enforced",
        HARNESS,
        '    assert langs.count("en") == 25 and langs.count("id") == 25, "language split must be 25/25"',
        '    assert True, "language split must be 25/25"',
    ),
    (
        "the upstream package version pin is checked",
        HARNESS,
        "    if getattr(laya, \"__version__\", None) != UPSTREAM_PACKAGE_VERSION:",
        "    if False:",
    ),
    (
        "candidate C refuses to run without the isolated environment",
        HARNESS,
        "    import laya  # noqa: F401",
        "    return None  # mutation: skip the real import entirely",
    ),
    (
        "the upstream checkpoint revision is a full 40-char pin",
        HARNESS,
        'UPSTREAM_CHECKPOINT_REVISION = "e4e9ddf21a7b1903b7acffd8814ad4307bf63a67"',
        'UPSTREAM_CHECKPOINT_REVISION = "e4e9ddf2"',
    ),
    (
        "the thresholds are declared frozen before testing",
        THRESHOLDS,
        '"frozen_before_testing": true',
        '"frozen_before_testing": false',
    ),
]


def run_tests(cwd, test_files):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *test_files,
         "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(cwd), capture_output=True, text=True,
    )
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    return result.returncode, lines[-1] if lines else result.stderr.strip()[-140:]


def main():
    baseline_code, tail = run_tests(ROOT, D4B1_TESTS)
    print(f"D4b.1 baseline: exit={baseline_code} {tail}")
    if baseline_code != 0:
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
            dest = work / relative
            if not dest.resolve().is_relative_to(work.resolve()):
                print(f"[ABORT]      {label} -- target escapes the temp tree")
                return 1
            dest.write_text(mutated, encoding="utf-8")
            code, tail = run_tests(work, D4B1_TESTS)
            if code == 0:
                print(f"[SURVIVED]   {label} -- tests still pass without this guard")
                unproven.append(f"{label}: tests still pass without this guard")
            elif "failed" in tail or "error" in tail:
                print(f"[KILLED]     {label} -- {tail}")
            else:
                print(f"[INVALID]    {label} -- {tail}")
                unproven.append(f"{label}: mutation produced no test outcome ({tail})")

    print()
    if unproven:
        print(f"{len(unproven)} guard(s) unproven:")
        for item in unproven:
            print("  -", item)
        return 1
    print(f"all {len(MUTATIONS)} guards killed by the focused D4b.1 tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
