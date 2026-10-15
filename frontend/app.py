#!/usr/bin/env python3
"""ATLAS 运镜工作台（Flask，只监听 127.0.0.1）。

访问：在自己电脑上 ssh -L 8765:127.0.0.1:8765 <用户名>@<服务器>，然后打开 http://localhost:8765
数据目录（不进仓库）：~/atlas_frontend/{scenes,jobs}
ATLAS_FRONTEND_FAKE=1 时不开 GPU，用合成场景和测试视频（测试界面用）。
"""
import json
import math
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from flask import Flask, abort, jsonify, request, send_file, send_from_directory

APP = Path(__file__).resolve().parent
REPO = APP.parent
DATA = Path(os.environ.get("ATLAS_FRONTEND_DATA", Path.home() / "atlas_frontend"))
FAKE = os.environ.get("ATLAS_FRONTEND_FAKE") == "1"
MODELS = json.loads((REPO / "pod/configs/models.json").read_text())
MODELS = {k: v for k, v in MODELS.items() if not k.startswith("_")}
GPU_PRICE = 1.79                    # A100 SXM $/h（gpu-up 的首选）
MAX_FRAMES = 900                    # 单次最多 30 秒 @ 30fps
MAX_PIXELS = 1600 * 1200

sys.path.insert(0, str(APP))
sys.path.insert(0, str(REPO / "guard"))
import traj  # noqa: E402

class ApiError(Exception):
    """返回给页面的错误：只有稳定的 code 和参数，文字由前端按当前语言翻译。"""
    def __init__(self, status, code, **params):
        super().__init__(code)
        self.status, self.code, self.params = status, code, params


def fail(status, code, **params):
    raise ApiError(status, code, **params)


app = Flask(__name__, static_folder=str(APP / "static"), static_url_path="/static")
(DATA / "jobs").mkdir(parents=True, exist_ok=True)
(DATA / "scenes").mkdir(parents=True, exist_ok=True)


def scene_dir(model):
    if model not in MODELS:
        fail(404, "model_not_found", model=model)
    return DATA / "scenes" / model


def load_scene(model):
    p = scene_dir(model) / "scene.json"
    if not p.exists():
        fail(409, "scene_not_ready", model=model)
    s = json.loads(p.read_text())
    s.setdefault("source_seconds", MODELS[model].get("source_seconds", 10.0))
    return s


def running_job():
    lock = DATA / "jobs" / ".lock"
    if not lock.exists():
        return None
    jid = lock.read_text().strip()
    st = DATA / "jobs" / jid / "status.json"
    if st.exists():
        s = json.loads(st.read_text())
        pid = s.get("pid")
        if s.get("state") in ("done", "failed") or (pid and not Path(f"/proc/{pid}").exists() and
                                                     time.time() - st.stat().st_mtime > 60):
            lock.unlink(missing_ok=True)
            return None
    return jid


def estimate(n_frames, width, height, kind="render"):
    """估算 GPU 时间和花费（分钟）。2026-10-15 实测：开 pod 到关 pod 的固定开销约 1.5–2 分钟
    （开 pod ~35s、恢复 4c4d 环境 + 复制模型 ~40s、加载模型 3s、编码和拉回 ~10s）；
    960×720 渲染 20.8 帧/秒，按像素数反比估算其他分辨率。导出场景实测约 1.5 分钟。"""
    fixed = 2.0
    if kind == "export":
        work = 0.5
    else:
        fps = 20.8 * (960 * 720) / max(width * height, 1)
        work = n_frames / fps / 60
    minutes = fixed + work
    return {"minutes": round(minutes, 1), "cost_usd": round(minutes / 60 * GPU_PRICE, 2),
            "gpu": "A100 80GB", "price_per_hr": GPU_PRICE}


