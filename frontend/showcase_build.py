#!/usr/bin/env python3
"""生成 /showcase 展示页的素材（只用已有结果，不开 GPU）。

输出到 DATA/showcase/（不在仓库里）：
  media/*.mp4   H.264 + faststart，浏览器可直接播放，单个 ≤ 20 MB
  media/*.jpg   机位缩略图
  showcase.json 页面用到的数字和文件名（只放数字、文件名和固定英文文本，不放任何路径、ID）

用法：python3 frontend/showcase_build.py      （重复运行会覆盖）
"""
import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np

HOME = Path.home()
RUNS = Path(os.environ.get("ATLAS_RUNS", HOME / "runs"))
DATA = Path(os.environ.get("ATLAS_FRONTEND_DATA", HOME / "atlas_frontend"))
OUT = DATA / "showcase"
MEDIA = OUT / "media"
MAX_BYTES = 20 * 1024 * 1024

# 已有结果的位置（相对 ~/runs 或前端数据目录）
SRC = {
    "baseline": RUNS / "20261014-070012-baseline/out",
    "fourc4d": RUNS / "20261014-223624-fourc4d/out",
    "n3v": RUNS / "20261015-020104-n3v3/out",
    "render_job": DATA / "jobs/20261015-062537-0cab",
    "n3v_scene": DATA / "scenes/n3v_flame_steak",
}
VIDEOS = {
    "side_by_side.mp4": SRC["fourc4d"] / "videos/171204_pose1_path_side_by_side.mp4",
    "gt_gen3c.mp4": SRC["baseline"] / "videos/171204_pose1_eval_gen3c_vs_gt.mp4",
    "gt_4c4d.mp4": SRC["fourc4d"] / "videos/171204_pose1_eval_4c4d_vs_gt.mp4",
    "n3v_arc.mp4": SRC["n3v"] / "videos/flame_steak_4c4d_arc.mp4",
    "workbench.mp4": SRC["render_job"] / "render.mp4",
}


def encode(src, dst):
    """H.264 High、yuv420p、faststart、无音轨；超过 20 MB 就提高 CRF 重编码。"""
    for crf in (18, 22, 26, 30):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-an", "-c:v", "libx264",
                        "-profile:v", "high", "-preset", "slow", "-crf", str(crf), "-pix_fmt", "yuv420p",
                        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-movflags", "+faststart",
                        "-map_metadata", "-1", str(dst)], check=True)
        if dst.stat().st_size <= MAX_BYTES:
            return
    raise SystemExit(f"{dst.name} 压缩后仍超过 20 MB")


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,nb_frames,r_frame_rate", "-of", "json", str(path)],
                       capture_output=True, text=True, check=True)
    s = json.loads(r.stdout)["streams"][0]
    num, den = map(int, s["r_frame_rate"].split("/"))
    return {"w": s["width"], "h": s["height"], "seconds": round(int(s["nb_frames"]) * den / num, 1)}


def azimuths(cams, up, subject):
    """机位投影到地面后，绕被摄主体的方位角（度）。0° = 机位平均所在的方向。"""
    up = up / np.linalg.norm(up)
    v = np.mean([c - subject for c in cams.values()], 0)
    v -= up * (v @ up)
    e1 = v / np.linalg.norm(v)
    e2 = np.cross(up, e1)
    out = {}
    for k, c in cams.items():
        d = c - subject
        d -= up * (d @ up)
        out[k] = round(math.degrees(math.atan2(d @ e2, d @ e1)), 1)
    return out


def span_and_gap(az):
    a = sorted(az)
    return round(a[-1] - a[0], 1), round(max(b - x for x, b in zip(a, a[1:])), 1)


def panoptic_layout():
    m = json.loads((SRC["baseline"] / "171204_pose1/meta.json").read_text())
    centers, fwd, ups = {}, {}, []
    for k, c in m["cameras"].items():
        R, t = np.array(c["R"]), np.array(c["t"]).reshape(3)
        centers[k], fwd[k] = -R.T @ t, R[2]
        ups.append(-R[1])
    # 主体位置：离所有光轴最近的点（Panoptic 机位环绕主体，光轴交汇处就是人）
    A, b = np.zeros((3, 3)), np.zeros(3)
    for k in centers:
        f = fwd[k] / np.linalg.norm(fwd[k])
        P = np.eye(3) - np.outer(f, f)
        A += P
        b += P @ centers[k]
    az = azimuths(centers, np.mean(ups, 0), np.linalg.solve(A, b))
    train = [f"cam{c:02d}" for c in m["train_cams"]]
    ev = f"cam{m['eval_cam']:02d}"
    cams = [{"name": k, "az": az[k], "role": "input" if k in train else "eval" if k == ev else "other"} for k in sorted(az)]
    span, gap = span_and_gap([az[k] for k in train])
    return {"cams": cams, "input_span": span, "input_gap": gap, "gen3c_input": f"cam{m['gen3c_input_cam']:02d}"}


