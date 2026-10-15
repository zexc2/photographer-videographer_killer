#!/usr/bin/env bash
# Stage 4: GEN3C vs 4C4D baseline. Usage: atlas-run baseline [sequence] (default 171204_pose1)
#   1. Restore envs; re-prepare the sequence with the new (principal-point-centred) pipeline (tasks/panoptic.sh)
#   2. Build the trajectories shared by both models + ViPE scale alignment (tools/make_trajectories.py)
#   3. GEN3C: 121 frames each for the eval camera and the camera-motion trajectory (tools/gen3c_run.py, model loaded once)
#   4. 4C4D: train 30k steps on 4 cameras -> official render.py --validate for the eval camera -> custom camera-motion trajectory (tools/fourc4d_render.py)
#   5. Evaluate PSNR / SSIM / LPIPS the same way for both (tools/eval_metrics.py) and build comparison videos
# Each step records time and peak VRAM; results go to out/ (small files fetched to the server), full results are synced to Global volume by run_task.sh.
RUN_DIR=$1; shift
SEQ=${1:-171204_pose1}
# Second argument (mode):
#   all        full pipeline (default)
#   4c4d-only  envs, data and GEN3C already done on this pod; re-run only 4C4D
#   4c4d       run only 4C4D on a fresh pod: restore 4c4d / vipe envs, unpack the stage 3 data package from Global volume (no re-trimming, no ViPE re-run)
MODE=${2:-all}
HERE=$(cd "$(dirname "$0")/.." && pwd)
source "$HERE/lib/common.sh"
CFG=$HERE/configs/panoptic.json
OUT=$RUN_DIR/out
W=$ATLAS_WORK/panoptic/$SEQ
S4=$W/stage4
mkdir -p "$OUT"
ITERS=${FOURC_ITERS:-30000}
# The 4C4D model lives under out/full: synced to Global volume right after training, and pre_stop syncs it too.
# On 2026-10-14 the model was on the container disk; a later render failure and pod shutdown lost 2.4 hours of training
MODEL_DIR=$OUT/full/4c4d_model

# ---------------------------------------------------------------- per-phase timing + peak VRAM
declare -A T_S VRAM
phase_begin() {
  PHASE=$1; PHASE_T0=$SECONDS
  echo 0 > "$ATLAS_LOGS/peak_$PHASE"
  # The marker file must exist before the sampling loop starts, or the loop exits immediately (2026-10-14: peak was always 0)
  touch "$ATLAS_LOGS/peak_$PHASE.on"
  ( while [ -f "$ATLAS_LOGS/peak_$PHASE.on" ]; do
      m=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | sort -n | tail -1)
      # Use if, not [ ... ] && ...: the latter returns non-zero when "not larger", and with set -e the sampler exits (2026-10-14: all peaks were 0)
      if [ -n "$m" ] && [ "$m" -gt "$(cat "$ATLAS_LOGS/peak_$PHASE")" ]; then echo "$m" > "$ATLAS_LOGS/peak_$PHASE"; fi
      sleep 3; done ) &
  log "▶ $PHASE"
}
phase_end() {
  rm -f "$ATLAS_LOGS/peak_$PHASE.on"; sleep 4
  T_S[$PHASE]=$((SECONDS - PHASE_T0)); VRAM[$PHASE]=$(cat "$ATLAS_LOGS/peak_$PHASE")
  log "■ $PHASE took ${T_S[$PHASE]}s, peak VRAM ${VRAM[$PHASE]} MiB"
}
write_timing() {
  python3 - "$OUT/timing.json" <<PY
import json, sys
t = {$(for k in "${!T_S[@]}"; do printf '"%s": {"seconds": %s, "peak_vram_mib": %s}, ' "$k" "${T_S[$k]}" "${VRAM[$k]}"; done)}
json.dump(t, open(sys.argv[1], "w"), indent=2, ensure_ascii=False)
PY
}
trap write_timing EXIT

# ---------------------------------------------------------------- 1. envs + data
if [ "$MODE" = 4c4d ]; then
  phase_begin env_and_data
  bash "$HERE/setup_4c4d.sh"
  env_restore vipe "$(env_key "$HERE/setup_vipe.sh")" || die "no vipe env pack on Global volume"
  if [ ! -f "$W/meta.json" ]; then
    PKG=$ATLAS_DATA/panoptic/$SEQ/prepared/stage3.tar
    [ -f "$PKG" ] || die "stage 3 data package $PKG missing"
    mkdir -p "$W"
    cp "$PKG" "$ATLAS_CACHE/stage3.tar" && tar -xf "$ATLAS_CACHE/stage3.tar" -C "$W" && rm -f "$ATLAS_CACHE/stage3.tar"
    log "Unpacked stage 3 data package to $W"
  fi
  phase_end
