#!/usr/bin/env bash
# GEN3C（https://github.com/nv-tlabs/GEN3C）环境 + 权重。幂等，可重复运行。
#
# 环境：官方 cosmos-predict1.yaml（Python 3.10、torch 2.6、conda 内置 CUDA 12.4 toolkit）
#       + transformer-engine 1.12 + apex（编译）+ MoGe。和基础镜像的 torch 2.9 / CUDA 12.8 无关，完全自带。
#       装好后 conda-pack 到 $ATLAS_VOL/envpacks/，下次开机直接解压（方案 C）。
# 权重：nvidia/GEN3C-Cosmos-7B、nvidia/Cosmos-Tokenize1-CV8x8x8-720p、google-t5/t5-11b（跳过用不到的
#       TensorFlow / Flax 权重，约 76 GB，官方脚本会下载约 121 GB）→ $ATLAS_VOL/weights/gen3c/checkpoints
# 许可：代码 Apache-2.0；GEN3C / Cosmos 权重 NVIDIA Open Model License；T5 Apache-2.0。
#       三个 HF 仓库都不是 gated，不需要点同意；HF token 可选（有的话下载限速更宽松）。
source "$(dirname "$(readlink -f "$0")")/lib/common.sh"
NAME=gen3c
GEN3C_REF=${GEN3C_REF:-db2ffe1}       # 2026-06-15 main，核实过 INSTALL.md
TE_VERSION=1.12.0
START=$SECONDS
busy_start
apt_ensure git curl rsync zstd build-essential

SRC=$ATLAS_SRC/GEN3C
GEN3C_SHA=$(repo_ensure https://github.com/nv-tlabs/GEN3C.git GEN3C "$GEN3C_REF")
KEY=$(env_key "$(readlink -f "$0")")
P=$ATLAS_ENVS/$NAME

if env_restore "$NAME" "$KEY"; then
  MODE=restored
else
  MODE=installed
  conda_ensure
  rm -rf "$P"
  log "创建 conda 环境（cosmos-predict1.yaml）"
  "$CONDA_ROOT/bin/mamba" env create -q -p "$P" -f "$SRC/cosmos-predict1.yaml" >/dev/null
  (
    export PATH=$P/bin:$PATH CUDA_HOME=$P
    PIP="$P/bin/python -m pip"
    cd "$SRC"
    log "pip install -r requirements.txt"
    $PIP install -q -r requirements.txt
    # 官方用 ln -sf 把头文件软链到 $CONDA_PREFIX/include；conda-pack 对绝对路径软链不友好，这里改成复制
    site=$P/lib/python3.10/site-packages
    for d in "$site"/nvidia/*/include "$site"/triton/backends/nvidia/include; do
      [ -d "$d" ] || continue
      cp -rf "$d"/. "$P/include/"
      cp -rf "$d"/. "$P/include/python3.10/"
    done
    log "编译 transformer-engine $TE_VERSION（较慢）"
    $PIP install -q "transformer-engine[pytorch]==$TE_VERSION"
    log "编译 apex（较慢）"
    [ -d "$ATLAS_SRC/apex/.git" ] || git clone -q https://github.com/NVIDIA/apex "$ATLAS_SRC/apex"
    git -C "$ATLAS_SRC/apex" rev-parse HEAD > "$P/.apex_commit"
    $PIP install -q --disable-pip-version-check --no-cache-dir --no-build-isolation \
      --config-settings "--build-option=--cpp_ext" --config-settings "--build-option=--cuda_ext" "$ATLAS_SRC/apex"
    $PIP install -q git+https://github.com/microsoft/MoGe.git
    log "官方环境自检 scripts/test_environment.py"
    PYTHONPATH=$SRC python scripts/test_environment.py
  )
  env_pack "$NAME" "$KEY"
fi

# ---------------------------------------------------------------- 权重
if weights_ready "$NAME"; then
  log "GEN3C 权重已在 Global volume"
else
  hf_token_load || log "没有 HF token（这几个仓库不需要，继续）"
  TMP=$ATLAS_CACHE/gen3c_ckpt
  log "下载权重到容器盘 $TMP（约 76 GB），完成后拷到 Global volume"
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
# 代码目录里的 checkpoints 指向 Global volume（推理时只读，符合对象存储的使用方式）
ln -sfn "$ATLAS_WEIGHTS/$NAME/checkpoints" "$SRC/checkpoints"

report "$NAME" "$MODE" "$START" "GEN3C $GEN3C_SHA"
