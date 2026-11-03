#!/usr/bin/env python3
"""Can the engine tell the per-map override maps from the maps around them, using constants it already copies?

Two signals, both read from the built DrawParam and the MSB part usage (tools/extract.py):

1. c73, gFC_ShadowLightDir. The collision the player is on picks a ShadowBank row index, and each forward draw looks
   that index up in the DrawParam of its own piece's archive: draws of one archive agree, and a drawn map of another
   archive shows that archive's row of the same index, or none (captures/m1: at the Darkroot Basin, Valley of Drakes
   pieces showed a16 row 0, not a12 row 0). c73 = (cos X sin Y, -sin X, cos X cos Y) from lightDegRotX/Y
   (data/probe_constants.json:20 and the prior round's check). Every ShadowBank row that a collision names is turned
   into a direction, and directions within TOL_DEG of each other are joined (connected components, over every archive
   and slot). A collision counts as unique against a set of maps when its direction's group holds no row that a
   collision of those maps names. This report reads the player's own archive only; --simulate models the rest.
2. The map-piece tuple a forward draw carries from its piece's LightBank, FogBank and LightScatteringBank rows.
   Confirmed formulas only: c5 = the same direction formula from degRotX_s/Y_s (probe_constants.json:12) and
   c6 = pow(col_s/255 * colA_s/100, 2.2) (probe_constants.json:13). c12, c19 and c20 have no confirmed formula, so
   the "extended" tuple adds the raw FogBank and LightScatteringBank fields: identical fields give identical
   registers, so its unique share is an upper bound for the true c5+c6+c12+c19+c20 tuple.

The seamless-neighbour lists come from two places, and every share is given against both: GIVEN_NEIGHBOURS (game
knowledge, approximate) and the connect collisions in the MSBs (each names a map it loads alongside; a superset of
walkable borders, because some are load-screen moves, see spec/maps.toml). m12_00_00_00 and m12_00_00_01 are two
versions of one map; only one counts as m12_00 (--m12-variant).

Outputs (in --out, never in the repo): report.txt, maps_table.json, c73_groups.json, live_rows_by_map.json,
map_links.json, and build.log when it builds.

    .venv\\Scripts\\python.exe tools\\maps_table.py [--dp dist\\_v4\\sun] [--fresh] [--out DIR] [--m12-variant 01]

The detection table
-------------------
--emit PATH writes the table the engine's per-map settings read (engine/data/maps_detect.txt, git-ignored, shipped as
dsrle/maps_detect.txt) from the --dp DrawParam, the MSBs and spec/maps.toml. --simulate [PATH] reads a table back and
replays the resolver over every collision of every map. --emit runs the same replay on its own text before writing, and
both fail on a leak, on an empty POS set, on a hold the rule below does not allow, or on own-view coverage below
dist/maps_accepted.toml [own]; --emit then writes nothing. Their reports go to --out (emit.txt, simulate.txt). Here
both m12_00 MSBs count as m12_00. The replay's look-out view runs over walk neighbours only: two maps across a
load-screen edge of spec/maps.toml are taken as never drawn in one frame, an assumption no session has checked yet.

    .venv\\Scripts\\python.exe tools\\maps_table.py --emit engine\\data\\maps_detect.txt [--dp dist\\_v4\\sun]
    .venv\\Scripts\\python.exe tools\\maps_table.py --simulate [engine\\data\\maps_detect.txt] [--dp dist\\_v4\\sun]

maps_detect.txt, format 1. ASCII, one record per line, each line ended by "\n", fields separated by one space, no
blank lines and no comments. The records come in this order, each kind in one block, indexes counting up from 0:

    dsrle_maps_detect 1
    dp <area> <sha256>                        9 lines, a10 to a18: the param/DrawParam/<area>_DrawParam.parambnd.dcx
                                              the table was built from, as lowercase hex
    tol <dir_deg> <c6_rel> <c6_abs>
    counts <maps> <tuples> <groups> <dirs> <evs>
    map <i> <id> <override>                   id is mAA_BB; override is 1 for the maps with shipped [map.*] values
    tuple <i> <c5x> <c5y> <c5z> <c6r> <c6g> <c6b> <owners>
    group <g> <global_only> <claimants> <no_evidence>
    dir <g> <x> <y> <z>                       one or more per group, in group order
    ev <g> <map> <hold> <pos> <walk> <sep>    one per claimant of every group with two or more claimants, by g then map
    end

Indexes, counts and flags (0 or 1) are unsigned decimal integers. Floats are plain decimal with nine digits after the
point and an optional leading minus, never -0.000000000 and never an exponent. A list is indexes in ascending order
joined by commas, or "-" when empty: <owners>, <claimants> and <no_evidence> hold map indexes, <pos>, <walk> and <sep>
tuple indexes.

What the records mean:
- c73 is in group g when the angle between c73 and one of g's dir vectors is at most dir_deg (dot >= cos dir_deg; the
  vectors are unit length). Directions of different groups are more than 2 * dir_deg apart, so at most one group can
  match. No match is an unknown direction. The engine matches the c73 of every voting draw on its own: the frame's
  group is the group with the most draws when it holds at least [maps] c73_share of the draws that matched one; when
  it does not, the frame is mixed (the globals, L untouched); when no draw matched, it is unknown.
- A forward draw's c5/c6 pair is tuple i when the angle between c5 and the tuple's c5 is at most dir_deg and, per
  channel, |c6 - c6_i| <= c6_abs + c6_rel * max(|c6|, |c6_i|). Tuples one value could both match have the same owners
  and sit in the same sets, so the first match is as good as any. owners (the maps whose parts carry it) is for the log.
- claimants: the maps whose collisions name a ShadowBank row in the group, plus no_evidence, the maps of an archive
  holding a row in the group's direction that no collision names (a cutscene or event row). They are rivals with no
  evidence of their own. A row in the archive of an override claimant of the group adds nobody. Then the cross-archive
  claimants: a map P whose collision names row r, when a connect neighbour Y of another archive has its row r (same
  a15 slot) in the group, since Y's draws show the group while the player stands in P. They join no_evidence: rivals
  whose tuples and neighbours count against the other claimants, never voted or held themselves. One claimant: unique.
- ev: pos, walk and sep are POS(g,map), WALK(g,map) and SEP(g,map) of the design, with one addition to sep: every
  neighbour Y that brought a cross-archive claimant into g adds the tuples its parts carry and the map's do not (per
  slot), even when Y is a walk neighbour of the map. Y is then in view on a frame in g that is not the map's own. hold
  is 1 when no walk rival of the map can show g without a WALK draw, so a hold is allowed and walk_share does not
  apply: the map has no walk rival in g, or each one has no ShadowBank row of its own archive in g (a cross-archive
  claimant, which shows g only in other archives' parts) and, on every collision whose row lies in g in a connect
  neighbour's archive, a light row in WALK, which the player's body draws there. --simulate checks that again. A
  rival behind a load screen never blocks a hold: the hold relies on that load to clear L, as where Oolacile's body
  and some of its parts carry only light rows the Darkroot Garden's parts carry too (spec/maps.toml, the Manus
  portal). A map that is not an override has hold 0 and walk and sep "-"; a no_evidence map has all four empty.
- global_only 1: the resolver gives the globals in g with no vote and no hold, and leaves L as it is. Its ev lines are
  there for the log only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import subprocess
import sys
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from soulstruct.darksouls1r.params.draw_param import DrawParamBND  # noqa: E402

import extract  # noqa: E402
from dp import AREAS, ROOT, get_field, internal_to_attr, iter_params  # noqa: E402

SPEC = tomllib.loads((ROOT / "spec" / "maps.toml").read_text(encoding="utf-8"))
# The own-view coverage the guard accepts per override map. Counted from the MSBs, so it stays in git-ignored dist/.
ACCEPTED = ROOT / "dist" / "maps_accepted.toml"
TOL_DEG = SPEC["tolerance"]["dir_deg"]
C6_REL_TOL = SPEC["tolerance"]["c6_rel"]
C6_ABS_TOL = SPEC["tolerance"]["c6_abs"]

OVERRIDES = {
    "m10_00": "Depths",
    "m10_01": "Undead Burg and Parish",
    "m12_00": "Darkroot Garden and Basin",
    "m15_01": "Anor Londo",
}
LABELS = {
    "m10_02": "Firelink", "m11_00": "Painted World", "m12_01": "Oolacile", "m13_00": "Catacombs",
    "m13_01": "Tomb of Giants", "m13_02": "Great Hollow and Ash Lake", "m14_00": "Blighttown",
    "m14_01": "Demon Ruins and Izalith", "m15_00": "Sen's Fortress", "m16_00": "New Londo and Valley of Drakes",
    "m17_00": "Duke's Archives", "m18_00": "Kiln", "m18_01": "Undead Asylum", **OVERRIDES,
}
# Walkable without a loading screen, from game knowledge (approximate; m15_01's two are uncertain). Made symmetric.
GIVEN_NEIGHBOURS = {
    "m10_00": {"m10_01", "m14_00"},
    "m10_01": {"m10_00", "m10_02", "m12_00", "m15_00"},
    "m10_02": {"m10_01", "m13_00", "m16_00"},
    "m12_00": {"m10_01", "m16_00"},
    "m14_00": {"m10_00", "m16_00"},
    "m14_01": {"m10_00", "m16_00"},
    "m15_01": {"m15_00", "m17_00"},
    "m12_01": set(),
}
# Connect-collision pairs that are load-screen moves by game knowledge, so they are not walkable borders.
LOAD_SCREEN = {frozenset(edge["maps"]): edge["via"] for edge in SPEC["load_screen"]}


def symmetric(graph: dict[str, set[str]]) -> dict[str, set[str]]:
    out = defaultdict(set)
    for a, bs in graph.items():
        for b in bs:
            if a != b:
                out[a].add(b)
                out[b].add(a)
    return out


def direction(x_deg: float, y_deg: float) -> tuple[float, float, float]:
    x, y = math.radians(x_deg), math.radians(y_deg)
    return (math.cos(x) * math.sin(y), -math.sin(x), math.cos(x) * math.cos(y))


def angle_deg(a, b) -> float:
    dot = sum(p * q for p, q in zip(a, b))
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def c6_of(row) -> tuple[float, float, float]:
    strength = get_field(row, "colA_s")
    return tuple((max(get_field(row, f"col{c}_s"), 0) / 255 * max(strength, 0) / 100) ** 2.2 for c in "RGB")


def c6_close(a, b, scale: float = 1.0) -> bool:
    return all(abs(p - q) <= scale * (C6_ABS_TOL + C6_REL_TOL * max(abs(p), abs(q))) for p, q in zip(a, b))


def raw_fields(row) -> tuple:
    out = []
    for internal in internal_to_attr(type(row)):
        if internal.startswith("pad") or internal.startswith("dummy"):
            continue
        v = get_field(row, internal)
        out.append(round(v, 5) if isinstance(v, float) else v)
    return tuple(out)


# ---------------------------------------------------------------------------------------------------------------------
# Inputs


def dp_files(dp_dir: Path) -> list[Path]:
    return [dp_dir / "param" / "DrawParam" / f"{area}_DrawParam.parambnd.dcx" for area in AREAS]


def build(dp_dir: Path, log: Path) -> None:
    print(f"building the DrawParam into {dp_dir} (log: {log})")
    with log.open("w", encoding="utf-8") as fh:
        subprocess.run([sys.executable, str(ROOT / "tools" / "build_v4.py"), "--out", str(dp_dir)],
                       cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, check=True)


def staleness(dp_dir: Path) -> str | None:
    """Say so when a build input is newer than the oldest built file."""
    inputs = [*(p for p in ROOT.glob("spec/**/*.toml") if p.name != "maps.toml"),  # maps.toml is this tool's own
              ROOT / "tools" / "build.py", ROOT / "tools" / "build_v4.py", ROOT / "data" / "live_rows.json"]
    newest = max(inputs, key=lambda p: p.stat().st_mtime)
    oldest = min(dp_files(dp_dir), key=lambda p: p.stat().st_mtime)
    if newest.stat().st_mtime > oldest.stat().st_mtime:
        return f"{newest.relative_to(ROOT)} is newer than {oldest.name}: {dp_dir} may be out of date (try --fresh)"
    return None


def load_dp(dp_dir: Path) -> dict[tuple[str, int, str], object]:
    params = {}
    for area, path in zip(AREAS, dp_files(dp_dir)):
        for slot, stem, param in iter_params(DrawParamBND.from_path(path)):
            params[(area, slot, stem)] = param
    return params


def slots_of(params, area: str) -> list[int]:
    return sorted({slot for (a, slot, stem) in params if a == area and stem == "ShadowBank"})


def signal_diff(a, b) -> Counter:
    """Fields that differ between two builds, on the rows and fields the two signals read."""
    fields = {"ShadowBank": ("lightDegRotX", "lightDegRotY"),
              "LightBank": ("degRotX_s", "degRotY_s", "colR_s", "colG_s", "colB_s", "colA_s"),
              "FogBank": None, "LightScatteringBank": None}
    diff = Counter()
    for key, pa in a.items():
        if key[2] not in fields or key not in b:
            continue
        pb = b[key]
        for rid, ra in pa.rows.items():
            rb = pb.rows.get(rid)
            if rb is None:
                continue
            names = fields[key[2]] or [n for n in internal_to_attr(type(ra)) if not n.startswith("pad")]
            for name in names:
                if get_field(ra, name) != get_field(rb, name):
                    diff[f"{key[0]} slot {key[1]} {key[2]}.{name}"] += 1
    return diff


def scan_msbs(unit_of) -> tuple[dict, dict, dict]:
    """Per map unit: Counter of map-piece (light, fog, scatter) row triples, connect targets, and part counts."""
    triples, links, counts = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    for stem, msb in extract.iter_msbs():
        unit = unit_of(stem)
        if unit is None:
            continue
        for part in msb.map_pieces:
            triples[unit][(part.ambient_light_id, part.fog_id, part.scattered_light_id)] += 1
        for cc in msb.connect_collisions:
            aa, bb = cc.connected_map_id[:2]
            links[unit][f"m{aa:02d}_{bb:02d}"] += 1
        counts[unit].update(map_pieces=len(msb.map_pieces), collisions=len(msb.collisions),
                            connect_collisions=len(msb.connect_collisions))
    return triples, links, counts


# ---------------------------------------------------------------------------------------------------------------------
# Signal 1: c73


def c73_groups(params, shadow_users: dict[str, dict[int, Counter]]):
    """Join every collision-named ShadowBank row, in every slot, into direction groups."""
    nodes = []  # (area, slot, row)
    dirs = {}
    for area, rows in shadow_users.items():
        for slot in slots_of(params, area):
            bank = params[(area, slot, "ShadowBank")]
            for rid in rows:
                if rid not in bank.rows:
                    continue
                r = bank.rows[rid]
                node = (area, slot, rid)
                nodes.append(node)
                dirs[node] = (get_field(r, "lightDegRotX"), get_field(r, "lightDegRotY"))
    vec = {n: direction(*dirs[n]) for n in nodes}
    parent = {n: n for n in nodes}

    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    cos_tol = math.cos(math.radians(TOL_DEG))
    for i, a in enumerate(nodes):
        va = vec[a]
        for b in nodes[i + 1:]:
            if sum(p * q for p, q in zip(va, vec[b])) >= cos_tol:
                parent[find(a)] = find(b)
    members = defaultdict(list)
    for n in nodes:
        members[find(n)].append(n)
    groups = []
    for ms in members.values():
        ms.sort()
        maps = Counter()
        for area, rid in {(m[0], m[2]) for m in ms}:  # a row in both a15 slots counts its collisions once
            maps.update(shadow_users[area][rid])
        diameter = max((angle_deg(vec[a], vec[b]) for a in ms for b in ms), default=0.0)
        groups.append({"members": ms, "angles": [dirs[m] for m in ms], "maps": maps, "diameter": diameter,
                       "dir": vec[ms[0]]})
    groups.sort(key=lambda g: (-sum(g["maps"].values()), g["members"]))
    group_of = {m: gi for gi, g in enumerate(groups) for m in g["members"]}
    return groups, group_of, dirs


def ranked(c: Counter) -> dict:
    return dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))


def same_time(area: str, slot: int, other_area: str, other_slot: int) -> bool:
    """Can these two rows be live together? One archive is in one slot at a time (a15: slot 1 is dark Anor Londo)."""
    return other_area != area or other_slot == slot


def c73_for_map(unit, params, shadow_users, groups, group_of, dirs, sets: dict[str, set[str]]):
    """A collision is unique against a set when, in every slot, its direction group holds no row the set's maps use
    (rows of the same archive count only from the same slot)."""
    area = "a" + unit[1:3]
    rows = {rid: users[unit] for rid, users in shadow_users.get(area, {}).items() if users.get(unit)}
    unique = Counter()
    shared_with = defaultdict(Counter)  # set name -> other map -> this map's collisions sharing a direction with it
    row_lines = []
    for rid, n in sorted(rows.items()):
        nodes = [(area, slot, rid) for slot in slots_of(params, area) if (area, slot, rid) in group_of]
        rows_seen = {(a2, r2) for node in nodes for (a2, s2, r2) in groups[group_of[node]]["members"]
                     if same_time(area, node[1], a2, s2)}
        group_maps = Counter()
        for a2, r2 in rows_seen:
            group_maps.update(shadow_users[a2][r2])
        others = {m for m in group_maps if m != unit}
        for name, s in sets.items():
            hit = others & s
            if not hit:
                unique[name] += n
            for m in hit:
                shared_with[name][m] += n
        row_lines.append({"row": rid, "collisions": n, "angles": [dirs[node] for node in nodes],
                          "groups": [group_of[node] for node in nodes],
                          "others": {m: group_maps[m] for m in sorted(others)}})
    return {"collisions": sum(rows.values()), "unique": dict(unique),
            "shared_with": {k: ranked(v) for k, v in shared_with.items()}, "rows": row_lines}


# ---------------------------------------------------------------------------------------------------------------------
# Signal 2: the map-piece tuple


def piece_signature(params, area, slot, triple):
    light, fog, scat = triple
    lb = params[(area, slot, "LightBank")].rows.get(light)
    fb = params[(area, slot, "FogBank")].rows.get(fog)
    sb = params[(area, slot, "LightScatteringBank")].rows.get(scat)
    if lb is None:
        return None
    return {"c5": direction(get_field(lb, "degRotX_s"), get_field(lb, "degRotY_s")), "c6": c6_of(lb),
            "fog": raw_fields(fb) if fb is not None else None,
            "scat": raw_fields(sb) if sb is not None else None}


def sig_match(a, b, extended: bool) -> bool:
    if angle_deg(a["c5"], b["c5"]) > TOL_DEG or not c6_close(a["c6"], b["c6"]):
        return False
    return not extended or (a["fog"] == b["fog"] and a["scat"] == b["scat"])


def sig_key(s) -> tuple:
    return (tuple(round(v, 6) for v in s["c5"]), tuple(round(v, 6) for v in s["c6"]), s["fog"], s["scat"])


def unit_signatures(params, unit, triples) -> list[tuple[str, int, dict]]:
    """Every distinct (area, slot, tuple) this map's pieces carry, in every slot of its archive."""
    area = "a" + unit[1:3]
    seen = {}
    for t in triples[unit]:
        for slot in slots_of(params, area):
            s = piece_signature(params, area, slot, t)
            if s is not None:
                seen.setdefault((slot, sig_key(s)), (area, slot, s))
    return list(seen.values())


