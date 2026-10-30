"""Generic constant-buffer helpers driven by fxc reflection (fxc /dumpbin /Fc text).

    python cbtool.py decl ASM [--cb $Globals] [--pin-until NAME | --pin-prefix P] [--macro-style]
        Emit HLSL declarations that reproduce the declared layout of a cbuffer as
        reflected in ASM.  Variables matching the pin rule get an explicit
        register(cN) (offset/16); the others are emitted unpinned, in reflection
        order, so fxc packs them after the highest pinned register (verified
        behaviour of fxc 10: unpinned globals are placed after the last pinned
        register, not into holes).  Use --macro-style to emit
        `TYPE name : REGMACRO(cN);` instead of `uniform TYPE name : register(cN);`.

    python cbtool.py annotate ASM
        Print the instruction stream with every cbN[i].sw operand annotated with
        the variable name(s) (from the same reflection) that cover that register.

Nothing in here is specific to any game.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fxcasm  # noqa: E402


def load(path):
    if path.lower().endswith((".fpo", ".fxo", ".cso", ".vpo", ".dxbc")):
        txt = fxcasm.disassemble(path)
    else:
        txt = open(path, encoding="utf-8", errors="replace").read()
    return fxcasm.parse_asm(txt)


def cb_regmap(si):
    """{cb_index: {register: [(name, first_comp_offset_in_reg)]}} using binding order."""
    out = {}
    cbslots = {b.name: int(b.bind[2:]) for b in si.bindings if b.type == "cbuffer"}
    for cb in si.cbuffers:
        if cb.kind != "cbuffer" or cb.name not in cbslots:
            continue
        m = {}
        for v in cb.vars:
            size = max(v.size, 4)
            r0, r1 = v.offset // 16, (v.offset + size - 1) // 16
            for r in range(r0, r1 + 1):
                m.setdefault(r, []).append(v.name if r1 == r0 else f"{v.name}+{r - r0}")
        out[cbslots[cb.name]] = m
    return out


def cmd_annotate(a):
    si = load(a.asm)
    rm = cb_regmap(si)

    def repl(mo):
        cb, reg = int(mo.group(1)), int(mo.group(2))
        names = rm.get(cb, {}).get(reg)
        return mo.group(0) + ("{" + "/".join(names) + "}" if names else "")
    for ins in si.instrs:
        print(re.sub(r"cb(\d+)\[(\d+)\]", repl, ins))


def cmd_decl(a):
    si = load(a.asm)
    cb = next((c for c in si.cbuffers if c.name == a.cb), None)
    if cb is None:
        sys.exit(f"no cbuffer {a.cb}")
    pinning = True if (a.pin_until or a.pin_prefix) else False
    for v in cb.vars:
        pin = False
        if a.pin_prefix:
            pin = v.name.startswith(a.pin_prefix)
        elif a.pin_until:
            pin = pinning
            if v.name.split("[")[0] == a.pin_until:
                pinning = False
        name = v.name
        if pin and v.offset % 16:
            print(f"// WARNING: {name} offset {v.offset} not register aligned", file=sys.stderr)
        if a.macro_style:
            reg = f" : {a.reg_macro}(c{v.offset // 16})" if pin else ""
            print(f"\t{a.uniform_kw}{v.type} {name}{reg};")
        else:
            reg = f" : register(c{v.offset // 16})" if pin else ""
            print(f"uniform {v.type} {name}{reg};\t// {v.offset}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decl")
    d.add_argument("asm")
    d.add_argument("--cb", default="$Globals")
    d.add_argument("--pin-until", help="pin every variable up to and including this one")
    d.add_argument("--pin-prefix", help="pin variables whose name starts with this prefix")
    d.add_argument("--macro-style", action="store_true")
    d.add_argument("--reg-macro", default="FC_REG")
    d.add_argument("--uniform-kw", default="uniform ")
    n = sub.add_parser("annotate")
    n.add_argument("asm")
    a = ap.parse_args()
    {"decl": cmd_decl, "annotate": cmd_annotate}[a.cmd](a)


if __name__ == "__main__":
    main()
