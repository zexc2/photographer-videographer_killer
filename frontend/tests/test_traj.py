"""Unit tests for the camera path: python3 -m unittest discover -s frontend/tests"""
import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import traj  # noqa: E402


def look_at_w2c(center, target):
    """w2c in the OpenCV convention: z points at target, y points down."""
    z = np.asarray(target, float) - center
    z /= np.linalg.norm(z)
    x = np.cross(z, np.array([0, 1.0, 0]))       # world y points down (OpenCV convention)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    R = np.stack([x, y, z])  # rows = camera axes in world coordinates
    w2c = np.eye(4)
    w2c[:3, :3] = R
    w2c[:3, 3] = -R @ center
    return w2c


def make_scene():
    cams = {}
    for i in range(5):
        C = np.array([i * 0.5 - 1.0, 0.0, -3.0])
        cams[f"cam{i:02d}"] = {"w2c": look_at_w2c(C, C + np.array([0, 0, 1.0])).tolist(),
                               "fx": 500.0, "fy": 500.0, "cx": 320, "cy": 240, "width": 640, "height": 480}
    return {"cameras": cams, "time_duration": [0.0, 10.0], "source_seconds": 10.0}


class TrajTest(unittest.TestCase):
    def setUp(self):
        self.scene = make_scene()
        self.base = self.scene["cameras"]["cam02"]["w2c"]
        self.sp = traj.camera_spacing(self.scene["cameras"])
        self.Rb = np.asarray(self.base)[:3, :3]          # rows: base camera x/y/z axes in world coordinates

    def c2w(self, **kf):
        return traj.keyframe_c2w(self.base, kf, self.sp)

    def test_spacing(self):
        self.assertAlmostEqual(self.sp, 0.5, places=6)

    def test_zero_keyframe_is_base(self):
        np.testing.assert_allclose(self.c2w(), np.linalg.inv(self.base), atol=1e-9)

    def test_translation_axes(self):
        C0 = traj.camera_center(self.base)
        np.testing.assert_allclose(self.c2w(right=1)[:3, 3] - C0, self.Rb[0] * self.sp, atol=1e-9)
        np.testing.assert_allclose(self.c2w(up=1)[:3, 3] - C0, -self.Rb[1] * self.sp, atol=1e-9)
        np.testing.assert_allclose(self.c2w(forward=2)[:3, 3] - C0, self.Rb[2] * 2 * self.sp, atol=1e-9)

    def test_yaw_right(self):
        fwd = self.c2w(yaw=90)[:3, 2]
        np.testing.assert_allclose(fwd, self.Rb[0], atol=1e-9)   # turn right 90° → face the original right direction

    def test_pitch_up(self):
        fwd = self.c2w(pitch=30)[:3, 2]
        self.assertGreater(float(fwd @ -self.Rb[1]), 0.49)         # upward (-y) component = sin30°
        self.assertAlmostEqual(float(fwd @ self.Rb[2]), math.cos(math.radians(30)), places=6)

    def test_build_two_keyframes(self):
        spec = {"base_cam": "cam02", "duration": 2.0, "fps": 10, "width": 640, "height": 480,
                "keyframes": [{"t": 0, "src": 1.0, "fov": 50}, {"t": 2, "src": 3.0, "right": 2, "yaw": 20, "fov": 40}]}
        out = traj.build_trajectory(self.scene, spec)
        fr = out["frames"]
        self.assertEqual(len(fr), 20)
        np.testing.assert_allclose(np.linalg.inv(fr[0]["w2c"]), self.c2w(), atol=1e-5)
        np.testing.assert_allclose(np.linalg.inv(fr[-1]["w2c"]), self.c2w(right=2, yaw=20), atol=1e-5)
        self.assertAlmostEqual(fr[0]["src_seconds"], 1.0)
        self.assertAlmostEqual(fr[-1]["src_seconds"], 3.0)
        self.assertAlmostEqual(fr[0]["timestamp"], 1.0)          # source 1 s → model time 1.0 (total length 10)
        fy0 = 480 / (2 * math.tan(math.radians(50) / 2))
        self.assertAlmostEqual(fr[0]["fy"], fy0, places=4)
        src = [f["src_seconds"] for f in fr]
        self.assertEqual(src, sorted(src))                       # source time is monotonically increasing

    def test_single_keyframe_static(self):
        spec = {"base_cam": "cam00", "duration": 1.0, "fps": 5, "width": 64, "height": 48,
                "keyframes": [{"t": 0, "src": 2.0}]}
        fr = traj.build_trajectory(self.scene, spec)["frames"]
        self.assertTrue(all(np.allclose(f["w2c"], fr[0]["w2c"]) for f in fr))

    def test_src_clamped(self):
        spec = {"base_cam": "cam00", "duration": 1.0, "fps": 5, "width": 64, "height": 48,
                "keyframes": [{"t": 0, "src": -3}, {"t": 1, "src": 99}]}
        fr = traj.build_trajectory(self.scene, spec)["frames"]
        self.assertEqual(fr[0]["src_seconds"], 0.0)
        self.assertEqual(fr[-1]["src_seconds"], 10.0)


if __name__ == "__main__":
    unittest.main()
