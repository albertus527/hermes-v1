"""Mutation driver for D3a.5 Part I (the Impeccable critic seam).

Same discipline as the other D3a.5 drivers: revert one guard at a time on a
THROWAWAY COPY and prove the focused tests go red. The working tree is never
edited.

This driver exists as its own file because the critic seam has one class of
failure that matters more than all the others:

    **a broken critic that reports a clean scan.**

A scan failure that is collapsed into "no findings" lets a broken engine
silently certify a design. Every mutation below is chosen so that, with the
guard removed, that specific lie becomes reachable -- not merely "some assert
fails".

  * exit-1-is-clean          -> a failed scan certifies a design
  * unparseable-is-clean     -> a garbage payload certifies a design
  * wrong-shape-is-clean     -> an unexpected payload certifies a design
  * findings-exit-no-output  -> an empty body under exit 2 is read as clean
  * missing-engine-clean     -> an unprovisioned host reports a pass
  * an-unsigned-finding      -> anonymous JSON becomes a claim in the prompt
  * severity-coerced         -> upstream's unknown urgency becomes ours
  * output-unbounded         -> an unbounded finding list flows into a prompt
  * truncation-not-reported  -> a silent cut claims completeness
  * engine-outside-skill     -> an uncontained path is executed
  * argv-grows-a-flag        -> a caller-supplied fragment reaches the engine
  * shell-enabled            -> the engine path becomes a shell word

Run:  python tools/mutation_check_d3a5_parti.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv", "venv")

CRITIC_TESTS = "tests/test_design_critic.py"
ACTIVATION_TESTS = "tests/test_design_activation.py"
CRITIC = "app/core/design_critic.py"
ACTIVATION = "app/core/design_activation.py"

MUTATIONS = [
    # --- 1. a scan failure must never be a clean report ------------------
    (
        "a scan failure is never a clean report",
        CRITIC,
        """    if returncode not in (EXIT_CLEAN, EXIT_FINDINGS):
        return CriticOutcome(ok=False, reasons=("scan_failed",))""",
        """    if returncode not in (EXIT_CLEAN, EXIT_FINDINGS):
        return CriticOutcome(ok=True)""",
    ),
    # --- 2. unparseable output must never be a clean report -------------
    (
        "unparseable output is never a clean report",
        CRITIC,
        """    except (ValueError, TypeError):
        return CriticOutcome(ok=False, reasons=("output_unparseable",))""",
        """    except (ValueError, TypeError):
        return CriticOutcome(ok=True)""",
    ),
    # --- 3. an unexpected shape must never be a clean report -----------
    (
        "an unexpected payload shape is never a clean report",
        CRITIC,
        """    if documents is None:
        return CriticOutcome(ok=False, reasons=("output_unexpected",))""",
        """    if documents is None:
        documents = []""",
    ),
    # --- 4. exit 2 with an empty body is a failure, not a clean scan ----
    (
        "findings-exit with empty output is not clean",
        CRITIC,
        """        if returncode == EXIT_CLEAN:
            return CriticOutcome(ok=True)
        return CriticOutcome(ok=False, reasons=("output_empty",))""",
        """        return CriticOutcome(ok=True)""",
    ),
    # --- 5. a missing engine or interpreter is an absence, not a pass ---
    (
        "an unresolved engine is reported, never a pass",
        CRITIC,
        """    if engine_path is None or not isinstance(node_executable, str) or not node_executable.strip():
        return CriticOutcome(ok=False, reasons=("engine_unavailable",))""",
        """    if engine_path is None or not isinstance(node_executable, str) or not node_executable.strip():
        return CriticOutcome(ok=True)""",
    ),
    # --- 6. a finding without identity must be dropped ------------------
        # Coercing anonymous JSON would put a claim into the prompt that the
        # engine never made.
    (
        "a finding with no identity or text is dropped",
        CRITIC,
        """    if not finding or not rule_id:
        return None""",
        """    if not finding and not rule_id:
        return None""",
    ),
    # --- 7. an unknown severity must not be rewritten as one of ours ---
    (
        "an unknown severity is preserved, not coerced",
        CRITIC,
        """    text = _bound(value).lower()
    return text if text in _SEVERITIES else _bound(value).lower()""",
        """    text = _bound(value).lower()
    return text if text in _SEVERITIES else "note\"""",
    ),
    # --- 8. the finding count is bounded --------------------------------
    (
        "the finding count is bounded",
        CRITIC,
        """    truncated = len(findings) > MAX_FINDINGS
    if truncated:
        findings = findings[:MAX_FINDINGS]""",
        """    truncated = False""",
    ),
    # --- 9. truncation is reported, never silent ------------------------
    (
        "truncation is reported rather than silent",
        CRITIC,
        """    reasons: List[str] = ["truncated"] if truncated else []""",
        """    reasons: List[str] = []""",
    ),
    # --- 10. field text is bounded ---------------------------------------
    (
        "upstream finding text is bounded",
        CRITIC,
        """    return text.strip()[:limit]""",
        """    return text.strip()""",
    ),
    # --- 11. an engine outside the skill root is refused ----------------
    #
    # The resolver now lives ONCE in design_activation, which encodes the
    # VERIFIED cross-platform Node entrypoint chain; design_critic re-exports
    # it. Two copies would drift, and the drifted copy is what previously
    # reported a correctly provisioned skill as unavailable.
    (
        "an engine resolving outside the skill is refused",
        ACTIVATION,
        """        try:
            # Containment on the RESOLVED path: a symlink inside the skill
            # pointing outside it is refused before anything executes.
            Path(path).resolve().relative_to(resolved_root)
        except (OSError, ValueError, RuntimeError):
            return None""",
        """        try:
            # Containment on the RESOLVED path: a symlink inside the skill
            # pointing outside it is refused before anything executes.
            pass
        except (OSError, ValueError, RuntimeError):
            return None""",
    ),
    # --- 12. the engine must be a real, non-empty file ------------------
    (
        "a non-file or empty engine path is refused",
        ACTIVATION,
        """        if not _is_readable_file(path):
            return None""",
        """        if not path.exists():
            return None""",
    ),
    # --- 13. the argv is fixed and takes no caller fragment ------------
    (
        "the critic argv takes no caller-supplied fragment",
        CRITIC,
        """    return (
        str(node_executable),
        str(engine_path),
    ) + CRITIC_ENGINE_ARGV_SUFFIX""",
        """    return (
        str(node_executable),
        str(engine_path),
    ) + CRITIC_ENGINE_ARGV_SUFFIX + (FIX,)""",
    ),
    # --- 13b. the scan target is fixed and load-bearing ----------------
    # Without the target the engine reads STDIN on a non-TTY and returns a clean
    # verdict over a project it never opened. Dropping the "." must fail.
    (
        "the scan target is fixed so the scan is deterministic",
        CRITIC,
        '''CRITIC_ENGINE_ARGV_SUFFIX: Tuple[str, ...] = ("detect", "--json", "--quiet", ".")''',
        '''CRITIC_ENGINE_ARGV_SUFFIX: Tuple[str, ...] = ("detect", "--json", "--quiet")''',
    ),
    # --- 14. the engine is never a shell word ---------------------------
    (
        "the engine is never run through a shell",
        CRITIC,
        """            timeout=timeout_seconds,
            shell=False,
        )""",
        """            timeout=timeout_seconds,
            shell=True,
        )""",
    ),
    # --- 15. a timeout is a failure, not a pass -------------------------
    (
        "a timeout is reported as a scan failure",
        CRITIC,
        """    except subprocess.TimeoutExpired:
        return CriticOutcome(ok=False, reasons=("scan_failed",))""",
        """    except subprocess.TimeoutExpired:
        return CriticOutcome(ok=True)""",
    ),
    # --- 16. a failure to execute is reported ---------------------------
    (
        "an unexecutable engine is reported",
        CRITIC,
        """    except (OSError, ValueError) as error:
        logger.warning("Impeccable critic could not be executed: %s", type(error).__name__)
        return CriticOutcome(ok=False, reasons=("engine_not_executable",))""",
        """    except (OSError, ValueError):
        return CriticOutcome(ok=True)""",
    ),
    # --- 17. the reason vocabulary stays closed -------------------------
    (
        "the critic reason vocabulary is closed",
        CRITIC,
        """        for reason in self.reasons:
            if reason not in CRITIC_REASONS:
                raise ValueError(f"unknown critic reason: {reason!r}")""",
        """        return""",
    ),
]


