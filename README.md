# photographer-videographer_killer

> 360 / multi-camera footage → AI scene reconstruction → moving-camera ("virtual camera move") video
> ERA MOMENT internal validation project · USF BUS 205 course project

## Quick start

Want to run it yourself? See **[DEPLOY.md](DEPLOY.md)** — 6 steps:

1. Sign up for [RunPod](https://www.runpod.io/) and add credit; **recommended GPU: A100 80GB**;
2. Create a Global volume and API keys;
3. Clone the repo;
4. Run `./deploy.sh`;
5. Train the sample model (one-time, about 4 hours, about $8);
6. Open the camera-move workbench.

After that, each camera move you render takes about 1.5 minutes and $0.05.

![Workflow](docs/workflow.svg)

## Background

ERA MOMENT does event photography and videography. In the US, a videographer for an 8-hour event rarely quotes below **$1,500**.
This project tests whether we can put just 1 360 camera (Insta360 X3) + a few phones on tripods at an event,
then use AI to reconstruct the scene afterwards and generate video with camera motion — replacing or supplementing an on-site videographer at a **GPU cost well below $1,500**.

## Stages

| Stage | Scope | Status |
|---|---|---|
| 0 | Repo setup | ✅ |
| 1 | GPU cost guard (start / stop / monitor RunPod pods), see [guard/](guard/README.md) | ✅ |
| 2 | Pod environment scripts (GEN3C / ViPE / 4C4D), see [pod/](pod/README.md) | ✅ |
| 3 | CMU Panoptic data preparation, see [docs/stage3-data.md](docs/stage3-data.md) | ✅ |
| 4 | GEN3C vs 4C4D baseline comparison, **report: [docs/report-baseline.md](docs/report-baseline.md)** | ✅ First round done |
| 5 | Camera-move workbench (Flask): 3D preview → set camera path → GPU render, see [frontend/](frontend/README.md) | ✅ Sample-scene version (real GPU test passed); footage upload still to do |
| 6 | Integrate the 360-to-perspective tool | ⏳ |

Detailed progress, issues, time and spending: [PROGRESS.md](PROGRESS.md).

## Phase 1 results (2026-10-15)

On CMU Panoptic, we generated frames from a camera that was not used in training and compared them with the real footage:

| Approach | Input | PSNR ↑ | SSIM ↑ | LPIPS ↓ | GPU cost |
|---|---|---|---|---|---|
| GEN3C | 1 camera | 15.96 | 0.661 | 0.578 | about $0.61 per 5 s of output |
| **4C4D** | 4 cameras | **21.74** | **0.818** | **0.378** | about $4.71 per 10 s of footage |

We also reproduced 4C4D on the N3V data used in the paper (PSNR 24.16 vs. 23.72 in the paper), which shows our environment is fine. Results on Panoptic are mediocre mainly because the cameras are too spread out; see the [reproduction report](docs/reproduction-n3v.md).

Producing a 3–5 minute AI camera-move highlight reel costs about $22–141 in GPU time, far below a $1,500 videographer quote; covering an entire 8-hour event, however, would exceed $1,500. 4C4D's current code cannot be used commercially. Details in the [comparison report](docs/report-baseline.md).

## Architecture

- **Orchestration server** (2 cores, 4 GB): runs only the frontend and orchestration scripts and stores small files; no models run here.
- **RunPod GPU pod** (A100 80GB, H100 if needed): all 3D reconstruction, training and video generation.
- **RunPod Global volume** (not tied to a data center; path inside the pod is `$ATLAS_VOL`): cold data only — model weights, raw datasets, environment packs, final results.
- **Pod container disk** (`$ATLAS_LOCAL`, 200 GB, wiped on stop): conda environments, working directories, intermediate files, checkpoints.
- Details: [docs/storage-and-env.md](docs/storage-and-env.md).

## Repository layout

```
.
├── DEPLOY.md         Deployment guide (starting from signing up for RunPod)
├── deploy.sh         One-step deploy / self-check (./deploy.sh --check)
├── requirements.txt  Server-side Python dependencies (flask, numpy)
├── README.md         Project overview
├── PROGRESS.md       Per-stage progress, issues, time, spending
├── CLAUDE.md         Project brief / working conventions
├── frontend/         Camera-move workbench (Flask + three.js, listens on 127.0.0.1 only)
├── guard/            GPU guard: gpu-up / gpu-down / gpu-status / gpu-guard + offline tests
├── runner/           atlas-run: server → pod job runner
├── pod/              Pod side: environment setup (GEN3C / ViPE / 4C4D), job scripts, model list
└── docs/             Reports and design docs
```

## Third-party model and data licenses

This repository **does not contain** any third-party model weights, datasets, or copies of their code — only links and the scripts that call them. Read and comply with each license before use:

| Name | Link | License (the official page is authoritative) | Commercial use? |
|---|---|---|---|
| CMU Panoptic Studio Dataset | http://domedb.perception.cs.cmu.edu/ | Non-commercial research only; see the site's terms of use | No |
| GEN3C (code) | https://github.com/nv-tlabs/GEN3C | Apache-2.0 | Yes |
| GEN3C-Cosmos-7B / Cosmos-Tokenize1 (weights) | https://huggingface.co/nvidia/GEN3C-Cosmos-7B | [NVIDIA Open Model License](https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license) | Yes, but disabling the safety guardrails terminates the license (see pod/README.md) |
| T5-11B (GEN3C's text encoder) | https://huggingface.co/google-t5/t5-11b | Apache-2.0 | Yes |
| ViPE | https://github.com/nv-tlabs/vipe | Apache-2.0 (Unik3D portion CC BY-NC-SA 4.0); auto-downloaded depth models each have their own licenses | Code yes, some models no |
| 4C4D (CVPR 2026) | https://github.com/yangzf-1023/4C4D | MIT, but bundles the Inria 3DGS rasterizer (non-commercial) | No |
| MASt3R | https://github.com/naver/mast3r | CC BY-NC-SA 4.0; checkpoints are additionally bound by their training-dataset licenses | No |

> Note: CMU Panoptic, MASt3R and others are non-commercial licenses. Phase 1 of this project is academic / technical validation only; commercial use would require a separate license review.

## Security rules

This is a public repo. Never commit any keys, tokens, weights, datasets, videos, point clouds, training outputs, logs, pod IDs or IPs; no single file over 10 MB.
