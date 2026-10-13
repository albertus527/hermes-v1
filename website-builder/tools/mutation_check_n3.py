"""Mutation driver for N3 — Decision Engine & OpenViking.

Same discipline as the earlier ``mutation_check_*.py`` drivers: revert one guard
at a time on a THROWAWAY COPY and prove the focused tests go red. The real
working tree is never edited.

Each mutation reverts a guard whose absence produces a SPECIFICALLY wrong
outcome — an outage that breaks intake, an unprovenanced candidate that reaches
selection, a bogus score that is trusted, a forced selection, or an unbounded
retrieval:

  * intake-no-fallback        -> a provider outage escapes as an exception
  * design-no-fallback        -> a provider outage escapes as an exception
  * provenance-not-required   -> an untrusted candidate is selected
  * score-range-unchecked     -> an out-of-range score is trusted
  * nan-inf-not-rejected      -> a NaN/Inf score is trusted
  * floor-not-enforced        -> a below-floor candidate is forced as selected
  * forced-selection          -> the engine selects when it should abstain
  * bound-not-enforced        -> the candidate cap is ignored
  * rank-not-deterministic    -> equal scores depend on iteration order
  * ov-outage-not-empty       -> an OpenViking outage raises instead of abstaining
  * ov-provenance-not-checked -> an unprovenanced OpenViking item is emitted

Run:  python tools/mutation_check_n3.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv", "venv")

TESTS = "tests/test_decision_engine.py"
ENGINE = "app/core/decision_engine.py"
PROVIDER = "app/core/decision_provider.py"
OPENVIKING = "app/core/openviking_decision.py"

MUTATIONS = [
    (
        "an intake provider outage is caught and falls back",
        ENGINE,
        """        except Exception as exc:  # noqa: BLE001 - a provider must never break intake
            logger.warning(
                "Decision intake provider %r failed (%s); using deterministic fallback",
                provider.name,
                type(exc).__name__,
            )
            return self._fallback_intake(request_text, ctx, exc)""",
        """        except Exception as exc:  # noqa: BLE001
            raise""",
    ),
    (
        "a design provider outage is caught and falls back",
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
        "a candidate without provenance is dropped",
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
        "an out-of-range score is not trusted",
        ENGINE,
        """    if score < 0.0 or score > 1.0:
        return None""",
        """    if False:
        return None""",
    ),
    (
        "a NaN or infinite score is not trusted",
        ENGINE,
        """    if math.isnan(score) or math.isinf(score):
        return None""",
        """    if False:
        return None""",
    ),
    (
        "a candidate below the score floor is not accepted",
        ENGINE,
        """            if score is None or score < self.score_floor:
                dropped.append(
                    CandidateValidation(candidate.candidate_id, False, ABSTAIN_BELOW_FLOOR)
                )
                continue""",
        """            if score is None:
                dropped.append(
                    CandidateValidation(candidate.candidate_id, False, ABSTAIN_BELOW_FLOOR)
                )
                continue""",
    ),
    (
        "the engine abstains rather than forcing a selection",
        ENGINE,
        """        if not accepted:
            reason = (""",
        """        if False:
            reason = (""",
    ),
    (
        "retrieval is bounded by the hard candidate ceiling",
        ENGINE,
        """        self.max_candidates = max(0, min(int(max_candidates), MAX_CANDIDATES))""",
        """        self.max_candidates = max(0, int(max_candidates))""",
    ),
    (
        "ranking is deterministic for equal scores",
        ENGINE,
        """        ranked = tuple(sorted(accepted, key=lambda c: (-c.score, c.candidate_id)))""",
        """        ranked = tuple(sorted(accepted, key=lambda c: -c.score))""",
    ),
    (
        "an OpenViking retrieval outage abstains instead of raising",
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
        "an OpenViking item without provenance is dropped",
        OPENVIKING,
        """        if not source_uri or not source_revision:
            return None""",
        """        if False:
            return None""",
    ),
    (
        "shadow mode never breaks intake when the engine raises",
        "app/core/intake.py",
        """            except Exception:  # pragma: no cover - shadow must never break intake
                self.last_shadow_decision = None""",
        """            except Exception:  # pragma: no cover
                raise""",
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
    print(f"N3 baseline: exit={code} {tail}")
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
