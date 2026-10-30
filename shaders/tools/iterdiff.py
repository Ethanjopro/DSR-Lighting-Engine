"""Compile one entry point and diff it against one reference blob (fast iteration loop).

    python iterdiff.py --src FILE.fx --entry MAIN --ref REF.fpo [--D NAME[=V] ...] [--flags "/T ps_5_0"]
                       [--out DIR] [--show N] [--mode loose|strict] [--quiet]

Prints interface differences (same rules as dxbc_diff.py), opcode-multiset
similarity, instruction counts and a unified diff of the normalised
instruction streams (first --show lines).  Generic: nothing game specific.
"""
from __future__ import annotations

import argparse
import difflib
import os
import shlex
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fxcasm  # noqa: E402
import dxbc_diff  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True)
    ap.add_argument("--entry", required=True)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--D", action="append", default=[])
    ap.add_argument("--flags", default="/T ps_5_0")
    ap.add_argument("--out", default=None)
    ap.add_argument("--show", type=int, default=80)
    ap.add_argument("--mode", default="strict")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    out = a.out or tempfile.mkdtemp()
    os.makedirs(out, exist_ok=True)
    fpo = os.path.join(out, a.entry + ".fpo")
    asm = os.path.join(out, a.entry + ".asm")
    cmd = [fxcasm.fxc_path(), "/nologo"] + shlex.split(a.flags, posix=False) + ["/E", a.entry]
    for d in a.D:
        cmd += ["/D", d]
    cmd += ["/Fo", fpo, "/Fc", asm, a.src]
    if os.path.exists(fpo):
        os.remove(fpo)
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if p.returncode != 0:
        print("COMPILE FAILED\n" + p.stdout + p.stderr)
        return 2
    errs = [l for l in (p.stdout + p.stderr).splitlines() if "warning" in l and "X3205" not in l]
    if errs and not a.quiet:
        print("\n".join(errs[:10]))
    cache = os.path.join(out, "cache")
    r = dxbc_diff.compare(a.entry, a.ref, fpo, cache, 0.97)
    print(f"class={r['class']} op_sim={r['opcode_similarity']} strict={r['strict_seq_ratio']} "
          f"loose={r['loose_seq_ratio']} n={r['n_instr_vanilla']}->{r['n_instr_rebuilt']} shex_identical={r['shex_identical']}")
    for d in r["iface_diffs"]:
        print("  IFACE", d[:600])
    if r["opcode_delta"]:
        print("  opdelta(rebuilt-vanilla):", r["opcode_delta"])
    if a.show and not r["stream_identical"]:
        v = fxcasm.parse_asm(fxcasm.cached_disasm(a.ref, cache))
        b = fxcasm.parse_asm(open(asm, encoding="utf-8", errors="replace").read())
        sv, sb = fxcasm.normalized_stream(v, a.mode), fxcasm.normalized_stream(b, a.mode)
        lines = list(difflib.unified_diff(sv, sb, "vanilla", "rebuilt", n=2, lineterm=""))
        print("\n".join(lines[:a.show]))
        if len(lines) > a.show:
            print(f"... ({len(lines) - a.show} more diff lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
