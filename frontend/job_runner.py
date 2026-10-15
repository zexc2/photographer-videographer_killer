#!/usr/bin/env python3
"""前端的 GPU 任务执行器（独立进程，Flask 重启不影响）。用法：job_runner.py <job_id>

任务类型（spec.json 里的 kind）：
  export  导出场景预览（点云、机位、缩略图） → 存到 DATA/scenes/<模型>/
  render  按轨迹渲染视频                     → DATA/jobs/<id>/render.mp4
流程：检查 GPU（已有 atlas-render pod 就复用；有其他 atlas 任务在用 GPU 就拒绝）→ gpu-up（上限 0.5 小时）
      → atlas-run 推送并启动 pod 任务 → 每 15 秒更新进度和花费 → 拉回结果 → gpu-down。
所有开关 pod 都走护栏（gpu-up / gpu-down），护栏的时长上限、空闲检测、任务结束自动关机照常生效。

ATLAS_FRONTEND_FAKE=1：模拟模式，不开 GPU，用合成数据走完整流程（测试界面用）。
"""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

APP = Path(__file__).resolve().parent
REPO = APP.parent
DATA = Path(os.environ.get("ATLAS_FRONTEND_DATA", Path.home() / "atlas_frontend"))
FAKE = os.environ.get("ATLAS_FRONTEND_FAKE") == "1"
BIN = Path.home() / "bin"
POD_NAME = "atlas-render"
POD_HOURS = "0.5"
POLL = 15

sys.path.insert(0, str(REPO / "guard"))
sys.path.insert(0, str(REPO / "runner"))


def now():
    return dt.datetime.now(dt.timezone.utc)


class Job:
    def __init__(self, job_id):
        self.dir = DATA / "jobs" / job_id
        self.id = job_id
        self.spec = json.loads((self.dir / "spec.json").read_text())
        self.status_path = self.dir / "status.json"
        self.st = json.loads(self.status_path.read_text()) if self.status_path.exists() else {}
        self.t0 = time.time()

    def update(self, **kw):
        self.st.update(kw)
        self.st["updated"] = now().isoformat(timespec="seconds")
        if self.st.get("cost_per_hr") and self.st.get("gpu_started"):
            start = dt.datetime.fromisoformat(self.st["gpu_started"]).timestamp()
            end = dt.datetime.fromisoformat(self.st["gpu_ended"]).timestamp() if self.st.get("gpu_ended") else time.time()
            self.st["cost_usd"] = round(self.st["cost_per_hr"] * (end - start) / 3600, 3)
        tmp = self.status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.st, indent=1, ensure_ascii=False))
        tmp.replace(self.status_path)

    def log(self, msg):
        with open(self.dir / "job.log", "a") as f:
            f.write(f"{now().strftime('%H:%M:%S')} {msg}\n")


def sh(cmd, timeout=None):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr)


# ---------------------------------------------------------------- 真实 GPU 流程
def ensure_pod(job):
    import atlas_gpu as g
    pods, _ = g.atlas_pods()
    active = [p for p in pods if p["status"] in g.ACTIVE]
    other = [p for p in active if p["name"] != POD_NAME]
    if other:
        raise RuntimeError(f"GPU 正被其他任务使用（{other[0]['name']}），请稍后再试")
    mine = [p for p in active if p["name"] == POD_NAME]
    if mine:
        job.log("复用已在运行的渲染 pod")
        p = mine[0]
        job.update(stage="复用已开启的 GPU", cost_per_hr=p.get("cost") or 1.79, gpu_started=now().isoformat(timespec="seconds"))
        return
    job.update(stage="开启 GPU（约 1–2 分钟）")
    rc, out = sh([str(BIN / "gpu-up"), "--name", "render", "--hours", POD_HOURS, "--wait", "10"], timeout=900)
    job.log(out[-1500:])
    if rc != 0:
        raise RuntimeError("开启 GPU 失败：" + (out.strip().splitlines() or ["未知原因"])[-1])
    price = 1.79
    for line in out.splitlines():
        if "估算花费" in line and "$" in line:
            try:
                price = float(line.split("$")[1].split("/")[0])
            except (ValueError, IndexError):
                pass
    job.update(cost_per_hr=price, gpu_started=now().isoformat(timespec="seconds"))


