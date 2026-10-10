# D4b.1 — Upstream Laya Operations Runbook

Status: operational guide for the **optional, isolated, benchmark-only** upstream
Laya environment. Branch: `web-design`.

This runbook covers the real upstream Laya package (`NandhaKishorM/laya`, PyPI
`laya`) and the official multilingual checkpoint
(`convaiinnovations/laya-multilingual`). It does **not** describe the accepted
D4b deterministic context preparer, which is a separate, already-accepted
component. **Laya is a benchmark artifact, not a production dependency** — see
§11 and the acceptance report's final recommendation (D: no production
integration).

---

## 1. What is installed, and where

| Item | Location | Measured size |
|---|---|---|
| Isolated venv | `~/.website-builder/laya/venv` | **1.2 GB** |
| HF cache / checkpoint | `~/.website-builder/laya/models` | **647 MB** |
| HF metadata | `~/.website-builder/laya/hf` | ~100 KB |
| Setup script | `tools/benchmark/d4b1_laya_setup.sh` | approval-gated |
| Verify script | `tools/benchmark/d4b1_verify_checkpoint.py` | SHA-256 + size |
| Short-lived worker | `tools/benchmark/d4b1_worker.py` | Mode B |
| Lifecycle driver | `tools/benchmark/d4b1_lifecycle.py` | Mode B |
| Retrieval probe | `tools/benchmark/d4b1_retrieval_probe.py` | read-only |

There is **no systemd unit**, **no persistent service**, and **no PATH change**.
The environment is inert until explicitly invoked. Total disk ≈ **1.8 GB**
(budget 3.5 GB).

## 2. Pinned versions (verified before download)

| Field | Value |
|---|---|
| Package | `laya==0.4.1` (PyPI) |
| Source tag | `v0.4.1` → commit `1adc59f7e371deb601fcfa18a14e25db238addcc` |
| Checkpoint | `convaiinnovations/laya-multilingual` |
| Checkpoint revision | `e4e9ddf21a7b1903b7acffd8814ad4307bf63a67` |
| Weights | `model.safetensors`, sha256 `9d628fd971b700382ac6f65920a86f149777b2e748e0c955fb3b19695aa8f204`, 643835514 bytes |
| Encoder | `jhu-clsp/mmBERT-base` (mmBERT-base, 322M params) |
| License | Apache-2.0 |
| Calibration | **uncalibrated** — ships `temperature: [1.0, 1.0, 1.0]` |

## 3. Install (approval-gated)

```bash
bash tools/benchmark/d4b1_laya_setup.sh
```

Creates the isolated venv, installs the CPU-only pinned stack plus `laya==0.4.1`,
downloads **only** the multilingual checkpoint at the pinned revision, and
verifies the weights hash.

**Dependency note (found in the D4b.1 resume).** The script installs `httpx`
**explicitly**. Newer `huggingface_hub` (≥2.x) depends on `httpx2` rather than
`httpx`, but the accepted D4b OpenViking adapter imports `httpx` **lazily at call
time**; without the explicit install the isolated interpreter cannot drive the
real OpenViking retrieval path (Candidate B) and every brief reports
`BACKEND_FAILURE`. This is an environment-completeness fix; no production code
changed.

## 4. Verify a checkpoint

```bash
bash tools/benchmark/d4b1_laya_setup.sh --verify
```

Prints the size/hash verdict and warns that the checkpoint is uncalibrated.
Expected: `size_ok: true`, `sha256_ok: true`, `verdict: PASS`,
`calibrated: false`.

## 5. Start / stop the model runtime

There is **no daemon**. "Starting" the model is loading it inside a Python
process; "stopping" is exiting that process (or `Router.unload()`):

```python
from laya import Router
router = Router(models={"multilingual": "~/.website-builder/laya/models"},
                revision="e4e9ddf21a7b1903b7acffd8814ad4307bf63a67",
                device="cpu", default="multilingual", max_loaded=1, preload=False)
res = router.predict(brief, questions, model="multilingual")   # typed decisions
router.unload()          # release the checkpoint from RAM
```

`max_loaded=1` keeps only one checkpoint resident. `preload=False` loads the
checkpoint **lazily on the first `predict`** — so a short-lived worker's
cold-start cost is `import + (load + first forward)`, measured at ~13 s here.

> **A `Router.unload()` call is not proof the OS reclaimed memory.** To *prove*
> reclamation, exit the process and re-read its tree RSS (see §6, Mode B).

## 6. Run the benchmark