def pieces_for_map(unit, params, triples, sigs, sets: dict[str, set[str]]):
    """A piece is unique against a set when, in every slot, no piece of the set's maps carries a matching tuple
    (pieces of the same archive count only from the same slot)."""
    area = "a" + unit[1:3]
    out = {"pieces": sum(triples[unit].values()),
           "no_light_row": sum(n for t, n in triples[unit].items()
                               if any(piece_signature(params, area, s, t) is None for s in slots_of(params, area)))}
    for extended in (False, True):
        tag = "extended" if extended else "confirmed"
        unique = Counter()
        shared_with = defaultdict(Counter)
        shared_rows = []
        for triple, n in sorted(triples[unit].items(), key=lambda kv: -kv[1]):
            slots = slots_of(params, area)
            mine = [piece_signature(params, area, slot, triple) for slot in slots]
            if any(s is None for s in mine):
                continue
            matched_in = {slot: {other for other, other_sigs in sigs.items() if other != unit and
                                 any(sig_match(m, o, extended) for oa, os_, o in other_sigs
                                     if same_time(area, slot, oa, os_))}
                          for slot, m in zip(slots, mine)}
            matched = set().union(*matched_in.values())
            for name, s in sets.items():
                hit = matched & s
                if not hit:
                    unique[name] += n
                for m in hit:
                    shared_with[name][m] += n
                if len(slots) > 1:
                    for slot in slots:
                        unique[f"{name} slot {slot}"] += 0 if matched_in[slot] & s else n
            if matched & sets["connect"]:
                shared_rows.append({"light_fog_scatter": list(triple), "pieces": n,
                                    "by_slot": {slot: {"degRot_s": [get_field(params[(area, slot, "LightBank")].rows[
                                                                        triple[0]], f) for f in ("degRotX_s", "degRotY_s")],
                                                       "c6_max": round(max(m["c6"]), 4),
                                                       "with": sorted(matched_in[slot] & sets["connect"])}
                                                for slot, m in zip(slots, mine)}})
        out[tag] = {"unique": dict(unique), "shared_with": {k: ranked(v) for k, v in shared_with.items()},
                    "shared_rows": shared_rows}
    return out


