"""Build, gate and pack rebuilt FRPG_Filter_DX11 (post-process) shader blobs.

    .venv\\Scripts\\python.exe shaders\\tools\\build_filter.py [--tag rebuild0] [--pack fil_rebuild0]
            [--manifest M.json] [--names A.fpo B.vpo ...] [--extra "/D X /I DIR"]
            [--replace exact|all] [--allow-rdef-change] [--no-cover-duplicates] [--fail-on-inexact]

Inputs (read only)
  * a manifest JSON (default vanilla_src/rebuild/filter/manifest.json, git-ignored) naming, per archive
    entry basename, the HLSL source (relative to the manifest's "src_root"), entry point, profile and
    defines:  {"src_root": "...", "defines": {"ps": [...], "vs": [...]},
               "blobs": {"X.fpo": {"src": "dir/X.fx", "entry": "FragmentMain", "profile": "ps_5_0",
                                   "defines": [...optional extra...]}, ...}}
  * the vanilla archive vanilla/shader/FRPG_Filter_DX11.shaderbnd.dcx, sha256-checked against
    vanilla/manifest.sha256. Vanilla blobs are taken from the archive itself, keyed by entry basename.

Build:  fxc (fxcasm.fxc_path()) per manifest record into dist/_shader/filter_<tag>/blobs/<name>.

Gate, per blob, against the vanilla archive entry:
  SHEX / ISGN / OSGN / STAT byte equality, RDEF equality up to the compiler creator string, and
  "normalised" whole-blob equality: the rebuilt container re-emitted with the vanilla creator string
  and a recomputed DXBC checksum, compared byte for byte with the vanilla blob. exact := SHEX, ISGN and
  OSGN identical (the project-wide definition). For inexact blobs: normalised instruction-stream
  equality (temp registers abstracted) and the first diverging lines.

Pack (--pack NAME, NAME must start with "fil_"):
  dist/_shader_install/<NAME>/shader/FRPG_Filter_DX11.shaderbnd.dcx = the vanilla archive with the
  raw fxc builds swapped in (--replace exact: only exact blobs; all: every compiled blob, for feature
  variants). Interface gate: every blob to be swapped in must keep the vanilla interface, i.e. ISGN and
  OSGN identical, RDEF identical up to the creator string, and STAT identical when SHEX is identical.
  --replace all lets SHEX (the code) differ, never the interface. If any blob fails, nothing is packed
  and a stale <NAME> folder is removed. RDEF is compared as raw bytes, so it also covers the compile
  flags and the per-variable "used" flags: a feature variant that reads a constant the vanilla shader
  declares but never reads differs there with an identical layout. --allow-rdef-change waives the RDEF
  test only (recorded in the report); ISGN/OSGN are never waived.
  With duplicate coverage (default) an exact build also replaces every other archive entry whose
  vanilla bytes equal the vanilla blob it was gated against. The archive is read back and
  checked: entry names/order equal vanilla, the entries differing from vanilla are exactly the
  replaced ones whose bytes differ, payloads equal what was written, BND3 header and DCX type/level
  unchanged. Control: the same replacement set using the creator-normalised blobs is packed in memory;
  when every normalised blob equals vanilla this must reproduce the shaderbnd.py repack of the
  unmodified archive (and the vanilla file) byte for byte.
  The install folder holds only the archive; reports go to dist/_shader/filter_<tag>/report.json.

Original DSRLE tooling: no game source is read or written beyond passing manifest paths to fxc.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import struct
import subprocess
import sys
import difflib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import dxbc  # noqa: E402
import fxcasm  # noqa: E402
import shaderbnd  # noqa: E402

ARCHIVE_REL = "shader/FRPG_Filter_DX11.shaderbnd.dcx"
VANILLA = ROOT / "vanilla" / ARCHIVE_REL
MANIFEST_SHA = ROOT / "vanilla" / "manifest.sha256"
DEFAULT_MANIFEST = ROOT / "vanilla_src" / "rebuild" / "filter" / "manifest.json"
DIST = ROOT / "dist" / "_shader"
INSTALL = ROOT / "dist" / "_shader_install"
CHUNKS = ("SHEX", "ISGN", "OSGN", "STAT")
CREATOR_MARK = b"Microsoft (R) HLSL Shader Compiler"


# ---------------------------------------------------------------------------
# helpers
def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def load_vanilla() -> shaderbnd.Archive:
    want = None
    for line in MANIFEST_SHA.read_text().splitlines():
        h, _, rel = line.partition("  ")
        if rel.strip() == ARCHIVE_REL:
            want = h
    got = sha256(VANILLA.read_bytes())
    if want is None or got != want:
        raise SystemExit(f"vanilla archive hash mismatch: manifest={want} file={got}")
    return shaderbnd.read_archive(VANILLA)


def basename(entry_name: str) -> str:
    return entry_name.replace("\\", "/").rsplit("/", 1)[-1]


def creator_split(rdef: bytes) -> tuple[bytes, bytes]:
    """(RDEF bytes before the creator string, creator string incl. its NUL padding)."""
    off = struct.unpack_from("<I", rdef, 0x18)[0]
    if rdef[off:off + len(CREATOR_MARK)] != CREATOR_MARK:
        i = rdef.find(CREATOR_MARK)
        off = i if i >= 0 else len(rdef)
    return rdef[:off], rdef[off:]


def creator_string(rdef: bytes) -> str:
    tail = creator_split(rdef)[1]
    return tail.split(b"\0", 1)[0].decode("latin-1")


def build_container(chunks: list[tuple[str, bytes]]) -> bytes:
    """Emit a DXBC container (version 1) with the given chunks in order and a valid checksum."""
    n = len(chunks)
    head = 0x20 + 4 * n
    offs, body, pos = [], bytearray(), head
    for cc, data in chunks:
        offs.append(pos)
        part = cc.encode("latin-1") + struct.pack("<I", len(data)) + data
        body += part
        pos += len(part)
    blob = bytearray(b"DXBC" + b"\0" * 16 + struct.pack("<III", 1, pos, n) + struct.pack(f"<{n}I", *offs)) + body
    blob[4:20] = dxbc.dxbc_checksum(bytes(blob))
    return bytes(blob)


def normalise_creator(blob: bytes, van: bytes) -> bytes:
    """blob re-emitted with van's RDEF creator string (everything else unchanged)."""
    vr = dxbc.chunk_map(van)["RDEF"]
    out = []
    for c in dxbc.parse(blob):
        data = c.data
        if c.fourcc == "RDEF":
            pre, _ = creator_split(data)
            data = pre + creator_split(vr)[1]
        out.append((c.fourcc, data))
    return build_container(out)


