"""Decode filter-census captures (see make_filter_census.py): which tonemap / AA / AO / fx passes run.

    .venv\\Scripts\\python.exe shaders\\tools\\filter_census_decode.py captures\\<run> [--vanilla DIR] [--json OUT]
    .venv\\Scripts\\python.exe shaders\\tools\\filter_census_decode.py --census DIR --archive fcensus_1 [--vanilla DIR]
    .venv\\Scripts\\python.exe shaders\\tools\\filter_census_decode.py --selftest VANILLA.png [VANILLA_OTHER_RUN.png]

Folder layout: captures/<run>/<variant>/<bonfire>.png or captures/<run>/<variant>/<variant>/<bonfire>.png (what
tools/shader_session.ps1 writes). Variant folders are matched by name (fcensus_1 .. fcensus_3, vanilla).
Every census capture is compared with the vanilla capture of the same bonfire (--vanilla, else <run>/vanilla).
PNGs are read with zlib + numpy (8-bit gray/RGB/RGBA, all five row filters). *.thumb.png files are ignored
unless --thumbs is given (then only thumbs are used; the geometry scales).

Cells (fcensus_1, fcensus_3): per cell, the median per-channel log2(census/vanilla) inside the cell minus the
same over a control region (rest of the frame without cells and HUD) -> delta. Its hue part (mean removed) is
compared with the hue part of log2(tint): proj = fraction of the expected cast that is present, cos = direction
match. A second, self-referenced test compares thin strips just inside vs just outside the four cell edges in
the census image (minus the same in vanilla when available), so a cell can be read without a vanilla capture.
Entries whose pass is followed by a non-linear transform (ResolveTAA, before the tonemap) carry atten < 1 in the
layout: their proj threshold is FULL_PROJ x atten. 'weak' = direction-consistent cast below the ACTIVE threshold;
a pass that is only ever ACTIVE+weak / weak is summarised as 'RUNS (attenuated)'.
'partial' cells (bloom, shafts, glow) score each pixel by its log2 change along the tint's gain direction, counted
only when every channel moves the way the tint moves it (an additive change such as drifting mist raises every
channel and scores 0); the upper tail of that score in the cell is compared with the control, and in a band just
inside the cell edges with a band a little outside them (the cast must stop at the cell: ACTIVE needs both).
'luma' cells (SAO_Combine alpha) use brightness only.

Stripes (fcensus_2): L = log2(census/vanilla luminance) is projected onto profiles across vertical stripes (pixel
SAO), horizontal (compute SAO), x+y diagonal (compute SSAO) and the anti-diagonal (control), separately in
STRIPE_BLOCKS bands of the frame. At each candidate period (pixel SAO 240 px; compute 2<<STRIPE_SHIFT px x s for
s in AO_SCALES screen px per AO texel) every band's profile is high-passed (moving average over 2 periods) and its
complex Fourier coefficient taken; amplitude = |mean over bands|, SNR = amplitude / standard error over bands.
Real stripes have the same phase in every band; scene changes and slow drift do not. All passing scales of a
compute path are listed (the hierarchy levels of an upsample chain superpose); the smallest is the scale of the
pass that writes the final AO. A free search over all periods is kept for an 'unexpected period' diagnostic.

Decision thresholds are the constants below; the JSON report keeps every number.
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_filter_census as fc  # noqa: E402  (layout only; no archive I/O at import)

# thresholds
FULL_PROJ, FULL_COS, FULL_NONE = 0.30, 0.85, 0.12        # cast fraction, direction, |hue delta| below = inactive
ATTEN_COS, ATTEN_EDGE_COS = 0.90, 0.80                     # direction match when atten < 1 (cell median / edge strips:
                                                           # thin strips in dark scenes are noisier; proj still gates)
WEAK_FRAC = 0.5                                            # 'weak' = direction-consistent, >= WEAK_FRAC x threshold
FULL_AGREE = 0.5                                           # cell pixels carrying the cast (minus control fraction)
PART_TAIL = 0.12                                           # partial cells: q95 sign-consistent score excess (log2)
PART_BAND, PART_GAP = 6, 3                                 # partial edge test: band width / outer gap, cell-inset units
LUMA_ACT = 0.25                                            # luma cells: |delta log2 luminance|
STRIPE_BLOCKS = 8                                          # independent frame bands for the stripe coherence test
STRIPE_SNR, STRIPE_AMP = 8.0, 0.01                         # at a candidate period: coherent SNR, amplitude (log2)
STRIPE_MIN_CYCLES = 3.0                                    # a band's profile must span this many periods
FREE_SNR, FREE_AMP = 8.0, 0.08                             # free search (diagnostic only)
STRIPE_TOL = 0.04                                          # free peak within this of a candidate = that candidate
STRIPE_LEAK = 2.5                                          # weaker same-period detection in another orientation = leakage
PERIOD_RANGE = (48.0, 1200.0)                              # free search range (capture px at 4K; scales with width)


# ---------------------------------------------------------------------------
# PNG


def read_png(path: str | Path) -> np.ndarray:
    """HxWx3 uint8 (alpha dropped, gray expanded)."""
    b = Path(path).read_bytes()
    if b[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path}: not a PNG")
    pos, idat, hdr = 8, [], None
    while pos < len(b):
        n, t = struct.unpack(">I4s", b[pos:pos + 8])
        d = b[pos + 8:pos + 8 + n]
        pos += 12 + n
        if t == b"IHDR":
            hdr = struct.unpack(">IIBBBBB", d)
        elif t == b"IDAT":
            idat.append(d)
        elif t == b"IEND":
            break
    w, h, depth, ctype, _comp, _filt, interlace = hdr
    if depth != 8 or interlace or ctype not in (0, 2, 4, 6):
        raise ValueError(f"{path}: unsupported PNG (depth {depth}, colour type {ctype}, interlace {interlace})")
    ch = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]
    stride = w * ch
    rows = np.frombuffer(zlib.decompress(b"".join(idat)), np.uint8).reshape(h, stride + 1)
    ftype, data = rows[:, 0], rows[:, 1:]
    if np.any(ftype):
        out = np.empty((h, stride), np.uint8)
        prev = np.zeros(stride, np.uint8)
        for y in range(h):
            f, line = ftype[y], data[y]
            if f == 0:
                cur = line.copy()
            elif f == 1:
                cur = np.cumsum(line.reshape(w, ch), axis=0, dtype=np.uint8).reshape(stride)
            elif f == 2:
                cur = line + prev
            elif f in (3, 4):
                cur = np.zeros(stride, np.int32)
                ln, pv = line.astype(np.int32), prev.astype(np.int32)
                for x in range(0, stride, ch):
                    a = cur[x - ch:x] if x else np.zeros(ch, np.int32)
                    up = pv[x:x + ch]
                    if f == 3:
                        cur[x:x + ch] = (ln[x:x + ch] + ((a + up) >> 1)) & 255
                    else:
                        c = pv[x - ch:x] if x else np.zeros(ch, np.int32)
                        p = a + up - c
                        pa, pb, pc = abs(p - a), abs(p - up), abs(p - c)
                        pred = np.where((pa <= pb) & (pa <= pc), a, np.where(pb <= pc, up, c))
                        cur[x:x + ch] = (ln[x:x + ch] + pred) & 255
                cur = cur.astype(np.uint8)
            else:
                raise ValueError(f"{path}: bad filter {f} in row {y}")
            out[y] = cur
            prev = cur
    else:
        out = data
    img = out.reshape(h, w, ch)
    if ch in (1, 2):
        return np.repeat(img[..., :1], 3, axis=2)
    return np.ascontiguousarray(img[..., :3])


# ---------------------------------------------------------------------------
# geometry


def _px(rect_uv, shape, inset=0):
    h, w = shape[:2]
    x0, y0, x1, y1 = rect_uv
    return (int(round(x0 * w)) + inset, int(round(y0 * h)) + inset, int(round(x1 * w)) - inset, int(round(y1 * h)) - inset)


def hud_mask(shape) -> np.ndarray:
    """True where the pixel is usable (outside the HUD masks of tools/dsr.psm1)."""
    h, w = shape[:2]
    m = np.ones((h, w), bool)
    W4, H4 = fc.CAPTURE_RES
    for x0, y0, x1, y1 in fc.HUD_MASKS:
        m[int(y0 * h / H4):int(np.ceil(y1 * h / H4)), int(x0 * w / W4):int(np.ceil(x1 * w / W4))] = False
    return m


def _log2r(c, v):
    return np.log2((c.astype(np.float32) + 0.5) / (v.astype(np.float32) + 0.5))


def _hue(v):
    v = np.asarray(v, float)
    return v - v.mean(axis=-1, keepdims=True)


# ---------------------------------------------------------------------------
# cells


def _partial_score(L: np.ndarray, ctrl_delta, tint) -> np.ndarray:
    """Per-pixel evidence of a partial (additive-contribution) tint: the control-referenced log2 change projected
    on the small-gain direction (t - 1), counted only when every channel moves the way the tint moves it (gain
    channels up, cut channels down); 0 otherwise. L: N x 3 log2(census/vanilla)."""
    t = np.asarray(tint[:3], float)
    d = L - np.asarray(ctrl_delta, float)
    u = (t - 1) / np.linalg.norm(t - 1)
    ok = np.ones(d.shape[0], bool)
    if np.any(t > 1):
        ok &= np.all(d[:, t > 1] > 0, axis=1)
    if np.any(t < 1):
        ok &= np.all(d[:, t < 1] < 0, axis=1)
    return np.where(ok, d @ u, 0.0)


def _rect_mask(shape, r_px):
    h, w = shape[:2]
    m = np.zeros((h, w), bool)
    x0, y0, x1, y1 = r_px
    m[max(y0, 0):max(min(y1, h), 0), max(x0, 0):max(min(x1, w), 0)] = True
    return m


def _pair_pre(C, V, cells_all, usable) -> dict:
    """Per image pair: the control region (frame minus cells, HUD and borders) and, with a vanilla capture,
    the per-pixel log ratios and control statistics shared by every cell."""
    h, w = C.shape[:2]
    ctrl = usable.copy()
    ctrl[: int(0.04 * h)] = False; ctrl[int(0.84 * h):] = False
    ctrl[:, : int(0.04 * w)] = False; ctrl[:, int(0.96 * w):] = False
    for r in cells_all:
        ctrl &= ~_rect_mask(C.shape, _px(r, C.shape, -int(0.02 * w)))
    pre = {"ctrl": ctrl}
    if V is not None:
        valid = (V >= 6) & (V <= 250)
        L = _log2r(C, V)
        allv = valid.all(axis=2)
        d_ctrl = []
        for c in range(3):
            sr = L[..., c][ctrl & valid[..., c]]
            d_ctrl.append(float(np.median(sr)) if sr.size > 200 else 0.0)
        lum = lambda X: X[..., 0] * 0.2126 + X[..., 1] * 0.7152 + X[..., 2] * 0.0722
        LL = _log2r(lum(C), lum(V))
        lv = lum(V) >= 6
        b = LL[ctrl & lv]
        L_ctrl = L[ctrl & allv]
        pre.update({"valid": valid, "L": L, "allv": allv, "d_ctrl": d_ctrl, "LL": LL, "lv": lv,
                    "luma_ctrl": float(np.median(b)) if b.size > 200 else float("nan"),
                    "L_ctrl": L_ctrl, "hue_ctrl": _hue(L_ctrl) if L_ctrl.size else np.zeros((0, 3))})
    return pre


def _cell_stats(C, V, entry, cells_all, usable, pre=None):
    rect, tint, sig = entry["rect"], entry["tint"], entry.get("signal", "full")
    h, w = C.shape[:2]
    m = max(2, round(0.004 * w))
    x0, y0, x1, y1 = _px(rect, C.shape, m)
    pre = pre or _pair_pre(C, V, cells_all, usable)
    ctrl = pre["ctrl"]
    out = {"cell_px": [x0, y0, x1, y1]}
    if V is not None:
        valid, L, allv, d_ctrl = pre["valid"], pre["L"], pre["allv"], pre["d_ctrl"]
        cell_mask = np.zeros((h, w), bool); cell_mask[y0:y1, x0:x1] = True
        cell_mask &= usable
        d_cell, n_cell = [], []
        for c in range(3):
            sc = L[..., c][cell_mask & valid[..., c]]
            n_cell.append(int(sc.size))
            d_cell.append(float(np.median(sc)) if sc.size > 200 else float("nan"))
        out["delta"] = [a - b for a, b in zip(d_cell, d_ctrl)]
        out["valid_px"] = n_cell
        out["control_delta"] = d_ctrl
        # per-pixel hue (full cells: spatial agreement of the cast)
        hc = _hue(L[cell_mask & allv]) if np.any(cell_mask & allv) else np.zeros((0, 3))
        out["_hue_px"] = (hc, pre["hue_ctrl"])
        a = pre["LL"][cell_mask & pre["lv"]]
        out["delta_luma"] = (float(np.median(a)) - pre["luma_ctrl"]) if a.size > 200 and np.isfinite(pre["luma_ctrl"]) else float("nan")
        if sig == "partial":
            others = np.zeros((h, w), bool)
            for r in cells_all:
                if list(r) != list(rect):
                    others |= _rect_mask(C.shape, _px(r, C.shape, -m))
            inner = _rect_mask(C.shape, _px(rect, C.shape, m)) & ~_rect_mask(C.shape, _px(rect, C.shape, (1 + PART_BAND) * m))
            # the outer band starts PART_GAP insets out, so a cast blurred a little past the edge by later passes
            # (bloom chains blur their intermediate targets) does not land in it
            outer = (_rect_mask(C.shape, _px(rect, C.shape, -(PART_GAP + PART_BAND) * m))
                     & ~_rect_mask(C.shape, _px(rect, C.shape, -PART_GAP * m)))
            outer &= usable & ~others
            inner &= usable
            q = lambda s_: float(np.quantile(s_, 0.95)) if s_.size > 200 else float("nan")
            sc = {k: _partial_score(L[msk & allv], d_ctrl, tint) for k, msk in
                  (("cell", cell_mask), ("inner", inner), ("outer", outer))}
            sc["ctrl"] = _partial_score(pre["L_ctrl"], d_ctrl, tint)
            out["partial"] = {"q95_cell": q(sc["cell"]), "q95_ctrl": q(sc["ctrl"]),
                              "q95_inner_band": q(sc["inner"]), "q95_outer_band": q(sc["outer"]),
                              "px": {k: int(v.size) for k, v in sc.items()}, "rect_px": list(_px(rect, C.shape))}
    # self-referenced edge test
    g, sw = m, 3 * m

    def edges(X):
        X = X.astype(np.float32) + 0.5
        vals = []
        if x0 - g - sw >= 0:
            vals.append(np.median(np.log2(X[y0:y1, x0 + g:x0 + g + sw].mean(1) / X[y0:y1, x0 - g - sw:x0 - g].mean(1)), axis=0))
        if x1 + g + sw <= w:
            vals.append(np.median(np.log2(X[y0:y1, x1 - g - sw:x1 - g].mean(1) / X[y0:y1, x1 + g:x1 + g + sw].mean(1)), axis=0))
        if y0 - g - sw >= 0:
            vals.append(np.median(np.log2(X[y0 + g:y0 + g + sw, x0:x1].mean(0) / X[y0 - g - sw:y0 - g, x0:x1].mean(0)), axis=0))
        if y1 + g + sw <= h:
            vals.append(np.median(np.log2(X[y1 - g - sw:y1 - g, x0:x1].mean(0) / X[y1 + g:y1 + g + sw, x0:x1].mean(0)), axis=0))
        return np.median(np.array(vals), axis=0)

    e = edges(C)
    if V is not None:
        e = e - edges(V)
    out["edge_delta"] = [float(x) for x in e]
    out["edge_referenced_to_vanilla"] = V is not None
    return out


def _thresholds(atten: float, edge: bool = False) -> tuple[float, float]:
    if atten >= 1:
        return FULL_PROJ, FULL_COS
    return FULL_PROJ * atten, (ATTEN_EDGE_COS if edge else ATTEN_COS)


def _judge_full(delta, tint, atten: float = 1.0, edge: bool = False):
    exp = np.log2(np.clip(np.asarray(tint[:3], float), 1e-3, None))
    dh, eh = _hue(delta), _hue(exp)
    if not np.all(np.isfinite(dh)):
        return {"verdict": "no-data"}
    pmin, cmin = _thresholds(atten, edge)
    proj = float(dh @ eh / (eh @ eh))
    nd = float(np.linalg.norm(dh))
    cos = float(dh @ eh / (nd * np.linalg.norm(eh))) if nd > 1e-9 else 0.0
    if proj >= pmin and cos >= cmin:
        v = "ACTIVE"
    elif proj >= WEAK_FRAC * pmin and cos >= cmin:
        v = "weak"
    elif nd < FULL_NONE:
        v = "inactive"
    else:
        v = "unclear"
    return {"verdict": v, "cast_fraction": round(proj, 3), "cos": round(cos, 3), "hue_delta_norm": round(nd, 3),
            "proj_min": round(pmin, 3)}


def _combine(prim: str, edge: str) -> tuple[str, str | None]:
    """Verdict of a full cell from the vanilla comparison and the edge test."""
    s = {prim, edge}
    if s == {"ACTIVE"}:
        return "ACTIVE", None
    if s <= {"ACTIVE", "weak"}:
        return "weak", f"vanilla comparison {prim}, edge test {edge}"
    if "ACTIVE" in s:
        return "unclear", f"vanilla comparison {prim} but edge test {edge}"
    if s == {"inactive"}:
        return "inactive", None
    if "weak" in s and s <= {"weak", "inactive"}:
        return "inactive", f"vanilla comparison {prim}, edge test {edge}"
    return "unclear", f"vanilla comparison {prim}, edge test {edge}"


def judge_cell(st, entry):
    tint, sig, atten = entry["tint"], entry.get("signal", "full"), float(entry.get("atten", 1.0))
    res = {}
    if sig == "luma":
        d = st.get("delta_luma", float("nan"))
        if np.isfinite(d):
            res["primary"] = {"verdict": "ACTIVE" if abs(d) >= LUMA_ACT else "inactive",
                              "delta_luma_log2": round(d, 3), "direction": "darker" if d < 0 else "brighter"}
        ed = np.asarray(st["edge_delta"]) @ np.array([0.2126, 0.7152, 0.0722])
        res["edge"] = {"verdict": "ACTIVE" if abs(ed) >= LUMA_ACT else "inactive", "delta_luma_log2": round(float(ed), 3)}
        prim = res.get("primary", res["edge"])
        res["verdict"] = prim["verdict"]
        if "primary" in res and {prim["verdict"], res["edge"]["verdict"]} == {"ACTIVE", "inactive"}:
            res["note"] = "edge test disagrees"
        return res
    if sig == "partial":
        p = st.get("partial")
        if p is None or not np.isfinite(p["q95_cell"]) or not np.isfinite(p["q95_ctrl"]):
            # no vanilla capture: only the hue edge test is available (informational)
            res["edge"] = _judge_full(st["edge_delta"], tint)
            res["verdict"] = "unclear" if res["edge"]["verdict"] == "ACTIVE" else "inactive"
            res["note"] = "partial cell without a vanilla capture: edge hue test only"
            return res
        tail = p["q95_cell"] - p["q95_ctrl"]
        band = p["q95_inner_band"] - p["q95_outer_band"] if np.isfinite(p["q95_inner_band"]) and np.isfinite(p["q95_outer_band"]) else float("nan")

        def lvl(x):
            return "ACTIVE" if x >= PART_TAIL else ("weak" if x >= WEAK_FRAC * PART_TAIL else "inactive")
        res["primary"] = {"verdict": lvl(tail), "q95_cast_excess_log2": round(tail, 3)}
        res["edge"] = {"verdict": lvl(band) if np.isfinite(band) else "no-data", "q95_band_excess_log2": round(band, 3) if np.isfinite(band) else None}
        pv, ev = res["primary"]["verdict"], res["edge"]["verdict"]
        if pv == "ACTIVE" and ev in ("ACTIVE", "weak"):
            res["verdict"] = "ACTIVE"
        elif pv == "ACTIVE":
            res["verdict"], res["note"] = "unclear", f"cast in the cell but not confined to it (edge band {ev})"
        elif pv == "weak" and ev in ("ACTIVE", "weak"):
            res["verdict"] = "weak"
        else:
            res["verdict"] = "inactive"
        return res
    res["edge"] = _judge_full(st["edge_delta"], tint, atten, edge=True)
    if "delta" in st:
        pr = _judge_full(st["delta"], tint, atten)
        hc, hr = st["_hue_px"]
        eh = _hue(np.log2(np.clip(np.asarray(tint[:3], float), 1e-3, None)))
        if hc.shape[0] > 200 and hr.shape[0] > 200 and pr["verdict"] != "no-data":
            # pixels carrying at least half of the cast actually measured (bounded by the threshold and the full tint),
            # so a uniformly attenuated cast still counts as covering the cell
            thr = 0.5 * min(1.0, max(pr["cast_fraction"], pr["proj_min"]))

            def frac(H):
                proj = H @ eh / (eh @ eh)
                cos = (H @ eh) / (np.linalg.norm(H, axis=1) * np.linalg.norm(eh) + 1e-9)
                return float(np.mean((proj >= thr) & (cos >= 0.8)))
            pr["pixel_agreement"] = round(frac(hc) - frac(hr), 3)
            pr["pixel_threshold"] = round(thr, 3)
            if pr["verdict"] in ("ACTIVE", "weak") and pr["pixel_agreement"] < FULL_AGREE:
                pr["verdict"] = "unclear"
                pr["why"] = "cast not spread over the cell"
        res["primary"] = pr
        res["verdict"], note = _combine(pr["verdict"], res["edge"]["verdict"])
        if note:
            res["note"] = note
    else:
        res["verdict"] = res["edge"]["verdict"]
        res["note"] = "no vanilla capture: edge test only"
    return res


def analyse_cells(C, V, archive):
    spec = fc.CENSUS[archive]
    usable = hud_mask(C.shape)
    rects = [e["rect"] for e in spec["entries"]]
    pre = _pair_pre(C, V, rects, usable)
    out = {}
    for e in spec["entries"]:
        st = _cell_stats(C, V, e, rects, usable, pre)
        j = judge_cell(st, e)
        st.pop("_hue_px", None)
        out[e["label"]] = {"entry": e["entry"], **j, "stats": _round(st)}
    return out


# ---------------------------------------------------------------------------
# stripes


_ORIENT = ("vertical", "horizontal", "diagonal", "antidiagonal")


def _stripe_field(C, V):
    h, w = C.shape[:2]
    f = max(1, w // 960)
    lum = lambda X: X[..., 0] * 0.2126 + X[..., 1] * 0.7152 + X[..., 2] * 0.0722
    lc, lv = lum(C.astype(np.float32)), lum(V.astype(np.float32))
    L = np.log2((lc + 2) / (lv + 2))
    wgt = (hud_mask(C.shape) & (lv >= 4)).astype(np.float32)
    H, W = h // f, w // f
    L, wgt = L[:H * f, :W * f], wgt[:H * f, :W * f]
    sw = wgt.reshape(H, f, W, f).sum((1, 3))
    sl = (L * wgt).reshape(H, f, W, f).sum((1, 3))
    return sl, sw, f


def _band_profiles(sl, sw, orient, nblocks):
    """Per frame band: (absolute bin index of the longest usable run, profile values). Profile coordinate k runs
    across the stripes of this orientation; bands split the transverse coordinate into equal-weight parts."""
    H, W = sw.shape
    ii, jj = np.mgrid[0:H, 0:W]
    k, q = {"vertical": (jj, ii), "horizontal": (ii, jj), "diagonal": (ii + jj, jj - ii),
            "antidiagonal": (jj - ii + H - 1, ii + jj)}[orient]
    n = int(k.max()) + 1
    qv, wv = q.ravel(), sw.ravel()
    order = np.argsort(qv, kind="stable")
    cw = np.cumsum(wv[order])
    edges = [qv[order][min(np.searchsorted(cw, cw[-1] * b / nblocks), len(order) - 1)] for b in range(1, nblocks)]
    band = np.searchsorted(np.asarray(edges), q, side="right")
    out = []
    for b in range(nblocks):
        msk = band == b
        num = np.bincount(k[msk], sl[msk], n)
        den = np.bincount(k[msk], sw[msk], n)
        if den.max() <= 0:
            continue
        idx = np.flatnonzero(den >= 0.25 * den.max())
        if idx.size < 16:
            continue
        runs = np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1)
        run = max(runs, key=len)
        out.append((run, num[run] / den[run]))
    return out


def _highpass(p: np.ndarray, nwin: int) -> np.ndarray:
    if nwin < 3 or nwin * 1.5 > p.size:
        x = np.arange(p.size)
        return p - np.polyval(np.polyfit(x, p, 2), x)
    pad = np.pad(p, (nwin, nwin), mode="reflect")
    c = np.cumsum(np.insert(pad, 0, 0.0))
    ma = (c[nwin:] - c[:-nwin]) / nwin                      # ma[i] = mean(pad[i:i+nwin])
    lo = nwin - nwin // 2
    return p - ma[lo:lo + p.size]


def _coherent_at(bands, step: float, period: float) -> dict:
    """Band-coherent amplitude of a period-`period` (capture px) component."""
    nb = period / step                                       # period in bins
    cs = []
    for run, p in bands:
        if run.size < STRIPE_MIN_CYCLES * nb:
            continue
        hp = _highpass(p, int(round(2 * nb)))
        win = np.hanning(run.size)
        cs.append(2 * np.sum(win * hp * np.exp(-2j * np.pi * run / nb)) / win.sum())
    if len(cs) < 3:
        return {"period_px": round(period, 1), "amplitude_log2": 0.0, "snr": 0.0, "bands": len(cs)}
    cs = np.asarray(cs)
    mean = cs.mean()
    se = np.sqrt(np.sum(np.abs(cs - mean) ** 2) / (len(cs) * (len(cs) - 1)))
    return {"period_px": round(period, 1), "amplitude_log2": round(float(abs(mean)), 4),
            "snr": round(float(abs(mean) / (se + 1e-9)), 2), "bands": len(cs),
            "phase_cycles": round(float(np.angle(mean) / (2 * np.pi)), 3)}


def _free_search(bands, step: float, w: int) -> dict:
    """Diagnostic: band-coherent spectrum over all periods in PERIOD_RANGE (scaled to the capture width)."""
    lo, hi = (x * w / fc.CAPTURE_RES[0] for x in PERIOD_RANGE)
    use = [(r, p) for r, p in bands if r.size >= 16]
    if len(use) < 3:
        return {"snr": 0.0, "period_px": None, "amplitude_log2": 0.0}
    n = int(max(r.max() for r, _ in use)) + 1
    nfft = 1 << int(np.ceil(np.log2(n * 8)))
    freq = np.fft.rfftfreq(nfft, d=step)
    spec = []
    for run, p in use:
        x = np.arange(p.size)
        hp = p - np.polyval(np.polyfit(x, p, 2), x)
        win = np.hanning(run.size)
        a = np.zeros(n); a[run] = win * hp
        spec.append(2 * np.fft.rfft(a, nfft) / win.sum())
    S = np.asarray(spec)
    mean = S.mean(0)
    se = np.sqrt(np.sum(np.abs(S - mean) ** 2, 0) / (len(S) * (len(S) - 1))) + 1e-9
    snr, amp = np.abs(mean) / se, np.abs(mean)
    with np.errstate(divide="ignore"):
        per = np.where(freq > 0, 1.0 / np.maximum(freq, 1e-12), np.inf)
    shortest = min(r.size for r, _ in use) * step
    sel = np.flatnonzero((per >= lo) & (per <= min(hi, shortest / STRIPE_MIN_CYCLES)))
    if sel.size < 8:
        return {"snr": 0.0, "period_px": None, "amplitude_log2": 0.0}
    k = sel[np.argmax(snr[sel] * (amp[sel] >= FREE_AMP))] if np.any(amp[sel] >= FREE_AMP) else sel[np.argmax(snr[sel])]
    interior = sel[0] < k < sel[-1]
    return {"snr": round(float(snr[k]), 2), "period_px": round(float(per[k]), 1), "amplitude_log2": round(float(amp[k]), 4),
            "interior_peak": bool(interior),
            "detected": bool(interior and snr[k] >= FREE_SNR and amp[k] >= FREE_AMP)}


def _detected(r: dict) -> bool:
    return r["snr"] >= STRIPE_SNR and r["amplitude_log2"] >= STRIPE_AMP


def analyse_stripes(C, V):
    if V is None:
        raise ValueError("stripe decode needs a vanilla capture")
    h, w = C.shape[:2]
    sl, sw, f = _stripe_field(C, V)
    sx = w / fc.CAPTURE_RES[0]
    pix_p = fc.AO_PATHS["pixel_sao"]["screen_period_px"] * sx
    cand = {s: fc.AO_PATHS["compute_sao"]["ao_period_px"] * s * sx for s in fc.AO_SCALES}
    prof = {o: _band_profiles(sl, sw, o, STRIPE_BLOCKS) for o in _ORIENT}
    at = {o: {"pixel": _coherent_at(prof[o], f, pix_p), **{s: _coherent_at(prof[o], f, P) for s, P in cand.items()}}
          for o in _ORIENT}
    free = {o: _free_search(prof[o], f, w) for o in _ORIENT}
    # leakage: a strong pattern can leave a weak same-period trace in another projection
    det = {(o, key): r for o in _ORIENT for key, r in at[o].items() if _detected(r)}
    leak = {}
    for (o, key), r in det.items():
        for (o2, key2), r2 in det.items():
            if o2 != o and abs(r2["period_px"] / r["period_px"] - 1) < STRIPE_TOL and r2["amplitude_log2"] >= STRIPE_LEAK * r["amplitude_log2"]:
                leak[(o, key)] = o2
    out = {}

    def levels(o):
        return [s for s in fc.AO_SCALES if (o, s) in det and (o, s) not in leak]

    # pixel SAO: vertical stripes at 240 px (4K); vertical stripes at a compute period = transposed compute buffer
    rv = dict(at["vertical"]["pixel"])
    if ("vertical", "pixel") in det and ("vertical", "pixel") not in leak:
        rv["verdict"] = "ACTIVE"
    elif levels("vertical"):
        rv["verdict"] = "stripes, unexpected period"
        rv["note"] = (f"vertical stripes at {fc.AO_PATHS['compute_sao']['ao_period_px']} px x {levels('vertical')} "
                      "(transposed compute buffer?)")
    elif free["vertical"].get("detected"):
        rv["verdict"] = "stripes, unexpected period"
    else:
        rv["verdict"] = f"inactive (leakage from {leak[('vertical', 'pixel')]})" if ("vertical", "pixel") in leak else "inactive"
    rv["free_search"] = free["vertical"]
    out["pixel_sao"] = rv
    for path, o in (("compute_sao", "horizontal"), ("compute_ssao", "diagonal")):
        lv = levels(o)
        best = max(at[o].items(), key=lambda kv: kv[1]["snr"] if kv[0] != "pixel" else -1)
        r = {"per_scale": {str(s): at[o][s] for s in fc.AO_SCALES}, "free_search": free[o]}
        if lv:
            top = at[o][lv[0]]
            r.update({"verdict": "ACTIVE", "levels_detected": lv, "screen_px_per_ao_texel": lv[0],
                      "snr": top["snr"], "amplitude_log2": top["amplitude_log2"], "period_px": top["period_px"]})
            if len(lv) > 1:
                r["note"] = (f"stripes at {len(lv)} hierarchy levels (upsample chain); the smallest scale ({lv[0]}) is "
                             f"the pass that writes the final AO, the largest ({lv[-1]}) the coarsest level")
        else:
            lk = [leak[(o, s)] for s in fc.AO_SCALES if (o, s) in leak]
            r.update({"verdict": f"inactive (leakage from {lk[0]})" if lk else
                      ("stripes, unexpected period" if free[o].get("detected") else "inactive"),
                      "snr": best[1]["snr"], "amplitude_log2": best[1]["amplitude_log2"], "period_px": best[1]["period_px"]})
        out[path] = r
    ctrl = {str(k): v for k, v in at["antidiagonal"].items()}
    out["_control_antidiagonal"] = {"per_candidate": ctrl, "free_search": free["antidiagonal"],
                                    "snr": max(v["snr"] for v in ctrl.values()),
                                    "amplitude_log2": max(v["amplitude_log2"] for v in ctrl.values()),
                                    "period_px": None}
    if any(_detected(v) for v in ctrl.values()) or free["antidiagonal"].get("detected"):
        out["_note"] = "anti-diagonal control also shows a periodic signal: treat stripe detections with care"
    return out


# ---------------------------------------------------------------------------
# aggregation / IO


def _round(o):
    if isinstance(o, float):
        return round(o, 4)
    if isinstance(o, (list, tuple)):
        return [_round(x) for x in o]
    if isinstance(o, dict):
        return {k: _round(v) for k, v in o.items()}
    return o


def _png_dir(d: Path) -> Path:
    inner = d / d.name
    return inner if inner.is_dir() and any(inner.glob("*.png")) else d


def _pngs(d: Path, thumbs: bool) -> dict[str, Path]:
    out = {}
    for p in sorted(d.glob("*.png")):
        is_thumb = p.name.endswith(".thumb.png")
        if is_thumb == thumbs:
            out[p.name[:-len(".thumb.png")] if is_thumb else p.stem] = p
    return out


def decode_archive(archive: str, census_dir: Path, vanilla_dir: Path | None, bonfires, thumbs) -> dict:
    spec = fc.CENSUS[archive]
    cens = _pngs(census_dir, thumbs)
    van = _pngs(vanilla_dir, thumbs) if vanilla_dir else {}
    per = {}
    for slug, p in cens.items():
        if bonfires and slug not in bonfires:
            continue
        C = read_png(p)
        V = read_png(van[slug]) if slug in van else None
        if V is not None and V.shape != C.shape:
            per[slug] = {"error": f"size mismatch {C.shape} vs vanilla {V.shape}"}
            continue
        try:
            if spec["decode"] == "cells":
                per[slug] = {"vanilla": str(van.get(slug)) if V is not None else None, "cells": analyse_cells(C, V, archive)}
            else:
                per[slug] = {"vanilla": str(van[slug]) if V is not None else None, "paths": analyse_stripes(C, V)}
        except ValueError as ex:
            per[slug] = {"error": str(ex)}
    return {"archive": archive, "title": spec["title"], "census_dir": str(census_dir),
            "vanilla_dir": str(vanilla_dir) if vanilla_dir else None, "bonfires": per, "summary": summarise(archive, per)}


def _pass_verdict(vs: dict) -> str:
    act = sum(v == "ACTIVE" for v in vs.values())
    weak = sum(v == "weak" for v in vs.values())
    ina = sum(v == "inactive" for v in vs.values())
    if act and act >= ina:
        return "RUNS"
    if act + weak and act + weak >= ina:
        return "RUNS (attenuated)"
    if ina and not act and not weak:
        return "not seen"
    if act or weak:
        return "mixed"
    return "unclear"


def summarise(archive: str, per: dict) -> dict:
    spec = fc.CENSUS[archive]
    keys = [e["label"] for e in spec["entries"]] if spec["decode"] == "cells" else list(fc.AO_PATHS)
    tally = {}
    for k in keys:
        vs = {}
        for slug, r in per.items():
            item = (r.get("cells") or r.get("paths") or {}).get(k)
            if item:
                vs[slug] = item["verdict"]
        tally[k] = {"verdict": _pass_verdict(vs), "per_bonfire": vs}
    ans = {}
    runs = [k if v["verdict"] == "RUNS" else f"{k} (attenuated)" for k, v in tally.items()
            if v["verdict"] in ("RUNS", "RUNS (attenuated)")]
    one = lambda xs: xs[0] if len(xs) == 1 else (xs or "none detected")
    if archive == "fcensus_1":
        ans["a_tonemap"] = one([k for k in runs if k.startswith("HDR")])
        ans["b_final_aa"] = one([k for k in runs if not k.startswith("HDR")])
    elif archive == "fcensus_2":
        ans["c_ao_path"] = one(runs)
        scales = {s: r["paths"][k].get("screen_px_per_ao_texel") for k in runs for s, r in per.items()
                  if r.get("paths") and r["paths"][k].get("screen_px_per_ao_texel")}
        if scales:
            ans["screen_px_per_ao_texel"] = scales      # finest detected level = the pass writing the final AO
        lv = {s: r["paths"][k].get("levels_detected") for k in runs for s, r in per.items()
              if r.get("paths") and len(r["paths"][k].get("levels_detected") or []) > 1}
        if lv:
            ans["ao_hierarchy_levels"] = lv
    else:
        ans["fx_passes_seen"] = runs or "none detected"
    ans["scope"] = "under the graphics settings of this capture run (DarkSouls.ini); 'none'/'not seen' = not selected there / entry name unused"
    return {"answer": ans, "by_pass": tally}


def print_report(rep: dict) -> None:
    print(f"== {rep['archive']}: {rep['title']}")
    print(f"   census {rep['census_dir']}  vanilla {rep['vanilla_dir']}")
    for slug, r in rep["bonfires"].items():
        if "error" in r:
            print(f"   {slug:<18} ERROR {r['error']}")
            continue
        if "cells" in r:
            parts = []
            for k, c in r["cells"].items():
                pr = c.get("primary", c.get("edge", {}))
                num = pr.get("cast_fraction", pr.get("q95_cast_excess_log2", pr.get("delta_luma_log2", "")))
                parts.append(f"{k}={c['verdict']}({num})")
            print(f"   {slug:<18} " + "  ".join(parts))
        else:
            parts = []
            for k, v in r["paths"].items():
                if k.startswith("_"):
                    continue
                lv = f", levels {v['levels_detected']}" if v.get("levels_detected") else ""
                parts.append(f"{k}={v['verdict']}(snr {v['snr']}, P {v['period_px']}, A {v['amplitude_log2']}{lv})")
            print(f"   {slug:<18} " + "  ".join(parts))
            if r["paths"].get("_note"):
                print(f"   {'':<18} note: {r['paths']['_note']}")
    s = rep["summary"]
    for k, v in s["by_pass"].items():
        print(f"   -> {k:<16} {v['verdict']:<18} {v['per_bonfire']}")
    print(f"   ANSWER: {json.dumps(s['answer'])}")


def discover(run: Path) -> dict[str, Path]:
    out = {}
    for d in sorted(p for p in run.iterdir() if p.is_dir()):
        if d.name in fc.CENSUS or d.name == "vanilla":
            out[d.name] = _png_dir(d)
    return out


# ---------------------------------------------------------------------------
# self test on synthetic captures


def _lum8(V):
    return (V[..., 0] * 0.2126 + V[..., 1] * 0.7152 + V[..., 2] * 0.0722) / 255.0


def _box_blur(m: np.ndarray, r: int) -> np.ndarray:
    for ax in (0, 1):
        c = np.cumsum(np.pad(m, [(r + 1, r) if a == ax else (0, 0) for a in range(2)], mode="edge"), axis=ax)
        m = (np.take(c, np.arange(2 * r + 1, c.shape[ax]), axis=ax) - np.take(c, np.arange(0, c.shape[ax] - 2 * r - 1), axis=ax)) / (2 * r + 1)
    return m


def _synth_cells(V, archive, active, rng, gain=1.0, shift=0, power=None, partial="uniform"):
    """power: {label: exponent or 'shoulder:<e>'}: the cast reaches the capture as tint**e (a pass before the
    tonemap; 'shoulder' also fades the cast towards white like a tonemap shoulder). partial: 'uniform' (15% of the
    cell's light), 'bright' (contribution grows with brightness, as bloom does) or 'bright_blur' (as 'bright', with
    the gated contribution blurred ~40 px past the cell edges, as a later blur pass would)."""
    C = V.astype(np.float32) * gain
    lum = _lum8(V)
    for e in fc.CENSUS[archive]["entries"]:
        if e["label"] not in active:
            continue
        x0, y0, x1, y1 = _px(e["rect"], V.shape)
        t = np.asarray(e["tint"][:3], np.float32)
        if e.get("signal") == "luma":
            C[y0:y1, x0:x1] *= 0.15
        elif e.get("signal") == "partial" and partial == "bright_blur":
            g = np.zeros(V.shape[:2], np.float32); g[y0:y1, x0:x1] = 1
            g = _box_blur(g, max(1, round(0.0105 * V.shape[1])))
            C *= 1 + (0.6 * lum ** 2 * g)[..., None] * (t - 1)
        elif e.get("signal") == "partial":
            a = 0.15 if partial == "uniform" else 0.6 * lum[y0:y1, x0:x1, None] ** 2
            C[y0:y1, x0:x1] *= 1 + a * (t - 1)
        else:
            p = (power or {}).get(e["label"], 1.0)
            if isinstance(p, str):
                e0 = float(p.split(":")[1])
                ex = e0 * (1 - lum[y0:y1, x0:x1, None] ** 3)
            else:
                ex = p
            C[y0:y1, x0:x1] *= t ** ex
    C = np.roll(C, shift, axis=1) + rng.normal(0, 1.5, C.shape)
    return np.clip(np.round(C), 0, 255).astype(np.uint8)


def _synth_mist(V, rect, rng, add=45.0, frac=0.5):
    """Additive grey haze over the left `frac` of a cell and the gap to its left (what drifting mist does)."""
    C = V.astype(np.float32)
    x0, y0, x1, y1 = _px(rect, V.shape)
    xm = x0 + int(frac * (x1 - x0))
    C[y0:y1, max(0, x0 - int(0.015 * V.shape[1])):xm] += add
    C += rng.normal(0, 1.5, C.shape)
    return np.clip(np.round(C), 0, 255).astype(np.uint8)


def _stripe_mask(path, shape, scale):
    h, w = shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    if path == "pixel_sao":
        return ((xx / w * fc.PIXEL_CYCLES) % 1.0) < 0.5
    if path == "compute_sao":
        return ((yy // scale) >> fc.STRIPE_SHIFT) & 1 == 1
    return (((xx // scale) + (yy // scale)) >> fc.STRIPE_SHIFT) & 1 == 1


def _synth_stripes(V, active, rng, depth=0.85, scale=2, ambient=False, chain=None):
    """active paths get AO = 0 on their stripes; the pixel darkens by `depth` (x an ambient-share map if
    `ambient`: darker pixels are more ambient-lit). chain: list of SSAO level scales whose stripes superpose."""
    h, w = V.shape[:2]
    vis = np.ones((h, w), np.float32)
    for path in active:
        for s in (chain if (chain and path == "compute_ssao") else [scale]):
            vis *= np.where(_stripe_mask(path, V.shape, s), 0.0, 1.0)
    d = depth * ((0.25 + 0.75 * (1 - _lum8(V))) if ambient else 1.0)
    C = V.astype(np.float32) * (1 - d * (1 - vis))[..., None] + rng.normal(0, 1.5, V.shape)
    return np.clip(np.round(C), 0, 255).astype(np.uint8)


def _active(res, also_weak=False):
    keep = ("ACTIVE", "weak") if also_weak else ("ACTIVE",)
    return sorted(k for k, r in res.items() if not k.startswith("_") and r["verdict"] in keep)


def selftest(v1: Path, v2: Path | None) -> int:
    rng = np.random.default_rng(1)
    V = read_png(v1)
    fails = []

    def check(name, got, want):
        ok = got == want
        print(f"   {'ok  ' if ok else 'FAIL'} {name}: got {got} want {want}")
        if not ok:
            fails.append(name)

    print("== selftest cells (fcensus_1)")
    for act in (["HDR_PBL", "FxAA2"], ["HDR_ColAdj", "ResolveTAA"], []):
        C = _synth_cells(V, "fcensus_1", act, rng, gain=1.06, shift=1, power={"ResolveTAA": 0.45})
        check(f"vs vanilla {act}", _active(analyse_cells(C, V, "fcensus_1")), sorted(act))
        # edge-only (no vanilla capture) is a fallback for full-strength casts; the compressed ResolveTAA cast
        # needs the vanilla reference (scene colour edges at the cell border are of the same size)
        C = _synth_cells(V, "fcensus_1", act, rng, gain=1.06, shift=1)
        check(f"edge only {act} (full-strength casts)", _active(analyse_cells(C, None, "fcensus_1")), sorted(act))
    # ResolveTAA writes HDR before the tonemap: its cast reaches the capture compressed
    for p in (1.0, 0.6, 0.45, 0.35, 0.25, "shoulder:0.45", "shoulder:0.3"):
        C = _synth_cells(V, "fcensus_1", ["ResolveTAA"], rng, gain=0.95, shift=1, power={"ResolveTAA": p})
        r = analyse_cells(C, V, "fcensus_1")
        check(f"ResolveTAA cast exponent {p}", _active(r), ["ResolveTAA"])
        print("      ", {k: r["ResolveTAA"]["primary"].get(k) for k in ("cast_fraction", "cos", "pixel_agreement")},
              "edge", r["ResolveTAA"]["edge"].get("cast_fraction"))
    # a faint, direction-consistent cast below the threshold must not be called inactive (nor ACTIVE)
    C = _synth_cells(V, "fcensus_1", ["ResolveTAA"], rng, power={"ResolveTAA": 0.09})
    r = analyse_cells(C, V, "fcensus_1")
    check("ResolveTAA very faint cast (exponent 0.09) -> weak or unclear",
          r["ResolveTAA"]["verdict"] in ("weak", "unclear"), True)
    print("      ", r["ResolveTAA"]["verdict"], r["ResolveTAA"].get("note"))
    check("summary of a weak-only pass", _pass_verdict({"a": "weak", "b": "weak", "c": "inactive"}), "RUNS (attenuated)")
    # a tonemap writing linear values into an sRGB target would show a gamma-compressed cast too
    C = _synth_cells(V, "fcensus_1", ["HDR"], rng, power={"HDR": 0.45})
    check("HDR cast through an sRGB target (exponent 0.45)", _active(analyse_cells(C, V, "fcensus_1")), ["HDR"])
    print("== selftest cells (fcensus_3)")
    for act, mode in ((["Bloom", "SAO_Combine"], "uniform"), (["Sfx_Glow_Blur", "LightShaft"], "uniform"),
                      (["Bloom", "Sfx_Glow_Blur"], "bright"), (["Bloom"], "bright_blur"), ([], "uniform")):
        C = _synth_cells(V, "fcensus_3", act, rng, gain=0.97, partial=mode)
        r = analyse_cells(C, V, "fcensus_3")
        check(f"vs vanilla {act} ({mode})", _active(r), sorted(act))
        print("      ", {k: (r[k]["primary"].get("q95_cast_excess_log2", r[k]["primary"].get("delta_luma_log2")),
                          r[k]["edge"].get("q95_band_excess_log2")) for k in r})
    for e in fc.CENSUS["fcensus_3"]["entries"]:
        C = _synth_mist(V, e["rect"], rng, frac=0.5 if e.get("signal") == "partial" else 0.4)
        check(f"additive mist over {'half' if e.get('signal') == 'partial' else '40%'} of {e['label']} is not a cast",
              _active(analyse_cells(C, V, "fcensus_3"), True), [])
    print("== selftest stripes (fcensus_2)")
    cases = [(["compute_ssao"], dict(scale=2)), (["pixel_sao"], dict(scale=1)), (["compute_sao"], dict(scale=1)),
             (["pixel_sao", "compute_sao"], dict(scale=2)), ([], dict(scale=1))]
    for path, sc in (("pixel_sao", 1), ("compute_sao", 1), ("compute_ssao", 2)):        # shallow: AO only dims ambient
        for dep in (0.15, 0.10, 0.08):
            cases.append(([path], dict(scale=sc, depth=dep)))
        cases.append(([path], dict(scale=sc, depth=0.15, ambient=True)))
    cases += [(["compute_ssao"], dict(chain=[2, 4, 8])), (["compute_ssao"], dict(chain=[1, 2, 4, 8])),
              (["compute_ssao"], dict(chain=[2, 4, 8, 16], depth=0.3))]
    for act, kw in cases:
        C = _synth_stripes(V, act, rng, **kw)
        res = analyse_stripes(C, V)
        check(f"stripes {act} {kw}", _active(res), sorted(act))
        print("      ", {k: (r["snr"], r["amplitude_log2"], r["period_px"], r.get("levels_detected"))
                         for k, r in res.items() if not k.startswith("_")})
        for k in act:
            if k != "pixel_sao":
                want = min(kw["chain"]) if kw.get("chain") and k == "compute_ssao" else kw["scale"]
                check(f"  final AO scale of {k}", res[k].get("screen_px_per_ao_texel"), want)
                if kw.get("chain") and k == "compute_ssao":
                    print(f"        chain {kw['chain']}: levels detected {res[k].get('levels_detected')}; per scale (snr, amp):",
                          {s: (v["snr"], v["amplitude_log2"]) for s, v in res[k]["per_scale"].items()})
    if v2:
        print("== null test: two vanilla captures of the same view from different runs")
        V2 = read_png(v2)
        r1 = analyse_cells(V2, V, "fcensus_1")
        check("cells fcensus_1 null", _active(r1, True), [])
        r3 = analyse_cells(V2, V, "fcensus_3")
        check("cells fcensus_3 null", _active(r3, True), [])
        r2 = analyse_stripes(V2, V)
        check("stripes null", _active(r2, True), [])
        check("stripes null: no unexpected-period hit",
              sorted(k for k, r in r2.items() if not k.startswith("_") and r["verdict"].startswith("stripes")), [])
        print("   null fcensus_1 (cast_fraction, cos):",
              {k: (r.get("primary", {}).get("cast_fraction"), r.get("primary", {}).get("cos")) for k, r in r1.items()})
        print("   null fcensus_3:", {k: (r["primary"].get("q95_cast_excess_log2", r["primary"].get("delta_luma_log2")),
                                       r["edge"].get("q95_band_excess_log2")) for k, r in r3.items()})
        print("   null stripes (snr, amplitude, period):",
              {k: (r["snr"], r["amplitude_log2"], r["period_px"]) for k, r in r2.items() if not k.startswith("_note")})
        print("   null stripes free search:", {k: r["free_search"] for k, r in r2.items() if "free_search" in r})
    print(f"selftest: {'PASS' if not fails else 'FAIL ' + ', '.join(fails)}")
    return 1 if fails else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run", nargs="?", help="captures/<run> holding fcensus_<n> and vanilla folders")
    p.add_argument("--census", help="one census capture folder (with --archive)")
    p.add_argument("--archive", choices=sorted(fc.CENSUS))
    p.add_argument("--vanilla", help="vanilla capture folder (default <run>/vanilla)")
    p.add_argument("--bonfires", help="comma list of bonfire slugs")
    p.add_argument("--thumbs", action="store_true", help="decode the 960px *.thumb.png files instead")
    p.add_argument("--json", help="write the full report here")
    p.add_argument("--selftest", nargs="+", metavar="PNG", help="synthetic test on a vanilla capture (+ optional 2nd run)")
    a = p.parse_args(argv)
    if a.selftest:
        return selftest(Path(a.selftest[0]), Path(a.selftest[1]) if len(a.selftest) > 1 else None)
    bonfires = set(a.bonfires.split(",")) if a.bonfires else None
    jobs = []
    if a.census:
        if not a.archive:
            name = Path(a.census).name
            if name not in fc.CENSUS:
                p.error("--archive is required when the folder name is not fcensus_<n>")
            a.archive = name
        van = Path(a.vanilla) if a.vanilla else None
        jobs.append((a.archive, _png_dir(Path(a.census)), _png_dir(van) if van else None))
    elif a.run:
        found = discover(Path(a.run))
        van = _png_dir(Path(a.vanilla)) if a.vanilla else found.get("vanilla")
        for name in fc.CENSUS:
            if name in found:
                jobs.append((name, found[name], van))
        if not jobs:
            p.error(f"no fcensus_<n> folders under {a.run}")
    else:
        p.error("give a run folder, --census DIR or --selftest")
    reps = [decode_archive(n, c, v, bonfires, a.thumbs) for n, c, v in jobs]
    for r in reps:
        print_report(r)
    if a.json:
        Path(a.json).write_text(json.dumps(_round(reps), indent=1), encoding="utf-8")
        print(f"report: {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
