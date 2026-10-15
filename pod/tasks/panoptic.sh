#!/usr/bin/env bash
# Stage 3: prepare CMU Panoptic data (COLMAP input for 4C4D + monocular video and ViPE results for GEN3C).
# Usage: atlas-run panoptic [sequence...] (default: all sequences in configs/panoptic.json)
# Outputs:
#   $ATLAS_DATA/panoptic/<seq>/calibration_<seq>.json, raw/ (trimmed video clips, so no re-download next time)
#   $ATLAS_DATA/panoptic/<seq>/prepared/stage3.tar (4c4d/ dataset + gen3c/ input and ViPE results + meta.json, one big file)
#   out/<seq>/: thumbnails, point cloud projection check images, ViPE visualizations, metrics (fetched to the server)
RUN_DIR=$1; shift
HERE=$(cd "$(dirname "$0")/.." && pwd)
source "$HERE/lib/common.sh"
CFG=$HERE/configs/panoptic.json
MAST3R_CK=$ATLAS_WEIGHTS/mast3r/MASt3R_ViTLarge_BaseDecoder_512_catmlpdpt_metric.pth

# ---------------------------------------------------------------- envs (restored from env packs on Global volume, timed)
t0=$SECONDS
bash "$HERE/setup_4c4d.sh"   # 4c4d + mast3r envs, code, MASt3R weights
bash "$HERE/setup_vipe.sh"   # vipe env + model cache
ENV_SEC=$((SECONDS - t0))
log "Envs ready in ${ENV_SEC}s"
PY=$ATLAS_ENVS/mast3r/bin/python

