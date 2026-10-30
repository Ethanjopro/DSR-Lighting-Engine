"""Spike A test build: set saturation=0 in every ToneCorrect row (slots 0/1) of a10..a18.

Writes to the given output dir; install/restore is done by the caller. Dev-only, never shipped.
"""
import sys
from pathlib import Path

from dp import AREAS, iter_params, load_vanilla, set_field

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
for area in AREAS:
    bnd = load_vanilla(area)
    changed = 0
    for _, stem, param in iter_params(bnd):
        if stem != "ToneCorrectBank":
            continue
        for row in param.rows.values():
            set_field(row, "saturation", 0.0)
            changed += 1
    bnd.write(out / f"{area}_DrawParam.parambnd.dcx")
    print(f"{area}: {changed} ToneCorrect rows desaturated")
