"""Check a capture run against the per-bonfire look targets in looks/targets.json.

For every bonfire it compares <slug>.stats.json of the run with the same file in the baseline and reports
PASS / WARN / FAIL / SKIP per check, then the cross-area pair constraints (exposure jumps at transitions).

Usage:
  python tools/check_looks.py captures/<run>                  every variant folder found under the run
  python tools/check_looks.py captures/<run> --variant tuned  only that variant (relative path under the run)
  python tools/check_looks.py captures/r1/tuned captures/r2/tuned --repeat   average repeated captures
  options: --baseline DIR [DIR ...] (default captures/baseline_vanilla/vanilla; several = averaged repeats)
           --targets PATH, --group G1 [G2 ...], --slug s [...], --priority high [...]
           --crops      also compute the character-crop checks from the PNGs (needs numpy; averaged over
                        repeat captures; a bonfire may set its own "char_box": [x0, y0, x1, y1])
           --json OUT   write the full report as JSON
           --verbose    list passing checks too
Exit code 1 if any FAIL (SKIP and WARN do not fail).

Metric definitions follow tools/input/DsrInput.cs StatsJson (sRGB decode, step 2, HUD masks). The baseline should be
the re-baseline (frozen shaders + vanilla DrawParam) once it exists; see looks/README.md.
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = ROOT / "captures" / "baseline_vanilla" / "vanilla"
DEFAULT_TARGETS = ROOT / "looks" / "targets.json"

# Character / environment crops (3840x2160 frame). CHAR stops above the rest-at-bonfire prompt.
CHAR_BOX = (1650, 880, 2210, 1720)
HUD_MASKS = [(0, 0, 1700, 460), (240, 1120, 1160, 2010), (3080, 1880, 3560, 2030)]
ENV_EXCLUDE = HUD_MASKS + [(1100, 1720, 2720, 1900), (1600, 0, 2300, 2160)]
EPS = 1e-9


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def level_ev(level: float) -> float:
    """log2 of the linear value of an 8-bit luma level (floored like StatsJson's log term)."""
    return math.log2(max(srgb_to_linear(level / 255.0), 1e-5))


# ---------------------------------------------------------------------------------------------------------------
# Inputs

def find_variant_dirs(path: Path) -> list[Path]:
    """Folders at or under `path` that directly contain *.stats.json files."""
    if any(path.glob("*.stats.json")):
        return [path]
    return sorted({p.parent for p in path.rglob("*.stats.json")})


def read_stats(dirs: list[Path]) -> dict[str, dict]:
    """slug -> stats averaged over every dir that has it; '_n' = number of captures, '_dirs' = their folders."""
    acc: dict[str, list[tuple[Path, dict]]] = {}
    for d in dirs:
        for f in sorted(d.glob("*.stats.json")):
            acc.setdefault(f.name[: -len(".stats.json")], []).append((d, load_json(f)))
    out = {}
    for slug, items in acc.items():
        keys = set.intersection(*(set(k for k, v in s.items() if isinstance(v, (int, float))) for _, s in items))
        avg = {k: sum(s[k] for _, s in items) / len(items) for k in keys}
        avg["_n"] = len(items)
        avg["_dirs"] = [str(d) for d, _ in items]
        out[slug] = avg
    return out


# ---------------------------------------------------------------------------------------------------------------
# Minimal PNG reader (8-bit RGB/RGBA, non-interlaced; all five row filters) for the crop checks.

def read_png(path: Path):
    import numpy as np

    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path}: not a PNG")
    pos, idat, ihdr = 8, [], None
    while pos < len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif ctype == b"IDAT":
            idat.append(body)
        elif ctype == b"IEND":
            break
    w, h, depth, color, _, _, interlace = ihdr
    if depth != 8 or color not in (2, 6) or interlace:
        raise ValueError(f"{path}: unsupported PNG (depth {depth}, color type {color}, interlace {interlace})")
    bpp = 3 if color == 2 else 4
    stride = w * bpp
    raw = np.frombuffer(zlib.decompress(b"".join(idat)), dtype=np.uint8).reshape(h, stride + 1)
    filters, rows = raw[:, 0], raw[:, 1:]
    if not filters.any():
        img = rows.reshape(h, w, bpp)
    else:
        img = np.empty((h, stride), dtype=np.uint8)
        prev = np.zeros(stride, dtype=np.int64)
        for y in range(h):
            f, line = int(filters[y]), rows[y].astype(np.int64)
            if f == 0:
                cur = line
            elif f == 1:  # Sub: running sum per channel
                cur = np.cumsum(line.reshape(w, bpp), axis=0).reshape(-1) & 0xFF
            elif f == 2:  # Up
                cur = (line + prev) & 0xFF
            else:  # Average / Paeth depend on the reconstructed left pixel: sequential
                cur = np.zeros(stride, dtype=np.int64)
                ln, up = line.tolist(), prev.tolist()
                out = [0] * stride
                for i in range(stride):
                    a = out[i - bpp] if i >= bpp else 0
                    b = up[i]
                    if f == 3:
                        out[i] = (ln[i] + ((a + b) >> 1)) & 0xFF
                    else:
                        c = up[i - bpp] if i >= bpp else 0
                        p = a + b - c
                        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                        pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                        out[i] = (ln[i] + pred) & 0xFF
                cur = np.array(out, dtype=np.int64)
            img[y] = cur
            prev = cur
        img = img.reshape(h, w, bpp)
    return img[:, :, :3]


