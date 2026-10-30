"""Generic helpers for working with fxc (D3DCompiler) shader blobs and their
text disassembly (`fxc /dumpbin /Fc`).

Nothing in here is specific to any game: it parses the standard reflection
comment header that fxc writes (Buffer Definitions, Resource Bindings,
Input/Output signature) and the SM4/SM5 instruction stream.
"""
from __future__ import annotations

import os
import re
import subprocess
import hashlib
from collections import Counter
from dataclasses import dataclass, field

FXC_DEFAULT = r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\fxc.exe"


def fxc_path() -> str:
    return os.environ.get("FXC", FXC_DEFAULT)


def disassemble(blob_path: str, out_asm: str | None = None, fxc: str | None = None) -> str:
    """Disassemble a compiled blob with fxc /dumpbin. Returns asm text and
    optionally writes it to out_asm."""
    fxc = fxc or fxc_path()
    tmp = out_asm or (blob_path + ".tmp.asm")
    r = subprocess.run([fxc, "/nologo", "/dumpbin", "/Fc", tmp, blob_path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"fxc /dumpbin failed for {blob_path}: {r.stderr.strip()}")
    with open(tmp, encoding="utf-8", errors="replace") as fh:
        txt = fh.read()
    if out_asm is None:
        os.remove(tmp)
    return txt


def cached_disasm(blob_path: str, cache_dir: str) -> str:
    """Disassemble with an on-disk cache keyed by blob content hash."""
    with open(blob_path, "rb") as fh:
        h = hashlib.sha1(fh.read()).hexdigest()
    os.makedirs(cache_dir, exist_ok=True)
    p = os.path.join(cache_dir, h + ".asm")
    if os.path.exists(p):
        with open(p, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    # unique temp name + atomic rename: identical blobs may be disassembled
    # concurrently by several threads
    import threading
    tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
    txt = disassemble(blob_path, tmp)
    try:
        os.replace(tmp, p)
    except OSError:
        pass
    return txt


# --------------------------------------------------------------------------
# Raw DXBC container
# --------------------------------------------------------------------------

D3DCOMPILE_FLAGS = [
    (0x1, "DEBUG"), (0x2, "SKIP_VALIDATION"), (0x4, "SKIP_OPTIMIZATION"),
    (0x8, "PACK_MATRIX_ROW_MAJOR"), (0x10, "PACK_MATRIX_COLUMN_MAJOR"),
    (0x20, "PARTIAL_PRECISION"), (0x40, "FORCE_VS_SOFTWARE_NO_OPT"),
    (0x80, "FORCE_PS_SOFTWARE_NO_OPT"), (0x100, "NO_PRESHADER"),
    (0x200, "AVOID_FLOW_CONTROL"), (0x400, "PREFER_FLOW_CONTROL"),
    (0x800, "ENABLE_STRICTNESS"), (0x1000, "ENABLE_BACKWARDS_COMPATIBILITY"),
    (0x2000, "IEEE_STRICTNESS"), (0x8000, "OPTIMIZATION_LEVEL3"),
    (0x10000, "RESERVED16"), (0x20000, "RESERVED17"), (0x40000, "WARNINGS_ARE_ERRORS"),
    (0x80000, "RESOURCES_MAY_ALIAS"), (0x100000, "ENABLE_UNBOUNDED_DESCRIPTOR_TABLES"),
    (0x200000, "ALL_RESOURCES_BOUND"), (0x400000, "DEBUG_NAME_FOR_SOURCE"),
    (0x800000, "DEBUG_NAME_FOR_BINARY"),
]


def dxbc_chunks(data: bytes) -> dict:
    """Return {fourcc: bytes} for a DXBC container."""
    import struct
    if data[:4] != b"DXBC":
        raise ValueError("not a DXBC container")
    n = struct.unpack_from("<I", data, 28)[0]
    offs = struct.unpack_from(f"<{n}I", data, 32)
    out = {}
    for o in offs:
        cc = data[o:o + 4].decode("latin-1")
        sz = struct.unpack_from("<I", data, o + 4)[0]
        out.setdefault(cc, data[o + 8:o + 8 + sz])
    return out


def rdef_flags(data: bytes) -> int | None:
    """D3DCOMPILE_* flags stored in the RDEF chunk (uint32 at +0x14; +0x10 is the target version)."""
    import struct
    ch = dxbc_chunks(data).get("RDEF")
    if ch is None or len(ch) < 0x18:
        return None
    return struct.unpack_from("<I", ch, 0x14)[0]


def decode_flags(f: int | None) -> dict:
    if f is None:
        return {"raw": None}
    names = [n for b, n in D3DCOMPILE_FLAGS if f & b]
    lvl = f & 0xC004
    opt = {0x4: "Od", 0x4000: "O0", 0x0: "O1", 0xC000: "O2", 0x8000: "O3"}.get(lvl & 0xC000 if not f & 4 else 0x4, "?")
    if not f & 4:
        opt = {0x4000: "O0", 0x0: "O1", 0xC000: "O2", 0x8000: "O3"}[f & 0xC000]
    fx = []
    if f & 0x1: fx.append("/Zi")
    if f & 0x4: fx.append("/Od")
    elif opt != "O1": fx.append("/" + opt)
    if f & 0x8: fx.append("/Zpr")
    if f & 0x10: fx.append("/Zpc")
    if f & 0x20: fx.append("/Gpp")
    if f & 0x200: fx.append("/Gfa")
    if f & 0x400: fx.append("/Gfp")
    if f & 0x800: fx.append("/Ges")
    if f & 0x1000: fx.append("/Gec")
    if f & 0x2000: fx.append("/Gis")
    if f & 0x40000: fx.append("/WX")
    if f & 0x80000: fx.append("/res_may_alias")
    if f & 0x200000: fx.append("/all_resources_bound")
    return {"raw": f"0x{f:08x}", "names": names, "opt": opt, "fxc_args": fx}


# --------------------------------------------------------------------------
# Reflection header parsing
# --------------------------------------------------------------------------

@dataclass
class CBVar:
    name: str
    type: str
    offset: int
    size: int
    used: bool


@dataclass
class CBuffer:
    name: str
    kind: str  # 'cbuffer' or 'struct' (resource bind info)
    vars: list = field(default_factory=list)


@dataclass
class Binding:
    name: str
    type: str
    format: str
    dim: str
    bind: str
    count: int


@dataclass
class SigElem:
    name: str
    index: int
    mask: str
    reg: int
    sysval: str
    fmt: str
    used: str


@dataclass
class ShaderInfo:
    compiler: str = ""
    cbuffers: list = field(default_factory=list)
    bindings: list = field(default_factory=list)
    isgn: list = field(default_factory=list)
    osgn: list = field(default_factory=list)
    profile: str = ""
    decls: list = field(default_factory=list)
    instrs: list = field(default_factory=list)   # raw instruction lines (no decls)
    global_flags: str = ""
    temps: int = 0
    has_debug_lines: bool = False
    stats: dict = field(default_factory=dict)


_VAR_RE = re.compile(
    r"^//\s+(?P<type>(?:row_major\s+|column_major\s+)?[A-Za-z_][\w]*)\s+"
    r"(?P<name>[\w$]+(?:\[\d+\])?);\s*//\s*Offset:\s*(?P<off>\d+)"
    r"(?:\s+Size:\s*(?P<size>\d+))?(?P<rest>.*)$")
_SIG_RE = re.compile(
    r"^//\s+(?P<name>\w+)\s+(?P<idx>\d+)\s+(?P<mask>[xyzw ]{1,4}?)\s+(?P<reg>\d+|N/A)\s+"
    r"(?P<sv>\w+)\s+(?P<fmt>\w+)\s*(?P<used>[xyzw ]*)$")
_BIND_RE = re.compile(
    r"^//\s+(?P<name>[\w$]+)\s+(?P<type>\w+)\s+(?P<fmt>\S+)\s+(?P<dim>\S+)\s+"
    r"(?P<bind>\w+)\s+(?P<count>\d+)\s*$")
_STATS_RE = re.compile(r"^//\s*(?:Approximately\s+)?(\d+)\s+instruction slots used")

DECL_PREFIXES = ("dcl_",)


def parse_asm(txt: str) -> ShaderInfo:
    si = ShaderInfo()
    lines = txt.splitlines()
    section = None
    cur_cb = None
    for ln in lines:
        s = ln.rstrip()
        if not s.startswith("//"):
            # body
            t = s.strip()
            if not t:
                continue
            if t.startswith("#line"):
                si.has_debug_lines = True
                continue
            # strip trailing comments
            t = t.split("//", 1)[0].strip()
            if not t:
                continue
            if re.match(r"^(ps|vs|gs|hs|ds|cs)_\d_\d$", t):
                si.profile = t
                continue
            if t.startswith("dcl_globalFlags"):
                si.global_flags = t[len("dcl_globalFlags"):].strip()
                si.decls.append(t)
                continue
            if t.startswith("dcl_temps"):
                si.temps = int(t.split()[1])
                si.decls.append(t)
                continue
            if t.startswith(DECL_PREFIXES) or t.startswith("{") or t.startswith("}") or \
                    re.match(r"^[-\d.eE+]+,?$", t) or t.startswith("dcl_immediateConstantBuffer"):
                si.decls.append(t)
                continue
            si.instrs.append(t)
            continue
        c = s[2:].strip()
        if c.startswith("Generated by"):
            si.compiler = c[len("Generated by"):].strip()
            continue
        if c.startswith("Buffer Definitions"):
            section = "cb"; continue
        if c.startswith("Resource Bindings"):
            section = "bind"; continue
        if c.startswith("Input signature"):
            section = "isgn"; continue
        if c.startswith("Output signature"):
            section = "osgn"; continue
        if c.startswith("Patch Constant signature"):
            section = "pcsg"; continue
        m = _STATS_RE.match(s)
        if m:
            si.stats["slots"] = int(m.group(1))
            continue
        if section == "cb":
            m = re.match(r"^(cbuffer|tbuffer)\s+([\w$]+)", c)
            if m:
                cur_cb = CBuffer(m.group(2), m.group(1)); si.cbuffers.append(cur_cb); continue
            m = re.match(r"^Resource bind info for\s+([\w$]+)", c)
            if m:
                cur_cb = CBuffer(m.group(1), "struct"); si.cbuffers.append(cur_cb); continue
            m = _VAR_RE.match(s)
            if m and cur_cb is not None:
                cur_cb.vars.append(CBVar(m.group("name"), m.group("type"), int(m.group("off")),
                                         int(m.group("size") or 0), "[unused]" not in (m.group("rest") or "")))
        elif section == "bind":
            m = _BIND_RE.match(s)
            if m and not m.group("name").startswith("Name") and not set(m.group("name")) <= {"-"}:
                si.bindings.append(Binding(m.group("name"), m.group("type"), m.group("fmt"),
                                           m.group("dim"), m.group("bind"), int(m.group("count"))))
        elif section in ("isgn", "osgn"):
            m = _SIG_RE.match(s)
            if m and m.group("name") != "Name":
                e = SigElem(m.group("name"), int(m.group("idx")), m.group("mask").strip(),
                            -1 if m.group("reg") == "N/A" else int(m.group("reg")),
                            m.group("sv"), m.group("fmt"), m.group("used").strip())
                (si.isgn if section == "isgn" else si.osgn).append(e)
    return si


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

_REG_RE = re.compile(r"\b(r|v|o|x|t|s|u|cb|icb|l|CB|vThread\w*)(\d+)\b")


def opcode(instr: str) -> str:
    return instr.split()[0] if instr else ""


def normalize_instr(instr: str, mode: str = "strict") -> str:
    """Normalise one instruction for comparison.

    strict: abstract only temp-register numbers (r12 -> r#, x3[..] -> x#[..]);
            constant-buffer indices, literals, swizzles, inputs/outputs and
            resource slots are kept, since they carry the semantics.
    loose:  additionally drop write masks/swizzles and literal values.
    """
    t = re.sub(r"\b(r|x)\d+\b", r"\1#", instr)
    if mode == "loose":
        t = re.sub(r"l\([^)]*\)", "l()", t)
        t = re.sub(r"(\b(?:r#|v\d+|o\d+|cb\d+\[[^\]]*\]|t\d+|icb\[[^\]]*\]))\.[xyzw]{1,4}", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()


def normalized_stream(si: "ShaderInfo", mode: str = "strict") -> list:
    return [normalize_instr(i, mode) for i in si.instrs]


def opcode_multiset(si: ShaderInfo) -> Counter:
    return Counter(opcode(i) for i in si.instrs)


def multiset_similarity(a: Counter, b: Counter) -> float:
    """Weighted Jaccard / Ruzicka similarity of two multisets in [0,1]."""
    keys = set(a) | set(b)
    if not keys:
        return 1.0
    num = sum(min(a[k], b[k]) for k in keys)
    den = sum(max(a[k], b[k]) for k in keys)
    return num / den if den else 1.0


def lcs_ratio(a: list, b: list) -> float:
    """Sequence similarity via difflib (order-sensitive)."""
    import difflib
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


# --------------------------------------------------------------------------
# Interface extraction
# --------------------------------------------------------------------------

def interface(si: ShaderInfo, used_only: bool = True) -> dict:
    """Comparable interface description."""
    cbs = {}
    for cb in si.cbuffers:
        vs = [(v.name, v.type, v.offset, v.size) for v in cb.vars if (v.used or not used_only)]
        if cb.kind == "struct":
            vs = [(v.name, v.type, v.offset, v.size) for v in cb.vars]
        cbs[cb.name] = vs
    binds = sorted((b.name, b.type, b.dim, b.bind, b.count) for b in si.bindings)
    # register-level view (ignores names): what slots are declared in the code
    slots = sorted(d for d in si.decls if d.startswith(("dcl_sampler", "dcl_resource", "dcl_constantbuffer")))
    return {
        "isgn": [(e.name, e.index, e.mask, e.reg, e.sysval, e.fmt, e.used) for e in si.isgn],
        "osgn": [(e.name, e.index, e.mask, e.reg, e.sysval, e.fmt, e.used) for e in si.osgn],
        "cbuffers": cbs,
        "bindings": binds,
        "slots": slots,
        "global_flags": si.global_flags,
    }


def cb_layout_signature(si: ShaderInfo, cbname: str = "$Globals") -> str:
    """Hash of the full declared layout (names, types, offsets) of a cbuffer,
    ignoring the used/unused flag. Identifies the header generation a blob was
    compiled against."""
    for cb in si.cbuffers:
        if cb.name == cbname:
            s = "|".join(f"{v.type} {v.name}@{v.offset}" for v in cb.vars)
            return hashlib.sha1(s.encode()).hexdigest()[:10]
    return "none"
