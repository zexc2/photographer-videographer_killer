#!/usr/bin/env bash
# Official 4C4D reproduction: Neural 3D Video (N3V) dataset, README example scene flame_steak, training views 1,10,13,20, official yaml.
# Goal: decide whether 4C4D's mediocre results on Panoptic come from our env / parameters or from Panoptic scenes simply being harder.
# Usage: atlas-run n3v_repro [scene, default flame_steak] [eval frame stride, default 10]
#
# Where this matches upstream / the paper:
#   - Raw videos: official N3V GitHub release (facebookresearch/Neural_3D_Video, CC BY-NC 4.0)
#   - Camera parameters + MASt3R initial point cloud: the authors' preprocessed archive n3v_mast3r.tar.gz (Google Drive, linked in the README)
#   - Frame extraction: same ffmpeg command as the official scripts/n3v2blender.py, first 300 frames only (paper: first 300 frames);
#     the COLMAP step after n3v2blender is not needed (cameras and points come from the authors' archive), so it is not run
#   - Training: official configs/dynerf/<scene>.yaml used as is (exhaust_test and saving the best checkpoint on the test set are upstream defaults)
#     The only explicit flag is --res 2: the paper halves the image size; train.py's --res default of 1 overrides the yaml's resolution: 2 (upstream issue #9)
#   - Rendering: the official command from the README "Visualization" section, --traj arc
#   - Evaluation: official render.py --validate, test views = all cameras except the training views (paper); to save time, 1 frame every N
RUN_DIR=$1; shift
SCENE=${1:-flame_steak}
STRIDE=${2:-10}
TRAIN=1,10,13,20
HERE=$(cd "$(dirname "$0")/.." && pwd)
source "$HERE/lib/common.sh"
OUT=$RUN_DIR/out
mkdir -p "$OUT/videos"
N3V_ROOT=$ATLAS_WORK/n3v
D=$N3V_ROOT/$SCENE                 # training dataset (images/ + sparse/0/)
E=$N3V_ROOT/${SCENE}_eval          # for evaluation: test views keep only every STRIDE-th frame
MODEL=$OUT/full/model              # model lives under out/ and is synced to Global volume right after training
CACHE=$ATLAS_DATA/n3v              # raw data cached on Global volume (cold data, single big files)
FENV=$ATLAS_ENVS/4c4d
PY=$FENV/bin/python
SRC=$ATLAS_SRC/4C4D
declare -A T
step() { STEP=$1; STEP_T0=$SECONDS; log "▶ $1"; }
done_step() { T[$STEP]=$((SECONDS - STEP_T0)); log "■ $STEP took ${T[$STEP]}s"; }
write_timing() {
  python3 -c "import json,sys; json.dump({$(for k in "${!T[@]}"; do printf '"%s": %s, ' "$k" "${T[$k]}"; done)}, open(sys.argv[1],'w'), indent=2)" "$OUT/timing.json"
}
trap write_timing EXIT

# ---------------------------------------------------------------- 1. env
step env
bash "$HERE/setup_4c4d.sh"
apt_ensure unzip ffmpeg libegl1 libgl1 libgomp1
"$PY" -c "import pkg_resources" 2>/dev/null || "$PY" -m pip install -q "setuptools<80"
"$PY" -c "import gdown" 2>/dev/null || "$PY" -m pip install -q gdown
done_step

# ---------------------------------------------------------------- 2. data
step data
mkdir -p "$CACHE" "$N3V_ROOT"
if [ ! -f "$CACHE/$SCENE.zip" ]; then
  log "Downloading N3V raw data $SCENE.zip (GitHub release)"
  curl -fL --retry 3 -o "$ATLAS_CACHE/$SCENE.zip" \
    "https://github.com/facebookresearch/Neural_3D_Video/releases/download/v1.0/$SCENE.zip"
  vol_sync "$ATLAS_CACHE/$SCENE.zip" "$CACHE/"
fi
if [ ! -f "$CACHE/n3v_mast3r.tar.gz" ]; then
  log "Downloading the authors' preprocessed archive n3v_mast3r.tar.gz (Google Drive)"
  # Newer gdown dropped the --id flag (seen 2026-10-15), so use Google Drive's confirm-download direct link
  curl -fsSL --retry 3 -o "$ATLAS_CACHE/n3v_mast3r.tar.gz" \
    "https://drive.usercontent.google.com/download?id=1ua9GnLG0bbg4zBYY5wOgtI90I_hp6OPh&export=download&confirm=t"
  gzip -t "$ATLAS_CACHE/n3v_mast3r.tar.gz" || die "preprocessed archive is incomplete or not gzip (Google Drive may have rate-limited it)"
  vol_sync "$ATLAS_CACHE/n3v_mast3r.tar.gz" "$CACHE/"
fi
if [ ! -d "$D" ]; then
  cp "$CACHE/$SCENE.zip" "$ATLAS_CACHE/raw.zip"
  unzip -q -o "$ATLAS_CACHE/raw.zip" -d "$N3V_ROOT" && rm -f "$ATLAS_CACHE/raw.zip"