def start_job(kind, model, spec_extra=None, traj_obj=None):
    if running_job():
        fail(409, "job_running")
    jid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    d = DATA / "jobs" / jid
    (d / "input").mkdir(parents=True)
    spec = {"kind": kind, "model": model, **(spec_extra or {})}
    (d / "spec.json").write_text(json.dumps(spec, indent=1, ensure_ascii=False))
    if traj_obj is not None:
        (d / "input" / "traj.json").write_text(json.dumps(traj_obj))
    (d / "status.json").write_text(json.dumps({"state": "queued", "stage_code": "queued", "kind": kind, "model": model,
                                                "created": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False))
    (DATA / "jobs" / ".lock").write_text(jid)
    env = dict(os.environ)
    subprocess.Popen([sys.executable, str(APP / "job_runner.py"), jid], env=env, start_new_session=True,
                     stdout=open(d / "runner.out", "w"), stderr=subprocess.STDOUT)
    return jid


# ---------------------------------------------------------------- 页面
@app.get("/")
def index():
    return send_from_directory(APP / "static", "index.html")


# ---------------------------------------------------------------- 结果展示页（课堂录屏用）
SHOWCASE = DATA / "showcase"


@app.get("/showcase")
def showcase():
    return send_from_directory(APP / "static", "showcase.html")


@app.get("/showcase/data.json")
def showcase_data():
    p = SHOWCASE / "showcase.json"
    if not p.exists():
        fail(404, "showcase_missing")
    return send_file(p, mimetype="application/json", max_age=0)


@app.get("/showcase/media/<name>")
def showcase_media(name):
    if not re.fullmatch(r"[a-z0-9_]+\.(mp4|jpg)", name):
        abort(404)
    return send_from_directory(SHOWCASE / "media", name, max_age=3600, conditional=True)


# ---------------------------------------------------------------- 模型与场景
@app.get("/api/models")
def api_models():
    out = []
    for k, m in MODELS.items():
        sd = DATA / "scenes" / k
        out.append({"key": k, "title": m["title"], "title_en": m.get("title_en", m["title"]), "license": m.get("license"), "train_cams": m.get("train_cams", []),
                    "source_seconds": m.get("source_seconds", 10.0), "scene_ready": (sd / "scene.json").exists(),
                    "export_estimate": estimate(0, 0, 0, "export")})
    return jsonify({"models": out, "fake": FAKE})


@app.get("/api/scene/<model>")
def api_scene(model):
    s = load_scene(model)
    cams = s["cameras"]
    out = {"cameras": {}, "spacing": traj.camera_spacing(cams), "num_points": s.get("num_points"),
           "source_seconds": s["source_seconds"], "fake": s.get("fake", False)}
    for name, c in cams.items():
        w2c = c["w2c"]
        c2w = np.linalg.inv(np.asarray(w2c, dtype=float)).tolist()
        out["cameras"][name] = {"c2w": c2w, "fov": traj.base_fov(s, name), "width": c["width"], "height": c["height"]}
    return jsonify(out)


@app.get("/api/scene/<model>/points.bin")
def api_points(model):
    p = scene_dir(model) / "points.bin"
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="application/octet-stream", max_age=3600)


@app.get("/api/scene/<model>/thumb/<cam>.jpg")
def api_thumb(model, cam):
    if not cam.replace("cam", "").isdigit():
        abort(404)
    return send_from_directory(scene_dir(model) / "thumbs", f"{cam}.jpg", max_age=3600)


@app.post("/api/scene/<model>/export")
def api_export(model):
    scene_dir(model)
    jid = start_job("export", model)
    return jsonify({"job": jid})


# ---------------------------------------------------------------- 轨迹
def parse_spec(body, scene):
    if not isinstance(body, dict):
        fail(400, "bad_request")
    spec = {k: body.get(k) for k in ("base_cam", "keyframes", "duration", "fps", "width", "height")}
    if spec["base_cam"] not in scene["cameras"]:
        fail(400, "base_cam_not_found", cam=str(spec["base_cam"]))
    spec["fps"] = int(spec["fps"] or 30)
    spec["duration"] = float(spec["duration"] or 4)
    spec["width"], spec["height"] = int(spec["width"] or 960), int(spec["height"] or 720)
    if not (0.5 <= spec["duration"] <= 30) or spec["fps"] not in (24, 25, 30):
        fail(400, "bad_duration_fps", min=0.5, max=30, fps=[24, 25, 30])
    if spec["width"] * spec["height"] > MAX_PIXELS or spec["width"] % 2 or spec["height"] % 2:
        fail(400, "bad_resolution", max_w=1600, max_h=1200)
    if not spec["keyframes"]:
        fail(400, "no_keyframes")
    return spec


