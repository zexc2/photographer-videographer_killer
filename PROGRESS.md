# Progress Log

Each stage records: what was done / problems encountered / time spent / cost.

| Stage | Date | Time | GPU cost |
|---|---|---|---|
| 0 Repository setup | 2026-10-13 | ~10 min | $0 |
| 1 GPU guard | 2026-10-13 | ~30 min | $0 |
| 1.1 Switch to Global volume | 2026-10-13 | ~45 min | $0 |
| 2 Pod environment scripts | 2026-10-13 | ~1 h | $0 (no pod started yet) |
| 2.2 First pod: install the three environments | 2026-10-14 | pod 2h03m | **$3.97** |
| 3 Panoptic data scripts | 2026-10-14 | ~1 h | $0 (no pod started yet) |
| 3.1 Pod run: prepare Panoptic data | 2026-10-14 | pod 1h39m | **$3.02** |
| 4.1 Pod run: baselines | 2026-10-14 | pod 3h50m | **$7.05** |
| 4.2 Retrieve results | 2026-10-14 | pod 1 min | $0.05 |
| 4.3 Rerun 4C4D | 2026-10-15 | pod 2h41m | **$4.77** |
| 4.4 Official N3V reproduction | 2026-10-15 | pod 3h45m | **$7.30** |
| 5.1 Real GPU test of the camera-path workbench | 2026-10-15 | pod ~5 min total | $0.12 |

---

## Stage 0: Repository setup (2026-10-13)

**What was done**
- Saved the project brief as `CLAUDE.md`
- Initialized git in `~/app` (main branch); wrote `.gitignore` first (excluding keys, weights, datasets, videos, point clouds, logs, etc.), then made the first commit
- Created the `README.md` skeleton (background, stage plan, architecture, third-party license table) and this file

**Problems encountered**
- None

**Time**: about 10 minutes
**Cost**: $0 (no GPU used)

---

## Stage 1: GPU guard (2026-10-13)

**What was done**
- `guard/atlas_gpu.py`, installed as `~/bin/gpu-up`, `gpu-down`, `gpu-status`, `gpu-guard`:
  - Whitelist is A100 80GB only; H100 is allowed only when all A100s are out of stock; names must have the atlas- prefix; at most 1 pod at a time; mounts `atlas_data` at `/workspace`; `--hours` defaults to 6, max 6
  - Can only act on atlas- prefixed pods; pods from other projects are always refused
- Watchdog: the atlas user's crontab runs it every 10 minutes (with `flock` to prevent overlapping runs). Over time limit → stop; GPU utilization at 0 for 30 consecutive minutes → stop; logs go to `~/logs/gpu-guard.log`
- Idle detection uses SSH + nvidia-smi instead of the RunPod API. Rationale in [guard/README.md](guard/README.md)
- Offline tests `guard/tests/run_tests.sh`: 39 cases using fake runpodctl / ssh, all passing. Ran a read-only dry-run against the real account; no pod was started

**Network Volume recommendation** (obsolete, see Stage 1.1)
- Region: **US-KS-2**: the only data center listing A100 SXM, A100 PCIe and H100 (SXM / PCIe / NVL) together, giving the most room to fall back to H100 when A100s are out of stock. Alternative: US-MO-1
- Recommended size **200 GB**, about $14/month (at $0.07/GB/month). Estimate: GEN3C weights and dependent models 60–80 GB, three conda environments about 35 GB, MASt3R and ViPE weights about 5 GB, 1–2 Panoptic sequences (selected cameras only) 10–20 GB, training outputs about 20 GB. It can be expanded later but not shrunk

**Problems encountered**
- runpodctl 2.15 does not expose a GPU utilization field, so idle detection uses SSH instead
- The output format of `runpodctl ssh info` and some pod fields can only be confirmed with a real pod; to be verified on the first pod run

**Time**: about 30 minutes
**Cost**: $0 (read-only queries and dry-runs only)

---

## Stage 1.1: Switch to Global volume (2026-10-13)

**Decision (Joey)**: Use the Global volume `atlas_data` instead of a Network volume in US-KS-2. Reason: after a pod is stopped, the same data center is often out of stock, while a Global volume is not tied to a data center.

**What was done**
- Checked the official docs (Global volume is still in beta):
  - Backed by object storage: no file locks, no atomic rename, eventually consistent
  - Mounted at `/workspace` by default; becomes `/workspace-global` when a network volume is also mounted
  - **Can only be mounted via REST API v2**; runpodctl 2.15 does not support it
  - The volume is deleted 15 days after the account balance reaches zero
