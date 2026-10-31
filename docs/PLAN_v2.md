# DSR Lighting Engine v2: the bold pass

## Why v2
At full resolution, 1.0.0 moves the median pixel 3.7/255 against vanilla (9% of pixels change by more than 8/255). The user saw no difference and wants the game to look "remastered like DS2 lighting engine, not a minor change" (2026-10-31).

The causes:
- The 1.0 guardrails (±0.3 EV, contrast ±0.10, Oklab about 0.01) kept every change below visibility.
- The shader items that show in a screenshot (tonemap, AO, fog) were never built.
- The sun only lights non-lightmapped draws.

v2 stays within the same files as 1.0: DrawParam a10..a18, `FRPG_FlverPBL_fpo_DX11` and `FRPG_Filter_DX11`. There is no DLL tier.

## What the files can reach
- **AO.** FlverPBL multiplies the SSAO texture into `envLightComponent`, which is the lightmap/env diffuse times shadow plus hemisphere ambient (`frpg_fs_hemenv_base.fxh`, three sites). A stronger AO curve therefore shows outdoors as well as indoors. Point lights and direct specular are not touched by vanilla AO. The SSAO generator (Filter compute entries 107-115) has no rebuilt source, so the generator is a fallback only.
- **Fog.** Fog is applied per pixel with a vertex-computed factor (`CalcGetFogCol(..., gFC_FogCol, In.VecNrm.w)`, in sRGB). A pixel-side remap plus sun in-scatter is possible, coloured by each area's own DrawParam fog and sun.
- **Tonemap/grade.** `FRPG_Fil_HDR_PBL_ColAdj` is live and has exact rebuilt source. It needs a hook site (`wire_filter_hooks.py`).
- **Light shafts.** `FRPG_Fil_LightShaft` exists and is driven by DrawParam ToneMapBank `lightShaftPower/Begin/AttenRate`. Power is 0 almost everywhere, so the pass does not run. Judging it needs sun-facing views.
- **Not reachable without new passes:** true volumetric fog, shadow-casting torches, extra shadow resolution.

## Prototype (the gate)
- **Builds.**
  - FlverPBL: 1.0's `all_sunA` plus `DSRLE_AO_BOLD` and `DSRLE_FOG_BOLD`.
  - Filter: `TAA_ALL` plus `DSRLE_GRADE_BOLD`.
  - DrawParam: a `bold` profile for a10 (Undead Burg) and a13 (Catacombs).
- **One game session:** vanilla vs bold at Burg (bonfire plus a turn-around pan that includes the sun) and the Catacombs.
- **Review:** full-resolution PNGs go to the user. `check_looks.py` is used only as a crash/sanity check. The look is the user's call.
- **After approval:** the bold profile goes to every area, and the parked items follow (sun on lightmapped geometry, generator-side AO).
