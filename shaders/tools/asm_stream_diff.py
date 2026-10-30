"""Line diff of two shaders' normalised instruction streams (plus interface diff).

    .venv\\Scripts\\python.exe shaders\\tools\\asm_stream_diff.py VANILLA REBUILT [--loose] [-n CONTEXT] [--max N]

VANILLA / REBUILT may be .fpo/.dxbc blobs (disassembled with fxc /dumpbin) or .asm text.
Temp register numbers are abstracted (strict mode) so the first diverging line
points at the first real source difference. Intended for the fix-first-divergence loop.
"""
from __future__ import annotations

import argparse
import difflib
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fxcasm  # noqa: E402


def load(p):
    if p.lower().endswith((".asm", ".txt")):
        return fxcasm.parse_asm(open(p, encoding="utf-8", errors="replace").read())
    d = tempfile.mkdtemp(prefix="asd_")
    return fxcasm.parse_asm(fxcasm.disassemble(p, os.path.join(d, "x.asm")))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vanilla")
    ap.add_argument("rebuilt")
    ap.add_argument("--loose", action="store_true")
    ap.add_argument("-n", type=int, default=3)
    ap.add_argument("--max", type=int, default=80, help="max diff lines printed")
    a = ap.parse_args()
    v, r = load(a.vanilla), load(a.rebuilt)
    iv, ir = fxcasm.interface(v), fxcasm.interface(r)
    for k in ("isgn", "osgn", "bindings", "slots", "global_flags"):
        if iv[k] != ir[k]:
            print(f"IFACE {k}: vanilla-only={sorted(set(map(str, iv[k])) - set(map(str, ir[k])))} rebuilt-only={sorted(set(map(str, ir[k])) - set(map(str, iv[k])))}")
    for name in sorted(set(iv["cbuffers"]) | set(ir["cbuffers"])):
        a_, b_ = set(map(tuple, iv["cbuffers"].get(name, []))), set(map(tuple, ir["cbuffers"].get(name, [])))
        if a_ != b_:
            print(f"IFACE cbuffer {name}: vanilla-only={sorted(a_ - b_)} rebuilt-only={sorted(b_ - a_)}")
    mode = "loose" if a.loose else "strict"
    sv, sr = fxcasm.normalized_stream(v, mode), fxcasm.normalized_stream(r, mode)
    print(f"instructions: vanilla {len(sv)}  rebuilt {len(sr)}  opcode-multiset sim "
          f"{fxcasm.multiset_similarity(fxcasm.opcode_multiset(v), fxcasm.opcode_multiset(r)):.4f}")
    if sv == sr:
        print("STREAMS IDENTICAL")
        return 0
    out = list(difflib.unified_diff(sv, sr, "vanilla", "rebuilt", n=a.n, lineterm=""))
    for ln in out[: a.max]:
        print(ln)
    if len(out) > a.max:
        print(f"... {len(out) - a.max} more diff lines")
    return 1


if __name__ == "__main__":
    sys.exit(main())
