"""Build modded DrawParam BNDs from pristine vanilla + spec/areas/*.toml.

Spec format (one file per map block or area, any name, e.g. spec/areas/m10_02_firelink.toml):

    area = "a10"

    [[edit]]
    bank = "ToneCorrectBank"         # bank stem; "s_LightBank" for the s-prefixed light bank
    slot = 0                         # 0 (default), 1, or "both" (a15 only has slot 1)
    rows = [20, 21]                  # row IDs (required; no wildcards, so every change is deliberate)
    op = "mul"                       # "mul" | "add" | "set"
    fields = { saturation = 1.08, contrastR = 0.04 }
    why = "Warmer Firelink: restore PTDE sepia"

    [[lockstep]]                     # declares char rows that must track a map row (checked by validate.py)
    bank = "LightBank"
    map = 20
    chars = [25, 26, 27]

Integer fields are rounded; every result is clamped to the paramdef min/max.
Writes dist/param/DrawParam/aXX_DrawParam.parambnd.dcx for edited areas and dist/build_manifest.json.

Usage: python tools/build.py [--out dist]
"""
from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

from dp import AREAS, ROOT, get_field, load_vanilla, set_field
from paramdef_limits import limits

SPEC_DIR = ROOT / "spec" / "areas"
INT_TYPES = {"s8", "u8", "s16", "u16", "s32", "u32"}


def load_specs() -> list[dict]:
    specs = []
    for path in sorted(SPEC_DIR.glob("*.toml")):
        spec = tomllib.loads(path.read_text(encoding="utf-8"))
        spec["_file"] = path.name
        if spec.get("area") not in AREAS:
            raise SystemExit(f"{path.name}: area must be one of {AREAS}")
        specs.append(spec)
    return specs


def paramdef_name(bank: str) -> str:
    return "LightBank" if bank == "s_LightBank" else bank


def apply_edit(bnd, edit: dict, spec_file: str, manifest: list) -> None:
    bank = edit["bank"]
    slots = {"both": (0, 1)}.get(edit.get("slot", 0), (edit.get("slot", 0),))
    op = edit["op"]
    if op not in ("mul", "add", "set"):
        raise SystemExit(f"{spec_file}: bad op {op!r}")
    defs = limits()[paramdef_name(bank)]
    for slot in slots:
        params = bnd.draw_params_0 if slot == 0 else bnd.draw_params_1
        param = params.get(bank)
        if param is None:
            raise SystemExit(f"{spec_file}: {bank} slot {slot} does not exist in {bnd.map_area}")
        for row_id in edit["rows"]:
            if row_id not in param.rows:
                raise SystemExit(f"{spec_file}: {bank} slot {slot} has no row {row_id}")
            row = param.rows[row_id]
            for field, value in edit["fields"].items():
                if field not in defs:
                    raise SystemExit(f"{spec_file}: {bank} has no field {field!r}")
                old = get_field(row, field)
                new = {"mul": old * value, "add": old + value, "set": value}[op]
                new = min(max(new, defs[field]["min"]), defs[field]["max"])
                if defs[field]["type"] in INT_TYPES:
                    new = int(round(new))
                set_field(row, field, new)
                manifest.append({
                    "area": bnd_area(bnd), "slot": slot, "bank": bank, "row": row_id, "field": field,
                    "op": op, "arg": value, "old": old, "new": get_field(row, field),
                    "why": edit.get("why", ""), "spec": spec_file,
                })


def bnd_area(bnd) -> str:
    return "a" + bnd.map_area[1:]


def build(out_dir: Path) -> list[dict]:
    specs = load_specs()
    manifest: list[dict] = []
    drawparam_out = out_dir / "param" / "DrawParam"
    drawparam_out.mkdir(parents=True, exist_ok=True)
    for area in AREAS:
        area_specs = [s for s in specs if s["area"] == area]
        target = drawparam_out / f"{area}_DrawParam.parambnd.dcx"
        if not area_specs:
            target.unlink(missing_ok=True)  # untouched areas stay vanilla in the game
            continue
        bnd = load_vanilla(area)
        for spec in area_specs:
            for edit in spec.get("edit", []):
                apply_edit(bnd, edit, spec["_file"], manifest)
        bnd.write(target)
    lockstep = [dict(l, area=s["area"], spec=s["_file"]) for s in specs for l in s.get("lockstep", [])]
    (out_dir / "build_manifest.json").write_text(
        json.dumps({"changes": manifest, "lockstep": lockstep}, indent=1, default=str), encoding="utf-8")
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dist"))
    args = ap.parse_args()
    manifest = build(Path(args.out))
    areas = sorted({m["area"] for m in manifest})
    print(f"built {len(manifest)} field changes in {len(areas)} area(s): {', '.join(areas) or 'none'}")


if __name__ == "__main__":
    main()
