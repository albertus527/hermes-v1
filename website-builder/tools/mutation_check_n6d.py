"""Mutation driver for N6-D — FRONTEND handoff + design pack activation.

Run:  python tools/mutation_check_n6d.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv", "venv")

TESTS = "tests/test_n6d_frontend_handoff.py"
DOC_GUIDANCE = "app/core/document_guidance.py"
BUILD = "app/projects/build.py"

MUTATIONS = [
    (
        "the pre-build guidance is labelled distinctly from the post-build DNA",
        DOC_GUIDANCE,
        """        sections.append(
            "## Design Guidance (pre-build, application-decided)\\n\\n"
            + guidance_text.strip()
        )""",
        """        sections.append(
            "## Design\\n\\n"
            + guidance_text.strip()
        )""",
    ),
    (
        "the post-build design is labelled distinctly",
        DOC_GUIDANCE,
        """        sections.append("## Design (post-build DNA)\\n\\n" + design_text.strip())""",
        """        sections.append("## Design\\n\\n" + design_text.strip())""",
    ),
    (
        "the digest folds in the pre-build guidance version",
        DOC_GUIDANCE,
        """        ("design_guidance", guidance_text),""",
        """        ("design_guidance_unused", None),""",
    ),
    (
        "the snapshot records the pre-build guidance digest",
        DOC_GUIDANCE,
        """        "design_guidance_digest": guidance.digest if guidance else None,""",
        """        "design_guidance_digest": None,""",
    ),
    (
        "the pack is built only in ACTIVE design mode",
        BUILD,
        """        if not self.decision_engine_config.design_active:
            return None, None""",
        """        if False:
            return None, None""",
    ),
    (
        "a missing hermes_home degrades the pack to None, not a crash",
        BUILD,
        """        hermes_home = getattr(self.hermes_adapter, "hermes_home", None)
        if hermes_home is None:
            return None""",
        """        hermes_home = getattr(self.hermes_adapter, "hermes_home", None)
        if hermes_home is None:
            hermes_home = Path("/nonexistent-hermes-home")""",
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
    print(f"N6-D baseline: exit={code} {tail}")
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
