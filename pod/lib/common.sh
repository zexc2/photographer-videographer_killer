#!/usr/bin/env bash
# pod 端公共函数库。由 setup_*.sh 和任务脚本 source，不单独运行。
#
# 存储约定（见 docs/storage-and-env.md）：
#   $ATLAS_VOL    Global volume（对象存储），只存冷数据：weights/ datasets/ envpacks/ results/ setup-reports/
#   $ATLAS_LOCAL  容器盘 /opt/atlas（pod stop 后清空）：envs/ src/ work/ cache/ secrets/ logs/ bin/
# 环境方案 C：装一次 → conda-pack 打包成一个 .tar.zst 存到 $ATLAS_VOL/envpacks/ → 下次开机解压复用。

set -euo pipefail

# ---------------------------------------------------------------- 路径
if [ -z "${ATLAS_VOL:-}" ]; then
  # 没有从 gpu-up 拿到变量时自动判断：同时挂 network volume 时 Global 在 /workspace-global
  if mountpoint -q /workspace-global 2>/dev/null; then ATLAS_VOL=/workspace-global; else ATLAS_VOL=/workspace; fi
fi
ATLAS_LOCAL=${ATLAS_LOCAL:-/opt/atlas}
export ATLAS_VOL ATLAS_LOCAL
export ATLAS_ENVS=$ATLAS_LOCAL/envs ATLAS_SRC=$ATLAS_LOCAL/src ATLAS_WORK=$ATLAS_LOCAL/work
export ATLAS_CACHE=$ATLAS_LOCAL/cache ATLAS_LOGS=$ATLAS_LOCAL/logs
export ATLAS_WEIGHTS=$ATLAS_VOL/weights ATLAS_DATA=$ATLAS_VOL/datasets ATLAS_PACKS=$ATLAS_VOL/envpacks
export ATLAS_RESULTS=$ATLAS_VOL/results
export CONDA_ROOT=$ATLAS_LOCAL/miniforge
# 各种下载缓存都放容器盘（Global volume 没有文件锁 / 权限位，HF 缓存直接放上面会出问题）
export HF_HOME=$ATLAS_CACHE/hf TORCH_HOME=$ATLAS_CACHE/torch PIP_CACHE_DIR=$ATLAS_CACHE/pip
export XDG_CACHE_HOME=$ATLAS_CACHE/xdg
# CUDA 扩展同时编译 A100 (sm_80) 和 H100 (sm_90)，打包后两种卡通用
export TORCH_CUDA_ARCH_LIST=${TORCH_CUDA_ARCH_LIST:-"8.0;9.0"}
export MAX_JOBS=${MAX_JOBS:-$(( $(nproc) > 16 ? 16 : $(nproc) ))}

mkdir -p "$ATLAS_ENVS" "$ATLAS_SRC" "$ATLAS_WORK" "$ATLAS_CACHE" "$ATLAS_LOGS" "$ATLAS_LOCAL/bin" "$ATLAS_LOCAL/secrets"
chmod 700 "$ATLAS_LOCAL/secrets"
if mountpoint -q "$ATLAS_VOL" 2>/dev/null || [ -d "$ATLAS_VOL" ]; then
  mkdir -p "$ATLAS_WEIGHTS" "$ATLAS_DATA" "$ATLAS_PACKS" "$ATLAS_RESULTS" "$ATLAS_VOL/setup-reports"
fi

log() { echo "[$(date -u +%H:%M:%S)] $*" >&2; }
die() { log "错误: $*"; exit 1; }

# ---------------------------------------------------------------- busy 心跳
# 下载 / 安装 / CPU 预处理时 GPU 为 0，巡检看到这个文件新鲜就不会判空闲。
busy_start() {
  touch "$ATLAS_LOCAL/.atlas_busy"
  ( parent=$$; while kill -0 "$parent" 2>/dev/null; do touch "$ATLAS_LOCAL/.atlas_busy"; sleep 60; done ) &
  ATLAS_BUSY_PID=$!
  trap 'kill ${ATLAS_BUSY_PID:-0} 2>/dev/null || true' EXIT
}

