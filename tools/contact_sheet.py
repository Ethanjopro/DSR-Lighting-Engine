"""Contact sheets for capture review: one row per bonfire, one column per capture folder (left to right in the order
given), built from the <slug>.thumb.png files (960x540) at half size, with a thin colour bar above each column
(column order: grey, blue, orange, green, ...) so columns can be told apart without text.

    .venv\\Scripts\\python.exe tools\\contact_sheet.py --cols captures\\rebaseline\\frozen_v1\\vanilla captures\\w4r1\\frozen_v1\\r1_main ^
        --out captures\\w4r1\\sheets [--group G1 ...] [--slug firelink ...] [--scale 2]

Writes <out>/<group>.png per group in data/bonfires.json (or one sheet for --slug).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shaders" / "tools"))
from probe_decode import read_png, write_png  # noqa: E402

BARS = [(128, 128, 128), (60, 120, 230), (240, 150, 40), (60, 190, 90), (200, 60, 200), (230, 60, 60)]


def tile(folder: Path, slug: str, step: int) -> np.ndarray:
    p = folder / f"{slug}.thumb.png"
    if not p.exists():
        return np.zeros((540 // step, 960 // step, 3), np.uint8)
    return read_png(p)[::step, ::step, :3]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cols", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--group", nargs="*")
    ap.add_argument("--slug", nargs="*")
    ap.add_argument("--scale", type=int, default=2, help="thumbnail decimation (2 = 480x270 tiles)")
    a = ap.parse_args()
    bonfires = json.loads((ROOT / "data" / "bonfires.json").read_text(encoding="utf-8-sig"))["bonfires"]
    if a.slug:
        groups = {"selection": [b["slug"] for b in bonfires if b["slug"] in a.slug]}
    else:
        groups = {}
        for b in bonfires:
            if not a.group or b["group"] in a.group:
                groups.setdefault(b["group"], []).append(b["slug"])
    cols = [Path(c) for c in a.cols]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for g, slugs in groups.items():
        rows = []
        for slug in slugs:
            tiles = []
            for i, c in enumerate(cols):
                t = tile(c, slug, a.scale)
                bar = np.zeros((6, t.shape[1], 3), np.uint8)
                bar[:] = BARS[i % len(BARS)]
                tiles.append(np.concatenate([bar, t], 0))
                tiles.append(np.full((tiles[-1].shape[0], 4, 3), 255, np.uint8))
            rows.append(np.concatenate(tiles[:-1], 1))
            rows.append(np.full((4, rows[-1].shape[1], 3), 255, np.uint8))
        sheet = np.concatenate(rows[:-1], 0)
        write_png(out / f"{g}.png", sheet)
        print(out / f"{g}.png", sheet.shape[1], "x", sheet.shape[0], slugs)


if __name__ == "__main__":
    main()
