#!/usr/bin/env bash
# export_scene.sh / render_traj.sh 共用：只恢复 4c4d 环境（不需要 mast3r / vipe），把模型从 Global volume 复制到容器盘。
# 调用前需设置 MODEL_KEY；之后可用 $CKPT、$CFG、$PY、$SRC4。
source "$(dirname "$0")/../lib/common.sh"
HERE=$(cd "$(dirname "$0")/.." && pwd)
SRC4=$ATLAS_SRC/4C4D
PY=$ATLAS_ENVS/4c4d/bin/python
read -r CKPT_REL CFG_REL SPARSE_TAR SPARSE_MEMBER FALLBACK PREPARE <<<"$(python3 -c "
import json; m=json.load(open('$HERE/configs/models.json'))['$MODEL_KEY']
print(m['checkpoint'], m['config'], m['sparse_tar'], m['sparse_member'], m.get('fallback_glob', '-'), m.get('prepare', '-').replace(' ', '|'))")"
[ -n "$CKPT_REL" ] || die "configs/models.json 里没有模型 $MODEL_KEY"
if [ ! -f "$ATLAS_VOL/$CKPT_REL" ] && [ "$FALLBACK" != "-" ]; then
  # 固定位置还没有模型：用最近一次训练结果（结果目录名以时间开头，按名字排序即按时间）
  # shellcheck disable=SC2086
  LATEST=$(ls -1 $ATLAS_VOL/$FALLBACK 2>/dev/null | sort | tail -1 || true)
  if [ -n "$LATEST" ]; then
    CKPT_REL=${LATEST#"$ATLAS_VOL"/}
    CFG_REL=${CKPT_REL%/full/model/run/chkpnt_best.pth}/config_used.yaml
    log "固定位置没有模型，使用最近一次训练结果 $CKPT_REL"
  fi
fi
[ -f "$ATLAS_VOL/$CKPT_REL" ] || die "Global volume 上还没有模型 $MODEL_KEY。先在服务器上运行一次：${PREPARE//|/ }（约 4 小时，见 DEPLOY.md）"
[ -f "$ATLAS_VOL/$SPARSE_TAR" ] || die "Global volume 上没有 $SPARSE_TAR。先运行：${PREPARE//|/ }"
FOURC_REF=ed6a3cb69782
repo_ensure https://github.com/yangzf-1023/4C4D.git 4C4D "$FOURC_REF" >/dev/null
env_restore 4c4d "$(env_key "$HERE/setup_4c4d.sh")" || die "Global volume 上没有 4c4d 环境包。先运行：${PREPARE//|/ }（会安装并打包环境）"
apt_ensure libegl1 libgl1 libgomp1
"$PY" -c "import pkg_resources" 2>/dev/null || "$PY" -m pip install -q "setuptools<80"
M=$ATLAS_CACHE/models/$MODEL_KEY
mkdir -p "$M"
[ -f "$M/model.pth" ] || cp "$ATLAS_VOL/$CKPT_REL" "$M/model.pth"
cp "$ATLAS_VOL/$CFG_REL" "$M/config.yaml"
CKPT=$M/model.pth
CFG=$M/config.yaml
log "模型 $MODEL_KEY 已就绪（$(du -h "$CKPT" | cut -f1)）"
