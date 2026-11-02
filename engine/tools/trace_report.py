#!/usr/bin/env python3
"""Summarise a frame trace written by the engine DLL (dsrle\\trace\\f<N>\\trace.jsonl).

Prints the frame as a pass list: runs of events that share a render-target set and a shader family,
with counts, target formats and sizes, viewports, and bound resources. --events lists every event in
a range; --cb decodes a constant buffer hash as float4 registers.
"""
import argparse
import json
import pathlib
import re
import struct

FORMATS = {
    0: "UNKNOWN", 2: "RGBA32F", 10: "RGBA16F", 11: "RGBA16", 13: "RGBA16S", 16: "RG32F", 19: "R32G8X24", 20: "D32S8", 21: "R32X8X24",
    24: "RGB10A2", 25: "RGB10A2", 26: "RG11B10F", 27: "RGBA8T", 28: "RGBA8", 29: "RGBA8_SRGB", 30: "RGBA8U", 34: "RG16F", 39: "R32T", 40: "D32F",
    41: "R32F", 42: "R32U", 44: "R24G8T", 45: "D24S8", 46: "R24X8", 47: "X24G8", 49: "RG8", 54: "R16F", 55: "D16", 56: "R16", 57: "R16U", 61: "R8", 62: "R8U",
    87: "BGRA8", 88: "BGRX8", 90: "BGRA8T", 91: "BGRA8_SRGB",
}


def fmt_name(f: int) -> str:
    return FORMATS.get(f, str(f))


def family(name: str) -> str:
    if not name:
        return "-"
    n = name.split("|")[0]
    n = re.sub(r"\.(fpo|vpo|cpo|gpo)$", "", n)
    m = re.match(r"FRPG_(Phn|Gst|Sfx|Snow|Water|Wat|FaceEye|Non|Deferred)", n)
    if m:
        return "Flver:" + m.group(1)
    return n


def load(path: pathlib.Path):
    res, views, states, layouts, cbdata, events, dumps = {}, {}, {}, {}, {}, [], {}
    with path.open(encoding="utf8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError as e:
                print("bad line:", e, line[:200])
                continue
            if "res" in o and "type" in o:
                res[o["res"]] = o
            elif "view" in o and "kind" in o:
                views[o["view"]] = o
            elif "state" in o:
                states[o["state"]] = o
            elif "layout" in o:
                layouts[o["layout"]] = o
            elif "cbdata" in o:
                cbdata[o["cbdata"]] = o
            elif "dump" in o:
                dumps.setdefault(len(events), []).append(o["dump"])
            elif "k" in o:
                o["i"] = len(events) + 1
                events.append(o)
    return res, views, states, layouts, cbdata, events, dumps


def desc_view(vid, views, res) -> str:
    v = views.get(vid)
    if not v:
        return f"v{vid}"
    r = res.get(v["res"], {})
    if r.get("type") == "tex2d":
        return f"r{v['res']}:{r['w']}x{r['h']}:{fmt_name(v['fmt'])}" + (f"[{r['arr']}]" if r.get("arr", 1) > 1 else "")
    if r.get("type") == "buffer":
        return f"r{v['res']}:buf{r['size']}/{r.get('stride', 0)}"
    return f"r{v['res']}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("trace", help="trace directory")
    ap.add_argument("--events", help="first:last event numbers to list in full")
    ap.add_argument("--cb", help="constant buffer hash to decode as float4 registers")
    ap.add_argument("--out", help="write the pass list here as well as stdout")
    a = ap.parse_args()
    d = pathlib.Path(a.trace)
    res, views, states, layouts, cbdata, events, dumps = load(d / "trace.jsonl")

    if a.cb:
        c = cbdata[a.cb]
        with (d / "cb.bin").open("rb") as f:
            f.seek(c["off"])
            raw = f.read(c["size"])
        for i in range(0, len(raw) - 15, 16):
            print(f"c{i // 16:<4}", " ".join(f"{v:12.5f}" for v in struct.unpack_from("4f", raw, i)))
        return 0

    if a.events:
        lo, hi = map(int, a.events.split(":"))
        for e in events:
            if lo <= e["i"] <= hi:
                print(json.dumps(e))
        return 0

    lines = []
    passes = []
    cur = None
    for e in events:
        k = e["k"]
        if k in ("CS",):
            key = ("CS", family(e.get("cs", "")))
        elif k in ("D", "DI", "DInst", "DIInst", "DAuto", "DIIndirect", "DIndirect"):
            key = ("G", tuple(e.get("rt", [])), e.get("ds", 0), family(e.get("ps", "")), family(e.get("vs", "")), tuple(e.get("vp", [])[:4]))
        else:
            key = ("X", k)
        if cur is None or cur["key"] != key:
            cur = {"key": key, "first": e["i"], "last": e["i"], "count": 0, "e": e, "tris": 0}
            passes.append(cur)
        cur["last"] = e["i"]
        cur["count"] += 1
        n = e.get("n")
        if n and k.startswith("D"):
            cur["tris"] += n[0] * max(n[3], 1) // 3

    for p in passes:
        e = p["e"]
        k = e["k"]
        if k == "CS":
            uav = " ".join(f"u{s}={desc_view(v, views, res)}" for s, v in e.get("uav", {}).items())
            srv = " ".join(f"t{s}={desc_view(v, views, res)}" for s, v in e.get("csrv", {}).items())
            cb = " ".join(f"b{s}" for s in e.get("ccb", {}))
            lines.append(f"#{p['first']:<6}x{p['count']:<4} CS   {e.get('cs', '')} dispatch={e.get('n')} {uav} | {srv} | {cb}")
        elif k.startswith("D"):
            rts = " ".join(desc_view(v, views, res) for v in e.get("rt", []))
            ds = desc_view(e["ds"], views, res) if e.get("ds") else "-"
            vp = e.get("vp", [])
            srv = " ".join(f"t{s}={desc_view(v, views, res)}" for s, v in e.get("psrv", {}).items())
            lines.append(f"#{p['first']:<6}x{p['count']:<4} DRAW vs={family(e.get('vs', ''))} ps={family(e.get('ps', ''))} tris={p['tris']} rt=[{rts}] ds={ds} vp={vp[:4]} bs={e.get('bs')} dss={e.get('dss')} | {srv}")
        elif k == "Present":
            lines.append(f"#{p['first']:<6}      PRESENT")
        elif k == "ExecList":
            lines.append(f"#{p['first']:<6}      --- ExecuteCommandList ctx={e.get('ctx')} thread={e.get('thread')} bytes={e.get('bytes')} {e.get('note', '')}")
        else:
            lines.append(f"#{p['first']:<6}x{p['count']:<4} {k} " + " ".join(f"{kk}={vv}" for kk, vv in e.items() if kk not in ("i", "k")) +
                         (" " + desc_view(e["v"], views, res) if "v" in e else ""))
        for at in range(p["first"], p["last"] + 1):
            if at in dumps:
                lines[-1] += "  dumps:" + ",".join(dumps[at])

    text = "\n".join(lines)
    print(text)
    print(f"\n{len(events)} events, {len(passes)} passes, {len(res)} resources, {len(views)} views, {len(cbdata)} unique CB contents")
    if a.out:
        pathlib.Path(a.out).write_text(text + "\n", encoding="utf8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
