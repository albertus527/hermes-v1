"""D4b mutation driver -- Laya's load-bearing guards.

Same discipline as the D4a/D4a.1/D3a.5/D3b drivers: revert ONE guard at a time on
a THROWAWAY COPY and prove the focused D4b tests go red. The working tree is
never edited.

Every mutation is chosen so that, with the guard removed, a SPECIFIC unsafe
behaviour becomes reachable:

   1. the Laya disabled guard is removed        -> retrieval runs when disabled
   2. the OpenViking disabled guard is removed   -> retrieval runs with no adapter
   3. the security fatal refusal is removed      -> a violation becomes partial
   4. the provenance belt-and-suspenders is off  -> an unprovenanced item is carried
   5. the pack size bound is removed             -> an unbounded pack is emitted
   6. the query-count bound is removed           -> unbounded queries are planned
   7. the dedup collapse is removed              -> duplicate references are carried
   8. the intake seam drops the reference block  -> FAST never sees the pack
   9. the renderer always emits a wrapper        -> the disabled prompt changes
  10. the intake fail-open wrapper is removed     -> a Laya error blocks FAST

Run:  python tools/mutation_check_d4b.py
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

LAYA = "app/core/laya_context.py"
INTAKE = "app/core/intake.py"
ADAPTER = "app/hermes/adapter.py"

D4B_TESTS = (
    "tests/test_laya_context.py",
    "tests/test_laya_integration.py",
    "tests/test_laya_composition.py",
)

MUTATIONS = [
    (
        "Laya refuses to run when the feature flag is disabled",
        LAYA,
        """        if not self._config.enabled:
            return self._empty(
                status=STATUS_SKIPPED, project_id=pid, quality=QUALITY_INSUFFICIENT,
                error_reason=ERROR_LAYLA_DISABLED, warnings=(WARNING_LAYLA_DISABLED,),
            )""",
        """        if False:
            return self._empty(
                status=STATUS_SKIPPED, project_id=pid, quality=QUALITY_INSUFFICIENT,
                error_reason=ERROR_LAYLA_DISABLED, warnings=(WARNING_LAYLA_DISABLED,),
            )""",
    ),
    (
        "Laya refuses to run when OpenViking is disabled/unavailable",
        LAYA,
        """        if self._adapter is None or not self._adapter.enabled:
            return self._empty(
                status=STATUS_SKIPPED, project_id=pid, quality=QUALITY_INSUFFICIENT,
                error_reason=ERROR_OPENVIKING_DISABLED,
                warnings=(WARNING_OPENVIKING_DISABLED,),
            )""",
        """        if False:
            return self._empty(
                status=STATUS_SKIPPED, project_id=pid, quality=QUALITY_INSUFFICIENT,
                error_reason=ERROR_OPENVIKING_DISABLED,
                warnings=(WARNING_OPENVIKING_DISABLED,),
            )""",
    ),
    (
        "a security violation refuses the whole pack (not a partial success)",
        LAYA,
        """                if fatal:
                    fatal_error = reason
                    fatal_warning = warning
                    break""",
        """                if False:
                    fatal_error = reason
                    fatal_warning = warning
                    break""",
    ),
    (
        "an item without complete provenance is never carried",
        LAYA,
        """                if not (item.source_id and item.source_revision and item.uri):
                    warnings.append(WARNING_PARTIAL_RETRIEVAL)
                    continue""",
        """                if False:
                    warnings.append(WARNING_PARTIAL_RETRIEVAL)
                    continue""",
    ),
    (
        "the final pack-size bound is enforced",
        LAYA,
        """            if _serialized_chars(payload) <= self._config.max_pack_chars:
                break""",
        """            if True:
                break""",
    ),
    (
        "the query-count bound is enforced by the planner",
        LAYA,
        """        if len(queries) >= cfg.max_queries:
            return""",
        """        if False:
            return""",
    ),
    (
        "duplicate references are collapsed to one entry",
        LAYA,
        """                if item.uri in items_by_uri:
                    deduped = True""",
        """                if False:
                    deduped = True""",
    ),
    (
        "the intake seam hands FAST the Laya reference block",
        INTAKE,
        """                    reference_context=reference_context,""",
        """                    reference_context=None,""",
    ),
    (
        "the renderer emits a wrapper only when there is real context",
        LAYA,
        """    if not items or status not in (STATUS_READY, STATUS_DEGRADED):
        return \"\"""",
        """    if not items or status not in (STATUS_READY, STATUS_DEGRADED):
        return \"placeholder\"""",
    ),
    (
        "a Laya internal failure never blocks the original FAST path",
        INTAKE,
        """        except Exception:
            # Fail open on availability: any Laya/internal failure must leave
            # the original FAST path intact.
            logger.debug(\"Laya context preparation failed; continuing without it\",
                         exc_info=True)
            return None""",
        """        except Exception:
            raise""",
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
    baseline_code, tail = run_tests(ROOT, D4B_TESTS)
    print(f"D4b baseline: exit={baseline_code} {tail}")
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
            print(f"[ANCHOR-MISS] {label} (replacement was a no-op)")
            unproven.append(f"{label}: mutation changed nothing")
            continue

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "tree"
            shutil.copytree(ROOT, work, ignore=IGNORED)
            destination = work / relative
            if not destination.resolve().is_relative_to(work.resolve()):
                print(f"[ABORT]      {label} -- target escapes the temp tree")
                return 1
            destination.write_text(mutated, encoding="utf-8")

            code, tail = run_tests(work, D4B_TESTS)
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

    print(f"all {len(MUTATIONS)} guards killed by the focused D4b tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
