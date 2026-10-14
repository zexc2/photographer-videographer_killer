#!/usr/bin/env python3
"""用 MASt3R 稠密匹配 + 已知相机位姿三角化，生成 4C4D 的初始点云 sparse/0/points3D.txt。

4C4D 官方流程用 MAtCha 的 posed 模式（MASt3R-SfM）做这件事：稀疏视角下 COLMAP 点太少，用 MASt3R 补稠密初始化。
这里不装 MAtCha（editable 安装 + 写死 CUDA 11.8 路径，无法打包），直接用 naver/mast3r：
  对训练机位两两配对 → MASt3R 推理 → fast_reciprocal_NNs 得到像素匹配
  → 用 Panoptic 标定（已去畸变、单位米）三角化 → 按深度、重投影误差、离场景中心距离过滤 → 合并、体素去重
只用训练机位，评估机位完全不参与（避免信息泄漏）。

在 mast3r 环境里运行：PYTHONPATH=$ATLAS_SRC/mast3r python mast3r_posed_points.py --data DIR --cams 5,8,24,25 --weights X.pth
"""
import argparse
import itertools
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np


def log(msg):
    print(f"[mast3r-points] {msg}", file=sys.stderr, flush=True)


def read_colmap_text(sparse):
    cams, imgs = {}, {}
    for line in open(sparse / "cameras.txt"):
        if line.startswith("#") or not line.strip():
            continue
        e = line.split()
        fx, fy, cx, cy = map(float, e[4:8])
        cams[int(e[0])] = (np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]]), int(e[2]), int(e[3]))
    for line in open(sparse / "images.txt"):
        if line.startswith("#") or not line.strip():
            continue
        e = line.split()
        qw, qx, qy, qz = map(float, e[1:5])
        R = np.array([
            [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qw * qz), 2 * (qx * qz + qw * qy)],
            [2 * (qx * qy + qw * qz), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qw * qx)],
            [2 * (qx * qz - qw * qy), 2 * (qy * qz + qw * qx), 1 - 2 * (qx * qx + qy * qy)]])
        t = np.array(list(map(float, e[5:8])))
        name = e[9]
        K, w, h = cams[int(e[8])]
        imgs[name.split("_")[0]] = {"K": K, "R": R, "t": t, "w": w, "h": h}
    return imgs


def triangulate_pair(c1, c2, p1, p2, scene_c, max_reproj, max_radius):
    """两个已知相机（K、R、t，世界→相机）的像素匹配 → 三维点 + 过滤掩码。"""
    P1 = c1["K"] @ np.hstack([c1["R"], c1["t"][:, None]])
    P2 = c2["K"] @ np.hstack([c2["R"], c2["t"][:, None]])
    Xh = cv2.triangulatePoints(P1, P2, p1.T.astype(np.float64), p2.T.astype(np.float64))
    X = (Xh[:3] / Xh[3]).T
    keep = np.ones(len(X), bool)
    for c, p in ((c1, p1), (c2, p2)):
        Xc = (c["R"] @ X.T).T + c["t"]
        keep &= Xc[:, 2] > 0.1
        proj = (c["K"] @ Xc.T).T
        proj = proj[:, :2] / np.maximum(proj[:, 2:3], 1e-9)
        keep &= np.linalg.norm(proj - p, axis=1) < max_reproj
    keep &= np.linalg.norm(X - scene_c, axis=1) < max_radius
    return X, keep