- Guard reworked to use RunPod API v2 as primary, with runpodctl as fallback:
  - `gpu-up` no longer locks a region; it searches all regions for A100 (H100 only if none are in stock or none can be started), mounts the Global volume, and always refers to its path via the `$ATLAS_VOL` variable
  - Container disk defaults to 200 GB (`$ATLAS_LOCAL=/opt/atlas`); the `.atlas_busy` marker moved to the container disk
  - Watchdog idle detection now uses API utilization as primary with SSH as a cross-check; before stopping it runs `pre_stop` to sync results back to the Global volume
  - If the API fails, the watchdog falls back to runpodctl and still stops pods on the time limit
  - Added a server-only pod SSH key, injected into pods via `PUBLIC_KEY`
- Storage tiering and comparison of conda environment options (A Docker image / B install every time / C install once then conda-pack) written up in [docs/storage-and-env.md](docs/storage-and-env.md); **chose C**
- Tests expanded to 50 cases, all passing; fixed a bug: `--gpu h100` previously did not check A100 stock

**Problems encountered**
- runpodctl does not support Global volumes, so Joey needs to create a separate restricted API key (see guard/README.md)

**Time**: about 45 minutes
**Cost**: $0

---

## Stage 2: Pod environment scripts (2026-10-13)

**What was done**
- Verified the three official repositories and pinned specific commits: GEN3C `db2ffe1`, ViPE `8c9f361`, 4C4D `ed6a3cb` (confirmed to be the CVPR 2026 paper authors' own repository), MASt3R `f5209af`
- `pod/setup_gen3c.sh`, `setup_vipe.sh`, `setup_4c4d.sh`: idempotent, following option C (install once → conda-pack into the Global volume → unpack on later boots). Each run records time, environment size and weight size
- `runner/atlas_run.py` (`atlas-run`): the server rsyncs scripts and task inputs to the pod → runs in the background → polls progress → pulls back only small files (previews ≤20 MB, metrics) to the server, and computes the GPU cost of the run. The HF token is passed only via SSH stdin
- `pod/run_task.sh`: busy heartbeat, peak VRAM sampling, syncs `out/` back to the Global volume at the end; `pre_stop.sh`: rescues results of unfinished tasks before the watchdog stops the pod
- `tasks/selftest.sh`: measures Global volume read/write speed on the first pod run; the time estimates for option C depend on this data
- Local tests `runner/tests/run_tests.sh`: all 20 cases pass (full runner flow, sync, pull-back size limit, failed tasks, pre_stop, environment pack and restore); the 50 guard tests still all pass

**Technical choices**
- 4C4D environment: the official py3.7 / torch 1.12 / CUDA 11.6 does not support H100 → upgraded to py3.10 / torch 2.4 / CUDA 12.4 and recompiled 4 CUDA extensions (adopted); fall back to the official environment if problems arise (A100 only)
- MASt3R preprocessing: the official route goes through MAtCha (editable install plus a hard-coded CUDA 11.8 path, cannot be packed) → install naver/mast3r directly and write our own "known poses → point cloud" script in Stage 3 (adopted)

**Problems and findings**
- The official GEN3C weight script downloads about 121 GB, of which 45 GB is unused T5 TF weights → changed to download only the ~76 GB actually needed
- **Both GEN3C and 4C4D command lines only offer preset camera paths**; Stage 5's custom keyframes require code changes (both can be modified; 4C4D is simpler)
- **Licenses**: 4C4D and MASt3R are both non-commercial; GEN3C weights allow commercial use, but the model card states that "disabling the safety guardrails terminates the license", while the official inference script forcibly disables them. Not an issue for Phase 1 research validation; must be resolved before commercial use
- None of the setup scripts have been verified on a real pod yet

**Time**: about 1 hour
**Cost**: $0

---

## Stage 2.1: Volume ID and balance check (2026-10-13)

- The Global volume is now identified only by the ID in the config. The name contains a slash and is used only for display; other volumes with the same name will not be picked up by mistake (covered by tests)
- gpu-up checks the balance before starting a pod: refuses if < $15 or unreadable; warns if the balance would drop below $15 after running the full time limit; gpu-status shows the balance
- 61 guard tests and 20 runner tests, all passing
- Real-account dry-run: both A100 types in stock (SXM $1.79/h, 3 regions; PCIe 2 regions). **However, the API key file was not found on the server, and the balance is still $7.07**, so the v2 API path is not yet verified; see report

**Cost**: $0

---

## Stage 2.2: First pod, install and pack the three environments (2026-10-13 23:25 – 2026-10-14 01:27 UTC)

**Result**: all succeeded, pod stopped. The Global volume now contains:

| Content | Size |
|---|---|
| Environment packs `envpacks/`: gen3c 5.0 GB, vipe 4.8 GB, 4c4d 5.2 GB, mast3r 2.4 GB | 17.4 GB |
| Weights `weights/`: gen3c 71 GB, vipe 4.0 GB, mast3r 2.6 GB | 77.6 GB |

**Measurements** (A100 SXM 80GB, $1.79/h)

| Item | Measured |
|---|---|
| Global volume sequential write / read | 164 MB/s / **75 MB/s** (reads are slow: reading all 71 GB of GEN3C weights once takes about 16 minutes) |
| Global volume small files | 350 files/s |
| Container disk read | 10 GB/s |
| GEN3C install | environment 19 min + weights 5 min |
| ViPE install | 25 min (18 min of which is the test video) |
| 4C4D + MASt3R install | 13 min |
| Packing one environment | 44–80 s |

**Cost**: pod ran 2h03m; GPU at $1.79/h comes to about $3.66; account balance went from $17.07 to $13.10, **actual cost $3.97** (including container disk and storage).
Had everything gone smoothly the first time, it would have taken only about 1 hour and about $1.8. The extra time came from the problems below; each time the cause was tracked down, the script fixed, and the install rerun.

**Problems encountered (all fixed, documented in script comments and pod/README.md)**
1. GEN3C: MoGe upgraded huggingface-hub to 2.x and transformers could no longer be imported; the official self-check script still returned 0 on error, nearly packing a broken environment → re-pinned versions and made the self-check strict
2. GEN3C: apex master uses a `list[int]` annotation that torch 2.6 does not support, while 24.10 is too old to compile → pinned apex to **25.09**
3. ViPE, 4C4D: CUDA 13.x crept in from conda-forge → pinned `cuda-version` and check the nvcc version after creating the environment
4. 4C4D: g++ could not find CUDA headers → added the header directory to CPATH
5. The Global volume cannot set file timestamps → no `rsync -t` when syncing
6. Guard: the API occasionally omits SSH info, and the watchdog nearly mistook a pod that was installing environments for idle → cache the last SSH command obtained (new test, 62 total)
7. atlas-run: Python output was buffered so progress could not be followed → switched to line-by-line output; the SSH call that launches the task sometimes does not return (the task itself starts normally) → added setsid and a timeout; root cause still to be investigated

**To do**
- On the next pod run, measure the time to "restore from environment pack" (expected 2–3 minutes per environment)
- ViPE's GeoCalib is too slow: Panoptic has camera calibration, so the known intrinsics could be used to skip this step

---

## Stage 3: CMU Panoptic data scripts (2026-10-14)

**What was done**
- Confirmed the CMU server is still reachable and supports HTTP range requests. Measured only about 3.5 MB/s from the server, so only the needed 10 seconds are cut from each camera instead of downloading full videos (up to 3 GB per camera)
- Chose two sequences: `171204_pose1` (single person, large motions) and `160906_band1` (three people playing music, similar to an event). Computed the positions of all 31 cameras from the calibration, then checked thumbnails for the subjects' facing direction, and selected 4 input cameras and 1 evaluation camera; selection rationale in [docs/stage3-data.md](docs/stage3-data.md)
- `pod/tools/panoptic_prepare.py`: cut → undistort → COLMAP (centimeters to meters) → GEN3C input video → meta.json
- `pod/tools/mast3r_posed_points.py`: MASt3R dense matching + triangulation with known poses to produce the 4C4D initial point cloud (replacing the MAtCha step used officially)
- `pod/tasks/panoptic.sh`: restore environments from packs (measuring restore time along the way) → process each sequence → ViPE → pack and store back to the Global volume

**Verification on the server** (using 5 actually downloaded frames; no models run on the server)
- The whole preparation pipeline runs end to end
- A vertical line at the world origin projects onto the person in all 5 cameras, confirming the coordinates and unit conversion are correct
- Synthetic triangulation test: median error under 1 mm

**Problems encountered**
- In bash, `set -e` has no effect inside a `( set -e; … ) || …` construct, so the task would not stop on errors → changed to run in the background and then `wait`
- The ViPE command line cannot use known intrinsics directly (it always runs GeoCalib first) → kept the official default flow and recorded the error between ViPE's estimated focal length and the true calibration, as a reference for the "single phone, uncalibrated" case

**Time**: about 1 hour  **Cost**: $0

- 2026-10-14: Per Joey's decision, the gpu-up balance floor was lowered from $15 to $3 (64 tests, all passing)

---

## Stage 3.1: Pod run to prepare Panoptic data (2026-10-14 02:30 – 04:10 UTC)

**Result**
- `171204_pose1`: ✅ fully done. The COLMAP dataset (5 cameras × 300 frames), MASt3R point cloud (33,153 points), GEN3C input video and ViPE results are all packed into `stage3.tar` (4 GB) and stored in the Global volume
- `160906_band1`: cutting, undistortion and point cloud (18,638 points) confirmed done; ViPE and packing **very likely also finished**, but not yet confirmed because the session was interrupted during this time. Check first on the next pod run and fill in whatever is missing (about 10 minutes)

**Measurements**
| Item | Measured |
|---|---|
| Restore three environments from packs (4c4d, mast3r, vipe + model cache) | **2 min 23 s** (first install took about 1 hour) |
| Cut + undistort one sequence (5 cameras × 300 frames) | about 3.5 min |
| MASt3R point cloud | about 1 min (56 s of which is loading the 2.75 GB weights) |
| ViPE on 300 frames (1080p) | about 10 min |
| Pack 4 GB into the Global volume | 28 s |

**Cost**: pod ran 1h39m; balance went from $12.97 to $9.95, **$3.02**. About $1 of that was wasted idling after the task finished (see problem 5).

**Problems encountered (all fixed)**
1. ViPE reported `No module named 'vipe'`: conda-pack rewrote the command-line scripts' shebang to `#!/usr/bin/env python`, so the system Python was used → when calling ViPE, put its environment first on PATH
2. ViPE reported `distutils.compilers.errors`: setuptools files installed by pip and by conda got mixed during install and no longer matched after pack/restore → worked around temporarily with `SETUPTOOLS_USE_DISTUTILS=stdlib`. **To do**: install setuptools via conda next time the ViPE environment is rebuilt
3. ViPE's `-v` launches the rerun visualization viewer; the pod has no display, so after ViPE finished the process hung for 20 minutes without exiting → removed `-v`; the process is killed once the log shows Finished and the result file is written; existing results are skipped automatically
4. Too few MASt3R points: the 2-pixel reprojection threshold was too strict, since MASt3R matches on 512-wide images and each pixel scales up 3.75× at full resolution → changed to 8 pixels. Loosening it only raised the point count from 29,551 to 33,153, showing that the real cause is that **for camera pairs more than 70° apart, most of the matches themselves are wrong** (see "Findings" below)
5. **The pod idled for about 35 minutes after the session was interrupted**: the task finished around 03:35, but my session had dropped and nobody stopped the pod. The watchdog should have stopped it after 30 idle minutes, but at 04:00 it read a residual 2% utilization, resetting the idle timer, and it was finally stopped at 04:10 by the time limit → the guard now **automatically stops the pod 5 minutes after a registered task finishes** (independent of the session), and the idle threshold is now < 5%. Guard tests increased to 74, all passing
6. A remote `pkill -f` matched and killed the very shell running it, so later commands did not run and the old task's processes stayed alive → switched to killing by exact PID

**Findings (directly relevant to real-world shooting)**
- For camera pairs with small angles between neighbors (26°, 46°), 75–95% of MASt3R matches pass the geometric check; for pairs 73° to 145° apart, only 2–8% pass
- Conclusion: **when shooting for real, keep the angle between adjacent phones to no more than about 45°**; otherwise almost no usable correspondences can be found between neighboring cameras and the reconstruction will be very sparse

---

## Stage 4: Baseline test code (2026-10-14)

**What was done** (no pod started yet)
- `pod/tools/make_trajectories.py`: generates camera paths shared by both models (evaluation camera, and a camera path from an input camera → the evaluation camera), using the MASt3R point cloud to align ViPE's scale. Synthetic test passed: a 1.3 scale factor is recovered exactly, and the start and end points of the path are correct
- `pod/tools/gen3c_run.py`: runs custom camera paths through the official gen3c_dynamic.py, replacing only "where the camera path comes from"; the model is loaded only once (71 GB of weights)
- `pod/tools/fourc4d_render.py`: renders the same camera path through the official render.py
- `pod/tools/eval_metrics.py`: PSNR, SSIM and LPIPS on a common basis, plus "ground truth | prediction | difference" comparison videos. Local self-check: 100 dB when identical, 24 dB when offset by 8 pixels, 38.8 dB for ground truth processed through the GEN3C pipeline
- `pod/tasks/baseline.sh`: environments → data → camera paths → GEN3C → 4C4D training and rendering → evaluation, recording time and peak VRAM per phase
- Method notes: [docs/stage4-method.md](docs/stage4-method.md)

**Fairness issues found and corrected**
- Panoptic camera principal points are off-center by up to about 18 pixels, while 4C4D assumes a centered principal point by default → move the principal point to the center during undistortion. Stage 3 data must be regenerated with the new pipeline; this is included in the Stage 4 task, about 15 minutes
- 4C4D officially saves only the checkpoint with "the best evaluation-camera score", which amounts to picking the model after seeing the answers → evaluate only once at the end of training
- 4C4D's yaml overrides command-line arguments → the iteration count is now written into the yaml

**Time**: about 2 hours  **Cost**: $0

---

## Stage 4.1: Pod run for baselines (2026-10-14 06:59 – 10:50 UTC)

**Result**
- **GEN3C ✅** (single camera cam24 + ViPE): evaluation camera PSNR **15.96**, SSIM **0.661**, LPIPS **0.578**. One video each was generated for the evaluation view and the camera path, and pulled back to the server
- **4C4D**: 30k training steps completed; the subsequent rendering and evaluation results **were synced to the Global volume but not yet pulled back to the server**. The session was interrupted during this time, and the pod was stopped by the "auto-stop after task ends" rule as designed (14 minutes after the task ended). Another ~10-minute pod is needed to pull the results back with the newly written `collect` task

**Measurements**
| Item | Measured |
|---|---|
| Restore environments + redo pose1 data | 21.5 min (11 min of which is ViPE) |
| GEN3C model load (71 GB of weights, read from the Global volume) | about 4 min |
| GEN3C generating 121 frames (704×1280) | **about 16 min per clip**, peak VRAM **43.6 GB** (with the T5 text encoder offloaded to CPU) |
| 4C4D training 30k steps (4 cameras × 300 frames, 960×540) | about 2h15m (roughly 08:11 – 10:27); the first 15k steps get progressively slower as the number of Gaussians grows, then it stabilizes at about 9 min per 1500 steps |
| ViPE scale / focal length | depth is **1.55×** the true value; focal length estimated at 2807 vs. true 1400, **100% error** |

**Cost**: balance went from $9.71 to $2.66, **$7.05**. The balance did not change in the 11 hours after shutdown, which means the "charges while no pod is running" seen in the morning were actually delayed billing; storage fees are not deducted continuously by the hour.

**Problems encountered (all fixed)**
1. GEN3C could not find its weights: the repository ships its own `checkpoints/` directory, and the symlink was created inside it → pass absolute paths directly
2. After GEN3C errored, the process hung instead of exiting, wasting about 8 minutes → force exit on error
3. 4C4D: torchmetrics needs `pkg_resources`, which newer setuptools removed → automatically install `setuptools<80` before training
4. Per-phase VRAM sampling exited as soon as it started (a `[ … ] && …` construct colliding with `set -e`) → switched to if; this run used a separate sampling process instead
5. `4c4d-only` mode reported successful tasks as failed → judge by 4C4D only
6. **When the session is interrupted, the pod gets stopped before results are pulled back to the server** → the guard now runs `atlas-run --fetch` before stopping a pod to pull small files back to the server (77 tests); added a `collect` task to pull results from the Global volume afterwards

**Why GEN3C's result is poor**: ViPE's focal-length estimate was off by a factor of two, so the 3D structure is compressed horizontally; projected into the evaluation camera, the entire scene is misaligned, and the middle section is almost entirely hallucinated. This shows that the bottleneck of the single-camera approach is "estimating camera parameters without calibration", whereas the intrinsics of a phone / Insta360 can be obtained with a one-time calibration

**To do (after Joey decides)**
- GEN3C variant ①: single camera + true intrinsics (about $0.6)
- GEN3C variant ②: multi-camera mode (4 cameras, bullet time, about $1.3; requires writing dense-depth code first)
- When rebuilding the ViPE environment, install setuptools via conda (root-cause fix)

### Stage 4.2: Retrieve results (2026-10-14 22:18, $0.05)
- Started a short pod to run the `collect` task. Confirmed that the Stage 3 data packs for both sequences are complete: pose1 3.8 GB, band1 4.0 GB
- **4C4D training completed successfully**: 30k steps, 2 hours 25 minutes, 4.2 million Gaussians. The training script's final self-test: **evaluation camera PSNR 21.42** (4C4D's own metric basis: 300 frames, 960×540), training cameras 34.57. For reference, GEN3C scores 15.96 on our common basis
- **But rendering failed** (missing `libEGL.so.1`, required by open3d), and **the model was saved on the container disk rather than in `out/`, so it was lost when the pod stopped**; retraining is needed
- Fixes:
  - The 4C4D model is now saved to `out/full/4c4d_model` and synced to the Global volume right after training
  - Install `libegl1` before rendering
  - Skip the `full/` directory when pulling results (large files stay on the Global volume)
  - Added a `baseline <sequence> 4c4d` mode: on a new pod, unpack the Stage 3 data pack directly and run only 4C4D

