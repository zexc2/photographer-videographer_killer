#!/usr/bin/env bash
# 阶段 4：GEN3C vs 4C4D 基线测试。用法：atlas-run baseline [序列名]（默认 171204_pose1）
#   1. 恢复环境；按（主点居中后的）新流程重新准备该序列数据（tasks/panoptic.sh）
#   2. 生成两个模型共用的轨迹 + ViPE 尺度对齐（tools/make_trajectories.py）
#   3. GEN3C：评估机位 + 运镜轨迹，各生成 121 帧（tools/gen3c_run.py，模型只加载一次）
#   4. 4C4D：4 个机位训练 30k 步 → 官方 render.py --validate 渲染评估机位 → 自定义运镜轨迹（tools/fourc4d_render.py）
#   5. 统一口径评估 PSNR / SSIM / LPIPS（tools/eval_metrics.py），生成对比视频
# 每一步记录耗时和峰值显存，结果放 out/（小文件拉回服务器），完整结果由 run_task.sh 同步到 Global volume。
RUN_DIR=$1; shift
SEQ=${1:-171204_pose1}
HERE=$(cd "$(dirname "$0")/.." && pwd)
source "$HERE/lib/common.sh"
CFG=$HERE/configs/panoptic.json
OUT=$RUN_DIR/out
W=$ATLAS_WORK/panoptic/$SEQ
S4=$W/stage4
mkdir -p "$OUT"
ITERS=${FOURC_ITERS:-30000}

# ---------------------------------------------------------------- 分阶段计时 + 峰值显存
declare -A T_S VRAM
phase_begin() {
  PHASE=$1; PHASE_T0=$SECONDS
  echo 0 > "$ATLAS_LOGS/peak_$PHASE"
  ( while [ -f "$ATLAS_LOGS/peak_$PHASE.on" ]; do
      m=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | sort -n | tail -1)
      [ "$m" -gt "$(cat "$ATLAS_LOGS/peak_$PHASE")" ] && echo "$m" > "$ATLAS_LOGS/peak_$PHASE"
      sleep 3; done ) &
  touch "$ATLAS_LOGS/peak_$PHASE.on"
  log "▶ $PHASE"
}
phase_end() {
  rm -f "$ATLAS_LOGS/peak_$PHASE.on"; sleep 4
  T_S[$PHASE]=$((SECONDS - PHASE_T0)); VRAM[$PHASE]=$(cat "$ATLAS_LOGS/peak_$PHASE")
  log "■ $PHASE 用时 ${T_S[$PHASE]}s，峰值显存 ${VRAM[$PHASE]} MiB"
}
write_timing() {
  python3 - "$OUT/timing.json" <<PY
import json, sys
t = {$(for k in "${!T_S[@]}"; do printf '"%s": {"seconds": %s, "peak_vram_mib": %s}, ' "$k" "${T_S[$k]}" "${VRAM[$k]}"; done)}
json.dump(t, open(sys.argv[1], "w"), indent=2, ensure_ascii=False)
PY
}
trap write_timing EXIT

# ---------------------------------------------------------------- 1. 环境 + 数据
phase_begin env_and_data
bash "$HERE/setup_gen3c.sh"          # 恢复 GEN3C 环境，权重链接到 Global volume
bash "$HERE/tasks/panoptic.sh" "$RUN_DIR" "$SEQ"   # 4c4d / mast3r / vipe 环境 + 重新准备数据（主点居中）
phase_end

META=$W/meta.json
read -r TRAIN EVAL_CAM <<<"$(python3 -c "import json; m=json.load(open('$META')); print(m['fourc4d_training_view'], 'cam%02d' % m['eval_cam'])")"
GEN3C_ENV=$ATLAS_ENVS/gen3c
FOURC_ENV=$ATLAS_ENVS/4c4d

# ---------------------------------------------------------------- 2. 轨迹 + 尺度对齐
"$GEN3C_ENV/bin/python" "$HERE/tools/make_trajectories.py" --work "$W"
cp "$S4/trajectories.json" "$OUT/"

# ---------------------------------------------------------------- 3. GEN3C
phase_begin gen3c
cat > "$S4/gen3c_jobs.json" <<JSON
[{"name": "${SEQ}_gen3c_eval", "vipe": "$W/gen3c/vipe", "traj": "$S4/gen3c_eval.npz"},
 {"name": "${SEQ}_gen3c_path", "vipe": "$W/gen3c/vipe", "traj": "$S4/gen3c_path.npz"}]
