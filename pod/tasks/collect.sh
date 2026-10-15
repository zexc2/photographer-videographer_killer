#!/usr/bin/env bash
# Copy results of finished runs from Global volume into this run's out/, to be fetched to the server by atlas-run --fetch.
# For when a pod was stopped before its results were fetched (Global volume can only be read from a GPU pod).
# Usage: atlas-run collect <run_id> [run_id...]
RUN_DIR=$1; shift
source "$(dirname "$0")/../lib/common.sh"
for r in "$@"; do
  [[ "$r" =~ ^[A-Za-z0-9_-]+$ ]] || { log "Skipping invalid run id: $r"; continue; }
  src=$ATLAS_RESULTS/$r/
  [ -d "$src" ] || { log "$src not on Global volume"; continue; }
  mkdir -p "$RUN_DIR/out/$r"
  # Small files only: metrics, logs, previews, downscaled videos; big tars / checkpoints stay on Global volume
  rsync -r --size-only --no-perms --no-times --max-size=20M --exclude '*.tar' --exclude '*.pth' "$src" "$RUN_DIR/out/$r/"
  log "Collected $r: $(find "$RUN_DIR/out/$r" -type f | wc -l) files"
done
# Also check the stage 3 data packages
for seq in 171204_pose1 160906_band1; do
  f=$ATLAS_DATA/panoptic/$seq/prepared/stage3.tar
  [ -f "$f" ] && log "Data package $seq: $(du -h "$f" | cut -f1)" || log "Data package $seq: missing"
done | tee "$RUN_DIR/out/stage3_packages.txt"
