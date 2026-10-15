#!/usr/bin/env python3
"""Held-out camera evaluation: PSNR / SSIM / LPIPS, plus a "ground truth | prediction | difference" comparison video. Run in the 4c4d env (torch, lpips, scikit-image).

Common protocol (so the two models are directly comparable):
  - Frames: frames 0..N-1 of the source clip (GEN3C generates 121 frames in one go)
  - Image: GT 1920x1080 -> resize to 960x540 -> crop 6 px top and bottom to 960x528
    This matches GEN3C's processing (1080p -> 720x1280 -> centre crop to 704x1280, scaled proportionally to 528x960)
  - GEN3C prediction 704x1280 -> resize to 528x960; 4C4D render 540x960 (--res 2) -> same 6 px top/bottom crop
Usage:
  eval_metrics.py --pred X.mp4|DIR --kind gen3c|4c4d --gt-dir images/ --gt-cam cam00 --frames 121 --out metrics.json [--video cmp.mp4]
"""
import argparse
import json
import subprocess
from pathlib import Path

import cv2
import numpy as np

EVAL_W, EVAL_H = 960, 528


def read_pred(path, n):
    p = Path(path)
    if p.is_dir():
        files = sorted(p.glob("*.png"))[:n]
        return [cv2.imread(str(f))[:, :, ::-1] for f in files]
    cap = cv2.VideoCapture(str(p))
    frames = []
    while len(frames) < n:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f[:, :, ::-1])
    return frames


def to_eval(img, kind):
    """Turn frames from any source into the common 528x960 evaluation image (RGB uint8)."""
    h, w = img.shape[:2]
    if kind == "gt":
        img = cv2.resize(img, (960, 540), interpolation=cv2.INTER_AREA)
        return img[6:534]
    if kind == "gen3c":  # 704×1280 → 528×960
        return cv2.resize(img, (EVAL_W, EVAL_H), interpolation=cv2.INTER_AREA)
    if kind == "4c4d":   # 540x960 (resized first if the resolution differs)
        if (w, h) != (960, 540):
            img = cv2.resize(img, (960, 540), interpolation=cv2.INTER_AREA)
        return img[6:534]
    raise ValueError(kind)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", required=True)
    ap.add_argument("--kind", required=True, choices=["gen3c", "4c4d"])
    ap.add_argument("--gt-dir", required=True)
    ap.add_argument("--gt-cam", required=True)
    ap.add_argument("--frames", type=int, default=121)
    ap.add_argument("--out", required=True)
    ap.add_argument("--video", help="output comparison video (ground truth | prediction | difference x3)")
    ap.add_argument("--no-lpips", action="store_true")
    a = ap.parse_args()

    from skimage.metrics import structural_similarity
    preds = read_pred(a.pred, a.frames)
    n = len(preds)
    if n == 0:
        raise SystemExit(f"cannot read predicted frames: {a.pred}")
    gts = [cv2.imread(str(Path(a.gt_dir) / f"{a.gt_cam}_{i:04d}.png"))[:, :, ::-1] for i in range(n)]

    lp = None
    if not a.no_lpips:
        import lpips
        import torch
        lp = lpips.LPIPS(net="alex", verbose=False).cuda().eval()

    rows, writer = [], None
    if a.video:
        writer = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{3 * 480}x{264}", "-r", "24", "-i", "-", "-c:v", "libx264", "-crf", "26",
             "-pix_fmt", "yuv420p", a.video], stdin=subprocess.PIPE)
    for i in range(n):
        g = to_eval(gts[i], "gt").astype(np.float32) / 255
        p = to_eval(preds[i], a.kind).astype(np.float32) / 255
        mse = float(np.mean((g - p) ** 2))
        psnr = 10 * np.log10(1.0 / max(mse, 1e-10))
        ssim = float(structural_similarity(g, p, channel_axis=2, data_range=1.0))
        r = {"frame": i, "psnr": round(psnr, 3), "ssim": round(ssim, 4)}
        if lp is not None:
            import torch
            tg = torch.from_numpy(g).permute(2, 0, 1)[None].cuda() * 2 - 1
            tp = torch.from_numpy(p).permute(2, 0, 1)[None].cuda() * 2 - 1
            with torch.no_grad():
                r["lpips"] = round(float(lp(tg, tp)), 4)
        rows.append(r)
        if writer:
            diff = np.clip(np.abs(g - p) * 3, 0, 1)
            tiles = [cv2.resize((x * 255).astype(np.uint8), (480, 264), interpolation=cv2.INTER_AREA) for x in (g, p, diff)]
            for t, label in zip(tiles, ("GT", a.kind.upper(), "|diff|x3")):
                cv2.putText(t, label, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
            writer.stdin.write(np.hstack(tiles).tobytes())
    if writer:
        writer.stdin.close()
        writer.wait()

    mean = {k: round(float(np.mean([r[k] for r in rows])), 4) for k in rows[0] if k != "frame"}
    res = {"kind": a.kind, "pred": str(a.pred), "gt_cam": a.gt_cam, "frames": n,
           "eval_resolution": f"{EVAL_W}x{EVAL_H}", "mean": mean, "per_frame": rows}
    json.dump(res, open(a.out, "w"), indent=1)
    print(json.dumps({"kind": a.kind, "frames": n, **mean}, ensure_ascii=False))


if __name__ == "__main__":
    main()
