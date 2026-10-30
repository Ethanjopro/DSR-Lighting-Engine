"""Runtime-side validation of shader bytecode: create each blob on a D3D11 device (WARP by default).

    python d3d11check.py BLOB [BLOB ...] [--hardware] [--debug]

ID3D11Device::Create{Vertex,Pixel,Compute,...}Shader validates the DXBC container, its checksum and the
token stream before accepting a shader; a blob that passes here is accepted by the same runtime the game
uses. WARP (the software rasterizer) needs no GPU and does not touch any running application.
--debug additionally enables the D3D11 debug layer when it is installed (falls back silently otherwise).

Library use: `with Device() as d: hr = d.create(blob)`; returns the HRESULT (0 = S_OK).
Generic: nothing game specific.
"""
from __future__ import annotations

import argparse
import ctypes
import struct
import sys
from ctypes import POINTER, byref, c_size_t, c_uint, c_void_p
from pathlib import Path

D3D_DRIVER_TYPE_HARDWARE, D3D_DRIVER_TYPE_WARP = 1, 5
D3D11_CREATE_DEVICE_DEBUG = 0x2
D3D11_SDK_VERSION = 7
# ID3D11Device vtable slots (IUnknown 0..2, then the device methods in declaration order)
_SLOT = {"vs": 12, "gs": 13, "ps": 15, "hs": 16, "ds": 17, "cs": 18}
_KIND = {0: "ps", 1: "vs", 2: "gs", 3: "hs", 4: "ds", 5: "cs"}


def shader_kind(blob: bytes) -> str:
    if blob[:4] != b"DXBC":
        raise ValueError("not DXBC")
    count = struct.unpack_from("<I", blob, 0x1C)[0]
    for o in struct.unpack_from(f"<{count}I", blob, 0x20):
        if blob[o:o + 4] in (b"SHEX", b"SHDR"):
            return _KIND[struct.unpack_from("<I", blob, o + 8)[0] >> 16]
    raise ValueError("no SHEX/SHDR chunk")


def _method(obj: c_void_p, slot: int, *argtypes):
    vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
    return ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, *argtypes)(vtbl[slot])


def _release(obj: c_void_p) -> None:
    if obj:
        vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
        ctypes.WINFUNCTYPE(c_uint, c_void_p)(vtbl[2])(obj)


class Device:
    def __init__(self, hardware: bool = False, debug: bool = False):
        self.dev, self.ctx = c_void_p(), c_void_p()
        self.debug = False
        d3d11 = ctypes.WinDLL("d3d11")
        fl = c_uint()
        drv = D3D_DRIVER_TYPE_HARDWARE if hardware else D3D_DRIVER_TYPE_WARP
        for flags in ([D3D11_CREATE_DEVICE_DEBUG, 0] if debug else [0]):
            hr = d3d11.D3D11CreateDevice(None, drv, None, flags, None, 0, D3D11_SDK_VERSION,
                                         byref(self.dev), byref(fl), byref(self.ctx))
            if hr == 0:
                self.debug = bool(flags)
                break
        else:
            raise OSError(f"D3D11CreateDevice failed: hr=0x{hr & 0xFFFFFFFF:08x}")
        self.feature_level = fl.value
        self.driver = "hardware" if hardware else "warp"

    def create(self, blob: bytes) -> int:
        kind = shader_kind(blob)
        out = c_void_p()
        buf = ctypes.create_string_buffer(blob, len(blob))
        fn = _method(self.dev, _SLOT[kind], c_void_p, c_size_t, c_void_p, POINTER(c_void_p))
        hr = fn(self.dev, ctypes.cast(buf, c_void_p), len(blob), None, byref(out)) & 0xFFFFFFFF
        _release(out)
        return hr

    def close(self) -> None:
        _release(self.ctx); _release(self.dev)
        self.ctx, self.dev = c_void_p(), c_void_p()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("blobs", nargs="+")
    p.add_argument("--hardware", action="store_true")
    p.add_argument("--debug", action="store_true")
    a = p.parse_args(argv)
    bad = 0
    with Device(a.hardware, a.debug) as d:
        print(f"device: {d.driver} feature level 0x{d.feature_level:x} debug={d.debug}")
        for f in a.blobs:
            try:
                hr = d.create(Path(f).read_bytes())
            except (ValueError, KeyError, OSError) as ex:
                print(f"FAIL {ex} {f}"); bad += 1; continue
            bad += hr != 0
            print(f"{'OK  ' if hr == 0 else 'FAIL'} 0x{hr:08x} {f}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