# ---------------------------------------------------------------------------------------------------------------------
# The detection table (--emit) and the rule replay (--simulate)

FORMAT = "dsrle_maps_detect 1"
DETECT = ROOT / "engine" / "data" / "maps_detect.txt"
# A tuple's owners: every part that draws with its LightBank row. A collision's row is the player's body.
OWNER_KINDS = ("map_pieces", "objects", "collisions", "characters")
VOTE_KINDS = ("map_pieces", "objects")  # what --simulate puts on screen for a map in view
VIEWS = ("own", "walk", "conn")


def num(v: float) -> str:
    s = f"{v:.9f}"
    return "0.000000000" if s == "-0.000000000" else s


def id_list(ids) -> str:
    return ",".join(str(i) for i in sorted(ids)) or "-"


def world_slot(params, area: str, world: int) -> int:
    """The slot an archive is read from while a15 is in slot `world`. Every other archive has slot 0 only."""
    slots = slots_of(params, area)
    return world if world in slots else slots[0]


def light_sig(params, area: str, slot: int, lid: int):
    lb = params[(area, slot, "LightBank")].rows.get(lid)
    if lb is None:
        return None
    c5 = tuple(float(num(v)) for v in direction(get_field(lb, "degRotX_s"), get_field(lb, "degRotY_s")))
    return c5, tuple(float(num(v)) for v in c6_of(lb))


def shadow_dir(params, area: str, slot: int, rid: int) -> tuple[float, float, float]:
    r = params[(area, slot, "ShadowBank")].rows[rid]
    return tuple(float(num(v)) for v in direction(get_field(r, "lightDegRotX"), get_field(r, "lightDegRotY")))


@dataclass
class Scan:
    units: list[str]
    parts: dict  # map -> kind -> Counter(LightBank row -> parts)
    colls: dict  # map -> Counter((name, ShadowBank row, LightBank row) -> collisions)
    no_row: Counter  # map -> parts that name no LightBank row
    connect: dict
    walk: dict


def scan_detect() -> Scan:
    """Every part's LightBank row and every collision's rows, per map. The two m12_00 MSBs are one map: a part both
    carry with the same rows counts once."""
    keys = defaultdict(Counter)
    links = defaultdict(set)
    no_row = Counter()
    for stem, msb in extract.iter_msbs():
        unit, mine = stem[:6], Counter()
        for kind in OWNER_KINDS:
            for part in getattr(msb, kind):
                lid = part.ambient_light_id
                if lid is None or lid < 0:
                    no_row[unit] += 1
                elif kind == "collisions":
                    mine[(kind, part.name, part.shadow_id, lid)] += 1
                else:
                    mine[(kind, part.name, None, lid)] += 1
        keys[unit] |= mine
        for cc in msb.connect_collisions:
            aa, bb = cc.connected_map_id[:2]
            links[unit].add(f"m{aa:02d}_{bb:02d}")
    parts = defaultdict(lambda: defaultdict(Counter))
    colls = defaultdict(Counter)
    for unit, c in keys.items():
        for (kind, name, sid, lid), n in c.items():
            parts[unit][kind][lid] += n
            if kind == "collisions":
                colls[unit][(name, sid, lid)] += n
    units = sorted(keys)
    connect = symmetric({u: set(v) for u, v in links.items()})
    for edge in LOAD_SCREEN:
        a, b = sorted(edge)
        if b not in connect.get(a, set()):
            raise SystemExit(f"spec/maps.toml: {a}-{b} is a load-screen edge, but no connect collision links them")
    walk = {u: {v for v in connect.get(u, set()) if frozenset((u, v)) not in LOAD_SCREEN} for u in units}
    return Scan(units, parts, colls, no_row, {u: connect.get(u, set()) for u in units}, walk)


def check_rows(params, scan: Scan) -> None:
    """Every row a collision or a part names must exist in every slot of its archive."""
    for u in scan.units:
        area = "a" + u[1:3]
        for slot in slots_of(params, area):
            for name, sid, lid in scan.colls[u]:
                if sid is None or sid < 0 or sid not in params[(area, slot, "ShadowBank")].rows:
                    raise SystemExit(f"{u} collision {name} names ShadowBank row {sid}, which {area} slot {slot} lacks")
            for kind in OWNER_KINDS:
                for lid in scan.parts[u][kind]:
                    if lid not in params[(area, slot, "LightBank")].rows:
                        raise SystemExit(f"{u} {kind} name LightBank row {lid}, which {area} slot {slot} lacks")


def near(a, b, scale: float) -> bool:
    return angle_deg(a[0], b[0]) <= scale * TOL_DEG and c6_close(a[1], b[1], scale)


