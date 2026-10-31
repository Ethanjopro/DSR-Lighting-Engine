"""Offline runtime test of FRPG_Fil_ResolveTAA.fpo builds on the D3D11 WARP rasteriser.

    .venv\\Scripts\\python.exe shaders\\tools\\taa_warp_test.py [--configs ident=rebuild0 cr=taa_cr ...]
            [--size 64] [--json OUT.json]

Runs the REAL compiled pixel shaders (dist/_shader/filter_<tag>/blobs/FRPG_Fil_ResolveTAA.fpo) with
synthetic inputs bound the way the shader reads them, frame after frame, feeding SV_Target0 back as
the history (t1) exactly as the engine does. Nothing from the game is read; the inputs are:
  t0  current HDR colour (RGBA32F, Load)          t1  history = previous frame's SV_Target0 (s1 bilinear clamp)
  t2  depth 0.5 (R32F, Gather, s2 point clamp)    t3  velocity: (vx, -vy) with vx < 1 = the object-velocity
  t4  mask (0, 1): no masked reset (s4)               path, so histUV = uv - v exactly (s3 point clamp)
  cb0 $Globals: c8 camera (0.1, 1000, -999.9, 100), c12 (W, H, 1/W, 1/H), c56.x = 0, c57 = (1,1,1,0),
      c58..c61 and c74..c77 identity, c78 0 (camera path non-degenerate; it is overridden by t3).
A fullscreen-triangle VS writes SV_Position + TEXCOORD0 (pixel-centre uv), matching the fpo ISGN.

Kernel harness (a faithful harness around the hook code, not the game shader): small test pixel
shaders that #include shaders/features/dsrle_filter_features.fxh and call its functions directly,
to measure (a) one history fetch, bilinear vs Catmull-Rom, against the exactly shifted truth, and
(b) one application of the sharpen function at blend = 0.

Scenes: hard edge, soft edge, 1-px checkerboard (high and low contrast), 1-px lines on a 4-px pitch,
flat, HDR highlights (10..50, grey and saturated), low-contrast texture; static with an 8-phase
Halton sub-pixel jitter, and moving by a sub-pixel velocity without jitter.

Regimes and metrics
  static                     zero velocity, 8-phase jitter, history seeded with the jitter mean.
                             "temporal_std" there IS output flicker (per-pixel std of the output luma).
                             Caveat: vanilla's blend weight is proportional to the distance of the
                             history luma from the nearest box edge, so two-level content (hard edge,
                             checkerboards, 1-px lines, HDR blocks) whose history sits on a box extreme
                             cannot move at all; "unchanged" rows for those scenes are partly blind to
                             the features. The moving regimes cover them.
  moving / moving_jitter     constant object velocity (0.37, 0.21) px/frame (jitter excluded from
                             the velocity). "error_spread_vs_truth" is the spread of (output - truth),
                             not flicker; "pulsing" (frame-to-frame variation of the output's detail
                             energy) is the flicker metric; "hp_ratio_vs_truth" > 1 = over-sharpened.
  moving_half_px             0.5 px/frame, the sub-pixel phase alternates every frame (max pulsing).
  static_jitter_in_velocity  static camera, engine velocity INCLUDES the jitter delta
                             (v = jitter[i-1] - jitter[i]); true output flicker vs identity.
  moving_sampler_filter      the same moving runs with s1 bilinear and s1 POINT.
  sharpen_buildup            share of the sharpening target reached after N still frames.
Coverage gaps (by construction): every pixel takes the object-velocity path (the camera
reprojection, c58..c61 / c74..c78, is not exercised) and depth is constant (closest-depth search
trivial); the mask never requests a reset (only the off-screen reset is tested); c56.x (motion
response) is 0; the history is RGBA32F (the game's history format is not known; the review
emulated fp16 and R11G11B10 rounding of the fed-back history on the CPU and saw no difference).
Original DSRLE tooling; pure ctypes against d3d11.dll, Windows only.
"""
from __future__ import annotations

import argparse
import ctypes as C
import json
import math
import struct
import subprocess
import sys
from ctypes import wintypes as W
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import fxcasm  # noqa: E402

DIST = ROOT / "dist" / "_shader"
WORK = DIST / "_work" / "taa_warp"
FEATURES = ROOT / "shaders" / "features"
Y = np.array([0.2126, 0.7152, 0.0722], np.float32)
FMT_RGBA32F, FMT_R32F = 2, 41
BIND_SRV, BIND_RTV = 0x8, 0x20

VS_SRC = r"""
static const float2 V[6] = { float2(-1,-1), float2(-1,3), float2(3,-1), float2(-1,-1), float2(3,-1), float2(-1,3) };
struct O { float4 pos : SV_Position; float2 uv : TEXCOORD0; };
O main(uint id : SV_VertexID) { O o; o.pos = float4(V[id], 0, 1); o.uv = float2(V[id].x * 0.5 + 0.5, 0.5 - V[id].y * 0.5); return o; }
"""

# (a) one history fetch: t0 = history, cb0.xy = offset in texels (histUV = uv - offset / size)
FETCH_PS = r"""
Texture2D gSMP_1 : register(t0);
SamplerState gSMP_1Sampler : register(s0);
cbuffer P : register(b0) { float4 gOff; };
#include "dsrle_filter_features.fxh"
float4 main(float4 pos : SV_Position, float2 uv : TEXCOORD0) : SV_Target0
{
	float2 size; gSMP_1.GetDimensions(size.x, size.y);
	float2 h = uv - gOff.xy / size;
#if MODE == 0
	return float4(gSMP_1.SampleLevel(gSMP_1Sampler, h, 0).rgb, 1);
#else
	return float4(dsrle_taa_hist_catmull_rom(h), 1);
#endif
}
"""

# (b) one sharpen application at blend 0 on the m-space 3x3 of t0 (out-of-range Loads read 0, as in the game)
SHARPEN_PS = r"""
Texture2D gSMP_0 : register(t0);
#include "dsrle_filter_features.fxh"
float3 M(int2 p) { float3 c = gSMP_0.Load(int3(p, 0)).rgb; return c / (dot(c, DSRLE_TAA_Y) + 1.0f); }
float4 main(float4 pos : SV_Position) : SV_Target0
{
	int2 p = int2(pos.xy);
	float3 c = M(p), n = M(p + int2(0, -1)), s = M(p + int2(0, 1)), w = M(p + int2(-1, 0)), e = M(p + int2(1, 0));
	float3 nw = M(p + int2(-1, -1)), ne = M(p + int2(1, -1)), sw = M(p + int2(-1, 1)), se = M(p + int2(1, 1));
	float3 cMin = min(min(min(min(n, c), s), w), e), cMax = max(max(max(max(n, c), s), w), e);
	float3 fMin = min(cMin, min(min(nw, ne), min(sw, se))), fMax = max(cMax, max(max(nw, ne), max(sw, se)));
	float3 r = dsrle_taa_sharpen(c, 0.0f, n, s, w, e, nw, ne, sw, se, (fMin + cMin) * 0.5f, (cMax + fMax) * 0.5f);
	r = r / (1.0f - min(dot(r, DSRLE_TAA_Y), 0.9999f));
	return float4(r, 1);
}
"""


# ---------------------------------------------------------------------------------------------
# minimal D3D11 via ctypes
class TEX2D_DESC(C.Structure):
    _fields_ = [("Width", W.UINT), ("Height", W.UINT), ("MipLevels", W.UINT), ("ArraySize", W.UINT),
                ("Format", W.UINT), ("SampleCount", W.UINT), ("SampleQuality", W.UINT), ("Usage", W.UINT),
                ("BindFlags", W.UINT), ("CPUAccessFlags", W.UINT), ("MiscFlags", W.UINT)]


