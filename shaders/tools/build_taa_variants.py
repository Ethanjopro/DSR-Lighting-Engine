"""Build, gate, pack and runtime-check the DSRLE ResolveTAA feature variants (filter archive).

    .venv\\Scripts\\python.exe shaders\\tools\\build_taa_variants.py [--only cr s30 ...] [--no-hardware]

Steps (each a hard failure when it does not hold):
  1. wire_filter_hooks.py --check: the hooked, git-ignored ResolveTAA source equals what the
     scripted patch step produces from the pristine snapshot.
  2. Identity with the hooks in place: build_filter.py --tag rebuild0 --pack fil_rebuild0 must give
     sha256 6665a715... (the pre-hook value), all 15 blobs EXACT.
  3. /D DSRLE_FEATURES alone (tag taa_featonly): every blob byte-identical to the identity build.
  4. Per variant: a full 15-blob build with the variant's defines (tag taa_<v>_full) in which only
     FRPG_Fil_ResolveTAA.fpo may differ (the shared vpo and the other 13 blobs stay EXACT), then the
     install folder (tag taa_<v>, --names FRPG_Fil_ResolveTAA.fpo --replace all --pack fil_taa_<v>):
     build_filter.py's interface gate (ISGN/OSGN equal, RDEF equal but creator) must pass, and the
     archive must differ from vanilla in exactly that one entry.
  5. ID3D11Device::Create*Shader for every entry of every packed archive on WARP, and on the
     hardware device when one can be created (skipped with a note otherwise; the game may own it).
  6. Instruction / sample statistics of the ResolveTAA fpo per variant (fxc /dumpbin).
  7. Test-only builds (TEST_ONLY: the 5-tap Catmull-Rom; the sampler-independent 16-tap fallback,
     DSRLE_TAA_CATMULL_TAPS=16, every tap at an exact texel centre) for shaders/tools/taa_warp_test.py:
     full 15-blob build, same "only the fpo changes" check and the same interface gate (ISGN/OSGN/RDEF),
     not packed. An entry may declare an expected RDEF change (e.g. a Load-only fetch that leaves s1
     unused, which makes fxc drop the s1 binding); it is then reported as RDEF-CHANGE and would need
     build_filter.py --allow-rdef-change to be packed.
Summary: dist/_shader/taa_variants.json. Install folders: dist/_shader_install/fil_taa_<v>/.
Original DSRLE tooling; reads only the vanilla archive copy under vanilla/ and the build outputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import d3d11check  # noqa: E402
import fxcasm  # noqa: E402
import shaderbnd  # noqa: E402

PY = sys.executable
DIST = ROOT / "dist" / "_shader"
INSTALL = ROOT / "dist" / "_shader_install"
ARCHIVE_REL = "shader/FRPG_Filter_DX11.shaderbnd.dcx"
IDENTITY_SHA = "6665a71533ee1bea9745f776df195e2375405e3ca20f96002220b1ae7c4cd95f"
TAA = "FRPG_Fil_ResolveTAA.fpo"
VARIANTS = {   # name: extra defines (on top of DSRLE_FEATURES)
    "cr": ["DSRLE_TAA_CATMULL"],
    "s30": ["DSRLE_TAA_SHARPEN"],                                   # DSRLE_TAA_SHARPEN_AMOUNT default 0.3
    "all": ["DSRLE_TAA_ALL"],
    "all_s50": ["DSRLE_TAA_ALL", "DSRLE_TAA_SHARPEN_AMOUNT=0.5"],
}
TEST_ONLY = {  # built (gated, not packed) for taa_warp_test.py comparisons: (defines, expected RDEF change)
    "cr5": (["DSRLE_TAA_CATMULL", "DSRLE_TAA_CATMULL_TAPS=5"], None),
    "cr16": (["DSRLE_TAA_CATMULL", "DSRLE_TAA_CATMULL_TAPS=16"], None),   # sampler-independent fallback
}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def run(args: list[str]) -> str:
    p = subprocess.run([PY, *args], cwd=ROOT, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"FAILED: {' '.join(args)}\n{p.stdout}\n{p.stderr}")
    return p.stdout


def build(tag: str, defines: list[str], names: list[str] | None = None, pack: str | None = None) -> dict:
    args = [str(HERE / "build_filter.py"), "--tag", tag]
    if defines:
        args += ["--extra", " ".join(f"/D {d}" for d in defines)]
    if names:
        args += ["--names", *names]
    if pack:
        args += ["--pack", pack] + (["--replace", "all"] if defines else [])
    run(args)
    return json.loads((DIST / f"filter_{tag}" / "report.json").read_text(encoding="utf-8"))


def asm_stats(blob: Path) -> dict:
    txt = fxcasm.cached_disasm(str(blob), str(DIST / "_work" / "taa_asm_cache"))
    count = lambda pat: len(re.findall(pat, txt, re.M))  # noqa: E731
    m = re.search(r"Approximately (\d+) instruction slots", txt)
    t = re.search(r"dcl_temps (\d+)", txt)
    return {"instruction_slots": int(m.group(1)) if m else None, "temps": int(t.group(1)) if t else None,
            "sample_l": count(r"^\s*sample_l"), "ld": count(r"^\s*ld_indexable"),
            "gather4": count(r"^\s*gather4"), "resinfo": count(r"^\s*resinfo")}


def entries(path: Path) -> dict[str, bytes]:
    ar = shaderbnd.read_archive(path)
    return {e.name.replace("\\", "/").rsplit("/", 1)[-1]: bytes(e.data) for e in ar.entries}


def create_all(path: Path, hardware: bool) -> dict:
    try:
        dev = d3d11check.Device(hardware=hardware)
    except OSError as ex:
        return {"device": "hardware" if hardware else "warp", "skipped": str(ex)}
    bad = {}
    with dev:
        ents = entries(path)
        for name, blob in ents.items():
            hr = dev.create(blob)
            if hr != 0:
                bad[name] = f"0x{hr:08x}"
        return {"device": dev.driver, "feature_level": hex(dev.feature_level), "entries": len(ents), "failures": bad}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", choices=sorted(VARIANTS))
    ap.add_argument("--no-hardware", action="store_true")
    a = ap.parse_args(argv)
    out: dict = {}

    print(run([str(HERE / "wire_filter_hooks.py"), "--check"]).strip())

    ident = build("rebuild0", [], pack="fil_rebuild0")
    ok = ident["pack"]["ok"] and ident["pack"]["sha256"] == IDENTITY_SHA and ident["summary"]["exact"] == 15
    out["identity"] = {"sha256": ident["pack"]["sha256"], "exact": ident["summary"]["exact"], "ok": ok}
    print(f"identity fil_rebuild0 sha256={ident['pack']['sha256']} exact={ident['summary']['exact']}/15 -> {'OK' if ok else 'FAIL'}")
    if not ok:
        return 1
    id_blobs = DIST / "filter_rebuild0" / "blobs"

    build("taa_featonly", ["DSRLE_FEATURES"])
    diff = [p.name for p in sorted(id_blobs.iterdir())
            if p.read_bytes() != (DIST / "filter_taa_featonly" / "blobs" / p.name).read_bytes()]
    out["features_flag_alone"] = {"blobs_differing_from_identity": diff}
    print(f"DSRLE_FEATURES alone: {len(diff)} blobs differ from the identity build {diff}")
    if diff:
        return 1

    vanilla = entries(ROOT / "vanilla" / ARCHIVE_REL)
    base_stats = asm_stats(id_blobs / TAA)
    out["identity"]["asm"] = base_stats
    out["variants"] = {}
    for v in a.only or VARIANTS:
        defs = ["DSRLE_FEATURES", *VARIANTS[v]]
        full = build(f"taa_{v}_full", defs)
        changed = [b["name"] for b in full["blobs"] if not b.get("exact")]
        vpo = next(b for b in full["blobs"] if b["name"] == "FRPG_Fil_ResolveTAA.vpo")
        rep = build(f"taa_{v}", defs, names=[TAA], pack=f"fil_taa_{v}")
        p = rep["pack"]
        g = rep["blobs"][0]
        arch = INSTALL / f"fil_taa_{v}" / ARCHIVE_REL
        res = {"defines": defs, "full_build_non_exact": changed, "vpo_exact": bool(vpo["exact"]),
               "gate": {k: bool(g.get(k)) for k in ("ISGN", "OSGN", "RDEF_but_creator", "SHEX", "STAT", "checksum_ok")},
               "pack_ok": bool(p.get("ok")), "interface_refused": p.get("interface_refused")}
        if p.get("ok"):
            ents = entries(arch)
            res["archive"] = str(arch.relative_to(ROOT)).replace("\\", "/")
            res["sha256"] = sha(arch.read_bytes())
            res["entries_differing_from_vanilla"] = sorted(n for n in ents if ents[n] != vanilla.get(n))
            res["entry_names_equal_vanilla"] = sorted(ents) == sorted(vanilla)
            res["create_warp"] = create_all(arch, False)
            res["create_hardware"] = {"skipped": "--no-hardware"} if a.no_hardware else create_all(arch, True)
            res["asm"] = asm_stats(DIST / f"filter_taa_{v}" / "blobs" / TAA)
            res["asm_delta"] = {k: (res["asm"][k] - base_stats[k]) if None not in (res["asm"][k], base_stats[k]) else None
                                for k in base_stats}
        good = (changed == [TAA] and res["vpo_exact"] and res["pack_ok"]
                and res.get("entries_differing_from_vanilla") == [TAA]
                and not res.get("create_warp", {}).get("failures") and "skipped" not in res.get("create_warp", {})
                and not res.get("create_hardware", {}).get("failures"))
        res["ok"] = good
        out["variants"][v] = res
        hw = res.get("create_hardware", {})
        print(f"fil_taa_{v:<8} {'OK  ' if good else 'FAIL'} sha256={res.get('sha256')} differ={res.get('entries_differing_from_vanilla')} "
              f"warp_fail={len(res.get('create_warp', {}).get('failures', {}))} "
              f"hw={'skipped' if 'skipped' in hw else str(len(hw.get('failures', {}))) + ' fail'} "
              f"slots {base_stats['instruction_slots']}->{res.get('asm', {}).get('instruction_slots')} "
              f"sample_l {base_stats['sample_l']}->{res.get('asm', {}).get('sample_l')}")
    out["test_only"] = {}
    for v, (d, rdef_note) in TEST_ONLY.items():
        full = build(f"taa_{v}_full", ["DSRLE_FEATURES", *d])
        changed = [b["name"] for b in full["blobs"] if not b.get("exact")]
        g = next(b for b in full["blobs"] if b["name"] == TAA)
        st = asm_stats(DIST / f"filter_taa_{v}_full" / "blobs" / TAA)
        sig_ok = changed == [TAA] and bool(g.get("ISGN")) and bool(g.get("OSGN"))
        rdef_ok = bool(g.get("RDEF_but_creator"))
        ok = sig_ok and (rdef_ok if rdef_note is None else not rdef_ok)   # an expected RDEF change must show
        state = "OK" if ok and rdef_ok else ("RDEF-CHANGE (expected)" if ok else "FAIL")
        out["test_only"][v] = {"defines": d, "full_build_non_exact": changed, "ISGN_OSGN_equal": sig_ok,
                               "RDEF_but_creator": rdef_ok, "expected_rdef_change": rdef_note, "ok": ok,
                               "packable_without_waiver": sig_ok and rdef_ok, "asm": st}
        print(f"test-only taa_{v}_full {state} non-exact={changed} slots {st['instruction_slots']} "
              f"sample_l {st['sample_l']} ld {st['ld']}" + (f" [{rdef_note}]" if rdef_note else ""))
    (DIST / "taa_variants.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print("summary:", DIST / "taa_variants.json")
    return 0 if all(r["ok"] for r in out["variants"].values()) and all(r["ok"] for r in out["test_only"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
