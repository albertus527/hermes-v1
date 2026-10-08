"""Mutation driver for D3a.5 Part C (multi-dimensional capability model).

Same discipline as the D0/D1/D2-D3a/Part-AB drivers: revert one guard at a time
on a THROWAWAY COPY and prove the focused tests go red. The real working tree is
never edited.

Every mutation corresponds to a guard whose absence produces a SPECIFICALLY
wrong outcome:

  * enum-collapse                 -> a resource with two true capabilities is
                                     forced to report one, discarding a truth
  * uncredentialed-catalog-gated  -> React Bits reports NO discovery while its
                                     open (no-credential) catalog works
  * auth-gates-everything         -> a designed open baseline is reported as a
                                     degradation, training operators to ignore
                                     real ones
  * credential-value-leaks        -> a secret reaches the capability report
  * blank-credential-counts       -> an empty credential claims a capability
  * manifest-declares-capability  -> the anti-placeholder property is lost
  * engine-not-verified           -> Impeccable claims a critic with no binary
  * engine-missing-downloads      -> a build downloads an executable into $HOME
  * engine-from-manifest          -> a platform literal decides the engine path
  * reason-outside-closed-set     -> callers get an unhandled state

Run:  python tools/mutation_check_d3a5_partc.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Tuple

ROOT = Path(__file__).resolve().parents[1]
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv", "venv")

ACTIVATION_TESTS = "tests/test_design_activation.py"
#: The cross-layer coherence suite carries the item-2 regression: capability
#: truth must MATCH the adapter's execution requirement. It is run alongside the
#: activation tests so a drift between the two layers is caught here.
COHERENCE_TESTS = "tests/test_design_resource_coherence.py"
#: Suite hygiene: no test file may define the same test name twice (a duplicate
#: silently shadows every earlier body). Carried here so the guard is proven.
HYGIENE_TESTS = "tests/test_suite_hygiene.py"
#: Suite offline guard: no default-suite test may reach the network. Carried
#: here so the guard is proven.
OFFLINE_TESTS = "tests/test_suite_offline.py"
#: Part M: the FINAL PROOF runner's own self-checks (pins match the drivers).
FINAL_PROOF_TESTS = "tests/test_final_proof_runner.py"
#: The runner itself (mutated by guard #23 to prove the pin cross-check).
FINAL_PROOF_RUNNER = "tools/d3a5_final_proof.py"
#: Part N: the source-review bullets, re-verified as live properties.
BULLETS_TESTS = "tests/test_source_review_bullets.py"
RUN_TESTS = (ACTIVATION_TESTS, COHERENCE_TESTS, HYGIENE_TESTS, OFFLINE_TESTS,
             FINAL_PROOF_TESTS, BULLETS_TESTS)
ACTIVATION = "app/core/design_activation.py"
CATALOG_FETCH = "app/core/design_catalog_fetch.py"
RESOURCES = "app/core/design_resources.py"
#: The audit doc records the manual LIVE smokes (Part L). The doc-rot guard in
#: the coherence suite resolves every symbol those commands import, so mutating
#: the doc to name a bogus symbol must fail the suite.
AUDIT_DOC = "docs/D3A5_DEPENDENCY_INGRESS_AUDIT.md"

MUTATIONS = [
    # --- 1. the model is multi-dimensional, not an enum -------------------
    # The mutation must change something OBSERVABLE, so it collapses the axes
    # inside `to_dict` (the surface D2 and the report actually read) rather
    # than adding an unused enum-shaped property, which would be dead code and
    # would prove nothing.
    (
        "a resource reports every capability axis it has",
        ACTIVATION,
        """        return {
            "resource_id": self.resource_id,
            "discovery_available": self.discovery_available,
            "retrieval_available": self.retrieval_available,
            "install_available": self.install_available,
            "critic_available": self.critic_available,""",
        """        # Enum collapse: report ONE state chosen by priority, discarding the
        # other true capabilities. This is the lie the multi-dimensional model
        # exists to avoid.
        _one = "absent"
        if self.critic_available:
            _one = "critic"
        elif self.install_available:
            _one = "install"
        elif self.retrieval_available:
            _one = "retrieval"
        elif self.discovery_available:
            _one = "discovery"
        return {
            "resource_id": self.resource_id,
            "capability": _one,
            "discovery_available": _one == "discovery",
            "retrieval_available": _one == "retrieval",
            "install_available": _one == "install",
            "critic_available": _one == "critic",""",
    ),
    # --- 0z. discovery needs a BUILDABLE request, not a key ---------------
    # Testing key membership reports discovery available for a None-valued
    # endpoint (nothing to fetch): discovery APPEARS available.
    (
        "the adapter probe asks for a buildable URL, not a key",
        ACTIVATION,
        """    try:
        return builder(source) is not None
    except Exception:  # pragma: no cover - a broken builder is not a capability
        return False""",
        """    index_endpoints = getattr(_fetch, "CATALOG_ENDPOINTS", None) or {}
    search_endpoints = getattr(_fetch, "CATALOG_SEARCH_ENDPOINTS", None) or {}
    return source in index_endpoints or source in search_endpoints""",
    ),
    # --- 0a. the official credential env names are recognised ------------
    # 21st's own skill documents TWENTYFIRST_TOKEN / API_KEY_21ST. Dropping them
    # makes a user who follows upstream's docs a false negative.
    (
        "the officially documented 21st credential names are recognised",
        RESOURCES,
        """    "twenty_first": (
        "TWENTYFIRST_TOKEN",
        "API_KEY_21ST",
        "TWENTY_FIRST_API_KEY",
        "TWENTYFIRST_API_KEY",
    ),""",
        """    "twenty_first": (
        "TWENTY_FIRST_API_KEY",
        "TWENTYFIRST_API_KEY",
    ),""",
    ),
    # --- 0b. the free-tier claim is SURFACE-SCOPED, not absolute ----------
    # 21st DOES advertise a free allowance (Web/CLI/MCP); the REST surface this
    # application calls is credential-gated. "no free tier" absolutely would be
    # its own inaccuracy.
    (
        "the free-tier claim is surface-scoped, not absolute",
        ACTIVATION,
        """  schema. 21st does advertise a free allowance, but for the Web/CLI/MCP
  surfaces, which this application never calls -- so on the REST surface there
  is no unauthenticated discovery or retrieval to report.""",
        """  schema. There is no free tier to report.""",
    ),
    # --- 1a. the MANIFEST comment must not claim a free 21st tier ---------
    # The resource manifest is the contract; its comment must agree with the
    # verified REST surface (audit question 9: docs vs executable policy).
    (
        "the manifest comment does not claim a free 21st tier",
        "config/design_resources.yaml",
        """    # D3a.5 (verified live 2026-10): 21st's ONLY machine surface is the""",
        """    # D3a.5: metadata SEARCH is real and works with no credential (the free""",
    ),
    # --- 1b. the 21st capability is credential-gated on BOTH axes ---------
    # The requirement is DERIVED from the adapter's tables. Ignoring the
    # derivation and asserting "no credential needed" flips the capability to a
    # free-discovery claim the adapter contradicts.
    (
        "21st is credential-gated on both discovery and retrieval",
        ACTIVATION,
        """    discovery_requires_auth, retrieval_requires_auth = _fetch.credential_requirement(
        resource_id
    )""",
        """    discovery_requires_auth, retrieval_requires_auth = (False, False)""",
    ),
    # --- 2. an UNCREDENTIALED catalog is not gated by the credential -----
        # A source whose catalog is listable WITHOUT a credential (React Bits)
        # must report discovery even with NO credential present. Gating discovery
        # on `auth_present` would make a working open catalog report itself
        # absent. (21st is credential-gated, so this branch is React Bits' alone.)
    (
        "an uncredentialed catalog stays available without a credential",
        ACTIVATION,
        """    discovery = adapter_exists and (metadata_without_auth or auth_present)""",
        """    discovery = adapter_exists and metadata_without_auth and auth_present""",
    ),
    # --- 3. a designed free tier is not a degradation --------------------
    (
        "an absent paid tier does not degrade a working free baseline",
        ACTIVATION,
        """        degraded=not discovery,""",
        """        degraded=True,""",
    ),
    # --- 3b. the free baseline is never reported as credential-blocked --
        # Found by Part L, not by Part C: a PROVISIONED Refero skill reported
        # `authentication_required=True` because it merely DECLARED an optional
        # paid credential. That claims a blocker which does not exist, and a
        # caller reading `usable` beside it could withhold a working resource.
    (
        "a provisioned free baseline is not reported as credential-blocked",
        ACTIVATION,
        """        authentication_required=False,
        authentication_optional=bool(paid_credential) and not auth_present,""",
        """        authentication_required=bool(paid_credential),
        authentication_optional=bool(paid_credential) and not auth_present,""",
    ),
    # --- 4. presence is a boolean, never the value -----------------------
    (
        "a credential value never reaches the capability report",
        ACTIVATION,
        """    env = os.environ if source is None else source
    for name in names:
        value = env.get(name)
        if isinstance(value, str) and value.strip():
            return True
    return False""",
        """    env = os.environ if source is None else source
    for name in names:
        value = env.get(name)
        if isinstance(value, str):
            return value
    return False""",
    ),
    # --- 5. an empty credential counts as absent ------------------------
    (
        "a blank credential is treated as absent",
        ACTIVATION,
        """        if isinstance(value, str) and value.strip():
            return True
    return False""",
        """        if isinstance(value, str):
            return True
    return False""",
    ),
    # --- 5b. the adapter probe is PER-SOURCE ----------------------------
    # A source-agnostic check would confer one source's capability on another.
    (
        "the discovery adapter probe is per-source",
        ACTIVATION,
        """    try:
        return builder(source) is not None
    except Exception:  # pragma: no cover - a broken builder is not a capability
        return False""",
        """    try:
        return builder(source) is not None or builder(
            next(iter(getattr(_fetch, "CATALOG_SEARCH_ENDPOINTS", {}) or {"_": "_"}))
        ) is not None
    except Exception:  # pragma: no cover
        return False""",
    ),
    # --- 6. declared does not mean capable --------------------------------
    (
        "a declared-but-unactivated resource claims no capability",
        ACTIVATION,
        """    # Generic path: declared but not activated. Honest absence, never a
    # capability inferred from the declaration itself.
    return _absent(resource_id, [REASON_NO_OFFICIAL_MECHANISM])""",
        """    # Generic path: declared but not activated.
    return ResourceActivationCapability(
        resource_id=resource_id,
        discovery_available=True,
        retrieval_available=True,
        reasons=(REASON_NO_OFFICIAL_MECHANISM,),
    )""",
    ),
    # --- 7. an absent optional never fails startup ----------------------
    (
        "an optional absent resource never fails the report",
        ACTIVATION,
        """        if capability.usable:
            continue
        if resource.required:
            failures.append(resource_id)""",
        """        if capability.usable:
            continue
        if True:
            failures.append(resource_id)""",
    ),
    # --- 8. the engine is verified before the critic is claimed ----------
        # The full-quality return is only reachable when `engine_quality` is
        # "full". A mutation that claims the critic on any OTHER quality makes
        # the "full-quality critic" test fail.
    (
        "a critic requires a verified engine binary",
        ACTIVATION,
        """    return ResourceActivationCapability(
        resource_id=resource_id,
        discovery_available=True,
        retrieval_available=True,
        critic_available=True,
        critic_degraded=False,""",
        """    return ResourceActivationCapability(
        resource_id=resource_id,
        discovery_available=True,
        retrieval_available=True,
        critic_available=quality != "full",
        critic_degraded=False,""",
    ),
    # --- 9. a missing engine never triggers a download ------------------
        # The engine-resolution guard is now `engine_quality(...) == "missing"`.
        # Removing it must not fall through to a download.
    (
        "a missing engine degrades honestly instead of downloading",
        ACTIVATION,
        """    quality = engine_quality(skill_root)

    if quality == "missing":
        return ResourceActivationCapability(
            resource_id=resource_id,
            discovery_available=True,
            retrieval_available=False,
            locally_provisioned=True,""",
        """    quality = engine_quality(skill_root)

    if quality == "missing":
        import urllib.request

        urllib.request.urlopen(f"https://example.invalid/{resource_id}")
        return ResourceActivationCapability(
            resource_id=resource_id,
            discovery_available=True,
            retrieval_available=True,
            critic_available=True,
            locally_provisioned=True,""",
    ),
    # --- 10. the engine layout is the verified cross-platform chain ------
    #
    # Upstream ships NO native binary and NO per-platform directory, so the
    # engine is one Node ESM entrypoint on every host. The mutation restores
    # the fabricated per-OS layout the previous revision assumed.
    (
        "the engine layout is the verified cross-platform entrypoint chain",
        ACTIVATION,
        """ENGINE_ENTRYPOINTS: Tuple[str, ...] = (
    "scripts/detect.mjs",
    "scripts/detector/detect-antipatterns.mjs",
)""",
        """ENGINE_ENTRYPOINTS: Tuple[str, ...] = (
    "scripts/bin/linux-x64/impeccable",
    "scripts/detector/detect-antipatterns.mjs",
)""",
    ),
    # --- 11. a missing engine file never yields a path -----------------
    #
    # There is no per-platform mapping to escape now, because upstream ships
    # no native binary. The equivalent property is that an absent engine is an
    # honest None rather than a fabricated path.
    (
        "a missing engine file never yields a path",
        ACTIVATION,
        """        if not _is_readable_file(path):
            return None""",
        """        if not _is_readable_file(path):
            fabricated = root / 'scripts/bin/linux-x64/impeccable'
            return fabricated""",
    ),
    # --- 12. the reason vocabulary stays closed -------------------------
    (
        "an unregistered reason is rejected at construction",
        ACTIVATION,
        """        unknown = [r for r in self.reasons if r not in ACTIVATION_REASONS]
        if unknown:
            raise ValueError(f"unregistered activation reason(s): {sorted(unknown)}")""",
        """        pass""",
    ),
    # --- 13. a placeholder skill directory is not a capability ----------
    (
        "a skill missing its declared artifacts is not a capability",
        ACTIVATION,
        """    for entry in resource.data_entries:
        entry_path = skill_dir / entry
        if not _is_readable_file(entry_path):
            return True, False""",
        """    for entry in resource.data_entries:
        entry_path = skill_dir / entry
        if not entry_path.exists():
            return True, False""",
    ),
    # --- 14. on-demand resources are installable, not degraded ----------
    (
        "an on-demand resource reports install availability, not absence",
        ACTIVATION,
        """    return ResourceActivationCapability(
        resource_id=resource_id,
        install_available=install_available,
        reasons=(),
    )""",
        """    return ResourceActivationCapability(
        resource_id=resource_id,
        install_available=False,
        degraded=True,
        reasons=(),
    )""",
    ),
    # --- 14b. the documented 21st discovery names the real authenticated surface
    # If the doc stops naming the credential requirement / the real endpoint, a
    # reader following it would try an unauthenticated call that can only 401.
    (
        "the documented 21st discovery names the credential requirement",
        AUDIT_DOC,
        "21st REAL discovery, with a credential when required",
        "21st discovery (free, no credential needed)",
    ),
    # --- 14c. the documented React Bits command states discovery != installability
    # If the doc stops saying so, a reader could mistake a discovered component
    # for an installable one -- the exact conflation the reviewed contract prevents.
    (
        "the documented React Bits command keeps discovery != installability",
        AUDIT_DOC,
        "**Discovery is not installability.** All 64 upstream components are discovered",
        "**Discovery means installable.** All 64 upstream components are discovered",
    ),
    # --- 14d. the documented fetch layer proves the bounds, not just the payload
    # If the doc stops stating that the bounds are ENFORCED (checked at the edge),
    # a reader could trust a declared-but-unenforced bound.
    (
        "the documented fetch layer states the bounds are enforced",
        AUDIT_DOC,
        "print('--- size bound is ENFORCED (not just declared) ---')",
        "print('--- size bound (declared) ---')",
    ),
    # --- 14e. the documented registry-JSON command states who fetches what
    # If the doc implies the APP fetches the registry JSON, a reader would look
    # for an HTTP client that does not exist -- the real boundary is post-hoc.
    (
        "the documented registry JSON command states who fetches",
        AUDIT_DOC,
        "**Who fetches what.** The app does NOT read this JSON.",
        "**Who fetches what.** The app reads this JSON.",
    ),
    # --- 14f. the documented disposable-starter command proves the build
    # If the doc stops proving the RESULT builds (and that the refusal is
    # load-bearing), a reader could read "installed" as "and it compiles".
    (
        "the documented disposable-starter command proves the build",
        AUDIT_DOC,
        "**The install is REFUSED when tampered — and the refusal is load-bearing.**",
        "**The install is REFUSED when tampered.**",
    ),
    # --- 15. Part L: the documented no-delta proof must be able to FAIL -----
    # The doc must not document a self-comparison as the "unchanged" proof:
    # `git diff --no-index package.json package.json` compares a file to itself,
    # so it exits 0 with empty output no matter what changed. Mutate the doc to
    # reintroduce it; the doc-integrity test must fail.
    (
        "the documented no-delta proof can actually fail",
        AUDIT_DOC,
        'cmp -s "$BASELINE" package.json \\\n  && echo "package.json UNCHANGED (no dependency delta)"',
        'git diff --no-index package.json package.json   # unchanged',
    ),
    # --- 16. Part L: the documented LIVE smokes stay runnable --------------
    # The doc records manual smoke commands. If a command names a symbol that no
    # longer exists, a reader following the doc hits an ImportError -- so the
    # doc-rot guard must fail. Mutate a documented import to a bogus name.
    (
        "the documented live smokes name real symbols",
        AUDIT_DOC,
        "from app.core.design_catalog_fetch import discover_catalog as d",
        "from app.core.design_catalog_fetch import discover_catalog_renamed as d",
    ),
    # --- 17. the documented FINAL exact-state command names the governed rule
    # The command's whole point is that the guard reasons over the GOVERNED set,
    # not just the CLI's delta. If the doc drops that, the reader cannot tell a
    # surviving range from a normalized pin. Mutate the doc's stated rule.
    (
        "the documented final exact-state command states the governed-set rule",
        AUDIT_DOC,
        "over the **governed** set, not just the CLI's delta",
        "over the introduced set",
    ),
    # --- 18. the documented typecheck command records the starter-dep fix
    # The command's value is that it installs the builtins whose source imports a
    # starter runtime dependency. If the doc drops the defect it fixed, a reader
    # cannot tell why those components were previously refused.
    (
        "the documented typecheck command records the starter-dep false-refusal fix",
        AUDIT_DOC,
        "was absent from the always-provided set",
        "was absent from the reviewed set",
    ),
    # --- 19. the documented build command records the shadow-config fix
    # The build's toolchain guard must reject a shadowing config. If the doc
    # drops that, a reader cannot tell the guard is broader than a by-name hash.
    (
        "the documented build command records the shadow-config fix",
        AUDIT_DOC,
        "FORBIDDEN_TOOLCHAIN_FILES",
        "the toolchain file list",
    ),
    # --- 20. the documented transitions command records the reserved-slug fix
    # The bulk selectors must be refused structurally. If the doc drops that, a
    # reader cannot tell `add all` is refused at the vocabulary, not by fixture.
    (
        "the documented transitions command records the reserved-slug fix",
        AUDIT_DOC,
        "refused at the slug VOCABULARY (`RESERVED_RECIPE_SLUGS`)",
        "refused by the catalog",
    ),
    # --- 21. the suite-hygiene scanner actually detects a duplicate
    # A duplicate `def test_x` silently shadows every earlier body. If the
    # scanner returns [] unconditionally, the guard is vacuous. Mutate it so it
    # never reports a duplicate; the self-proof test must fail.
    (
        "the duplicate-test-name scanner actually detects a duplicate",
        HYGIENE_TESTS,
        """    for name in names:
        seen[name] = seen.get(name, 0) + 1
    return sorted(name for name, count in seen.items() if count > 1)""",
        """    return []""",
    ),
    # --- 21b. the invalid-escape scanner actually detects one
    # A non-raw string with `\s` is a SyntaxWarning today and a SyntaxError in a
    # future Python. If the scanner returns [] unconditionally the guard is
    # vacuous. Mutate it so it never reports a finding; the self-proof must fail.
    (
        "the invalid-escape scanner actually detects one",
        HYGIENE_TESTS,
        """        for item in caught:
            if issubclass(item.category, SyntaxWarning):
                found.append((item.lineno or 0, str(item.message)))
    return found""",
        """        pass
    return []""",
    ),
    # --- 22. the offline guard actually detects a network-reaching test
    # If the scanner never reports an offender, a test that shells out to npm
    # would slip back into the default suite. Mutate the reachability check so
    # it never fires; the self-proof test must fail.
    (
        "the offline guard actually detects a network-reaching test",
        OFFLINE_TESTS,
        """        if reaches(node, set()):
            offenders.append((node.name, node.lineno))""",
        """        if False:
            offenders.append((node.name, node.lineno))""",
    ),
    # --- 23. the FINAL PROOF runner's pinned counts are cross-checked
    # "all N guards killed" is self-referential: delete a mutation and the
    # sentence stays true. The runner pins N externally; if the pin drifts from
    # the driver's real count, the proof must FAIL. Mutate a pin to a wrong
    # number; the self-check test must catch it.
    (
        "the final-proof runner pins match the drivers",
        FINAL_PROOF_RUNNER,
        '''    ("mutation_check_d3a5_parta.py", 16),''',
        '''    ("mutation_check_d3a5_parta.py", 99),''',
    ),
    # --- 24. the source-review bullets are re-verified as LIVE properties
    # Part N. The bullets file pins e.g. "401 -> auth rejected, 429 -> rate
    # limited". Collapse 429 into the generic reason; the bullet test must fail,
    # proving the re-verification is load-bearing rather than a doc restatement.
    (
        "the source-review bullets are enforced on HEAD",
        CATALOG_FETCH,
        """    if status == 429:
        return REASON_RATE_LIMITED""",
        """    if status == 429:
        return REASON_BAD_STATUS""",
    ),
    # --- 25. the doc does not claim a socket ban the suite does not have
    # The default suite is offline (no non-loopback network) but the
    # port-allocation tests bind LOOPBACK sockets, so a blanket "bans sockets"
    # claim is false. Reintroduce the stale wording; the coherence doc-integrity
    # test must fail.
    (
        "the doc does not claim a socket ban the suite lacks",
        AUDIT_DOC,
        "The default suite is **offline** (no non-loopback network), enforced by",
        "The suite bans sockets (`socket.socket.connect`, `create_connection`), enforced by",
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
    baseline_code, tail = run_tests(ROOT, RUN_TESTS)
    print(f"Part C baseline: exit={baseline_code} {tail}")
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

            code, tail = run_tests(work, RUN_TESTS)
            if code == 0:
                print(f"[SURVIVED]   {label} -- tests still pass without this guard")
                unproven.append(f"{label}: tests still pass without this guard")
            elif "failed" in tail:
                # A real FAILED test is the ONLY outcome that proves a guard is
                # load-bearing. pytest's summary line is the discriminator.
                print(f"[KILLED]     {label} -- {tail}")
            else:
                # No test produced a pass/fail outcome: a collection error
                # (pytest prints "1 error in" SINGULAR or "N errors in" plural)
                # or "no tests ran". The mutation proved NOTHING, so it must not
                # count as a kill.
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
