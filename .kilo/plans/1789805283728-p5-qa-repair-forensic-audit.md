# Forensic Audit: tg-6329821361-p5 QA Repair Budget Exhaustion

Status: AUDIT ONLY. No code modified. No E2E run. No commits.

Environment note: this workspace has no access to
`/home/albertus527/.website-builder/state/tg-6329821361-p5.json` or the p5
workspace/screenshots. All findings below are derived from (a) the VISION
pass text supplied by the user and (b) the actual repo code at
`website-builder/app/qa/*`, `website-builder/app/hermes/adapter.py`,
`website-builder/app/core/*`. No production artifact was read; none is
claimed to have been read.

## 1. Executive finding

The repair budget was exhausted not because FRONTEND failed twice at a
well-specified task, but because **the QA loop has no grounding check
between a VISION finding and an explicit Design DNA/app-level policy**.
`VisionFindings.blocking` (`app/qa/findings.py:26-37`) is `bool(critical) or
bool(major)` — full stop. Any string VISION writes into `major` becomes a
blocking requirement with the same force as a deterministic build failure,
with zero verification that the requirement traces to anything persisted in
Design DNA, `brief`, or app policy.

Two concrete defects compound this:

- **The only textual "policy" for the unresolved-CTA case is one line in
  the FRONTEND prompt** (`_build_frontend_prompt`,
  `app/hermes/adapter.py:1102`): *"If a CTA destination is unresolved, use a
  placeholder and mark it clearly."* This does not say "disabled", does not
  name `aria-disabled`/`opacity-60`/`cursor-not-allowed`, does not name
  which components, and is not a persisted Design DNA field. VISION pass 2
  asserted *"Design DNA preview policy requires CTA disabled until
  destination is resolved"* — no such field or policy exists anywhere in
  the Design DNA schema exercised by the codebase (`primary_cta.label`,
  `primary_cta.destination`, `unresolved_facts: [...]` — see
  `tests/test_build.py:98-101`). VISION invented the word "disabled" and
  the specific DOM-attribute mechanism.
- **Repair instructions carry only the current attempt's findings**
  (`_build_repair_instructions`, `app/qa/orchestrator.py:382-413`), not a
  cumulative/normalized list of "still-open blockers since attempt 0", and
  the only structural guard against regression is prose ("Do NOT redesign
  unrelated areas... Preserve the existing Design DNA unless it directly
  caused a blocking finding") — never verified by `validate_composed_dna`,
  which only checks typography-family count and reference synthesis
  (`app/core/design_dna.py`, `app/core/composition.py:35-40`).

## 2. Timeline — what changed / what persisted

| Step | What VISION saw as new | What persisted from before |
|---|---|---|
| VISION #1 | Full placeholder leakage (CTA label text, disclaimer text, sample data) | — (first pass) |
| repair #1 | FRONTEND edited App/Hero/etc. | — |
| VISION #2 | critical findings gone; CTA now "active" (not disabled); missing SAMPEL badge | Underlying unresolved-destination fact unchanged |
| repair #2 | FRONTEND presumably attempted a disabled-style change | SAMPEL badge fixed (not in pass 3) — repair #2 correctly resolved that finding |
| VISION #3 | CTA "still not disabled" in 3 *named components*; NEW: color-mismatch finding; NEW: two-CTA-is-forbidden finding | Same underlying unresolved-destination fact; SAMPEL fix held |

Note the escalation pattern pass2→pass3: the requirement went from a
generic "CTA disabled" to naming exact Tailwind-esque classes
(`aria-disabled`/`opacity-60`/`cursor-not-allowed`) and exact components
(Hero, InfoSection, BookingCta). This is VISION *inventing increasingly
specific implementation detail* across passes, not the app supplying a
progressively richer, persisted spec. Nothing in the repo ever asked
FRONTEND to use those exact attributes/classes.

## 3. Persistent CTA root cause (Q1)

Verdict: **(a) + (e)**, with supporting code evidence — not (b), (c), (d), or (f):

- **Not (b):** repair #2 does receive the destination-unresolved context.
  `_repair()` (`app/qa/orchestrator.py:339-353`) rebuilds `brief` fresh from
  `state.brief` every call and passes it into `hermes_adapter.frontend_build`,
  which re-derives `cta_note` from `brief["why_destination"]` inside
  `_build_frontend_prompt` (`adapter.py:1067-1073`) on *every* invocation,
  repair or not. The unresolved-destination fact is not lost.
- **(a) confirmed:** the *only* instruction available to FRONTEND on what
  "handle an unresolved destination" means is "use a placeholder and mark
  it clearly" — never "disable the button", never naming a mechanism. A
  prompt that doesn't say "disabled" cannot reliably produce disabled
  buttons; FRONTEND's own placeholder-styling choice (visible label +
  disclaimer text in pass 1) was arguably a reasonable-but-wrong reading of
  that instruction, and repair attempts only add the *latest* VISION
  finding text, which introduces the word "disabled" for the first time in
  repair #1's `_build_repair_instructions` payload (constructed from VISION
  pass-1's raw finding string, not from a stable policy).
