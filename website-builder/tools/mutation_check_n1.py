"""Mutation driver for N1 — canonical PRD.md / Design.md document contracts.

Same discipline as ``tools/mutation_check_d0.py``, ``mutation_check_d1.py`` and
``mutation_check_d2_d3a.py``: revert one guard at a time on a THROWAWAY COPY and
prove the focused tests go red. The real working tree is never edited.

Each mutation corresponds to a guard whose absence produces a SPECIFICALLY wrong
outcome — a document that validates when it must not, or loses determinism:

  * fence-not-required        -> prose with no front matter parses as a document
  * fence-not-terminated      -> a truncated document is silently accepted
  * non-scalar-line-allowed   -> an arbitrary line becomes front-matter content
  * duplicate-key-allowed     -> two contradictory values for one field
  * unknown-field-ignored     -> a typo'd field silently vanishes
  * required-field-optional   -> a document with no identity validates
  * kind-unchecked            -> a Design.md passes as a PRD.md
  * schema-version-unpinned   -> a future/foreign schema validates as current
  * section-not-required      -> a document missing a contract section validates
  * empty-section-allowed     -> a present-but-empty section validates
  * serialisation-unordered   -> the same document no longer hashes stably
  * partial-presence-hidden   -> a half-migrated project reads as consistent
  * dna-contradiction-allowed -> Design.md may contradict design-dna.json

Run:  python tools/mutation_check_n1.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv", "venv")

CONTRACTS_TESTS = "tests/test_document_contracts.py"
CONTRACTS = "app/core/document_contracts.py"

# Each entry: (label, relative-path, text that must be present, replacement).
MUTATIONS = [
    # --- 1. The opening fence is required ---------------------------------
    (
        "a document must begin with a front-matter fence",
        CONTRACTS,
        """    if not text.startswith(_FRONT_MATTER_FENCE):
        raise DocumentContractError(
            ERR_MISSING_FRONT_MATTER,
            "document must begin with a '---' front-matter fence",
        )""",
        """    if False:
        raise DocumentContractError(
            ERR_MISSING_FRONT_MATTER,
            "document must begin with a '---' front-matter fence",
        )""",
    ),
    # --- 2. The closing fence is required ---------------------------------
    (
        "an unterminated front-matter fence is rejected",
        CONTRACTS,
        """    if end_index is None:
        raise DocumentContractError(
            ERR_UNTERMINATED_FRONT_MATTER,
            "front-matter fence opened but never closed",
        )""",
        """    if end_index is None:
        end_index = len(lines)""",
    ),
    # --- 3. Every front-matter line is a scalar ---------------------------
    (
        "a non-scalar front-matter line is rejected",
        CONTRACTS,
        """        if ":" not in line:
            raise DocumentContractError(
                ERR_MALFORMED_FRONT_MATTER,
                f"front-matter line is not 'key: value': {line!r}",
            )""",
        """        if ":" not in line:
            continue""",
    ),
    # --- 4. Duplicate keys are rejected -----------------------------------
    (
        "a duplicate front-matter key is rejected",
        CONTRACTS,
        """        if key in front_matter:
            raise DocumentContractError(
                ERR_MALFORMED_FRONT_MATTER,
                f"duplicate front-matter key: {key!r}",
            )""",
        """        if False:
            raise DocumentContractError(
                ERR_MALFORMED_FRONT_MATTER,
                f"duplicate front-matter key: {key!r}",
            )""",
    ),
    # --- 5. Unknown fields are rejected -----------------------------------
    (
        "an unknown front-matter field is rejected",
        CONTRACTS,
        """    for field in front_matter:
        if field not in RECOGNISED_FRONT_MATTER_FIELDS:
            raise DocumentContractError(ERR_UNKNOWN_FIELD, f"unknown front-matter field {field!r}")""",
        """    for field in front_matter:
        if False:
            raise DocumentContractError(ERR_UNKNOWN_FIELD, f"unknown front-matter field {field!r}")""",
    ),
    # --- 6. Required fields are mandatory ---------------------------------
    (
        "every required front-matter field is mandatory",
        CONTRACTS,
        """    for field in REQUIRED_FRONT_MATTER_FIELDS:
        if field not in front_matter:
            raise DocumentContractError(ERR_MISSING_FIELD, f"missing required field {field!r}")""",
        """    for field in REQUIRED_FRONT_MATTER_FIELDS:
        if False:
            raise DocumentContractError(ERR_MISSING_FIELD, f"missing required field {field!r}")""",
    ),
    # --- 7. Kind is a closed set ------------------------------------------
    (
        "the document kind must be a known kind",
        CONTRACTS,
        """    if kind not in DOCUMENT_KINDS:
        raise DocumentContractError(
            ERR_INVALID_KIND,
            f"document kind must be one of {DOCUMENT_KINDS}, got {kind!r}",
        )""",
        """    if False:
        raise DocumentContractError(
            ERR_INVALID_KIND,
            f"document kind must be one of {DOCUMENT_KINDS}, got {kind!r}",
        )""",
    ),
    # --- 8. Schema version is pinned --------------------------------------
    (
        "the schema version must match the kind",
        CONTRACTS,
        """    if schema_version != expected_schema:
        raise DocumentContractError(
            ERR_INVALID_SCHEMA_VERSION,
            f"{kind} schema_version must be {expected_schema}, got {schema_version}",
        )""",
        """    if False:
        raise DocumentContractError(
            ERR_INVALID_SCHEMA_VERSION,
            f"{kind} schema_version must be {expected_schema}, got {schema_version}",
        )""",
    ),
    # --- 9. Required sections are mandatory -------------------------------
    (
        "every required section must be present",
        CONTRACTS,
        """        if required not in sections:
            violations.append(
                DocumentViolation(
                    ERR_MISSING_SECTION,
                    field=required,
                    detail=f"required section '## {required}' is absent",
                )
            )""",
        """        if required not in sections:
            pass""",
    ),
    # --- 10. Empty sections are rejected ----------------------------------
    (
        "a present-but-empty required section is rejected",
        CONTRACTS,
        """        elif not sections[required]:
            violations.append(
                DocumentViolation(
                    ERR_EMPTY_SECTION,
                    field=required,
                    detail=f"required section '## {required}' is present but empty",
                )
            )""",
        """        elif False:
            violations.append(
                DocumentViolation(
                    ERR_EMPTY_SECTION,
                    field=required,
                    detail=f"required section '## {required}' is present but empty",
                )
            )""",
    ),
    # --- 11. Serialisation is order-independent ---------------------------
    (
        "serialisation sorts keys so the bytes are stable",
        CONTRACTS,
        """    for key in sorted(front_matter):
        lines.append(f"{key}: {front_matter[key]}")""",
        """    for key in front_matter:
        lines.append(f"{key}: {front_matter[key]}")""",
    ),
    # --- 12. Partial presence is not hidden -------------------------------
    (
        "partial document presence is distinguishable from legacy",
        CONTRACTS,
        """    @property
    def partial(self) -> bool:
        \"\"\"Exactly one document present: an inconsistent, unsupported state.\"\"\"
        return self.prd_present != self.design_present""",
        """    @property
    def partial(self) -> bool:
        \"\"\"Exactly one document present: an inconsistent, unsupported state.\"\"\"
        return False""",
    ),
    # --- 13. Design.md may not contradict design-dna.json -----------------
    (
        "a Design.md section that contradicts design-dna.json is rejected",
        CONTRACTS,
        """        if candidates and not any(c in section_text for c in candidates):
            violations.append(
                DocumentViolation(
                    ERR_LEGACY_CONTRADICTION,""",
        """        if False:
            violations.append(
                DocumentViolation(
                    ERR_LEGACY_CONTRADICTION,""",
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
    baseline_code, tail = run_tests(ROOT, CONTRACTS_TESTS)
    print(f"N1 contracts baseline: exit={baseline_code} {tail}")
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
            (work / relative).write_text(mutated, encoding="utf-8")

            code, tail = run_tests(work, CONTRACTS_TESTS)
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
