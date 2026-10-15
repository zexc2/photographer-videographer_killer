#!/usr/bin/env python3
"""Frontend GPU job runner (separate process, unaffected by Flask restarts). Usage: job_runner.py <job_id>

Job kinds (kind in spec.json):
  export  export a scene preview (point cloud, cameras, thumbnails) → saved to DATA/scenes/<model>/
  render  render a video along a trajectory                        → DATA/jobs/<id>/render.mp4
Flow: check GPU (reuse an existing atlas-render pod; refuse if another atlas job is using the GPU) → gpu-up (0.5 hour limit)
      → atlas-run pushes and starts the pod task → update progress and cost every 15 seconds → pull results → gpu-down.
All pod starts and stops go through the guard (gpu-up / gpu-down); its time limit, idle detection and auto-stop after runs still apply.

ATLAS_FRONTEND_FAKE=1: simulation mode, no GPU, runs the full flow on synthetic data (for testing the UI).
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


class JobError(Exception):
    """Error shown in the job bar: a stable code + params, translated by the frontend; technical details go only to job.log."""
    def __init__(self, code, **params):
        super().__init__(code)
        self.code, self.params = code, params


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

    def stage(self, code, **params):
        """Job progress: store only the code and params; the frontend renders them in the current language."""
        self.update(stage_code=code, stage_params=params)

    def log(self, msg):
        with open(self.dir / "job.log", "a") as f:
            f.write(f"{now().strftime('%H:%M:%S')} {msg}\n")


def sh(cmd, timeout=None):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr)


# ---------------------------------------------------------------- real GPU flow
def ensure_pod(job):
    import atlas_gpu as g
    pods, _ = g.atlas_pods()
    active = [p for p in pods if p["status"] in g.ACTIVE]
    other = [p for p in active if p["name"] != POD_NAME]
    if other:
        raise JobError("gpu_busy", name=other[0]["name"])
    mine = [p for p in active if p["name"] == POD_NAME]
    if mine:
        job.log("Reusing the already running render pod")
        p = mine[0]
        job.stage("reusing_gpu")
        job.update(cost_per_hr=p.get("cost") or 1.79, gpu_started=now().isoformat(timespec="seconds"), pod_id=p["id"])
        return
    # RunPod bills from pod creation, so start the clock before calling gpu-up (better to slightly overestimate)
    t_start = now().isoformat(timespec="seconds")
    job.stage("starting_gpu")
    rc, out = sh([str(BIN / "gpu-up"), "--name", "render", "--hours", POD_HOURS, "--wait", "10"], timeout=900)
    job.log(out[-1500:])
    if rc != 0:
        raise JobError("gpu_start_failed")      # the guard's raw output is already in job.log
    price = 1.79
    for line in out.splitlines():
        if "Est. cost" in line and "$" in line:
            try:
                price = float(line.split("$")[1].split("/")[0])
            except (ValueError, IndexError):
                pass
    pod_id = next((l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("Pod ID")), None)
    if not pod_id:
        raise JobError("no_pod_id")             # the guard still stops the pod at its time limit
    job.update(cost_per_hr=price, gpu_started=t_start, pod_id=pod_id)


def run_on_pod(job, task, args, input_dir=None):
    import atlas_run as ar
    run_name = f"fe-{job.id}"
    cmd = [str(BIN / "atlas-run"), "--name", run_name, "--detach"]
    if input_dir:
        cmd += ["--input", str(input_dir)]
    cmd += [task, *args]
    job.stage("pushing")
    try:
        sh(cmd, timeout=120)       # the launch SSH occasionally never returns though the run has started; keep tracking after the timeout
    except subprocess.TimeoutExpired:
        pass
    runs = sorted((Path.home() / "runs").glob(f"*-{run_name}"))
    if not runs:
        raise JobError("run_not_started")
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
            job.stage("rendering", done=prog["done"], total=prog["total"])
            job.update(progress=round(prog["done"] / prog["total"], 3))
        elif status == "running":
            job.stage("preparing")
        else:
            job.update()
        if status and status != "running":
            break
        time.sleep(POLL)
    job.stage("fetching")
    try:
        ar.fetch(run_id, pod)
    except SystemExit:
        pass
    out = Path.home() / "runs" / run_id / "out"
    if status != "done":
        tail = (out / "run.log").read_text(errors="ignore")[-1500:] if (out / "run.log").exists() else ""
        job.log(tail)
        raise JobError("pod_task_failed")
    return out


def stop_pod(job):
    # Stop and delete by pod ID: stopping by name is refused by the guard when an old pod with the same name exists
    # (2026-10-15: the pod was not stopped because of this).
    # Results are already on the server and nothing on the container disk needs keeping; deleting avoids piling up same-named pods
    target = job.st.get("pod_id") or POD_NAME
    rc, out = sh([str(BIN / "gpu-down"), target, "--delete"], timeout=300)
    job.log(out[-500:])
    job.update(gpu_ended=now().isoformat(timespec="seconds"))


# ---------------------------------------------------------------- simulated flow
def fake_run(job, kind):
    job.stage("starting_gpu", fake=True)
    job.update(cost_per_hr=1.79, gpu_started=now().isoformat(timespec="seconds"))
    time.sleep(2)
    job.stage("preparing", fake=True)
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
            job.stage("rendering", done=i, total=n, fake=True)
            job.update(progress=round(i / n, 3))
            time.sleep(1)
        sh(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
            f"testsrc2=size={t['width']}x{t['height']}:rate={t['fps']}", "-frames:v", str(n),
            "-pix_fmt", "yuv420p", str(out / "render.mp4")], timeout=300)
        (out / "metrics.json").write_text(json.dumps({"frames": n, "fake": True}))
    job.update(gpu_ended=now().isoformat(timespec="seconds"))
    return out


# ---------------------------------------------------------------- main flow
def main(job_id):
    job = Job(job_id)
    kind = job.spec["kind"]
    model = job.spec["model"]
    lock = DATA / "jobs" / ".lock"
    job.update(state="running", pid=os.getpid(), stage_code="queued", stage_params={}, started=now().isoformat(timespec="seconds"))
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
        job.update(state="done", stage_code="done", stage_params={}, progress=1.0)
    except JobError as e:
        job.log(traceback.format_exc())
        job.update(state="failed", stage_code="failed", stage_params={}, error_code=e.code, error_params=e.params)
    except Exception:  # noqa: BLE001  unexpected error: the page shows a generic message, details are in the log
        job.log(traceback.format_exc())
        job.update(state="failed", stage_code="failed", stage_params={}, error_code="unexpected", error_params={})
    finally:
        if started_pod and not FAKE:
            if job.st.get("state") == "running":
                job.stage("stopping_gpu")
            stop_pod(job)
        job.update(finished=now().isoformat(timespec="seconds"))
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    main(sys.argv[1])
