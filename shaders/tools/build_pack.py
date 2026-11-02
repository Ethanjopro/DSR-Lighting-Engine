"""Build the engine's runtime shader pack, engine/data/shaders.pak: replacement blobs keyed by the DXBC checksum
of the vanilla blob they stand in for. The DLL swaps them in at PSSetShader; the game's archives stay untouched.

    .venv\\Scripts\\python.exe shaders\\tools\\build_pack.py [--skip-compile] [--jobs 24]

FlverPBL comes from the unified 421b tree (vanilla_src/rebuild, ignored) with the engine defines; the Filter's
TAA resolve comes from the build in dist/_shader/filter_taa_all (shaders/tools/build_taa_variants.py).

Format (little-endian):
  char magic[4] "DLPK"; u32 version = 1; u32 count;
  count entries: u8 key[16]; u32 offset; u32 size;
  blobs, 16-byte aligned, deduplicated.
"""
from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
from strip_debug import has_debug  # noqa: E402

PY = sys.executable
GATE = ROOT / "vanilla_src" / "rebuild" / "unify" / "gate.py"
MAPPING = ROOT / "vanilla_src" / "rebuild" / "unify" / "mapping_421b.json"
SRC_ROOT = ROOT / "vanilla_src" / "rebuild" / "src"
VAN_FLVER = ROOT / "vanilla_src" / "blobs" / "FRPG_FlverPBL_fpo_DX11"
VAN_FILTER = ROOT / "vanilla_src" / "blobs" / "FRPG_Filter_DX11"
OUT_FLVER = ROOT / "dist" / "_shader" / "engine_flver"
FILTER_TAA = ROOT / "dist" / "_shader" / "filter_taa_all" / "blobs" / "FRPG_Fil_ResolveTAA.fpo"
PACK = ROOT / "engine" / "data" / "shaders.pak"
DEFINES = ["/D", "DSRLE_FEATURES", "/D", "DSRLE_ALL", "/D", "DSRLE_SUN_A", "/D", "DSRLE_ENGINE"]


def compile_flver(jobs: int) -> dict:
    extra = DEFINES + ["/I", str(ROOT / "shaders" / "features")]
    if any(" " in x for x in extra):
        raise SystemExit("gate.py splits --extra on spaces; the repo path must not contain one")
    cmd = [PY, str(GATE), "--mapping", str(MAPPING), "--src-root", str(SRC_ROOT), "--out", str(OUT_FLVER),
           "--extra", " ".join(extra), "--tag", "engine", "--jobs", str(jobs), "--quiet"]
    subprocess.run(cmd, check=False)
    report = json.loads((OUT_FLVER / "gate_engine.json").read_text(encoding="utf-8"))
    failed = [r for r in report["results"] if not r["ok"]]
    print(f"flver: {report['total']['ok']}/{report['total']['n']} compiled in {report['seconds']} s")
    for r in failed[:10]:
        print("  FAILED", r["name"], r.get("error"))
    if failed:
        raise SystemExit(f"{len(failed)} blobs failed to compile")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-compile", action="store_true")
    ap.add_argument("--jobs", type=int, default=24)
    a = ap.parse_args()
    if not a.skip_compile:
        compile_flver(a.jobs)

    mapping = json.loads(MAPPING.read_text(encoding="utf-8"))
    groups: dict[bytes, list[tuple[str, bytes]]] = {}
    for name, rec in mapping.items():
        if not rec.get("top_level_file"):
            continue
        built = OUT_FLVER / rec["family"] / f"{name}.fpo"
        vanilla = (VAN_FLVER / f"{name}.fpo").read_bytes()
        groups.setdefault(vanilla[4:20], []).append((name, built.read_bytes()))

    if FILTER_TAA.exists():
        vanilla = (VAN_FILTER / "FRPG_Fil_ResolveTAA.fpo").read_bytes()
        groups[vanilla[4:20]] = [("FRPG_Fil_ResolveTAA.fpo", FILTER_TAA.read_bytes())]
    else:
        print("warning: no TAA filter build; run shaders/tools/build_taa_variants.py")

    conflicts = 0
    for key, members in groups.items():
        if len({m[1] for m in members}) > 1:
            conflicts += 1
            print("conflict: one vanilla blob, different replacements:", [m[0] for m in members][:4])
    if conflicts:
        raise SystemExit(f"{conflicts} conflicts")
    if any(has_debug(m[1]) for ms in groups.values() for m in ms):
        raise SystemExit("a replacement blob carries debug chunks")

    # Entries, then deduplicated blobs.
    blobs: dict[bytes, int] = {}
    body = bytearray()
    header_size = 12 + 24 * len(groups)
    entries = []
    for key in sorted(groups):
        blob = groups[key][0][1]
        if blob not in blobs:
            pad = (-(header_size + len(body))) % 16
            body += b"\0" * pad
            blobs[blob] = header_size + len(body)
            body += blob
        entries.append((key, blobs[blob], len(blob)))
    data = b"DLPK" + struct.pack("<II", 1, len(entries))
    data += b"".join(k + struct.pack("<II", o, s) for k, o, s in entries)
    data += bytes(body)
    PACK.parent.mkdir(parents=True, exist_ok=True)
    tmp = PACK.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(PACK)
    print(f"{PACK.relative_to(ROOT)}: {len(entries)} entries, {len(blobs)} unique blobs, {len(data) / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
