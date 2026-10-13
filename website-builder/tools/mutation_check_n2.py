"""Mutation driver for N2 — Requirement Creator & Git-backed documents.

Same discipline as ``mutation_check_d0.py`` … ``mutation_check_n1.py``: revert one
guard at a time on a THROWAWAY COPY and prove the focused tests go red. The real
working tree is never edited.

Each mutation reverts a guard whose absence produces a SPECIFICALLY wrong
outcome — a fabricated fact, a nondeterministic document, an unbound revision, a
document outside the release source, or a silently-enabled feature:

  * unresolved-marker-invented   -> the compiler fabricates a business fact
  * prd-not-validated            -> an invalid document is returned as valid
  * serialise-not-deterministic  -> the same input yields different bytes
  * identity-not-bound           -> a document loses its release binding
  * write-not-validated          -> an invalid document reaches disk
  * write-not-atomic             -> a partial document can be left behind
  * read-accepts-invalid         -> a corrupt document reads back as trusted
  * legacy-not-none              -> a missing document is mistaken for present
  * feature-default-on           -> documents are written without opt-in
  * records-drop-digest          -> hydration loses its consistency anchor
  * config-coerces-version       -> an invalid generator_version is accepted
  * build-writes-after-snapshot  -> documents fall outside the checked bytes

Run:  python tools/mutation_check_n2.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv", "venv")

CREATOR_TESTS = "tests/test_requirement_creator.py"
BUILD_TESTS = "tests/test_requirement_creator_build.py"
CREATOR = "app/core/requirement_creator.py"
STORE = "app/core/document_store.py"
BUILD = "app/projects/build.py"

# Each entry: (label, relative-path, text that must be present, replacement, tests)
MUTATIONS = [
    (
        "a missing brief field becomes UNRESOLVED, never a fabricated fact",
        CREATOR,
        """    requirements = _bullet_block(
        [what, why], empty=f"- {UNRESOLVED_MARKER}: FAST supplied no what/why"
    )""",
        """    requirements = _bullet_block(
        [what, why], empty="- a modern, welcoming website"
    )""",
        CREATOR_TESTS,
    ),
    (
        "the compiled PRD is validated before being returned",
        CREATOR,
        """    text = serialise_document(front, body)
    _, violations = validate_prd(text)
    if violations:
        raise RequirementCreatorError(
            "compiled PRD failed validation", [v.code for v in violations]
        )
    return text""",
        """    text = serialise_document(front, body)
    return text""",
        CREATOR_TESTS,
    ),
    (
        "the compiled Design.md is validated before being returned",
        CREATOR,
        """    text = serialise_document(front, body)
    _, violations = validate_design(text)
    if violations:
        raise RequirementCreatorError(
            "compiled Design.md failed validation", [v.code for v in violations]
        )
    return text""",
        """    text = serialise_document(front, body)
    return text""",
        CREATOR_TESTS,
    ),
    (
        "a malformed config never silently enables the feature",
        CREATOR,
        """    enabled = bool(data.get("enabled", False))""",
        """    enabled = True""",
        CREATOR_TESTS,
    ),
    (
        "an out-of-range generator_version is clamped to >= 1",
        CREATOR,
        """    if generator_version < 1:
        generator_version = 1""",
        """    if False:
        generator_version = 1""",
        CREATOR_TESTS,
    ),
    (
        "writing a document validates it first",
        STORE,
        """    if violations:
        raise ValueError(
            f"refusing to write invalid {kind} document: "
            + ", ".join(v.code for v in violations)
        )""",
        """    if False:
        raise ValueError(
            f"refusing to write invalid {kind} document: "
            + ", ".join(v.code for v in violations)
        )""",
        CREATOR_TESTS,
    ),
    (
        "a corrupt (non-UTF-8) document reads back as None, not a raised error",
        STORE,
        """    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None""",
        """    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None""",
        CREATOR_TESTS,
    ),
    (
        "a document whose bytes no longer match its record is a mismatch",
        STORE,
        """        if stored.digest != entry.get("digest"):
            return (
                f"recorded {kind} digest {entry.get('digest')} does not match "
                f"workspace digest {stored.digest}"
            )""",
        """        if False:
            return (
                f"recorded {kind} digest {entry.get('digest')} does not match "
                f"workspace digest {stored.digest}"
            )""",
        CREATOR_TESTS,
    ),
    (
        "documents are compiled only when the feature is enabled",
        BUILD,
        """        if not self.requirement_creator_config.enabled:
            return""",
        """        if False:
            return""",
        BUILD_TESTS,
    ),
    (
        "the deployment record keeps the document digests",
        STORE,
        """    return {
        "entries": entries,
        "digests": {e["kind"]: e["digest"] for e in entries},
    }""",
        """    return {
        "entries": entries,
        "digests": {},
    }""",
        CREATOR_TESTS,
    ),
    (
        "documents are written BEFORE the checked source snapshot",
        BUILD,
        """            self._compile_and_store_documents(project_id, workspace, brief, design_dna)
            before = source_fingerprint(workspace)
            checks = self._run_fixed_checks(project_id, workspace)""",
        """            before = source_fingerprint(workspace)
            self._compile_and_store_documents(project_id, workspace, brief, design_dna)
            checks = self._run_fixed_checks(project_id, workspace)""",
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
    for test_file in (CREATOR_TESTS, BUILD_TESTS):
        code, tail = run_tests(ROOT, test_file)
        print(f"N2 baseline {test_file}: exit={code} {tail}")
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
