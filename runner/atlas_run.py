#!/usr/bin/env python3
"""atlas-run: server-side generic task runner.

Flow: find the running atlas- pod → rsync the pod/ scripts and task input → run pod/run_task.sh
      in the background on the pod → poll progress → pull small previews and metrics back to the server's ~/runs/<run_id>/.
Large files (data, weights, original videos) never pass through the server: tasks read and write the Global volume directly on the pod.

Usage:
  atlas-run [--input DIR] [--name NAME] [--detach] <task> [args...]
      task = pod/tasks/<task>.sh, e.g.: selftest, setup gen3c vipe 4c4d
  atlas-run --attach <run_id>   keep following a run
  atlas-run --fetch <run_id>    only pull back results
  atlas-run --list              list runs already on the server
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

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "guard"))
import atlas_gpu as g  # noqa: E402  reuse the guard's pod lookup and SSH config

RUNS = Path(os.environ.get("ATLAS_RUNS", Path.home() / "runs"))
HF_TOKEN_FILE = Path.home() / ".config/atlas-gpu/hf_token"
POD_ROOT = os.environ.get("ATLAS_POD_ROOT", "/opt/atlas")
# Test mode: no pod connection, the "pod" is a local directory (only for runner/tests, which run tasks that need no GPU)
LOCAL_MODE = os.environ.get("ATLAS_RUN_LOCAL") == "1"
PULL_MAX_SIZE = "20M"   # size limit per pulled file: previews and metrics only
POLL_SECONDS = int(os.environ.get("ATLAS_RUN_POLL", 30))


def die(msg):
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(2)


def the_pod():
    if LOCAL_MODE:
        return {"id": "local", "name": "atlas-local-test", "gpu": "none", "cost": 1.0, "status": "RUNNING"}
    pods, src = g.atlas_pods()
    running = [p for p in pods if p["status"] == "RUNNING"]
    if not running:
        die("No running atlas- pod (run gpu-up first)")
    p = running[0]
    if not g.ssh_base(p):
        die(f"pod {p['name']} has no SSH info yet (source: {src}), try again later")
    return p


def ssh_parts(pod):
    """Split into the form rsync needs: (ssh program and options, user@host)."""
    base = g.ssh_base(pod)
    target = next(a for a in base[1:] if "@" in a)
    opts = [a for a in base if a != target]
    return opts, target


def ssh(pod, remote, check=True, capture=False, stdin=None, timeout=None):
    base = ["bash", "-c"] if LOCAL_MODE else g.ssh_base(pod)
    r = subprocess.run([*base, remote], text=True, input=stdin, timeout=timeout,
                       capture_output=capture)
    if check and r.returncode != 0:
        die(f"Command failed on pod (rc={r.returncode}): {remote[:80]}")
    return r


def rsync(pod, src, dst, extra=()):
    if LOCAL_MODE:
        r = subprocess.run(["rsync", "-rt", *extra, src.replace("POD:", ""), dst.replace("POD:", "")])
        if r.returncode != 0:
            die(f"rsync failed: {src} → {dst}")
        return
    opts, target = ssh_parts(pod)
    rsh = " ".join(shlex.quote(o) for o in opts)
    src = src.replace("POD:", f"{target}:")
    dst = dst.replace("POD:", f"{target}:")
    r = subprocess.run(["rsync", "-rtz", "--partial", "-e", rsh, *extra, src, dst])
    if r.returncode != 0:
        die(f"rsync failed: {src} → {dst}")


def prepare_pod(pod):
    if not LOCAL_MODE:
        ssh(pod, "command -v rsync >/dev/null || (DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
                 "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rsync zstd >/dev/null)")
    ssh(pod, f"mkdir -p {POD_ROOT}/repo {POD_ROOT}/work {POD_ROOT}/bin {POD_ROOT}/secrets && chmod 700 {POD_ROOT}/secrets")
    rsync(pod, f"{REPO}/pod", f"POD:{POD_ROOT}/repo/", ["--delete", "--exclude", "__pycache__"])
    ssh(pod, f"install -m 755 {POD_ROOT}/repo/pod/pre_stop.sh {POD_ROOT}/bin/pre_stop")
    # HF token: passed only via SSH stdin, never on the command line, in logs or in the environment
    if HF_TOKEN_FILE.is_file() and HF_TOKEN_FILE.stat().st_size > 0:
        ssh(pod, f"umask 077; cat > {POD_ROOT}/secrets/hf_token", stdin=HF_TOKEN_FILE.read_text())


def start(a):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", a.task) or not (REPO / "pod/tasks" / f"{a.task}.sh").exists():
        die(f"No such task: pod/tasks/{a.task}.sh")
    run_id = f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{a.name or a.task}"
    pod = the_pod()
    print(f"pod {pod['name']} ({pod.get('gpu')}), run {run_id}")
    prepare_pod(pod)
    rd = f"{POD_ROOT}/work/{run_id}"
    ssh(pod, f"mkdir -p {rd}/task {rd}/out")
    if a.input:
        rsync(pod, f"{Path(a.input).resolve()}/", f"POD:{rd}/task/")
    local = RUNS / run_id
    local.mkdir(parents=True, exist_ok=True)
    meta = {"run_id": run_id, "task": a.task, "args": a.args, "pod": pod["name"], "gpu": pod.get("gpu"),
            "cost_per_hr": pod.get("cost"), "started": g.iso(g.now())}
    (local / "run.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    args = " ".join(shlex.quote(x) for x in a.args)
    # Register with the guard: after this run finishes (plus a 5-minute grace period) the cron patrol stops the pod,
    # regardless of whether this session is still alive.
    # Done before starting the remote run: the launch SSH occasionally hangs and is killed by the timeout, so registering afterwards might never happen.
    if not LOCAL_MODE:
        st = g.load_state(pod["id"])
        if a.keep_alive:
            st["stop_after_runs"] = []
            print("--keep-alive: the pod will not be auto-stopped after the run (time limit and idle detection still apply)")
        else:
            st["stop_after_runs"] = (st.get("stop_after_runs") or [])[-9:] + [run_id]
            print(f"{g.DONE_GRACE_MINUTES} minutes after the run finishes, the patrol will stop the pod automatically (add --keep-alive to prevent this)")
        g.save_state(pod["id"], st)
    # setsid: put the run in its own session so an SSH disconnect sends no SIGHUP and the launch command returns immediately (in practice ssh hangs with nohup alone)
    ssh(pod, f"cd {rd} && setsid nohup bash {POD_ROOT}/repo/pod/run_task.sh {rd} {a.task} {args} > {rd}/run.log 2>&1 < /dev/null & "
             f"echo $! > {rd}/pid", timeout=120)
    print(f"Started on the pod. Log: {rd}/run.log")
    if a.detach:
        print(f"Follow: atlas-run --attach {run_id}")
        return 0
    return attach(run_id, pod)


def attach(run_id, pod=None):
    pod = pod or the_pod()
    rd = f"{POD_ROOT}/work/{run_id}"
    shown = 0
    t0 = time.time()
    while True:
        r = ssh(pod, f"cat {rd}/status 2>/dev/null; echo ---; tail -n +{shown + 1} {rd}/run.log 2>/dev/null",
                check=False, capture=True, timeout=60)
        if r.returncode != 0:
            print(f"[{int(time.time() - t0)}s] SSH temporarily unreachable, retrying in {POLL_SECONDS}s")
            time.sleep(POLL_SECONDS)
            continue
        status, _, new = r.stdout.partition("---\n")
        lines = [l for l in new.splitlines()]
        for l in lines[-40:]:
            print("  " + l)
        shown += len(lines)
        status = status.strip()
        if status and status != "running":
            print(f"Run finished: {status}")
            fetch(run_id, pod)
            return 0 if status == "done" else 1
        time.sleep(POLL_SECONDS)


def fetch(run_id, pod=None):
    pod = pod or the_pod()
    local = RUNS / run_id
    local.mkdir(parents=True, exist_ok=True)
    rsync(pod, f"POD:{POD_ROOT}/work/{run_id}/out/", f"{local}/out/",
          ["--max-size", PULL_MAX_SIZE, "--exclude", "*.pt", "--exclude", "*.pth", "--exclude", "*.ply",
           "--exclude", "full/"])  # full/: full-resolution videos, renders, 4C4D models; these stay on the Global volume only
    meta_p = local / "run.json"
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else {"run_id": run_id}
    m_p = local / "out/metrics.json"
    if m_p.exists():
        m = json.loads(m_p.read_text())
        meta["metrics"] = m
        if meta.get("cost_per_hr") and m.get("seconds") is not None:
            meta["gpu_cost_usd"] = round(meta["cost_per_hr"] * m["seconds"] / 3600, 3)
    meta_p.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    print(f"Results pulled to {local}/out/ (each file ≤ {PULL_MAX_SIZE}; full results on the Global volume at results/{run_id}/)")
    if "gpu_cost_usd" in meta:
        print(f"GPU cost of this run ≈ ${meta['gpu_cost_usd']} ({meta['metrics']['seconds']}s × ${meta['cost_per_hr']}/h)")
    return 0


def main():
    sys.stdout.reconfigure(line_buffering=True)  # flush line by line even when redirected to a file, for easier following
    ap = argparse.ArgumentParser(prog="atlas-run", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", help="task input directory pushed to the pod (small files)")
    ap.add_argument("--name", help="run name suffix")
    ap.add_argument("--detach", action="store_true", help="do not follow after starting")
    ap.add_argument("--keep-alive", action="store_true", help="do not auto-stop the pod after the run (stopped by default)")
    ap.add_argument("--attach", metavar="RUN_ID")
    ap.add_argument("--fetch", metavar="RUN_ID")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("task", nargs="?")
    ap.add_argument("args", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    if a.list:
        for d in sorted(RUNS.glob("*/run.json")):
            m = json.loads(d.read_text())
            print(f"{m['run_id']:<40} {m.get('task', ''):<10} {m.get('metrics', {}).get('exit_code', '?')!s:<4} ${m.get('gpu_cost_usd', '?')}")
        return 0
    if a.attach:
        return attach(a.attach)
    if a.fetch:
        return fetch(a.fetch)
    if not a.task:
        ap.error("task name required")
    try:
        return start(a)
    except g.GuardError as e:
        die(str(e))


if __name__ == "__main__":
    sys.exit(main())
