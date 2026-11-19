"""Assemble the release: release/build/DSR_Lighting_Engine-<version>/ (files/, manifest.json, install/uninstall scripts,
README) plus a zip of it. Inputs: engine/bin/d3d11.dll, engine/data, a DrawParam build (tools/build_v4.py output) and release/.

    .venv\\Scripts\\python.exe tools\\package.py --version 2.0.0-beta1 [--dp dist\\_v4\\sun]

It refuses, before writing anything, if an input is missing, if d3d11.dll is older than engine/src or shaders.pak older
than shaders/features, if engine/data holds a file not on the release list, if the pack carries debug chunks (SPDB embeds
HLSL source), if maps_detect.txt was emitted from another DrawParam than --dp, or if a shipped file names a local path.
Build folders stay as they are: the two changes to shipped bytes
are made on the copy (dump_unknown forced to 0 in dsrle.ini, and the local folder cut from the .pdb path in d3d11.dll).

manifest.json records each file's sha256, the vanilla sha256 of a file it replaces (null for a new file), and the vanilla
hashes of the shader archives the pack is keyed to (from vanilla/manifest.sha256). install.ps1 checks all of them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import struct
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shaders" / "tools"))
from strip_debug import has_debug  # noqa: E402

NAME = "DSR_Lighting_Engine"
DEFAULT_DP = "dist/_v4/sun"
SCRIPTS = ["install.ps1", "uninstall.ps1", "common.ps1", "install.bat", "uninstall.bat", "README.txt"]
DRAWPARAM = [f"param/DrawParam/a{n}_DrawParam.parambnd.dcx" for n in range(10, 19)]
REQUIRES_VANILLA = ["shader/FRPG_FlverPBL_fpo_DX11.shaderbnd.dcx", "shader/FRPG_Filter_DX11.shaderbnd.dcx"]
# What goes from engine/data into the game's dsrle folder. A new runtime file must be added here; until then it stops
# the build, so nothing the DLL needs is dropped silently and no stray dev file ships.
DATA_FILES = ["shaders.pak", "shader_names.tsv", "dsrle.ini", "maps.ini", "maps_detect.txt"]
EMIT = "tools/maps_table.py --emit engine/data/maps_detect.txt --dp"
HLSL_SUFFIXES = (".hlsl", ".hlsli")
NEVER = re.compile(r"\.(pdb|map|exp|lib|obj|ilk|dxbc|log|shaderbnd|shaderbnd\.dcx)$|(^|/)(vanilla_src|captures|trace|unknown)(/|$)", re.I)


def fail(message: str) -> None:
    raise SystemExit(f"refused: {message}")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read(path: Path) -> bytes:
    """An input, read once. If a build rewrites it during the read, stop rather than ship a torn file."""
    if not path.is_file():
        fail(f"missing input: {path}")
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        fail(f"{path} changed while it was being read; run again")
    if not data:
        fail(f"empty input: {path}")
    return data


def require_newer(target: Path, folder: Path, how: str) -> None:
    if not target.is_file():
        fail(f"missing input: {target}")
    sources = [f for f in folder.rglob("*") if f.is_file()]
    if not sources:
        fail(f"missing input: no files in {folder}")
    newest = max(sources, key=lambda f: f.stat().st_mtime_ns)
    if target.stat().st_mtime_ns < newest.stat().st_mtime_ns:
        fail(f"{target} is older than {newest}; {how}")


def allowlist() -> re.Pattern:
    """install.ps1's own allowlist, so the two cannot drift."""
    m = re.search(r"^\$DsrleAllow = '(.+)'\s*$", read(ROOT / "release" / "common.ps1").decode("utf-8"), re.M)
    if not m:
        fail("no $DsrleAllow line in release/common.ps1")
    return re.compile(m.group(1).replace(r"\z", r"\Z"))


def ini_values(text: bytes) -> dict[str, bytes]:
    """dsrle.ini as the DLL reads it (engine/src/config.cpp)."""
    values, section = {}, ""
    for raw in text.splitlines():
        line = raw.strip(b" \t")
        if not line or line[:1] in (b";", b"#"):
            continue
        if line[:1] == b"[":
            section = line[1:].split(b"]", 1)[0].decode("utf-8")
        elif b"=" in line:
            key, value = line.split(b"=", 1)
            key = key.strip(b" \t").decode("utf-8")
            values[f"{section}.{key}" if section else key] = re.split(rb"[;#]", value, maxsplit=1)[0].strip(b" \t")
    return values