# ---------------------------------------------------------------------------
# build + gate
def compile_one(name: str, rec: dict, man: dict, src_root: Path, out_dir: Path, extra: list[str], fxc: str) -> dict:
    prof = rec.get("profile") or ("ps_5_0" if name.endswith(".fpo") else "vs_5_0")
    stage = prof.split("_", 1)[0]
    defs = list(man.get("defines", {}).get(stage, [])) + list(rec.get("defines", []))
    src = src_root / rec["src"]
    dst = out_dir / name
    if dst.exists():
        dst.unlink()
    cmd = [fxc, "/nologo", "/T", prof, "/E", rec["entry"]]
    for d in defs:
        cmd += ["/D", d]
    cmd += list(rec.get("fxc_args", [])) + extra + ["/Fo", str(dst), str(src)]
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    msgs = [ln.strip() for ln in (p.stdout + p.stderr).splitlines()
            if ("warning" in ln or "error" in ln) and "compilation object save succeeded" not in ln]
    ok = p.returncode == 0 and dst.exists()
    if not ok and not msgs:
        msgs = [ln.strip() for ln in (p.stdout + p.stderr).splitlines() if ln.strip()][-5:]
    return {"name": name, "ok": ok, "profile": prof, "entry": rec["entry"], "src": rec["src"],
            "defines": defs, "extra": extra, "messages": msgs[:20],
            "cmdline": " ".join(shlex.quote(c) for c in cmd[1:])}


