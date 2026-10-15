#!/usr/bin/env bash
# ViPE (https://github.com/nv-tlabs/vipe): estimates camera poses / intrinsics / depth for monocular video, used as GEN3C input. Idempotent.
#
# Must be separate from GEN3C: ViPE needs Python >=3.11, GEN3C pins 3.10 (upstream also states the envs are incompatible).
# Env: the conda part of the official envs/cu128.yml (CUDA 12.8 nvcc, eigen, cusparse/cublas/cusolver headers)
#       + Python 3.11 + torch cu128 + non-editable ViPE install (builds vipe_ext), so it can be conda-packed.
# Weights: downloaded on first run into TORCH_HOME / HF_HOME (DROID-SLAM, UniDepth-L, GeoCalib, Video-Depth-Anything,
#       GroundingDINO, SAM, AOT, about 3-5 GB). This script runs the default pipeline once on a 3 s test video to trigger the downloads,
#       then stores the cache as one tar in $ATLAS_VOL/weights/vipe/ (some come from Google Drive, which rate-limits; a copy is safer).
# License: ViPE code is Apache-2.0 (the Unik3D part is CC BY-NC-SA 4.0); the UniDepth weights used by the default pipeline are non-commercial
#       (to be verified one by one) -> swap in a commercially usable depth model before commercial use. No HF token needed.
source "$(dirname "$(readlink -f "$0")")/lib/common.sh"
NAME=vipe
VIPE_REF=${VIPE_REF:-8c9f36144e08}   # 2026-09-10 main (nvidia-vipe 1.2.0)
START=$SECONDS
busy_start
apt_ensure git curl rsync zstd build-essential ffmpeg libgl1 libglib2.0-0

SRC=$ATLAS_SRC/vipe
VIPE_SHA=$(repo_ensure https://github.com/nv-tlabs/vipe.git vipe "$VIPE_REF")
# Environment pack revision (= the content hash before the 2026-10-15 translation, so existing packs still match).
# Bump it (any new 12-hex value) whenever this script changes what gets installed.
ENV_REV=dfb6dc3513ad
KEY=$(env_key "$(readlink -f "$0")")
P=$ATLAS_ENVS/$NAME

if env_restore "$NAME" "$KEY"; then
  MODE=restored
else
  MODE=installed
  conda_ensure
  rm -rf "$P"
  log "Creating vipe env (py3.11 + CUDA 12.8 nvcc + eigen)"
  "$CONDA_ROOT/bin/mamba" create -q -p "$P" -c nvidia/label/cuda-12.8.0 -c conda-forge \
    python=3.11 pip cuda-version=12.8 "cuda-nvcc=12.8.*" eigen zlib libcusparse-dev libcublas-dev libcusolver-dev ninja >/dev/null
  # cuda-version=12.8 must be pinned: otherwise conda-forge picks nvcc 13.x, which mismatches torch cu128 and the build fails (seen 2026-10-13)
  "$P/bin/nvcc" --version | grep -q 'release 12.8' || die "nvcc is not 12.8"
  (
    export PATH=$P/bin:$PATH CUDA_HOME=$P CONDA_PREFIX=$P
    export USE_SYSTEM_EIGEN=1 CPATH=$P/include/eigen3:$P/include:${CPATH:-}
    PIP="$P/bin/python -m pip"
    $PIP install -q torch torchvision --index-url https://download.pytorch.org/whl/cu128
    $PIP install -q setuptools wheel
    log "Building and installing ViPE (vipe_ext)"
    cd "$SRC"
    $PIP install -q --no-build-isolation .
    "$P/bin/vipe" --help >/dev/null
  )
  env_pack "$NAME" "$KEY"
fi

# ---------------------------------------------------------------- weights cache
CACHE_TAR=$ATLAS_WEIGHTS/$NAME/model-cache.tar
if weights_ready "$NAME"; then
  log "Restoring ViPE model cache from Global volume"
  tar -xf "$CACHE_TAR" -C "$ATLAS_CACHE"
else
  log "Running the default pipeline once on a test video to trigger all model downloads"
  T=$ATLAS_WORK/vipe-smoke; mkdir -p "$T"
  ffmpeg -loglevel error -y -f lavfi -i testsrc2=size=640x480:rate=24 -t 3 -pix_fmt yuv420p "$T/smoke.mp4"
  (cd "$T" && PATH=$P/bin:$PATH "$P/bin/vipe" infer smoke.mp4 -o "$T/out")
  ls "$T/out/pose/" >/dev/null || die "ViPE smoke test produced no poses"
  mkdir -p "$ATLAS_WEIGHTS/$NAME"
  # Write to Global volume as one big file (object storage handles many small files poorly)
  tar -cf "$ATLAS_CACHE/vipe-cache.tar" -C "$ATLAS_CACHE" torch/hub hf
  vol_sync "$ATLAS_CACHE/vipe-cache.tar" "$ATLAS_WEIGHTS/$NAME/"
  mv -f "$ATLAS_WEIGHTS/$NAME/vipe-cache.tar" "$CACHE_TAR"
  rm -f "$ATLAS_CACHE/vipe-cache.tar"
  weights_mark "$NAME"
fi

report "$NAME" "$MODE" "$START" "ViPE $VIPE_SHA"
