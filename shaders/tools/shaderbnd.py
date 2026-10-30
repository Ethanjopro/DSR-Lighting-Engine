"""Read / list / repack DSR .shaderbnd.dcx archives (DCX(DFLT) wrapping a BND3).

Reading uses soulstruct (DCX decompress + Binder). Two soulstruct 2.6 quirks are
worked around so an unchanged archive repacks bit-for-bit:
  * Binder V3 reading does not keep the header flags byte (it falls back to the
    class default 0b00101110); we restore it from offset 0xC of the raw BND.
  * soulstruct's DCX compress() uses zlib level 7 while the DFLT header says 9;
    we compress at the level stored in the source header and reuse the source
    DCX header verbatim with only the two size fields patched.

An independent minimal BND3 parser (parse_bnd3) is used for verification so the
gate does not depend on soulstruct reading its own output.

CLI (run from repo root with the venv python):
  shaderbnd.py list    <archive.dcx>
  shaderbnd.py extract <archive.dcx> <out_dir>
  shaderbnd.py replace <in.dcx> <out.dcx> <dir_with_replacement_files>
  shaderbnd.py compare <a.dcx> <b.dcx>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

from soulstruct.containers import Binder
from soulstruct.containers.core import BinderFlags
from soulstruct.dcx import core as dcx

DCX_HEADER_SIZE = 0x4C  # DCX_DFLT_10000_24_9 header incl. 'DCA' + 8


@dataclass
class Archive:
    dcx_raw: bytes          # the original file bytes
    dcx_header: bytes       # first 0x4C bytes
    dcx_type: object
    level: int              # zlib level from the DFLT header
    bnd: bytes              # decompressed BND3
    binder: Binder

    @property
    def entries(self):
        return self.binder.entries

    def entry_by_name(self) -> dict:
        return {e.name: e for e in self.binder.entries}


def read_archive(path: str | Path) -> Archive:
    raw = Path(path).read_bytes()
    bnd, dcx_type = dcx.decompress(raw)
    if dcx_type != dcx.DCXType.DCX_DFLT_10000_24_9:
        raise ValueError(f"{path}: unsupported DCX type {dcx_type}")
    hdr = raw[:DCX_HEADER_SIZE]
    if hdr[0x28:0x2C] != b"DFLT" or hdr[0x44:0x4C] != b"DCA\0\0\0\0\x08":
        raise ValueError(f"{path}: unexpected DCX header layout")
    level = hdr[0x30]
    binder = Binder.from_bytes(bnd)
    binder.flags = BinderFlags.from_byte(bnd[0xC], binder.bit_big_endian)
    return Archive(raw, hdr, dcx_type, level, bnd, binder)


def compress_like(bnd: bytes, template: Archive) -> bytes:
    comp = zlib.compress(bnd, template.level)
    hdr = bytearray(template.dcx_header)
    struct.pack_into(">II", hdr, 0x1C, len(bnd), len(comp))
    return bytes(hdr) + comp


def build(template: Archive, replacements: dict[str, bytes] | None = None) -> tuple[bytes, bytes]:
    """Return (bnd_bytes, dcx_bytes) of template with entry payloads replaced by name."""
    replacements = replacements or {}
    binder = Binder.from_bytes(template.bnd)
    binder.flags = BinderFlags.from_byte(template.bnd[0xC], binder.bit_big_endian)
    by_name = {e.name: e for e in binder.entries}
    missing = sorted(set(replacements) - set(by_name))
    if missing:
        raise KeyError(f"replacement names not in archive: {missing[:5]}")
    for name, data in replacements.items():
        by_name[name].set_uncompressed_data(data)
    bnd = binder.to_bytes()
    return bnd, compress_like(bnd, template)


def write(template: Archive, out_path: str | Path, replacements: dict[str, bytes] | None = None) -> bytes:
    bnd, blob = build(template, replacements)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(blob)
    return bnd


# ---------------------------------------------------------------------------
# independent BND3 parser for verification


@dataclass
class Bnd3Entry:
    flags: int
    entry_id: int
    name: str
    data: bytes


@dataclass
class Bnd3:
    signature: bytes
    flags: int
    big_endian: int
    bit_big_endian: int
    unk18: bytes
    entries: list[Bnd3Entry]


def parse_bnd3(bnd: bytes) -> Bnd3:
    if bnd[:4] != b"BND3":
        raise ValueError("not BND3")
    sig = bnd[4:12]
    flags, be, bbe = bnd[0xC], bnd[0xD], bnd[0xE]
    if be or bbe or flags not in (0x54, 0x74):
        raise ValueError(f"unsupported BND3 flags {flags:#x} be={be} bbe={bbe}")
    # raw 0x54 is bit-reversed 0x2A: IDs | Names2 | Compression(sizes); raw 0x74 (FRPG_Filter) is 0x2E,
    # which adds Names1. Both give the same 0x18-byte entry headers (flags, size, offset, id, name, usize).
    count, _hdr_end = struct.unpack_from("<iI", bnd, 0x10)
    entries = []
    for i in range(count):
        ef, csize, off, eid, noff, usize = struct.unpack_from("<B3xIIiII", bnd, 0x20 + 0x18 * i)
        if csize != usize:
            raise ValueError(f"entry {i}: compressed entry not supported")
        name = bnd[noff:bnd.index(b"\0", noff)].decode("shift_jis")
        entries.append(Bnd3Entry(ef, eid, name, bnd[off:off + usize]))
    return Bnd3(sig, flags, be, bbe, bnd[0x18:0x20], entries)


def semantic_diff(a: bytes, b: bytes, expect_changed: set[str] | None = None) -> list[str]:
    """Differences between two BND3 blobs. Entries named in expect_changed may differ in payload only."""
    expect_changed = expect_changed or set()
    A, B = parse_bnd3(a), parse_bnd3(b)
    out = []
    for f in ("signature", "flags", "big_endian", "bit_big_endian", "unk18"):
        if getattr(A, f) != getattr(B, f):
            out.append(f"header {f}: {getattr(A, f)!r} != {getattr(B, f)!r}")
    if len(A.entries) != len(B.entries):
        out.append(f"entry count {len(A.entries)} != {len(B.entries)}")
    for i, (ea, eb) in enumerate(zip(A.entries, B.entries)):
        for f in ("flags", "entry_id", "name"):
            if getattr(ea, f) != getattr(eb, f):
                out.append(f"entry {i} {f}: {getattr(ea, f)!r} != {getattr(eb, f)!r}")
        if ea.data != eb.data and ea.name not in expect_changed:
            out.append(f"entry {i} {ea.name}: payload differs")
    return out


def dcx_info(raw: bytes) -> dict:
    return {
        "dcx_header_hex": raw[:DCX_HEADER_SIZE].hex(),
        "compression_type": raw[0x28:0x2C].decode(),
        "level": raw[0x30],
        "decompressed_size": struct.unpack_from(">I", raw, 0x1C)[0],
        "compressed_size": struct.unpack_from(">I", raw, 0x20)[0],
        "zlib_header": raw[0x4C:0x4E].hex(),
    }


# ---------------------------------------------------------------------------
# CLI


def _cmd_list(a):
    ar = read_archive(a.archive)
    print(f"{a.archive}: {len(ar.entries)} entries, dcx={ar.dcx_type.name} level={ar.level} "
          f"bnd_flags={ar.bnd[0xC]:#04x} sig={ar.binder.signature!r}")
    for e in ar.entries:
        print(f"{e.entry_id:5d} {e.flags:#04x} {len(e.data):8d} {hashlib.md5(e.data).hexdigest()} {e.path}")


def _cmd_extract(a):
    ar = read_archive(a.archive)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for e in ar.entries:
        (out / e.name).write_bytes(e.get_uncompressed_data())
    print(f"extracted {len(ar.entries)} entries to {out}")


def _cmd_replace(a):
    ar = read_archive(a.src)
    names = {e.name for e in ar.entries}
    reps = {p.name: p.read_bytes() for p in sorted(Path(a.dir).iterdir()) if p.name in names}
    bnd = write(ar, a.dst, reps)
    back = read_archive(a.dst)
    diffs = semantic_diff(ar.bnd, back.bnd, set(reps))
    print(f"replaced {len(reps)} entries -> {a.dst}; semantic diffs: {len(diffs)}")
    for d in diffs[:20]:
        print("  ", d)
    return 0 if not diffs and back.bnd == bnd else 1


def _cmd_compare(a):
    A, B = read_archive(a.a), read_archive(a.b)
    diffs = semantic_diff(A.bnd, B.bnd)
    print(json.dumps({"bnd_identical": A.bnd == B.bnd, "dcx_identical": A.dcx_raw == B.dcx_raw,
                      "semantic_diffs": len(diffs), "first": diffs[:20]}, indent=1))
    return 0 if not diffs else 1


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("list"); s.add_argument("archive"); s.set_defaults(fn=_cmd_list)
    s = sub.add_parser("extract"); s.add_argument("archive"); s.add_argument("out_dir"); s.set_defaults(fn=_cmd_extract)
    s = sub.add_parser("replace"); s.add_argument("src"); s.add_argument("dst"); s.add_argument("dir"); s.set_defaults(fn=_cmd_replace)
    s = sub.add_parser("compare"); s.add_argument("a"); s.add_argument("b"); s.set_defaults(fn=_cmd_compare)
    a = p.parse_args(argv)
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