def gate_one(r: dict, out_dir: Path, van: bytes, cache: Path) -> dict:
    blob = (out_dir / r["name"]).read_bytes()
    vc, rc = dxbc.chunk_map(van), dxbc.chunk_map(blob)
    g = {k: vc.get(k) == rc.get(k) for k in CHUNKS}
    g["RDEF_but_creator"] = creator_split(vc["RDEF"])[0] == creator_split(rc["RDEF"])[0]
    g["creator_vanilla"], g["creator_rebuilt"] = creator_string(vc["RDEF"]), creator_string(rc["RDEF"])
    g["checksum_ok"] = dxbc.checksum_ok(blob)
    norm = normalise_creator(blob, van)
    g["normalised_identical"] = norm == van
    g["exact"] = g["SHEX"] and g["ISGN"] and g["OSGN"]
    g["bytes"], g["sha256"] = len(blob), sha256(blob)
    g["vanilla_bytes"], g["vanilla_sha256"] = len(van), sha256(van)
    if not g["exact"]:
        vp = out_dir / ("_vanilla_" + r["name"])
        vp.write_bytes(van)
        sv = fxcasm.parse_asm(fxcasm.cached_disasm(str(vp), str(cache)))
        sr = fxcasm.parse_asm(fxcasm.cached_disasm(str(out_dir / r["name"]), str(cache)))
        a, b = fxcasm.normalized_stream(sv, "strict"), fxcasm.normalized_stream(sr, "strict")
        g["n_instr_vanilla"], g["n_instr_rebuilt"] = len(a), len(b)
        g["stream_equal_strict"] = a == b
        g["stream_equal_loose"] = fxcasm.normalized_stream(sv, "loose") == fxcasm.normalized_stream(sr, "loose")
        g["first_diff"] = [ln for ln in difflib.unified_diff(sv.instrs, sr.instrs, lineterm="", n=1)][:24]
        vp.unlink()
    return {**r, **g, "_norm": norm}


# ---------------------------------------------------------------------------
# pack
def interface_errors(r: dict, allow_rdef: bool = False) -> list[str]:
    """Why a gated blob may not replace its vanilla entry (empty = it keeps the vanilla interface)."""
    errs = []
    for k in ("ISGN", "OSGN"):
        if not r.get(k):
            errs.append(f"{k} differs from vanilla")
    if not r.get("RDEF_but_creator") and not allow_rdef:
        errs.append("RDEF differs from vanilla beyond the creator string (layout, bindings, used flags or compile flags)")
    if r.get("SHEX") and not r.get("STAT"):
        errs.append("STAT differs although SHEX is identical")
    if not r.get("checksum_ok"):
        errs.append("DXBC checksum invalid")
    return errs


