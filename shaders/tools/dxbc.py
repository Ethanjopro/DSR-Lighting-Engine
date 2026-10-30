"""Generic DXBC (D3D10/11 shader bytecode container) helpers.

Container layout:
  0x00 'DXBC'
  0x04 16-byte checksum (MD5 variant over bytes [0x14:], see dxbc_checksum)
  0x14 uint32 version (1)
  0x18 uint32 total size
  0x1C uint32 chunk count
  0x20 uint32 chunk offsets[count]; each chunk = fourcc, uint32 size, payload

Nothing in here is game specific and no shader source is involved.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# container


@dataclass
class Chunk:
    fourcc: str
    offset: int
    data: bytes


def parse(blob: bytes) -> list[Chunk]:
    """Validate the container header and return the chunks in file order."""
    if len(blob) < 0x20 or blob[:4] != b"DXBC":
        raise ValueError("not a DXBC container")
    version, total, count = struct.unpack_from("<III", blob, 0x14)
    if version != 1:
        raise ValueError(f"unexpected DXBC version {version}")
    if total != len(blob):
        raise ValueError(f"DXBC size field {total} != blob length {len(blob)}")
    offs = struct.unpack_from(f"<{count}I", blob, 0x20)
    out = []
    for o in offs:
        cc = blob[o:o + 4].decode("latin-1")
        sz = struct.unpack_from("<I", blob, o + 4)[0]
        if o + 8 + sz > len(blob):
            raise ValueError(f"chunk {cc} at {o:#x} overruns blob")
        out.append(Chunk(cc, o, blob[o + 8:o + 8 + sz]))
    return out


def chunk_map(blob: bytes) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for c in parse(blob):
        out.setdefault(c.fourcc, c.data)
    return out


def code_chunk(chunks: dict[str, bytes]) -> bytes | None:
    return chunks.get("SHEX") or chunks.get("SHDR")


def shader_version(chunks: dict[str, bytes]) -> tuple[str, int, int]:
    """('ps'|'vs'|..., major, minor) from the SHEX/SHDR version token."""
    tok = struct.unpack_from("<I", code_chunk(chunks), 0)[0]
    kind = {0: "ps", 1: "vs", 2: "gs", 3: "hs", 4: "ds", 5: "cs"}.get(tok >> 16, f"?{tok >> 16}")
    return kind, (tok >> 4) & 0xF, tok & 0xF


# ---------------------------------------------------------------------------
# signatures (ISGN / OSGN, 24-byte elements)


@dataclass
class SigElem:
    name: str
    index: int
    sysval: int        # D3D_NAME (0 none, 1 position, 2 clipdist, 9 is_front_face, 64 target, ...)
    comp_type: int     # 1 uint32, 2 sint32, 3 float32
    register: int
    mask: int
    rw: int            # input: always-read mask; output: never-written mask


def parse_signature(data: bytes) -> list[SigElem]:
    count, _eight = struct.unpack_from("<II", data, 0)
    els = []
    for i in range(count):
        no, si, sv, ct, reg, m, rw = struct.unpack_from("<IIIIIBB", data, 8 + 24 * i)
        name = data[no:data.index(b"\0", no)].decode("ascii")
        els.append(SigElem(name, si, sv, ct, reg, m, rw))
    return els


# ---------------------------------------------------------------------------
# checksum: MD5 core with the DXBC-specific final-block padding


_S = [7, 12, 17, 22] * 4 + [5, 9, 14, 20] * 4 + [4, 11, 16, 23] * 4 + [6, 10, 15, 21] * 4
_K = [int(abs(math.sin(i + 1)) * 2**32) & 0xFFFFFFFF for i in range(64)]
_M32 = 0xFFFFFFFF


def _rotl(x: int, c: int) -> int:
    return ((x << c) | (x >> (32 - c))) & _M32


def _transform(state: list[int], block: bytes) -> None:
    m = struct.unpack("<16I", block)
    a, b, c, d = state
    for i in range(64):
        if i < 16:
            f = (b & c) | (~b & d); g = i
        elif i < 32:
            f = (d & b) | (~d & c); g = (5 * i + 1) & 15
        elif i < 48:
            f = b ^ c ^ d; g = (3 * i + 5) & 15
        else:
            f = c ^ (b | (~d & _M32)); g = (7 * i) & 15
        f = (f + a + _K[i] + m[g]) & _M32
        a, d, c = d, c, b
        b = (b + _rotl(f, _S[i])) & _M32
    for i, v in enumerate((a, b, c, d)):
        state[i] = (state[i] + v) & _M32


def dxbc_checksum(blob: bytes) -> bytes:
    """Compute the 16-byte checksum stored at blob[4:20]."""
    data = blob[0x14:]
    n = len(data)
    bits = (n * 8) & _M32
    state = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476]
    full = n & ~63
    for off in range(0, full, 64):
        _transform(state, data[off:off + 64])
    rest = data[full:]
    pad = b"\x80" + b"\0" * 63
    tail = struct.pack("<I", (bits >> 2) | 1)
    if len(rest) < 56:
        block = struct.pack("<I", bits) + rest + pad[:56 - len(rest)] + tail
        _transform(state, block)
    else:
        block = rest + pad[:64 - len(rest)]
        _transform(state, block)
        _transform(state, struct.pack("<I", bits) + b"\0" * 56 + tail)
    return struct.pack("<4I", *state)


def checksum_ok(blob: bytes) -> bool:
    return dxbc_checksum(blob) == blob[4:20]
