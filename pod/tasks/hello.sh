#!/usr/bin/env bash
# Minimal task: checks the full atlas-run flow (push -> run -> sync to Global volume -> fetch). No GPU needed.
RUN_DIR=$1; shift
source "$(dirname "$0")/../lib/common.sh"
echo "hello from $(hostname), args: $*"
ls "$RUN_DIR/task" 2>/dev/null
echo "preview" > "$RUN_DIR/out/preview/hello.txt"
head -c 30000000 /dev/zero > "$RUN_DIR/out/big.bin"   # 30MB: should stay on Global volume only, not be fetched to the server
[ "${1:-}" = fail ] && exit 3
printf '{"hello": true, "args": "%s"}\n' "$*" > "$RUN_DIR/out/metrics.json"