```bash
# free candidates (FAST-only stand-in + real D4b deterministic + live OpenViking)
./.venv/bin/python tools/benchmark/d4b1_benchmark.py --candidates A,B

# add real upstream Laya (requires the isolated env + checkpoint + the OV key)
export OPENVIKING_API_KEY="$(sed -n 's/^OPENVIKING_USER_KEY=//p' ~/.website-builder/openviking/openviking.env)"
~/.website-builder/laya/venv/bin/python tools/benchmark/d4b1_benchmark.py --candidates A,B,C \
    --out tools/benchmark/results/d4b1_ABC.json

# comparison / CIs / paired tests
./.venv/bin/python tools/benchmark/d4b1_analyze.py --in tools/benchmark/results/d4b1_ABC.json
```

**Mode B lifecycle** (short-lived worker per brief, concurrency = 1):

```bash
~/.website-builder/laya/venv/bin/python tools/benchmark/d4b1_lifecycle.py --samples 5
```

**Indonesian/English retrieval probe** (read-only, floor disabled):

```bash
./.venv/bin/python tools/benchmark/d4b1_retrieval_probe.py
```

> The isolated venv can drive **both** Candidate B (OpenViking, via `httpx`) and
> Candidate C (Laya) in one process, which is why the A,B,C run uses the laya
> venv interpreter.

## 7. Resource monitoring

```bash
free -m ; cat /proc/loadavg ; vmstat 1 3
ps -eo rss,args | grep -E "d4b1_benchmark|d4b1_worker" | grep -v grep   # the Laya process
curl -s http://127.0.0.1:1933/health                                    # must stay ok
tmux ls                                                                 # trade + website unchanged
```

Measured on this 4 vCPU / 8 GB VPS:

| Metric | Mode A (resident) | Mode B (worker) |
|---|---|---|
| Cold start | 19.3 s | 12.6 s (p50) |
| Warm / e2e per brief | 2.09 s (p50) | 14.3 s (p50) |
| Steady-state RSS | ~1.85 GB | 0 (between briefs) |
| Peak RSS | 2.35 GB | 2.32 GB |
| CPU | ~1.9 of 4 vCPU | ~1.95 load |
| Swap activity | none (si/so ≈ 0) | none (si/so ≈ 0) |

## 8. Log inspection & failure diagnosis

| Symptom | Likely cause | Action |
|---|---|---|
| `ModuleNotFoundError: laya` | venv not created / wrong interpreter | run setup, use the venv python |
| Candidate B `BACKEND_FAILURE` from the laya venv | `httpx` missing (`huggingface_hub` pulled `httpx2`) | `~/.website-builder/laya/venv/bin/pip install "httpx>=0.27"` (the setup script now does this) |
| `laya.load()` hangs | `transformers` TensorFlow probe | `export USE_TF=0` (laya sets this itself) |
| checkpoint download 401/404 | network / revision typo | re-check the pinned revision |
| weights hash mismatch | corrupted/partial download | delete `models/` and re-run setup |
| `CUDA` errors | CPU-only VPS | ensure `device="cpu"` and CPU torch wheels |
| OpenViking `unavailable` | service down | `systemctl --user status openviking-website` |

## 9. Rollback / removal

```bash
bash tools/benchmark/d4b1_laya_setup.sh --uninstall
```

Removes the isolated venv and the checkpoint. Nothing global was ever created, so
removal is complete. Production has **no** Laya runtime to roll back: the advisory
flag `laya.enabled` is `false` and no integration was performed.

## 10. Cost

- Upstream Laya inference is **local**: **$0** additional API cost.
- The paid FAST downstream comparison is **separate** and remains **BLOCKED**
  (no paid approval); the harness enforces a hard call cap in code.

## 11. Runtime capacity policy (recommended, NOT activated)

The production runtime must also hold Hermes Website, Hermes Trade, OpenViking,
FRONTEND execution, Chromium/browser QA, and Strix scanning. **Browser QA and
Strix RAM were not measured**, so no overlap is assumed safe.

* Laya inference **concurrency = 1**.
* **No permanently resident Laya model by default.**
* **No overlapping Laya inference with resource-heavy Browser QA or Strix
  scanning** unless separately qualified (a 2.35 GB Laya peak plus an unmeasured
  Chromium/Strix peak could exhaust the ~3.9 GB headroom).
* Preserve RAM headroom for Hermes Trade.
* If headroom is insufficient, or Laya times out / fails to load / exceeds
  limits, use the **deterministic D4b fallback**.
* **Never kill Hermes Trade to make room for Laya.**

## 12. Safety rules

- Never install into the WB venv or a global environment.
- Never point the model at an unpinned GitHub `main`.
- Never enable a persistent service without measured justification and approval.
- Never represent the deterministic D4b planner as upstream Laya inference.
- Never treat raw Laya confidence as a calibrated probability (the checkpoint
  ships uncalibrated).
- Never claim Laya improves multilingual retrieval — the measured evidence (§6
  of the acceptance report) says it does not.
