"""Mutation driver for the D1 design retrieval + policy layer.

Reverts one guard at a time on a THROWAWAY COPY of the tree and proves the
focused tests go red. Mirrors tools/mutation_check_d0.py's discipline: the real
working tree is never edited.

Each mutation below corresponds to a guard whose absence would produce a
SPECIFICALLY wrong outcome, not merely different code. That specificity is the
whole point of the exercise -- a mutation that changes code without changing an
observable outcome proves nothing about whether the guard matters.

Run:  python tools/mutation_check_d1.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv", "venv")
TEST_FILE = "tests/test_design_resource_activation.py"

RETRIEVAL = "app/core/design_retrieval.py"
POLICIES = "app/core/design_policies.py"

# Each entry: (label, relative-path, text that must be present, replacement).
#
# The anchors are intentionally LARGE multi-line blocks rather than short
# tokens. A one-token anchor matches too many places, and when a mutation
# "fails" because the anchor drifted the driver reports ANCHOR-MISS loudly
# rather than silently proving nothing.
MUTATIONS = [
    # --- 1. Required-missing must still fail closed ------------------------
    (
        "required-missing still fails closed",
        RETRIEVAL,
        """        if result.status == STATUS_UNAVAILABLE_REQUIRED:
            ok = False""",
        """        if result.status == STATUS_UNAVAILABLE_REQUIRED:
            ok = True""",
    ),
    # --- 2. Entry-count cap ------------------------------------------------
    (
        "entry-count cap removed",
        RETRIEVAL,
        """        if len(entries) >= limits.max_entries_per_resource:
            dropped += 1
            continue""",
        """        if False:
            dropped += 1
            continue""",
    ),
    # --- 3. Per-entry cap must count the FULL payload, not body ------------
    (
        "per-entry cap counts full payload, not body only",
        RETRIEVAL,
        """    total = len(entry.title or "") + len(entry.body or "")
    for key, value in entry.fields.items():
        total += len(key) + len(value)""",
        """    total = len(entry.body or "")""",
    ),
    # --- 4. Truncation order: provenance first, body last -----------------
    (
        "truncation order puts body before provenance/fields",
        RETRIEVAL,
        """    remaining = budget - mandatory
    was_truncated = False

    # Step 2: title.
    title, cut = _truncate_text(entry.title or "", remaining)""",
        """    remaining = budget - mandatory
    was_truncated = False

    body_first, cut_body = _truncate_text(entry.body or "", remaining)
    remaining -= len(body_first)

    # Step 2: title.
    title, cut = _truncate_text(entry.title or "", remaining)""",
    ),
    # --- 5. Entry dropped when mandatory metadata cannot fit --------------
    (
        "entry dropped when mandatory metadata cannot fit",
        RETRIEVAL,
        """    if mandatory > budget:
        return None""",
        """    if False:
        return None""",
    ),
    # --- 6. Aggregate cap must use the SAME size function ------------------
    (
        "aggregate cap uses the same size function as the entry cap",
        RETRIEVAL,
        """    for entry in entries:
        size = payload_chars(entry)
        if running + size > limits.max_resource_chars:""",
        """    for entry in entries:
        size = len(entry.body or "")
        if running + size > limits.max_resource_chars:""",
    ),
    # --- 7. Aggregate total char budget ------------------------------------
    (
        "aggregate total char budget enforced",
        RETRIEVAL,
        """        if total_chars + consumed > limits.max_total_chars:""",
        """        if False:""",
    ),
    # --- 8. Query filter must exist at all ---------------------------------
    (
        "query filter applied",
        RETRIEVAL,
        """    selected = filter_rows(normalized, context.tokens)""",
        """    selected = list(normalized)""",
    ),
    # --- 9. Query filter must run BEFORE the count cap --------------------
    # Reverting this makes a late-matching row unreachable, because capping
    # first truncates the head of the file before relevance is ever considered.
    (
        "query filter runs before the count cap",
        RETRIEVAL,
        """    # Filter over the WHOLE dataset before any cap. See filter_rows.
    selected = filter_rows(normalized, context.tokens)""",
        """    # Filter over the head of the file only.
    selected = filter_rows(normalized[:1], context.tokens)""",
    ),
    # --- 10. Matched rows must not be re-sorted by match count -------------
    (
        "matched rows keep source order, never re-ranked",
        RETRIEVAL,
        """def filter_rows(
    rows: Sequence[Mapping[str, str]], tokens: Sequence[str]
) -> List[Mapping[str, str]]:""",
        """def _rank(row, tokens):
    return sum(1 for t in tokens for v in row.values() if isinstance(v, str) and t in v.lower())