def crop_evs(png: Path, char_box=CHAR_BOX) -> dict:
    """char_ev / env_ev = log2(mean linear luminance) of the character box and of the frame without HUD, prompt and
    character column, sampled every 2nd pixel like StatsJson."""
    import numpy as np

    rgb = read_png(png)
    h, w, _ = rgb.shape
    lut = np.array([srgb_to_linear(i / 255.0) for i in range(256)])
    ys, xs = np.arange(0, h, 2), np.arange(0, w, 2)
    sub = rgb[::2, ::2]
    lum = 0.2126 * lut[sub[:, :, 0]] + 0.7152 * lut[sub[:, :, 1]] + 0.0722 * lut[sub[:, :, 2]]

    def mask(boxes):
        m = np.zeros(lum.shape, dtype=bool)
        for x0, y0, x1, y1 in boxes:
            m[np.ix_((ys >= y0) & (ys < y1), (xs >= x0) & (xs < x1))] = True
        return m

    sx, sy = w / 3840.0, h / 2160.0
    scale = lambda boxes: [(x0 * sx, y0 * sy, x1 * sx, y1 * sy) for x0, y0, x1, y1 in boxes]
    char = mask(scale([tuple(char_box)]))
    env = ~mask(scale(ENV_EXCLUDE))
    return {"char_ev": math.log2(max(float(lum[char].mean()), 1e-9)),
            "env_ev": math.log2(max(float(lum[env].mean()), 1e-9))}


# ---------------------------------------------------------------------------------------------------------------
# Checks

def in_range(value, rng) -> bool:
    lo, hi = rng
    return (lo is None or value >= lo - EPS) and (hi is None or value <= hi + EPS)


def fmt_range(rng) -> str:
    lo, hi = rng
    num = lambda v: f"{v:+.4g}"
    return f"[{'-inf' if lo is None else num(lo)}, {'+inf' if hi is None else num(hi)}]"


def exposure_delta(t: dict, new: dict, base: dict) -> tuple[str, float]:
    if t.get("exposure_metric") == "p50":
        return "p50_ev", level_ev(new["p50"]) - level_ev(base["p50"])
    return "ev", new["ev"] - base["ev"]