---

## Stage 4.3: Rerun 4C4D and complete the baseline comparison (2026-10-14 22:36 – 2026-10-15 01:17 UTC)

**Result**: all succeeded; the pod was stopped 1 minute after the task finished. Comparison report: [docs/report-baseline.md](docs/report-baseline.md)

| Method | PSNR | SSIM | LPIPS |
|---|---|---|---|
| GEN3C (1 camera) | 15.96 | 0.661 | 0.578 |
| **4C4D (4 cameras)** | **21.74** | **0.818** | **0.378** |

- Unpacking the Stage 3 data pack from the Global volume took only 2 minutes of preparation (redoing the data last time took 21.5 minutes)
- 4C4D training took 2 hours 27 minutes, peak VRAM 24.6 GB; the end-of-training self-test gave 21.69, consistent with the first run's 21.42, so the result is reproducible
- The model was synced to the Global volume right after training and libEGL was installed for rendering; neither of last time's problems recurred
- Subjective: 4C4D gets the person's position and motion right, but textures are blurry and there are floaters; GEN3C is misaligned, and two people appear at the end of the camera path (a visible artifact)

**Cost**: $4.77. **Cumulative Phase 1 GPU cost about $18.9** ($3.97 + $3.02 + $7.05 + $0.05 + $4.77, plus a small amount of storage fees)