JSON
GEN3C_OK=1
(cd "$ATLAS_SRC/GEN3C" && CUDA_HOME=$GEN3C_ENV PATH=$GEN3C_ENV/bin:$PATH PYTHONPATH=. \
   "$GEN3C_ENV/bin/python" "$HERE/tools/gen3c_run.py" --jobs "$S4/gen3c_jobs.json" --out "$S4/gen3c") \
  || { log "GEN3C 失败，改用 --offload 重试一次（显存不够时）"; \
       (cd "$ATLAS_SRC/GEN3C" && CUDA_HOME=$GEN3C_ENV PATH=$GEN3C_ENV/bin:$PATH PYTHONPATH=. \
          "$GEN3C_ENV/bin/python" "$HERE/tools/gen3c_run.py" --jobs "$S4/gen3c_jobs.json" --out "$S4/gen3c" --offload) \
       || GEN3C_OK=0; }
phase_end
cp "$S4/gen3c/gen3c_timing.json" "$OUT/" 2>/dev/null || true

# ---------------------------------------------------------------- 4. 4C4D
phase_begin fourc4d_train
FC=$S4/4c4d_${SEQ}.yaml
"$FOURC_ENV/bin/python" - "$ATLAS_SRC/4C4D/configs/dynerf/flame_steak.yaml" "$FC" "$W/4c4d" "$S4/4c4d_model" "$ITERS" <<'PY'
import sys
from omegaconf import OmegaConf
c = OmegaConf.load(sys.argv[1])
# 注意：train.py 先解析命令行再合并 yaml，yaml 里的值会覆盖命令行，所以迭代次数必须写在这里
c.OptimizationParams.iterations = int(sys.argv[5])
c.ModelParams.source_path = sys.argv[3]
c.ModelParams.model_path = sys.argv[4]
c.time_duration = [0.0, 10.0]
c.exhaust_test = False   # 训练中不反复在评估机位上测试（既省时间，也避免用评估机位挑模型）
OmegaConf.save(c, sys.argv[2])
PY
FOURC_OK=1
(cd "$ATLAS_SRC/4C4D" && PATH=$FOURC_ENV/bin:$PATH "$FOURC_ENV/bin/python" train.py --config "$FC" \
   --training_view "$TRAIN" --output_dir run --res 2 \
   --test_iterations "$ITERS" --save_iterations "$ITERS" 2>&1 | tr '\r' '\n' | grep -vE '^Training: +[0-9]+%.*it/s\]$' | tail -40) \
  || FOURC_OK=0
CKPT=$S4/4c4d_model/run/chkpnt_best.pth
[ -f "$CKPT" ] || { log "4C4D 没有产出 checkpoint"; FOURC_OK=0; }
phase_end

if [ $FOURC_OK = 1 ]; then
  phase_begin fourc4d_render
  RARGS=(--config "$FC" --training_view "$TRAIN" --start_checkpoint "$CKPT" --res 2 --output_dir run)
  (cd "$ATLAS_SRC/4C4D" && "$FOURC_ENV/bin/python" render.py "${RARGS[@]}" --validate 2>&1 | tail -5) \
    || log "4C4D 评估机位渲染失败"
  (cd "$ATLAS_SRC/4C4D" && ATLAS_4C4D_PATH=$S4/4c4d_path.npz PYTHONPATH=. \
     "$FOURC_ENV/bin/python" "$HERE/tools/fourc4d_render.py" "${RARGS[@]}" --traj custom 2>&1 | tail -5) \
    || log "4C4D 运镜渲染失败"
  phase_end
fi

# ---------------------------------------------------------------- 5. 评估 + 对比视频
phase_begin eval
EV="$FOURC_ENV/bin/python $HERE/tools/eval_metrics.py --gt-dir $W/4c4d/images --gt-cam $EVAL_CAM"
mkdir -p "$OUT/videos"
if [ $GEN3C_OK = 1 ]; then
  $EV --pred "$S4/gen3c/${SEQ}_gen3c_eval.mp4" --kind gen3c --frames 121 --out "$OUT/metrics_gen3c.json" \
      --video "$OUT/videos/${SEQ}_eval_gen3c_vs_gt.mp4" || log "GEN3C 评估失败"
