"""Check reflection (RDEF) identity of rebuilt vs vanilla blobs, ignoring the compiler creator string.

    .venv\\Scripts\\python.exe shaders\\tools\\rdef_check.py --mapping M.json [selection as compile.py] [--rebuilt DIR] [--out report.json]

For each pair it reports:
  rdef_identical_but_creator : RDEF bytes identical up to the creator string (which is the last
                               string in the chunk), i.e. same cbuffers, variable names/types/offsets/
                               flags, declaration order and resource bindings
  declared_layout_identical  : same set of declared cbuffer variables (name, type, offset, size,
                               used flag) and the same resource bindings, order ignored
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import fxcasm  # noqa: E402
from compile import select  # noqa: E402


def creator_offset(rdef: bytes) -> int:
    i = rdef.find(b"Microsoft (R) HLSL Shader Compiler")
    return i if i >= 0 else len(rdef)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mapping", default=os.path.join(REPO, "vanilla_src", "rebuild", "mapping.json"))
    ap.add_argument("--vanilla", default=os.path.join(REPO, "vanilla_src", "blobs", "FRPG_FlverPBL_fpo_DX11"))
    ap.add_argument("--rebuilt", default=os.path.join(REPO, "vanilla_src", "rebuild", "out"))
    ap.add_argument("--cache", default=os.path.join(REPO, "vanilla_src", "rebuild", "asm_cache"))
    ap.add_argument("--names", nargs="*")
    ap.add_argument("--names-file")
    ap.add_argument("--match")
    ap.add_argument("--family")
    ap.add_argument("--status")
    ap.add_argument("--unique", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.names_file:
        a.names = (a.names or []) + [l.strip() for l in open(a.names_file, encoding="utf-8") if l.strip()]
    m = json.load(open(a.mapping, encoding="utf-8"))
    rows = []
    for n in select(m, a):
        vp = os.path.join(a.vanilla, n + ".fpo")
        rp = os.path.join(a.rebuilt, m[n]["family"], n + ".fpo")
        if not (os.path.exists(vp) and os.path.exists(rp)):
            continue
        vd, rd = open(vp, "rb").read(), open(rp, "rb").read()
        rv, rr = fxcasm.dxbc_chunks(vd).get("RDEF", b""), fxcasm.dxbc_chunks(rd).get("RDEF", b"")
        ident = rv[:creator_offset(rv)] == rr[:creator_offset(rr)]
        sv = fxcasm.parse_asm(fxcasm.cached_disasm(vp, a.cache))
        sr = fxcasm.parse_asm(fxcasm.cached_disasm(rp, a.cache))

        def lay(si):
            return ({cb.name: sorted((x.name, x.type, x.offset, x.size, x.used) for x in cb.vars) for cb in si.cbuffers},
                    sorted((b.name, b.type, b.format, b.dim, b.bind, b.count) for b in si.bindings))
        rows.append({"name": n, "rdef_identical_but_creator": ident, "declared_layout_identical": lay(sv) == lay(sr)})
    summ = {"compared": len(rows),
            "rdef_identical_but_creator": sum(r["rdef_identical_but_creator"] for r in rows),
            "declared_layout_identical": sum(r["declared_layout_identical"] for r in rows)}
    print(json.dumps(summ))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump({"summary": summ, "rows": rows}, fh, indent=1)


if __name__ == "__main__":
    main()
