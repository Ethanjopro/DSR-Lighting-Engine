"""Recover embedded HLSL from DSR shader archives (SPDB debug chunks) and dump raw DXBC blobs.

Everything written here is FromSoftware/QLOC material: output goes to vanilla_src/ (git-ignored) only.

  vanilla_src/blobs/<archive>/<shader>.(fpo|vpo|cpo|...)   raw DXBC for every program
  vanilla_src/hlsl/<relative source path>                   recovered source files (deduplicated)
  vanilla_src/spdb_index.json                                which program embeds which source files

Usage: python tools/spdb_extract.py
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

from soulstruct.containers import Binder

ROOT = Path(__file__).resolve().parents[1]
SHADER_DIR = ROOT / "vanilla" / "shader"
OUT = ROOT / "vanilla_src"
MSF_MAGIC = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\0\0\0"


def dxbc_chunks(blob: bytes) -> dict[str, bytes]:
    if blob[:4] != b"DXBC":
        return {}
    count = struct.unpack_from("<I", blob, 28)[0]
    chunks = {}
    for i in range(count):
        off = struct.unpack_from("<I", blob, 32 + 4 * i)[0]
        fourcc = blob[off:off + 4].decode("ascii", "replace")
        size = struct.unpack_from("<I", blob, off + 4)[0]
        chunks[fourcc] = blob[off + 8:off + 8 + size]
    return chunks


class Msf:
    """Minimal MSF 7.00 (PDB) reader: streams by index."""

    def __init__(self, data: bytes):
        if not data.startswith(MSF_MAGIC):
            raise ValueError("not an MSF 7.00 file")
        self.data = data
        self.block_size, _, _, dir_bytes, _, block_map_addr = struct.unpack_from("<6I", data, 32)
        n_dir_blocks = -(-dir_bytes // self.block_size)
        dir_block_ids = struct.unpack_from(f"<{n_dir_blocks}I", data, block_map_addr * self.block_size)
        directory = b"".join(self._block(b) for b in dir_block_ids)[:dir_bytes]
        n_streams = struct.unpack_from("<I", directory, 0)[0]
        sizes = struct.unpack_from(f"<{n_streams}I", directory, 4)
        pos = 4 + 4 * n_streams
        self.streams = []
        for size in sizes:
            if size == 0xFFFFFFFF:
                self.streams.append(b"")
                continue
            nb = -(-size // self.block_size)
            blocks = struct.unpack_from(f"<{nb}I", directory, pos)
            pos += 4 * nb
            self.streams.append(b"".join(self._block(b) for b in blocks)[:size])

    def _block(self, i: int) -> bytes:
        return self.data[i * self.block_size:(i + 1) * self.block_size]

    def named_streams(self) -> dict[str, int]:
        """Parse the PDB info stream's named stream map (name -> stream index)."""
        s = self.streams[1]
        pos = 4 + 4 + 4 + 16  # version, signature, age, guid
        buf_size = struct.unpack_from("<I", s, pos)[0]; pos += 4
        strings = s[pos:pos + buf_size]; pos += buf_size
        size, capacity = struct.unpack_from("<II", s, pos); pos += 8
        n_present = struct.unpack_from("<I", s, pos)[0]; pos += 4
        present_words = struct.unpack_from(f"<{n_present}I", s, pos); pos += 4 * n_present
        n_deleted = struct.unpack_from("<I", s, pos)[0]; pos += 4 + 4 * n_deleted
        out = {}
        for bucket in range(capacity):
            word, bit = divmod(bucket, 32)
            if word < len(present_words) and present_words[word] >> bit & 1:
                key, value = struct.unpack_from("<II", s, pos); pos += 8
                end = strings.index(b"\0", key)
                out[strings[key:end].decode("utf-8", "replace")] = value
        return out


def rel_source_path(name: str) -> str:
    """'/src/files/c:\\...\\shader\\frpg_flverpbl\\x.fx' -> 'frpg_flverpbl/x.fx' (lowercase, under Shader)."""
    p = name.removeprefix("/src/files/").replace("\\", "/").lower()
    return p.split("/shader/", 1)[1] if "/shader/" in p else p.rsplit("/", 1)[-1]


def main():
    (OUT / "hlsl").mkdir(parents=True, exist_ok=True)
    index = {}
    sources: dict[str, bytes] = {}
    for bnd_path in sorted(SHADER_DIR.glob("*_DX11.shaderbnd.dcx")):
        archive = bnd_path.name.split(".")[0]
        blob_dir = OUT / "blobs" / archive
        blob_dir.mkdir(parents=True, exist_ok=True)
        binder = Binder.from_path(bnd_path)
        n_src = 0
        for entry in binder.entries:
            name = entry.name.rsplit("\\", 1)[-1]
            data = entry.get_uncompressed_data()
            (blob_dir / name).write_bytes(data)
            spdb = dxbc_chunks(data).get("SPDB")
            if not spdb:
                continue
            msf = Msf(spdb)
            files = []
            for stream_name, idx in msf.named_streams().items():
                if not stream_name.startswith("/src/files/"):
                    continue
                rel = rel_source_path(stream_name)
                text = msf.streams[idx]
                if rel in sources and sources[rel] != text:
                    rel_alt = rel + f".{name}.variant"
                    sources[rel_alt] = text
                    files.append(rel_alt)
                else:
                    sources[rel] = text
                    files.append(rel)
            index[f"{archive}/{name}"] = sorted(files)
            n_src += 1
        print(f"{archive}: {len(binder.entries)} programs, {n_src} with embedded source")
    for rel, text in sources.items():
        p = OUT / "hlsl" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text)
    (OUT / "spdb_index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    print(f"{len(sources)} source files recovered -> {OUT / 'hlsl'}")
    for rel in sorted(sources):
        print(f"  {rel}  ({len(sources[rel])} bytes)")


if __name__ == "__main__":
    main()
