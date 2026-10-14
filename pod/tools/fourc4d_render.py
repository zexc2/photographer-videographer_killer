#!/usr/bin/env python3
"""用官方 4C4D render.py 渲染自定义运镜轨迹。在 4c4d 环境里、4C4D 仓库目录下运行。

官方 render.py --traj 只支持 ellipse / interpolate / arc。这里在 render.py 导入之前替换两个函数，其余完全是官方流程：
  utils.render_utils.generate_path → 返回 make_trajectories.py 生成的轨迹相机（与 GEN3C 走同一条路径）
  utils.render_utils.create_videos → 不做（视频由任务脚本用 ffmpeg 统一编码）
相机构造方式与官方 generate_path 相同：deepcopy 场景里的相机，改位姿、视场角、投影矩阵和时间戳。

用法：ATLAS_4C4D_PATH=.../4c4d_path.npz python fourc4d_render.py <render.py 的参数，需含 --traj custom>
渲染结果：<model_path>/<output_dir>/render/traj/ours_<iter>/renders/00000.png …
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
        # 视场角只取决于全分辨率焦距和画幅，与渲染分辨率无关
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
    print(f"[fourc4d_render] 自定义轨迹 {len(cams)} 帧", flush=True)
    return cams


ru.generate_path = custom_generate_path
ru.create_videos = lambda **kw: None

if __name__ == "__main__":
    sys.argv = ["render.py"] + sys.argv[1:]
    runpy.run_path("render.py", run_name="__main__")
