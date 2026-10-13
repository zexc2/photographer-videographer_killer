#!/usr/bin/env bash
# ViPE（https://github.com/nv-tlabs/vipe）：给单目视频估计相机位姿 / 内参 / 深度，输出给 GEN3C 使用。幂等。
#
# 必须和 GEN3C 分开：ViPE 要求 Python ≥3.11，GEN3C 固定 3.10（官方也说明两者环境不兼容）。
# 环境：官方 envs/cu128.yml 的 conda 部分（CUDA 12.8 nvcc、eigen、cusparse/cublas/cusolver 头文件）
#       + Python 3.11 + torch cu128 + 非 editable 安装 ViPE（编译 vipe_ext），可以 conda-pack。
# 权重：首次运行时自动下载到 TORCH_HOME / HF_HOME（DROID-SLAM、UniDepth-L、GeoCalib、Video-Depth-Anything、
#       GroundingDINO、SAM、AOT，约 3–5 GB）。本脚本用一段 3 秒测试视频跑一次默认 pipeline 触发下载，
#       再把缓存打成一个 tar 存到 $ATLAS_VOL/weights/vipe/（有的来自 Google Drive，容易被限流，存一份更稳）。
# 许可：ViPE 代码 Apache-2.0（Unik3D 部分 CC BY-NC-SA 4.0）；默认 pipeline 用到的 UniDepth 权重为非商业许可
#       （待逐个核实）→ 商用前需要换成商业可用的深度模型。不需要 HF token。
source "$(dirname "$(readlink -f "$0")")/lib/common.sh"
NAME=vipe
VIPE_REF=${VIPE_REF:-8c9f36144e08}   # 2026-09-10 main（nvidia-vipe 1.2.0）
START=$SECONDS
busy_start
apt_ensure git curl rsync zstd build-essential ffmpeg libgl1 libglib2.0-0

SRC=$ATLAS_SRC/vipe
VIPE_SHA=$(repo_ensure https://github.com/nv-tlabs/vipe.git vipe "$VIPE_REF")
KEY=$(env_key "$(readlink -f "$0")")
P=$ATLAS_ENVS/$NAME

if env_restore "$NAME" "$KEY"; then
  MODE=restored
else
  MODE=installed
  conda_ensure
  rm -rf "$P"
  log "创建 vipe 环境（py3.11 + CUDA 12.8 nvcc + eigen）"
  "$CONDA_ROOT/bin/mamba" create -q -p "$P" -c nvidia/label/cuda-12.8.0 -c conda-forge \
    python=3.11 pip cuda-nvcc eigen zlib libcusparse-dev libcublas-dev libcusolver-dev ninja >/dev/null
  (
    export PATH=$P/bin:$PATH CUDA_HOME=$P CONDA_PREFIX=$P
    export USE_SYSTEM_EIGEN=1 CPATH=$P/include/eigen3:$P/include:${CPATH:-}
    PIP="$P/bin/python -m pip"
    $PIP install -q torch torchvision --index-url https://download.pytorch.org/whl/cu128
    $PIP install -q setuptools wheel
    log "编译安装 ViPE（vipe_ext）"
    cd "$SRC"
    $PIP install -q --no-build-isolation .
    "$P/bin/vipe" --help >/dev/null
  )
  env_pack "$NAME" "$KEY"
fi

# ---------------------------------------------------------------- 权重缓存
CACHE_TAR=$ATLAS_WEIGHTS/$NAME/model-cache.tar
if weights_ready "$NAME"; then
  log "从 Global volume 恢复 ViPE 模型缓存"
  tar -xf "$CACHE_TAR" -C "$ATLAS_CACHE"
else
  log "用测试视频跑一次默认 pipeline，触发全部模型下载"
  T=$ATLAS_WORK/vipe-smoke; mkdir -p "$T"
  ffmpeg -loglevel error -y -f lavfi -i testsrc2=size=640x480:rate=24 -t 3 -pix_fmt yuv420p "$T/smoke.mp4"
  (cd "$T" && PATH=$P/bin:$PATH "$P/bin/vipe" infer smoke.mp4 -o "$T/out")
  ls "$T/out/pose/" >/dev/null || die "ViPE 冒烟测试没有输出位姿"
  mkdir -p "$ATLAS_WEIGHTS/$NAME"
  # 打成一个大文件写入 Global volume（对象存储不适合大量小文件）
  tar -cf "$ATLAS_CACHE/vipe-cache.tar" -C "$ATLAS_CACHE" torch/hub hf
  vol_sync "$ATLAS_CACHE/vipe-cache.tar" "$ATLAS_WEIGHTS/$NAME/"
  mv -f "$ATLAS_WEIGHTS/$NAME/vipe-cache.tar" "$CACHE_TAR"
  rm -f "$ATLAS_CACHE/vipe-cache.tar"
  weights_mark "$NAME"
fi

report "$NAME" "$MODE" "$START" "ViPE $VIPE_SHA"
