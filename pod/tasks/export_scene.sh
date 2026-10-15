#!/usr/bin/env bash
# Export the scene preview for the frontend (point cloud, all camera parameters, per-camera thumbnails). Usage: atlas-run export_scene <model name>
RUN_DIR=$1; MODEL_KEY=${2:?model name required}
source "$(dirname "$0")/_model_common.sh"
SP=$ATLAS_CACHE/sparse_$MODEL_KEY
rm -rf "$SP"; mkdir -p "$SP"
# Extract only this scene's sparse/0 (the path prefix inside the preprocessing tar varies, so use a wildcard)
tar -xzf "$ATLAS_VOL/$SPARSE_TAR" -C "$SP" --wildcards "*${SPARSE_MEMBER}*"
S0=$(dirname "$(find "$SP" -name images.bin | head -1)") || true
[ -f "$S0/cameras.bin" ] || die "$SPARSE_MEMBER not found in the preprocessing tar"
(cd "$SRC4" && PYTHONPATH=. "$PY" "$HERE/tools/fourc4d_direct.py" export --config "$CFG" --checkpoint "$CKPT" \
   --sparse "$S0" --out "$RUN_DIR/out/scene")
