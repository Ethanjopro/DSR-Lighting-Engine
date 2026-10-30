"""Compare capture variants against a reference capture set (same bonfires, same session).

    .venv\\Scripts\\python.exe tools\\ab_report.py --ref captures\\s3\\dp\\vanilla\\pin_van
        [--repeat captures\\s3\\dp\\vanilla\\pin_van_b] --var slb0=captures\\s3\\dp\\vanilla\\pin_slb0 ...
        [--gpu captures\\s3\\gpu.csv] [--sheets OUTDIR] [--json OUT]

Per bonfire and variant: whole-frame stats deltas (from <slug>.stats.json), the character-crop and
environment EV change (mean linear luminance), the share of pixels whose 8-bit luma moved by more than 4
levels, and, with --repeat, the same numbers for the reference repeat (the noise floor). --gpu adds the mean
GPU load (utilisation x graphics clock, nvidia-smi CSV) over the 4 s before each capture was written.
--sheets writes <slug>_<variant>.png: reference | variant | |difference| x8, at half resolution.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shaders" / "tools"))
from probe_decode import read_png, write_png  # noqa: E402  (pure numpy PNG I/O)

CHAR = (slice(880, 1720), slice(1650, 2210))     # y, x of 3840x2160 (looks/README.md section 6)


def lin(rgb8: np.ndarray) -> np.ndarray:
    c = rgb8.astype(np.float32) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def luma_lin(rgb8: np.ndarray) -> np.ndarray:
    l = lin(rgb8[..., :3])
    return l[..., 0] * 0.2126 + l[..., 1] * 0.7152 + l[..., 2] * 0.0722


def ev(a: float, b: float) -> float:
    return float(np.log2(max(b, 1e-6) / max(a, 1e-6)))


def compare(ref_png: Path, var_png: Path) -> dict:
    a, b = read_png(ref_png)[..., :3], read_png(var_png)[..., :3]
    ya, yb = luma_lin(a), luma_lin(b)
    env = np.ones(ya.shape, bool)
    env[CHAR] = False
    env[:, 1600:2300] = False
    la = a.astype(np.int16) @ np.array([54, 183, 19], np.int16) // 256
    lb = b.astype(np.int16) @ np.array([54, 183, 19], np.int16) // 256
    moved = np.abs(lb - la) > 4
    return {
        "char_ev": round(ev(ya[CHAR].mean(), yb[CHAR].mean()), 3),
        "env_ev": round(ev(ya[env].mean(), yb[env].mean()), 3),
        "frame_ev": round(ev(ya.mean(), yb.mean()), 3),
        "moved_pct": round(100.0 * moved.mean(), 2),
        "up_pct": round(100.0 * (moved & (lb > la)).mean(), 2),
    }


def stats_delta(ref_js: Path, var_js: Path) -> dict:
    a, b = json.loads(ref_js.read_text()), json.loads(var_js.read_text())
    return {
        "ev": round(b["ev"] - a["ev"], 3),
        "clip": round(b["clip_pct"] - a["clip_pct"], 2),
        "crush": round(b["crush_pct"] - a["crush_pct"], 2),
        "sat_x": round(b["mean_sat"] / max(a["mean_sat"], 1e-6), 3),
        "da": round(b["mid_oklab_a"] - a["mid_oklab_a"], 4),
        "db": round(b["mid_oklab_b"] - a["mid_oklab_b"], 4),
    }


def load_gpu(path: Path):
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        p = [s.strip() for s in line.split(",")]
        try:
            t = dt.datetime.strptime(p[0], "%Y/%m/%d %H:%M:%S.%f")
            util, power, clk = float(p[1].split()[0]), float(p[2].split()[0]), float(p[3].split()[0])
        except (ValueError, IndexError):
            continue
        rows.append((t.timestamp(), util, power, clk))
    return np.array(rows) if rows else None


def gpu_window(gpu, png: Path, secs: float = 4.0) -> dict | None:
    if gpu is None:
        return None
    t1 = png.stat().st_mtime
    w = gpu[(gpu[:, 0] >= t1 - secs) & (gpu[:, 0] <= t1)]
    if not len(w):
        return None
    return {"load": round(float((w[:, 1] * w[:, 3]).mean() / 1000.0), 1), "util": round(float(w[:, 1].mean()), 1),
            "power_w": round(float(w[:, 2].mean()), 1), "clock": round(float(w[:, 3].mean()))}


def sheet(ref_png: Path, var_png: Path, out: Path) -> None:
    a, b = read_png(ref_png)[::2, ::2, :3], read_png(var_png)[::2, ::2, :3]
    d = np.clip(np.abs(b.astype(np.int16) - a.astype(np.int16)) * 8, 0, 255).astype(np.uint8)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_png(out, np.concatenate([a, b, d], axis=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True)
    ap.add_argument("--repeat")
    ap.add_argument("--var", action="append", default=[], help="name=dir")
    ap.add_argument("--gpu")
    ap.add_argument("--sheets")
    ap.add_argument("--json")
    a = ap.parse_args()
    ref = Path(a.ref)
    variants = [("repeat", Path(a.repeat))] if a.repeat else []
    variants += [(v.split("=", 1)[0], Path(v.split("=", 1)[1])) for v in a.var]
    gpu = load_gpu(Path(a.gpu)) if a.gpu else None
    out = {}
    for png in sorted(ref.glob("*.png")):
        if png.name.endswith(".thumb.png"):
            continue
        slug = png.stem
        row = {"_ref_gpu": gpu_window(gpu, png)}
        for name, d in variants:
            vp = d / png.name
            if not vp.exists():
                row[name] = None
                continue
            r = compare(png, vp)
            r.update({"stats": stats_delta(png.with_suffix(".stats.json"), vp.with_suffix(".stats.json"))})
            r["gpu"] = gpu_window(gpu, vp)
            row[name] = r
            if a.sheets and name != "repeat":
                sheet(png, vp, Path(a.sheets) / f"{slug}_{name}.png")
        out[slug] = row
        print(f"== {slug}  ref gpu {row['_ref_gpu']}")
        for name, _ in variants:
            r = row.get(name)
            if not r:
                print(f"   {name:<12} missing")
                continue
            s, g = r["stats"], r["gpu"] or {}
            print(f"   {name:<12} frame {r['frame_ev']:+.3f}  char {r['char_ev']:+.3f}  env {r['env_ev']:+.3f}  "
                  f"moved {r['moved_pct']:5.2f}% (up {r['up_pct']:5.2f})  clip {s['clip']:+.2f}  crush {s['crush']:+.2f}  "
                  f"sat x{s['sat_x']:.3f}  gpu {g.get('load', '-')}")
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
