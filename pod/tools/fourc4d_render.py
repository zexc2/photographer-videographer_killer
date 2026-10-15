#!/usr/bin/env python3
"""Render a custom camera-motion trajectory with the official 4C4D render.py. Run in the 4c4d env from the 4C4D repo dir.

The official render.py --traj only supports ellipse / interpolate / arc. Two functions are replaced before render.py is imported; the rest is the upstream pipeline:
  utils.render_utils.generate_path -> returns the trajectory cameras from make_trajectories.py (same path as GEN3C)
  utils.render_utils.create_videos -> skipped (videos are encoded uniformly by the task script with ffmpeg)
Cameras are built the same way as the official generate_path: deepcopy a scene camera, then change pose, FoV, projection matrix and timestamp.

Usage: ATLAS_4C4D_PATH=.../4c4d_path.npz python fourc4d_render.py <render.py args, must include --traj custom>
Output: <model_path>/<output_dir>/render/traj/ours_<iter>/renders/00000.png ...
"""
import copy
import math
import os
import runpy
import sys

import numpy as np
import torch

import utils.render_utils as ru
from utils.graphics_utils import getProjectionMatrix


def custom_generate_path(viewpoint_cameras, n_frames=480, traj="custom", **kw):
    d = np.load(os.environ["ATLAS_4C4D_PATH"])
    tmpl = viewpoint_cameras[0][1]
    W, H = float(d["width"]), float(d["height"])
    cams = []
    for i in range(len(d["w2c"])):
        cam = copy.deepcopy(tmpl)
        cam.image_height = int(cam.image_height / 2) * 2
        cam.image_width = int(cam.image_width / 2) * 2
        # FoV depends only on the full-resolution focal length and frame size, not on the render resolution
        cam.FoVx = 2 * math.atan(W / (2 * float(d["fx"][i])))
        cam.FoVy = 2 * math.atan(H / (2 * float(d["fy"][i])))
        cam.world_view_transform = torch.from_numpy(np.ascontiguousarray(d["w2c"][i].T)).float().cuda()
        cam.projection_matrix = getProjectionMatrix(znear=cam.znear, zfar=cam.zfar,
                                                    fovX=cam.FoVx, fovY=cam.FoVy).transpose(0, 1).cuda()
        cam.full_proj_transform = (cam.world_view_transform.unsqueeze(0).bmm(
            cam.projection_matrix.unsqueeze(0))).squeeze(0)
        cam.camera_center = cam.world_view_transform.inverse()[3, :3]
        cam.timestamp = float(d["timestamp"][i])
        cams.append(cam)
    print(f"[fourc4d_render] custom trajectory: {len(cams)} frames", flush=True)
    return cams


ru.generate_path = custom_generate_path
ru.create_videos = lambda **kw: None

if __name__ == "__main__":
    sys.argv = ["render.py"] + sys.argv[1:]
    runpy.run_path("render.py", run_name="__main__")
