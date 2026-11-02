#!/usr/bin/env python3
"""Read native HDR back-buffer dumps (scRGB, 1 = 80 nits).

  hdr_stats.py <shot.dds> [<shot.dds> ...] [--paper 200] [--heat out.png]

For each shot it prints the share of bad values and the brightest pixels in nits. --heat writes the last shot as its SDR
image, with pixels above paper white painted from yellow to red, which shows where the highlights are on any screen.
"""
import argparse
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from dds2png import decode, shot_image, write_png  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("shots", nargs="+")
    ap.add_argument("--paper", type=float, default=200.0)
    ap.add_argument("--heat")
    a = ap.parse_args()
    print("shot                                nonfinite negative | luma nits p50  p99  max | channel max | >paper  >1.5x")
    nits = None
    for name in a.shots:
        path = pathlib.Path(name)
        rgb, _, desc = decode(path)
        if desc != "RGBA16F":
            print(f"{path.stem:36s} not an scRGB shot ({desc})")
            continue
        bad = ~np.isfinite(rgb)
        v = np.nan_to_num(rgb, nan=0.0, posinf=0.0, neginf=0.0)
        y = np.maximum(v[..., 0] * 0.2126 + v[..., 1] * 0.7152 + v[..., 2] * 0.0722, 0.0) * 80.0
        peak = v.max(axis=-1) * 80.0
        over = 100.0 * np.mean(peak > a.paper)
        over2 = 100.0 * np.mean(peak > a.paper * 1.5)
        print(f"{path.stem:36s} {100 * bad.any(axis=-1).mean():8.3f}% {100 * (v < 0).any(axis=-1).mean():7.3f}% | "
              f"{np.percentile(y, 50):8.1f} {np.percentile(y, 99):4.0f} {y.max():4.0f} | {peak.max():11.0f} | {over:5.2f}% {over2:5.2f}%")
        nits = peak
    if a.heat and nits is not None:
        image = shot_image(pathlib.Path(a.shots[-1]), a.paper).astype(np.float32)
        t = np.clip((nits - a.paper) / a.paper, 0.0, 1.0)[..., None]
        heat = np.concatenate([np.full_like(t, 255.0), 230.0 * (1.0 - t), np.zeros_like(t)], axis=-1)
        mask = (nits > a.paper)[..., None]
        out = np.where(mask, image * 0.25 + heat * 0.75, image)
        write_png(pathlib.Path(a.heat), np.clip(out, 0, 255).astype(np.uint8)[::3, ::3])
    return 0


if __name__ == "__main__":
    sys.exit(main())
