"""Compare rebuilt DXBC blobs against vanilla ones.

    .venv\\Scripts\\python.exe shaders\\tools\\dxbc_diff.py [selection] [--tag NAME] [options]

Selection flags are the same as compile.py (--names/--match/--family/--status/--unique).
For every selected blob that exists in both --vanilla and --rebuilt:

Interface (from the fxc /dumpbin reflection header + declarations)
  * ISGN / OSGN elements (semantic, index, mask, register, sysval, format, used mask)
  * used variables of every cbuffer (name, type, offset, size); structured
    buffer element layouts
  * resource/sampler/cbuffer bindings (name, type, dim, slot, count)
  * dcl_constantbuffer sizes + indexing mode, dcl_globalFlags
  * informational only: dcl_temps, SHEX byte identity, compile flags (RDEF)

Instructions (decls and #line/debug comments stripped)
  * opcode multiset similarity (weighted Jaccard) and instruction counts
  * strict normalised-sequence ratio (only temp register numbers abstracted)
  * loose normalised-sequence ratio (also masks/literals abstracted)

Classification per blob:
  exact      interface identical and strict normalised stream identical
  near       interface identical and opcode-multiset similarity >= --near (default 0.97)
  iface_ok   interface identical, instructions further apart
  iface_diff interface differs (listed in the report)

Writes <reports>/<tag>/diff.csv, diff.json and summary.json and prints a summary.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import fxcasm  # noqa: E402


def load(path, cache):
    txt = fxcasm.cached_disasm(path, cache)
    with open(path, "rb") as fh:
        data = fh.read()
    return fxcasm.parse_asm(txt), data


def cb_decls(si):
    out = {}
    for d in si.decls:
        m = re.match(r"dcl_constantbuffer (CB\d+)\[(\d+)\], (\w+)", d)
        if m:
            out[m.group(1)] = (int(m.group(2)), m.group(3))
    return out


def iface_diff(v, r):
    """Return list of human-readable interface differences."""
    diffs = []
    iv, ir = fxcasm.interface(v), fxcasm.interface(r)
    for k in ("isgn", "osgn"):
        if iv[k] != ir[k]:
            sv, sr = set(map(tuple, iv[k])), set(map(tuple, ir[k]))
            diffs.append(f"{k}: vanilla-only={sorted(sv - sr)} rebuilt-only={sorted(sr - sv)}")
    cbv, cbr = iv["cbuffers"], ir["cbuffers"]
    for name in sorted(set(cbv) | set(cbr)):
        a, b = cbv.get(name), cbr.get(name)
        if a is None or b is None:
            if (a or b):  # a cbuffer with used vars missing on one side
                diffs.append(f"cbuffer {name}: present only in {'rebuilt' if a is None else 'vanilla'}")
            continue
        sa, sb = set(map(tuple, a)), set(map(tuple, b))
        if sa != sb:
            diffs.append(f"cbuffer {name} used vars: vanilla-only={sorted(sa - sb)} rebuilt-only={sorted(sb - sa)}")
    bv, br = set(map(tuple, iv["bindings"])), set(map(tuple, ir["bindings"]))
    if bv != br:
        diffs.append(f"bindings: vanilla-only={sorted(bv - br)} rebuilt-only={sorted(br - bv)}")
    dv, dr = cb_decls(v), cb_decls(r)
    if dv != dr:
        diffs.append(f"dcl_constantbuffer: vanilla={dv} rebuilt={dr}")
    sv, sr = [s for s in iv["slots"] if not s.startswith("dcl_constantbuffer")], \
             [s for s in ir["slots"] if not s.startswith("dcl_constantbuffer")]
    if sv != sr:
        diffs.append(f"slot decls: vanilla-only={sorted(set(sv) - set(sr))} rebuilt-only={sorted(set(sr) - set(sv))}")
    if iv["global_flags"] != ir["global_flags"]:
        diffs.append(f"globalFlags: vanilla='{iv['global_flags']}' rebuilt='{ir['global_flags']}'")
    return diffs


def compare(name, vpath, rpath, cache, near):
    v, vdata = load(vpath, cache)
    r, rdata = load(rpath, cache)
    idiffs = iface_diff(v, r)
    ov, orr = fxcasm.opcode_multiset(v), fxcasm.opcode_multiset(r)
    osim = fxcasm.multiset_similarity(ov, orr)
    sv, sr = fxcasm.normalized_stream(v, "strict"), fxcasm.normalized_stream(r, "strict")
    lv, lr = fxcasm.normalized_stream(v, "loose"), fxcasm.normalized_stream(r, "loose")
    strict_ratio = fxcasm.lcs_ratio(sv, sr)
    loose_ratio = fxcasm.lcs_ratio(lv, lr)
    exact_stream = sv == sr
    if idiffs:
        cls = "iface_diff"
    elif exact_stream:
        cls = "exact"
    elif osim >= near:
        cls = "near"
    else:
        cls = "iface_ok"
    opdelta = {k: orr[k] - ov[k] for k in set(ov) | set(orr) if orr[k] != ov[k]}
    shex_v = fxcasm.dxbc_chunks(vdata).get("SHEX")
    shex_r = fxcasm.dxbc_chunks(rdata).get("SHEX")
    return {
        "name": name, "class": cls,
        "iface_identical": not idiffs, "iface_diffs": idiffs,
        "n_instr_vanilla": len(v.instrs), "n_instr_rebuilt": len(r.instrs),
        "instr_delta": len(r.instrs) - len(v.instrs),
        "opcode_similarity": round(osim, 4),
        "strict_seq_ratio": round(strict_ratio, 4),
        "loose_seq_ratio": round(loose_ratio, 4),
        "stream_identical": exact_stream,
        "shex_identical": shex_v == shex_r,
        "chunks_identical": sorted(k for k, c in fxcasm.dxbc_chunks(vdata).items() if fxcasm.dxbc_chunks(rdata).get(k) == c),
        "file_identical": vdata == rdata,
        "temps_vanilla": v.temps, "temps_rebuilt": r.temps,
        "opcode_delta": dict(sorted(opdelta.items())),
        "compiler_vanilla": v.compiler, "compiler_rebuilt": r.compiler,
        "flags_vanilla": fxcasm.decode_flags(fxcasm.rdef_flags(vdata))["raw"],
        "flags_rebuilt": fxcasm.decode_flags(fxcasm.rdef_flags(rdata))["raw"],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mapping", default=os.path.join(REPO, "vanilla_src", "rebuild", "mapping.json"))
    ap.add_argument("--vanilla", default=os.path.join(REPO, "vanilla_src", "blobs", "FRPG_FlverPBL_fpo_DX11"))
    ap.add_argument("--rebuilt", default=os.path.join(REPO, "vanilla_src", "rebuild", "out"),
                    help="dir containing <family>/<blob>.fpo")
    ap.add_argument("--reports", default=os.path.join(REPO, "vanilla_src", "rebuild", "reports"))
    ap.add_argument("--cache", default=os.path.join(REPO, "vanilla_src", "rebuild", "asm_cache"))
    ap.add_argument("--tag", default="latest")
    ap.add_argument("--near", type=float, default=0.97)
    ap.add_argument("--names", nargs="*")
    ap.add_argument("--names-file", help="file with one blob name per line (added to --names)")
    ap.add_argument("--match")
    ap.add_argument("--family")
    ap.add_argument("--status")
    ap.add_argument("--unique", action="store_true")
    ap.add_argument("--jobs", type=int, default=24)
    ap.add_argument("--verbose", "-v", action="store_true")
    a = ap.parse_args()
    if a.names_file:
        a.names = (a.names or []) + [l.strip() for l in open(a.names_file, encoding="utf-8") if l.strip()]

    from compile import select  # same selection semantics
    mapping = json.load(open(a.mapping, encoding="utf-8"))
    names = select(mapping, a)
    pairs, missing = [], []
    for n in names:
        vp = os.path.join(a.vanilla, n + ".fpo")
        rp = os.path.join(a.rebuilt, mapping[n]["family"], n + ".fpo")
        (pairs if os.path.exists(rp) and os.path.exists(vp) else missing).append((n, vp, rp))
    with ThreadPoolExecutor(a.jobs) as ex:
        rows = list(ex.map(lambda t: compare(t[0], t[1], t[2], a.cache, a.near), pairs))

    outdir = os.path.join(a.reports, a.tag)
    os.makedirs(outdir, exist_ok=True)
    cols = ["name", "class", "iface_identical", "n_instr_vanilla", "n_instr_rebuilt", "instr_delta",
            "opcode_similarity", "strict_seq_ratio", "loose_seq_ratio", "stream_identical", "shex_identical", "file_identical",
            "temps_vanilla", "temps_rebuilt", "flags_vanilla", "flags_rebuilt", "iface_diffs"]
    with open(os.path.join(outdir, "diff.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([" | ".join(r[c]) if c == "iface_diffs" else r[c] for c in cols])
    with open(os.path.join(outdir, "diff.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=1)

    def stats(xs):
        if not xs: return {}
        xs = sorted(xs)
        return {"min": xs[0], "median": xs[len(xs) // 2], "mean": round(sum(xs) / len(xs), 4), "max": xs[-1]}
    cls = Counter(r["class"] for r in rows)
    summ = {
        "tag": a.tag, "compared": len(rows), "missing_rebuilt": [m[0] for m in missing],
        "classes": dict(cls),
        "interface_identical": sum(r["iface_identical"] for r in rows),
        "stream_identical": sum(r["stream_identical"] for r in rows),
        "shex_identical": sum(r["shex_identical"] for r in rows),
        "file_identical": sum(r["file_identical"] for r in rows),
        "chunks_identical_hist": dict(Counter(",".join(r["chunks_identical"]) for r in rows)),
        f"opcode_similarity>={a.near}": sum(r["opcode_similarity"] >= a.near for r in rows),
        "instr_count_equal": sum(r["instr_delta"] == 0 for r in rows),
        "opcode_similarity": stats([r["opcode_similarity"] for r in rows]),
        "strict_seq_ratio": stats([r["strict_seq_ratio"] for r in rows]),
        "loose_seq_ratio": stats([r["loose_seq_ratio"] for r in rows]),
        "instr_delta": stats([r["instr_delta"] for r in rows]),
        "abs_instr_delta_pct": stats([round(100 * abs(r["instr_delta"]) / max(1, r["n_instr_vanilla"]), 2) for r in rows]),
        "opcode_delta_total": dict(sorted(sum((Counter(r["opcode_delta"]) for r in rows), Counter()).items())),
    }
    # Counter addition drops non-positive counts; recompute signed totals
    tot = Counter()
    for r in rows:
        for k, d in r["opcode_delta"].items():
            tot[k] += d
    summ["opcode_delta_total"] = {k: v for k, v in sorted(tot.items()) if v}
    with open(os.path.join(outdir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summ, fh, indent=1)

    print(f"compared {len(rows)} (missing rebuilt: {len(missing)}) -> {outdir}")
    for k in ("classes", "interface_identical", "stream_identical", "shex_identical", "file_identical", "chunks_identical_hist",
              f"opcode_similarity>={a.near}", "instr_count_equal", "opcode_similarity",
              "strict_seq_ratio", "loose_seq_ratio", "instr_delta", "abs_instr_delta_pct", "opcode_delta_total"):
        print(f"  {k}: {summ[k]}")
    if a.verbose:
        for r in sorted(rows, key=lambda r: r["opcode_similarity"]):
            print(f"  {r['name']:44} {r['class']:10} op={r['opcode_similarity']:.4f} strict={r['strict_seq_ratio']:.4f} "
                  f"n={r['n_instr_vanilla']}->{r['n_instr_rebuilt']}")
            for d in r["iface_diffs"]:
                print("      ", d[:300])


if __name__ == "__main__":
    main()
