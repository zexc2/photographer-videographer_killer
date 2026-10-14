#!/usr/bin/env bash
# 阶段 3：准备 CMU Panoptic 数据（4C4D 的 COLMAP 输入 + GEN3C 的单目视频和 ViPE 结果）。
# 用法：atlas-run panoptic [序列名...]（默认 configs/panoptic.json 里的全部序列）
# 产出：
#   $ATLAS_DATA/panoptic/<seq>/calibration_<seq>.json、raw/（截取的视频片段，下次不用再联网）
#   $ATLAS_DATA/panoptic/<seq>/prepared/stage3.tar（4c4d/ 数据集 + gen3c/ 输入和 ViPE 结果 + meta.json，一个大文件）
#   out/<seq>/：缩略图、点云投影检查图、ViPE 可视化、各项指标（拉回服务器）
RUN_DIR=$1; shift
HERE=$(cd "$(dirname "$0")/.." && pwd)
source "$HERE/lib/common.sh"
CFG=$HERE/configs/panoptic.json
MAST3R_CK=$ATLAS_WEIGHTS/mast3r/MASt3R_ViTLarge_BaseDecoder_512_catmlpdpt_metric.pth

# ---------------------------------------------------------------- 环境（从 Global volume 的环境包恢复，实测耗时）
t0=$SECONDS
bash "$HERE/setup_4c4d.sh"   # 4c4d + mast3r 环境、代码、MASt3R 权重
bash "$HERE/setup_vipe.sh"   # vipe 环境 + 模型缓存
ENV_SEC=$((SECONDS - t0))
log "环境就绪，用时 ${ENV_SEC}s"
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
  # 注意：( set -e; … ) || … 这种写法里 set -e 会失效；放到后台再 wait，set -e 才真正生效
  (
    set -e
    t=$SECONDS
    "$PY" "$HERE/tools/panoptic_prepare.py" --config "$CFG" --seq "$SEQ" --work "$W" --data-root "$ATLAS_DATA/panoptic"
    PREP=$((SECONDS - t))

    t=$SECONDS
    PYTHONPATH=$ATLAS_SRC/mast3r "$PY" "$HERE/tools/mast3r_posed_points.py" --data "$W/4c4d" --cams "$TRAIN" \
      --weights "$MAST3R_CK" --preview "$W/previews"
    PTS=$((SECONDS - t))

    # GEN3C 的输入：单目视频 → ViPE 估计深度 / 位姿 / 内参（模拟只有一台手机、没有标定的情况）
    t=$SECONDS
    (cd "$W/gen3c" && "$ATLAS_ENVS/vipe/bin/vipe" infer input.mp4 -o vipe -v)
    VIPE=$((SECONDS - t))
    # ViPE 估计的内参 vs Panoptic 真实标定
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

    # 小文件拉回服务器：缩略图、点云检查图、ViPE 可视化、指标
    cp "$W"/previews/*.jpg "$O/"
    cp "$W/meta.json" "$W/4c4d/points3D_stats.json" "$W/vipe_intrinsics.json" "$O/"
    find "$W/gen3c/vipe" -name '*.mp4' -path '*vipe/vipe/*' -size -15M -exec cp {} "$O/" \; 2>/dev/null || true
    printf '{"prepare_s":%d,"points_s":%d,"vipe_s":%d,"pack_s":%d,"tar_gb":%s,"frames":%d}\n' \
      "$PREP" "$PTS" "$VIPE" "$PACK" "${TAR_GB:-0}" "$(ls "$W/4c4d/images" | wc -l)" > "$O/timing.json"
    log "$SEQ 完成：$(cat "$O/timing.json")"
  ) &
  wait $! || { log "$SEQ 失败"; rc=1; }
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