SEQS=("$@")
[ ${#SEQS[@]} -eq 0 ] && mapfile -t SEQS < <(python3 -c "import json; [print(s['name']) for s in json.load(open('$CFG'))['sequences']]")

rc=0
for SEQ in "${SEQS[@]}"; do
  W=$ATLAS_WORK/panoptic/$SEQ
  O=$RUN_DIR/out/$SEQ
  mkdir -p "$W" "$O"
  log "===== $SEQ ====="
  TRAIN=$(python3 -c "import json; s=[x for x in json.load(open('$CFG'))['sequences'] if x['name']=='$SEQ'][0]; print(','.join(map(str, s['train_cams'])))")
  # Note: set -e is ignored inside ( set -e; ... ) || ...; running it in the background and then waiting makes set -e actually work
  (
    set -e
    t=$SECONDS
    "$PY" "$HERE/tools/panoptic_prepare.py" --config "$CFG" --seq "$SEQ" --work "$W" --data-root "$ATLAS_DATA/panoptic"
    PREP=$((SECONDS - t))

    t=$SECONDS
    if [ -s "$W/4c4d/sparse/0/points3D.txt" ] && [ -f "$W/4c4d/points3D_stats.json" ]; then
      log "Point cloud already exists, skipping"
    else
      PYTHONPATH=$ATLAS_SRC/mast3r "$PY" "$HERE/tools/mast3r_posed_points.py" --data "$W/4c4d" --cams "$TRAIN" \
        --weights "$MAST3R_CK" --preview "$W/previews"
    fi
    PTS=$((SECONDS - t))

    # GEN3C input: monocular video -> ViPE estimates depth / poses / intrinsics (simulates a single uncalibrated phone)
    t=$SECONDS
    # After a conda-pack restore, bin/vipe has the shebang "#!/usr/bin/env python", so the env must be first on PATH,
    # otherwise the system python is used and fails with No module named 'vipe' (seen 2026-10-14)
    # SETUPTOOLS_USE_DISTUTILS=stdlib: in the ViPE env the files of the pip and conda setuptools copies get mixed by conda-pack,
    # and torch.utils.cpp_extension importing setuptools fails with No module named 'distutils.compilers.errors' (seen 2026-10-14).
    # Work around it with Python 3.11's bundled distutils; the real fix is a PROGRESS to-do (install setuptools via conda on the next env rebuild)
    # No -v: -v starts the rerun visualization viewer; the pod has no display, so the process hangs after ViPE finishes (hung for 20 min in testing).
    # Safety net: once the log shows Finished and the pose file exists, kill the process.
    if [ -f "$W/gen3c/vipe/pose/input.npz" ]; then
      log "ViPE results already exist, skipping"
    else
      (cd "$W/gen3c" && PATH=$ATLAS_ENVS/vipe/bin:$PATH SETUPTOOLS_USE_DISTUTILS=stdlib \
        vipe infer input.mp4 -o vipe > vipe.log 2>&1) &
      vp=$!
      while kill -0 $vp 2>/dev/null; do
        if grep -q "vipe - INFO - Finished$" "$W/gen3c/vipe.log" 2>/dev/null && [ -f "$W/gen3c/vipe/pose/input.npz" ]; then
          sleep 20; pkill -f "vipe infer input.mp4" || true; break
        fi
        sleep 10
      done
      wait $vp || true
      tail -3 "$W/gen3c/vipe.log" >&2
      [ -f "$W/gen3c/vipe/pose/input.npz" ] || { echo "ViPE produced no poses" >&2; exit 1; }
    fi
    VIPE=$((SECONDS - t))
    # ViPE-estimated intrinsics vs. Panoptic ground-truth calibration
    "$ATLAS_ENVS/vipe/bin/python" - "$W" <<'PY' > "$W/vipe_intrinsics.json"
import json, sys, numpy as np
w = sys.argv[1]
meta = json.load(open(f"{w}/meta.json"))
K = np.array(meta["cameras"][f"cam{meta['gen3c_input_cam']:02d}"]["K"])
est = np.load(f"{w}/gen3c/vipe/intrinsics/input.npz")["data"]
fx, fy, cx, cy = est.mean(0)[:4]
gt = [K[0, 0], K[1, 1], K[0, 2], K[1, 2]]
print(json.dumps({"vipe_mean": [round(float(x), 1) for x in (fx, fy, cx, cy)], "gt": [round(x, 1) for x in gt],
                  "focal_error_pct": round(float(abs(fx - gt[0]) / gt[0] * 100), 2)}))
PY

    t=$SECONDS
    tar -cf "$ATLAS_CACHE/$SEQ-stage3.tar" -C "$W" 4c4d gen3c meta.json
    mkdir -p "$ATLAS_DATA/panoptic/$SEQ/prepared"
    vol_sync "$ATLAS_CACHE/$SEQ-stage3.tar" "$ATLAS_DATA/panoptic/$SEQ/prepared/"
    mv -f "$ATLAS_DATA/panoptic/$SEQ/prepared/$SEQ-stage3.tar" "$ATLAS_DATA/panoptic/$SEQ/prepared/stage3.tar"
    TAR_GB=$(du -BG "$ATLAS_CACHE/$SEQ-stage3.tar" | cut -f1 | tr -d G)
    rm -f "$ATLAS_CACHE/$SEQ-stage3.tar"
    PACK=$((SECONDS - t))

    # Small files fetched to the server: thumbnails, point cloud check images, ViPE visualizations, metrics
    cp "$W"/previews/*.jpg "$O/"
    cp "$W/meta.json" "$W/4c4d/points3D_stats.json" "$W/vipe_intrinsics.json" "$O/"
    find "$W/gen3c/vipe" -name '*.mp4' -path '*vipe/vipe/*' -size -15M -exec cp {} "$O/" \; 2>/dev/null || true
    printf '{"prepare_s":%d,"points_s":%d,"vipe_s":%d,"pack_s":%d,"tar_gb":%s,"frames":%d}\n' \
      "$PREP" "$PTS" "$VIPE" "$PACK" "${TAR_GB:-0}" "$(ls "$W/4c4d/images" | wc -l)" > "$O/timing.json"
    log "$SEQ done: $(cat "$O/timing.json")"
  ) &
  wait $! || { log "$SEQ failed"; rc=1; }
done

python3 - "$RUN_DIR/out" "$ENV_SEC" <<'PY'
import json, sys, pathlib
out = pathlib.Path(sys.argv[1])
m = {"env_restore_s": int(sys.argv[2]), "sequences": {}}
for d in sorted(p for p in out.iterdir() if p.is_dir() and p.name != "preview"):
    r = {}
    for f in ("timing.json", "points3D_stats.json", "vipe_intrinsics.json"):
        if (d / f).exists():
            r[f.split(".")[0]] = json.load(open(d / f))
    m["sequences"][d.name] = r
json.dump(m, open(out / "metrics.json", "w"), indent=2, ensure_ascii=False)
PY
exit $rc