# ---------------------------------------------------------------- 系统包 / conda
apt_ensure() {  # apt_ensure pkg...
  local missing=()
  for p in "$@"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
  [ ${#missing[@]} -eq 0 ] && return 0
  log "apt 安装: ${missing[*]}"
  DEBIAN_FRONTEND=noninteractive apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}" >/dev/null
}

conda_ensure() {
  if [ ! -x "$CONDA_ROOT/bin/conda" ]; then
    log "安装 Miniforge 到 $CONDA_ROOT"
    local inst=$ATLAS_CACHE/miniforge.sh
    curl -fsSL -o "$inst" "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh"
    bash "$inst" -b -p "$CONDA_ROOT" >/dev/null
  fi
  # shellcheck disable=SC1091
  source "$CONDA_ROOT/etc/profile.d/conda.sh"
  "$CONDA_ROOT/bin/conda" config --set always_yes yes --set changeps1 no >/dev/null
  [ -x "$CONDA_ROOT/bin/conda-pack" ] || "$CONDA_ROOT/bin/mamba" install -q -n base -c conda-forge conda-pack >/dev/null
}

env_python() { echo "$ATLAS_ENVS/$1/bin/python"; }

# 激活一个已装好或已解压的环境（不依赖 conda 本身，打包后的环境自带 python）
env_activate() {
  local p=$ATLAS_ENVS/$1
  [ -x "$p/bin/python" ] || die "环境 $1 不存在"
  # shellcheck disable=SC1091
  source "$p/bin/activate" 2>/dev/null || export PATH="$p/bin:$PATH"
}

# ---------------------------------------------------------------- 环境打包 / 恢复（方案 C）
# 版本键 = 安装脚本 + common.sh 的哈希。脚本改了就会重装、重新打包。
env_key() {  # env_key <setup 脚本路径>
  cat "$1" "${BASH_SOURCE[0]}" | sha256sum | cut -c1-12
}

env_restore() {  # env_restore <name> <key>；成功返回 0，没有包返回 1
  local name=$1 key=$2 dst=$ATLAS_ENVS/$1 pack=$ATLAS_PACKS/$1-$2.tar.zst
  if [ -f "$dst/.atlas_key" ] && [ "$(cat "$dst/.atlas_key")" = "$key" ]; then
    log "环境 $name 已在容器盘上（$key）"; return 0
  fi
  [ -f "$pack" ] || return 1
  log "从 Global volume 恢复环境 $name（$(du -h "$pack" | cut -f1)）"
  rm -rf "$dst"; mkdir -p "$dst"
  # 先整块读到本地再解压：对象存储顺序读一个大文件最快
  local t0=$SECONDS tmp=$ATLAS_CACHE/$name.tar.zst
  cp "$pack" "$tmp"
  log "  复制耗时 $((SECONDS - t0))s"
  zstd -dc -T0 "$tmp" | tar -x -C "$dst"
  rm -f "$tmp"
  "$dst/bin/conda-unpack" >/dev/null 2>&1 || true
  echo "$key" > "$dst/.atlas_key"
  log "  恢复完成，共 $((SECONDS - t0))s"
}

env_pack() {  # env_pack <name> <key>：把容器盘上的环境打包存到 Global volume
  local name=$1 key=$2 src=$ATLAS_ENVS/$1 pack=$ATLAS_PACKS/$1-$2.tar.zst
  local tmp=$ATLAS_CACHE/$name-$key.tar.zst t0=$SECONDS
  log "打包环境 $name → $pack"
  "$CONDA_ROOT/bin/conda-pack" -p "$src" --format tar -o "$ATLAS_CACHE/$name.tar" --force --quiet \
    --ignore-missing-files
  zstd -q -T0 -10 --rm -f "$ATLAS_CACHE/$name.tar" -o "$tmp"
  # 写 Global volume：一次写一个大文件；先写临时名再改名（rename 不保证原子，所以改名后再核对大小）
  cp "$tmp" "$pack.part" && mv -f "$pack.part" "$pack"
  [ "$(stat -c %s "$tmp")" = "$(stat -c %s "$pack")" ] || die "打包文件大小不一致"
  rm -f "$tmp"
  # 旧版本的包删掉，只保留当前版本
  find "$ATLAS_PACKS" -maxdepth 1 -name "$name-*.tar.zst" ! -name "$name-$key.tar.zst" -delete
  echo "$key" > "$src/.atlas_key"
  log "  打包完成 $(du -h "$pack" | cut -f1)，耗时 $((SECONDS - t0))s"
}

# ---------------------------------------------------------------- 代码仓库
# 固定 commit 克隆到容器盘。代码不打进环境包，每次开机重新克隆（几秒钟）。
repo_ensure() {  # repo_ensure <url> <dir名> <commit或分支> [--recursive]
  local url=$1 dir=$ATLAS_SRC/$2 ref=$3 rec=${4:-}
  if [ ! -d "$dir/.git" ]; then
    log "克隆 $url → $dir"
    git clone -q ${rec:+--recursive} "$url" "$dir"
  fi
  git -C "$dir" fetch -q --all --tags 2>/dev/null || true
  git -C "$dir" checkout -q "$ref"
  [ -n "$rec" ] && git -C "$dir" submodule update -q --init --recursive
  git -C "$dir" rev-parse HEAD
}

# ---------------------------------------------------------------- Hugging Face token
# 来源二选一：RunPod Secret 注入的环境变量 HF_TOKEN，或者服务器推送过来的 $ATLAS_LOCAL/secrets/hf_token。
# 只导出到环境变量，绝不打印。
hf_token_load() {
  if [ -z "${HF_TOKEN:-}" ] && [ -s "$ATLAS_LOCAL/secrets/hf_token" ]; then
    HF_TOKEN=$(cat "$ATLAS_LOCAL/secrets/hf_token")
  fi
  case "${HF_TOKEN:-}" in
    ""|"{{"*) unset HF_TOKEN; return 1 ;;  # 没有，或 RunPod Secret 模板没有被替换
  esac
  export HF_TOKEN HUGGING_FACE_HUB_TOKEN=$HF_TOKEN
}

