# Baseline comparison report: GEN3C vs 4C4D (phase 1)

> Date: 2026-10-15 | Data: CMU Panoptic `171204_pose1` (one person, large movements, 10 s) | GPU: RunPod A100 80GB SXM ($1.79/h)
> Method details: [stage4-method.md](stage4-method.md); data preparation: [stage3-data.md](stage3-data.md).

## Bottom line

**With "4 fixed phones at the event", 4C4D is clearly better**: evaluation-camera PSNR 21.7 vs. 16.0 for GEN3C; 4C4D gets the person's position and motion right, while GEN3C has severe visible artifacts. But 4C4D's **current code cannot be used commercially**, and it takes about 2.5 hours of training per 10 seconds of footage.
For 3–5 minute highlight reels, both approaches cost **far less than $1,500** in GPU time; for covering an entire 8-hour event, both cost **far more than $1,500**.

## 1. Test setup

| | GEN3C | 4C4D |
|---|---|---|
| Input | Video from **1 camera** (cam24); all camera parameters estimated by ViPE | Video from **4 cameras** + ground-truth calibration |
| Scenario simulated | One camera, no calibration | 4 fixed phones at the event, calibrated in advance |
| How it works | A pretrained 7B video diffusion model projects the frames into the new view by depth, then "fills in" the rest | Trains 4D Gaussians specifically for this clip (30k steps) |
| Evaluation | Both models generate frames from the **evaluation camera cam00** (not used in training), compared frame by frame against what cam00 actually captured. Same protocol: the same 121 frames, the same 960×528 image region | Same |

## 2. Objective metrics (evaluation camera)

| Approach | PSNR ↑ | SSIM ↑ | LPIPS ↓ | Notes |
|---|---|---|---|---|
| GEN3C (1 camera) | 15.96 | 0.661 | 0.578 | 121 frames; per-frame PSNR 14.9–16.7 |
| **4C4D (4 cameras)** | **21.74** | **0.818** | **0.378** | 121 frames; per-frame PSNR 20.8–22.0 |
| 4C4D, all 300 frames | 21.83 | 0.818 | 0.375 | Quality stable over the 10 s |

- Every 3 dB of PSNR roughly halves the pixel error. 4C4D is **5.8 dB** higher than GEN3C, with **35%** lower perceptual difference (LPIPS).
- Two independent 4C4D training runs scored 21.42 and 21.69 on the training script's final self-test, so **the result is reproducible**.
- For reference: under the same protocol, "the real footage resized and compressed once" reaches 38.8 dB. Both approaches are still far from indistinguishable from reality.

## 3. Subjective issues (watching the videos)

| Issue | GEN3C | 4C4D |
|---|---|---|
| Person's position and size | ❌ Misplaced and too small; the person gets enlarged in the second half of the camera move | ✅ Correct |
| Motion matches reality | ⚠️ Roughly follows, but the pose is distorted | ✅ Timing and pose both match |
| Person distortion | ❌ Clearly distorted in the middle | ⚠️ No distortion, but face and clothing texture are blurry and noisy |
| Flicker | ⚠️ Background texture jumps | ⚠️ Slight |
| Visible artifacts | ❌ **Two people appear at the end of the camera move**; large hallucinated areas mid-way in the evaluation view | ⚠️ Black specks floating in the air (the "floaters" common in 3DGS) |
| Background | ❌ Wall structure misaligned and blurry | ✅ Clean and accurate |

**The main reason GEN3C does poorly is not the generative model itself, but that ViPE got the camera focal length wrong by a factor of two** (estimated 2807 vs. a true 1400), and the depth scale is off by 1.55×. The 3D structure was wrong from the start. Phone and Insta360 intrinsics can be obtained with a one-time calibration, so "GEN3C + calibrated camera" needs to be tested again (see section 7).

## 4. Time, GPU memory, cost (measured)

| Step | GEN3C | 4C4D |
|---|---|---|
| Environment setup (restore environment pack from the Global volume) | about 1 minute | about 2 minutes |
| Data preprocessing | ViPE depth and pose estimation: **11.4 minutes per 10 s of footage** | MASt3R initial point cloud: 1.5 minutes |
| Model loading | about 4 minutes (71 GB of weights) | — |
| Generation / training | **about 16 minutes per 5 s of output** (121 frames, 704×1280) | **Training 2 h 27 min per 10 s of footage** (30k steps, 960×540, 4.2 million Gaussians) |
| Rendering | (generation is the output) | 300 evaluation-view frames + 121 camera-move frames, about 9 minutes total (including loading); once trained, the camera move can be changed freely at almost no cost |
| Peak GPU memory | **44 GB** (T5 text encoder on CPU) | **24.6 GB** |
| GPU cost of this test | about $1.4 (ViPE + loading + 2 generated clips) | about $4.8 (training + rendering) |

## 5. Converting to the cost of one 8-hour event

**Assumptions**: GPU cost only — no labor, equipment or storage; at the speeds measured in this test.