def n3v_layout():
    s = json.loads((SRC["n3v_scene"] / "scene.json").read_text())
    centers, ups = {}, []
    for k, c in s["cameras"].items():
        w = np.array(c["w2c"])
        R = w[:3, :3]
        centers[k] = -R.T @ w[:3, 3]
        ups.append(-R[1])
    buf = (SRC["n3v_scene"] / "points.bin").read_bytes()
    n = int(np.frombuffer(buf[:4], np.uint32)[0])
    # N3V 机位朝同一方向、光轴近乎平行，求交点不稳定：用点云中位数当主体位置
    subject = np.median(np.frombuffer(buf[4:4 + 12 * n], np.float32).reshape(-1, 3), 0)
    az = azimuths(centers, np.mean(ups, 0), subject)
    train = ["cam01", "cam10", "cam13", "cam20"]
    cams = [{"name": k, "az": az[k], "role": "input" if k in train else "eval" if k == "cam00" else "other"} for k in sorted(az)]
    span, gap = span_and_gap([az[k] for k in train])
    all_span, _ = span_and_gap(list(az.values()))
    return {"cams": cams, "input_span": span, "input_gap": gap, "all_span": all_span}


def crop_panoptic_tiles():
    """阶段 3 的机位拼图是 3×2（每格 480×270），拆成单张，标签由页面用大字重新写。"""
    sheet = SRC["baseline"] / "171204_pose1/171204_pose1_cams.jpg"
    order = ["cam00", "cam05", "cam08", "cam24", "cam25"]
    for i, name in enumerate(order):
        x, y = (i % 3) * 480, (i // 3) * 270
        # 跳过左上角烧录的标签（约 30 px 高）
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(sheet), "-vf",
                        f"crop=480:236:{x}:{y + 34}", "-q:v", "3", str(MEDIA / f"pan_{name}.jpg")], check=True)


def mean_metrics(path):
    m = json.loads(Path(path).read_text())["mean"]
    return {k: round(m[k], 4) for k in ("psnr", "ssim", "lpips")}


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    MEDIA.mkdir(parents=True)
    videos = {}
    for name, src in VIDEOS.items():
        encode(src, MEDIA / name)
        videos[name] = {**probe(MEDIA / name), "mb": round((MEDIA / name).stat().st_size / 2 ** 20, 2)}
    crop_panoptic_tiles()
    for cam in ("cam01", "cam10", "cam13", "cam20", "cam00"):
        shutil.copy(SRC["n3v_scene"] / "thumbs" / f"{cam}.jpg", MEDIA / f"n3v_{cam}.jpg")

    n3v = json.loads((SRC["n3v"] / "metrics.json").read_text())["ours"]
    job = json.loads((SRC["render_job"] / "status.json").read_text())
    data = {
        "videos": videos,
        "layout": {"panoptic": panoptic_layout(), "n3v": n3v_layout()},
        "metrics": {
            "gen3c": mean_metrics(SRC["baseline"] / "metrics_gen3c.json"),
            "fourc4d": mean_metrics(SRC["fourc4d"] / "metrics_4c4d.json"),
        },
        "n3v": {
            "ours": {"psnr": round(n3v["psnr"], 2), "dssim1": n3v["Dssim1"], "dssim2": n3v["Dssim2"], "lpips": n3v["lpips"]},
            # 论文（4C4D, CVPR 2026）N3V flame_steak 的数字，DSSIM2 由 Joey 提供
            "paper": {"psnr": 23.72, "dssim1": 0.075, "dssim2": 0.046, "lpips": 0.114},
        },
        "workbench": {
            "frames": job["render_stats"]["frames"],
            "render_fps": job["render_stats"]["fps_render"],
            "prepare": {"minutes": "~1.5", "usd": "0.04"},
            "render": {"minutes": "~1.5", "usd": "0.045"},
        },
    }
    text = json.dumps(data, indent=1)
    # 安全自查：只允许数字、文件名和固定英文；出现路径、IP、ID 就停下
    bad = re.findall(r"/(?:home|opt|workspace|root)\b|\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|atlas", text)
    if bad:
        raise SystemExit(f"showcase.json 含不该出现的内容：{bad}")
    (OUT / "showcase.json").write_text(text)
    for name, v in videos.items():
        print(f"{name:18s} {v['w']}x{v['h']} {v['seconds']:5.1f}s {v['mb']:5.2f} MB")
    print("layout", {k: {kk: vv for kk, vv in v.items() if kk != "cams"} for k, v in data["layout"].items()})


if __name__ == "__main__":
    main()
