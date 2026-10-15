#!/usr/bin/env bash
# Local tests for the task runner + pod-side scripts (no pod connection, no GPU needed):
#   full atlas-run flow, Global volume sync, pull size limit, failed runs, pre_stop, env pack / restore.
set -u
HERE=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$HERE/../.." && pwd)
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
export ATLAS_RUN_LOCAL=1 ATLAS_POD_ROOT=$T/pod ATLAS_LOCAL=$T/pod ATLAS_VOL=$T/vol ATLAS_RUNS=$T/runs ATLAS_RUN_POLL=1
mkdir -p "$T/vol" "$T/input"; echo cfg > "$T/input/task.json"
PASS=0; FAIL=0
check() { local d=$1 w=$2; shift 2; if eval "$@" >/dev/null 2>&1; then g=0; else g=1; fi
  if [ $g = $w ]; then PASS=$((PASS+1)); echo "  ✓ $d"; else FAIL=$((FAIL+1)); echo "  ✗ $d"; fi; }
R="python3 $REPO/runner/atlas_run.py"

echo "== atlas-run"
check "hello task succeeds"                       0 "$R --input $T/input --name t1 hello a b"
ID=$(ls "$T/runs" | grep t1)
check "input files pushed to pod"                   0 "test -f $T/pod/work/$ID/task/task.json"
check "previews and metrics pulled to server"                 0 "test -f $T/runs/$ID/out/preview/hello.txt && grep -q '\"seconds\"' $T/runs/$ID/out/metrics.json"
check "30MB file not pulled to server"              1 "test -f $T/runs/$ID/out/big.bin"
check "full results synced to Global volume"         0 "test -f $T/vol/results/$ID/big.bin && test -f $T/vol/results/$ID/metrics.json"
check "GPU cost computed"                      0 "grep -q gpu_cost_usd $T/runs/$ID/run.json"
check "busy marker touched"                    0 "test -f $T/pod/.atlas_busy"
check "pre_stop installed"                      0 "test -x $T/pod/bin/pre_stop"
check "failed task returns non-zero"                      1 "$R --name t2 hello fail"
ID2=$(ls "$T/runs" | grep t2)
check "failed state written to status"                  0 "grep -q 'failed 3' $T/pod/work/$ID2/status"
check "nonexistent task rejected"                   1 "$R nosuchtask"
check "task name cannot contain a path"                      1 "$R ../x"
check "--list lists runs"                        0 "$R --list | grep -q t1"

echo "== pre_stop"
mkdir -p "$T/pod/work/run-x/out"; echo running > "$T/pod/work/run-x/status"; echo partial > "$T/pod/work/run-x/out/ckpt.txt"
check "pre_stop syncs unfinished run"            0 "$T/pod/bin/pre_stop && test -f $T/vol/results/run-x/ckpt.txt && test -f $T/vol/results/run-x/INTERRUPTED"

echo "== env pack / restore (fake conda-pack)"
mkdir -p "$T/pod/miniforge/bin" "$T/pod/envs/demo/bin"
cat > "$T/pod/miniforge/bin/conda-pack" <<'X'
#!/usr/bin/env bash
while [ $# -gt 0 ]; do case $1 in -p) P=$2; shift;; -o) O=$2; shift;; esac; shift; done
tar -cf "$O" -C "$P" .
X
chmod +x "$T/pod/miniforge/bin/conda-pack"
echo '#!/bin/sh' > "$T/pod/envs/demo/bin/python"; echo 'touch "$(dirname "$0")/../UNPACKED"' > "$T/pod/envs/demo/bin/conda-unpack"
chmod +x "$T/pod/envs/demo/bin/"*
L="source $REPO/pod/lib/common.sh"
check "env_pack creates envpacks/demo-k1.tar.zst" 0 "bash -c '$L; env_pack demo k1' && test -f $T/vol/envpacks/demo-k1.tar.zst"
rm -rf "$T/pod/envs/demo"
check "env_restore restores from pack and runs conda-unpack" 0 "bash -c '$L; env_restore demo k1' && test -x $T/pod/envs/demo/bin/python && test -f $T/pod/envs/demo/UNPACKED"
check "restore with same key again is skipped"        0 "bash -c '$L; env_restore demo k1' 2>&1 | grep -q 'already on container disk'"
check "returns 1 when no pack for the key"            1 "bash -c '$L; env_restore demo k2'"
check "packing a new key deletes the old pack"                0 "bash -c '$L; env_pack demo k2' && test -f $T/vol/envpacks/demo-k2.tar.zst && ! test -f $T/vol/envpacks/demo-k1.tar.zst"
check "env_key changes with script content"               0 "a=\$(bash -c '$L; env_key $REPO/pod/setup_gen3c.sh'); b=\$(bash -c '$L; env_key $REPO/pod/setup_vipe.sh'); [ \"\$a\" != \"\$b\" ]"

echo; echo "Passed $PASS, failed $FAIL"; [ $FAIL = 0 ]
