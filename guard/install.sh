#!/usr/bin/env bash
# 安装 GPU 护栏：~/bin 软链接 + atlas 用户 crontab（幂等，只动 atlas 自己的 crontab）
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p ~/bin ~/logs ~/.local/state
# 服务器专用的 pod SSH key（私钥只在本机，公钥由 gpu-up 通过 PUBLIC_KEY 注入 pod）
[ -f ~/.ssh/atlas_runpod ] || ssh-keygen -q -t ed25519 -N "" -C "atlas-gpu-guard" -f ~/.ssh/atlas_runpod
mkdir -p ~/.config/atlas-gpu && chmod 700 ~/.config/atlas-gpu
for n in gpu-up gpu-down gpu-status gpu-guard; do ln -sfn "$HERE/atlas_gpu.py" ~/bin/$n; done
( { crontab -l 2>/dev/null || true; } | { grep -v -e 'gpu-guard' -e 'GPU 护栏巡检' || true; } ; cat "$HERE/crontab.txt" ) | crontab -
echo "已安装。当前 crontab："; crontab -l
