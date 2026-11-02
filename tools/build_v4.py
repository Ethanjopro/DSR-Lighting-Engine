#!/usr/bin/env python3
"""Build the DrawParam set for the v4 lighting: the 1.0 area specs, then the key light from spec/sun_rows.toml.

The engine's key light reads LightBank _s (c6): colour and strength per row, per draw, with its direction from
ShadowBank. So a row's _s is its sun switch. c6 = (col/255 * colA/100)^2.2; 0 means no sun, and above 0.33 the shader
reads the row as water or lava specular and also gives no sun. spec/sun_rows.toml holds the decision for every live
row and the reasoning.

A row left out of the table keeps its spec values with no sun: colA_s 0, or untouched if it is above 60. Live rows
left out draw a warning, and so do table rows that are not live.

    .venv\\Scripts\\python.exe tools\\build_v4.py [--out dist\\_v4\\sun] [--table spec\\sun_rows.toml] [--no-sun]
"""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build import apply_edit, load_specs  # noqa: E402
from dp import AREAS, ROOT, get_field, load_vanilla, set_field  # noqa: E402

SUN_GATE = 0.33  # DSRLE_SUN_SPEC_ONLY: the largest c6 channel above this is a water or lava row
WATER_COL_A = 61  # the lowest white colA_s over the gate
STATES = ("on", "off", "keep", "water")


def c6_max(rgb: tuple[int, int, int], strength: int) -> float:
    return (max(rgb) / 255 * strength / 100) ** 2.2


def row_rgb(row, suffix: str) -> tuple[int, int, int]:
    return tuple(get_field(row, f"col{c}_{suffix}") for c in "RGB")


def load_table(path: Path) -> dict[tuple[str, int, int], dict]:
    """(area, slot, row) -> its group, with the group's fields checked."""
    table: dict[tuple[str, int, int], dict] = {}
    for group in tomllib.loads(path.read_text(encoding="utf-8"))["group"]:
        where = f"{path.name}: {group.get('area')} {group.get('place')!r}"
        if group.get("area") not in AREAS:
            raise SystemExit(f"{where}: bad area")
        if group.get("sun") not in STATES:
            raise SystemExit(f"{where}: sun must be one of {STATES}")
        if not group.get("note"):
            raise SystemExit(f"{where}: needs a note")
        if group["sun"] == "on":
            if not 1 <= group.get("strength", 0) <= 60:
                raise SystemExit(f"{where}: strength must be 1..60")
            if "colour" not in group:
                raise SystemExit(f"{where}: needs a colour")
        elif "strength" in group or "colour" in group:
            raise SystemExit(f"{where}: strength and colour belong to sun = 'on'")
        for row_id in group["rows"]:
            key = (group["area"], group.get("slot", 0), row_id)
            if key in table:
                raise SystemExit(f"{where}: row {row_id} is also in {table[key]['place']!r}")
            table[key] = group
    return table


def sun_colour(group: dict, bank, row_id: int, where: str) -> tuple[int, int, int]:
    """The group's colour, scaled so its largest channel is 255: strength is the only brightness knob."""
    colour = group["colour"]
    if isinstance(colour, str):
        if colour != "key" and not colour.startswith("key:"):
            raise SystemExit(f"{where}: colour must be [r, g, b], 'key' or 'key:N'")
        key_id = row_id if colour == "key" else int(colour[4:])
        key_row = bank.rows[key_id]
        if get_field(key_row, "colA_0") <= 0 or max(row_rgb(key_row, "0")) <= 0:
            raise SystemExit(f"{where}: row {key_id} has no _0 light to take a colour from")
        colour = row_rgb(key_row, "0")
    top = max(colour)
    if len(colour) != 3 or top <= 0:
        raise SystemExit(f"{where}: colour must be three channels and not black")
    return tuple(int(round(c * 255 / top)) for c in colour)


def apply_sun(bnd, area: str, table: dict, live: dict[str, set[int]]) -> None:
    """Write the table into this area's LightBank rows and print the area's counts."""
    for slot, params in enumerate((bnd.draw_params_0, bnd.draw_params_1)):
        bank = params.get("LightBank")
        if bank is None:
            if any(k[:2] == (area, slot) for k in table):
                raise SystemExit(f"sun table: {area} has no slot {slot}")
            continue
        counts = dict.fromkeys(STATES, 0)
        listed = {k[2] for k in table if k[:2] == (area, slot)}
        if listed - set(bank.rows):
            raise SystemExit(f"sun table: {area} slot {slot} has no LightBank rows {sorted(listed - set(bank.rows))}")
        for row_id in sorted(listed - live[area]):
            print(f"WARNING: {area} slot {slot} row {row_id} is in the sun table but is not live")
        missing = []
        for row_id, row in bank.rows.items():
            group = table.get((area, slot, row_id))
            strength = get_field(row, "colA_s")
            if group is None:
                if row_id in live[area]:
                    missing.append(row_id)
                if 0 < strength <= 60:
                    set_field(row, "colA_s", 0)
                continue
            state = group["sun"]
            where = f"{area} slot {slot} row {row_id}"
            counts[state] += 1
            if state == "on":
                rgb = sun_colour(group, bank, row_id, where)
                if c6_max(rgb, group["strength"]) > SUN_GATE:
                    raise SystemExit(f"{where}: strength {group['strength']} is over the shader's sun gate")
                for channel, value in zip("RGB", rgb):
                    set_field(row, f"col{channel}_s", value)
                set_field(row, "colA_s", group["strength"])
            elif state == "off":
                set_field(row, "colA_s", 0)
            elif state == "water":
                if strength > WATER_COL_A:
                    raise SystemExit(f"{where}: colA_s {strength} is already over the gate; use sun = 'keep'")
                set_field(row, "colA_s", WATER_COL_A)
            if state in ("keep", "water") and c6_max(row_rgb(row, "s"), get_field(row, "colA_s")) <= SUN_GATE:
                raise SystemExit(f"{where}: sun = '{state}' but the row is under the sun gate, so it would have a sun")
        label = area if bnd.draw_params_1.get("LightBank") is None else f"{area} slot {slot}"
        print(f"{label}: {counts['on']} rows with a sun, {counts['off']} off, {counts['keep']} keep, "
              f"{counts['water']} water, {len(missing)} live rows missing")
        if missing:
            print(f"WARNING: {label}: live rows not in the sun table, sun off: {missing}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dist" / "_v4" / "sun"))
    ap.add_argument("--table", default=str(ROOT / "spec" / "sun_rows.toml"))
    ap.add_argument("--no-sun", action="store_true", help="only the 1.0 specs")
    a = ap.parse_args()
    out = Path(a.out) / "param" / "DrawParam"
    out.mkdir(parents=True, exist_ok=True)
    specs = load_specs()
    manifest: list = []
    if not a.no_sun:
        table = load_table(Path(a.table))
        rows = json.loads((ROOT / "data" / "live_rows.json").read_text(encoding="utf-8-sig"))
        live = {area: {int(r) for r in rows[area]["LightBank"]} for area in AREAS}
    for area in AREAS:
        bnd = load_vanilla(area)
        for spec in (s for s in specs if s["area"] == area):
            for edit in spec.get("edit", []):
                apply_edit(bnd, edit, spec["_file"], manifest)
        if a.no_sun:
            print(f"{area}: 1.0 specs only")
        else:
            apply_sun(bnd, area, table, live)
        bnd.write(out / f"{area}_DrawParam.parambnd.dcx")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
