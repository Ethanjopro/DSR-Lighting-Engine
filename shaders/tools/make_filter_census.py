"""Build the filter-pass census: shader archives that show, in an ordinary capture, which post-process passes
of FRPG_Filter_DX11 actually run in DSR.

    .venv\\Scripts\\python.exe shaders\\tools\\make_filter_census.py [--only fcensus_1,fcensus_2] [--no-d3d]

Every census entry is the *vanilla* blob with a small gate patched onto its output (shexpatch.py), so the
pass keeps its vanilla interface (RDEF, ISGN, OSGN, STAT byte-identical; only SHEX differs) and its vanilla
result everywhere outside the gate:

  fcensus_1  tonemap + final AA (questions a, b). Each candidate pass multiplies its output by a strong
             colour tint inside its own cell of a 4x2 grid on the right-centre of the screen (UV space,
             clear of the HUD and of the screen-state probe pixels used by tools/dsr.psm1).
  fcensus_2  ambient occlusion path (question c). The shaders that write the final AO value force AO = 0
             (fully occluded) on stripes, one orientation per path: pixel SAO vertical (UV, 16 cycles
             across the screen), compute SAO horizontal, compute SSAO diagonal (both 64 AO-buffer pixels
             per period, so the measured period also gives the AO buffer scale; every level of an upsample
             chain is striped, and the finest detected level is the pass that writes the final AO).
  fcensus_3  optional: SAO_Combine, LightShaft, Bloom and Sfx_Glow_Blur cells (same grid as fcensus_1; Bloom and
             Sfx_Glow_Blur are byte-identical in vanilla, so each gets its own cell).

Inputs (read only): vanilla/shader/FRPG_Filter_DX11.shaderbnd.dcx, sha256-checked against vanilla/manifest.sha256.
Outputs:
  dist/_shader_install/fcensus_<n>/shader/FRPG_Filter_DX11.shaderbnd.dcx   install folder (archive only)
  dist/_shader_install/fcensus_plan.txt                                    what to capture and what to expect
  dist/_shader/filter_census/fcensus_<n>.report.json                       per-archive gates
  dist/_shader/filter_census/layout.json                                   cells / stripes for the decoder
  dist/_shader/filter_census/_work/                                        patched blobs and disassembly

Gates, per patched blob: (1) all non-SHEX chunks byte-identical to vanilla, checksum valid; (2) fxc /dumpbin of
the patched blob equals the vanilla disassembly with exactly the expected edits (o0 renamed, dcl_temps bumped,
gate block inserted before each ret / gated store); (3) register sanity (temps < dcl_temps, o0 written only by
the gate); (4) the blob is accepted by ID3D11Device::Create*Shader on a WARP device (vanilla as control).
Per archive: opens, entry names/ids/order identical to vanilla, only the intended entries differ, DCX
type/level/header layout as vanilla, read-back payloads equal what was written.

Original tooling; no FromSoftware source is read or emitted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dxbc  # noqa: E402
import shexpatch  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_REL = "shader/FRPG_Filter_DX11.shaderbnd.dcx"
VANILLA = ROOT / "vanilla" / ARCHIVE_REL
MANIFEST = ROOT / "vanilla" / "manifest.sha256"
INSTALL = ROOT / "dist" / "_shader_install"
OUT = ROOT / "dist" / "_shader" / "filter_census"
WORK = OUT / "_work"
FXC = os.environ.get("FXC", r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\fxc.exe")

# ---------------------------------------------------------------------------
# layout (normalised screen UV, origin top-left). Shared with filter_census_decode.py.

CAPTURE_RES = (3840, 2160)
# tools/dsr.psm1: HUD masks (4K px, x0 y0 x1 y1) and the screen-state probe pixels (4K px)
HUD_MASKS = [(0, 0, 1700, 460), (240, 1120, 1160, 2010), (3080, 1880, 3560, 2030)]
STATE_PROBES = {"hp": (1000, 244), "stamina": (920, 304), "dialog": (1920, 960), "menu_row0": (1920, 1580)}
CELL_X0, CELL_DX, CELL_W = 0.62, 0.09, 0.07   # right of the player character (bonfire camera)
CELL_Y0, CELL_DY, CELL_H = 0.08, 0.38, 0.30


def cell_rect(col: int, row: int) -> tuple[float, float, float, float]:
    x0 = round(CELL_X0 + CELL_DX * col, 6)
    y0 = round(CELL_Y0 + CELL_DY * row, 6)
    return x0, y0, round(x0 + CELL_W, 6), round(y0 + CELL_H, 6)


K = 0.3   # tint factor of the suppressed channels
TINT = {"red": (1, K, K, 1), "green": (K, 1, K, 1), "blue": (K, K, 1, 1), "yellow": (1, 1, K, 1),
        "magenta": (1, K, 1, 1), "cyan": (K, 1, 1, 1),
        "bloom_red": (3, 0.2, 0.2, 1), "shaft_blue": (0.2, 0.2, 3, 1), "glow_green": (0.2, 3, 0.2, 1),
        "alpha0": (1, 1, 1, 0)}
# ResolveTAA writes HDR (it undoes the c/(1+luma) compression at its output), so its tint passes through the
# tonemap curve and the 1/2.2 gamma before it reaches the capture: in log terms only ~0.15-0.5 of log2(tint)
# survives (0.45 from the gamma alone, less on the curve's shoulder). The decoder scales its thresholds by this.
ATTEN_PRE_TONEMAP = 0.4

# compute paths: (coord >> 5) & 1 -> 64 AO-buffer px per period. 64 (not 128) keeps a 1/16-res level of an SSAO
# upsample chain at 1024 screen px, i.e. several periods per frame band: a level whose period spans only one or two
# periods per band leaks its square edges into the finer levels' frequencies and hides them.
STRIPE_SHIFT = 5
PIXEL_CYCLES = 16  # pixel SAO: 16 periods across the screen (240 px at 3840)
AO_SCALES = (1, 2, 4, 8, 16)   # candidate screen px (at CAPTURE_RES) per AO-buffer texel for the compute paths
AO_PATHS = {
    "pixel_sao": {"orientation": "vertical", "period": f"{PIXEL_CYCLES} cycles across the width (UV)",
                  "screen_period_px": CAPTURE_RES[0] / PIXEL_CYCLES},
    "compute_sao": {"orientation": "horizontal", "period": f"{2 << STRIPE_SHIFT} AO-buffer px",
                    "ao_period_px": 2 << STRIPE_SHIFT, "scales": list(AO_SCALES)},
    "compute_ssao": {"orientation": "diagonal", "period": f"{2 << STRIPE_SHIFT} AO-buffer px along x+y",
                     "ao_period_px": 2 << STRIPE_SHIFT, "scales": list(AO_SCALES)},
}


def _cell(entry, label, col, row, tint, note="", signal="full", atten=1.0):
    """signal: 'full' = the pass output is the visible colour (cast over the whole cell); 'partial' = the pass
    output is one additive contribution (cast only where that contribution exists); 'luma' = brightness only.
    atten (full cells): expected fraction of log2(tint) that reaches the capture (1 = the pass output is the
    displayed colour; < 1 = a non-linear transform such as the tonemap follows the pass)."""
    return {"entry": entry, "label": label, "kind": "ps_cell", "cell": [col, row], "rect": list(cell_rect(col, row)),
            "tint_name": tint, "tint": list(TINT[tint]), "note": note, "signal": signal, "atten": atten}


CENSUS = {
    "fcensus_1": {
        "title": "tonemap + final AA (questions a, b)",
        "decode": "cells",
        "entries": [
            _cell("FRPG_Fil_HDR.fpo", "HDR", 0, 0, "red"),
            _cell("FRPG_Fil_HDR_ColAdj.fpo", "HDR_ColAdj", 1, 0, "green"),
            _cell("FRPG_Fil_HDR_PBL.fpo", "HDR_PBL", 2, 0, "blue"),
            _cell("FRPG_Fil_HDR_PBL_ColAdj.fpo", "HDR_PBL_ColAdj", 3, 0, "yellow"),
            _cell("FRPG_Fil_HDR_Menu.fpo", "HDR_Menu", 0, 1, "magenta"),
            _cell("FRPG_Fil_FxAA2.fpo", "FxAA2", 1, 1, "cyan"),
            _cell("FRPG_Fil_FxAA2_High.fpo", "FxAA2_High", 2, 1, "red"),
            _cell("FRPG_Fil_ResolveTAA.fpo", "ResolveTAA", 3, 1, "green",
                  "o0 only (o1 untouched). o0 is HDR, before the tonemap: the cast reaches the capture compressed "
                  "(about 0.15-0.5 of x0.3 in log terms). The per-channel 3x3 history clamp pulls a tinted history "
                  "back every frame, so the tint does not compound.", atten=ATTEN_PRE_TONEMAP),
        ],
    },
    "fcensus_2": {
        "title": "ambient-occlusion path (question c)",
        "decode": "stripes",
        "entries": [
            {"entry": "FRPG_Fil_SAO_Main.fpo", "label": "SAO_Main (pixel)", "kind": "ps_stripes", "path": "pixel_sao",
             "gate": ["stripes_u", PIXEL_CYCLES], "tint": [0, 1, 1, 1]},
            {"entry": "FRPG_Fil_SAO_Blur.fpo", "label": "SAO_Blur (pixel)", "kind": "ps_stripes", "path": "pixel_sao",
             "gate": ["stripes_u", PIXEL_CYCLES], "tint": [0, 1, 1, 1]},
            {"entry": "FRPG_Compute_SAO_Main.cpo", "label": "SAO_Main (compute)", "kind": "cs_stripes",
             "path": "compute_sao", "uav": 0, "axis": "y", "shift": STRIPE_SHIFT, "comps": "xw"},
            {"entry": "FRPG_Compute_SAO_Blur.cpo", "label": "SAO_Blur (compute)", "kind": "cs_stripes",
             "path": "compute_sao", "uav": 0, "axis": "y", "shift": STRIPE_SHIFT, "comps": "xw"},
        ] + [
            {"entry": f"FRPG_Compute_SSAO_{n}.cpo", "label": f"SSAO_{n} (compute)", "kind": "cs_stripes",
             "path": "compute_ssao", "uav": 0, "axis": "x+y", "shift": STRIPE_SHIFT, "comps": "x"}
            for n in ("BlurUpsample", "BlurUpsampleBlendOut", "BlurUpsamplePreMin", "BlurUpsamplePreMinBlendOut")
        ],
    },
    "fcensus_3": {
        "title": "optional: Bloom / Sfx_Glow_Blur, LightShaft, SAO_Combine",
        "decode": "cells",
        # The partial-signal cells sit in columns 1-3: column 0 overlaps the drifting mist in the darkroot
        # archway, which gave a cross-run false Bloom hit there. SAO_Combine (luma signal) takes column 0.
        "entries": [
            _cell("FRPG_Fil_SAO_Combine.fpo", "SAO_Combine", 0, 0, "alpha0",
                  "alpha (= AO visibility) forced to 0: darker cell if the combine multiplies the frame by alpha",
                  signal="luma"),
            _cell("FRPG_Fil_LightShaft.fpo", "LightShaft", 1, 0, "shaft_blue", "blue x3 gain; shows only where shafts exist",
                  signal="partial"),
            _cell("FRPG_Fil_Bloom.fpo", "Bloom", 2, 0, "bloom_red", "red x3 gain on the bloom output; shows only where bloom exists",
                  signal="partial"),
            _cell("FRPG_Fil_Sfx_Glow_Blur.fpo", "Sfx_Glow_Blur", 3, 0, "glow_green",
                  "byte-identical to FRPG_Fil_Bloom.fpo in vanilla; patched in its own cell so the entry name the "
                  "engine uses for the bloom blur can be told apart. Green x3 gain.", signal="partial"),
        ],
    },
}


def layout() -> dict:
    return {
        "capture_res": list(CAPTURE_RES), "hud_masks_4k": [list(m) for m in HUD_MASKS],
        "state_probes_4k": {k: list(v) for k, v in STATE_PROBES.items()},
        "cell_grid": {"x0": CELL_X0, "dx": CELL_DX, "w": CELL_W, "y0": CELL_Y0, "dy": CELL_DY, "h": CELL_H},
        "ao_paths": AO_PATHS, "archives": CENSUS,
    }


# ---------------------------------------------------------------------------
# helpers


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def load_vanilla():
    import shaderbnd
    want = None
    for line in MANIFEST.read_text().splitlines():
        h, _, rel = line.partition("  ")
        if rel.strip() == ARCHIVE_REL:
            want = h
    got = sha256(VANILLA.read_bytes())
    if want is None or got != want:
        raise SystemExit(f"vanilla archive hash mismatch: manifest={want} file={got}")
    return shaderbnd.read_archive(VANILLA)


def disasm(blob: bytes, tag: str) -> list[str]:
    d = WORK / "asm"
    d.mkdir(parents=True, exist_ok=True)
    h = sha256(blob)[:16]
    bp, ap = d / f"{tag}.{h}.bin", d / f"{tag}.{h}.asm"
    if not ap.exists():
        bp.write_bytes(blob)
        r = subprocess.run([FXC, "/nologo", "/dumpbin", "/Fc", str(ap), str(bp)], capture_output=True, text=True)
        if r.returncode != 0 or not ap.exists():
            raise RuntimeError(f"fxc /dumpbin failed for {tag}: {(r.stdout + r.stderr).strip()[-300:]}")
    return ap.read_text(encoding="utf-8", errors="replace").splitlines()


def _ff(v: float) -> str:
    return f"{float(v):f}"


def _split_ops(rest: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in rest:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip()); cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _norm(line: str):
    """Comparable form of one disassembly line: comments verbatim; instructions as (indent, opcode, operands)
    with immediates l(...) parsed to numbers (fxc prints the same value as 0, 0.000000 or 0x00000000)."""
    s = line.rstrip()
    if not s.strip() or s.lstrip().startswith("//"):
        return ("text", s)
    indent = len(s) - len(s.lstrip())
    body = s.strip()
    if " " not in body:
        return ("ins", indent, body, ())
    opc, rest = body.split(" ", 1)
    ops = []
    for o in _split_ops(rest):
        m = re.fullmatch(r"l\((.*)\)", o)
        if m:
            vals = []
            for t in m.group(1).split(","):
                t = t.strip()
                vals.append(int(t, 16) if t.lower().startswith("0x") else float(t))
            ops.append(("imm", tuple(vals)))
        else:
            ops.append(o)
    return ("ins", indent, opc, tuple(ops))


def _same(a, b) -> bool:
    if a[0] != b[0] or a[0] == "text":
        return a == b
    if a[1:3] != b[1:3] or len(a[3]) != len(b[3]):
        return False
    for x, y in zip(a[3], b[3]):
        if isinstance(x, tuple) and isinstance(y, tuple):
            if len(x[1]) != len(y[1]) or any(abs(float(p) - float(q)) > 5e-7 for p, q in zip(x[1], y[1])):
                return False
        elif x != y:
            return False
    return True


def expected_ps(van: list[str], info: dict) -> list[str]:
    rn, rt, uv = info["redirect_temp"], info["gate_temp"], info["uv_register"]
    g = info["gate"]
    if g[0] == "rect":
        _, x0, y0, x1, y1 = g
        block = [f"ge r{rt}.xy, v{uv}.xyxx, l({_ff(x0)}, {_ff(y0)}, 0.000000, 0.000000)",
                 f"lt r{rt}.zw, v{uv}.xxxy, l(0.000000, 0.000000, {_ff(x1)}, {_ff(y1)})",
                 f"and r{rt}.xy, r{rt}.xyxx, r{rt}.zwzz",
                 f"and r{rt}.x, r{rt}.x, r{rt}.y"]
    else:
        block = [f"mul r{rt}.x, v{uv}.x, l({_ff(g[1])})", f"frc r{rt}.x, r{rt}.x", f"lt r{rt}.x, r{rt}.x, l(0.500000)"]
    t = info["tint"]
    block += [f"movc r{rt}.xyzw, r{rt}.xxxx, l({', '.join(_ff(v) for v in t)}), l(1.000000, 1.000000, 1.000000, 1.000000)",
              f"mul o0.{info['o0_written_mask']}, r{rn}.xyzw, r{rt}.xyzw"]
    out, in_code = [], False
    for line in van:
        s = line.strip()
        if re.fullmatch(r"(ps|vs|cs)_\d_\d", s):
            in_code = True
        if in_code and s.startswith("dcl_temps "):
            line = line.replace(s, f"dcl_temps {int(s.split()[1]) + 2}")
        elif in_code and s and not s.startswith(("dcl_", "//")):
            line = re.sub(r"\bo0\.", f"r{rn}.", line)
            if s.split()[0] in ("ret", "retc_nz", "retc_z"):
                ind = line[:len(line) - len(line.lstrip())]
                out += [ind + b for b in block]
        out.append(line)
    return out


def expected_cs(van: list[str], info: dict) -> list[str]:
    rb, rv, k, axis, comps = info["bit_temp"], info["value_temp"], info["shift"], info["axis"], info["zeroed_components"]
    out, in_code = [], False
    for line in van:
        s = line.strip()
        if re.fullmatch(r"(ps|vs|cs)_\d_\d", s):
            in_code = True
        if in_code and s.startswith("dcl_temps "):
            line = line.replace(s, f"dcl_temps {int(s.split()[1]) + 2}")
        elif in_code and s.startswith(f"store_uav_typed u{info['uav']}."):
            ind = line[:len(line) - len(line.lstrip())]
            u, coord, val = _split_ops(s.split(" ", 1)[1])
            base, sw = coord.rsplit(".", 1)
            cx, cy = base + "." + sw[0] * 4, base + "." + sw[1] * 4
            if axis == "x":
                pre = [f"ushr r{rb}.x, {cx}, l({k})"]
            elif axis == "y":
                pre = [f"ushr r{rb}.x, {cy}, l({k})"]
            else:
                pre = [f"iadd r{rb}.x, {cx}, {cy}", f"ushr r{rb}.x, r{rb}.xxxx, l({k})"]
            pre += [f"and r{rb}.x, r{rb}.xxxx, l(1)", f"mov r{rv}.xyzw, {val}",
                    f"movc r{rv}.{comps}, r{rb}.xxxx, l(0, 0, 0, 0), r{rv}.xyzw"]
            out += [ind + p for p in pre]
            line = f"{ind}store_uav_typed {u}, {coord}, r{rv}.xyzw"
        out.append(line)
    return out


def check_disasm(van_blob: bytes, new_blob: bytes, info: dict, kind: str, tag: str) -> list[str]:
    van = disasm(van_blob, tag + ".vanilla")
    got = disasm(new_blob, tag + ".patched")
    exp = expected_ps(van, info) if kind == "ps" else expected_cs(van, info)
    a = [_norm(x) for x in exp if x.strip()]
    b = [_norm(x) for x in got if x.strip()]
    errs = []
    if len(a) != len(b):
        errs.append(f"line count expected {len(a)} got {len(b)}")
    for i, (x, y) in enumerate(zip(a, b)):
        if not _same(x, y):
            errs.append(f"line {i}: expected {x!r} got {y!r}")
            if len(errs) > 5:
                break
    return errs


def check_registers(new_blob: bytes, info: dict, kind: str) -> list[str]:
    _, instrs = shexpatch.parse_shex(dxbc.chunk_map(new_blob)["SHEX"])
    ntemps = [i.toks[1] for i in instrs if i.opcode == shexpatch.OP_DCL_TEMPS][0]
    errs, o0_writes = [], 0
    for ins in instrs:
        if ins.is_decl:
            continue
        for op in shexpatch.operands(ins):
            if op.otype == shexpatch.T_TEMP and op.indices and op.indices[0] is not None and op.indices[0] >= ntemps:
                errs.append(f"temp r{op.indices[0]} >= dcl_temps {ntemps}")
            if op.otype == shexpatch.T_OUTPUT and op.indices == [0]:
                o0_writes += 1
                if ins.opcode != shexpatch.OP_MUL:
                    errs.append("o0 written outside the gate block")
    if kind == "ps" and o0_writes != info["returns_gated"]:
        errs.append(f"o0 writes {o0_writes} != gated returns {info['returns_gated']}")
    return errs


def check_chunks(van_blob: bytes, new_blob: bytes) -> list[str]:
    errs = []
    a, b = dxbc.parse(van_blob), dxbc.parse(new_blob)
    if [c.fourcc for c in a] != [c.fourcc for c in b]:
        errs.append("chunk list/order differs")
    for x, y in zip(a, b):
        if x.fourcc != "SHEX" and x.data != y.data:
            errs.append(f"chunk {x.fourcc} differs")
    if not dxbc.checksum_ok(new_blob):
        errs.append("checksum invalid")
    if dxbc.chunk_map(van_blob)["SHEX"] == dxbc.chunk_map(new_blob)["SHEX"]:
        errs.append("SHEX unchanged")
    return errs


# ---------------------------------------------------------------------------
# build


def patch_entry(spec: dict, van_blob: bytes) -> tuple[bytes, dict, str]:
    if spec["kind"] == "ps_cell":
        blob, info = shexpatch.ps_output_gate(van_blob, ("rect", *spec["rect"]), tuple(spec["tint"]))
        return blob, info, "ps"
    if spec["kind"] == "ps_stripes":
        blob, info = shexpatch.ps_output_gate(van_blob, tuple(spec["gate"]), tuple(spec["tint"]))
        return blob, info, "ps"
    if spec["kind"] == "cs_stripes":
        blob, info = shexpatch.cs_store_gate(van_blob, spec["uav"], spec["axis"], spec["shift"], spec["comps"])
        return blob, info, "cs"
    raise ValueError(spec["kind"])


def build_archive(name: str, van, d3d) -> dict:
    import shaderbnd
    spec = CENSUS[name]
    by_name = van.entry_by_name()
    reps, entries, ok = {}, [], True
    bdir = WORK / name
    bdir.mkdir(parents=True, exist_ok=True)
    for e in spec["entries"]:
        vb = by_name[e["entry"]].data
        rec = {"entry": e["entry"], "label": e["label"]}
        try:
            nb, info, kind = patch_entry(e, vb)
        except ValueError as ex:
            rec["error"] = str(ex); entries.append(rec); ok = False; continue
        tag = e["entry"].rsplit(".", 1)[0]
        (bdir / e["entry"]).write_bytes(nb)
        gates = {
            "chunks": check_chunks(vb, nb),
            "disasm": check_disasm(vb, nb, info, kind, f"{name}.{tag}"),
            "registers": check_registers(nb, info, kind),
        }
        if d3d is not None:
            hv, hn = d3d.create(vb), d3d.create(nb)
            gates["d3d11"] = [] if hv == 0 and hn == 0 else [f"Create*Shader vanilla=0x{hv:08x} patched=0x{hn:08x}"]
        rec.update({"patch": info, "gates": gates, "vanilla_sha256": sha256(vb), "patched_sha256": sha256(nb),
                    "shex_dwords": [len(dxbc.chunk_map(vb)["SHEX"]) // 4, len(dxbc.chunk_map(nb)["SHEX"]) // 4]})
        if any(gates.values()):
            ok = False
        else:
            reps[e["entry"]] = nb
        entries.append(rec)
    report = {"archive_name": name, "title": spec["title"], "entries": entries}
    if not ok:
        report["gate_ok"] = False
        report["note"] = "a blob failed a gate; archive not written"
        return report

    dst = INSTALL / name / ARCHIVE_REL
    for stale in (INSTALL / name).rglob("*"):
        if stale.is_file() and stale != dst:
            stale.unlink()
    bnd = shaderbnd.write(van, dst, reps)
    back = shaderbnd.read_archive(dst)
    A, B = shaderbnd.parse_bnd3(van.bnd), shaderbnd.parse_bnd3(back.bnd)
    short = lambda n: n.rsplit("\\", 1)[-1]  # BND3 stores full source paths; soulstruct names are basenames
    full_reps = {e.name for e in A.entries if short(e.name) in reps}
    diffs = shaderbnd.semantic_diff(van.bnd, back.bnd, full_reps)
    changed = sorted(short(eb.name) for ea, eb in zip(A.entries, B.entries) if ea.data != eb.data)
    payload_bad = [eb.name for eb in B.entries if eb.data != reps.get(short(eb.name), by_name[short(eb.name)].data)]
    order_same = [(e.name, e.entry_id, e.flags) for e in A.entries] == [(e.name, e.entry_id, e.flags) for e in B.entries]
    files = sorted(str(p.relative_to(INSTALL / name)).replace("\\", "/") for p in (INSTALL / name).rglob("*") if p.is_file())
    bnd0, dcx0 = shaderbnd.build(van, {})   # writer exactness on this archive (BND3 flags 0x74, Names1)
    report.update({
        "repack0_bnd_identical": bnd0 == van.bnd,
        "repack0_dcx_identical": dcx0 == van.dcx_raw,
        "archive": str(dst.relative_to(ROOT)).replace("\\", "/"),
        "sha256": sha256(dst.read_bytes()),
        "bytes": dst.stat().st_size,
        "install_folder_files": files,
        "entries_total": len(B.entries),
        "entry_names_ids_flags_order_identical": order_same,
        "changed_entries": changed,
        "changed_entries_equal_intended": changed == sorted(reps),
        "semantic_diffs_vs_vanilla": diffs,
        "payload_mismatches": payload_bad,
        "readback_bnd_equals_written": back.bnd == bnd,
        "dcx_type_level_same": back.dcx_type == van.dcx_type and back.level == van.level,
        "dcx_header_layout_same": back.dcx_header[:0x1C] == van.dcx_header[:0x1C] and back.dcx_header[0x24:] == van.dcx_header[0x24:],
        "dcx": shaderbnd.dcx_info(back.dcx_raw),
    })
    report["gate_ok"] = (order_same and not diffs and not payload_bad and back.bnd == bnd and changed == sorted(reps)
                         and report["repack0_bnd_identical"] and report["repack0_dcx_identical"]
                         and report["dcx_type_level_same"] and report["dcx_header_layout_same"]
                         and files == [ARCHIVE_REL])
    return report


# ---------------------------------------------------------------------------
# plan


def _pct(r):
    x0, y0, x1, y1 = r
    W, H = CAPTURE_RES
    return f"x {x0:.2f}-{x1:.2f} ({int(x0 * W)}-{int(x1 * W)} px), y {y0:.2f}-{y1:.2f} ({int(y0 * H)}-{int(y1 * H)} px)"


def write_plan(reports: dict, ini: dict) -> Path:
    L = []
    L.append("DSRLE filter-pass census: capture plan (generated by shaders/tools/make_filter_census.py)")
    L.append("")
    L.append("Game config read (read only) from %LOCALAPPDATA%\\FromSoftware\\NBGI\\DarkSouls\\DarkSouls.ini:")
    for k, v in ini.items():
        L.append(f"  {k} = {v}")
    L.append("")
    L.append("Each archive needs one game restart (shaders are read at startup). Install folder =")
    L.append("dist\\_shader_install\\fcensus_<n>\\shader\\FRPG_Filter_DX11.shaderbnd.dcx (nothing else inside).")
    L.append("Only the listed entries differ from vanilla; every pass keeps its vanilla result outside its gate.")
    gx0, gy0 = cell_rect(0, 0)[:2]
    gx1, gy1 = cell_rect(3, 1)[2:]
    L.append(f"All cells sit inside x {gx0:.2f}-{gx1:.2f} / y {gy0:.2f}-{gy1:.2f} of the screen (AO stripes are full-frame),")
    L.append("right of the player character and clear of the HUD masks and of the dsr.psm1 state probes")
    L.append("(HP/stamina bars, offline dialog, main-menu row).")
    L.append("")
    L.append("Recommended bonfires: firelink, parish, anor_interior, darkroot_garden")
    L.append("  (two bright outdoor views with sky and bloom, one interior with strong AO, one foliage view).")
    L.append("")
    L.append("Suggested commands (vanilla reference first, same session so exposure/flicker conditions match):")
    L.append("  .\\tools\\shader_session.ps1 -Run fcensus -Variants vanilla,dist\\_shader_install\\fcensus_1,"
             "dist\\_shader_install\\fcensus_2 -Only firelink,parish,anor_interior,darkroot_garden")
    L.append("  optional: .\\tools\\shader_session.ps1 -Run fcensus -Variants dist\\_shader_install\\fcensus_3 "
             "-Only firelink,parish,anor_interior,darkroot_garden")
    L.append("  -> captures\\fcensus\\<variant>\\<variant>\\<bonfire>.png")
    L.append("Decode:")
    L.append("  .venv\\Scripts\\python.exe shaders\\tools\\filter_census_decode.py captures\\fcensus [--json OUT]")
    L.append("  (or --census DIR --vanilla DIR --archive fcensus_N for explicit folders; without a vanilla capture the")
    L.append("   full cells fall back to the self-referenced edge test, partial cells are not decided, stripes need one)")
    L.append("  Vanilla reference without the extra restart: --vanilla captures\\baseline_vanilla\\vanilla (all four")
    L.append("  bonfires). Cross-run vanilla-vs-vanilla null tests (8 pairs over firelink, parish, anor_interior,")
    L.append("  darkroot_garden) give no ACTIVE or weak cell in fcensus_1/fcensus_3 and no stripe hit (largest coherent")
    L.append("  stripe SNR 3.3 against the threshold 8).")
    L.append("  Self-test of the decoder: filter_census_decode.py --selftest <vanilla.png> [<same view, other run>.png]")
    L.append("")
    L.append("Scope of every negative: one capture run sees only the passes selected by the graphics settings above")
    L.append("(Antialiasing, AmbientOcclusion, ...). 'not seen' means 'not selected under these settings, or this entry")
    L.append("name is not the one the engine binds', not 'never used'. Mapping the Antialiasing values to passes needs a")
    L.append("second run with another Antialiasing value.")
    L.append("")
    for name, spec in CENSUS.items():
        rep = reports.get(name, {})
        L.append("=" * 100)
        L.append(f"{name}: {spec['title']}   [{'built, gates OK' if rep.get('gate_ok') else 'NOT BUILT / gate failed'}]")
        if rep.get("sha256"):
            L.append(f"  {rep['archive']}  sha256 {rep['sha256']}")
        if spec["decode"] == "cells":
            L.append("  Cells (vanilla x tint inside the cell, vanilla outside; tint multiplies the pass output o0):")
            for e in spec["entries"]:
                L.append(f"    {e['label']:<16} cell c{e['cell'][0]}r{e['cell'][1]}  {_pct(e['rect'])}  tint {e['tint_name']} {tuple(e['tint'])}")
                if e.get("note"):
                    L.append(f"    {'':<16} {e['note']}")
            L.append("  Expected signature per outcome:")
            if name == "fcensus_1":
                L.append("    - exactly one top-row cell (or HDR_Menu) shows its colour cast -> that pass is the tonemap that")
                L.append("      writes the frame. Cast strength: full x0.3 on the suppressed channels when the pass output is")
                L.append("      the displayed colour; weaker (still in the tint's direction) if a non-linear step follows")
                L.append("      (e.g. an sRGB render target).")
                L.append("    - bottom-row FxAA2 / FxAA2_High cell with a full cast -> that FXAA entry runs. The two blobs are")
                L.append("      byte-identical, so only the census (separate cells per entry name) tells which name is bound.")
                L.append("    - ResolveTAA cell: the pass writes HDR before the tonemap, so its cast arrives compressed (about")
                L.append("      0.15-0.5 of x0.3 in log terms, cos ~ 1). The decoder lowers its threshold accordingly (atten")
                L.append(f"      {ATTEN_PRE_TONEMAP}); a direction-consistent cast below that is reported 'weak' -> 'RUNS (attenuated)'.")
                L.append("      The cast does not compound through the history (per-channel 3x3 clamp); slight smear if the")
                L.append("      camera moves.")
                L.append("    - a cell with no cast -> that pass is not selected under these settings at these bonfires")
                L.append("      (HDR_Menu is expected only behind menus). Two cells in one row -> both passes write the frame.")
                L.append("    - a cast that bleeds out of its cell -> the pass output is resampled later (lower-res buffer).")
            else:
                L.append("    - Bloom / Sfx_Glow_Blur cells: the two entries are byte-identical in vanilla; a gain-coloured halo")
                L.append("      (red / green) inside a cell around bright sky or fire shows which entry name carries the")
                L.append("      bloom blur. No cast in both -> neither entry is used for the bloom under these settings (the")
                L.append("      bloom effect itself may still come from another pass).")
                L.append("    - LightShaft cell: blue shafts inside the cell where shafts are visible; often absent by design.")
                L.append("    - SAO_Combine cell much darker -> AO is applied by SAO_Combine as frame x alpha; brighter ->")
                L.append("      inverted blend; unchanged -> SAO_Combine does not apply the AO that is used.")
                L.append("    - partial cells only count pixels whose channels all move the way the tint moves them, so")
                L.append("      additive changes (mist, smoke, flames) do not register; the cast must also stop at the cell edge.")
        else:
            per = AO_PATHS["compute_sao"]["ao_period_px"]
            L.append("  Stripes (AO value forced to 0 = fully occluded on half of each period):")
            for e in spec["entries"]:
                p = AO_PATHS[e["path"]]
                L.append(f"    {e['label']:<34} -> {e['path']:<13} {p['orientation']:<10} stripes, period {p['period']}")
            L.append("  Expected signature per outcome:")
            L.append(f"    - vertical stripes, period {AO_PATHS['pixel_sao']['screen_period_px']:.0f} px -> pixel SAO (FRPG_Fil_SAO_*) runs")
            L.append(f"    - horizontal stripes -> compute SAO (FRPG_Compute_SAO_*); period/{per} = screen px per AO texel")
            L.append("    - diagonal stripes (x+y) -> compute SSAO (FRPG_Compute_SSAO_*, MiniEngine-style chain). All four")
            L.append("      BlurUpsample variants are striped and each reads the previous level's output, so the stripes")
            L.append(f"      of every hierarchy level superpose: several periods {per} x s (s = 1, 2, 4, 8, 16) can show at")
            L.append("      once. The decoder lists them (levels_detected); the smallest s is the scale of the pass that")
            L.append("      writes the final AO, the largest the coarsest striped level. Outside the stripes of all levels")
            L.append("      the AO is vanilla; for a single-level path that is simply outside its stripes.")
            L.append(f"    - vertical stripes with period {per}*s px (not 240) -> a compute path writing a transposed buffer;")
            L.append("      the decoder reports this as pixel_sao 'stripes, unexpected period'")
            L.append("    - no stripes -> AO path not running under these settings (or AO not applied in these views);")
            L.append("      several orientations -> several paths contribute. AO only scales the ambient term, so stripes")
            L.append("      are partial darkening; the decoder detects down to ~1% modulation when it is phase-coherent")
            L.append("      across 8 frame bands (SNR >= 8).")
        L.append("")
    p = INSTALL / "fcensus_plan.txt"
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    return p


def read_ini() -> dict:
    path = Path(os.environ.get("LOCALAPPDATA", "")) / "FromSoftware" / "NBGI" / "DarkSouls" / "DarkSouls.ini"
    want = {"DisplaySetting": ["WindowMode"], "DisplaySettingWindow": ["Width", "Height"],
            "DisplaySettingFilter": ["Blur", "Antialiasing", "DOF", "AmbientOcclusion", "ForceDisableAA"]}
    out, sec = {}, None
    try:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                sec = line[1:-1]
            elif "=" in line and sec in want:
                k, v = (x.strip() for x in line.split("=", 1))
                if k in want[sec]:
                    out[f"{sec}.{k}"] = v
    except OSError as ex:
        out["error"] = str(ex)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only", default=",".join(CENSUS), help="comma list of archives (default all)")
    p.add_argument("--no-d3d", action="store_true", help="skip the D3D11 WARP Create*Shader gate")
    a = p.parse_args(argv)
    names = [n for n in a.only.split(",") if n]
    for n in names:
        if n not in CENSUS:
            raise SystemExit(f"unknown archive {n}")
    OUT.mkdir(parents=True, exist_ok=True)
    van = load_vanilla()
    d3d = None
    if not a.no_d3d:
        import d3d11check
        d3d = d3d11check.Device()
    reports = {}
    try:
        for n in names:
            rep = build_archive(n, van, d3d)
            rep["d3d11_device"] = None if d3d is None else {"driver": d3d.driver, "feature_level": hex(d3d.feature_level)}
            (OUT / f"{n}.report.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")
            reports[n] = rep
    finally:
        if d3d is not None:
            d3d.close()
    for n in CENSUS:
        if n not in reports and (OUT / f"{n}.report.json").exists():
            reports[n] = json.loads((OUT / f"{n}.report.json").read_text(encoding="utf-8"))
    ini = read_ini()
    (OUT / "layout.json").write_text(json.dumps({**layout(), "ini": ini}, indent=1), encoding="utf-8")
    plan = write_plan(reports, ini)
    rc = 0
    for n in names:
        r = reports[n]
        bad = {e["entry"]: {k: v for k, v in e.get("gates", {}).items() if v} or e.get("error") for e in r["entries"]
               if e.get("error") or any(e.get("gates", {}).values())}
        print(f"{n}: gate_ok={r.get('gate_ok')} entries_changed={len(r.get('changed_entries', []))} "
              f"sha256={r.get('sha256', '-')[:16]} {r.get('archive', '')}")
        if bad:
            print("   failures:", json.dumps(bad, indent=1)[:2000])
        rc |= 0 if r.get("gate_ok") else 1
    print(f"plan: {plan.relative_to(ROOT)}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
