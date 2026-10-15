# Pod-side scripts (stage 2)

These scripts run on the RunPod GPU pod; `atlas-run` on the server pushes them there and executes them. The server itself never runs a model.

| File | Purpose |
|---|---|
| `lib/common.sh` | Shared functions: path conventions, busy heartbeat, Miniforge, env pack and restore, repo cloning, HF token, writing to Global volume, setup report |
| `setup_gen3c.sh` | GEN3C env and weights |
| `setup_vipe.sh` | ViPE env and model cache (separate env, incompatible with GEN3C) |
| `setup_4c4d.sh` | 4C4D env, MASt3R env and MASt3R weights |
| `run_task.sh` | Generic task wrapper: timing, peak VRAM, status file, syncing `out/` back to Global volume at the end |
| `pre_stop.sh` | Called by the guard before it stops a pod; syncs every run's `out/` back to Global volume |
| `tasks/selftest.sh` | For the first pod start: checks mounts and GPU, measures Global volume and container disk read/write speed |
| `tasks/setup.sh` | Install entry point: `atlas-run setup gen3c vipe 4c4d` |
| `tasks/hello.sh` | Minimal task for testing the runner |

All setup scripts are **idempotent** and follow "option C" (see [docs/storage-and-env.md](../docs/storage-and-env.md)):

1. If Global volume's `envpacks/` has a pack for the current version -> unpack it to the container disk, done in a few minutes
2. If not -> full install, then pack and upload it so the next boot can reuse it
3. Weights are downloaded only once, stored in `$ATLAS_VOL/weights/<model>/`, with a `.complete` marker meaning the download finished

The version key is the `ENV_REV=<12 hex chars>` pinned in each setup script (`env_key` in `lib/common.sh`). Bump it whenever a script changes what gets installed; the next run then reinstalls and repacks. Edits to comments or log text do not invalidate existing packs.

## Environments at a glance