def build_detect(params, scan: Scan, hashes: dict[str, str]) -> tuple[str, list[str], list[str]]:
    """The table's text, report lines and failures. One table serves both a15 slots, because the engine cannot tell
    which is loaded: POS takes a tuple only when no rival owns it in either slot, and WALK and SEP join both slots'."""
    units, report, failures = scan.units, [], []
    index = {u: i for i, u in enumerate(units)}

    # Tuples. Values within twice the tolerance join one class and share its owners, so no runtime value can match
    # tuples that disagree.
    sig_of = {}
    for world in (0, 1):
        for u in units:
            area = "a" + u[1:3]
            slot = world_slot(params, area, world)
            for kind in OWNER_KINDS:
                for lid in scan.parts[u][kind]:
                    sig_of[(world, u, lid)] = light_sig(params, area, slot, lid)
    values = sorted(set(sig_of.values()))
    rec = {v: i for i, v in enumerate(values)}
    parent = list(range(len(values)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(values):
        for j in range(i + 1, len(values)):
            if near(a, values[j], 2.0):
                parent[find(i)] = find(j)
    cls = [find(i) for i in range(len(values))]
    T = {w: defaultdict(set) for w in (0, 1)}  # world -> map -> classes its parts carry
    for (world, u, lid), s in sig_of.items():
        T[world][u].add(cls[rec[s]])

    def TU(world, us):
        return set().union(*(T[world][u] for u in us)) if us else set()

    def records(classes):
        return {i for i, c in enumerate(cls) if c in classes}

    # Direction groups: collision-named rows, both slots, joined within the tolerance.
    shadow_users = defaultdict(lambda: defaultdict(Counter))
    for u in units:
        for (name, sid, lid), n in scan.colls[u].items():
            shadow_users["a" + u[1:3]][sid][u] += n
    groups, group_of, _ = c73_groups(params, shadow_users)
    claim = [set(g["maps"]) for g in groups]
    dirs = [[shadow_dir(params, *m) for m in g["members"]] for g in groups]
    no_ev = [set() for _ in groups]

    def groups_near(v):
        c = math.cos(math.radians(TOL_DEG))
        return [gi for gi, ds in enumerate(dirs) if any(sum(p * q for p, q in zip(v, d)) >= c for d in ds)]

    # Rows no collision names but that share an override map's direction: their archive's maps become claimants with
    # no evidence (a cutscene or event row could turn the direction on there), unless the row is an override
    # claimant's own.
    report.append("ShadowBank rows no collision names, in an override map's direction:")
    for (area, slot, stem), bank in sorted(params.items()):
        if stem != "ShadowBank":
            continue
        for rid in sorted(bank.rows):
            if (area, slot, rid) in group_of:
                continue
            v = shadow_dir(params, area, slot, rid)
            hits = groups_near(v)
            if len(hits) > 1:
                failures.append(f"{area} slot {slot} ShadowBank {rid} lies within {TOL_DEG} deg of groups {hits}")
                continue
            if not hits or not claim[hits[0]] & set(OVERRIDES):
                continue
            gi = hits[0]
            own = sorted(m for m in claim[gi] & set(OVERRIDES) if "a" + m[1:3] == area)
            added = sorted(u for u in units if "a" + u[1:3] == area and u not in claim[gi])
            if not own:
                no_ev[gi] |= set(added)
            if v not in dirs[gi]:
                dirs[gi].append(v)
            report.append(f"  {area} slot {slot} row {rid} -> group {gi}: " +
                          (f"{own[0]}'s own row, adds nobody" if own else f"no-evidence claimants {added or '-'}"))
    for gi in range(len(groups)):
        claim[gi] |= no_ev[gi]

    # Cross-archive claimants. Each drawn part shows the row of the collision's index in its own archive, so standing on
    # a collision of P that names row r, a frame that a connect neighbour Y of another archive fills lands in the group
    # of Y's row r (same slot). P joins the group as a no-evidence claimant: a rival whose tuples and neighbours count
    # against the other claimants, never voted or held itself. Y's tuples join the other claimants' SEP below. This runs
    # after the pass above, so that an override map claiming a group only across archives never keeps a row of its own
    # archive from adding no-evidence claimants.
    cross = defaultdict(lambda: defaultdict(set))  # group -> P -> (Y, row r) that put it there
    for p in units:
        area = "a" + p[1:3]
        sids = sorted({sid for (_, sid, _) in scan.colls[p]})
        for y in sorted(scan.connect[p]):
            y_area = "a" + y[1:3]
            if y_area == area:
                continue  # the same archive shows P's own rows
            for world in (0, 1):
                slot = world_slot(params, y_area, world)
                for sid in sids:
                    if sid not in params[(y_area, slot, "ShadowBank")].rows:
                        continue
                    hits = groups_near(shadow_dir(params, y_area, slot, sid))
                    if len(hits) == 1 and (p not in claim[hits[0]] or p in no_ev[hits[0]]):
                        cross[hits[0]][p].add((y, sid))
    via = defaultdict(set)  # group -> the neighbours Y that brought a cross-archive claimant into it
    for gi, by_map in cross.items():
        claim[gi] |= set(by_map)
        no_ev[gi] |= set(by_map)
        via[gi] = {y for pairs in by_map.values() for y, _ in pairs}

    # By the maps whose collisions name the direction, not by angle or row, so the spec holds no value from the game.
    # Exactly one group, so a later build cannot quietly widen it.
    global_only = [False] * len(groups)
    for entry in SPEC.get("global_only", []):
        want = set(entry["maps"])
        hits = [gi for gi, g in enumerate(groups) if want <= set(g["maps"])]
        if len(hits) != 1:
            failures.append(f"spec/maps.toml global_only {sorted(want)} matches direction groups {hits}, not one")
        for gi in hits:
            global_only[gi] = True

    for gi, ds in enumerate(dirs):
        for gj in range(gi + 1, len(dirs)):
            if any(angle_deg(a, b) <= 2 * TOL_DEG for a in ds for b in dirs[gj]):
                failures.append(f"groups {gi} and {gj} have directions within {2 * TOL_DEG} deg of each other")

    # The groups the ShadowBank rows of each archive lie in, every slot and row, named by a collision or not.
    area_groups = defaultdict(set)
    for (area, slot, stem), bank in params.items():
        if stem == "ShadowBank":
            for rid in bank.rows:
                area_groups[area].update(groups_near(shadow_dir(params, area, slot, rid)))

    def body_is_walk(r: str, gi: int, walk_ev: set) -> bool:
        """Whether every frame in gi while the player stands in walk rival r holds a WALK draw, the body's. No row of
        r's own archive lies in gi, so r's body and parts never show it: a frame there is in gi only through the parts
        of a connect neighbour of another archive whose row of the collision's index lies in gi, as the cross-archive
        claimants above. Each collision of r that such a row puts in gi must carry a light row in walk_ev, in both
        slots."""
        if gi in area_groups["a" + r[1:3]]:
            return False
        for world in (0, 1):
            for y in scan.connect[r]:
                y_area = "a" + y[1:3]
                slot = world_slot(params, y_area, world)
                rows = params[(y_area, slot, "ShadowBank")].rows
                for _, sid, lid in scan.colls[r]:
                    if sid in rows and gi in groups_near(shadow_dir(params, y_area, slot, sid)) and \
                            cls[rec[sig_of[(world, r, lid)]]] not in walk_ev:
                        return False
        return True

    # Evidence, per claimant of every shared group.
    evs = {}
    for gi, S in enumerate(claim):
        if len(S) < 2:
            continue
        for x in sorted(S):
            if x in no_ev[gi]:
                evs[(gi, x)] = (0, set(), set(), set())
                continue
            rivals = S - {x}
            w_set = set().union(*({r} | scan.connect[r] for r in rivals)) - {x}
            pos = (TU(0, {x}) | TU(1, {x})) - (TU(0, w_set) | TU(1, w_set))
            if x not in OVERRIDES:
                evs[(gi, x)] = (0, pos, set(), set())
                continue
            wr = rivals & scan.walk[x]
            sr = rivals - wr
            # WALK, not an empty POS, is what keeps a walk rival on the globals. Blighttown shares the Depths'
            # straight-down direction, and the light rows of nearly all its pieces and of every collision it has
            # there, which the player's body carries, are its own, so a frame there shows WALK draws. That relies on
            # character draws voting.
            walk_ev = {c for w in (0, 1) for r in wr for c in T[w][r] - T[w][x]}
            sep = {c for w in (0, 1) for r in sr for c in TU(w, {r} | scan.walk[r]) - TU(w, {x} | scan.walk[x])}
            # A neighbour Y that brought a cross-archive claimant in is in view on that claimant's frames in g, so its
            # tuples that x lacks are evidence against x, even when Y is a walk neighbour of x: a frame showing Y and
            # nothing of x's own then blocks a hold and weighs against a vote.
            sep |= {c for w in (0, 1) for y in via[gi] for c in T[w][y] - T[w][x]}
            if pos & (walk_ev | sep):
                failures.append(f"group {gi} {x}: POS meets WALK or SEP")
            # A hold is allowed, and walk_share waived, where no walk rival can show g without a WALK draw: there is
            # none, or each sees g only in other archives' parts and stands there on a body in WALK. The Burg and the
            # Valley are such rivals of the Darkroot Garden: they show its direction only in its own parts, whose POS
            # rows are almost only its trees, so walk_share would refuse the Garden in most of its own frames and
            # nothing would hold it when the view turns from the trees. Like Blighttown's case this relies on the
            # player's body voting, and a DrawParam edit that took a body out of WALK turns the hold off here. Against
            # a SEP rival it relies on the load between them clearing L (Oolacile: spec/maps.toml).
            evs[(gi, x)] = (int(all(body_is_walk(r, gi, walk_ev) for r in wr)), pos, walk_ev, sep)

    owners = [{u for w in (0, 1) for u in units if cls[i] in T[w][u]} for i in range(len(values))]
    lines = [FORMAT]
    lines += [f"dp {area} {hashes[area]}" for area in AREAS]
    lines.append(f"tol {num(TOL_DEG)} {num(C6_REL_TOL)} {num(C6_ABS_TOL)}")
    lines.append(f"counts {len(units)} {len(values)} {len(groups)} {sum(map(len, dirs))} {len(evs)}")
    lines += [f"map {i} {u} {int(u in OVERRIDES)}" for i, u in enumerate(units)]
    lines += [f"tuple {i} {' '.join(num(v) for v in (*c5, *c6))} {id_list(index[u] for u in owners[i])}"
              for i, (c5, c6) in enumerate(values)]
    lines += [f"group {gi} {int(global_only[gi])} {id_list(index[u] for u in claim[gi])} "
              f"{id_list(index[u] for u in no_ev[gi])}" for gi in range(len(groups))]
    lines += [f"dir {gi} {' '.join(num(v) for v in d)}" for gi, ds in enumerate(dirs) for d in ds]
    lines += [f"ev {gi} {index[x]} {hold} {id_list(records(pos))} {id_list(records(walk_ev))} {id_list(records(sep))}"
              for (gi, x), (hold, pos, walk_ev, sep) in sorted(evs.items(), key=lambda kv: (kv[0][0], index[kv[0][1]]))]
    lines.append("end")

    report.append(f"tuples: {len(values)} values in {len(set(cls))} classes (a class joins values within "
                  f"{2 * TOL_DEG} deg and twice the c6 tolerance)")
    if scan.no_row:
        report.append(f"parts naming no LightBank row (they vote nothing): {dict(scan.no_row)}")
    report.append("direction groups with an override claimant or two or more claimants:")
    for gi, S in enumerate(claim):
        if len(S) < 2 and not S & set(OVERRIDES):
            continue
        rows = ", ".join(f"{m[0]}/{m[1]}/{m[2]}" for m in groups[gi]["members"])
        seen_via = "; ".join(f"{m} via {', '.join(f'{y} row {r}' for y, r in sorted(v))}"
                             for m, v in sorted(cross[gi].items()))
        report.append(f"  group {gi}{' global_only' if global_only[gi] else ''}: claimants {sorted(S)}"
                      f"{f' (no evidence: {sorted(no_ev[gi])})' if no_ev[gi] else ''}"
                      f"{f' (cross-archive: {seen_via})' if seen_via else ''}; rows {rows}")
        for x in sorted(S):
            if (gi, x) in evs and x in OVERRIDES and x not in no_ev[gi]:
                hold, pos, walk_ev, sep = evs[(gi, x)]
                wr = sorted((S - {x}) & scan.walk[x])
                why = f" (walk rivals {', '.join(wr)} see it only in other archives' parts, over a body in WALK)" \
                    if hold and wr else ""
                report.append(f"    {x}: POS {len(records(pos))} "
                              f"WALK {len(records(walk_ev))} SEP {len(records(sep))} tuples, hold {hold}{why}; "
                              f"parts carrying a POS tuple: " + ", ".join(
                                  f"{k} {sum(n for lid, n in scan.parts[x][k].items() if cls[rec[sig_of[(0, x, lid)]]] in pos)}"
                                  f"/{sum(scan.parts[x][k].values())}" for k in ("map_pieces", "objects", "collisions")))
    return "\n".join(lines) + "\n", report, failures


@dataclass
class Evidence:
    hold: bool
    pos: frozenset
    walk: frozenset
    sep: frozenset


@dataclass
class Group:
    global_only: bool
    claimants: list[int]
    no_evidence: set[int]
    dirs: list[tuple]
    ev: dict  # map index -> Evidence


@dataclass
class Table:
    dp: dict[str, str]
    tol: tuple[float, float, float]
    maps: list[str]
    override: list[bool]
    tuples: list[tuple]  # (c5, c6, owners)
    groups: list[Group]


def parse_detect(text: str) -> Table:
    """The reference reader of the format in the module docstring. Anything else is refused."""
    if not text.isascii() or "\r" in text or not text.endswith("\n"):
        raise SystemExit("maps_detect: not ASCII text with \\n line ends")
    lines = text[:-1].split("\n")
    pos = 0

    def take(tag: str, n: int) -> list[str]:
        nonlocal pos
        if pos >= len(lines):
            raise SystemExit(f"maps_detect: ends before a {tag} line")
        f = lines[pos].split(" ")
        if f[0] != tag or len(f) != n + 1 or "" in f:
            raise SystemExit(f"maps_detect line {pos + 1}: expected '{tag}' with {n} fields: {lines[pos]!r}")
        pos += 1
        return f[1:]

    def uint(s: str) -> int:
        if not s.isdigit() or (len(s) > 1 and s[0] == "0"):
            raise SystemExit(f"maps_detect line {pos}: not an index: {s!r}")
        return int(s)

    def flt(s: str) -> float:
        body = s[1:] if s.startswith("-") else s
        whole, dot, frac = body.partition(".")
        if not (whole.isdigit() and dot and len(frac) == 9 and frac.isdigit()) or s == "-0.000000000":
            raise SystemExit(f"maps_detect line {pos}: not a nine-decimal number: {s!r}")
        return float(s)

    def ids(s: str, limit: int) -> list[int]:
        if s == "-":
            return []
        out = [uint(x) for x in s.split(",")]
        if out != sorted(set(out)) or out[-1] >= limit:
            raise SystemExit(f"maps_detect line {pos}: bad list {s!r}")
        return out

    if lines[0] != FORMAT:
        raise SystemExit(f"maps_detect: first line is {lines[0]!r}, not {FORMAT!r}")
    pos = 1
    dp = {}
    for area in AREAS:
        a, h = take("dp", 2)
        if a != area or len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
            raise SystemExit(f"maps_detect line {pos}: expected the {area} hash")
        dp[a] = h
    tol = tuple(flt(s) for s in take("tol", 3))
    n_maps, n_tuples, n_groups, n_dirs, n_ev = (uint(s) for s in take("counts", 5))
    maps, override = [], []
    for i in range(n_maps):
        f = take("map", 3)
        if uint(f[0]) != i or len(f[1]) != 6 or f[2] not in ("0", "1"):
            raise SystemExit(f"maps_detect line {pos}: bad map record")
        maps.append(f[1])
        override.append(f[2] == "1")
    tuples = []
    for i in range(n_tuples):
        f = take("tuple", 8)
        if uint(f[0]) != i:
            raise SystemExit(f"maps_detect line {pos}: tuple {f[0]} out of order")
        v = [flt(s) for s in f[1:7]]
        tuples.append((tuple(v[:3]), tuple(v[3:]), ids(f[7], n_maps)))
    groups = []
    for gi in range(n_groups):
        f = take("group", 4)
        if uint(f[0]) != gi or f[1] not in ("0", "1"):
            raise SystemExit(f"maps_detect line {pos}: bad group record")
        claimants, no_evidence = ids(f[2], n_maps), set(ids(f[3], n_maps))
        if not claimants or not no_evidence <= set(claimants):
            raise SystemExit(f"maps_detect line {pos}: bad claimants")
        groups.append(Group(f[1] == "1", claimants, no_evidence, [], {}))
    last = 0
    for _ in range(n_dirs):
        f = take("dir", 4)
        gi = uint(f[0])
        if gi < last or gi >= n_groups:
            raise SystemExit(f"maps_detect line {pos}: dir out of order")
        last = gi
        groups[gi].dirs.append(tuple(flt(s) for s in f[1:]))
    seen = []
    for _ in range(n_ev):
        f = take("ev", 6)
        gi, m = uint(f[0]), uint(f[1])
        if gi >= n_groups or m not in groups[gi].claimants or f[2] not in ("0", "1") or (seen and (gi, m) <= seen[-1]):
            raise SystemExit(f"maps_detect line {pos}: bad ev record")
        seen.append((gi, m))
        groups[gi].ev[m] = Evidence(f[2] == "1", *(frozenset(ids(s, n_tuples)) for s in f[3:]))
    take("end", 0)
    if pos != len(lines):
        raise SystemExit(f"maps_detect: {len(lines) - pos} lines after 'end'")
    for gi, g in enumerate(groups):
        if not g.dirs or (len(g.claimants) > 1) != bool(g.ev) or (g.ev and set(g.ev) != set(g.claimants)):
            raise SystemExit(f"maps_detect: group {gi} has no direction or an incomplete ev block")
    return Table(dp, tol, maps, override, tuples, groups)


def tuple_hits(table: Table, c5, c6) -> list[int]:
    cos_tol = math.cos(math.radians(table.tol[0]))
    return [i for i, (t5, t6, _) in enumerate(table.tuples) if sum(p * q for p, q in zip(c5, t5)) >= cos_tol and all(
        abs(p - q) <= table.tol[2] + table.tol[1] * max(abs(p), abs(q)) for p, q in zip(c6, t6))]


def group_hits(table: Table, c73) -> list[int]:
    cos_tol = math.cos(math.radians(table.tol[0]))
    return [gi for gi, g in enumerate(table.groups) if any(sum(p * q for p, q in zip(c73, d)) >= cos_tol for d in g.dirs)]


def match_tuple(table: Table, c5, c6) -> int | None:
    """The engine's lookup: the first tuple within the table's tolerance."""
    hits = tuple_hits(table, c5, c6)
    return hits[0] if hits else None


def match_group(table: Table, c73) -> int | None:
    hits = group_hits(table, c73)
    return hits[0] if hits else None


def check_first_match(table: Table) -> list[str]:
    """The engine takes the first match. That is safe only if every record a value can match is interchangeable: a
    direction matches one group, and tuples that one value matches have the same owners and the same set memberships."""
    failures = []
    for gi, g in enumerate(table.groups):
        for d in g.dirs:
            if group_hits(table, d) != [gi]:
                failures.append(f"group {gi} direction {d} also matches groups {group_hits(table, d)}")

    def role(i):
        return table.tuples[i][2], [(gi, m, i in ev.pos, i in ev.walk, i in ev.sep)
                                    for gi, g in enumerate(table.groups) for m, ev in sorted(g.ev.items())]

    for i, (c5, c6, _) in enumerate(table.tuples):
        hits = tuple_hits(table, c5, c6)
        if i not in hits or any(role(j) != role(i) for j in hits):
            failures.append(f"tuple {i} matches tuples {hits}, which are not interchangeable")
    return failures


def check_pos(table: Table) -> list[str]:
    """An override claimant with evidence and an empty POS can never be found in that group. An edit to
    spec/sun_rows.toml can take away a map's only evidence (Darkroot's rests on one LightBank value), and coverage
    alone would not always notice."""
    return [f"group {gi} {table.maps[m]}: POS is empty, so {table.maps[m]} can never be found there"
            for gi, g in enumerate(table.groups) if not g.global_only
            for m, ev in sorted(g.ev.items()) if table.override[m] and m not in g.no_evidence and not ev.pos]


def check_hold(table: Table, params, scan: Scan) -> list[str]:
    """build_detect's rule for a hold beside a walk rival, checked again on the table as read, so that a hand-edited
    hold fails. Each walk rival in the group must have no ShadowBank row of its own archive there, and each of its
    collisions whose row index lies in the group in a connect neighbour's archive must carry a light row in WALK, the
    body's."""
    failures = []
    for gi, g in enumerate(table.groups):
        for m, ev in sorted(g.ev.items()):
            x = table.maps[m]
            if not ev.hold:
                continue
            for r in sorted(table.maps[c] for c in g.claimants if table.maps[c] in scan.walk[x]):
                area = "a" + r[1:3]
                if any(match_group(table, shadow_dir(params, a, slot, rid)) == gi
                       for (a, slot, stem), bank in params.items() if a == area and stem == "ShadowBank"
                       for rid in bank.rows):
                    failures.append(f"group {gi} {x}: hold 1, but walk rival {r} has a ShadowBank row in the group")
                    continue
                for world in (0, 1):
                    r_slot = world_slot(params, area, world)
                    for y in sorted(scan.connect[r]):
                        y_area = "a" + y[1:3]
                        slot = world_slot(params, y_area, world)
                        rows = params[(y_area, slot, "ShadowBank")].rows
                        bad = sorted(name for name, sid, lid in scan.colls[r]
                                     if sid in rows and match_group(table, shadow_dir(params, y_area, slot, sid)) == gi
                                     and match_tuple(table, *light_sig(params, area, r_slot, lid)) not in ev.walk)
                        if bad:
                            failures.append(f"slot {world} group {gi} {x}: hold 1, but on {len(bad)} collisions of "
                                            f"walk rival {r} that {y}'s parts show in the group, the body is not "
                                            f"WALK: {' '.join(bad[:8])}")
    return failures


def f32(v: float) -> float:
    """A [maps] float as the engine holds it. It compares double(float) * count, so 0.6 * 5 is just over 3 there."""
    return struct.unpack("<f", struct.pack("<f", v))[0]


def resolve(table: Table, rules, gi: int | None, votes: Counter, held, honour_global_only: bool = True):
    """detection_rules steps 1 to 3 for one sampled frame. Returns (candidate map index, or None for the globals,
    why, the new L)."""
    if gi is None:
        return None, "unknown", held
    g = table.groups[gi]
    if g.global_only and honour_global_only:
        return None, "global_only", held
    if len(g.claimants) == 1:
        p = g.claimants[0]
        return (p if table.override[p] else None), "unique", p
    total = sum(votes.values())
    # At least one draw, as in the engine: with none, a frame showing nothing of a map's own would find it.
    min_votes = max(rules["min_votes"], 1)
    dominance, walk_share = f32(rules["dominance"]), f32(rules["walk_share"])

    def e(ids):
        return sum(votes[t] for t in ids if t in votes)

    pos = {m: e(ev.pos) for m, ev in g.ev.items()}
    chosen = []
    for m, ev in g.ev.items():
        if not table.override[m] or e(ev.walk):
            continue
        sep = e(ev.sep)
        if pos[m] >= min_votes and pos[m] >= dominance * sep and (ev.hold or pos[m] >= walk_share * total):
            chosen.append((m, "vote"))
        elif held == m and ev.hold and sep == 0:
            chosen.append((m, "hold"))
    if len(chosen) == 1:
        return chosen[0][0], chosen[0][1], chosen[0][0]
    for x, n in pos.items():
        if not table.override[x] and n >= min_votes and all(n >= dominance * k for y, k in pos.items() if y != x):
            return None, "vote", x
    return None, "none", held


MISS = -1  # a c73 tally key: draws whose c73 matches no group


def frame_group(rules, tally: Counter) -> tuple[int | None, str | None]:
    """The frame's group from the c73 of each voting draw (tally: group -> draws, MISS -> draws matching none): the
    group with the most draws, when it holds at least c73_share of the draws that matched a group. Otherwise
    (None, "mixed"), or (None, "unknown") when none matched. A tie goes to the lower group index; with c73_share above
    0.5 a tied group can never pass. The share is clamped to 0..1 and passes inclusively, as in the engine."""
    matched = {g: n for g, n in tally.items() if g != MISS and n > 0}
    if not matched:
        return None, "unknown"
    top = min(matched, key=lambda g: (-matched[g], g))
    if matched[top] < min(max(f32(rules["c73_share"]), 0.0), 1.0) * sum(matched.values()):
        return None, "mixed"
    return top, None


def decide(table: Table, rules, tally: Counter, votes: Counter, held, honour_global_only: bool = True):
    """frame_group, then resolve. A mixed or unknown frame gives the globals and leaves L as it is."""
    gi, why = frame_group(rules, tally)
    if gi is None:
        return None, why, held
    return resolve(table, rules, gi, votes, held, honour_global_only)


def group_name(table: Table, gi) -> str:
    if gi is None:
        return "no row"
    if gi == MISS:
        return "no group"
    return f"{gi}{'*' if table.groups[gi].global_only else ''}"


def simulate(table: Table, params, scan: Scan, rules) -> tuple[list[str], list[str], dict[str, int]]:
    """Replay the resolver over every collision of every map, with a15 in either slot. One draw per part in view and
    rules['body_votes'] for the player's body (the collision's LightBank row): bounds, not real frames. Every part in
    view is a voting draw (lightmapped or not): it votes its tuple, and its c73 is the ShadowBank row of the
    collision's index in the part's own archive, which frame_group tallies; the body shows the collision's own row.
    The body always draws, so no frame here is blank.
      own       the map's own pieces and objects
      walk      plus every walk neighbour's
      conn      plus every connect neighbour's
      partial   at a walk rival's collision in a shared group: the rival's body, only its parts whose tuple the
                override map also carries, and the override map's parts whose tuple the rival does not carry
      look-out  for every walk neighbour Y: all of Y's parts, the body, and rules['lookout_parts'] draws of one
                tuple the map's own parts carry, or the body alone. Every such choice is tried: one that leaks is a
                leak, and a collision counts as covered only when every choice gives its own map. A load-screen
                neighbour is left out: the two are taken as never drawn in one frame (spec/maps.toml).
    A leak is an override map's values chosen while standing in another map, with no L, L = the player's map, or a
    stale L naming any other override map. Returns (report lines, failures, own-view coverage by map, the lower of
    the two slots)."""
    if table.maps != scan.units:
        return [], [f"the table's maps {table.maps} are not the MSBs' {scan.units}"], {}
    lines, failures = [], check_first_match(table) + check_pos(table) + check_hold(table, params, scan)
    idx = {u: i for i, u in enumerate(table.maps)}
    overrides = [u for u in table.maps if table.override[idx[u]]]
    body_n, look_n = rules["body_votes"], rules["lookout_parts"]

    def states(p: str) -> list:
        return [None] + [idx[m] for m in overrides] + ([] if p in overrides else [idx[p]])

    own_cov = {}
    first_rows = {}  # (map, Y, ShadowBank row) -> slot 0's look-out line, so slot 1 prints only what differs
    for world in (0, 1):
        rec, misses = {}, Counter()
        for u in scan.units:
            area = "a" + u[1:3]
            slot = world_slot(params, area, world)
            for kind in OWNER_KINDS:
                for lid in scan.parts[u][kind]:
                    c5, c6 = light_sig(params, area, slot, lid)
                    rec[(u, lid)] = match_tuple(table, c5, c6)
                    if rec[(u, lid)] is None:
                        misses[u] += 1
        if misses:
            failures.append(f"slot {world}: parts whose tuple is not in the table: {dict(misses)}")
        votes = {u: Counter() for u in scan.units}
        owned = {u: set() for u in scan.units}
        for u in scan.units:
            for kind in OWNER_KINDS:
                for lid, n in scan.parts[u][kind].items():
                    if rec[(u, lid)] is not None:
                        owned[u].add(rec[(u, lid)])
                        if kind in VOTE_KINDS:
                            votes[u][rec[(u, lid)]] += n
        # Every voting part draws, whether or not its tuple is in the table: c73 and the tuple are read apart.
        draws = {u: sum(sum(scan.parts[u][k].values()) for k in VOTE_KINDS) for u in scan.units}
        shown = {}

        def shows(x: str, sid: int):
            """The group map x's draws show while the player's collision names ShadowBank row sid: the row of that
            index in x's own archive. None when that archive has no such row (no tally), MISS when it is in no group."""
            if (x, sid) not in shown:
                area = "a" + x[1:3]
                slot = world_slot(params, area, world)
                gi = None
                if sid in params[(area, slot, "ShadowBank")].rows:
                    gi = match_group(table, shadow_dir(params, area, slot, sid))
                    gi = MISS if gi is None else gi
                shown[(x, sid)] = gi
            return shown[(x, sid)]

        def tally_of(sid: int, n_by_map: dict[str, int]) -> Counter:
            t = Counter()
            for x, n in n_by_map.items():
                g = shows(x, sid)
                if g is not None and n > 0:
                    t[g] += n
            return t

        leaks = defaultdict(lambda: (set(), set(), set()))  # (view, map, row, Y, leaked map) -> collisions, whys, Ls

        def check(view: str, p: str, name: str, sid: int, y, cand, why: str, held) -> bool:
            if cand is None or table.maps[cand] == p:
                return False
            names, whys, held_ids = leaks[(view, p, sid, y, table.maps[cand])]
            names.add(name)
            whys.add(why)
            held_ids.add(table.maps[held] if held is not None else "-")
            return True

        in_view = {"own": lambda u: set(), "walk": lambda u: scan.walk[u], "conn": lambda u: scan.connect[u]}
        cov, held_cov = Counter(), Counter()
        where = {}  # (map, collision) -> group
        for p in scan.units:
            area = "a" + p[1:3]
            slot = world_slot(params, area, world)
            base = {}
            for view in VIEWS:
                base[view] = Counter(votes[p])
                for x in in_view[view](p):
                    base[view].update(votes[x])
            for (name, sid, lid), n in scan.colls[p].items():
                gi = match_group(table, shadow_dir(params, area, slot, sid))
                where[(p, name, sid, lid)] = gi
                if gi is None:
                    failures.append(f"slot {world}: {p} collision {name} has a direction in no group")
                    continue
                body = rec[(p, lid)]
                for view in VIEWS:
                    v = Counter(base[view])
                    if body is not None:
                        v[body] += body_n
                    tally = tally_of(sid, {p: draws[p] + body_n} | {x: draws[x] for x in in_view[view](p)})
                    for held in states(p):
                        cand, why, _ = decide(table, rules, tally, v, held)
                        check(view, p, name, sid, None, cand, why, held)
                        if p in overrides and cand == idx[p]:
                            if held is None:
                                cov[(p, view)] += n
                            elif held == idx[p]:
                                held_cov[(p, view)] += n

        look_cov, look_held = Counter(), Counter()  # (map, Y) -> collisions where every choice gives the map
        look_rows = {}  # (map, Y, row) -> [collisions, frame groups over the choices, leaking collisions, own ones]
        for p in scan.units:
            extras = [Counter()] + [Counter({t: look_n}) for t in sorted(votes[p])]
            for (name, sid, lid), n in scan.colls[p].items():
                if where[(p, name, sid, lid)] is None:
                    continue
                body = rec[(p, lid)]
                for y in sorted(scan.walk[p]):
                    frames, leaked, mine, mine_held = set(), False, True, True
                    for extra in extras:
                        v = Counter(votes[y])
                        v.update(extra)
                        if body is not None:
                            v[body] += body_n
                        tally = tally_of(sid, {p: body_n + sum(extra.values()), y: draws[y]})
                        gi, why = frame_group(rules, tally)
                        frames.add(why if gi is None else group_name(table, gi))
                        for held in states(p):
                            cand, why, _ = decide(table, rules, tally, v, held)
                            leaked |= check("look-out", p, name, sid, y, cand, why, held)
                            if cand != idx[p] and held is None:
                                mine = False
                            if cand != idx[p] and held == idx[p]:
                                mine_held = False
                    r = look_rows.setdefault((p, y, sid), [0, set(), 0, 0])
                    r[0] += n
                    r[1] |= frames
                    r[2] += n if leaked else 0
                    r[3] += n if mine else 0
                    if p in overrides:
                        look_cov[(p, y)] += n if mine else 0
                        look_held[(p, y)] += n if mine_held else 0

        lines.append(f"a15 in slot {world}: coverage, collisions resolving to their own map (every part in view a "
                     f"voting draw)")
        for m in overrides:
            total = sum(scan.colls[m].values())
            lines.append(f"  {m} {LABELS[m]}: no L: " + ", ".join(f"{v} {cov[(m, v)]}/{total}" for v in VIEWS) +
                         "; with L = it: " + ", ".join(f"{v} {held_cov[(m, v)]}/{total}" for v in VIEWS))
            lines.append("    look-out into: no L: " +
                         ", ".join(f"{y} {look_cov[(m, y)]}/{total}" for y in sorted(scan.walk[m])) +
                         "; with L = it: " +
                         ", ".join(f"{y} {look_held[(m, y)]}/{total}" for y in sorted(scan.walk[m])))
            own_cov[m] = min(own_cov.get(m, cov[(m, "own")]), cov[(m, "own")])

        lines.append(f"a15 in slot {world}: look-out by collision row, where the map or the one in view has values"
                     f"{' (lines that differ from slot 0)' if world else ''}: the group each shows, the frame's group "
                     f"over the choices of the map's own draws, collisions that leak, collisions giving the map")
        for (p, y, sid), (n, frames, n_leak, n_mine) in sorted(look_rows.items()):
            if p not in overrides and y not in overrides:
                continue
            to = sorted(f"{k[4]} ({', '.join(sorted(whys))})" for k, (_, whys, _) in leaks.items()
                        if k[:4] == ("look-out", p, sid, y))
            text = (f"  {p} row {sid} x{n} shows {group_name(table, shows(p, sid))}, {y} shows "
                    f"{group_name(table, shows(y, sid))}: frame {'/'.join(sorted(frames))}; leaks {n_leak}"
                    f"{' to ' + ', '.join(to) if to else ''}" + (f"; gives {p} {n_mine}/{n}" if p in overrides else ""))
            if world == 0:
                first_rows[(p, y, sid)] = text
            elif first_rows.get((p, y, sid)) == text:
                continue
            lines.append(text)

        partial = []
        for gi, g in enumerate(table.groups):
            for mi, ev in sorted(g.ev.items()):
                m = table.maps[mi]
                if not table.override[mi] or mi in g.no_evidence:
                    continue
                for pi in g.claimants:
                    p = table.maps[pi]
                    if p == m or p not in scan.walk[m]:
                        continue
                    p_shared = Counter({t: n for t, n in votes[p].items() if t in owned[m]})
                    m_only = Counter({t: n for t, n in votes[m].items() if t not in owned[p]})
                    seen = p_shared + m_only
                    n_coll = n_neutral = n_would = 0
                    neutral, frames = [], Counter()
                    for (name, sid, lid), n in scan.colls[p].items():
                        if where.get((p, name, sid, lid)) != gi:
                            continue
                        v = Counter(seen)
                        body = rec[(p, lid)]
                        if body is not None:
                            v[body] += body_n
                        tally = tally_of(sid, {p: sum(p_shared.values()) + body_n, m: sum(m_only.values())})
                        fg, fwhy = frame_group(rules, tally)
                        frames[fwhy if fg is None else group_name(table, fg)] += n
                        n_coll += n
                        if body not in ev.walk:
                            n_neutral += n
                            neutral.append(name)
                        for held in states(p):
                            cand, why, _ = decide(table, rules, tally, v, held)
                            check("partial", p, name, sid, m, cand, why, held)
                        if decide(table, rules, tally, v, None, honour_global_only=False)[0] == mi:
                            n_would += n
                    names = f" ({' '.join(sorted(neutral))})" if neutral else ""
                    partial.append(f"  group {gi}{' global_only' if g.global_only else ''}, {m} against walk rival "
                                   f"{p}: {n_coll} collisions, {n_neutral} with a body outside WALK{names}; frame "
                                   f"group " + ", ".join(f"{k} on {c}" for k, c in sorted(frames.items())) +
                                   f"; {n_would} would give {m} without global_only")
        lines.append(f"a15 in slot {world}: partial view")
        lines += partial or ["  no walk rival in any shared group"]

        found = []
        order = (*VIEWS, "partial", "look-out")
        for (view, p, sid, y, m), (names, whys, held_ids) in sorted(leaks.items(), key=lambda kv: (
                order.index(kv[0][0]), kv[0][1], kv[0][2], kv[0][3] or "", kv[0][4])):
            seen_as = f" with {y} in view, which shows group {group_name(table, shows(y, sid))}," if y else ""
            found.append(f"slot {world} {view}: standing in {p} on row {sid} (group {group_name(table, shows(p, sid))})"
                         f"{seen_as} gives {m} ({', '.join(sorted(whys))}) with L {', '.join(sorted(held_ids))}; "
                         f"{len(names)} collisions: {' '.join(sorted(names)[:8])}{' ...' if len(names) > 8 else ''}")
        lines.append(f"a15 in slot {world}: leaks: " + (f"{sum(len(v[0]) for v in leaks.values())} collisions "
                                                        f"in {len(found)} cases" if found else "none"))
        lines += [f"  {x}" for x in found[:60]]
        if len(found) > 60:
            lines.append(f"  ... {len(found) - 60} more")
        failures += found
    return lines, failures, own_cov


def coverage_guard(own: dict[str, int]) -> tuple[list[str], list[str]]:
    accepted = tomllib.loads(ACCEPTED.read_text(encoding="utf-8")).get("own", {}) if ACCEPTED.is_file() else {}
    where = ACCEPTED.relative_to(ROOT).as_posix()
    notes, failures = [], []
    for m in OVERRIDES:
        if m not in own:
            continue
        if m not in accepted:
            failures.append(f"{where} [own] has no {m} (this run: {own[m]})")
        elif own[m] < accepted[m]:
            failures.append(f"{m}: own-view coverage {own[m]} is below the accepted {accepted[m]}")
        elif own[m] > accepted[m]:
            notes.append(f"{m}: own-view coverage {own[m]} is above the accepted {accepted[m]}; raise it in "
                         f"{where} once the change is understood")
    if failures:
        notes.append(f"[own] for this run, for {where} once checked:\n" +
                     "\n".join(f"{m} = {own[m]}" for m in OVERRIDES if m in own))
    return notes, failures


def detect_main(a) -> int:
    dp_dir = Path(a.dp)
    files = dp_files(dp_dir)
    for p in files:
        if not p.is_file():
            raise SystemExit(f"missing DrawParam: {p}")
    stale = staleness(dp_dir)
    if stale:
        print(f"WARNING: {stale}")
    hashes = {area: hashlib.sha256(p.read_bytes()).hexdigest() for area, p in zip(AREAS, files)}
    params = load_dp(dp_dir)
    scan = scan_detect()
    check_rows(params, scan)
    lines = [f"DrawParam: {dp_dir}"]
    if a.emit:
        text, report, failures = build_detect(params, scan, hashes)
        lines += report
    else:
        path = Path(a.simulate)
        if not path.is_file():
            raise SystemExit(f"missing table: {path} (run --emit first)")
        text, failures = path.read_bytes().decode("ascii", errors="replace"), []
        lines.append(f"table: {path}")
    table = parse_detect(text)
    if table.dp != hashes:
        failures.append(f"the table's DrawParam hashes are not those of {dp_dir}: rerun --emit")
    sim_lines, sim_failures, own = simulate(table, params, scan, SPEC["rules"])
    notes, guard = coverage_guard(own)
    lines += sim_lines + notes
    failures += sim_failures + guard
    lines.append(f"table: {len(table.maps)} maps, {len(table.tuples)} tuples, {len(table.groups)} groups, "
                 f"{sum(len(g.dirs) for g in table.groups)} directions, {sum(len(g.ev) for g in table.groups)} "
                 f"ev records, {len(text.splitlines())} lines, {len(text.encode('ascii'))} bytes")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / ("emit.txt" if a.emit else "simulate.txt")).write_text("\n".join(lines + failures) + "\n", encoding="utf-8")
    print("\n".join(lines))
    if failures:
        print("\n".join(f"FAIL: {f}" for f in failures[:60]))
        raise SystemExit(f"refused: {len(failures)} problems" + ("; nothing written" if a.emit else ""))
    if a.emit:
        dst = Path(a.emit)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".tmp")
        tmp.write_bytes(text.encode("ascii"))
        tmp.replace(dst)
        print(f"wrote {dst}")
    return 0


