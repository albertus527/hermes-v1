"""D4a.1 mutation driver -- the LIVE backend's load-bearing guards.

Same discipline as the D4a/D3a.5/D3b drivers: revert ONE guard at a time on a
THROWAWAY COPY and prove the focused D4a.1 tests go red. The working tree is
never edited.

Every mutation is chosen so that, with the guard removed, a SPECIFIC unsafe
behaviour becomes reachable:

   1. the reader traversal guard is removed      -> a source can escape its root
   2. the reader forbidden-source guard is removed-> a .env can be read
   3. the reader oversize guard is removed        -> an unbounded file is read
   4. the backend provenance drop is removed      -> an unprovenanced match leaks
   5. the write scope check is removed            -> a foreign URI can be written
   6. the write project check is removed          -> a cross-project write lands
   7. the corpus existence filter is removed      -> a missing source gets a spec

Run:  python tools/mutation_check_d4a1.py
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

LIVE = "app/core/openviking_live.py"
CORPUS = "app/core/openviking_corpus.py"

D4A1_TESTS = ("tests/test_openviking_live.py",)

MUTATIONS = [
    (
        "the live reader refuses a path that escapes its root",
        LIVE,
        """        if base != candidate and base not in candidate.parents:
            return None""",
        """        if False:
            return None""",
    ),
    (
        "the live reader refuses a forbidden source before any read",
        LIVE,
        """        if is_forbidden_source(text):
            return None""",
        """        if False:
            return None""",
    ),
    (
        "the live reader refuses an oversize source",
        LIVE,
        """            if candidate.stat().st_size > MAX_SOURCE_BYTES:
                return None""",
        """            if False:
                return None""",
    ),
    (
        "an unprovenanced live match is dropped before the adapter sees it",
        LIVE,
        """            record_dir, record = self._find_record_dir(match.uri)
            if record is None or record_dir is None:
                # Not application-provenanced content (e.g. a server-generated
                # directory node with no record). Never surfaced.
                continue""",
        """            record_dir, record = self._find_record_dir(match.uri)
            if False:
                continue""",
    ),
    (
        "a live write outside the project scope is refused",
        LIVE,
        """        if not is_uri_within_scope(directory, project_root_uri(project_id)):
            raise LiveBackendError("resource uri escapes the project scope")""",
        """        if False:
            raise LiveBackendError("resource uri escapes the project scope")""",
    ),
    (
        "a live write for another project is refused",
        LIVE,
        """        if record.project_id != project_id:
            raise LiveBackendError("record project does not match write scope")""",
        """        if False:
            raise LiveBackendError("record project does not match write scope")""",
    ),
    (
        "the corpus never emits a spec for a missing file",
        CORPUS,
        """        if not corpus_file.exists:
            continue""",
        """        if False:
            continue""",
    ),
    (
        "a paid-VLM write without explicit opt-in is refused",
        LIVE,
        """            if not self._allow_paid_vlm:
                raise LiveBackendError(
                    "paid VLM ingestion is not enabled; call enable_paid_vlm() "
                    "with an approved budget, or use vectors_only"
                )""",
        """            if False:
                raise LiveBackendError(
                    "paid VLM ingestion is not enabled; call enable_paid_vlm() "
                    "with an approved budget, or use vectors_only"
                )""",
    ),
    (
        "the paid-VLM per-run ceiling is enforced",
        LIVE,
        """            if self._paid_vlm_sources >= ceiling:
                raise LiveBackendError(
                    f"paid VLM ingestion ceiling reached ({ceiling} sources this run); "
                    "refusing further paid writes"
                )""",
        """            if False:
                raise LiveBackendError(
                    f"paid VLM ingestion ceiling reached ({ceiling} sources this run); "
                    "refusing further paid writes"
                )""",
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
    baseline_code, tail = run_tests(ROOT, D4A1_TESTS)
    print(f"D4a.1 baseline: exit={baseline_code} {tail}")
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

            code, tail = run_tests(work, D4A1_TESTS)
            if code == 0:
                print(f"[SURVIVED]   {label} -- tests still pass without this guard")
                unproven.append(f"{label}: tests still pass without this guard")
            elif "failed" in tail:
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

    print(f"all {len(MUTATIONS)} guards killed by the focused D4a.1 tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
