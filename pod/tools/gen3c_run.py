#!/usr/bin/env python3
"""用官方 gen3c_dynamic.py 跑多条自定义轨迹，模型只加载一次。在 gen3c 环境里、GEN3C 仓库目录下运行。

官方命令行只支持预设运镜（left / right / clockwise …），而且 batch 模式下每个任务都读同一个 --vipe_path。
这里只替换两个函数，其余流程（缓存、渲染、扩散生成、保存）完全是官方代码：
  load_vipe_data             → 按任务顺序读各自的 ViPE 结果
  generate_camera_trajectory → 返回该任务的轨迹：新 w2c[f] = T[f] @ ViPE 输入相机 w2c[f]，内参 K[f]
安全护栏、提示词增强的处理和官方脚本 __main__ 完全一致（官方脚本强制关闭两者，见 pod/README.md 的许可说明）。

用法（cwd = GEN3C 仓库）：
  CUDA_HOME=$ENV PYTHONPATH=. python gen3c_run.py --jobs jobs.json --out OUTDIR [--offload]
  jobs.json: [{"name": "pose1_eval", "vipe": ".../gen3c/vipe", "traj": ".../stage4/gen3c_eval.npz"}, ...]
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import torch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--checkpoint_dir", default="checkpoints")
    ap.add_argument("--offload", action="store_true", help="把 T5 文本编码器放到 CPU（显存不够时用）")
    a = ap.parse_args()

    jobs = json.load(open(a.jobs))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    raw = out / "_raw"
    batch = out / "_batch.jsonl"
    with open(batch, "w") as f:
        for j in jobs:
            f.write(json.dumps({"prompt": "", "visual_input": j["vipe"]}) + "\n")

    import cosmos_predict1.diffusion.inference.gen3c_dynamic as gd

    queue = list(jobs)
    state = {"job": None, "t0": None}
    timings = {}
    orig_load = gd.load_vipe_data

    def load_for_job(vipe_root_or_mp4=None, **kw):
        state["job"] = queue.pop(0)
        state["t0"] = time.time()
        print(f"[gen3c_run] 任务 {state['job']['name']}：读取 {state['job']['vipe']}", flush=True)
        return orig_load(vipe_root_or_mp4=state["job"]["vipe"], **kw)

    def traj_for_job(trajectory_type, initial_w2c, initial_intrinsics, num_frames, movement_distance,
                     camera_rotation, center_depth=1.0, device="cuda"):
        d = np.load(state["job"]["traj"])
        T = torch.from_numpy(d["T"][:num_frames]).to(initial_w2c.device, initial_w2c.dtype)
        K = torch.from_numpy(d["K"][:num_frames]).to(initial_intrinsics.device, initial_intrinsics.dtype)
        base = initial_w2c[:num_frames] if initial_w2c.dim() == 3 else initial_w2c.expand(num_frames, 4, 4)
        return (T @ base).unsqueeze(0), K.unsqueeze(0)

    orig_save = gd.save_video

    def save_for_job(video, fps, H, W, video_save_quality, video_save_path):
        orig_save(video=video, fps=fps, H=H, W=W, video_save_quality=video_save_quality, video_save_path=video_save_path)
        name = state["job"]["name"]
        shutil.copy(video_save_path, out / f"{name}.mp4")
        timings[name] = round(time.time() - state["t0"], 1)
        print(f"[gen3c_run] 任务 {name} 完成，用时 {timings[name]}s → {out / (name + '.mp4')}", flush=True)

    gd.load_vipe_data = load_for_job
    gd.generate_camera_trajectory = traj_for_job
    gd.save_video = save_for_job

    sys.argv = ["gen3c_dynamic.py",
                "--checkpoint_dir", a.checkpoint_dir,
                "--batch_input_path", str(batch),
                "--video_save_folder", str(raw),
                "--vipe_path", jobs[0]["vipe"],
                "--vipe_starting_frame_idx", "0",
                "--num_video_frames", "121",
                "--guidance", "1",
                "--num_gpus", "1",
                "--disable_prompt_upsampler"]
    if a.offload:
        sys.argv += ["--offload_text_encoder_model"]
    args = gd.parse_arguments()
    # 与官方 gen3c_dynamic.py 的 __main__ 一致
    args.disable_guardrail = True
    args.disable_prompt_upsampler = True

    t0 = time.time()
    gd.demo(args)
    total = round(time.time() - t0, 1)
    missing = [j["name"] for j in jobs if j["name"] not in timings]
    json.dump({"jobs": timings, "total_s": total, "missing": missing,
               "peak_vram_gib": round(torch.cuda.max_memory_allocated() / 2 ** 30, 1)},
              open(out / "gen3c_timing.json", "w"), indent=2)
    print(f"[gen3c_run] 全部完成，总用时 {total}s（含加载模型）；未完成：{missing or '无'}", flush=True)
    if missing:
        sys.exit(1)


if __name__ == "__main__":
    main()
