#!/usr/bin/env bash
# Offline GPU guard tests: fake RunPod API v2 + fake runpodctl / ssh; never touches the real account.
# Usage: guard/tests/run_tests.sh
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
export ATLAS_GPU_FETCH_CMD="$HERE/fake_fetch" ATLAS_GPU_BALANCE_LOG="$FAKE_DIR/balance.log" ATLAS_GPU_KEYFILE="$FAKE_DIR/key" ATLAS_GPU_SSH_KEY="$FAKE_DIR/sshkey"
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
  # data for the runpodctl fallback
  echo '[{"id":"US-KS-2","gpuAvailability":[{"gpuId":"NVIDIA A100-SXM4-80GB","stockStatus":"Low"}]}]' > "$FAKE_DIR/dc.json"
  echo '[{"gpuId":"NVIDIA A100-SXM4-80GB","securePricePerHr":1.79}]' > "$FAKE_DIR/gpu.json"
  echo '{"keys":[{"key":"ssh-ed25519 AAAAjoey joey"}]}' > "$FAKE_DIR/keys.json"
}
avail() { python3 -c "import json,sys;p=sys.argv[1];d=json.load(open(p));d[sys.argv[2]]['availability']=sys.argv[3];json.dump(d,open(p,'w'))" "$FAKE_DIR/api_catalog.json" "$1" "$2"; }
apipods() { echo "$1" > "$FAKE_DIR/api_pods.json"; }
ctlpods() { echo "$1" > "$FAKE_DIR/pods.json"; }
state() { mkdir -p "$FAKE_DIR/state"; echo "$2" > "$FAKE_DIR/state/$1.json"; }
pod() {  # pod id name status minutes-since-start [util]
  local u=${5:-null}; [ "$u" = null ] && rt=null || rt="{\"gpus\":[{\"util\":$u}]}"
  echo "{\"id\":\"$1\",\"name\":\"$2\",\"status\":\"$3\",\"startedAt\":\"$(ago $4)\",\"cost\":1.79,\"gpu\":{\"id\":\"NVIDIA A100-SXM4-80GB\"},\"runtime\":$rt,\"ssh\":{\"direct\":{\"command\":\"ssh root@pod.example.invalid -p 1\"}}}"
}
check() {  # check description expected(0/1) command...
  local desc=$1 want=$2; shift 2
  if eval "$@" >/dev/null 2>&1; then got=0; else got=1; fi
  if [ "$got" = "$want" ]; then PASS=$((PASS+1)); echo "  ✓ $desc"; else FAIL=$((FAIL+1)); echo "  ✗ $desc"; fi
}
called() { grep -q -- "$1" "$FAKE_DIR/calls.log" 2>/dev/null; }
OTHER=$(pod other1 eramoment_find_attendee_tool RUNNING 600 0)

