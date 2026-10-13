#!/usr/bin/env bash
# GPU 护栏离线测试：用假的 runpodctl / ssh，不会碰真实 RunPod 账号。
# 用法: guard/tests/run_tests.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
G="python3 $HERE/../atlas_gpu.py"
PASS=0; FAIL=0
export FAKE_DIR=$(mktemp -d)
trap 'rm -rf "$FAKE_DIR"' EXIT
export RUNPODCTL="$HERE/fake_runpodctl"
export ATLAS_GPU_SSH="$HERE/fake_ssh"
export ATLAS_GPU_STATE="$FAKE_DIR/state" ATLAS_GPU_LOG="$FAKE_DIR/guard.log" ATLAS_GPU_CONFIG="$FAKE_DIR/none.json"

ago() { date -u -d "-$1 minutes" +%Y-%m-%dT%H:%M:%SZ; }
reset() {
  rm -rf "$FAKE_DIR"/{state,calls.log,guard.log,util,busy}
  echo '[]' > "$FAKE_DIR/pods.json"
  echo '[{"id":"vol1","name":"atlas_data","size":150,"dataCenterId":"US-KS-2"}]' > "$FAKE_DIR/nv.json"
  dc '"Low"' '"Low"'
  echo '[{"gpuId":"NVIDIA A100-SXM4-80GB","securePricePerHr":1.79},{"gpuId":"NVIDIA H100 80GB HBM3","securePricePerHr":3.99}]' > "$FAKE_DIR/gpu.json"
}
dc() {  # $1=A100 SXM 库存, $2=H100 SXM 库存
  echo "[{\"id\":\"US-KS-2\",\"gpuAvailability\":[{\"gpuId\":\"NVIDIA A100-SXM4-80GB\",\"stockStatus\":$1},{\"gpuId\":\"NVIDIA A100 80GB PCIe\",\"stockStatus\":null},{\"gpuId\":\"NVIDIA H100 80GB HBM3\",\"stockStatus\":$2},{\"gpuId\":\"NVIDIA GeForce RTX 4090\",\"stockStatus\":\"High\"}]}]" > "$FAKE_DIR/dc.json"
}
pods() { echo "$1" > "$FAKE_DIR/pods.json"; }
state() { mkdir -p "$FAKE_DIR/state"; echo "$2" > "$FAKE_DIR/state/$1.json"; }
check() {  # check 描述 期望(0/1) 命令...
  local desc=$1 want=$2; shift 2
  if eval "$@" >/dev/null 2>&1; then got=0; else got=1; fi
  if [ "$got" = "$want" ]; then PASS=$((PASS+1)); echo "  ✓ $desc"; else FAIL=$((FAIL+1)); echo "  ✗ $desc"; fi
}
called() { grep -q -- "$1" "$FAKE_DIR/calls.log" 2>/dev/null; }

OTHER='{"id":"other1","name":"eramoment_find_attendee_tool","desiredStatus":"RUNNING","costPerHr":0.5}'

echo "== gpu-up"
reset
check "dry-run 默认参数可以通过"            0 "$G up --dry-run"
check "dry-run 不会真的 create"             1 "called 'pod create'"
check "选中 A100 SXM 并挂 Volume"           0 "$G up --dry-run | grep -q 'NVIDIA A100-SXM4-80GB' && $G up --dry-run | grep -q -- '--network-volume-id vol1'"
check "--hours 7 被拒绝"                    1 "$G up --dry-run --hours 7"
check "--hours 0 被拒绝"                    1 "$G up --dry-run --hours 0"
check "--gpu rtx4090 被拒绝"                1 "$G up --dry-run --gpu rtx4090"
check "A100 有货时 --gpu h100 被拒绝"       1 "$G up --dry-run --gpu h100"
dc 'null' '"Low"'
check "A100 无货时 auto 回退到 H100"        0 "$G up --dry-run | grep -q 'H100 80GB HBM3'"
check "A100 无货时 --gpu a100 报错"         1 "$G up --dry-run --gpu a100"
dc 'null' 'null'
check "全部无货时报错"                       1 "$G up --dry-run"
reset
check "名字强制 atlas- 前缀"                0 "$G up --dry-run --name gen3c | grep -q 'atlas-gen3c'"
check "非法名字被拒绝"                       1 "$G up --dry-run --name 'a b;rm'"
pods "[{\"id\":\"p1\",\"name\":\"atlas-x\",\"desiredStatus\":\"RUNNING\"},$OTHER]"
check "已有 1 个 atlas- pod 运行时拒绝"      1 "$G up --dry-run"
pods "[$OTHER]"
check "别的项目的 pod 不占名额"              0 "$G up --dry-run"
echo '[]' > "$FAKE_DIR/nv.json"
check "没有 Volume 时真开 pod 被拒绝"        1 "$G up"
check "没有 Volume 时 dry-run 仍可预览"      0 "$G up --dry-run"
reset
check "真开（假 runpodctl）成功并输出 SSH"   0 "$G up --hours 2 --wait 0.1 | grep -q 'SSH      : .*fake_ssh'"
check "开完写入状态文件"                     0 "test -f $FAKE_DIR/state/fakepod123.json"

