# Hermes R2.8.1 — Human Calibration Reviewer

A minimal, offline, loopback-only web tool for **one authorized human
reviewer** to independently label historical news headlines toward the
`§11.4` calibration requirement of
`docs/specs/HERMES_FABLE_R2_8_1_CANONICAL_BACKTEST_SPECIFICATION.md`.

It is a **review workflow, not a trading-strategy change**. It does not touch
production trading logic, G7 rules, confidence semantics, keyword fallback,
the published news cache, or any historical calibration CSV.

---

## 1. What it does

- Reads one historical headline at a time from the three calibration datasets
  (read-only).
- Lets the reviewer select `category`, `direction`, `severity`, `ma_role` by
  clicking options, plus certainty, notes, and a flag.
- Saves incomplete drafts; only an **explicit lock** counts as reviewed.
- Enforces **strict server-side blind review**: prior consensus labels, notes,
  selection cues, and model predictions are never sent to the browser before
  a label is locked.
- Allows **opt-in, post-lock reveal** of (a) prior LLM consensus and
  (b) the pinned GPT-6 Luna prediction.
- Supports **revision of locked labels** with a mandatory reason and an
  append-only audit ledger; revisions after reveal are tagged.
- Exports deterministic CSV/JSON (labels, progress, provenance, integrity)
  **without writing files** and **without claiming** `HUMAN_ACCEPTED` or
  `PHASE_2_PASS`.

Datasets are always reported separately:

| ID | Name | Rows | Role |
|---|---|---|---|
| `A_CANONICAL_240` | Canonical 240 (Dataset A) | 240 | canonical §11.4 population |
| `B_SAFETY_99` | Safety challenge 99 (Dataset B) | 99 | separate safety evaluation |
| `C_SUPPLEMENTAL_36` | Supplemental direct-event 36 (Dataset C) | 36 | separate safety evaluation |

The existing 375 labels are treated as **`LLM_CONSENSUS_SPOT_CHECKED`**, not as
independently human-adjudicated ground truth.

---

## 2. Startup and browser access

```bash
# from the repository root, with the project venv active
python -m backtest.calibration_reviewer --port 8765
```

The server binds **only** to `127.0.0.1` (there is no public-bind option). On
start it prints a **one-time pairing code** to the terminal. Open
`http://127.0.0.1:8765/` in a local browser and enter that code once; the
server then issues an `HttpOnly`, `SameSite=Strict` session cookie.

### Remote access (SSH port-forward)

```bash
ssh -L 8765:127.0.0.1:8765 <user>@<vps>
# then browse http://127.0.0.1:8765/ locally
```

### Options

| Flag | Meaning |
|---|---|
| `--port` | loopback port (default 8765) |
| `--db-path` | reviewer SQLite path (default `~/.hermes/backtest/calibration/reviewer/reviewer.sqlite3`) |
| `--cache-db` | production cache, opened **read-only** for model reveal |
| `--no-cache` | disable model-prediction reveal entirely |

Stop with `Ctrl-C`. Restarting generates a fresh pairing code; existing
sessions remain valid until revoked (see §5).

---

## 3. Rubric approval workflow

The rubric is `PROPOSED — PENDING HUMAN APPROVAL`, version
`r281-reviewer-rubric-0.1`.

- Labels can be **drafted and locked** at any time.
- A locked label counts toward **canonical §11.4 progress only if the rubric
  was approved before that lock**.
- Approval is recorded via the Rubric panel (`POST /api/rubric/approve`) with
  an optional note and appears in the append-only audit ledger.
- Before approval, locked labels are fully recorded and auditable but shown as
  **provisional** (`rubric_approved = 0`), and are excluded from
  `canonical_locked`.

---

## 4. Data isolation and integrity

- Reviewer state lives in a **new** SQLite DB under
  `~/.hermes/backtest/calibration/reviewer/`.
- A hard guard (`schema.assert_isolated_path`) refuses to open the production
  store (`~/.hermes/backtest/backtest.sqlite3`) as the reviewer DB, and
  refuses to create the reviewer DB directly in the production backtest root.
- Source CSVs are opened `rb` only; their exact SHA-256 is stored at
  initialization. On restart the loader re-verifies the hash **and** the
  sample identity (ids, order, hashes); any change fails closed — it never
  silently reimports, reorders, or overwrites reviewed samples.
- The production cache is opened only in SQLite read-only URI mode
  (`mode=ro`, `PRAGMA query_only=1`).

**Untouched:** `~/.hermes/backtest/backtest.sqlite3`, all calibration CSVs and
their `.meta.json`, all benchmark reports, and every production module
(`trading_core/*`, `backtest/news/*`, `backtest/db/*`).

---

## 5. Authentication & blinding guarantees

**Threat model (single user, loopback only).** Localhost binding is *not*
treated as authentication. A one-time pairing code is printed to the operator's
terminal only — never placed in a URL, log, or artifact. The browser exchanges
it for an opaque server-side session. **Limitation:** any process that can read
the operator's terminal stdout, or the same OS user's files, can bootstrap a
session. This is acceptable only because access is loopback + SSH tunnel.

- Session cookie: `HttpOnly; SameSite=Strict`; value is a 256-bit random token
  whose SHA-256 is the stored session id.
- State-changing requests require the CSRF token in the `X-CSRF-Token` header
  (never a URL query string) plus an Origin check.
- All responses are `no-store` with a restrictive CSP; no CDN/remote assets.
- Request bodies are size-capped (64 KiB); notes are length-capped.
- Static file serving rejects path traversal and non-whitelisted files.

**Blind review is enforced server-side.** Before a sample is locked, no
response contains the prior consensus label, `human_notes`, model prediction
or confidence, `challenge_reason`, `candidate_dimension`,
`direct_event_evidence`, or mismatch flags. This holds for `/api/sample`,
every export, HTML/JS bundles, and after refresh or session recreation.

**Revoke / reinitialize a session:** restarting the server rotates the pairing
code; to invalidate all sessions immediately, call
`app.security.revoke_all()` (or delete the `session` rows). Locked reviewer
data is unaffected.

---

## 6. Tests

Hermetic; no network, no LLM, no production data (fixtures use `tmp_path`).

```bash
scripts/run_tests.sh tests/backtest/test_calibration_reviewer/ -q
# or
.venv/bin/python -m pytest tests/backtest/test_calibration_reviewer/ -q
```

Coverage: four-field + conditional validation; no implicit defaults; source
CSV read-only loading; tampered-source and reorder fail-closed; draft/lock
state machine; stale-autosave and duplicate-lock races; revision audit trail;
post-lock opt-in reveal; pre-lock leakage (API + exports + refresh); auth,
CSRF, size limits, path traversal; export determinism and digests; approved-
rubric gate; reviewer-DB isolation; production-DB immutability; and a live
loopback HTTP smoke flow.

---

## 7. Non-goals / boundaries

No re-running LLM classifications, no cache writes, no trading-logic changes,
no G7/confidence/keyword changes, no public endpoints, no persistent service,
no git automation, and no automatic §11.4 or Phase-2 PASS. Human calibration
acceptance remains a human decision after review.