- **(e) confirmed:** VISION pass 3's own finding text names `aria-disabled`
  as required evidence — that is a DOM attribute, structurally
  unobservable from a static PNG screenshot. `vision_inspect()`
  (`adapter.py:1122-1199`) attaches only two rendered images
  (`build_native_content_parts`); VISION has no DOM/accessibility-tree
  access. It cannot verify `aria-disabled` ever, regardless of what
  FRONTEND implements, so this specific finding is structurally
  un-satisfiable through the current evidence channel — a QA test that can
  never pass by design is not a code defect in FRONTEND, it's an
  evidence-capability mismatch in VISION's own finding.
- **Not (c)/(d):** with no persisted policy and no evidence to observe the
  named attribute, there is no way to determine from available artifacts
  whether FRONTEND "failed to implement" vs "regressed" the fix — both
  require the missing production workspace/diff, which is unavailable
  here (see Verdict F caveat below, scoped only to this sub-question).

## 4. Repair-context audit (Q2)

Exact code path: `QAOrchestrator._repair()` → `_build_repair_instructions()`.

```
repair(
    failed_attempt.deterministic.failures        # only THIS attempt's det failures
  + failed_attempt.vision.critical/major          # only THIS attempt's VISION findings
  + state.design_dna (persisted, current)         # current DNA snapshot
)
```

This is **`repair(current_findings_only)`**, not the cumulative form. The
`attempts` list in `QAOrchestrator.run()` retains full history, but only
`qa_attempt` (the just-completed one) is threaded into `_repair()` —
`app/qa/orchestrator.py:147-149`:
```python
repaired = self._repair(project_id, workspace, brief, design_dna, qa_attempt)
```
`qa_attempt` here is always the most recent single `QAAttempt`, never the
accumulated list. There is no data structure carrying forward "prior
critical/major findings that must remain fixed."

