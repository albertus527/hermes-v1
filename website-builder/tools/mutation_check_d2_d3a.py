"""Mutation driver for D2 (selection) + D3a (bounded execution).

Same discipline as tools/mutation_check_d0.py and mutation_check_d1.py: revert one
guard at a time on a THROWAWAY COPY and prove the focused tests go red. The real
working tree is never edited.

Every mutation below corresponds to a guard whose absence produces a SPECIFICALLY
wrong outcome, not merely different code. Each of these is a real vulnerability
class rather than a style rule:

  * installed-without-verification    -> a build claims a package it never added
  * forbid-not-honoured               -> "use no libraries" is silently ignored
  * allowlist-returns-input           -> arbitrary npm install becomes reachable
  * unselected-runs-command           -> a plan installs what it did not select
  * global-install-flag               -> a project install escapes its workspace
  * 3d-veto-overridable               -> decorative depth selects Three.js
  * shadcn-unpinned                   -> production resolves a floating latest
  * selection-can-reach-installed     -> D2 claims an install it never performed

Run:  python tools/mutation_check_d2_d3a.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv", "venv")

SELECTION_TESTS = "tests/test_design_selection.py"
INSTALL_TESTS = "tests/test_design_install.py"

SELECTION = "app/core/design_selection.py"
INSTALL = "app/core/design_install.py"
CONTEXT = "app/core/design_context.py"
POLICIES = "app/core/design_policies.py"
RETRIEVAL = "app/core/design_retrieval.py"

# Each entry: (label, relative-path, text that must be present, replacement).
#
# The anchors are LARGE multi-line blocks. A one-token anchor matches too many
# places, and when a mutation "fails" because the anchor drifted the driver
# reports ANCHOR-MISS loudly rather than silently proving nothing.
MUTATIONS = [
    # --- 1. installed requires manifest verification -----------------------
    (
        "installed requires an observed manifest change",
        INSTALL,
        """        verified = project_declares_dependency(self.project_root, package)
        receipt = CommandReceipt.from_process(process, cwd_label="<project>")
        if not verified:""",
        """        verified = True
        receipt = CommandReceipt.from_process(process, cwd_label="<project>")
        if False:""",
    ),
    # --- 2. A failed command never reports installed ----------------------
    (
        "a failing command never reports installed",
        INSTALL,
        """        if process is None or process.returncode != 0:
            receipt = (
                CommandReceipt.from_process(process, cwd_label="<project>")
                if process is not None
                else None
            )
            return InstallOutcome(
                dependency_id=dependency_id,
                state=INSTALL_FAILED,""",
        """        if False:
            receipt = (
                CommandReceipt.from_process(process, cwd_label="<project>")
                if process is not None
                else None
            )
            return InstallOutcome(
                dependency_id=dependency_id,
                state=INSTALL_FAILED,""",
    ),
    # --- 3. The allowlist never echoes its input --------------------------
    # Returning the input is precisely what makes `npm install <arbitrary>`
    # reachable from a string that came out of resource text.
    (
        "an unknown dependency id resolves to no package",
        INSTALL,
        """    if not isinstance(dependency_id, str):
        return None
    return DEPENDENCY_PACKAGES.get(dependency_id)""",
        """    if not isinstance(dependency_id, str):
        return None
    return DEPENDENCY_PACKAGES.get(dependency_id, dependency_id)""",
    ),
    # --- 4. An unallowlisted id runs no command --------------------------
    (
        "an unallowlisted dependency never reaches a shell",
        INSTALL,
        """        package = resolve_package(dependency_id)
        if package is None:
            return InstallOutcome(
                dependency_id=dependency_id,
                state=INSTALL_FAILED,""",
        """        package = resolve_package(dependency_id) or dependency_id
        if package is None:
            return InstallOutcome(
                dependency_id=dependency_id,
                state=INSTALL_FAILED,""",
    ),
    # --- 5. An unselected dependency runs nothing ------------------------
    (
        "an unselected dependency produces no command",
        INSTALL,
        """        for dependency_id in allowlisted_dependencies():
            if dependency_id not in selected_ids:""",
        """        for dependency_id in allowlisted_dependencies():
            if False:""",
    ),
    # --- 6. No global install flag ---------------------------------------
    (
        "no global install flag is ever added",
        INSTALL,
        """    if argv and argv[0] == "npm":
        return argv + ("install", package, "--save-exact", "--no-audit", "--no-fund")""",
        """    if argv and argv[0] == "npm":
        return argv + ("install", "--global", package, "--save-exact")""",
    ),
    # --- 7. shadcn is pinned ---------------------------------------------
    # A floating `latest` inside a production build is a silent supply-chain
    # upgrade.
    (
        "the registry CLI is pinned, never a floating latest",
        INSTALL,
        """        + ("dlx", f"shadcn@{version}", "add")""",
        """        + ("dlx", "shadcn@latest", "add")""",
    ),
    # --- 8. Only allowlisted components reach the CLI -------------------
    (
        "only allowlisted components reach the registry CLI",
        INSTALL,
        """        if component in ALLOWED_SHADCN_COMPONENTS:
            allowed.append(component)""",
        """        if True:
            allowed.append(component)""",
    ),
    # --- 9. No components => no invocation ------------------------------
    (
        "zero requested components runs zero commands",
        INSTALL,
        """        if not components:
            return (""",
        """        if False:
            return (""",
    ),
    # --- 10. Commands stay inside the project ---------------------------
    (
        "every command cwd is containment-checked",
        INSTALL,
        """        cwd = self._assert_inside_project(".")
        if cwd is None:
            return None, False""",
        """        cwd = self.project_root
        if False:
            return None, False""",
    ),
    # --- 11. D2 can never reach installed -------------------------------
    (
        "D2 selection cannot reach the installed state",
        SELECTION,
        """    @property
    def is_installed(self) -> bool:
        return False""",
        """    @property
    def is_installed(self) -> bool:
        return self.state == "installed\"""",
    ),
    # --- 12. A blanket forbid is not overridable ------------------------
    (
        "a blanket user forbid is not overridden by a reservation",
        SELECTION,
        """        reserved = (
            None
            if forbidden
            else _reserved_by_user(requirements, user, resource_id)
        )""",
        """        reserved = _reserved_by_user(requirements, user, resource_id)""",
    ),
    # --- 13. A 3D counter-signal is an absolute veto --------------------
    # Reverting this lets `decorative depth only, no real 3d scene` select
    # Three.js on the very phrase it is negating.
    (
        "a depth counter-signal vetoes Three.js absolutely",
        SELECTION,
        """    depth_vetoed = _has_any(layout, _3D_NEGATIVE) or _has_any(body, _3D_NEGATIVE)""",
        """    depth_vetoed = False""",
    ),
    # --- 14. An ordinary transition never selects GSAP ------------------
    (
        "ordinary transitions do not satisfy the GSAP gate",
        SELECTION,
        """_TIMELINE_POSITIVE = (
    "timeline",
    "sequenced",""",
        """_TIMELINE_POSITIVE = (
    "timeline",
    "transition",
    "hover",
    "fade",
    "slide",
    "sequenced",""",
    ),
    # --- 15. Smooth scroll is never default-on --------------------------
    (
        "smooth scroll is never selected by default",
        SELECTION,
        """_SMOOTH_SCROLL_POSITIVE = (
    "smooth scroll",""",
        """_SMOOTH_SCROLL_POSITIVE = (
    "scroll",
    "smooth scroll",""",
    ),
    # --- 16. An unavailable reference degrades honestly ------------------
    (
        "an absent reference is rejected with an honest reason",
        SELECTION,
        """    if not _is_reachable(capability_report, resource_id):
        return _RuleOutcome(
            False,
            reason,
            detail=WARNING_REFERENCE_NO_INTEGRATION,
        )""",
        """    if False:
        return _RuleOutcome(
            False,
            reason,
            detail=WARNING_REFERENCE_NO_INTEGRATION,
        )""",
    ),
    # --- 17. The critic is never selected into a build ------------------
    (
        "the critic resource is never selected into a build",
        SELECTION,
        """        if resource_id == CRITIC_RESOURCE:""",
        """        if False:""",
    ),
    # --- 18. The identity projection stays scoped ------------------------
    (
        "the identity projection is scoped to ui_ux_pro_max",
        RETRIEVAL,
        '''    "ui_ux_pro_max": ResourceIdentityProjection(
        title_column=UI_UX_PRO_MAX_TITLE_COLUMN,
        id_column=UI_UX_PRO_MAX_ID_COLUMN,
    ),''',
        '''    "ui_ux_pro_max": ResourceIdentityProjection(
        title_column=UI_UX_PRO_MAX_TITLE_COLUMN,
        id_column=UI_UX_PRO_MAX_ID_COLUMN,
    ),
    ResourceIdentityProjection(title_column=UI_UX_PRO_MAX_TITLE_COLUMN),''',
    ),
    # --- 19. The pack drops rejections before selections -----------------
    # Losing a rejection is inconvenient; losing a selection fails unsafely.
    (
        "the pack keeps selections when the decision section is truncated",
        CONTEXT,
        """    shrunk["rejected_resources"] = []
    shrunk["dependency_decisions"] = [
        d for d in decisions["dependency_decisions"] if d["state"] == "selected"
    ]""",
        """    shrunk["rejected_resources"] = decisions["rejected_resources"]
    shrunk["dependency_decisions"] = []""",
    ),
    # --- 20. Retrieval never widens the decision set --------------------
    (
        "retrieval runs only for resources that were selected",
        CONTEXT,
        """    if retrieve and plan.selected_ids:""",
        """    if retrieve:""",
    ),
    # --- 21. STATE_SELECTED is a real, distinct rung ---------------------
    (
        "the dependency ladder keeps selected distinct from installed",
        POLICIES,
        '''STATE_SELECTED = "selected"''',
        '''STATE_SELECTED = "installed"''',
    ),
]


def run_tests(cwd, test_file):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            test_file,
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    return result.returncode, lines[-1] if lines else result.stderr.strip()[-140:]


def main():
    baseline_code, tail = run_tests(ROOT, SELECTION_TESTS)
    print(f"D2 baseline: exit={baseline_code} {tail}")
    if baseline_code != 0:
        return 1
    install_code, tail = run_tests(ROOT, INSTALL_TESTS)
    print(f"D3a baseline: exit={install_code} {tail}")
    if install_code != 0:
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
            print(f"[ANCHOR-MISS] {label} (replacement was a no-op)")
            unproven.append(f"{label}: mutation changed nothing")
            continue

        test_file = INSTALL_TESTS if relative == INSTALL else SELECTION_TESTS
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "tree"
            shutil.copytree(ROOT, work, ignore=IGNORED)
            (work / relative).write_text(mutated, encoding="utf-8")

            code, tail = run_tests(work, test_file)
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