def check_bonfire(slug: str, t: dict, new: dict, base: dict, crops: dict | None) -> list[dict]:
    res = []
    enf = {"ev": True, "sat": True, "oklab": True, "char": True, **t.get("enforce", {})}

    def add(name, value, rng, level="fail", note=""):
        ok = in_range(value, rng)
        res.append({"check": name, "value": round(value, 5), "range": rng, "status": "PASS" if ok else level.upper(),
                    **({"note": note} if note else {})})

    def skip(name, why):
        res.append({"check": name, "status": "SKIP", "note": why})

    metric, d = exposure_delta(t, new, base)
    if metric == "p50_ev":
        add("exposure(p50)", d, t["ev_delta"], "fail" if enf["ev"] else "warn")
        add("ev(advisory)", new["ev"] - base["ev"], t["ev_delta"], "warn", "geometric mean over-reacts in crushed views")
    else:
        add("ev", d, t["ev_delta"], "fail" if enf["ev"] else "warn", "" if enf["ev"] else "not enforced here")
    add("sat_ratio", new["mean_sat"] / max(base["mean_sat"], 1e-9), t["sat_ratio"], "fail" if enf["sat"] else "warn")
    for k, f in (("oklab_a_delta", "mid_oklab_a"), ("oklab_b_delta", "mid_oklab_b")):
        add(k[:-6], new[f] - base[f], t[k], "fail" if enf["oklab"] else "warn")
    dclip, dcrush = new["clip_pct"] - base["clip_pct"], new["crush_pct"] - base["crush_pct"]
    add("clip", dclip, [t.get("clip_min_delta"), t["clip_max_delta"]], "fail",
        "must fall" if t["clip_max_delta"] < 0 else "")
    add("crush", dcrush, [None, t["crush_max_delta"]], "fail", "must fall" if t["crush_max_delta"] < 0 else "")
    for p, floor in t.get("floors", {}).items():
        add(f"{p}_floor", new[p], [floor, None])
    for p, rng in t.get("pctl_delta", {}).items():
        add(f"{p}_delta", new[p] - base[p], rng)
    if "spread_delta" in t:
        spread = lambda s: math.log2((s["p90"] + 1) / (s["p10"] + 1))
        add("spread_delta", spread(new) - spread(base), t["spread_delta"])
    for f, rng in t.get("abs_limits", {}).items():
        add(f"{f}(abs)", new[f], rng)
    for name in ("char_ev_delta", "d_char_env"):
        if name not in t:
            continue
        if crops is None:
            skip(name, "run with --crops")
        elif "error" in crops:
            skip(name, crops["error"])
        else:
            dc = crops["new"]["char_ev"] - crops["base"]["char_ev"]
            de = crops["new"]["env_ev"] - crops["base"]["env_ev"]
            add(name, dc if name == "char_ev_delta" else dc - de, t[name], "fail" if enf["char"] else "warn",
                "" if enf["char"] else "not enforced here")
    return res


def verdict(checks: list[dict]) -> str:
    st = {c["status"] for c in checks}
    return "FAIL" if "FAIL" in st else ("WARN" if "WARN" in st else "PASS")


def crop_pair(slug: str, new: dict, base: dict, char_box=CHAR_BOX) -> dict:
    """Crop EVs averaged over every repeat capture of the view (mean of the per-capture log2 values)."""
    try:
        import numpy  # noqa: F401
    except ImportError:
        return {"error": "numpy not available"}
    pick = lambda s: [Path(d) / f"{slug}.png" for d in s["_dirs"] if (Path(d) / f"{slug}.png").exists()]
    pn, pb = pick(new), pick(base)
    if not pn or not pb:
        return {"error": "PNG missing"}
    try:
        def avg(paths):
            evs = [crop_evs(q, char_box) for q in paths]
            return {k: sum(e[k] for e in evs) / len(evs) for k in ("char_ev", "env_ev")}
        return {"new": avg(pn), "base": avg(pb)}
    except Exception as e:  # unsupported PNG etc.
        return {"error": f"crop failed: {e}"}


