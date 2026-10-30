"""Run the generated probe pixel-shader body on the D3D11 WARP software rasteriser (offline test).

Compiles the probe body from make_probe.probe_hlsl with a minimal interface (SV_Position in,
SV_Target0 out), uploads known register contents to cb0 / cb2..cb5, draws a full-screen triangle
into an R32G32B32A32_FLOAT target at the layout resolution and returns the red channel divided by
the white level, i.e. the same 'level' image probe_layout.render() produces in Python.

Pure ctypes against d3d11.dll (no extra packages). Windows only.
"""
from __future__ import annotations

import ctypes as C
import struct
import sys
from ctypes import wintypes as W
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dxbc  # noqa: E402
import make_probe as MP  # noqa: E402
import probe_layout as PL  # noqa: E402

VS_SRC = """
static const float2 V[6] = { float2(-1,-1), float2(-1,3), float2(3,-1), float2(-1,-1), float2(3,-1), float2(-1,3) };
float4 main(uint id : SV_VertexID) : SV_Position { return float4(V[id], 0.0, 1.0); }
"""


class BUFFER_DESC(C.Structure):
    _fields_ = [("ByteWidth", W.UINT), ("Usage", W.UINT), ("BindFlags", W.UINT), ("CPUAccessFlags", W.UINT),
                ("MiscFlags", W.UINT), ("StructureByteStride", W.UINT)]


class SUBRESOURCE_DATA(C.Structure):
    _fields_ = [("pSysMem", C.c_void_p), ("SysMemPitch", W.UINT), ("SysMemSlicePitch", W.UINT)]


class TEX2D_DESC(C.Structure):
    _fields_ = [("Width", W.UINT), ("Height", W.UINT), ("MipLevels", W.UINT), ("ArraySize", W.UINT),
                ("Format", W.UINT), ("SampleCount", W.UINT), ("SampleQuality", W.UINT), ("Usage", W.UINT),
                ("BindFlags", W.UINT), ("CPUAccessFlags", W.UINT), ("MiscFlags", W.UINT)]


class VIEWPORT(C.Structure):
    _fields_ = [(n, C.c_float) for n in ("TopLeftX", "TopLeftY", "Width", "Height", "MinDepth", "MaxDepth")]


class MAPPED(C.Structure):
    _fields_ = [("pData", C.c_void_p), ("RowPitch", W.UINT), ("DepthPitch", W.UINT)]


def _m(obj, idx, restype, *argtypes):
    """Bound COM method idx of interface pointer obj (c_void_p)."""
    vtbl = C.cast(obj, C.POINTER(C.POINTER(C.c_void_p)))[0]
    return C.WINFUNCTYPE(restype, C.c_void_p, *argtypes)(vtbl[idx])


def _ok(hr, what):
    if hr < 0:
        raise RuntimeError(f"{what} failed: HRESULT {hr & 0xFFFFFFFF:#010x}")


def _compile(src: str, tag: str) -> bytes:
    blob, err = MP.compile_cached(src, tag) if tag != "vs" else _compile_vs(src)
    if blob is None:
        raise RuntimeError(err)
    return blob


def _compile_vs(src: str):
    p = MP.WORK / "hlsl" / "warp_vs.hlsl"; o = MP.WORK / "fpo" / "warp_vs.vpo"
    p.parent.mkdir(parents=True, exist_ok=True); o.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src, newline="\n")
    rc, log = MP.fxc(p, o, "vs_5_0")
    return (o.read_bytes(), "") if rc == 0 else (None, log)


def test_ps_source(lay: dict) -> str:
    isgn = [dxbc.SigElem("SV_Position", 0, 1, 3, 0, 0xF, 0x3)]
    osgn = [dxbc.SigElem("SV_Target", 0, 64, 3, 0, 0xF, 0x0)]
    return MP.probe_hlsl(isgn, osgn, False, lay, "warp-test")