echo "== gpu-up"
reset
check "dry-run with default args passes"         0 "$G up --dry-run"
check "dry-run does not actually create"         1 "called 'API POST /v2/pods '"
check "picks A100 SXM, mounts Global volume, no region lock"  0 "$G up --dry-run | grep -q 'vol_test123' && $G up --dry-run | grep -q '\"path\": \"/workspace\"' && ! $G up --dry-run | grep -q dataCenterIds"
check "mount path comes from ATLAS_VOL_MOUNT"         0 "ATLAS_VOL_MOUNT=/workspace-global $G up --dry-run | grep -q '\"ATLAS_VOL\": \"/workspace-global\"'"
check "container disk defaults to 200GB"          0 "$G up --dry-run | grep -q '\"disk\": 200'"
check "--hours 7 is rejected"                    1 "$G up --dry-run --hours 7"
check "--hours 0 is rejected"                    1 "$G up --dry-run --hours 0"
check "--gpu rtx4090 is rejected"                1 "$G up --dry-run --gpu rtx4090"
check "--gpu h100 rejected while A100 in stock"  1 "$G up --dry-run --gpu h100"
avail "NVIDIA A100-SXM4-80GB" NONE
check "auto falls back to H100 when A100 out of stock everywhere"       0 "$G up --dry-run | grep -q '\"id\": \"NVIDIA H100 80GB HBM3\"'"
check "--gpu a100 errors when A100 out of stock" 1 "$G up --dry-run --gpu a100"
avail "NVIDIA H100 80GB HBM3" NONE
check "errors when everything is out of stock"   1 "$G up --dry-run"
reset
check "name is forced to atlas- prefix"          0 "$G up --dry-run --name gen3c | grep -q 'atlas-gen3c'"
check "invalid name is rejected"                 1 "$G up --dry-run --name 'a b;rm'"
apipods "[$(pod p1 atlas-x RUNNING 10 50),$OTHER]"
check "refused when 1 atlas- pod already running" 1 "$G up --dry-run"
apipods "[$(pod p1 atlas-x PROVISIONING 1),$OTHER]"
check "PROVISIONING pod also counts toward limit" 1 "$G up --dry-run"
apipods "[$OTHER]"
check "other projects' pods do not count"        0 "$G up --dry-run"
echo '[]' > "$FAKE_DIR/api_gv.json"
check "refused when configured volume ID not found in API"      1 "$G up"
reset
check "volume identified by ID (slash in name is fine)"  0 "$G up --dry-run | grep -q 'vol_test123 (confirmed via v2 API)' && $G up --dry-run | grep -q 'photographer/videographer_killer'"
check "no name matching (another volume named atlas_data is not used)" 1 "$G up --dry-run | grep -q vol_other99"
echo '{}' > "$FAKE_DIR/config.json"
check "refused without configured volume ID (no guessing by name)"   1 "$G up --dry-run"
echo '{"global_volume_id":"bad/id"}' > "$FAKE_DIR/config.json"
check "invalid volume ID is rejected"            1 "$G up --dry-run"
echo '== balance check'
reset; echo '{"clientBalance": 2.99}' > "$FAKE_DIR/user.json"
check "refuse to start pod when balance < \$3"   1 "$G up --wait 0"
check "no create request when balance too low"   1 "called 'API POST /v2/pods '"
check "dry-run still previews and warns when balance < \$3"    0 "$G up --dry-run | grep -q 'please top up first'"
echo '{"clientBalance": 3}' > "$FAKE_DIR/user.json"
check "balance = \$3 can start"                  0 "$G up --wait 0"
reset; echo '{"clientBalance": 12.97}' > "$FAKE_DIR/user.json"
check "balance \$12.97 (below old \$15 minimum) can now start" 0 "$G up --wait 0"
reset; touch "$FAKE_DIR/user_fail"
check "refuse to start pod when balance unreadable" 1 "$G up --wait 0"
reset; echo '{"clientBalance": 10}' > "$FAKE_DIR/user.json"
check "warn when balance would drop below \$3 after full run"          0 "$G up --dry-run --hours 6 | grep -q 'consider topping up'"
check "no warning when still above \$3 after full run"                1 "$G up --dry-run --hours 1 | grep -q 'consider topping up'"
reset; echo '{"clientBalance": 20}' > "$FAKE_DIR/user.json"
apipods "[$(pod p1 atlas-x RUNNING 10 50)]"
check "gpu-status shows balance"                  0 "$G status | grep -q 'Account balance: \$20.00'"
reset; key_off
check "real start refused without API key"       1 "$G up"
check "dry-run previews via runpodctl without API key" 0 "$G up --dry-run | grep -q 'not confirmed via API'"
reset; chmod 644 "$FAKE_DIR/key"
check "refused when key file permissions too open" 1 "$G up"
reset
check "real start (fake API) succeeds and prints SSH" 0 "$G up --hours 2 --wait 0 | grep -q 'SSH      : ssh root@pod.example.invalid'"
check "state file written after start"           0 "test -f $FAKE_DIR/state/fakepod123.json"
check "PUBLIC_KEY has server key and account key" 0 "grep 'API POST /v2/pods ' $FAKE_DIR/calls.log | grep -q 'AAAAtest' && grep 'API POST /v2/pods ' $FAKE_DIR/calls.log | grep -q 'AAAAjoey'"
reset; echo 'NVIDIA A100-SXM4-80GB' > "$FAKE_DIR/create_fail"
check "switches to H100 when A100 cannot be obtained" 0 "$G up --wait 0 && grep 'API POST /v2/pods ' $FAKE_DIR/calls.log | tail -1 | grep -q 'H100 80GB HBM3'"

echo "== gpu-down"
reset
apipods "[$(pod p1 atlas-x RUNNING 10 50),$OTHER]"
check "refuse to stop non-atlas- pod (by name)"  1 "$G down eramoment_find_attendee_tool"
check "refuse to stop non-atlas- pod (by ID)"    1 "$G down other1"
check "no write operation ever sent for other1"  1 "grep -E 'POST|DELETE' $FAKE_DIR/calls.log | grep -q other1"
check "can stop atlas- pod (via API)"            0 "$G down atlas-x && called 'API POST /v2/pods/p1/action {\"action\": \"stop\"}'"
check "--all --delete deletes only atlas-"       0 "$G down --all --delete && called 'API DELETE /v2/pods/p1' && ! grep -E 'POST|DELETE' $FAKE_DIR/calls.log | grep -q other1"
reset; touch "$FAKE_DIR/api_fail"
ctlpods '[{"id":"p1","name":"atlas-x","desiredStatus":"RUNNING"},{"id":"other1","name":"eramoment_find_attendee_tool","desiredStatus":"RUNNING"}]'
check "falls back to runpodctl stop on API failure" 0 "$G down atlas-x && called 'pod stop p1'"

