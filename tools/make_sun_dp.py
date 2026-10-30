"""Build DrawParam variants for the sun recheck: colA_s (the sun carrier c6) set explicitly on every
LightBank / s_LightBank row, optionally with exposure pinned so captures measure the sun and not
auto-exposure compensation. Each variant folder also carries the sun_A shader archive, so capture_set
reinstalls an identical file set per variant (shaders are only read at startup).

    .venv\\Scripts\\python.exe tools\\make_sun_dp.py            -> dist\\_sun\\<variant>\\...
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from dp import ROOT, get_field, iter_params, load_vanilla, set_field

AREAS = ["a10", "a11", "a12", "a15", "a17"]
OUT = ROOT / "dist" / "_sun"
SHADER = ROOT / "dist" / "_shader_install" / "sun_A" / "shader" / "FRPG_FlverPBL_fpo_DX11.shaderbnd.dcx"

# name -> (pin exposure?, colA_s or None for vanilla)
VARIANTS = {
    "pin_s0": (True, 0),
    "pin_s30": (True, 30),
    "pin_s58": (True, 58),     # c6 = 0.58^2.2 = 0.30, x gain 2 = 0.60 = the shader cap
    "pin_s0b": (True, 0),      # repeat of pin_s0: noise between warps
    "live_s58": (False, 58),   # normal exposure, for judging the look
}


def pin(stem: str, row) -> None:
    if stem == "ToneMapBank":
        for f, v in (("grayKeyValue", 0.18), ("minAdaptedLum", 0.18), ("maxAdapredLum", 0.18),
                     ("bloomMul", 0.0), ("bloomMulFar", 0.0), ("lightShaftPower", 0.0)):
            set_field(row, f, type(get_field(row, f))(v))
    elif stem == "DofBank":
        for f, v in (("farDofMul", 0), ("nearDofMul", 0), ("farDofBegin", 999999.0), ("farDofEnd", 999999.0),
                     ("nearDofBegin", 0.0), ("nearDofEnd", 0.0)):
            set_field(row, f, type(get_field(row, f))(v))
    elif stem == "LensFlareBank":
        set_field(row, "colA", type(get_field(row, "colA"))(0))


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    report = {}
    for name, (pinned, col_a) in VARIANTS.items():
        n = 0
        for area in AREAS:
            bnd = load_vanilla(area)
            for _slot, stem, param in iter_params(bnd):
                for row in param.rows.values():
                    if pinned:
                        pin(stem, row)
                    if col_a is not None and stem in ("LightBank", "s_LightBank"):
                        set_field(row, "colA_s", col_a)
                        for f in ("colR_s", "colG_s", "colB_s"):
                            set_field(row, f, 255)
                        n += 1
            dst = OUT / name / "param" / "DrawParam" / f"{area}_DrawParam.parambnd.dcx"
            dst.parent.mkdir(parents=True, exist_ok=True)
            bnd.write(dst)
        sh = OUT / name / "shader" / SHADER.name
        sh.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SHADER, sh)
        report[name] = {"pinned": pinned, "colA_s": col_a, "light_rows_set": n}
    (OUT.parent / "_sun_report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
