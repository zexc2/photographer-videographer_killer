#!/usr/bin/env python3
"""直接用训练好的 4C4D 模型渲染，不加载训练数据集。在 4c4d 环境里、4C4D 仓库目录下运行。

两个子命令：
  export  导出前端用的场景预览：点云（从高斯中心采样）、全部机位的相机参数、每个机位 t=0 的缩略图
  render  按轨迹逐帧渲染（轨迹由服务器算好：每帧的 w2c、焦距、画面尺寸、时间戳）

渲染调用与官方 render.py 相同：render(相机, 高斯, pipe, 背景色)，不做训练时的透明度衰减。
相机对象只提供渲染函数用到的属性（视场角、位姿、投影、相机中心、画面尺寸、时间戳），主点居中。
"""
import argparse
import json
import math
import os
import struct
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import torch
from omegaconf import OmegaConf

from gaussian_renderer import render
from scene.gaussian_model import GaussianModel
from utils.graphics_utils import getProjectionMatrix


class Cam:
    def __init__(self, w2c, fx, fy, width, height, timestamp, znear=0.01, zfar=100.0):
        self.image_width, self.image_height = int(width), int(height)
        self.FoVx = 2 * math.atan(width / (2 * fx))
        self.FoVy = 2 * math.atan(height / (2 * fy))
        self.znear, self.zfar = znear, zfar
        self.world_view_transform = torch.tensor(np.asarray(w2c, dtype=np.float32).T).cuda()
        self.projection_matrix = getProjectionMatrix(znear=znear, zfar=zfar, fovX=self.FoVx, fovY=self.FoVy).transpose(0, 1).cuda()
        self.full_proj_transform = self.world_view_transform.unsqueeze(0).bmm(self.projection_matrix.unsqueeze(0)).squeeze(0)
        self.camera_center = self.world_view_transform.inverse()[3, :3]
        self.timestamp = float(timestamp)


def load_model(cfg_path, ckpt_path):
    cfg = OmegaConf.load(cfg_path)
    pipe_cfg = OmegaConf.to_container(cfg.PipelineParams)
    g = GaussianModel(cfg.ModelParams.sh_degree, gaussian_dim=cfg.gaussian_dim, time_duration=list(cfg.time_duration),
                      rot_4d=cfg.rot_4d, force_sh_3d=cfg.force_sh_3d,
                      sh_degree_t=2 if pipe_cfg.get("eval_shfs_4d") else 0)
    params, it = torch.load(ckpt_path, weights_only=False)
    g.restore(params, None)
    bg = torch.tensor([1, 1, 1] if cfg.ModelParams.white_background else [0, 0, 0], dtype=torch.float32, device="cuda")
    return g, SimpleNamespace(**pipe_cfg), bg, it


def save_rgb(t, path, quality=90):
    import cv2
    img = (t.clamp(0, 1).permute(1, 2, 0).detach().cpu().numpy() * 255).astype(np.uint8)[:, :, ::-1]
    cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, quality] if str(path).endswith(".jpg") else [])


def read_colmap(sparse):
    """读 COLMAP 二进制 cameras.bin / images.bin，返回 {名字: {w2c, fx, fy, cx, cy, W, H}}。"""
    cams = {}
    with open(sparse / "cameras.bin", "rb") as f:
        for _ in range(struct.unpack("<Q", f.read(8))[0]):
            cid, model, w, h = struct.unpack("<iiQQ", f.read(24))
            n = {0: 3, 1: 4, 2: 4, 3: 5, 4: 8}[model]
            p = struct.unpack("<" + "d" * n, f.read(8 * n))
            fx, fy = (p[0], p[0]) if model in (0, 2, 3) else (p[0], p[1])
            cx, cy = (p[1], p[2]) if model in (0, 2, 3) else (p[2], p[3])
            cams[cid] = (fx, fy, cx, cy, w, h)
    out = {}
    with open(sparse / "images.bin", "rb") as f:
        for _ in range(struct.unpack("<Q", f.read(8))[0]):
            struct.unpack("<i", f.read(4))
            qw, qx, qy, qz = struct.unpack("<dddd", f.read(32))
            t = np.array(struct.unpack("<ddd", f.read(24)))
            cid = struct.unpack("<i", f.read(4))[0]
            name = b""
            while (c := f.read(1)) != b"\x00":
                name += c
            f.read(struct.unpack("<Q", f.read(8))[0] * 24)
            R = np.array([[1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qw * qz), 2 * (qx * qz + qw * qy)],
                          [2 * (qx * qy + qw * qz), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qw * qx)],
                          [2 * (qx * qz - qw * qy), 2 * (qy * qz + qw * qx), 1 - 2 * (qx * qx + qy * qy)]])
            w2c = np.eye(4)
            w2c[:3, :3], w2c[:3, 3] = R, t
            fx, fy, cx, cy, w, h = cams[cid]
            out[name.decode().split("_")[0].split(".")[0]] = {
                "w2c": w2c.tolist(), "fx": fx, "fy": fy, "cx": cx, "cy": cy, "width": w, "height": h}
    return dict(sorted(out.items()))