echo "== gpu-status"
reset
apipods "[$(pod p1 atlas-x RUNNING 90 42),$OTHER]"
state p1 '{"max_hours":6}'
check "shows atlas- pods, hides other projects"  0 "$G status | grep -q atlas-x && ! $G status | grep -q eramoment"
check "uptime / cost / remaining / utilization correct" 0 "$G status | grep -q '1h30m' && $G status | grep -q '\$2.6' && $G status | grep -qE '4h(29|30)m' && $G status | grep -q ' 42 '"

echo "== gpu-guard"
reset
apipods "[$(pod p1 atlas-x RUNNING 370 80),$OTHER]"
state p1 '{"max_hours":6}'
check "over 6h → stop (even if GPU busy)"        0 "$G guard && called 'API POST /v2/pods/p1/action'"
check "never stops other projects"               1 "grep -E 'POST|DELETE' $FAKE_DIR/calls.log | grep -q other1"
check "pre_stop sync ran before stop"            0 "grep -q 'PRE_STOP p1' $FAKE_DIR/guard.log"
reset
apipods "[$(pod p1 atlas-x RUNNING 100 80)]"
state p1 '{"max_hours":1.5}'
check "over custom 1.5h → stop"                  0 "$G guard && called 'API POST /v2/pods/p1/action'"
reset
apipods "[$(pod p1 atlas-x RUNNING 60 0)]"
state p1 '{"max_hours":6,"idle_since":null}'
echo 0 > "$FAKE_DIR/util"
check "first util=0 → only starts the timer"     0 "$G guard && ! called '/action'"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 20)\"}"
check "idle 20 minutes → no stop"                0 "$G guard && ! called '/action'"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 30)\"}"
check "idle 30 minutes → stop"                   0 "$G guard && called 'API POST /v2/pods/p1/action'"
reset
apipods "[$(pod p1 atlas-x RUNNING 60 37)]"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 40)\"}"
echo FAIL > "$FAKE_DIR/util"
check "API util>0 → no SSH needed, idle timer cleared" 0 "$G guard && ! called '/action' && grep -q '\"idle_since\": null' $FAKE_DIR/state/p1.json"
reset
apipods "[$(pod p1 atlas-x RUNNING 60 0)]"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 40)\"}"
echo 55 > "$FAKE_DIR/util"
check "API says 0 but nvidia-smi recheck >0 → no stop" 0 "$G guard && ! called '/action'"
echo 0 > "$FAKE_DIR/util"; echo 60 > "$FAKE_DIR/busy"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 40)\"}"
check "util=0 but busy marker fresh → no stop"   0 "$G guard && ! called '/action'"
echo 3600 > "$FAKE_DIR/busy"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 40)\"}"
check "busy marker stale → stop"                 0 "$G guard && called 'API POST /v2/pods/p1/action'"
reset
apipods "[$(pod p1 atlas-x RUNNING 10)]"
state p1 '{"max_hours":6}'
echo FAIL > "$FAKE_DIR/util"
check "10 min after boot, no util, SSH down → grace period" 0 "$G guard && ! called '/action'"
apipods "[$(pod p1 atlas-x RUNNING 90)]"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 30)\"}"
check "probe failing for 30 minutes after boot → stop" 0 "$G guard && called 'API POST /v2/pods/p1/action'"
reset
nossh="{\"id\":\"p1\",\"name\":\"atlas-x\",\"status\":\"RUNNING\",\"startedAt\":\"$(ago 60)\",\"cost\":1.79,\"runtime\":{\"gpus\":[{\"util\":0}]}}"
apipods "[$nossh]"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 40)\",\"ssh_cmd\":\"ssh root@pod.example.invalid -p 1\"}"
echo 0 > "$FAKE_DIR/util"; echo 30 > "$FAKE_DIR/busy"
check "cached SSH command used when API lacks SSH info, busy still works" 0 "$G guard && ! called '/action' && grep -q '\"idle_since\": null' $FAKE_DIR/state/p1.json"
reset
apipods "[$(pod p9 atlas-manual RUNNING 400 90)]"
check "atlas- pod not created by gpu-up also gets 6h limit" 0 "$G guard && called 'API POST /v2/pods/p9/action'"
reset
apipods "[$(pod p1 atlas-x RUNNING 400 90)]"
check "guard --dry-run does not really stop"     0 "$G guard --dry-run && ! called '/action' && grep -q dry-run $FAKE_DIR/guard.log"
reset
apipods "[$(pod p1 atlas-x EXITED 400)]"
check "already stopped pod is not stopped again" 0 "$G guard && ! called '/action'"
reset; touch "$FAKE_DIR/api_fail"
ctlpods "[{\"id\":\"p1\",\"name\":\"atlas-x\",\"desiredStatus\":\"RUNNING\",\"lastStartedAt\":\"$(ago 400)\"},{\"id\":\"other1\",\"name\":\"eramoment_find_attendee_tool\",\"desiredStatus\":\"RUNNING\",\"lastStartedAt\":\"$(ago 999)\"}]"
check "patrol falls back to runpodctl on API failure, still stops on timeout" 0 "$G guard && called 'pod stop p1' && ! called 'pod stop other1'"

