#!/usr/bin/env bash
# 任务执行器 + pod 端脚本的本机测试（不连 pod、不需要 GPU）：
#   atlas-run 全流程、Global volume 同步、拉回大小限制、失败任务、pre_stop、环境打包 / 恢复。
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
check "hello 任务成功"                       0 "$R --input $T/input --name t1 hello a b"
ID=$(ls "$T/runs" | grep t1)
check "输入文件推送到 pod"                   0 "test -f $T/pod/work/$ID/task/task.json"
check "预览和指标拉回服务器"                 0 "test -f $T/runs/$ID/out/preview/hello.txt && grep -q '\"seconds\"' $T/runs/$ID/out/metrics.json"
check "30MB 大文件不拉回服务器"              1 "test -f $T/runs/$ID/out/big.bin"
check "完整结果同步到 Global volume"         0 "test -f $T/vol/results/$ID/big.bin && test -f $T/vol/results/$ID/metrics.json"
check "计算出 GPU 花费"                      0 "grep -q gpu_cost_usd $T/runs/$ID/run.json"
check "busy 标记被 touch"                    0 "test -f $T/pod/.atlas_busy"
check "pre_stop 已安装"                      0 "test -x $T/pod/bin/pre_stop"
check "失败任务返回非 0"                      1 "$R --name t2 hello fail"
ID2=$(ls "$T/runs" | grep t2)
check "失败状态写入 status"                  0 "grep -q 'failed 3' $T/pod/work/$ID2/status"
check "不存在的任务被拒绝"                   1 "$R nosuchtask"
check "任务名不能含路径"                      1 "$R ../x"
check "--list 能列出"                        0 "$R --list | grep -q t1"

echo "== pre_stop"
mkdir -p "$T/pod/work/run-x/out"; echo running > "$T/pod/work/run-x/status"; echo partial > "$T/pod/work/run-x/out/ckpt.txt"
check "pre_stop 同步未完成的 run"            0 "$T/pod/bin/pre_stop && test -f $T/vol/results/run-x/ckpt.txt && test -f $T/vol/results/run-x/INTERRUPTED"

echo "== 环境打包 / 恢复（假 conda-pack）"
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
check "env_pack 生成 envpacks/demo-k1.tar.zst" 0 "bash -c '$L; env_pack demo k1' && test -f $T/vol/envpacks/demo-k1.tar.zst"
rm -rf "$T/pod/envs/demo"
check "env_restore 从包恢复并运行 conda-unpack" 0 "bash -c '$L; env_restore demo k1' && test -x $T/pod/envs/demo/bin/python && test -f $T/pod/envs/demo/UNPACKED"
check "同一 key 再次 restore 直接跳过"        0 "bash -c '$L; env_restore demo k1' 2>&1 | grep -q 已在容器盘"
check "没有对应 key 的包时返回 1"            1 "bash -c '$L; env_restore demo k2'"
check "新 key 打包后删除旧包"                0 "bash -c '$L; env_pack demo k2' && test -f $T/vol/envpacks/demo-k2.tar.zst && ! test -f $T/vol/envpacks/demo-k1.tar.zst"
check "env_key 随脚本内容变化"               0 "a=\$(bash -c '$L; env_key $REPO/pod/setup_gen3c.sh'); b=\$(bash -c '$L; env_key $REPO/pod/setup_vipe.sh'); [ \"\$a\" != \"\$b\" ]"

echo; echo "通过 $PASS，失败 $FAIL"; [ $FAIL = 0 ]
