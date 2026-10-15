#!/usr/bin/env bash
# Install the GPU guard: ~/bin symlinks + the atlas user's crontab (idempotent; only touches atlas's own crontab)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p ~/bin ~/logs ~/.local/state
# Server-dedicated pod SSH key (private key stays on this machine; gpu-up injects the public key into pods via PUBLIC_KEY)
[ -f ~/.ssh/atlas_runpod ] || ssh-keygen -q -t ed25519 -N "" -C "atlas-gpu-guard" -f ~/.ssh/atlas_runpod
mkdir -p ~/.config/atlas-gpu && chmod 700 ~/.config/atlas-gpu
for n in gpu-up gpu-down gpu-status gpu-guard; do ln -sfn "$HERE/atlas_gpu.py" ~/bin/$n; done
ln -sfn "$HERE/../runner/atlas_run.py" ~/bin/atlas-run
# cron's PATH is minimal: add runpodctl's directory and ~/.local/bin
CTL_DIR=$(dirname "$(command -v runpodctl 2>/dev/null || echo "$HOME/.local/bin/runpodctl")")
CRON_PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
case ":$CRON_PATH:" in *":$CTL_DIR:"*) ;; *) CRON_PATH="$CTL_DIR:$CRON_PATH" ;; esac
( { crontab -l 2>/dev/null || true; } | { grep -v -e 'gpu-guard' -e '^# GPU ' || true; } ;
  sed -e "s|@HOME@|$HOME|g" -e "s|@PATH@|$CRON_PATH|g" "$HERE/crontab.txt" ) | crontab -
echo "Installed. Current crontab:"; crontab -l
