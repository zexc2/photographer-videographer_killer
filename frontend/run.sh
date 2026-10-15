#!/usr/bin/env bash
# 启动 / 重启运镜工作台（只监听 127.0.0.1:8765）。日志：~/logs/frontend.log
# 访问：在自己电脑上 ssh -L 8765:127.0.0.1:8765 <用户名>@<服务器地址>，然后浏览器打开 http://localhost:8765
# 注意：按端口找到旧进程的 PID 再 kill，不用 pkill -f（会匹配到执行它的 shell 自己）
APP=$(cd "$(dirname "$0")" && pwd)
PORT=${ATLAS_FRONTEND_PORT:-8765}
PID=$(ss -ltnp 2>/dev/null | grep "127.0.0.1:$PORT " | grep -oE 'pid=[0-9]+' | cut -d= -f2 | head -1)
if [ -n "$PID" ]; then kill "$PID"; sleep 1; fi
mkdir -p ~/logs
# deploy.sh 在系统 Python 缺 Flask 时会建仓库内的 .venv
PY=python3
[ -x "$APP/../.venv/bin/python" ] && PY="$APP/../.venv/bin/python"
cd "$APP" && setsid nohup "$PY" app.py >> ~/logs/frontend.log 2>&1 < /dev/null &
sleep 2
if ss -ltn | grep -q "127.0.0.1:$PORT "; then
  echo "已启动：http://127.0.0.1:$PORT（通过 SSH 隧道访问）"
else
  echo "启动失败，见 ~/logs/frontend.log"; exit 1
fi