def force_no_dumps(ini: bytes) -> bytes:
    """[dev] dump_unknown = 0, whatever the development copy says. Everything else stays byte for byte."""
    out, section = [], ""
    for line in ini.splitlines(keepends=True):
        stripped = line.strip()
        if stripped[:1] == b"[":
            section = stripped[1:].split(b"]", 1)[0].decode("utf-8")
        elif section == "dev" and re.match(rb"[ \t]*dump_unknown[ \t]*=", line):
            line = re.sub(rb"(=[ \t]*)[^ \t\r\n;#]*", rb"\g<1>0", line, count=1)
        out.append(line)
    text = b"".join(out)
    if "dev.dump_unknown" not in ini_values(text):
        text = text.rstrip(b"\r\n") + b"\n\n[dev]\ndump_unknown = 0\n"
    if ini_values(text)["dev.dump_unknown"] != b"0":
        fail("could not force [dev] dump_unknown = 0 in dsrle.ini")
    return text


def scrub_pdb_path(dll: bytes) -> bytes:
    """The linker writes the absolute .pdb path, with the builder's user name, into the DLL's debug record. Keep only
    the file name, as /PDBALTPATH:%_PDB% would. Same length; the loader never reads this record and the DLL is unsigned."""
    out = bytearray(dll)
    pos = out.find(b"RSDS")
    while pos >= 0:
        start = pos + 24  # "RSDS", GUID, age
        end = out.find(b"\0", start)
        path = bytes(out[start:end])
        if path.lower().endswith(b".pdb"):
            name = path.replace(b"/", b"\\").rsplit(b"\\", 1)[-1]
            out[start:end] = name + b"\0" * (len(path) - len(name))
        pos = out.find(b"RSDS", start)
    return bytes(out)


def check_pak(pak: bytes) -> int:
    """engine/src/pack.cpp's format. Every blob must be compiled code only: SPDB would ship recovered source."""
    if pak[:4] != b"DLPK" or len(pak) < 12:
        fail("shaders.pak has no DLPK header")
    version, count = struct.unpack_from("<II", pak, 4)
    if version != 1 or not count or 12 + count * 24 > len(pak):
        fail(f"shaders.pak: version {version}, {count} entries, {len(pak)} bytes is not usable")
    checked = set()
    for i in range(count):
        offset, size = struct.unpack_from("<II", pak, 12 + i * 24 + 16)
        if offset in checked:
            continue
        checked.add(offset)
        blob = pak[offset:offset + size]
        if len(blob) != size or blob[:4] != b"DXBC":
            fail(f"shaders.pak entry {i} is not a DXBC blob")
        if has_debug(blob):
            fail(f"shaders.pak entry {i} carries debug chunks (they can embed HLSL source)")
    return count


def check_detect(table: bytes, drawparam: dict[str, bytes], dp: str) -> None:
    """maps_detect.txt (tools/maps_table.py) is derived from one DrawParam build, and the engine turns per-map settings
    off when the hashes it records differ from the installed files. So it must match the DrawParam shipped beside it."""
    try:
        lines = table.decode("ascii").split("\n")
    except UnicodeDecodeError:
        fail("maps_detect.txt is not ASCII")
    if lines[0] != "dsrle_maps_detect 1" or lines[-2:] != ["end", ""]:
        fail(f"maps_detect.txt is not a whole format-1 table; run {EMIT} {dp}")
    recorded = dict(line.split(" ")[1:] for line in lines[1:10] if line.startswith("dp ") and line.count(" ") == 2)
    for rel, data in drawparam.items():
        area = rel.rsplit("/", 1)[-1].split("_", 1)[0]
        if recorded.get(area) != sha(data):
            fail(f"maps_detect.txt was emitted from another DrawParam ({area} differs from {dp}); run {EMIT} {dp}")


def local_path_hits(data: bytes) -> list[str]:
    low = data.lower()
    needles = ["\\users\\"]
    if len(Path.home().name) >= 4:
        needles.append(Path.home().name.lower())
    return [n for n in needles if n.encode("utf-8") in low or n.encode("utf-16-le") in low]


