#!/usr/bin/env bash
# GPU 护栏离线测试：假的 RunPod API v2 + 假的 runpodctl / ssh，不会碰真实账号。
# 用法: guard/tests/run_tests.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
G="python3 $HERE/../atlas_gpu.py"
PASS=0; FAIL=0
export FAKE_DIR=$(mktemp -d)
PORT=$((20000 + RANDOM % 20000))
python3 "$HERE/fake_api.py" $PORT & API_PID=$!
trap 'kill $API_PID 2>/dev/null; rm -rf "$FAKE_DIR"' EXIT
export ATLAS_RUNPOD_API="http://127.0.0.1:$PORT"
export RUNPODCTL="$HERE/fake_runpodctl" ATLAS_GPU_SSH="$HERE/fake_ssh"
export ATLAS_GPU_STATE="$FAKE_DIR/state" ATLAS_GPU_LOG="$FAKE_DIR/guard.log" ATLAS_GPU_CONFIG="$FAKE_DIR/config.json"
export ATLAS_GPU_BALANCE_LOG="$FAKE_DIR/balance.log" ATLAS_GPU_KEYFILE="$FAKE_DIR/key" ATLAS_GPU_SSH_KEY="$FAKE_DIR/sshkey"
for i in $(seq 50); do curl -s -o /dev/null "$ATLAS_RUNPOD_API/" && break; sleep 0.1; done

ago() { date -u -d "-$1 minutes" +%Y-%m-%dT%H:%M:%SZ; }
key_on()  { printf 'testkey\n' > "$FAKE_DIR/key"; chmod 600 "$FAKE_DIR/key"; }
key_off() { rm -f "$FAKE_DIR/key"; }
reset() {
  rm -rf "$FAKE_DIR"/{state,calls.log,guard.log,util,busy,api_fail,create_fail,api_created.json,user_fail,runs_status}
  key_on; touch "$FAKE_DIR/sshkey"; echo 'ssh-ed25519 AAAAtest atlas' > "$FAKE_DIR/sshkey.pub"
  echo '[]' > "$FAKE_DIR/api_pods.json"; echo '[]' > "$FAKE_DIR/pods.json"
  echo '[{"id":"vol_test123","name":"photographer/videographer_killer"},{"id":"vol_other99","name":"atlas_data"}]' > "$FAKE_DIR/api_gv.json"
  echo '{"global_volume_id":"vol_test123","global_volume_label":"photographer/videographer_killer"}' > "$FAKE_DIR/config.json"
  echo '{"clientBalance": 50.0}' > "$FAKE_DIR/user.json"
  cat > "$FAKE_DIR/api_catalog.json" <<J
{"NVIDIA A100-SXM4-80GB":{"availability":"LOW","price":1.79},"NVIDIA A100 80GB PCIe":{"availability":"NONE","price":1.64},
 "NVIDIA H100 80GB HBM3":{"availability":"LOW","price":3.99},"NVIDIA H100 PCIe":{"availability":"NONE","price":2.89},
 "NVIDIA H100 NVL":{"availability":"NONE","price":3.29}}
J
  # runpodctl 兜底用的数据
  echo '[{"id":"US-KS-2","gpuAvailability":[{"gpuId":"NVIDIA A100-SXM4-80GB","stockStatus":"Low"}]}]' > "$FAKE_DIR/dc.json"
  echo '[{"gpuId":"NVIDIA A100-SXM4-80GB","securePricePerHr":1.79}]' > "$FAKE_DIR/gpu.json"
  echo '{"keys":[{"key":"ssh-ed25519 AAAAjoey joey"}]}' > "$FAKE_DIR/keys.json"
}
avail() { python3 -c "import json,sys;p=sys.argv[1];d=json.load(open(p));d[sys.argv[2]]['availability']=sys.argv[3];json.dump(d,open(p,'w'))" "$FAKE_DIR/api_catalog.json" "$1" "$2"; }
apipods() { echo "$1" > "$FAKE_DIR/api_pods.json"; }
ctlpods() { echo "$1" > "$FAKE_DIR/pods.json"; }
state() { mkdir -p "$FAKE_DIR/state"; echo "$2" > "$FAKE_DIR/state/$1.json"; }
pod() {  # pod id name status 开机分钟前 [util]
  local u=${5:-null}; [ "$u" = null ] && rt=null || rt="{\"gpus\":[{\"util\":$u}]}"
  echo "{\"id\":\"$1\",\"name\":\"$2\",\"status\":\"$3\",\"startedAt\":\"$(ago $4)\",\"cost\":1.79,\"gpu\":{\"id\":\"NVIDIA A100-SXM4-80GB\"},\"runtime\":$rt,\"ssh\":{\"direct\":{\"command\":\"ssh root@pod.example.invalid -p 1\"}}}"
}
check() {  # check 描述 期望(0/1) 命令...
  local desc=$1 want=$2; shift 2
  if eval "$@" >/dev/null 2>&1; then got=0; else got=1; fi
  if [ "$got" = "$want" ]; then PASS=$((PASS+1)); echo "  ✓ $desc"; else FAIL=$((FAIL+1)); echo "  ✗ $desc"; fi
}
called() { grep -q -- "$1" "$FAKE_DIR/calls.log" 2>/dev/null; }
OTHER=$(pod other1 eramoment_find_attendee_tool RUNNING 600 0)

