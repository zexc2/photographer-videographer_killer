#!/usr/bin/env python3
"""Stage 4: build the camera trajectories shared by both models and align GEN3C (ViPE) scale to real metres. Run in the gen3c env (needs OpenEXR).

Writes to <work>/stage4/:
  gen3c_eval.npz   GEN3C eval trajectory: all 121 frames at the eval camera. T = eval camera pose relative to the input camera (scaled by k), K = intrinsics in GEN3C's 704x1280 frame
  gen3c_path.npz   GEN3C camera-motion trajectory: smooth move from the input camera to the eval camera
  4c4d_path.npz    4C4D camera-motion trajectory (the same path): world w2c (metres), focal length, timestamps
  trajectories.json  scale alignment results etc.

Scale alignment: GEN3C's 3D cache comes from ViPE's estimated depth (monocular, "self-assessed metres"), while the eval camera's relative pose is in real metres.
Project the MASt3R point cloud (triangulated from the real calibration, metric) into the input camera, compare with ViPE depth at the same pixels, take the median ratio k,
and scale the translation of relative poses by k. This is the usual practice for evaluating monocular methods on novel views.
"""
import argparse
import json
import zipfile
from pathlib import Path

import numpy as np

GEN3C_RESIZE = (720, 1280)  # (h, w), same as the load_vipe_data argument in gen3c_dynamic.py
GEN3C_CROP = (704, 1280)
NUM_FRAMES = 121


def read_points(path):
    pts = []
    for line in open(path):
        if line.startswith("#") or not line.strip():
            continue
        e = line.split()
        pts.append([float(e[1]), float(e[2]), float(e[3])])
    return np.array(pts)


def read_vipe_depth(zip_path, frame=0):
    import OpenEXR
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open(f"{frame:05d}.exr") as f:
            exr = OpenEXR.InputFile(f)
            dw = exr.header()["dataWindow"]
            w, h = dw.max.x - dw.min.x + 1, dw.max.y - dw.min.y + 1
            return np.frombuffer(exr.channel("Z"), np.float16).astype(np.float32).reshape(h, w)


def w2c_of(cam):
    T = np.eye(4)
    T[:3, :3], T[:3, 3] = np.array(cam["R"]), np.array(cam["t"])
    return T


def quat_from_R(R):
    t = np.trace(R)
    if t > 0:
        s = 0.5 / np.sqrt(t + 1)
        q = [0.25 / s, (R[2, 1] - R[1, 2]) * s, (R[0, 2] - R[2, 0]) * s, (R[1, 0] - R[0, 1]) * s]
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = 2 * np.sqrt(1 + R[0, 0] - R[1, 1] - R[2, 2])
        q = [(R[2, 1] - R[1, 2]) / s, 0.25 * s, (R[0, 1] + R[1, 0]) / s, (R[0, 2] + R[2, 0]) / s]
    elif R[1, 1] > R[2, 2]:
        s = 2 * np.sqrt(1 + R[1, 1] - R[0, 0] - R[2, 2])
        q = [(R[0, 2] - R[2, 0]) / s, (R[0, 1] + R[1, 0]) / s, 0.25 * s, (R[1, 2] + R[2, 1]) / s]
    else:
        s = 2 * np.sqrt(1 + R[2, 2] - R[0, 0] - R[1, 1])
        q = [(R[1, 0] - R[0, 1]) / s, (R[0, 2] + R[2, 0]) / s, (R[1, 2] + R[2, 1]) / s, 0.25 * s]
    q = np.array(q)
    return q / np.linalg.norm(q)


def R_from_quat(q):
    w, x, y, z = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                     [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                     [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])


def slerp(q0, q1, t):
    d = np.dot(q0, q1)
    if d < 0:
        q1, d = -q1, -d
    if d > 0.9995:
        q = q0 + t * (q1 - q0)
        return q / np.linalg.norm(q)
    th = np.arccos(d)
    return (np.sin((1 - t) * th) * q0 + np.sin(t * th) * q1) / np.sin(th)


def interp_path(wa, wb, n):
    """Smoothly interpolate between two w2c (camera centre linear, orientation slerp, ease-in/ease-out at both ends). Returns (n,4,4) w2c and the blend weights."""
    Ca, Cb = -wa[:3, :3].T @ wa[:3, 3], -wb[:3, :3].T @ wb[:3, 3]
    qa, qb = quat_from_R(wa[:3, :3].T), quat_from_R(wb[:3, :3].T)  # c2w rotations
    out, ss = [], []
    for i in range(n):
        u = i / (n - 1)
        s = u * u * (3 - 2 * u)  # smoothstep
        Rc2w = R_from_quat(slerp(qa, qb, s))
        C = (1 - s) * Ca + s * Cb
        T = np.eye(4)
        T[:3, :3] = Rc2w.T
        T[:3, 3] = -Rc2w.T @ C
        out.append(T)
        ss.append(s)
    return np.stack(out), np.array(ss)


