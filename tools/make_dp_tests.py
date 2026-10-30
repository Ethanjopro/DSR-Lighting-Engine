"""Build DrawParam-only test variants for shader A/B sessions (no shader files inside, so the shaders loaded at
the last restart stay in use while capture_set cycles these by warping):

    pin_van     exposure pinned (make_sun_dp.pin), otherwise vanilla: the reference for feature A/Bs
    pin_van_b   identical copy of pin_van: noise between warps
    pin_slb0    pin_van + every s_LightBank colA field and env multiplier set to 0 (both slots).
                If this matches pin_van, the engine never reads s_LightBank (looks/README.md P2).

    .venv\\Scripts\\python.exe tools\\make_dp_tests.py            -> dist\\_dptest\\<variant>\\param\\DrawParam\\...
"""
from __future__ import annotations

import json
import shutil

from dp import AREAS, ROOT, iter_params, load_vanilla, set_field
from make_sun_dp import pin

OUT = ROOT / "dist" / "_dptest"
SLB_ZERO = ["colA_0", "colA_1", "colA_2", "colA_u", "colA_d", "colA_s", "envDif_colA", "envSpc_colA"]


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    report = {}
    for name, zero_slb in (("pin_van", False), ("pin_van_b", False), ("pin_slb0", True)):
        n = 0
        for area in AREAS:
            bnd = load_vanilla(area)
            for _slot, stem, param in iter_params(bnd):
                for row in param.rows.values():
                    pin(stem, row)
                    if zero_slb and stem == "s_LightBank":
                        for f in SLB_ZERO:
                            set_field(row, f, 0)
                        n += 1
            dst = OUT / name / "param" / "DrawParam" / f"{area}_DrawParam.parambnd.dcx"
            dst.parent.mkdir(parents=True, exist_ok=True)
            bnd.write(dst)
        report[name] = {"s_lightbank_rows_zeroed": n}
    (OUT.parent / "_dptest_report.json").write_text(json.dumps(report, indent=1))  # capture_set rejects extra files
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