# ---------------------------------------------------------------------------------------------------------------

def run_variant(name: str, dirs: list[Path], base: dict, targets: dict, slugs: list[str], use_crops: bool) -> dict:
    new = read_stats(dirs)
    bon = targets["bonfires"]
    report = {"variant": name, "dirs": [str(d) for d in dirs], "bonfires": {}, "pairs": []}
    deltas = {}
    for slug in slugs:
        t = bon[slug]
        if slug not in new or slug not in base:
            report["bonfires"][slug] = {"verdict": "SKIP", "group": t["group"], "priority": t["priority"],
                                        "note": "no capture in run" if slug not in new else "no baseline capture"}
            continue
        crops = (crop_pair(slug, new[slug], base[slug], t.get("char_box", CHAR_BOX))
                 if use_crops and ("char_ev_delta" in t or "d_char_env" in t) else None)
        checks = check_bonfire(slug, t, new[slug], base[slug], crops)
        metric, d = exposure_delta(t, new[slug], base[slug])
        deltas[slug] = {"exposure": d, "metric": metric, "oklab_b": new[slug]["mid_oklab_b"] - base[slug]["mid_oklab_b"]}
        entry = {"verdict": verdict(checks), "group": t["group"], "priority": t["priority"], "captures": new[slug]["_n"],
                 "exposure_metric": metric, "checks": checks}
        if t.get("signoff"):
            entry["signoff"] = t["signoff"]
        report["bonfires"][slug] = entry
    for p in targets.get("pairs", []):
        a, b = p["a"], p["b"]
        if a not in deltas or b not in deltas:
            report["pairs"].append({**p, "status": "SKIP"})
            continue
        v = deltas[a]["exposure"] - deltas[b]["exposure"]
        report["pairs"].append({**p, "value": round(v, 4), "status": "PASS" if in_range(v, p["diff"]) else p["level"].upper()})
    for g in targets.get("same_sign", []):
        have = [s for s in g["slugs"] if s in deltas]
        if len(have) < 2:
            report["pairs"].append({**g, "status": "SKIP"})
            continue
        vals = {s: deltas[s][g["metric"]] for s in have}
        tol = g["tolerance"]
        ok = all(v <= tol for v in vals.values()) or all(v >= -tol for v in vals.values())
        report["pairs"].append({**g, "value": {s: round(v, 5) for s, v in vals.items()},
                                "status": "PASS" if ok else g["level"].upper()})
    return report