echo "== gpu-up"
reset
check "dry-run 默认参数可以通过"                 0 "$G up --dry-run"
check "dry-run 不会真的创建"                     1 "called 'API POST /v2/pods '"
check "选 A100 SXM、挂 Global volume、不锁区域"  0 "$G up --dry-run | grep -q 'vol_test123' && $G up --dry-run | grep -q '\"path\": \"/workspace\"' && ! $G up --dry-run | grep -q dataCenterIds"
check "挂载路径来自 ATLAS_VOL_MOUNT 变量"         0 "ATLAS_VOL_MOUNT=/workspace-global $G up --dry-run | grep -q '\"ATLAS_VOL\": \"/workspace-global\"'"
check "容器盘默认 200GB"                          0 "$G up --dry-run | grep -q '\"disk\": 200'"
check "--hours 7 被拒绝"                         1 "$G up --dry-run --hours 7"
check "--hours 0 被拒绝"                         1 "$G up --dry-run --hours 0"
check "--gpu rtx4090 被拒绝"                     1 "$G up --dry-run --gpu rtx4090"
check "A100 有货时 --gpu h100 被拒绝"            1 "$G up --dry-run --gpu h100"
avail "NVIDIA A100-SXM4-80GB" NONE
check "A100 全区域无货时 auto 回退到 H100"       0 "$G up --dry-run | grep -q '\"id\": \"NVIDIA H100 80GB HBM3\"'"
check "A100 无货时 --gpu a100 报错"              1 "$G up --dry-run --gpu a100"
avail "NVIDIA H100 80GB HBM3" NONE
check "全部无货时报错"                            1 "$G up --dry-run"
reset
check "名字强制 atlas- 前缀"                     0 "$G up --dry-run --name gen3c | grep -q 'atlas-gen3c'"
check "非法名字被拒绝"                            1 "$G up --dry-run --name 'a b;rm'"
apipods "[$(pod p1 atlas-x RUNNING 10 50),$OTHER]"
check "已有 1 个 atlas- pod 运行时拒绝"           1 "$G up --dry-run"
apipods "[$(pod p1 atlas-x PROVISIONING 1),$OTHER]"
check "PROVISIONING 的 pod 也占名额"             1 "$G up --dry-run"
apipods "[$OTHER]"
check "别的项目的 pod 不占名额"                   0 "$G up --dry-run"
echo '[]' > "$FAKE_DIR/api_gv.json"
check "API 里找不到配置的 volume ID 时拒绝"      1 "$G up"
reset
check "按 ID 识别 volume（名字含斜杠也没问题）"  0 "$G up --dry-run | grep -q 'vol_test123（v2 API 已确认）' && $G up --dry-run | grep -q 'photographer/videographer_killer'"
check "不按名字匹配（同名 atlas_data 的另一个 volume 不会被用）" 1 "$G up --dry-run | grep -q vol_other99"
echo '{}' > "$FAKE_DIR/config.json"
check "没有配置 volume ID 时拒绝（不按名字猜）"   1 "$G up --dry-run"
echo '{"global_volume_id":"bad/id"}' > "$FAKE_DIR/config.json"
check "非法 volume ID 被拒绝"                    1 "$G up --dry-run"
echo '== 余额检查'
reset; echo '{"clientBalance": 2.99}' > "$FAKE_DIR/user.json"
check "余额 < \$3 时拒绝开 pod"                 1 "$G up --wait 0"
check "余额不足时没有创建请求"                   1 "called 'API POST /v2/pods '"
check "余额 < \$3 时 dry-run 仍可预览并提示"    0 "$G up --dry-run | grep -q '请先充值'"
echo '{"clientBalance": 3}' > "$FAKE_DIR/user.json"
check "余额 = \$3 可以开"                       0 "$G up --wait 0"
reset; echo '{"clientBalance": 12.97}' > "$FAKE_DIR/user.json"
check "余额 \$12.97（原 \$15 下限以下）现在可以开" 0 "$G up --wait 0"
reset; touch "$FAKE_DIR/user_fail"
check "读不到余额时拒绝开 pod"                   1 "$G up --wait 0"
reset; echo '{"clientBalance": 10}' > "$FAKE_DIR/user.json"
check "跑满后余额会低于 \$3 时给出提醒"          0 "$G up --dry-run --hours 6 | grep -q '建议再充值'"
check "跑满后仍高于 \$3 时不提醒"                1 "$G up --dry-run --hours 1 | grep -q '建议再充值'"
reset; echo '{"clientBalance": 20}' > "$FAKE_DIR/user.json"
apipods "[$(pod p1 atlas-x RUNNING 10 50)]"
check "gpu-status 显示余额"                       0 "$G status | grep -q '账户余额: \$20.00'"
reset; key_off
check "没有 API key 时真开 pod 被拒绝"           1 "$G up"
check "没有 API key 时 dry-run 用 runpodctl 预览" 0 "$G up --dry-run | grep -q '未经 API 确认'"
reset; chmod 644 "$FAKE_DIR/key"
check "key 文件权限过宽时拒绝"                    1 "$G up"
reset
check "真开（假 API）成功并输出 SSH"              0 "$G up --hours 2 --wait 0 | grep -q 'SSH      : ssh root@pod.example.invalid'"
check "开完写入状态文件"                          0 "test -f $FAKE_DIR/state/fakepod123.json"
check "PUBLIC_KEY 含服务器 key 和账户 key"        0 "grep 'API POST /v2/pods ' $FAKE_DIR/calls.log | grep -q 'AAAAtest' && grep 'API POST /v2/pods ' $FAKE_DIR/calls.log | grep -q 'AAAAjoey'"
reset; echo 'NVIDIA A100-SXM4-80GB' > "$FAKE_DIR/create_fail"
check "A100 抢不到卡时自动改开 H100"              0 "$G up --wait 0 && grep 'API POST /v2/pods ' $FAKE_DIR/calls.log | tail -1 | grep -q 'H100 80GB HBM3'"

