#!/usr/bin/env bash
# 4C4D（CVPR 2026，https://github.com/yangzf-1023/4C4D，已核实为官方仓库）+ MASt3R 预处理环境。幂等。
#
# 两套环境：
#   4c4d    训练 / 渲染。官方 environment.yml 是 py3.7 + torch 1.12 + CUDA 11.6，不支持 H100（sm_90），
#           这里改用 py3.10 + torch 2.4.1 cu124 + conda CUDA 12.4 toolkit，重新编译仓库自带的 4 个 CUDA 扩展
#           （simple-knn / pointops2 / fused-ssim / diff-gaussian-rasterization），A100、H100 通用。
#   mast3r  稀疏视角点云初始化。官方 README 走 MAtCha（editable 安装 + 写死 /usr/local/cuda-11.8，无法打包），
#           这里直接装 naver/mast3r，阶段 3 用自己的脚本按已知相机位姿生成 points3D。
# 权重：MASt3R_ViTLarge_BaseDecoder_512_catmlpdpt_metric.pth（2.75 GB）→ $ATLAS_VOL/weights/mast3r/
# 许可：4C4D 代码 MIT，但内含 Inria 3DGS 光栅化器（仅限非商业 / 研究 / 评估）；
#       MASt3R 代码 CC BY-NC-SA 4.0，checkpoint 还受训练数据集许可约束 → 整条 4C4D 路线仅限非商业研究。
# 不需要 HF token。
source "$(dirname "$(readlink -f "$0")")/lib/common.sh"
FOURC_REF=${FOURC_REF:-ed6a3cb69782}   # 2026-04-07 main
MAST3R_REF=${MAST3R_REF:-f5209afc300c} # 2025-06-30 main
TORCH_PIP="torch==2.4.1 torchvision==0.19.1 --index-url https://download.pytorch.org/whl/cu124"
START=$SECONDS
busy_start
apt_ensure git curl rsync zstd build-essential ffmpeg libgl1 libglib2.0-0

FOURC_SHA=$(repo_ensure https://github.com/yangzf-1023/4C4D.git 4C4D "$FOURC_REF")
MAST3R_SHA=$(repo_ensure https://github.com/naver/mast3r.git mast3r "$MAST3R_REF" --recursive)
KEY=$(env_key "$(readlink -f "$0")")
MODE=restored

# ---------------------------------------------------------------- 4c4d 环境
if ! env_restore 4c4d "$KEY"; then
  MODE=installed
  conda_ensure
  P=$ATLAS_ENVS/4c4d; rm -rf "$P"
  log "创建 4c4d 环境（py3.10 + CUDA 12.4 toolkit + colmap）"
  # 必须固定 cuda-version=12.4：不固定时 conda-forge 会混进 CUDA 13.x 的 cudart，链接扩展时报 cannot find -lcudart（实测 2026-10-14）
  "$CONDA_ROOT/bin/mamba" create -q -p "$P" -c conda-forge \
    python=3.10 pip cmake ninja gcc=12 gxx=12 cuda-version=12.4 "cuda-toolkit=12.4.*" colmap plyfile=0.8.1 >/dev/null
  "$P/bin/nvcc" --version | grep -q 'release 12.4' || die "nvcc 不是 12.4"
  ls "$P"/lib/libcudart.so.12* >/dev/null 2>&1 || ls "$P"/targets/x86_64-linux/lib/libcudart.so.12* >/dev/null || die "缺少 CUDA 12 cudart"
  (
    export PATH=$P/bin:$PATH CUDA_HOME=$P
    # conda-forge 的 CUDA 头文件和库在 targets/x86_64-linux/ 下；nvcc 能找到，但 g++ 编译 .cpp 时找不到
    # cuda_runtime_api.h（pointops2 实测报错，2026-10-14），所以显式加进搜索路径
    export CPATH=$P/targets/x86_64-linux/include:${CPATH:-}
    export LIBRARY_PATH=$P/targets/x86_64-linux/lib:${LIBRARY_PATH:-}
    PIP="$P/bin/python -m pip"
    $PIP install -q $TORCH_PIP
    $PIP install -q torch_scatter -f https://data.pyg.org/whl/torch-2.4.0+cu124.html
    $PIP install -q tqdm==4.66.1 torchmetrics==0.11.4 imagesize==1.4.1 kornia==0.6.12 omegaconf==2.3.0 \
      scipy scikit-image opencv-python-headless imageio imageio-ffmpeg mediapy matplotlib open3d lpips
    cd "$ATLAS_SRC/4C4D"
    # 官方 yml 写的是 ./diff_gaussian_rasterization，实际目录是 diff-gaussian-rasterization
    for ext in simple-knn pointops2 fused-ssim-main diff-gaussian-rasterization; do
      log "编译 CUDA 扩展 $ext"
      $PIP install -q --no-build-isolation "./$ext"
    done
    python -c "import torch, simple_knn, pointops2_cuda, fused_ssim, diff_gaussian_rasterization, torch_scatter; print('4c4d imports ok, torch', torch.__version__)"
  )
  env_pack 4c4d "$KEY"
fi

# ---------------------------------------------------------------- mast3r 环境
if ! env_restore mast3r "$KEY"; then
  MODE=installed
  conda_ensure
  P=$ATLAS_ENVS/mast3r; rm -rf "$P"
  log "创建 mast3r 环境（py3.11）"
  "$CONDA_ROOT/bin/mamba" create -q -p "$P" -c conda-forge python=3.11 pip >/dev/null
  (
    PIP="$P/bin/python -m pip"
    $PIP install -q $TORCH_PIP
    cd "$ATLAS_SRC/mast3r"
    $PIP install -q -r requirements.txt -r dust3r/requirements.txt
    # asmk（大规模检索）和 curope（RoPE 加速，in-tree 编译无法打包）对 4 个视角都不需要，跳过
    PYTHONPATH=$ATLAS_SRC/mast3r "$P/bin/python" -c "import mast3r.model, dust3r; print('mast3r imports ok')"
  )
  env_pack mast3r "$KEY"
fi

# ---------------------------------------------------------------- MASt3R 权重
CK=MASt3R_ViTLarge_BaseDecoder_512_catmlpdpt_metric.pth
if weights_ready mast3r; then
  log "MASt3R 权重已在 Global volume"
else
  log "下载 $CK（2.75 GB）"
  curl -fL --retry 3 -o "$ATLAS_CACHE/$CK" "https://download.europe.naverlabs.com/ComputerVision/MASt3R/$CK"
  [ "$(stat -c %s "$ATLAS_CACHE/$CK")" = 2754910614 ] || die "$CK 大小不对"
  mkdir -p "$ATLAS_WEIGHTS/mast3r"
  vol_sync "$ATLAS_CACHE/$CK" "$ATLAS_WEIGHTS/mast3r/"
  weights_mark mast3r
  rm -f "$ATLAS_CACHE/$CK"
fi

report 4c4d "$MODE" "$START" "4C4D $FOURC_SHA / mast3r $MAST3R_SHA"
