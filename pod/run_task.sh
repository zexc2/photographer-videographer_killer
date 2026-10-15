#!/usr/bin/env bash
# Generic pod-side task wrapper (called by atlas-run on the server).
# Usage: run_task.sh <run dir> <task script name> [args...]
#   <run dir>/task/   inputs pushed from the server (small files)
#   <run dir>/out/    task output: preview/ (small previews), metrics.json, and results to keep
# Handles: busy heartbeat, peak VRAM sampling, timing, the status file, and syncing out/ back to Global volume at the end.
RUN_DIR=$1; TASK=$2; shift 2
source "$(dirname "$0")/lib/common.sh"
set +e
RUN_ID=$(basename "$RUN_DIR")
mkdir -p "$RUN_DIR/out/preview"
cd "$RUN_DIR"
echo running > status
busy_start

# Sample peak VRAM (every 5 s)
( peak=0; while kill -0 $$ 2>/dev/null; do
    m=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | sort -n | tail -1)
    [ -n "$m" ] && [ "$m" -gt "$peak" ] && { peak=$m; echo "$peak" > peak_vram_mib; }
    sleep 5; done ) &
SAMPLER=$!

t0=$(date +%s)
log "Task $TASK started (run $RUN_ID)"
bash "$(dirname "$0")/tasks/$TASK.sh" "$RUN_DIR" "$@"
rc=$?
t1=$(date +%s)
kill $SAMPLER 2>/dev/null

# A metrics.json written by the task is merged with the generic metrics here
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

log "Task finished rc=$rc in $((t1 - t0))s; syncing out/ -> $ATLAS_RESULTS/$RUN_ID/"
cp -f run.log out/run.log 2>/dev/null
vol_sync "$RUN_DIR/out/" "$ATLAS_RESULTS/$RUN_ID/" || log "WARN sync to Global volume failed"
[ $rc -eq 0 ] && echo done > status || echo "failed $rc" > status
exit $rc
