#!/usr/bin/env bash
# Shared by export_scene.sh / render_traj.sh: restores only the 4c4d env (no mast3r / vipe) and copies the model from Global volume to the container disk.
# Set MODEL_KEY before sourcing; afterwards $CKPT, $CFG, $PY and $SRC4 are available.
source "$(dirname "$0")/../lib/common.sh"
HERE=$(cd "$(dirname "$0")/.." && pwd)
SRC4=$ATLAS_SRC/4C4D
PY=$ATLAS_ENVS/4c4d/bin/python
read -r CKPT_REL CFG_REL SPARSE_TAR SPARSE_MEMBER FALLBACK PREPARE <<<"$(python3 -c "
import json; m=json.load(open('$HERE/configs/models.json'))['$MODEL_KEY']
print(m['checkpoint'], m['config'], m['sparse_tar'], m['sparse_member'], m.get('fallback_glob', '-'), m.get('prepare', '-').replace(' ', '|'))")"
[ -n "$CKPT_REL" ] || die "model $MODEL_KEY not found in configs/models.json"
if [ ! -f "$ATLAS_VOL/$CKPT_REL" ] && [ "$FALLBACK" != "-" ]; then
  # No model at the fixed location yet: use the latest training result (result dir names start with a timestamp, so name order = time order)
  # shellcheck disable=SC2086
  LATEST=$(ls -1 $ATLAS_VOL/$FALLBACK 2>/dev/null | sort | tail -1 || true)
  if [ -n "$LATEST" ]; then
    CKPT_REL=${LATEST#"$ATLAS_VOL"/}
    CFG_REL=${CKPT_REL%/full/model/run/chkpnt_best.pth}/config_used.yaml
    log "No model at the fixed location; using the latest training result $CKPT_REL"
  fi
fi
[ -f "$ATLAS_VOL/$CKPT_REL" ] || die "Model $MODEL_KEY is not on Global volume yet. Run this once on the server first: ${PREPARE//|/ } (about 4 hours, see DEPLOY.md)"
[ -f "$ATLAS_VOL/$SPARSE_TAR" ] || die "$SPARSE_TAR is not on Global volume. Run first: ${PREPARE//|/ }"
FOURC_REF=ed6a3cb69782
repo_ensure https://github.com/yangzf-1023/4C4D.git 4C4D "$FOURC_REF" >/dev/null
env_restore 4c4d "$(env_key "$HERE/setup_4c4d.sh")" || die "No 4c4d env pack on Global volume. Run first: ${PREPARE//|/ } (installs and packs the env)"
apt_ensure libegl1 libgl1 libgomp1
"$PY" -c "import pkg_resources" 2>/dev/null || "$PY" -m pip install -q "setuptools<80"
M=$ATLAS_CACHE/models/$MODEL_KEY
mkdir -p "$M"
[ -f "$M/model.pth" ] || cp "$ATLAS_VOL/$CKPT_REL" "$M/model.pth"
cp "$ATLAS_VOL/$CFG_REL" "$M/config.yaml"
CKPT=$M/model.pth
CFG=$M/config.yaml
log "Model $MODEL_KEY ready ($(du -h "$CKPT" | cut -f1))"
