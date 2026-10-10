#!/usr/bin/env bash
# D4b.1 -- isolated upstream Laya environment setup (APPROVAL-GATED).
#
# This script performs the ONLY substantial download in D4b.1: an isolated venv
# plus the official multilingual checkpoint. It is deliberately NOT run by the
# agent until the operator approves the D4b.1 resource checkpoint.
#
# Isolation guarantees:
#   * a DEDICATED venv at  ~/.website-builder/laya/venv   (never the WB venv,
#     never a global site-packages, never Hermes Trade's environment);
#   * a DEDICATED HF cache at ~/.website-builder/laya/models  (outside git);
#   * NO systemd unit, NO persistent service, NO PATH change;
#   * fully reversible: delete the two directories above (see --uninstall).
#
# Pins (verified before writing this file):
#   package   : laya==0.4.1            (PyPI; source tag v0.4.1 = 1adc59f7e371)
#   checkpoint: convaiinnovations/laya-multilingual
#   revision  : e4e9ddf21a7b1903b7acffd8814ad4307bf63a67  (HF commit)
#   weights   : model.safetensors  sha256 9d628fd971b700382ac6f65920a86f149777b2e748e0c955fb3b19695aa8f204
#               size 643835514 bytes (~644 MB)
#   license   : Apache-2.0
#
# Usage:
#   bash tools/benchmark/d4b1_laya_setup.sh            # create venv + download
#   bash tools/benchmark/d4b1_laya_setup.sh --verify   # verify only
#   bash tools/benchmark/d4b1_laya_setup.sh --uninstall
set -euo pipefail

LAYA_ROOT="${HOME}/.website-builder/laya"
VENV="${LAYA_ROOT}/venv"
MODELS="${LAYA_ROOT}/models"
PKG_VERSION="0.4.1"
CHECKPOINT_REPO="convaiinnovations/laya-multilingual"
CHECKPOINT_REV="e4e9ddf21a7b1903b7acffd8814ad4307bf63a67"
WEIGHTS_SHA256="9d628fd971b700382ac6f65920a86f149777b2e748e0c955fb3b19695aa8f204"
WEIGHTS_SIZE="643835514"

log() { printf '[d4b1-laya] %s\n' "$*"; }

uninstall() {
  log "removing ${VENV} and ${MODELS}"
  rm -rf "${VENV}" "${MODELS}"
  log "uninstalled. No global package or service was ever created."
}

verify() {
  log "checking package version..."
  "${VENV}/bin/python" -I -c "import laya; print('laya', laya.__version__)"
  log "checking checkpoint weights hash..."
  "${VENV}/bin/python" "${0%/*}/d4b1_verify_checkpoint.py" \
      --dir "${MODELS}" --expected-sha256 "${WEIGHTS_SHA256}" --expected-size "${WEIGHTS_SIZE}"
}

if [[ "${1:-}" == "--uninstall" ]]; then uninstall; exit 0; fi
if [[ "${1:-}" == "--verify" ]]; then verify; exit 0; fi

mkdir -p "${LAYA_ROOT}"
log "creating isolated venv at ${VENV}"
python3 -m venv "${VENV}"

log "installing pinned CPU stack + laya==${PKG_VERSION} (isolated)"
"${VENV}/bin/python" -m pip install --quiet --upgrade pip
# CPU-only torch to avoid pulling CUDA wheels onto a CPU-only VPS.
"${VENV}/bin/python" -m pip install --quiet \
    --index-url https://download.pytorch.org/whl/cpu torch
"${VENV}/bin/python" -m pip install --quiet \
    "transformers>=4.48.0" "safetensors>=0.4.0" "huggingface_hub>=0.20.0" "numpy>=1.20.0"
"${VENV}/bin/python" -m pip install --quiet "laya==${PKG_VERSION}"

log "downloading ONLY the multilingual checkpoint @ ${CHECKPOINT_REV}"
export HF_HOME="${LAYA_ROOT}/hf"
export HF_HUB_CACHE="${MODELS}"
"${VENV}/bin/python" - <<PY
from huggingface_hub import snapshot_download
p = snapshot_download(
    repo_id="${CHECKPOINT_REPO}",
    revision="${CHECKPOINT_REV}",
    local_dir="${MODELS}",
    allow_patterns=["*.json", "*.safetensors", "tokenizer/*", "encoder/*"],
)
print("downloaded to", p)
PY

log "measuring disk usage"
du -sh "${VENV}" "${MODELS}" || true
verify
log "setup complete. Run the benchmark with:"
log "  ${VENV}/bin/python tools/benchmark/d4b1_benchmark.py --candidates A,B,C"