fi
[ -f "$D/poses_bounds.npy" ] || die "no $D/poses_bounds.npy after unzip (zip layout differs from expected): $(ls "$N3V_ROOT")"
ls "$D"/cam*.mp4 > /dev/null || die "no cam*.mp4 under $D"
log "$(ls "$D"/cam*.mp4 | wc -l) raw videos"

# Authors' preprocessed archive: inspect the layout and find this scene's sparse/0 (must contain parameters for all cameras)
PRE=$N3V_ROOT/_pre
if [ ! -d "$PRE" ]; then
  mkdir -p "$PRE" && tar -xzf "$CACHE/n3v_mast3r.tar.gz" -C "$PRE"
fi
# When piping into head, the upstream command gets SIGPIPE; with set -e / pipefail that aborts the task (seen 2026-10-15), hence || true
{ find "$PRE" -maxdepth 8 | sed "s|$PRE/||" | grep -i "$SCENE" | head -40; } > "$OUT/preprocessed_layout.txt" || true
log "Paths in the preprocessed archive matching $SCENE (first 40): $(tr '\n' ' ' < "$OUT/preprocessed_layout.txt")"
"$PY" - "$PRE" "$SCENE" "$D" <<'PY' || die "no usable sparse/0 in the preprocessed archive (see out/preprocessed_layout.txt); not training, to avoid wasting GPU time"
import sys, os, glob, shutil, struct
pre, scene, dst = sys.argv[1:]
def n_images(sp):
    b, t = os.path.join(sp, "images.bin"), os.path.join(sp, "images.txt")
    if os.path.exists(b):
        with open(b, "rb") as f:
            return struct.unpack("<Q", f.read(8))[0]
    if os.path.exists(t):
        lines = [l for l in open(t) if l.strip() and not l.startswith("#")]
        return len(lines) // 2
    return 0
cands = [p for p in glob.glob(os.path.join(pre, "**", "sparse", "0"), recursive=True) if scene in p]
info = sorted(((n_images(p), p) for p in cands), reverse=True)
for n, p in info:
    pts = [f for f in os.listdir(p) if f.startswith("points3D")]
    print(f"candidate {p.replace(pre, '')}: {n} cameras, {pts}", file=sys.stderr)
best = [(n, p) for n, p in info if n >= 18 and any(f.startswith("points3D") for f in os.listdir(p))]
if not best:
    sys.exit(1)
n, p = best[0]
out = os.path.join(dst, "sparse", "0")
os.makedirs(out, exist_ok=True)
for f in os.listdir(p):
    shutil.copy(os.path.join(p, f), out)
print(f"using {p.replace(pre, '')}: {n} cameras", file=sys.stderr)
PY

# Frame extraction: same command as line 239 of the official n3v2blender.py (ffmpeg -i video -start_number 0 camXX_%04d.png), plus -frames:v 300
mkdir -p "$D/images"
if [ ! -f "$D/images/cam00_0299.png" ]; then
  ls "$D"/cam*.mp4 | xargs -P 8 -I{} sh -c 'v={}; c=$(basename "$v" .mp4); ffmpeg -loglevel error -y -i "$v" -frames:v 300 -start_number 0 "'"$D"'/images/${c}_%04d.png"'
fi
log "$(ls "$D/images" | wc -l) images ($(ls "$D/images" | sed 's/_.*//' | sort -u | wc -l) cameras x 300 frames)"
# 4C4D only reads images whose cam name is in sparse/0; image count must equal cameras x frames (the data loader asserts this)
done_step

# ---------------------------------------------------------------- 3. training (official yaml)
step train
CFG=$N3V_ROOT/${SCENE}.yaml
"$PY" - "$SRC/configs/dynerf/$SCENE.yaml" "$CFG" "$D" "$MODEL" <<'PY'
import sys
from omegaconf import OmegaConf
c = OmegaConf.load(sys.argv[1])
c.ModelParams.source_path = sys.argv[3]   # only change data and output paths; all other params stay as upstream
c.ModelParams.model_path = sys.argv[4]
OmegaConf.save(c, sys.argv[2])
PY
cp "$CFG" "$OUT/config_used.yaml"
# Note: grep returns non-zero on no match, which with set -e / pipefail aborts the whole task, so the pipeline ends with || true; success is judged by the checkpoint
(cd "$SRC" && "$PY" train.py --config "$CFG" --training_view "$TRAIN" --output_dir run --res 2 2>&1 \
   | tr '\r' '\n' | grep -E 'ITER|Saving|complete|Error|error|Traceback' | tail -40) || true
CKPT=$MODEL/run/chkpnt_best.pth
[ -f "$CKPT" ] || die "training produced no checkpoint"
vol_sync "$OUT/" "$ATLAS_RESULTS/$(basename "$RUN_DIR")/" && log "Model synced to Global volume"
# Also publish to the fixed location so the frontend (n3v_<scene> in pod/configs/models.json) can use it directly
mkdir -p "$ATLAS_CACHE/publish/n3v_$SCENE"
cp "$CKPT" "$ATLAS_CACHE/publish/n3v_$SCENE/model.pth"
cp "$CFG" "$ATLAS_CACHE/publish/n3v_$SCENE/config.yaml"
vol_sync "$ATLAS_CACHE/publish/n3v_$SCENE/" "$ATLAS_VOL/models/n3v_$SCENE/" && log "Frontend model placed at Global volume models/n3v_$SCENE/"
done_step

