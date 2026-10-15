#!/usr/bin/env python3
"""ATLAS GPU guard: every RunPod pod start / stop / status / patrol goes through here.

Entry points (symlinks in ~/bin):
  gpu-up      start a pod (whitelisted GPU, atlas- prefix, at most 1, Global volume atlas_data mounted, time-limited)
  gpu-down    stop / delete atlas- prefixed pods
  gpu-status  list atlas- pods, uptime, estimated cost, time until auto-stop
  gpu-guard   patrol (cron, every 10 minutes): over time limit → stop; GPU idle for 30 minutes → stop

Backends
- Balance: the v2 API has no balance endpoint, so it is read via runpodctl user; refuse to start a pod if
  the balance is < $3 or unreadable (Joey's decision 2026-10-14, previously $15).
- Main path: RunPod REST API v2 (https://api.runpod.io/v2). Global volume mounts, live GPU utilization and
  SSH info are only available in v2. The API key is a separately created restricted key stored in
  ~/.config/atlas-gpu/runpod_api_key; it is only read into memory when sending a request, never printed or logged.
- Fallback: when the API is unavailable, the patrol falls back to runpodctl (which reads ~/.runpod itself;
  this script never touches it) and only does listing + time-limit stop.

Runtime state (pod ID, deadline, idle timer) lives in ~/.local/state/atlas-gpu/, not in the repo.
Test environment variables: ATLAS_RUNPOD_API (fake API URL), RUNPODCTL, ATLAS_GPU_SSH, ATLAS_GPU_* paths.
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
# The Global volume is identified by ID only (config global_volume_id); the name is for display only -- it may contain slashes and is not guaranteed unique
VOLUME_LABEL_DEFAULT = "atlas_data"
MIN_BALANCE = 3.0  # Joey's decision 2026-10-14 (previously $15). Refuse to start a pod below this balance: 15 days after the balance hits zero the Global volume is permanently deleted
# Mount path of the Global volume inside the pod: set explicitly at pod creation and passed in as env var ATLAS_VOL.
# All pod scripts use $ATLAS_VOL only, never a hard-coded /workspace (with a network volume also attached the default becomes /workspace-global).
VOL_MOUNT = os.environ.get("ATLAS_VOL_MOUNT", "/workspace")
# Container disk (local NVMe, wiped on pod stop): conda envs, work dirs, intermediate files, checkpoints
LOCAL_ROOT = "/opt/atlas"
BUSY_MARKER = f"{LOCAL_ROOT}/.atlas_busy"   # touched periodically by the task runner; while fresh, GPU at 0 does not count as idle
PRE_STOP = f"{LOCAL_ROOT}/bin/pre_stop"    # best-effort run once before stop: sync results to keep back to $ATLAS_VOL
PRE_STOP_TIMEOUT = 600
BUSY_FRESH_MINUTES = 15

MAX_PODS = 1
DEFAULT_HOURS = 6.0
MAX_HOURS = 6.0
IDLE_MINUTES = 30
# Utilization below this counts as idle: leftover processes report 1-2%, and a >0 check resets the idle timer (observed 2026-10-14: pod sat idle 35 minutes without being stopped)
IDLE_UTIL_PCT = 5
# Auto-stop once all runs registered by atlas-run have finished and the last one finished this long ago (leaves time for a follow-up run)
DONE_GRACE_MINUTES = 5
DEFAULT_IMAGE = "runpod/pytorch:1.0.3-cu1281-torch291-ubuntu2404"
MIN_CUDA = "12.8"  # the image above is cu128, so the host driver must support at least 12.8
DEFAULT_CONTAINER_DISK_GB = 200

# Whitelist: prefer A100 80GB; H100 is only allowed when both A100 types are out of stock in all regions (or all fail to start)
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
ACTIVE = {"PROVISIONING", "STARTING", "RUNNING"}  # all of these states are billed


# ---------------------------------------------------------------- helpers
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
        raise NoApiKey(f"API key file {API_KEY_FILE} not found")
    if API_KEY_FILE.stat().st_mode & 0o077:
        raise GuardError(f"{API_KEY_FILE} permissions are too open, please chmod 600")
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
        raise GuardError(f"API {method} {path} connection failed: {getattr(e, 'reason', e)}")
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


# ---------------------------------------------------------------- runpodctl fallback
def ctl(*args, timeout=60):
    try:
        r = subprocess.run([RUNPODCTL, *args], capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise GuardError(f"runpodctl {' '.join(args[:2])} failed: {e}")
    if r.returncode != 0:
        raise GuardError(f"runpodctl {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[:300]}")
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


# ---------------------------------------------------------------- unified interface
def list_pods(allow_fallback=True):
    """Return (pods, source). Falls back to runpodctl if the API fails and fallback is allowed."""
    try:
        return [norm_v2(p) for p in as_list(api("GET", "/v2/pods"), "pods")], "api"
    except GuardError as e:
        if not allow_fallback:
            raise
        if not isinstance(e, NoApiKey):
            log(f"WARN API unavailable ({e}), falling back to runpodctl", also_print=False)
        return [norm_ctl(p) for p in as_list(ctl("pod", "list", "--all"))], "runpodctl"


def atlas_pods(allow_fallback=True):
    pods, src = list_pods(allow_fallback)
    return [p for p in pods if p["name"].startswith(PREFIX)], src


def pod_action(pod, action):
    """action: stop / delete. Only atlas- pods are allowed."""
    if not pod["name"].startswith(PREFIX):
        raise GuardError(f"Refusing to act on non-atlas- pod: {pod['name']}")
    try:
        if action == "stop":
            api("POST", f"/v2/pods/{pod['id']}/action", body={"action": "stop"})
        else:
            api("DELETE", f"/v2/pods/{pod['id']}")
    except GuardError as e:
        log(f"WARN API {action} failed ({e}), falling back to runpodctl", also_print=False)
        ctl("pod", action, pod["id"], timeout=120)


def volume_config():
    """Return (volume ID, display name). Errors out if no ID is configured; never guesses by name."""
    cfg = load_config()
    vid = str(cfg.get("global_volume_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", vid):
        raise GuardError(f"No valid global_volume_id in {CONFIG_FILE}")
    return vid, cfg.get("global_volume_label") or VOLUME_LABEL_DEFAULT


def find_global_volume():
    """Confirm via the v2 API that the Global volume with the configured ID exists."""
    vid, _ = volume_config()
    try:
        v = api("GET", f"/v2/global-volumes/{urllib.parse.quote(vid, safe='')}")
    except GuardError as e:
        if "HTTP 404" in str(e):
            return None
        raise
    return v if isinstance(v, dict) and v.get("id") == vid else None


def get_balance():
    """Account balance (USD). The v2 API has no balance endpoint, so use runpodctl user (it reads its own key). Returns None if unreadable."""
    try:
        u = ctl("user", timeout=30)
    except GuardError:
        return None
    if isinstance(u, dict):
        for k in ("clientBalance", "balance"):
            if u.get(k) is not None:
                try:
                    return float(u[k])
                except (TypeError, ValueError):
                    pass
    return None


def gpu_availability(gpu_ids):
    """Return {gpuId: (availability, price, [dc...], [cuda...])} across all regions."""
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
    # No key (only reachable in dry-run): approximate with runpodctl's stock data
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
    """Return the order to try based on whitelist + stock. H100 is not allowed while A100 is in stock."""
    if want not in GPU_ALIASES:
        raise GuardError(f"GPU '{want}' is not whitelisted. Options: {', '.join(GPU_ALIASES)}")
    ok = lambda g: avail.get(g, ("NONE",))[0] != "NONE"
    a100_ok = [g for g in A100_IDS if ok(g)]
    cands = GPU_ALIASES[want]
    if a100_ok and not any(g in A100_IDS for g in cands):
        raise GuardError(f"A100 80GB is currently in stock ({', '.join(a100_ok)}); the rules do not allow H100")
    order = [g for g in cands if g in A100_IDS and ok(g)]
    if not order:  # only when all A100s are out of stock → allow H100
        order = [g for g in cands if g in H100_IDS and ok(g)]
    if not order:
        raise GuardError("No whitelisted GPU available in any region: " +
                         ", ".join(f"{g}={avail.get(g, ('NONE',))[0]}" for g in cands))
    return order


# ---------------------------------------------------------------- state files
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
    """Start of the current run: RunPod startedAt / uptime → creation time → gpu-up record."""
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
        return None, "no SSH info"
    try:
        r = subprocess.run([*base, remote], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "SSH timeout"
    if r.returncode != 0:
        return None, f"SSH failed rc={r.returncode}"
    return r.stdout, "ok"


def probe_gpu(pod):
    """Sample GPU utilization over 5 seconds via SSH + check the busy marker. Returns (util or None, busy, note)."""
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
    return util, busy, "ok" if util is not None else "no nvidia-smi output"


def runs_finished(pod, runs):
    """Check the status of runs registered by atlas-run via SSH. Returns (True, note) if all have finished and the
    most recent one finished ≥ DONE_GRACE_MINUTES ago; (False, note) if still running, unknown or just finished."""
    q = "; ".join(
        f'f=/opt/atlas/work/{r}/status; if [ -f $f ]; then echo "{r} $(head -1 $f | cut -d" " -f1) $(( $(date +%s) - $(stat -c %Y $f) ))"; else echo "{r} missing 0"; fi'
        for r in runs if re.fullmatch(r"[A-Za-z0-9_-]+", r))
    out, why = ssh_run(pod, ": ATLAS_RUNS_QUERY; " + q)
    if out is None:
        return False, f"cannot read run status ({why})"
    ages, states = [], []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        states.append(parts[1])
        ages.append(int(parts[2]) if parts[2].isdigit() else 0)
    if not states or any(x in ("running", "missing") for x in states):
        return False, "runs in progress: " + ",".join(states)
    if min(ages) < DONE_GRACE_MINUTES * 60:
        return False, f"run just finished {min(ages)}s ago"
    return True, f"registered runs all finished ({','.join(states)}), no new run for {min(ages) // 60} minutes"


FETCH_TIMEOUT = 300


def fetch_runs(pod):
    """Before stopping, pull the results of runs registered by atlas-run (small files ≤20MB) back to the server's ~/runs/.
    Once the pod is stopped the Global volume can only be read by starting another pod, so this must happen before
    the stop (2026-10-14: the session dropped and the pod was stopped before results were pulled back)."""
    runs = load_state(pod["id"]).get("stop_after_runs") or []
    cmd = os.environ.get("ATLAS_GPU_FETCH_CMD") or \
        f"{sys.executable} {Path(__file__).resolve().parent.parent / 'runner' / 'atlas_run.py'} --fetch"
    for r in runs:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", r):
            continue
        try:
            res = subprocess.run(shlex.split(cmd) + [r], capture_output=True, text=True, timeout=FETCH_TIMEOUT)
            log(f"FETCH {r}: rc={res.returncode}", also_print=False)
        except subprocess.TimeoutExpired:
            log(f"FETCH {r}: timeout", also_print=False)


def stop_pod(pod, reason, dry_run=False, delete=False):
    if not pod["name"].startswith(PREFIX):  # belt and braces
        raise GuardError(f"Refusing to act on non-atlas- pod: {pod['name']}")
    action = "delete" if delete else "stop"
    if dry_run:
        log(f"[dry-run] would {action} {pod['id']} {pod['name']}: {reason}")
        return
    if pod["status"] == "RUNNING":
        # The container disk is wiped on stop: first try to sync results back to the Global volume (with a timeout; stop anyway on failure)
        out, why = ssh_run(pod, f"[ -x {PRE_STOP} ] && timeout {PRE_STOP_TIMEOUT} {PRE_STOP} || true",
                           timeout=PRE_STOP_TIMEOUT + 30)
        log(f"PRE_STOP {pod['id']} {pod['name']}: {why}", also_print=False)
        fetch_runs(pod)
    pod_action(pod, action)
    log(f"{action.upper()} {pod['id']} {pod['name']}: {reason}")
    if delete:
        state_path(pod["id"]).unlink(missing_ok=True)


# ---------------------------------------------------------------- gpu-up
def pod_public_keys():
    """SSH public keys injected into the pod: this server's dedicated key + public keys registered on the account (keeps Joey's own login working)."""
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
        raise GuardError(f"--hours must be in (0, {MAX_HOURS:g}]")
    name = a.name if (a.name or "").startswith(PREFIX) else PREFIX + (a.name or now().strftime("%Y%m%d-%H%M"))
    if not re.fullmatch(r"atlas-[A-Za-z0-9_-]{1,40}", name):
        raise GuardError(f"Invalid pod name: {name}")
    if not a.dry_run and not have_api_key():
        raise GuardError(f"Starting a pod requires the RunPod API key file {API_KEY_FILE} (see guard/README.md)")
    if not a.dry_run and not SSH_KEY.exists():
        raise GuardError(f"Missing SSH key {SSH_KEY}; run guard/install.sh first")

    pods, src = atlas_pods(allow_fallback=a.dry_run)
    active = [p for p in pods if p["status"] in ACTIVE]
    if len(active) >= MAX_PODS:
        raise GuardError(f"{len(active)} atlas- pod(s) already running (limit {MAX_PODS}): " +
                         ", ".join(p["name"] for p in active) + ". Run gpu-down first.")

    # Balance check: refuse even if unreadable (better not to start than to let the balance hit zero and lose the Global volume)
    balance = get_balance()
    if balance is None:
        msg = "Cannot read account balance (runpodctl user failed)"
        if not a.dry_run:
            raise GuardError(msg + "; refusing to start a pod")
        print(f"[dry-run] {msg}; a real start would be refused")
    elif balance < MIN_BALANCE:
        msg = f"Account balance ${balance:.2f} < ${MIN_BALANCE:.0f}, please top up first (the Global volume is deleted 15 days after the balance hits zero)"
        if not a.dry_run:
            raise GuardError(msg)
        print(f"[dry-run] {msg}; a real start would be refused")

    vol_id, vol_label = volume_config()
    if have_api_key():
        vol = find_global_volume()
        if not vol:
            raise GuardError(f"v2 API cannot find Global volume {vol_id} ({vol_label})")
        vol_label = vol.get("name") or vol_label
        vol_src = "confirmed via v2 API"
    else:
        vol_src = "not confirmed via API (no API key)"
        print("[dry-run] No API key: stock comes from runpodctl, Global volume not confirmed via API")

    # Always check A100 stock whatever GPU is requested: H100 is not allowed while A100 is in stock
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
        # No dataCenterIds: the Global volume is not tied to a data center, so let the scheduler pick any region with stock

    def price_of(gpu):
        return avail.get(gpu, (None, None))[1] or FALLBACK_PRICE.get(gpu)

    deadline = now() + dt.timedelta(hours=a.hours)
    print(f"Name     : {name}")
    print("GPU order: " + " → ".join(f"{g} ({avail[g][0]}, {len(avail[g][2])} regions)" for g in order))
    print(f"Volume   : Global \"{vol_label}\" {vol_id} ({vol_src}) → {VOL_MOUNT} (in-pod variable $ATLAS_VOL)")
    print(f"Disk     : {a.disk} GB container disk ($ATLAS_LOCAL={LOCAL_ROOT}, wiped on stop)")
    print(f"Image    : {a.image}")
    print(f"Max time : {a.hours:g} h (auto-stop at {iso(deadline)})")
    p0 = price_of(order[0])
    print(f"Est. cost: ${p0:.2f}/h × {a.hours:g} h ≈ ${p0 * a.hours:.2f} (upper bound, excluding container disk and volume storage)" if p0 else "Est. cost: price unknown")
    if balance is not None:
        print(f"Balance  : ${balance:.2f} (minimum ${MIN_BALANCE:.0f})")
        if p0 and balance - p0 * a.hours < MIN_BALANCE:
            print(f"  ⚠️ After the full {a.hours:g} h the balance would drop below ${MIN_BALANCE:.0f}; consider topping up")
    if a.dry_run:
        shown = body_for(order[0])
        shown["env"] = {**env, "PUBLIC_KEY": f"<{len(pubkeys.splitlines())} public keys>"} if pubkeys else env
        print("[dry-run] Would POST /v2/pods:\n" + json.dumps(shown, indent=2, ensure_ascii=False))
        return 0

    pod, errors = None, []
    for gpu in order:
        try:
            pod = api("POST", "/v2/pods", body=body_for(gpu), timeout=120)
            break
        except GuardError as e:
            errors.append(f"{gpu}: {e}")
            print(f"  {gpu} could not be started: {e}")
    if pod is None and order[0] in A100_IDS and a.gpu == "auto":
        avail.update(gpu_availability(H100_IDS))
        h100 = [g for g in H100_IDS if avail.get(g, ("NONE",))[0] != "NONE"]
        if h100:
            print("  No A100 could be started; trying H100 per the rules")
            for gpu in h100:
                try:
                    pod = api("POST", "/v2/pods", body=body_for(gpu), timeout=120)
                    break
                except GuardError as e:
                    errors.append(f"{gpu}: {e}")
    if pod is None:
        raise GuardError("No pod was started: " + "; ".join(errors))

    pod = norm_v2(pod)
    save_state(pod["id"], {"name": name, "gpu": pod["gpu"], "price_per_hr": pod["cost"] or price_of(pod["gpu"]),
                           "max_hours": a.hours, "created_at": iso(now()), "deadline": iso(deadline),
                           "idle_since": None})
    log(f"UP {pod['id']} {name} gpu={pod['gpu']} max_hours={a.hours:g}")
    print(f"\nPod ID   : {pod['id']}")
    t0 = time.time()
    while time.time() - t0 < a.wait * 60 and not pod.get("ssh"):
        time.sleep(10)
        try:
            pod = norm_v2(api("GET", f"/v2/pods/{pod['id']}"))
        except GuardError:
            pass
    print(f"Region   : {pod.get('dc') or '?'}, status {pod['status']}")
    print(f"SSH      : {ssh_human(pod)}" if pod.get("ssh") else
          f"SSH      : not ready yet, check later with gpu-status -v")
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
                raise GuardError(f"{a.target} is not an atlas- prefixed pod; refusing")
            raise GuardError(f"atlas- pod not found: {a.target}")
        if len(targets) > 1:
            raise GuardError(f"{a.target} matches multiple pods; use the pod ID")
    if not targets:
        print("No atlas- pods to act on")
    for p in targets:
        stop_pod(p, "manual gpu-down", dry_run=a.dry_run, delete=a.delete)
    return 0


# ---------------------------------------------------------------- gpu-status
def cmd_status(a):
    pods, src = atlas_pods()
    if not pods:
        print(f"No atlas- pods (source: {src})")
        return 0
    total = 0.0
    print(f"{'Name':<22}{'Status':<13}{'GPU':<24}{'Uptime':>8}{'$/h':>7}{'Spent':>9}{'Auto-stop in':>13}  GPU%  Idle")
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
        idle_txt = f"idle {fmt_dur((now() - parse_time(idle)).total_seconds())}" if idle else "-"
        gpu = p["gpu"] or st.get("gpu") or "?"
        util = f"{p['util']:g}" if p["util"] is not None else "-"
        print(f"{p['name']:<22}{p['status']:<13}{gpu[:23]:<24}"
              f"{fmt_dur(elapsed) if act else '-':>8}{(f'{price:.2f}' if price else '?'):>7}"
              f"{(f'${cost:.2f}' if cost is not None else '-'):>9}{(fmt_dur(left) if act else '-'):>13}  {util:>4}  {idle_txt}")
        if a.verbose:
            print(f"    id={p['id']} region={p.get('dc') or '?'}")
            if ssh_human(p):
                print(f"    ssh: {ssh_human(p)}")
    print(f"Estimated total this run: ${total:.2f} (running pods only, $/h × current run time; source: {src})")
    bal = get_balance()
    if bal is not None:
        print(f"Account balance: ${bal:.2f}" + (f"  ⚠️ below ${MIN_BALANCE:.0f}, please top up" if bal < MIN_BALANCE else ""))
    return 0


# ---------------------------------------------------------------- gpu-guard (patrol)
BALANCE_LOG = Path(os.environ.get("ATLAS_GPU_BALANCE_LOG", HOME / "logs/balance.log"))


def log_balance(n_active):
    """Record the balance on every patrol, to estimate ongoing costs such as storage while no pod runs (2026-10-14: noticed the balance dropping with no pod running)."""
    bal = get_balance()
    if bal is None:
        return
    BALANCE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with BALANCE_LOG.open("a") as f:
        f.write(f"{iso(now())} {bal:.4f} active_atlas_pods={n_active}\n")


def cmd_guard(a):
    try:
        pods, src = atlas_pods()
    except GuardError as e:
        log(f"ERROR cannot get pod list (both API and runpodctl failed): {e}")
        return 1
    active = [p for p in pods if p["status"] in ACTIVE]
    if not a.dry_run:
        log_balance(len(active))
    if not active:
        if a.verbose:
            log(f"Patrol: no running atlas- pods ({src})")
        return 0
    rc = 0
    for p in active:
        st = load_state(p["id"])
        if not st:
            st = {"name": p["name"], "max_hours": MAX_HOURS, "created_at": iso(now()), "idle_since": None,
                  "note": "not created by gpu-up, managed with the default 6h"}
            log(f"WARN {p['id']} {p['name']} has no gpu-up record, managing with a {MAX_HOURS:g}h limit")
        # The API sometimes omits ssh info (observed 2026-10-14); remember the last command seen as a backup to avoid false idle detection
        if p.get("ssh"):
            st["ssh_cmd"] = p["ssh"]
        elif st.get("ssh_cmd"):
            p["ssh"] = st["ssh_cmd"]
        start = run_start(p, st)
        elapsed = (now() - start).total_seconds() if start else 0
        max_h = min(float(st.get("max_hours") or MAX_HOURS), MAX_HOURS)
        try:
            # 1) Over time limit: relies only on the pod list (API or runpodctl), no SSH needed
            if elapsed >= max_h * 3600:
                stop_pod(p, f"exceeded time limit {max_h:g}h (running {fmt_dur(elapsed)})", dry_run=a.dry_run)
                continue
            # 2) All runs registered by atlas-run have finished → stop now (does not depend on Claude's session still being alive)
            if st.get("stop_after_runs") and p["status"] == "RUNNING":
                done, why_done = runs_finished(p, st["stop_after_runs"])
                if done:
                    stop_pod(p, why_done, dry_run=a.dry_run)
                    continue
            # 3) Idle: API utilization ≥ IDLE_UTIL_PCT counts as busy; if lower or unavailable, double-check via SSH nvidia-smi + the busy marker
            util, busy, why = p["util"], False, "api"
            if util is None or util < IDLE_UTIL_PCT:
                s_util, busy, why = probe_gpu(p)
                if s_util is not None:
                    util = s_util
            in_grace = elapsed < IDLE_MINUTES * 60
            if util is not None and (util >= IDLE_UTIL_PCT or busy):
                st["idle_since"] = None
                state = f"util={util:g}%" + (" busy" if busy else "")
            elif util is None and (in_grace or p["status"] != "RUNNING"):
                state = f"probe failed ({why}), {p['status']}, not counted as idle within {IDLE_MINUTES} minutes of boot"
            else:
                st["idle_since"] = st.get("idle_since") or iso(now())
                idle_for = (now() - parse_time(st["idle_since"])).total_seconds()
                state = (f"util={util:g}%" if util is not None else f"probe failed ({why})") + f", idle for {fmt_dur(idle_for)}"
                if idle_for >= IDLE_MINUTES * 60 - 30:
                    stop_pod(p, f"GPU idle for ≥{IDLE_MINUTES} minutes ({state})", dry_run=a.dry_run)
                    continue
            log(f"Patrol {p['id']} {p['name']}: running {fmt_dur(elapsed)}/{max_h:g}h, {state} ({src})",
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

    up = sp.add_parser("up", help="start a pod")
    up.add_argument("--gpu", default="auto", help=f"{'/'.join(GPU_ALIASES)} (default auto: A100 first, H100 only if out of stock)")
    up.add_argument("--image", default=DEFAULT_IMAGE)
    up.add_argument("--hours", type=float, default=DEFAULT_HOURS, help=f"time limit in hours, default {DEFAULT_HOURS:g}, max {MAX_HOURS:g}")
    up.add_argument("--name", help="pod name (atlas- prefix added automatically)")
    up.add_argument("--disk", type=int, default=DEFAULT_CONTAINER_DISK_GB, help="container disk in GB")
    up.add_argument("--wait", type=float, default=10, help="minutes to wait for SSH to be ready")
    up.add_argument("--dry-run", action="store_true")

    dn = sp.add_parser("down", help="stop / delete atlas- pod")
    dn.add_argument("target", nargs="?", help="pod ID or name")
    dn.add_argument("--all", action="store_true", help="all atlas- pods")
    dn.add_argument("--delete", action="store_true", help="delete (default: stop only)")
    dn.add_argument("--dry-run", action="store_true")

    stp = sp.add_parser("status", help="show atlas- pods")
    stp.add_argument("-v", "--verbose", action="store_true", help="show pod ID, region, SSH")

    gd = sp.add_parser("guard", help="patrol (for cron)")
    gd.add_argument("--dry-run", action="store_true", help="only log what would be done, do not actually stop")
    gd.add_argument("-v", "--verbose", action="store_true")

    a = ap.parse_args(argv)
    if a.cmd == "down" and not (a.target or a.all):
        ap.error("gpu-down needs a pod ID / name, or --all")
    try:
        return {"up": cmd_up, "down": cmd_down, "status": cmd_status, "guard": cmd_guard}[a.cmd](a)
    except GuardError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
