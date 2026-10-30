"""Stage recovered HLSL into a compile tree with the file-name casing the
sources themselves use in their #include directives / #line paths.

    python shaders/tools/stage_sources.py --src vanilla_src/hlsl --dst vanilla_src/rebuild/src
        [--variant FRPG_Sfx_DifSpcBmp_________.fpo]

Extracted files are lower-case (e.g. frpg_common_fc.fxh); the includes ask for
"FRPG_Common_FC.fxh" and "../Common/dx11.h". Windows is case-insensitive, but
we keep the canonical casing so #line paths in /Zi builds look like vanilla.
When a file has '*.<blob>.variant' copies (different versions embedded in
different blobs), --variant picks which one is staged.
Existing files in dst that were edited by hand are never overwritten unless
--force is given.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil

INC_RE = re.compile(r'^\s*#\s*include\s+"([^"]+)"', re.M)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    ap.add_argument("--variant", default=None, help="blob name whose *.variant copies to prefer")
    ap.add_argument("--extra-name", action="append", default=[],
                    help="lowercase=Canonical for files never #included (top-level .fx)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    files = []
    for root, _, fs in os.walk(args.src):
        for f in fs:
            files.append(os.path.join(root, f))
    canon = {}
    for p in files:
        if p.endswith(".variant"):
            continue
        try:
            txt = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for inc in INC_RE.findall(txt):
            parts = inc.replace("\\", "/").split("/")
            base = parts[-1]
            canon[base.lower()] = base
            if len(parts) > 1 and parts[-2] != "..":
                canon[parts[-2].lower()] = parts[-2]
    for e in args.extra_name:
        k, v = e.split("=", 1)
        canon[k.lower()] = v

    n = 0
    for p in files:
        rel = os.path.relpath(p, args.src).replace("\\", "/")
        if rel.endswith(".variant"):
            continue
        parts = rel.split("/")
        if "win32dx11" in parts:
            continue
        out_parts = [canon.get(x.lower(), x) for x in parts]
        # directory casing for the two include roots
        out_parts[0] = {"common": "Common", "frpg_flverpbl": "FRPG_FlverPBL",
                        "frpg_filter": "FRPG_Filter", "frpg_deferred": "FRPG_Deferred"}.get(out_parts[0], out_parts[0])
        dst = os.path.join(args.dst, *out_parts)
        srcp = p
        if args.variant:
            v = p + "." + args.variant + ".variant"
            if os.path.exists(v):
                srcp = v
        if os.path.exists(dst) and not args.force:
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(srcp, dst)
        n += 1
        print(f"{rel} -> {os.path.relpath(dst, args.dst)}" + ("  (variant)" if srcp != p else ""))
    print(f"staged {n} files into {args.dst}")


if __name__ == "__main__":
    main()