elif [ "$MODE" != 4c4d-only ]; then
phase_begin env_and_data
bash "$HERE/setup_gen3c.sh"          # restore GEN3C env, weights linked to Global volume
bash "$HERE/tasks/panoptic.sh" "$RUN_DIR" "$SEQ"   # 4c4d / mast3r / vipe envs + re-prepare data (principal point centred)
phase_end
fi

META=$W/meta.json
read -r TRAIN EVAL_CAM <<<"$(python3 -c "import json; m=json.load(open('$META')); print(m['fourc4d_training_view'], 'cam%02d' % m['eval_cam'])")"
GEN3C_ENV=$ATLAS_ENVS/gen3c
FOURC_ENV=$ATLAS_ENVS/4c4d

# ---------------------------------------------------------------- 2. trajectories + scale alignment
# make_trajectories only needs numpy + OpenEXR: both gen3c and vipe envs have them, and 4c4d mode restores only vipe
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
# Use an absolute weights path: the GEN3C repo ships a checkpoints/ dir (README only), so setup_gen3c.sh's ln -sfn creates the link
# inside it (checkpoints/checkpoints) and model.pt is not found (seen 2026-10-14). setup_gen3c.sh is left alone so env packs stay valid.
(cd "$ATLAS_SRC/GEN3C" && CUDA_HOME=$GEN3C_ENV PATH=$GEN3C_ENV/bin:$PATH PYTHONPATH=. \
   "$GEN3C_ENV/bin/python" "$HERE/tools/gen3c_run.py" --jobs "$S4/gen3c_jobs.json" --out "$S4/gen3c" --checkpoint_dir "$ATLAS_WEIGHTS/gen3c/checkpoints") \
  || { log "GEN3C failed, retrying once with --offload (in case VRAM ran out)"; \
       (cd "$ATLAS_SRC/GEN3C" && CUDA_HOME=$GEN3C_ENV PATH=$GEN3C_ENV/bin:$PATH PYTHONPATH=. \
          "$GEN3C_ENV/bin/python" "$HERE/tools/gen3c_run.py" --jobs "$S4/gen3c_jobs.json" --out "$S4/gen3c" --checkpoint_dir "$ATLAS_WEIGHTS/gen3c/checkpoints" --offload) \
       || GEN3C_OK=0; }
phase_end
cp "$S4/gen3c/gen3c_timing.json" "$OUT/" 2>/dev/null || true
fi

# ---------------------------------------------------------------- 4. 4C4D
phase_begin fourc4d_train
# torchmetrics 0.11.4 used by 4C4D needs pkg_resources, which newer setuptools removed -> install it before training (seen 2026-10-14;
# done here rather than in setup_4c4d.sh, since changing the setup script invalidates the env pack and forces a reinstall)
"$FOURC_ENV/bin/python" -c "import pkg_resources" 2>/dev/null || "$FOURC_ENV/bin/python" -m pip install -q "setuptools<80"
FC=$S4/4c4d_${SEQ}.yaml
"$FOURC_ENV/bin/python" - "$ATLAS_SRC/4C4D/configs/dynerf/flame_steak.yaml" "$FC" "$W/4c4d" "$MODEL_DIR" "$ITERS" <<'PY'
import sys
from omegaconf import OmegaConf
c = OmegaConf.load(sys.argv[1])
# Note: train.py parses the CLI first and then merges the yaml, so yaml values override the CLI; the iteration count must be set here
c.OptimizationParams.iterations = int(sys.argv[5])
c.ModelParams.source_path = sys.argv[3]
c.ModelParams.model_path = sys.argv[4]
c.time_duration = [0.0, 10.0]
c.exhaust_test = False   # don't keep testing on the eval camera during training (saves time and avoids picking the model using the eval camera)
OmegaConf.save(c, sys.argv[2])
PY
FOURC_OK=1
(cd "$ATLAS_SRC/4C4D" && PATH=$FOURC_ENV/bin:$PATH "$FOURC_ENV/bin/python" train.py --config "$FC" \
   --training_view "$TRAIN" --output_dir run --res 2 \
   --test_iterations "$ITERS" --save_iterations "$ITERS" 2>&1 | tr '\r' '\n' | grep -vE '^Training: +[0-9]+%.*it/s\]$' | tail -40) \
  || FOURC_OK=0
CKPT=$MODEL_DIR/run/chkpnt_best.pth
[ -f "$CKPT" ] || { log "4C4D produced no checkpoint"; FOURC_OK=0; }
[ $FOURC_OK = 1 ] && { vol_sync "$OUT/" "$ATLAS_RESULTS/$(basename "$RUN_DIR")/" && log "4C4D model synced to Global volume"; } || true
phase_end

