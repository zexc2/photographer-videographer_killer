#!/usr/bin/env python3
"""ATLAS GPU 护栏：所有 RunPod pod 的开 / 关 / 状态 / 巡检都走这里。

入口（~/bin 下的软链接）：
  gpu-up      开 pod（白名单 GPU、atlas- 前缀、最多 1 个、挂 Global volume atlas_data、限时）
  gpu-down    stop / delete atlas- 前缀的 pod
  gpu-status  列出 atlas- pod、已运行时长、估算花费、距自动关机时间
  gpu-guard   巡检（cron 每 10 分钟）：超时 → stop；GPU 连续 30 分钟空闲 → stop

后端
- 主路径：RunPod REST API v2（https://api.runpod.io/v2）。Global volume 挂载、GPU 实时利用率、
  SSH 信息都只有 v2 提供。API key 用单独建的「限权 key」，存在 ~/.config/atlas-gpu/runpod_api_key，
  只在发请求时读进内存，绝不打印、不写日志。
- 兜底：API 不可用时，巡检退回 runpodctl（它自己读 ~/.runpod，本脚本不碰），只做列表 + 超时 stop。

运行状态（pod ID、到期时间、空闲计时）存在 ~/.local/state/atlas-gpu/，不在仓库里。
测试用环境变量：ATLAS_RUNPOD_API（假 API 地址）、RUNPODCTL、ATLAS_GPU_SSH、ATLAS_GPU_* 路径。
"""
import argparse
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home()
API_BASE = os.environ.get("ATLAS_RUNPOD_API", "https://api.runpod.io").rstrip("/")
API_KEY_FILE = Path(os.environ.get("ATLAS_GPU_KEYFILE", HOME / ".config/atlas-gpu/runpod_api_key"))
RUNPODCTL = os.environ.get("RUNPODCTL", str(HOME / ".local/bin/runpodctl"))
STATE_DIR = Path(os.environ.get("ATLAS_GPU_STATE", HOME / ".local/state/atlas-gpu"))
CONFIG_FILE = Path(os.environ.get("ATLAS_GPU_CONFIG", HOME / ".config/atlas-gpu/config.json"))
LOG_FILE = Path(os.environ.get("ATLAS_GPU_LOG", HOME / "logs/gpu-guard.log"))
SSH_KEY = Path(os.environ.get("ATLAS_GPU_SSH_KEY", HOME / ".ssh/atlas_runpod"))

PREFIX = "atlas-"
VOLUME_NAME = "atlas_data"
# Global volume 在 pod 里的挂载路径：创建 pod 时显式指定，并作为环境变量 ATLAS_VOL 传进 pod。
# pod 上所有脚本只认 $ATLAS_VOL，不写死 /workspace（同时挂 network volume 时默认会变成 /workspace-global）。
VOL_MOUNT = os.environ.get("ATLAS_VOL_MOUNT", "/workspace")
# 容器盘（本地 NVMe，pod stop 后清空）：conda 环境、工作目录、中间文件、checkpoint
LOCAL_ROOT = "/opt/atlas"
BUSY_MARKER = f"{LOCAL_ROOT}/.atlas_busy"   # 任务执行器定期 touch；新鲜时 GPU 为 0 也不算空闲
PRE_STOP = f"{LOCAL_ROOT}/bin/pre_stop"    # stop 前尽力执行一次：把要保留的结果同步回 $ATLAS_VOL
PRE_STOP_TIMEOUT = 600
BUSY_FRESH_MINUTES = 15

MAX_PODS = 1
DEFAULT_HOURS = 6.0
MAX_HOURS = 6.0
IDLE_MINUTES = 30
DEFAULT_IMAGE = "runpod/pytorch:1.0.3-cu1281-torch291-ubuntu2404"
MIN_CUDA = "12.8"  # 上面镜像是 cu128，主机驱动至少要支持 12.8
DEFAULT_CONTAINER_DISK_GB = 200

