"""Validate a build in dist/ against vanilla and spec/global.toml rules. Exit code 1 on any error.

Checks: file allowlist, immutable fields, paramdef limits, rule bands, readability floor, lockstep (declared
char rows and same-ID s_LightBank rows), slot-1 mirroring where slot 1 == slot 0 in vanilla, no-op edits,
declared 0 -> nonzero colA edits, shadow fade scaling with endDist, and a sun decision for every live row.

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


def live_light_rows(area: str) -> dict[int, dict]:
    live = json.loads((ROOT / "data" / "live_rows.json").read_text(encoding="utf-8")).get(area, {})
    return {int(r): rec["users"] for r, rec in live.get("LightBank", {}).items()}


def map_rows(area: str) -> set[int]:
    """LightBank rows used by map pieces/objects (vs. character rows chosen by collisions)."""
    return {r for r, users in live_light_rows(area).items() if users.get("map_pieces") or users.get("objects")}


def slots_of(entry) -> tuple:
    return {"both": (0, 1)}.get(entry.get("slot", 0), (entry.get("slot", 0),))


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
    from_zero = {(c["area"], c["slot"], c["bank"], c["row"], c["field"]) for c in manifest["changes"] if c.get("from_zero")}
    edited_by_spec = {}
    for c in manifest["changes"]:
        edited_by_spec.setdefault((c["spec"], c["area"], c["bank"], c["field"], json.dumps(c["arg"])), []).append(c["old"] != c["new"])
    for key, changed in edited_by_spec.items():
        if not any(changed):
            warnings.append(f"no-op edit in {key[0]}: {key[2]}.{key[3]} op arg {key[4]} changes nothing")

    sun_rules = rules.get("sun", {})
    for area in AREAS:
        path = dist / "param" / "DrawParam" / f"{area}_DrawParam.parambnd.dcx"
        if not path.exists():
            if sun_rules.get("decisions_required"):
                warnings.append(f"sun: {area} has no build, so every live row keeps its vanilla colA_s as the sun")
            continue
        van, mod = load_vanilla(area), DrawParamBND.from_path(path)
        mrows = map_rows(area)
        changes = {}  # (slot, bank, row) -> {field: (old, new)}
        values = {}   # (slot, bank, row) -> (vanilla values, modded values), changed rows only
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
                values[(slot, bank, rid)] = (vv, mv)
                imm = rules["immutable"].get(bank, [])
                for field, (old, new) in diff.items():
                    where = f"{area} {bank}[{rid}] s{slot}.{field} {old}->{new}"
                    if "*" in imm or field in imm:
                        errors.append(f"immutable field changed: {where}")
                    if not (defs[field]["min"] <= new <= defs[field]["max"]):
                        errors.append(f"outside paramdef limits: {where}")
                    if (bank in ("LightBank", "s_LightBank") and field.startswith("colA") and old == 0 and new != 0
                            and (area, slot, bank, rid, field) not in from_zero):
                        errors.append(f"undeclared 0 -> nonzero (set from_zero = true on the edit): {where}")
                    for band in rules.get("band", []):
                        bank_ok = band["bank"] == bank if band.get("exact_bank") else band["bank"] == bank.removeprefix("s_")
                        if not bank_ok or field not in band["fields"]:
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
                    ev_drop = math.log2(cap_v / max(cap_m, 1e-9)) * rules["readability"].get("model_gain", 1.0)
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

        # Shadow fade: fadeBeginDist / fadeDist follow endDist, because the sun fades with the shadow fade.
        sf_tol = rules.get("shadow_fade", {}).get("ratio_tolerance")
        for (slot, bank, rid), (vv, mv) in values.items():
            if sf_tol is None or bank != "ShadowBank" or not {"endDist", "fadeBeginDist", "fadeDist"} & changes[(slot, bank, rid)].keys():
                continue
            r_end = ratio(mv["endDist"], vv["endDist"])
            for f in ("fadeBeginDist", "fadeDist"):
                if vv[f] > 0 and abs(mv[f] / vv[f] - r_end) > sf_tol * r_end:
                    errors.append(f"shadow fade: {area} ShadowBank[{rid}] s{slot}.{f} x{mv[f] / vv[f]:.3f} vs endDist x{r_end:.3f}")
            if mv["fadeBeginDist"] > mv["endDist"]:
                errors.append(f"shadow fade: {area} ShadowBank[{rid}] s{slot} fadeBeginDist {mv['fadeBeginDist']} > endDist {mv['endDist']}")

        # Lockstep: s_LightBank same-ID rows and declared char rows track the map row's change ratio.
        # colA_s is per-row sun intent and is never lockstepped. 0 -> nonzero fields are covered by from_zero.
        tol = rules["lockstep"]["ratio_tolerance"]
        def ratios(slot, bank, rid):
            return {f: ratio(n, o) for f, (o, n) in changes.get((slot, bank, rid), {}).items()
                    if f.startswith("colA") and f != "colA_s"}
        if rules["lockstep"].get("s_lightbank_same_id"):
            light_rows = {(slot, rid) for (slot, bank, rid) in changes if bank in ("LightBank", "s_LightBank")}
            for slot, rid in sorted(light_rows):
                vparams = van.draw_params_0 if slot == 0 else van.draw_params_1
                mparams = mod.draw_params_0 if slot == 0 else mod.draw_params_1
                if "s_LightBank" not in vparams or rid not in vparams["s_LightBank"].rows:
                    continue
                s_old, s_new = row_values(vparams["s_LightBank"].rows[rid]), row_values(mparams["s_LightBank"].rows[rid])
                m_r = ratios(slot, "LightBank", rid)
                for f in set(m_r) | set(ratios(slot, "s_LightBank", rid)):
                    r = m_r.get(f, 1.0)
                    if s_old[f] == 0 or math.isinf(r):
                        continue  # a zero s_ field cannot follow a ratio
                    if isinstance(s_old[f], int) and s_new[f] == round(s_old[f] * r):
                        continue  # small integer fields can only express some ratios
                    if abs(s_new[f] / s_old[f] - r) > tol * r:
                        errors.append(f"lockstep: {area} s_LightBank[{rid}] s{slot}.{f} x{s_new[f] / s_old[f]:.3f} vs LightBank x{r:.3f}")
        for decl in (l for l in manifest.get("lockstep", []) if l["area"] == area):
            lo, hi = decl.get("char_rel", (1 - tol, 1 + tol))
            for slot in (0, 1):
                m_r = ratios(slot, decl["bank"], decl["map"])
                for c in decl["chars"]:
                    c_r = ratios(slot, decl["bank"], c)
                    for f in set(m_r) | set(c_r):
                        r, cr = m_r.get(f, 1.0), c_r.get(f, 1.0)
                        if math.isinf(r) or math.isinf(cr):
                            continue
                        if not (lo - 1e-6 <= cr / r <= hi + 1e-6):
                            errors.append(f"lockstep: {area} {decl['bank']}[{c}] s{slot}.{f} x{cr:.3f} vs map row {decl['map']} x{r:.3f} (allowed {lo}..{hi} of it)")

        # Sun decisions: every live row whose vanilla colA_s would render as a sun needs an explicit choice.
        if sun_rules.get("decisions_required"):
            decided = {(c["slot"], c["row"]) for c in manifest["changes"]
                       if c["area"] == area and c["bank"] == "LightBank" and c["field"] == "colA_s"}
            for k in (k for k in manifest.get("sun_keep", []) if k["area"] == area):
                decided |= {(s, r) for s in slots_of(k) for r in k["rows"]}
            live = live_light_rows(area)
            for slot, bank, vparam in iter_params(van):
                if bank != "LightBank":
                    continue
                for rid, vrow in vparam.rows.items():
                    col_a = row_values(vrow)["colA_s"]
                    if rid in live and 0 < col_a <= sun_rules["spec_only_above"] and (slot, rid) not in decided:
                        warnings.append(f"sun: {area} LightBank[{rid}] s{slot} vanilla colA_s {col_a} has no sun decision (edit colA_s or add [[sun_keep]])")

    for w in warnings:
        print("WARN ", w)
    for e in errors:
        print("ERROR", e)
    print(f"validate: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
