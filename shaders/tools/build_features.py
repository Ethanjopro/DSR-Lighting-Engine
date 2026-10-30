"""Build, verify and pack the DSRLE quality features (shaders/features/dsrle_features.fxh):
DSRLE_SHADOW_SOFT, DSRLE_SPEC_OCC, DSRLE_PL_SMOOTH, DSRLE_ALL, and DSRLE_ALL + DSRLE_SUN_A.

    .venv\\Scripts\\python.exe shaders\\tools\\build_features.py [--only shadow,all] [--skip-build]
            [--no-pack] [--no-regress] [--features-dir DIR] [--jobs 8] [--pl-table]

Steps
  1. gate.py builds of the unified 421b tree (vanilla_src/rebuild, ignored), each with
     /I <features-dir> (default shaders/features; the mapping's fxc_args carry /I shaders/hooks):
       dist/_shader/feat_off        /D DSRLE_FEATURES                     (must equal the identity build)
       dist/_shader/feat_shadow     ... /D DSRLE_SHADOW_SOFT
       dist/_shader/feat_specocc    ... /D DSRLE_SPEC_OCC
       dist/_shader/feat_plsmooth   ... /D DSRLE_PL_SMOOTH
       dist/_shader/feat_all        ... /D DSRLE_ALL
       dist/_shader/feat_all_sunA   ... /D DSRLE_ALL /D DSRLE_SUN_A
     and, for the sun regression, feat_regress_sunA / feat_regress_sunB (/D DSRLE_SUN_A / _B).
     Wired sets: dist/_shader/feat_wired_<hook> (shadow, amb, pl, direct), each built with a one-line
     mutation of that hook (header written to dist/_shader/_work/feat_wired_<hook>/) and compared
     with the identity build; the blobs that differ are the ones the hook reaches.
  2. Checks
       off   902 compile; SHEX/ISGN/OSGN equal to the identity build (--identity, dist/_shader/verify_A)
             and the same vanilla-exact set as that build's gate.
       each  all compile; ISGN/OSGN equal vanilla for every blob; the set whose SHEX/ISGN/OSGN differ
             from feat_off is a subset of the wired set of the hooks the feature uses and equals the
             set predicted from the entry point names; RDEF equal to vanilla once the D3D_SVF_USED bit
             is masked (cbuffer layout, types, bindings, flags), used-flag flips listed.
       all   changed(all) = changed(shadow) | changed(specocc) | changed(plsmooth).
       all_sunA  changed = changed(all) | sun-A prediction, and the blobs whose SHEX differs from
             feat_all are exactly the sun-A set.
       sun regression  feat_off / feat_regress_sunA / _B byte-identical to dist/_shader/sun_off /
             sun_A / sun_B (the existing build_sun.py outputs), and regress_sunA / _B pass the same
             per-config checks. A missing reference counts as a failure; --no-regress skips all of it.
       freshness  with --skip-build (wired sets and feature builds reused), a wired gate older than
             shaders/hooks/*.fxh, the mapping or the unified source tree, or a feature gate older than
             those or shaders/features/*.fxh, is reported (warning, not a failure). Without wired
             builds on disk, --skip-build falls back to the older dist/_shader/verify_mut_<hook> gates.
       baseline  changed_nonexact_in_identity: changed blobs whose identity build is not SHEX/ISGN/
             OSGN-exact to vanilla (the 12 FRPG_Snow_* register-allocation rebuilds), so a pack that
             ships them is known to carry that baseline difference.
     Instruction slots (fxc /dumpbin) of representative blobs, off vs each feature.
     Exit status 0 only when every check above passes (warnings excepted).
  3. Pack dist/_shader_install/feat_<cfg>/shader/FRPG_FlverPBL_fpo_DX11.shaderbnd.dcx (shadow,
     specocc, plsmooth, all, all_sunA): the vanilla archive (sha256-checked) with only the blobs that
     differ from feat_off replaced, read back and diffed. Reports: dist/_shader/feat_<cfg>/report.json
     and dist/_shader/feat_summary.json (never inside an install folder).
  --pl-table prints the PL_SMOOTH new/old falloff statistics over the vanilla PointLightBank rows
  (data/vanilla/*.json, ignored extract) that the feature header quotes.

Original DSRLE code. No game source is read or written beyond passing paths to fxc.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import dxbc  # noqa: E402
import fxcasm  # noqa: E402
import shaderbnd  # noqa: E402
import build_sun  # noqa: E402  (rdef_compare, predicted)
from make_variant import load_vanilla, sha256  # noqa: E402

PY = sys.executable
GATE = ROOT / "vanilla_src" / "rebuild" / "unify" / "gate.py"
MAPPING = ROOT / "vanilla_src" / "rebuild" / "unify" / "mapping_421b.json"
SRC_ROOT = ROOT / "vanilla_src" / "rebuild" / "src"
VAN = ROOT / "vanilla_src" / "blobs" / "FRPG_FlverPBL_fpo_DX11"
DIST = ROOT / "dist" / "_shader"
INSTALL = ROOT / "dist" / "_shader_install"
ARCHIVE_REL = "shader/FRPG_FlverPBL_fpo_DX11.shaderbnd.dcx"
ASM_CACHE = DIST / "_work" / "asm_cache_feat"

BASE = ["/D", "DSRLE_FEATURES"]
CONFIGS = {  # name: (extra defines, hooks used, packed)
    "off": ([], [], False),
    "shadow": (["/D", "DSRLE_SHADOW_SOFT"], ["shadow"], True),
    "specocc": (["/D", "DSRLE_SPEC_OCC"], ["amb"], True),
    "plsmooth": (["/D", "DSRLE_PL_SMOOTH"], ["pl"], True),
    "all": (["/D", "DSRLE_ALL"], ["shadow", "amb", "pl"], True),
    "all_sunA": (["/D", "DSRLE_ALL", "/D", "DSRLE_SUN_A"], ["shadow", "amb", "pl", "direct"], True),
    "regress_sunA": (["/D", "DSRLE_SUN_A"], ["direct"], False),
    "regress_sunB": (["/D", "DSRLE_SUN_B"], ["direct"], False),
}
FEATURES = ("shadow", "specocc", "plsmooth", "all", "all_sunA")
MUT_GATES = {  # older cached wired sets (fallback for --skip-build only)
    "shadow": "verify_mut_shadow/gate_mshadow.json",
    "amb": "verify_mut_amb/gate_mamb.json",
    "pl": "verify_mut_pl/gate_mpl.json",
    "direct": "verify_mut_direct/gate_mdirect.json",
}
MUTATIONS = {  # one-line hook mutations whose changed set is the hook's wired set
    "shadow": "#define DSRLE_HOOK_SHADOW(rate, worldPos, N, eyeVec, flags) ((rate) * 0.5f)",
    "amb": "#define DSRLE_HOOK_AMBIENT(diffAmb, specAmb, N, V, worldPos, lightRate, flags) diffAmb *= 0.5f;",
    "pl": "#define DSRLE_HOOK_POINTLIGHT(contrib, L, dist, lightColor, N, V, worldPos, lightShadow) contrib *= 0.5f;",
    "direct": ("#define DSRLE_HOOK_DIRECT(N, V, worldPos, diffColor, f0, roughness, shadowRate, lightmap, flags, "
               "vertexNormal, svPos, specF90) (saturate(dot(N, float3(0.3f, 0.8f, 0.5f))) * diffColor * shadowRate * 1.7f)"),
}
REPRESENTATIVE = [  # (label, blob)
    ("Phn HemEnv DifSpcBmp Csd", "FRPG_Phn_DifSpcBmp______Csd_HemEnv"),
    ("Phn HemEnv DifSpcBmp LitCsd", "FRPG_Phn_DifSpcBmp___LitCsd_HemEnv"),
    ("Phn HemEnv DifSpcBmp LitSdw", "FRPG_Phn_DifSpcBmp___LitSdw_HemEnv"),
    ("Phn HemEnv DifSpcBmpMul Sdw PntS", "FRPG_Phn_DifSpcBmpMul___Sdw_HemEnvPntS"),
    ("Phn HemEnv Dif Bmp Csd (no PBL map)", "FRPG_Phn_Dif___Bmp______Csd_HemEnv"),
    ("Phn HemEnv DifSpcBmp unshadowed", "FRPG_Phn_DifSpcBmp__________HemEnv"),
    ("Phn FaceEye Csd", "FRPG_Phn_FaceEye_Csd"),
    ("Phn FaceEye CsdPntS (legacy loop)", "FRPG_Phn_FaceEye_CsdPntS"),
    ("Gst HemEnv DifSpcBmp Csd (ghost light)", "FRPG_Gst_DifSpcBmp______Csd_HemEnv"),
    ("Sfx HemEnv DifSpcBmp Csd", "FRPG_Sfx_DifSpcBmp______Csd_HemEnv"),
    ("Phn Non DifSpcBmp LitSdw", "FRPG_Phn_DifSpcBmp___LitSdw_Non"),
    ("Snow Csd", "FRPG_Snow____Csd"),
    ("Snow LitNcs", "FRPG_Snow_LitNcs"),
    ("Water Env Ncs", "FRPG_Water_Env_Ncs"),
    ("Water Env Csd", "FRPG_Water_Env_Csd"),
]


# ---------------------------------------------------------------------------
def rel(p: Path) -> str:
    """Repo-relative path for reports (absolute when outside the repo)."""
    try:
        return str(Path(p).relative_to(ROOT))
    except ValueError:
        return str(p)


def out_dir(cfg: str) -> Path:
    return DIST / f"feat_{cfg}"


def blob_path(cfg_dir: Path, fam: str, name: str) -> Path:
    return cfg_dir / fam / (name + ".fpo")


def run_gate(cfg: str, inc: Path, identity: Path, jobs: int) -> dict:
    out = out_dir(cfg)
    if out.exists():
        shutil.rmtree(out)
    if " " in str(inc):
        raise SystemExit("gate.py splits --extra on spaces; the features path must not contain one")
    extra = BASE + CONFIGS[cfg][0] + ["/I", str(inc)]
    ref = identity if cfg == "off" else out_dir("off")
    cmd = [PY, str(GATE), "--mapping", str(MAPPING), "--src-root", str(SRC_ROOT), "--out", str(out),
           "--extra", " ".join(extra), "--tag", f"feat_{cfg}", "--ref-out", str(ref), "--quiet",
           "--jobs", str(jobs)]
    print(">", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)
    return load_gate(cfg)


def load_gate(cfg: str) -> dict:
    return json.loads((out_dir(cfg) / f"gate_feat_{cfg}.json").read_text(encoding="utf-8"))


def wired_gate(hook: str) -> Path:
    """The fresh wired-set gate if built, else the older verify_mut one."""
    fresh = DIST / f"feat_wired_{hook}" / f"gate_feat_wired_{hook}.json"
    return fresh if fresh.exists() else DIST / MUT_GATES[hook]


def build_wired(hook: str, identity: Path, jobs: int) -> None:
    work = DIST / "_work" / f"feat_wired_{hook}"
    work.mkdir(parents=True, exist_ok=True)
    (work / "dsrle_features.fxh").write_text(
        "// Generated by shaders/tools/build_features.py: hook mutation for the wired-set build.\n"
        "#ifndef DSRLE_FEATURES_FXH\n#define DSRLE_FEATURES_FXH\n" + MUTATIONS[hook] + "\n#endif\n", encoding="utf-8")
    out = DIST / f"feat_wired_{hook}"
    if out.exists():
        shutil.rmtree(out)
    if " " in str(work):
        raise SystemExit("gate.py splits --extra on spaces; the work path must not contain one")
    cmd = [PY, str(GATE), "--mapping", str(MAPPING), "--src-root", str(SRC_ROOT), "--out", str(out),
           "--extra", " ".join(BASE + ["/I", str(work)]), "--tag", f"feat_wired_{hook}", "--ref-out", str(identity),
           "--quiet", "--jobs", str(jobs)]
    print(">", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def wired(hook: str) -> set[str]:
    g = json.loads(wired_gate(hook).read_text(encoding="utf-8"))
    if not all(r["ok"] for r in g["results"]):
        raise SystemExit(f"wired-set build for {hook} has compile failures")
    if all("ref_same" in r for r in g["results"]):
        return {r["name"] for r in g["results"] if r.get("ref_same") is False}
    return {r["name"] for r in g["results"] if not r.get("exact")}


def _newest(paths) -> tuple[float, str]:
    best = (0.0, "")
    for p in paths:
        if p.is_file() and p.stat().st_mtime > best[0]:
            best = (p.stat().st_mtime, rel(p))
    return best


def freshness(cfgs: list[str]) -> dict:
    """Reused gates (--skip-build) older than what they were built from. Warnings only."""
    hooks_t, hooks_f = _newest(list((ROOT / "shaders" / "hooks").glob("*.fxh")) + [MAPPING]
                               + list((SRC_ROOT / "g421b_unified").rglob("*")))
    feat_t, feat_f = max(_newest((ROOT / "shaders" / "features").glob("*.fxh")), (hooks_t, hooks_f))
    stale = {}
    for h in MUT_GATES:
        g = wired_gate(h)
        if g.stat().st_mtime < hooks_t:
            stale[rel(g)] = f"older than {hooks_f}"
    for c in cfgs:
        g = out_dir(c) / f"gate_feat_{c}.json"
        if g.stat().st_mtime < feat_t:
            stale[rel(g)] = f"older than {feat_f}"
    return {"stale": stale, "warning": bool(stale)}


def nonexact_identity(identity_gate: Path) -> set[str]:
    g = json.loads(identity_gate.read_text(encoding="utf-8"))
    return {r["name"] for r in g["results"] if not r.get("exact")}


# ---------------------------------------------------------------------------
# predictions from the entry point names
def _shadowed(rec: dict) -> bool:
    return bool(re.search(r"Csd|Sdw|Ncs", rec["entry_point"]))


def _spc_site(rec: dict) -> bool:
    # DSRLE_VF_SPC call sites: the HemEnv Spc entry points, every FaceEye and Snow lit body
    base = rec.get("base", "")
    return "Spc" in rec["entry_point"] or "FaceEye" in base or "Snow" in base


def predicted(cfg: str, mapping: dict, names: list[str], wires: dict[str, set[str]]) -> set[str]:
    p: set[str] = set()
    if cfg in ("shadow", "all", "all_sunA"):
        p |= {n for n in names if n in wires["shadow"] and _shadowed(mapping[n])}
    if cfg in ("specocc", "all", "all_sunA"):
        p |= {n for n in names if n in wires["amb"] and _spc_site(mapping[n])}
    if cfg in ("plsmooth", "all", "all_sunA"):
        p |= {n for n in names if n in wires["pl"]}
    if cfg in ("all_sunA", "regress_sunA"):
        p |= build_sun.predicted(mapping, [n for n in names if n in wires["direct"]], "A")
    if cfg == "regress_sunB":
        p |= build_sun.predicted(mapping, [n for n in names if n in wires["direct"]], "B")
    return p


# ---------------------------------------------------------------------------
def verify(cfg: str, mapping: dict, gate: dict, wires: dict[str, set[str]], nonexact: set[str]) -> dict:
    res = gate["results"]
    names = [r["name"] for r in res]
    rep = {"compiled": sum(r["ok"] for r in res), "n": len(res),
           "isgn_osgn_vs_vanilla_all_same": all(r.get("ISGN") and r.get("OSGN") for r in res)}
    changed = sorted(r["name"] for r in res if not r.get("ref_same"))
    rep["changed_vs_off"] = len(changed)
    fam: dict[str, int] = {}
    for n in changed:
        fam[mapping[n]["family"]] = fam.get(mapping[n]["family"], 0) + 1
    rep["changed_by_family"] = dict(sorted(fam.items()))
    rep["changed_nonexact_in_identity"] = sorted(set(changed) & nonexact)
    hooks = CONFIGS[cfg][1]
    wset = set().union(*(wires[h] for h in hooks)) if hooks else set()
    rep["hooks"] = hooks
    rep["wired_set_size"] = len(wset)
    rep["subset_of_wired"] = set(changed) <= wset
    rep["outside_wired"] = sorted(set(changed) - wset)[:20]
    pred = predicted(cfg, mapping, names, wires)
    rep["predicted"] = len(pred)
    rep["equals_prediction"] = set(changed) == pred
    rep["unexpected_changed"] = sorted(set(changed) - pred)[:20]
    rep["expected_but_unchanged"] = sorted(pred - set(changed))[:20]
    rd = {"layout_same": 0, "bindings_same": 0, "masked_bytes_same": 0, "checked": 0}
    flips: dict[str, int] = {}
    bad = []
    for n in changed:
        b = blob_path(out_dir(cfg), mapping[n]["family"], n).read_bytes()
        v = (VAN / (n + ".fpo")).read_bytes()
        c = build_sun.rdef_compare(b, v)
        rd["checked"] += 1
        for k in ("layout_same", "bindings_same", "masked_bytes_same"):
            rd[k] += c[k]
        if not (c["layout_same"] and c["bindings_same"] and c["masked_bytes_same"]):
            bad.append(n)
        for f in c["used_flips"]:
            flips[f] = flips.get(f, 0) + 1
    rd["used_flag_flips"] = dict(sorted(flips.items()))
    rd["mismatch"] = bad[:20]
    rd["all_same"] = not bad
    rep["rdef_vs_vanilla_changed_blobs"] = rd
    rep["ok"] = (rep["compiled"] == rep["n"] and rep["isgn_osgn_vs_vanilla_all_same"] and rep["subset_of_wired"]
                 and rep["equals_prediction"] and rd["all_same"])
    rep["changed"] = changed
    return rep


def shex(cfg: str, mapping: dict, n: str) -> bytes:
    return dxbc.chunk_map(blob_path(out_dir(cfg), mapping[n]["family"], n).read_bytes())["SHEX"]


def compare_dirs(a: Path, b: Path) -> dict:
    fa = sorted(p.relative_to(a) for p in a.rglob("*.fpo"))
    if not b.exists():
        return {"reference": rel(b), "skipped": "reference build not present"}
    diff = [str(p) for p in fa if not (b / p).exists() or (a / p).read_bytes() != (b / p).read_bytes()]
    nb = len(list(b.rglob("*.fpo")))
    return {"reference": rel(b), "blobs": len(fa), "reference_blobs": nb,
            "byte_identical": len(fa) - len(diff), "differ": diff[:20], "ok": not diff and nb == len(fa)}


# ---------------------------------------------------------------------------
def slots(path: Path) -> dict:
    si = fxcasm.parse_asm(fxcasm.cached_disasm(str(path), str(ASM_CACHE)))
    txt = fxcasm.cached_disasm(str(path), str(ASM_CACHE))
    return {"slots": si.stats.get("slots", -1), "instrs": len(si.instrs), "temps": si.temps,
            "samples": len(re.findall(r"^\s*(sample|gather)", txt, re.M))}


def pack(cfg: str, mapping: dict, changed: list[str]) -> dict:
    van = load_vanilla()
    by_name = van.entry_by_name()
    reps = {}
    for n in changed:
        ename = n + ".fpo"
        if ename not in by_name:
            raise SystemExit(f"{ename} not in the vanilla archive")
        b = blob_path(out_dir(cfg), mapping[n]["family"], n).read_bytes()
        if not dxbc.checksum_ok(b):
            raise SystemExit(f"{n}: DXBC checksum invalid")
        reps[ename] = b
    inst = INSTALL / f"feat_{cfg}"
    if inst.exists():
        shutil.rmtree(inst)
    dst = inst / ARCHIVE_REL
    bnd = shaderbnd.write(van, dst, reps)
    back = shaderbnd.read_archive(dst)
    parsed = shaderbnd.parse_bnd3(back.bnd)
    vparsed = shaderbnd.parse_bnd3(van.bnd)
    differ = sorted(e.name for e, ve in zip(parsed.entries, vparsed.entries) if e.data != ve.data)
    p = {
        "archive": rel(dst),
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
               and not p["semantic_diffs_vs_vanilla"] and p["readback_bnd_equals_written"] and p["dcx_type_level_same"]
               and p["install_folder_files"] == [str(Path(ARCHIVE_REL))])
    return p


# ---------------------------------------------------------------------------
def pl_table(cap: float = 1.15, beta: float = 1.0) -> dict:
    """PL_SMOOTH new/old over the vanilla PointLightBank (dwindleBegin, dwindleEnd) rows."""
    import collections
    import numpy as np
    rows = []
    for f in sorted((ROOT / "data" / "vanilla").glob("a*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        for banks in d.values():
            for r in banks.get("PointLightBank", {}).values():
                b, e = r.get("dwindleBegin"), r.get("dwindleEnd")
                if b is not None and e and e > b:
                    rows.append((b, e))
    if not rows:
        return {"skipped": "no data/vanilla PointLightBank rows"}

    def factor(d, b, e):  # mirrors dsrle_pl_Factor
        x = np.maximum((e - d) / (e - b), 0.0)
        x3 = x ** 3
        old = np.minimum(x3, 1.0)
        y = np.minimum(x3, 64.0)
        t = d / max(beta * e, 1e-4)
        q = np.minimum(y / (cap / (1.0 + t * t)), 1e4)
        sm = y / (1.0 + q ** 8) ** 0.125
        return np.minimum(sm / np.maximum(old, 1e-6), cap), old

    st = []
    for (b, e), n in collections.Counter(rows).items():
        d = np.linspace(0.0, e * 0.9999, 20001)
        f, old = factor(d, b, e)
        lit = old > 1e-3
        tail = (d > e / 2) & (old > 1e-4)
        st.append((n, f[0], f[np.argmin(abs(d - e / 3))], f[lit].min(), f.max(), f[tail].min() if tail.any() else 1.0))
    w = np.array([s[0] for s in st])

    def q(i):
        v = np.repeat([s[i] for s in st], w)
        return {k: round(float(x), 3) for k, x in zip(("min", "p10", "median", "p90", "max"),
                                                       np.quantile(v, [0, .1, .5, .9, 1]))}
    s = np.array([b / e for b, e in rows])
    return {"rows": len(rows), "start_over_end": {k: round(float(x), 3) for k, x in zip(("p10", "median", "p90"), np.quantile(s, [.1, .5, .9]))},
            "ratio_at_source": q(1), "ratio_at_r_over_3": q(2), "min_ratio_inside": q(3), "max_ratio": q(4),
            "min_ratio_beyond_r_over_2": q(5)}


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="comma list of feature configs (default: all five)")
    ap.add_argument("--skip-build", action="store_true", help="reuse dist/_shader/feat_* builds")
    ap.add_argument("--no-pack", action="store_true")
    ap.add_argument("--no-regress", action="store_true", help="skip the sun A/B byte regression builds")
    ap.add_argument("--features-dir", default=str(ROOT / "shaders" / "features"))
    ap.add_argument("--identity", default=str(DIST / "verify_A"), help="identity (default-hook) build dir")
    ap.add_argument("--identity-gate", default=str(DIST / "verify_A" / "gate_vA.json"))
    ap.add_argument("--jobs", type=int, default=8, help="parallel fxc jobs for gate.py")
    ap.add_argument("--pl-table", action="store_true", help="also print the PL_SMOOTH falloff table")
    a = ap.parse_args(argv)

    feats = [c for c in (a.only.split(",") if a.only else FEATURES) if c]
    for c in feats:
        if c not in FEATURES:
            raise SystemExit(f"unknown config {c}; choose from {FEATURES}")
    if any(c in ("all", "all_sunA") for c in feats):  # the union checks need the single features
        feats = sorted(set(feats) | {"shadow", "specocc", "plsmooth"}, key=FEATURES.index)
    if "all_sunA" in feats and "all" not in feats:
        feats.insert(feats.index("all_sunA"), "all")
    order = ["off"] + feats + ([] if a.no_regress else ["regress_sunA", "regress_sunB"])
    inc = Path(a.features_dir).resolve()

    mapping = json.loads(MAPPING.read_text(encoding="utf-8"))
    gates = {}
    for cfg in order:
        gates[cfg] = load_gate(cfg) if a.skip_build else run_gate(cfg, inc, Path(a.identity), a.jobs)
    if not a.skip_build:
        for h in MUTATIONS:
            build_wired(h, Path(a.identity), a.jobs)
    wires = {h: wired(h) for h in MUT_GATES}

    ident = json.loads(Path(a.identity_gate).read_text(encoding="utf-8"))
    ident_exact = {r["name"] for r in ident["results"] if r.get("exact")}
    nonexact = nonexact_identity(Path(a.identity_gate))
    off = gates["off"]["results"]
    summary: dict = {"features_dir": str(inc), "wired": {h: len(s) for h, s in wires.items()},
                     "wired_gates": {h: rel(wired_gate(h)) for h in MUT_GATES},
                     "freshness": freshness(order) if a.skip_build else {"stale": {}, "warning": False,
                                                                         "note": "all gates built in this run"},
                     "identity_nonexact_vs_vanilla": sorted(nonexact)}
    for k, why in summary["freshness"]["stale"].items():
        print(f"WARNING stale: {k} is {why}", flush=True)
    summary["off"] = {
        "compiled": sum(r["ok"] for r in off), "n": len(off),
        "exact_vs_vanilla": sum(bool(r.get("exact")) for r in off),
        "same_exact_set_as_identity_gate": {r["name"] for r in off if r.get("exact")} == ident_exact,
        "identical_to_identity_build": sum(bool(r.get("ref_same")) for r in off),
    }
    summary["off"]["ok"] = (summary["off"]["compiled"] == len(off) and summary["off"]["same_exact_set_as_identity_gate"]
                            and summary["off"]["identical_to_identity_build"] == len(off))

    changed: dict[str, list[str]] = {}
    regress = [] if a.no_regress else ["regress_sunA", "regress_sunB"]
    for cfg in feats + regress:
        rep = verify(cfg, mapping, gates[cfg], wires, nonexact)
        changed[cfg] = rep.pop("changed")
        if cfg in FEATURES and not a.no_pack and CONFIGS[cfg][2]:
            rep["pack"] = pack(cfg, mapping, changed[cfg])
        summary[cfg] = rep
        (out_dir(cfg) / "report.json").write_text(json.dumps(dict(rep, changed=changed[cfg]), indent=1, sort_keys=True))

    if "all" in feats:
        union = set(changed["shadow"]) | set(changed["specocc"]) | set(changed["plsmooth"])
        summary["all_equals_union_of_single_features"] = set(changed["all"]) == union
    if "all_sunA" in feats:
        sun_a = predicted("regress_sunA", mapping, list(mapping), wires) & {r["name"] for r in off}
        shex_vs_all = {n for n in changed["all_sunA"] if shex("all_sunA", mapping, n) != shex("all", mapping, n)}
        summary["all_sunA_changed_equals_all_plus_sunA"] = set(changed["all_sunA"]) == set(changed["all"]) | sun_a
        summary["all_sunA_shex_differs_from_all_exactly_on_sunA_set"] = shex_vs_all == sun_a
        summary["all_sunA_shex_vs_all_unexpected"] = sorted(shex_vs_all ^ sun_a)[:20]
    if not a.no_regress:
        summary["sun_regression"] = {
            "off_vs_dist_sun_off": compare_dirs(out_dir("off"), DIST / "sun_off"),
            "sunA_vs_dist_sun_A": compare_dirs(out_dir("regress_sunA"), DIST / "sun_A"),
            "sunB_vs_dist_sun_B": compare_dirs(out_dir("regress_sunB"), DIST / "sun_B"),
        }

    inst = []
    for label, n in REPRESENTATIVE:
        if n not in mapping:
            continue
        fam = mapping[n]["family"]
        row = {"blob": n, "label": label, "off": slots(blob_path(out_dir("off"), fam, n))}
        for cfg in feats:
            s = slots(blob_path(out_dir(cfg), fam, n))
            row[cfg] = dict(s, delta_slots=s["slots"] - row["off"]["slots"], delta_instrs=s["instrs"] - row["off"]["instrs"])
        inst.append(row)
    summary["instructions"] = inst
    if a.pl_table:
        summary["pl_table"] = pl_table()
    # the sun regression is a hard rule: a skipped reference (no "ok") fails unless --no-regress
    summary["sun_regression_ok"] = (True if a.no_regress else
                                    all(v.get("ok") is True for v in summary["sun_regression"].values())
                                    and all(summary[c]["ok"] for c in regress))
    summary["ok"] = bool(summary["off"]["ok"] and all(summary[c]["ok"] for c in feats)
                         and all(summary[c].get("pack", {"ok": True})["ok"] for c in feats)
                         and summary.get("all_equals_union_of_single_features", True)
                         and summary.get("all_sunA_changed_equals_all_plus_sunA", True)
                         and summary.get("all_sunA_shex_differs_from_all_exactly_on_sunA_set", True)
                         and summary["sun_regression_ok"])
    (DIST / "feat_summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True))
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
