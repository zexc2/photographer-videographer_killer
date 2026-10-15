#!/usr/bin/env bash
# Task entry point for installing / restoring envs: setup.sh <run dir> gen3c vipe 4c4d ...
RUN_DIR=$1; shift
HERE=$(cd "$(dirname "$0")/.." && pwd)
rc=0
for m in "$@"; do
  bash "$HERE/setup_$m.sh" || { echo "setup_$m failed" >&2; rc=1; }
  cp -f /opt/atlas/logs/setup-$m.json "$RUN_DIR/out/setup-$m.json" 2>/dev/null || true
done
exit $rc