# ---------------------------------------------------------------- 4. official camera-motion render (--traj arc)
step render_traj
(cd "$SRC" && "$PY" render.py --config "$CFG" --training_view "$TRAIN" --output_dir run --res 2 \
   --traj arc --start_checkpoint "$CKPT" 2>&1 | tail -3) || true
TRAJ=$(ls -d "$MODEL"/run/render/traj/ours_*/renders 2>/dev/null | tail -1) || true
if [ -n "$TRAJ" ]; then
  ffmpeg -loglevel error -y -framerate 30 -i "$TRAJ/%05d.png" -vf "scale=1280:-2" -c:v libx264 -crf 22 \
    -pix_fmt yuv420p "$OUT/videos/${SCENE}_4c4d_arc.mp4"
fi
done_step

# ---------------------------------------------------------------- 5. official evaluation (1 frame every STRIDE on test views)
step eval
rm -rf "$E"; mkdir -p "$E/images"; ln -s "$D/sparse" "$E/sparse"
TRAIN_SET=" $(echo "$TRAIN" | tr ',' '\n' | awk '{printf "cam%02d ", $1}')"   # e.g. " cam01 cam10 cam13 cam20 "
for f in "$D"/images/cam*_*.png; do
  b=$(basename "$f"); cam=${b%%_*}; fr=${b##*_}; fr=$((10#${fr%.png}))
  # Use bash string matching, not echo | grep -q (the latter can misfire on SIGPIPE)
  if [[ "$TRAIN_SET" == *" $cam "* ]] || [ $((fr % STRIDE)) -eq 0 ]; then ln -s "$f" "$E/images/$b"; fi
done
log "Eval set: $(ls "$E/images" | wc -l) images (all frames of training views + every $STRIDE-th frame of test views)"
# 4C4D requires at least one image per camera in sparse/0; each test view keeps frame 0, which satisfies this
ECFG=$N3V_ROOT/${SCENE}_eval.yaml
sed "s|$D|$E|" "$CFG" > "$ECFG"
(cd "$SRC" && "$PY" render.py --config "$ECFG" --training_view "$TRAIN" --output_dir run --res 2 \
   --validate --start_checkpoint "$CKPT" 2>&1 | grep -E 'PSNR|LPIPS|Valid|Error|Traceback' | tail -8) || true
# Official evaluation writes results to render/test/ours_<iter>/stats/validation.json
STATS=$(find "$MODEL/run/render" -name validation.json 2>/dev/null | head -1) || true
[ -n "$STATS" ] && cp "$STATS" "$OUT/official_validation.json"
done_step

# ---------------------------------------------------------------- 6. summary, compared with the paper's tables
"$PY" - "$OUT" "$SCENE" "$STRIDE" "$MODEL" <<'PY'
import json, sys, os, glob, re
out, scene, stride, model = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
paper = {  # paper Table 6/7 (4 training views, N3V)
  "coffee_martini": (19.57, 0.140, 0.203), "cook_spinach": (24.44, 0.075, 0.111),
  "cut_roasted_beef": (23.22, 0.083, 0.124), "flame_salmon": (19.15, 0.136, 0.205),
  "flame_steak": (23.72, 0.075, 0.114), "sear_steak": (23.66, 0.078, 0.118)}
m = {"scene": scene, "eval_frame_stride": stride, "paper": dict(zip(("psnr", "dssim1", "lpips"), paper.get(scene, (None,)*3)))}
v = os.path.join(out, "official_validation.json")
if os.path.exists(v):
    s = json.load(open(v))
    # The official evaluation outputs exactly the paper's metrics: psnr, Dssim1, Dssim2, lpips (AlexNet)
    m["ours"] = {k: (round(s[k], 4) if isinstance(s.get(k), float) else s.get(k))
                 for k in ("psnr", "Dssim1", "Dssim2", "lpips", "num_GS", "valid_images")}
    m["psnr_gap_db"] = round(m["ours"]["psnr"] - m["paper"]["psnr"], 2) if m["paper"]["psnr"] else None
# Official self-test at the end of training (the training script evaluates 1 in 100 test-view images)
log = open(os.path.join(os.path.dirname(out), "run.log"), errors="ignore").read()
tests = re.findall(r"\[ITER (\d+)\] Evaluating test: L1 [\d.]+ PSNR ([\d.]+)", log)
m["train_time_test_psnr"] = {it: round(float(p), 2) for it, p in tests}
json.dump(m, open(os.path.join(out, "metrics.json"), "w"), indent=2, ensure_ascii=False)
print(json.dumps(m, ensure_ascii=False))
PY
[ -f "$OUT/official_validation.json" ]
