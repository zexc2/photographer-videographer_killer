# Stage 4: baseline test method

Goal: on the same CMU Panoptic clip (`171204_pose1`, one person, large movements), run GEN3C and 4C4D once each.
1. **Evaluation**: generate frames from the position of the **evaluation camera** (cam00, not used in training) and compare frame by frame with what that camera actually captured.
2. **Camera move**: both models generate a video along **the same camera trajectory**, moving smoothly from GEN3C's input camera (cam24) to the evaluation camera (cam00), 4 seconds, 121 frames.
3. **Logging**: time, peak GPU memory and GPU cost for every step.

## Inputs each model receives

| | GEN3C | 4C4D |
|---|---|---|
| Video | 1 camera (cam24) | 4 cameras (cam05, 08, 24, 25) |
| Camera parameters | **No ground-truth calibration**: depth, pose and intrinsics all estimated from the video by ViPE | Ground-truth calibration (Panoptic, undistorted) |
| Scenario simulated | One camera, no calibration | 4 fixed phones at the event, calibrated in advance |
| How new views are generated | A pretrained video diffusion model (7B) projects the input frames into the new view by depth, then "fills in" the rest | Trains 4D Gaussians specifically for this clip (30k steps) |

## Common evaluation protocol (`pod/tools/eval_metrics.py`)

- **Frames**: frames 0–120 of the source clip (GEN3C generates 121 frames in one go). For 4C4D we also report scores on all 300 frames.
- **Image**: both models and the ground truth are converted to the same 960×528 region before comparison. GEN3C outputs 704×1280, corresponding to the original resized to 720×1280 with 8 px cropped top and bottom; 4C4D renders 960×540, likewise with 6 px cropped top and bottom.
- **Metrics**: PSNR (higher is better), SSIM (higher is better, max 1), LPIPS (AlexNet, lower is better).
- **Self-check**: identical images give PSNR 100; a deliberate 8 px offset drops it to 24 dB; running the ground truth through GEN3C's pipeline gives 38.8 dB (only resizing and compression loss remain).

## Steps taken for fairness

1. **Centered principal point**: the Panoptic cameras' principal points are off-center by up to about 18 px, while 4C4D assumes the principal point is exactly at the center when reading cameras. So the principal point is moved to the center during undistortion; otherwise 4C4D would have a systematic misalignment at every camera.
2. **The evaluation camera is not used to pick the model**: 4C4D's official training keeps only the checkpoint with "the best test-set score", and the test set is the evaluation camera — equivalent to picking the model after seeing the answers. Here we test only once at the end of training, using the final model. Repeated testing during training (`exhaust_test`) is also turned off.
3. **GEN3C scale alignment**: depth estimated by ViPE from monocular video is only in "what it thinks are meters". We compare ViPE's depth with the MASt3R point cloud (triangulated from the ground-truth calibration, in real meters) at the input camera, take the median ratio, and use it to scale the evaluation camera's relative displacement. This is standard practice when evaluating novel views from monocular methods.
4. **The evaluation camera takes no part in any training or preprocessing**: the MASt3R point cloud uses only the 4 training cameras; the evaluation camera's images are used only for the final scoring.
5. **The models themselves are not modified**: for both models we replace only the single function that decides "where the camera trajectory comes from" (`gen3c_run.py`, `fourc4d_render.py`); the rendering and generation pipelines are entirely official code.

## Known limitations

- Only one 10-second sequence and one evaluation camera were tested, so the conclusions are preliminary.
- GEN3C gets less information than 4C4D (1 camera, no calibration), which works against it; but that is exactly the question the "only one camera" approach needs to answer.
- The evaluation camera is 1.26 m and 32° away from GEN3C's input camera, far beyond the camera-move range in GEN3C's examples (about 0.3). This is a deliberate stress test; GEN3C showing obvious flaws here is expected.