echo "== gpu-down"
reset
pods "[{\"id\":\"p1\",\"name\":\"atlas-x\",\"desiredStatus\":\"RUNNING\"},$OTHER]"
check "拒绝 stop 非 atlas- pod（按名字）"    1 "$G down eramoment_find_attendee_tool"
check "拒绝 stop 非 atlas- pod（按 ID）"     1 "$G down other1"
check "没有调用 stop other1"                 1 "called 'pod stop other1'"
check "可以 stop atlas- pod"                 0 "$G down atlas-x && called 'pod stop p1'"
check "--all --delete 只删 atlas-"           0 "$G down --all --delete && called 'pod delete p1' && ! called 'other1'"

echo "== gpu-status"
reset
pods "[{\"id\":\"p1\",\"name\":\"atlas-x\",\"desiredStatus\":\"RUNNING\",\"costPerHr\":1.79,\"lastStartedAt\":\"$(ago 90)\"},$OTHER]"
state p1 "{\"max_hours\":6,\"gpu\":\"NVIDIA A100-SXM4-80GB\"}"
check "显示 atlas- pod、不显示别的项目"       0 "$G status | grep -q atlas-x && ! $G status | grep -q eramoment"
check "时长 / 花费 / 剩余时间正确"           0 "$G status | grep -q '1h30m' && $G status | grep -q '\$2.6' && $G status | grep -qE '4h(29|30)m'"

echo "== gpu-guard"
reset
pods "[{\"id\":\"p1\",\"name\":\"atlas-x\",\"desiredStatus\":\"RUNNING\",\"lastStartedAt\":\"$(ago 370)\"},$OTHER]"
state p1 '{"max_hours":6}'
check "超过 6h → stop"                       0 "$G guard && called 'pod stop p1'"
check "绝不 stop 别的项目"                   1 "called other1"
reset
pods "[{\"id\":\"p1\",\"name\":\"atlas-x\",\"desiredStatus\":\"RUNNING\",\"lastStartedAt\":\"$(ago 100)\"}]"
state p1 '{"max_hours":1.5}'
check "超过自定义 1.5h → stop"               0 "$G guard && called 'pod stop p1'"
reset
pods "[{\"id\":\"p1\",\"name\":\"atlas-x\",\"desiredStatus\":\"RUNNING\",\"lastStartedAt\":\"$(ago 60)\"}]"
state p1 '{"max_hours":6,"idle_since":null}'
echo 0 > "$FAKE_DIR/util"
check "第一次 util=0 → 只开始计时"           0 "$G guard && ! called 'pod stop'"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 20)\"}"
check "空闲 20 分钟 → 不 stop"               0 "$G guard && ! called 'pod stop'"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 30)\"}"
check "空闲 30 分钟 → stop"                  0 "$G guard && called 'pod stop p1'"
reset
pods "[{\"id\":\"p1\",\"name\":\"atlas-x\",\"desiredStatus\":\"RUNNING\",\"lastStartedAt\":\"$(ago 60)\"}]"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 40)\"}"
echo 37 > "$FAKE_DIR/util"
check "util>0 → 清空空闲计时、不 stop"        0 "$G guard && ! called 'pod stop' && grep -q '\"idle_since\": null' $FAKE_DIR/state/p1.json"
echo 0 > "$FAKE_DIR/util"; echo 60 > "$FAKE_DIR/busy"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 40)\"}"
check "util=0 但 busy 标记新鲜 → 不 stop"    0 "$G guard && ! called 'pod stop'"
echo 3600 > "$FAKE_DIR/busy"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 40)\"}"
check "busy 标记过期 → stop"                 0 "$G guard && called 'pod stop p1'"
reset
pods "[{\"id\":\"p1\",\"name\":\"atlas-x\",\"desiredStatus\":\"RUNNING\",\"lastStartedAt\":\"$(ago 10)\"}]"
state p1 '{"max_hours":6}'
echo FAIL > "$FAKE_DIR/util"
check "开机 10 分钟 SSH 不通 → 宽限、不 stop" 0 "$G guard && ! called 'pod stop'"
pods "[{\"id\":\"p1\",\"name\":\"atlas-x\",\"desiredStatus\":\"RUNNING\",\"lastStartedAt\":\"$(ago 90)\"}]"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 30)\"}"
check "开机后 SSH 持续 30 分钟不通 → stop"   0 "$G guard && called 'pod stop p1'"
reset
pods "[{\"id\":\"p9\",\"name\":\"atlas-manual\",\"desiredStatus\":\"RUNNING\",\"lastStartedAt\":\"$(ago 400)\"}]"
check "没走 gpu-up 的 atlas- pod 也按 6h 管" 0 "$G guard && called 'pod stop p9'"
reset
pods "[{\"id\":\"p1\",\"name\":\"atlas-x\",\"desiredStatus\":\"RUNNING\",\"lastStartedAt\":\"$(ago 400)\"}]"
check "guard --dry-run 不真 stop"            0 "$G guard --dry-run && ! called 'pod stop' && grep -q dry-run $FAKE_DIR/guard.log"
reset
pods "[{\"id\":\"p1\",\"name\":\"atlas-x\",\"desiredStatus\":\"EXITED\",\"lastStartedAt\":\"$(ago 400)\"}]"
check "已停止的 pod 不重复 stop"              0 "$G guard && ! called 'pod stop'"

echo
echo "通过 $PASS，失败 $FAIL"
[ "$FAIL" = 0 ]
