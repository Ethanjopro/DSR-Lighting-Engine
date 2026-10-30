"""Phase 0 field sensitivity sweep: one variant per key field, applied to every row of a bank in the given areas.

Writes dist/_sweep/<variant>/param/DrawParam/aXX_DrawParam.parambnd.dcx. Dev-only; never shipped.
LightBank variants are split into map rows vs character rows (from data/live_rows.json).

Usage: python tools/sweep.py a10 a15
"""
import json
import shutil
import sys

from dp import ROOT, get_field, iter_params, load_vanilla, set_field
from paramdef_limits import limits

VARIANTS = {
    # name: (bank, field, op, value, row_filter)
    "key_map_x2": ("LightBank", ["colA_0"], "mul", 2.0, "map"),
    "key_char_x2": ("LightBank", ["colA_0"], "mul", 2.0, "char"),
    "ambient_x2": ("LightBank", ["colA_u", "colA_d"], "mul", 2.0, None),
    "envdif_x2": ("LightBank", ["envDif_colA"], "mul", 2.0, None),
    "envspc_x2": ("LightBank", ["envSpc_colA"], "mul", 2.0, None),
    "graykey_x1.5": ("ToneMapBank", ["grayKeyValue"], "mul", 1.5, None),
    "minlum_x3": ("ToneMapBank", ["minAdaptedLum"], "mul", 3.0, None),
    "maxlum_x0.5": ("ToneMapBank", ["maxAdapredLum"], "mul", 0.5, None),
    "bloom_x2": ("ToneMapBank", ["bloomMul", "bloomMulFar"], "mul", 2.0, None),
    "shaft_0.08": ("ToneMapBank", ["lightShaftPower"], "set", 0.08, None),
    "fog_x1.6": ("FogBank", ["degRotW"], "mul", 1.6, None),
    "contrast_+0.1": ("ToneCorrectBank", ["contrastR", "contrastG", "contrastB"], "add", 0.1, None),
    "shadow_end_x2": ("ShadowBank", ["endDist"], "mul", 2.0, None),
    "scatter_x2": ("LightScatteringBank", ["sunA"], "mul", 2.0, None),
}


def main():
    areas = sys.argv[1:]
    live = json.loads((ROOT / "data" / "live_rows.json").read_text(encoding="utf-8"))
    out_root = ROOT / "dist" / "_sweep"
    shutil.rmtree(out_root, ignore_errors=True)
    for name, (bank, fields, op, value, row_filter) in VARIANTS.items():
        for area in areas:
            lb = live.get(area, {}).get("LightBank", {})
            map_rows = {int(r) for r, rec in lb.items() if rec["users"].get("map_pieces") or rec["users"].get("objects")}
            bnd = load_vanilla(area)
            defs = limits()[bank]
            n = 0
            for _, stem, param in iter_params(bnd):
                if stem != bank:
                    continue
                for rid, row in param.rows.items():
                    if row_filter == "map" and rid not in map_rows:
                        continue
                    if row_filter == "char" and rid in map_rows:
                        continue
                    for f in fields:
                        old = get_field(row, f)
                        new = {"mul": old * value, "add": old + value, "set": value}[op]
                        new = min(max(new, defs[f]["min"]), defs[f]["max"])
                        set_field(row, f, int(round(new)) if isinstance(old, int) else new)
                    n += 1
            dst = out_root / name / "param" / "DrawParam"
            dst.mkdir(parents=True, exist_ok=True)
            bnd.write(dst / f"{area}_DrawParam.parambnd.dcx")
        print(f"{name}: built for {', '.join(areas)}")


if __name__ == "__main__":
    main()
