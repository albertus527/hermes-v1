"""Mutation driver for N5 — Regression, Replay & Recovery.

N5 is an acceptance batch, so its mutations prove the *tests* are load-bearing:
each reverts a real R2 guard and the N5 regression suite must go red. Same
throwaway-copy discipline as the other ``mutation_check_*.py`` drivers.

Run:  python tools/mutation_check_n5.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv", "venv")

TESTS = "tests/test_r2_n5_regression.py"
ENGINE = "app/core/decision_engine.py"
PROVIDER = "app/core/decision_provider.py"
OPENVIKING = "app/core/openviking_decision.py"
STORE = "app/core/document_store.py"
CREATOR = "app/core/requirement_creator.py"

MUTATIONS = [
    (
        "the engine falls back on a provider outage",
        ENGINE,
        """        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Decision design provider %r failed (%s); using deterministic fallback",
                provider.name,
                type(exc).__name__,
            )
            fallback = self.registry.fallback""",
        """        except Exception as exc:  # noqa: BLE001
            raise
            fallback = self.registry.fallback""",
    ),
    (
        "the engine abstains when there is no evidence",
        ENGINE,
        """        if not accepted:
            reason = (""",
        """        if False:
            reason = (""",
    ),
    (
        "an unprovenanced candidate is dropped",
        ENGINE,
        """            if not candidate.has_provenance:
                dropped.append(
                    CandidateValidation(candidate.candidate_id, False, ABSTAIN_NO_PROVENANCE)
                )
                continue""",
        """            if False:
                dropped.append(
                    CandidateValidation(candidate.candidate_id, False, ABSTAIN_NO_PROVENANCE)
                )
                continue""",
    ),
    (
        "a NaN score is not trusted",
        ENGINE,
        """    if math.isnan(score) or math.isinf(score):
        return None""",
        """    if False:
        return None""",
    ),
    (
        "an OpenViking outage abstains instead of raising",
        OPENVIKING,
        """        except Exception as exc:  # noqa: BLE001 - fail closed, never raise
            logger.warning(
                "OpenViking decision retrieval failed (%s); abstaining",
                type(exc).__name__,
            )
            return ()""",
        """        except Exception as exc:  # noqa: BLE001
            raise""",
    ),
    (
        "an invalid document is refused before disk",
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
    ),
    (
        "a corrupt document reads back as None",
        STORE,
        """    except (OSError, UnicodeDecodeError):
        return None""",
        """    except OSError:
        return None""",
    ),
    (
        "a recorded document digest mismatch is detected",
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
    ),
    (
        "a missing brief fact becomes UNRESOLVED, never fabricated",
        CREATOR,
        """    requirements = _bullet_block(
        [what, why], empty=f"- {UNRESOLVED_MARKER}: FAST supplied no what/why"
    )""",
        """    requirements = _bullet_block(
        [what, why], empty="- a modern, welcoming website"
    )""",
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
    code, tail = run_tests(ROOT, TESTS)
    print(f"N5 baseline: exit={code} {tail}")
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
            print(f"[ANCHOR-MISS] {label} (replacement was a no-op)")
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
