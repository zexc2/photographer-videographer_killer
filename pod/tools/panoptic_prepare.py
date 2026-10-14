#!/usr/bin/env python3
"""CMU Panoptic → 4C4D（COLMAP 格式）+ GEN3C（单目视频）输入。在 pod 上运行（mast3r 环境的 python：numpy、opencv）。

对一个序列：
  1. 下载标定 JSON；按配置的起点截取 N 帧 HD 视频（ffmpeg 通过 HTTP 范围请求，只下载需要的片段），
     同时把截出的片段存一份到 Global volume（raw/），下次直接从这里读，不再联网
  2. 用标定的畸变参数去畸变 → images/camXX_YYYY.png（4C4D 只支持 PINHOLE）
  3. 写 COLMAP 文本模型 sparse/0/{cameras,images}.txt：每个机位一条，单位换成米
     （Panoptic 标定是厘米；3DGS 相机 zfar=100，用厘米会把 2.7 m 外的人裁掉）
     points3D.txt 由 mast3r_posed_points.py 生成
  4. GEN3C：输入机位 → gen3c/input.mp4；评估机位真值 → gen3c/eval_gt.mp4
  5. meta.json：机位、内外参、评估机位相对 GEN3C 输入机位的位姿；previews/ 下放缩略图

用法: panoptic_prepare.py --config pod/configs/panoptic.json --seq 171204_pose1 --work DIR --data-root DIR
"""
import argparse
import json
import math
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import cv2
import numpy as np


def log(msg):
    print(f"[prepare] {msg}", file=sys.stderr, flush=True)


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"命令失败: {' '.join(cmd[:6])} …\n{r.stderr[-1500:]}")
    return r.stdout


def rotmat_to_qvec(R):
    """旋转矩阵 → COLMAP 四元数 (qw, qx, qy, qz)，Shepperd 方法，数值稳定。"""
    t = np.trace(R)
    if t > 0:
        s = 0.5 / math.sqrt(t + 1.0)
        q = [0.25 / s, (R[2, 1] - R[1, 2]) * s, (R[0, 2] - R[2, 0]) * s, (R[1, 0] - R[0, 1]) * s]
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = 2.0 * math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2])
        q = [(R[2, 1] - R[1, 2]) / s, 0.25 * s, (R[0, 1] + R[1, 0]) / s, (R[0, 2] + R[2, 0]) / s]
    elif R[1, 1] > R[2, 2]:
        s = 2.0 * math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2])
        q = [(R[0, 2] - R[2, 0]) / s, (R[0, 1] + R[1, 0]) / s, 0.25 * s, (R[1, 2] + R[2, 1]) / s]
    else:
        s = 2.0 * math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1])
        q = [(R[1, 0] - R[0, 1]) / s, (R[0, 2] + R[2, 0]) / s, (R[1, 2] + R[2, 1]) / s, 0.25 * s]
    q = np.array(q)
    q /= np.linalg.norm(q)
    return q if q[0] >= 0 else -q


