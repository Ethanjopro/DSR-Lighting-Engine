"""Surgical, verifiable patches of compiled SM4/SM5 shader bytecode (the DXBC SHEX chunk).

Used to build diagnostic variants that keep a shader's vanilla behaviour and interface and only add a
small, known gate at its outputs:

  ps_output_gate(blob, gate, tint)
      Pixel shader: every write to o0 is redirected to a fresh temp rN (same token size: only the operand
      type and register index change), and before every ret/retc a short block computes
          o0.<written mask> = rN * (gate(uv) ? tint : 1)
      gate is evaluated from the TEXCOORD input the shader already declares and reads (no new inputs, so
      ISGN/OSGN stay byte-identical). Other outputs (o1, ...) are untouched.
        ("rect", x0, y0, x1, y1)      inside = x0 <= u < x1 and y0 <= v < y1
        ("stripes_u", cycles)         inside = frac(u * cycles) < 0.5          (vertical stripes)

  cs_store_gate(blob, uav, axis, shift, comps)
      Compute shader: before every store_uav_typed to u<uav> the stored value is copied to a fresh temp
      and the components in `comps` are forced to 0 where ((coord.<axis>) >> shift) & 1 == 1
      (axis 'x', 'y', or 'x+y' for diagonal stripes; coord = the store's own address operand).

Every other chunk of the container (RDEF, ISGN, OSGN, STAT, ...) is kept byte-identical; only SHEX changes
and the container checksum is recomputed. STAT is deliberately left stale (it is informational only).

Token encodings used here were checked against fxc 10 output (see make_filter_census.py's verification,
which disassembles every patched blob with fxc /dumpbin and compares it with the expected stream).

Generic: nothing game specific, no shader source is read or emitted.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

import dxbc

# opcodes (D3D10/11 SB) used or recognised here
OP_ADD, OP_AND, OP_FRC, OP_GE, OP_IADD, OP_LT, OP_MOV, OP_MOVC, OP_MUL = 0, 1, 26, 29, 30, 49, 54, 55, 56
OP_CUSTOMDATA, OP_RET, OP_RETC, OP_USHR = 53, 62, 63, 85
OP_DCL_INPUT_PS, OP_DCL_TEMPS = 98, 104
OP_STORE_UAV_TYPED = 164
DECL_OPCODES = set(range(88, 107)) | {113} | set(range(143, 163)) | {OP_CUSTOMDATA}
OPNAME = {OP_ADD: "add", OP_AND: "and", OP_FRC: "frc", OP_GE: "ge", OP_IADD: "iadd", OP_LT: "lt",
          OP_MOV: "mov", OP_MOVC: "movc", OP_MUL: "mul", OP_USHR: "ushr", OP_RET: "ret",
          OP_STORE_UAV_TYPED: "store_uav_typed"}

# operand types
T_TEMP, T_INPUT, T_OUTPUT, T_IMM32, T_IMM64, T_UAV = 0, 1, 2, 4, 5, 30
SW = "xyzw"


# ---------------------------------------------------------------------------
# token stream


@dataclass
class Instr:
    toks: list[int]

    @property
    def opcode(self) -> int:
        return self.toks[0] & 0x7FF

    @property
    def is_decl(self) -> bool:
        return self.opcode in DECL_OPCODES


@dataclass
class Operand:
    start: int          # token index inside Instr.toks
    end: int            # one past the last token
    otype: int
    ncomp: int          # 0 / 1 / 2(=4 components) / 3(=N)
    sel_mode: int       # 0 mask, 1 swizzle, 2 select-1 (4-component operands only)
    sel: int            # raw bits 4..11
    indices: list       # immediate index values (None for relative)

    def components(self) -> list[int]:
        """Source components selected, in order x,y,z,w of the swizzle (4-comp only)."""
        if self.ncomp != 2:
            return [0]
        if self.sel_mode == 1:
            return [(self.sel >> (2 * i)) & 3 for i in range(4)]
        if self.sel_mode == 2:
            return [self.sel & 3] * 4
        return [i for i in range(4) if self.sel >> i & 1]

    def mask(self) -> int:
        if self.ncomp != 2:
            return 1
        if self.sel_mode == 0:
            return self.sel & 0xF
        raise ValueError("operand is not mask-selected")


def parse_shex(data: bytes) -> tuple[int, list[Instr]]:
    n = len(data) // 4
    toks = list(struct.unpack(f"<{n}I", data[:4 * n]))
    if toks[1] != n:
        raise ValueError(f"SHEX length token {toks[1]} != {n} dwords")
    out, i = [], 2
    while i < n:
        t = toks[i]
        ln = toks[i + 1] if (t & 0x7FF) == OP_CUSTOMDATA else (t >> 24) & 0x7F
        if ln <= 0 or i + ln > n:
            raise ValueError(f"bad instruction length {ln} at dword {i}")
        out.append(Instr(toks[i:i + ln]))
        i += ln
    return toks[0], out


def build_shex(version: int, instrs: list[Instr]) -> bytes:
    body = [t for ins in instrs for t in ins.toks]
    return struct.pack(f"<{len(body) + 2}I", version, len(body) + 2, *body)


def _parse_operand(toks: list[int], pos: int) -> tuple[Operand, int]:
    start = pos
    t = toks[pos]; pos += 1
    ext = t >> 31
    while ext:
        ext = toks[pos] >> 31; pos += 1
    ncomp = t & 3
    otype = (t >> 12) & 0xFF
    dim = (t >> 20) & 3
    indices = []
    for d in range(dim):
        rep = (t >> (22 + 3 * d)) & 7
        if rep == 0:
            indices.append(toks[pos]); pos += 1
        elif rep == 1:
            indices.append(toks[pos] | toks[pos + 1] << 32); pos += 2
        elif rep in (2, 3, 4):
            pos += {2: 0, 3: 1, 4: 2}[rep]
            _, pos = _parse_operand(toks, pos)
            indices.append(None)
        else:
            raise ValueError(f"unknown index representation {rep}")
    if otype == T_IMM32:
        pos += {0: 0, 1: 1, 2: 4}[ncomp]
    elif otype == T_IMM64:
        pos += {1: 2, 2: 8}[ncomp]
    sel_mode = (t >> 2) & 3 if ncomp == 2 else 0
    sel = (t >> 4) & 0xFF if ncomp == 2 else 0
    return Operand(start, pos, otype, ncomp, sel_mode, sel, indices), pos


def operands(ins: Instr) -> list[Operand]:
    """All operands of a non-declaration instruction (after the opcode and extended opcode tokens)."""
    if ins.is_decl:
        raise ValueError("declarations carry no generic operand list")
    toks = ins.toks
    pos = 1
    if toks[0] >> 31:
        while True:
            e = toks[pos]; pos += 1
            if not e >> 31:
                break
    ops = []
    while pos < len(toks):
        op, pos = _parse_operand(toks, pos)
        ops.append(op)
    if pos != len(toks):
        raise ValueError(f"operand walk overran opcode {ins.opcode}")
    return ops


# ---------------------------------------------------------------------------
# encoders (forms verified against fxc output)


def _f(v: float) -> int:
    return struct.unpack("<I", struct.pack("<f", float(v)))[0]


def dst(otype: int, reg: int, mask: str) -> list[int]:
    m = sum(1 << SW.index(c) for c in mask)
    return [2 | (m << 4) | (otype << 12) | (1 << 20), reg]


def src(otype: int, reg: int, swz: str) -> list[int]:
    if len(swz) == 1:      # select-1, as fxc emits for scalar sources
        return [2 | (2 << 2) | (SW.index(swz) << 4) | (otype << 12) | (1 << 20), reg]
    s = sum(SW.index(c) << (2 * i) for i, c in enumerate(swz))
    return [2 | (1 << 2) | (s << 4) | (otype << 12) | (1 << 20), reg]


def imm4f(*v: float) -> list[int]:
    return [0x00004002] + [_f(x) for x in v]


def imm1f(v: float) -> list[int]:
    return [0x00004001, _f(v)]


def imm1u(v: int) -> list[int]:
    return [0x00004001, v & 0xFFFFFFFF]


def reswizzle(toks: list[int], op: Operand, comp_index: int) -> list[int]:
    """Copy of a 4-component source operand that replicates its comp_index-th selected component."""
    if op.ncomp != 2:
        raise ValueError("can only reswizzle 4-component operands")
    c = op.components()[comp_index]
    out = toks[op.start:op.end]
    out[0] = (out[0] & ~0xFFC) | (1 << 2) | ((c * 0x55) << 4)
    return out


def instr(opc: int, *ops: list[int], sat: bool = False) -> Instr:
    body = [t for o in ops for t in o]
    return Instr([opc | (int(sat) << 13) | ((1 + len(body)) << 24)] + body)


# ---------------------------------------------------------------------------
# container helpers


def rebuild_container(blob: bytes, new_chunks: dict[str, bytes]) -> bytes:
    chunks = [(c.fourcc, new_chunks.get(c.fourcc, c.data)) for c in dxbc.parse(blob)]
    n = len(chunks)
    off = 0x20 + 4 * n
    offs, body = [], b""
    for cc, data in chunks:
        offs.append(off + len(body))
        body += cc.encode("latin-1") + struct.pack("<I", len(data)) + data
    total = off + len(body)
    out = bytearray(b"DXBC" + b"\0" * 16 + struct.pack("<III", 1, total, n) + struct.pack(f"<{n}I", *offs) + body)
    out[4:20] = dxbc.dxbc_checksum(bytes(out))
    return bytes(out)


def _temps(instrs: list[Instr]) -> Instr:
    t = [i for i in instrs if i.opcode == OP_DCL_TEMPS]
    if len(t) != 1:
        raise ValueError(f"expected one dcl_temps, found {len(t)}")
    return t[0]


# ---------------------------------------------------------------------------
# pixel shader: gated tint of o0


def uv_input_register(blob: bytes) -> int:
    """Register of the float TEXCOORD input whose x and y the vanilla shader always reads."""
    ch = dxbc.chunk_map(blob)
    regs = [e.register for e in dxbc.parse_signature(ch["ISGN"])
            if e.name == "TEXCOORD" and e.comp_type == 3 and (e.rw & 3) == 3 and e.sysval == 0]
    if not regs:
        raise ValueError("no TEXCOORD input with x and y read")
    return regs[0]


def ps_output_gate(blob: bytes, gate: tuple, tint: tuple[float, float, float, float]) -> tuple[bytes, dict]:
    ch = dxbc.chunk_map(blob)
    kind, _, _ = dxbc.shader_version(ch)
    if kind != "ps":
        raise ValueError("not a pixel shader")
    uv = uv_input_register(blob)
    version, instrs = parse_shex(ch["SHEX"])
    dcl_in = [i for i in instrs if i.opcode == OP_DCL_INPUT_PS and i.toks[2] == uv]
    if not dcl_in or ((dcl_in[0].toks[1] >> 4) & 3) != 3:
        raise ValueError(f"v{uv}.xy is not declared as a pixel-shader input")
    tmp = _temps(instrs)
    rn, rt = tmp.toks[1], tmp.toks[1] + 1
    tmp.toks[1] += 2

    written = 0
    redirected = 0
    for ins in instrs:
        if ins.is_decl:
            continue
        for op in operands(ins):
            if op.otype == T_OUTPUT and op.indices == [0]:
                if op.ncomp != 2 or op.sel_mode != 0 or len(op.indices) != 1:
                    raise ValueError("unsupported o0 operand form")
                written |= op.mask()
                ins.toks[op.start] = (ins.toks[op.start] & ~(0xFF << 12)) | (T_TEMP << 12)
                ins.toks[op.start + 1] = rn
                redirected += 1
            elif op.otype == T_OUTPUT and None in op.indices:
                raise ValueError("relative output indexing not supported")
    if not redirected:
        raise ValueError("shader never writes o0")
    wmask = "".join(SW[i] for i in range(4) if written >> i & 1)

    if gate[0] == "rect":
        _, x0, y0, x1, y1 = gate
        block = [
            instr(OP_GE, dst(T_TEMP, rt, "xy"), src(T_INPUT, uv, "xyxx"), imm4f(x0, y0, 0, 0)),
            instr(OP_LT, dst(T_TEMP, rt, "zw"), src(T_INPUT, uv, "xxxy"), imm4f(0, 0, x1, y1)),
            instr(OP_AND, dst(T_TEMP, rt, "xy"), src(T_TEMP, rt, "xyxx"), src(T_TEMP, rt, "zwzz")),
            instr(OP_AND, dst(T_TEMP, rt, "x"), src(T_TEMP, rt, "x"), src(T_TEMP, rt, "y")),
        ]
    elif gate[0] == "stripes_u":
        _, cycles = gate
        block = [
            instr(OP_MUL, dst(T_TEMP, rt, "x"), src(T_INPUT, uv, "x"), imm1f(cycles)),
            instr(OP_FRC, dst(T_TEMP, rt, "x"), src(T_TEMP, rt, "x")),
            instr(OP_LT, dst(T_TEMP, rt, "x"), src(T_TEMP, rt, "x"), imm1f(0.5)),
        ]
    else:
        raise ValueError(f"unknown gate {gate[0]}")
    block += [
        instr(OP_MOVC, dst(T_TEMP, rt, "xyzw"), src(T_TEMP, rt, "xxxx"), imm4f(*tint), imm4f(1, 1, 1, 1)),
        instr(OP_MUL, dst(T_OUTPUT, 0, wmask), src(T_TEMP, rn, "xyzw"), src(T_TEMP, rt, "xyzw")),
    ]
    out, rets = [], 0
    for ins in instrs:
        if ins.opcode in (OP_RET, OP_RETC):
            out += [Instr(list(b.toks)) for b in block]
            rets += 1
        out.append(ins)
    shex = build_shex(version, out)
    info = {"uv_register": uv, "redirect_temp": rn, "gate_temp": rt, "o0_writes_redirected": redirected,
            "o0_written_mask": wmask, "returns_gated": rets, "block_instructions": len(block),
            "gate": list(gate), "tint": list(tint)}
    return rebuild_container(blob, {"SHEX": shex}), info


# ---------------------------------------------------------------------------
# compute shader: striped zeroing of a typed UAV store


def cs_store_gate(blob: bytes, uav: int, axis: str, shift: int, comps: str) -> tuple[bytes, dict]:
    ch = dxbc.chunk_map(blob)
    kind, _, _ = dxbc.shader_version(ch)
    if kind != "cs":
        raise ValueError("not a compute shader")
    version, instrs = parse_shex(ch["SHEX"])
    tmp = _temps(instrs)
    rb, rv = tmp.toks[1], tmp.toks[1] + 1
    tmp.toks[1] += 2
    if not comps or any(c not in SW for c in comps):
        raise ValueError(f"bad component list {comps!r}")
    out, stores = [], 0
    for ins in instrs:
        if ins.opcode == OP_STORE_UAV_TYPED:
            ops = operands(ins)
            if len(ops) != 3:
                raise ValueError("unexpected store_uav_typed operand count")
            u, coord, val = ops
            if u.otype == T_UAV and u.indices == [uav]:
                t = ins.toks
                if axis == "x":
                    pre = [instr(OP_USHR, dst(T_TEMP, rb, "x"), reswizzle(t, coord, 0), imm1u(shift))]
                elif axis == "y":
                    pre = [instr(OP_USHR, dst(T_TEMP, rb, "x"), reswizzle(t, coord, 1), imm1u(shift))]
                elif axis == "x+y":
                    pre = [instr(OP_IADD, dst(T_TEMP, rb, "x"), reswizzle(t, coord, 0), reswizzle(t, coord, 1)),
                           instr(OP_USHR, dst(T_TEMP, rb, "x"), src(T_TEMP, rb, "xxxx"), imm1u(shift))]
                else:
                    raise ValueError(f"unknown axis {axis}")
                zero = [0.0, 0.0, 0.0, 0.0]
                pre += [
                    instr(OP_AND, dst(T_TEMP, rb, "x"), src(T_TEMP, rb, "xxxx"), imm1u(1)),
                    instr(OP_MOV, dst(T_TEMP, rv, "xyzw"), t[val.start:val.end]),
                    instr(OP_MOVC, dst(T_TEMP, rv, comps), src(T_TEMP, rb, "xxxx"), imm4f(*zero), src(T_TEMP, rv, "xyzw")),
                ]
                new = Instr(t[:val.start] + src(T_TEMP, rv, "xyzw") + t[val.end:])
                new.toks[0] = (new.toks[0] & ~(0x7F << 24)) | (len(new.toks) << 24)
                out += pre
                out.append(new)
                stores += 1
                continue
        out.append(ins)
    if not stores:
        raise ValueError(f"no store_uav_typed to u{uav}")
    shex = build_shex(version, out)
    info = {"uav": uav, "axis": axis, "shift": shift, "period_px": 2 << shift, "zeroed_components": comps,
            "bit_temp": rb, "value_temp": rv, "stores_gated": stores}
    return rebuild_container(blob, {"SHEX": shex}), info
