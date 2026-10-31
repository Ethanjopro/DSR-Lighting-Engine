"""Remove debug chunks (SPDB, SDBG, ILDB) from every DXBC blob of a shaderbnd archive, so a released archive
carries compiled code only (some retail blobs embed their HLSL source in SPDB).

    .venv\\Scripts\\python.exe shaders\\tools\\strip_debug.py IN.shaderbnd.dcx OUT.shaderbnd.dcx [--no-device]

Every other chunk is copied byte for byte in its original order, the DXBC checksum is recomputed, and each
stripped blob is checked: it parses, its remaining chunks equal the original's, the checksum is valid, and
(unless --no-device) D3D11 on WARP accepts it. Entry names, order, IDs and flags of the archive are kept.
Library use: strip_archive(in_path, out_path) -> report dict; has_debug(blob) -> bool.
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dxbc  # noqa: E402
import shaderbnd  # noqa: E402

DEBUG_CHUNKS = {"SPDB", "SDBG", "ILDB"}


def has_debug(blob: bytes) -> bool:
    return blob[:4] == b"DXBC" and any(c.fourcc in DEBUG_CHUNKS for c in dxbc.parse(blob))


def strip_blob(blob: bytes) -> bytes:
    keep = [c for c in dxbc.parse(blob) if c.fourcc not in DEBUG_CHUNKS]
    head = 0x20 + 4 * len(keep)
    body, offsets = bytearray(), []
    for c in keep:
        offsets.append(head + len(body))
        body += c.fourcc.encode("latin-1") + struct.pack("<I", len(c.data)) + c.data
        body += b"\0" * (-len(body) % 4)
    total = head + len(body)
    out = bytearray(b"DXBC" + b"\0" * 16 + struct.pack("<III", 1, total, len(keep)))
    out += struct.pack(f"<{len(keep)}I", *offsets) + body
    out[4:20] = dxbc.dxbc_checksum(bytes(out))
    return bytes(out)


def strip_archive(in_path, out_path, device: bool = True) -> dict:
    arc = shaderbnd.read_archive(in_path)
    reps, report = {}, {"stripped": [], "device_failures": []}
    for e in arc.entries:
        blob = e.get_uncompressed_data()
        if not has_debug(blob):
            continue
        new = strip_blob(blob)
        old_chunks = [(c.fourcc, c.data) for c in dxbc.parse(blob) if c.fourcc not in DEBUG_CHUNKS]
        if [(c.fourcc, c.data) for c in dxbc.parse(new)] != old_chunks or not dxbc.checksum_ok(new):
            raise ValueError(f"{e.name}: stripped blob does not round-trip")
        reps[e.name] = new
        report["stripped"].append({"entry": e.name, "bytes_before": len(blob), "bytes_after": len(new)})
    if device and reps:
        import d3d11check
        with d3d11check.Device() as d:
            report["device_failures"] = [n for n, b in reps.items() if d.create(b) != 0]
        if report["device_failures"]:
            raise ValueError(f"D3D11 rejected stripped blobs: {report['device_failures'][:5]}")
    shaderbnd.write(arc, out_path, reps)
    left = [e.name for e in shaderbnd.read_archive(out_path).entries if has_debug(e.get_uncompressed_data())]
    if left:
        raise ValueError(f"debug chunks remain in {out_path}: {left[:5]}")
    report.update({"entries": len(arc.entries), "count": len(reps)})
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--no-device", action="store_true")
    a = ap.parse_args(argv)
    rep = strip_archive(a.src, a.dst, device=not a.no_device)
    print(json.dumps({k: v for k, v in rep.items() if k != "stripped"} | {"stripped": [s["entry"] for s in rep["stripped"]]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
