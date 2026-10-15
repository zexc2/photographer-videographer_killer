# Stage 3: CMU Panoptic test data

## Why CMU Panoptic

[CMU Panoptic Studio](http://domedb.perception.cs.cmu.edu/) is a spherical capture dome with 31 synchronized 1080p HD cameras mounted on its walls, each with precise intrinsic and extrinsic calibration. It lets us simulate "a few fixed cameras at an event". The footage from cameras not chosen as input is exactly what we need to check whether the AI-generated results are right.

License: **non-commercial research only**. This repo contains no Panoptic images or videos, only download and conversion scripts; the data is downloaded directly to the RunPod Global volume.

## The two sequences we chose

| Sequence | Content | Why we chose it | Clip |
|---|---|---|---|
| `171204_pose1` | One person doing large stretching movements | Single person, clear motion, simple background: best for seeing "does the person get distorted" | 10 s starting at 30 s, 300 frames |
| `160906_band1` | Three people sitting in a circle playing guitar and hand drums | Closest to a real event: multiple people, occlusion, small hand movements | 10 s starting at 20 s, 300 frames |

For both, only the needed 10 seconds are downloaded: ffmpeg clips them with HTTP range requests, so each camera is only a few dozen MB. A full video is up to 3 GB per camera, and the CMU server measured only about 3.5 MB/s.

## How the cameras were chosen

First we computed the positions of all 31 cameras from the calibration, then grabbed one thumbnail frame per camera to see which way the person faces. Only the lowest ring of cameras is used, 1.2–1.5 m above the floor — close to the height of a phone on a tripod.

| Sequence | 4C4D input (4 cameras) | **Evaluation camera** (not used in training) | GEN3C input (1 camera) |
|---|---|---|---|
| 171204_pose1 | cam05, 08, 24, 25: around the front of the person, covering about 145° | **cam00**: directly in front, in the gap between cam24 and cam25 | cam24 (1.26 m from the evaluation camera, 32° rotation) |
| 160906_band1 | cam03, 08, 09, 14 | **cam23**: between cam14 and cam09 | cam14 |

Config file: [`pod/configs/panoptic.json`](../pod/configs/panoptic.json).

### What the evaluation camera is for

It's the **answer key for the exam**. The model only sees the input cameras' footage; we then have it "virtually shoot" from the evaluation camera's position and compare, frame by frame, with what the evaluation camera actually captured at the same moment, giving objective scores:

- **PSNR**: pixel error, higher is better
- **SSIM**: structural similarity, higher is better (max 1)
- **LPIPS**: perceptual difference to the human eye, lower is better

This answers exactly the business question: **at a position where no camera was placed at the event, how close is the AI-generated image to reality?**

### The two models get unequal inputs

- **4C4D**: video from 4 cameras + known calibration. This is the "4 fixed phones at the event" setup.
- **GEN3C**: video from only 1 camera. Camera position, depth and intrinsics are all estimated from the video by ViPE; no ground-truth calibration is given. This is the "one camera, no calibration" setup.

The comparison report notes this. GEN3C also has a multi-view mode (`gen3c_multiview`); if single-camera results aren't good enough, we'll try it in stage 4.

## Data processing pipeline (`atlas-run panoptic`, runs on the pod)

1. **Download**: download the calibration JSON and clip 300 frames from each camera. The clips are saved to `datasets/panoptic/<sequence>/raw/` on the Global volume, so no network access is needed later.
2. **Undistortion**: Panoptic cameras have lens distortion and 4C4D only supports undistorted PINHOLE cameras, so each frame is undistorted using the calibration parameters. Output: `images/camXX_YYYY.png`.
3. **COLMAP model**: one entry per camera, with units converted **from centimeters to meters**. The 3DGS camera far clipping plane is 100, so in centimeters a person 2.7 m away would be clipped.
4. **Initial point cloud**: run MASt3R dense matching on every pair of the 4 training cameras, then triangulate with the known poses to get `points3D.txt` (`pod/tools/mast3r_posed_points.py`). The official pipeline uses MAtCha's posed mode — same idea, but MAtCha can't be packed. **The evaluation camera is excluded**, to avoid "peeking at the answers".
5. **GEN3C input**: turn the input camera into a video → ViPE estimates depth, pose and intrinsics. We also record how far ViPE's estimated focal length is from the true calibration.
6. **Packaging**: everything is packed into a single `stage3.tar` on the Global volume (object storage suits large files). Thumbnails, point-cloud projection check images and metrics are pulled back to the server, **not committed to the repo**.

## What has already been verified on the server

Offline tests with 5 real downloaded frames (no models run on the server):
- Clipping, undistortion, COLMAP files, GEN3C video and meta.json are all generated correctly.
- **Coordinate convention check**: a vertical line from 0 to 1.8 m drawn at the world origin and projected into 5 cameras lands on the person standing in the middle of the stage every time, from feet to head. This shows the units (meters), the rotation and translation conventions, and the undistortion are all correct.
- **Triangulation check**: synthetic 3D points projected into two cameras, with 0.3 px noise added, then triangulated again: median error under 1 mm.

MASt3R matching and ViPE can only run on the pod. When they do, we'll project the point cloud into every camera, including the evaluation camera, and generate check images to verify.
