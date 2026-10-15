#!/usr/bin/env bash
# Called by the guard before it stops the pod (installed as $ATLAS_LOCAL/bin/pre_stop): the container disk will be wiped, so sync every run's out/ back to Global volume first.
source "$(dirname "$(readlink -f "$0")")/../repo/pod/lib/common.sh" 2>/dev/null || source /opt/atlas/repo/pod/lib/common.sh
set +e
for d in "$ATLAS_WORK"/*/; do
  [ -d "$d/out" ] || continue
  id=$(basename "$d")
  [ -f "$d/status" ] && grep -q running "$d/status" && echo "interrupted" > "$d/out/INTERRUPTED"
  vol_sync "$d/out/" "$ATLAS_RESULTS/$id/" && log "pre_stop: synced $id"
done
