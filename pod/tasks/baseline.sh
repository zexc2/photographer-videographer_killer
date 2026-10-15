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
# 第二个参数（模式）：
#   all        全流程（默认）
#   4c4d-only  环境、数据、GEN3C 都已在本 pod 上完成时，只重跑 4C4D
#   4c4d       新 pod 上只跑 4C4D：恢复 4c4d / vipe 环境，从 Global volume 解压阶段 3 数据包（不重新截取、不重跑 ViPE）
MODE=${2:-all}
HERE=$(cd "$(dirname "$0")/.." && pwd)
source "$HERE/lib/common.sh"
CFG=$HERE/configs/panoptic.json
OUT=$RUN_DIR/out
W=$ATLAS_WORK/panoptic/$SEQ
S4=$W/stage4
mkdir -p "$OUT"
ITERS=${FOURC_ITERS:-30000}
# 4C4D 模型放在 out/full 下：训练完立即同步到 Global volume，pre_stop 也会同步。
# 2026-10-14 模型放在容器盘上，后续渲染失败、pod 关机后，2.4 小时的训练结果丢失
MODEL_DIR=$OUT/full/4c4d_model

# ---------------------------------------------------------------- 分阶段计时 + 峰值显存
declare -A T_S VRAM
phase_begin() {
  PHASE=$1; PHASE_T0=$SECONDS
  echo 0 > "$ATLAS_LOGS/peak_$PHASE"
  # 标记文件必须在启动采样循环之前创建，否则循环一开始就因为找不到标记而退出（2026-10-14 峰值一直是 0）
  touch "$ATLAS_LOGS/peak_$PHASE.on"
  ( while [ -f "$ATLAS_LOGS/peak_$PHASE.on" ]; do
      m=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | sort -n | tail -1)
      # 用 if，不用 [ … ] && …：后者在"不更大"时返回非 0，配合 set -e 会让采样进程直接退出（2026-10-14 实测峰值全是 0）
      if [ -n "$m" ] && [ "$m" -gt "$(cat "$ATLAS_LOGS/peak_$PHASE")" ]; then echo "$m" > "$ATLAS_LOGS/peak_$PHASE"; fi
      sleep 3; done ) &
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
if [ "$MODE" = 4c4d ]; then
  phase_begin env_and_data
  bash "$HERE/setup_4c4d.sh"
  env_restore vipe "$(env_key "$HERE/setup_vipe.sh")" || die "Global volume 上没有 vipe 环境包"
  if [ ! -f "$W/meta.json" ]; then
    PKG=$ATLAS_DATA/panoptic/$SEQ/prepared/stage3.tar
    [ -f "$PKG" ] || die "没有阶段 3 数据包 $PKG"
    mkdir -p "$W"
    cp "$PKG" "$ATLAS_CACHE/stage3.tar" && tar -xf "$ATLAS_CACHE/stage3.tar" -C "$W" && rm -f "$ATLAS_CACHE/stage3.tar"
    log "已解压阶段 3 数据包到 $W"
  fi
  phase_end
elif [ "$MODE" != 4c4d-only ]; then
phase_begin env_and_data
bash "$HERE/setup_gen3c.sh"          # 恢复 GEN3C 环境，权重链接到 Global volume
bash "$HERE/tasks/panoptic.sh" "$RUN_DIR" "$SEQ"   # 4c4d / mast3r / vipe 环境 + 重新准备数据（主点居中）
phase_end
fi

META=$W/meta.json
read -r TRAIN EVAL_CAM <<<"$(python3 -c "import json; m=json.load(open('$META')); print(m['fourc4d_training_view'], 'cam%02d' % m['eval_cam'])")"
GEN3C_ENV=$ATLAS_ENVS/gen3c
FOURC_ENV=$ATLAS_ENVS/4c4d

# ---------------------------------------------------------------- 2. 轨迹 + 尺度对齐
# make_trajectories 只需要 numpy + OpenEXR：gen3c 和 vipe 环境都有，4c4d 模式下只恢复了 vipe
TRAJ_PY=$GEN3C_ENV/bin/python; [ -x "$TRAJ_PY" ] || TRAJ_PY=$ATLAS_ENVS/vipe/bin/python
[ -f "$S4/trajectories.json" ] || "$TRAJ_PY" "$HERE/tools/make_trajectories.py" --work "$W"
cp "$S4/trajectories.json" "$OUT/"

# ---------------------------------------------------------------- 3. GEN3C
GEN3C_OK=0
if [ "$MODE" = all ]; then
phase_begin gen3c
cat > "$S4/gen3c_jobs.json" <<JSON
[{"name": "${SEQ}_gen3c_eval", "vipe": "$W/gen3c/vipe", "traj": "$S4/gen3c_eval.npz"},
 {"name": "${SEQ}_gen3c_path", "vipe": "$W/gen3c/vipe", "traj": "$S4/gen3c_path.npz"}]
