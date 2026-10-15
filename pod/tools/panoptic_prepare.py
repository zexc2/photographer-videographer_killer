#!/usr/bin/env python3
"""CMU Panoptic -> 4C4D (COLMAP format) + GEN3C (monocular video) inputs. Runs on the pod (python from the mast3r env: numpy, opencv).

For one sequence:
  1. Download the calibration JSON; trim N HD frames from the configured start (ffmpeg via HTTP range requests, downloading only the needed part),
     and save the trimmed clips to Global volume (raw/) so later runs read them from there without going online
  2. Undistort with the calibrated distortion parameters -> images/camXX_YYYY.png (4C4D only supports PINHOLE)
  3. Write the COLMAP text model sparse/0/{cameras,images}.txt: one entry per camera, units converted to metres
     (Panoptic calibration is in centimetres; the 3DGS camera has zfar=100, so centimetres would clip a person 2.7 m away)
     points3D.txt is produced by mast3r_posed_points.py
  4. GEN3C: input camera -> gen3c/input.mp4; eval camera ground truth -> gen3c/eval_gt.mp4
  5. meta.json: cameras, intrinsics/extrinsics, eval camera pose relative to the GEN3C input camera; thumbnails under previews/

Usage: panoptic_prepare.py --config pod/configs/panoptic.json --seq 171204_pose1 --work DIR --data-root DIR
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
        raise RuntimeError(f"command failed: {' '.join(cmd[:6])} ...\n{r.stderr[-1500:]}")
    return r.stdout


def rotmat_to_qvec(R):
    """Rotation matrix -> COLMAP quaternion (qw, qx, qy, qz), Shepperd's method, numerically stable."""
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
        log(f"downloading calibration {url}")
        tmp = p.with_suffix(".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(p)
    cams = {}
    for c in json.load(open(p))["cameras"]:
        if c["type"] == "hd" and c["panel"] == 0:
            K = np.array(c["K"], dtype=np.float64)
            w, h = c["resolution"]
            # While undistorting, also move the principal point to the exact image centre: 4C4D's PINHOLE reader uses only the focal length and assumes a centred principal point;
            # Panoptic's original principal points are up to ~18 px off-centre, which would misalign every camera systematically (found 2026-10-14)
            K_new = K.copy()
            K_new[0, 2], K_new[1, 2] = w / 2.0, h / 2.0
            cams[c["node"]] = {
                "K": K_new,
                "K_orig": K,
                "dist": np.array(c["distCoef"], dtype=np.float64),
                "R": np.array(c["R"], dtype=np.float64),
                "t": np.array(c["t"], dtype=np.float64).reshape(3) / 100.0,  # centimetres -> metres
                "size": tuple(c["resolution"]),
            }
    return cams


def fetch_frames(seq, cam, cfg, seq_root, raw_dir, n, start_frame):
    """Get n frames into raw_dir/%04d.png. Prefer the archived clip on Global volume; otherwise trim from the web and archive it."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    if len(list(raw_dir.glob("*.png"))) >= n:
        return
    archive = seq_root / "raw" / f"hd_00_{cam:02d}_f{start_frame}_n{n}.mp4"
    if archive.exists():
        log(f"  cam{cam:02d}: decoding from archive {archive.name}")
        run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(archive), "-frames:v", str(n),
             "-start_number", "0", str(raw_dir / "%04d.png")])
    else:
        url = f"{cfg['endpoint']}/webdata/dataset/{seq}/videos/hd_shared_crf20/hd_00_{cam:02d}.mp4"
        start = start_frame / (30000 / 1001)
        log(f"  cam{cam:02d}: trimming {n} frames from the web (start {start:.3f}s)")
        # -ss before -i + decoding the output = frame-accurate seek (HD cameras are hardware-synced, so the same frame index is the same instant)
        run(["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{start:.4f}", "-i", url, "-frames:v", str(n),
             "-start_number", "0", str(raw_dir / "%04d.png")])
        archive.parent.mkdir(parents=True, exist_ok=True)
        tmp = archive.with_name(archive.stem + ".part.mp4")
        run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", "30000/1001", "-i", str(raw_dir / "%04d.png"),
             "-c:v", "libx264", "-crf", "12", "-preset", "medium", "-pix_fmt", "yuv420p", str(tmp)])
        tmp.replace(archive)
    got = len(list(raw_dir.glob("*.png")))
    if got < n:
        raise RuntimeError(f"cam{cam:02d}: got only {got}/{n} frames")


def undistort_cam(cam, c, raw_dir, img_dir, n):
    w, h = c["size"]
    # original intrinsics + distortion -> new pinhole camera with a centred principal point
    m1, m2 = cv2.initUndistortRectifyMap(c["K_orig"], c["dist"], None, c["K"], (w, h), cv2.CV_32FC1)
    for i in range(n):
        dst = img_dir / f"cam{cam:02d}_{i:04d}.png"
        if dst.exists():
            continue
        img = cv2.imread(str(raw_dir / f"{i:04d}.png"))
        if img is None:
            raise RuntimeError(f"cannot read {raw_dir}/{i:04d}.png")
        cv2.imwrite(str(dst), cv2.remap(img, m1, m2, cv2.INTER_LINEAR))


def write_colmap(cams, use, sparse_dir):
    sparse_dir.mkdir(parents=True, exist_ok=True)
    with open(sparse_dir / "cameras.txt", "w") as f:
        f.write("# CAMERA_ID MODEL WIDTH HEIGHT fx fy cx cy (Panoptic calibration, undistorted, principal point moved to centre)\n")
        for i, cam in enumerate(use, 1):
            K = cams[cam]["K"]
            w, h = cams[cam]["size"]
            f.write(f"{i} PINHOLE {w} {h} {K[0,0]:.6f} {K[1,1]:.6f} {K[0,2]:.6f} {K[1,2]:.6f}\n")
    with open(sparse_dir / "images.txt", "w") as f:
        f.write("# IMAGE_ID QW QX QY QZ TX TY TZ CAMERA_ID NAME (world->camera, metres)\n")
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
    ap.add_argument("--work", required=True, help="work dir on the container disk (the generated dataset goes here)")
    ap.add_argument("--data-root", required=True, help="panoptic dir on Global volume (stores calibration and trimmed clips)")
    ap.add_argument("--frames", type=int, help="override the frame count from the config (for testing)")
    a = ap.parse_args()

    cfg = json.load(open(a.config))
    s = next((x for x in cfg["sequences"] if x["name"] == a.seq), None)
    if s is None:
        sys.exit(f"sequence {a.seq} not in config")
    n = a.frames or cfg["num_frames"]
    start_frame = int(round(s["start_sec"] * 30000 / 1001))
    train, ev, g_in = s["train_cams"], s["eval_cam"], s["gen3c_input_cam"]
    use = sorted(set(train + [ev, g_in]))
    seq_root = Path(a.data_root) / a.seq
    seq_root.mkdir(parents=True, exist_ok=True)
    work = Path(a.work)
    ds = work / "4c4d"            # 4C4D dataset dir
    img_dir = ds / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    prev = work / "previews"
    prev.mkdir(parents=True, exist_ok=True)

    cams = load_calibration(a.seq, cfg["endpoint"], seq_root)
    missing = [c for c in use if c not in cams]
    if missing:
        sys.exit(f"cameras missing from calibration: {missing}")

    log(f"{a.seq}: cameras {use}, frames {start_frame}..{start_frame + n - 1}")
    for cam in use:
        raw = work / "raw" / f"cam{cam:02d}"
        fetch_frames(a.seq, cam, cfg, seq_root, raw, n, start_frame)
        undistort_cam(cam, cams[cam], raw, img_dir, n)
        subprocess.run(["rm", "-rf", str(raw)])  # delete raw frames once used, to save container disk
    log("undistortion done")

    write_colmap(cams, use, ds / "sparse" / "0")

    g = work / "gen3c"
    g.mkdir(exist_ok=True)
    encode_video(img_dir, g_in, n, g / "input.mp4")
    encode_video(img_dir, ev, n, g / "eval_gt.mp4")

    # eval camera pose relative to the GEN3C input camera (input camera frame -> eval camera frame, metres)
    T_rel = w2c(cams[ev]) @ np.linalg.inv(w2c(cams[g_in]))
    center = lambda c: -c["R"].T @ c["t"]
    baseline = float(np.linalg.norm(center(cams[ev]) - center(cams[g_in])))
    angle = float(np.degrees(np.arccos(np.clip((np.trace(T_rel[:3, :3]) - 1) / 2, -1, 1))))
    meta = {
        "sequence": a.seq, "desc": s.get("desc"), "fps": 30000 / 1001, "num_frames": n,
        "start_frame": start_frame, "units": "meters",
        "train_cams": train, "eval_cam": ev, "gen3c_input_cam": g_in,
        "fourc4d_training_view": ",".join(str(c) for c in sorted(train)),
        "cameras": {f"cam{c:02d}": {"K": cams[c]["K"].tolist(), "K_orig": cams[c]["K_orig"].tolist(),
                                    "R": cams[c]["R"].tolist(),
                                    "t": cams[c]["t"].tolist(), "center": center(cams[c]).tolist(),
                                    "size": list(cams[c]["size"])} for c in use},
        "eval_from_gen3c_input": {"T": T_rel.tolist(), "baseline_m": round(baseline, 3),
                                  "rotation_deg": round(angle, 1)},
        "license": "CMU Panoptic Studio Dataset: non-commercial research use only",
    }
    json.dump(meta, open(work / "meta.json", "w"), indent=2, ensure_ascii=False)
    labels = {c: f"cam{c:02d}" + (" EVAL" if c == ev else " IN" if c in train else "") +
                 (" +GEN3C" if c == g_in else "") for c in use}
    contact_sheet(img_dir, use, labels, prev / f"{a.seq}_cams.jpg")
    log(f"done: {len(use)} cameras x {n} frames; eval camera is {baseline:.2f} m from the GEN3C input, rotated {angle:.1f}°")


if __name__ == "__main__":
    main()