def pack(name: str, van_ar: shaderbnd.Archive, results: list[dict], out_dir: Path, replace: str,
         cover_dups: bool, allow_rdef: bool = False) -> dict:
    if not name.startswith("fil_"):
        raise SystemExit("--pack name must start with 'fil_'")
    by_base = {}
    for e in van_ar.entries:
        by_base.setdefault(basename(e.name), []).append(e)
    if any(len(v) > 1 for v in by_base.values()):
        raise SystemExit("ambiguous entry basenames in the vanilla archive")
    # soulstruct Binder names (used by shaderbnd.build) are basenames here; the independent BND3
    # parser returns the full stored path. Everything below is keyed by basename.
    full = {basename(e.name): e.name for e in van_ar.entries}
    vbytes = {basename(e.name): bytes(e.data) for e in van_ar.entries}

    raw, norm, src_of = {}, {}, {}
    chosen = [r for r in results if r["ok"] and (replace == "all" or r["exact"])]
    refused = {r["name"]: e for r in chosen for e in [interface_errors(r, allow_rdef)] if e}
    inst = INSTALL / name
    if refused:
        removed = inst.exists()
        if removed:
            shutil.rmtree(inst)
        return {"ok": False, "interface_ok": False, "interface_refused": refused, "replace": replace,
                "allow_rdef_change": allow_rdef, "archive": None, "sha256": None, "replaced": 0,
                "entries_differing_from_vanilla": 0, "control_normalised_archive_equals_repack": None,
                "note": "not packed: a blob changes the vanilla interface"
                        + (f"; stale {inst.relative_to(ROOT)} removed" if removed else "")}
    for r in chosen:                      # explicit manifest records first
        raw[r["name"]] = (out_dir / r["name"]).read_bytes()
        norm[r["name"]] = r["_norm"]
        src_of[r["name"]] = r["name"]
    if cover_dups:                        # then byte-identical vanilla duplicates of exact builds
        for r in chosen:
            if not r["exact"]:
                continue
            for b in sorted(vbytes):
                if b not in raw and vbytes[b] == vbytes[r["name"]]:
                    raw[b], norm[b], src_of[b] = raw[r["name"]], r["_norm"], r["name"]
    reps = {full[b]: d for b, d in raw.items()}
    for b, d in raw.items():
        if not dxbc.checksum_ok(d):
            raise SystemExit(f"{b}: DXBC checksum invalid")

    if inst.exists():
        shutil.rmtree(inst)
    dst = inst / ARCHIVE_REL
    bnd = shaderbnd.write(van_ar, dst, reps)
    back = shaderbnd.read_archive(dst)
    parsed, vparsed = shaderbnd.parse_bnd3(back.bnd), shaderbnd.parse_bnd3(van_ar.bnd)
    differ = sorted(basename(e.name) for e, ve in zip(parsed.entries, vparsed.entries) if e.data != ve.data)
    expect_differ = sorted(b for b, d in raw.items() if d != vbytes[b])
    p = {
        "interface_ok": True, "interface_refused": {}, "replace": replace, "allow_rdef_change": allow_rdef,
        "interface_per_entry": {b: {k: bool(next(r for r in chosen if r["name"] == src_of[b]).get(k))
                                    for k in ("ISGN", "OSGN", "RDEF_but_creator", "STAT", "SHEX")}
                                for b in sorted(raw)},
        "archive": str(dst.relative_to(ROOT)).replace("\\", "/"),
        "sha256": sha256(dst.read_bytes()),
        "bytes": dst.stat().st_size,
        "entries": len(parsed.entries),
        "vanilla_entries": len(vparsed.entries),
        "names_equal_vanilla_in_order": [e.name for e in parsed.entries] == [e.name for e in vparsed.entries],
        "flags_ids_equal_vanilla": [(e.flags, e.entry_id) for e in parsed.entries]
                                   == [(e.flags, e.entry_id) for e in vparsed.entries],
        "replaced": len(reps),
        "replaced_entries": {b: src_of[b] for b in sorted(raw)},
        "entries_differing_from_vanilla": len(differ),
        "differing_equals_replaced_with_new_bytes": differ == expect_differ,
        "payload_mismatches": [e.name for e in parsed.entries
                               if e.data != raw.get(basename(e.name), vbytes[basename(e.name)])][:20],
        "semantic_diffs_vs_vanilla": shaderbnd.semantic_diff(
            van_ar.bnd, back.bnd, {e.name for e in vparsed.entries if basename(e.name) in raw})[:20],
        "readback_bnd_equals_written": back.bnd == bnd,
        "dcx_type_level_same": back.dcx_type == van_ar.dcx_type and back.level == van_ar.level,
        "dcx_header_same_but_sizes": back.dcx_header[:0x1C] == van_ar.dcx_header[:0x1C]
                                     and back.dcx_header[0x24:] == van_ar.dcx_header[0x24:],
        "install_folder_files": sorted(str(x.relative_to(inst)).replace("\\", "/") for x in inst.rglob("*") if x.is_file()),
    }
    # control: creator-normalised replacements
    nreps = {full[b]: d for b, d in norm.items()}
    all_norm_identical = all(d == vbytes[b] for b, d in norm.items())
    nbnd, ndcx = shaderbnd.build(van_ar, nreps)
    rbnd, rdcx = shaderbnd.build(van_ar, {})
    p["control_all_normalised_identical"] = all_norm_identical
    p["control_normalised_archive_equals_repack"] = ndcx == rdcx and nbnd == rbnd
    p["control_repack_equals_vanilla_file"] = rdcx == van_ar.dcx_raw
    p["control_normalised_archive_sha256"] = sha256(ndcx)
    p["ok"] = (p["interface_ok"] and p["names_equal_vanilla_in_order"] and p["flags_ids_equal_vanilla"]
               and p["differing_equals_replaced_with_new_bytes"] and not p["payload_mismatches"]
               and not p["semantic_diffs_vs_vanilla"] and p["readback_bnd_equals_written"]
               and p["dcx_type_level_same"] and p["dcx_header_same_but_sizes"]
               and p["install_folder_files"] == [ARCHIVE_REL]
               and (not all_norm_identical or p["control_normalised_archive_equals_repack"]))
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    ap.add_argument("--tag", default="rebuild0", help="build dir dist/_shader/filter_<tag>")
    ap.add_argument("--names", nargs="*", help="subset of manifest entry basenames")
    ap.add_argument("--extra", default="", help="extra fxc args for every compile (shlex-split)")
    ap.add_argument("--pack", help="install folder name under dist/_shader_install (must start with fil_)")
    ap.add_argument("--replace", default="exact", choices=("exact", "all"))
    ap.add_argument("--allow-rdef-change", action="store_true",
                    help="pack blobs whose RDEF differs (never ISGN/OSGN); recorded in the report")
    ap.add_argument("--no-cover-duplicates", action="store_true")
    ap.add_argument("--fail-on-inexact", action="store_true")
    ap.add_argument("--jobs", type=int, default=8)
    a = ap.parse_args(argv)

    man_path = Path(a.manifest)
    man = json.loads(man_path.read_text(encoding="utf-8"))
    src_root = (man_path.parent / man.get("src_root", "src")).resolve()
    names = [n for n in man["blobs"] if not a.names or n in a.names]
    if a.names and set(a.names) - set(names):
        raise SystemExit(f"not in manifest: {sorted(set(a.names) - set(names))}")
    tag_dir = DIST / f"filter_{a.tag}"
    out_dir, cache = tag_dir / "blobs", tag_dir / "asm_cache"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    extra = shlex.split(a.extra, posix=False) if a.extra else []
    fxc = fxcasm.fxc_path()

    van_ar = load_vanilla()
    vbytes = {basename(e.name): bytes(e.data) for e in van_ar.entries}
    missing = [n for n in names if n not in vbytes]
    if missing:
        raise SystemExit(f"manifest names not in the vanilla archive: {missing}")

    with ThreadPoolExecutor(a.jobs) as ex:
        built = list(ex.map(lambda n: compile_one(n, man["blobs"][n], man, src_root, out_dir, extra, fxc), names))
    results = [gate_one(r, out_dir, vbytes[r["name"]], cache) if r["ok"] else r for r in built]

    rep = {"manifest": str(man_path), "src_root": str(src_root), "fxc": fxc, "tag": a.tag,
           "vanilla_archive_sha256": sha256(van_ar.dcx_raw),
           "summary": {"n": len(results), "compiled": sum(r["ok"] for r in results),
                       "exact": sum(bool(r.get("exact")) for r in results),
                       "rdef_but_creator": sum(bool(r.get("RDEF_but_creator")) for r in results),
                       "stat": sum(bool(r.get("STAT")) for r in results),
                       "normalised_identical": sum(bool(r.get("normalised_identical")) for r in results)},
           "blobs": [{k: v for k, v in r.items() if not k.startswith("_")} for r in results]}
    if a.pack:
        rep["pack"] = pack(a.pack, van_ar, results, out_dir, a.replace, not a.no_cover_duplicates, a.allow_rdef_change)
    (tag_dir / "report.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")

    for r in results:
        if not r["ok"]:
            print(f"FAIL   {r['name']}: {' | '.join(r['messages'][:3])}")
            continue
        flags = " ".join(k for k in ("SHEX", "ISGN", "OSGN", "STAT", "RDEF_but_creator", "normalised_identical") if r[k])
        state = "EXACT " if r["exact"] else ("STREAM" if r.get("stream_equal_strict") else "DIFF  ")
        print(f"{state} {r['name']:<32} {flags}")
    print("summary:", json.dumps(rep["summary"]))
    if a.pack:
        p = rep["pack"]
        if p.get("interface_refused"):
            print(f"pack REFUSED ({p['note']}):")
            for b, e in p["interface_refused"].items():
                print(f"   {b}: {'; '.join(e)}")
        else:
            print(f"pack {'OK' if p['ok'] else 'FAILED'}: {p['archive']} sha256={p['sha256']} replaced={p['replaced']} "
                  f"differing={p['entries_differing_from_vanilla']} interface_ok={p['interface_ok']} "
                  f"control_repack_equal={p['control_normalised_archive_equals_repack']}")
    print("report:", tag_dir / "report.json")
    bad = (a.fail_on_inexact and rep["summary"]["exact"] != rep["summary"]["n"]) or rep["summary"]["compiled"] != rep["summary"]["n"]
    if a.pack and not rep["pack"]["ok"]:
        bad = True
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
