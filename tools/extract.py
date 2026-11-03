"""Extract vanilla DrawParam tables and live-row usage from all MSBs into data/.

Outputs:
  data/vanilla/<area>.json   {slot: {bank: {row_id: {"name": str, <internal field>: value}}}}
  data/live_rows.json        {area: {slot0 bank: {row_id: {"maps": [...], "users": {part_type: count}, "place_names": [...],
                                                           "collision_maps": {map: count}, "piece_maps": {map: count}}}}}
  data/row_names_ja.json     sorted unique Japanese row names (input for translation)
  data/row_names_en.json     merged if present (translation output; not generated here)

In live_rows.json, "maps" and "users" are pooled over every part type. Every part carries every bank field, so a row
listed for a map may only be named by that map's objects or navmeshes. "collision_maps" counts collisions alone: they
choose the scene-wide rows (ShadowBank, ToneMap, ToneCorrect, Dof, LensFlareEx). "piece_maps" counts map pieces alone,
only on the banks a map piece picks for its own draws (PIECE_BANKS). tools/maps_table.py, which reads these per-map
counts without writing live_rows.json, counts more owners for its detection table: a LightBank row belongs to every map
whose map pieces, objects, collisions (the row the player's body is drawn with) or characters name it.

Usage: python tools/extract.py
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from soulstruct.darksouls1r.maps import MSB

from dp import AREAS, GAME_DIR, ROOT, internal_to_attr, iter_params, load_vanilla

DATA = ROOT / "data"
MSB_DIR = GAME_DIR / "map" / "MapStudio"

# MSB part field -> DrawParam bank stem.
PART_FIELD_TO_BANK = {
    "ambient_light_id": "LightBank",
    "fog_id": "FogBank",
    "scattered_light_id": "LightScatteringBank",
    "lens_flare_id": "LensFlareExBank",
    "shadow_id": "ShadowBank",
    "dof_id": "DofBank",
    "tone_map_id": "ToneMapBank",
    "tone_correction_id": "ToneCorrectBank",
    "point_light_id": "PointLightBank",
    "lod_id": "LodBank",
}
PART_LISTS = ["map_pieces", "objects", "characters", "player_starts", "collisions", "navmeshes",
              "dummy_objects", "dummy_characters", "connect_collisions"]
# The banks a map piece picks for its own draws (docs/PLAN.md:57-61).
PIECE_BANKS = ("LightBank", "FogBank", "LightScatteringBank")


def jsonable(v):
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, (int, str, bool)) or v is None:
        return v
    return str(v)


def dump_tables() -> set[str]:
    names = set()
    (DATA / "vanilla").mkdir(parents=True, exist_ok=True)
    for area in AREAS:
        bnd = load_vanilla(area)
        out = defaultdict(dict)
        for slot, stem, param in iter_params(bnd):
            rows = {}
            for row_id, row in param.rows.items():
                attr_by_internal = internal_to_attr(type(row))
                rec = {"name": row.Name or ""}
                for internal, attr in attr_by_internal.items():
                    if internal.startswith("pad") or internal.startswith("dummy"):
                        continue
                    rec[internal] = jsonable(getattr(row, attr))
                rows[str(row_id)] = rec
                if row.Name:
                    names.add(row.Name)
            out[f"slot{slot}"][stem] = rows
        (DATA / "vanilla" / f"{area}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{area}: {sum(len(b) for s in out.values() for b in s.values())} rows")
    return names


def iter_msbs(verbose: bool = False):
    """Yield (map_stem, MSB) for every gameplay map, m10..m18 (the m99 test maps are skipped)."""
    for msb_path in sorted(MSB_DIR.glob("m1[0-8]_*.msb")):
        yield msb_path.stem, MSB.from_path(msb_path)
        if verbose:
            print(f"{msb_path.stem}: parsed")


def collect_live_rows(verbose: bool = False) -> dict:
    """The live_rows.json content: for each area, bank and row, which parts of which maps name the row."""
    live = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: {
        "maps": set(), "users": defaultdict(int), "place_names": set(),
        "collision_maps": Counter(), "piece_maps": Counter()})))
    for map_stem, msb in iter_msbs(verbose):
        area = "a" + map_stem[1:3]
        for list_name in PART_LISTS:
            for part in getattr(msb, list_name, []):
                for field_name, bank in PART_FIELD_TO_BANK.items():
                    row_id = getattr(part, field_name, -1)
                    if row_id is None or row_id < 0:
                        continue
                    rec = live[area][bank][str(row_id)]
                    rec["maps"].add(map_stem)
                    rec["users"][list_name] += 1
                    if list_name == "collisions":
                        rec["collision_maps"][map_stem] += 1
                        if getattr(part, "place_name_banner_id", -1) not in (-1, None):
                            rec["place_names"].add(int(part.place_name_banner_id))
                    elif list_name == "map_pieces" and bank in PIECE_BANKS:
                        rec["piece_maps"][map_stem] += 1

    def row_out(bank: str, r: dict) -> dict:
        out = {"maps": sorted(r["maps"]), "users": dict(r["users"]), "place_names": sorted(r["place_names"]),
               "collision_maps": dict(sorted(r["collision_maps"].items()))}
        if bank in PIECE_BANKS:
            out["piece_maps"] = dict(sorted(r["piece_maps"].items()))
        return out

    return {
        area: {
            bank: {rid: row_out(bank, r) for rid, r in sorted(rows.items(), key=lambda kv: int(kv[0]))}
            for bank, rows in banks.items()
        }
        for area, banks in sorted(live.items())
    }


def dump_live_rows() -> None:
    out = collect_live_rows(verbose=True)
    (DATA / "live_rows.json").write_text(json.dumps(out, indent=1), encoding="utf-8")


def main():
    names = dump_tables()
    ja = sorted(n for n in names if re.search(r"[^\x00-\x7f]", n))
    (DATA / "row_names_ja.json").write_text(json.dumps(ja, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(names)} unique row names, {len(ja)} non-ASCII (for translation)")
    dump_live_rows()


if __name__ == "__main__":
    main()