echo "== gpu-down"
reset
apipods "[$(pod p1 atlas-x RUNNING 10 50),$OTHER]"
check "拒绝 stop 非 atlas- pod（按名字）"         1 "$G down eramoment_find_attendee_tool"
check "拒绝 stop 非 atlas- pod（按 ID）"          1 "$G down other1"
check "没有对 other1 发过任何写操作"              1 "grep -E 'POST|DELETE' $FAKE_DIR/calls.log | grep -q other1"
check "可以 stop atlas- pod（走 API）"            0 "$G down atlas-x && called 'API POST /v2/pods/p1/action {\"action\": \"stop\"}'"
check "--all --delete 只删 atlas-"                0 "$G down --all --delete && called 'API DELETE /v2/pods/p1' && ! grep -E 'POST|DELETE' $FAKE_DIR/calls.log | grep -q other1"
reset; touch "$FAKE_DIR/api_fail"
ctlpods '[{"id":"p1","name":"atlas-x","desiredStatus":"RUNNING"},{"id":"other1","name":"eramoment_find_attendee_tool","desiredStatus":"RUNNING"}]'
check "API 故障时退回 runpodctl stop"             0 "$G down atlas-x && called 'pod stop p1'"

echo "== gpu-status"
reset
apipods "[$(pod p1 atlas-x RUNNING 90 42),$OTHER]"
state p1 '{"max_hours":6}'
check "显示 atlas- pod、不显示别的项目"            0 "$G status | grep -q atlas-x && ! $G status | grep -q eramoment"
check "时长 / 花费 / 剩余 / 利用率正确"           0 "$G status | grep -q '1h30m' && $G status | grep -q '\$2.6' && $G status | grep -qE '4h(29|30)m' && $G status | grep -q ' 42 '"

