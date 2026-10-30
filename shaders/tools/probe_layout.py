"""Screen layout and bit encoding shared by make_probe.py (shader generator) and probe_decode.py.

The probe pixel shader paints a grid of *blocks* in render-target pixels. Each block shows one
constant register (4 x 32 bits) as black/white cells, so the value survives any monotonic post
chain (exposure, tone map, colour grading, mild blur) and decodes bit-exactly.

Block = BLOCK_COLS x BLOCK_ROWS cells, each CELL x CELL pixels:
  row 0      : cells 0..3 = W,B,W,B reference; cells 4..15 = 12-bit block code (MSB first);
               cell 16 = parity (popcount & 1) of the 12 code bits
  rows 1..8  : component k = (row-1) >> 1; row (1+2k) holds bits 31..16, row (2+2k) bits 15..0,
               MSB in cell 0; cell 16 = parity of that row's 16 bits
White = bit 1. Pixels outside used blocks are black.

Block code = space << 9 | reg  (12 bits)
  space 0     : $Globals (cb0) register reg
  space 2..5  : cbuffer slot b2..b5 register reg (unbound slots read as zero)
  space 7     : grey ramp, reg 0 = linear, reg 1 = logarithmic (calibration; not bits)

Nothing in here is game specific except the default resolution and the HUD masks read from
tools/dsr.psm1.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]

BLOCK_COLS = 17
BLOCK_ROWS = 9
SPACE_CB0 = 0
SPACE_RAMP = 7
RAMP_CELLS = (BLOCK_ROWS - 1) * BLOCK_COLS  # 136 grey patches per ramp block
EMPTY = 0xFFFF


def hud_masks(path: Path = ROOT / "tools" / "dsr.psm1") -> list[tuple[int, int, int, int]]:
    """HUD rectangles (x0, y0, x1, y1) exactly as the capture harness masks them."""
    txt = path.read_text(encoding="utf-8")
    m = re.search(r"\$script:HudMasks\s*=\s*\[int\[\]\]@\(([^)]*)\)", txt)
    if not m:
        raise SystemExit(f"HudMasks not found in {path}")
    v = [int(x) for x in m.group(1).split(",")]
    return [tuple(v[i:i + 4]) for i in range(0, len(v), 4)]


def ramp_level(kind: int, idx: int) -> float:
    """Relative HDR level (multiplied by the white level) of ramp patch idx (0..135)."""
    if kind == 0:
        return idx / (RAMP_CELLS - 1)
    return 2.0 ** ((idx - 108) / 12.0)


def code_of(space: int, reg: int) -> int:
    assert 0 <= space < 8 and 0 <= reg < 512
    return space << 9 | reg


def make_layout(res=(3840, 2160), cell=10, margin=10, primaries=None, duplicates=None,
                masks=None) -> dict:
    """Assign block codes to grid slots.

    primaries are placed in raster order from the top-left, duplicates in reverse raster order
    from the bottom-right, so the two copies of a duplicated register sit far apart vertically
    (one survives if sky or a large non-probe object covers the other)."""
    masks = hud_masks() if masks is None else masks
    bw, bh = BLOCK_COLS * cell, BLOCK_ROWS * cell
    cols, rows = res[0] // bw, res[1] // bh
    ox, oy = (res[0] - cols * bw) // 2, (res[1] - rows * bh) // 2

    def free(c, r):
        x0, y0 = ox + c * bw, oy + r * bh
        x1, y1 = x0 + bw, y0 + bh
        for mx0, my0, mx1, my1 in masks:
            if x0 < mx1 + margin and x1 > mx0 - margin and y0 < my1 + margin and y1 > my0 - margin:
                return False
        return True

    order = [(c, r) for r in range(rows) for c in range(cols) if free(c, r)]
    slots = [EMPTY] * (cols * rows)
    placed, dropped = [], []
    fwd = list(order)
    for code in primaries:
        if not fwd:
            dropped.append(code); continue
        c, r = fwd.pop(0)
        slots[r * cols + c] = code
        placed.append({"code": code, "col": c, "row": r, "copy": 0})
    for code in duplicates or []:
        if not fwd:
            dropped.append(code); continue
        c, r = fwd.pop()
        slots[r * cols + c] = code
        placed.append({"code": code, "col": c, "row": r, "copy": 1})
    lay = {
        "res": list(res), "cell": cell, "block_cols": BLOCK_COLS, "block_rows": BLOCK_ROWS,
        "origin": [ox, oy], "grid_cols": cols, "grid_rows": rows, "hud_masks": [list(m) for m in masks],
        "mask_margin": margin, "free_slots": len(order), "slots": slots, "blocks": placed,
        "dropped": dropped, "unused_free_slots": len(fwd),
    }
    lay["layout_id"] = hashlib.sha1(json.dumps(lay, sort_keys=True).encode()).hexdigest()[:12]
    return lay


def describe(code: int) -> str:
    space, reg = code >> 9, code & 511
    if space == SPACE_CB0:
        return f"c{reg}"
    if space == SPACE_RAMP:
        return ["ramp_lin", "ramp_log"][reg]
    return f"b{space}[{reg}]"


# ---------------------------------------------------------------------------
# reference encoder (mirrors the HLSL in make_probe.py; used for the synthetic self-test)


def cell_value(code: int, cx: int, cy: int, regs: dict) -> float:
    """Relative level (0/1 for bit cells, ramp level for ramp cells) of cell (cx, cy) of a block.
    regs maps code -> 4-tuple of uint32."""
    if cy == 0:
        if cx < 4:
            return 0.0 if cx & 1 else 1.0
        if cx < 16:
            return float(code >> (15 - cx) & 1)
        return float(bin(code & 0xFFF).count("1") & 1)
    space, reg = code >> 9, code & 511
    if space == SPACE_RAMP:
        return ramp_level(reg, (cy - 1) * BLOCK_COLS + cx)
    w = regs.get(code, (0, 0, 0, 0))[(cy - 1) >> 1]
    h16 = (w & 0xFFFF) if (cy - 1) & 1 else (w >> 16)
    if cx < 16:
        return float(h16 >> (15 - cx) & 1)
    return float(bin(h16).count("1") & 1)


def render(lay: dict, regs: dict) -> np.ndarray:
    """Relative-level image (H, W) float32 of the probe pattern."""
    W, H = lay["res"]
    s = lay["cell"]
    img = np.zeros((H, W), np.float32)
    ox, oy = lay["origin"]
    for b in lay["blocks"]:
        x0 = ox + b["col"] * BLOCK_COLS * s
        y0 = oy + b["row"] * BLOCK_ROWS * s
        for cy in range(BLOCK_ROWS):
            for cx in range(BLOCK_COLS):
                img[y0 + cy * s:y0 + (cy + 1) * s, x0 + cx * s:x0 + (cx + 1) * s] = cell_value(b["code"], cx, cy, regs)
    return img