class SAMPLER_DESC(C.Structure):
    _fields_ = [("Filter", W.UINT), ("AddressU", W.UINT), ("AddressV", W.UINT), ("AddressW", W.UINT),
                ("MipLODBias", C.c_float), ("MaxAnisotropy", W.UINT), ("ComparisonFunc", W.UINT),
                ("BorderColor", C.c_float * 4), ("MinLOD", C.c_float), ("MaxLOD", C.c_float)]


class BUFFER_DESC(C.Structure):
    _fields_ = [("ByteWidth", W.UINT), ("Usage", W.UINT), ("BindFlags", W.UINT), ("CPUAccessFlags", W.UINT),
                ("MiscFlags", W.UINT), ("StructureByteStride", W.UINT)]


class SUBRESOURCE_DATA(C.Structure):
    _fields_ = [("pSysMem", C.c_void_p), ("SysMemPitch", W.UINT), ("SysMemSlicePitch", W.UINT)]


class VIEWPORT(C.Structure):
    _fields_ = [(n, C.c_float) for n in ("TopLeftX", "TopLeftY", "Width", "Height", "MinDepth", "MaxDepth")]


class MAPPED(C.Structure):
    _fields_ = [("pData", C.c_void_p), ("RowPitch", W.UINT), ("DepthPitch", W.UINT)]


def _m(obj, idx, restype, *argtypes):
    vtbl = C.cast(obj, C.POINTER(C.POINTER(C.c_void_p)))[0]
    return C.WINFUNCTYPE(restype, C.c_void_p, *argtypes)(vtbl[idx])


def _ok(hr, what):
    if hr < 0:
        raise RuntimeError(f"{what} failed: HRESULT {hr & 0xFFFFFFFF:#010x}")


class Tex:
    def __init__(self, dev, w, h, fmt, bind, data=None):
        self.dev, self.w, self.h, self.fmt = dev, w, h, fmt
        self.ch = 4 if fmt == FMT_RGBA32F else 1
        self.tex, self.srv, self.rtv = C.c_void_p(), C.c_void_p(), C.c_void_p()
        d = TEX2D_DESC(w, h, 1, 1, fmt, 1, 0, 0, bind, 0, 0)
        init = None
        if data is not None:
            buf = np.ascontiguousarray(data, np.float32).reshape(h, w * self.ch)
            self._keep = buf
            init = C.byref(SUBRESOURCE_DATA(buf.ctypes.data, 4 * w * self.ch, 0))
        _ok(_m(dev.dev, 5, C.c_long, C.POINTER(TEX2D_DESC), C.c_void_p, C.POINTER(C.c_void_p))(
            dev.dev, C.byref(d), init, C.byref(self.tex)), "CreateTexture2D")
        if bind & BIND_SRV:
            _ok(_m(dev.dev, 7, C.c_long, C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p))(
                dev.dev, self.tex, None, C.byref(self.srv)), "CreateShaderResourceView")
        if bind & BIND_RTV:
            _ok(_m(dev.dev, 9, C.c_long, C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p))(
                dev.dev, self.tex, None, C.byref(self.rtv)), "CreateRenderTargetView")
        if bind:
            dev.objs += [o for o in (self.srv, self.rtv) if o.value]
        dev.objs.append(self.tex)

    def upload(self, data):
        buf = np.ascontiguousarray(data, np.float32).reshape(self.h, self.w * self.ch)
        _m(self.dev.ctx, 48, None, C.c_void_p, W.UINT, C.c_void_p, C.c_void_p, W.UINT, W.UINT)(
            self.dev.ctx, self.tex, 0, None, C.c_void_p(buf.ctypes.data), 4 * self.w * self.ch, 0)


