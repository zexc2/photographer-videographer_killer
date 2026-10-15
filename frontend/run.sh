#!/usr/bin/env bash
# Start / restart the camera path workbench (listens on 127.0.0.1:8765 only). Log: ~/logs/frontend.log
# Access: on your own machine run ssh -L 8765:127.0.0.1:8765 <user>@<server>, then open http://localhost:8765 in a browser
# Note: find the old process PID by port and kill it; do not use pkill -f (it would match the shell running it)
APP=$(cd "$(dirname "$0")" && pwd)
PORT=${ATLAS_FRONTEND_PORT:-8765}
PID=$(ss -ltnp 2>/dev/null | grep "127.0.0.1:$PORT " | grep -oE 'pid=[0-9]+' | cut -d= -f2 | head -1)
if [ -n "$PID" ]; then kill "$PID"; sleep 1; fi
mkdir -p ~/logs
# deploy.sh creates a .venv inside the repo when the system Python lacks Flask
PY=python3
[ -x "$APP/../.venv/bin/python" ] && PY="$APP/../.venv/bin/python"
cd "$APP" && setsid nohup "$PY" app.py >> ~/logs/frontend.log 2>&1 < /dev/null &
sleep 2
if ss -ltn | grep -q "127.0.0.1:$PORT "; then
  echo "Started: http://127.0.0.1:$PORT (access via SSH tunnel)"
else
  echo "Failed to start, see ~/logs/frontend.log"; exit 1
fi