def main() -> None:
    ap = argparse.ArgumentParser(description="Build release/build/DSR_Lighting_Engine-<version>.zip")
    ap.add_argument("--dp", default=DEFAULT_DP, help=f"DrawParam build root, holds param/DrawParam/ (default {DEFAULT_DP})")
    ap.add_argument("--version", required=True, help="for example 2.0.0-beta1")
    args = ap.parse_args()
    if not re.fullmatch(r"[0-9][0-9A-Za-z.+-]*", args.version):
        fail(f"not a version: {args.version}")
    dp = Path(args.dp)
    if not dp.is_absolute() and not dp.exists():
        dp = ROOT / args.dp

    dll = ROOT / "engine" / "bin" / "d3d11.dll"
    data_dir = ROOT / "engine" / "data"
    require_newer(dll, ROOT / "engine" / "src", "run engine/build.ps1")
    # The DLL names its version in the log and on screen, and holds dsrle.ini as its built-in defaults.
    named = re.search(r'#define DSRLE_VERSION "([^"]+)"', read(ROOT / "engine" / "src" / "version.h").decode("utf-8"))
    if not named or named.group(1) != args.version:
        fail(f"engine/src/version.h says {named.group(1) if named else 'nothing'}, not {args.version}; change it and run engine/build.ps1")
    if (data_dir / "dsrle.ini").stat().st_mtime > dll.stat().st_mtime:
        fail("engine/data/dsrle.ini is newer than d3d11.dll, which has the older one built in; run engine/build.ps1")
    require_newer(data_dir / "shaders.pak", ROOT / "shaders" / "features", "run shaders/tools/build_pack.py")
    if not (data_dir / "maps_detect.txt").is_file():
        fail(f"missing input: {data_dir / 'maps_detect.txt'}; run {EMIT} {args.dp}")

    if not any((data_dir / "hlsl").glob("*.hlsl")):
        fail(f"missing input: no .hlsl files in {data_dir / 'hlsl'}")
    hlsl = []
    for f in sorted(data_dir.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(data_dir).as_posix()
        if re.fullmatch(r"hlsl/[^/]+", rel) and f.suffix.lower() in HLSL_SUFFIXES:
            hlsl.append(rel)
        elif rel not in DATA_FILES:
            fail(f"{f} is not on the release list. If the DLL needs it, add it to DATA_FILES in tools/package.py; if not, remove it.")

    # Everything shipped, in memory: nothing is written until every check has passed.
    payload: dict[str, bytes] = {"d3d11.dll": scrub_pdb_path(read(dll))}
    if payload["d3d11.dll"][:2] != b"MZ":
        fail(f"{dll} is not a DLL")
    for rel in DATA_FILES + hlsl:
        payload[f"dsrle/{rel}"] = read(data_dir / rel)
    payload["dsrle/dsrle.ini"] = force_no_dumps(payload["dsrle/dsrle.ini"])
    pack_entries = check_pak(payload["dsrle/shaders.pak"])

    vanilla = {}
    for line in read(ROOT / "vanilla" / "manifest.sha256").decode("utf-8").splitlines():
        h, rel = line.split("  ", 1)
        vanilla[rel] = h
    for rel in DRAWPARAM + REQUIRES_VANILLA:
        if rel not in vanilla:
            fail(f"no vanilla hash for {rel}")
    for rel in DRAWPARAM:
        payload[rel] = read(dp / rel)
        if sha(payload[rel]) == vanilla[rel]:
            fail(f"{dp / rel} is identical to vanilla; --dp must be a DrawParam build")
    extra = sorted(f.name for f in (dp / "param" / "DrawParam").glob("*") if f"param/DrawParam/{f.name}" not in DRAWPARAM)
    if extra:
        fail(f"{dp / 'param' / 'DrawParam'} holds files outside a10..a18: {extra}")
    check_detect(payload["dsrle/maps_detect.txt"], {rel: payload[rel] for rel in DRAWPARAM}, args.dp)

    scripts = {s: read(ROOT / "release" / s) for s in SCRIPTS}
    allow = allowlist()
    for rel, data in payload.items():
        if not allow.fullmatch(rel):
            fail(f"outside the install allowlist: {rel}")
        if NEVER.search(rel):
            fail(f"never shipped: {rel}")
    for rel, data in {**payload, **scripts}.items():
        hits = local_path_hits(data)
        if hits:
            fail(f"{rel} names a local path or user name: {hits}")

    # All checks passed.
    build = ROOT / "release" / "build"
    out = build / f"{NAME}-{args.version}"
    if out.exists():
        shutil.rmtree(out)
    files = []
    for rel in sorted(payload):
        dst = out / "files" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(payload[rel])
        files.append({"path": rel, "sha256": sha(payload[rel]), "vanilla_sha256": vanilla.get(rel), "bytes": len(payload[rel])})
    for s, data in scripts.items():
        (out / s).write_bytes(data)
    (out / "manifest.json").write_text(json.dumps({
        "name": NAME, "version": args.version, "files": files,
        "requires_vanilla": [{"path": rel, "sha256": vanilla[rel]} for rel in REQUIRES_VANILLA],
    }, indent=1), encoding="utf-8")

    zpath = build / f"{out.name}.zip"  # not with_suffix: the version contains dots
    tmp = build / f"{out.name}.zip.tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(build).as_posix())
    with zipfile.ZipFile(tmp) as z:
        infos = z.infolist()
        if z.testzip() is not None:
            fail(f"{tmp} is damaged")
    banned = [i.filename for i in infos if NEVER.search(i.filename)]
    if banned:
        tmp.unlink()
        fail(f"never shipped: {banned}")
    tmp.replace(zpath)

    print(f"shaders.pak: {pack_entries} entries, no debug chunks; dsrle.ini: dump_unknown = 0")
    for i in infos:
        print(f"{i.file_size:>12,}  {i.filename}")
    print(f"{sum(i.file_size for i in infos):>12,}  {len(infos)} files")
    print(f"{zpath.stat().st_size:>12,}  {zpath}")
    print(f"sha256 {sha(zpath.read_bytes())}")


if __name__ == "__main__":
    main()
