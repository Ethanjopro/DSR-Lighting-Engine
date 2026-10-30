"""Build installable test variants of the FlverPBL pixel-shader archive.

  python shaders/tools/make_variant.py repack0    # unchanged unpack+repack (archive I/O gate)
  python shaders/tools/make_variant.py rebuild0   # swap in rebuilt blobs whose SHEX/ISGN/OSGN match vanilla
  python shaders/tools/make_variant.py gentint    # every entry -> constant-colour stub by header generation
  python shaders/tools/make_variant.py all

Inputs (read only): vanilla/shader/FRPG_FlverPBL_fpo_DX11.shaderbnd.dcx (sha256-checked against
vanilla/manifest.sha256), vanilla_src/rebuild/mapping.json, vanilla_src/rebuild/out/**.fpo.
Outputs: dist/_shader/<variant>/shader/FRPG_FlverPBL_fpo_DX11.shaderbnd.dcx + report.json,
intermediates under dist/_shader/_work/. Deterministic: sorted iteration, fixed fxc arguments,
content-addressed stub names, no debug info.

The stub HLSL is generated purely from the vanilla blob's ISGN/OSGN signature tables; no recovered
source is used or emitted.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dxbc  # noqa: E402
import shaderbnd  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_REL = "shader/FRPG_FlverPBL_fpo_DX11.shaderbnd.dcx"
VANILLA = ROOT / "vanilla" / ARCHIVE_REL
MANIFEST = ROOT / "vanilla" / "manifest.sha256"
MAPPING = ROOT / "vanilla_src" / "rebuild" / "mapping.json"
REBUILT = ROOT / "vanilla_src" / "rebuild" / "out"
DIST = ROOT / "dist" / "_shader"
WORK = DIST / "_work"
FXC = os.environ.get("FXC", r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\fxc.exe")

GEN_COLOURS = {  # header_gen prefix -> RGBA
    "421b": (1, 0, 0, 1), "b56c": (0, 1, 0, 1), "5519": (0, 0, 1, 1),
    "3e9a": (1, 1, 0, 1), "c634": (0, 1, 1, 1), "0f37": (1, 1, 1, 1),
}
MAGENTA = (1, 0, 1, 1)


# ---------------------------------------------------------------------------
# shared


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_vanilla() -> shaderbnd.Archive:
    want = None
    for line in MANIFEST.read_text().splitlines():
        h, _, rel = line.partition("  ")
        if rel.strip() == ARCHIVE_REL:
            want = h
    got = sha256(VANILLA)
    if want is None or got != want:
        raise SystemExit(f"vanilla archive hash mismatch: manifest={want} file={got}")
    return shaderbnd.read_archive(VANILLA)


def load_mapping() -> dict:
    return json.loads(MAPPING.read_text())


def out_paths(variant: str) -> tuple[Path, Path]:
    d = DIST / variant
    return d / ARCHIVE_REL, d / "report.json"


def gen_label(gen: str | None) -> str:
    g = (gen or "none")
    return g[:4] if g[:4] in GEN_COLOURS else "none"


def finish(variant: str, van: shaderbnd.Archive, reps: dict[str, bytes], report: dict) -> dict:
    """Write the archive, read it back from disk and gate it."""
    dst, rep_path = out_paths(variant)
    bnd = shaderbnd.write(van, dst, reps)
    back = shaderbnd.read_archive(dst)
    diffs = shaderbnd.semantic_diff(van.bnd, back.bnd, set(reps))
    parsed = shaderbnd.parse_bnd3(back.bnd)
    payload_bad = [e.name for e in parsed.entries
                   if e.data != reps.get(e.name, van.entry_by_name()[e.name].data)]
    report.update({
        "archive": str(dst.relative_to(ROOT)),
        "sha256": sha256(dst),
        "entries": len(parsed.entries),
        "replaced_entries": len(reps),
        "replaced_payload_differs_from_vanilla": sum(1 for n, d in reps.items() if d != van.entry_by_name()[n].data),
        "readback_bnd_equals_written": back.bnd == bnd,
        "semantic_diffs_vs_vanilla": diffs[:50],
        "payload_mismatches": payload_bad[:50],
        "dcx": shaderbnd.dcx_info(back.dcx_raw),
        "dcx_bytes_identical_to_vanilla": back.dcx_raw == van.dcx_raw,
        "bnd_bytes_identical_to_vanilla": back.bnd == van.bnd,
    })
    ok = not diffs and not payload_bad and back.bnd == bnd and back.dcx_type == van.dcx_type and back.level == van.level
    report["gate_ok"] = ok
    rep_path.write_text(json.dumps(report, indent=1, sort_keys=True))
    return report


# ---------------------------------------------------------------------------
# repack0


def cmd_repack0(_a) -> dict:
    van = load_vanilla()
    rep = finish("repack0", van, {}, {"variant": "repack0"})
    rep["vanilla_dcx_header_hex"] = van.dcx_header.hex()
    out_paths("repack0")[1].write_text(json.dumps(rep, indent=1, sort_keys=True))
    return rep


# ---------------------------------------------------------------------------
# rebuild0


def cmd_rebuild0(_a) -> dict:
    van = load_vanilla()
    mapping = load_mapping()
    cands: dict[str, list[Path]] = collections.defaultdict(list)
    for p in sorted(REBUILT.rglob("*.fpo"), key=lambda p: p.relative_to(REBUILT).as_posix()):
        cands[p.name].append(p)

    reps: dict[str, bytes] = {}
    swapped = collections.Counter(); by_gen = collections.Counter(); by_dir = collections.Counter()
    rejected = collections.Counter(); notes = []
    rebuilt_identical = 0; diff_chunks = collections.Counter()
    multi_differ = []
    for e in van.entries:
        stem = e.name.rsplit(".", 1)[0]
        vb = e.data
        vc = dxbc.chunk_map(vb)
        ok_paths = []
        for p in cands.get(e.name, []):
            rb = p.read_bytes()
            try:
                rc = dxbc.chunk_map(rb)
            except ValueError as ex:
                rejected["bad_container"] += 1; notes.append(f"{p}: {ex}"); continue
            if not dxbc.checksum_ok(rb):
                rejected["bad_checksum"] += 1; notes.append(f"{p}: checksum invalid"); continue
            code_v = "SHEX" if "SHEX" in vc else "SHDR"
            fails = [cc for cc in (code_v, "ISGN", "OSGN") if vc.get(cc) is None or rc.get(cc) != vc.get(cc)]
            if fails:
                rejected["mismatch_" + "+".join(fails)] += 1; continue
            ok_paths.append((p, rb, rc))
        if not cands.get(e.name):
            rejected["no_rebuilt_blob"] += 1
        if not ok_paths:
            continue
        p, rb, rc = ok_paths[0]
        if len({x[1] for x in ok_paths}) > 1:
            multi_differ.append([str(x[0].relative_to(ROOT)) for x in ok_paths])
        reps[e.name] = rb
        rec = mapping.get(stem, {})
        swapped[rec.get("family", "?")] += 1
        by_gen[rec.get("header_gen", "?")] += 1
        by_dir[p.parent.relative_to(REBUILT).as_posix()] += 1
        if rb == vb:
            rebuilt_identical += 1
        for cc in sorted(set(vc) | set(rc)):
            if vc.get(cc) != rc.get(cc):
                diff_chunks[cc] += 1
    rep = {
        "variant": "rebuild0",
        "rebuilt_files": sum(len(v) for v in cands.values()),
        "rebuilt_unique_names": len(cands),
        "swapped_total": len(reps),
        "swapped_by_family": dict(sorted(swapped.items())),
        "swapped_by_header_gen": dict(sorted(by_gen.items())),
        "swapped_by_out_dir": dict(sorted(by_dir.items())),
        "swapped_byte_identical_to_vanilla": rebuilt_identical,
        "swapped_chunks_differing_from_vanilla": dict(sorted(diff_chunks.items())),
        "rejected_candidates": dict(sorted(rejected.items())),
        "names_with_multiple_matching_candidates_that_differ": multi_differ[:20],
        "notes": notes[:50],
    }
    return finish("rebuild0", van, reps, rep)


# ---------------------------------------------------------------------------
# gentint


COMP_TYPE = {1: "uint", 2: "int", 3: "float"}
SWZ = "xyzw"


def _low_bit(m: int) -> int:
    return (m & -m).bit_length() - 1


def _contiguous(m: int) -> bool:
    lo = m >> _low_bit(m)
    return m != 0 and (lo & (lo + 1)) == 0


def _type(ct: int, width: int) -> str:
    base = COMP_TYPE[ct]
    return base if width == 1 else f"{base}{width}"


def stub_hlsl(isgn: list[dxbc.SigElem], osgn: list[dxbc.SigElem], colour, label: str) -> str:
    """Constant-colour pixel shader with the given interface.

    Inputs: declared in ISGN order; exactly the components in each element's 'always read' mask are
    consumed through Z(u) = (u*(u+1)) & 1, which is 0 for every 32-bit u (a product of two
    consecutive integers is even, also mod 2^32) but is not folded by fxc. The OR of all Z terms
    feeds 'if (k != 0) discard;', which therefore never fires, so the written outputs are exact
    constants. Outputs: declared in OSGN order; components in the 'never written' mask are left
    unwritten. SV_Target0 = generation colour, everything else 0.
    """
    lines = [f"// generated interface stub ({label}); colour {colour}", "struct PSIn {"]
    reads = []
    for n, e in enumerate(isgn):
        if not _contiguous(e.mask):
            raise ValueError(f"non-contiguous input mask {e.mask:#x}")
        width = bin(e.mask).count("1")
        interp = "nointerpolation " if e.comp_type != 3 and e.sysval == 0 else ""
        lines.append(f"  {interp}{_type(e.comp_type, width)} i{n} : {e.name}{e.index};")
        lo = _low_bit(e.mask)
        for b in range(4):
            if e.rw >> b & 1:
                reads.append(f"i.i{n}.{SWZ[b - lo]}")
    lines.append("};")
    lines.append("uint Z(uint u) { return (u * (u + 1u)) & 1u; }")
    params = ["PSIn i"]
    body = ["  uint k = 0u;"]
    body += [f"  k |= Z(asuint({r}));" for r in reads]
    if reads:
        body.append("  if (k != 0u) discard;")
    for n, e in enumerate(osgn):
        if not _contiguous(e.mask) or _low_bit(e.mask) != 0:
            raise ValueError(f"unsupported output mask {e.mask:#x}")
        width = bin(e.mask).count("1")
        params.append(f"out {_type(e.comp_type, width)} o{n} : {e.name}{e.index if e.name == 'SV_Target' else ''}")
        written = [SWZ[b] for b in range(width) if not (e.rw >> b & 1)]
        if not written:
            continue
        if e.name == "SV_Target" and e.index == 0 and e.comp_type == 3:
            vals = [float(colour[SWZ.index(c)]) for c in written]
        else:
            vals = [0] * len(written)
        lit = ", ".join(repr(v) if isinstance(v, float) else "0" for v in vals)
        tgt = f"o{n}" if len(written) == width else f"o{n}.{''.join(written)}"
        cast = _type(e.comp_type, len(written))
        body.append(f"  {tgt} = {cast}({lit});")
    lines.append(f"void main({', '.join(params)})")
    lines.append("{")
    lines += body
    lines.append("}")
    return "\n".join(lines) + "\n"


def compile_stub(src: Path, out: Path, target: str) -> tuple[int, str]:
    r = subprocess.run([FXC, "/nologo", "/T", target, "/E", "main", "/Fo", str(out), str(src)],
                       capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def cmd_gentint(a) -> dict:
    van = load_vanilla()
    mapping = load_mapping()
    work = WORK / "gentint"
    work.mkdir(parents=True, exist_ok=True)

    plan = {}     # entry name -> (key, label)
    jobs = {}     # key -> (hlsl, target, vanilla ISGN, vanilla OSGN)
    gen_entries = collections.Counter()
    prep_fail = {}
    for e in van.entries:
        stem = e.name.rsplit(".", 1)[0]
        label = gen_label(mapping.get(stem, {}).get("header_gen"))
        gen_entries[label] += 1
        colour = GEN_COLOURS.get(label, MAGENTA)
        ch = dxbc.chunk_map(e.data)
        kind, maj, mnr = dxbc.shader_version(ch)
        target = f"{kind}_{maj}_{mnr}"
        try:
            src = stub_hlsl(dxbc.parse_signature(ch["ISGN"]), dxbc.parse_signature(ch["OSGN"]), colour, label)
        except (ValueError, KeyError) as ex:
            prep_fail[e.name] = str(ex)
            continue
        key = hashlib.sha1((target + "\n" + src).encode()).hexdigest()[:16]
        plan[e.name] = (key, label)
        jobs.setdefault(key, (src, target, ch["ISGN"], ch["OSGN"]))

    def run(key):
        src, target, isgn, osgn = jobs[key]
        hp, op = work / f"{key}.hlsl", work / f"{key}.fpo"
        hp.write_text(src, newline="\n")
        if op.exists():
            op.unlink()
        rc, log = compile_stub(hp, op, target)
        if rc != 0 or not op.exists():
            return key, None, f"fxc failed: {log[-400:]}"
        blob = op.read_bytes()
        ch = dxbc.chunk_map(blob)
        problems = []
        if ch.get("ISGN") != isgn:
            problems.append("ISGN differs")
        if ch.get("OSGN") != osgn:
            problems.append("OSGN differs")
        if not dxbc.checksum_ok(blob):
            problems.append("checksum invalid")
        if "%s_%d_%d" % dxbc.shader_version(ch) != target:
            problems.append("shader model differs")
        if any(c in ch for c in ("RDEF",)) and len(ch["RDEF"]) and b"$Globals" in ch["RDEF"]:
            problems.append("unexpected cbuffer")
        return key, blob, "; ".join(problems)

    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        results = {k: (b, p) for k, b, p in ex.map(run, sorted(jobs))}

    reps = {}
    stubbed = collections.Counter(); kept = {}
    for e in van.entries:
        if e.name in prep_fail:
            kept[e.name] = prep_fail[e.name]; continue
        key, label = plan[e.name]
        blob, prob = results[key]
        if blob is None or prob:
            kept[e.name] = prob or "compile failed"
            continue
        reps[e.name] = blob
        stubbed[label] += 1
    bad_keys = {k: p for k, (b, p) in results.items() if b is None or p}
    rep = {
        "variant": "gentint",
        "colours": {**{k: v for k, v in GEN_COLOURS.items()}, "none": MAGENTA},
        "entries_by_generation": dict(sorted(gen_entries.items())),
        "stubbed_by_generation": dict(sorted(stubbed.items())),
        "kept_vanilla_interface_mismatch": len(kept),
        "kept_vanilla": dict(sorted(kept.items())[:100]),
        "unique_stubs_compiled": len(jobs),
        "stubs_failing_gate": bad_keys,
        "isgn_osgn_byte_identical_all_stubs": not bad_keys and not prep_fail,
        "entries_without_written_SV_Target0_no_tint": sorted(
            n for n in reps if all(el.rw == el.mask for el in dxbc.parse_signature(dxbc.chunk_map(van.entry_by_name()[n].data)["OSGN"])
                                   if el.name == "SV_Target" and el.index == 0)),
        "work_dir": str(work.relative_to(ROOT)),
    }
    return finish("gentint", van, reps, rep)


# ---------------------------------------------------------------------------


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("variant", choices=["repack0", "rebuild0", "gentint", "all"])
    p.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 8)
    a = p.parse_args(argv)
    todo = ["repack0", "rebuild0", "gentint"] if a.variant == "all" else [a.variant]
    rc = 0
    for v in todo:
        rep = {"repack0": cmd_repack0, "rebuild0": cmd_rebuild0, "gentint": cmd_gentint}[v](a)
        summary = {k: v2 for k, v2 in rep.items() if not isinstance(v2, (dict, list)) or k.startswith(("swapped_by", "stubbed_by", "entries_by", "rejected"))}
        print(json.dumps({v: summary}, indent=1, sort_keys=True))
        rc |= 0 if rep.get("gate_ok") else 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