**Problems encountered**: the marker file for per-phase VRAM sampling was created only after the sampling loop started, so the loop exited immediately → reordered. Peak VRAM for this run was taken from the separate sampling log of the previous pod

---

## Stage 4.4: Official 4C4D reproduction, N3V flame_steak (2026-10-15 01:57 – 05:42 UTC)

**Purpose**: determine whether 4C4D's mediocre results on Panoptic are due to our environment / parameters, or because the data itself is too hard.

**Result**: reproduction succeeded; details in [docs/reproduction-n3v.md](docs/reproduction-n3v.md)

| flame_steak | PSNR | DSSIM1 | DSSIM2 | LPIPS |
|---|---|---|---|---|
| Our reproduction | **24.16** | 0.0738 | 0.0451 | 0.1137 |
| Paper Table 7 | 23.72 | 0.075 | 0.046 | 0.114 |

- **Conclusion: the environment and parameters are fine.** The 2.5 dB gap on Panoptic is mainly due to camera placement: our 4 cameras surround the person along an arc of about 145°, with 26°–73° between neighbors; N3V is a row of cameras all facing the same direction. In addition, the Panoptic background is a plain white wall and the subject's movements are large
- Recommendations for real-world shooting: concentrate the cameras on one side, with no more than about 30° between neighbors; keep virtual camera paths within the area covered by the cameras
- Training took 3 hours 14 minutes (1352×1014, 2.35 million Gaussians)