# 白名单：优先 A100 80GB，两种 A100 在所有区域都没货（或都开不出来）时才允许 H100
A100_IDS = ["NVIDIA A100-SXM4-80GB", "NVIDIA A100 80GB PCIe"]
H100_IDS = ["NVIDIA H100 80GB HBM3", "NVIDIA H100 PCIe", "NVIDIA H100 NVL"]
GPU_ALIASES = {
    "auto": A100_IDS + H100_IDS,
    "a100": A100_IDS,
    "a100-sxm": ["NVIDIA A100-SXM4-80GB"],
    "a100-pcie": ["NVIDIA A100 80GB PCIe"],
    "h100": H100_IDS,
    "h100-sxm": ["NVIDIA H100 80GB HBM3"],
    "h100-pcie": ["NVIDIA H100 PCIe"],
    "h100-nvl": ["NVIDIA H100 NVL"],
}
FALLBACK_PRICE = {"NVIDIA A100-SXM4-80GB": 1.79, "NVIDIA A100 80GB PCIe": 1.64,
                  "NVIDIA H100 80GB HBM3": 3.99, "NVIDIA H100 PCIe": 2.89, "NVIDIA H100 NVL": 3.29}
ACTIVE = {"PROVISIONING", "STARTING", "RUNNING"}  # 这些状态都在计费


# ---------------------------------------------------------------- 工具函数
class GuardError(Exception):
    pass


class NoApiKey(GuardError):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    if not s:
        return None
    s = str(s).strip().replace(" UTC", "").replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            t = dt.datetime.fromisoformat(s) if fmt is None else dt.datetime.strptime(s, fmt)
            return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def log(msg, also_print=True):
    line = f"{iso(now())} {msg}"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")
    if also_print:
        print(line)


def fmt_dur(sec):
    if sec is None:
        return "?"
    sec = int(max(sec, 0))
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


# ---------------------------------------------------------------- RunPod API v2
def have_api_key():
    return API_KEY_FILE.is_file() and API_KEY_FILE.stat().st_size > 0


def api(method, path, params=None, body=None, timeout=60):
    if not have_api_key():
        raise NoApiKey(f"没有 API key 文件 {API_KEY_FILE}")
    if API_KEY_FILE.stat().st_mode & 0o077:
        raise GuardError(f"{API_KEY_FILE} 权限过宽，请 chmod 600")
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + API_KEY_FILE.read_text().strip())
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            j = json.loads(e.read().decode())
            detail = j.get("detail") or j.get("title") or ""
        except Exception:
            pass
        raise GuardError(f"API {method} {path} → HTTP {e.code} {detail}".strip())
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GuardError(f"API {method} {path} 连接失败: {getattr(e, 'reason', e)}")
    return json.loads(raw) if raw.strip() else None


def norm_v2(p):
    rt = p.get("runtime") or {}
    gpus = rt.get("gpus") or []
    utils = [g.get("util") for g in gpus if g.get("util") is not None]
    ssh = ((p.get("ssh") or {}).get("direct") or {}).get("command")
    started = parse_time(p.get("startedAt"))
    if not started and rt.get("uptime"):
        started = now() - dt.timedelta(seconds=float(rt["uptime"]))
    return {"id": p.get("id"), "name": p.get("name") or "", "status": str(p.get("status") or "").upper(),
            "cost": p.get("cost"), "started": started, "created": parse_time(p.get("createdAt")),
            "gpu": (p.get("gpu") or {}).get("id"), "util": max(utils) if utils else None,
            "ssh": ssh, "dc": p.get("dataCenterId"), "src": "api"}