@app.post("/api/path/<model>")
def api_path(model):
    """预览用：返回每一帧的相机（c2w、视场角、源时间），浏览器里画轨迹、按镜头视角播放点云。"""
    scene = load_scene(model)
    spec = parse_spec(request.get_json(force=True), scene)
    t = traj.build_trajectory(scene, spec)
    frames = []
    for f in t["frames"]:
        frames.append({"c2w": np.linalg.inv(np.asarray(f["w2c"])).round(5).tolist(),
                       "fov": math.degrees(2 * math.atan(spec["height"] / (2 * f["fy"]))), "src": f["src_seconds"]})
    n = len(frames)
    return jsonify({"frames": frames, "estimate": estimate(n, spec["width"], spec["height"]), "num_frames": n,
                    "too_long": n > MAX_FRAMES})


# ---------------------------------------------------------------- 任务
@app.post("/api/render/<model>")
def api_render(model):
    scene = load_scene(model)
    body = request.get_json(force=True)
    if not body.get("confirm"):
        fail(400, "confirm_required")
    spec = parse_spec(body, scene)
    t = traj.build_trajectory(scene, spec)
    if len(t["frames"]) > MAX_FRAMES:
        fail(400, "too_many_frames", max=MAX_FRAMES)
    jid = start_job("render", model, {"trajectory_spec": spec,
                                      "estimate": estimate(len(t["frames"]), spec["width"], spec["height"])}, t)
    return jsonify({"job": jid})


@app.get("/api/jobs")
def api_jobs():
    out = []
    for d in sorted((DATA / "jobs").iterdir(), reverse=True):
        if not d.is_dir() or not (d / "status.json").exists():
            continue
        s = json.loads((d / "status.json").read_text())
        spec = json.loads((d / "spec.json").read_text()) if (d / "spec.json").exists() else {}
        out.append({"id": d.name, **s, "spec": spec.get("trajectory_spec"), "estimate": spec.get("estimate"),
                    "has_video": (d / "render.mp4").exists()})
        if len(out) >= 30:
            break
    return jsonify({"jobs": out, "running": running_job()})


@app.get("/api/jobs/<jid>/video")
def api_video(jid):
    d = DATA / "jobs" / jid
    if not d.is_dir() or "/" in jid or not (d / "render.mp4").exists():
        abort(404)
    return send_file(d / "render.mp4", mimetype="video/mp4", as_attachment=request.args.get("download") == "1",
                     download_name=f"atlas-{jid}.mp4", conditional=True)


@app.get("/api/jobs/<jid>/log")
def api_log(jid):
    p = DATA / "jobs" / jid / "job.log"
    return (p.read_text()[-4000:] if p.exists() else ""), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.get("/api/status")
def api_status():
    """余额和 GPU 状态（缓存 60 秒，避免每次轮询都调 RunPod）。"""
    cache = DATA / ".status_cache.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 60:
        return jsonify(json.loads(cache.read_text()))
    out = {"balance": None, "pods": [], "fake": FAKE}
    if not FAKE:
        try:
            import atlas_gpu as g
            out["balance"] = g.get_balance()
            pods, _ = g.atlas_pods()
            out["pods"] = [{"name": p["name"], "status": p["status"]} for p in pods if p["status"] in g.ACTIVE]
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)
    cache.write_text(json.dumps(out))
    return jsonify(out)


@app.errorhandler(ApiError)
def api_err(e):
    return jsonify({"code": e.code, "params": e.params}), e.status


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(409)
def http_err(e):
    # Flask 自己产生的错误（找不到路由、JSON 解析失败等）
    return jsonify({"code": {400: "bad_request", 404: "not_found", 409: "conflict"}[e.code], "params": {}}), e.code


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("ATLAS_FRONTEND_PORT", 8765)), threaded=True)