echo "== gpu-guard"
reset
apipods "[$(pod p1 atlas-x RUNNING 370 80),$OTHER]"
state p1 '{"max_hours":6}'
check "超过 6h → stop（即使 GPU 在忙）"           0 "$G guard && called 'API POST /v2/pods/p1/action'"
check "绝不 stop 别的项目"                         1 "grep -E 'POST|DELETE' $FAKE_DIR/calls.log | grep -q other1"
check "stop 前执行了 pre_stop 同步"               0 "grep -q 'PRE_STOP p1' $FAKE_DIR/guard.log"
reset
apipods "[$(pod p1 atlas-x RUNNING 100 80)]"
state p1 '{"max_hours":1.5}'
check "超过自定义 1.5h → stop"                     0 "$G guard && called 'API POST /v2/pods/p1/action'"
reset
apipods "[$(pod p1 atlas-x RUNNING 60 0)]"
state p1 '{"max_hours":6,"idle_since":null}'
echo 0 > "$FAKE_DIR/util"
check "第一次 util=0 → 只开始计时"                 0 "$G guard && ! called '/action'"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 20)\"}"
check "空闲 20 分钟 → 不 stop"                     0 "$G guard && ! called '/action'"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 30)\"}"
check "空闲 30 分钟 → stop"                        0 "$G guard && called 'API POST /v2/pods/p1/action'"
reset
apipods "[$(pod p1 atlas-x RUNNING 60 37)]"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 40)\"}"
echo FAIL > "$FAKE_DIR/util"
check "API util>0 → 不需要 SSH，清空空闲计时"     0 "$G guard && ! called '/action' && grep -q '\"idle_since\": null' $FAKE_DIR/state/p1.json"
reset
apipods "[$(pod p1 atlas-x RUNNING 60 0)]"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 40)\"}"
echo 55 > "$FAKE_DIR/util"
check "API 说 0 但 nvidia-smi 复核 >0 → 不 stop"   0 "$G guard && ! called '/action'"
echo 0 > "$FAKE_DIR/util"; echo 60 > "$FAKE_DIR/busy"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 40)\"}"
check "util=0 但 busy 标记新鲜 → 不 stop"         0 "$G guard && ! called '/action'"
echo 3600 > "$FAKE_DIR/busy"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 40)\"}"
check "busy 标记过期 → stop"                       0 "$G guard && called 'API POST /v2/pods/p1/action'"
reset
apipods "[$(pod p1 atlas-x RUNNING 10)]"
state p1 '{"max_hours":6}'
echo FAIL > "$FAKE_DIR/util"
check "开机 10 分钟没有利用率、SSH 不通 → 宽限"   0 "$G guard && ! called '/action'"
apipods "[$(pod p1 atlas-x RUNNING 90)]"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 30)\"}"
check "开机后持续 30 分钟探测不到 → stop"         0 "$G guard && called 'API POST /v2/pods/p1/action'"
reset
nossh="{\"id\":\"p1\",\"name\":\"atlas-x\",\"status\":\"RUNNING\",\"startedAt\":\"$(ago 60)\",\"cost\":1.79,\"runtime\":{\"gpus\":[{\"util\":0}]}}"
apipods "[$nossh]"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 40)\",\"ssh_cmd\":\"ssh root@pod.example.invalid -p 1\"}"
echo 0 > "$FAKE_DIR/util"; echo 30 > "$FAKE_DIR/busy"
check "API 缺 SSH 信息时用缓存的 SSH 命令，busy 仍生效" 0 "$G guard && ! called '/action' && grep -q '\"idle_since\": null' $FAKE_DIR/state/p1.json"
reset
apipods "[$(pod p9 atlas-manual RUNNING 400 90)]"
check "没走 gpu-up 的 atlas- pod 也按 6h 管"      0 "$G guard && called 'API POST /v2/pods/p9/action'"
reset
apipods "[$(pod p1 atlas-x RUNNING 400 90)]"
check "guard --dry-run 不真 stop"                  0 "$G guard --dry-run && ! called '/action' && grep -q dry-run $FAKE_DIR/guard.log"
reset
apipods "[$(pod p1 atlas-x EXITED 400)]"
check "已停止的 pod 不重复 stop"                   0 "$G guard && ! called '/action'"
reset; touch "$FAKE_DIR/api_fail"
ctlpods "[{\"id\":\"p1\",\"name\":\"atlas-x\",\"desiredStatus\":\"RUNNING\",\"lastStartedAt\":\"$(ago 400)\"},{\"id\":\"other1\",\"name\":\"eramoment_find_attendee_tool\",\"desiredStatus\":\"RUNNING\",\"lastStartedAt\":\"$(ago 999)\"}]"
check "API 故障时巡检退回 runpodctl，超时照样 stop" 0 "$G guard && called 'pod stop p1' && ! called 'pod stop other1'"

