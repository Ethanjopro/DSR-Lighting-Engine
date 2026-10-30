"""Parallel fxc compile of entries from a mapping.json.

    .venv\\Scripts\\python.exe shaders\\tools\\compile.py [selection] [options]

Selection (combinable; all given filters must match):
    --names N [N ...]     exact blob names
    --match REGEX         regex on blob name
    --family F            Phn, Gst, Sfx, ...
    --status S            recovered / unrecovered
    --unique              skip blobs whose md5 duplicates an earlier blob (dup_of)

Options:
    --mapping PATH        default vanilla_src/rebuild/mapping.json
    --src-root PATH       root that top_level_file is relative to (default vanilla_src/rebuild/src)
    --out PATH            default vanilla_src/rebuild/out  (writes <out>/<family>/<blob>.fpo + .log)
    --jobs N              default 24
    --flags "..."         override fxc flags (default: per-record fxc_args derived from the
                          vanilla RDEF compile flags, e.g. '/T ps_5_0' or '/T ps_5_0 /Zi')
    --extra "..."         extra fxc args appended to every compile (e.g. '/O3' or '/Gec')
    --no-zi               drop /Zi even where vanilla used it

Only the per-record 'cmdline_defines' are passed with /D; 'wrapper_defines' are
expected to be set inside the top-level wrapper file itself.
Writes <out>/compile_report.json (or --report PATH).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import fxcasm  # noqa: E402


def select(mapping: dict, a) -> list:
    out = []
    rx = re.compile(a.match) if a.match else None
    for n, r in mapping.items():
        if a.names and n not in a.names: continue
        if rx and not rx.search(n): continue
        if a.family and r["family"] != a.family: continue
        if a.status and r.get("status") != a.status: continue
        if a.unique and r.get("dup_of"): continue
        out.append(n)
    return out


def build_cmd(fxc, rec, src_root, out_fpo, a):
    src = os.path.join(src_root, rec["top_level_file"])
    if a.flags is not None:
        flags = shlex.split(a.flags, posix=False)
    else:
        flags = list(rec.get("fxc_args") or ["/T", "ps_5_0"])
    if a.no_zi:
        flags = [f for f in flags if f != "/Zi"]
    cmd = [fxc, "/nologo"] + flags + ["/E", rec["entry_point"]]
    for d in rec.get("cmdline_defines", []):
        cmd += ["/D", d]
    if a.extra:
        cmd += shlex.split(a.extra, posix=False)
    cmd += ["/Fo", out_fpo, src]
    return cmd, src


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mapping", default=os.path.join(REPO, "vanilla_src", "rebuild", "mapping.json"))
    ap.add_argument("--src-root", default=os.path.join(REPO, "vanilla_src", "rebuild", "src"))
    ap.add_argument("--out", default=os.path.join(REPO, "vanilla_src", "rebuild", "out"))
    ap.add_argument("--names", nargs="*")
    ap.add_argument("--names-file", help="file with one blob name per line (added to --names)")
    ap.add_argument("--match")
    ap.add_argument("--family")
    ap.add_argument("--status")
    ap.add_argument("--unique", action="store_true")
    ap.add_argument("--jobs", type=int, default=24)
    ap.add_argument("--flags", default=None)
    ap.add_argument("--extra", default=None)
    ap.add_argument("--no-zi", action="store_true")
    ap.add_argument("--fxc", default=fxcasm.fxc_path())
    ap.add_argument("--report", default=None, help="compile report path (default <out>/compile_report.json)")
    a = ap.parse_args()
    if a.names_file:
        a.names = (a.names or []) + [l.strip() for l in open(a.names_file, encoding="utf-8") if l.strip()]

    mapping = json.load(open(a.mapping, encoding="utf-8"))
    names = select(mapping, a)
    if not names:
        print("nothing selected"); return 1

    def job(n):
        rec = mapping[n]
        d = os.path.join(a.out, rec["family"])
        os.makedirs(d, exist_ok=True)
        out_fpo = os.path.join(d, n + ".fpo")
        cmd, src = build_cmd(a.fxc, rec, a.src_root, out_fpo, a)
        res = {"name": n, "cmd": subprocess.list2cmdline(cmd), "out": out_fpo}
        if not os.path.exists(src):
            res.update(ok=False, error=f"missing source {rec['top_level_file']}")
            return res
        if os.path.exists(out_fpo):
            os.remove(out_fpo)
        t = time.time()
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        res["seconds"] = round(time.time() - t, 2)
        res["ok"] = p.returncode == 0 and os.path.exists(out_fpo)
        log = (p.stdout or "") + (p.stderr or "")
        with open(os.path.join(d, n + ".log"), "w", encoding="utf-8") as fh:
            fh.write(res["cmd"] + "\n\n" + log)
        res["warnings"] = len(re.findall(r"warning X\d+", log))
        if not res["ok"]:
            errs = [l for l in log.splitlines() if "error" in l]
            res["error"] = errs[:5] if errs else log[-500:]
        return res

    t0 = time.time()
    with ThreadPoolExecutor(a.jobs) as ex:
        results = list(ex.map(job, names))
    ok = sum(r["ok"] for r in results)
    rep = {"selected": len(names), "ok": ok, "failed": len(names) - ok,
           "wall_seconds": round(time.time() - t0, 1), "results": results}
    os.makedirs(a.out, exist_ok=True)
    rep_path = a.report or os.path.join(a.out, "compile_report.json")
    os.makedirs(os.path.dirname(os.path.abspath(rep_path)), exist_ok=True)
    with open(rep_path, "w", encoding="utf-8") as fh:
        json.dump(rep, fh, indent=1)
    print(f"compiled {ok}/{len(names)} ok in {rep['wall_seconds']}s -> {a.out}")
    for r in results:
        if not r["ok"]:
            print("  FAIL", r["name"], r.get("error"))
    return 0 if ok == len(names) else 2


if __name__ == "__main__":
    sys.exit(main())