**Problems encountered** (all fixed; each failure cost about 2 minutes)
1. Newer gdown removed the `--id` argument → switched to Google Drive's confirmed direct-download link and verify gzip integrity
2. In a `find | grep | head` pipeline, grep received SIGPIPE, which together with `set -e` / `pipefail` aborted the task → added `|| true` to every pipeline feeding head/tail; training-camera detection now uses bash string matching instead of `echo | grep -q`
3. Before starting the pod, listed only the file manifest of the preprocessed archive on the server and extracted two small files to check (21 cameras, PINHOLE, naming format), to avoid wasted runs on the pod

**Cost**: $7.30. **Cumulative Phase 1 GPU cost about $26.2**

---

## Stage 5 (first version): Camera-path workbench, sample scene (2026-10-15)

**Joey's request**: build a frontend using the 4C4D sample, starting from a fixed camera, where the 4C4D camera path can be set.

**What was done** (documentation in [frontend/README.md](frontend/README.md))
- The Flask backend listens only on 127.0.0.1:8765 and is accessed via SSH tunnel; it starts automatically after a server reboot
- Page: three.js point cloud + previews of the 21 real cameras; choose a fixed camera (thumbnails); keyframe editing (move right / up / forward in units of "camera spacing", pan left/right, tilt up/down, field of view, output time, source video time); in "camera view", the camera path is previewed by playing it over the point cloud
- `traj.py`: keyframes → per-frame poses (smooth curves + spherical interpolation + ease in/out); all 8 unit tests pass
- The confirmation dialog shows the GPU, frame count, estimated time and cost, and balance; after confirmation a separate process starts the pod → renders → pulls back the video → stops the pod, and the page shows progress and actual cost
- Pod side `fourc4d_direct.py`: loads the trained model directly to export the scene / render along a camera path, **without needing the training dataset**; each render job is expected to take about 5 minutes and $0.15
- Tested the whole flow in simulation mode (no GPU): scene export, camera path computation, rejection of unconfirmed requests, no duplicate submissions while a job is running, and video playback and download all work

