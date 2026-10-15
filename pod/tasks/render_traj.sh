#!/usr/bin/env bash
# 按前端给的轨迹渲染视频。用法：atlas-run --input <含 traj.json 的目录> render_traj <模型名>
RUN_DIR=$1; MODEL_KEY=${2:?需要模型名}
source "$(dirname "$0")/_model_common.sh"
TRAJ=$RUN_DIR/task/traj.json
[ -f "$TRAJ" ] || die "没有 task/traj.json"
FR=$ATLAS_WORK/frames_$(basename "$RUN_DIR")
rm -rf "$FR"
(cd "$SRC4" && PYTHONPATH=. "$PY" "$HERE/tools/fourc4d_direct.py" render --config "$CFG" --checkpoint "$CKPT" \
   --traj "$TRAJ" --out "$FR/frames")
FPS=$(python3 -c "import json; print(json.load(open('$TRAJ')).get('fps', 30))")
ffmpeg -loglevel error -y -framerate "$FPS" -i "$FR/frames/%05d.png" -c:v libx264 -crf 18 -pix_fmt yuv420p \
  -movflags +faststart "$RUN_DIR/out/render.mp4"
cp "$FR/render_stats.json" "$RUN_DIR/out/metrics.json"
rm -rf "$FR"
log "完成：$(du -h "$RUN_DIR/out/render.mp4" | cut -f1)"
