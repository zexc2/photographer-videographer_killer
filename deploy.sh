#!/usr/bin/env bash
# One-step deploy / self-check (idempotent, safe to run repeatedly). Full instructions in DEPLOY.md.
#   ./deploy.sh          install dependencies, the GPU guard and patrol cron, start the camera-path studio, and check the RunPod config
#   ./deploy.sh --check  check only, no install, no start
# Only touches the current user's own things: ~/bin, ~/.config/atlas-gpu, ~/logs, the current user's crontab, the in-repo .venv. No sudo needed.
set -uo pipefail
REPO=$(cd "$(dirname "$0")" && pwd)
CHECK_ONLY=0; [ "${1:-}" = "--check" ] && CHECK_ONLY=1
CFG_DIR=~/.config/atlas-gpu
OK=0; BAD=0; TODO=()
ok()   { echo "  ✅ $*"; OK=$((OK + 1)); }
bad()  { echo "  ❌ $*"; BAD=$((BAD + 1)); }
todo() { TODO+=("$*"); }
have() { command -v "$1" >/dev/null 2>&1; }

echo "== 1. System tools"
[ "$(uname -s)" = Linux ] && ok "Linux" || bad "A Linux server is required (the guard uses flock, ss, crontab)"
for c in python3 rsync ssh ssh-keygen crontab flock ss curl; do
  have "$c" && ok "$c" || { bad "Missing $c"; todo "Install $c (Debian/Ubuntu: sudo apt install rsync openssh-client cron util-linux iproute2 curl python3-venv)"; }
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null && ok "Python ≥ 3.9" || bad "Python ≥ 3.9 is required"

echo "== 2. Python dependencies (flask, numpy)"
if python3 -c "import flask, numpy" 2>/dev/null; then
  ok "System Python already has flask, numpy"
elif [ -x "$REPO/.venv/bin/python" ] && "$REPO/.venv/bin/python" -c "import flask, numpy" 2>/dev/null; then
  ok "In-repo .venv already has flask, numpy"
elif [ $CHECK_ONLY = 0 ]; then
  echo "  … creating .venv in the repo and installing requirements.txt"
  if python3 -m venv "$REPO/.venv" && "$REPO/.venv/bin/pip" install -q -r "$REPO/requirements.txt"; then
    ok "Installed into $REPO/.venv"
  else
    bad "Failed to create .venv"; todo "Install python3-venv (sudo apt install python3-venv), then rerun ./deploy.sh"
  fi
else
  bad "Missing flask / numpy"; todo "Run ./deploy.sh (without --check) to install automatically"
fi

echo "== 3. runpodctl (used to read the account balance; version 2.x)"
CTL=$(command -v runpodctl 2>/dev/null || { [ -x ~/.local/bin/runpodctl ] && echo ~/.local/bin/runpodctl; })
if [ -n "$CTL" ]; then
  ok "runpodctl: $("$CTL" --version 2>/dev/null | head -1)"
  if "$CTL" user >/dev/null 2>&1; then ok "runpodctl is logged in (can read account info)"
  else bad "runpodctl has no API key configured yet"; todo "Run runpodctl config --apiKey <your key> (see DEPLOY.md step 3)"; fi
else
  bad "runpodctl not found"; todo "Install runpodctl to ~/.local/bin (see DEPLOY.md step 3)"
fi

echo "== 4. GPU guard (~/bin/gpu-up etc.) and patrol cron"
if [ $CHECK_ONLY = 0 ]; then
  bash "$REPO/guard/install.sh" >/dev/null && ok "Installed guard commands and patrol cron (every 10 minutes)" || bad "guard/install.sh failed"
else
  [ -x ~/bin/gpu-up ] && ok "Guard commands installed" || { bad "Guard commands not installed"; todo "Run ./deploy.sh (without --check) to install"; }
  crontab -l 2>/dev/null | grep -q gpu-guard && ok "Patrol cron installed" || { bad "Patrol cron not installed"; todo "Run ./deploy.sh (without --check) to install"; }
fi
case ":$PATH:" in *":$HOME/bin:"*) ;; *) todo "Add ~/bin to PATH: echo 'export PATH=\$HOME/bin:\$PATH' >> ~/.bashrc && source ~/.bashrc" ;; esac

echo "== 5. RunPod config (~/.config/atlas-gpu/)"
[ $CHECK_ONLY = 0 ] && mkdir -p "$CFG_DIR" && chmod 700 "$CFG_DIR"
KEY=$CFG_DIR/runpod_api_key
if [ -s "$KEY" ]; then
  perm=$(stat -c %a "$KEY")
  [ "$perm" = 600 ] && ok "API key file exists, permissions 600" || { bad "API key file permissions are $perm, must be 600"; todo "chmod 600 $KEY"; }
else
  bad "No API key file yet"; todo "Create a RunPod API key per DEPLOY.md step 2 and save it to $KEY (permissions 600)"
fi
CONF=$CFG_DIR/config.json
if [ ! -f "$CONF" ] && [ $CHECK_ONLY = 0 ]; then
  printf '{\n  "global_volume_id": "",\n  "global_volume_label": "atlas_data"\n}\n' > "$CONF"; chmod 600 "$CONF"
fi
VID=$(python3 -c "import json; print(json.load(open('$CONF')).get('global_volume_id', ''))" 2>/dev/null)
if [ -n "$VID" ]; then ok "Global volume ID is set"
else bad "Global volume ID not set in config.json yet"; todo "Create a Global volume per DEPLOY.md step 2 and put its ID in global_volume_id in $CONF"; fi

echo "== 6. Camera-path studio (127.0.0.1:8765)"
if [ $CHECK_ONLY = 0 ]; then
  ( { crontab -l 2>/dev/null || true; } | { grep -v 'frontend/run.sh' || true; }
    echo "@reboot $REPO/frontend/run.sh >/dev/null 2>&1" ) | crontab -
  "$REPO/frontend/run.sh" >/dev/null && ok "Started, starts automatically on boot" || bad "Failed to start, see ~/logs/frontend.log"
else
  ss -ltn | grep -q '127.0.0.1:8765 ' && ok "Running" || { bad "Not running"; todo "Start it with ./deploy.sh or frontend/run.sh; on failure see ~/logs/frontend.log"; }
fi

echo
if [ $BAD = 0 ]; then
  echo "All set."
  echo "  - First time? Prepare the sample model first (one-off, about 4 hours, about \$8, A100 80GB): see DEPLOY.md step 5"
  echo "  - Open the studio: on your own computer run ssh -L 8765:127.0.0.1:8765 $(whoami)@<server address>, then open http://localhost:8765 in a browser"
else
  echo "$BAD item(s) not ready yet; handle them in order:"
  i=1; for t in "${TODO[@]}"; do echo "  $i. $t"; i=$((i + 1)); done
  echo "Then run ./deploy.sh again."
fi
exit $((BAD > 0))
