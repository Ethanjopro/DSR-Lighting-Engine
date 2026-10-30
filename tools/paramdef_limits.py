"""Read min/max/default per field from the game's own paramdef (DS1 PARAMDEF, 0xB0-byte field records)."""
from __future__ import annotations

import struct
from functools import cache

from soulstruct.containers import Binder

from dp import ROOT

PARAMDEF_BND = ROOT / "vanilla" / "paramdef" / "paramdef.paramdefbnd.dcx"


@cache
def limits() -> dict[str, dict[str, dict]]:
    """{bank stem: {internal field name: {"type", "default", "min", "max"}}} for every paramdef in the BND."""
    out = {}
    for entry in Binder.from_path(PARAMDEF_BND).entries:
        data = entry.get_uncompressed_data()
        header_size, _, field_count, field_size = struct.unpack_from("<4H", data, 4)
        fields = {}
        for i in range(field_count):
            o = header_size + i * field_size
            default, vmin, vmax = struct.unpack_from("<3f", data, o + 0x50)
            itype = data[o + 0x6C:o + 0x8C].split(b"\0")[0].decode("ascii", "replace")
            iname = data[o + 0x8C:o + 0xAC].split(b"\0")[0].decode("ascii", "replace")
            fields[iname] = {"type": itype, "default": default, "min": vmin, "max": vmax}
        out[entry.stem] = fields
    return out


if __name__ == "__main__":
    for bank in ("LightBank", "ToneMapBank", "ToneCorrectBank", "ShadowBank", "FogBank"):
        print(bank, {k: (round(v["min"], 3), round(v["max"], 3)) for k, v in limits()[bank].items() if not k.startswith(("pad", "dummy"))})