# ---------------------------------------------------------------- runpodctl 兜底
def ctl(*args, timeout=60):
    try:
        r = subprocess.run([RUNPODCTL, *args], capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise GuardError(f"runpodctl {' '.join(args[:2])} 失败: {e}")
    if r.returncode != 0:
        raise GuardError(f"runpodctl {' '.join(args[:2])} 失败: {(r.stderr or r.stdout).strip()[:300]}")
    out = r.stdout.strip()
    try:
        return json.loads(out) if out else None
    except json.JSONDecodeError:
        return out


def as_list(x, key=None):
    if x is None:
        return []
    if isinstance(x, dict):
        for k in (key, "pods", "items", "data"):
            if k and isinstance(x.get(k), list):
                return x[k]
        return [x]
    return x if isinstance(x, list) else []


def norm_ctl(p):
    started = parse_time(p.get("lastStartedAt"))
    up = (p.get("runtime") or {}).get("uptimeInSeconds")
    if not started and up:
        started = now() - dt.timedelta(seconds=float(up))
    cost = p.get("adjustedCostPerHr") or p.get("costPerHr")
    return {"id": p.get("id"), "name": p.get("name") or "", "status": str(p.get("desiredStatus") or "").upper(),
            "cost": float(cost) if cost else None, "started": started, "created": None,
            "gpu": (p.get("machine") or {}).get("gpuDisplayName") or p.get("gpuTypeId"),
            "util": None, "ssh": None, "dc": None, "src": "runpodctl"}


# ---------------------------------------------------------------- 统一接口
def list_pods(allow_fallback=True):
    """返回 (pods, 来源)。API 失败且允许时退回 runpodctl。"""
    try:
        return [norm_v2(p) for p in as_list(api("GET", "/v2/pods"), "pods")], "api"
    except GuardError as e:
        if not allow_fallback:
            raise
        if not isinstance(e, NoApiKey):
            log(f"WARN API 不可用（{e}），改用 runpodctl", also_print=False)
        return [norm_ctl(p) for p in as_list(ctl("pod", "list", "--all"))], "runpodctl"


def atlas_pods(allow_fallback=True):
    pods, src = list_pods(allow_fallback)
    return [p for p in pods if p["name"].startswith(PREFIX)], src


def pod_action(pod, action):
    """action: stop / delete。只允许 atlas- pod。"""
    if not pod["name"].startswith(PREFIX):
        raise GuardError(f"拒绝操作非 atlas- pod: {pod['name']}")
    try:
        if action == "stop":
            api("POST", f"/v2/pods/{pod['id']}/action", body={"action": "stop"})
        else:
            api("DELETE", f"/v2/pods/{pod['id']}")
    except GuardError as e:
        log(f"WARN API {action} 失败（{e}），改用 runpodctl", also_print=False)
        ctl("pod", action, pod["id"], timeout=120)


def find_global_volume():
    cfg = load_config()
    want = cfg.get("global_volume_id")
    for v in as_list(api("GET", "/v2/global-volumes"), "globalVolumes"):
        if (want and v.get("id") == want) or (not want and v.get("name") == VOLUME_NAME):
            return v
    return None


def gpu_availability(gpu_ids):
    """返回 {gpuId: (availability, price, [dc...], [cuda...])}，覆盖所有区域。"""
    out = {}
    if have_api_key():
        for g in gpu_ids:
            try:
                j = api("GET", f"/v2/catalog/gpus/{urllib.parse.quote(g, safe='')}",
                        {"include": "AVAILABILITY", "product": "POD", "cloud": "SECURE", "minCudaVersion": MIN_CUDA})
            except GuardError:
                continue
            j = (as_list(j, "gpus") or [{}])[0]
            dcs = [d.get("id") for d in j.get("dataCenters") or [] if str(d.get("availability")).upper() != "NONE"]
            cuda = [c.get("version") for c in j.get("cudaVersions") or [] if c.get("available")]
            out[g] = (str(j.get("availability") or "NONE").upper(), (j.get("price") or {}).get("secure"), dcs, cuda)
        return out
    # 没有 key（只在 dry-run 时会走到这里）：用 runpodctl 的库存数据近似
    for d in as_list(ctl("datacenter", "list")):
        for g in d.get("gpuAvailability") or []:
            gid, s = g.get("gpuId"), str(g.get("stockStatus") or "none").upper()
            if gid in gpu_ids and s not in ("NONE", ""):
                lvl, price, dcs, cuda = out.get(gid, ("NONE", None, [], []))
                out[gid] = (s if lvl == "NONE" else lvl, price, dcs + [d.get("id")], cuda)
    for g in as_list(ctl("gpu", "list")):
        if g.get("gpuId") in out:
            lvl, _, dcs, cuda = out[g["gpuId"]]
            out[g["gpuId"]] = (lvl, g.get("securePricePerHr"), dcs, cuda)
    return out


def plan_gpus(want, avail):
    """按白名单 + 库存给出尝试顺序。A100 有货时不允许 H100。"""
    if want not in GPU_ALIASES:
        raise GuardError(f"GPU '{want}' 不在白名单。可选: {', '.join(GPU_ALIASES)}")
    ok = lambda g: avail.get(g, ("NONE",))[0] != "NONE"
    a100_ok = [g for g in A100_IDS if ok(g)]
    cands = GPU_ALIASES[want]
    if a100_ok and not any(g in A100_IDS for g in cands):
        raise GuardError(f"A100 80GB 当前有货（{', '.join(a100_ok)}），规则不允许改用 H100")
    order = [g for g in cands if g in A100_IDS and ok(g)]
    if not order:  # A100 全部没货 → 才允许 H100
        order = [g for g in cands if g in H100_IDS and ok(g)]
    if not order:
        raise GuardError("所有区域都没有可用的白名单 GPU：" +
                         ", ".join(f"{g}={avail.get(g, ('NONE',))[0]}" for g in cands))
    return order


# ---------------------------------------------------------------- 状态文件
def state_path(pod_id):
    return STATE_DIR / f"{re.sub(r'[^A-Za-z0-9_-]', '_', pod_id)}.json"


def load_state(pod_id):
    p = state_path(pod_id)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(pod_id, st):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    tmp = state_path(pod_id).with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2))
    tmp.replace(state_path(pod_id))


