"""Assemble the release: release/build/<name>-<version>/ (files/, manifest.json, install/uninstall scripts, README)
plus a zip of it. Inputs are a DrawParam build (tools/build.py output) and shader install folders.

    .venv\\Scripts\\python.exe tools\\package.py --dp dist --shaders dist\\_shader_install\\<frozen> [--version 1.0.0]

Every file must match the install allowlist and must differ from vanilla; manifest.json records each file's
sha256 and the vanilla sha256 it replaces (from vanilla/manifest.sha256), which install.ps1 checks.
Shader archives are copied through shaders/tools/strip_debug.py: debug chunks (SPDB etc., which embed HLSL
source in some retail blobs) are removed, so the release carries compiled code only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shaders" / "tools"))
import strip_debug  # noqa: E402
ALLOW = re.compile(r"^(param/DrawParam/a1[0-8]_DrawParam\.parambnd\.dcx|shader/FRPG_(Filter|FlverPBL_fpo)_DX11\.shaderbnd\.dcx)$")
SCRIPTS = ["install.ps1", "uninstall.ps1", "common.ps1", "install.bat", "uninstall.bat", "README.txt"]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dp", required=True, help="DrawParam build root (holds param/DrawParam/)")
    ap.add_argument("--shaders", nargs="*", default=[], help="shader install folders (each holds shader/)")
    ap.add_argument("--name", default="DSR_Lighting_Engine")
    ap.add_argument("--version", default="1.0.0")
    args = ap.parse_args()

    vanilla = {}
    for line in (ROOT / "vanilla" / "manifest.sha256").read_text(encoding="utf-8").splitlines():
        h, rel = line.split("  ", 1)
        vanilla[rel] = h

    picked: dict[str, Path] = {}
    for f in sorted((Path(args.dp) / "param" / "DrawParam").glob("*.dcx")):
        picked[f"param/DrawParam/{f.name}"] = f
    for folder in args.shaders:
        for f in sorted((Path(folder) / "shader").glob("*.dcx")):
            rel = f"shader/{f.name}"
            if rel in picked:
                raise SystemExit(f"{rel} comes from two shader folders")
            picked[rel] = f
    if not picked:
        raise SystemExit("nothing to package")

    out = ROOT / "release" / "build" / f"{args.name}-{args.version}"
    if out.exists():
        shutil.rmtree(out)
    files = []
    for rel, src in picked.items():
        if not ALLOW.fullmatch(rel):
            raise SystemExit(f"outside the install allowlist: {rel}")
        if rel not in vanilla:
            raise SystemExit(f"no vanilla hash for {rel}")
        if sha(src) == vanilla[rel]:
            raise SystemExit(f"{rel} is identical to vanilla; leave it out")
        dst = out / "files" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if rel.startswith("shader/"):
            rep = strip_debug.strip_archive(src, dst)
            print(f"{rel}: stripped debug chunks from {rep['count']} blob(s)")
        else:
            shutil.copyfile(src, dst)
        files.append({"path": rel, "sha256": sha(dst), "vanilla_sha256": vanilla[rel], "bytes": dst.stat().st_size})
    for s in SCRIPTS:
        if not (ROOT / "release" / s).exists():
            raise SystemExit(f"missing release/{s}")
        shutil.copyfile(ROOT / "release" / s, out / s)
    (out / "manifest.json").write_text(json.dumps({"name": args.name, "version": args.version, "files": files}, indent=1),
                                       encoding="utf-8")
    zpath = out.parent / (out.name + ".zip")  # not with_suffix: the version contains dots
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(out.parent).as_posix())
    print(json.dumps({"folder": str(out), "zip": str(zpath), "zip_sha256": sha(zpath),
                      "files": [(f["path"], f["sha256"][:12]) for f in files]}, indent=1))


if __name__ == "__main__":
    main()
