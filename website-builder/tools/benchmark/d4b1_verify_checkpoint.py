#!/usr/bin/env python3
"""D4b.1 -- verify the downloaded upstream Laya multilingual checkpoint.

Checks the immutable weight artifact against the LFS-recorded SHA-256 and size
before the checkpoint is ever used, so an unverified download cannot be mistaken
for the official model.

    python d4b1_verify_checkpoint.py --dir ~/.website-builder/laya/models \\
        --expected-sha256 9d628f... --expected-size 643835514
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--expected-sha256", required=True)
    ap.add_argument("--expected-size", type=int, required=True)
    args = ap.parse_args()

    root = Path(args.dir).expanduser()
    weights = root / "model.safetensors"
    if not weights.exists():
        print(f"FAIL: {weights} not found")
        return 2

    size = weights.stat().st_size
    digest = sha256_of(weights)
    ok_size = size == args.expected_size
    ok_hash = digest == args.expected_sha256
    rl = root / "rl_agent_config.json"
    cfg = json.loads(rl.read_text()) if rl.exists() else {}
    report = {
        "weights_path": str(weights),
        "size_bytes": size,
        "size_ok": ok_size,
        "sha256": digest,
        "sha256_ok": ok_hash,
        "encoder": cfg.get("encoder"),
        "temperature": cfg.get("temperature"),
        "calibrated": cfg.get("temperature") not in (None, [1.0, 1.0, 1.0]),
        "verdict": "PASS" if (ok_size and ok_hash) else "FAIL",
    }
    print(json.dumps(report, indent=2))
    if not report["calibrated"]:
        print("NOTE: checkpoint ships with temperature [1.0,1.0,1.0] => UNCALIBRATED; "
              "raw confidence must not be trusted as a probability without refitting.")
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
