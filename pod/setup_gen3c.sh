#!/usr/bin/env bash
# GEN3C (https://github.com/nv-tlabs/GEN3C) env + weights. Idempotent; safe to re-run.
#
# Env: official cosmos-predict1.yaml (Python 3.10, torch 2.6, conda-provided CUDA 12.4 toolkit)
#       + transformer-engine 1.12 + apex (built) + MoGe. Fully self-contained, independent of the base image's torch 2.9 / CUDA 12.8.
#       After install it is conda-packed to $ATLAS_VOL/envpacks/ and unpacked on the next boot (option C).
# Weights: nvidia/GEN3C-Cosmos-7B, nvidia/Cosmos-Tokenize1-CV8x8x8-720p, google-t5/t5-11b (skipping the unused
#       TensorFlow / Flax weights: about 76 GB vs about 121 GB with the official script) -> $ATLAS_VOL/weights/gen3c/checkpoints
# License: code Apache-2.0; GEN3C / Cosmos weights NVIDIA Open Model License; T5 Apache-2.0.
#       None of the three HF repos is gated, so nothing to accept; an HF token is optional (it gives looser download rate limits).
source "$(dirname "$(readlink -f "$0")")/lib/common.sh"
NAME=gen3c
GEN3C_REF=${GEN3C_REF:-db2ffe1}       # 2026-06-15 main, INSTALL.md verified
TE_VERSION=1.12.0
# apex pinned to 25.09 (tested 2026-10-14):
#   - master (since 2025-11) uses list[int] annotations (custom_op) unsupported by torch 2.6 -> import megatron.core fails
#   - 24.10 (the official Docker base image's version) still uses .type(), incompatible with torch 2.6 headers -> build fails
#   - 25.09 has neither problem: List[int] + .scalar_type()
APEX_REF=25.09
START=$SECONDS
busy_start
apt_ensure git curl rsync zstd build-essential

SRC=$ATLAS_SRC/GEN3C
GEN3C_SHA=$(repo_ensure https://github.com/nv-tlabs/GEN3C.git GEN3C "$GEN3C_REF")
# Environment pack revision (= the content hash before the 2026-10-15 translation, so existing packs still match).
# Bump it (any new 12-hex value) whenever this script changes what gets installed.
ENV_REV=273108751c00
KEY=$(env_key "$(readlink -f "$0")")
P=$ATLAS_ENVS/$NAME

if env_restore "$NAME" "$KEY"; then
  MODE=restored
else
  MODE=installed
  conda_ensure
  rm -rf "$P"
  log "Creating conda env (cosmos-predict1.yaml)"
  "$CONDA_ROOT/bin/mamba" env create -q -p "$P" -f "$SRC/cosmos-predict1.yaml" >/dev/null
  (
    export PATH=$P/bin:$PATH CUDA_HOME=$P
    PIP="$P/bin/python -m pip"
    cd "$SRC"
    log "pip install -r requirements.txt"
    $PIP install -q -r requirements.txt
    # Upstream symlinks the headers into $CONDA_PREFIX/include with ln -sf; conda-pack handles absolute symlinks poorly, so copy instead
    site=$P/lib/python3.10/site-packages
    for d in "$site"/nvidia/*/include "$site"/triton/backends/nvidia/include; do
      [ -d "$d" ] || continue
      cp -rf "$d"/. "$P/include/"
      cp -rf "$d"/. "$P/include/python3.10/"
    done
    log "Building transformer-engine $TE_VERSION (slow)"
    $PIP install -q "transformer-engine[pytorch]==$TE_VERSION"
    log "Building apex (slow)"
    repo_ensure https://github.com/NVIDIA/apex.git apex "$APEX_REF" > "$P/.apex_commit"
    $PIP install -q --disable-pip-version-check --no-cache-dir --no-build-isolation \
      --config-settings "--build-option=--cpp_ext" --config-settings "--build-option=--cuda_ext" "$ATLAS_SRC/apex"
    $PIP install -q git+https://github.com/microsoft/MoGe.git
    # MoGe upgrades huggingface-hub to 2.x, which breaks transformers 4.49 (seen 2026-10-13) -> reinstall official requirements to restore all pins
    $PIP install -q -r requirements.txt
    log "Official env self-check scripts/test_environment.py"
    # The official self-check returns 0 even when imports fail, so look for [ERROR] in its output to avoid packing a broken env
    PYTHONPATH=$SRC python scripts/test_environment.py 2>&1 | tee "$ATLAS_LOGS/gen3c-envtest.log"
    if grep -q 'ERROR' "$ATLAS_LOGS/gen3c-envtest.log"; then echo "GEN3C env self-check failed" >&2; exit 1; fi
    # MoGe 3.x needs numpy>=2, conflicting with GEN3C's pinned numpy 1.26; MoGe is only for single-image mode and we use video + ViPE, so it's not in the required-imports check
    python -c "import transformers, megatron.core, transformer_engine, apex; print('gen3c imports ok')"
  )
  env_pack "$NAME" "$KEY"
fi

# ---------------------------------------------------------------- weights
if weights_ready "$NAME"; then
  log "GEN3C weights already on Global volume"
else
  hf_token_load || log "No HF token (not needed for these repos, continuing)"
  TMP=$ATLAS_CACHE/gen3c_ckpt
  log "Downloading weights to container disk $TMP (about 76 GB), then copying to Global volume"
  "$P/bin/python" - "$TMP" <<'PY'
import sys
from huggingface_hub import snapshot_download
root = sys.argv[1]
snapshot_download("nvidia/GEN3C-Cosmos-7B", local_dir=f"{root}/Gen3C-Cosmos-7B",
                  allow_patterns=["model.pt", "config.json", "*.md", "guardrail/*"])
snapshot_download("nvidia/Cosmos-Tokenize1-CV8x8x8-720p", local_dir=f"{root}/Cosmos-Tokenize1-CV8x8x8-720p")
snapshot_download("google-t5/t5-11b", local_dir=f"{root}/google-t5/t5-11b",
                  ignore_patterns=["tf_model.h5", "*.msgpack", "*.onnx", "rust_model.ot", "*.h5"])
PY
  rm -rf "$TMP"/*/.cache "$TMP"/*/*/.cache
  vol_sync "$TMP/" "$ATLAS_WEIGHTS/$NAME/checkpoints/"
  weights_mark "$NAME"
  rm -rf "$TMP"
fi
# checkpoints in the code dir points to Global volume (read-only during inference, which suits object storage)
ln -sfn "$ATLAS_WEIGHTS/$NAME/checkpoints" "$SRC/checkpoints"

report "$NAME" "$MODE" "$START" "GEN3C $GEN3C_SHA"
