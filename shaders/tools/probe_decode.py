"""Decode cbuffer-probe screenshots (see make_probe.py / probe_layout.py) and diff captures.

    python shaders/tools/probe_decode.py decode IMG.png [IMG2.png ...] [--json out.json]
    python shaders/tools/probe_decode.py diff --ref A.png [--repeat A2.png] B.png [C.png ...]
    python shaders/tools/probe_decode.py session captures/probe1/probe_reg [--json out.json]
    python shaders/tools/probe_decode.py names captures/probe1
    python shaders/tools/probe_decode.py selftest [--warp]

Common option: --layout dist/_probe/layout.json (default), --names dist/_probe/reg_names.json.

PNG reading is pure numpy + zlib (PIL is not installed): 8-bit grey/RGB/RGBA, non-interlaced, all
five filter types. The capture harness (System.Drawing Bitmap.Save) writes filter 0 everywhere,
which decodes in well under a second; Sub/Up rows are vectorised, Average/Paeth rows fall back to
a per-pixel Python loop (correct but slow, only for PNGs from other tools).

Decoding: every block carries its own W,B,W,B reference cells, so each block is thresholded
locally (exposure, tone curve, colour grading and local darkening such as SSAO cancel out). A
cell is the mean of its central pixels (FXAA / 1-px blur tolerant). A row is accepted only if
no cell lies within 20% of the contrast of the threshold and its parity cell matches; the header
row must also carry the expected 12-bit block code. A component (32 bits) needs both of its rows.
Duplicated registers are merged; disagreeing copies (per-draw constants seen on two different
objects) are reported as conflicts.

Diff: values are compared bit-exactly per register component. With --repeat (a second capture of
the reference condition) registers that change between the two reference captures are 'noisy'
(camera sway, time, wind ...); a register is 'changed' only if it differs from the reference and
is stable across the reference repeat.
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import zlib
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import probe_layout as PL  # noqa: E402

ROOT = HERE.parents[1]
DEF_LAYOUT = ROOT / "dist" / "_probe" / "layout.json"
DEF_NAMES = ROOT / "dist" / "_probe" / "reg_names.json"
MIN_CONTRAST = 16.0      # 8-bit levels between W and B reference means
MARGIN = 0.20            # cells closer than this fraction of the contrast to the threshold are uncertain


# ---------------------------------------------------------------------------
# PNG


def _paeth_row(f: np.ndarray, prev: np.ndarray, bpp: int) -> np.ndarray:
    out = bytearray(f.tobytes()); p = prev.tobytes()
    for i in range(len(out)):
        a = out[i - bpp] if i >= bpp else 0
        b = p[i]; c = p[i - bpp] if i >= bpp else 0
        pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
        pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
        out[i] = (out[i] + pr) & 255
    return np.frombuffer(bytes(out), np.uint8)


def _avg_row(f: np.ndarray, prev: np.ndarray, bpp: int) -> np.ndarray:
    out = bytearray(f.tobytes()); p = prev.tobytes()
    for i in range(len(out)):
        a = out[i - bpp] if i >= bpp else 0
        out[i] = (out[i] + ((a + p[i]) >> 1)) & 255
    return np.frombuffer(bytes(out), np.uint8)


def read_png(path) -> np.ndarray:
    """(H, W, 3) uint8 RGB."""
    b = Path(path).read_bytes()
    if b[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path}: not a PNG")
    i, idat, ihdr = 8, [], None
    while i < len(b):
        n, t = struct.unpack(">I4s", b[i:i + 8])
        d = b[i + 8:i + 8 + n]
        if t == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", d)
        elif t == b"IDAT":
            idat.append(d)
        elif t == b"IEND":
            break
        i += 12 + n
    w, h, depth, ctype, _, _, interlace = ihdr
    if depth != 8 or interlace or ctype not in (0, 2, 6):
        raise ValueError(f"{path}: unsupported PNG (depth {depth}, colour type {ctype}, interlace {interlace})")
    bpp = {0: 1, 2: 3, 6: 4}[ctype]
    raw = np.frombuffer(zlib.decompress(b"".join(idat)), np.uint8).reshape(h, w * bpp + 1)
    ft = raw[:, 0]
    data = raw[:, 1:].copy()
    if np.any(ft):
        prev = np.zeros(w * bpp, np.uint8)
        for y in range(h):
            f, row = ft[y], data[y]
            if f == 1:
                row = np.cumsum(row.reshape(w, bpp).astype(np.uint32), axis=0).astype(np.uint8).reshape(-1)
            elif f == 2:
                row = (row.astype(np.uint16) + prev).astype(np.uint8)
            elif f == 3:
                row = _avg_row(row, prev, bpp)
            elif f == 4:
                row = _paeth_row(row, prev, bpp)
            elif f != 0:
                raise ValueError(f"{path}: bad filter {f}")
            data[y] = row
            prev = data[y]
    img = data.reshape(h, w, bpp)
    return np.repeat(img, 3, axis=2) if bpp == 1 else img[:, :, :3]


def write_png(path, rgb: np.ndarray, filters=(0,)) -> None:
    """Minimal PNG writer (RGB8); cycles through the given filter types row by row (for tests)."""
    h, w, _ = rgb.shape
    rows, prev = [], np.zeros(w * 3, np.int32)
    for y in range(h):
        cur = rgb[y].reshape(-1).astype(np.int32)
        f = filters[y % len(filters)]
        left = np.concatenate([np.zeros(3, np.int32), cur[:-3]])
        upleft = np.concatenate([np.zeros(3, np.int32), prev[:-3]])
        if f == 0:
            enc = cur
        elif f == 1:
            enc = cur - left
        elif f == 2:
            enc = cur - prev
        elif f == 3:
            enc = cur - (left + prev) // 2
        else:
            pa, pb, pc = np.abs(prev - upleft), np.abs(left - upleft), np.abs(left + prev - 2 * upleft)
            pr = np.where((pa <= pb) & (pa <= pc), left, np.where(pb <= pc, prev, upleft))
            enc = cur - pr
        rows.append(bytes([f]) + (enc & 255).astype(np.uint8).tobytes())
        prev = cur

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
                           chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) + chunk(b"IEND", b""))


# ---------------------------------------------------------------------------
# decode


def load_layout(path=DEF_LAYOUT) -> dict:
    return json.loads(Path(path).read_text())


def cell_means(img: np.ndarray, lay: dict) -> np.ndarray:
    """(grid_rows, BLOCK_ROWS, grid_cols, BLOCK_COLS) mean luminance of each cell's core."""
    W, H = lay["res"]
    if img.shape[1] != W or img.shape[0] != H:
        raise ValueError(f"image is {img.shape[1]}x{img.shape[0]}, layout expects {W}x{H} (render-target pixels)")
    s, (ox, oy) = lay["cell"], lay["origin"]
    gr, gc = lay["grid_rows"], lay["grid_cols"]
    lum = img.astype(np.float32).mean(axis=2)
    a = lum[oy:oy + gr * PL.BLOCK_ROWS * s, ox:ox + gc * PL.BLOCK_COLS * s]
    a = a.reshape(gr, PL.BLOCK_ROWS, s, gc, PL.BLOCK_COLS, s)
    m = max(1, s // 3)
    return a[:, :, m:s - m, :, :, m:s - m].mean(axis=(2, 5))


def decode_blocks(img: np.ndarray, lay: dict) -> list[dict]:
    cm = cell_means(img, lay)
    out = []
    for b in lay["blocks"]:
        c = cm[b["row"], :, b["col"], :]          # (9, 17)
        code = b["code"]
        rec = {"code": code, "copy": b["copy"], "col": b["col"], "row": b["row"]}
        w, bk = (c[0, 0] + c[0, 2]) / 2, (c[0, 1] + c[0, 3]) / 2
        con = w - bk
        rec["white"], rec["black"] = round(float(w), 1), round(float(bk), 1)
        ref_ok = con >= MIN_CONTRAST and abs(c[0, 0] - c[0, 2]) < 0.35 * con and abs(c[0, 1] - c[0, 3]) < 0.35 * con
        if not ref_ok:
            rec["status"] = "no_reference"; out.append(rec); continue
        thr = (w + bk) / 2
        bits = c > thr
        unsure = np.abs(c - thr) < MARGIN * con
        hdr_val = int("".join("1" if x else "0" for x in bits[0, 4:16]), 2)
        hdr_ok = (not unsure[0].any() and hdr_val == (code & 0xFFF)
                  and bool(bits[0, 16]) == bool(bin(hdr_val).count("1") & 1)
                  and list(bits[0, :4]) == [True, False, True, False])
        if not hdr_ok:
            rec["status"] = "bad_header"; rec["header_seen"] = hdr_val; out.append(rec); continue
        if code >> 9 == PL.SPACE_RAMP:
            rec["status"] = "ok"
            rec["ramp"] = [[round(PL.ramp_level(code & 511, i), 5), round(float(v), 2)]
                           for i, v in enumerate(c[1:].reshape(-1))]
            out.append(rec); continue
        rows_ok = []
        halves = []
        for r in range(1, PL.BLOCK_ROWS):
            v = int("".join("1" if x else "0" for x in bits[r, :16]), 2)
            ok = not unsure[r].any() and bool(bits[r, 16]) == bool(bin(v).count("1") & 1)
            rows_ok.append(ok); halves.append(v)
        comps = [(halves[2 * k] << 16 | halves[2 * k + 1]) if rows_ok[2 * k] and rows_ok[2 * k + 1] else None for k in range(4)]
        rec["status"] = "ok" if all(x is not None for x in comps) else "partial"
        rec["comps"] = comps
        out.append(rec)
    return out


def f32(u: int | None):
    if u is None:
        return None
    x = struct.unpack("<f", struct.pack("<I", u))[0]
    return x if math.isfinite(x) else repr(x)


def merge(blocks: list[dict], reg_names: dict | None = None, variant: str | None = None) -> dict:
    """{label: {"u32": [..], "f32": [..], "copies": n_valid, "conflict": bool}} per register."""
    by = {}
    for b in blocks:
        by.setdefault(b["code"], []).append(b)
    regs = {}
    for code, copies in sorted(by.items()):
        if code >> 9 == PL.SPACE_RAMP:
            continue
        vals, conflict = [], False
        for k in range(4):
            seen = sorted({c["comps"][k] for c in copies if c.get("comps") and c["comps"][k] is not None})
            if len(seen) > 1:
                conflict = True
            vals.append(seen[0] if len(seen) == 1 else (None if not seen else seen))
        label = PL.describe(code)
        e = {"u32": [(f"{v:08x}" if isinstance(v, int) else (None if v is None else [f"{x:08x}" for x in v])) for v in vals],
             "f32": [(f32(v) if isinstance(v, int) else (None if v is None else [f32(x) for x in v])) for v in vals],
             "valid_copies": sum(1 for c in copies if c["status"] in ("ok", "partial")),
             "copies": len(copies), "conflict": conflict,
             "status": [c["status"] for c in copies]}
        if reg_names and code >> 9 == 0:
            reg = str(code & 511)
            nm = reg_names.get("vanilla_421b", {}).get(reg)
            if nm:
                e["name"] = f"{nm['name']}.{nm['comps']}"
            elif reg in reg_names.get("probe_name_extras", {}):
                e["name"] = ("(probe_name) " if variant != "probe_name" else "") + reg_names["probe_name_extras"][reg]
        regs[label] = e
    return regs


def decode_image(path, lay, reg_names=None, variant=None) -> dict:
    img = read_png(path)
    blocks = decode_blocks(img, lay)
    st = {}
    for b in blocks:
        st[b["status"]] = st.get(b["status"], 0) + 1
    ramps = {PL.describe(b["code"]) + f"#{b['copy']}": b["ramp"] for b in blocks if "ramp" in b}
    return {"image": str(path), "block_status": st, "registers": merge(blocks, reg_names, variant), "ramps": ramps}


def _comp_state(a, b):
    if a is None or b is None or isinstance(a, list) or isinstance(b, list):
        return "unknown"
    return "same" if a == b else "diff"


def diff(ref: dict, other: dict, repeat: dict | None = None) -> dict:
    changed, noisy, unknown = {}, {}, []
    for label, ra in ref["registers"].items():
        rb = other["registers"].get(label)
        if rb is None:
            continue
        for k in range(4):
            s = _comp_state(ra["u32"][k], rb["u32"][k])
            key = f"{label}.{'xyzw'[k]}"
            if s == "unknown":
                if ra["u32"][k] != rb["u32"][k]:
                    unknown.append(key)
                continue
            if s == "same":
                continue
            rep = repeat["registers"].get(label) if repeat else None
            rs = _comp_state(ra["u32"][k], rep["u32"][k]) if rep else "unknown"
            entry = {"ref": ra["f32"][k], "new": rb["f32"][k], "ref_u32": ra["u32"][k], "new_u32": rb["u32"][k],
                     "name": ra.get("name", "")}
            if rs == "diff":
                entry["repeat"] = rep["f32"][k]
                noisy[key] = entry
            else:
                entry["repeat_stable"] = rs == "same"
                changed[key] = entry
    return {"changed": changed, "noisy": noisy, "unknown": unknown}


def nonzero_summary(dec: dict) -> dict:
    """Registers outside the declared FC range (c103+), and other slots, that hold non-zero data."""
    out = {}
    for label, r in dec["registers"].items():
        if label.startswith("c") and int(label[1:]) < 103:
            continue
        nz = [u for u in r["u32"] if isinstance(u, str) and u != "00000000"]
        if nz:
            out[label] = r["f32"]
    return out


# ---------------------------------------------------------------------------
# CLI


def _names(path):
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else None


def cmd_decode(a):
    lay, rn = load_layout(a.layout), _names(a.names)
    res = [decode_image(p, lay, rn) for p in a.images]
    for r in res:
        print(f"{r['image']}: {r['block_status']}")
        for label, e in r["registers"].items():
            if any(isinstance(u, str) and u != "00000000" for u in e["u32"]) or e["conflict"]:
                print(f"  {label:9s} {e.get('name', ''):42s} {e['f32']}{'  CONFLICT' if e['conflict'] else ''}")
    _dump(a, res)


def cmd_diff(a):
    lay, rn = load_layout(a.layout), _names(a.names)
    ref = decode_image(a.ref, lay, rn)
    rep = decode_image(a.repeat, lay, rn) if a.repeat else None
    res = {"ref": a.ref, "repeat": a.repeat, "noise_ref_vs_repeat": diff(ref, rep) if rep else None, "diffs": {}}
    for p in a.images:
        d = diff(ref, decode_image(p, lay, rn), rep)
        res["diffs"][str(p)] = d
        _print_diff(str(p), d)
    _dump(a, res)


def _print_diff(title, d):
    print(f"== {title}: {len(d['changed'])} changed, {len(d['noisy'])} noisy, {len(d['unknown'])} unreadable")
    for k, e in d["changed"].items():
        print(f"   {k:11s} {e['name']:42s} {e['ref']!s:>14} -> {e['new']!s:<14}{'' if e.get('repeat_stable') else ' (no repeat check)'}")


def cmd_session(a):
    lay, rn = load_layout(a.layout), _names(a.names)
    root = Path(a.dir)
    variant = root.name
    dirs = sorted(p for p in root.iterdir() if p.is_dir() and "__" in p.name)
    by_dp = {p.name.split("__", 1)[1]: p for p in dirs}
    if "dp_fixed" not in by_dp:
        raise SystemExit(f"{root}: no *__dp_fixed folder")
    bonfires = sorted(p.stem for p in by_dp["dp_fixed"].glob("*.png") if not p.stem.endswith(".thumb"))
    report = {"variant": variant, "layout_id": lay["layout_id"], "bonfires": {}}
    for bf in bonfires:
        dec = {dp: decode_image(d / f"{bf}.png", lay, rn, variant) for dp, d in by_dp.items() if (d / f"{bf}.png").exists()}
        ref, rep = dec["dp_fixed"], dec.get("dp_fixed_b")
        bfrep = {"block_status": {dp: x["block_status"] for dp, x in dec.items()},
                 "noise_fixed_vs_fixed_b": diff(ref, rep) if rep else None,
                 "nonzero_beyond_c102_or_other_slots": nonzero_summary(ref),
                 "ramps_dp_fixed": ref["ramps"], "diffs": {},
                 "reference_registers": ref["registers"]}
        print(f"#### {variant} / {bf}: blocks {ref['block_status']}")
        if rep:
            n = bfrep["noise_fixed_vs_fixed_b"]
            print(f"   noise (dp_fixed vs dp_fixed_b): {len(n['changed'])} registers differ: {sorted(n['changed'])[:24]}")
        print(f"   non-zero beyond c102 / other slots: {sorted(bfrep['nonzero_beyond_c102_or_other_slots'])[:40]}")
        for dp, x in sorted(dec.items()):
            if dp in ("dp_fixed", "dp_fixed_b"):
                continue
            d = diff(ref, x, rep)
            bfrep["diffs"][dp] = d
            _print_diff(f"{bf} {dp}", d)
        report["bonfires"][bf] = bfrep
    out = Path(a.json) if a.json else root / "probe_report.json"
    out.write_text(json.dumps(report, indent=1, default=str))
    print(f"wrote {out}")


def cmd_names(a):
    """probe_reg vs probe_name for the same DrawParam variant: does the named region change?"""
    lay, rn = load_layout(a.layout), _names(a.names)
    root = Path(a.dir)
    named = {f"c{r}" for r in range(400, 412)}
    res = {}
    for dreg in sorted((root / "probe_reg").glob("probe_reg__*")):
        dp = dreg.name.split("__", 1)[1]
        dname = root / "probe_name" / f"probe_name__{dp}"
        for img in sorted(dreg.glob("*.png")):
            if img.stem.endswith(".thumb") or not (dname / img.name).exists():
                continue
            A = decode_image(img, lay, rn, "probe_reg")
            B = decode_image(dname / img.name, lay, rn, "probe_name")
            d = diff(A, B)
            key = f"{dp}/{img.stem}"
            res[key] = {"named_region_reg": {k: A["registers"][k]["f32"] for k in sorted(named) if k in A["registers"]},
                        "named_region_name": {k: B["registers"][k]["f32"] for k in sorted(named) if k in B["registers"]},
                        "changed_registers": sorted(d["changed"]), "unreadable": d["unknown"][:50]}
            hit = [k for k in d["changed"] if k.split(".")[0] in named]
            print(f"{key:28s} named-region changes: {hit or 'none'}; other changes: {len(d['changed']) - len(hit)}")
    _dump(a, res)


def _dump(a, obj):
    if getattr(a, "json", None):
        Path(a.json).write_text(json.dumps(obj, indent=1, default=str))
        print(f"wrote {a.json}")


# ---------------------------------------------------------------------------
# self-test


def _synthetic_regs(lay, seed, overrides=None):
    rng = np.random.default_rng(seed)
    regs = {}
    for b in lay["blocks"]:
        code = b["code"]
        if code >> 9 == PL.SPACE_RAMP or code in regs:
            continue
        regs[code] = tuple(int(x) for x in rng.integers(0, 2**32, 4, dtype=np.uint64))
    special = [0x7FC00000, 0x80000000, 0x00000001, 0x3F800000]   # NaN, -0, denormal, 1.0
    regs[PL.code_of(0, 35)] = tuple(special)
    for c, v in (overrides or {}).items():
        regs[c] = v
    return regs


def _post_chain(level: np.ndarray, white: float, rng) -> np.ndarray:
    """HDR level image -> 8-bit RGB through a plausible post chain."""
    x = level * white
    x = x * np.linspace(0.55, 1.0, x.shape[1], dtype=np.float32)[None, :]          # vignette / SSAO-like darkening
    k = np.ones(3, np.float32) / 3                                                   # 3-tap blur (FXAA stand-in)
    x = np.apply_along_axis(lambda r: np.convolve(r, k, "same"), 1, x)
    x = np.apply_along_axis(lambda c: np.convolve(c, k, "same"), 0, x)
    A, B, C, D, E, F, Wt = 0.15, 0.50, 0.10, 0.20, 0.02, 0.30, 11.2                  # Hable
    h = lambda v: ((v * (A * v + C * B) + D * E) / (v * (A * v + B) + D * F)) - E / F  # noqa: E731
    y = h(x * 2.0) / h(np.float32(Wt))
    y = np.clip(y, 0, 1) ** (1 / 2.2) * 255 + rng.normal(0, 2.0, x.shape)
    g = np.clip(np.round(y), 0, 255).astype(np.uint8)
    return np.stack([g, (g * 0.97).astype(np.uint8), (g * 0.9).astype(np.uint8)], axis=2)  # mild tint


def cmd_selftest(a):
    lay = load_layout(a.layout)
    rng = np.random.default_rng(1)
    tmp = ROOT / "dist" / "_probe" / "_selftest"
    tmp.mkdir(parents=True, exist_ok=True)
    regs_a = _synthetic_regs(lay, 7)
    ch_code = PL.code_of(0, 5)
    regs_b = dict(regs_a)
    regs_b[ch_code] = (0x3F000000, regs_a[ch_code][1], regs_a[ch_code][2], 0x40490FDB)      # 0.5 and pi
    regs_r = dict(regs_a)
    noisy = PL.code_of(0, 107)                                                              # 'camera'
    regs_r[noisy] = (regs_a[noisy][0] ^ 1, *regs_a[noisy][1:])
    regs_b[noisy] = (regs_a[noisy][0] ^ 2, *regs_a[noisy][1:])
    if a.warp:
        import probe_warp
        lev = {k: probe_warp.render_levels(lay, v) for k, v in (("a", regs_a), ("r", regs_r), ("b", regs_b))}
        ref_lev = PL.render(lay, regs_a)
        mism = float(np.abs(lev["a"] - ref_lev).max())
        print(f"WARP render vs python reference encoder: max |diff| = {mism:g}")
        if mism > 1e-5:
            raise SystemExit("WARP output does not match the reference encoder")
    else:
        lev = {"a": PL.render(lay, regs_a), "r": PL.render(lay, regs_r), "b": PL.render(lay, regs_b)}
    paths = {}
    for k, L in lev.items():
        img = _post_chain(L, lay["white_hdr"], rng)
        if k == "b":
            img[0:700, 1600:2400] = 135                                                     # occluder (sky / non-probe draw)
        p = tmp / f"synthetic_{k}.png"
        write_png(p, img, filters=(0,) if k != "b" else (0, 1, 2, 3, 4))
        paths[k] = p
    # PNG round trip
    back = read_png(paths["b"])
    print(f"PNG all-filter round trip ok: {back.shape}")
    A = decode_image(paths["a"], lay)
    R = decode_image(paths["r"], lay)
    B = decode_image(paths["b"], lay)
    exp = {PL.describe(c): v for c, v in regs_a.items()}
    bad = []
    for label, e in A["registers"].items():
        want = [f"{x:08x}" for x in exp[label]]
        if e["u32"] != want:
            bad.append((label, e["u32"], want))
    print(f"decode A: blocks {A['block_status']}, registers {len(A['registers'])}, mismatches {len(bad)}")
    d = diff(A, B, R)
    print(f"diff A->B: changed {sorted(d['changed'])}, noisy {sorted(d['noisy'])}, unreadable {len(d['unknown'])}")
    print(f"decode B (occluded): blocks {B['block_status']}")
    ok = (not bad and set(d["changed"]) == {"c5.x", "c5.w"} and set(d["noisy"]) == {"c107.x"}
          and A["registers"]["c35"]["f32"][0] == "nan" and B["block_status"].get("ok", 0) < A["block_status"]["ok"])
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--layout", default=str(DEF_LAYOUT))
    ap.add_argument("--names", default=str(DEF_NAMES))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("decode"); p.add_argument("images", nargs="+"); p.add_argument("--json")
    p = sub.add_parser("diff"); p.add_argument("--ref", required=True); p.add_argument("--repeat"); p.add_argument("images", nargs="+"); p.add_argument("--json")
    p = sub.add_parser("session"); p.add_argument("dir"); p.add_argument("--json")
    p = sub.add_parser("names"); p.add_argument("dir"); p.add_argument("--json")
    p = sub.add_parser("selftest"); p.add_argument("--warp", action="store_true")
    a = ap.parse_args(argv)
    return {"decode": cmd_decode, "diff": cmd_diff, "session": cmd_session, "names": cmd_names, "selftest": cmd_selftest}[a.cmd](a) or 0


if __name__ == "__main__":
    sys.exit(main())
