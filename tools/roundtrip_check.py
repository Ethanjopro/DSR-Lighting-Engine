"""Phase 0 gate: Soulstruct must round-trip every DSR DrawParam BND without semantic change.

For each vanilla aXX_DrawParam.parambnd.dcx: load with Soulstruct, write to a temp dir, then compare
original vs rewritten at the binder level (entry names, IDs, flags, order) and at the PARAM level
(param type, row IDs, row names, raw row bytes). Raw row bytes equal => every field value is equal.

Usage: python tools/roundtrip_check.py [out_dir]
"""
from __future__ import annotations

import struct
import sys
import tempfile
from pathlib import Path

from soulstruct.containers import Binder
from soulstruct.darksouls1r.params.draw_param import DrawParamBND

ROOT = Path(__file__).resolve().parents[1]
VANILLA = ROOT / "vanilla" / "param" / "DrawParam"
AREAS = [f"a{n}" for n in range(10, 19)]


def parse_param(data: bytes) -> dict:
    """Minimal DS1 PARAM reader: returns type, row size, and {row_id: (name_bytes, row_bytes)}."""
    strings_offset, data_start = struct.unpack_from("<IH", data, 0)
    row_count = struct.unpack_from("<H", data, 0x0A)[0]
    param_type = data[0x0C:0x2C].split(b"\0")[0].decode("ascii")
    rows = []
    for i in range(row_count):
        row_id, data_offset, name_offset = struct.unpack_from("<iII", data, 0x30 + i * 12)
        rows.append((row_id, data_offset, name_offset))
    # Row size = distance between consecutive data offsets (all rows in a DrawParam have the same size).
    row_size = (rows[1][1] - rows[0][1]) if len(rows) > 1 else (strings_offset - rows[0][1])
    out = {}
    for row_id, data_offset, name_offset in rows:
        name = b""
        if name_offset:
            end = data.index(b"\0", name_offset)
            name = data[name_offset:end]
        out[row_id] = (name, data[data_offset:data_offset + row_size])
    return {"type": param_type, "row_size": row_size, "rows": out, "row_order": [r[0] for r in rows]}


def binder_summary(path: Path) -> list[tuple]:
    binder = Binder.from_path(path)
    return [
        (e.entry_id, e.name, int(e.flags), parse_param(e.get_uncompressed_data()))
        for e in binder.entries
    ]


def compare(area: str, original: Path, rewritten: Path) -> list[str]:
    problems = []
    a, b = binder_summary(original), binder_summary(rewritten)
    if [x[:3] for x in a] != [x[:3] for x in b]:
        problems.append(f"{area}: binder entries differ (id/name/flags/order)")
        for x, y in zip(a, b):
            if x[:3] != y[:3]:
                problems.append(f"    {x[:3]}  !=  {y[:3]}")
    for (_, name, _, pa), (_, _, _, pb) in zip(a, b):
        stem = name.rsplit("\\", 1)[-1]
        if pa["type"] != pb["type"] or pa["row_size"] != pb["row_size"]:
            problems.append(f"{area}/{stem}: type/row size {pa['type']}/{pa['row_size']} != {pb['type']}/{pb['row_size']}")
        if pa["row_order"] != pb["row_order"]:
            problems.append(f"{area}/{stem}: row IDs/order differ")
        for row_id, (name_a, bytes_a) in pa["rows"].items():
            name_b, bytes_b = pb["rows"].get(row_id, (None, None))
            if name_a != name_b:
                problems.append(f"{area}/{stem} row {row_id}: name {name_a!r} != {name_b!r}")
            if bytes_a != bytes_b:
                problems.append(f"{area}/{stem} row {row_id}: row data differs")
    return problems


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="dsrle_rt_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    all_problems = []
    for area in AREAS:
        src = VANILLA / f"{area}_DrawParam.parambnd.dcx"
        dst = out_dir / src.name
        bnd = DrawParamBND.from_path(src)
        bnd.write(dst)
        problems = compare(area, src, dst)
        n_params = sum(p is not None for p in bnd.draw_params_0.values()) + sum(
            p is not None for p in bnd.draw_params_1.values()
        )
        status = "OK" if not problems else f"{len(problems)} problem(s)"
        print(f"{area}: map_area={bnd.map_area} params={n_params} -> {status}")
        all_problems += problems
    for p in all_problems[:50]:
        print("  " + p)
    print(f"\nRound-trip {'PASSED' if not all_problems else 'FAILED'} ({len(all_problems)} problems). Output: {out_dir}")
    return 0 if not all_problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