def run_tests(cwd, test_files):
    if isinstance(test_files, str):
        test_files = (test_files,)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *test_files,
         "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(cwd), capture_output=True, text=True,
    )
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    return result.returncode, lines[-1] if lines else result.stderr.strip()[-140:]


def main():
    baseline_code, tail = run_tests(ROOT, CRITIC_TESTS)
    print(f"Part I baseline: exit={baseline_code} {tail}")
    if baseline_code != 0:
        return 1

    print()
    unproven = []
    for label, relative, present, replacement in MUTATIONS:
        target = ROOT / relative
        original = target.read_text(encoding="utf-8")
        # The engine resolver moved into design_activation, but its guards are
        # exercised from BOTH test files (the critic tests cover the empty-file
        # and symlink-escape cases directly). Run both so a strong guard is never
        # reported as "surviving" for want of a test that happens to live in the
        # other file.
        tests = CRITIC_TESTS
        if str(relative).endswith("design_activation.py"):
            # A TUPLE, not a joined string: run_tests passes these straight to
            # pytest, and a single "a b" argument would be read as one filename.
            tests = (CRITIC_TESTS, ACTIVATION_TESTS)

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
            destination = work / relative
            # Guard: the mutation MUST land in the temp copy. An absolute
            # `relative` would resolve outside `work` and silently edit the
            # real source tree, leaving mutated code behind for the next run.
            if not destination.resolve().is_relative_to(work.resolve()):
                print(f"[ABORT]      {label} -- target escapes the temp tree")
                return 1
            destination.write_text(mutated, encoding="utf-8")

            code, tail = run_tests(work, tests)
            if code == 0:
                print(f"[SURVIVED]   {label} -- tests still pass without this guard")
                unproven.append(f"{label}: tests still pass without this guard")
            elif "failed" in tail:
                # A real FAILED test is the ONLY outcome that proves a guard is
                # load-bearing. pytest's summary line is the discriminator.
                print(f"[KILLED]     {label} -- {tail}")
            else:
                # No test produced a pass/fail outcome: a collection error
                # ("1 error in" SINGULAR or "N errors in" plural) or "no tests
                # ran". The mutation proved NOTHING, so it must not count.
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