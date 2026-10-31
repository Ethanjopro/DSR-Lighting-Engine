# DSR Lighting Engine

A lighting overhaul for Dark Souls Remastered (PC, DX11). It retunes the game's per-area lighting data (DrawParam) and upgrades the renderer's shaders. The goal is to keep the original release's area moods while lighting them with a modern model.

**Status: 1.0.0** (2026-10-30). The release is `release/build/DSR_Lighting_Engine-1.0.0.zip`; build it with the steps below. Its user README is [release/README.txt](release/README.txt). The original plan is in [docs/PLAN.md](docs/PLAN.md).

## What 1.0 contains
- **Per-area lighting** (`spec/areas/*.toml`, 288 field changes over a10..a18). It was drafted from the look bibles (`looks/`) and refined over three capture rounds against `looks/targets.json`.
- **FlverPBL shader** (`data/frozen_shaders.json`): a real-time sun on non-lightmapped draws, with per-row strength via LightBank `colA_s` and snow, water and lava excluded. Also soft shadows (16-tap rotated PCF plus a cascade cross-fade), specular occlusion and smoother point-light falloff.
- **TAA resolve** (`FRPG_Filter_DX11`): a Catmull-Rom history fetch and contrast-adaptive sharpening.

## What the install changes
Only loose, client-side visual files:
- `param/DrawParam/a10..a18_DrawParam.parambnd.dcx`
- `shader/FRPG_FlverPBL_fpo_DX11.shaderbnd.dcx`
- `shader/FRPG_Filter_DX11.shaderbnd.dcx`

It never touches GameParam, map layout (MSB), events, saves, the executable or any DLL, so it is in the same category as texture mods. Online safety is still not guaranteed. The shipped shader archives carry no debug chunks (`shaders/tools/strip_debug.py`).

## Building a release
```
.venv\Scripts\python.exe tools\build.py --out dist\_w4\r3_full                 # DrawParam from spec/areas
.venv\Scripts\python.exe tools\validate.py --dist dist\_w4\r3_full             # must report 0 errors, 0 warnings
.venv\Scripts\python.exe shaders\tools\build_features.py                       # FlverPBL (all_sunA = frozen build)
.venv\Scripts\python.exe shaders\tools\build_taa_variants.py                   # Filter (fil_taa_all)
.venv\Scripts\python.exe tools\package.py --dp dist\_w4\r3_full --shaders dist\_shader_install\final_v1 --version 1.0.0
```
`dist\_shader_install\final_v1` holds the frozen FlverPBL archive and the `fil_taa_all` Filter archive. The shader builds need the git-ignored `vanilla_src/` tree, which is recovered locally from the game's own files.

## Evidence
| File | What it records |
|---|---|
| `data/sweep_sensitivity.json` | the effect of each DrawParam lever on exposure |
| `data/probe_constants.json` | which shader constant each DrawParam field drives |
| `data/session3_ab.json` | shader feature A/B; `s_LightBank` is not read by the engine |
| `data/filter_census.json` | the live tonemap (HDR_PBL_ColAdj), anti-aliasing (TAA) and AO (compute SSAO) passes |
| `looks/README.md`, `looks/G1..G5.md`, `looks/targets.json` | the look model and the per-bonfire targets |

## Testing tools
- `tools/test_session.ps1` swaps the test save in, then restores and hash-checks the real save.
- `tools/shader_session.ps1` restarts the game per shader variant.
- `tools/capture_set.ps1` warps to bonfires and captures them.
- `tools/taa_burst_set.ps1` captures standstill and pan bursts.
- `tools/check_looks.py` grades captures against the targets; `tools/ab_report.py` and `tools/contact_sheet.py` compare variants.

## Repository layout
| Path | Contents |
|---|---|
| `vanilla/` | Pristine copies of the files we change. Only `manifest.sha256` is committed. |
| `vanilla_src/` | Shader source recovered from the game's own debug data. Git-ignored and never distributed. |
| `data/` | Vanilla tables, live-row maps and the measurements above. |
| `spec/` | The mod's lighting changes, written relative to vanilla, and the global rules (`spec/global.toml`). |
| `looks/` | Per-area look bibles and numeric targets. |
| `shaders/` | Shader hooks, features, the filter rebuild and the builds. |
| `tools/` | Build, validate, package and capture scripts. |
| `release/` | The installer, uninstaller and user README; `release/build/` holds the built packages (ignored). |
| `dist/` | Build output, laid out like the game folder (ignored). |

## Compatibility
- Works alongside DSR-TPUP texture packs and UI mods.
- Conflicts with other DrawParam or shader mods, such as DSR Restored Lighting, DSR Shader Overhaul and Visual Overhaul.
