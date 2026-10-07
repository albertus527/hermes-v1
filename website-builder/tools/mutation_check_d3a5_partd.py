"""Mutation driver for D3a.5 Part D (typed registry install boundary).

Same discipline as the other D3a.5 drivers: revert one guard at a time on a
THROWAWAY COPY and prove the focused tests go red. The real working tree is
never edited.

Every mutation corresponds to a guard whose absence produces a SPECIFICALLY
wrong outcome:

  * builtin-allowlist-widened  -> an external component becomes installable by
                                  name, bypassing the reviewed table entirely
  * locator-echoes-input        -> any caller-supplied string becomes an argv
  * approval-bypassed          -> an unreviewed component becomes installable
  * url-passes-id-check        -> attacker-shaped text is substituted into a URL
  * host-check-removed         -> a template edited to another host still passes
  * dependency-unknown-ignored -> upstream metadata widens the allowlist
  * dependency-request-bypass  -> a request can carry an un-allowlisted dep
  * builtin-carries-locator    -> the two registry paths are reconflated
  * none-request-crashes       -> callers are pushed toward an if/ok check

Parts F/G add the catalog bounds (a widening payload, a per-source id
vocabulary, an untrusted dependency name). Part H adds the transitions
invariants:

  * argv-requires-catalog      -> arbitrary text and open-ended requests
                                  (`all`, `--free`) reach the CLI
  * pro-tier-reachable         -> a build triggers a browser sign-in flow
  * slug-prefix-matched        -> `card` silently installs `card-resize`
  * partial-materialization-ok  -> the caller builds against a missing recipe
  * uncontained-file-verified   -> a symlinked recipe passes the postcondition
  * unknown-destination-verified-> an unresolvable dir reports success
  * symlinked-dir-accepted      -> the recipes dir escapes the workspace

Run:  python tools/mutation_check_d3a5_partd.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv", "venv")

REGISTRY_TESTS = "tests/test_design_registry.py"
CATALOG_TESTS = "tests/test_design_catalog.py"
REGISTRY = "app/core/design_registry.py"
CATALOG = "app/core/design_catalog.py"
TRANSITIONS = "app/core/design_transitions.py"
INSTALL = "app/core/design_install.py"

MUTATIONS = [
    # --- 1. the builtin allowlist is never widened ------------------------
        # Mutates `build_registry_request`: with the builtin membership check
        # removed, an external component id could be requested under
        # `shadcn_builtin` and be accepted without ever appearing in
        # ALLOWED_SHADCN_COMPONENTS -- i.e. the external path would have bypassed
        # the reviewed table entirely.
        (
            "an external component never enters the builtin install path",
            REGISTRY,
            """    if source == SOURCE_SHADCN_BUILTIN:
        if component_id not in ALLOWED_SHADCN_COMPONENTS:
            return RegistryRequestOutcome(
                ok=False, request=None, reason=REASON_COMPONENT_NOT_BUILTIN
            )""",
        """    if source == SOURCE_SHADCN_BUILTIN:
        if False:
            return RegistryRequestOutcome(
                ok=False, request=None, reason=REASON_COMPONENT_NOT_BUILTIN
            )""",
    ),
    # --- 2. the resolver never echoes its input ---------------------------
    (
        "the locator resolver never derives a locator from arbitrary input",
        REGISTRY,
        """    if component_id not in _APPROVED_COMPONENTS.get(source, frozenset()):
        return None
    return _format_locator(source, component_id)""",
        """    return _format_locator(source, component_id)""",
    ),
    # --- 3. approval is required ------------------------------------------
        # The pre-repair external path had NO approval enforcement and NO
        # reviewed-contract check: any well-formed component identity resolved
        # (or fabricated) a locator and produced an install request. This
        # mutation restores exactly that path -- both the locator approval check
        # AND the contract check are bypassed, so the unreviewed-component and
        # contract tests go red together. It is deliberately a TWO-guard
        # mutation: the two checks are defence-in-depth for the same property,
        # and removing only one leaves the other to catch the case (which is
        # what the contract-missing mutation in partbc.py proves independently).
    (
        "an unreviewed component is refused before any locator is used",
        REGISTRY,
        """    locator = resolve_registry_locator(source, component_id)
    if locator is None:
        return RegistryRequestOutcome(
            ok=False, request=None, reason=REASON_COMPONENT_UNKNOWN
        )""",
        """    # MUTATED: pre-repair behaviour -- fabricate a locator for any identity
    # and return the request immediately, bypassing approval AND the contract.
    locator = resolve_registry_locator(source, component_id) or (
        f"https://{REGISTRY_HOSTS.get(source, 'evil.example')}/r/{component_id}"
    )
    return RegistryRequestOutcome(
        ok=True,
        request=RegistryInstallRequest(
            source=source,
            component_id=component_id,
            registry_locator_id=locator,
            required_dependency_ids=dependency_ids,
        ),
        reason=REASON_REQUEST_OK,
        dependency_ids=dependency_ids,
    )""",
    ),
    # --- 4. a URL is never a component identity ---------------------------
    (
        "URL-shaped text is never treated as a component identity",
        REGISTRY,
        """    if not isinstance(component_id, str) or not component_id or len(component_id) > 64:
        return False
    return bool(_COMPONENT_ID_RE.match(component_id) or _COMPONENT_SLUG_RE.match(component_id))""",
        """    if not isinstance(component_id, str) or not component_id:
        return True""",
    ),
    # --- 5. the resolved locator is host-checked --------------------------
    (
        "a locator must resolve to its source's host",
        REGISTRY,
        """    expected_host = REGISTRY_HOSTS.get(source)
    if not expected_host or not locator.startswith(f"https://{expected_host}/"):
        logger.error("A registry locator resolved outside its source's host; refusing.")
        return None""",
        """    if not locator:
        return None""",
    ),
    # --- 6. unknown dependencies refuse the component ---------------------
    (
        "an unknown dependency requirement refuses the whole component",
        REGISTRY,
        """    dependency_ids, unknown = resolve_dependency_requirements(declared_dependencies)
    if unknown:""",
        """    dependency_ids, unknown = resolve_dependency_requirements(declared_dependencies)
    if False:""",
    ),
    # --- 7. a request cannot carry an un-allowlisted dependency ----------
    (
        "a request rejects a dependency outside the closed allowlist",
        REGISTRY,
        """        for dependency_id in self.required_dependency_ids:
            if dependency_id not in DEPENDENCY_PACKAGES:
                raise ValueError(
                    f"required dependency is not allowlisted: {dependency_id!r}"
                )""",
        """        pass""",
    ),
    # --- 8. a builtin cannot carry a locator ------------------------------
    (
        "a builtin registry request can never carry a locator",
        REGISTRY,
        """            if self.registry_locator_id:
                raise ValueError("a shadcn builtin must not carry a registry locator")""",
        """            if False:
                raise ValueError("a shadcn builtin must not carry a registry locator")""",
    ),
    # --- 9. a locator must match the canonical one -----------------------
    (
        "a request's locator must match the canonical locator",
        REGISTRY,
        """            if resolve_registry_locator(self.source, self.component_id) != self.registry_locator_id:
                raise ValueError(
                    "registry locator does not match the application's canonical "
                    "locator for this source and component"
                )""",
        """            if not self.registry_locator_id.startswith("https://"):
                raise ValueError("registry locator must be https")""",
    ),
    # --- 10. a refused request yields no argv -----------------------------
    (
        "a missing request produces no argv rather than crashing",
        REGISTRY,
        """    if request is None or request.is_builtin or not request.registry_locator_id:
        return ()""",
        """    if request is None:
        raise ValueError("no request")
    if request.is_builtin or not request.registry_locator_id:
        return ()""",
    ),
    # --- 11. the source vocabulary is closed ------------------------------
    (
        "an unknown registry source is refused",
        REGISTRY,
        """    if not isinstance(source, str) or source not in REGISTRY_SOURCES:
        return RegistryRequestOutcome(ok=False, request=None, reason=REASON_SOURCE_UNKNOWN)""",
        """    if not isinstance(source, str):
        return RegistryRequestOutcome(ok=False, request=None, reason=REASON_SOURCE_UNKNOWN)""",
    ),
    # ------------------------------------------------------------------
    # Parts F/G: catalog normalization (21st.dev + React Bits)
    # ------------------------------------------------------------------
    # --- 11a. a reserved 21st route segment is never a component id -----
    (
        "a reserved 21st route segment is not a component id",
        CATALOG,
        """    if component_id in _RESERVED_COMPONENT_IDS.get(source, frozenset()):
        return False""",
        """    if False:
        return False""",
    ),
    # --- 11b. installable requires a REVIEWED locator, not just clean deps
    #          (the false positive: 64 listed, 1 installable)
    (
        "installable requires an approved locator, not just clean deps",
        CATALOG,
        """        return self.dependencies_in_policy and self.has_approved_locator""",
        """        return self.dependencies_in_policy""",
    ),
    # --- 12. each source validates ids in its OWN vocabulary ------------
    (
        "a React Bits id is never valid as a 21st id",
        CATALOG,
        """    if source == SOURCE_TWENTY_FIRST:
        return bool(_SLUG_RE.match(component_id))
    if source == SOURCE_REACT_BITS:
        return bool(_PASCAL_RE.match(component_id))
    return False""",
        """    if source in (SOURCE_TWENTY_FIRST, SOURCE_REACT_BITS):
        return bool(_SLUG_RE.match(component_id) or _PASCAL_RE.match(component_id))
    return False""",
    ),
    # --- 13. a malformed payload invents nothing ------------------------
    (
        "an unparseable payload yields no entries",
        CATALOG,
        """    if not ok:
        return CatalogResult(
            source=source, entries=(), warnings=(WARNING_CATALOG_MALFORMED,)
        )""",
        """    if not ok:
        return CatalogResult(
            source=source,
            entries=(
                CatalogEntry(
                    source=source,
                    component_id="placeholder",
                    display_name="placeholder",
                    provenance_host=SOURCE_HOSTS.get(source, ""),
                    provenance_path="placeholder",
                ),
            ),
            warnings=(),
        )""",
    ),
    # --- 14. an empty payload invents nothing -------------------------
    (
        "an empty catalog yields no entries",
        CATALOG,
        """    if not entries:
        return CatalogResult(
            source=source, entries=(), warnings=(WARNING_CATALOG_EMPTY,)
        )""",
        """    if not entries:
        return CatalogResult(source=source, entries=(), warnings=())""",
    ),
    # --- 15. an unknown dependency makes a component non-installable ---
    (
        "a component requiring an un-allowlisted package is not installable",
        CATALOG,
        """        return not self.unknown_dependency_ids""",
        """        return True""",
    ),
    # --- 16. upstream text is bounded before it is copied --------------
    (
        "upstream field text is bounded",
        CATALOG,
        """    cleaned = " ".join(value.split())
    return cleaned[:MAX_FIELD_CHARS]""",
        """    return value""",
    ),
    # --- 17. identity lookup is exact, never a prefix -------------------
    (
        "a component lookup never matches by prefix",
        CATALOG,
        """    for entry in result.entries:
        if entry.component_id == component_id:
            return entry
    return None""",
        """    for entry in result.entries:
        if component_id and (
            entry.component_id == component_id
            or entry.component_id.startswith(component_id)
        ):
            return entry
    return None""",
    ),
    # --- 18. truncation is reported, never silent ----------------------
    (
        "catalog truncation is reported rather than silent",
        CATALOG,
        """    return CatalogResult(
        source=source,
        entries=tuple(entries),
        truncated=len(entries) < len(documents),
    )""",
        """    return CatalogResult(
        source=source,
        entries=tuple(entries),
        truncated=False,
    )""",
    ),
    # --- 19. the entry limit is enforced --------------------------------
    (
        "the catalog entry limit is enforced",
        CATALOG,
        """        entries.append(entry)
        if len(entries) >= limit:
            break""",
        """        entries.append(entry)""",
    ),
    # =====================================================================
    # Part H -- transitions.dev recipes
    # =====================================================================
    # --- 20. argv requires CATALOG MEMBERSHIP, not just a well-formed slug
        # `not-a-real-recipe` and `all` are both valid kebab slugs, so a
        # shape-only check forwards arbitrary caller text -- and a request for
        # every recipe at once -- straight into argv.
    (
        "arbitrary text never reaches the transitions argv",
        TRANSITIONS,
        """    resolved = resolve_recipe_slug(slug, catalog)
    if resolved is None:
        return None
    return tuple(cli_prefix) + ("add", resolved)""",
        """    if not recipe_slug_is_well_formed(slug):
        return None
    return tuple(cli_prefix) + ("add", slug)""",
    ),
    # --- 21. the pro tier is unreachable --------------------------------
        # `add --pro` opens a browser device-flow sign-in. If pro entries were
        # catalogued, a build could trigger an interactive authenticated flow
        # nobody asked for.
    (
        "the authenticated pro tier never becomes installable",
        TRANSITIONS,
        """    if tier not in SUPPORTED_TIERS:
        return None""",
        """    if tier not in SUPPORTED_TIERS and tier != "pro":
        return None""",
    ),
    # --- 22. a slug is matched exactly, never by prefix -----------------
        # Prefix matching turns `card` into a silent install of `card-resize`,
        # i.e. the user gets a transition they did not select.
    (
        "a slug is never matched by prefix",
        TRANSITIONS,
        """    recipe = catalog.get(slug)
    return recipe.slug if recipe is not None else None""",
        """    for candidate in catalog.entries:
        if candidate.slug.startswith(slug):
            return candidate.slug
    return None""",
    ),
    # --- 23. materialization is all-or-nothing --------------------------
        # A partial materialization leaves the caller importing a recipe whose
        # CSS was never written; the build fails far from the cause.
    (
        "a missing required recipe artifact is not a success",
        TRANSITIONS,
        """        required = suffix == RECIPE_REQUIRED_SUFFIX
        if not candidate.exists():
            if required:
                return ()""",
        """        required = suffix == RECIPE_REQUIRED_SUFFIX
        if not candidate.exists():
            # MUTATED: a missing required artifact is treated as an
            # optional companion, so a partial materialization would pass.
            continue""",
    ),
    # --- 24. verification requires containment under the project ---------
        # Otherwise a symlinked recipe file writes through to a location the
        # workspace does not own and still counts as materialized.
    (
        "a recipe file outside the project is not materialized",
        TRANSITIONS,
        """            if not is_contained(project_root, candidate):
                return ()""",
        """            pass""",
    ),
    # --- 25. an unknown destination verifies nothing --------------------
        # The fail-closed answer when the recipes dir cannot be resolved.
    (
        "an unresolved destination verifies nothing",
        TRANSITIONS,
        """    if recipes_dir is None or not recipe_slug_is_well_formed(slug):
        return ()""",
        """    if not recipe_slug_is_well_formed(slug):
        return ()""",
    ),
    # --- 26. a symlinked recipes dir escaping the project is refused -----
        # Without containment, a crafted project writes transitions outside the
        # workspace through a symlink. This guard is host-dependent: Windows
        # needs Developer Mode to create a symlink, so the test SKIPS there and
        # this mutation would look like a survivor. The driver reports that as
        # HOST-SKIPPED, never as proven -- the property it guards is separately
        # proven portably by the containment tests.
    (
        "a recipes dir escaping the project is refused",
        TRANSITIONS,
        """    candidate = Path(project_root) / RECIPES_DIRNAME""",
        """    candidate = Path(project_root) / RECIPES_DIRNAME
    if candidate.is_dir():
        return candidate""",
    ),
]

#: Guards whose proof needs a host capability the current host may not have.
#: A skipped test is NOT a kill, so these are reported separately rather than
#: folded into the survivor list.
HOST_DEPENDENT = {"a recipes dir escaping the project is refused"}


TRANSITIONS_TESTS = "tests/test_design_transitions.py"


def tests_for(relative):
    """Which focused test file(s) prove the guards in ``relative``."""
    if relative == REGISTRY:
        return (REGISTRY_TESTS, CATALOG_TESTS)
    if relative == CATALOG:
        return (CATALOG_TESTS,)
    if relative == TRANSITIONS:
        return (TRANSITIONS_TESTS,)
    return (REGISTRY_TESTS,)


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


def _host_cannot_prove(label):
    """Whether ``label``'s only proof skips on this host.

    A skipped test is not a kill, so a host-dependent guard must never be
    silently counted as proven. Reporting it as HOST-SKIP keeps the ledger
    honest without pretending the property was verified here.
    """
    probe = subprocess.run(
        [sys.executable, "-m", "pytest", TRANSITIONS_TESTS, "-q", "--no-header",
         "-p", "no:cacheprovider", "-rs", "-k", "symlinked"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    return "skipped" in probe.stdout and " passed" not in probe.stdout


def main():
    baseline_code, tail = run_tests(ROOT, REGISTRY_TESTS)
    print(f"Part D baseline: exit={baseline_code} {tail}")
    if baseline_code != 0:
        return 1
    catalog_code, catalog_tail = run_tests(ROOT, CATALOG_TESTS)
    print(f"Parts F/G baseline: exit={catalog_code} {catalog_tail}")
    if catalog_code != 0:
        return 1
    transitions_code, transitions_tail = run_tests(ROOT, TRANSITIONS_TESTS)
    print(f"Part H baseline: exit={transitions_code} {transitions_tail}")
    if transitions_code != 0:
        return 1

    print()
    unproven = []
    host_skipped = []
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
                if label in HOST_DEPENDENT and _host_cannot_prove(label):
                    print(f"[HOST-SKIP]  {label} -- needs a host capability "
                          f"this machine lacks; NOT proven here")
                    host_skipped.append(label)
                else:
                    print(f"[SURVIVED]   {label} -- tests still pass without this guard")
                    unproven.append(f"{label}: tests still pass without this guard")
            elif "no tests ran" in tail or " errors in " in tail:
                print(f"[INVALID]    {label} -- {tail}")
                unproven.append(f"{label}: mutation invalidated collection ({tail})")
            else:
                print(f"[KILLED]     {label} -- {tail}")

    print()
    if host_skipped:
        print(f"{len(host_skipped)} guard(s) not provable on this host:")
        for item in host_skipped:
            print("  -", item)
        print()
    if unproven:
        print(f"{len(unproven)} guard(s) unproven:")
        for item in unproven:
            print("  -", item)
        return 1

    proven = len(MUTATIONS) - len(host_skipped)
    if host_skipped:
        print(f"{proven} of {len(MUTATIONS)} guards killed; "
              f"{len(host_skipped)} not provable on this host (see above)")
    else:
        print(f"all {len(MUTATIONS)} guards killed by the focused tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
