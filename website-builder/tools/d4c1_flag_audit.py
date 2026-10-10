#!/usr/bin/env python3
"""D4c.1 flag-audit probe (ZERO paid calls).

Proves that enabling ``laya.enabled`` (with the OpenViking adapter) on the
intake path activates ONLY deterministic D4b context preparation + OpenViking
retrieval -- and does NOT import/load the upstream Laya multilingual model
(torch / transformers / the convaiinnovations checkpoint).

Runs the FULL intake seam with a FAKE FAST agent (no paid call) and inspects
``sys.modules`` afterwards.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Snapshot BEFORE
before = set(sys.modules)

import app.hermes.adapter as adapter_mod  # noqa: E402
import tools.d4c1_dry_run as dry  # noqa: E402

adapter_mod.AIAgent = dry.FakeAgent
import tools.d4c1_real_fast_smoke as smoke  # noqa: E402

rc = smoke.main(["--run", "B", "--out", "/tmp/d4c1_flagaudit_B.json"])

after = set(sys.modules)
new_mods = sorted(m.split(".")[0] for m in (after - before))
suspicious = sorted({m for m in new_mods if m in {
    "torch", "transformers", "safetensors", "laya", "sentencepiece",
    "accelerate", "bitsandbytes", "onnxruntime", "tensorflow", "jax",
}})

# grep the whole app package for upstream-model loading primitives
import subprocess  # noqa: E402

grep = subprocess.run(
    ["grep", "-rniE",
     r"convaiinnovations|from_pretrained|AutoModel|AutoTokenizer|model\.safetensors|load_checkpoint|SentenceTransformer",
     str(ROOT / "app")],
    capture_output=True, text=True,
)
hits = [ln for ln in grep.stdout.splitlines() if ln.strip()]

print(json.dumps({
    "run": "B",
    "new_top_level_modules_count": len(new_mods),
    "suspicious_model_modules_imported": suspicious,
    "upstream_laya_load_primitives_in_app": hits,
    "app_contains_no_model_loader": len(hits) == 0,
}, indent=2))