def load_calibration(seq, endpoint, seq_root):
    p = seq_root / f"calibration_{seq}.json"
    if not p.exists():
        url = f"{endpoint}/webdata/dataset/{seq}/calibration_{seq}.json"
        log(f"下载标定 {url}")
        tmp = p.with_suffix(".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(p)
    cams = {}
    for c in json.load(open(p))["cameras"]:
        if c["type"] == "hd" and c["panel"] == 0:
            cams[c["node"]] = {
                "K": np.array(c["K"], dtype=np.float64),
                "dist": np.array(c["distCoef"], dtype=np.float64),
                "R": np.array(c["R"], dtype=np.float64),
                "t": np.array(c["t"], dtype=np.float64).reshape(3) / 100.0,  # 厘米 → 米
                "size": tuple(c["resolution"]),
            }
    return cams


def fetch_frames(seq, cam, cfg, seq_root, raw_dir, n, start_frame):
    """取 n 帧到 raw_dir/%04d.png。优先用 Global volume 上的存档片段，没有就从网上截取并存档。"""
    raw_dir.mkdir(parents=True, exist_ok=True)
    if len(list(raw_dir.glob("*.png"))) >= n:
        return
    archive = seq_root / "raw" / f"hd_00_{cam:02d}_f{start_frame}_n{n}.mp4"
    if archive.exists():
        log(f"  cam{cam:02d}: 从存档解码 {archive.name}")
        run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(archive), "-frames:v", str(n),
             "-start_number", "0", str(raw_dir / "%04d.png")])
    else:
        url = f"{cfg['endpoint']}/webdata/dataset/{seq}/videos/hd_shared_crf20/hd_00_{cam:02d}.mp4"
        start = start_frame / (30000 / 1001)
        log(f"  cam{cam:02d}: 从网上截取 {n} 帧（起点 {start:.3f}s）")
        # -ss 放在 -i 前 + 解码输出 = 精确定位到帧（各 HD 机位硬件同步，同一帧号即同一时刻）
        run(["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{start:.4f}", "-i", url, "-frames:v", str(n),
             "-start_number", "0", str(raw_dir / "%04d.png")])
        archive.parent.mkdir(parents=True, exist_ok=True)
        tmp = archive.with_name(archive.stem + ".part.mp4")
        run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", "30000/1001", "-i", str(raw_dir / "%04d.png"),
             "-c:v", "libx264", "-crf", "12", "-preset", "medium", "-pix_fmt", "yuv420p", str(tmp)])
        tmp.replace(archive)
    got = len(list(raw_dir.glob("*.png")))
    if got < n:
        raise RuntimeError(f"cam{cam:02d} 只取到 {got}/{n} 帧")


def undistort_cam(cam, c, raw_dir, img_dir, n):
    K, dist = c["K"], c["dist"]
    w, h = c["size"]
    m1, m2 = cv2.initUndistortRectifyMap(K, dist, None, K, (w, h), cv2.CV_32FC1)
    for i in range(n):
        dst = img_dir / f"cam{cam:02d}_{i:04d}.png"
        if dst.exists():
            continue
        img = cv2.imread(str(raw_dir / f"{i:04d}.png"))
        if img is None:
            raise RuntimeError(f"读不到 {raw_dir}/{i:04d}.png")
        cv2.imwrite(str(dst), cv2.remap(img, m1, m2, cv2.INTER_LINEAR))


def write_colmap(cams, use, sparse_dir):
    sparse_dir.mkdir(parents=True, exist_ok=True)
    with open(sparse_dir / "cameras.txt", "w") as f:
        f.write("# CAMERA_ID MODEL WIDTH HEIGHT fx fy cx cy（Panoptic 标定，已去畸变）\n")
        for i, cam in enumerate(use, 1):
            K = cams[cam]["K"]
            w, h = cams[cam]["size"]
            f.write(f"{i} PINHOLE {w} {h} {K[0,0]:.6f} {K[1,1]:.6f} {K[0,2]:.6f} {K[1,2]:.6f}\n")
    with open(sparse_dir / "images.txt", "w") as f:
        f.write("# IMAGE_ID QW QX QY QZ TX TY TZ CAMERA_ID NAME（世界→相机，单位米）\n")
        for i, cam in enumerate(use, 1):
            q = rotmat_to_qvec(cams[cam]["R"])
            t = cams[cam]["t"]
            f.write(f"{i} {q[0]:.9f} {q[1]:.9f} {q[2]:.9f} {q[3]:.9f} {t[0]:.6f} {t[1]:.6f} {t[2]:.6f} {i} cam{cam:02d}_0000.png\n\n")


def w2c(c):
    T = np.eye(4)
    T[:3, :3], T[:3, 3] = c["R"], c["t"]
    return T


def encode_video(img_dir, cam, n, out):
    if out.exists():
        return
    tmp = out.with_name(out.stem + ".part.mp4")
    run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", "30000/1001", "-start_number", "0",
         "-i", str(img_dir / f"cam{cam:02d}_%04d.png"), "-frames:v", str(n),
         "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", str(tmp)])
    tmp.replace(out)


