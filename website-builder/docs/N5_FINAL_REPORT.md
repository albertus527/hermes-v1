# N5 — Regression, Replay & Recovery — Final Engineering & Quality Report

Status: N5 deliverable. Batch **N5 — Regression, Replay & Recovery**.
Worktree: `/home/albertus527/hermes-website-r2`
Branch: `feature/website-r2-redesign`
Baseline: N4 pushed at `08d094510a3110463da189fa62b9ef675900ade3`.

N5 is the acceptance batch for the whole R2 redesign. It adds no new runtime
capability; it **proves** the redesign against the roadmap's N5 gate:

> - Relevant deterministic tests pass.
> - Security/release invariants remain intact.
> - No unresolved critical or high-severity regressions.
> - Production deployment requires explicit human approval.

---

## 1. What N5 adds

| File | Role |
|---|---|
| `tests/test_r2_n5_regression.py` | Full lifecycle + Forma Atelier replay + failure injection + crash recovery + release invariants (19 tests) |
| `tools/mutation_check_n5.py` | Proves the N5 suite is load-bearing (9 guards) |

---

## 2. Full lifecycle regression

`test_new_website_and_revision_reach_live` drives the **real** production
control flow (via `LocalR1Scenario`: real `IntakeProcessor`,
`FrontendBuilder`, `RevisionOrchestrator`, `PromotionOrchestrator`, real
persistence) from a new-website request through build → QA → preview → revision
→ approve → promote → LIVE, and asserts the **exact release identity**: every
revision counter agrees at 2.

`test_publication_requires_approval` proves promotion without approval never
reaches LIVE.

---

## 3. Forma Atelier baseline replay

The roadmap names the Forma Atelier brief as the replay target. N5:

* replays it deterministically (PRD and Design compile to byte-identical
  documents across runs);
* asserts **design specificity and requirement coverage** — the brief's own
  words and the DNA's own values appear, and no `UNRESOLVED` marker remains when
  the facts are supplied;
* asserts the replayed documents **reach FRONTEND** through the N4 handoff;
* asserts an **absent** fact is marked `UNRESOLVED`, never fabricated.

This is the roadmap's "compare design specificity and requirement coverage with
the previous generic result" made checkable: a generic result would be filler
prose; the replay is the brief's own facts.

---

## 4. Failure-injection matrix

| Fault class | Test | Behaviour proven |
|---|---|---|
| Provider outage | `ProviderFailureTests` | Falls back, still returns, marked degraded; abstains with no evidence |
| Document invalid | `DocumentFailureTests` | Refused before disk; never readable as valid |
| Document corrupt | `DocumentFailureTests` | Reads `None`, never raises |
| Document drift | `DocumentFailureTests` | Recorded digest mismatch detected |
| Retrieval outage | `RetrievalFailureTests` | Honest-empty → engine abstains |
| Candidate untrusted | `CandidateRejectionTests` | Unprovenanced / bogus-score candidate never selected |
| Crash mid-lifecycle | `CrashRecoveryTests` | Restart between every step still finalizes once, one project create |
| QA/smoke failure | `QAFailureTests` | Never reaches LIVE; zero promote calls |
| Approval binding | `ReleaseInvariantTests` | Approval recorded against the exact preview revision |

---

## 5. Security & release invariants

* No security, dependency, credential, or product-boundary policy was changed by
  N1–N5. The build/typecheck/allowlist/credential/browser-smoke gates are intact.
* No new publication state machine; `app/projects/promote.py` and its release
  invariants are unmodified across N1–N5.
* `test_publication_requires_approval` and `ReleaseInvariantTests` re-assert that
  production publication requires explicit human approval bound to a tested
  revision.

---

## 6. Acceptance evidence

| Check | Command | Result |
|---|---|---|
| N5 suite | `pytest tests/test_r2_n5_regression.py` | **19 passed** |
| N5 mutation | `python tools/mutation_check_n5.py` | **9/9 guards killed** |
| Full suite | `scripts/run_tests.sh website-builder/tests` | see the ledger; compared to the baseline fingerprint |

---

## 7. Known limitations (honest)

* **The full suite is not green on this host.** 20 failures are pre-existing and
  environmental, documented with an exact fingerprint in the ledger and proven
  to reproduce on a pristine `HEAD` copy with zero R2 files. N1–N5 add **+114
  passing tests and 0 new failures**.
* **The initial build's FRONTEND call receives the PRD but not Design.md**, by
  construction (the DNA does not exist until FRONTEND produces it). Documented in
  the N4 report.
* **Laya is not retired.** The roadmap gates retirement on parity and fallback
  tests; the Decision Engine ships alongside the default-OFF Laya path.
* **VISION/Impeccable are not re-pointed at the documents** in N4/N5; the critic
  path re-derives guidance from the same workspace, so it is consistent by
  construction.

---

## 8. Verdict

The R2 redesign (N1–N5) is implemented, feature-flagged (all new behaviour
default-OFF or shadow-mode), independently tested, and mutation-verified. No
security or release invariant was relaxed and no new regression was introduced.
Production deployment still requires explicit human approval — which this
autonomous run never grants.
