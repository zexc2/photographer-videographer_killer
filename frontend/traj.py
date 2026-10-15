"""Camera path: keyframes → per-frame camera poses (OpenCV / COLMAP convention: x right, y down, z forward; w2c is world→camera).

All keyframes are relative to one fixed camera (the base camera), in units of "camera spacing" (the mean distance
between neighbouring real cameras), so values mean the same thing across scenes:
  right / up / forward   translation relative to the base camera (camera spacings); positive up means upward
  yaw / pitch            rotation relative to the base camera orientation (degrees); positive yaw turns right, positive pitch tilts up
  fov                    vertical field of view (degrees)
  t                      time of this keyframe in the output video (seconds)
  src                    corresponding time in the source video (seconds)
"""
import math

import numpy as np


def _rot_x(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def _rot_y(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def quat_from_R(R):
    t = np.trace(R)
    if t > 0:
        s = 0.5 / math.sqrt(t + 1)
        q = [0.25 / s, (R[2, 1] - R[1, 2]) * s, (R[0, 2] - R[2, 0]) * s, (R[1, 0] - R[0, 1]) * s]
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = 2 * math.sqrt(1 + R[0, 0] - R[1, 1] - R[2, 2])
        q = [(R[2, 1] - R[1, 2]) / s, 0.25 * s, (R[0, 1] + R[1, 0]) / s, (R[0, 2] + R[2, 0]) / s]
    elif R[1, 1] > R[2, 2]:
        s = 2 * math.sqrt(1 + R[1, 1] - R[0, 0] - R[2, 2])
        q = [(R[0, 2] - R[2, 0]) / s, (R[0, 1] + R[1, 0]) / s, 0.25 * s, (R[1, 2] + R[2, 1]) / s]
    else:
        s = 2 * math.sqrt(1 + R[2, 2] - R[0, 0] - R[1, 1])
        q = [(R[1, 0] - R[0, 1]) / s, (R[0, 2] + R[2, 0]) / s, (R[1, 2] + R[2, 1]) / s, 0.25 * s]
    q = np.array(q)
    return q / np.linalg.norm(q)


def R_from_quat(q):
    w, x, y, z = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                     [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                     [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])


def slerp(q0, q1, u):
    d = float(np.dot(q0, q1))
    if d < 0:
        q1, d = -q1, -d
    if d > 0.9995:
        q = q0 + u * (q1 - q0)
        return q / np.linalg.norm(q)
    th = math.acos(d)
    return (math.sin((1 - u) * th) * q0 + math.sin(u * th) * q1) / math.sin(th)


def camera_spacing(cameras):
    """Mean distance between neighbouring real cameras (median of each camera's distance to its nearest other camera)."""
    C = np.array([camera_center(c["w2c"]) for c in cameras.values()])
    d = np.linalg.norm(C[:, None] - C[None], axis=-1)
    np.fill_diagonal(d, np.inf)
    return float(np.median(d.min(axis=1)))


def camera_center(w2c):
    w2c = np.asarray(w2c, dtype=float)
    return -w2c[:3, :3].T @ w2c[:3, 3]


def keyframe_c2w(base_w2c, kf, spacing):
    """Keyframe → c2w (OpenCV convention). Translation and rotation are in the base camera's own frame."""
    base = np.asarray(base_w2c, dtype=float)
    Rb = base[:3, :3].T                      # base camera c2w rotation
    C = camera_center(base)
    # Camera frame: x right, y down, z forward, so "up" is -y
    offset = np.array([kf.get("right", 0.0), -kf.get("up", 0.0), kf.get("forward", 0.0)]) * spacing
    # Yaw first (about camera y; positive turns forward z toward right +x), then pitch (about camera x; positive turns forward z toward up -y)
    R_rel = _rot_y(math.radians(kf.get("yaw", 0.0))) @ _rot_x(math.radians(kf.get("pitch", 0.0)))
    c2w = np.eye(4)
    c2w[:3, :3] = Rb @ R_rel
    c2w[:3, 3] = C + Rb @ offset
    return c2w


def _catmull_rom(p0, p1, p2, p3, u):
    return 0.5 * ((2 * p1) + (-p0 + p2) * u + (2 * p0 - 5 * p1 + 4 * p2 - p3) * u * u
                  + (-p0 + 3 * p1 - 3 * p2 + p3) * u ** 3)


def build_trajectory(scene, spec):
    """spec: {base_cam, keyframes: [...], duration, fps, width, height}; scene: the scene.json written by export_scene.
    Returns {width, height, fps, frames: [{w2c, fx, fy, timestamp}], ...} for rendering on the pod."""
    cams = scene["cameras"]
    base = cams[spec["base_cam"]]
    spacing = camera_spacing(cams)
    kfs = sorted(spec["keyframes"], key=lambda k: k["t"])
    if not kfs:
        raise ValueError("no keyframes")   # app.py checks this first; normally unreachable
    duration = float(spec["duration"])
    fps = int(spec.get("fps", 30))
    W, H = int(spec["width"]), int(spec["height"])
    src_max = float(scene.get("source_seconds", 10.0))
    t_max = float(scene.get("time_duration", [0.0, 10.0])[1])
    poses = [keyframe_c2w(base["w2c"], k, spacing) for k in kfs]
    P = [p[:3, 3] for p in poses]
    Q = [quat_from_R(p[:3, :3]) for p in poses]
    ts = [float(k["t"]) for k in kfs]
    # Frame times evenly cover [0, duration] so the last frame lands exactly on the final keyframe
    n = max(2, int(round(duration * fps)))
    frames = []
    for i in range(n):
        tau = duration * i / (n - 1)
        if len(kfs) == 1 or tau <= ts[0]:
            j, u = 0, 0.0
        elif tau >= ts[-1]:
            j, u = len(kfs) - 2, 1.0
        else:
            j = max(k for k in range(len(kfs) - 1) if ts[k] <= tau)
            u = (tau - ts[j]) / max(ts[j + 1] - ts[j], 1e-9)
        if len(kfs) == 1:
            pos, q, k0, k1, uu = P[0], Q[0], kfs[0], kfs[0], 0.0
        else:
            uu = u * u * (3 - 2 * u) if spec.get("ease", True) else u   # ease in/out at both ends of each segment
            pos = _catmull_rom(P[max(j - 1, 0)], P[j], P[j + 1], P[min(j + 2, len(P) - 1)], uu)
            q = slerp(Q[j], Q[j + 1], uu)
            k0, k1 = kfs[j], kfs[j + 1]
        fov = (1 - uu) * k0.get("fov", 60.0) + uu * k1.get("fov", 60.0)
        src = (1 - uu) * k0.get("src", 0.0) + uu * k1.get("src", 0.0)
        src = min(max(src, 0.0), src_max)
        c2w = np.eye(4)
        c2w[:3, :3] = R_from_quat(q)
        c2w[:3, 3] = pos
        w2c = np.linalg.inv(c2w)
        fy = H / (2 * math.tan(math.radians(fov) / 2))
        frames.append({"w2c": w2c.round(6).tolist(), "fx": fy, "fy": fy,
                       "timestamp": src / src_max * t_max, "src_seconds": round(src, 4)})
    return {"width": W, "height": H, "fps": fps, "frames": frames, "spacing": spacing,
            "base_cam": spec["base_cam"]}


def base_fov(scene, cam_name):
    c = scene["cameras"][cam_name]
    return math.degrees(2 * math.atan(c["height"] / (2 * c["fy"])))