def filter_rows(
    rows: Sequence[Mapping[str, str]], tokens: Sequence[str]
) -> List[Mapping[str, str]]:""",
    ),
    # --- 11. No column-name heuristic --------------------------------------
    (
        "no column-name heuristic: every column searched",
        RETRIEVAL,
        """    for value in row.values():
        if not isinstance(value, str):
            continue
        lowered = value.lower()
        for token in tokens:
            if token in lowered:
                return True
    return False""",
        """    for key, value in row.items():
        if key.lower() in ("instruction", "prompt", "recommendation"):
            continue
        if not isinstance(value, str):
            continue
        lowered = value.lower()
        for token in tokens:
            if token in lowered:
                return True
    return False""",
    ),
    # --- 12. Containment re-checked on every read -------------------------
    (
        "containment re-checked before each read",
        RETRIEVAL,
        """        if not entry_is_contained(skill_root, path):
            adapter_failed = True
            warnings.append(WARNING_UNDECLARED_LOCATOR)
            continue""",
        """        if False:
            adapter_failed = True
            warnings.append(WARNING_UNDECLARED_LOCATOR)
            continue""",
    ),
    # --- 13. Unavailable optional must never fall back --------------------
    (
        "unavailable optional degrades without inventing entries",
        RETRIEVAL,
        """        return (
            _empty_result(resource, status, available, warning=WARNING_ABSENT),
            0,
        )""",
        """        return (
            _empty_result(resource, STATUS_AVAILABLE, True, warning=WARNING_ABSENT),
            0,
        )""",
    ),
    # --- 14. Truncation must remain deterministic -------------------------
    # Iterating a set makes shrinkage order depend on hash randomization, so
    # the same input normalizes to different bytes across processes.
    (
        "truncation ordering is deterministic",
        RETRIEVAL,
        """    for key in entry.fields:
        value = entry.fields[key]""",
        """    for key in set(entry.fields):
        value = entry.fields[key]""",
    ),
    # --- 15. Provenance must survive normalization ------------------------
    (
        "provenance survives normalization",
        RETRIEVAL,
        """    return DesignEntry(
        entry_id=entry.entry_id,
        kind=entry.kind,
        title=title,
        body=body,
        fields=fields,
        provenance=entry.provenance,
        truncated=entry.truncated or was_truncated,
    )""",
        """    return DesignEntry(
        entry_id=entry.entry_id,
        kind=entry.kind,
        title=title,
        body=body,
        fields=fields,
        provenance=EntryProvenance(
            resource_id=entry.provenance.resource_id,
            resource_kind=entry.provenance.resource_kind,
            adapter=entry.provenance.adapter,
            locator="",
            entry_index=entry.provenance.entry_index,
        ),
        truncated=entry.truncated or was_truncated,
    )""",
    ),
    # --- 16. On-demand must never conflate with installed -----------------
    (
        "on-demand resting state is not a degradation",
        RETRIEVAL,
            """    if resource.kind in POLICY_ONLY_RESOURCE_KINDS:
        return (""",
            """    if False:
        return (""",
    ),
    # --- 17. Entry schema carries no authority-bearing field --------------
    # Promoting matched text into a `fields` key named `instruction` is the
    # exact promotion the trust boundary forbids.
    (
        "resource text stays inert data, never authority",
        RETRIEVAL,
        """    for key in entry.fields:
        value = entry.fields[key]""",
        """    for key in list(entry.fields) + ["instruction"]:
        value = entry.fields.get(key, "")""",
    ),
    # --- 18. Locator is always skill-root-relative ------------------------
    (
        "provenance locator stays relative (no host path leakage)",
        RETRIEVAL,
        """            locator=raw.locator,""",
        """            locator=str(skill_root / raw.locator),""",
    ),
    # --- 19. Unknown resource id must fail loudly --------------------------
    (
        "undeclared resource id fails loudly",
        RETRIEVAL,
        """        resource = manifest.get(resource_id)""",
        """        try:
            resource = manifest.get(resource_id)
        except Exception:
            continue""",
    ),
    # --- 20. On-demand dependency never reported globally installed -------
    (
        "on-demand dependency never reported installed",
        POLICIES,
        """    return bool(policy is not None and policy.is_installed)""",
        """    return True""",
    ),
    # --- 21. Policy never auto-selects a dependency ------------------------
    (
        "policy does not auto-select a dependency",
        POLICIES,
        """    return policy.state if policy is not None else "known\"""",
        """    return "selected" if policy is not None else "known\"""",
    ),
    # --- 22. Adapter naming an undeclared locator is inert ----------------
    (
        "adapter naming an undeclared locator is rendered inert",
        RETRIEVAL,
        """    permitted, undeclared = _adapter_locators_allowed(adapter, resource)
    if undeclared:""",
        """    permitted, undeclared = _adapter_locators_allowed(adapter, resource)
    if False:""",
    ),
    # --- 23. Malformed output fails closed ---------------------------------
    (
        "malformed adapter output fails closed",
        RETRIEVAL,
        """    if adapter_failed and not raw_entries:""",
        """    if False:""",
    ),
    # --- 24. Critic adapter stays inert ------------------------------------
    (
        "critic adapter ships no parser and synthesizes nothing",
        RETRIEVAL,
        """    return AdapterResult(entries=(), warnings=(WARNING_CRITIC_INERT,), ok=True)""",
        """    return AdapterResult(
        entries=(
            _RawEntry(
                title="synthetic",
                body="invented finding",
                fields={"finding": "invented"},
                index=0,
                locator="",
            ),
        ),
        warnings=(WARNING_CRITIC_INERT,),
        ok=True,
    )""",
    ),
    # --- 25. Reference corpora never invent guidance ----------------------
    (
        "reference corpus returns no invented guidance",
        RETRIEVAL,
        """    return AdapterResult(entries=(), warnings=(WARNING_REFERENCE_UNAVAILABLE,), ok=True)""",
        """    return AdapterResult(
        entries=(
            _RawEntry(
                title="Refero",
                body="typical corpus contents summarized",
                fields={"note": "invented"},
                index=0,
                locator="",
            ),
        ),
        warnings=(WARNING_REFERENCE_UNAVAILABLE,),
        ok=True,
    )""",
    ),
    # --- 26. Total-char overrun marks the result truncated ----------------
    (
        "budget-dropped resource reports truncation",
        RETRIEVAL,
        """        warnings=(WARNING_DROPPED_FOR_BUDGET,),
        truncated=True,""",
        """        warnings=(WARNING_DROPPED_FOR_BUDGET,),
        truncated=False,""",
    ),
    # --- 27. Field keys stay whole -----------------------------------------
    # Truncating a key corrupts column identity, producing two
    # indistinguishable half-columns.
    (
        "field keys stay whole",
        RETRIEVAL,
        """        if len(key) > remaining:""",
        """        if len(key) > remaining and False:""",
    ),
]


def run_tests(cwd):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            TEST_FILE,
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

        mutated = original.replace(present, replacement, 1)
        if mutated == original:
            print(f"[ANCHOR-MISS] {label} (replacement was a no-op)")
            unproven.append(f"{label}: mutation changed nothing")
            continue

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "tree"
            shutil.copytree(ROOT, work, ignore=IGNORED)
            (work / relative).write_text(mutated, encoding="utf-8")
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