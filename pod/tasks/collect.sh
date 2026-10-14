#!/usr/bin/env bash
# 把 Global volume 上已完成任务的结果复制到本次任务的 out/，随后由 atlas-run --fetch 拉回服务器。
# 用于：pod 在结果拉回服务器之前就被关掉的情况（Global volume 只能挂在 GPU pod 上读）。
# 用法：atlas-run collect <run_id> [run_id...]
RUN_DIR=$1; shift
source "$(dirname "$0")/../lib/common.sh"
for r in "$@"; do
  [[ "$r" =~ ^[A-Za-z0-9_-]+$ ]] || { log "跳过非法 run id: $r"; continue; }
  src=$ATLAS_RESULTS/$r/
  [ -d "$src" ] || { log "Global volume 上没有 $src"; continue; }
  mkdir -p "$RUN_DIR/out/$r"
  # 只取小文件：指标、日志、预览、缩小的视频；大的 tar / checkpoint 留在 Global volume
  rsync -r --size-only --no-perms --no-times --max-size=20M --exclude '*.tar' --exclude '*.pth' "$src" "$RUN_DIR/out/$r/"
  log "已收集 $r：$(find "$RUN_DIR/out/$r" -type f | wc -l) 个文件"
done
# 顺便核对阶段 3 的数据包
for seq in 171204_pose1 160906_band1; do
  f=$ATLAS_DATA/panoptic/$seq/prepared/stage3.tar
  [ -f "$f" ] && log "数据包 $seq：$(du -h "$f" | cut -f1)" || log "数据包 $seq：不存在"
done | tee "$RUN_DIR/out/stage3_packages.txt"
