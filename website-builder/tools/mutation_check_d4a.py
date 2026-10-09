"""D4a mutation driver -- the context library's load-bearing isolation and
fallback guards.

Same discipline as the D3a.5 / D3b drivers: revert ONE guard at a time on a
THROWAWAY COPY and prove the focused D4a tests go red. The working tree is never
edited; the mutation MUST land inside the temp copy (guarded explicitly below).

Every mutation is chosen so that, with the guard removed, a SPECIFIC unsafe
behaviour becomes reachable -- not merely "some assert fails":

   1. Viking-URI validation is removed          -> a traversing/foreign URI passes
   2. scope containment is loosened to a prefix -> project 'ab' leaks into 'a'
   3. the feature flag default is flipped on     -> OpenViking is on by default
   4. the disabled short-circuit is removed      -> a disabled flag still retrieves
   5. the malformed-match drop is removed        -> a junk match is accepted
   6. the retrieval scope check is removed       -> a foreign URI is accepted
   7. the provenance requirement is removed      -> an unsourced item is accepted
   8. the cross-tenant record check is removed   -> another project's record leaks
   9. the credential check is removed            -> credential-shaped text is returned
  10. the category allowlist is removed          -> an out-of-scope category leaks
  11. the result-count budget is removed         -> unbounded results
  12. the byte budget is removed                 -> unbounded context bytes
  13. the token budget is removed                -> unbounded context tokens
  14. the forbidden-source skip is removed       -> a secret/dep path is ingested
  15. duplicate detection is removed             -> re-ingestion is not idempotent
  16. the oversize bound is removed              -> an unbounded file is ingested
  17. the L0/L1 policy is removed                -> L2 detail loads unconditionally

Run:  python tools/mutation_check_d4a.py
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

LIBRARY = "app/core/openviking_library.py"
RETRIEVAL = "app/core/openviking_retrieval.py"

#: The focused D4a tests. The composition test is intentionally NOT included in
#: the per-mutation loop (it composes the whole runtime and is slow); every
#: guard below is exercised by the library/retrieval suites directly.
D4A_TESTS = (
    "tests/test_openviking_library.py",
    "tests/test_openviking_retrieval.py",
)

MUTATIONS = [
    # --- 1. Viking-URI validation (scheme AND segment safety) --------------
    #     One guard: the single URI-validation point. Removing it lets a
    #     traversing in-scope URI (``.../alpha/../../x``) pass the scope check.
    (
        "a malformed or foreign-scheme URI is refused",
        LIBRARY,
        """    if not isinstance(uri, str) or not uri.startswith(f"{VIKING_SCHEME}://"):
        return None
    path = uri[len(f"{VIKING_SCHEME}://"):]
    segments = [s for s in path.split("/") if s != ""]
    if not segments:
        return None
    for segment in segments:
        if segment in (".", ".."):
            return None
        if not _SAFE_SEGMENT_RE.match(segment):
            return None
    return segments""",
        """    if not isinstance(uri, str):
        return None
    path = uri[len(f"{VIKING_SCHEME}://"):]
    segments = [s for s in path.split("/") if s != ""]
    return segments or None""",
    ),
    # --- 2. scope containment (the cross-project guarantee) ---------------
    (
        "scope containment is exact, not a string prefix",
        LIBRARY,
        """    return candidate == scope or candidate.startswith(scope + "/")""",
        """    return candidate.startswith(scope)""",
    ),
    # --- 4. the feature flag default is DISABLED --------------------------
    (
        "the OpenViking feature flag defaults to disabled",
        RETRIEVAL,
        """    enabled: bool = False""",
        """    enabled: bool = True""",
    ),
    # --- 5. the disabled short-circuit ------------------------------------
    (
        "a disabled adapter retrieves nothing",
        RETRIEVAL,
        """        if not self.enabled:
            return self._empty(
                status=STATUS_DISABLED,""",
        """        if False:
            return self._empty(
                status=STATUS_DISABLED,""",
    ),
    # --- 6. the malformed-match drop --------------------------------------
    (
        "a malformed match is dropped",
        RETRIEVAL,
        """            if not isinstance(raw, RawMatch) or not isinstance(raw.uri, str) or not raw.uri:
                warnings.append(WARNING_MALFORMED_ITEM)
                continue""",
        """            if False:
                warnings.append(WARNING_MALFORMED_ITEM)
                continue""",
    ),
    # --- 7. the retrieval scope check -------------------------------------
    (
        "a retrieved URI outside the project scope fails closed",
        RETRIEVAL,
        """            if not is_uri_within_scope(raw.uri, scope_uri):
                violations.append(_Violation(ERROR_ISOLATION_VIOLATION, WARNING_ISOLATION))
                continue""",
        """            if False:
                violations.append(_Violation(ERROR_ISOLATION_VIOLATION, WARNING_ISOLATION))
                continue""",
    ),
    # --- 8. the provenance requirement ------------------------------------
    (
        "an item with no provenance fails closed",
        RETRIEVAL,
        """            if record is None:
                violations.append(_Violation(ERROR_PROVENANCE_MISSING, WARNING_PROVENANCE))
                continue""",
        """            if False:
                violations.append(_Violation(ERROR_PROVENANCE_MISSING, WARNING_PROVENANCE))
                continue""",
    ),
    # --- 9. the cross-tenant record check ---------------------------------
    (
        "a cross-tenant record fails closed",
        RETRIEVAL,
        """            if record_project and record_project != project_id:
                violations.append(_Violation(ERROR_CROSS_TENANT, WARNING_CROSS_TENANT))
                continue""",
        """            if False:
                violations.append(_Violation(ERROR_CROSS_TENANT, WARNING_CROSS_TENANT))
                continue""",
    ),
    # --- 10. the credential check -----------------------------------------
    (
        "credential-shaped content fails closed",
        RETRIEVAL,
        """            if text_looks_like_credential(haystack):
                violations.append(_Violation(ERROR_CREDENTIAL_LEAK, WARNING_CREDENTIAL))
                continue""",
        """            if False:
                violations.append(_Violation(ERROR_CREDENTIAL_LEAK, WARNING_CREDENTIAL))
                continue""",
    ),
    # --- 11. the category allowlist ---------------------------------------
    (
        "an out-of-scope category is filtered",
        RETRIEVAL,
        """            if categories and category not in categories:
                continue""",
        """            if False:
                continue""",
    ),
    # --- 12. the result-count budget --------------------------------------
    (
        "the result-count budget is enforced",
        RETRIEVAL,
        """            if len(kept) >= budget.max_items:
                truncated = True
                warnings.append(WARNING_RESULT_LIMIT)
                break""",
        """            if False:
                truncated = True
                warnings.append(WARNING_RESULT_LIMIT)
                break""",
    ),
    # --- 13. the byte budget ----------------------------------------------
    (
        "the context byte budget is enforced",
        RETRIEVAL,
        """            if total_bytes + item_bytes > budget.max_bytes:
                truncated = True
                warnings.append(WARNING_TRUNCATED)
                break""",
        """            if False:
                truncated = True
                warnings.append(WARNING_TRUNCATED)
                break""",
    ),
    # --- 14. the token budget ---------------------------------------------
    (
        "the context token budget is enforced",
        RETRIEVAL,
        """            if total_tokens + item.estimated_tokens > budget.max_tokens:
                truncated = True
                warnings.append(WARNING_TRUNCATED)
                break""",
        """            if False:
                truncated = True
                warnings.append(WARNING_TRUNCATED)
                break""",
    ),
    # --- 15. the forbidden-source skip ------------------------------------
    (
        "a forbidden source path is never ingested",
        LIBRARY,
        """        elif is_forbidden_source(spec.locator):
            status, reason = "skipped_forbidden", "source path is on the never-index list\"""",
        """        elif False:
            status, reason = "skipped_forbidden", "source path is on the never-index list\"""",
    ),
    # --- 16. duplicate detection (idempotent ingestion) -------------------
    (
        "re-ingesting an unchanged source is a no-op",
        LIBRARY,
        """                if existing is not None and existing.digest == digest:
                    status, reason = "skipped_duplicate", "unchanged source already indexed\"""",
        """                if False:
                    status, reason = "skipped_duplicate", "unchanged source already indexed\"""",
    ),
    # --- 17. the per-source oversize bound --------------------------------
    (
        "an oversize source is refused",
        LIBRARY,
        """            elif len(content) > MAX_SOURCE_BYTES:
                status, reason = "rejected_oversize", "source exceeds the per-source byte limit\"""",
        """            elif False:
                status, reason = "rejected_oversize", "source exceeds the per-source byte limit\"""",
    ),
    # --- 18. the L0/L1-only policy ----------------------------------------
    (
        "L2 detail is loaded only when justified",
        RETRIEVAL,
        """        if budget.allow_detail and raw.content:
            return LEVEL_DETAIL, raw.content""",
        """        if raw.content:
            return LEVEL_DETAIL, raw.content""",
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
    baseline_code, tail = run_tests(ROOT, D4A_TESTS)
    print(f"D4a baseline: exit={baseline_code} {tail}")
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

            code, tail = run_tests(work, D4A_TESTS)
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

    print(f"all {len(MUTATIONS)} guards killed by the focused tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