def dust3r_input(path, idx, size=512):
    """和 dust3r.utils.image.load_images 相同的预处理，同时返回 缩放后像素 → 原图像素 的映射。"""
    from PIL import Image
    from dust3r.utils.image import ImgNorm
    img = Image.open(path).convert("RGB")
    W0, H0 = img.size
    S = size / max(W0, H0)
    Wr, Hr = round(W0 * S), round(H0 * S)
    img = img.resize((Wr, Hr), Image.LANCZOS)
    cx, cy = Wr // 2, Hr // 2
    halfw, halfh = ((2 * cx) // 16) * 8, ((2 * cy) // 16) * 8
    img = img.crop((cx - halfw, cy - halfh, cx + halfw, cy + halfh))
    view = dict(img=ImgNorm(img)[None], true_shape=np.int32([img.size[::-1]]), idx=idx, instance=str(idx))
    off = np.array([cx - halfw, cy - halfh], dtype=np.float64)
    scale = np.array([W0 / Wr, H0 / Hr])
    return view, (lambda xy: (xy + off + 0.5) * scale - 0.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="4C4D 数据集目录（含 images/ 和 sparse/0/）")
    ap.add_argument("--cams", required=True, help="训练机位，如 5,8,24,25")
    ap.add_argument("--weights", required=True)
    ap.add_argument("--frame", type=int, default=0)
    ap.add_argument("--subsample", type=int, default=2, help="匹配网格步长（越小点越多）")
    ap.add_argument("--max-reproj", type=float, default=2.0, help="重投影误差上限（像素，原图分辨率）")
    ap.add_argument("--max-radius", type=float, default=4.0, help="离相机中心均值的最大距离（米）")
    ap.add_argument("--voxel", type=float, default=0.005, help="体素去重边长（米）")
    ap.add_argument("--preview", help="把点云投影到各机位的预览图输出目录")
    a = ap.parse_args()

    from mast3r.model import AsymmetricMASt3R
    from mast3r.fast_nn import fast_reciprocal_NNs
    from dust3r.inference import inference

    data = Path(a.data)
    sparse = data / "sparse" / "0"
    cams = read_colmap_text(sparse)
    train = [f"cam{int(c):02d}" for c in a.cams.split(",")]
    for c in train:
        if c not in cams:
            sys.exit(f"images.txt 里没有 {c}")
    centers = np.array([-cams[c]["R"].T @ cams[c]["t"] for c in train])
    scene_c = centers.mean(0)

    t0 = time.time()
    dev = "cuda"
    model = AsymmetricMASt3R.from_pretrained(a.weights).to(dev).eval()
    log(f"模型加载 {time.time() - t0:.1f}s")

    paths = {c: data / "images" / f"{c}_{a.frame:04d}.png" for c in train}
    views, to_full = {}, {}
    for i, c in enumerate(train):
        views[c], to_full[c] = dust3r_input(paths[c], i)
    full_imgs = {c: cv2.imread(str(paths[c]))[:, :, ::-1] for c in train}

    all_xyz, all_rgb, stats = [], [], []
    for ci, cj in itertools.combinations(train, 2):
        out = inference([(views[ci], views[cj])], model, dev, batch_size=1, verbose=False)
        d1 = out["pred1"]["desc"].squeeze(0).detach()
        d2 = out["pred2"]["desc"].squeeze(0).detach()
        m1, m2 = fast_reciprocal_NNs(d1, d2, subsample_or_initxy1=a.subsample, device=dev, dist="dot", block_size=2 ** 13)
        H, W = views[ci]["true_shape"][0]
        ok = (m1[:, 0] >= 3) & (m1[:, 0] < W - 3) & (m1[:, 1] >= 3) & (m1[:, 1] < H - 3) & \
             (m2[:, 0] >= 3) & (m2[:, 0] < W - 3) & (m2[:, 1] >= 3) & (m2[:, 1] < H - 3)
        p1 = to_full[ci](m1[ok].astype(np.float64))
        p2 = to_full[cj](m2[ok].astype(np.float64))
        X, keep = triangulate_pair(cams[ci], cams[cj], p1, p2, scene_c, a.max_reproj, a.max_radius)
        px = np.clip(np.round(p1[keep]).astype(int), 0, [cams[ci]["w"] - 1, cams[ci]["h"] - 1])
        all_xyz.append(X[keep])
        all_rgb.append(full_imgs[ci][px[:, 1], px[:, 0]])
        stats.append({"pair": f"{ci}-{cj}", "matches": int(ok.sum()), "kept": int(keep.sum())})
        log(f"{ci}-{cj}: 匹配 {ok.sum()}，保留 {keep.sum()}")

    xyz = np.concatenate(all_xyz)
    rgb = np.concatenate(all_rgb)
    # 体素去重：同一体素只留一个点
    _, idx = np.unique(np.floor(xyz / a.voxel).astype(np.int64), axis=0, return_index=True)
    xyz, rgb = xyz[idx], rgb[idx]
    with open(sparse / "points3D.txt", "w") as f:
        f.write("# POINT3D_ID X Y Z R G B ERROR（MASt3R 匹配 + 已知位姿三角化，单位米）\n")
        for i, (p, c) in enumerate(zip(xyz, rgb), 1):
            f.write(f"{i} {p[0]:.5f} {p[1]:.5f} {p[2]:.5f} {int(c[0])} {int(c[1])} {int(c[2])} 0\n")
    (sparse / "points3D.ply").unlink(missing_ok=True)  # 让 4C4D 按新点云重新生成 ply
    summary = {"points": int(len(xyz)), "pairs": stats, "seconds": round(time.time() - t0, 1),
               "frame": a.frame, "train_cams": train}
    json.dump(summary, open(data / "points3D_stats.json", "w"), indent=2)
    log(f"写出 {len(xyz)} 个点，用时 {summary['seconds']}s")

    if a.preview:
        # 投影到所有机位（包括评估机位）检查标定 / 单位是否正确：点应该贴在人和物体上
        out = Path(a.preview)
        out.mkdir(parents=True, exist_ok=True)
        sel = np.random.default_rng(0).choice(len(xyz), min(len(xyz), 40000), replace=False)
        for c, cam in cams.items():
            img = cv2.imread(str(data / "images" / f"{c}_{a.frame:04d}.png"))
            Xc = (cam["R"] @ xyz[sel].T).T + cam["t"]
            front = Xc[:, 2] > 0.1
            uv = (cam["K"] @ Xc[front].T).T
            uv = (uv[:, :2] / uv[:, 2:3]).astype(int)
            for (u, v), col in zip(uv, rgb[sel][front]):
                if 0 <= u < img.shape[1] and 0 <= v < img.shape[0]:
                    cv2.circle(img, (u, v), 2, (0, 255, 0), -1)
            tag = "train" if c in train else "held-out"
            cv2.putText(img, f"{c} ({tag})", (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 255, 255), 3)
            cv2.imwrite(str(out / f"points_on_{c}.jpg"), cv2.resize(img, (960, 540)), [cv2.IMWRITE_JPEG_QUALITY, 80])


if __name__ == "__main__":
    import torch
    with torch.no_grad():
        main()
