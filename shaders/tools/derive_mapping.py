"""Derive a per-family mapping file whose top_level_file points at per-generation source trees.

    .venv\\Scripts\\python.exe shaders\\tools\\derive_mapping.py --family Phn --out vanilla_src\\rebuild\\mapping_Phn.json
        --gen 421b132aa3=Phn/g421b [--gen b56cb60b57=Phn/gb56c ...]
        [--set-file REGEX=relative/file.fx ...] [--add-define REGEX=NAME[=VAL] ...]
        [--drop-define REGEX=NAME ...] [--note REGEX=text ...]

Each selected record is copied from mapping.json; when its header_gen has a
--gen prefix, top_level_file becomes <prefix>/<original top_level_file>.
--set-file overrides top_level_file (after the prefix) for blob names matching
REGEX. --add-define appends a cmdline define for matching blob names;
--drop-define removes cmdline defines whose name (before '=') equals NAME.
--note appends a free-text note to rebuild_notes of matching records.
REGEX may be prefixed with '<header_gen>:' to restrict it to one generation.
The shared mapping.json is not modified.
"""
from __future__ import annotations

import argparse
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mapping", default=os.path.join(REPO, "vanilla_src", "rebuild", "mapping.json"))
    ap.add_argument("--family", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--gen", action="append", default=[])
    ap.add_argument("--set-file", action="append", default=[])
    ap.add_argument("--add-define", action="append", default=[])
    ap.add_argument("--drop-define", action="append", default=[])
    ap.add_argument("--note", action="append", default=[])
    a = ap.parse_args()
    m = json.load(open(a.mapping, encoding="utf-8"))
    gens = dict(g.split("=", 1) for g in a.gen)
    def rules(lst):
        out = []
        for s in lst:
            k, v = s.split("=", 1)
            g = None
            if ":" in k and not k.startswith("^"):
                g, k = k.split(":", 1)
            out.append((g, re.compile(k), v))
        return out
    setf, adds, drops, notes = rules(a.set_file), rules(a.add_define), rules(a.drop_define), rules(a.note)

    def hit(g, rx, n, r):
        return (g is None or r.get("header_gen") == g) and rx.search(n)
    out = {}
    for n, r in m.items():
        if r["family"] != a.family:
            continue
        r = dict(r)
        r["cmdline_defines"] = list(r.get("cmdline_defines", []))
        pre = gens.get(r.get("header_gen"))
        if pre:
            r["top_level_file"] = pre.rstrip("/") + "/" + r["top_level_file"]
        for g, rx, f in setf:
            if hit(g, rx, n, r):
                r["top_level_file"] = f
        for g, rx, d in drops:
            if hit(g, rx, n, r):
                r["cmdline_defines"] = [x for x in r["cmdline_defines"] if x.split("=", 1)[0] != d]
        for g, rx, d in adds:
            if hit(g, rx, n, r) and d not in r["cmdline_defines"]:
                r["cmdline_defines"].append(d)
        for g, rx, t in notes:
            if hit(g, rx, n, r):
                r.setdefault("rebuild_notes", []).append(t)
        out[n] = r
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    print(f"{len(out)} records -> {a.out}")


if __name__ == "__main__":
    main()
