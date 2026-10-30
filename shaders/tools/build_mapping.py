"""Build vanilla_src/rebuild/mapping.json: one record per vanilla FlverPBL
pixel-shader blob describing how to (re)compile it.

Usage:
    .venv\\Scripts\\python.exe shaders\\tools\\build_mapping.py [--root vanilla_src]

The blob names follow a token grammar (material feature token + lighting
suffix). Token meanings are cross-checked against each blob's own
reflection/disassembly (bound texture slots, used constants, signatures,
legacy point-light constant registers) and the result of every check is
stored in the record, so uncertainty stays explicit.

This script contains no shader source text; it only reads the blobs and their
fxc disassembly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fxcasm  # noqa: E402

FEATURE_RE = re.compile(r"^(Dif)(Spc|___)(Bmp|___)(Mul|___)(Lit|___)(Sdw|Csd|___)$")
LIGHT_RE = re.compile(r"^(?P<base>HemEnv|HemDir3)(?P<lerp>Lerp)?(?P<mod>Alp|Parallax|Subsurf)?(?P<pnt>Pnt(?:S+)?)?$")
CMDLINE = ["_WIN32=1", "_FRAGMENT_SHADER=1", "_DX11=1"]   # from the SPDB env block (hlslDefines)

# texture slot roles (slot numbers are generic sampler registers t0..t18)
SLOT = {"Spc": "t1", "Bmp": "t2", "Mul": "t3", "Lit": "t6", "ShadowMap": "t7",
        "AO": "t8", "DFG": "t9", "Height/Subsurf": "t10", "EnvDif": "t11", "EnvSpc": "t12",
        "EnvDif2": "t13", "EnvSpc2": "t14", "DetailBump": "t15", "Clustered": "t16"}


def md5(p):
    with open(p, "rb") as fh:
        return hashlib.md5(fh.read()).hexdigest()


def features(si: fxcasm.ShaderInfo) -> dict:
    tex = sorted({b.bind for b in si.bindings if b.type in ("texture",)}, key=lambda s: int(s[1:]))
    used = []
    allvars = []
    for cb in si.cbuffers:
        if cb.name == "$Globals":
            allvars = [v.name for v in cb.vars]
            used = [v.name for v in cb.vars if v.used]
    ops = fxcasm.opcode_multiset(si)
    # legacy point-light constants: registers c112..c115 (pos) referenced in code
    refs = Counter()
    for i in si.instrs:
        for m in re.finditer(r"cb0\[(\d+)\]", i):
            refs[int(m.group(1))] += 1
    legacy_pnt = sorted(r for r in refs if 112 <= r <= 115)
    cbsize = None
    dyn = None
    for d in si.decls:
        m = re.match(r"dcl_constantbuffer CB0\[(\d+)\], (\w+)", d)
        if m:
            cbsize, dyn = int(m.group(1)), m.group(2)
    return {
        "tex": tex,
        "used_cb": used,
        "cb_style": ("DL_FREG" if any(n.startswith("DL_FREG") for n in allvars) else
                     ("named" if allvars else "none")),
        "cb0": [cbsize, dyn],
        "isgn": [f"{e.name}{e.index}" for e in si.isgn],
        "isgn_mask": {f"{e.name}{e.index}": e.mask for e in si.isgn},
        "n_out": len(si.osgn),
        "n_instr": len(si.instrs),
        "loops": ops.get("loop", 0),
        "discard": ops.get("discard_nz", 0) + ops.get("discard_z", 0),
        "legacy_pnt_regs": legacy_pnt,
        "frontface": any(e.sysval == "FFACE" for e in si.isgn),
        "debug_lines": si.has_debug_lines,
    }


def used_any(f, *names):
    return any(any(u == n or u.startswith(n + "[") for n in names) for u in f["used_cb"])


def parse_name(name: str) -> dict:
    """Split a blob name into family / feature token / suffix tokens."""
    if name == "FRPG_Ghost":
        return {"family": "Ghost", "kind": "single"}
    m = re.match(r"^FRPG_(Phn|Gst|Sfx|Dbg|WWS)_(Dif(?:Spc|___)(?:Bmp|___)(?:Mul|___)(?:Lit|___)(?:Sdw|Csd|___))(?:_(.*))?$", name)
    if m and (m.group(3) is None or m.group(3) != ""):
        return {"family": m.group(1), "kind": "material", "token": m.group(2), "suffix": m.group(3) or ""}
    m = re.match(r"^FRPG_(Phn|Gst|Dbg|WWS)_(Dif(?:Spc|___)(?:Bmp|___)(?:Mul|___))_______?(.*)$", name)
    if m:  # Dbg_Dif___BmpMul_______Nrm style (Lit/shadow slots blank)
        return {"family": m.group(1), "kind": "material", "token": m.group(2) + "______", "suffix": m.group(3)}
    m = re.match(r"^FRPG_(Phn|Gst)_FaceEye_(Csd|Sdw|___)(Pnt(?:S+))?$", name)
    if m:
        return {"family": m.group(1), "kind": "faceeye", "shadow": m.group(2), "pnt": m.group(3) or ""}
    m = re.match(r"^FRPG_DeferredDisney_(Dif)(Spc|___)(Bmp|___)(Lit|___)(Mul)?$", name)
    if m:
        return {"family": "DeferredDisney", "kind": "deferred", "spc": m.group(2) == "Spc",
                "bmp": m.group(3) == "Bmp", "lit": m.group(4) == "Lit", "mul": bool(m.group(5))}
    m = re.match(r"^FRPG_Snow_(Lit|___)(Csd|Ncs|___)(Pnt(?:S+))?$", name)
    if m:
        return {"family": "Snow", "kind": "snow", "lit": m.group(1) == "Lit", "shadow": m.group(2), "pnt": m.group(3) or ""}
    m = re.match(r"^FRPG_Water_(Env|Reflect)_(Csd|Ncs|___)(Pnt(?:S+))?$", name)
    if m:
        return {"family": "Water", "kind": "water", "mode": m.group(1), "shadow": m.group(2), "pnt": m.group(3) or ""}
    m = re.match(r"^FRPG_(Snow|Water)_(HeightMap|Mask)$", name)
    if m:
        return {"family": m.group(1), "kind": "aux", "pass": m.group(2)}
    m = re.match(r"^FRPG_NtoA_(Csd|Sdw|Non|DepAlp)$", name)
    if m:
        return {"family": "NtoA", "kind": "ntoa", "variant": m.group(1)}
    m = re.match(r"^FRPG_Dbg_PntNum_(Pnt(?:S*))$", name)
    if m:
        return {"family": "Dbg", "kind": "pntnum", "pnt": m.group(1)}
    m = re.match(r"^FRPG_Dbg_Snow_(\w+)$", name)
    if m:
        return {"family": "Dbg", "kind": "dbgsnow", "variant": m.group(1)}
    return {"family": name.split("_")[1], "kind": "unknown"}


def pnt_defines(pnt: str) -> list:
    """PntS..PntSSSS -> N point lights of type Specular (one 'S' per light).
    'Pnt' alone (Dbg_PntNum_Pnt) -> 0 typed lights."""
    n = pnt.count("S") if pnt else 0
    return [f"POINT_LIGHT_{i}=POINT_LIGHT_TYPE_Specular" for i in range(n)]


def material_record(p: dict, f: dict) -> dict:
    fam, tok, suf = p["family"], p["token"], p["suffix"]
    m = FEATURE_RE.match(tok)
    spc, bmp, mul, lit, sdw = (m.group(2) == "Spc", m.group(3) == "Bmp", m.group(4) == "Mul",
                               m.group(5) == "Lit", m.group(6))
    wrap = []          # defines set inside the wrapper around the base #include
    cmd = list(CMDLINE)
    if bmp: wrap.append("WITH_BumpMap")
    if lit: wrap.append("WITH_LightMap")
    if sdw == "Sdw": wrap.append("WITH_ShadowMap=CalcLispPos_VS")
    if sdw == "Csd": wrap.append("WITH_ShadowMap=CalcLispPos_PS")
    notes = []
    light = {}
    if fam == "Sfx" and suf == "":
        top = "FRPG_FlverPBL/FRPG_FS_Sfx.fx"
        wrap = ["WITH_Glow", "FS_TRANSLUCENT"] + wrap
        status = "recovered"
    elif fam in ("Phn", "Gst", "Sfx"):
        status = "unrecovered"
        if suf == "Non":
            top = f"FRPG_FlverPBL/FRPG_FS_{fam}_Non.fx"
            light = {"base": "Non"}
        elif suf in ("Dep", "DepAlp", "Vel", "VelAlp"):
            top = f"FRPG_FlverPBL/FRPG_FS_{fam}_{suf.replace('Alp', '')}.fx"
            light = {"base": suf.replace("Alp", ""), "alp": suf.endswith("Alp")}
            if suf.endswith("Alp"): wrap.append("WITH_AlphaTest")
        else:
            lm = LIGHT_RE.match(suf)
            if not lm:
                raise ValueError(f"unparsed lighting suffix {suf}")
            light = {k: v for k, v in lm.groupdict().items() if v}
            top = f"FRPG_FlverPBL/FRPG_FS_{fam}_{lm.group('base')}.fx"
            if lm.group("lerp"): cmd.append("WITH_EnvLerp")
            mod = lm.group("mod")
            if mod == "Subsurf": cmd.append("FS_SUBSURF")
            if mod == "Parallax": cmd.append("WITH_Parallax")
            if mod == "Alp": cmd.append("FS_ALPHA")  # name is a convention; meaning unconfirmed (see notes)
            cmd += pnt_defines(lm.group("pnt") or "")
        if fam == "Gst":
            wrap = ["WITH_GhostMap"] + wrap
        if suf and used_any(f, "gFC_InverseToneMapEnable", "gFC_ToneCorrectParams"):
            # translucent forward path: output is reverse-tonemapped like the
            # recovered Sfx wrapper's FS_TRANSLUCENT/ReverseToneMap path
            wrap.append("FS_TRANSLUCENT")
            notes.append("FS_TRANSLUCENT inferred: blob reads gFC_ToneCorrectParams/gFC_InverseToneMapEnable and binds t6 (LumTex)")
        if light.get("mod") == "Alp":
            notes.append("Alp: 4-output DL_FREG generation with dir-light loop; exact define unknown, 'FS_ALPHA' is a placeholder name")
    else:  # Dbg / WWS material-token debug shaders
        status = "unrecovered"
        top = f"FRPG_FlverPBL/FRPG_FS_{fam}_{suf}.fx"
    entry = "FragmentMain_" + tok

    # ---- evidence checks (None = not applicable) ----
    tex = set(f["tex"])
    # magenta placeholder function: Dif + Mul without Spc compiles to a constant
    # colour (no textures, no cbuffer reads) in every generation seen so far
    stub = (mul and not spc and fam in ("Phn", "Gst", "Sfx") and f["n_instr"] < 100)
    lightless = stub or suf == "Non" or suf.startswith(("Dep", "Vel")) or fam in ("Dbg", "WWS")
    ev = {}
    if not stub:
        ev["Bmp<->t2(BumpMap)"] = (bmp == ("t2" in tex)) if not lightless else None
        ev["Spc<->t1(Spec/PBL)"] = (spc == ("t1" in tex)) if not suf.startswith(("Dep", "Vel")) else None
        if fam == "Dbg":  # normal-display debug: no diffuse textures, check the dual-UV input
            ev["Mul<->TEXCOORD6 xyzw (dual UV)"] = (mul == (f["isgn_mask"].get("TEXCOORD6") == "xyzw"))
        else:
            ev["Mul<->t3(Diffuse2)"] = (mul == ("t3" in tex)) if not suf.startswith(("Dep", "Vel")) else None
        if not lightless:
            # t6 doubles as the luminance texture for the reverse tonemap in the
            # translucent families, so check the light-map UV input instead:
            # Mul -> extra TEXCOORD7 (TexLit), else TEXCOORD6 widened to xyzw (TexDifLit)
            sig_lit = ("TEXCOORD7" in f["isgn"]) if mul else (f["isgn_mask"].get("TEXCOORD6") == "xyzw")
            ev["Lit<->lightmap UV in ISGN"] = (lit == sig_lit)
            if fam == "Phn":
                ev["Lit<->t6(LightMap)"] = (lit == ("t6" in tex))
        if not lightless:
            ev["Sdw/Csd<->t7(ShadowMap)"] = ((sdw != "___") == ("t7" in tex))
            ev["Csd<->ShadowMapMtx used"] = ((sdw == "Csd") == used_any(f, "gFC_ShadowMapMtxArray", "DL_FREG_140A", "DL_FREG_140", "DL_FREG_144"))
    if fam == "Gst" and not lightless and not stub:
        ev["Gst->Ghost consts used"] = used_any(f, "gFC_GhostParam", "gFC_GhostEdgeColor", "DL_FREG_136", "DL_FREG_137", "DL_FREG_138")
    if fam == "Phn" and not lightless:
        ev["Phn->no Ghost consts"] = not used_any(f, "gFC_GhostParam", "DL_FREG_138")
    if light.get("lerp") and not stub:
        # informational: in several generations Lerp compiles to the same code
        # as the non-Lerp sibling (see 'noop_modifiers')
        ev["Lerp->t13/t14 bound (informational)"] = ("t13" in tex and "t14" in tex)
    if light.get("mod") == "Subsurf" and spc:
        ev["Subsurf->SubsurfaceParam used"] = used_any(f, "gFC_SubsurfaceParam", "DL_FREG_192")
    if light.get("mod") == "Parallax" and bmp and spc:
        ev["Parallax->t10(Height) bound"] = "t10" in tex
    if light.get("pnt") and f["cb_style"] == "DL_FREG":
        n = light["pnt"].count("S")
        ev[f"Pnt{'S'*n}->{n} legacy light regs"] = (len(f["legacy_pnt_regs"]) == n)
    if fam == "Sfx" and suf == "" and not stub:
        ev["Sfx->GlowColor used"] = used_any(f, "DL_FREG_35", "gFC_GlowColor")

    return {
        "status": status, "top_level_file": top, "entry_point": entry,
        "cmdline_defines": cmd, "wrapper_defines": wrap, "light": light,
        "tokens": {"Spc": spc, "Bmp": bmp, "Mul": mul, "Lit": lit, "Shadow": sdw},
        "placeholder_stub": stub, "evidence": ev, "notes": notes,
    }


def other_record(p: dict, f: dict) -> dict:
    fam, kind = p["family"], p["kind"]
    cmd = list(CMDLINE)
    wrap = []
    ev = {}
    tex = set(f["tex"])
    if kind == "faceeye":
        top, entry = f"FRPG_FlverPBL/FRPG_FS_{fam}_FaceEye.fx", "FragmentMain_FaceEye_" + p["shadow"]
        if fam == "Gst": wrap.append("WITH_GhostMap")
        if p["shadow"] == "Sdw": wrap.append("WITH_ShadowMap=CalcLispPos_VS")
        if p["shadow"] == "Csd": wrap.append("WITH_ShadowMap=CalcLispPos_PS")
        cmd += pnt_defines(p["pnt"])
        ev["shadow<->t7"] = ((p["shadow"] != "___") == ("t7" in tex))
        if p["pnt"] and f["cb_style"] == "DL_FREG":
            ev["pnt count<->legacy regs"] = len(f["legacy_pnt_regs"]) == p["pnt"].count("S")
    elif kind == "deferred":
        tok = "Dif" + ("Spc" if p["spc"] else "___") + ("Bmp" if p["bmp"] else "___") + ("Lit" if p["lit"] else "___") + ("Mul" if p["mul"] else "")
        top, entry = "FRPG_FlverPBL/FRPG_FS_DeferredDisney.fx", "FragmentMain_" + tok
        for k, d in (("bmp", "WITH_BumpMap"), ("lit", "WITH_LightMap")):
            if p[k]: wrap.append(d)
        ev["Bmp<->t2"] = p["bmp"] == ("t2" in tex)
        ev["Spc<->t1"] = p["spc"] == ("t1" in tex)
        ev["Mul<->t3"] = p["mul"] == ("t3" in tex)
        ev["Lit<->t6"] = p["lit"] == ("t6" in tex)
    elif kind == "snow":
        top = "FRPG_FlverPBL/FRPG_FS_Snow.fx"
        entry = "FragmentMain_Snow_" + ("Lit" if p["lit"] else "___") + p["shadow"]
        if p["lit"]: wrap.append("WITH_LightMap")
        if p["shadow"] == "Csd": wrap.append("WITH_ShadowMap=CalcLispPos_PS_Csd")
        if p["shadow"] == "Ncs": wrap.append("WITH_ShadowMap=CalcLispPos_PS_NoCsd")
        cmd += pnt_defines(p["pnt"])
        ev["Lit<->t6"] = p["lit"] == ("t6" in tex)
        ev["shadow<->t7"] = (p["shadow"] != "___") == ("t7" in tex)
        if p["pnt"] and f["cb_style"] == "DL_FREG":
            ev["pnt count<->legacy regs"] = len(f["legacy_pnt_regs"]) == p["pnt"].count("S")
    elif kind == "water":
        top = "FRPG_FlverPBL/FRPG_FS_Water.fx"
        entry = f"FragmentMain_Water_{p['mode']}_{p['shadow']}"
        wrap.append(f"WITH_Water{p['mode']}")
        if p["shadow"] == "Csd": wrap.append("WITH_ShadowMap=CalcLispPos_PS_Csd")
        if p["shadow"] == "Ncs": wrap.append("WITH_ShadowMap=CalcLispPos_PS_NoCsd")
        cmd += pnt_defines(p["pnt"])
        ev["shadow<->t7"] = (p["shadow"] != "___") == ("t7" in tex)
        if p["pnt"] and f["cb_style"] == "DL_FREG":
            ev["pnt count<->legacy regs"] = len(f["legacy_pnt_regs"]) == p["pnt"].count("S")
    elif kind == "aux":
        top, entry = f"FRPG_FlverPBL/FRPG_FS_{fam}.fx", f"FragmentMain_{fam}_{p['pass']}"
    elif kind == "ntoa":
        top, entry = "FRPG_FlverPBL/FRPG_FS_NtoA.fx", "FragmentMain_NtoA_" + p["variant"]
        if p["variant"] == "Sdw": wrap.append("WITH_ShadowMap=CalcLispPos_VS")
        if p["variant"] == "Csd": wrap.append("WITH_ShadowMap=CalcLispPos_PS")
        if p["variant"] in ("Csd", "Sdw", "Non"):
            ev["shadow<->t7"] = (p["variant"] in ("Csd", "Sdw")) == ("t7" in tex)
        ev["NormalToAlphaParam used"] = used_any(f, "gFC_NormalToAlphaParam", "DL_FREG_176")
    elif kind == "pntnum":
        top, entry = "FRPG_FlverPBL/FRPG_FS_Dbg_PntNum.fx", "FragmentMain_PntNum"
        cmd += pnt_defines(p["pnt"])
        if f["cb_style"] == "DL_FREG":
            ev["pnt count<->legacy regs"] = len(f["legacy_pnt_regs"]) == p["pnt"].count("S")
    elif kind == "dbgsnow":
        top, entry = "FRPG_FlverPBL/FRPG_FS_Dbg_Snow.fx", "FragmentMain_Snow_" + p["variant"]
    elif kind == "single":
        top, entry = f"FRPG_FlverPBL/FRPG_FS_{fam}.fx", "FragmentMain"
    else:
        top, entry = "?", "?"
    return {"status": "unrecovered", "top_level_file": top, "entry_point": entry,
            "cmdline_defines": cmd, "wrapper_defines": wrap, "evidence": ev, "notes": []}


GEN_LABELS = {}


def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.abspath(os.path.join(here, "..", ".."))
    ap.add_argument("--root", default=os.path.join(repo, "vanilla_src"))
    args = ap.parse_args()
    blobdir = os.path.join(args.root, "blobs", "FRPG_FlverPBL_fpo_DX11")
    rebuild = os.path.join(args.root, "rebuild")
    asmdir = os.path.join(rebuild, "vanilla_asm")
    os.makedirs(asmdir, exist_ok=True)
    names = sorted(f[:-4] for f in os.listdir(blobdir) if f.endswith(".fpo"))

    def load(n):
        bp = os.path.join(blobdir, n + ".fpo")
        ap_ = os.path.join(asmdir, n + ".asm")
        if not os.path.exists(ap_):
            fxcasm.disassemble(bp, ap_)
        with open(ap_, encoding="utf-8", errors="replace") as fh:
            si = fxcasm.parse_asm(fh.read())
        with open(bp, "rb") as fh:
            data = fh.read()
        return n, si, data

    with ThreadPoolExecutor(24) as ex:
        loaded = list(ex.map(load, names))

    # header generations by $Globals layout hash
    gens = Counter()
    rec_gen = {}
    for n, si, _ in loaded:
        g = fxcasm.cb_layout_signature(si)
        rec_gen[n] = g
        gens[g] += 1

    first_by_md5 = {}
    out = {}
    for n, si, data in loaded:
        h = hashlib.md5(data).hexdigest()
        f = features(si)
        p = parse_name(n)
        if p["kind"] == "material":
            r = material_record(p, f)
        else:
            r = other_record(p, f)
        flags = fxcasm.decode_flags(fxcasm.rdef_flags(data))
        r_out = {
            "family": p["family"],
            "top_level_file": r["top_level_file"],
            "entry_point": r["entry_point"],
            "defines": r["cmdline_defines"] + r["wrapper_defines"],
        }
        r_out.update(r)
        r_out["profile"] = si.profile
        r_out["compile_flags"] = flags
        r_out["fxc_args"] = ["/T", "ps_5_0"] + flags["fxc_args"]
        r_out["header_gen"] = rec_gen[n]
        r_out["md5"] = h
        first = first_by_md5.setdefault(h, n)
        r_out["dup_of"] = first if first != n else None
        r_out["features"] = f
        evals = [v for k, v in r["evidence"].items() if v is not None and "informational" not in k]
        r_out["evidence_ok"] = all(evals) if evals else None
        if r["status"] == "recovered":
            r_out["confidence"] = "high: source recovered from SPDB; entry/defines from SPDB env block" if f["debug_lines"] \
                else "high: same wrapper/header generation as SPDB blobs"
        elif p["kind"] == "material":
            r_out["confidence"] = "medium: feature tokens evidenced; lighting-suffix defines and wrapper names are reconstruction conventions"
        else:
            r_out["confidence"] = "low: token meanings inferred from name + reflection only"
        out[n] = r_out

    # sibling analysis: which suffix modifiers are byte-level no-ops
    for n, r in out.items():
        lt = r.get("light") or {}
        if not lt or "base" not in lt or lt["base"] not in ("HemEnv", "HemDir3"):
            continue
        prefix = n[: n.rfind("_" + lt["base"])]
        noops = []
        for key in ("lerp", "mod", "pnt"):
            if key not in lt:
                continue
            parts = [lt["base"], lt.get("lerp", ""), lt.get("mod", ""), lt.get("pnt", "")]
            parts[["", "lerp", "mod", "pnt"].index(key)] = ""
            sib = prefix + "_" + "".join(parts)
            if sib in out and out[sib]["md5"] == r["md5"]:
                noops.append(lt[key])
        r["noop_modifiers"] = noops

    # generation labels: describe each $Globals layout from the data itself
    by_gen = defaultdict(list)
    for n, si, _ in loaded:
        by_gen[rec_gen[n]].append((n, si))
    gen_info = {}
    for g, items in by_gen.items():
        n0, si0 = items[0]
        gl = [cb for cb in si0.cbuffers if cb.name == "$Globals"]
        vs = [v.name for v in gl[0].vars] if gl else []
        fam = Counter(out[n]["family"] for n, _ in items)
        outs = Counter(len(si.osgn) for _, si in items)
        recovered = sum(out[n]["status"] == "recovered" for n, _ in items)
        gen_info[g] = {
            "blobs": len(items), "families": dict(fam), "osgn_counts": dict(outs),
            "style": ("register-pinned DL_FREG_nnn names" if any(v.startswith("DL_FREG") for v in vs)
                      else ("sequential gFC_* names (no register pinning)" if vs else "no $Globals")),
            "n_vars": len(vs),
            "has_DirLightVec": any(v.startswith("gFC_DirLightVec") for v in vs),
            "has_FREG_200plus": any(re.match(r"DL_FREG_2\d\d", v) for v in vs),
            "has_DebugDraw": "gFC_DebugDraw" in vs,
            "matches_recovered_headers": recovered == len(items),
            "example": n0,
        }
    for n, r in out.items():
        r["header_gen_info"] = {k: gen_info[r["header_gen"]][k] for k in ("style", "matches_recovered_headers")}
    with open(os.path.join(rebuild, "header_generations.json"), "w", encoding="utf-8") as fh:
        json.dump(gen_info, fh, indent=1)
    with open(os.path.join(rebuild, "mapping.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)

    # summary
    print(f"blobs: {len(out)}  unique md5: {len(set(r['md5'] for r in out.values()))}")
    print("families:", dict(Counter(r["family"] for r in out.values())))
    print("header generations:")
    for g, gi in json.load(open(os.path.join(rebuild, "header_generations.json"))).items():
        print(f"  {g}: {gi['blobs']:4} blobs  {gi['style']}, {gi['n_vars']} vars, DirLightVec={gi['has_DirLightVec']} "
              f"c200+={gi['has_FREG_200plus']} DebugDraw={gi['has_DebugDraw']} outputs={gi['osgn_counts']} "
              f"recovered={gi['matches_recovered_headers']} fam={gi['families']}")
    print("status:", dict(Counter(r["status"] for r in out.values())))
    fails = defaultdict(int)
    tot = defaultdict(int)
    for n, r in out.items():
        for k, v in r["evidence"].items():
            if v is None: continue
            tot[k] += 1
            if not v: fails[k] += 1
    print("evidence checks (fail/total):")
    for k in sorted(tot):
        print(f"  {k:36} {fails[k]:4}/{tot[k]}")
    print("evidence_ok:", dict(Counter(str(r["evidence_ok"]) for r in out.values())))
    unk = [n for n, r in out.items() if r["entry_point"] == "?"]
    print("noop modifiers (blob identical to sibling without that modifier):",
          dict(Counter(m for r in out.values() for m in r.get("noop_modifiers", []))))
    if unk:
        print("UNPARSED:", unk)


if __name__ == "__main__":
    main()
