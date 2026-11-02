#!/usr/bin/env python3
"""View the engine tracer's DDS render-target dumps.

  dds2png.py <trace dir> --sheet out.png [--thumb 480]     contact sheet of every dump, in pass order
  dds2png.py <file.dds> --png out.png                      one texture at full size

HDR and depth formats are normalised per image (percentile range) so structure is visible; these are
inspection views, not colour-accurate renders. 8-bit formats pass through untouched.
"""
import argparse
import pathlib
import struct
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "shaders" / "tools"))
from probe_decode import write_png  # noqa: E402


def read_dds(path: pathlib.Path):
    raw = path.read_bytes()
    h, w, pitch = struct.unpack_from("<III", raw, 12)
    fmt = struct.unpack_from("<I", raw, 128)[0]
    data = raw[148:]
    return w, h, pitch, fmt, data


def rows(data: bytes, h: int, pitch: int, bpp: int, w: int) -> np.ndarray:
    a = np.frombuffer(data, dtype=np.uint8, count=pitch * h).reshape(h, pitch)
    return a[:, : w * bpp]


def decode(path: pathlib.Path):
    """Return (float RGB array in 0..1 or raw range, already_ldr, description)."""
    w, h, pitch, fmt, data = read_dds(path)
    if fmt in (27, 28, 29, 87, 88, 90, 91, 92, 93):  # 8888 variants
        a = rows(data, h, pitch, 4, w).reshape(h, w, 4)
        rgb = a[:, :, :3].astype(np.float32) / 255
        if fmt in (87, 88, 90, 91, 92, 93):
            rgb = rgb[:, :, ::-1]
        return rgb, True, "RGBA8"
    if fmt in (9, 10, 11, 13):  # RGBA16
        a = rows(data, h, pitch, 8, w)
        v = np.frombuffer(a.tobytes(), dtype=np.float16 if fmt == 10 else np.uint16).reshape(h, w, 4).astype(np.float32)
        return v[:, :, :3], fmt != 10, "RGBA16" + ("F" if fmt == 10 else "")
    if fmt in (24, 25):  # RGB10A2
        a = rows(data, h, pitch, 4, w)
        p = np.frombuffer(a.tobytes(), dtype=np.uint32).reshape(h, w)
        return np.stack([(p & 1023) / 1023, ((p >> 10) & 1023) / 1023, ((p >> 20) & 1023) / 1023], -1).astype(np.float32), True, "RGB10A2"
    if fmt == 26:  # R11G11B10F
        a = rows(data, h, pitch, 4, w)
        p = np.frombuffer(a.tobytes(), dtype=np.uint32).reshape(h, w)

        def f11(x, mbits):
            e = (x >> mbits) & 31
            m = x & ((1 << mbits) - 1)
            return np.where(e == 0, m / (1 << mbits) * 2.0 ** -14, (1 + m / (1 << mbits)) * 2.0 ** (e.astype(np.int32) - 15))

        return np.stack([f11(p & 2047, 6), f11((p >> 11) & 2047, 6), f11((p >> 22) & 1023, 5)], -1).astype(np.float32), False, "RG11B10F"
    if fmt in (2,):  # RGBA32F
        a = rows(data, h, pitch, 16, w)
        return np.frombuffer(a.tobytes(), dtype=np.float32).reshape(h, w, 4)[:, :, :3].copy(), False, "RGBA32F"
    if fmt in (16,):  # RG32F
        a = rows(data, h, pitch, 8, w)
        v = np.frombuffer(a.tobytes(), dtype=np.float32).reshape(h, w, 2)
        return np.dstack([v[:, :, 0], v[:, :, 1], np.zeros((h, w))]), False, "RG32F"
    if fmt in (34,):  # RG16F
        a = rows(data, h, pitch, 4, w)
        v = np.frombuffer(a.tobytes(), dtype=np.float16).reshape(h, w, 2).astype(np.float32)
        return np.dstack([v[:, :, 0], v[:, :, 1], np.zeros((h, w))]), False, "RG16F"
    if fmt in (39, 40, 41, 42):  # R32
        a = rows(data, h, pitch, 4, w)
        dt = np.uint32 if fmt == 42 else np.float32
        v = np.frombuffer(a.tobytes(), dtype=dt).reshape(h, w).astype(np.float32)
        return np.dstack([v, v, v]), False, "R32"
    if fmt in (44, 45, 46, 47):  # D24S8
        a = rows(data, h, pitch, 4, w)
        v = (np.frombuffer(a.tobytes(), dtype=np.uint32).reshape(h, w) & 0xFFFFFF).astype(np.float32) / 0xFFFFFF
        return np.dstack([v, v, v]), False, "D24"
    if fmt in (19, 20, 21, 22):  # D32S8X24
        a = rows(data, h, pitch, 8, w)
        v = np.frombuffer(a.tobytes(), dtype=np.float32).reshape(h, w * 2)[:, ::2]
        return np.dstack([v, v, v]), False, "D32S8"
    if fmt in (53, 54, 55, 56, 57, 58, 59):  # R16
        a = rows(data, h, pitch, 2, w)
        dt = np.float16 if fmt == 54 else np.uint16
        v = np.frombuffer(a.tobytes(), dtype=dt).reshape(h, w).astype(np.float32)
        return np.dstack([v, v, v]), False, "R16"
    if fmt in (60, 61, 62, 63, 64, 65):  # R8
        a = rows(data, h, pitch, 1, w).astype(np.float32) / 255
        return np.dstack([a, a, a]), True, "R8"
    if fmt in (48, 49, 50, 51, 52):  # RG8
        a = rows(data, h, pitch, 2, w).reshape(h, w, 2).astype(np.float32) / 255
        return np.dstack([a[:, :, 0], a[:, :, 1], np.zeros((h, w))]), True, "RG8"
    raise ValueError(f"format {fmt}")


