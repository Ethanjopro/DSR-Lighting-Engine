"""Turn a copy of the FlverPBL constant headers into a given header generation.

    .venv\\Scripts\\python.exe shaders\\tools\\make_gen_headers.py --dir <variant>/FRPG_FlverPBL --asm <blob>.asm
        [--fc FRPG_Common_FC.fxh] [--vc FRPG_Common_VC.fxh] [--alias]

In the two constant headers of the variant directory it removes every uniform
declaration (lines using the FC_REG / VC_REG register macros) and every
'#define gXC_name <register-name>' alias line, keeps all other macros, and
includes generated declaration files (gen_globals.py) in their place:
    <dir>/FRPG_Common_FC_decls.fxh   (fragment constants)
    <dir>/FRPG_Common_VC_decls.fxh   (vertex constants)
With --alias the generated files keep '#define public reflected' aliases parsed
from the original headers (for register-name generations such as DL_FREG_nnn).
The originals are saved next to them as *.orig the first time.
No shader source text is embedded in this tool.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

DECL_RE = re.compile(r"^\s*(UNIFORM_\w+|uniform)\b.*:\s*(FC_REG|VC_REG)\s*\(")
ALIAS_RE = re.compile(r"^\s*#define\s+g[FV]C_\w+\s+(DL_FREG|VR)_\w+\s*(//.*)?$")


def rewrite(path, include_name):
    orig = path + ".orig"
    if not os.path.exists(orig):
        shutil.copyfile(path, orig)
    lines = open(orig, encoding="utf-8", errors="surrogateescape").read().split("\n")
    out, inserted, removed = [], False, 0
    for ln in lines:
        if DECL_RE.match(ln) or ALIAS_RE.match(ln):
            removed += 1
            if "*/" in ln:      # keep a block comment closed
                out.append("*/")
            if not inserted:
                out.append(f'#include "{include_name}"')
                inserted = True
            continue
        out.append(ln)
    with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
        fh.write("\n".join(out))
    return removed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--asm", required=True)
    ap.add_argument("--fc", default="FRPG_Common_FC.fxh")
    ap.add_argument("--vc", default="FRPG_Common_VC.fxh")
    ap.add_argument("--alias", action="store_true")
    a = ap.parse_args()
    py = sys.executable
    for kind, hdr in (("fc", a.fc), ("vc", a.vc)):
        p = os.path.join(a.dir, hdr)
        decl = f"FRPG_Common_{kind.upper()}_decls.fxh"
        cmd = [py, os.path.join(HERE, "gen_globals.py"), "--asm", a.asm, "--select", kind,
               "--out", os.path.join(a.dir, decl)]
        if a.alias:
            src = p + ".orig" if os.path.exists(p + ".orig") else p
            cmd += ["--alias-header", src]
        subprocess.run(cmd, check=True)
        n = rewrite(p, decl)
        print(f"{hdr}: removed {n} declaration/alias lines, includes {decl}")


if __name__ == "__main__":
    main()
