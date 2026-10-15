#!/usr/bin/env bash
# Shared pod-side function library. Sourced by setup_*.sh and task scripts; not run on its own.
#
# Storage conventions (see docs/storage-and-env.md):
#   $ATLAS_VOL    Global volume (object storage), cold data only: weights/ datasets/ envpacks/ results/ setup-reports/
#   $ATLAS_LOCAL  container disk /opt/atlas (wiped when the pod stops): envs/ src/ work/ cache/ secrets/ logs/ bin/
# Environment option C: install once -> conda-pack into a single .tar.zst in $ATLAS_VOL/envpacks/ -> unpack and reuse on the next boot.

set -euo pipefail

# ---------------------------------------------------------------- paths
if [ -z "${ATLAS_VOL:-}" ]; then
  # If gpu-up did not set it, detect it: with a network volume also attached, Global is at /workspace-global
  if mountpoint -q /workspace-global 2>/dev/null; then ATLAS_VOL=/workspace-global; else ATLAS_VOL=/workspace; fi
fi
ATLAS_LOCAL=${ATLAS_LOCAL:-/opt/atlas}
export ATLAS_VOL ATLAS_LOCAL
export ATLAS_ENVS=$ATLAS_LOCAL/envs ATLAS_SRC=$ATLAS_LOCAL/src ATLAS_WORK=$ATLAS_LOCAL/work
export ATLAS_CACHE=$ATLAS_LOCAL/cache ATLAS_LOGS=$ATLAS_LOCAL/logs
export ATLAS_WEIGHTS=$ATLAS_VOL/weights ATLAS_DATA=$ATLAS_VOL/datasets ATLAS_PACKS=$ATLAS_VOL/envpacks
export ATLAS_RESULTS=$ATLAS_VOL/results
export CONDA_ROOT=$ATLAS_LOCAL/miniforge
# All download caches live on the container disk (Global volume has no file locks / permission bits; an HF cache there breaks)
export HF_HOME=$ATLAS_CACHE/hf TORCH_HOME=$ATLAS_CACHE/torch PIP_CACHE_DIR=$ATLAS_CACHE/pip
export XDG_CACHE_HOME=$ATLAS_CACHE/xdg
# Build CUDA extensions for both A100 (sm_80) and H100 (sm_90) so the packed env works on either card
export TORCH_CUDA_ARCH_LIST=${TORCH_CUDA_ARCH_LIST:-"8.0;9.0"}
export MAX_JOBS=${MAX_JOBS:-$(( $(nproc) > 16 ? 16 : $(nproc) ))}

mkdir -p "$ATLAS_ENVS" "$ATLAS_SRC" "$ATLAS_WORK" "$ATLAS_CACHE" "$ATLAS_LOGS" "$ATLAS_LOCAL/bin" "$ATLAS_LOCAL/secrets"
chmod 700 "$ATLAS_LOCAL/secrets"
if mountpoint -q "$ATLAS_VOL" 2>/dev/null || [ -d "$ATLAS_VOL" ]; then
  mkdir -p "$ATLAS_WEIGHTS" "$ATLAS_DATA" "$ATLAS_PACKS" "$ATLAS_RESULTS" "$ATLAS_VOL/setup-reports"
fi