def cmd_export(a):
    out = Path(a.out)
    (out / "thumbs").mkdir(parents=True, exist_ok=True)
    g, pipe, bg, it = load_model(a.config, a.checkpoint)
    cams = read_colmap(Path(a.sparse))
    # 点云：高斯中心 + 球谐 0 阶颜色，按不透明度过滤后随机采样
    with torch.no_grad():
        xyz = g.get_xyz.detach().cpu().numpy()
        op = g.get_opacity.detach().cpu().numpy().reshape(-1)
        dc = g._features_dc.detach().cpu().numpy().reshape(len(xyz), -1)[:, :3]
    rgb = np.clip(0.28209479177387814 * dc + 0.5, 0, 1)
    keep = np.where(op > a.min_opacity)[0]
    if len(keep) > a.max_points:
        keep = np.random.default_rng(0).choice(keep, a.max_points, replace=False)
    pts = xyz[keep].astype(np.float32)
    col = (rgb[keep] * 255).astype(np.uint8)
    with open(out / "points.bin", "wb") as f:
        f.write(struct.pack("<I", len(pts)))
        f.write(pts.tobytes())
        f.write(col.tobytes())
    # 每个机位 t=0 的缩略图（从模型渲染，宽 480）
    with torch.no_grad():
        for name, c in cams.items():
            s = 480 / c["width"]
            cam = Cam(c["w2c"], c["fx"] * s, c["fy"] * s, round(c["width"] * s), round(c["height"] * s), 0.0)
            save_rgb(render(cam, g, pipe, bg)["render"], out / "thumbs" / f"{name}.jpg", 80)
    meta = {"cameras": cams, "num_points": int(len(pts)), "num_gaussians": int(len(xyz)), "iteration": it,
            "time_duration": [0.0, 10.0]}
    json.dump(meta, open(out / "scene.json", "w"), indent=1)
    print(f"[export] {len(cams)} 个机位，点云 {len(pts)} / {len(xyz)} 个高斯", flush=True)


def cmd_render(a):
    traj = json.load(open(a.traj))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    g, pipe, bg, it = load_model(a.config, a.checkpoint)
    load_s = time.time() - t0
    frames = traj["frames"]
    W, H = traj["width"], traj["height"]
    t1 = time.time()
    with torch.no_grad():
        for i, fr in enumerate(frames):
            cam = Cam(fr["w2c"], fr["fx"], fr["fy"], W, H, fr["timestamp"])
            save_rgb(render(cam, g, pipe, bg)["render"], out / f"{i:05d}.png")
            if i % 10 == 0 or i == len(frames) - 1:
                (out.parent / "progress.json").write_text(json.dumps({"done": i + 1, "total": len(frames)}))
    json.dump({"frames": len(frames), "load_s": round(load_s, 1), "render_s": round(time.time() - t1, 1),
               "fps_render": round(len(frames) / max(time.time() - t1, 1e-6), 1), "iteration": it,
               "peak_vram_gib": round(torch.cuda.max_memory_allocated() / 2 ** 30, 2)},
              open(out.parent / "render_stats.json", "w"), indent=1)
    print(f"[render] {len(frames)} 帧，加载 {load_s:.1f}s，渲染 {time.time() - t1:.1f}s", flush=True)


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    for name in ("export", "render"):
        p = sp.add_parser(name)
        p.add_argument("--config", required=True)
        p.add_argument("--checkpoint", required=True)
        p.add_argument("--out", required=True)
    sp.choices["export"].add_argument("--sparse", required=True)
    sp.choices["export"].add_argument("--max-points", type=int, default=200000)
    sp.choices["export"].add_argument("--min-opacity", type=float, default=0.3)
    sp.choices["render"].add_argument("--traj", required=True)
    a = ap.parse_args()
    with torch.no_grad():
        {"export": cmd_export, "render": cmd_render}[a.cmd](a)


if __name__ == "__main__":
    main()
