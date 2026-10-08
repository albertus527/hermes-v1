"""Mutation driver for D3a.5 Part C (multi-dimensional capability model).

Same discipline as the D0/D1/D2-D3a/Part-AB drivers: revert one guard at a time
on a THROWAWAY COPY and prove the focused tests go red. The real working tree is
never edited.

Every mutation corresponds to a guard whose absence produces a SPECIFICALLY
wrong outcome:

  * enum-collapse                 -> a resource with two true capabilities is
                                     forced to report one, discarding a truth
  * free-search-gated             -> 21st reports NO discovery while its free
                                     metadata search demonstrably works
  * auth-gates-everything         -> a designed free tier is reported as a
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
ACTIVATION = "app/core/design_activation.py"

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
    # --- 1b. the 21st capability is credential-gated on BOTH axes ---------
    # The module docstring once claimed a free 21st search surface; the verified
    # REST/OpenAPI surface (global ApiKeyAuth, every endpoint 401) disproves it.
    (
        "21st is credential-gated on both discovery and retrieval",
        ACTIVATION,
        """            metadata_without_auth=False,
            retrieval_requires_auth=True,
            credential_names=CREDENTIAL_ENV_NAMES["twenty_first"],""",
        """            metadata_without_auth=True,
            retrieval_requires_auth=True,
            credential_names=CREDENTIAL_ENV_NAMES["twenty_first"],""",
    ),
    # --- 2. an UNCREDENTIALED catalog is not gated by the credential -----
        # A source whose catalog is listable WITHOUT a credential (React Bits)
        # must report discovery even with NO credential present. Gating discovery
        # on `auth_present` would make a working open catalog report itself
        # absent. (21st is credential-gated and passes metadata_without_auth=False,
        # so this branch is React Bits' alone.)
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
        """    index_endpoints = getattr(_fetch, "CATALOG_ENDPOINTS", None) or {}
    search_endpoints = getattr(_fetch, "CATALOG_SEARCH_ENDPOINTS", None) or {}
    return source in index_endpoints or source in search_endpoints""",
        """    index_endpoints = getattr(_fetch, "CATALOG_ENDPOINTS", None) or {}
    search_endpoints = getattr(_fetch, "CATALOG_SEARCH_ENDPOINTS", None) or {}
    return bool(index_endpoints or search_endpoints)""",
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
    baseline_code, tail = run_tests(ROOT, ACTIVATION_TESTS)
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

            code, tail = run_tests(work, ACTIVATION_TESTS)
            if code == 0:
                print(f"[SURVIVED]   {label} -- tests still pass without this guard")
                unproven.append(f"{label}: tests still pass without this guard")
            elif "no tests ran" in tail:
                # A collection failure must never count as a kill: it proves
                # the tests did not execute, which invalidates the mutation
                # rather than demonstrating the guard is load-bearing.
                print(f"[INVALID]    {label} -- {tail}")
                unproven.append(f"{label}: mutation invalidated collection ({tail})")
            elif " errors in " in tail:
                # A nonzero ERROR count means collection did not fully
                # succeed. The baseline is verified to have zero, so any
                # errors here were introduced by the mutation itself.
                print(f"[INVALID]    {label} -- collection errors: {tail}")
                unproven.append(f"{label}: mutation introduced collection errors ({tail})")
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