echo "== 任务结束自动关机 / 空闲阈值"
reset
apipods "[$(pod p1 atlas-x RUNNING 60 80)]"
state p1 '{"max_hours":6,"stop_after_runs":["r1"]}'
echo "r1 running 0" > "$FAKE_DIR/runs_status"
check "登记的任务还在跑 → 不关"                    0 "$G guard && ! called '/action'"
echo "r1 done 600" > "$FAKE_DIR/runs_status"
check "登记的任务结束 10 分钟 → 自动关"            0 "$G guard && called 'API POST /v2/pods/p1/action' && grep -q '已全部结束' $FAKE_DIR/guard.log"
check "自动关之前执行了 pre_stop"                  0 "grep -q 'PRE_STOP p1' $FAKE_DIR/guard.log"
reset
apipods "[$(pod p1 atlas-x RUNNING 60 80)]"
state p1 '{"max_hours":6,"stop_after_runs":["r1"]}'
echo "r1 done 60" > "$FAKE_DIR/runs_status"
check "任务刚结束 1 分钟（宽限期内）→ 不关"         0 "$G guard && ! called '/action'"
state p1 '{"max_hours":6,"stop_after_runs":["r1","r2"]}'
printf 'r1 done 900\nr2 running 0\n' > "$FAKE_DIR/runs_status"
check "一个结束、一个还在跑 → 不关"                0 "$G guard && ! called '/action'"
printf 'r1 done 900\nr2 failed 700\n' > "$FAKE_DIR/runs_status"
check "失败的任务也算结束 → 关"                    0 "$G guard && called 'API POST /v2/pods/p1/action'"
reset
apipods "[$(pod p1 atlas-x RUNNING 60 80)]"
state p1 '{"max_hours":6,"stop_after_runs":[]}'
echo "r1 done 9999" > "$FAKE_DIR/runs_status"
check "没有登记任务（--keep-alive）→ 不按任务关"    0 "$G guard && ! called '/action'"
reset
apipods "[$(pod p1 atlas-x RUNNING 60 2)]"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 30)\"}"
echo 2 > "$FAKE_DIR/util"
check "利用率 2%（低于 5%）连续 30 分钟 → 关"       0 "$G guard && called 'API POST /v2/pods/p1/action'"
reset
apipods "[$(pod p1 atlas-x RUNNING 60 2)]"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 30)\"}"
echo 2 > "$FAKE_DIR/util"; echo 30 > "$FAKE_DIR/busy"
check "利用率 2% 但 busy 新鲜 → 不关"              0 "$G guard && ! called '/action'"
reset
apipods "[$(pod p1 atlas-x RUNNING 60 6)]"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 40)\"}"
check "利用率 6% → 算忙、清空空闲计时"             0 "$G guard && ! called '/action' && grep -q '\"idle_since\": null' $FAKE_DIR/state/p1.json"

reset
apipods "[]"
check "巡检记录余额到 balance.log"                0 "$G guard && grep -q '50.0000 active_atlas_pods=0' $FAKE_DIR/balance.log"

echo
echo "通过 $PASS，失败 $FAIL"
[ "$FAIL" = 0 ]
