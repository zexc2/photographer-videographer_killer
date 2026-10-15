# Official 4C4D reproduction: N3V flame_steak (2026-10-15)

## Why we did this

In our Panoptic test, 4C4D scored only 21.7 PSNR at the evaluation camera, with blurry images and floaters (see [report-baseline.md](report-baseline.md)). We needed to determine which of these was the cause:
- **Our problem**: the environment (we upgraded the official py3.7 / CUDA 11.6 to py3.10 / CUDA 12.4) or the parameter settings are wrong;
- **The data itself is hard**: the Panoptic scene is too difficult — wide angles between cameras, little overlap.

Approach: run once on the dataset used in the paper, **exactly with the official settings**, and compare against the paper's table.

## Setup (same as official / paper)

| Item | Setting |
|---|---|
| Scene | N3V **flame_steak** (the example scene in the official README) |
| Raw data | Official N3V GitHub release, 21 cameras, 2704×2028, first 300 frames |
| Camera parameters + initial point cloud | **The authors' preprocessed package** (MASt3R point cloud, 21 cameras) |
| Training | Official `configs/dynerf/flame_steak.yaml` used unchanged, training views `1,10,13,20`, 30k steps, `--res 2` (the paper states images are downscaled by half) |
| Camera-move video | The official README command `--traj arc` |
| Evaluation | Official `render.py --validate`, test views = the remaining 17 cameras; to save time, 1 frame in every 10 (510 images total). The paper uses all frames |
| Code and environment | Exactly the same as the Panoptic test (same environment pack, same commit) |

## Results

| flame_steak (4 training views) | PSNR ↑ | DSSIM1 ↓ | DSSIM2 ↓ | LPIPS ↓ |
|---|---|---|---|---|
| **Our reproduction** | **24.16** | **0.0738** | **0.0451** | **0.1137** |
| Paper, Table 7 | 23.72 | 0.075 | 0.046 | 0.114 |
| Difference | +0.44 dB | 0.001 better | 0.001 better | Same |

- Official self-test during training (1 in every 100 test-view images): 24.22 at step 10,500 → 24.76 at step 27,000 (best) → 24.68 at step 30,000. The official script keeps the checkpoint with the best test-set score (step 27,000); the paper's numbers were presumably obtained the same way.
- 2.35 million Gaussians at the end.
- Subjectively, the person and background are both sharp in the camera-move video, with only slight blur and ghosting at angles the training cameras didn't capture (image edges).

**Conclusion: the reproduction succeeded; our environment and parameters are fine.** A 0.44 dB difference is within a reasonable range, possibly due to frame subsampling in evaluation, random seed, or a different GPU.

## So why is Panoptic worse?

Same code: 24.2 on N3V, 21.7 on Panoptic — a 2.5 dB gap. The differences are all in the data:

| | N3V flame_steak | Our Panoptic setup |
|---|---|---|
| Camera layout | A row of cameras **facing the same direction** (forward-facing array); large overlap between the 4 training cameras | 4 cameras **surrounding the person on an arc of about 145°**, 26°–73° between neighbors |
| Evaluation view | Between the training cameras, with a similar viewpoint | 26°–47° away from the nearest training camera, seeing sides the training cameras barely captured |
| Background | Richly textured kitchen | Large, plain-white, repetitive dome walls; hard to match |
| Motion | Cooking in place, small movements | Large full-body stretches, fast arm movements |
| MASt3R matching | — | For camera pairs more than 70° apart, only 2%–8% of matches pass the check |

So Panoptic does worse **mainly because the cameras are too spread out and overlap too little**, not because of the environment or parameters. The paper shows the same pattern: the authors' two hardest N3V scenes score only 19.2–19.6 dB.

## Implications for real shoots

1. **Cameras should be concentrated in one direction with enough overlap**: e.g. 4 phones in a fan of about 60°–90° directly in front of the stage, no more than 30° between neighbors. Don't place them in a ring around the subject.
2. **Virtual camera moves only work well within the area covered by the cameras**: once the shot leaves that area — e.g. turning to the side or back — quality drops noticeably.
3. **Richly textured backgrounds help**; large solid-color walls are bad for reconstruction.

As a next step, we could pick 4 more concentrated cameras on Panoptic following this principle and test again, to measure how much the camera layout actually matters.

## Time and cost

| Step | Time |
|---|---|
| Environment (already on the container disk) | 5 seconds |
| Data (download, extract 6,300 images) | 3 minutes |
| Training, 30k steps (1352×1014) | **3 h 14 min** |
| Official camera-move render (480 frames) | 5 minutes |
| Evaluation (510 images) | 18 minutes |

Total pod cost **$7.30** (including two startup failures caused by script errors, about 4 minutes in total).

## License

N3V dataset: CC BY-NC 4.0, research only; this repo contains no N3V data.
