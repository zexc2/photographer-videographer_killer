#!/usr/bin/env bash
# 巡检 stop pod 之前调用（安装为 $ATLAS_LOCAL/bin/pre_stop）：容器盘会被清空，先把所有 run 的 out/ 同步回 Global volume。
source "$(dirname "$(readlink -f "$0")")/../repo/pod/lib/common.sh" 2>/dev/null || source /opt/atlas/repo/pod/lib/common.sh
set +e
for d in "$ATLAS_WORK"/*/; do
  [ -d "$d/out" ] || continue
  id=$(basename "$d")
  [ -f "$d/status" ] && grep -q running "$d/status" && echo "interrupted" > "$d/out/INTERRUPTED"
  vol_sync "$d/out/" "$ATLAS_RESULTS/$id/" && log "pre_stop: 已同步 $id"
done
