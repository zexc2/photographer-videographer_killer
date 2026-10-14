#!/usr/bin/env python3
"""留出机位评估：PSNR / SSIM / LPIPS，并生成"真值 | 预测 | 差异"对比视频。在 4c4d 环境里运行（torch、lpips、scikit-image）。

统一口径（两个模型可直接比较）：
  - 帧：源片段的第 0..N-1 帧（GEN3C 一次生成 121 帧）
  - 画面：GT 1920×1080 → 缩放到 960×540 → 上下各裁 6 像素到 960×528
    这与 GEN3C 的处理一致（1080p → 720×1280 → 中心裁成 704×1280，等比缩到 528×960）
  - GEN3C 预测 704×1280 → 缩放到 528×960；4C4D 渲染 540×960（--res 2）→ 同样上下各裁 6 像素
用法：
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
    """把任意来源的帧变成 528×960 的统一评估画面（RGB uint8）。"""
    h, w = img.shape[:2]
    if kind == "gt":
        img = cv2.resize(img, (960, 540), interpolation=cv2.INTER_AREA)
        return img[6:534]
    if kind == "gen3c":  # 704×1280 → 528×960
        return cv2.resize(img, (EVAL_W, EVAL_H), interpolation=cv2.INTER_AREA)
    if kind == "4c4d":   # 540×960（若分辨率不同先缩放）
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
    ap.add_argument("--video", help="输出对比视频（真值 | 预测 | 差异×3）")
    ap.add_argument("--no-lpips", action="store_true")
    a = ap.parse_args()

    from skimage.metrics import structural_similarity
    preds = read_pred(a.pred, a.frames)
    n = len(preds)
    if n == 0:
        raise SystemExit(f"读不到预测帧：{a.pred}")
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
