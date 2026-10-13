"""Mutation driver for N6-B — Decision Engine intake modes.

Same throwaway-copy discipline as the earlier ``mutation_check_*.py`` drivers:
revert one guard at a time on a COPY and prove the focused tests go red. A
guard that survives is not load-bearing and must be strengthened.

Run:  python tools/mutation_check_n6b.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv", "venv")

TESTS = "tests/test_decision_intake_modes.py"
INTAKE = "app/core/intake.py"

MUTATIONS = [
    (
        "disabled mode never consults the engine",
        INTAKE,
        """        if (
            self.decision_engine is not None
            and self.decision_engine_config.intake_enabled
        ):""",
        """        if self.decision_engine is not None:""",
    ),
    (
        "shadow mode records the decision without changing the result",
        INTAKE,
        """                if self.decision_engine_config.intake_active:
                    # ACTIVE: expose the classification as an ADVISORY routing
                    # hint only. The IntakeResult below is still built from FAST.
                    self.last_intake_routing_hint = {""",
        """                if True:
                    # ACTIVE: expose the classification as an ADVISORY routing
                    # hint only. The IntakeResult below is still built from FAST.
                    self.last_intake_routing_hint = {""",
    ),
    (
        "active mode sets a routing hint",
        INTAKE,
        """                    self.last_intake_routing_hint = {
                        "kind": decision.kind,""",
        """                    self.last_intake_routing_hint = None
                    _unused_hint = {
                        "kind": decision.kind,""",
    ),
    (
        "a provider failure never breaks intake",
        INTAKE,
        """            except Exception:  # pragma: no cover - a shadow/active engine must never break intake
                decision = None""",
        """            except Exception:  # pragma: no cover
                raise""",
    ),
    (
        "an injected engine with no config defaults to shadow (N3 contract)",
        INTAKE,
        """        elif decision_engine is not None:
            self.decision_engine_config = DecisionEngineConfig(
                intake_mode=DecisionEngineMode.SHADOW
            )""",
        """        elif decision_engine is not None:
            self.decision_engine_config = DecisionEngineConfig(
                intake_mode=DecisionEngineMode.DISABLED
            )""",
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
    print(f"N6-B baseline: exit={code} {tail}")
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
