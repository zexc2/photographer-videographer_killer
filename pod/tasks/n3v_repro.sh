#!/usr/bin/env bash
# 4C4D 官方复现：Neural 3D Video（N3V）数据集，README 示例场景 flame_steak，训练视角 1,10,13,20，官方 yaml。
# 目的：判断 4C4D 在 Panoptic 上效果一般，是我们的环境 / 参数问题，还是 Panoptic 场景本身太难。
# 用法：atlas-run n3v_repro [场景名，默认 flame_steak] [评估帧间隔，默认 10]
#
# 与官方 / 论文对齐的地方：
#   - 原始视频：N3V 官方 GitHub release（facebookresearch/Neural_3D_Video，CC BY-NC 4.0）
#   - 相机参数 + MASt3R 初始点云：作者提供的预处理包 n3v_mast3r.tar.gz（Google Drive，README 中的链接）
#   - 图片提取：与官方 scripts/n3v2blender.py 相同的 ffmpeg 命令，只取前 300 帧（论文：first 300 frames）；
#     n3v2blender 之后的 COLMAP 步骤不需要（相机参数和点云直接用作者的预处理包），故不运行
#   - 训练：官方 configs/dynerf/<场景>.yaml 原样使用（exhaust_test、按测试集保存最佳 checkpoint 都是官方默认）
#     唯一显式参数 --res 2：论文写明图片缩小一半；train.py 的 --res 默认 1 会覆盖 yaml 的 resolution: 2（官方 issue #9）
#   - 渲染：README "Visualization" 的官方命令 --traj arc
#   - 评估：官方 render.py --validate，测试视角 = 训练视角以外的全部机位（论文）；为省时间每隔 N 帧取 1 帧
RUN_DIR=$1; shift
SCENE=${1:-flame_steak}
STRIDE=${2:-10}
TRAIN=1,10,13,20
HERE=$(cd "$(dirname "$0")/.." && pwd)
source "$HERE/lib/common.sh"
OUT=$RUN_DIR/out
mkdir -p "$OUT/videos"
N3V_ROOT=$ATLAS_WORK/n3v
D=$N3V_ROOT/$SCENE                 # 训练用数据集（images/ + sparse/0/）
E=$N3V_ROOT/${SCENE}_eval          # 评估用：测试视角只保留每 STRIDE 帧
MODEL=$OUT/full/model              # 模型放 out/ 下，训练完立即同步到 Global volume
CACHE=$ATLAS_DATA/n3v              # 原始数据缓存在 Global volume（冷数据、单个大文件）
FENV=$ATLAS_ENVS/4c4d
PY=$FENV/bin/python
SRC=$ATLAS_SRC/4C4D
declare -A T
step() { STEP=$1; STEP_T0=$SECONDS; log "▶ $1"; }
done_step() { T[$STEP]=$((SECONDS - STEP_T0)); log "■ $STEP 用时 ${T[$STEP]}s"; }
write_timing() {
  python3 -c "import json,sys; json.dump({$(for k in "${!T[@]}"; do printf '"%s": %s, ' "$k" "${T[$k]}"; done)}, open(sys.argv[1],'w'), indent=2)" "$OUT/timing.json"
}
trap write_timing EXIT

# ---------------------------------------------------------------- 1. 环境
step env
bash "$HERE/setup_4c4d.sh"
apt_ensure unzip ffmpeg libegl1 libgl1 libgomp1
"$PY" -c "import pkg_resources" 2>/dev/null || "$PY" -m pip install -q "setuptools<80"
"$PY" -c "import gdown" 2>/dev/null || "$PY" -m pip install -q gdown
done_step

# ---------------------------------------------------------------- 2. 数据
step data
mkdir -p "$CACHE" "$N3V_ROOT"
if [ ! -f "$CACHE/$SCENE.zip" ]; then
  log "下载 N3V 原始数据 $SCENE.zip（GitHub release）"
  curl -fL --retry 3 -o "$ATLAS_CACHE/$SCENE.zip" \
    "https://github.com/facebookresearch/Neural_3D_Video/releases/download/v1.0/$SCENE.zip"
  vol_sync "$ATLAS_CACHE/$SCENE.zip" "$CACHE/"
fi
if [ ! -f "$CACHE/n3v_mast3r.tar.gz" ]; then
  log "下载作者的预处理包 n3v_mast3r.tar.gz（Google Drive）"
  "$PY" -m gdown --quiet --id 1ua9GnLG0bbg4zBYY5wOgtI90I_hp6OPh -O "$ATLAS_CACHE/n3v_mast3r.tar.gz"
  vol_sync "$ATLAS_CACHE/n3v_mast3r.tar.gz" "$CACHE/"