**Not done yet**
- The pod-side export and render scripts have not yet been run on a real GPU
- Footage upload (the other half of the original Stage 5 plan) and GEN3C model selection

**Problems encountered**: a remote / local `pkill -f` once again matched the very shell running it → the launch script now looks up the PID by port and kills that

**Cost**: $0

### Stage 5.1: Real GPU test of the camera-path workbench (2026-10-15 06:23 – 06:28 UTC)

- **Preparing the scene preview**: about 1.5 minutes from pod start to pod stop, $0.04. Thumbnails for all 21 cameras were rendered correctly by the model; the point cloud is 200,000 points sampled from the 2.35 million Gaussians
- **Rendering a 4-second sample camera path** (cam00 → move right 2 steps, pan left 10°): about 1.5 minutes, $0.045; rendering itself took 5.8 seconds (20.8 frames/s), 4.3 GB VRAM. The image is correct; the foreground at the very bottom (an area the training cameras did not capture) is somewhat blurry
- **Problem found and fixed**: at the end of a job the pod was stopped by name, but export and rendering had each started a pod with the same name `atlas-render`; the guard found two pods for that name and refused to act, so **the render pod was not stopped**. I noticed and immediately stopped it by ID, adding only about 30 seconds of runtime. The guard's "auto-stop after task ends" would also have caught it within 12 minutes. Fix: the scheduler records the pod ID and **stops and removes it by ID** at the end; cost timing now starts before calling gpu-up; the estimate formula on the page was updated from measurements (fixed overhead about 2 minutes)