| Deliverable | GEN3C (A100) | 4C4D (A100) | 4C4D (cheaper 48 GB card, **to be measured**) |
|---|---|---|---|
| Unit cost | About 20 GPU-minutes per 5 s of output (ViPE + generation) ≈ **$0.61** | About 2.63 GPU-hours per 10 s of footage ≈ **$4.71**; multiple camera moves can be rendered from the same clip | RTX A6000 / A40 $0.33–0.59/h; if speed is comparable to A100, about **$0.9–1.6 per 10 s** |
| Camera-move shots for a **3-minute highlight reel** | 36 clips → about **$22** | 18 clips → about **$85** | about **$16–28** |
| Camera-move shots for a **5-minute highlight reel** | 60 clips → about **$37** | 30 clips → about **$141** | about **$26–47** |
| Covering the entire **8 hours** (virtual camera moves throughout) | 5,760 clips → about **$3,500** | 2,880 clips → about **$13,600** | about $2,500–4,500 |
| Compared with a $1,500 videographer quote | Highlights ✅ far below; full event ❌ over | Highlights ✅ far below; full event ❌ over | Highlights ✅; full event ❌ |

**Conclusions**:
- On GPU cost, **"AI camera-move highlight reels" are commercially viable**, at tens to a bit over a hundred dollars per event.
- "Replacing the videographer for the whole event" is **not viable** today: GPU cost already exceeds the labor quote, and that's before processing time — 4C4D would need thousands of GPU-hours for 8 hours of footage.
- A realistic product: **fixed cameras record the whole event + AI camera moves on hand-picked highlights afterwards**, as an add-on to the videography service rather than a replacement for the videographer.

## 6. Commercial use

| Component | License | Commercial use? | Reason |
|---|---|---|---|
| GEN3C code | Apache-2.0 | ✅ | |
| GEN3C / Cosmos weights | NVIDIA Open Model License | ⚠️ **Conditional** | The model card states that "disabling the safety guardrails automatically terminates the license", while the official inference script forces the guardrails off. Before commercial use, the guardrails must be enabled, or this must be confirmed with NVIDIA |
| T5-11B | Apache-2.0 | ✅ | |
| ViPE code | Apache-2.0 (Unik3D portion CC BY-NC-SA) | ⚠️ | Depth models used in the default pipeline, such as UniDepth, are non-commercial (to be verified one by one). If the cameras are calibrated, a commercially usable depth model can be swapped in |
| 4C4D code | MIT, but bundles the Inria 3DGS rasterizer | ❌ | The Inria rasterizer is restricted to non-commercial, research and evaluation use |
| MASt3R | CC BY-NC-SA 4.0; weights also bound by training-dataset licenses | ❌ | Non-commercial |
| CMU Panoptic | Research only | ❌ (test data only) | Commercial use would use our own footage, so not affected |

**Summary**:
- **GEN3C route**: conditionally usable commercially; the guardrail clause must be resolved, and ViPE's non-commercial depth models replaced.
- **4C4D route**: not commercially usable today; the rasterizer and point-cloud initialization must be replaced with commercially usable implementations — see the survey in section 7.

## 6.5 Reproduction check: 4C4D's mediocre results come from hard data, not from our setup

To confirm why 4C4D does only moderately well on Panoptic, we reproduced it with the official settings on N3V flame_steak, the data used in the paper (details in [reproduction-n3v.md](reproduction-n3v.md)):
- **Our reproduction: PSNR 24.16 / DSSIM1 0.074 / LPIPS 0.114; paper: 23.72 / 0.075 / 0.114** — the results match.
- Conclusion: the environment and parameters are fine. The 2.5 dB drop on Panoptic is because the 4 cameras we chose surround the person on an arc of about 145°, with little overlap between them; N3V is a row of cameras all facing the same direction.
- Implication for real shoots: cameras should be concentrated in one direction, with no more than about 30° between neighbors; virtual camera moves should also stay within the area covered by the cameras.

## 7. Suggested next steps (Joey decides)

1. **A commercially usable sparse-view 4DGS approach** (required by our agreement if 4C4D wins): survey in [commercial-4dgs-alternatives.md](commercial-4dgs-alternatives.md). Conclusion: nothing ready-made exists; we suggest gsplat (Apache-2.0) + MapAnything's Apache weights, porting the ideas of 4DGS / 4C4D, an estimated 1–2 weeks of engineering.
2. **GEN3C + true intrinsics**: rule out the effect of ViPE's wrong focal length, about $0.6. If quality improves a lot, the "one calibrated camera" approach is worth pursuing.
3. **GEN3C multi-view mode** (4 cameras, bullet-time freeze-frame camera move): about $1.3. Freeze-frame orbits are a very popular effect in event videography.
4. **Lower 4C4D's cost**: measure speed on RTX A6000 / A40; try 15k steps instead of 30k and see how much quality is lost.
5. Repeat on `160906_band1` (three people playing music, occluding each other — closer to a real event) to confirm the conclusions.

## Appendix: result files (not in the repo)

- Server `~/runs/20261014-070012-baseline/out/videos/`: GEN3C evaluation comparison video, camera-move video
- Server `~/runs/20261014-223624-fourc4d/out/videos/`: 4C4D evaluation comparison video, camera-move video, **side-by-side camera-move comparison of both models** `171204_pose1_path_side_by_side.mp4`
- Global volume `results/`: full-resolution videos, the 4C4D model and all rendered images
