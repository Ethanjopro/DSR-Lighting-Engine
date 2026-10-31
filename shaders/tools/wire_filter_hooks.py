"""Wire the DSRLE filter hook call sites into the rebuilt (git-ignored) FRPG_Fil_ResolveTAA source.

    .venv\\Scripts\\python.exe shaders\\tools\\wire_filter_hooks.py            # (re)wire from the snapshot
    .venv\\Scripts\\python.exe shaders\\tools\\wire_filter_hooks.py --check    # verify, write nothing
    .venv\\Scripts\\python.exe shaders\\tools\\wire_filter_hooks.py --unwire   # restore the pristine file

This is the scripted patch step for the filter hooks (the filter counterpart of the FlverPBL
wire_hooks.py), so the hooked source is always reproducible from the recovered one:

  pristine  vanilla_src/rebuild/filter/snapshot_prehook/FRPG_Filter/FRPG_Fil_ResolveTAA.fx
            (created on the first run from the unhooked source; sha256 asserted below)
  hooked    vanilla_src/rebuild/filter/src/FRPG_Filter/FRPG_Fil_ResolveTAA.fx
            (the file build_filter.py compiles via manifest.json)

Every run rebuilds the hooked file from the pristine snapshot, so it is idempotent and never
patches a patch. The edits only INSERT lines (the include and two statement hooks, whose
identity defaults in shaders/hooks/dsrle_filter_hooks.fxh are empty); no vanilla line is
changed or removed. Anchors are identified by the sha256 of the anchor line's text (leading
and trailing whitespace stripped) and must occur exactly once, so a changed source makes this
fail loudly instead of silently dropping a hook. The script therefore holds no recovered
source text, only hashes, prose descriptions and the original DSRLE call-site text.
Line endings of the pristine file (CRLF) are kept; inserted lines copy the anchor's indent.

After wiring, the identity build must stay byte-exact:
    build_filter.py --tag rebuild0 --pack fil_rebuild0   ->  sha256 6665a715...  (unchanged)
Original DSRLE tooling.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILT = ROOT / "vanilla_src" / "rebuild" / "filter"
REL = Path("FRPG_Filter") / "FRPG_Fil_ResolveTAA.fx"
TARGET = FILT / "src" / REL
SNAPSHOT = FILT / "snapshot_prehook" / REL
PRISTINE_SHA = "5b0d712150281cc9fb1874e3e61b28b9e0619ddc8924b141b1ece9dd4474b0d6"

# (what, sha256 of the stripped anchor line, "after" | "before", inserted lines)
EDITS = [
    ("include point: after the TAA luma #define (so feature helpers can be parsed with every "
     "filter constant, texture and sampler declared)",
     "dc2a32d759cfb128436937376cc2ff46b1321db5f8d8223d58406042f400c15d", "after",
     ["//[DSRLE] filter hook points (shaders/hooks/dsrle_filter_hooks.fxh, identity defaults)",
      '#include "dsrle_filter_hooks.fxh"']),
    ("history hook: after the statement that maps the bilinear history sample to c/(1+luma), "
     "before the neighbourhood-box clamp",
     "70fb23b78d00da11bb18983b2c765d8ac714decbbfaa6d192aa3a0d6a4dca4c0", "after",
     ["DSRLE_TAA_HOOK_HISTORY(hist, histUV, uv); //[DSRLE]"]),
    ("current-sample hook: before the statement that lerps the clamped history towards the "
     "current sample with the final blend weight",
     "415174ca507242ca58b009db4edefe09257990b420300918cbdf20e4c29e3119", "before",
     ["DSRLE_TAA_HOOK_CURRENT(cC, blend, hist, cN, cS, cW, cE, cNW, cNE, cSW, cSE, minAvg, maxAvg, fullMin, fullMax); //[DSRLE]"]),
]


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def wire(pristine: bytes) -> bytes:
    eol = b"\r\n" if b"\r\n" in pristine else b"\n"
    lines = pristine.split(eol)
    keyed = [sha(ln.strip()) for ln in lines]
    inserts: dict[int, list[bytes]] = {}
    for what, h, where, new in EDITS:
        hits = [i for i, k in enumerate(keyed) if k == h]
        if len(hits) != 1:
            raise SystemExit(f"[{what}] expected exactly 1 anchor line, found {len(hits)}")
        i = hits[0]
        anchor = lines[i]
        indent = anchor[:len(anchor) - len(anchor.lstrip())]
        at = i + 1 if where == "after" else i
        inserts.setdefault(at, []).extend(indent + s.encode("ascii") for s in new)
    out: list[bytes] = []
    for i, ln in enumerate(lines):
        out.extend(inserts.get(i, []))
        out.append(ln)
    out.extend(inserts.get(len(lines), []))
    return eol.join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="verify the hooked file is current; write nothing")
    ap.add_argument("--unwire", action="store_true", help="restore the pristine source into the build tree")
    a = ap.parse_args(argv)

    if not SNAPSHOT.exists():
        cur = TARGET.read_bytes()
        if sha(cur) != PRISTINE_SHA:
            raise SystemExit(f"no snapshot and {TARGET} is not the pristine source (sha256 {sha(cur)}); "
                             "restore the recovered file first")
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TARGET, SNAPSHOT)
        print(f"snapshot created: {SNAPSHOT.relative_to(ROOT)}")
    pristine = SNAPSHOT.read_bytes()
    if sha(pristine) != PRISTINE_SHA:
        raise SystemExit(f"snapshot sha256 {sha(pristine)} != expected {PRISTINE_SHA}")

    want = pristine if a.unwire else wire(pristine)
    have = TARGET.read_bytes() if TARGET.exists() else b""
    if a.check:
        ok = have == want
        print(f"{'OK' if ok else 'STALE'}: {TARGET.relative_to(ROOT)} sha256={sha(have)} expected={sha(want)}")
        return 0 if ok else 1
    if have != want:
        TARGET.write_bytes(want)
        print(f"wrote {TARGET.relative_to(ROOT)}")
    else:
        print(f"unchanged {TARGET.relative_to(ROOT)}")
    print(f"{'pristine' if a.unwire else 'hooked'} sha256={sha(want)} ({len(EDITS) if not a.unwire else 0} insertions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
