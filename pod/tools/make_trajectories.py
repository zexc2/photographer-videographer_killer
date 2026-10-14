#!/usr/bin/env python3
"""阶段 4：生成两个模型共用的相机轨迹，并把 GEN3C（ViPE）的尺度对齐到真实米制。在 gen3c 环境里运行（需要 OpenEXR）。

输出到 <work>/stage4/：
  gen3c_eval.npz   GEN3C 评估轨迹：121 帧都在评估机位。T = 评估相机相对输入相机的位姿（已乘尺度 k），K = GEN3C 704×1280 坐标下的内参
  gen3c_path.npz   GEN3C 运镜轨迹：输入机位 → 评估机位平滑移动
  4c4d_path.npz    4C4D 运镜轨迹（同一条）：世界坐标 w2c（米）、焦距、时间戳
  trajectories.json  尺度对齐结果等

尺度对齐：GEN3C 的 3D 缓存来自 ViPE 估计的深度（单目，"自认为的米"），而评估机位的相对位姿是真实米制。
把 MASt3R 点云（由真实标定三角化，米制）投影到输入机位，与同一像素的 ViPE 深度比较，取中位数比例 k，
用 k 缩放相对位姿的平移。这是单目方法做新视角评估的常规做法。
"""
import argparse
import json
import zipfile
from pathlib import Path

import numpy as np

GEN3C_RESIZE = (720, 1280)  # (h, w)，与 gen3c_dynamic.py 中 load_vipe_data 的参数一致
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
    """两个 w2c 之间平滑插值（相机中心线性、朝向球面插值、两端缓入缓出）。返回 (n,4,4) w2c 和插值系数。"""
    Ca, Cb = -wa[:3, :3].T @ wa[:3, 3], -wb[:3, :3].T @ wb[:3, 3]
    qa, qb = quat_from_R(wa[:3, :3].T), quat_from_R(wb[:3, :3].T)  # c2w 旋转
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
    """全分辨率内参 → GEN3C 缩放 + 中心裁剪后的内参（与 vipe_utils._adjust_intrinsics_for_resize_and_crop 相同）。"""
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
    ap.add_argument("--work", required=True, help="阶段 3 的序列工作目录（含 meta.json、4c4d/、gen3c/vipe/）")
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

    # ---- 尺度对齐：MASt3R 点（真实米制）在输入机位的深度 vs ViPE 深度
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

    def rel(w2c):  # 相对输入相机的位姿，平移换到 ViPE 尺度
        T = w2c @ np.linalg.inv(w_in)
        T[:3, 3] *= k
        return T

    n = NUM_FRAMES
    # ---- 评估轨迹：每帧都在评估机位
    T_eval = np.stack([rel(w_ev)] * n)
    K_eval = np.stack([to_gen3c_K(K_ev, src_wh)] * n)
    np.savez(out / "gen3c_eval.npz", T=T_eval.astype(np.float32), K=K_eval.astype(np.float32))

    # ---- 运镜轨迹：输入机位 → 评估机位（两个模型共用同一条）
    path_w2c, s = interp_path(w_in, w_ev, n)
    K_path_full = np.stack([(1 - si) * K_in + si * K_ev for si in s])
    np.savez(out / "gen3c_path.npz", T=np.stack([rel(w) for w in path_w2c]).astype(np.float32),
             K=np.stack([to_gen3c_K(Kf, src_wh) for Kf in K_path_full]).astype(np.float32))
    # 4C4D：世界坐标 w2c（米）、全分辨率焦距、时间戳（与 4C4D 读数据时相同：帧号 / 帧数 × 10）
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
