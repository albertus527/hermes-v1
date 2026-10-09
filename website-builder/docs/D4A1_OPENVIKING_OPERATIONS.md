# D4a.1 — OpenViking Operations Runbook

Operator workflow for the application-owned OpenViking context library used by
Hermes Website Builder R2. It runs as a **dedicated service independent of the
interactive `website` tmux session**, bound to **loopback only**, with its own
isolated venv, data directory, and credentials.

> **Status note.** This runbook describes the *provisioned* operating state.
> Provisioning is gated by the D4a.1 approval checkpoint; until it is approved
> and executed, the service is **not** running and the feature flag stays
> **disabled**. See `docs/D4A1_OPENVIKING_LIVE_ACCEPTANCE.md` for the current
> verdict.

---

## 1. Layout (all application-owned, all outside the git tree)

| Item | Path |
|---|---|
| Isolated venv | `~/.website-builder/openviking/venv` |
| Data / index | `~/.website-builder/openviking/data` |
| Server config | `~/.website-builder/openviking/ov.conf` (0600) |
| Env (secrets) | `~/.website-builder/openviking/openviking.env` (0600) |
| Logs | `journalctl --user -u openviking-website` |
| Qualification evidence | `~/.website-builder/openviking/qualification.json` |
| Source manifest | `~/.website-builder/openviking/source-manifest.json` |
| systemd unit | `~/.config/systemd/user/openviking-website.service` |

Nothing here is inside the repository. No index, data, or secret is ever
committed.

---

## 2. Initial provisioning (approval-gated)

Prerequisite: the isolated venv already holds the **exact pinned** version.

```bash
# Create the isolated venv (once; no global install)
mkdir -p ~/.website-builder/openviking
~/.hermes/bin/uv venv --python 3.12 ~/.website-builder/openviking/venv
~/.hermes/bin/uv pip install --python ~/.website-builder/openviking/venv/bin/python openviking==0.4.23

# Provision config + env + systemd unit, start the service, and create the
# application account + user key. Refuses to run without --yes (approval gate).
website-builder/tools/openviking_provision.sh --yes
```

The provision script:

1. verifies the installed version equals the pin (`0.4.23`);
2. reuses `NINEROUTER_API_KEY` from `~/.hermes-website/.env` (no new secret is
   invented) and generates a fresh `OPENVIKING_ROOT_KEY`;
3. writes `ov.conf` from `deploy/openviking/ov.conf.template`;
4. installs + starts `openviking-website.service` (linger is already enabled, so
   it starts on boot without an interactive login);
5. waits for `/health`;
6. creates the application account + admin **user key** via
   `tools/openviking_admin.py` and stores it as `OPENVIKING_USER_KEY`.

### Authentication model (two-layer keys) — IMPORTANT

OpenViking's `api_key` mode uses a **two-layer key model**:

| Key | Source | Can do |
|---|---|---|
| Root key | `ov.conf` `server.root_api_key` | account administration + system routes ONLY |
| User/admin key | Admin API | tenant DATA APIs: `/api/v1/resources`, `/api/v1/search/find`, `/api/v1/fs`, `/api/v1/content` |

