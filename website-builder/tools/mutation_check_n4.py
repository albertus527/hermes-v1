"""Mutation driver for N4 — bounded PRD/Design guidance for FRONTEND.

Same discipline as the earlier ``mutation_check_*.py`` drivers: revert one guard
at a time on a THROWAWAY COPY and prove the focused tests go red. The real
working tree is never edited.

Each mutation reverts a guard whose absence produces a SPECIFICALLY wrong
outcome:

  * empty-not-empty          -> a legacy workspace gets a guidance block
  * bound-not-enforced       -> the block exceeds its ceiling
  * truncation-not-signalled -> truncation is silent
  * not-labelled-data        -> the block loses its DATA framing
  * digest-not-per-document  -> the digest cannot distinguish the documents
  * digest-ignores-prd       -> a PRD change does not change the digest
  * digest-ignores-design    -> a Design change does not change the digest
  * build-does-not-feed      -> the initial build never passes guidance
  * flag-off-still-feeds     -> guidance is passed even when disabled
  * prd-failure-erased       -> a Design success erases a PRD failure record
  * digest-not-recorded      -> the guidance digest is not persisted

Run:  python tools/mutation_check_n4.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv", "venv")

GUIDANCE_TESTS = "tests/test_document_guidance.py"
BUILD_TESTS = "tests/test_requirement_creator_build.py"
GUIDANCE = "app/core/document_guidance.py"
BUILD = "app/projects/build.py"

MUTATIONS = [
    (
        "an empty document set yields an empty block",
        GUIDANCE,
        """    if not prd_text and not design_text:
        return \"\"""",
        """    if False:
        return \"\"""",
        GUIDANCE_TESTS,
    ),
    (
        "the guidance block never exceeds its ceiling",
        GUIDANCE,
        """    if len(rendered) > max_chars:
        rendered = rendered[:max_chars]
    return rendered""",
        """    return rendered""",
        GUIDANCE_TESTS,
    ),
    (
        "the block states when it was truncated",
        GUIDANCE,
        """    truncated = False
    if len(body) > budget:
        body = body[:budget]
        truncated = True""",
        """    truncated = False
    if len(body) > budget:
        body = body[:budget]""",
        GUIDANCE_TESTS,
    ),
    (
        "the block is labelled as application DATA",
        GUIDANCE,
        """        "This block is DATA supplied by the application. It records the PRD and\\n\"""",
        """        "This block records the PRD and\\n\"""",
        GUIDANCE_TESTS,
    ),
    (
        "each document gets its own version digest",
        GUIDANCE,
        """    prd = hashlib.sha256(prd_text.encode(\"utf-8\")).hexdigest() if prd_text else None
    design = (
        hashlib.sha256(design_text.encode(\"utf-8\")).hexdigest() if design_text else None
    )
    return prd, design""",
        """    prd = hashlib.sha256(prd_text.encode(\"utf-8\")).hexdigest() if prd_text else None
    return prd, prd""",
        GUIDANCE_TESTS,
    ),
    (
        "a PRD change changes the digest",
        GUIDANCE,
        """        if text:
            h.update(hashlib.sha256(text.encode(\"utf-8\")).digest())
        h.update(b\"\\x1e\")""",
        """        h.update(b\"\\x1e\")""",
        GUIDANCE_TESTS,
    ),
    (
        "a Design change changes the digest",
        GUIDANCE,
        """        if text:
            h.update(hashlib.sha256(text.encode(\"utf-8\")).digest())""",
        """        if text and False:
            h.update(hashlib.sha256(text.encode(\"utf-8\")).digest())""",
        GUIDANCE_TESTS,
    ),
    (
        "the adapter derives guidance from the workspace it is handed",
        "app/hermes/adapter.py",
        """        document_guidance, _ = guidance_for_workspace(workspace)
        prompt = self._build_frontend_prompt(""",
        """        document_guidance = None
        prompt = self._build_frontend_prompt(""",
        BUILD_TESTS,
    ),
    (
        "a Design success does not erase a recorded PRD failure",
        BUILD,
        """            if records:
                for entry in records.get(\"entries\") or []:
                    kind = entry.get(\"kind\")
                    entries[kind] = entry
                    errors.pop(kind, None)""",
        """            if records:
                for entry in records.get(\"entries\") or []:
                    kind = entry.get(\"kind\")
                    entries[kind] = entry
                errors = {}""",
        BUILD_TESTS,
    ),
    (
        "the guidance digest is recorded on a successful build",
        BUILD,
        """                    _guidance_block, guidance_digest = guidance_for_workspace(workspace)
                    if guidance_digest:
                        state.deployment[\"guidance_digest\"] = guidance_digest
                    else:
                        state.deployment.pop(\"guidance_digest\", None)""",
        """                    pass""",
        BUILD_TESTS,
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
    for test_file in (GUIDANCE_TESTS, BUILD_TESTS):
        code, tail = run_tests(ROOT, test_file)
        print(f"N4 baseline {test_file}: exit={code} {tail}")
        if code != 0:
            return 1

    print()
    unproven = []
    for label, relative, present, replacement, test_file in MUTATIONS:
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