class Device:
    def __init__(self):
        d3d = C.WinDLL("d3d11")
        self.dev, self.ctx, fl = C.c_void_p(), C.c_void_p(), W.UINT()
        _ok(d3d.D3D11CreateDevice(None, 5, None, 0, None, 0, 7, C.byref(self.dev), C.byref(fl), C.byref(self.ctx)),
            "D3D11CreateDevice(WARP)")
        self.objs = []
        self.vs = self.shader(compile_hlsl(VS_SRC, "vs_5_0", "warp_vs"), 12)
        self.samp = {}

    def shader(self, blob: bytes, slot: int):
        s = C.c_void_p()
        _ok(_m(self.dev, slot, C.c_long, C.c_char_p, C.c_size_t, C.c_void_p, C.POINTER(C.c_void_p))(
            self.dev, blob, len(blob), None, C.byref(s)), "Create*Shader")
        self.objs.append(s)
        return s

    def ps(self, blob: bytes):
        return self.shader(blob, 15)

    def sampler(self, spec):
        """spec: True = bilinear clamp, False = point clamp, or (linear, address mode) with
        D3D11_TEXTURE_ADDRESS_* 1 WRAP, 2 MIRROR, 3 CLAMP."""
        linear, addr = spec if isinstance(spec, tuple) else (spec, 3)
        key = (bool(linear), addr)
        if key not in self.samp:
            d = SAMPLER_DESC(0x15 if linear else 0, addr, addr, addr, 0.0, 1, 1, (C.c_float * 4)(0, 0, 0, 0), 0.0, 3.4e38)
            s = C.c_void_p()
            _ok(_m(self.dev, 23, C.c_long, C.POINTER(SAMPLER_DESC), C.POINTER(C.c_void_p))(
                self.dev, C.byref(d), C.byref(s)), "CreateSamplerState")
            self.objs.append(s)
            self.samp[key] = s
        return self.samp[key]

    def cbuffer(self, regs: np.ndarray):
        data = np.ascontiguousarray(regs, np.float32)
        b = C.c_void_p()
        _ok(_m(self.dev, 3, C.c_long, C.POINTER(BUFFER_DESC), C.POINTER(SUBRESOURCE_DATA), C.POINTER(C.c_void_p))(
            self.dev, C.byref(BUFFER_DESC(data.nbytes, 0, 4, 0, 0, 0)), C.byref(SUBRESOURCE_DATA(data.ctypes.data, 0, 0)),
            C.byref(b)), "CreateBuffer")
        self.objs.append(b)
        return b

    def draw(self, ps, rts, srvs: dict, samplers: dict, cb=None):
        ctx = self.ctx
        w, h = rts[0].w, rts[0].h
        arr = (C.c_void_p * len(rts))(*[r.rtv for r in rts])
        _m(ctx, 33, None, W.UINT, C.c_void_p, C.c_void_p)(ctx, len(rts), arr, None)
        _m(ctx, 44, None, W.UINT, C.POINTER(VIEWPORT))(ctx, 1, C.byref(VIEWPORT(0, 0, w, h, 0, 1)))
        _m(ctx, 24, None, W.UINT)(ctx, 4)
        _m(ctx, 11, None, C.c_void_p, C.c_void_p, W.UINT)(ctx, self.vs, None, 0)
        _m(ctx, 9, None, C.c_void_p, C.c_void_p, W.UINT)(ctx, ps, None, 0)
        n = max(srvs) + 1 if srvs else 0
        sv = (C.c_void_p * 8)(*[srvs[i].srv if i in srvs else None for i in range(8)])
        _m(ctx, 8, None, W.UINT, W.UINT, C.c_void_p)(ctx, 0, 8, sv)
        sm = (C.c_void_p * 8)(*[self.sampler(samplers[i]) if i in samplers else None for i in range(8)])
        _m(ctx, 10, None, W.UINT, W.UINT, C.c_void_p)(ctx, 0, 8, sm)
        if cb is not None:
            cba = (C.c_void_p * 1)(cb)
            _m(ctx, 16, None, W.UINT, W.UINT, C.c_void_p)(ctx, 0, 1, cba)
        _m(ctx, 13, None, W.UINT, W.UINT)(ctx, 6, 0)
        # unbind the targets so the same textures can be copied / read next
        nul = (C.c_void_p * 8)()
        _m(ctx, 33, None, W.UINT, C.c_void_p, C.c_void_p)(ctx, 0, None, None)
        _m(ctx, 8, None, W.UINT, W.UINT, C.c_void_p)(ctx, 0, 8, nul)
        return n

    def copy(self, dst: Tex, src: Tex):
        _m(self.ctx, 47, None, C.c_void_p, C.c_void_p)(self.ctx, dst.tex, src.tex)

    def read(self, t: Tex) -> np.ndarray:
        stg = C.c_void_p()
        d = TEX2D_DESC(t.w, t.h, 1, 1, t.fmt, 1, 0, 3, 0, 0x20000, 0)
        _ok(_m(self.dev, 5, C.c_long, C.POINTER(TEX2D_DESC), C.c_void_p, C.POINTER(C.c_void_p))(
            self.dev, C.byref(d), None, C.byref(stg)), "CreateTexture2D(staging)")
        _m(self.ctx, 47, None, C.c_void_p, C.c_void_p)(self.ctx, stg, t.tex)
        mp = MAPPED()
        _ok(_m(self.ctx, 14, C.c_long, C.c_void_p, W.UINT, W.UINT, W.UINT, C.POINTER(MAPPED))(
            self.ctx, stg, 0, 1, 0, C.byref(mp)), "Map")
        try:
            buf = (C.c_ubyte * (mp.RowPitch * t.h)).from_address(mp.pData)
            a = np.frombuffer(buf, np.float32).reshape(t.h, mp.RowPitch // 4)[:, :t.w * t.ch].reshape(t.h, t.w, t.ch).copy()
        finally:
            _m(self.ctx, 15, None, C.c_void_p, W.UINT)(self.ctx, stg, 0)
            _m(stg, 2, W.ULONG)(stg)
        return a

    def close(self):
        for o in reversed(self.objs):
            if o.value:
                _m(o, 2, W.ULONG)(o)
        for o in (self.ctx, self.dev):
            _m(o, 2, W.ULONG)(o)


def compile_hlsl(src: str, profile: str, tag: str, defines=()) -> bytes:
    WORK.mkdir(parents=True, exist_ok=True)
    p, o = WORK / f"{tag}.hlsl", WORK / f"{tag}.bin"
    p.write_text(src, newline="\n")
    cmd = [fxcasm.fxc_path(), "/nologo", "/T", profile, "/E", "main", "/I", str(FEATURES)]
    for d in defines:
        cmd += ["/D", d]
    cmd += ["/Fo", str(o), str(p)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"fxc {tag}: {r.stdout}{r.stderr}")
    return o.read_bytes()


# ---------------------------------------------------------------------------------------------
# the real ResolveTAA blob, frame by frame
def globals_regs(w: int, h: int) -> np.ndarray:
    r = np.zeros((88, 4), np.float32)
    r[8] = (0.1, 1000.0, -999.9, 100.0)
    r[12] = (w, h, 1.0 / w, 1.0 / h)
    r[56] = (0.0, 0, 0, 0)
    r[57] = (1.0, 1.0, 1.0, 0.0)
    r[58:62] = np.eye(4)
    r[74:78] = np.eye(4)
    return r


class TAARun:
    """One ResolveTAA pixel shader bound to a WARP device with a history that feeds back."""

    def __init__(self, dev: Device, blob: bytes, w: int, h: int, hist_filter: bool = True):
        self.dev, self.w, self.h = dev, w, h
        self.hist_filter = hist_filter                # s1: True = bilinear (assumed in game), False = point
        self.ps = dev.ps(blob)
        rgba = lambda data=None, b=BIND_SRV: Tex(dev, w, h, FMT_RGBA32F, b, data)  # noqa: E731
        self.cur = rgba(np.zeros((h, w, 4)))
        self.hist = rgba(np.zeros((h, w, 4)))
        self.depth = Tex(dev, w, h, FMT_R32F, BIND_SRV, np.full((h, w), 0.5))
        self.vel = rgba(np.zeros((h, w, 4)))
        mask = np.zeros((h, w, 4)); mask[..., 1] = 1.0
        self.mask = rgba(mask)
        self.o0 = rgba(None, BIND_RTV)
        self.o1 = rgba(None, BIND_RTV)
        self.cb = dev.cbuffer(globals_regs(w, h))

    def set_velocity_px(self, vx: float, vy: float):
        v = np.zeros((self.h, self.w, 4), np.float32)
        v[..., 0], v[..., 1] = vx / self.w, -vy / self.h
        self.vel.upload(v)

    def frame(self, cur_rgb: np.ndarray, hist_rgb: np.ndarray | None = None):
        c = np.ones((self.h, self.w, 4), np.float32); c[..., :3] = cur_rgb
        self.cur.upload(c)
        if hist_rgb is not None:
            hh = np.ones((self.h, self.w, 4), np.float32); hh[..., :3] = hist_rgb
            self.hist.upload(hh)
        self.dev.draw(self.ps, [self.o0, self.o1], {0: self.cur, 1: self.hist, 2: self.depth, 3: self.vel, 4: self.mask},
                      {1: self.hist_filter, 2: False, 3: False, 4: True}, self.cb)
        self.dev.copy(self.hist, self.o0)            # SV_Target0 is next frame's history
        return self.dev.read(self.o0)[..., :3], self.dev.read(self.o1)[..., :2]


# ---------------------------------------------------------------------------------------------
# scenes: f(x, y) -> rgb, x/y in pixel units (pixel centres at i + 0.5)
def halton(i: int, b: int) -> float:
    f, r = 1.0, 0.0
    while i > 0:
        f /= b; r += f * (i % b); i //= b
    return r


JITTER = [(halton(i + 1, 2) - 0.5, halton(i + 1, 3) - 0.5) for i in range(8)]


def grey(v):
    v = np.asarray(v, np.float32)
    return np.repeat(v[..., None], 3, axis=-1)


def smooth_noise(n: int, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    a = rng.standard_normal((n, n))
    k = np.fft.fftfreq(n)
    kk = np.sqrt(k[None, :] ** 2 + k[:, None] ** 2)
    spec = np.fft.fft2(a) * np.exp(-(kk / 0.18) ** 2)      # band-limited, detail around 4-8 px
    t = np.real(np.fft.ifft2(spec))
    return (t / np.abs(t).max()).astype(np.float32)


def make_scene(name: str, n: int):
    """Returns f(X, Y) evaluated at arbitrary pixel-space coordinates (arrays)."""
    tex = smooth_noise(n)

    def bilerp_tex(X, Yy):  # periodic smooth texture lookup
        x0, y0 = np.floor(X - 0.5).astype(int), np.floor(Yy - 0.5).astype(int)
        fx, fy = X - 0.5 - x0, Yy - 0.5 - y0
        g = lambda xx, yy: tex[yy % n, xx % n]  # noqa: E731
        return ((1 - fx) * (1 - fy) * g(x0, y0) + fx * (1 - fy) * g(x0 + 1, y0)
                + (1 - fx) * fy * g(x0, y0 + 1) + fx * fy * g(x0 + 1, y0 + 1))

    c = n / 2 + 0.3
    if name == "hard_edge":
        return lambda X, Yy: grey(np.where(X < c, 0.05, 0.8))
    if name == "soft_edge":
        return lambda X, Yy: grey(0.1 + 0.4 / (1 + np.exp(-(X - c) / 0.9)))
    if name == "checker_hi":
        return lambda X, Yy: grey(np.where((np.floor(X) + np.floor(Yy)) % 2 == 0, 0.1, 0.6))
    if name == "checker_lo":
        return lambda X, Yy: grey(np.where((np.floor(X) + np.floor(Yy)) % 2 == 0, 0.30, 0.36))
    if name == "lines":
        return lambda X, Yy: grey(np.where(np.floor(X) % 4 == 1, 0.7, 0.1))
    if name == "flat":
        return lambda X, Yy: grey(np.full_like(X, 0.3))
    if name == "texture":
        return lambda X, Yy: grey(0.25 + 0.06 * bilerp_tex(X, Yy))
    if name == "hdr":
        def f(X, Yy):
            v = np.full(X.shape, 0.1, np.float32)
            v = np.where((np.abs(X - (n * 0.3)) < 1.0) & (np.abs(Yy - n * 0.3) < 1.0), 30.0, v)      # 2x2 block 30
            v = np.where((np.floor(X) == int(n * 0.6)) & (np.floor(Yy) == int(n * 0.4)), 50.0, v)  # single px 50
            v = np.where((np.abs(X - n * 0.5) < 1.5) & (Yy > n * 0.65), 10.0, v)                  # 3-px bar 10
            return grey(v)
        return f
    if name == "hdr_color":
        def f(X, Yy):
            rgb = np.zeros(X.shape + (3,), np.float32); rgb[...] = (0.08, 0.06, 0.05)
            m = (np.abs(X - c) < 1.0) & (np.abs(Yy - c) < 1.0)
            rgb[m] = (50.0, 4.0, 0.2)
            m2 = (np.floor(X) == int(n * 0.25)) & (np.floor(Yy) == int(n * 0.7))
            rgb[m2] = (0.5, 12.0, 40.0)
            return rgb
        return f
    # moving-content scenes (band-limited so point sampling at sub-pixel positions is meaningful)
    if name == "grating4":
        return lambda X, Yy: grey(0.3 + 0.2 * np.sin(2 * np.pi * X / 4.0))
    if name == "grating8":
        return lambda X, Yy: grey(0.3 + 0.2 * np.sin(2 * np.pi * X / 8.0))
    if name == "edge_aa":
        return lambda X, Yy: grey(0.05 + 0.75 / (1 + np.exp(-(X - c) / 0.35)))
    if name == "thin_lines":
        return lambda X, Yy: grey(0.1 + 0.6 * np.exp(-0.5 * ((((X - 1.5) % 4.0) - 2.0) / 0.45) ** 2))
    if name == "hdr_blob":
        return lambda X, Yy: grey(0.1 + 40.0 * np.exp(-0.5 * (((X - c) ** 2 + (Yy - c) ** 2) / 0.6 ** 2)))
    raise KeyError(name)


def sample(f, n: int, dx: float = 0.0, dy: float = 0.0) -> np.ndarray:
    """Point-sample the scene at pixel centres displaced by (dx, dy) pixels."""
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float64)
    return f(xx + 0.5 + dx, yy + 0.5 + dy).astype(np.float32)


# ---------------------------------------------------------------------------------------------
# numpy model of the vanilla 3x3 box (m-space), for the boundedness check
def to_m(c):
    return c / (1.0 + c @ Y)[..., None]


def rounded_box(cur: np.ndarray):
    m = to_m(cur.astype(np.float64))
    p = np.pad(m, ((1, 1), (1, 1), (0, 0)))     # out-of-range Loads return 0
    h, w = cur.shape[:2]
    at = lambda dx, dy: p[1 + dy:1 + dy + h, 1 + dx:1 + dx + w]  # noqa: E731
    cross = [at(0, 0), at(0, -1), at(0, 1), at(-1, 0), at(1, 0)]
    diag = [at(-1, -1), at(1, -1), at(-1, 1), at(1, 1)]
    cmin, cmax = np.min(cross, 0), np.max(cross, 0)
    fmin, fmax = np.minimum(cmin, np.min(diag, 0)), np.maximum(cmax, np.max(diag, 0))
    return (fmin + cmin) * 0.5, (cmax + fmax) * 0.5


def box_violation(out: np.ndarray, cur: np.ndarray) -> float:
    lo, hi = rounded_box(cur)
    m = to_m(out.astype(np.float64))
    return float(max((lo - m).max(), (m - hi).max(), 0.0))


# ---------------------------------------------------------------------------------------------
def run_static(dev, blobs, scene, n, frames=96, tail=16):
    """Static camera, 8-phase Halton jitter of the current frame, zero velocity.
    The history starts as the mean of the 8 jitter phases (a converged TAA state). Starting from a
    raw frame would freeze binary content: vanilla's blend weight is proportional to the distance
    of the history luma from the nearest box edge, so a history sitting exactly on an extreme
    (any raw sample of two-level content) gets blend 0 and never moves."""
    f = make_scene(scene, n)
    cur_frames = [sample(f, n, *JITTER[i % 8]) for i in range(frames)]
    hist0 = np.mean(cur_frames[:8], axis=0)
    res = {}
    for cfg, blob in blobs.items():
        r = TAARun(dev, blob, n, n)
        r.set_velocity_px(0.0, 0.0)
        outs = []
        for i, cf in enumerate(cur_frames):
            o0, o1 = r.frame(cf, hist0 if i == 0 else None)
            if i >= frames - tail:
                outs.append(o0)
        res[cfg] = {"outs": np.array(outs), "o1": o1, "last_cur": cur_frames[-1]}
    return res


def run_static_jitter_vel(dev, blobs, scene, n, frames=128, tail=32):
    """Static camera whose engine velocity INCLUDES the jitter (review finding 1): before frame i
    the velocity is set to jitter[i-1] - jitter[i], so histUV = uv - v lands on the previous frame's
    jittered sample of the same scene point and the history is resampled at a sub-pixel offset
    every frame although nothing moves. Whether DSR's velocity contains the jitter is unknown
    offline (in-game check 1). History seeded with the 8-phase jitter mean, which is also the
    reference image."""
    f = make_scene(scene, n)
    jit = [JITTER[i % 8] for i in range(frames)]
    cur_frames = [sample(f, n, *jit[i]) for i in range(frames)]
    ref = np.mean([sample(f, n, *JITTER[k]) for k in range(8)], axis=0)
    res = {}
    for cfg, blob in blobs.items():
        r = TAARun(dev, blob, n, n)
        outs = []
        for i, cf in enumerate(cur_frames):
            pj = JITTER[(i - 1) % 8]
            r.set_velocity_px(pj[0] - jit[i][0], pj[1] - jit[i][1])
            o0, _ = r.frame(cf, ref if i == 0 else None)
            if i >= frames - tail:
                outs.append((o0, cf))
        res[cfg] = outs
    return res, ref


def hp_rms(L: np.ndarray) -> float:
    """rms of the 4-neighbour high-pass of a luma image (interior), the detail-energy proxy."""
    hp = L - 0.25 * (np.roll(L, 1, 0) + np.roll(L, -1, 0) + np.roll(L, 1, 1) + np.roll(L, -1, 1))
    return float(np.sqrt((hp[2:-2, 2:-2] ** 2).mean()))


def summarise_jitter_vel(res, ref):
    """flicker = per-pixel temporal std of the OUTPUT luma over the tail (a true output-flicker
    metric), with the raw jittered input's own temporal std as scale; hp_rms = mean per-frame
    detail energy; rmse against the 8-phase jitter-mean reference and against each frame's own
    jittered truth."""
    core = (slice(6, -6), slice(6, -6))
    Lref = lum(ref)[core]
    out = {}
    for cfg, frames in res.items():
        L = np.array([lum(o0)[core] for o0, _ in frames])
        Lt = np.array([lum(t)[core] for _, t in frames])
        out[cfg] = {"flicker_output_temporal_std": float(L.std(axis=0).mean()),
                    "input_temporal_std": float(Lt.std(axis=0).mean()),
                    "hp_rms": float(np.mean([hp_rms(x) for x in L])),
                    "hp_rms_ref": hp_rms(Lref),
                    "rmse_vs_jitter_mean_ref": float(np.sqrt(((L - Lref) ** 2).mean(axis=(1, 2))).mean()),
                    "rmse_vs_frame_truth": float(np.sqrt(((L - Lt) ** 2).mean(axis=(1, 2))).mean()),
                    "box_violation_m": max(box_violation(o0, t) for o0, t in frames),
                    "nan_inf": int(sum((~np.isfinite(o0)).sum() for o0, _ in frames))}
    for cfg, e in out.items():
        for k in ("flicker_output_temporal_std", "hp_rms", "rmse_vs_jitter_mean_ref", "rmse_vs_frame_truth"):
            base = out["ident"][k]
            e[k + "_ratio"] = e[k] / base if base > 0 else None
    return out


def run_moving(dev, blobs, scene, n, v=(0.37, 0.21), frames=48, tail=8, jitter=False, hist_filter=True):
    """Content moving by v px/frame (object velocity): the history is fetched at a constant
    sub-pixel offset every frame and the blur (or not) compounds through the feedback.
    jitter=True adds the 8-phase sub-pixel jitter to the current frames (velocity excludes it).
    hist_filter=False binds s1 as a POINT sampler (the engine's s1 state is not observed offline)."""
    f = make_scene(scene, n)
    jit = (lambda i: JITTER[i % 8]) if jitter else (lambda i: (0.0, 0.0))
    cur = [sample(f, n, -v[0] * i + jit(i)[0], -v[1] * i + jit(i)[1]) for i in range(frames)]
    res = {}
    for cfg, blob in blobs.items():
        r = TAARun(dev, blob, n, n, hist_filter)
        r.set_velocity_px(*v)
        outs = []
        for i, cf in enumerate(cur):
            o0, _ = r.frame(cf, cur[0] if i == 0 else None)
            if i >= frames - tail:
                outs.append((o0, cf))
        res[cfg] = outs
    return res


def lum(a):
    return a @ Y


def profile_metrics(out_mean: np.ndarray, n: int, lo: float, hi: float, row_band=slice(None)):
    """Edge metrics on the column profile (vertical structures): max |gradient| and overshoot."""
    prof = lum(out_mean[row_band]).mean(axis=0)
    g = np.abs(np.diff(prof)).max()
    return {"max_grad": float(g), "overshoot": float(max(prof.max() - hi, 0.0)),
            "undershoot": float(max(lo - prof.min(), 0.0))}


def summarise_static(res, scene, n):
    ident = res["ident"]
    lo = float(lum(ident["last_cur"]).min()); hi = float(lum(ident["last_cur"]).max())
    inner = slice(4, n - 4)
    out = {}
    for cfg, d in res.items():
        o = d["outs"]
        mean = o.mean(axis=0)
        e = {"nan_inf": int((~np.isfinite(o)).sum()),
             "box_violation_m": box_violation(o[-1], d["last_cur"]),
             "temporal_std": float(lum(o).std(axis=0)[inner, inner].mean()),
             "max_abs_diff_vs_ident": float(np.abs(o[-1] - ident["outs"][-1]).max()),
             "o1_equal_ident": bool(np.array_equal(np.nan_to_num(d["o1"], nan=-7, posinf=-8), np.nan_to_num(ident["o1"], nan=-7, posinf=-8)))}
        if scene in ("hard_edge", "soft_edge", "lines", "checker_hi", "checker_lo", "texture"):
            e.update(profile_metrics(mean[inner, inner], n, lo, hi))
            # detail energy: variance of the high-passed time-mean luma (sharpness proxy)
            L = lum(mean)[inner, inner]
            hp = L - 0.25 * (np.roll(L, 1, 0) + np.roll(L, -1, 0) + np.roll(L, 1, 1) + np.roll(L, -1, 1))
            e["hp_rms"] = float(np.sqrt((hp[2:-2, 2:-2] ** 2).mean()))
        if scene in ("hard_edge", "soft_edge"):
            prof = lum(mean[inner, inner]).mean(axis=0)
            a, b = prof.min(), prof.max()
            lo10, hi90 = a + 0.1 * (b - a), a + 0.9 * (b - a)
            xs = np.arange(len(prof), dtype=np.float64)
            # 10-90 rise distance in pixels, linear interpolation on the monotone profile
            e["rise_10_90_px"] = float(np.interp(hi90, prof, xs) - np.interp(lo10, prof, xs))
        if scene.startswith("hdr"):
            e["max_out"] = float(o.max()); e["min_out"] = float(o.min())
        out[cfg] = e
    for cfg, e in out.items():
        for k in ("max_grad", "hp_rms", "temporal_std", "rise_10_90_px"):
            if k in e and out["ident"].get(k):
                e[k + "_ratio"] = e[k] / out["ident"][k] if out["ident"][k] > 0 else None
    return out


def summarise_moving(res, scene):
    """error_spread_vs_truth: per-pixel spread over the tail of (output - truth), i.e. how
    consistently the output tracks the moving truth (NOT output flicker; the content moves).
    pulsing: frame-to-frame relative variation of the output's spatial luma std (detail energy),
    a true flicker metric for moving content; truth_pulsing is the same for the point-sampled
    truth (non-zero for content near the pixel Nyquist, e.g. 1-px lines). peak_pulsing: the same
    for the p99-p1 range, which also moves with the sampling phase of the truth itself (a 4-px
    sine sampled at alternating half-pixel phases peaks at 0.71 and 1.0 of its amplitude), so read
    it against truth_peak_pulsing. hp_ratio_vs_truth:
    output / truth high-pass energy (> 1 = sharper than the truth, i.e. over-sharpened)."""
    out = {}
    for cfg, frames in res.items():
        err, amp_o, amp_t, over, nan, viol, sd_o, sd_t, hpr = [], [], [], [], 0, 0.0, [], [], []
        for o0, truth in frames:
            core = (slice(6, -6), slice(6, -6))
            Lo, Lt = lum(o0[core]), lum(truth[core])
            err.append(np.sqrt(((Lo - Lt) ** 2).mean()))
            amp_o.append(np.percentile(Lo, 99) - np.percentile(Lo, 1))
            amp_t.append(np.percentile(Lt, 99) - np.percentile(Lt, 1))
            over.append(max(Lo.max() - Lt.max(), Lt.min() - Lo.min(), 0.0))
            nan += int((~np.isfinite(o0)).sum())
            viol = max(viol, box_violation(o0, truth))
            sd_o.append(Lo.std()); sd_t.append(Lt.std())
            ht = hp_rms(Lt)
            hpr.append(hp_rms(Lo) / ht if ht > 0 else np.nan)
        out[cfg] = {"rmse_vs_truth": float(np.mean(err)), "contrast_retention": float(np.mean(amp_o) / np.mean(amp_t)),
                    "ringing": float(np.max(over)), "nan_inf": nan, "box_violation_m": viol,
                    "error_spread_vs_truth": float(np.std([lum(o0[6:-6, 6:-6]) - lum(t[6:-6, 6:-6]) for o0, t in frames], axis=0).mean()),
                    "pulsing": float(np.std(sd_o) / np.mean(sd_o)) if np.mean(sd_o) > 0 else None,
                    "truth_pulsing": float(np.std(sd_t) / np.mean(sd_t)) if np.mean(sd_t) > 0 else None,
                    "peak_pulsing": float(np.std(amp_o) / np.mean(amp_o)) if np.mean(amp_o) > 0 else None,
                    "truth_peak_pulsing": float(np.std(amp_t) / np.mean(amp_t)) if np.mean(amp_t) > 0 else None,
                    "hp_ratio_vs_truth": float(np.nanmean(hpr)) if np.isfinite(hpr).any() else None}
        if scene.startswith("hdr"):
            # small moving highlight: peak and energy (sum of luma above the 0.1 background) vs truth
            pk = [lum(o0).max() / lum(t).max() for o0, t in frames]
            en = [(lum(o0) - 0.1).clip(0).sum() / (lum(t) - 0.1).clip(0).sum() for o0, t in frames]
            out[cfg].update(peak_retention=float(np.mean(pk)), energy_ratio=float(np.mean(en)),
                            min_out=float(min(o0.min() for o0, _ in frames)))
            del out[cfg]["contrast_retention"]
    return out


# ---------------------------------------------------------------------------------------------
# kernel harness
def kernel_fetch(dev, n=64):
    """One history fetch at a sub-pixel offset: bilinear vs Catmull-Rom (9 / 5 taps) against the
    exactly shifted truth; plus the per-frequency response of one resample."""
    blobs = {"bilinear": compile_hlsl(FETCH_PS, "ps_5_0", "fetch_bil", ["MODE=0", "_FRAGMENT_SHADER=1", "DSRLE_TAA_CATMULL"]),
             "cr9": compile_hlsl(FETCH_PS, "ps_5_0", "fetch_cr9", ["MODE=1", "_FRAGMENT_SHADER=1", "DSRLE_TAA_CATMULL"]),
             "cr5": compile_hlsl(FETCH_PS, "ps_5_0", "fetch_cr5", ["MODE=1", "_FRAGMENT_SHADER=1", "DSRLE_TAA_CATMULL",
                                                                   "DSRLE_TAA_CATMULL_TAPS=5"]),
             "cr16": compile_hlsl(FETCH_PS, "ps_5_0", "fetch_cr16", ["MODE=1", "_FRAGMENT_SHADER=1", "DSRLE_TAA_CATMULL",
                                                                     "DSRLE_TAA_CATMULL_TAPS=16"])}
    out = {}
    for scene in ("grating4", "grating8", "edge_aa", "thin_lines", "hdr_blob", "hard_edge", "flat"):
        f = make_scene(scene, n)
        hist = sample(f, n)
        for off in ((0.5, 0.0), (0.37, 0.21), (0.0, 0.0)):
            truth = sample(f, n, -off[0], -off[1])
            src = Tex(dev, n, n, FMT_RGBA32F, BIND_SRV, np.concatenate([hist, np.ones((n, n, 1), np.float32)], -1))
            rt = Tex(dev, n, n, FMT_RGBA32F, BIND_RTV)
            cb = dev.cbuffer(np.array([[off[0], off[1], 0, 0]], np.float32))
            row = {}
            for k, ps in blobs.items():
                dev.draw(dev.ps(ps), [rt], {0: src}, {0: True}, cb)
                o = dev.read(rt)[..., :3]
                core = (slice(4, -4), slice(4, -4))
                Lo, Lt = lum(o[core]), lum(truth[core])
                rt_ = np.percentile(Lt, 99.5) - np.percentile(Lt, 0.5)
                row[k] = {"rmse": float(np.sqrt(((Lo - Lt) ** 2).mean())),
                          "contrast": float((np.percentile(Lo, 99.5) - np.percentile(Lo, 0.5)) / rt_) if rt_ > 1e-6 else None,
                          "min": float(o.min()), "max": float(o.max()), "nan_inf": int((~np.isfinite(o)).sum()),
                          "border_max_abs_err": float(np.abs(lum(o) - lum(truth))[[0, -1], :].max())}
            out[f"{scene}@{off[0]:.2f},{off[1]:.2f}"] = row
    # analytic response of one resample at a half-texel offset (per axis) and repeated N times
    def resp(wts, offs, w):
        return abs(sum(a * np.exp(-1j * w * o) for a, o in zip(wts, offs)))
    fr = 0.5
    cr = [fr * (-0.5 + fr * (1 - 0.5 * fr)), 1 + fr * fr * (-2.5 + 1.5 * fr), fr * (0.5 + fr * (2 - 1.5 * fr)), fr * fr * (-0.5 + 0.5 * fr)]
    ana = {}
    for name, w in (("w=pi/4 (8 px)", np.pi / 4), ("w=pi/2 (4 px)", np.pi / 2), ("w=3pi/4", 3 * np.pi / 4)):
        b = resp([0.5, 0.5], [0, 1], w); c = resp(cr, [-1, 0, 1, 2], w)
        ana[name] = {"bilinear": round(float(b), 4), "catmull_rom": round(float(c), 4),
                     "bilinear^16": float(b ** 16), "catmull_rom^16": float(c ** 16)}
    # worst-case gain of one CR resample over all offsets and frequencies (> 1 would mean growth)
    worst = max(resp([ff * (-0.5 + ff * (1 - 0.5 * ff)), 1 + ff * ff * (-2.5 + 1.5 * ff), ff * (0.5 + ff * (2 - 1.5 * ff)),
                      ff * ff * (-0.5 + 0.5 * ff)], [-1, 0, 1, 2], w)
                for ff in np.linspace(0, 1, 101) for w in np.linspace(0, np.pi, 181))
    ana["catmull_rom_max_gain_any_offset_any_freq"] = float(worst)

    def tap_resp(f, w):  # per-axis responses of the three bilinear tap columns: texel -1, merged inner, texel +2
        a = [f * (-0.5 + f * (1 - 0.5 * f)), 1 + f * f * (-2.5 + 1.5 * f), f * (0.5 + f * (2 - 1.5 * f)), f * f * (-0.5 + 0.5 * f)]
        e = np.exp(-1j * w * np.array([-1, 0, 1, 2]))
        return [a[0] * e[0], a[1] * e[1] + a[2] * e[2], a[3] * e[3]], a
    g9 = g5 = 0.0
    for fx in np.linspace(0, 1, 21):
        for fy in np.linspace(0, 1, 21):
            for ox in np.linspace(0, np.pi, 19):
                for oy in np.linspace(0, np.pi, 19):
                    (ax, wx), (ay, wy) = tap_resp(fx, ox), tap_resp(fy, oy)
                    g9 = max(g9, abs(sum(ax) * sum(ay)))
                    s5 = ax[1] * (ay[0] + ay[1] + ay[2]) + ay[1] * (ax[0] + ax[2])
                    ws = (wx[1] + wx[2]) * sum(wy) + (wy[1] + wy[2]) * (wx[0] + wx[3])
                    g5 = max(g5, abs(s5 / ws))
    ana["max_gain_2d_9tap"], ana["max_gain_2d_5tap"] = float(g9), float(g5)

    # 9-tap response when the hardware quantises the merged tap's bilinear fraction to 8 bits, and
    # when s1 is a POINT sampler (the merged inner tap then snaps to the nearer texel while the two
    # outer lobes stay): per axis, separable, so the 2-D worst case is the square.
    def cr_w(f):
        return [f * (-0.5 + f * (1 - 0.5 * f)), 1 + f * f * (-2.5 + 1.5 * f), f * (0.5 + f * (2 - 1.5 * f)), f * f * (-0.5 + 0.5 * f)]
    g8 = g8t = gp = 0.0
    gp_at = None
    w = np.linspace(0, np.pi, 361)
    e1, em1, em2 = np.exp(1j * w), np.exp(-1j * w), np.exp(-2j * w)
    for ff in np.linspace(0, 1, 801):
        a = cr_w(ff); win = a[1] + a[2]; x = a[2] / win
        q, qt = round(x * 256) / 256, math.floor(x * 256) / 256      # round-to-nearest / truncating hardware
        snap = 1.0 if x >= 0.5 else 0.0
        g8 = max(g8, float(np.abs(a[0] * e1 + win * ((1 - q) + q * em1) + a[3] * em2).max()))
        g8t = max(g8t, float(np.abs(a[0] * e1 + win * ((1 - qt) + qt * em1) + a[3] * em2).max()))
        hp = np.abs(a[0] * e1 + win * np.exp(-1j * w * snap) + a[3] * em2)
        if hp.max() > gp:
            gp, gp_at = float(hp.max()), (float(ff), float(w[hp.argmax()]))
    ana["max_gain_1d_9tap_8bit_fraction"] = float(g8)
    ana["max_gain_1d_9tap_8bit_fraction_truncating"] = float(g8t)
    ana["max_gain_1d_9tap_point_sampler"] = float(gp)
    ana["max_gain_2d_9tap_point_sampler"] = float(gp * gp)
    ana["max_gain_1d_9tap_point_sampler_at_f_w"] = gp_at
    ana["note_point_sampler"] = ("gain > 1/(1-blend) (~1.06 at blend 0.06) means the history is re-sharpened every "
                                 "frame under motion until the 3x3 box clamp stops it")

    # sampler-filter independence: the same fetches with a POINT sampler on the history slot.
    # The 16-tap fallback (texel-centre taps) must give identical bytes; the 9-tap form and bilinear change.
    filt = {}
    src = Tex(dev, n, n, FMT_RGBA32F, BIND_SRV, np.concatenate([sample(make_scene("grating4", n), n),
                                                                np.ones((n, n, 1), np.float32)], -1))
    rt = Tex(dev, n, n, FMT_RGBA32F, BIND_RTV)
    for off in ((0.5, 0.0), (0.37, 0.21), (0.0, 0.0)):
        cb = dev.cbuffer(np.array([[off[0], off[1], 0, 0]], np.float32))
        got = {}
        for k, ps in blobs.items():
            res = {}
            for mode, spec in (("bilinear", (True, 3)), ("point", (False, 3))):
                dev.draw(dev.ps(ps), [rt], {0: src}, {0: spec}, cb)
                res[mode] = dev.read(rt)
            got[k] = res
            filt[f"{k}@{off[0]:.2f},{off[1]:.2f}"] = {
                "point_equals_bilinear_bytes": bool(np.array_equal(res["point"], res["bilinear"])),
                "point_vs_bilinear_max_abs": float(np.abs(res["point"] - res["bilinear"]).max())}
        filt[f"cr16_vs_cr9_bilinear_max_abs@{off[0]:.2f},{off[1]:.2f}"] = float(
            np.abs(got["cr16"]["bilinear"] - got["cr9"]["bilinear"]).max())
    # the same at the game's width (3840 is not a power of two, so the texel-centre uvs of the
    # 16-tap form carry float rounding there): a 3840 x 4 strip of a 4-px grating
    ww, hh = 3840, 4
    xx = np.arange(ww, dtype=np.float64)[None, :].repeat(hh, 0)
    strip = grey(0.3 + 0.2 * np.sin(2 * np.pi * (xx + 0.5) / 4.0) + 0.05 * np.arange(hh)[:, None])
    src = Tex(dev, ww, hh, FMT_RGBA32F, BIND_SRV, np.concatenate([strip, np.ones((hh, ww, 1), np.float32)], -1))
    rt = Tex(dev, ww, hh, FMT_RGBA32F, BIND_RTV)
    for off in ((0.5, 0.0), (0.37, 0.21), (0.11, -0.43)):
        cb = dev.cbuffer(np.array([[off[0], off[1], 0, 0]], np.float32))
        for k in ("cr9", "cr16"):
            res = {}
            for mode, spec in (("bilinear", (True, 3)), ("point", (False, 3))):
                dev.draw(dev.ps(blobs[k]), [rt], {0: src}, {0: spec}, cb)
                res[mode] = dev.read(rt)
            filt[f"w3840:{k}@{off[0]:.2f},{off[1]:.2f}"] = {
                "point_equals_bilinear_bytes": bool(np.array_equal(res["point"], res["bilinear"])),
                "point_vs_bilinear_max_abs": float(np.abs(res["point"] - res["bilinear"]).max())}

    # addressing independence: the same fetches with a WRAP and a MIRROR sampler on the history slot.
    # Content differs strongly between the left and right border (bright left, dark right) so any wrap
    # shows. Catmull-Rom clamps its tap uvs itself and must give identical bytes; bilinear may not.
    yy, xx = np.mgrid[0:n, 0:n]
    hist = grey(np.where(xx < n // 2, 2.0, 0.05) + 0.3 * (yy < n // 2))
    src = Tex(dev, n, n, FMT_RGBA32F, BIND_SRV, np.concatenate([hist, np.ones((n, n, 1), np.float32)], -1))
    rt = Tex(dev, n, n, FMT_RGBA32F, BIND_RTV)
    addr = {}
    for off in ((0.5, 0.0), (-0.5, 0.3), (0.37, -0.21)):
        cb = dev.cbuffer(np.array([[off[0], off[1], 0, 0]], np.float32))
        for k, ps in blobs.items():
            res = {}
            for mode, code in (("clamp", 3), ("wrap", 1), ("mirror", 2)):
                dev.draw(dev.ps(ps), [rt], {0: src}, {0: (True, code)}, cb)
                res[mode] = dev.read(rt)
            addr[f"{k}@{off[0]:.2f},{off[1]:.2f}"] = {
                "wrap_equals_clamp_bytes": bool(np.array_equal(res["wrap"], res["clamp"])),
                "mirror_equals_clamp_bytes": bool(np.array_equal(res["mirror"], res["clamp"])),
                "wrap_vs_clamp_max_abs": float(np.abs(res["wrap"] - res["clamp"]).max())}
    return {"fetch": out, "response_half_texel": ana, "address_mode_independence": addr,
            "sampler_filter_independence": filt}


def kernel_sharpen(dev, n=64, amounts=(0.3, 0.5, 1.0)):
    out = {}
    for amt in amounts:
        ps = dev.ps(compile_hlsl(SHARPEN_PS, "ps_5_0", f"sharp_{amt}",
                                 ["_FRAGMENT_SHADER=1", "DSRLE_TAA_SHARPEN", f"DSRLE_TAA_SHARPEN_AMOUNT={amt}"]))
        for scene in ("soft_edge", "edge_aa", "hard_edge", "texture", "checker_lo", "checker_hi", "lines", "flat", "hdr", "hdr_color"):
            f = make_scene(scene, n)
            cur = sample(f, n)
            src = Tex(dev, n, n, FMT_RGBA32F, BIND_SRV, np.concatenate([cur, np.ones((n, n, 1), np.float32)], -1))
            rt = Tex(dev, n, n, FMT_RGBA32F, BIND_RTV)
            dev.draw(ps, [rt], {0: src}, {})
            o = dev.read(rt)[..., :3]
            core = (slice(4, -4), slice(4, -4))
            Lo, Lc = lum(o[core]), lum(cur[core])
            gC, gO = np.abs(np.diff(Lc, axis=1)).max(), np.abs(np.diff(Lo, axis=1)).max()
            hpC = Lc - 0.25 * (np.roll(Lc, 1, 0) + np.roll(Lc, -1, 0) + np.roll(Lc, 1, 1) + np.roll(Lc, -1, 1))
            hpO = Lo - 0.25 * (np.roll(Lo, 1, 0) + np.roll(Lo, -1, 0) + np.roll(Lo, 1, 1) + np.roll(Lo, -1, 1))
            out[f"{scene}@{amt}"] = {
                "edge_grad_gain": float(gO / gC) if gC > 0 else None,
                "hp_rms_gain": float(np.sqrt((hpO[2:-2, 2:-2] ** 2).mean() / max((hpC[2:-2, 2:-2] ** 2).mean(), 1e-20))),
                "overshoot_vs_scene_range": float(max(Lo.max() - Lc.max(), Lc.min() - Lo.min(), 0.0)),
                "box_violation_m": box_violation(o, cur),
                "max_abs_change": float(np.abs(o - cur).max()),
                "nan_inf": int((~np.isfinite(o)).sum())}
    return out


SHARPEN_AMOUNT = {"s30": 0.3, "all": 0.3, "all_s50": 0.5}   # config name -> DSRLE_TAA_SHARPEN_AMOUNT


def sharpen_once(dev, img: np.ndarray, amt: float) -> np.ndarray:
    """One application of the feature's sharpen function at blend 0 (the kernel harness)."""
    n = img.shape[0]
    ps = dev.ps(compile_hlsl(SHARPEN_PS, "ps_5_0", f"sharp_{amt}",
                             ["_FRAGMENT_SHADER=1", "DSRLE_TAA_SHARPEN", f"DSRLE_TAA_SHARPEN_AMOUNT={amt}"]))
    src = Tex(dev, n, n, FMT_RGBA32F, BIND_SRV, np.concatenate([img, np.ones((n, n, 1), np.float32)], -1))
    rt = Tex(dev, n, n, FMT_RGBA32F, BIND_RTV)
    dev.draw(ps, [rt], {0: src}, {})
    return dev.read(rt)[..., :3]


def sharpen_buildup(dev, blobs, n, frames=400, marks=(8, 16, 32, 64, 128, 256, 400)):
    """How fast the sharpening reaches its target when standing still (review finding 2).
    no_jitter: the history is seeded with the still frame c itself; share(t) = projection of
    (out_t - c) onto (S(c) - c), S = one application at blend 0 (1 = the full single-application
    target; the real fixed point is a few % lower because k carries (1 - blend)).
    jitter: 8-phase jitter, history seeded with the jitter mean; per 8-frame window the projection
    of (window mean - identity's window mean) onto the same difference averaged over the last 64
    frames (1 = the steady state). At 60 fps, 32 frames = 0.53 s."""
    f = make_scene("texture", n)
    still = sample(f, n)
    core = (slice(6, -6), slice(6, -6))
    cfgs = [c for c in blobs if c in SHARPEN_AMOUNT]
    out = {"frames_per_second_assumed": 60, "no_jitter": {}, "jitter": {}}
    tgt = {a: sharpen_once(dev, still, a) for a in sorted({SHARPEN_AMOUNT[c] for c in cfgs})}
    Ls = lum(still)[core]
    for cfg in cfgs:
        dT = lum(tgt[SHARPEN_AMOUNT[cfg]])[core] - Ls
        r = TAARun(dev, blobs[cfg], n, n); r.set_velocity_px(0.0, 0.0)
        share = {}
        for i in range(frames):
            o0, _ = r.frame(still, still if i == 0 else None)
            if i + 1 in marks:
                share[str(i + 1)] = float(((lum(o0)[core] - Ls) * dT).sum() / (dT * dT).sum())
        out["no_jitter"][cfg] = share
    cf = [sample(f, n, *JITTER[i % 8]) for i in range(frames)]
    hist0 = np.mean(cf[:8], axis=0)
    win = {}
    for cfg in ["ident", *cfgs]:
        r = TAARun(dev, blobs[cfg], n, n); r.set_velocity_px(0.0, 0.0)
        acc, means = [], []
        for i, c in enumerate(cf):
            o0, _ = r.frame(c, hist0 if i == 0 else None)
            acc.append(lum(o0)[core])
            if len(acc) == 8:
                means.append(np.mean(acc, axis=0)); acc = []
        win[cfg] = np.array(means)
    for cfg in cfgs:
        d = win[cfg] - win["ident"]
        final = d[-8:].mean(axis=0)
        den = float((final * final).sum())
        out["jitter"][cfg] = {str(8 * (k + 1)): float((d[k] * final).sum() / den) for k in range(len(d))
                              if 8 * (k + 1) in marks} if den > 0 else None
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--configs", nargs="*", default=["ident=rebuild0", "cr=taa_cr", "cr5=taa_cr5_full", "cr16=taa_cr16_full",
                                                     "s30=taa_s30", "all=taa_all", "all_s50=taa_all_s50"],
                    help="name=tag pairs (blob dist/_shader/filter_<tag>/blobs/FRPG_Fil_ResolveTAA.fpo); the defaults are "
                         "the blobs packed by build_taa_variants.py plus its test-only builds (5-tap, 16-tap fallback)")
    ap.add_argument("--size", type=int, default=64)
    ap.add_argument("--json", default=str(DIST / "taa_warp_test.json"))
    a = ap.parse_args(argv)
    blobs = {}
    for c in a.configs:
        k, tag = c.split("=", 1)
        blobs[k] = (DIST / f"filter_{tag}" / "blobs" / "FRPG_Fil_ResolveTAA.fpo").read_bytes()
    if "ident" not in blobs:
        raise SystemExit("an 'ident' config is required")
    n = a.size
    dev = Device()
    rep = {"configs": dict(c.split("=", 1) for c in a.configs), "size": n, "static": {}, "moving": {}}
    try:
        for scene in ("hard_edge", "soft_edge", "checker_hi", "checker_lo", "lines", "flat", "texture", "hdr", "hdr_color"):
            rep["static"][scene] = summarise_static(run_static(dev, blobs, scene, n), scene, n)
            print("static", scene, "done", flush=True)
        for scene in ("grating4", "grating8", "edge_aa", "thin_lines", "hdr_blob"):
            rep["moving"][scene] = summarise_moving(run_moving(dev, blobs, scene, n), scene)
            print("moving", scene, "done", flush=True)
        rep["moving_jitter"] = {}
        for scene in ("grating8", "edge_aa", "texture", "hdr_blob"):
            rep["moving_jitter"][scene] = summarise_moving(run_moving(dev, blobs, scene, n, frames=64, tail=16, jitter=True), scene)
            print("moving+jitter", scene, "done", flush=True)
        # review finding 1: half-pixel motion (the sub-pixel phase alternates every frame)
        rep["moving_half_px"] = {}
        for scene in ("grating4", "thin_lines", "texture"):
            rep["moving_half_px"][scene] = summarise_moving(run_moving(dev, blobs, scene, n, v=(0.5, 0.0), frames=48, tail=16), scene)
            print("moving 0.5 px", scene, "done", flush=True)
        # review finding 1: static camera, engine velocity includes the jitter
        rep["static_jitter_in_velocity"] = {}
        for scene in ("texture", "grating8", "grating4", "soft_edge", "thin_lines"):
            rep["static_jitter_in_velocity"][scene] = summarise_jitter_vel(*run_static_jitter_vel(dev, blobs, scene, n))
            print("static, jitter in velocity", scene, "done", flush=True)
        # review finding 3: s1 bound as a point sampler, against the same run with s1 bilinear
        rep["moving_sampler_filter"] = {}
        for scene in ("texture", "grating8", "grating4"):
            rep["moving_sampler_filter"][scene] = {
                hf_name: summarise_moving(run_moving(dev, blobs, scene, n, frames=64, tail=16, hist_filter=hf), scene)
                for hf_name, hf in (("bilinear", True), ("point", False))}
            print("moving, s1 bilinear vs point", scene, "done", flush=True)
        # review finding 2: sharpening build-up while standing still
        rep["sharpen_buildup"] = sharpen_buildup(dev, blobs, n)
        print("sharpen build-up done", flush=True)
        # reset path: history reprojected off-screen for the left part of the image (v = 20 px)
        f = make_scene("texture", n)
        cf = [sample(f, n, -20.0 * i, 0) for i in range(4)]
        rr = {}
        for cfg, blob in blobs.items():
            r = TAARun(dev, blob, n, n); r.set_velocity_px(20.0, 0.0)
            for i, c in enumerate(cf):
                o0, _ = r.frame(c, cf[0] if i == 0 else None)
            rr[cfg] = o0
        rep["reset_offscreen_cols_0_19_max_abs_diff_vs_ident"] = {
            cfg: float(np.abs(o[:, :20] - rr["ident"][:, :20]).max()) for cfg, o in rr.items()}
        # long run (feedback stability): 256 jittered frames of the low-contrast texture
        f = make_scene("texture", n)
        cf = [sample(f, n, *JITTER[i % 8]) for i in range(256)]
        lr = {}
        for cfg, blob in blobs.items():
            r = TAARun(dev, blob, n, n); r.set_velocity_px(0, 0)
            means = []
            acc = []
            for i, c in enumerate(cf):
                o0, _ = r.frame(c, cf[0] if i == 0 else None)
                acc.append(lum(o0))
                if len(acc) == 32:
                    means.append(np.array(acc).mean(0)); acc = []
            d = [float(np.abs(means[k + 1] - means[k]).max()) for k in range(len(means) - 1)]
            lr[cfg] = {"max_change_between_32frame_means": d, "box_violation_m_last": box_violation(o0, cf[-1]),
                       "nan_inf": int((~np.isfinite(o0)).sum())}
        rep["long_run_texture_256f"] = lr
        rep["kernel"] = {"fetch": kernel_fetch(dev, n), "sharpen": kernel_sharpen(dev, n)}
    finally:
        dev.close()
    Path(a.json).write_text(json.dumps(rep, indent=1), encoding="utf-8")
    print("report:", a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
