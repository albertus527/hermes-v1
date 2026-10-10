# D4b.1 — Upstream Laya Operations Runbook

Status: operational guide for the **optional, isolated, approval-gated** upstream
Laya benchmark environment. Branch: `web-design`.

This runbook covers the real upstream Laya package
(`NandhaKishorM/laya`, PyPI `laya`) and the official multilingual checkpoint
(`convaiinnovations/laya-multilingual`). It does **not** describe the accepted
D4b deterministic context preparer, which is a separate, already-accepted
component.

---

## 1. What is installed, and where

| Item | Location | Notes |
|---|---|---|
| Isolated venv | `~/.website-builder/laya/venv` | NEVER the WB venv; NEVER global site-packages |
| HF cache / checkpoint | `~/.website-builder/laya/models` | outside the git tree |
| HF home | `~/.website-builder/laya/hf` | metadata cache |
| Setup script | `tools/benchmark/d4b1_laya_setup.sh` | approval-gated; not run automatically |
| Verify script | `tools/benchmark/d4b1_verify_checkpoint.py` | SHA-256 + size check |

There is **no systemd unit**, **no persistent service**, and **no PATH change**.
The environment is inert until explicitly invoked.

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

This creates the isolated venv, installs the CPU-only pinned stack plus
`laya==0.4.1`, downloads **only** the multilingual checkpoint at the pinned
revision, and verifies the weights hash. Expected: ~3.1 GB disk
(~2.5 GB venv + 644 MB checkpoint).

## 4. Verify a checkpoint

```bash
bash tools/benchmark/d4b1_laya_setup.sh --verify
```

Prints the size/hash verdict and warns if the checkpoint is uncalibrated.

## 5. Start / stop the model runtime

There is no daemon. "Starting" the model is loading it inside a Python process;
"stopping" is exiting that process (or `Router.unload()`):

```python
from laya import Router
router = Router(models={"multilingual": "~/.website-builder/laya/models"},
                revision="e4e9ddf21a7b1903b7acffd8814ad4307bf63a67",
                device="cpu", max_loaded=1, preload=True)
# ... predict ...
router.unload()          # release the checkpoint from RAM
```

`max_loaded=1` keeps only one checkpoint resident (the multilingual one here),
bounding RSS. Prefer lazy loading (`preload=False`) when the process may not
need the model; it avoids unnecessary RAM residency.

## 6. Run the benchmark

```bash
# free candidates (FAST-only stand-in + real D4b deterministic + live OpenViking)
./.venv/bin/python tools/benchmark/d4b1_benchmark.py --candidates A,B

# add real upstream Laya (requires the isolated env + checkpoint)
~/.website-builder/laya/venv/bin/python tools/benchmark/d4b1_benchmark.py --candidates A,B,C
```

Output: `tools/benchmark/results/d4b1_results.json`.

## 7. Resource monitoring

```bash
# host headroom
free -m ; cat /proc/loadavg ; vmstat 1 3
# the Laya process
ps -o rss= -C python | awk '{s+=$1} END {print s/1024 " MB"}'
# OpenViking health (must stay ok)
curl -s http://127.0.0.1:1933/health
```

The benchmark records per-candidate RSS samples and cold/warm latency in its
result JSON.

## 8. Log inspection & failure diagnosis

| Symptom | Likely cause | Action |
|---|---|---|
| `ModuleNotFoundError: laya` | venv not created / wrong interpreter | run setup, use the venv python |
| `laya.load()` hangs | `transformers` TensorFlow probe | `export USE_TF=0` (laya sets this itself) |
| checkpoint download 401/404 | network / revision typo | re-check the pinned revision |
| weights hash mismatch | corrupted/partial download | delete `models/` and re-run setup |
| `CUDA` errors | CPU-only VPS | ensure `device="cpu"` and CPU torch wheels |
| OpenViking `unavailable` | service down | `systemctl --user status openviking-website` |

## 9. Rollback / removal

```bash
bash tools/benchmark/d4b1_laya_setup.sh --uninstall
```

Removes the isolated venv and the checkpoint. Nothing global was ever created,
so removal is complete. To roll back a *production integration* (if one is ever
enabled), set the Laya advisory flag back to `false` — see the acceptance
document's rollout/rollback section.

## 10. Cost

- Upstream Laya inference is **local**: **$0** additional API cost.
- The paid FAST downstream comparison is **separate** and gated behind explicit
  operator approval with a hard call cap enforced in code.

## 11. Safety rules

- Never install into the WB venv or a global environment.
- Never point the model at an unpinned GitHub `main`.
- Never enable a persistent service without measured justification and approval.
- Never represent the deterministic D4b planner as upstream Laya inference.