---

## Repository cleanup: making it deployable by others (2026-10-15)

**Joey's request**: push both the frontend and backend to GitHub and clean up the repository so anyone can deploy it and use it right away; prompt users to sign up for RunPod and choose the recommended GPU type.

**What was done**
- Added [DEPLOY.md](DEPLOY.md): 6 steps, from signing up for RunPod, adding funds, the recommended GPU type (A100 80GB; H100 only when out of stock), creating the Global volume and two API keys, to deploying, training the sample model and opening the workbench
- Added `deploy.sh`: one-step install or self-check (`--check`); it only changes the current user's own files; anything missing is listed in order
- Added `requirements.txt`; if the system Python lacks Flask, a `.venv` is created automatically inside the repository
- Removed hard-coded local machine details:
  - The watchdog crontab is now a template that fills in the local home directory and PATH at install time;
  - `atlas@` in the docs changed to `<username>@`
- Models are now kept at a fixed location on the Global volume, `models/n3v_flame_steak/`:
  - `n3v_repro` places the model there automatically after training;
  - if there is no model at the fixed location, the most recent N3V training result is used automatically, so the existing model does not need to be moved;
  - if there is no model at all, it errors out and says which command to run
- README gained a "Quick start" section and a workflow diagram (docs/workflow.svg)