fi
if [ ! -d "$D" ]; then
  cp "$CACHE/$SCENE.zip" "$ATLAS_CACHE/raw.zip"
  unzip -q -o "$ATLAS_CACHE/raw.zip" -d "$N3V_ROOT" && rm -f "$ATLAS_CACHE/raw.zip"
fi
[ -f "$D/poses_bounds.npy" ] || die "解压后没有 $D/poses_bounds.npy（zip 目录结构和预期不同）：$(ls "$N3V_ROOT")"
ls "$D"/cam*.mp4 > /dev/null || die "$D 下没有 cam*.mp4"
log "原始视频 $(ls "$D"/cam*.mp4 | wc -l) 路"

# 作者的预处理包：先看目录结构，找出该场景的 sparse/0（要求包含全部机位的相机参数）
PRE=$N3V_ROOT/_pre
if [ ! -d "$PRE" ]; then
  mkdir -p "$PRE" && tar -xzf "$CACHE/n3v_mast3r.tar.gz" -C "$PRE"
fi
find "$PRE" -maxdepth 6 | sed "s|$PRE/||" | grep -i "$SCENE" | head -40 > "$OUT/preprocessed_layout.txt"
log "预处理包里与 $SCENE 相关的路径（前 40 条）：$(tr '\n' ' ' < "$OUT/preprocessed_layout.txt")"
"$PY" - "$PRE" "$SCENE" "$D" <<'PY' || die "预处理包里没有找到可用的 sparse/0（见 out/preprocessed_layout.txt），不训练，避免浪费 GPU"
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
    print(f"候选 {p.replace(pre, '')}: {n} 个相机, {pts}", file=sys.stderr)
best = [(n, p) for n, p in info if n >= 18 and any(f.startswith("points3D") for f in os.listdir(p))]
if not best:
    sys.exit(1)
n, p = best[0]
out = os.path.join(dst, "sparse", "0")
os.makedirs(out, exist_ok=True)
for f in os.listdir(p):
    shutil.copy(os.path.join(p, f), out)
print(f"使用 {p.replace(pre, '')}：{n} 个相机", file=sys.stderr)
PY

# 图片提取：命令与官方 n3v2blender.py 第 239 行相同（ffmpeg -i video -start_number 0 camXX_%04d.png），加 -frames:v 300
mkdir -p "$D/images"
if [ ! -f "$D/images/cam00_0299.png" ]; then
  ls "$D"/cam*.mp4 | xargs -P 8 -I{} sh -c 'v={}; c=$(basename "$v" .mp4); ffmpeg -loglevel error -y -i "$v" -frames:v 300 -start_number 0 "'"$D"'/images/${c}_%04d.png"'
fi
log "图片 $(ls "$D/images" | wc -l) 张（$(ls "$D/images" | sed 's/_.*//' | sort -u | wc -l) 个机位 × 300 帧）"
# 4C4D 只读 cam 名在 sparse/0 里的图片；图片数必须和相机数 × 帧数一致（数据读取器里有断言）
done_step

# ---------------------------------------------------------------- 3. 训练（官方 yaml）
step train
CFG=$N3V_ROOT/${SCENE}.yaml
"$PY" - "$SRC/configs/dynerf/$SCENE.yaml" "$CFG" "$D" "$MODEL" <<'PY'
import sys
from omegaconf import OmegaConf
c = OmegaConf.load(sys.argv[1])
c.ModelParams.source_path = sys.argv[3]   # 只改数据和输出路径，其余参数保持官方原样
c.ModelParams.model_path = sys.argv[4]
OmegaConf.save(c, sys.argv[2])
PY
cp "$CFG" "$OUT/config_used.yaml"
# 注意：grep 没匹配时返回非 0，配合 set -e / pipefail 会让整个任务中止，所以管道末尾加 || true，成败看 checkpoint
(cd "$SRC" && "$PY" train.py --config "$CFG" --training_view "$TRAIN" --output_dir run --res 2 2>&1 \
   | tr '\r' '\n' | grep -E 'ITER|Saving|complete|Error|error|Traceback' | tail -40) || true
CKPT=$MODEL/run/chkpnt_best.pth
[ -f "$CKPT" ] || die "训练没有产出 checkpoint"
vol_sync "$OUT/" "$ATLAS_RESULTS/$(basename "$RUN_DIR")/" && log "模型已同步到 Global volume"
done_step