def render_levels(lay: dict, regs: dict) -> np.ndarray:
    Wd, Ht = lay["res"]
    d3d = C.WinDLL("d3d11")
    dev, ctx, fl = C.c_void_p(), C.c_void_p(), W.UINT()
    _ok(d3d.D3D11CreateDevice(None, 5, None, 0, None, 0, 7, C.byref(dev), C.byref(fl), C.byref(ctx)), "D3D11CreateDevice(WARP)")
    vsb, psb = _compile(VS_SRC, "vs"), _compile(test_ps_source(lay), "warp_ps")
    vs, ps = C.c_void_p(), C.c_void_p()
    _ok(_m(dev, 12, C.c_long, C.c_char_p, C.c_size_t, C.c_void_p, C.POINTER(C.c_void_p))(dev, vsb, len(vsb), None, C.byref(vs)), "CreateVertexShader")
    _ok(_m(dev, 15, C.c_long, C.c_char_p, C.c_size_t, C.c_void_p, C.POINTER(C.c_void_p))(dev, psb, len(psb), None, C.byref(ps)), "CreatePixelShader")

    def cbuf(nreg, space):
        data = bytearray(16 * nreg)
        for r in range(nreg):
            v = regs.get(PL.code_of(space, r))
            if v is not None:
                struct.pack_into("<4I", data, 16 * r, *v)
        raw = (C.c_ubyte * len(data)).from_buffer(data)
        desc = BUFFER_DESC(len(data), 0, 4, 0, 0, 0)
        init = SUBRESOURCE_DATA(C.cast(raw, C.c_void_p), 0, 0)
        b = C.c_void_p()
        _ok(_m(dev, 3, C.c_long, C.POINTER(BUFFER_DESC), C.POINTER(SUBRESOURCE_DATA), C.POINTER(C.c_void_p))(dev, C.byref(desc), C.byref(init), C.byref(b)), "CreateBuffer")
        return b

    cbs = {0: cbuf(MP.NREG, 0)}
    for sl in MP.EXTRA_SLOTS:
        cbs[sl] = cbuf(MP.EXTRA_SLOT_REGS, sl)
    tex, stg, rtv = C.c_void_p(), C.c_void_p(), C.c_void_p()
    td = TEX2D_DESC(Wd, Ht, 1, 1, 2, 1, 0, 0, 0x20, 0, 0)
    _ok(_m(dev, 5, C.c_long, C.POINTER(TEX2D_DESC), C.c_void_p, C.POINTER(C.c_void_p))(dev, C.byref(td), None, C.byref(tex)), "CreateTexture2D")
    sd = TEX2D_DESC(Wd, Ht, 1, 1, 2, 1, 0, 3, 0, 0x20000, 0)
    _ok(_m(dev, 5, C.c_long, C.POINTER(TEX2D_DESC), C.c_void_p, C.POINTER(C.c_void_p))(dev, C.byref(sd), None, C.byref(stg)), "CreateTexture2D(staging)")
    _ok(_m(dev, 9, C.c_long, C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p))(dev, tex, None, C.byref(rtv)), "CreateRenderTargetView")

    _m(ctx, 33, None, W.UINT, C.POINTER(C.c_void_p), C.c_void_p)(ctx, 1, C.byref(rtv), None)      # OMSetRenderTargets
    vp = VIEWPORT(0, 0, Wd, Ht, 0, 1)
    _m(ctx, 44, None, W.UINT, C.POINTER(VIEWPORT))(ctx, 1, C.byref(vp))                            # RSSetViewports
    _m(ctx, 24, None, W.UINT)(ctx, 4)                                                              # IASetPrimitiveTopology
    _m(ctx, 11, None, C.c_void_p, C.c_void_p, W.UINT)(ctx, vs, None, 0)                           # VSSetShader
    _m(ctx, 9, None, C.c_void_p, C.c_void_p, W.UINT)(ctx, ps, None, 0)                            # PSSetShader
    for slot, b in cbs.items():
        _m(ctx, 16, None, W.UINT, W.UINT, C.POINTER(C.c_void_p))(ctx, slot, 1, C.byref(b))        # PSSetConstantBuffers
    _m(ctx, 13, None, W.UINT, W.UINT)(ctx, 6, 0)                                                   # Draw
    _m(ctx, 47, None, C.c_void_p, C.c_void_p)(ctx, stg, tex)                                       # CopyResource
    mp = MAPPED()
    _ok(_m(ctx, 14, C.c_long, C.c_void_p, W.UINT, W.UINT, W.UINT, C.POINTER(MAPPED))(ctx, stg, 0, 1, 0, C.byref(mp)), "Map")
    try:
        buf = (C.c_ubyte * (mp.RowPitch * Ht)).from_address(mp.pData)
        arr = np.frombuffer(buf, np.float32).reshape(Ht, mp.RowPitch // 4)[:, :Wd * 4].reshape(Ht, Wd, 4).copy()
    finally:
        _m(ctx, 15, None, C.c_void_p, W.UINT)(ctx, stg, 0)                                         # Unmap
    for o in (rtv, stg, tex, ps, vs, *cbs.values(), ctx, dev):
        _m(o, 2, W.ULONG)(o)                                                                        # Release
    if not np.allclose(arr[:, :, 3], 1.0) or not (np.array_equal(arr[:, :, 0], arr[:, :, 1]) and np.array_equal(arr[:, :, 0], arr[:, :, 2])):
        raise RuntimeError("unexpected alpha or non-grey output")
    return arr[:, :, 0] / np.float32(MP.WHITE)


def create_check(blobs: dict[str, bytes]) -> dict[str, str]:
    """CreatePixelShader every blob on a WARP device (the runtime validates container, checksum,
    signatures and bytecode). Returns {name: error} for failures."""
    d3d = C.WinDLL("d3d11")
    dev, ctx, fl = C.c_void_p(), C.c_void_p(), W.UINT()
    _ok(d3d.D3D11CreateDevice(None, 5, None, 0, None, 0, 7, C.byref(dev), C.byref(fl), C.byref(ctx)), "D3D11CreateDevice(WARP)")
    create = _m(dev, 15, C.c_long, C.c_char_p, C.c_size_t, C.c_void_p, C.POINTER(C.c_void_p))
    bad = {}
    for name, b in sorted(blobs.items()):
        ps = C.c_void_p()
        hr = create(dev, b, len(b), None, C.byref(ps))
        if hr < 0:
            bad[name] = f"{hr & 0xFFFFFFFF:#010x}"
        else:
            _m(ps, 2, W.ULONG)(ps)
    for o in (ctx, dev):
        _m(o, 2, W.ULONG)(o)
    return bad


if __name__ == "__main__":
    # python probe_warp.py ARCHIVE.dcx [...]: CreatePixelShader every entry of each archive
    import shaderbnd
    for path in sys.argv[1:]:
        arc = shaderbnd.read_archive(path)
        bad = create_check({e.name: e.data for e in arc.entries})
        print(f"{path}: {len(arc.entries)} entries, CreatePixelShader failures: {len(bad)} {dict(list(bad.items())[:5])}")