def run_on_pod(job, task, args, input_dir=None):
    import atlas_run as ar
    run_name = f"fe-{job.id}"
    cmd = [str(BIN / "atlas-run"), "--name", run_name, "--detach"]
    if input_dir:
        cmd += ["--input", str(input_dir)]
    cmd += [task, *args]
    job.update(stage="推送任务到 GPU")
    try:
        sh(cmd, timeout=120)       # 启动用的 SSH 偶尔不返回，任务本身已经启动；超时后照常跟踪
    except subprocess.TimeoutExpired:
        pass
    runs = sorted((Path.home() / "runs").glob(f"*-{run_name}"))
    if not runs:
        raise RuntimeError("任务没有启动（服务器上找不到记录）")
    run_id = runs[-1].name
    job.update(run_id=run_id)
    pod = ar.the_pod()
    rd = f"{ar.POD_ROOT}/work/{run_id}"
    while True:
        try:
            r = ar.ssh(pod, f"cat {rd}/status 2>/dev/null; echo; cat {ar.POD_ROOT}/work/frames_{run_id}/progress.json 2>/dev/null",
                       check=False, capture=True, timeout=60)
            lines = r.stdout.strip().splitlines()
        except (subprocess.TimeoutExpired, SystemExit):
            lines = []
        status = lines[0].strip() if lines else ""
        prog = None
        for l in lines[1:]:
            if l.startswith("{"):
                try:
                    prog = json.loads(l)
                except json.JSONDecodeError:
                    pass
        if prog:
            job.update(stage=f"渲染中 {prog['done']}/{prog['total']} 帧", progress=round(prog["done"] / prog["total"], 3))
        elif status == "running":
            job.update(stage="GPU 上准备环境和模型（约 2–3 分钟）")
        else:
            job.update()
        if status and status != "running":
            break
        time.sleep(POLL)
    job.update(stage="拉回结果")
    try:
        ar.fetch(run_id, pod)
    except SystemExit:
        pass
    out = Path.home() / "runs" / run_id / "out"
    if status != "done":
        tail = (out / "run.log").read_text(errors="ignore")[-1500:] if (out / "run.log").exists() else ""
        job.log(tail)
        raise RuntimeError("GPU 上的任务失败，详见任务日志")
    return out


def stop_pod(job):
    rc, out = sh([str(BIN / "gpu-down"), POD_NAME], timeout=300)
    job.log(out[-500:])
    job.update(gpu_ended=now().isoformat(timespec="seconds"))


# ---------------------------------------------------------------- 模拟流程
def fake_run(job, kind):
    job.update(stage="开启 GPU（模拟）", cost_per_hr=1.79, gpu_started=now().isoformat(timespec="seconds"))
    time.sleep(2)
    job.update(stage="GPU 上准备环境和模型（模拟）")
    time.sleep(2)
    out = job.dir / "_fake_out"
    out.mkdir(exist_ok=True)
    if kind == "export":
        from fake_scene import write_fake_scene
        write_fake_scene(out / "scene")
    else:
        t = json.loads((job.dir / "input" / "traj.json").read_text())
        n = len(t["frames"])
        for i in range(0, n, max(1, n // 5)):
            job.update(stage=f"渲染中 {i}/{n} 帧（模拟）", progress=round(i / n, 3))
            time.sleep(1)
        sh(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
            f"testsrc2=size={t['width']}x{t['height']}:rate={t['fps']}", "-frames:v", str(n),
            "-pix_fmt", "yuv420p", str(out / "render.mp4")], timeout=300)
        (out / "metrics.json").write_text(json.dumps({"frames": n, "fake": True}))
    job.update(gpu_ended=now().isoformat(timespec="seconds"))
    return out


# ---------------------------------------------------------------- 主流程
def main(job_id):
    job = Job(job_id)
    kind = job.spec["kind"]
    model = job.spec["model"]
    lock = DATA / "jobs" / ".lock"
    job.update(state="running", pid=os.getpid(), stage="排队中", started=now().isoformat(timespec="seconds"))
    started_pod = False
    try:
        if FAKE:
            out = fake_run(job, kind)
        else:
            ensure_pod(job)
            started_pod = True
            if kind == "export":
                out = run_on_pod(job, "export_scene", [model])
            else:
                out = run_on_pod(job, "render_traj", [model], input_dir=job.dir / "input")
        if kind == "export":
            dst = DATA / "scenes" / model
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(out / "scene", dst)
        else:
            shutil.copy(out / "render.mp4", job.dir / "render.mp4")
            if (out / "metrics.json").exists():
                job.update(render_stats=json.loads((out / "metrics.json").read_text()))
        job.update(state="done", stage="完成", progress=1.0)
    except Exception as e:  # noqa: BLE001
        job.log(traceback.format_exc())
        job.update(state="failed", stage="失败", error=str(e))
    finally:
        if started_pod and not FAKE:
            job.update(stage=job.st.get("stage", "") + "，正在关闭 GPU" if job.st.get("state") == "running" else job.st.get("stage"))
            stop_pod(job)
        job.update(finished=now().isoformat(timespec="seconds"))
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    main(sys.argv[1])
