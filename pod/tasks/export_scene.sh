#!/usr/bin/env bash
# 导出前端用的场景预览（点云、全部机位相机参数、各机位缩略图）。用法：atlas-run export_scene <模型名>
RUN_DIR=$1; MODEL_KEY=${2:?需要模型名}
source "$(dirname "$0")/_model_common.sh"
SP=$ATLAS_CACHE/sparse_$MODEL_KEY
rm -rf "$SP"; mkdir -p "$SP"
# 只解出这个场景的 sparse/0（预处理包里的路径前缀不固定，用通配匹配）
tar -xzf "$ATLAS_VOL/$SPARSE_TAR" -C "$SP" --wildcards "*${SPARSE_MEMBER}*"
S0=$(dirname "$(find "$SP" -name images.bin | head -1)") || true
[ -f "$S0/cameras.bin" ] || die "预处理包里找不到 $SPARSE_MEMBER"
(cd "$SRC4" && PYTHONPATH=. "$PY" "$HERE/tools/fourc4d_direct.py" export --config "$CFG" --checkpoint "$CKPT" \
   --sparse "$S0" --out "$RUN_DIR/out/scene")
