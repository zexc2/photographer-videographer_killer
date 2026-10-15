"""Synthetic scene for fake mode: same format as the output of fourc4d_direct.py export on the pod (scene.json, points.bin, thumbs/).
21 cameras in a forward-facing row (mimicking the N3V forward array); the point cloud is a room plus a "person". For UI testing only, not real data."""
import json
import math
import struct
import subprocess
from pathlib import Path

import numpy as np


def _w2c(center, yaw):
    """OpenCV convention: camera looks along +z (world), yawed about the world y axis."""
    c, s = math.cos(yaw), math.sin(yaw)
    R_c2w = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    w2c = np.eye(4)
    w2c[:3, :3] = R_c2w.T
    w2c[:3, 3] = -R_c2w.T @ center
    return w2c


def write_fake_scene(out):
    out = Path(out)
    (out / "thumbs").mkdir(parents=True, exist_ok=True)
    cams = {}
    for i in range(21):
        x = (i - 10) * 0.12
        yaw = -x * 0.25
        cams[f"cam{i:02d}"] = {"w2c": _w2c(np.array([x, -0.2 * ((i % 3) - 1), 0.0]), yaw).tolist(),
                               "fx": 1400.0, "fy": 1400.0, "cx": 1352.0, "cy": 1014.0, "width": 2704, "height": 2028}
    rng = np.random.default_rng(0)
    n = 60000
    # Room: back wall z=4, floor y=1.2, side walls x=±2.5 (y points down)
    wall = np.c_[rng.uniform(-2.5, 2.5, n), rng.uniform(-1.5, 1.2, n), np.full(n, 4.0)]
    floor = np.c_[rng.uniform(-2.5, 2.5, n), np.full(n, 1.2), rng.uniform(0.5, 4.0, n)]
    side = np.c_[np.sign(rng.uniform(-1, 1, n)) * 2.5, rng.uniform(-1.5, 1.2, n), rng.uniform(0.5, 4.0, n)]
    person = np.c_[rng.normal(0, 0.18, n // 2), rng.uniform(-0.6, 1.2, n // 2), rng.normal(2.5, 0.12, n // 2)]
    pts = np.concatenate([wall, floor, side, person]).astype(np.float32)
    col = np.concatenate([np.tile([210, 200, 185], (n, 1)), np.tile([120, 85, 60], (n, 1)),
                          np.tile([190, 190, 200], (n, 1)), np.tile([60, 70, 90], (n // 2, 1))]).astype(np.uint8)
    col = np.clip(col.astype(int) + rng.integers(-15, 15, col.shape), 0, 255).astype(np.uint8)
    with open(out / "points.bin", "wb") as f:
        f.write(struct.pack("<I", len(pts)))
        f.write(pts.tobytes())
        f.write(col.tobytes())
    for name in cams:
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=0x5a6b62:s=480x360",
                        "-vf", f"drawtext=text='{name} (demo)':fontsize=36:fontcolor=white:x=20:y=20",
                        "-frames:v", "1", str(out / "thumbs" / f"{name}.jpg")], capture_output=True)
    json.dump({"cameras": cams, "num_points": int(len(pts)), "num_gaussians": int(len(pts)),
               "iteration": 0, "time_duration": [0.0, 10.0], "fake": True},
              open(out / "scene.json", "w"), indent=1)
