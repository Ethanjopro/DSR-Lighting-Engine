"""Unified diff of the normalized instruction streams of two DXBC blobs.

    .venv\\Scripts\\python.exe shaders\\tools\\asmdiff.py vanilla.fpo rebuilt.fpo [--mode strict|loose|raw] [-n 3]

Also prints interface differences (dxbc_diff.iface_diff).
"""
from __future__ import annotations

import argparse
import difflib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import fxcasm  # noqa: E402
from dxbc_diff import iface_diff  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--mode", default="strict", choices=("strict", "loose", "raw"))
    ap.add_argument("-n", type=int, default=2)
    ap.add_argument("--cache", default=os.path.join(REPO, "vanilla_src", "rebuild", "asm_cache"))
    a = ap.parse_args()
    sa = fxcasm.parse_asm(fxcasm.cached_disasm(a.a, a.cache))
    sb = fxcasm.parse_asm(fxcasm.cached_disasm(a.b, a.cache))
    for d in iface_diff(sa, sb):
        print("IFACE:", d[:600])
    if a.mode == "raw":
        la, lb = sa.instrs, sb.instrs
    else:
        la, lb = fxcasm.normalized_stream(sa, a.mode), fxcasm.normalized_stream(sb, a.mode)
    print(f"instr {len(sa.instrs)} -> {len(sb.instrs)}")
    for ln in difflib.unified_diff(la, lb, "vanilla", "rebuilt", n=a.n, lineterm=""):
        print(ln)


if __name__ == "__main__":
    main()
