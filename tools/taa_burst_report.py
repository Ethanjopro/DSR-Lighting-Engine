"""Analyse taa_burst_set.ps1 bursts: per variant and bonfire, standstill flicker and sharpness, and detail energy
during the camera pan, as ratios to a reference variant.

    .venv\\Scripts\\python.exe tools\\taa_burst_report.py captures\\s6\\burst --ref vanilla [--json OUT]

Layout: <root>/<variant>/<slug>/still_NN.png, pan_NN.png. Analysis region: x 1800..3700, y 100..1800 of the
3840x2160 frame (right of the character, clear of the HUD).
  flicker    median over pixels of the temporal std of 8-bit luma across the still frames
  sharp      mean |4-neighbour Laplacian| of the temporal-mean still image (detail at a standstill)
  pan_detail mean over pan frames of mean |Laplacian| (detail in motion; > ~2x the reference suggests the
             history sampler is point-filtered and the Catmull-Rom fetch over-sharpens)
  pan_moved  mean |frame - previous frame| over the pan (checks that the camera really moved)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shaders" / "tools"))
from probe_decode import read_png  # noqa: E402

Y0, Y1, X0, X1 = 100, 1800, 1800, 3700


def luma(p: Path) -> np.ndarray:
    a = read_png(p)[Y0:Y1, X0:X1, :3].astype(np.float32)
    return a[..., 0] * 0.2126 + a[..., 1] * 0.7152 + a[..., 2] * 0.0722


def lap(y: np.ndarray) -> float:
    return float(np.abs(4 * y[1:-1, 1:-1] - y[:-2, 1:-1] - y[2:, 1:-1] - y[1:-1, :-2] - y[1:-1, 2:]).mean())


def analyse(d: Path) -> dict | None:
    still = sorted(d.glob("still_*.png"))
    pan = sorted(d.glob("pan_*.png"))
    if len(still) < 3:
        return None
    s = np.stack([luma(p) for p in still])
    out = {"flicker": round(float(np.median(s.std(0))), 4), "sharp": round(lap(s.mean(0)), 4)}
    if len(pan) >= 2:
        pf = [luma(p) for p in pan]
        out["pan_detail"] = round(float(np.mean([lap(f) for f in pf])), 4)
        out["pan_moved"] = round(float(np.mean([np.abs(pf[i] - pf[i - 1]).mean() for i in range(1, len(pf))])), 3)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--ref", default="vanilla")
    ap.add_argument("--json")
    a = ap.parse_args()
    root = Path(a.root)
    res = {v.name: {s.name: analyse(s) for s in sorted(v.iterdir()) if s.is_dir()} for v in sorted(root.iterdir()) if v.is_dir()}
    ref = res.get(a.ref, {})
    for v, slugs in res.items():
        print(f"== {v}")
        for slug, r in slugs.items():
            if not r:
                continue
            b = ref.get(slug) or {}
            def ratio(k):
                return f"x{r[k] / b[k]:.2f}" if k in r and b.get(k) else "-"
            print(f"   {slug:<18} flicker {r['flicker']:.3f} ({ratio('flicker')})  sharp {r['sharp']:.3f} ({ratio('sharp')})"
                  f"  pan_detail {r.get('pan_detail', 0):.3f} ({ratio('pan_detail')})  pan_moved {r.get('pan_moved', 0):.1f}")
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
