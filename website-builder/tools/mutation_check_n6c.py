"""Mutation driver for N6-C — pre-build Design Guidance.

Throwaway-copy discipline: revert one guard at a time on a COPY and prove the
focused tests go red.

Run:  python tools/mutation_check_n6c.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv", "venv")

TESTS = "tests/test_design_guidance.py"
CREATOR = "app/core/design_guidance.py"
CONTRACTS = "app/core/document_contracts.py"
STORE = "app/core/document_store.py"

MUTATIONS = [
    (
        "a section with no evidence is marked UNRESOLVED, never fabricated",
        CREATOR,
        """def _line(value: str) -> str:
    \"\"\"Render one honest section line: the value, or an explicit UNRESOLVED note.\"\"\"
    return value if value else f\"{UNRESOLVED_MARKER}: no supporting evidence supplied\"""",
        """def _line(value: str) -> str:
    \"\"\"Render one honest section line: the value, or an explicit UNRESOLVED note.\"\"\"
    return value if value else "a modern, welcoming aesthetic\"""",
    ),
    (
        "an approved reference requires real provenance",
        CREATOR,
        """    if evidence.get("has_candidate") and evidence.get("source_uri"):""",
        """    if evidence.get("has_candidate"):""",
    ),
    (
        "the guidance kind is distinct from the design kind",
        CONTRACTS,
        """DOCUMENT_KIND_DESIGN_GUIDANCE = "design_guidance\"""",
        """DOCUMENT_KIND_DESIGN_GUIDANCE = "design\"""",
    ),
    (
        "the guidance schema version is recorded in front matter",
        CREATOR,
        """        "design_guidance_version": str(design_guidance_version),""",
        """        "design_dna_version": str(design_guidance_version),""",
    ),
    (
        "the store refuses an invalid guidance document",
        STORE,
        """    identity, violations = _validate(kind, text)

    if violations:
        raise ValueError(
            f"refusing to write invalid {kind} document: "
            + ", ".join(v.code for v in violations)
        )
    if identity is None:  # defensive: validators return an identity when clean
        raise ValueError(f"refusing to write {kind} document with no identity")""",
        """    identity, violations = _validate(kind, text)

    if False:
        raise ValueError(
            f"refusing to write invalid {kind} document: "
            + ", ".join(v.code for v in violations)
        )
    if False:
        raise ValueError(f"refusing to write {kind} document with no identity")""",
    ),
    (
        "reading the design kind never returns the guidance document",
        STORE,
        """    if kind == DOCUMENT_KIND_DESIGN:
        return validate_design(text)
    if kind == DOCUMENT_KIND_DESIGN_GUIDANCE:
        return validate_design_guidance(text)""",
        """    if kind == DOCUMENT_KIND_DESIGN:
        return validate_design(text)
    if kind == DOCUMENT_KIND_DESIGN_GUIDANCE:
        return validate_design(text)""",
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
    print(f"N6-C baseline: exit={code} {tail}")
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
