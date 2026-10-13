#!/usr/bin/env bash
# pod 端通用任务包装器（由服务器上的 atlas-run 调用）。
# 用法: run_task.sh <run 目录> <任务脚本名> [参数...]
#   <run 目录>/task/   服务器推过来的输入（小文件）
#   <run 目录>/out/    任务输出：preview/（小预览）、metrics.json，以及要保留的结果
# 负责：busy 心跳、峰值显存采样、计时、状态文件、结束时把 out/ 同步回 Global volume。
RUN_DIR=$1; TASK=$2; shift 2
source "$(dirname "$0")/lib/common.sh"
set +e
RUN_ID=$(basename "$RUN_DIR")
mkdir -p "$RUN_DIR/out/preview"
cd "$RUN_DIR"
echo running > status
busy_start

# 峰值显存采样（每 5 秒）
( peak=0; while kill -0 $$ 2>/dev/null; do
    m=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | sort -n | tail -1)
    [ -n "$m" ] && [ "$m" -gt "$peak" ] && { peak=$m; echo "$peak" > peak_vram_mib; }
    sleep 5; done ) &
SAMPLER=$!

t0=$(date +%s)
log "任务 $TASK 开始（run $RUN_ID）"
bash "$(dirname "$0")/tasks/$TASK.sh" "$RUN_DIR" "$@"
rc=$?
t1=$(date +%s)
kill $SAMPLER 2>/dev/null

# 任务自己写的 metrics.json 会和这里的通用指标合并
python3 - "$RUN_DIR" "$TASK" "$rc" "$t0" "$t1" <<'PY'
import json, sys, os, subprocess
run, task, rc, t0, t1 = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
p = os.path.join(run, "out", "metrics.json")
m = json.load(open(p)) if os.path.exists(p) else {}
peak = open(os.path.join(run, "peak_vram_mib")).read().strip() if os.path.exists(os.path.join(run, "peak_vram_mib")) else None
try:
    gpu = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], capture_output=True, text=True).stdout.strip()
except OSError:
    gpu = None
m.update({"task": task, "exit_code": rc, "seconds": t1 - t0, "peak_vram_mib": int(peak) if peak else None, "gpu": gpu})
json.dump(m, open(p, "w"), indent=2, ensure_ascii=False)
PY

log "任务结束 rc=$rc，用时 $((t1 - t0))s；同步 out/ → $ATLAS_RESULTS/$RUN_ID/"
cp -f run.log out/run.log 2>/dev/null
vol_sync "$RUN_DIR/out/" "$ATLAS_RESULTS/$RUN_ID/" || log "WARN 同步到 Global volume 失败"
[ $rc -eq 0 ] && echo done > status || echo "failed $rc" > status
exit $rc