# ---------------------------------------------------------------------------------------------------------------------


def share(n: int, total: int) -> str:
    return f"{n}/{total} ({100 * n / total:.0f}%)" if total else "0/0"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dp", default=str(ROOT / "dist" / "_v4" / "sun"), help="built DrawParam root (has param/DrawParam)")
    ap.add_argument("--fresh", action="store_true", help="build the DrawParam into --out/sun first and read that")
    ap.add_argument("--out", default=str(ROOT / "dist" / "_maps_gate"))
    ap.add_argument("--m12-variant", default="01", choices=("00", "01"),
                    help="which m12_00 MSB counts as Darkroot; the other is left out")
    ap.add_argument("--emit", metavar="PATH", help="write the detection table (engine/data/maps_detect.txt)")
    ap.add_argument("--simulate", metavar="PATH", nargs="?", const=str(DETECT),
                    help=f"replay the rules over a detection table (default {DETECT.relative_to(ROOT)})")
    a = ap.parse_args()
    if a.emit or a.simulate:
        return detect_main(a)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    shipped = Path(a.dp)
    dp_dir = out / "sun" if a.fresh else shipped
    if a.fresh or not all(p.exists() for p in dp_files(dp_dir)):
        build(dp_dir, out / "build.log")
    lines: list[str] = []

    def say(s: str = "") -> None:
        print(s)
        lines.append(s)

    say(f"DrawParam: {dp_dir}")
    stale = staleness(dp_dir)
    if stale:
        say(f"WARNING: {stale}")
    params = load_dp(dp_dir)
    if a.fresh and all(p.exists() for p in dp_files(shipped)):
        diff = signal_diff(load_dp(shipped), params)
        say(f"against {shipped}: {sum(diff.values())} signal fields differ" +
            ("".join(f"\n  {k}: {v} rows" for k, v in sorted(diff.items())) if diff else ""))

    keep = f"m12_00_00_{a.m12_variant}"

    def unit_of(stem: str) -> str | None:
        if stem.startswith("m12_00") and stem != keep:
            return None
        return stem[:6]

    live = extract.collect_live_rows()
    (out / "live_rows_by_map.json").write_text(json.dumps(live, indent=1), encoding="utf-8")
    triples, links, part_counts = scan_msbs(unit_of)
    (out / "map_links.json").write_text(json.dumps({u: dict(c) for u, c in sorted(links.items())}, indent=1),
                                        encoding="utf-8")

    def per_unit(counts: dict[str, int]) -> Counter:
        c = Counter()
        for stem, n in counts.items():
            u = unit_of(stem)
            if u:
                c[u] += n
        return c

    shadow_users = {area: {int(rid): per_unit(r["collision_maps"]) for rid, r in banks["ShadowBank"].items()
                           if per_unit(r["collision_maps"])}
                    for area, banks in live.items() if "ShadowBank" in banks}
    piece_rows = defaultdict(lambda: defaultdict(Counter))  # unit -> bank -> row -> pieces
    for area, banks in live.items():
        for bank in extract.PIECE_BANKS:
            for rid, r in banks.get(bank, {}).items():
                for u, n in per_unit(r["piece_maps"]).items():
                    piece_rows[u][bank][int(rid)] = n
    # Self-check: the per-piece triples must add up to extract.py's per-row piece counts.
    for u, tri in triples.items():
        for i, bank in enumerate(extract.PIECE_BANKS):
            marg = Counter()
            for t, n in tri.items():
                if t[i] is not None and t[i] >= 0:
                    marg[t[i]] += n
            if marg != piece_rows[u][bank]:
                raise SystemExit(f"self-check: {u} {bank} piece counts disagree with collect_live_rows")

    units = sorted(set(part_counts))
    given = symmetric(GIVEN_NEIGHBOURS)
    connect = symmetric({u: set(c) for u, c in links.items()})
    walk = {u: {v for v in connect.get(u, set()) if frozenset((u, v)) not in LOAD_SCREEN} for u in units}

    def sets_for(u: str) -> dict[str, set[str]]:
        return {"given": given.get(u, set()), "connect": connect.get(u, set()), "walk": walk.get(u, set()),
                "all": set(units) - {u}}

    say(f"maps: {', '.join(units)} (m12_00 = {keep})")
    say("")
    say("Seamless neighbours: the task's list (made symmetric) against the MSB connect collisions")
    for u in units:
        g, c = sorted(given.get(u, set())), sorted(connect.get(u, set()))
        flagged = [v for v in c if frozenset((u, v)) in LOAD_SCREEN]
        note = "" if g == c else f"   connect only: {sorted(set(c) - set(g))}; given only: {sorted(set(g) - set(c))}"
        if flagged:
            note += f"   load-screen moves: {flagged}"
        say(f"  {u} {LABELS.get(u, ''):30} given {g}  connect {c}{note}")
    say("")

    groups, group_of, dirs = c73_groups(params, shadow_users)
    (out / "c73_groups.json").write_text(json.dumps([
        {"dir": [round(v, 4) for v in g["dir"]], "diameter_deg": round(g["diameter"], 3),
         "members": [f"{m[0]} slot {m[1]} row {m[2]} ({x}, {y})" for m, (x, y) in zip(g["members"], g["angles"])],
         "collisions_by_map": dict(sorted(g["maps"].items()))} for g in groups], indent=1), encoding="utf-8")
    say(f"c73: {sum(len(g['members']) for g in groups)} collision-named ShadowBank rows (all archives and slots) "
        f"in {len(groups)} direction groups within {TOL_DEG} deg")
    for gi, g in enumerate(groups):
        if len(g["maps"]) > 1:
            maps = ", ".join(f"{m} {n}" for m, n in sorted(g["maps"].items()))
            mem = ", ".join(f"{m[0]}/{m[1]}/{m[2]}({x},{y})" for m, (x, y) in zip(g["members"], g["angles"]))
            say(f"  group {gi} shared, diameter {g['diameter']:.2f} deg: collisions {maps}; rows {mem}")
    say("")

    sigs = {u: unit_signatures(params, u, triples) for u in units}

    relevant = sorted(set(OVERRIDES) | {v for u in OVERRIDES for v in given.get(u, set()) | connect.get(u, set())})
    table = {}
    for u in units:
        sets = sets_for(u)
        table[u] = {"label": LABELS.get(u, ""), "neighbours": {k: sorted(v) for k, v in sets.items() if k != "all"},
                    "parts": dict(part_counts[u]),
                    "c73": c73_for_map(u, params, shadow_users, groups, group_of, dirs, sets),
                    "pieces": pieces_for_map(u, params, triples, sigs, sets),
                    "piece_rows": {b: dict(sorted(c.items())) for b, c in piece_rows[u].items()}}
    (out / "maps_table.json").write_text(json.dumps(table, indent=1), encoding="utf-8")

    for u in relevant:
        t = table[u]
        c, p = t["c73"], t["pieces"]
        say(f"{u} {t['label']}{'  (override)' if u in OVERRIDES else ''}")
        say(f"  parts: {t['parts']}")
        say(f"  c73, {c['collisions']} collisions: unique vs given {share(c['unique'].get('given', 0), c['collisions'])}, "
            f"vs connect {share(c['unique'].get('connect', 0), c['collisions'])}, "
            f"vs walk {share(c['unique'].get('walk', 0), c['collisions'])}, "
            f"vs all {share(c['unique'].get('all', 0), c['collisions'])}")
        for name in ("connect", "all"):
            if c["shared_with"].get(name):
                say(f"    shared with ({name}): {c['shared_with'][name]}")
        for r in c["rows"]:
            say(f"    Sha {r['row']:>3} x{r['collisions']:<3} angles {r['angles']} groups {r['groups']} "
                f"others {r['others'] or '-'}")
        for tag in ("confirmed", "extended"):
            q = p[tag]
            say(f"  pieces {tag} ({'c5 c6' if tag == 'confirmed' else 'c5 c6 + raw fog and scatter rows'}), "
                f"{p['pieces']} pieces: unique vs given {share(q['unique'].get('given', 0), p['pieces'])}, "
                f"vs connect {share(q['unique'].get('connect', 0), p['pieces'])}, "
                f"vs walk {share(q['unique'].get('walk', 0), p['pieces'])}, "
                f"vs all {share(q['unique'].get('all', 0), p['pieces'])}")
            slot_keys = sorted(k for k in q["unique"] if k.startswith("given slot") or k.startswith("connect slot"))
            if slot_keys:
                say("    per slot: " + ", ".join(f"vs {k} {share(q['unique'][k], p['pieces'])}" for k in slot_keys))
            if q["shared_with"].get("connect"):
                say(f"    shared with (connect): {q['shared_with']['connect']}")
                for r in q["shared_rows"]:
                    say(f"      Light/Fog/Scatter {r['light_fog_scatter']} x{r['pieces']}: " + "; ".join(
                        f"slot {s} degRot_s {v['degRot_s']} c6 max {v['c6_max']} matches {v['with'] or '-'}"
                        for s, v in r["by_slot"].items()))
        if p["no_light_row"]:
            say(f"    {p['no_light_row']} pieces name no LightBank row (id below 0 or absent); counted as not unique")
        say("  piece rows: " + "; ".join(f"{b} " + " ".join(f"{r}:{n}" for r, n in rows.items())
                                        for b, rows in t["piece_rows"].items()))
        say("")
    (out / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
