# D4c — FAST Context Integration: Operations Runbook

Operator workflow for the D4c FAST-context handoff: how to verify, enable,
disable, and roll back the handing of a D4b context pack to FAST. This runbook
is the operational companion to
`docs/D4C_FAST_CONTEXT_INTEGRATION_ACCEPTANCE.md`.

> **Status note.** The feature ships **disabled by default** and the verdict is
> `D4C_READY_FOR_CONTROLLED_FAST_SMOKE`: the code wiring, offline suites, and the
> **real OpenViking** integration are proven, but a **paid real-FAST** run has
> **not** been executed. Keep `fast_context_injection: false` until an operator
> approves and runs the paid smoke (§6).

---

## 1. What D4c changes (and what it does not)

D4c adds **one independent gate** — `laya.fast_context_injection` — that decides
whether FAST receives the prepared D4b context pack.

* It does **not** create a new FAST path or a second decision maker.
* It does **not** change FAST's authority (intent, scope, requirements,
  clarification, action selection, execution authorization).
* It does **not** change the context contract or the retrieval stack.
* Retrieved references remain **lower-trust DATA** and never authoritative
  instructions.

The gate lives in `IntakeProcessor._prepare_reference_context`
(`app/core/intake.py`) and composes the block into the single FAST call via the
separate `reference_context=` argument.

---

## 2. The three flags (all independent, all default OFF)

| Flag | Default | Controls |
|---|---|---|
| `website_builder.laya.enabled` | `false` | real upstream Laya preparation / OpenViking retrieval |
| `website_builder.laya.multilingual_expansion` | `false` | D4b.2 Indonesian-gated English gloss expansion |
| `website_builder.laya.fast_context_injection` | `false` | **D4c** — whether FAST receives the pack |

Effective condition for FAST to receive a block:
`fast_context_injection AND enabled`. Enabling `enabled` alone hands FAST
nothing. Configured in `config/default.yaml` — **no `HERMES_*` env var**.

---

## 3. Preconditions (verify before enabling)

```bash
# OpenViking must be healthy (see docs/D4A1_OPENVIKING_OPERATIONS.md)
systemctl --user is-active openviking-website      # -> active
curl -s http://127.0.0.1:1933/health               # -> {"status":"ok",...}
curl -s http://127.0.0.1:1933/ready                # -> vectordb/embedding ok

# The FAST handoff must be OFF by default
grep -n fast_context_injection config/default.yaml  # -> ...: false
```

---

## 4. Verifying the handoff without paid calls

The contract test drives the real intake seam with a recording FAST stand-in:

```bash
./.venv/bin/python -m pytest -q tests/test_d4c_fast_context_integration.py
```

The live smoke exercises the **real OpenViking** service end to end (no paid
calls; the FAST *model* is a labelled recording stand-in):

```bash
bash tools/d4c_run_live_smoke.sh
# evidence -> ~/.website-builder/openviking/d4c_live_smoke.json
```

Expected: flag OFF ⇒ 1 FAST call, no block, brief verbatim; flag ON ⇒ 1 FAST
call, labelled lower-trust block, brief verbatim; outage ⇒ Laya `unavailable`,
no block, 1 FAST call; `paid_model_calls: 0`.

---

## 5. Enabling / disabling

**Enable (only after the paid smoke in §6 passes):**

```yaml
# config/default.yaml  ->  website_builder.laya
enabled: true
fast_context_injection: true      # D4c
# multilingual_expansion: true    # optional, D4b.2 — independent
```

Restart the Website Builder process. With any of the three flags `false`, that
layer is a strict no-op.

**Disable (safest first step):**

```yaml
fast_context_injection: false
```

`enabled: false` also disables preparation entirely. Both degrade safely; FAST
receives no reference block.

---

## 6. Paid real-FAST smoke (operator-gated)

This is the **only** remaining acceptance gate. It is paid and must be approved
explicitly.

1. Confirm §3 preconditions.
2. Run one brief with the flag **OFF**, capture the FAST decision.
3. Run the **same** brief with the flag **ON**, capture the FAST decision.
4. Confirm: exactly **one** FAST call per run; brief unchanged; the block is
   labelled lower-trust DATA; the decision contract
   (`scope/name/what/why/…/readiness`) is unchanged in shape.
5. Record the outcome in the acceptance report; if all gates pass, the verdict
   may be upgraded to `D4C_READY_FOR_STAGED_ROLLOUT`.

Do **not** enable the paid VLM ingestion path; Laya never ingests.

---

## 7. Failure behaviour (fail-closed)

| Condition | Behaviour |
|---|---|
| Flag OFF | FAST receives byte-identical inputs to baseline |
| OpenViking unavailable/timeout | Laya `unavailable`; **no block**; **1 FAST call**; brief verbatim |
| Malformed response | fail-closed; no partial unverified pack |
| Invalid provenance | item dropped / pack refused; no promotion of unverified refs |
| Empty result | honest-empty (not an error) |
| Retry exhaustion | bounded attempts, then degrade |
| FAST timeout after prep | original FAST error surfaces; **no second FAST call** |

The context layer is optional; its failure never blocks the existing FAST-only
intake path.

---

## 8. Resource envelope (VPS shared with Hermes Trade + OpenViking)

The live smoke peaked at **≈86–88 MB RSS**; retrieval latency ~5.5 s for 3
queries (real OpenViking + embedding). No Laya checkpoint, no TypeSafe Jev, no
new resident model, no Strix. Abort if memory/swap pressure appears.

---

## 9. Rollback

No persistent state is touched (no reindex, no migration, no OpenViking restart).

```bash
# (a) disable the feature (preferred)
#     website_builder.laya.fast_context_injection: false

# (b) revert the D4c commit(s)
git revert <D4c commit SHA>
```

---

## 10. Troubleshooting

| Symptom | Check |
|---|---|
| FAST receives a block when it shouldn't | `fast_context_injection` in `config/default.yaml`; restart |
| FAST receives **no** block when it should | `laya.enabled` AND `fast_context_injection` both true |
| Laya `unavailable` | OpenViking `/health`, `/ready`; systemd unit; key file |
| No items returned | corpus/project id (`wb-design`); relevance floor 0.62 |
| Unexpected paid calls | ensure the VLM ingestion path is not enabled; Laya never ingests |

---

## 11. Reference

* Acceptance report: `docs/D4C_FAST_CONTEXT_INTEGRATION_ACCEPTANCE.md`
* OpenViking runbook: `docs/D4A1_OPENVIKING_OPERATIONS.md`
* D4b context prep: `docs/D4B_LAYA_CONTEXT_PREPARATION_ACCEPTANCE.md`
* D4b.2 multilingual: `docs/D4B2_MULTILINGUAL_RETRIEVAL_ACCEPTANCE.md`