**Tests**: 77 guard, 20 runner and 8 camera path tests all pass; simulating a new user running `deploy.sh --check` in an empty home directory correctly lists the 4 missing items; the workbench flow works in simulation mode

**Cost**: $0

---

## BUS 205 showcase page /showcase (2026-10-15)

**Joey's request**: an English results page for the final presentation, added to the workbench as /showcase, to be screen-recorded for class.

**What was done**
- 6 screens: title → camera layout (spread out vs. concentrated) → GEN3C vs. 4C4D side by side → comparison with a real camera (three-panel video) → N3V reproduction (video + metric comparison table against the paper) → workbench render sample and measured cost
- The two GEN3C variants, "true intrinsics" and "multi-camera bullet time", were never run, so as requested this section was left out
- The top-down camera maps are computed from real camera poses:
  - Panoptic's 4 input cameras span 144° around the subject, with a maximum gap of 72°, and the evaluation camera sits right in that gap;
  - N3V's 21 cameras all lie within 53° on one side, and the 4 input cameras span 51°
- Asset build script `frontend/showcase_build.py`: converts videos to H.264 + faststart (7.2 MB total), stored outside the repository; page data contains only numbers and file names
- Took screenshots with headless Chromium at 1920×1080 and 1366×768 and checked each screen (fixed overlapping labels and misaligned headers on the three-panel video); checked the page, data and video metadata, and none contain paths, IPs, IDs or usernames

**Problem**: initially used "the intersection point of the optical axes" as the N3V subject position; because N3V's optical axes are nearly parallel, the computed angle was too large (64°). Changed to use the point-cloud median, projected onto the ground plane to compute azimuths

**Cost**: $0

---

## Camera-path workbench switched to an English UI, with a Chinese option (2026-10-15)

**Joey's request**: the main page defaults to English, with an EN / 中文 toggle in the top-right corner that remembers the choice; the backend returns only a code + parameters and the frontend does the translation; /showcase stays fully English.

**What was done**
- Added `frontend/static/i18n.js`: English and Chinese dictionaries, with unified terms keyframe, camera path, base camera, confirm & render, scene, render job; the choice is stored in localStorage, with every read and write wrapped in try/catch, falling back to English if unreadable
- `index.html`: all static text is now tagged with `data-i18n`; the top-bar "Results page ↗" link (previously in Chinese) changed to "Results ↗"
- `app.js`: slider names, keyframe list, hints, estimates, confirmation dialog, job bar, and balance / GPU status all go through `t()`; switching language redraws these dynamic parts without interrupting a playing video
- `app.py`: errors are unified as `{"code", "params"}`, 10 kinds in total, e.g. `job_running`, `too_many_frames {max}`; Flask's own 400/404/409 are also converted to codes
- `job_runner.py`: progress is stored as `stage_code` + `stage_params` (e.g. `rendering {done, total}`), failures as `error_code` + `error_params`; technical details go only into job.log
- `models.json` gained `title_en`
- Compatibility with jobs from before the change: they only have Chinese stage text, so the frontend displays them by state and no Chinese leaks through

**Tests** (no GPU)
- End to end: headless Chromium + DevTools protocol, running the full flow in English in simulation mode, 12 checks in total; at every step the page text and title / alt / aria-label were scanned, with **0 Chinese leftovers** and 0 console errors:
  - prepare scene (confirmation dialog → job) → choose base camera → add keyframes, adjust sliders → play in camera view → confirmation dialog → rendering → result video;
  - English translations of 8 backend error codes;
  - a failed job in the new format, and old jobs from before the change (with Chinese stage text);
  - after switching between Chinese / English and refreshing, the choice is remembered;
  - with localStorage disabled, it defaults to English and switching still works
- Real data: opened in English on the production server, the two old jobs show as "Done / Finished", with no Chinese on the page
- Code grep: the only Chinese left in the frontend code is comments, the Chinese dictionary in `i18n.js`, the "中文" toggle button itself, and server terminal output and internal logs that never appear in the UI
- 8 camera path, 77 guard and 20 runner unit tests all pass

**Cost**: $0