echo "== auto-stop after runs finish / idle threshold"
reset
apipods "[$(pod p1 atlas-x RUNNING 60 80)]"
state p1 '{"max_hours":6,"stop_after_runs":["r1"]}'
echo "r1 running 0" > "$FAKE_DIR/runs_status"
check "registered run still running → no stop"   0 "$G guard && ! called '/action'"
echo "r1 done 600" > "$FAKE_DIR/runs_status"
check "registered run finished 10 minutes ago → auto-stop"            0 "$G guard && called 'API POST /v2/pods/p1/action' && grep -q 'all finished' $FAKE_DIR/guard.log"
check "pre_stop ran before auto-stop"            0 "grep -q 'PRE_STOP p1' $FAKE_DIR/guard.log"
reset
apipods "[$(pod p1 atlas-x RUNNING 60 80)]"
state p1 '{"max_hours":6,"stop_after_runs":["r1"]}'
echo "r1 done 60" > "$FAKE_DIR/runs_status"
check "run finished 1 minute ago (grace period) → no stop" 0 "$G guard && ! called '/action'"
state p1 '{"max_hours":6,"stop_after_runs":["r1","r2"]}'
printf 'r1 done 900\nr2 running 0\n' > "$FAKE_DIR/runs_status"
check "one finished, one still running → no stop" 0 "$G guard && ! called '/action'"
printf 'r1 done 900\nr2 failed 700\n' > "$FAKE_DIR/runs_status"
check "failed runs count as finished → stop"     0 "$G guard && called 'API POST /v2/pods/p1/action'"
reset
apipods "[$(pod p1 atlas-x RUNNING 60 80)]"
state p1 '{"max_hours":6,"stop_after_runs":[]}'
echo "r1 done 9999" > "$FAKE_DIR/runs_status"
check "no registered runs (--keep-alive) → no run-based stop" 0 "$G guard && ! called '/action'"
reset
apipods "[$(pod p1 atlas-x RUNNING 60 2)]"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 30)\"}"
echo 2 > "$FAKE_DIR/util"
check "utilization 2% (below 5%) for 30 minutes → stop" 0 "$G guard && called 'API POST /v2/pods/p1/action'"
reset
apipods "[$(pod p1 atlas-x RUNNING 60 2)]"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 30)\"}"
echo 2 > "$FAKE_DIR/util"; echo 30 > "$FAKE_DIR/busy"
check "utilization 2% but busy fresh → no stop"  0 "$G guard && ! called '/action'"
reset
apipods "[$(pod p1 atlas-x RUNNING 60 6)]"
state p1 "{\"max_hours\":6,\"idle_since\":\"$(ago 40)\"}"
check "utilization 6% → busy, idle timer cleared" 0 "$G guard && ! called '/action' && grep -q '\"idle_since\": null' $FAKE_DIR/state/p1.json"

reset
apipods "[]"
check "patrol records balance to balance.log"    0 "$G guard && grep -q '50.0000 active_atlas_pods=0' $FAKE_DIR/balance.log"

reset
apipods "[$(pod p1 atlas-x RUNNING 60 80)]"
state p1 '{"max_hours":6,"stop_after_runs":["r1","r2"]}'
printf 'r1 done 900\nr2 done 700\n' > "$FAKE_DIR/runs_status"
check "results of registered runs pulled to server before stop" 0 "$G guard && grep -q 'FETCH r1: rc=0' $FAKE_DIR/guard.log && grep -q 'FETCH r2: rc=0' $FAKE_DIR/guard.log && grep -q 'r1' $FAKE_DIR/fetched"
check "fetch happens before stop"                0 "awk '/FETCH r2/{f=NR} /STOP p1/{s=NR} END{exit !(f && s && f<s)}' $FAKE_DIR/guard.log"

echo
echo "Passed $PASS, failed $FAIL"
[ "$FAIL" = 0 ]
