#!/usr/bin/env python3
"""Build the DXBC checksum -> entry name table the DLL uses to identify the game's shaders.

Reads the blobs extracted from the game's own archives (vanilla_src/blobs/<archive>/<entry>) and
writes one line per unique checksum: <32 hex>\t<archive>\t<entry>[|<other entries with the same bytes>].
"""
import argparse
import pathlib
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--blobs", default="vanilla_src/blobs")
    ap.add_argument("--out", default="engine/data/shader_names.tsv")
    a = ap.parse_args()

    table: dict[str, tuple[str, list[str]]] = {}
    skipped = 0
    for path in sorted(pathlib.Path(a.blobs).rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if data[:4] != b"DXBC":
            skipped += 1
            continue
        key = data[4:20].hex()
        archive = path.relative_to(a.blobs).parts[0]
        entry = table.setdefault(key, (archive, []))
        entry[1].append(path.name)

    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="ascii", newline="\n") as f:
        for key in sorted(table):
            archive, names = table[key]
            f.write(f"{key}\t{archive}\t{'|'.join(names)}\n")
    print(f"{len(table)} unique shaders, {sum(len(v[1]) for v in table.values())} entries, {skipped} non-DXBC files skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