log() { echo "[$(date -u +%H:%M:%S)] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# ---------------------------------------------------------------- busy heartbeat
# GPU usage is 0 during downloads / installs / CPU preprocessing; while this file is fresh the guard won't treat the pod as idle.
busy_start() {
  touch "$ATLAS_LOCAL/.atlas_busy"
  ( parent=$$; while kill -0 "$parent" 2>/dev/null; do touch "$ATLAS_LOCAL/.atlas_busy"; sleep 60; done ) &
  ATLAS_BUSY_PID=$!
  trap 'kill ${ATLAS_BUSY_PID:-0} 2>/dev/null || true' EXIT
}

# ---------------------------------------------------------------- system packages / conda
apt_ensure() {  # apt_ensure pkg...
  local missing=()
  for p in "$@"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
  [ ${#missing[@]} -eq 0 ] && return 0
  log "apt install: ${missing[*]}"
  DEBIAN_FRONTEND=noninteractive apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}" >/dev/null
}

conda_ensure() {
  if [ ! -x "$CONDA_ROOT/bin/conda" ]; then
    log "Installing Miniforge to $CONDA_ROOT"
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

# Activate an installed or unpacked env (does not need conda itself; a packed env ships its own python)
env_activate() {
  local p=$ATLAS_ENVS/$1
  [ -x "$p/bin/python" ] || die "environment $1 does not exist"
  # shellcheck disable=SC1091
  source "$p/bin/activate" 2>/dev/null || export PATH="$p/bin:$PATH"
}

# ---------------------------------------------------------------- env pack / restore (option C)
# Version key: the setup script's ENV_REV (see below). Changing it triggers a reinstall and repack.
env_key() {  # env_key <setup script path>
  # The setup script pins its pack revision with ENV_REV=<12 hex chars>. Bump it whenever the
  # installation itself changes; edits to comments or log text then no longer invalidate packs.
  # Without ENV_REV, fall back to hashing the setup script plus this file (the original behaviour).
  local rev
  rev=$(sed -n 's/^ENV_REV=\([0-9a-f]\{12\}\).*/\1/p' "$1" | head -1)
  if [ -n "$rev" ]; then echo "$rev"; else cat "$1" "${BASH_SOURCE[0]}" | sha256sum | cut -c1-12; fi
}

env_restore() {  # env_restore <name> <key>; returns 0 on success, 1 if there is no pack
  local name=$1 key=$2 dst=$ATLAS_ENVS/$1 pack=$ATLAS_PACKS/$1-$2.tar.zst
  if [ -f "$dst/.atlas_key" ] && [ "$(cat "$dst/.atlas_key")" = "$key" ]; then
    log "Environment $name already on container disk ($key)"; return 0
  fi
  [ -f "$pack" ] || return 1
  log "Restoring environment $name from Global volume ($(du -h "$pack" | cut -f1))"
  rm -rf "$dst"; mkdir -p "$dst"
  # Copy the whole file locally before unpacking: one sequential read of a big file is fastest on object storage
  local t0=$SECONDS tmp=$ATLAS_CACHE/$name.tar.zst
  cp "$pack" "$tmp"
  log "  copy took $((SECONDS - t0))s"
  zstd -dc -T0 "$tmp" | tar -x -C "$dst"
  rm -f "$tmp"
  "$dst/bin/conda-unpack" >/dev/null 2>&1 || true
  echo "$key" > "$dst/.atlas_key"
  log "  restore done, $((SECONDS - t0))s total"
}

env_pack() {  # env_pack <name> <key>: pack the env on the container disk and store it on Global volume
  local name=$1 key=$2 src=$ATLAS_ENVS/$1 pack=$ATLAS_PACKS/$1-$2.tar.zst
  local tmp=$ATLAS_CACHE/$name-$key.tar.zst t0=$SECONDS
  log "Packing environment $name -> $pack"
  "$CONDA_ROOT/bin/conda-pack" -p "$src" --format tar -o "$ATLAS_CACHE/$name.tar" --force --quiet \
    --ignore-missing-files
  zstd -q -T0 -10 --rm -f "$ATLAS_CACHE/$name.tar" -o "$tmp"
  # Write to Global volume as one big file; write to a temp name then rename (rename is not guaranteed atomic, so verify size afterwards)
  cp "$tmp" "$pack.part" && mv -f "$pack.part" "$pack"
  [ "$(stat -c %s "$tmp")" = "$(stat -c %s "$pack")" ] || die "packed file size mismatch"
  rm -f "$tmp"
  # Delete old packs; keep only the current version
  find "$ATLAS_PACKS" -maxdepth 1 -name "$name-*.tar.zst" ! -name "$name-$key.tar.zst" -delete
  echo "$key" > "$src/.atlas_key"
  log "  pack done: $(du -h "$pack" | cut -f1), took $((SECONDS - t0))s"
}

# ---------------------------------------------------------------- code repos
# Clone at a pinned commit onto the container disk. Code is not packed into the env; it is re-cloned on every boot (a few seconds).
repo_ensure() {  # repo_ensure <url> <dir name> <commit or branch> [--recursive]
  local url=$1 dir=$ATLAS_SRC/$2 ref=$3 rec=${4:-}
  if [ ! -d "$dir/.git" ]; then
    log "Cloning $url -> $dir"
    git clone -q ${rec:+--recursive} "$url" "$dir"
  fi
  git -C "$dir" fetch -q --all --tags 2>/dev/null || true
  git -C "$dir" checkout -q "$ref"
  [ -n "$rec" ] && git -C "$dir" submodule update -q --init --recursive
  git -C "$dir" rev-parse HEAD
}

# ---------------------------------------------------------------- Hugging Face token
# Two possible sources: HF_TOKEN env var injected by a RunPod Secret, or $ATLAS_LOCAL/secrets/hf_token pushed from the server.
# Only exported as an env var; never printed.
hf_token_load() {
  if [ -z "${HF_TOKEN:-}" ] && [ -s "$ATLAS_LOCAL/secrets/hf_token" ]; then
    HF_TOKEN=$(cat "$ATLAS_LOCAL/secrets/hf_token")
  fi
  case "${HF_TOKEN:-}" in
    ""|"{{"*) unset HF_TOKEN; return 1 ;;  # missing, or the RunPod Secret template was not substituted
  esac
  export HF_TOKEN HUGGING_FACE_HUB_TOKEN=$HF_TOKEN
}

# ---------------------------------------------------------------- writing to Global volume
# Cold data only. --inplace: no reliance on atomic rename; --no-perms etc.: Global volume has no permission bits;
# Global volume cannot set file times (rsync -t fails with Operation not permitted in testing), so no -t; --size-only decides what to resend.
vol_sync() {  # vol_sync <local src/> <volume dst/>
  mkdir -p "$2"
  rsync -r --inplace --size-only --no-perms --no-owner --no-group --no-times --omit-dir-times --partial "$1" "$2"
}

weights_ready() { [ -f "$ATLAS_WEIGHTS/$1/.complete" ]; }
weights_mark()  { date -u +%FT%TZ > "$ATLAS_WEIGHTS/$1/.complete"; }

# ---------------------------------------------------------------- setup report
# Records time taken, disk usage and required licenses to $ATLAS_VOL/setup-reports/<name>.jsonl (one line per run)
report() {  # report <name> <mode: restored|installed> <start seconds> <note>
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
  log "report: $line"
}
