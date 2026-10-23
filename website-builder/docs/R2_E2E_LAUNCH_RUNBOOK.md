# R2 E2E — Operator Launch Runbook (single manual run)

Status: **prepared, NOT started.** Awaiting explicit owner GO.

Bot: `@website527_bot` (id `8722247904`, display name `wbtest`)
Worktree: `/home/albertus527/wt-n8-int/website-builder` @ `824b7b47a` (branch `merge/n9-into-r2`)
R2 root: `~/.website-builder-r2/`

---

## 0. Pre-launch state (verified 2026-10-22)

| Check | Value |
|---|---|
| Bot identity | `@website527_bot` / `8722247904` / `wbtest` |
| Polling conflict | none — no process holds the token, no webhook, `pending=0` |
| R2 config | `r2-e2e.yaml` selected via `WEBSITE_BUILDER_CONFIG_PATH` |
| `requirement_creator.enabled` | `true` |
| `decision_engine.design_mode` | `disabled` |
| `decision_engine.intake_mode` | `disabled` |
| `vercel.publication_enabled` | `false` |
| `github.enabled` | `false` |
| `design_fidelity.enabled` | `false` |
| Ownership namespace | `wb-e2e-staging` |
| Vercel account | `albertus527` / `team_E50r6RSMGypiUpWGdMHEK1DG` |
| Free slug | `dorma-atelier` — confirmed unused |
| Browser | `/opt/r2-browser/chrome`, root-owned, `r2-browser` profile loaded |
| Cost baseline | `max_id=5266`, 5,266 rows, last request `2026-10-21T13:59:27Z` |
| Gateway credential | `NINEROUTER_API_KEY` in `profile/.env` — **required** (see §1a) |

Post-repair preflight: **21/21 PASS**.

---

## 1a. Gateway authentication prerequisite (READ THIS FIRST)

**The single most expensive failure this runbook prevents.** On the first
attempt at this E2E the run died immediately: every model call returned

```
HTTP 401: {"error":"API key required for remote API access"}
```

eight times, no retry could ever succeed, and the runtime fell back to asking
the owner for "missing fields" — which was misleading, because the brief was
fine and the model simply never saw it.

### Where the key must live

`NINEROUTER_API_KEY` belongs in the **profile `.env`**, not `app.env`:

```
~/.website-builder-r2/profile/.env        <- correct
~/.website-builder-r2/state/app.env       <- does NOT reach the model child
```

Why: the `hermes -z` child builds its environment from a model-credential
allowlist that does **not** include a self-hosted gateway's key name. The
child then re-runs `load_hermes_dotenv()` on `$HERMES_HOME/.env` with
`override=True`, so that file is what actually supplies the key. `app.env` is
for the application's own operational credentials (Telegram, Vercel).

### Credential exposure — stated plainly

`NINEROUTER_API_KEY` is a **model** credential, not a deploy credential, so
`assert_profile_dotenv_clean` permits it (it bans 33 privileged names:
`TELEGRAM_BOT_TOKEN`, `VERCEL_TOKEN`, …). But:

- The FRONTEND child runs with `HERMES_YOLO_MODE=1` and holds **file**,
  **terminal** and **skills** toolsets.
- It can therefore **read** `$HERMES_HOME/.env` as a file.
- A spawned shell **cannot** `printenv` the key — the provider-env blocklist
  scrubs it from every shell (`model_credentials_not_shell_protected` → NONE).

So the residual risk is a deliberate file read by a misbehaving generation
agent, not an ambient environment leak. The key is scoped to a **localhost
gateway** (`127.0.0.1:20128`), not a remote account. This is the same posture
R1 has run with. Accept it deliberately or do not run the E2E.

### Provisioning procedure (non-echoing)

```bash
# back up first
cp -p ~/.website-builder-r2/profile/.env \
      ~/.website-builder-r2/profile/.env.bak-$(date +%Y%m%dT%H%M%S)

# copy ONLY that one key, preserving every other line
K="$(awk -F= '/^NINEROUTER_API_KEY=/{print $2; exit}' ~/.hermes-website/.env)"
grep -v '^NINEROUTER_API_KEY=' ~/.website-builder-r2/profile/.env > /tmp/e2e.env
printf 'NINEROUTER_API_KEY=%s\n' "$K" >> /tmp/e2e.env
chmod 600 /tmp/e2e.env && mv -f /tmp/e2e.env ~/.website-builder-r2/profile/.env
unset K
```

### Verification procedure

```bash
# 1. the guard must still pass
python3 -c "
import sys; sys.path.insert(0,'/home/albertus527/wt-n8-int/website-builder')
from app.core import credentials
from pathlib import Path
credentials.assert_profile_dotenv_clean(Path.home()/'.website-builder-r2/profile')
print('guard PASS')"

# 2. the gateway must accept it (non-billable, no generation)
python3 -c "
import sys,os; sys.path.insert(0,'/home/albertus527/wt-n8-int/website-builder')
from pathlib import Path
for l in (Path.home()/'.website-builder-r2/profile/.env').read_text().splitlines():
    if l.startswith('NINEROUTER_API_KEY='): os.environ['NINEROUTER_API_KEY']=l.split('=',1)[1]
import json,urllib.request
r=urllib.request.Request('http://127.0.0.1:20128/v1/models',
  headers={'Authorization':'Bearer '+os.environ['NINEROUTER_API_KEY']})
with urllib.request.urlopen(r,timeout=15) as x: print('HTTP',x.status,len(json.loads(x.read())['data']),'models')"
```

**Since this run, the runtime performs both checks itself at startup** via
`preflight_gateway_authentication()` — a run cannot reach polling with a
missing or rejected gateway key.

---

## 1b. Safe recovery after a 401

If a run still fails with 401:

