#!/usr/bin/env bash
# 4C4D (CVPR 2026, https://github.com/yangzf-1023/4C4D, verified as the official repo) + MASt3R preprocessing env. Idempotent.
#
# Two environments:
#   4c4d    training / rendering. The official environment.yml is py3.7 + torch 1.12 + CUDA 11.6, which does not support H100 (sm_90);
#           here we use py3.10 + torch 2.4.1 cu124 + conda CUDA 12.4 toolkit and rebuild the repo's 4 bundled CUDA extensions
#           (simple-knn / pointops2 / fused-ssim / diff-gaussian-rasterization); works on both A100 and H100.
#   mast3r  sparse-view point cloud init. The official README goes through MAtCha (editable install + hard-coded /usr/local/cuda-11.8, can't be packed);
#           here we install naver/mast3r directly; stage 3 uses our own script to build points3D from known camera poses.
# Weights: MASt3R_ViTLarge_BaseDecoder_512_catmlpdpt_metric.pth (2.75 GB) -> $ATLAS_VOL/weights/mast3r/
# License: 4C4D code is MIT but bundles the Inria 3DGS rasterizer (non-commercial / research / evaluation only);
#       MASt3R code is CC BY-NC-SA 4.0 and the checkpoint is also bound by its training-data licenses -> the whole 4C4D path is non-commercial research only.
# No HF token needed.
source "$(dirname "$(readlink -f "$0")")/lib/common.sh"
FOURC_REF=${FOURC_REF:-ed6a3cb69782}   # 2026-04-07 main
MAST3R_REF=${MAST3R_REF:-f5209afc300c} # 2025-06-30 main
TORCH_PIP="torch==2.4.1 torchvision==0.19.1 --index-url https://download.pytorch.org/whl/cu124"
START=$SECONDS
busy_start
apt_ensure git curl rsync zstd build-essential ffmpeg libgl1 libglib2.0-0

FOURC_SHA=$(repo_ensure https://github.com/yangzf-1023/4C4D.git 4C4D "$FOURC_REF")
MAST3R_SHA=$(repo_ensure https://github.com/naver/mast3r.git mast3r "$MAST3R_REF" --recursive)
# Environment pack revision (= the content hash before the 2026-10-15 translation, so existing packs still match).
# Bump it (any new 12-hex value) whenever this script changes what gets installed.
ENV_REV=1aa635c4e05d
KEY=$(env_key "$(readlink -f "$0")")
MODE=restored

# ---------------------------------------------------------------- 4c4d env
if ! env_restore 4c4d "$KEY"; then
  MODE=installed
  conda_ensure
  P=$ATLAS_ENVS/4c4d; rm -rf "$P"
  log "Creating 4c4d env (py3.10 + CUDA 12.4 toolkit + colmap)"
  # cuda-version=12.4 must be pinned: otherwise conda-forge mixes in a CUDA 13.x cudart and linking the extensions fails with cannot find -lcudart (seen 2026-10-14)
  "$CONDA_ROOT/bin/mamba" create -q -p "$P" -c conda-forge \
    python=3.10 pip cmake ninja gcc=12 gxx=12 cuda-version=12.4 "cuda-toolkit=12.4.*" colmap plyfile=0.8.1 >/dev/null
  "$P/bin/nvcc" --version | grep -q 'release 12.4' || die "nvcc is not 12.4"
  ls "$P"/lib/libcudart.so.12* >/dev/null 2>&1 || ls "$P"/targets/x86_64-linux/lib/libcudart.so.12* >/dev/null || die "CUDA 12 cudart missing"
  (
    export PATH=$P/bin:$PATH CUDA_HOME=$P
    # conda-forge puts CUDA headers and libs under targets/x86_64-linux/; nvcc finds them but g++ compiling .cpp files does not find
    # cuda_runtime_api.h (pointops2 failed on this, 2026-10-14), so add them to the search paths explicitly
    export CPATH=$P/targets/x86_64-linux/include:${CPATH:-}
    export LIBRARY_PATH=$P/targets/x86_64-linux/lib:${LIBRARY_PATH:-}
    PIP="$P/bin/python -m pip"
    $PIP install -q $TORCH_PIP
    $PIP install -q torch_scatter -f https://data.pyg.org/whl/torch-2.4.0+cu124.html
    $PIP install -q tqdm==4.66.1 torchmetrics==0.11.4 imagesize==1.4.1 kornia==0.6.12 omegaconf==2.3.0 \
      scipy scikit-image opencv-python-headless imageio imageio-ffmpeg mediapy matplotlib open3d lpips
    cd "$ATLAS_SRC/4C4D"
    # The official yml says ./diff_gaussian_rasterization, but the actual directory is diff-gaussian-rasterization
    for ext in simple-knn pointops2 fused-ssim-main diff-gaussian-rasterization; do
      log "Building CUDA extension $ext"
      $PIP install -q --no-build-isolation "./$ext"
    done
    python -c "import torch, simple_knn, pointops2_cuda, fused_ssim, diff_gaussian_rasterization, torch_scatter; print('4c4d imports ok, torch', torch.__version__)"
  )
  env_pack 4c4d "$KEY"
fi

# ---------------------------------------------------------------- mast3r env
if ! env_restore mast3r "$KEY"; then
  MODE=installed
  conda_ensure
  P=$ATLAS_ENVS/mast3r; rm -rf "$P"
  log "Creating mast3r env (py3.11)"
  "$CONDA_ROOT/bin/mamba" create -q -p "$P" -c conda-forge python=3.11 pip >/dev/null
  (
    PIP="$P/bin/python -m pip"
    $PIP install -q $TORCH_PIP
    cd "$ATLAS_SRC/mast3r"
    $PIP install -q -r requirements.txt -r dust3r/requirements.txt
    # asmk (large-scale retrieval) and curope (RoPE speed-up, built in-tree so it can't be packed) aren't needed for 4 views; skipped
    PYTHONPATH=$ATLAS_SRC/mast3r "$P/bin/python" -c "import mast3r.model, dust3r; print('mast3r imports ok')"
  )
  env_pack mast3r "$KEY"
fi

# ---------------------------------------------------------------- MASt3R weights
CK=MASt3R_ViTLarge_BaseDecoder_512_catmlpdpt_metric.pth
if weights_ready mast3r; then
  log "MASt3R weights already on Global volume"
else
  log "Downloading $CK (2.75 GB)"
  curl -fL --retry 3 -o "$ATLAS_CACHE/$CK" "https://download.europe.naverlabs.com/ComputerVision/MASt3R/$CK"
  [ "$(stat -c %s "$ATLAS_CACHE/$CK")" = 2754910614 ] || die "$CK has the wrong size"
  mkdir -p "$ATLAS_WEIGHTS/mast3r"
  vol_sync "$ATLAS_CACHE/$CK" "$ATLAS_WEIGHTS/mast3r/"
  weights_mark mast3r
  rm -f "$ATLAS_CACHE/$CK"
fi

report 4c4d "$MODE" "$START" "4C4D $FOURC_SHA / mast3r $MAST3R_SHA"
