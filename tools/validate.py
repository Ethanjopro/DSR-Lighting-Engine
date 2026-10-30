"""Validate a build in dist/ against vanilla and spec/global.toml rules. Exit code 1 on any error.

Checks: file allowlist, immutable fields, paramdef limits, rule bands, readability floor, lockstep (declared
char rows and same-ID s_LightBank rows), slot-1 mirroring where slot 1 == slot 0 in vanilla, no-op edits.

Usage: python tools/validate.py [--dist dist]
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import tomllib
from pathlib import Path

from soulstruct.darksouls1r.params.draw_param import DrawParamBND

from dp import AREAS, ROOT, internal_to_attr, iter_params, load_vanilla
from paramdef_limits import limits

ALLOW = re.compile(r"^(param/DrawParam/a1[0-8]_DrawParam\.parambnd\.dcx|shader/[^/]+\.shaderbnd\.dcx|build_manifest\.json)$")


def row_values(row) -> dict:
    return {k: getattr(row, a) for k, a in internal_to_attr(type(row)).items() if not k.startswith(("pad", "dummy"))}


def map_rows(area: str) -> set[int]:
    """LightBank rows used by map pieces/objects (vs. character rows chosen by collisions)."""
    live = json.loads((ROOT / "data" / "live_rows.json").read_text(encoding="utf-8")).get(area, {})
    return {int(r) for r, rec in live.get("LightBank", {}).items()
            if rec["users"].get("map_pieces") or rec["users"].get("objects")}


def ratio(new, old):
    if old == 0:
        return math.inf if new != 0 else 1.0
    return new / old


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default=str(ROOT / "dist"))
    dist = Path(ap.parse_args().dist)
    rules = tomllib.loads((ROOT / "spec" / "global.toml").read_text(encoding="utf-8"))
    errors, warnings = [], []

    for f in dist.rglob("*"):
        if f.is_file():
            rel = f.relative_to(dist).as_posix()
            if not ALLOW.match(rel) and not rel.startswith("_"):
                errors.append(f"file outside allowlist: {rel}")

    manifest = json.loads((dist / "build_manifest.json").read_text(encoding="utf-8")) if (dist / "build_manifest.json").exists() else {"changes": [], "lockstep": []}
    edited_by_spec = {}
    for c in manifest["changes"]:
        edited_by_spec.setdefault((c["spec"], c["area"], c["bank"], c["field"], json.dumps(c["arg"])), []).append(c["old"] != c["new"])
    for key, changed in edited_by_spec.items():
        if not any(changed):
            warnings.append(f"no-op edit in {key[0]}: {key[2]}.{key[3]} op arg {key[4]} changes nothing")

    for area in AREAS:
        path = dist / "param" / "DrawParam" / f"{area}_DrawParam.parambnd.dcx"
        if not path.exists():
            continue
        van, mod = load_vanilla(area), DrawParamBND.from_path(path)
        mrows = map_rows(area)
        changes = {}  # (slot, bank, row) -> {field: (old, new)}
        for slot, bank, vparam in iter_params(van):
            mparam = (mod.draw_params_0 if slot == 0 else mod.draw_params_1).get(bank)
            if mparam is None or set(mparam.rows) != set(vparam.rows):
                errors.append(f"{area} {bank} slot {slot}: rows missing or added")
                continue
            defs = limits()["LightBank" if bank == "s_LightBank" else bank]
            for rid, vrow in vparam.rows.items():
                vv, mv = row_values(vrow), row_values(mparam.rows[rid])
                diff = {k: (vv[k], mv[k]) for k in vv if vv[k] != mv[k]}
                if mparam.rows[rid].Name != vrow.Name:
                    errors.append(f"{area} {bank}[{rid}] slot {slot}: row name changed")
                if not diff:
                    continue
                changes[(slot, bank, rid)] = diff
                imm = rules["immutable"].get(bank, [])
                for field, (old, new) in diff.items():
                    where = f"{area} {bank}[{rid}] s{slot}.{field} {old}->{new}"
                    if "*" in imm or field in imm:
                        errors.append(f"immutable field changed: {where}")
                    if not (defs[field]["min"] <= new <= defs[field]["max"]):
                        errors.append(f"outside paramdef limits: {where}")
                    for band in rules.get("band", []):
                        if band["bank"] != bank.removeprefix("s_") or field not in band["fields"]:
                            continue
                        if band.get("rows") == "map" and rid not in mrows:
                            continue
                        if "rel" in band and old != 0:
                            lo, hi = band["rel"]
                            if not (lo - 1e-6 <= new / old <= hi + 1e-6):
                                errors.append(f"band rel {band['rel']} violated: {where} (x{new / old:.3f})")
                        if "abs" in band and abs(new - old) > band["abs"] + 1e-6:
                            errors.append(f"band abs {band['abs']} violated: {where}")
                        if "max" in band and new > band["max"] + 1e-6:
                            errors.append(f"band max {band['max']} violated: {where}")
                # Readability floor on exposure cap in the darkest scenes.
                if bank == "ToneMapBank" and ("grayKeyValue" in diff or "minAdaptedLum" in diff):
                    cap_v = vv["grayKeyValue"] / max(vv["minAdaptedLum"], 1e-6)
                    cap_m = mv["grayKeyValue"] / max(mv["minAdaptedLum"], 1e-6)
                    ev_drop = math.log2(cap_v / max(cap_m, 1e-9))
                    if ev_drop > rules["readability"]["max_ev_below_vanilla"] + 1e-6:
                        errors.append(f"readability floor: {area} ToneMap[{rid}] s{slot} dark-scene cap {ev_drop:.2f} EV below vanilla")

        # Slot-1 mirroring: rows identical between slots in vanilla must stay identical.
        for bank, vp0 in van.draw_params_0.items():
            vp1 = van.draw_params_1.get(bank)
            if vp0 is None or vp1 is None:
                continue
            mp0, mp1 = mod.draw_params_0[bank], mod.draw_params_1[bank]
            for rid in vp0.rows:
                if rid in vp1.rows and row_values(vp0.rows[rid]) == row_values(vp1.rows[rid]):
                    if row_values(mp0.rows[rid]) != row_values(mp1.rows[rid]):
                        errors.append(f"{area} {bank}[{rid}]: slot 1 mirrored slot 0 in vanilla but differs now")

        # Lockstep: s_LightBank same-ID rows and declared char rows track the map row's change ratio.
        tol = rules["lockstep"]["ratio_tolerance"]
        def ratios(slot, bank, rid):
            return {f: ratio(n, o) for f, (o, n) in changes.get((slot, bank, rid), {}).items() if f.startswith("colA")}
        for (slot, bank, rid), diff in list(changes.items()):
            if bank != "LightBank" or not rules["lockstep"].get("s_lightbank_same_id"):
                continue
            s_param = (mod.draw_params_0 if slot == 0 else mod.draw_params_1).get("s_LightBank")
            if s_param is None or rid not in s_param.rows:
                continue
            m_r, s_r = ratios(slot, "LightBank", rid), ratios(slot, "s_LightBank", rid)
            for f, r in m_r.items():
                if abs(s_r.get(f, 1.0) - r) > tol * r:
                    errors.append(f"lockstep: {area} s_LightBank[{rid}].{f} x{s_r.get(f, 1.0):.3f} vs LightBank x{r:.3f}")
        for decl in (l for l in manifest.get("lockstep", []) if l["area"] == area):
            for slot in (0, 1):
                m_r = ratios(slot, decl["bank"], decl["map"])
                for c in decl["chars"]:
                    c_r = ratios(slot, decl["bank"], c)
                    for f in set(m_r) | set(c_r):
                        r = m_r.get(f, 1.0)
                        if abs(c_r.get(f, 1.0) - r) > tol * r:
                            errors.append(f"lockstep: {area} {decl['bank']}[{c}].{f} x{c_r.get(f, 1.0):.3f} vs map row {decl['map']} x{r:.3f}")

    for w in warnings:
        print("WARN ", w)
    for e in errors:
        print("ERROR", e)
    print(f"validate: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
