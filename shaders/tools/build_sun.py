"""Build, verify and pack the DSRLE sun feature (shaders/features/dsrle_features.fxh).

    .venv\\Scripts\\python.exe shaders\\tools\\build_sun.py [--skip-build] [--gain 2.0] [--extra "/D ..."]

Steps
  1. gate.py builds of the unified 421b tree (vanilla_src/rebuild, ignored) into
       dist/_shader/sun_off   /D DSRLE_FEATURES               (no sun switch: must equal identity)
       dist/_shader/sun_A     /D DSRLE_FEATURES /D DSRLE_SUN_A
       dist/_shader/sun_B     /D DSRLE_FEATURES /D DSRLE_SUN_B
     each with /I shaders/features (the mapping's fxc_args already carry /I shaders/hooks).
  2. Checks
       off: 902 compile, SHEX/ISGN/OSGN equal to the identity build (--identity, default
            dist/_shader/verify_A) and the same vanilla-exact set as that build's gate.
       A/B: all compile; ISGN/OSGN equal vanilla; the set whose SHEX differs from sun_off
            is a subset of the DIRECT-wired set (dist/_shader/verify_mut_direct/gate_mdirect.json)
            and equals the set predicted from the entry point name
            (A: shadowed, no light map, not a ghost; B: shadowed, not a ghost); A is a subset of B
            and the A-set blobs are SHEX-identical in A and B; RDEF equal to vanilla once the
            D3D_SVF_USED bit of the variables is masked (layout, types, bindings, flags).
     Instruction slots (fxc /dumpbin) for representative blobs, off vs A vs B.
  3. Pack dist/_shader_install/sun_<X>/shader/FRPG_FlverPBL_fpo_DX11.shaderbnd.dcx: the vanilla
     archive (sha256-checked) with only the blobs that differ from sun_off replaced, read back
     and diffed. Reports go to dist/_shader/sun_<X>/report.json (never into the install folder).

No game source is read or written by this script beyond passing paths to fxc.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import dxbc  # noqa: E402
import fxcasm  # noqa: E402
import shaderbnd  # noqa: E402
from make_variant import load_vanilla, sha256  # noqa: E402

PY = sys.executable
GATE = ROOT / "vanilla_src" / "rebuild" / "unify" / "gate.py"
MAPPING = ROOT / "vanilla_src" / "rebuild" / "unify" / "mapping_421b.json"
SRC_ROOT = ROOT / "vanilla_src" / "rebuild" / "src"
VAN = ROOT / "vanilla_src" / "blobs" / "FRPG_FlverPBL_fpo_DX11"
FEATURES = ROOT / "shaders" / "features"
DIST = ROOT / "dist" / "_shader"
INSTALL = ROOT / "dist" / "_shader_install"
ARCHIVE_REL = "shader/FRPG_FlverPBL_fpo_DX11.shaderbnd.dcx"
ASM_CACHE = DIST / "_work" / "asm_cache_sun"

CONFIGS = {
    "off": ["/D", "DSRLE_FEATURES"],
    "A": ["/D", "DSRLE_FEATURES", "/D", "DSRLE_SUN_A"],
    "B": ["/D", "DSRLE_FEATURES", "/D", "DSRLE_SUN_B"],
}

REPRESENTATIVE = [  # (label, entry point regex, family, base suffix)
    ("Phn HemEnv DifSpcBmp Csd (A,B)", "FRPG_Phn_DifSpcBmp______Csd_HemEnv"),
    ("Phn HemEnv DifSpcBmp LitCsd (B only)", "FRPG_Phn_DifSpcBmp___LitCsd_HemEnv"),
    ("Phn HemEnv DifSpcBmpMul Sdw PntS (A,B)", "FRPG_Phn_DifSpcBmpMul___Sdw_HemEnvPntS"),
    ("Phn FaceEye Csd (A,B)", "FRPG_Phn_FaceEye_Csd"),
    ("Sfx HemEnv DifSpcBmp Csd (A,B)", "FRPG_Sfx_DifSpcBmp______Csd_HemEnv"),
    ("Gst HemEnv DifSpcBmp Csd (ghost: excluded)", "FRPG_Gst_DifSpcBmp______Csd_HemEnv"),
    ("Snow Csd (A,B)", "FRPG_Snow____Csd"),
]
SPOT_UNTOUCHED_A = [
    "FRPG_Phn_DifSpcBmp___Lit____HemEnv",      # light map, no shadow
    "FRPG_Phn_DifSpcBmp___LitCsd_HemEnv",      # light map + shadow (B only)
    "FRPG_Phn_DifSpcBmp__________HemEnv",      # neither
    "FRPG_Phn_FaceEye____",                    # unshadowed face/eye
    "FRPG_Snow_Lit___",
    "FRPG_Gst_DifSpcBmp______Csd_HemEnv",      # ghost, shadowed: excluded
    "FRPG_Gst_FaceEye_Csd",                    # ghost face/eye, shadowed: excluded
]


# ---------------------------------------------------------------------------
def run_gate(cfg: str, extra_user: list[str], identity: Path) -> dict:
    out = DIST / f"sun_{cfg}"
    if out.exists():
        shutil.rmtree(out)
    inc = str(FEATURES)
    if " " in inc:
        raise SystemExit("gate.py splits --extra on spaces; the features path must not contain one")
    extra = CONFIGS[cfg] + extra_user + ["/I", inc]
    ref = identity if cfg == "off" else DIST / "sun_off"
    cmd = [PY, str(GATE), "--mapping", str(MAPPING), "--src-root", str(SRC_ROOT), "--out", str(out),
           "--extra", " ".join(extra), "--tag", f"sun_{cfg}", "--ref-out", str(ref), "--quiet"]
    print(">", " ".join(cmd))
    subprocess.run(cmd, check=True)
    return json.loads((out / f"gate_sun_{cfg}.json").read_text(encoding="utf-8"))


def load_gate(cfg: str) -> dict:
    return json.loads((DIST / f"sun_{cfg}" / f"gate_sun_{cfg}.json").read_text(encoding="utf-8"))


def blob_path(cfg_dir: Path, fam: str, name: str) -> Path:
    return cfg_dir / fam / (name + ".fpo")


# ---------------------------------------------------------------------------
# RDEF (SM5 'RD11'): compare with the variable D3D_SVF_USED bit (0x2) masked
SVF_USED = 0x2


def rdef_parse(r: bytes) -> dict:
    cb_n, cb_off, bind_n, bind_off, _ver, _flags, creator = struct.unpack_from("<7I", r, 0)
    def cstr(o):
        return r[o:r.index(b"\0", o)].decode("latin-1")
    cbs, used = [], {}
    for i in range(cb_n):
        name_o, var_n, var_o, size, fl, typ = struct.unpack_from("<6I", r, cb_off + 24 * i)
        cname = cstr(name_o)
        vs = []
        for j in range(var_n):
            vn, start, vsize, vfl, type_o, def_o, *_rest = struct.unpack_from("<10I", r, var_o + 40 * j)
            tdesc = struct.unpack_from("<6H", r, type_o)
            vs.append((cstr(vn), start, vsize, vfl & ~SVF_USED, tdesc, def_o != 0))
            used[f"{cname}.{cstr(vn)}"] = bool(vfl & SVF_USED)
        cbs.append((cname, size, fl, typ, vs))
    binds = [(cstr(struct.unpack_from("<I", r, bind_off + 32 * i)[0]),) + struct.unpack_from("<7I", r, bind_off + 32 * i + 4)
             for i in range(bind_n)]
    masked = bytearray(r)
    for i in range(cb_n):
        _n, var_n, var_o, *_ = struct.unpack_from("<6I", r, cb_off + 24 * i)
        for j in range(var_n):
            o = var_o + 40 * j + 12
            struct.pack_into("<I", masked, o, struct.unpack_from("<I", r, o)[0] & ~SVF_USED)
    return {"cbuffers": cbs, "bindings": binds, "used": used, "masked": bytes(masked[:creator]),
            "creator": cstr(creator)}


def rdef_compare(blob: bytes, van: bytes) -> dict:
    a = rdef_parse(dxbc.chunk_map(blob)["RDEF"])
    v = rdef_parse(dxbc.chunk_map(van)["RDEF"])
    flips = sorted(k for k in a["used"] if a["used"][k] != v["used"].get(k))
    return {
        "layout_same": a["cbuffers"] == v["cbuffers"],
        "bindings_same": a["bindings"] == v["bindings"],
        "masked_bytes_same": a["masked"] == v["masked"],
        "used_flips": flips,
    }


# ---------------------------------------------------------------------------
def slots(path: Path) -> tuple[int, int, int]:
    si = fxcasm.parse_asm(fxcasm.cached_disasm(str(path), str(ASM_CACHE)))
    return si.stats.get("slots", -1), len(si.instrs), si.temps


def predicted(mapping: dict, names: list[str], cfg: str) -> set[str]:
    out = set()
    for n in names:
        ep = mapping[n]["entry_point"]
        shadowed = bool(re.search(r"Csd|Sdw|Ncs", ep))
        lit = "Lit" in ep
        ghost = "WITH_GhostMap" in mapping[n].get("wrapper_defines", [])
        if shadowed and not ghost and (cfg == "B" or not lit):
            out.add(n)
    return out


def verify(cfg: str, mapping: dict, gate: dict, mdirect: set[str]) -> dict:
    res = gate["results"]
    names = [r["name"] for r in res]
    rep = {"compiled": sum(r["ok"] for r in res), "n": len(res),
           "isgn_osgn_vs_vanilla_all_same": all(r.get("ISGN") and r.get("OSGN") for r in res)}
    changed = sorted(r["name"] for r in res if not r.get("ref_same"))
    rep["changed_vs_off"] = len(changed)
    rep["changed_by_family"] = {}
    for n in changed:
        f = mapping[n]["family"]
        rep["changed_by_family"][f] = rep["changed_by_family"].get(f, 0) + 1
    pred = predicted(mapping, [n for n in names if n in mdirect], cfg)
    rep["subset_of_direct_wired"] = set(changed) <= mdirect
    rep["equals_prediction"] = set(changed) == pred
    rep["unexpected_changed"] = sorted(set(changed) - pred)[:20]
    rep["expected_but_unchanged"] = sorted(pred - set(changed))[:20]
    rd = {"layout_same": 0, "bindings_same": 0, "masked_bytes_same": 0, "checked": 0}
    flips: dict[str, int] = {}
    bad = []
    for n in changed:
        fam = mapping[n]["family"]
        b = blob_path(DIST / f"sun_{cfg}", fam, n).read_bytes()
        v = (VAN / (n + ".fpo")).read_bytes()
        c = rdef_compare(b, v)
        rd["checked"] += 1
        for k in ("layout_same", "bindings_same", "masked_bytes_same"):
            rd[k] += c[k]
        if not (c["layout_same"] and c["bindings_same"] and c["masked_bytes_same"]):
            bad.append(n)
        for f in c["used_flips"]:
            flips[f] = flips.get(f, 0) + 1
    rd["used_flag_flips"] = dict(sorted(flips.items()))
    rd["mismatch"] = bad[:20]
    rep["rdef_vs_vanilla_changed_blobs"] = rd
    rep["changed"] = changed
    return rep


def pack(cfg: str, mapping: dict, changed: list[str], report: dict) -> dict:
    van = load_vanilla()
    by_name = van.entry_by_name()
    reps = {}
    for n in changed:
        ename = n + ".fpo"
        if ename not in by_name:
            raise SystemExit(f"{ename} not in the vanilla archive")
        b = blob_path(DIST / f"sun_{cfg}", mapping[n]["family"], n).read_bytes()
        if not dxbc.checksum_ok(b):
            raise SystemExit(f"{n}: DXBC checksum invalid")
        reps[ename] = b
    inst = INSTALL / f"sun_{cfg}"
    if inst.exists():
        shutil.rmtree(inst)
    dst = inst / ARCHIVE_REL
    bnd = shaderbnd.write(van, dst, reps)
    back = shaderbnd.read_archive(dst)
    parsed = shaderbnd.parse_bnd3(back.bnd)
    vparsed = shaderbnd.parse_bnd3(van.bnd)
    differ = sorted(e.name for e, ve in zip(parsed.entries, vparsed.entries) if e.data != ve.data)
    p = {
        "archive": str(dst.relative_to(ROOT)),
        "sha256": sha256(dst),
        "bytes": dst.stat().st_size,
        "entries": len(parsed.entries),
        "vanilla_entries": len(vparsed.entries),
        "names_equal_vanilla_in_order": [e.name for e in parsed.entries] == [e.name for e in vparsed.entries],
        "replaced": len(reps),
        "entries_differing_from_vanilla": len(differ),
        "differing_equals_replaced": set(differ) == set(reps),
        "payload_mismatches": [e.name for e in parsed.entries if e.data != reps.get(e.name, by_name[e.name].data)][:20],
        "semantic_diffs_vs_vanilla": shaderbnd.semantic_diff(van.bnd, back.bnd, set(reps))[:20],
        "readback_bnd_equals_written": back.bnd == bnd,
        "dcx_type_level_same": back.dcx_type == van.dcx_type and back.level == van.level,
        "install_folder_files": sorted(str(x.relative_to(inst)) for x in inst.rglob("*") if x.is_file()),
    }
    p["ok"] = (p["names_equal_vanilla_in_order"] and p["differing_equals_replaced"] and not p["payload_mismatches"]
               and not p["semantic_diffs_vs_vanilla"] and p["readback_bnd_equals_written"] and p["dcx_type_level_same"])
    report["pack"] = p
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-build", action="store_true", help="reuse dist/_shader/sun_* builds")
    ap.add_argument("--no-pack", action="store_true")
    ap.add_argument("--identity", default=str(DIST / "verify_A"), help="identity (default-hook) build dir")
    ap.add_argument("--identity-gate", default=str(DIST / "verify_A" / "gate_vA.json"))
    ap.add_argument("--gain", help="DSRLE_SUN_GAIN override (float literal)")
    ap.add_argument("--extra", default="", help="more fxc args for A/B, space separated")
    a = ap.parse_args(argv)

    mapping = json.loads(MAPPING.read_text(encoding="utf-8"))
    user = a.extra.split() if a.extra else []
    if a.gain:
        user += ["/D", f"DSRLE_SUN_GAIN={float(a.gain)!r}f"]
    gates = {}
    for cfg in ("off", "A", "B"):
        gates[cfg] = load_gate(cfg) if a.skip_build else run_gate(cfg, user if cfg != "off" else [], Path(a.identity))

    mdir = json.loads((DIST / "verify_mut_direct" / "gate_mdirect.json").read_text(encoding="utf-8"))
    mdirect = {r["name"] for r in mdir["results"] if not r.get("exact")}
    ident = json.loads(Path(a.identity_gate).read_text(encoding="utf-8"))
    ident_exact = {r["name"] for r in ident["results"] if r.get("exact")}

    summary = {"direct_wired": len(mdirect)}
    off = gates["off"]["results"]
    summary["off"] = {
        "compiled": sum(r["ok"] for r in off), "n": len(off),
        "exact_vs_vanilla": sum(bool(r.get("exact")) for r in off),
        "same_exact_set_as_identity_gate": {r["name"] for r in off if r.get("exact")} == ident_exact,
        "identical_to_identity_build": sum(bool(r.get("ref_same")) for r in off),
        "not_exact": sorted(r["name"] for r in off if not r.get("exact")),
    }

    gates_changed = {}
    for cfg in ("A", "B"):
        rep = verify(cfg, mapping, gates[cfg], mdirect)
        gates_changed[cfg] = rep["changed"]
        rep["spot_untouched_in_A"] = {}
        if cfg == "A":
            for n in SPOT_UNTOUCHED_A:
                if n in mapping:
                    rep["spot_untouched_in_A"][n] = n not in rep["changed"]
        if not a.no_pack:
            pack(cfg, mapping, rep["changed"], rep)
        full = dict(rep)
        rep = {k: v for k, v in rep.items() if k != "changed"}
        summary[cfg] = rep
        (DIST / f"sun_{cfg}" / "report.json").write_text(json.dumps(full, indent=1, sort_keys=True))

    # A must be a subset of B, and its blobs must be SHEX-identical in A and B
    a_set, b_set = set(gates_changed["A"]), set(gates_changed["B"])
    shex_diff = [n for n in sorted(a_set & b_set)
                 if dxbc.chunk_map(blob_path(DIST / "sun_A", mapping[n]["family"], n).read_bytes())["SHEX"]
                 != dxbc.chunk_map(blob_path(DIST / "sun_B", mapping[n]["family"], n).read_bytes())["SHEX"]]
    summary["A_subset_of_B"] = a_set <= b_set
    summary["A_set_shex_identical_in_B"] = not shex_diff
    summary["A_set_shex_differs_in_B"] = shex_diff[:20]

    inst = []
    for label, n in REPRESENTATIVE:
        if n not in mapping:
            continue
        fam = mapping[n]["family"]
        row = {"blob": n, "label": label}
        for cfg in ("off", "A", "B"):
            s, ni, t = slots(blob_path(DIST / f"sun_{cfg}", fam, n))
            row[cfg] = {"slots": s, "instrs": ni, "temps": t}
        row["delta_A"] = row["A"]["instrs"] - row["off"]["instrs"]
        row["delta_B"] = row["B"]["instrs"] - row["off"]["instrs"]
        inst.append(row)
    summary["instructions"] = inst
    (DIST / "sun_summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True))
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
