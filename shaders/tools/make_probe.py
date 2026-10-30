"""Build the constant-buffer probe: shader archives, DrawParam variants, run folders, capture plan.

    .venv\\Scripts\\python.exe shaders\\tools\\make_probe.py [--res 3840x2160] [--cell 9] [--margin 20] [-j 24] [--skip-shaders]

Everything is rebuilt deterministically from pristine inputs (read only):
  vanilla/shader/FRPG_FlverPBL_fpo_DX11.shaderbnd.dcx   (sha256-checked against vanilla/manifest.sha256)
  vanilla/param/DrawParam/a10_DrawParam.parambnd.dcx  (via tools/dp.py)
  tools/dsr.psm1                                        (HUD masks of the capture harness)

Outputs (git-ignored):
  dist/_probe/layout.json                         screen layout + encoding, read by probe_decode.py
  dist/_probe/<shader>/shader/FRPG_FlverPBL_fpo_DX11.shaderbnd.dcx + report.json
        probe_reg         every 421b entry -> probe PS; $Globals RDEF = vanilla 421b list + float4 gProbe_Ext[57] @ c359
        probe_name        same SHEX bytes as probe_reg; RDEF additionally names c400..c411 with the
                          dir-light names of the older generations (gFC_DirLightVec[3], gFC_DirLightCol[3],
                          DL_FREG_092..097); only the RDEF differs from probe_reg
        probe_reg_vrdef   fallback: probe_reg SHEX with each entry's own vanilla RDEF chunk verbatim
                          (engine sees exactly the vanilla reflection, size 5744 B; registers past c358
                          then read whatever is bound, 0 if the bound buffer is smaller)
        probe_reg_strict  fallback: like probe_reg but the 306 entries whose vanilla PS never reads
                          SV_Position stay vanilla (their ISGN would differ in the SV_Position used-mask byte)
  dist/_probe/dp_<variant>/param/DrawParam/a10_DrawParam.parambnd.dcx + changes.json
  dist/_probe/run/<shader>__<dp>/{shader,param\\DrawParam}/...   install folders for capture_set.ps1
  dist/_probe/capture_plan.ps1                    exact capture commands
  dist/_probe/_work/                              generated HLSL, compiled blobs, disassembly cache

How a probe blob is made (no FromSoftware source is read or emitted):
  1. Interface: struct generated from the vanilla blob's ISGN/OSGN (as make_variant.py gentint);
     every vanilla 'always read' input component is consumed through Z(u)=(u*(u+1))&1 == 0 so fxc
     keeps the same used-masks; SV_Position.xy is read additionally for the tile position.
  2. Body: paints the bit grid of probe_layout.py from cb0 = float4 PR[416] (dynamic index) and
     cb2..cb5 (2 registers each). Families Sfx/Water/Ghost/NtoA/WWS (blended effects) discard
     every pixel instead (through an always-true but unfoldable test, keeping the used-masks).
  3. RDEF: compiled separately from a declaration-only shader generated from the vanilla 421b
     reflection (every variable pinned with register(cN) to its vanilla offset) plus the
     variant's extra variables and AlphaTestBuffer (b1) when the vanilla blob declares it; this
     RDEF chunk replaces the probe's own and the DXBC checksum is recomputed. The D3D runtime
     ignores RDEF, so the engine is the only consumer: if it binds by name, probe_name differs.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dxbc  # noqa: E402
import fxcasm  # noqa: E402
import probe_layout as PL  # noqa: E402
import shaderbnd  # noqa: E402
from make_variant import SWZ, _contiguous, _low_bit, _type, load_vanilla, sha256  # noqa: E402

ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "tools"))
ARCHIVE_REL = "shader/FRPG_FlverPBL_fpo_DX11.shaderbnd.dcx"
OUT = ROOT / "dist" / "_probe"
WORK = OUT / "_work"
FXC = os.environ.get("FXC", r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\fxc.exe")

GEN_421B = "421b132aa3"
NREG = 416                       # cb0 registers c0..c415 readable by the probe
VANILLA_END = 359                # 421b $Globals ends at offset 5744 = c359
EXTRA_SLOTS = (2, 3, 4, 5)       # other PS cbuffer slots sampled (2 registers each)
EXTRA_SLOT_REGS = 2
WHITE = 2.0                      # HDR level of a '1' cell (exposure is pinned by dp_fixed)
DISCARD_FAMILIES = {"Sfx", "Water", "Ghost", "NtoA", "WWS"}

NAMED = [  # probe_name: names used by older generations for the directional lights, at c400..c411
    ("float4", "gFC_DirLightVec[3]", 400),   # 3e9a/c634: c92..c94
    ("float4", "gFC_DirLightCol[3]", 403),   # 3e9a/c634: c95..c97
] + [("float4", f"DL_FREG_{n:03d}", 406 + n - 92) for n in range(92, 98)]  # 5519/b56c/0f37: c92..c97


def primaries_and_duplicates():
    cb0 = lambda r: PL.code_of(PL.SPACE_CB0, r)  # noqa: E731
    prim = [cb0(r) for r in range(256)]
    prim += [cb0(r) for r in range(352, 368)]          # end of the vanilla range and first registers past it
    prim += [cb0(r) for r in range(396, 416)]          # named region (c400..c411) with neighbours
    prim += [PL.code_of(PL.SPACE_RAMP, 0), PL.code_of(PL.SPACE_RAMP, 1)]
    prim += [PL.code_of(s, r) for s in EXTRA_SLOTS for r in range(EXTRA_SLOT_REGS)]
    dup = [cb0(r) for r in range(400, 412)] + [cb0(r) for r in range(103)] + [cb0(r) for r in range(352, 368)]
    dup += [PL.code_of(PL.SPACE_RAMP, 0), PL.code_of(PL.SPACE_RAMP, 1)]
    return prim, dup


# ---------------------------------------------------------------------------
# HLSL generation


def probe_hlsl(isgn, osgn, discard_all: bool, lay: dict, label: str) -> str:
    s, bc, br = lay["cell"], PL.BLOCK_COLS, PL.BLOCK_ROWS
    L = [f"// generated cbuffer probe ({label}); layout {lay['layout_id']}; no vendor source",
         "struct PSIn {"]
    reads, pos = [], None
    for n, e in enumerate(isgn):
        if not _contiguous(e.mask):
            raise ValueError(f"non-contiguous input mask {e.mask:#x}")
        width = bin(e.mask).count("1")
        interp = "nointerpolation " if e.comp_type != 3 and e.sysval == 0 else ""
        L.append(f"  {interp}{_type(e.comp_type, width)} i{n} : {e.name}{e.index};")
        lo = _low_bit(e.mask)
        for b in range(4):
            if e.rw >> b & 1:
                reads.append(f"i.i{n}.{SWZ[b - lo]}")
        if e.name == "SV_Position" and e.index == 0:
            pos = n
    if pos is None:
        raise ValueError("no SV_Position input")
    L.append("};")
    L.append(f"cbuffer ProbeCB0 : register(b0) {{ float4 PR[{NREG}]; }};")
    for sl in EXTRA_SLOTS:
        L.append(f"cbuffer ProbeCB{sl} : register(b{sl}) {{ float4 PS{sl}[{EXTRA_SLOT_REGS}]; }};")
    slots = lay["slots"]
    L.append(f"static const uint LAYOUT[{len(slots)}] = {{")
    for i in range(0, len(slots), 24):
        L.append("  " + ", ".join(f"0x{c:04x}u" for c in slots[i:i + 24]) + ",")
    L.append("};")
    L.append("uint Z(uint u) { return (u * (u + 1u)) & 1u; }")
    L.append("uint4 Fetch(uint space, uint reg) {")
    L.append(f"  if (space == 0u) return asuint(PR[min(reg, {NREG - 1}u)]);")
    for sl in EXTRA_SLOTS:
        L.append(f"  if (space == {sl}u) return asuint(PS{sl}[min(reg, {EXTRA_SLOT_REGS - 1}u)]);")
    L.append("  return uint4(0u, 0u, 0u, 0u);")
    L.append("}")
    L.append("float Level(uint2 px) {")
    L.append(f"  int2 q = int2(px) - int2({lay['origin'][0]}, {lay['origin'][1]});")
    L.append("  if (q.x < 0 || q.y < 0) return 0.0;")
    L.append(f"  uint bx = uint(q.x) / {bc * s}u; uint by = uint(q.y) / {br * s}u;")
    L.append(f"  if (bx >= {lay['grid_cols']}u || by >= {lay['grid_rows']}u) return 0.0;")
    L.append(f"  uint code = LAYOUT[by * {lay['grid_cols']}u + bx];")
    L.append("  if (code == 0xFFFFu) return 0.0;")
    L.append(f"  uint cx = (uint(q.x) - bx * {bc * s}u) / {s}u; uint cy = (uint(q.y) - by * {br * s}u) / {s}u;")
    L.append("  if (cy == 0u) {")
    L.append("    if (cx < 4u) return (cx & 1u) ? 0.0 : 1.0;")
    L.append("    if (cx < 16u) return float((code >> (15u - cx)) & 1u);")
    L.append("    return float(countbits(code & 0xFFFu) & 1u);")
    L.append("  }")
    L.append("  uint space = code >> 9; uint reg = code & 511u;")
    L.append(f"  if (space == {PL.SPACE_RAMP}u) {{")
    L.append(f"    float idx = float((cy - 1u) * {bc}u + cx);")
    L.append(f"    return reg == 0u ? idx / {PL.RAMP_CELLS - 1}.0 : exp2((idx - 108.0) / 12.0);")
    L.append("  }")
    L.append("  uint4 v = Fetch(space, reg);")
    L.append("  uint k = (cy - 1u) >> 1;")
    L.append("  uint w = k == 0u ? v.x : (k == 1u ? v.y : (k == 2u ? v.z : v.w));")
    L.append("  uint h16 = ((cy - 1u) & 1u) ? (w & 0xFFFFu) : (w >> 16);")
    L.append("  if (cx < 16u) return float((h16 >> (15u - cx)) & 1u);")
    L.append("  return float(countbits(h16) & 1u);")
    L.append("}")
    params = ["PSIn i"]
    body = ["  uint k = 0u;"] + [f"  k |= Z(asuint({r}));" for r in reads]
    # discard_all: k is always 0, so 'k == 0' always discards, but fxc cannot prove it and keeps the reads
    body.append("  if (k == 0u) discard;" if discard_all else ("  if (k != 0u) discard;" if reads else ""))
    body.append(f"  float g = Level(uint2(i.i{pos}.xy)) * {WHITE!r};")
    for n, e in enumerate(osgn):
        if not _contiguous(e.mask) or _low_bit(e.mask) != 0:
            raise ValueError(f"unsupported output mask {e.mask:#x}")
        width = bin(e.mask).count("1")
        params.append(f"out {_type(e.comp_type, width)} o{n} : {e.name}{e.index if e.name == 'SV_Target' else ''}")
        written = [SWZ[b] for b in range(width) if not (e.rw >> b & 1)]
        if not written:
            continue
        if e.name == "SV_Target" and e.index == 0 and e.comp_type == 3:
            vals = ["1.0" if c == "w" else "g" for c in written]
        else:
            vals = ["0"] * len(written)
        tgt = f"o{n}" if len(written) == width else f"o{n}.{''.join(written)}"
        body.append(f"  {tgt} = {_type(e.comp_type, len(written))}({', '.join(vals)});")
    L.append(f"void main({', '.join(params)})")
    L += ["{"] + [b for b in body if b] + ["}"]
    return "\n".join(L) + "\n"


def decl_hlsl(gvars, extras, alphatest: bool, label: str) -> str:
    """Declaration-only shader whose RDEF reproduces the vanilla $Globals list (+ extras)."""
    L = [f"// generated RDEF declaration ({label}) from vanilla 421b reflection"]
    for v in gvars:
        if v.offset % 16:
            raise ValueError(f"{v.name} offset {v.offset} not register aligned")
        L.append(f"uniform {v.type} {v.name} : register(c{v.offset // 16});")
    for typ, name, reg in extras:
        L.append(f"uniform {typ} {name} : register(c{reg});")
    if alphatest:
        L.append("cbuffer AlphaTestBuffer : register(b1) { int AlphaTest; float3 AlphaTestRef; float4 AlphaTest_padding; };")
    use = "gFC_ToneMap" + (" + (float)AlphaTest" if alphatest else "")
    L.append(f"float4 main() : SV_Target {{ return {use}; }}")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# compile / splice


def fxc(src: Path, out: Path, target="ps_5_0") -> tuple[int, str]:
    if out.exists():
        out.unlink()
    r = subprocess.run([FXC, "/nologo", "/T", target, "/E", "main", "/Fo", str(out), str(src)],
                       capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def compile_cached(src: str, tag: str) -> tuple[bytes | None, str]:
    key = hashlib.sha1(src.encode()).hexdigest()[:16]
    hp, op = WORK / "hlsl" / f"{tag}_{key}.hlsl", WORK / "fpo" / f"{tag}_{key}.fpo"
    hp.parent.mkdir(parents=True, exist_ok=True); op.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text(src, newline="\n")
    rc, log = fxc(hp, op)
    if rc != 0 or not op.exists():
        return None, f"{hp.name}: {log[-600:]}"
    return op.read_bytes(), ""


def build_dxbc(chunks: list[tuple[str, bytes]]) -> bytes:
    n = len(chunks)
    off = 0x20 + 4 * n
    offs, body = [], b""
    for cc, data in chunks:
        offs.append(off + len(body))
        body += cc.encode("latin-1") + struct.pack("<I", len(data)) + data
    total = off + len(body)
    blob = bytearray(b"DXBC" + b"\0" * 16 + struct.pack("<III", 1, total, n) + struct.pack(f"<{n}I", *offs) + body)
    blob[4:20] = dxbc.dxbc_checksum(bytes(blob))
    return bytes(blob)


def splice_rdef(probe: bytes, rdef: bytes) -> bytes:
    return build_dxbc([(c.fourcc, rdef if c.fourcc == "RDEF" else c.data) for c in dxbc.parse(probe)])


def disasm(blob: bytes) -> fxcasm.ShaderInfo:
    d = WORK / "asm_in"
    d.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha1(blob).hexdigest()
    p = d / f"{h}.fpo"
    if not p.exists():
        p.write_bytes(blob)
    return fxcasm.parse_asm(fxcasm.cached_disasm(str(p), str(WORK / "asm_cache")))


# ---------------------------------------------------------------------------
# shader variants


def build_shaders(lay: dict, jobs: int) -> dict:
    van = load_vanilla()
    entries = list(van.entries)
    uniq = {hashlib.sha1(e.data).hexdigest(): e.data for e in entries}  # identical blobs once (no file races)
    with ThreadPoolExecutor(jobs) as ex:
        by_hash = dict(zip(sorted(uniq), ex.map(lambda h: disasm(uniq[h]), sorted(uniq))))
    infos = [by_hash[hashlib.sha1(e.data).hexdigest()] for e in entries]
    gen421 = []
    ref_vars = None
    sigs = collections.Counter(fxcasm.cb_layout_signature(si) for si in infos)
    for e, si in zip(entries, infos):
        if fxcasm.cb_layout_signature(si) != GEN_421B:
            continue
        gvars = next(cb for cb in si.cbuffers if cb.name == "$Globals").vars
        sig = [(v.type, v.name, v.offset, v.size) for v in gvars]
        if ref_vars is None:
            ref_vars, ref_sig = gvars, sig
        elif sig != ref_sig:
            raise SystemExit(f"{e.name}: 421b $Globals differs from the first 421b blob")
        has_at = any(cb.name == "AlphaTestBuffer" for cb in si.cbuffers)
        gen421.append((e, has_at))
    last = ref_vars[-1]
    if (last.offset + last.size + 15) // 16 != VANILLA_END:
        raise SystemExit(f"421b $Globals does not end at c{VANILLA_END}")
    write_reg_names(ref_vars)

    variants = {
        "probe_reg": [("float4", f"gProbe_Ext[{NREG - VANILLA_END}]", VANILLA_END)],
        "probe_name": [("float4", f"gProbe_Ext[{400 - VANILLA_END}]", VANILLA_END)] + NAMED +
                      [("float4", "gProbe_Ext2[4]", 412)],
    }
    # RDEF per (variant, alphatest)
    rdefs, decl_reports = {}, {}
    for vname, extras in variants.items():
        for at in (False, True):
            blob, err = compile_cached(decl_hlsl(ref_vars, extras, at, f"{vname} alphatest={at}"), f"decl_{vname}_{int(at)}")
            if blob is None:
                raise SystemExit(f"decl compile failed: {err}")
            rdefs[(vname, at)] = dxbc.chunk_map(blob)["RDEF"]
            si = disasm(blob)
            g = next(cb for cb in si.cbuffers if cb.name == "$Globals").vars
            got = [(v.type, v.name, v.offset, v.size) for v in g]
            decl_reports[f"{vname}/alphatest={int(at)}"] = {
                "vanilla_vars_identical_prefix": got[:len(ref_sig)] == ref_sig,
                "n_vars": len(got), "extra_vars": [f"{t} {n} @c{o // 16} ({s} B)" for t, n, o, s in got[len(ref_sig):]],
                "cbuffers": [cb.name for cb in si.cbuffers],
            }
            if got[:len(ref_sig)] != ref_sig:
                raise SystemExit(f"decl RDEF for {vname} does not reproduce the vanilla 421b list")

    # probe bodies (shared by all variants)
    plan, jobs_src = {}, {}
    for e, has_at in gen421:
        ch = dxbc.chunk_map(e.data)
        fam = e.name.rsplit(".", 1)[0].split("_")[1]
        src = probe_hlsl(dxbc.parse_signature(ch["ISGN"]), dxbc.parse_signature(ch["OSGN"]),
                         fam in DISCARD_FAMILIES, lay, fam)
        key = hashlib.sha1(src.encode()).hexdigest()[:16]
        plan[e.name] = (key, fam, has_at)
        jobs_src.setdefault(key, src)
    with ThreadPoolExecutor(jobs) as ex:
        compiled = dict(zip(sorted(jobs_src), ex.map(lambda k: compile_cached(jobs_src[k], "probe"), sorted(jobs_src))))
    bad = {k: err for k, (b, err) in compiled.items() if b is None}
    if bad:
        raise SystemExit(f"probe compile failures: {json.dumps(bad, indent=1)[:3000]}")

    results = {}
    shex_by_variant = collections.defaultdict(dict)
    for vname in ("probe_reg", "probe_name", "probe_reg_strict", "probe_reg_vrdef"):
        rkey = "probe_reg" if vname in ("probe_reg_strict", "probe_reg_vrdef") else vname
        reps, gate, kept = {}, collections.Counter(), {}
        fams = collections.Counter()
        for e, has_at in gen421:
            key, fam, _ = plan[e.name]
            pb = compiled[key][0]
            vch, pch = dxbc.chunk_map(e.data), dxbc.chunk_map(pb)
            vi, pi = dxbc.parse_signature(vch["ISGN"]), dxbc.parse_signature(pch["ISGN"])
            only_pos_rw = (vch["ISGN"] != pch["ISGN"] and len(vi) == len(pi) and all(
                (a.name, a.index, a.sysval, a.comp_type, a.register, a.mask) == (b.name, b.index, b.sysval, b.comp_type, b.register, b.mask)
                and (a.rw == b.rw or (a.name == "SV_Position" and a.rw == 0 and b.rw == 3)) for a, b in zip(vi, pi)))
            if pch["OSGN"] != vch["OSGN"]:
                kept[e.name] = "OSGN differs"; continue
            if vch["ISGN"] == pch["ISGN"]:
                gate["isgn_identical"] += 1
            elif only_pos_rw:
                if vname == "probe_reg_strict":
                    kept[e.name] = "vanilla PS does not read SV_Position (strict variant keeps vanilla)"; continue
                gate["isgn_differs_only_in_SV_Position_used_mask_0_to_3"] += 1
            else:
                kept[e.name] = "ISGN differs beyond SV_Position used mask"; continue
            blob = splice_rdef(pb, vch["RDEF"] if vname == "probe_reg_vrdef" else rdefs[(rkey, has_at)])
            if not dxbc.checksum_ok(blob) or dxbc.shader_version(dxbc.chunk_map(blob)) != ("ps", 5, 0):
                kept[e.name] = "checksum/version gate failed"; continue
            reps[e.name] = blob
            shex_by_variant[vname][e.name] = dxbc.chunk_map(blob)["SHEX"]
            fams[("discard:" if fam in DISCARD_FAMILIES else "probe:") + fam] += 1
        rep = finish(van, vname, reps, {
            "variant": vname, "replaced_by_family": dict(sorted(fams.items())), "gate": dict(gate),
            "kept_vanilla_count": len(kept), "kept_vanilla": dict(sorted(kept.items())[:400]),
            "unique_probe_bodies": len(jobs_src), "rdef_decl": ("each entry keeps its own vanilla RDEF chunk verbatim" if vname == "probe_reg_vrdef" else
                                                         {k: v for k, v in decl_reports.items() if k.startswith(rkey + "/")}),
            "non421b_entries_kept_vanilla": len(entries) - len(gen421), "layout_id": lay["layout_id"],
            "entries_by_layout_signature": dict(sorted(sigs.items())),
        })
        results[vname] = rep
    same = shex_by_variant["probe_reg"] == shex_by_variant["probe_name"]
    results["probe_reg"]["shex_identical_to_probe_name"] = same
    results["probe_name"]["shex_identical_to_probe_reg"] = same
    for v in ("probe_reg", "probe_name"):
        (OUT / v / "report.json").write_text(json.dumps(results[v], indent=1, sort_keys=True))
    return results


def write_reg_names(gvars) -> None:
    """dist/_probe/reg_names.json: register -> 421b variable (with element/row/column suffix and
    the components it covers), plus the probe_name extras. Read by probe_decode.py for labels."""
    def regs_of(typ, name, off, size):
        base, n = (name.split("[")[0], int(name.split("[")[1][:-1])) if "[" in name else (name, 1)
        t = typ.split()[-1]
        rpe = {"float4x4": 4, "float3x4": 3}.get(t, 1)
        width = {"float": 1, "float2": 2, "float3": 3}.get(t, 4)
        out = []
        for el in range(n):
            for r in range(rpe):
                suf = (f"[{el}]" if "[" in name else "") + (f".{'row' if 'row_major' in typ else 'col'}{r}" if rpe > 1 else "")
                out.append((off // 16 + el * rpe + r, f"{base}{suf}", "xyzw"[:width]))
        return out
    names = {}
    for v in gvars:
        for reg, nm, comps in regs_of(v.type, v.name, v.offset, v.size):
            names[str(reg)] = {"name": nm, "type": v.type, "comps": comps}
    extra = {str(reg + i): f"{n.split('[')[0]}[{i}]" if "[" in n else n
             for t, n, reg in NAMED for i in range(int(n.split("[")[1][:-1]) if "[" in n else 1)}
    (OUT / "reg_names.json").write_text(json.dumps({"vanilla_421b": names, "probe_name_extras": extra}, indent=1))


def finish(van, variant: str, reps: dict, report: dict) -> dict:
    dst = OUT / variant / ARCHIVE_REL
    bnd = shaderbnd.write(van, dst, reps)
    back = shaderbnd.read_archive(dst)
    diffs = shaderbnd.semantic_diff(van.bnd, back.bnd, set(reps))
    parsed = shaderbnd.parse_bnd3(back.bnd)
    vmap = van.entry_by_name()
    payload_bad = [e.name for e in parsed.entries if e.data != reps.get(e.name, vmap[e.name].data)]
    report.update({
        "archive": str(dst.relative_to(ROOT)), "sha256": sha256(dst), "entries": len(parsed.entries),
        "replaced_entries": len(reps), "semantic_diffs_vs_vanilla": diffs[:50], "payload_mismatches": payload_bad[:50],
        "readback_bnd_equals_written": back.bnd == bnd,
    })
    report["gate_ok"] = not diffs and not payload_bad and back.bnd == bnd
    (OUT / variant / "report.json").write_text(json.dumps(report, indent=1, sort_keys=True))
    return report


# ---------------------------------------------------------------------------
# DrawParam variants


def _rot(v: int, lo: int, hi: int, by: int = 90) -> int:
    """Rotate an angle field by `by` degrees, staying inside [lo, hi] without clamping."""
    if hi - lo >= 360:  # Y: wrap
        r = (v + by - lo) % 360 + lo
        return r - 360 if r > hi else r
    return v + by if v + by <= hi else v - by  # X in [-90, 90]: move by 90 the other way


def build_drawparams() -> dict:
    from dp import get_field, iter_params, load_vanilla as dp_load, set_field
    from paramdef_limits import limits
    lim = limits()

    def clamp(bank, f, v):
        d = lim["LightBank" if bank == "s_LightBank" else bank][f]
        v = min(max(v, d["min"]), d["max"])
        return int(round(v)) if d["type"] in {"s8", "u8", "s16", "u16", "s32", "u32"} else float(v)

    def fixed(bank, row, put):
        if bank == "ToneMapBank":
            put("grayKeyValue", 0.18); put("minAdaptedLum", 0.18); put("maxAdapredLum", 0.18)
            put("bloomMul", 0); put("bloomMulFar", 0); put("lightShaftPower", 0.0)
        elif bank == "DofBank":
            put("farDofMul", 0); put("nearDofMul", 0)
            put("farDofBegin", 999999.0); put("farDofEnd", 999999.0); put("nearDofBegin", 0.0); put("nearDofEnd", 0.0)
        elif bank == "LensFlareBank":
            put("colA", 0)

    def light(edit):
        def f(bank, row, put, get):
            if bank in ("LightBank", "s_LightBank"):
                edit(put, get)
        return f

    V = {
        "dp_fixed": None,
        "dp_key0": light(lambda put, get: put("colA_0", 0)),
        "dp_key4red": light(lambda put, get: (put("colA_0", get("colA_0") * 4), put("colR_0", 255), put("colG_0", 0), put("colB_0", 0))),
        "dp_keyrot": light(lambda put, get: (put("degRotX_0", _rot(get("degRotX_0"), -90, 90)), put("degRotY_0", _rot(get("degRotY_0"), -180, 180)))),
        "dp_amb2": light(lambda put, get: (put("colA_u", get("colA_u") * 2), put("colA_d", get("colA_d") * 2))),
        "dp_spc": light(lambda put, get: (put("colA_s", max(get("colA_s") * 4, 400)), put("colR_s", 255), put("colG_s", 0), put("colB_s", 0),
                                         put("degRotX_s", _rot(get("degRotX_s"), -90, 90)), put("degRotY_s", _rot(get("degRotY_s"), -180, 180)))),
    }

    def scatter(bank, row, put, get):
        if bank == "LightScatteringBank":
            put("sunA", get("sunA") * 4); put("sunR", 255); put("sunG", 40); put("sunB", 0)
            put("sunRotX", _rot(get("sunRotX"), -90, 90)); put("sunRotY", _rot(get("sunRotY"), -180, 180))

    def shadow(bank, row, put, get):
        if bank == "ShadowBank":
            put("lightDegRotX", _rot(get("lightDegRotX"), -90, 90)); put("lightDegRotY", _rot(get("lightDegRotY"), -180, 180))

    V["dp_scatter"] = scatter
    V["dp_shadowrot"] = shadow
    reports = {}
    for name, extra in V.items():
        bnd = dp_load("a10")
        changes = collections.Counter()
        samples = {}
        for slot, stem, param in iter_params(bnd):
            for rid, row in param.rows.items():
                def get(f, row=row):
                    return get_field(row, f)

                def put(f, v, row=row, stem=stem, rid=rid, slot=slot):
                    old = get_field(row, f)
                    new = clamp(stem, f, v)
                    set_field(row, f, new)
                    if old != new:
                        changes[f"{stem}.{f}"] += 1
                    samples.setdefault(f"{stem}.{f}", [rid, old, new])
                fixed(stem, row, put)
                if extra:
                    extra(stem, row, put, get)
        dst = OUT / name / "param" / "DrawParam" / "a10_DrawParam.parambnd.dcx"
        dst.parent.mkdir(parents=True, exist_ok=True)
        bnd.write(dst)
        reports[name] = {"file": str(dst.relative_to(ROOT)), "sha256": sha256(dst),
                         "rows_changed_per_field": dict(sorted(changes.items())),
                         "first_row_sample_[row,old,new]": dict(sorted(samples.items()))}
        (OUT / name / "changes.json").write_text(json.dumps(reports[name], indent=1, default=str))
    return reports


# ---------------------------------------------------------------------------
# run folders + capture plan

DP_ORDER = ["dp_fixed", "dp_key0", "dp_key4red", "dp_keyrot", "dp_amb2", "dp_spc", "dp_scatter", "dp_shadowrot", "dp_fixed_b"]
SHADER_RUNS = ["probe_reg", "probe_name"]
BONFIRES = ["parish", "firelink"]


def build_runs() -> list[str]:
    run = OUT / "run"
    if run.exists():
        shutil.rmtree(run)
    for sh in SHADER_RUNS + ["probe_reg_strict", "probe_reg_vrdef"]:
        for dp in DP_ORDER:
            d = run / f"{sh}__{dp}"
            (d / "shader").mkdir(parents=True)
            (d / "param" / "DrawParam").mkdir(parents=True)
            shutil.copyfile(OUT / sh / ARCHIVE_REL, d / ARCHIVE_REL)
            src_dp = "dp_fixed" if dp == "dp_fixed_b" else dp   # dp_fixed_b = repeat control (same file)
            shutil.copyfile(OUT / src_dp / "param" / "DrawParam" / "a10_DrawParam.parambnd.dcx",
                            d / "param" / "DrawParam" / "a10_DrawParam.parambnd.dcx")
    lines = [
        "# Generated by shaders/tools/make_probe.py - cbuffer probe capture runs.",
        "# One game restart per shader variant (shader_session.ps1), then DrawParam variants are cycled by",
        "# warping (capture_set.ps1 installs each run folder; the shader file is re-copied but not re-read).",
        "# Order per bonfire: " + ", ".join(DP_ORDER) + "  (dp_fixed_b = repeat of dp_fixed = noise baseline).",
        "$ErrorActionPreference = 'Stop'",
        "Set-Location (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))",
    ]
    for sh in SHADER_RUNS:
        cv = ",".join(f"dist\\_probe\\run\\{sh}__{dp}" for dp in DP_ORDER)
        lines.append(f".\\tools\\shader_session.ps1 -Run probe1 -Variants dist\\_probe\\{sh} -Only {','.join(BONFIRES)} -CaptureVariants {cv}")
    lines += [
        "# decode (writes captures\\probe1\\<shader>\\probe_report.json):",
        ".\\.venv\\Scripts\\python.exe shaders\\tools\\probe_decode.py session captures\\probe1\\probe_reg",
        ".\\.venv\\Scripts\\python.exe shaders\\tools\\probe_decode.py session captures\\probe1\\probe_name",
        ".\\.venv\\Scripts\\python.exe shaders\\tools\\probe_decode.py names captures\\probe1",
        "# fallback if probe_reg fails to start: replace dist\\_probe\\probe_reg by dist\\_probe\\probe_reg_strict and",
        "# the run folders by dist\\_probe\\run\\probe_reg_strict__<dp>; if every enlarged-RDEF variant fails, use",
        "# dist\\_probe\\probe_reg_vrdef (each entry keeps its vanilla RDEF chunk) the same way.",
    ]
    (OUT / "capture_plan.ps1").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--res", default="3840x2160")
    ap.add_argument("--cell", type=int, default=9)
    ap.add_argument("--margin", type=int, default=20)
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 8)
    ap.add_argument("--skip-shaders", action="store_true")
    a = ap.parse_args(argv)
    res = tuple(int(x) for x in a.res.lower().split("x"))
    OUT.mkdir(parents=True, exist_ok=True)
    prim, dup = primaries_and_duplicates()
    lay = PL.make_layout(res, a.cell, margin=a.margin, primaries=prim, duplicates=dup)
    lay.update({"white_hdr": WHITE, "nreg": NREG, "vanilla_end_reg": VANILLA_END,
                "named_region": {n: r for _, n, r in NAMED}, "extra_slots": list(EXTRA_SLOTS),
                "discard_families": sorted(DISCARD_FAMILIES)})
    (OUT / "layout.json").write_text(json.dumps(lay, indent=1))
    summary = {"layout": {k: lay[k] for k in ("layout_id", "res", "cell", "grid_cols", "grid_rows", "free_slots", "unused_free_slots")},
               "layout_blocks": len(lay["blocks"]), "layout_dropped": [PL.describe(c) for c in lay["dropped"]]}
    if lay["dropped"]:
        print(f"warning: {len(lay['dropped'])} blocks did not fit: {summary['layout_dropped'][:20]}")
    if not a.skip_shaders:
        sh = build_shaders(lay, a.jobs)
        summary["shaders"] = {k: {kk: v[kk] for kk in ("gate_ok", "replaced_entries", "kept_vanilla_count", "gate",
                                                       "replaced_by_family", "sha256") if kk in v} for k, v in sh.items()}
        summary["shex_identical_reg_vs_name"] = sh["probe_reg"].get("shex_identical_to_probe_name")
    summary["drawparams"] = {k: v["rows_changed_per_field"] for k, v in build_drawparams().items()}
    if not a.skip_shaders:
        build_runs()
    (OUT / "build_summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True))
    print(json.dumps(summary, indent=1, sort_keys=True))
    ok = a.skip_shaders or all(v["gate_ok"] for v in summary["shaders"].values())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