**Yes, a later repair can regress an earlier fix.** Nothing enforces
persistence of previously-resolved findings: `validate_composed_dna` checks
only typography-family count + reference synthesis
(`app/core/design_dna.py`, `composition.py:35-40`); it does not diff the new
DNA/source against the prior attempt's fixes. The only safeguard is a prose
instruction ("Preserve the existing Design DNA unless it directly caused a
blocking finding") with no verification. In this incident the SAMPEL-badge
fix from repair #2 apparently held (not present in pass-3 findings) —
that's incidental, not enforced.

## 5. VISION grounding audit (Q3 / Q5 deliverable table)

| PASS-3 finding | Grounding | Blocking justified? | Evidence |
|---|---|---|---|
| CTA not disabled (`aria-disabled`/`opacity-60`/`cursor-not-allowed`) in Hero/InfoSection/BookingCta | **UNSUPPORTED_INFERENCE** (no persisted "disabled" contract; DOM attribute unobservable from screenshot) | No — as literally stated (aria-disabled cannot be QA'd via screenshot); the underlying idea "placeholder must not look actionable" is REASONABLE_BUT_NONBLOCKING_INFERENCE at best given the only real instruction was "mark it clearly" | `_build_frontend_prompt` line 1102 (`adapter.py`); no `disabled`/`aria-disabled` string anywhere in `app/core/design_dna.py`, `composition.py`, or the Design DNA fields exercised in tests |
| Hero/Booking CTA use muted brown/terracotta instead of accent `#C2410C` | **UNSUPPORTED_INFERENCE** | No | No code path validates or even records a specific accent hex requirement per-component; `validate_typography` only checks font-family count (`design_dna.py:26-34`); no analogous per-component color validator exists. "Accent color exists in palette" (if it does) is not "every CTA must use this hex" |
| Secondary CTA "Cek Informasi" forbidden (DNA "only defines one primary CTA") | **UNSUPPORTED_INFERENCE** | No | Design DNA schema observed in tests contains `primary_cta` (singular field name) but naming one field "primary" is not a prohibition on a second, unnamed CTA; nothing in `composition.py`/`design_dna.py` enforces CTA cardinality |
| mobile hamburger not explicitly defined (minor) | UNKNOWN | No (minor, non-blocking by policy anyway) | Not grounded, but doesn't matter — minors never block per `VisionFindings.blocking` |
| feature bar wrapping (minor) | UNKNOWN | No | same as above |
| Cara Pesan spacing (minor) | UNKNOWN | No | same as above |

None of the three PASS-3 *major* findings that drove the exhaustion trace to
an EXPLICIT_CONTRACT in the codebase. All three are VISION-authored
constraints with no persisted source.

## 6. Evidence freshness audit (Q5)

Each loop iteration in `QAOrchestrator.run()` performs, per attempt:
`_run_rebuild_checks` (fresh `npm run build`) → `_run_one_attempt` → fresh
`LocalRenderer.start()` → fresh `ScreenshotCapture.capture(handle.url,
qa_dir, attempt_num)` → `vision_inspect(screenshots.desktop,
screenshots.mobile, ...)` using that same attempt's screenshot paths
(`app/qa/orchestrator.py:98-103, 174-200, 244-316`). Screenshot filenames
are keyed by `attempt_num`, which is a strictly increasing monotonic
counter (explicitly documented at lines 84-91 to prevent number reuse).
There is no caching layer, no reused render handle across attempts (`stop`
is called in a `finally` at line 257-258 every attempt), and
`validate_screenshot_dimensions` (line 268) runs before VISION consumes any
evidence.

**Conclusion: no staleness defect found in the code.** Each VISION pass
plausibly inspected the render produced by the immediately preceding
repair. This rules out (F) "state/revision/evidence mismatch" as the cause
based on the code path (absent contrary evidence from the unavailable
production state file).

## 7. Trust-boundary analysis (Q4)

Yes — **VISION currently has unilateral authority to invent a blocking
"major" finding**. The trust boundary is exactly
`VisionFindings.blocking` in `app/qa/findings.py:26-37`: it is a pure
`bool(critical) or bool(major)` check with a code comment explaining *why*
VISION failures fail closed, but nothing gates *content* — there is no
step anywhere between `vision_inspect()` returning parsed JSON
(`_parse_vision_response`, called at `adapter.py:1199`) and
`QAAttempt.repair_required`/`final_pass` that cross-references a finding
against Design DNA, `brief`, or any deterministic policy. Any string VISION
puts in `major` consumes one of the two repair attempts with full force,
identical to a real deterministic build failure.

## 8. Minimal recommended fix (audit-only — not implemented)

Two smallest-footprint corrections, in order of leverage, neither adding a
new agent/role/framework:

1. **Make the actual policy explicit and persisted, once, in the one place
   that already governs unresolved-destination behavior** —
   `app/core/contact_form.py::compose_contact_form_instructions()` already
   composes destination-state-dependent instructions
   (`contact_form.py:159-175`). Extend the "no verified destination"
   branch text there (or add one adjacent, equally small, pure function
   near it) to state precisely and mechanically what "placeholder CTA"
   must look like (e.g. visually inert / non-navigating, single canonical
   wording) so FRONTEND receives one unambiguous, testable instruction on
   every build **and** every repair (it already flows through
   `compose_project_instructions` → `_build_repair_instructions`'s
   `dna_note`/instructions path). This directly fixes the root cause in
   §3 without adding any new component.
2. **Add one grounding gate before a VISION finding is allowed to consume a
   repair attempt**: in `VisionFindings` or at the call site in
   `_run_one_attempt`, require that `major`/`critical` strings VISION emits
   be checked against a short, fixed allow-list of finding *categories*
   already declared in `_build_vision_prompt` (missing hero/content,
   clipping/overflow, responsive failure, overlapping UI, unreadable text,
   contrast, broken hierarchy, placeholder leakage, broken CTA
   presentation, accessibility, DNA mismatch) — i.e., make VISION's own
   prompt categories machine-checkable (VISION already must classify each
   finding into a `category` field; add that one field to the JSON
   contract) rather than free text, and only findings inside declared
   categories are blocking. This does not require a second model role — it
   constrains VISION's own JSON response shape, still a single evidence-only
   pass.

Both changes are additive text/schema tweaks to existing functions; no new
files, roles, services, or infra.

## 9. Tests that would be required (if implemented)

- `test_contact_form.py`: assert the "no verified destination" instruction
  text contains the exact, unambiguous placeholder-CTA directive (a
  behavior/contract assertion on the returned instruction string, not a
  source-regex test).
- `test_qa.py`: a case where `VisionFindings.major` contains a finding
  string with no matching declared category → assert it is NOT blocking
  (`repair_required is False`) while a category-tagged critical/major
  finding still blocks.
- `test_qa.py`: repair-context regression test — assert
  `_build_repair_instructions` output for repair #2 still contains the
  original unresolved-destination directive verbatim (guards against the
  instruction silently dropping across repairs).
- `test_hermes_adapter.py`: assert `_build_vision_prompt` / VISION JSON
  contract requires a `category` field per finding, and unknown/missing
  categories are exercised in `_parse_vision_response`.

## 10. Files that would need modification (names only, not implemented)

- `website-builder/app/core/contact_form.py` — make the placeholder-CTA
  directive explicit in `compose_contact_form_instructions()`.
- `website-builder/app/qa/findings.py` — add category-aware blocking logic
  to `VisionFindings`.
- `website-builder/app/hermes/adapter.py` — extend `_build_vision_prompt`'s
  JSON contract with a `category` field per finding and update
  `_parse_vision_response` accordingly.
- `website-builder/tests/test_contact_form.py`,
  `website-builder/tests/test_qa.py`,
  `website-builder/tests/test_hermes_adapter.py` — new regression tests
  per §9.

## 11. Q6 — repair effectiveness vs. instructions

`_build_repair_instructions` (`orchestrator.py:399-413`) explicitly
instructs: smallest targeted fix, no redesign, no invented business facts,
preserve existing Design DNA "unless it directly caused a blocking
finding." This is consistent with minimal-repair intent. Observed behavior
(SAMPEL badge fix held; CTA handling changed shape between repairs without
ever satisfying VISION) is consistent with FRONTEND attempting targeted,
bounded fixes against a moving, ungrounded target — not with FRONTEND
ignoring the "minimal fix" instruction. No evidence of broad
regeneration/redesign in the supplied VISION text.

## 12. Q7 — is the 2-repair budget correct?

**Yes, the default (2) should stand.** The failure mode here is not
"almost converged, needed one more try" — it is "the blocking target
itself was never a fixed, checkable spec," so no number of repairs bounded
or unbounded would reliably converge while VISION can keep restating the
same idea with new invented specifics (label text → "disabled" → DOM
attributes → color hex → CTA cardinality). Increasing the budget would
mask this defect and raise cost per failed project. Fix the grounding
defect (§8) before ever revisiting the budget.

## 13. Verdict

**C — VISION blocking/grounding has a concrete defect** (primary), with a
contributing minor factor under **B** (repair-context audit, §4: no
enforcement against fix regression across repairs, though no direct
evidence a regression actually occurred here). **Not E**: evidence
freshness audit (§6) found no defect in the code path. Full confirmation
of "what FRONTEND actually rendered at each step" would require the
production workspace/state file, which this environment does not have
access to — but the verdict on the grounding defect (§7) stands
independent of that artifact, since it is proven directly from
`app/qa/findings.py`'s unconditional `blocking` property and the absence of
any persisted "CTA disabled" contract anywhere in `app/core/design_dna.py`
or `app/core/composition.py`.