def contact_sheet(img_dir, cams, labels, out, frame=0):
    tiles = []
    for cam in cams:
        img = cv2.imread(str(img_dir / f"cam{cam:02d}_{frame:04d}.png"))
        img = cv2.resize(img, (480, 270))
        cv2.putText(img, labels[cam], (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
        tiles.append(img)
    while len(tiles) % 3:
        tiles.append(np.zeros_like(tiles[0]))
    rows = [np.hstack(tiles[i:i + 3]) for i in range(0, len(tiles), 3)]
    cv2.imwrite(str(out), np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 85])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--seq", required=True)
    ap.add_argument("--work", required=True, help="容器盘上的工作目录（生成的数据集放这里）")
    ap.add_argument("--data-root", required=True, help="Global volume 上的 panoptic 目录（存标定和截取的片段）")
    ap.add_argument("--frames", type=int, help="覆盖配置里的帧数（测试用）")
    a = ap.parse_args()

    cfg = json.load(open(a.config))
    s = next((x for x in cfg["sequences"] if x["name"] == a.seq), None)
    if s is None:
        sys.exit(f"配置里没有序列 {a.seq}")
    n = a.frames or cfg["num_frames"]
    start_frame = int(round(s["start_sec"] * 30000 / 1001))
    train, ev, g_in = s["train_cams"], s["eval_cam"], s["gen3c_input_cam"]
    use = sorted(set(train + [ev, g_in]))
    seq_root = Path(a.data_root) / a.seq
    seq_root.mkdir(parents=True, exist_ok=True)
    work = Path(a.work)
    ds = work / "4c4d"            # 4C4D 数据集目录
    img_dir = ds / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    prev = work / "previews"
    prev.mkdir(parents=True, exist_ok=True)

    cams = load_calibration(a.seq, cfg["endpoint"], seq_root)
    missing = [c for c in use if c not in cams]
    if missing:
        sys.exit(f"标定里没有机位 {missing}")

    log(f"{a.seq}: 机位 {use}，帧 {start_frame}..{start_frame + n - 1}")
    for cam in use:
        raw = work / "raw" / f"cam{cam:02d}"
        fetch_frames(a.seq, cam, cfg, seq_root, raw, n, start_frame)
        undistort_cam(cam, cams[cam], raw, img_dir, n)
        subprocess.run(["rm", "-rf", str(raw)])  # 原始帧用完即删，省容器盘
    log("去畸变完成")

    write_colmap(cams, use, ds / "sparse" / "0")

    g = work / "gen3c"
    g.mkdir(exist_ok=True)
    encode_video(img_dir, g_in, n, g / "input.mp4")
    encode_video(img_dir, ev, n, g / "eval_gt.mp4")

    # 评估机位相对 GEN3C 输入机位的位姿（输入相机坐标系 → 评估相机坐标系，米）
    T_rel = w2c(cams[ev]) @ np.linalg.inv(w2c(cams[g_in]))
    center = lambda c: -c["R"].T @ c["t"]
    baseline = float(np.linalg.norm(center(cams[ev]) - center(cams[g_in])))
    angle = float(np.degrees(np.arccos(np.clip((np.trace(T_rel[:3, :3]) - 1) / 2, -1, 1))))
    meta = {
        "sequence": a.seq, "desc": s.get("desc"), "fps": 30000 / 1001, "num_frames": n,
        "start_frame": start_frame, "units": "meters",
        "train_cams": train, "eval_cam": ev, "gen3c_input_cam": g_in,
        "fourc4d_training_view": ",".join(str(c) for c in sorted(train)),
        "cameras": {f"cam{c:02d}": {"K": cams[c]["K"].tolist(), "R": cams[c]["R"].tolist(),
                                    "t": cams[c]["t"].tolist(), "center": center(cams[c]).tolist(),
                                    "size": list(cams[c]["size"])} for c in use},
        "eval_from_gen3c_input": {"T": T_rel.tolist(), "baseline_m": round(baseline, 3),
                                  "rotation_deg": round(angle, 1)},
        "license": "CMU Panoptic Studio Dataset：仅限非商业研究用途",
    }
    json.dump(meta, open(work / "meta.json", "w"), indent=2, ensure_ascii=False)
    labels = {c: f"cam{c:02d}" + (" EVAL" if c == ev else " IN" if c in train else "") +
                 (" +GEN3C" if c == g_in else "") for c in use}
    contact_sheet(img_dir, use, labels, prev / f"{a.seq}_cams.jpg")
    log(f"完成：{len(use)} 个机位 × {n} 帧；评估机位距 GEN3C 输入 {baseline:.2f} m、转角 {angle:.1f}°")


if __name__ == "__main__":
    main()
