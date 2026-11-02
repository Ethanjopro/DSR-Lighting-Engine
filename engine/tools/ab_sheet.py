#!/usr/bin/env python3
"""Side-by-side PNGs from back-buffer shots: captures/<run>/<variant>/<slug>.dds.

  ab_sheet.py <run> --variants off,on            one half-size side-by-side per bonfire, in captures/<run>/cmp
  ab_sheet.py <run> --variants off,on --full     also full-size PNGs per variant

Prints the median pixel move and the share of pixels moved by more than 8/255 for each pair.
"""
import argparse
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from dds2png import decode, to_image, write_png


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--variants", default="off,on")
    ap.add_argument("--full", action="store_true")
    a = ap.parse_args()
    root = pathlib.Path("captures") / a.run
    variants = a.variants.split(",")
    slugs = sorted(p.stem for p in (root / variants[0]).glob("*.dds"))
    out = root / "cmp"
    out.mkdir(parents=True, exist_ok=True)
    for slug in slugs:
        imgs = []
        for v in variants:
            path = root / v / f"{slug}.dds"
            if not path.exists():
                break
            rgb, ldr, _ = decode(path)
            imgs.append(to_image(rgb, ldr))
            if a.full:
                write_png(root / v / f"{slug}.png", imgs[-1])
        if len(imgs) != len(variants):
            print(slug, "missing a variant")
            continue
        h = imgs[0].shape[0] // 2
        parts = []
        for i, im in enumerate(imgs):
            if i:
                parts.append(np.full((h, 8, 3), 255, np.uint8))
            parts.append(im[::2, ::2])
        write_png(out / f"{slug}.png", np.concatenate(parts, axis=1))
        for i in range(1, len(imgs)):
            d = np.abs(imgs[0].astype(np.int16) - imgs[i].astype(np.int16)).max(axis=2)
            print(f"{slug:20s} {variants[0]} vs {variants[i]}: median {np.median(d):.0f}, mean {d.mean():.2f}, >8: {(d > 8).mean() * 100:.1f}%, >32: {(d > 32).mean() * 100:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