def to_image(rgb: np.ndarray, ldr: bool) -> np.ndarray:
    v = np.nan_to_num(rgb, nan=0.0, posinf=0.0, neginf=0.0)
    if not ldr:
        lo, hi = np.percentile(v, 0.5), np.percentile(v, 99.5)
        v = (v - lo) / max(hi - lo, 1e-6)
    return (np.clip(v, 0, 1) * 255 + 0.5).astype(np.uint8)


def shot_image(path: pathlib.Path, paper_white: float = 200.0) -> np.ndarray:
    """A back-buffer dump as the 8-bit SDR image it stands for. An scRGB dump (native HDR) is read as 1 = 80 nits, with
    paper white at 255 and anything above it clipped, so its shadows and midtones compare directly with an SDR dump."""
    rgb, ldr, desc = decode(path)
    if desc != "RGBA16F":
        return to_image(rgb, ldr)
    v = np.nan_to_num(rgb, nan=0.0, posinf=0.0, neginf=0.0) * 80.0 / paper_white
    return (np.clip(np.maximum(v, 0.0) ** (1.0 / 2.2), 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--png")
    ap.add_argument("--sheet")
    ap.add_argument("--thumb", type=int, default=480)
    ap.add_argument("--cols", type=int, default=6)
    a = ap.parse_args()
    p = pathlib.Path(a.input)
    if a.png:
        rgb, ldr, _ = decode(p)
        write_png(a.png, to_image(rgb, ldr))
        return 0

    files = sorted(p.glob("*.dds"))
    tile_h = a.thumb * 9 // 16
    rows_n = (len(files) + a.cols - 1) // a.cols
    sheet = np.full((rows_n * tile_h, a.cols * a.thumb, 3), 20, dtype=np.uint8)
    index = []
    for i, f in enumerate(files):
        row, col = i // a.cols, i % a.cols
        try:
            rgb, ldr, desc = decode(f)
        except Exception as e:  # noqa: BLE001 - keep going, the index says what failed
            index.append(f"r{row} c{col} {f.stem} FAILED {e}")
            continue
        h, w = rgb.shape[:2]
        step = max(1, int(np.ceil(max(w / (a.thumb - 4), h / (tile_h - 4)))))
        img = to_image(rgb[::step, ::step], ldr)
        ih, iw = img.shape[:2]
        y, x = row * tile_h + 2, col * a.thumb + 2
        sheet[y:y + ih, x:x + iw] = img
        index.append(f"r{row} c{col} {f.stem} {w}x{h} {desc}")
    write_png(a.sheet, sheet)
    pathlib.Path(a.sheet).with_suffix(".txt").write_text("\n".join(index) + "\n", encoding="utf8")
    print(f"{len(files)} dumps -> {a.sheet} (index beside it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
