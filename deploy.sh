#!/usr/bin/env bash
# 一键部署 / 自检（幂等，可以反复运行）。完整说明见 DEPLOY.md。
#   ./deploy.sh          安装依赖、GPU 护栏、巡检 cron，启动运镜工作台，并检查 RunPod 配置
#   ./deploy.sh --check  只检查，不安装、不启动
# 只改动当前用户自己的东西：~/bin、~/.config/atlas-gpu、~/logs、当前用户的 crontab、仓库内 .venv。不需要 sudo。
set -uo pipefail
REPO=$(cd "$(dirname "$0")" && pwd)
CHECK_ONLY=0; [ "${1:-}" = "--check" ] && CHECK_ONLY=1
CFG_DIR=~/.config/atlas-gpu
OK=0; BAD=0; TODO=()
ok()   { echo "  ✅ $*"; OK=$((OK + 1)); }
bad()  { echo "  ❌ $*"; BAD=$((BAD + 1)); }
todo() { TODO+=("$*"); }
have() { command -v "$1" >/dev/null 2>&1; }

echo "== 1. 系统工具"
[ "$(uname -s)" = Linux ] && ok "Linux" || bad "需要 Linux 服务器（护栏用到 flock、ss、crontab）"
for c in python3 rsync ssh ssh-keygen crontab flock ss curl; do
  have "$c" && ok "$c" || { bad "缺少 $c"; todo "安装 $c（Debian/Ubuntu：sudo apt install rsync openssh-client cron util-linux iproute2 curl python3-venv）"; }
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null && ok "Python ≥ 3.9" || bad "需要 Python ≥ 3.9"

echo "== 2. Python 依赖（flask、numpy）"
if python3 -c "import flask, numpy" 2>/dev/null; then
  ok "系统 Python 已有 flask、numpy"
elif [ -x "$REPO/.venv/bin/python" ] && "$REPO/.venv/bin/python" -c "import flask, numpy" 2>/dev/null; then
  ok "仓库内 .venv 已有 flask、numpy"
elif [ $CHECK_ONLY = 0 ]; then
  echo "  … 在仓库内建 .venv 并安装 requirements.txt"
  if python3 -m venv "$REPO/.venv" && "$REPO/.venv/bin/pip" install -q -r "$REPO/requirements.txt"; then
    ok "已安装到 $REPO/.venv"
  else
    bad "建 .venv 失败"; todo "安装 python3-venv（sudo apt install python3-venv）后重新运行 ./deploy.sh"
  fi
else
  bad "缺少 flask / numpy"; todo "运行 ./deploy.sh（不带 --check）自动安装"
fi

echo "== 3. runpodctl（用来读账户余额；版本 2.x）"
CTL=$(command -v runpodctl 2>/dev/null || { [ -x ~/.local/bin/runpodctl ] && echo ~/.local/bin/runpodctl; })
if [ -n "$CTL" ]; then
  ok "runpodctl：$("$CTL" --version 2>/dev/null | head -1)"
  if "$CTL" user >/dev/null 2>&1; then ok "runpodctl 已登录（能读到账户信息）"
  else bad "runpodctl 还没配置 API key"; todo "运行 runpodctl config --apiKey <你的 key>（见 DEPLOY.md 第 3 步）"; fi
else
  bad "没有找到 runpodctl"; todo "安装 runpodctl 到 ~/.local/bin（见 DEPLOY.md 第 3 步）"
fi

echo "== 4. GPU 护栏（~/bin/gpu-up 等）和巡检 cron"
if [ $CHECK_ONLY = 0 ]; then
  bash "$REPO/guard/install.sh" >/dev/null && ok "已安装护栏命令和巡检 cron（每 10 分钟）" || bad "guard/install.sh 失败"
else
  [ -x ~/bin/gpu-up ] && ok "护栏命令已安装" || { bad "护栏命令未安装"; todo "运行 ./deploy.sh（不带 --check）安装"; }
  crontab -l 2>/dev/null | grep -q gpu-guard && ok "巡检 cron 已安装" || { bad "巡检 cron 未安装"; todo "运行 ./deploy.sh（不带 --check）安装"; }
fi
case ":$PATH:" in *":$HOME/bin:"*) ;; *) todo "把 ~/bin 加入 PATH：echo 'export PATH=\$HOME/bin:\$PATH' >> ~/.bashrc && source ~/.bashrc" ;; esac

echo "== 5. RunPod 配置（~/.config/atlas-gpu/）"
[ $CHECK_ONLY = 0 ] && mkdir -p "$CFG_DIR" && chmod 700 "$CFG_DIR"
KEY=$CFG_DIR/runpod_api_key
if [ -s "$KEY" ]; then
  perm=$(stat -c %a "$KEY")
  [ "$perm" = 600 ] && ok "API key 文件存在，权限 600" || { bad "API key 文件权限是 $perm，必须是 600"; todo "chmod 600 $KEY"; }
else
  bad "还没有 API key 文件"; todo "按 DEPLOY.md 第 2 步创建 RunPod API key，保存到 $KEY（权限 600）"
fi
CONF=$CFG_DIR/config.json
if [ ! -f "$CONF" ] && [ $CHECK_ONLY = 0 ]; then
  printf '{\n  "global_volume_id": "",\n  "global_volume_label": "atlas_data"\n}\n' > "$CONF"; chmod 600 "$CONF"
fi
VID=$(python3 -c "import json; print(json.load(open('$CONF')).get('global_volume_id', ''))" 2>/dev/null)
if [ -n "$VID" ]; then ok "已填写 Global volume ID"
else bad "config.json 里还没填 Global volume ID"; todo "按 DEPLOY.md 第 2 步建 Global volume，把 ID 填进 $CONF 的 global_volume_id"; fi

echo "== 6. 运镜工作台（127.0.0.1:8765）"
if [ $CHECK_ONLY = 0 ]; then
  ( { crontab -l 2>/dev/null || true; } | { grep -v 'frontend/run.sh' || true; }
    echo "@reboot $REPO/frontend/run.sh >/dev/null 2>&1" ) | crontab -
  "$REPO/frontend/run.sh" >/dev/null && ok "已启动，开机自动启动" || bad "启动失败，见 ~/logs/frontend.log"
else
  ss -ltn | grep -q '127.0.0.1:8765 ' && ok "正在运行" || { bad "没有运行"; todo "运行 ./deploy.sh 或 frontend/run.sh 启动，失败看 ~/logs/frontend.log"; }
fi

echo
if [ $BAD = 0 ]; then
  echo "全部就绪。"
  echo "  - 第一次用？先准备样例模型（一次性，约 4 小时、约 \$8，A100 80GB）：见 DEPLOY.md 第 5 步"
  echo "  - 打开工作台：在自己电脑上 ssh -L 8765:127.0.0.1:8765 $(whoami)@<服务器地址>，浏览器打开 http://localhost:8765"
else
  echo "还有 $BAD 项没就绪，按顺序处理："
  i=1; for t in "${TODO[@]}"; do echo "  $i. $t"; i=$((i + 1)); done
  echo "处理完再运行一次 ./deploy.sh。"
fi
exit $((BAD > 0))