def to_gen3c_K(K, src_wh):
    """Full-resolution intrinsics -> intrinsics after GEN3C resize + centre crop (same as vipe_utils._adjust_intrinsics_for_resize_and_crop)."""
    sw, sh = src_wh
    (rh, rw), (ch, cw) = GEN3C_RESIZE, GEN3C_CROP
    K = np.array(K, dtype=np.float64).copy()
    K[0, 0] *= rw / sw
    K[0, 2] *= rw / sw
    K[1, 1] *= rh / sh
    K[1, 2] *= rh / sh
    K[0, 2] -= max((rw - cw) // 2, 0)
    K[1, 2] -= max((rh - ch) // 2, 0)
    return K


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True, help="stage 3 sequence work dir (contains meta.json, 4c4d/, gen3c/vipe/)")
    a = ap.parse_args()
    W = Path(a.work)
    meta = json.load(open(W / "meta.json"))
    cams = meta["cameras"]
    cin, cev = f"cam{meta['gen3c_input_cam']:02d}", f"cam{meta['eval_cam']:02d}"
    w_in, w_ev = w2c_of(cams[cin]), w2c_of(cams[cev])
    K_in, K_ev = np.array(cams[cin]["K"]), np.array(cams[cev]["K"])
    src_wh = tuple(cams[cin]["size"])
    out = W / "stage4"
    out.mkdir(exist_ok=True)

    # ---- scale alignment: depth of MASt3R points (real metres) in the input camera vs ViPE depth
    X = read_points(W / "4c4d" / "sparse" / "0" / "points3D.txt")
    Xc = (w_in[:3, :3] @ X.T).T + w_in[:3, 3]
    z_true = Xc[:, 2]
    uv = (K_in @ Xc.T).T
    uv = uv[:, :2] / uv[:, 2:3]
    d_vipe = read_vipe_depth(W / "gen3c" / "vipe" / "depth" / "input.zip", 0)
    dh, dw = d_vipe.shape
    px = np.round(uv[:, 0] * dw / src_wh[0]).astype(int)
    py = np.round(uv[:, 1] * dh / src_wh[1]).astype(int)
    ok = (z_true > 0.1) & (px >= 0) & (px < dw) & (py >= 0) & (py < dh)
    zv = d_vipe[py[ok], px[ok]]
    ok2 = np.isfinite(zv) & (zv > 0)
    ratio = zv[ok2] / z_true[ok][ok2]
    k = float(np.median(ratio)) if len(ratio) > 50 else 1.0
    spread = float(np.median(np.abs(ratio - k)) / k) if len(ratio) > 50 else None

    def rel(w2c):  # pose relative to the input camera, translation converted to ViPE scale
        T = w2c @ np.linalg.inv(w_in)
        T[:3, 3] *= k
        return T

    n = NUM_FRAMES
    # ---- eval trajectory: every frame at the eval camera
    T_eval = np.stack([rel(w_ev)] * n)
    K_eval = np.stack([to_gen3c_K(K_ev, src_wh)] * n)
    np.savez(out / "gen3c_eval.npz", T=T_eval.astype(np.float32), K=K_eval.astype(np.float32))

    # ---- camera-motion trajectory: input camera -> eval camera (shared by both models)
    path_w2c, s = interp_path(w_in, w_ev, n)
    K_path_full = np.stack([(1 - si) * K_in + si * K_ev for si in s])
    np.savez(out / "gen3c_path.npz", T=np.stack([rel(w) for w in path_w2c]).astype(np.float32),
             K=np.stack([to_gen3c_K(Kf, src_wh) for Kf in K_path_full]).astype(np.float32))
    # 4C4D: world w2c (metres), full-resolution focal length, timestamps (same as 4C4D's data loader: frame index / frame count x 10)
    np.savez(out / "4c4d_path.npz", w2c=path_w2c.astype(np.float32),
             fx=K_path_full[:, 0, 0].astype(np.float32), fy=K_path_full[:, 1, 1].astype(np.float32),
             width=src_wh[0], height=src_wh[1],
             timestamp=(np.arange(n) / meta["num_frames"] * 10.0).astype(np.float32))

    info = {"vipe_scale_k": round(k, 4), "scale_points_used": int(len(ratio)),
            "scale_relative_spread": None if spread is None else round(spread, 3),
            "eval_baseline_m": meta["eval_from_gen3c_input"]["baseline_m"],
            "eval_rotation_deg": meta["eval_from_gen3c_input"]["rotation_deg"],
            "frames": n, "input_cam": cin, "eval_cam": cev}
    json.dump(info, open(out / "trajectories.json", "w"), indent=2)
    print(json.dumps(info, ensure_ascii=False))


if __name__ == "__main__":
    main()
