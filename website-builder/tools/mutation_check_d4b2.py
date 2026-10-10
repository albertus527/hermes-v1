"""D4b.2 mutation driver -- the multilingual-expansion load-bearing guards.

Same discipline as the D4a/D4a.1/D4b drivers: revert ONE guard at a time on a
THROWAWAY COPY and prove the focused D4b.2 tests go red. The working tree is
never edited.

Every mutation is chosen so that, with the guard removed, a SPECIFIC unsafe
behaviour becomes reachable:

   1. the opt-in flag defaults to DISABLED          -> disabled default lost
   2. the shipped config declares the flag disabled -> shipped default lost
   3. the Indonesian gate is removed                -> English briefs change
   4. the expansion stays strictly additive         -> base queries displaced
   5. English collisions are excluded from ID keys  -> English detected as ID
   6. config_from_mapping reads the flag            -> the flag is ignored
   7. Indonesian detection is real                  -> detection always fires

Run:  ./.venv/bin/python tools/mutation_check_d4b2.py
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

LAYA = "app/core/laya_context.py"
CONFIG = "config/default.yaml"

D4B2_TESTS = ("tests/test_d4b2_multilingual.py",)

MUTATIONS = [
    (
        "the opt-in multilingual flag defaults to DISABLED",
        LAYA,
        "    multilingual_expansion: bool = False",
        "    multilingual_expansion: bool = True",
    ),
    (
        "the shipped default config declares the flag disabled",
        CONFIG,
        "    multilingual_expansion: false",
        "    multilingual_expansion: true",
    ),
    (
        "the expansion only applies to an INDONESIAN brief",
        LAYA,
        """        if looks_indonesian(combined):
            gloss = _gloss_keywords(keywords)""",
        """        if True:
            gloss = _gloss_keywords(keywords)""",
    ),
    (
        "the expansion is strictly additive (base queries preserved as a prefix)",
        LAYA,
        """            if gloss:
                _add(" ".join(gloss))""",
        """            if gloss:
                queries[0] = " ".join(gloss)[: cfg.max_query_chars]""",
    ),
    (
        "English-word collisions are excluded from Indonesian evidence",
        LAYA,
        "_ID_ONLY_GLOSSARY_KEYS = frozenset(_ID_EN_GLOSSARY) - _ENGLISH_COLLISIONS",
        "_ID_ONLY_GLOSSARY_KEYS = frozenset(_ID_EN_GLOSSARY)",
    ),
    (
        "config_from_mapping reads the multilingual flag",
        LAYA,
        'multilingual_expansion=bool(data.get("multilingual_expansion", False)),',
        "multilingual_expansion=False,",
    ),
    (
        "Indonesian detection requires real evidence (not always-true)",
        LAYA,
        "    return bool(tokens & (_INDONESIAN_MARKERS | _ID_ONLY_GLOSSARY_KEYS))",
        "    return True",
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
    baseline_code, tail = run_tests(ROOT, D4B2_TESTS)
    print(f"D4b.2 baseline: exit={baseline_code} {tail}")
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

            code, tail = run_tests(work, D4B2_TESTS)
            if code == 0:
                print(f"[SURVIVED]   {label} -- tests still pass without this guard")
                unproven.append(f"{label}: tests still pass without this guard")
            elif "failed" in tail or "error" in tail:
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

    print(f"all {len(MUTATIONS)} guards killed by the focused D4b.2 tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