fi
if [ $FOURC_OK = 1 ]; then
  TEST_R=$(ls -d "$S4"/4c4d_model/run/render/test/ours_*/renders 2>/dev/null | tail -1)
  TRAJ_R=$(ls -d "$S4"/4c4d_model/run/render/traj/ours_*/renders 2>/dev/null | tail -1)
  if [ -n "$TEST_R" ]; then
    $EV --pred "$TEST_R" --kind 4c4d --frames 121 --out "$OUT/metrics_4c4d.json" \
        --video "$OUT/videos/${SEQ}_eval_4c4d_vs_gt.mp4" || log "4C4D 评估失败"
    $EV --pred "$TEST_R" --kind 4c4d --frames 300 --out "$OUT/metrics_4c4d_300f.json" || true
  fi
  [ -n "$TRAJ_R" ] && ffmpeg -loglevel error -y -framerate 24 -i "$TRAJ_R/%05d.png" -c:v libx264 -crf 18 \
    -pix_fmt yuv420p "$S4/${SEQ}_4c4d_path.mp4" || true
  cp "$S4"/4c4d_model/run/render/test/ours_*/../../stats/*.json "$OUT/" 2>/dev/null || true
fi
# 运镜视频（两个模型同一条轨迹），缩到 960 宽方便在服务器上看；完整分辨率的在 Global volume
for v in "$S4/gen3c/${SEQ}_gen3c_path.mp4" "$S4/${SEQ}_4c4d_path.mp4" "$S4/gen3c/${SEQ}_gen3c_eval.mp4"; do
  [ -f "$v" ] && ffmpeg -loglevel error -y -i "$v" -vf scale=960:-2 -c:v libx264 -crf 24 -pix_fmt yuv420p "$OUT/videos/$(basename "$v")"
done
if [ -f "$OUT/videos/${SEQ}_gen3c_path.mp4" ] && [ -f "$OUT/videos/${SEQ}_4c4d_path.mp4" ]; then
  ffmpeg -loglevel error -y -i "$OUT/videos/${SEQ}_gen3c_path.mp4" -i "$OUT/videos/${SEQ}_4c4d_path.mp4" \
    -filter_complex "[0:v]scale=960:528,drawtext=text='GEN3C (1 camera)':x=10:y=10:fontsize=24:fontcolor=yellow:box=1:boxcolor=black[a];[1:v]scale=960:528,drawtext=text='4C4D (4 cameras)':x=10:y=10:fontsize=24:fontcolor=yellow:box=1:boxcolor=black[b];[a][b]hstack" \
    -c:v libx264 -crf 24 -pix_fmt yuv420p "$OUT/videos/${SEQ}_path_side_by_side.mp4" || true
fi
phase_end

# 完整结果（含全分辨率视频、4C4D 渲染）放进 out/full，由 run_task.sh 同步到 Global volume；
# fetch 只拉回 ≤20MB 的文件，且排除 .pth
mkdir -p "$OUT/full"
cp "$S4"/gen3c/*.mp4 "$S4"/*.mp4 "$S4"/*.yaml "$OUT/full/" 2>/dev/null || true
tar -cf "$OUT/full/4c4d_renders.tar" -C "$S4/4c4d_model/run/render" . 2>/dev/null || true

python3 - "$OUT" "$SEQ" "$GEN3C_OK" "$FOURC_OK" <<'PY'
import json, sys, pathlib
o = pathlib.Path(sys.argv[1])
def load(n):
    p = o / n
    return json.load(open(p)) if p.exists() else None
m = {"sequence": sys.argv[2], "gen3c_ok": sys.argv[3] == "1", "fourc4d_ok": sys.argv[4] == "1"}
for k, f in (("gen3c", "metrics_gen3c.json"), ("4c4d", "metrics_4c4d.json"), ("4c4d_300f", "metrics_4c4d_300f.json")):
    d = load(f)
    if d:
        m[k] = d["mean"]
m["trajectories"] = load("trajectories.json")
m["gen3c_timing"] = load("gen3c_timing.json")
json.dump(m, open(o / "metrics.json", "w"), indent=2, ensure_ascii=False)
print(json.dumps(m, ensure_ascii=False))
PY
[ $GEN3C_OK = 1 ] && [ $FOURC_OK = 1 ]
