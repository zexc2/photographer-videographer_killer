#!/usr/bin/env python3
"""Run several custom trajectories with the official gen3c_dynamic.py, loading the model only once. Run in the gen3c env from the GEN3C repo dir.

The official CLI only supports preset camera moves (left / right / clockwise ...), and in batch mode every job reads the same --vipe_path.
Only two functions are replaced here; everything else (cache, rendering, diffusion generation, saving) is upstream code:
  load_vipe_data             -> reads each job's own ViPE result, in job order
  generate_camera_trajectory -> returns that job's trajectory: new w2c[f] = T[f] @ ViPE input camera w2c[f], intrinsics K[f]
Guardrail and prompt-upsampling handling is identical to the official script's __main__ (which forces both off; see the license notes in pod/README.md).

Usage (cwd = GEN3C repo):
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
    ap.add_argument("--offload", action="store_true", help="put the T5 text encoder on CPU (use when VRAM is short)")
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
        print(f"[gen3c_run] job {state['job']['name']}: reading {state['job']['vipe']}", flush=True)
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
        print(f"[gen3c_run] job {name} done in {timings[name]}s -> {out / (name + '.mp4')}", flush=True)

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
    # Same as the official gen3c_dynamic.py __main__
    args.disable_guardrail = True
    args.disable_prompt_upsampler = True

    t0 = time.time()
    gd.demo(args)
    total = round(time.time() - t0, 1)
    missing = [j["name"] for j in jobs if j["name"] not in timings]
    json.dump({"jobs": timings, "total_s": total, "missing": missing,
               "peak_vram_gib": round(torch.cuda.max_memory_allocated() / 2 ** 30, 1)},
              open(out / "gen3c_timing.json", "w"), indent=2)
    print(f"[gen3c_run] all done, {total}s total (incl. model load); unfinished: {missing or 'none'}", flush=True)
    if missing:
        sys.exit(1)


if __name__ == "__main__":
    # After an error the process used to hang instead of exiting (seen 2026-10-14: hung 8 minutes after a FileNotFoundError),
    # presumably due to background threads started during loading. Force exit with os._exit here; the exit code is unchanged
    import os
    import traceback
    try:
        main()
    except SystemExit as e:
        sys.stdout.flush()
        os._exit(e.code if isinstance(e.code, int) else 1)
    except BaseException:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
    sys.stdout.flush()
    os._exit(0)
