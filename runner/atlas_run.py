#!/usr/bin/env python3
"""atlas-run：服务器端的通用任务执行器。

流程：找到正在运行的 atlas- pod → rsync 推送 pod/ 脚本和任务输入 → 在 pod 上后台执行
      pod/run_task.sh → 轮询进度 → 把小预览和指标拉回服务器 ~/runs/<run_id>/。
大文件（数据、权重、视频原件）从不经过服务器：任务在 pod 上直接读写 Global volume。

用法：
  atlas-run [--input 目录] [--name 名字] [--detach] <任务> [参数...]
      任务 = pod/tasks/<任务>.sh，例如：selftest、setup gen3c vipe 4c4d
  atlas-run --attach <run_id>   继续跟踪一个任务
  atlas-run --fetch <run_id>    只拉回结果
  atlas-run --list              列出服务器上已有的 run
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
import atlas_gpu as g  # noqa: E402  复用护栏里的 pod 查询和 SSH 配置

RUNS = Path(os.environ.get("ATLAS_RUNS", Path.home() / "runs"))
HF_TOKEN_FILE = Path.home() / ".config/atlas-gpu/hf_token"
POD_ROOT = os.environ.get("ATLAS_POD_ROOT", "/opt/atlas")
# 测试模式：不连 pod，"pod" 就是本机目录（只用于 runner/tests，跑的是不需要 GPU 的测试任务）
LOCAL_MODE = os.environ.get("ATLAS_RUN_LOCAL") == "1"
PULL_MAX_SIZE = "20M"   # 单个拉回文件的上限：只要预览和指标
POLL_SECONDS = int(os.environ.get("ATLAS_RUN_POLL", 30))


def die(msg):
    print(f"错误: {msg}", file=sys.stderr)
    sys.exit(2)


def the_pod():
    if LOCAL_MODE:
        return {"id": "local", "name": "atlas-local-test", "gpu": "none", "cost": 1.0, "status": "RUNNING"}
    pods, src = g.atlas_pods()
    running = [p for p in pods if p["status"] == "RUNNING"]
    if not running:
        die("没有运行中的 atlas- pod（先 gpu-up）")
    p = running[0]
    if not g.ssh_base(p):
        die(f"pod {p['name']} 还没有 SSH 信息（数据来源 {src}），稍后再试")
    return p


def ssh_parts(pod):
    """拆成 rsync 需要的形式：(ssh 程序及选项, user@host)。"""
    base = g.ssh_base(pod)
    target = next(a for a in base[1:] if "@" in a)
    opts = [a for a in base if a != target]
    return opts, target


def ssh(pod, remote, check=True, capture=False, stdin=None, timeout=None):
    base = ["bash", "-c"] if LOCAL_MODE else g.ssh_base(pod)
    r = subprocess.run([*base, remote], text=True, input=stdin, timeout=timeout,
                       capture_output=capture)
    if check and r.returncode != 0:
        die(f"pod 上执行失败（rc={r.returncode}）: {remote[:80]}")
    return r


def rsync(pod, src, dst, extra=()):
    if LOCAL_MODE:
        r = subprocess.run(["rsync", "-rt", *extra, src.replace("POD:", ""), dst.replace("POD:", "")])
        if r.returncode != 0:
            die(f"rsync 失败: {src} → {dst}")
        return
    opts, target = ssh_parts(pod)
    rsh = " ".join(shlex.quote(o) for o in opts)
    src = src.replace("POD:", f"{target}:")
    dst = dst.replace("POD:", f"{target}:")
    r = subprocess.run(["rsync", "-rtz", "--partial", "-e", rsh, *extra, src, dst])
    if r.returncode != 0:
        die(f"rsync 失败: {src} → {dst}")


def prepare_pod(pod):
    if not LOCAL_MODE:
        ssh(pod, "command -v rsync >/dev/null || (DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
                 "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rsync zstd >/dev/null)")
    ssh(pod, f"mkdir -p {POD_ROOT}/repo {POD_ROOT}/work {POD_ROOT}/bin {POD_ROOT}/secrets && chmod 700 {POD_ROOT}/secrets")
    rsync(pod, f"{REPO}/pod", f"POD:{POD_ROOT}/repo/", ["--delete", "--exclude", "__pycache__"])
    ssh(pod, f"install -m 755 {POD_ROOT}/repo/pod/pre_stop.sh {POD_ROOT}/bin/pre_stop")
    # HF token：只通过 SSH 的 stdin 传，不出现在命令行、日志或环境变量列表里
    if HF_TOKEN_FILE.is_file() and HF_TOKEN_FILE.stat().st_size > 0:
        ssh(pod, f"umask 077; cat > {POD_ROOT}/secrets/hf_token", stdin=HF_TOKEN_FILE.read_text())


def start(a):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", a.task) or not (REPO / "pod/tasks" / f"{a.task}.sh").exists():
        die(f"没有这个任务: pod/tasks/{a.task}.sh")
    run_id = f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{a.name or a.task}"
    pod = the_pod()
    print(f"pod {pod['name']}（{pod.get('gpu')}），run {run_id}")
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
    # setsid：任务放进独立会话，SSH 断开不会收到 SIGHUP，启动命令也能立即返回（实测只用 nohup 时 ssh 会挂住）
    ssh(pod, f"cd {rd} && setsid nohup bash {POD_ROOT}/repo/pod/run_task.sh {rd} {a.task} {args} > {rd}/run.log 2>&1 < /dev/null & "
             f"echo $! > {rd}/pid", timeout=120)
    print(f"已在 pod 上启动。日志：{rd}/run.log")
    if a.detach:
        print(f"跟踪：atlas-run --attach {run_id}")
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
            print(f"[{int(time.time() - t0)}s] SSH 暂时不通，{POLL_SECONDS}s 后重试")
            time.sleep(POLL_SECONDS)
            continue
        status, _, new = r.stdout.partition("---\n")
        lines = [l for l in new.splitlines()]
        for l in lines[-40:]:
            print("  " + l)
        shown += len(lines)
        status = status.strip()
        if status and status != "running":
            print(f"任务结束：{status}")
            fetch(run_id, pod)
            return 0 if status == "done" else 1
        time.sleep(POLL_SECONDS)


def fetch(run_id, pod=None):
    pod = pod or the_pod()
    local = RUNS / run_id
    local.mkdir(parents=True, exist_ok=True)
    rsync(pod, f"POD:{POD_ROOT}/work/{run_id}/out/", f"{local}/out/",
          ["--max-size", PULL_MAX_SIZE, "--exclude", "*.pt", "--exclude", "*.pth", "--exclude", "*.ply"])
    meta_p = local / "run.json"
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else {"run_id": run_id}
    m_p = local / "out/metrics.json"
    if m_p.exists():
        m = json.loads(m_p.read_text())
        meta["metrics"] = m
        if meta.get("cost_per_hr") and m.get("seconds") is not None:
            meta["gpu_cost_usd"] = round(meta["cost_per_hr"] * m["seconds"] / 3600, 3)
    meta_p.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    print(f"结果已拉回 {local}/out/（单个文件 ≤ {PULL_MAX_SIZE}；完整结果在 Global volume results/{run_id}/）")
    if "gpu_cost_usd" in meta:
        print(f"本任务 GPU 花费约 ${meta['gpu_cost_usd']}（{meta['metrics']['seconds']}s × ${meta['cost_per_hr']}/h）")
    return 0


def main():
    sys.stdout.reconfigure(line_buffering=True)  # 输出重定向到文件时也逐行刷新，方便跟踪
    ap = argparse.ArgumentParser(prog="atlas-run", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", help="推送到 pod 的任务输入目录（小文件）")
    ap.add_argument("--name", help="run 名字后缀")
    ap.add_argument("--detach", action="store_true", help="启动后不跟踪")
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
        ap.error("需要任务名")
    try:
        return start(a)
    except g.GuardError as e:
        die(str(e))


if __name__ == "__main__":
    sys.exit(main())