| | GEN3C | ViPE | 4C4D | MASt3R |
|---|---|---|---|---|
| Official repo (pinned commit) | [nv-tlabs/GEN3C](https://github.com/nv-tlabs/GEN3C) `db2ffe1` | [nv-tlabs/vipe](https://github.com/nv-tlabs/vipe) `8c9f361` | [yangzf-1023/4C4D](https://github.com/yangzf-1023/4C4D) `ed6a3cb` (verified as the official repo: [project page](https://junshengzhou.github.io/4C4D/), [arXiv 2604.04063](https://arxiv.org/abs/2604.04063)) | [naver/mast3r](https://github.com/naver/mast3r) `f5209af` |
| Python / torch / CUDA | 3.10 / 2.6 / 12.4 (official yml) | 3.11 / latest cu128 | 3.10 / 2.4.1 / 12.4 (**changed**; upstream is 3.7 / 1.12 / 11.6, which does not support H100) | 3.11 / 2.4.1 / 12.4 |
| Needs compiling | transformer-engine 1.12, apex | vipe_ext | simple-knn, pointops2, fused-ssim, diff-gaussian-rasterization | No (curope is optional, skipped) |
| Weights | ~76 GB: GEN3C-Cosmos-7B, Cosmos-Tokenize1, T5-11B (skips 45 GB of unused TF weights) | ~3–5 GB, downloaded automatically on first run, saved as a tar | No pretrained weights needed | 2.75 GB |
| HF token / license acceptance | Not needed (none of the three repos is gated) | Not needed | Not needed | Not needed |
| First install (**measured**, A100 SXM) | Env 19 min (incl. ~15 min building apex) + ~5 min to download weights and store them on Global volume | 25 min (of which ~18 min is the test video; GeoCalib alone took 11 min) | 13 min for 4C4D and MASt3R together | (included on the left) |
| Env size (**measured**) | 13 GB, 5.0 GB compressed | 12 GB, 4.8 GB compressed; model cache 4 GB | 13 GB, 5.2 GB compressed | 2.4 GB compressed; weights 2.6 GB |
| Restore on later boots (estimate, at 75 MB/s Global volume reads) | ~2–3 min | ~2–3 min | ~2–3 min | ~1 min |

Install times and sizes were measured on the first pod start on 2026-10-14; restore times will be measured on the next pod start. Every run appends `{time, env size, weights size, GPU}` to `$ATLAS_VOL/setup-reports/<model>.jsonl`.

## Changes to the official setup (and why)

- **GEN3C**: upstream symlinks the CUDA headers into the env with `ln -sf`; here they are copied, because conda-pack cannot handle absolute-path symlinks. Weights are downloaded with our own `snapshot_download` call, fetching only the files needed; the official script downloads T5's TF weights and re-verifies MD5s on every re-run.
- **ViPE**: upstream installs into an in-repo `.venv` with `uv sync`; here it is a conda env plus `pip install --no-build-isolation .` (non-editable), so it can be packed. `USE_SYSTEM_EIGEN=1` is also set to avoid downloading Eigen during the build.
- **4C4D**:
  - Env upgraded to py3.10 / torch 2.4 / CUDA 12.4 so it also runs on H100. If building or training breaks, fall back to the official py3.7 env (A100 only).
  - Fixed a path error in the official yml: `./diff_gaussian_rasterization` should be `./diff-gaussian-rasterization`.
  - Added packages the code actually imports but the yml does not list: torch_scatter, scipy, scikit-image, opencv, open3d, mediapy, etc.
  - The `render.py --test` flag in the official README is actually called `--validate`.
- **MASt3R**: the official 4C4D pipeline uses MAtCha (`train.py --sfm_config posed --sfm_only`) to build the sparse-view point cloud. MAtCha installs in editable mode and hard-codes `/usr/local/cuda-11.8`, so it cannot be packed. Here naver/mast3r is installed directly, and stage 3 uses a small script of our own for "known poses -> points3D".

## Known limitations

- Fixed during the first pod run on 2026-10-14:
  - GEN3C: MoGe upgraded huggingface-hub to 2.x and broke transformers -> after installing MoGe, re-pin versions from the official requirements. The official self-check returns 0 even on errors -> check its output for ERROR instead. apex master is incompatible with torch 2.6 and 24.10 is too old -> pinned to **25.09**
  - ViPE / 4C4D: conda-forge mixed in CUDA 13.x -> pin `cuda-version`; 4C4D's `.cpp` builds could not find the CUDA headers -> add `targets/x86_64-linux/include` to CPATH
  - Global volume cannot set file times -> `vol_sync` no longer uses `rsync -t`
- MoGe 3.x needs numpy>=2, conflicting with GEN3C's pinned numpy 1.26. It is only used for single-image mode; we use video + ViPE, so this does not matter
- ViPE's GeoCalib (intrinsics estimation) took 11 minutes on the test video, mostly on CPU. Stages 3/4 should find out why; it can also be skipped when intrinsics are known (Panoptic has calibration data)
- **GEN3C's CLI only supports preset camera moves**: left, right, up, down, zoom_in, zoom_out, clockwise, counterclockwise, with adjustable distance and orientation mode. The custom keyframe trajectories needed in stage 5 require changing `generate_camera_trajectory` or using the server interface of the official GUI. 4C4D's `render.py` likewise has only ellipse, interpolate and arc trajectories; changing `generate_path` is straightforward.

## License summary (see the root README for details)

| Component | License | Commercial use? |
|---|---|---|
| GEN3C code | Apache-2.0 | Yes |
| GEN3C / Cosmos weights | NVIDIA Open Model License | Yes, but **the model card states that disabling the safety guardrails automatically terminates the license**, and the official inference script forces the guardrails off (`gen3c_dynamic.py:389`). Must be resolved before commercial use |
| T5-11B | Apache-2.0 | Yes |
| ViPE code | Apache-2.0 (the Unik3D part is CC BY-NC-SA 4.0) | Code yes; the depth models used by the default pipeline (UniDepth etc.) are non-commercial (to be verified one by one) |
| 4C4D | MIT, but bundles the Inria 3DGS rasterizer (non-commercial / research only) | **No** |
| MASt3R | Code CC BY-NC-SA 4.0; the checkpoint is also bound by its training-data licenses | **No** |
| CMU Panoptic | Non-commercial research only | **No** |