def run_start(pod, st):
    """本次运行的起点：RunPod 的 startedAt / uptime → 创建时间 → gpu-up 记录。"""
    return pod["started"] or pod["created"] or parse_time(st.get("created_at"))


# ---------------------------------------------------------------- SSH
def ssh_base(pod):
    override = os.environ.get("ATLAS_GPU_SSH")
    if override:
        return shlex.split(override.replace("{pod}", pod["id"]))
    if not pod.get("ssh"):
        return None
    parts = shlex.split(pod["ssh"])
    opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={HOME / '.ssh/known_hosts_runpod'}"]
    if SSH_KEY.exists() and "-i" not in parts:
        opts += ["-i", str(SSH_KEY)]
    return [parts[0], *opts, *parts[1:]]


def ssh_human(pod):
    if not pod.get("ssh"):
        return None
    cmd = pod["ssh"]
    if SSH_KEY.exists() and " -i " not in f" {cmd} ":
        cmd += f" -i {SSH_KEY}"
    return cmd


def ssh_run(pod, remote, timeout=60):
    base = ssh_base(pod)
    if not base:
        return None, "没有 SSH 信息"
    try:
        r = subprocess.run([*base, remote], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "SSH 超时"
    if r.returncode != 0:
        return None, f"SSH 失败 rc={r.returncode}"
    return r.stdout, "ok"


def probe_gpu(pod):
    """SSH 采样 5 秒 GPU 利用率 + busy 标记。返回 (util 或 None, busy, 说明)。"""
    remote = (
        "for i in 1 2 3 4 5; do nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits; sleep 1; done"
        f" | sort -n | tail -1; if [ -f {BUSY_MARKER} ]; then echo BUSY_AGE=$(( $(date +%s) - $(stat -c %Y {BUSY_MARKER}) )); fi"
    )
    out, why = ssh_run(pod, remote)
    if out is None:
        return None, False, why
    util, busy = None, False
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("BUSY_AGE="):
            try:
                busy = int(line.split("=", 1)[1]) <= BUSY_FRESH_MINUTES * 60
            except ValueError:
                pass
        elif re.fullmatch(r"\d+(\.\d+)?", line):
            util = max(util or 0, float(line))
    return util, busy, "ok" if util is not None else "nvidia-smi 无输出"


def stop_pod(pod, reason, dry_run=False, delete=False):
    if not pod["name"].startswith(PREFIX):  # 双保险
        raise GuardError(f"拒绝操作非 atlas- pod: {pod['name']}")
    action = "delete" if delete else "stop"
    if dry_run:
        log(f"[dry-run] 会 {action} {pod['id']} {pod['name']}：{reason}")
        return
    if pod["status"] == "RUNNING":
        # 容器盘在 stop 后会清空：先尽力把结果同步回 Global volume（有超时，失败也继续 stop）
        out, why = ssh_run(pod, f"[ -x {PRE_STOP} ] && timeout {PRE_STOP_TIMEOUT} {PRE_STOP} || true",
                           timeout=PRE_STOP_TIMEOUT + 30)
        log(f"PRE_STOP {pod['id']} {pod['name']}: {why}", also_print=False)
    pod_action(pod, action)
    log(f"{action.upper()} {pod['id']} {pod['name']}：{reason}")
    if delete:
        state_path(pod["id"]).unlink(missing_ok=True)


# ---------------------------------------------------------------- gpu-up
def pod_public_keys():
    """注入 pod 的 SSH 公钥：本服务器专用 key + 账户里已登记的公钥（保留 Joey 自己的登录方式）。"""
    keys = []
    pub = SSH_KEY.with_suffix(".pub")
    if pub.exists():
        keys.append(pub.read_text().strip())
    try:
        for k in as_list(ctl("ssh", "list-keys"), "keys"):
            k = k.get("key") if isinstance(k, dict) else k
            if isinstance(k, str) and k.startswith(("ssh-", "ecdsa-")):
                keys.append(k.strip())
    except GuardError:
        pass
    return "\n".join(dict.fromkeys(keys))


def cmd_up(a):
    if not (0 < a.hours <= MAX_HOURS):
        raise GuardError(f"--hours 必须在 (0, {MAX_HOURS:g}] 之间")
    name = a.name if (a.name or "").startswith(PREFIX) else PREFIX + (a.name or now().strftime("%Y%m%d-%H%M"))
    if not re.fullmatch(r"atlas-[A-Za-z0-9_-]{1,40}", name):
        raise GuardError(f"pod 名字不合法: {name}")
    if not a.dry_run and not have_api_key():
        raise GuardError(f"开 pod 需要 RunPod API key 文件 {API_KEY_FILE}（见 guard/README.md）")
    if not a.dry_run and not SSH_KEY.exists():
        raise GuardError(f"缺少 SSH key {SSH_KEY}，先运行 guard/install.sh")

    pods, src = atlas_pods(allow_fallback=a.dry_run)
    active = [p for p in pods if p["status"] in ACTIVE]
    if len(active) >= MAX_PODS:
        raise GuardError(f"已有 {len(active)} 个 atlas- pod 在运行（上限 {MAX_PODS}）：" +
                         ", ".join(p["name"] for p in active) + "。先 gpu-down。")

    if have_api_key():
        vol = find_global_volume()
        if not vol:
            raise GuardError(f"找不到 Global volume '{VOLUME_NAME}'（或 config 里的 global_volume_id）")
        vol_id = vol["id"]
    else:
        vol_id = "<GLOBAL_VOLUME_ID>"
        print("[dry-run] 没有 API key：库存来自 runpodctl，Global volume ID 用占位符")

    # 不管要哪种卡都要查 A100 库存：A100 有货时不允许 H100
    avail = gpu_availability(list(dict.fromkeys(A100_IDS + GPU_ALIASES.get(a.gpu, []))))
    order = plan_gpus(a.gpu, avail)
    env = {"ATLAS_VOL": VOL_MOUNT, "ATLAS_LOCAL": LOCAL_ROOT, "ATLAS_POD_NAME": name}
    pubkeys = pod_public_keys()
    if pubkeys:
        env["PUBLIC_KEY"] = pubkeys

    def body_for(gpu):
        g = {"id": gpu, "count": 1}
        cuda = avail.get(gpu, (None, None, [], []))[3]
        if cuda:
            g["allowedCudaVersions"] = cuda
        return {"name": name, "image": a.image, "cloud": "SECURE", "gpu": g, "disk": a.disk,
                "ports": ["22/tcp"], "env": env,
                "mounts": {"global": [{"volumeId": vol_id, "path": VOL_MOUNT}]}}
        # 不传 dataCenterIds：Global volume 不绑机房，让调度器在任何有货的区域开

    def price_of(gpu):
        return avail.get(gpu, (None, None))[1] or FALLBACK_PRICE.get(gpu)

    deadline = now() + dt.timedelta(hours=a.hours)
    print(f"名字     : {name}")
    print("GPU 顺序 : " + " → ".join(f"{g}（{avail[g][0]}，{len(avail[g][2])} 个区域）" for g in order))
    print(f"Volume   : Global {VOLUME_NAME} → {VOL_MOUNT}（pod 内变量 $ATLAS_VOL）")
    print(f"容器盘   : {a.disk} GB（$ATLAS_LOCAL={LOCAL_ROOT}，stop 后清空）")
    print(f"镜像     : {a.image}")
    print(f"时长上限 : {a.hours:g} h（到 {iso(deadline)} 自动 stop）")
    p0 = price_of(order[0])
    print(f"估算花费 : ${p0:.2f}/h × {a.hours:g} h ≈ ${p0 * a.hours:.2f}（上限，不含容器盘和 Volume 存储）" if p0 else "估算花费 : 价格未知")
    if a.dry_run:
        shown = body_for(order[0])
        shown["env"] = {**env, "PUBLIC_KEY": f"<{len(pubkeys.splitlines())} 个公钥>"} if pubkeys else env
        print("[dry-run] 将 POST /v2/pods:\n" + json.dumps(shown, indent=2, ensure_ascii=False))
        return 0

    pod, errors = None, []
    for gpu in order:
        try:
            pod = api("POST", "/v2/pods", body=body_for(gpu), timeout=120)
            break
        except GuardError as e:
            errors.append(f"{gpu}: {e}")
            print(f"  {gpu} 开不出来：{e}")
    if pod is None and order[0] in A100_IDS and a.gpu == "auto":
        avail.update(gpu_availability(H100_IDS))
        h100 = [g for g in H100_IDS if avail.get(g, ("NONE",))[0] != "NONE"]
        if h100:
            print("  所有 A100 都开不出来，按规则改试 H100")
            for gpu in h100:
                try:
                    pod = api("POST", "/v2/pods", body=body_for(gpu), timeout=120)
                    break
                except GuardError as e:
                    errors.append(f"{gpu}: {e}")
    if pod is None:
        raise GuardError("没有开出 pod：" + "；".join(errors))

    pod = norm_v2(pod)
    save_state(pod["id"], {"name": name, "gpu": pod["gpu"], "price_per_hr": pod["cost"] or price_of(pod["gpu"]),
                           "max_hours": a.hours, "created_at": iso(now()), "deadline": iso(deadline),
                           "idle_since": None})
    log(f"UP {pod['id']} {name} gpu={pod['gpu']} max_hours={a.hours:g}")
    print(f"\npod ID   : {pod['id']}")
    t0 = time.time()
    while time.time() - t0 < a.wait * 60 and not pod.get("ssh"):
        time.sleep(10)
        try:
            pod = norm_v2(api("GET", f"/v2/pods/{pod['id']}"))
        except GuardError:
            pass
    print(f"区域     : {pod.get('dc') or '?'}，状态 {pod['status']}")
    print(f"SSH      : {ssh_human(pod)}" if pod.get("ssh") else
          f"SSH      : 暂未就绪，稍后 gpu-status -v 查看")
    return 0


# ---------------------------------------------------------------- gpu-down
def cmd_down(a):
    pods, _ = atlas_pods()
    if a.all:
        targets = pods if a.delete else [p for p in pods if p["status"] in ACTIVE]
    else:
        targets = [p for p in pods if a.target in (p["id"], p["name"])]
        if not targets:
            allp, _ = list_pods()
            if any(a.target in (p["id"], p["name"]) for p in allp):
                raise GuardError(f"{a.target} 不是 atlas- 前缀的 pod，拒绝操作")
            raise GuardError(f"找不到 atlas- pod: {a.target}")
        if len(targets) > 1:
            raise GuardError(f"{a.target} 匹配到多个 pod，请用 pod ID")
    if not targets:
        print("没有需要处理的 atlas- pod")
    for p in targets:
        stop_pod(p, "手动 gpu-down", dry_run=a.dry_run, delete=a.delete)
    return 0


# ---------------------------------------------------------------- gpu-status
def cmd_status(a):
    pods, src = atlas_pods()
    if not pods:
        print(f"没有 atlas- pod（数据来源 {src}）")
        return 0
    total = 0.0
    print(f"{'名字':<22}{'状态':<13}{'GPU':<24}{'已运行':>8}{'$/h':>7}{'已花费':>9}{'距自动关机':>12}  GPU%  空闲")
    for p in pods:
        st = load_state(p["id"])
        price = p["cost"] or st.get("price_per_hr")
        start = run_start(p, st)
        act = p["status"] in ACTIVE
        elapsed = (now() - start).total_seconds() if (start and act) else None
        cost = price * elapsed / 3600 if (price and elapsed) else None
        total += cost or 0
        max_h = float(st.get("max_hours") or MAX_HOURS)
        left = (max_h * 3600 - elapsed) if elapsed is not None else None
        idle = st.get("idle_since")
        idle_txt = f"空闲 {fmt_dur((now() - parse_time(idle)).total_seconds())}" if idle else "-"
        gpu = p["gpu"] or st.get("gpu") or "?"
        util = f"{p['util']:g}" if p["util"] is not None else "-"
        print(f"{p['name']:<22}{p['status']:<13}{gpu[:23]:<24}"
              f"{fmt_dur(elapsed) if act else '-':>8}{(f'{price:.2f}' if price else '?'):>7}"
              f"{(f'${cost:.2f}' if cost is not None else '-'):>9}{(fmt_dur(left) if act else '-'):>12}  {util:>4}  {idle_txt}")
        if a.verbose:
            print(f"    id={p['id']} 区域={p.get('dc') or '?'}")
            if ssh_human(p):
                print(f"    ssh: {ssh_human(p)}")
    print(f"本次运行累计估算: ${total:.2f}（只算运行中的 pod，$/h × 本次运行时长；数据来源 {src}）")
    return 0


# ---------------------------------------------------------------- gpu-guard（巡检）
def cmd_guard(a):
    try:
        pods, src = atlas_pods()
    except GuardError as e:
        log(f"ERROR 拿不到 pod 列表（API 和 runpodctl 都失败）：{e}")
        return 1
    active = [p for p in pods if p["status"] in ACTIVE]
    if not active:
        if a.verbose:
            log(f"巡检：没有运行中的 atlas- pod（{src}）")
        return 0
    rc = 0
    for p in active:
        st = load_state(p["id"])
        if not st:
            st = {"name": p["name"], "max_hours": MAX_HOURS, "created_at": iso(now()), "idle_since": None,
                  "note": "非 gpu-up 创建，按默认 6h 管理"}
            log(f"WARN {p['id']} {p['name']} 没有 gpu-up 记录，按 {MAX_HOURS:g}h 上限管理")
        start = run_start(p, st)
        elapsed = (now() - start).total_seconds() if start else 0
        max_h = min(float(st.get("max_hours") or MAX_HOURS), MAX_HOURS)
        try:
            # 1) 超时：只依赖 pod 列表（API 或 runpodctl），不需要 SSH
            if elapsed >= max_h * 3600:
                stop_pod(p, f"超过时长上限 {max_h:g}h（已运行 {fmt_dur(elapsed)}）", dry_run=a.dry_run)
                continue
            # 2) 空闲：API 利用率 > 0 直接算忙；为 0 或拿不到时，再 SSH 用 nvidia-smi 复核 + 看 busy 标记
            util, busy, why = p["util"], False, "api"
            if util is None or util == 0:
                s_util, busy, why = probe_gpu(p)
                if s_util is not None:
                    util = s_util
            in_grace = elapsed < IDLE_MINUTES * 60
            if util is not None and (util > 0 or busy):
                st["idle_since"] = None
                state = f"util={util:g}%" + (" busy" if busy else "")
            elif util is None and (in_grace or p["status"] != "RUNNING"):
                state = f"探测失败（{why}），{p['status']}，开机 {IDLE_MINUTES} 分钟内不计空闲"
            else:
                st["idle_since"] = st.get("idle_since") or iso(now())
                idle_for = (now() - parse_time(st["idle_since"])).total_seconds()
                state = ("util=0%" if util is not None else f"探测失败（{why}）") + f"，已空闲 {fmt_dur(idle_for)}"
                if idle_for >= IDLE_MINUTES * 60 - 30:
                    stop_pod(p, f"GPU 连续空闲 ≥{IDLE_MINUTES} 分钟（{state}）", dry_run=a.dry_run)
                    continue
            log(f"巡检 {p['id']} {p['name']}: 已运行 {fmt_dur(elapsed)}/{max_h:g}h，{state}（{src}）",
                also_print=a.verbose)
        except GuardError as e:
            log(f"ERROR {p['id']} {p['name']}: {e}")
            rc = 1
        finally:
            save_state(p["id"], st)
    return rc


# ---------------------------------------------------------------- main
def main(argv=None):
    prog = Path(sys.argv[0]).name
    sub_from_prog = {"gpu-up": "up", "gpu-down": "down", "gpu-status": "status", "gpu-guard": "guard"}.get(prog)
    argv = list(sys.argv[1:] if argv is None else argv)
    if sub_from_prog:
        argv = [sub_from_prog, *argv]

    ap = argparse.ArgumentParser(prog="atlas_gpu")
    sp = ap.add_subparsers(dest="cmd", required=True)

    up = sp.add_parser("up", help="开 pod")
    up.add_argument("--gpu", default="auto", help=f"{'/'.join(GPU_ALIASES)}（默认 auto：A100 优先，无货才 H100）")
    up.add_argument("--image", default=DEFAULT_IMAGE)
    up.add_argument("--hours", type=float, default=DEFAULT_HOURS, help=f"时长上限，默认 {DEFAULT_HOURS:g}，最大 {MAX_HOURS:g}")
    up.add_argument("--name", help="pod 名字（自动加 atlas- 前缀）")
    up.add_argument("--disk", type=int, default=DEFAULT_CONTAINER_DISK_GB, help="容器盘 GB")
    up.add_argument("--wait", type=float, default=10, help="等待 SSH 就绪的分钟数")
    up.add_argument("--dry-run", action="store_true")

    dn = sp.add_parser("down", help="stop / delete atlas- pod")
    dn.add_argument("target", nargs="?", help="pod ID 或名字")
    dn.add_argument("--all", action="store_true", help="所有 atlas- pod")
    dn.add_argument("--delete", action="store_true", help="删除（默认只 stop）")
    dn.add_argument("--dry-run", action="store_true")

    stp = sp.add_parser("status", help="查看 atlas- pod")
    stp.add_argument("-v", "--verbose", action="store_true", help="显示 pod ID、区域、SSH")

    gd = sp.add_parser("guard", help="巡检（cron 用）")
    gd.add_argument("--dry-run", action="store_true", help="只记录会做什么，不真的 stop")
    gd.add_argument("-v", "--verbose", action="store_true")

    a = ap.parse_args(argv)
    if a.cmd == "down" and not (a.target or a.all):
        ap.error("gpu-down 需要 pod ID / 名字，或 --all")
    try:
        return {"up": cmd_up, "down": cmd_down, "status": cmd_status, "guard": cmd_guard}[a.cmd](a)
    except GuardError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
