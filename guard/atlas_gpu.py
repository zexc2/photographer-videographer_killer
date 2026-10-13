#!/usr/bin/env python3
"""ATLAS GPU 护栏：所有 RunPod pod 的开 / 关 / 状态 / 巡检都走这里。

入口（~/bin 下的软链接）：
  gpu-up      开 pod（白名单 GPU、atlas- 前缀、最多 1 个、挂 atlas_data、限时）
  gpu-down    stop / delete atlas- 前缀的 pod
  gpu-status  列出 atlas- pod、已运行时长、估算花费、距自动关机时间
  gpu-guard   巡检（cron 每 10 分钟）：超时 → stop；GPU 连续 30 分钟空闲 → stop

设计要点
- 只调用 runpodctl（它自己读 ~/.runpod 里的 key），本脚本从不读取 key。
- 运行状态（pod ID、到期时间、空闲计时）存在 ~/.local/state/atlas-gpu/，不在仓库里。
- 环境变量 RUNPODCTL / ATLAS_GPU_SSH 可替换成假的命令，用于离线测试（见 guard/tests）。
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
from pathlib import Path

HOME = Path.home()
RUNPODCTL = os.environ.get("RUNPODCTL", str(HOME / ".local/bin/runpodctl"))
STATE_DIR = Path(os.environ.get("ATLAS_GPU_STATE", HOME / ".local/state/atlas-gpu"))
CONFIG_FILE = Path(os.environ.get("ATLAS_GPU_CONFIG", HOME / ".config/atlas-gpu/config.json"))
LOG_FILE = Path(os.environ.get("ATLAS_GPU_LOG", HOME / "logs/gpu-guard.log"))

PREFIX = "atlas-"
VOLUME_NAME = "atlas_data"
MOUNT_PATH = "/workspace"
MAX_PODS = 1
DEFAULT_HOURS = 6.0
MAX_HOURS = 6.0
IDLE_MINUTES = 30
# pod 上的任务执行器会定期 touch 这个文件；下载 / 安装 / CPU 预处理期间 GPU 为 0 也不算空闲
BUSY_MARKER = "/workspace/.atlas_busy"
BUSY_FRESH_MINUTES = 15
DEFAULT_IMAGE = "runpod/pytorch:1.0.3-cu1281-torch291-ubuntu2404"
DEFAULT_CONTAINER_DISK_GB = 50

# 白名单：优先 A100 80GB，两种 A100 都没货时才允许 H100
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
# gpu list 拿不到价格时的兜底估价（$/h，Secure Cloud，2026-10 查询）
FALLBACK_PRICE = {"NVIDIA A100-SXM4-80GB": 1.79, "NVIDIA A100 80GB PCIe": 1.64,
                  "NVIDIA H100 80GB HBM3": 3.99, "NVIDIA H100 PCIe": 2.89, "NVIDIA H100 NVL": 3.29}
IN_STOCK = {"low", "medium", "high"}


# ---------------------------------------------------------------- 工具函数
class GuardError(Exception):
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


def rp(*args, timeout=60):
    """调用 runpodctl，返回解析后的 JSON。"""
    cmd = [RUNPODCTL, *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GuardError(f"runpodctl {' '.join(args[:2])} 超时")
    if r.returncode != 0:
        raise GuardError(f"runpodctl {' '.join(args[:2])} 失败: {(r.stderr or r.stdout).strip()[:300]}")
    out = r.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


def as_list(x):
    if x is None:
        return []
    if isinstance(x, dict):
        for k in ("pods", "items", "data"):
            if isinstance(x.get(k), list):
                return x[k]
        return [x]
    return x if isinstance(x, list) else []


def is_atlas(pod):
    return str(pod.get("name") or "").startswith(PREFIX)


def pod_running(pod):
    return str(pod.get("desiredStatus") or "").upper() == "RUNNING"


def fmt_dur(sec):
    if sec is None:
        return "?"
    sec = int(max(sec, 0))
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


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


def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


# ---------------------------------------------------------------- RunPod 查询
def list_pods():
    return as_list(rp("pod", "list", "--all"))


def atlas_pods():
    return [p for p in list_pods() if is_atlas(p)]


def find_volume():
    """找 Network Volume：优先配置文件里的 ID，否则按名字 atlas_data 查。"""
    cfg = load_config()
    vols = as_list(rp("network-volume", "list"))
    want_id = cfg.get("network_volume_id")
    for v in vols:
        if (want_id and v.get("id") == want_id) or (not want_id and v.get("name") == VOLUME_NAME):
            return {"id": v.get("id"), "name": v.get("name"), "size": v.get("size"),
                    "dc": v.get("dataCenterId") or (v.get("dataCenter") or {}).get("id")}
    return None


def stock_in_dc(dc_id):
    """返回 {gpuId: stockStatus}，只看指定数据中心。"""
    out = {}
    for d in as_list(rp("datacenter", "list")):
        if d.get("id") == dc_id:
            for g in d.get("gpuAvailability") or []:
                out[g.get("gpuId")] = str(g.get("stockStatus") or "").lower()
    return out


def gpu_prices():
    out = dict(FALLBACK_PRICE)
    try:
        for g in as_list(rp("gpu", "list")):
            if g.get("gpuId") and g.get("securePricePerHr"):
                out[g["gpuId"]] = float(g["securePricePerHr"])
    except GuardError:
        pass
    return out


def pod_price(pod, st):
    for k in ("adjustedCostPerHr", "costPerHr"):
        if pod.get(k):
            try:
                return float(pod[k])
            except (TypeError, ValueError):
                pass
    return st.get("price_per_hr")


def pod_start_time(pod, st):
    """本次运行的起点：优先 RunPod 返回的 lastStartedAt / uptime，其次 gpu-up 记录的时间。"""
    t = parse_time(pod.get("lastStartedAt"))
    if t:
        return t
    up = (pod.get("runtime") or {}).get("uptimeInSeconds")
    if up:
        return now() - dt.timedelta(seconds=float(up))
    return parse_time(st.get("created_at"))


# ---------------------------------------------------------------- SSH
def ssh_base(pod_id):
    """拿到 pod 的 SSH 命令（列表形式）。拿不到返回 None。"""
    override = os.environ.get("ATLAS_GPU_SSH")
    if override:
        return shlex.split(override.replace("{pod}", pod_id))
    try:
        info = rp("ssh", "info", pod_id, timeout=30)
    except GuardError:
        return None
    cmd = None
    if isinstance(info, dict):
        for k in ("sshCommand", "ssh_command", "command"):
            if info.get(k):
                cmd = info[k]
                break
        if not cmd and info.get("ip") and info.get("port"):
            cmd = f"ssh root@{info['ip']} -p {info['port']}"
            if info.get("key") or info.get("keyPath"):
                cmd += f" -i {info.get('key') or info.get('keyPath')}"
    elif isinstance(info, str):
        m = re.search(r"ssh\s+\S.*", info)
        cmd = m.group(0).strip() if m else None
    if not cmd:
        return None
    parts = shlex.split(cmd)
    opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={HOME / '.ssh/known_hosts_runpod'}"]
    return [parts[0], *opts, *parts[1:]]


def ssh_command_str(pod_id):
    base = ssh_base(pod_id)
    if not base:
        return None
    # 给人看的版本：去掉巡检专用的选项
    out, skip = [], False
    for a in base:
        if skip:
            skip = False
            continue
        if a == "-o":
            skip = True
            continue
        out.append(a)
    return " ".join(shlex.quote(a) for a in out)


def probe_gpu(pod_id):
    """SSH 到 pod 采样 5 秒 GPU 利用率，顺便看 busy 标记。
    返回 (util_max 或 None, busy: bool, 说明)。"""
    base = ssh_base(pod_id)
    if not base:
        return None, False, "拿不到 SSH 信息"
    remote = (
        "for i in 1 2 3 4 5; do nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits; sleep 1; done"
        f" | sort -n | tail -1; if [ -f {BUSY_MARKER} ]; then echo BUSY_AGE=$(( $(date +%s) - $(stat -c %Y {BUSY_MARKER}) )); fi"
    )
    try:
        r = subprocess.run([*base, remote], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return None, False, "SSH 超时"
    if r.returncode != 0:
        return None, False, f"SSH 失败 rc={r.returncode}"
    util, busy = None, False
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("BUSY_AGE="):
            try:
                busy = int(line.split("=", 1)[1]) <= BUSY_FRESH_MINUTES * 60
            except ValueError:
                pass
        elif re.fullmatch(r"\d+(\.\d+)?", line):
            util = max(util or 0, float(line))
    return util, busy, "ok" if util is not None else "nvidia-smi 无输出"


# ---------------------------------------------------------------- gpu-up
def pick_gpu(want, stock):
    """按白名单 + 库存挑 GPU。A100 有货时不允许 H100。"""
    if want not in GPU_ALIASES:
        raise GuardError(f"GPU '{want}' 不在白名单。可选: {', '.join(GPU_ALIASES)}")
    a100_in_stock = [g for g in A100_IDS if stock.get(g) in IN_STOCK]
    candidates = GPU_ALIASES[want]
    if any(g in H100_IDS for g in candidates) and not any(g in A100_IDS for g in candidates):
        if a100_in_stock:
            raise GuardError(f"A100 80GB 当前有货（{', '.join(a100_in_stock)}），规则不允许改用 H100")
    for g in candidates:
        if stock.get(g) in IN_STOCK:
            return g
    raise GuardError("Network Volume 所在数据中心暂无可用的白名单 GPU：" +
                     ", ".join(f"{g}={stock.get(g) or 'none'}" for g in candidates))


def cmd_up(a):
    if not (0 < a.hours <= MAX_HOURS):
        raise GuardError(f"--hours 必须在 (0, {MAX_HOURS:g}] 之间")
    name = a.name if (a.name or "").startswith(PREFIX) else PREFIX + (a.name or now().strftime("%Y%m%d-%H%M"))
    if not re.fullmatch(r"atlas-[A-Za-z0-9_-]{1,40}", name):
        raise GuardError(f"pod 名字不合法: {name}")

    running = [p for p in atlas_pods() if pod_running(p)]
    if len(running) >= MAX_PODS:
        raise GuardError(f"已有 {len(running)} 个 atlas- pod 在运行（上限 {MAX_PODS}）：" +
                         ", ".join(p.get("name", "?") for p in running) + "。先 gpu-down。")

    vol = find_volume()
    if not vol:
        if not a.dry_run:
            raise GuardError(f"找不到 Network Volume '{VOLUME_NAME}'。请先在 RunPod 后台创建，"
                             f"或在 {CONFIG_FILE} 写入 network_volume_id。")
        vol = {"id": "<VOLUME_ID>", "name": VOLUME_NAME, "dc": a.dc_hint, "size": None}
        print(f"[dry-run] 还没有 Network Volume，假设建在 {a.dc_hint}")

    stock = stock_in_dc(vol["dc"])
    gpu = pick_gpu(a.gpu, stock)
    price = gpu_prices().get(gpu)
    est = price * a.hours if price else None
    deadline = now() + dt.timedelta(hours=a.hours)

    cmd = [RUNPODCTL, "pod", "create",
           "--name", name, "--gpu-id", gpu, "--gpu-count", "1",
           "--image", a.image, "--cloud-type", "SECURE",
           "--data-center-ids", vol["dc"], "--network-volume-id", vol["id"],
           "--volume-mount-path", MOUNT_PATH,
           "--container-disk-in-gb", str(a.disk), "--ports", "22/tcp,8888/http"]

    print(f"名字     : {name}")
    print(f"GPU      : {gpu}（库存 {stock.get(gpu)}）")
    print(f"数据中心 : {vol['dc']}，Volume {vol['name']} → {MOUNT_PATH}")
    print(f"镜像     : {a.image}")
    print(f"时长上限 : {a.hours:g} h（到 {iso(deadline)} 自动 stop）")
    print(f"估算花费 : ${price:.2f}/h × {a.hours:g} h ≈ ${est:.2f}（上限）" if price else "估算花费 : 价格未知")
    if a.dry_run:
        print("[dry-run] 将执行: " + " ".join(shlex.quote(c) for c in cmd))
        return 0

    created = rp(*cmd[1:], timeout=180)
    pod = as_list(created)[0] if as_list(created) else {}
    pod_id = pod.get("id")
    if not pod_id:  # 兜底：按名字再查一次
        pod_id = next((p.get("id") for p in atlas_pods() if p.get("name") == name), None)
    if not pod_id:
        raise GuardError("pod create 没返回 ID，请立刻 gpu-status 检查！")
    save_state(pod_id, {"name": name, "gpu": gpu, "price_per_hr": price, "max_hours": a.hours,
                        "created_at": iso(now()), "deadline": iso(deadline), "idle_since": None})
    log(f"UP {pod_id} {name} gpu={gpu} max_hours={a.hours:g} price={price}")

    print(f"\npod ID   : {pod_id}")
    ssh = None
    t0 = time.time()
    while time.time() - t0 < a.wait * 60:
        ssh = ssh_command_str(pod_id)
        if ssh:
            break
        time.sleep(15)
    print(f"SSH      : {ssh}" if ssh else f"SSH      : 暂未就绪，稍后运行 runpodctl ssh info {pod_id}")
    return 0


# ---------------------------------------------------------------- gpu-down
def resolve_target(target):
    pods = atlas_pods()
    hits = [p for p in pods if p.get("id") == target or p.get("name") == target]
    if not hits:
        others = [p for p in list_pods() if p.get("id") == target or p.get("name") == target]
        if others:
            raise GuardError(f"{target} 不是 atlas- 前缀的 pod，拒绝操作")
        raise GuardError(f"找不到 atlas- pod: {target}")
    if len(hits) > 1:
        raise GuardError(f"{target} 匹配到多个 pod，请用 pod ID")
    return hits[0]


def stop_pod(pod, reason, dry_run=False, delete=False):
    pid, name = pod.get("id"), pod.get("name")
    if not is_atlas(pod):  # 双保险
        raise GuardError(f"拒绝操作非 atlas- pod: {name}")
    action = "delete" if delete else "stop"
    if dry_run:
        log(f"[dry-run] 会 {action} {pid} {name}：{reason}")
        return
    rp("pod", action, pid, timeout=120)
    log(f"{action.upper()} {pid} {name}：{reason}")
    if delete:
        state_path(pid).unlink(missing_ok=True)


def cmd_down(a):
    targets = atlas_pods() if a.all else [resolve_target(a.target)]
    if a.all and not a.delete:
        targets = [p for p in targets if pod_running(p)]
    if not targets:
        print("没有需要处理的 atlas- pod")
    for p in targets:
        stop_pod(p, "手动 gpu-down", dry_run=a.dry_run, delete=a.delete)
    return 0


# ---------------------------------------------------------------- gpu-status
def cmd_status(a):
    pods = atlas_pods()
    if not pods:
        print("没有 atlas- pod")
        return 0
    total = 0.0
    print(f"{'名字':<22}{'状态':<10}{'GPU':<24}{'已运行':>8}{'$/h':>7}{'已花费':>9}{'距自动关机':>12}  空闲")
    for p in pods:
        st = load_state(p.get("id"))
        price = pod_price(p, st)
        start = pod_start_time(p, st)
        run = pod_running(p)
        elapsed = (now() - start).total_seconds() if (start and run) else None
        cost = price * elapsed / 3600 if (price and elapsed) else None
        total += cost or 0
        max_h = float(st.get("max_hours") or MAX_HOURS)
        left = (max_h * 3600 - elapsed) if elapsed is not None else None
        idle = st.get("idle_since")
        idle_txt = f"空闲 {fmt_dur((now() - parse_time(idle)).total_seconds())}" if idle else "-"
        gpu = st.get("gpu") or (p.get("machine") or {}).get("gpuDisplayName") or p.get("gpuTypeId") or "?"
        print(f"{p.get('name', '?'):<22}{str(p.get('desiredStatus')):<10}{gpu[:23]:<24}"
              f"{fmt_dur(elapsed) if run else '-':>8}{(f'{price:.2f}' if price else '?'):>7}"
              f"{(f'${cost:.2f}' if cost is not None else '-'):>9}{(fmt_dur(left) if run else '-'):>12}  {idle_txt}")
        if a.verbose:
            print(f"    id={p.get('id')}")
    print(f"本次运行累计估算: ${total:.2f}（只算正在运行的 pod，按 $/h × 本次运行时长）")
    return 0


# ---------------------------------------------------------------- gpu-guard（巡检）
def cmd_guard(a):
    try:
        pods = atlas_pods()
    except GuardError as e:
        log(f"ERROR 拿不到 pod 列表：{e}")
        return 1
    running = [p for p in pods if pod_running(p)]
    if not running:
        if a.verbose:
            log("巡检：没有运行中的 atlas- pod")
        return 0
    rc = 0
    for p in running:
        pid, name = p.get("id"), p.get("name")
        st = load_state(pid)
        if not st:  # 不是 gpu-up 开的 → 按最严格的默认值管
            st = {"name": name, "max_hours": MAX_HOURS, "created_at": iso(now()), "idle_since": None,
                  "note": "非 gpu-up 创建，按默认 6h 管理"}
            log(f"WARN {pid} {name} 没有 gpu-up 记录，按 {MAX_HOURS:g}h 上限管理")
        start = pod_start_time(p, st)
        elapsed = (now() - start).total_seconds() if start else 0
        max_h = min(float(st.get("max_hours") or MAX_HOURS), MAX_HOURS)
        try:
            # 1) 超时
            if elapsed >= max_h * 3600:
                stop_pod(p, f"超过时长上限 {max_h:g}h（已运行 {fmt_dur(elapsed)}）", dry_run=a.dry_run)
                continue
            # 2) GPU 空闲
            util, busy, why = probe_gpu(pid)
            in_grace = elapsed < IDLE_MINUTES * 60
            if util is not None and (util > 0 or busy):
                st["idle_since"] = None
                state = f"util={util:g}%" + (" busy" if busy else "")
            elif util is None and in_grace:
                state = f"探测失败（{why}），开机 {IDLE_MINUTES} 分钟内不计空闲"
            else:
                # util == 0，或开机超过 30 分钟仍探测不到（SSH 不通的 pod 同样在白烧钱）
                st["idle_since"] = st.get("idle_since") or iso(now())
                idle_for = (now() - parse_time(st["idle_since"])).total_seconds()
                state = (f"util=0%" if util is not None else f"探测失败（{why}）") + f"，已空闲 {fmt_dur(idle_for)}"
                if idle_for >= IDLE_MINUTES * 60 - 30:  # 留 30s 余量，避免 cron 抖动多等一轮
                    stop_pod(p, f"GPU 连续空闲 ≥{IDLE_MINUTES} 分钟（{state}）", dry_run=a.dry_run)
                    continue
            log(f"巡检 {pid} {name}: 已运行 {fmt_dur(elapsed)}/{max_h:g}h，{state}", also_print=a.verbose)
        except GuardError as e:
            log(f"ERROR {pid} {name}: {e}")
            rc = 1
        finally:
            save_state(pid, st)
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
    up.add_argument("--dc-hint", default="US-KS-2", help="dry-run 且没有 Volume 时假设的数据中心")
    up.add_argument("--dry-run", action="store_true")

    dn = sp.add_parser("down", help="stop / delete atlas- pod")
    dn.add_argument("target", nargs="?", help="pod ID 或名字")
    dn.add_argument("--all", action="store_true", help="所有 atlas- pod")
    dn.add_argument("--delete", action="store_true", help="删除（默认只 stop）")
    dn.add_argument("--dry-run", action="store_true")

    stp = sp.add_parser("status", help="查看 atlas- pod")
    stp.add_argument("-v", "--verbose", action="store_true", help="显示 pod ID")

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
