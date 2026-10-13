#!/usr/bin/env bash
# 最小任务：验证 atlas-run 全流程（推送 → 执行 → 同步 Global volume → 拉回）。不需要 GPU。
RUN_DIR=$1; shift
source "$(dirname "$0")/../lib/common.sh"
echo "hello from $(hostname)，参数: $*"
ls "$RUN_DIR/task" 2>/dev/null
echo "preview" > "$RUN_DIR/out/preview/hello.txt"
head -c 30000000 /dev/zero > "$RUN_DIR/out/big.bin"   # 30MB：应该只留在 Global volume，不拉回服务器
[ "${1:-}" = fail ] && exit 3
printf '{"hello": true, "args": "%s"}\n' "$*" > "$RUN_DIR/out/metrics.json"