1. **Stop the listener** (Ctrl-C once). Do not let it retry — the failure is
   deterministic and every attempt is wasted time.
2. **Confirm the cost was zero**: `python3 tools/r2_cost_report.py --delta --since <baseline_id>`.
   A 401 means no model ran, so new rows should be `0`.
3. **Check the credential** with the two commands in §1a.
4. **Do not delete the project.** A project left in `WAITING_INPUT` holds no
   accepted brief and no deployment. `WAITING_INPUT` maps back to
   `ConversationIntent.INTAKE`, and intake **merges** brief fields across
   turns — so simply re-sending the brief through Telegram resumes cleanly.
5. If you prefer a pristine project, start a new conversation instead of
   mutating lifecycle state by hand.

---

## 1. Activation (corrected)

```bash
source "$HOME/.website-builder-r2/activate-r2.sh"
```

The script now:
- **`cd`s into `/home/albertus527/wt-n8-int/website-builder`** — required because
  `python -m app` resolves the `app` package from the current directory. Before
  this fix the shell stayed in whatever tree it was in (R1), which would have
  launched the R1 code.
- **Exports no credential values.** The previous version exported
  `TELEGRAM_BOT_TOKEN=000000000:...` and `VERCEL_TOKEN=R2-PLACEHOLDER-NO-DEPLOY`.
  Those are gone: `app.core.app_env.load_application_env()` reads
  `~/.website-builder-r2/state/app.env` (mode `0600`) at startup and is
  authoritative. A missing credential now fails closed and loudly.

Confirm before proceeding:

```bash
pwd                                    # expect .../wt-n8-int/website-builder
git rev-parse --short HEAD             # expect 824b7b47a
echo "$WEBSITE_BUILDER_CONFIG_PATH"    # expect .../r2-e2e.yaml
```

---

## 2. Start

```bash
python3 -m app
```

This begins long-polling `@website527_bot`. **No model request is sent until a
brief arrives.**

---

## 3. Test brief (send from the owner Telegram account `6329821361`)

> Buat website landing page untuk studio arsitektur bernama Dorma Atelier. Gaya
> desain editorial minimalis, premium, dan modern. Gunakan tipografi elegan,
> warna off-white, charcoal, serta aksen terracotta. Website harus memiliki hero
> section, showcase 3 proyek arsitektur, tentang studio, layanan, dan kontak.

Expected: FAST → Requirement Creator → `PRD.md` + `requirements.json` →
FRONTEND → Design DNA → `Design.md` → VISION QA + deterministic checks →
native capture → preview URL delivered to the bot.

Expected slug: `dorma-atelier`. Expected project id: `tg-6329821361-p1`
(R2 state is empty, so the sequence starts at `p1`).

---

## 4. Monitor

Watch the R2 log; do **not** interact with the run beyond observing:

```bash
tail -f ~/.website-builder-r2/logs/*.log 2>/dev/null
```

Cost, in a second shell:

```bash
cd /home/albertus527/wt-n8-int/website-builder
python3 tools/r2_cost_report.py --delta --since 5266
```

**Abort target $1.00 is a manual threshold, not an enforced kill switch.**
Worst case at 8 provider calls ≈ $1.40. If the delta crosses $1.00, stop the run.

---

## 5. Stop (graceful)

**Ctrl-C once** in the running terminal. The runtime installs flag-only
`SIGINT`/`SIGTERM` handlers, then `loop.stop()`, then drains in `finally:`.

- Never `kill -9` first — it skips the drain and can leave a partial
  `frontend_budget` lock.
- Do **not** press Ctrl-C during an active FRONTEND call unless aborting; the
  call is cancelled but still billed by the provider.

Verify nothing remains:

```bash
pgrep -af "python3? -m app" || echo "clean"
for d in /proc/[0-9]*; do sudo -n tr '\0' '\n' < "$d/environ" 2>/dev/null \
  | grep -q "^TELEGRAM_BOT_TOKEN=8722247904:" && echo "LEAK ${d#/proc/}"; done; echo "no holder"
```

If a stale `frontend_budget` lock exists under
`~/.website-builder-r2/state/.frontend_budget/`, inspect before deleting — do
not remove blindly.

---

## 6. Evidence to collect

| Item | Source |
|---|---|
| Wall time, intake → preview | log timestamps |
| FAST / FRONTEND / VISION request counts | `r2_cost_report.py --delta` |
| Actual cost + retries | same |
| `PRD.md`, `requirements.json`, `Design.md` | `~/.website-builder-r2/workspaces/tg-6329821361-p1/` |
| Document consistency | `verify_artifact_matches_prd()` |
| Screenshot readiness + QA | `qa/attempt-*/`, readiness reason codes |
| Preview URL + rendering | deployment record |
| Requirement compliance | deterministic checks |
| Final lifecycle | **must be `PREVIEW_READY`, never `LIVE`** |

---

## 7. Hard boundaries during the run

- No approval trigger — do not reply "approve" to the bot.
- No production promotion, no custom-domain publishing.
- Design Scoring stays disabled: **0 Decisions calls are possible**.
- R1 (`~/.website-builder`, `~/.hermes-website`, gateway pid `1578384`) is not
  touched. The `wbe2e` staging env is read-only reference.

---

## 8. Model-call budget

| Role | Min | Max |
|---|---|---|
| FAST | 1 | 2 |
| FRONTEND | 1 | 4 |
| VISION | 1 | 2 |
| Decisions | 0 | 0 |
| **Total** | **3** | **8** |

`agent.api_max_retries = 2` (R2 profile only), `MAX_COMPILE_REPAIR_ATTEMPTS = 1`.
Empirical p50 ≈ $0.024 per glm call, p99 ≈ $0.178 → expected ≈ $0.06.