# ---------------------------------------------------------------- 4. 官方运镜渲染（--traj arc）
step render_traj
(cd "$SRC" && "$PY" render.py --config "$CFG" --training_view "$TRAIN" --output_dir run --res 2 \
   --traj arc --start_checkpoint "$CKPT" 2>&1 | tail -3) || true
TRAJ=$(ls -d "$MODEL"/run/render/traj/ours_*/renders 2>/dev/null | tail -1)
if [ -n "$TRAJ" ]; then
  ffmpeg -loglevel error -y -framerate 30 -i "$TRAJ/%05d.png" -vf "scale=1280:-2" -c:v libx264 -crf 22 \
    -pix_fmt yuv420p "$OUT/videos/${SCENE}_4c4d_arc.mp4"
fi
done_step

# ---------------------------------------------------------------- 5. 官方评估（测试视角每 STRIDE 帧取 1 帧）
step eval
rm -rf "$E"; mkdir -p "$E/images"; ln -s "$D/sparse" "$E/sparse"
TRAIN_CAMS=$(echo "$TRAIN" | tr ',' '\n' | awk '{printf "cam%02d\n", $1}')
for f in "$D"/images/cam*_*.png; do
  b=$(basename "$f"); cam=${b%%_*}; fr=${b##*_}; fr=$((10#${fr%.png}))
  if echo "$TRAIN_CAMS" | grep -qx "$cam" || [ $((fr % STRIDE)) -eq 0 ]; then ln -s "$f" "$E/images/$b"; fi
done
log "评估集：$(ls "$E/images" | wc -l) 张（训练视角全部帧 + 测试视角每 $STRIDE 帧）"
# 4C4D 要求 sparse/0 里每个相机至少有一张图；测试视角各保留第 0 帧，满足要求
ECFG=$N3V_ROOT/${SCENE}_eval.yaml
sed "s|$D|$E|" "$CFG" > "$ECFG"
(cd "$SRC" && "$PY" render.py --config "$ECFG" --training_view "$TRAIN" --output_dir run --res 2 \
   --validate --start_checkpoint "$CKPT" 2>&1 | grep -E 'PSNR|LPIPS|Valid|Error|Traceback' | tail -8) || true
# 官方评估把结果写在 render/test/ours_<iter>/stats/validation.json
STATS=$(find "$MODEL/run/render" -name validation.json 2>/dev/null | head -1)
[ -n "$STATS" ] && cp "$STATS" "$OUT/official_validation.json"
done_step

# ---------------------------------------------------------------- 6. 汇总，并和论文表格对比
"$PY" - "$OUT" "$SCENE" "$STRIDE" "$MODEL" <<'PY'
import json, sys, os, glob, re
out, scene, stride, model = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
paper = {  # 论文 Table 6/7（4 个训练视角，N3V）
  "coffee_martini": (19.57, 0.140, 0.203), "cook_spinach": (24.44, 0.075, 0.111),
  "cut_roasted_beef": (23.22, 0.083, 0.124), "flame_salmon": (19.15, 0.136, 0.205),
  "flame_steak": (23.72, 0.075, 0.114), "sear_steak": (23.66, 0.078, 0.118)}
m = {"scene": scene, "eval_frame_stride": stride, "paper": dict(zip(("psnr", "dssim1", "lpips"), paper.get(scene, (None,)*3)))}
v = os.path.join(out, "official_validation.json")
if os.path.exists(v):
    s = json.load(open(v))
    # 官方评估输出的就是论文用的指标：psnr、Dssim1、Dssim2、lpips（AlexNet）
    m["ours"] = {k: (round(s[k], 4) if isinstance(s.get(k), float) else s.get(k))
                 for k in ("psnr", "Dssim1", "Dssim2", "lpips", "num_GS", "valid_images")}
    m["psnr_gap_db"] = round(m["ours"]["psnr"] - m["paper"]["psnr"], 2) if m["paper"]["psnr"] else None
# 训练末尾的官方自测（训练脚本在测试视角上每 100 张取 1 张）
log = open(os.path.join(os.path.dirname(out), "run.log"), errors="ignore").read()
tests = re.findall(r"\[ITER (\d+)\] Evaluating test: L1 [\d.]+ PSNR ([\d.]+)", log)
m["train_time_test_psnr"] = {it: round(float(p), 2) for it, p in tests}
json.dump(m, open(os.path.join(out, "metrics.json"), "w"), indent=2, ensure_ascii=False)
print(json.dumps(m, ensure_ascii=False))
PY
[ -f "$OUT/official_validation.json" ]