A **root key cannot access tenant data APIs** — doing so returns
`403 PERMISSION_DENIED` ("ROOT API keys cannot access tenant-scoped data APIs in
api_key mode"). The application sends ONE key as `X-API-Key`, so the application
must be configured with the **USER key**. The provision script sets this up
automatically; `tools/openviking_admin.py` is idempotent and returns the same
user key on re-run.

Provider configuration (in `ov.conf`, secret-free — the key is `${NINEROUTER_API_KEY}`):

| Role | Provider | Model | Endpoint |
|---|---|---|---|
| Embedding | `openai` (OpenAI-compatible) | `openrouter/text-embedding-3-small` (1536-d) | `http://127.0.0.1:20128/v1` (9router) |
| VLM (L0/L1) | `openai` (OpenAI-compatible) | `openrouter/z-ai/glm-5.3-flash` | `http://127.0.0.1:20128/v1` (9router) |

No local embedding/VLM weights are downloaded. The default OpenViking local
model (`bge-small-zh-v1.5-f16`) is **never** used — it would fetch HuggingFace
weights, which is out of policy.

---

## 3. Start / stop / restart

```bash
systemctl --user start   openviking-website
systemctl --user stop    openviking-website
systemctl --user restart openviking-website
systemctl --user status  openviking-website
```

The service is independent of the `website` tmux session: stopping or restarting
it never affects the Website pipeline or Hermes Trade.

---

## 4. Health checks

```bash
curl -s http://127.0.0.1:1933/health | python3 -m json.tool     # liveness
curl -s http://127.0.0.1:1933/ready  | python3 -m json.tool     # readiness
```

`/health` returns `{"status":"ok",...}` when the process is up. `/ready` also
checks the AGFS filesystem and vector backend.

---

## 5. Index the reviewed corpus + qualify

> **COST WARNING + CONTROL (measured, D4a.1).** Ingestion with
> `processing_mode=semantic_and_vectors` calls the **paid VLM** to generate L0/L1
> for every resource directory **and refreshes every ancestor directory** — so
> ingesting N sources costs many more than N paid LLM calls. A first 11-file
> corpus cost **≈ US$0.58**. The paid path is now **fail-closed**: the FREE
> `vectors_only` mode is the default; the paid mode requires an explicit opt-in
> (`--allow-paid-vlm`, or `backend.enable_paid_vlm(ceiling=…)`) and is bounded by
> a hard per-run ceiling (`MAX_PAID_VLM_SOURCES_PER_RUN`, default 12). The
> qualifier **refuses** `semantic_and_vectors` without `--allow-paid-vlm`.

```bash
cd ~/hermes-website/website-builder
export OPENVIKING_API_KEY="$(grep '^OPENVIKING_USER_KEY=' ~/.website-builder/openviking/openviking.env | cut -d= -f2-)"
# Free path (default; embeddings only, no L0/L1, $0.00 metered):
./.venv/bin/python tools/openviking_qualify.py \
    --base-url http://127.0.0.1:1933 \
    --profile-skills-dir ~/.hermes-website/skills \
    --project-id wb-design \
    --out ~/.website-builder/openviking/qualification.json

# Paid L0/L1 path — ONLY with an approved budget, explicitly opted in and bounded:
./.venv/bin/python tools/openviking_qualify.py \
    --base-url http://127.0.0.1:1933 \
    --profile-skills-dir ~/.hermes-website/skills \
    --project-id wb-design \
    --processing-mode semantic_and_vectors --allow-paid-vlm --paid-vlm-ceiling 12 \
    --out ~/.website-builder/openviking/qualification.json
```

> `OPENVIKING_API_KEY` must be the **USER key** (`OPENVIKING_USER_KEY`), not the
> root key — a root key is rejected by the data APIs (see §2).

This ingests the reviewed corpus through the **production** D4a ingestion policy
and exercises retrieval A–J through the production adapter. It writes a
secret-free evidence file. It refuses to run against a mock (it requires a live
`/health`) and refuses a non-loopback URL without `--allow-remote`.

The reviewed corpus is declared in `app/core/openviking_corpus.py`:

| source_id | category | trust |
|---|---|---|
| `refero_typography` | design_dna | reviewed |
| `refero_color` | design_dna | reviewed |
| `refero_anti_ai_slop` | design_dna | reviewed |
| `refero_visual_workflow` | design_dna | reviewed |
| `refero_motion` | motion | reviewed |
| `refero_craft_details` | components | reviewed |
| `refero_icons` | components | reviewed |
| `impeccable_skill` | design_dna | reviewed |
| `impeccable_critique` | design_dna | reviewed |
| `impeccable_layout` | components | reviewed |
| `impeccable_audit` | components | reviewed |

Only files that actually exist on the host are ingested; a missing entry is
reported, never invented.

---

## 6. Secret rotation

| Secret | Rotate |
|---|---|
| `NINEROUTER_API_KEY` | update `~/.hermes-website/.env`, then re-run the provision script (it re-reads the file) |
| `OPENVIKING_ROOT_KEY` | edit `~/.website-builder/openviking/openviking.env`, then `systemctl --user restart openviking-website` |
| `OPENVIKING_USER_KEY` | re-run `tools/openviking_admin.py` (idempotent; returns the account's user key) and update the env file, or delete the account's user and recreate it |

Both files are `0600`. Neither is ever logged or committed. The application reads
the **user** key from `OPENVIKING_API_KEY` and sends it as `X-API-Key`; the D4a
adapter's `to_dict()` reports the key by **presence only**.

---

## 7. Index refresh

Re-run `tools/openviking_qualify.py`. Ingestion is idempotent: an unchanged
source is `skipped_duplicate`; a changed source advances its revision. There is
no background crawler and no scheduled reindex.

To refresh after editing a reviewed source file, re-run the qualifier; only the
changed source is rewritten.

---

## 8. Version upgrades

1. Review the new OpenViking release notes and API changes.
2. Bump `OPENVIKING_PINNED_VERSION` in `app/core/openviking_library.py` and the
   `pinned_version` in `config/default.yaml` **in the same commit**.
3. Re-install into the isolated venv: `uv pip install --python ~/.website-builder/openviking/venv/bin/python openviking==<new>`.
4. `systemctl --user restart openviking-website`, re-run the qualifier.
5. A vector-dimension or model change may require a **reindex** (the config
   comment in `ov.conf` warns about this). Take a backup first (§9).

Never upgrade silently. Never upgrade the website project venv.

---

## 9. Backup and recovery

The whole state is two directories:

```bash
# Stop the service, copy state, restart.
systemctl --user stop openviking-website
tar czf ~/openviking-backup-$(date +%Y%m%dT%H%M%SZ).tgz \
    -C ~/.website-builder/openviking data ov.conf
systemctl --user start openviking-website
```

Recovery: stop the service, restore `data/` (and `ov.conf`), restart. OpenViking
recovers its indexed data from `data/` **without reindexing** (verified in the
D4a.1 persistence test). The provenance records travel with the resources, so
retrieval provenance stays consistent.

---

## 10. Disk usage

```bash
du -sh ~/.website-builder/openviking/data
du -sh ~/.website-builder/openviking/venv
df -h ~
```

The reviewed corpus is ~150 KB of source; the index and venv dominate. The venv
is ~750 MB. Set an alert well below the disk ceiling.

---

## 11. Log inspection

```bash
journalctl --user -u openviking-website -n 200 --no-pager
journalctl --user -u openviking-website -f
```

Logs are structured and **secret-free**: the application never logs a key value
(credential-shaped content is dropped before it can be surfaced or logged).

---

## 12. Failure diagnosis

| Symptom | Check |
|---|---|
| `/health` fails | `systemctl --user status openviking-website`; `journalctl --user -u openviking-website -n 100` |
| Adapter returns `unavailable` | server down or unreachable — the adapter fails open, the Website pipeline is unaffected |
| Adapter returns `timeout` | raise `timeout_seconds` in `config/default.yaml` `openviking:` block |
| Ingestion returns `error` | the server rejected the write or the task failed — check logs; the resource is NOT marked indexed |
| Retrieval returns zero items | corpus empty for that scope, or the source is not provenanced (dropped, never fabricated) |
| Embedding/VLM 401 | rotate `NINEROUTER_API_KEY` and restart |

---

## 13. Reboot recovery

`linger` is enabled for the user and the unit is `WantedBy=default.target`, so
`openviking-website` starts automatically after a VPS reboot without an
interactive login. It does not depend on the `website` tmux session.

Verify after a reboot:

```bash
systemctl --user is-enabled openviking-website   # -> enabled
curl -s http://127.0.0.1:1933/health
```

---

## 14. Safety invariants (do not violate)

* Bind loopback only. Never expose OpenViking publicly.
* Never reuse Hermes Trade's runtime, secrets, or model configuration.
* Never install a global Hermes memory plugin or edit the global Hermes config.
* Never run an unrestricted installer or an unpinned upgrade.
* Never index secrets, `.env`, logs, dependency dirs, or generated files.
* Keep the feature flag **disabled** for normal production traffic until live
  qualification passes.