def print_report(rep: dict, verbose: bool) -> None:
    print(f"\n=== variant {rep['variant']}  ({'; '.join(rep['dirs'])})")
    cur = None
    for slug, e in rep["bonfires"].items():
        if e["group"] != cur:
            cur = e["group"]
            print(f"-- {cur}")
        if e["verdict"] == "SKIP":
            if verbose:
                print(f"  {slug:20s} SKIP  {e['note']}")
            continue
        c = {x["check"]: x for x in e["checks"]}
        expo = c.get("exposure(p50)") or c.get("ev")
        line = (f"  {slug:20s} {e['verdict']:4s}  {'p50EV' if 'exposure(p50)' in c else 'dEV'} {expo['value']:+.2f}"
                f"  sat x{c['sat_ratio']['value']:.3f}  da {c['oklab_a']['value']:+.4f}  db {c['oklab_b']['value']:+.4f}"
                f"  clip {c['clip']['value']:+.2f}  crush {c['crush']['value']:+.2f}  [{e['priority']}, n={e['captures']}]")
        print(line)
        for x in e["checks"]:
            if x["status"] == "PASS" and not verbose:
                continue
            if x["status"] == "SKIP" and not verbose:
                continue
            val = ""
            if "value" in x:
                val = f"{x['value']:+.4f} {'in' if x['status'] == 'PASS' else 'not in'} {fmt_range(x['range'])}"
            print(f"      {x['status']:4s} {x['check']:16s} {val} {x.get('note', '')}".rstrip())
        if e.get("signoff") and e["verdict"] != "FAIL":
            print(f"      NOTE sign-off needed: {e['signoff']}")
    shown = [p for p in rep["pairs"] if p["status"] != "SKIP" or verbose]
    if shown:
        print("-- pairs (delta_a - delta_b on each view's exposure metric)")
        for p in shown:
            if "slugs" in p:
                print(f"  {p['status']:4s} same sign of {p['metric']} across {', '.join(p['slugs'])}: {p.get('value', '')}")
            else:
                val = f"{p['value']:+.3f}" if "value" in p else "n/a"
                print(f"  {p['status']:4s} {p['a']} - {p['b']} = {val} in {fmt_range(p['diff'])}  ({p['why']})")
    counts = {}
    for e in rep["bonfires"].values():
        counts[e["verdict"]] = counts.get(e["verdict"], 0) + 1
    pf = sum(1 for p in rep["pairs"] if p["status"] == "FAIL")
    print("summary: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + f"; pair FAIL {pf}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("runs", nargs="+", help="capture run folder(s) or variant folder(s)")
    ap.add_argument("--variant", action="append", help="variant path relative to the run (repeatable)")
    ap.add_argument("--repeat", action="store_true", help="treat all resolved variant folders as repeats of one variant")
    ap.add_argument("--baseline", nargs="+", default=[str(DEFAULT_BASELINE)])
    ap.add_argument("--targets", default=str(DEFAULT_TARGETS))
    ap.add_argument("--group", nargs="+")
    ap.add_argument("--slug", nargs="+")
    ap.add_argument("--priority", nargs="+")
    ap.add_argument("--crops", action="store_true", help="compute character-crop checks from the PNGs (numpy)")
    ap.add_argument("--json", help="write the report as JSON")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    targets = load_json(Path(args.targets))
    bon = targets["bonfires"]
    slugs = [s for s in bon
             if (not args.group or bon[s]["group"] in args.group)
             and (not args.slug or s in args.slug)
             and (not args.priority or bon[s]["priority"] in args.priority)]
    if args.slug:
        unknown = set(args.slug) - set(bon)
        if unknown:
            print(f"unknown slug(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2

    base_dirs = [Path(b) for b in args.baseline]
    for b in base_dirs:
        if not b.is_dir():
            print(f"baseline folder not found: {b}", file=sys.stderr)
            return 2
    base = read_stats(base_dirs)

    variants: list[tuple[str, list[Path]]] = []
    for r in args.runs:
        run = Path(r)
        if not run.is_dir():
            print(f"run folder not found: {run}", file=sys.stderr)
            return 2
        for d in find_variant_dirs(run):
            rel = d.relative_to(run).as_posix() if d != run else d.name
            if args.variant and rel not in args.variant and d.name not in args.variant:
                continue
            variants.append((rel if d != run else str(run), [d]))
    if not variants:
        print("no *.stats.json found under the given run(s)", file=sys.stderr)
        return 2
    if args.repeat:
        variants = [("+".join(n for n, _ in variants), [d for _, ds in variants for d in ds])]

    print(f"targets {args.targets}\nbaseline {'; '.join(str(b) for b in base_dirs)} ({len(base)} captures)")
    reports = [run_variant(n, ds, base, targets, slugs, args.crops) for n, ds in variants]
    for rep in reports:
        print_report(rep, args.verbose)
    if args.json:
        Path(args.json).write_text(json.dumps({"targets": args.targets, "baseline": [str(b) for b in base_dirs],
                                               "reports": reports}, indent=1), encoding="utf-8")
    failed = any(e["verdict"] == "FAIL" for rep in reports for e in rep["bonfires"].values()) or \
        any(p["status"] == "FAIL" for rep in reports for p in rep["pairs"])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
