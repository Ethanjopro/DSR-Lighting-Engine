"""Shared DrawParam helpers: load vanilla BNDs and address row fields by paramdef internal name."""
from __future__ import annotations

from functools import cache
from pathlib import Path

from soulstruct.darksouls1r.params.draw_param import DrawParamBND

ROOT = Path(__file__).resolve().parents[1]
VANILLA_DRAWPARAM = ROOT / "vanilla" / "param" / "DrawParam"
GAME_DIR = Path(r"C:\Program Files (x86)\Steam\steamapps\common\DARK SOULS REMASTERED")
AREAS = [f"a{n}" for n in range(10, 19)]


def load_vanilla(area: str) -> DrawParamBND:
    return DrawParamBND.from_path(VANILLA_DRAWPARAM / f"{area}_DrawParam.parambnd.dcx")


@cache
def internal_to_attr(row_type: type) -> dict[str, str]:
    """Map paramdef internal names (e.g. 'saturation') to Soulstruct attributes (e.g. 'SaturationCorrection')."""
    return {meta.internal_name: attr for attr, meta in row_type.get_all_field_metadata().items()}


def get_field(row, internal_name: str):
    return getattr(row, internal_to_attr(type(row))[internal_name])


def set_field(row, internal_name: str, value) -> None:
    setattr(row, internal_to_attr(type(row))[internal_name], value)


def iter_params(bnd: DrawParamBND):
    """Yield (slot, bank_stem, DrawParam) for every present param in both slots."""
    for slot, params in enumerate((bnd.draw_params_0, bnd.draw_params_1)):
        for stem, param in params.items():
            if param is not None:
                yield slot, stem, param