# ---------------------------------------------------------------- 写 Global volume
# 只用于冷数据。--inplace：不依赖原子 rename；--no-perms 等：Global volume 没有权限位；
# Global volume 不能设置文件时间（实测 rsync -t 报 Operation not permitted），所以不带 -t，用 --size-only 判断是否需要重传。
vol_sync() {  # vol_sync <本地源/> <volume 目标/>
  mkdir -p "$2"
  rsync -r --inplace --size-only --no-perms --no-owner --no-group --no-times --omit-dir-times --partial "$1" "$2"
}

weights_ready() { [ -f "$ATLAS_WEIGHTS/$1/.complete" ]; }
weights_mark()  { date -u +%FT%TZ > "$ATLAS_WEIGHTS/$1/.complete"; }

# ---------------------------------------------------------------- 安装报告
# 记录耗时、磁盘占用、需要的许可，写到 $ATLAS_VOL/setup-reports/<name>.jsonl（每次一行）
report() {  # report <name> <mode: restored|installed> <开始秒> <备注>
  local name=$1 mode=$2 start=$3 note=${4:-}
  local env_gb weights_gb gpu
  env_gb=$(du -sBG "$ATLAS_ENVS/$name" 2>/dev/null | cut -f1 | tr -d G || echo 0)
  weights_gb=$(du -sBG "$ATLAS_WEIGHTS/$name" 2>/dev/null | cut -f1 | tr -d G || echo 0)
  gpu=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo "?")
  local line
  line=$(printf '{"name":"%s","mode":"%s","seconds":%d,"env_gb":%s,"weights_gb":%s,"gpu":"%s","date":"%s","note":"%s"}' \
    "$name" "$mode" "$((SECONDS - start))" "${env_gb:-0}" "${weights_gb:-0}" "$gpu" "$(date -u +%FT%TZ)" "$note")
  echo "$line" >> "$ATLAS_VOL/setup-reports/$name.jsonl"
  echo "$line" > "$ATLAS_LOGS/setup-$name.json"
  log "报告: $line"
}
