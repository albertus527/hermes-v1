"""Mutation driver for D3a.5 Part A (exact pins + companion types).

Same discipline as ``tools/mutation_check_d0.py``, ``mutation_check_d1.py`` and
``mutation_check_d2_d3a.py``: revert one guard at a time on a THROWAWAY COPY and
prove the focused tests go red. The real working tree is never edited.

Every mutation below corresponds to a guard whose absence produces a
SPECIFICALLY wrong outcome, not merely different code:

  * runtime-only-satisfies     -> Three reports installed while `tsc` fails TS7016
  * companion-section-ignored  -> a dev-only declaration satisfies a runtime need
  * companion-version-ignored  -> a near-miss version satisfies an exact pin
  * generic-types-derivation   -> any runtime package acquires an arbitrary
                                  dev dependency without a human adding a row
  * pin-not-exact-accepted     -> a caret/range reaches npm inside a build
  * bare-package-argv          -> an unpinned install whose claim expires tomorrow
  * companion-not-installed    -> the @types/three command is never issued

Run:  python tools/mutation_check_d3a5_parta.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Tuple

ROOT = Path(__file__).resolve().parents[1]
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv", "venv")

PIN_TESTS = "tests/test_design_dependency_pins.py"
CLI_TESTS = "tests/test_design_pinned_cli.py"

INSTALL = "app/core/design_install.py"

# Each entry: (label, relative-path, text that must be present, replacement).
#
# The anchors are multi-line blocks. A one-token anchor matches too many places,
# and when a mutation "fails" because the anchor drifted the driver reports
# ANCHOR-MISS loudly rather than silently proving nothing.
MUTATIONS = [
    # --- 1. installed requires the companion, not just the runtime ---------
    (
        "a runtime-only Three never satisfies the postcondition",
        INSTALL,
        """    specs = required_package_specs(dependency_id)
    if not specs:
        return False
    return all(project_satisfies_spec(project_root, spec) for spec in specs)""",
        """    specs = required_package_specs(dependency_id)
    if not specs:
        return False
    return project_satisfies_spec(project_root, specs[0])""",
    ),
    # --- 2. the exact dependency SECTION is part of the postcondition ------
    (
        "a companion in the wrong dependency section does not satisfy its spec",
        INSTALL,
        """    block = document.get(spec.dependency_section)
    if not isinstance(block, Mapping):
        return False

    declared = block.get(spec.package)
    if not isinstance(declared, str):
        return False
    return declared.strip() == spec.version""",
        """    declared = None
    for section in DEPENDENCY_SECTIONS:
        block = document.get(section)
        if isinstance(block, Mapping) and spec.package in block:
            declared = block[spec.package]
            break
    if not isinstance(declared, str):
        return False
    return declared.strip() == spec.version""",
    ),
    # --- 3. the exact VERSION is part of the postcondition ------------------
    (
        "a near-miss companion version does not satisfy the spec",
        INSTALL,
        """    declared = block.get(spec.package)
    if not isinstance(declared, str):
        return False
    return declared.strip() == spec.version""",
        """    declared = block.get(spec.package)
    if not isinstance(declared, str):
        return False
    return True""",
    ),
    # --- 4. no generic @types/<runtime> derivation -------------------------
    # Deriving the companion from the runtime package name would let any future
    # package acquire an arbitrary dev dependency with no human decision.
    (
        "no companion is derived from the runtime package name",
        INSTALL,
        """    if not isinstance(dependency_id, str):
        return ()
    return DEPENDENCY_COMPANION_PACKAGES.get(dependency_id, ())""",
        """    if not isinstance(dependency_id, str):
        return ()
    declared = DEPENDENCY_COMPANION_PACKAGES.get(dependency_id, ())
    if declared:
        return declared
    package = DEPENDENCY_PACKAGES.get(dependency_id)
    if not package:
        return ()
    return (
        CompanionPackage(
            package=f"@types/{package}",
            version="0.0.0",
            dependency_section=SECTION_DEV_DEPENDENCIES,
        ),
    )""",
    ),
    # --- 5. the resolver never echoes its input ----------------------------
    (
        "an unknown dependency id resolves to no companion",
        INSTALL,
        """    if not isinstance(dependency_id, str):
        return ()
    return DEPENDENCY_COMPANION_PACKAGES.get(dependency_id, ())""",
        """    if not isinstance(dependency_id, str):
        return ()
    return DEPENDENCY_COMPANION_PACKAGES.get(dependency_id, (CompanionPackage(
        package=dependency_id, version="0.0.0",
        dependency_section=SECTION_DEV_DEPENDENCIES,
    ),))""",
    ),
    # --- 6. a non-exact pin is refused before any command ------------------
    (
        "a non-exact pin runs no command at all",
        INSTALL,
        """        runtime_spec = required_package_specs(dependency_id)[0]
        if not runtime_spec.is_exact():""",
        """        runtime_spec = required_package_specs(dependency_id)[0]
        if False:""",
    ),
    # --- 7. the argv carries the exact pinned spec -------------------------
    (
        "the install argv carries an exact pinned spec, not a bare name",
        INSTALL,
        '''    @property
    def spec(self) -> str:
        """The ``name@version`` string handed to the package manager."""
        return f"{self.package}@{self.version}"''',
        '''    @property
    def spec(self) -> str:
        """The ``name@version`` string handed to the package manager."""
        return self.package''',
    ),
    # --- 8. companions are actually installed ------------------------------
    (
        "a dependency with a companion runs its companion install command",
        INSTALL,
        """        for argv in build_companion_install_argv(manager, dependency_id):""",
        """        for argv in ():""",
    ),
# --- 9. a companion install failure is a failure -----------------------
    # This guard is DEFENCE IN DEPTH and is proven as such rather than by a
    # survivor: removing it does not flip the reported state, because the
    # conjunctive postcondition also catches the case (the manifest still lacks
    # the companion, so verification fails). The observable outcome is
    # therefore identical with and without this guard, and mutation cannot
    # demonstrate a difference that does not exist.
    #
    # What this DOES assert is that the fast, honest failure path is taken: a
    # failed companion command stops the sequence immediately rather than
    # running further commands, and reports REASON_INSTALL_FAILED (a command
    # failure) rather than REASON_COMPANION_NOT_VERIFIED (a verification
    # failure). Those are different operational diagnoses of the same outcome,
    # and conflating them is what this mutation would let survive.
    (
        "a failed companion command reports a command failure, not a manifest gap",
        INSTALL,
        """            if companion_process is None or companion_process.returncode != 0:
                receipt = (
                    CommandReceipt.from_process(
                        companion_process, cwd_label="<project>"
                    )
                    if companion_process is not None
                    else receipts[-1]
                )
                return InstallOutcome(
                    dependency_id=dependency_id,
                    state=INSTALL_FAILED,
                    package=package,
                    reason=REASON_INSTALL_FAILED,
                    receipt=receipt,
                )
            receipts.append(
                CommandReceipt.from_process(companion_process, cwd_label="<project>")
            )""",
        """            if False:
                receipt = (
                    CommandReceipt.from_process(
                        companion_process, cwd_label="<project>"
                    )
                    if companion_process is not None
                    else receipts[-1]
                )
                return InstallOutcome(
                    dependency_id=dependency_id,
                    state=INSTALL_FAILED,
                    package=package,
                    reason=REASON_INSTALL_FAILED,
                    receipt=receipt,
                )
            receipts.append(
                CommandReceipt.from_process(companion_process, cwd_label="<project>")
            )""",
    ),
    # --- 10. the dev section is used for companions ------------------------
    (
        "a companion installs into devDependencies, not the runtime set",
        INSTALL,
        '''    if argv and argv[0] == "npm":
        return argv + (
            "install",
            spec.spec,
            "--save-dev",
            "--save-exact",
            "--no-audit",
            "--no-fund",
        )''',
        '''    if argv and argv[0] == "npm":
        return argv + (
            "install",
            spec.spec,
            "--save-exact",
            "--no-audit",
            "--no-fund",
        )''',
    ),
    # ------------------------------------------------------------------
    # Part B: the generic pinned-CLI primitive
    # ------------------------------------------------------------------
    # --- 11. the closed table is the only source of a CLI spec -----------
    (
        "a pinned CLI spec comes only from the closed table",
        INSTALL,
        '''    pinned = PINNED_CLIS.get(cli_id) if isinstance(cli_id, str) else None
    if pinned is None:
        return None''',
        '''    pinned = PINNED_CLIS.get(cli_id) if isinstance(cli_id, str) else None
    if pinned is None:
        return (
            ("npm", "exec", "--yes", f"--package={cli_id}@latest", "--", cli_id)
            if isinstance(cli_id, str)
            else None
        )''',
    ),
    # --- 12. an unknown cli_id never echoes its input --------------------
    (
        "an unknown cli id resolves to no pinned CLI",
        INSTALL,
        """    if not isinstance(cli_id, str):
        return None
    return PINNED_CLIS.get(cli_id)""",
        """    if not isinstance(cli_id, str):
        return None
    return PINNED_CLIS.get(cli_id, PinnedCli(cli_id, "0.0.0", cli_id))""",
    ),
    # --- 13. npm gets `exec`, never the fictional `dlx` -----------------
    (
        "npm is invoked through `exec`, never a bare `dlx`",
        INSTALL,
        '''        return argv + ("exec", "--yes", f"--package={spec}", "--", binary)''',
        '''        return argv + ("dlx", spec)''',
    ),
    # --- 14. yarn Classic fails closed instead of falling back ----------
    (
        "Yarn Classic has no supported runner and refuses to invoke",
        INSTALL,
        '''        if (Path(project_root) / YARN_BERRY_CONFIG).exists():
            return argv + ("dlx", spec)''',
        '''        if True:
            return argv + ("dlx", spec)''',
    ),
    # --- 15. the registry seam cannot take a caller-supplied version ---
    (
        "the registry seam refuses a non-pinned shadcn version",
        INSTALL,
        """    if version != SHADCN_CLI_VERSION:
        logger.error("Refusing a registry invocation with a non-pinned shadcn version.")
        return None""",
        """    if False:
        return None""",
    ),
    # --- 16. impeccable stays out of the pinned-CLI table ---------------
    (
        "impeccable is not a pinned CLI (binary-download shim)",
        INSTALL,
        '''    "transitions_dev": PinnedCli(
        package="transitions-dev", version="0.3.0", binary="transitions-dev"
    ),
}''',
        '''    "transitions_dev": PinnedCli(
        package="transitions-dev", version="0.3.0", binary="transitions-dev"
    ),
    "impeccable": PinnedCli(
        package="impeccable", version="4.1.0", binary="impeccable"
    ),
}''',
    ),
]


def run_tests(cwd, test_files):
    """Run the given focused test file(s) and return (exit_code, summary_line).

    ``test_files`` may be a single path or several; each is passed as its own
    argv element. Passing several paths as ONE space-joined string makes pytest
    treat the whole string as a single path, collect nothing, and exit non-zero
    -- which looks exactly like a successful kill. The caller therefore also
    checks for "no tests ran" and reports INVALID rather than KILLED.
    """
    if isinstance(test_files, str):
        test_files = (test_files,)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *test_files,
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


def tests_for(relative: str) -> Tuple[str, ...]:
    """Which focused test file(s) prove the guards in ``relative``.

    Part A's guards live in the pin tests; Part B's live in the CLI tests. Both
    run for a mutation in ``design_install.py`` because that module now owns
    both concerns, so running only one file would let a Part B mutation appear
    to survive merely because the wrong suite was executed.
    """
    if relative == INSTALL:
        return (PIN_TESTS, CLI_TESTS)
    return (PIN_TESTS,)


def main():
    baseline_code, tail = run_tests(ROOT, PIN_TESTS)
    print(f"Part A baseline: exit={baseline_code} {tail}")
    if baseline_code != 0:
        return 1
    cli_baseline, cli_tail = run_tests(ROOT, CLI_TESTS)
    print(f"Part B baseline: exit={cli_baseline} {cli_tail}")
    if cli_baseline != 0:
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

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "tree"
            shutil.copytree(ROOT, work, ignore=IGNORED)
            (work / relative).write_text(mutated, encoding="utf-8")

            code, tail = run_tests(work, tests_for(relative))
            if code == 0:
                print(f"[SURVIVED]   {label} -- tests still pass without this guard")
                unproven.append(f"{label}: tests still pass without this guard")
            elif "failed" in tail:
                # A real FAILED test is the ONLY outcome that proves a guard is
                # load-bearing. pytest's summary line is the discriminator.
                print(f"[KILLED]     {label} -- {tail}")
            else:
                # No test produced a pass/fail outcome: a collection error
                # (pytest prints "1 error in" SINGULAR or "N errors in" plural)
                # or "no tests ran". The mutation proved NOTHING, so it must not
                # count as a kill.
                print(f"[INVALID]    {label} -- {tail}")
                unproven.append(f"{label}: mutation produced no test outcome ({tail})")

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