if [ $FOURC_OK = 1 ]; then
  phase_begin fourc4d_render
  # open3d imported by render.py needs libEGL (missing from the runpod/pytorch image; rendering failed because of it on 2026-10-14)
  apt_ensure libegl1 libgl1 libgomp1
  RARGS=(--config "$FC" --training_view "$TRAIN" --start_checkpoint "$CKPT" --res 2 --output_dir run)
  (cd "$ATLAS_SRC/4C4D" && "$FOURC_ENV/bin/python" render.py "${RARGS[@]}" --validate 2>&1 | tail -5) \
    || log "4C4D eval-camera render failed"
  (cd "$ATLAS_SRC/4C4D" && ATLAS_4C4D_PATH=$S4/4c4d_path.npz PYTHONPATH=. \
     "$FOURC_ENV/bin/python" "$HERE/tools/fourc4d_render.py" "${RARGS[@]}" --traj custom 2>&1 | tail -5) \
    || log "4C4D camera-motion render failed"
  phase_end
fi

# ---------------------------------------------------------------- 5. evaluation + comparison videos
phase_begin eval
EV="$FOURC_ENV/bin/python $HERE/tools/eval_metrics.py --gt-dir $W/4c4d/images --gt-cam $EVAL_CAM"
mkdir -p "$OUT/videos"
if [ $GEN3C_OK = 1 ]; then
  $EV --pred "$S4/gen3c/${SEQ}_gen3c_eval.mp4" --kind gen3c --frames 121 --out "$OUT/metrics_gen3c.json" \
      --video "$OUT/videos/${SEQ}_eval_gen3c_vs_gt.mp4" || log "GEN3C evaluation failed"
fi
if [ $FOURC_OK = 1 ]; then
  TEST_R=$(ls -d "$MODEL_DIR"/run/render/test/ours_*/renders 2>/dev/null | tail -1)
  TRAJ_R=$(ls -d "$MODEL_DIR"/run/render/traj/ours_*/renders 2>/dev/null | tail -1)
  if [ -n "$TEST_R" ]; then
    $EV --pred "$TEST_R" --kind 4c4d --frames 121 --out "$OUT/metrics_4c4d.json" \
        --video "$OUT/videos/${SEQ}_eval_4c4d_vs_gt.mp4" || log "4C4D evaluation failed"
    $EV --pred "$TEST_R" --kind 4c4d --frames 300 --out "$OUT/metrics_4c4d_300f.json" || true
  fi
  [ -n "$TRAJ_R" ] && ffmpeg -loglevel error -y -framerate 24 -i "$TRAJ_R/%05d.png" -c:v libx264 -crf 18 \
    -pix_fmt yuv420p "$S4/${SEQ}_4c4d_path.mp4" || true
  cp "$MODEL_DIR"/run/render/test/ours_*/../../stats/*.json "$OUT/" 2>/dev/null || true
fi
# Camera-motion videos (same trajectory for both models), scaled to 960 wide for viewing on the server; full resolution stays on Global volume
for v in "$S4/gen3c/${SEQ}_gen3c_path.mp4" "$S4/${SEQ}_4c4d_path.mp4" "$S4/gen3c/${SEQ}_gen3c_eval.mp4"; do
  [ -f "$v" ] && ffmpeg -loglevel error -y -i "$v" -vf scale=960:-2 -c:v libx264 -crf 24 -pix_fmt yuv420p "$OUT/videos/$(basename "$v")"
done
if [ -f "$OUT/videos/${SEQ}_gen3c_path.mp4" ] && [ -f "$OUT/videos/${SEQ}_4c4d_path.mp4" ]; then
  ffmpeg -loglevel error -y -i "$OUT/videos/${SEQ}_gen3c_path.mp4" -i "$OUT/videos/${SEQ}_4c4d_path.mp4" \
    -filter_complex "[0:v]scale=960:528,drawtext=text='GEN3C (1 camera)':x=10:y=10:fontsize=24:fontcolor=yellow:box=1:boxcolor=black[a];[1:v]scale=960:528,drawtext=text='4C4D (4 cameras)':x=10:y=10:fontsize=24:fontcolor=yellow:box=1:boxcolor=black[b];[a][b]hstack" \
    -c:v libx264 -crf 24 -pix_fmt yuv420p "$OUT/videos/${SEQ}_path_side_by_side.mp4" || true
fi
phase_end

# Full results (incl. full-resolution videos, 4C4D renders) go to out/full and are synced to Global volume by run_task.sh;
# fetch only pulls files <=20MB and excludes .pth
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
# In 4c4d-only mode GEN3C is skipped on purpose, so success is judged on 4C4D only (2026-10-14 a successful run was once reported as failed because of this)
if [ "$MODE" != all ]; then [ $FOURC_OK = 1 ]; else [ $GEN3C_OK = 1 ] && [ $FOURC_OK = 1 ]; fi