JSON
GEN3C_OK=1
# 权重目录用绝对路径：GEN3C 仓库自带一个 checkpoints/ 目录（只有 README），setup_gen3c.sh 的 ln -sfn 会把链接建到
# 它里面（checkpoints/checkpoints），导致找不到 model.pt（实测 2026-10-14）。不改 setup_gen3c.sh 以免环境包失效。
(cd "$ATLAS_SRC/GEN3C" && CUDA_HOME=$GEN3C_ENV PATH=$GEN3C_ENV/bin:$PATH PYTHONPATH=. \
   "$GEN3C_ENV/bin/python" "$HERE/tools/gen3c_run.py" --jobs "$S4/gen3c_jobs.json" --out "$S4/gen3c" --checkpoint_dir "$ATLAS_WEIGHTS/gen3c/checkpoints") \
  || { log "GEN3C 失败，改用 --offload 重试一次（显存不够时）"; \
       (cd "$ATLAS_SRC/GEN3C" && CUDA_HOME=$GEN3C_ENV PATH=$GEN3C_ENV/bin:$PATH PYTHONPATH=. \
          "$GEN3C_ENV/bin/python" "$HERE/tools/gen3c_run.py" --jobs "$S4/gen3c_jobs.json" --out "$S4/gen3c" --checkpoint_dir "$ATLAS_WEIGHTS/gen3c/checkpoints" --offload) \
       || GEN3C_OK=0; }
phase_end
cp "$S4/gen3c/gen3c_timing.json" "$OUT/" 2>/dev/null || true
fi

# ---------------------------------------------------------------- 4. 4C4D
phase_begin fourc4d_train
# 4C4D 用的 torchmetrics 0.11.4 需要 pkg_resources，新版 setuptools 已移除 → 训练前补装（实测 2026-10-14；
# 写在这里而不是 setup_4c4d.sh，改安装脚本会让环境包失效、需要重装）
"$FOURC_ENV/bin/python" -c "import pkg_resources" 2>/dev/null || "$FOURC_ENV/bin/python" -m pip install -q "setuptools<80"
FC=$S4/4c4d_${SEQ}.yaml
"$FOURC_ENV/bin/python" - "$ATLAS_SRC/4C4D/configs/dynerf/flame_steak.yaml" "$FC" "$W/4c4d" "$MODEL_DIR" "$ITERS" <<'PY'
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
CKPT=$MODEL_DIR/run/chkpnt_best.pth
[ -f "$CKPT" ] || { log "4C4D 没有产出 checkpoint"; FOURC_OK=0; }
[ $FOURC_OK = 1 ] && { vol_sync "$OUT/" "$ATLAS_RESULTS/$(basename "$RUN_DIR")/" && log "4C4D 模型已同步到 Global volume"; } || true
phase_end

if [ $FOURC_OK = 1 ]; then
  phase_begin fourc4d_render
  # render.py 导入的 open3d 需要 libEGL（runpod/pytorch 镜像里没有，2026-10-14 渲染因此失败）
  apt_ensure libegl1 libgl1 libgomp1
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
  TEST_R=$(ls -d "$MODEL_DIR"/run/render/test/ours_*/renders 2>/dev/null | tail -1)
  TRAJ_R=$(ls -d "$MODEL_DIR"/run/render/traj/ours_*/renders 2>/dev/null | tail -1)
  if [ -n "$TEST_R" ]; then
    $EV --pred "$TEST_R" --kind 4c4d --frames 121 --out "$OUT/metrics_4c4d.json" \
        --video "$OUT/videos/${SEQ}_eval_4c4d_vs_gt.mp4" || log "4C4D 评估失败"
    $EV --pred "$TEST_R" --kind 4c4d --frames 300 --out "$OUT/metrics_4c4d_300f.json" || true
  fi
  [ -n "$TRAJ_R" ] && ffmpeg -loglevel error -y -framerate 24 -i "$TRAJ_R/%05d.png" -c:v libx264 -crf 18 \
    -pix_fmt yuv420p "$S4/${SEQ}_4c4d_path.mp4" || true
  cp "$MODEL_DIR"/run/render/test/ours_*/../../stats/*.json "$OUT/" 2>/dev/null || true
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
[ -d "$MODEL_DIR/run/render" ] && tar -cf "$OUT/full/4c4d_renders.tar" -C "$MODEL_DIR/run/render" . 2>/dev/null || true

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
# 4c4d-only 模式下 GEN3C 是有意跳过的，只按 4C4D 判断成败（2026-10-14 曾因此把成功的任务报成 failed）
if [ "$MODE" != all ]; then [ $FOURC_OK = 1 ]; else [ $GEN3C_OK = 1 ] && [ $FOURC_OK = 1 ]; fi
