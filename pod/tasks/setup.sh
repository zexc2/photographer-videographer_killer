#!/usr/bin/env bash
# 安装 / 恢复环境的任务入口：setup.sh <run 目录> gen3c vipe 4c4d ...
RUN_DIR=$1; shift
HERE=$(cd "$(dirname "$0")/.." && pwd)
rc=0
for m in "$@"; do
  bash "$HERE/setup_$m.sh" || { echo "setup_$m 失败" >&2; rc=1; }
  cp -f /opt/atlas/logs/setup-$m.json "$RUN_DIR/out/setup-$m.json" 2>/dev/null || true
done
exit $rc
