"""Mutation driver for the D0.1-D0.2 design capability layer.

Reverts one guard at a time on a THROWAWAY COPY of the tree and proves the
focused tests go red. Mirrors tools/mutation_check.py's discipline: the real
working tree is never edited.

Each mutation below corresponds to a guard whose absence would produce a
SPECIFICALLY wrong outcome, not merely different code:

  duplicate-key detection  -> a manifest contradicting itself loads and applies
                              whichever value came last, on the exact field
                              that decides whether a missing capability is fatal
  required->failure        -> a missing REQUIRED capability passes preflight
  on-demand resting state  -> every registry/npm resource degrades forever
  data_entries containment -> capability data can be read from outside the skill
  traversal rejection      -> a manifest can escape the skill root by string
  unknown-kind rejection   -> a typo'd kind falls through to default behaviour
  SKILL.md reality         -> a placeholder directory passes as a capability
  data-entry existence     -> the data check silently becomes vacuous
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv", "venv")
TEST_FILE = "tests/test_design_resources.py"

MUTATIONS = [
    (
        "duplicate-key detection",
        "app/core/design_resources.py",
        """        if key in mapping:
            raise DesignResourceManifestError(
                f"duplicate key in design resource manifest: {key!r}"
            )
""",
        "",
    ),
    (
        "on-demand resting state keyed off install_mode",
        "app/core/design_resources.py",
        '''        return self.install_mode == "project_on_demand"''',
        '''        return self.kind == "npm_optional"''',
    ),
    (
        "required resources fail preflight",
        "app/core/design_capabilities.py",
        """        elif capability.status == STATUS_UNAVAILABLE_OPTIONAL:
            degraded.append(resource_id)""",
        """        else:
            failures.append(resource_id)""",
    ),
    (
        "data_entries containment (resolved half)",
        "app/core/design_capabilities.py",
        """        if not entry_is_contained(skill_dir, entry_path):
            return False, DETAIL_DATA_ENTRY_ESCAPES""",
        "",
    ),
    (
        "data_entries traversal rejection (syntactic half)",
        "app/core/design_resources.py",
        '''    if ".." in parts:
        raise DesignResourceManifestError(
            f"data_entries entry must not traverse outside the skill root: {entry!r}"
        )
''',
        "",
    ),
    (
        "unknown kind rejected",
        "app/core/design_resources.py",
        """    if kind not in RESOURCE_KINDS:""",
        """    if False:""",
    ),
    (
        "SKILL.md must be a real non-empty file",
        "app/core/design_capabilities.py",
        """    if not _is_readable_file(skill_dir / "SKILL.md"):""",
        """    if not (skill_dir / "SKILL.md").exists():""",
    ),
    (
        "declared data entry must exist",
        "app/core/design_capabilities.py",
        """        if not entry_path.is_file():
            return False, DETAIL_DATA_ENTRY_MISSING""",
        "",
    ),
    (
        "non-relative data entries rejected",
        "app/core/design_resources.py",
        """    if normalized.startswith(_FORBIDDEN_ENTRY_PREFIXES):""",
        """    if False:""",
    ),
]


def run_tests(cwd):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", TEST_FILE, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    return result.returncode, lines[-1] if lines else result.stderr.strip()[-140:]


def main() -> int:
    code, baseline = run_tests(ROOT)
    print(f"baseline (unmutated): exit={code} {baseline}")
    if code != 0:
        print("baseline is not green; fix that before reading mutation results")
        return 1

    unproven = []
    for label, relative, present, replacement in MUTATIONS:
        target = ROOT / relative
        original = target.read_text(encoding="utf-8")
        if present not in original:
            print(f"[ANCHOR-MISS] {label}")
            unproven.append(f"{label}: anchor not found in {relative}")
            continue

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "tree"
            shutil.copytree(ROOT, work, ignore=IGNORED)
            (work / relative).write_text(
                original.replace(present, replacement, 1), encoding="utf-8"
            )
            code, tail = run_tests(work)

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