# DSR Lighting Engine

A lighting engine for Dark Souls Remastered (PC, DX11), in the manner of the DS2 Lighting Engine: a `d3d11.dll` proxy replaces the game's lighting at runtime while keeping each area's art direction.

**Status: 2.0 beta, under the user's look review.** The design and the measured frame are in [docs/PLAN_v3.md](docs/PLAN_v3.md). Look questions that come down to taste are tabled in [docs/TASTE_QUESTIONS.md](docs/TASTE_QUESTIONS.md). Its user README is [release/README.txt](release/README.txt). The 1.0 loose-file release this grew out of is described in [docs/PLAN.md](docs/PLAN.md).

## What the engine does
- **GTAO** in place of the game's SSAO, written into the game's own AO texture (`engine/src/gtao.cpp`).
- **AO on point lights** and **sun contact shadows** (engine versions of the forward shaders, `shaders/features/dsrle_features.fxh`, swapped in at runtime from `dsrle/shaders.pak`).
- **World sun shadows**: depth prepass draws replayed into four stable cascades (`engine/src/sunshadow.cpp`). Only surfaces with no baked light receive them, except where a setting says otherwise.
- **Sun table**: each area's sun colour and strength (`spec/sun_rows.toml`, built by `tools/build_v4.py`).
- **Torch and fire shadows**: real-time cube shadows for the nearest lights (`engine/src/pointshadow.cpp`, world store in `engine/src/casters.cpp`), and a longer reach for the game's small lights (`engine/src/lightreach.cpp`).
- **Volumetrics**: light in the air around the game's clustered lights, over vanilla's per-object fog (`engine/src/volumetrics.cpp`).
- **Reflections and bounce light**: screen-space reflections (`engine/src/reflect.cpp`) and screen-space one-bounce light (`engine/src/ssgi.cpp`).
- **Ambient**: bent normals from the GTAO pass's horizons, and light bouncing inside creases, in `DSRLE_ENGINE_BENT` (`shaders/features/dsrle_features.fxh`).
- **Image**: wide bloom (`engine/src/bloom.cpp`), and in the composite (`engine/data/hlsl/composite.hlsl`) a highlight roll-off, filmic contrast that leaves near black alone, a lift in very dark scenes and dither.
- **Native HDR**: a flip-model scRGB swap chain on an HDR desktop, an extended image from the composite, and a replacement for the game's last blit (`engine/src/hdr.cpp`, `composite_hdr.hlsl`, `hdr_final.hlsl`). Off, or on an SDR desktop, the game's own path runs.
- **Per-map settings**: a few settings take their own values in four maps (`engine/data/maps.ini`). The engine tells the maps apart from the sun direction and light rows that the forward draws carry, matched against a table that `tools/maps_table.py --emit` builds from the shipped DrawParam (`engine/src/maps.cpp`, rules in `spec/maps.toml`). Unknown or shared views keep the global values.
- From 1.0: the per-area DrawParam retune (`spec/areas/*.toml`), a real-time sun on non-lightmapped draws, soft shadow filtering, specular occlusion, smoother point-light falloff, and a sharper TAA resolve.

- **Quality levels and safe fallbacks**: `[engine] quality` caps the costly settings by level, picked from the adapter's video memory when 0 (`engine/src/config.cpp`). The shipped `dsrle.ini` is compiled into the DLL as the defaults, so a partial or missing file works. With the game's Ambient Occlusion setting off the engine has no place to run and steps aside; when its own AO cannot run the game's SSAO is let through (`engine/src/engine.cpp`).
- **Notices**: a few seconds of text on the back buffer for the version, F10 and a forced turn-off (`engine/src/osd.cpp`).

F10 flips the whole engine on and off in game; `dsrle\dsrle.ini` reloads live.

## What the install changes
- Adds `d3d11.dll` and a `dsrle` folder to the game folder. **This is code running in the game process.**
- Replaces `param/DrawParam/a10..a18_DrawParam.parambnd.dcx`, with backups.

It leaves GameParam, map layout, events, saves, the executable and the game's shader archives alone. It patches no game code and evades nothing. Online safety is not claimed: play offline. What the DLL reads, writes and loads, and how to report a security problem, is in [SECURITY.md](SECURITY.md).

## What is not in this repository
No game files. These are created on your own PC from your own copy of the game, and are git-ignored:
- `vanilla/` (pristine copies of the files the mod changes) and `vanilla_src/` (shader source recovered from the game's debug data, with the scripts that rebuild it).
- `data/vanilla/`, `data/live_rows.json` and `data/row_names_ja.json`, which `tools/extract.py` writes, and `data/row_names_en.json`, a translation of the last.
- `engine/data/shaders.pak` and `engine/data/maps_detect.txt`, which the build steps below make.
- `saves/`, `captures/`, `dist/` and `release/build/`.

What the repository does hold about the game is names, checksums and measurements: the game's shader file names with their checksums (`engine/data/shader_names.tsv`), hash lists of the vanilla files (`vanilla/manifest.sha256`, `data/frozen_shaders.json`, `data/tpup_baseline.sha256`), DrawParam row numbers with some of the game's row names quoted in `looks/` and `docs/`, and the measurement tables in `data/`.

So the DLL builds from a clone as it is, while the DrawParam and shader-pack steps need those local folders first. The shader-pack build drives scripts kept in the ignored `vanilla_src/` tree and cannot be run from this repository alone.

## Building a release
```
.venv\Scripts\python.exe tools\build.py --out dist\_w4\r3_full                 # DrawParam from spec/areas
.venv\Scripts\python.exe tools\validate.py --dist dist\_w4\r3_full             # must report 0 errors, 0 warnings
.venv\Scripts\python.exe shaders\tools\build_taa_variants.py                   # the TAA resolve the pack takes
.venv\Scripts\python.exe shaders\tools\build_pack.py                           # engine/data/shaders.pak
.\engine\build.ps1                                                             # engine/bin/d3d11.dll
.venv\Scripts\python.exe tools\build_v4.py --out dist\_v4\sun                  # the 2.0 DrawParam: 1.0 specs plus the sun table
.venv\Scripts\python.exe tools\maps_table.py --emit engine\data\maps_detect.txt --dp dist\_v4\sun   # refuses on any leak
.venv\Scripts\python.exe tools\package.py --dp dist\_v4\sun --version 2.0.0-beta8
```
The version is `DSRLE_VERSION` in `engine/src/version.h`; `package.py` refuses another one, and refuses a `dsrle.ini` newer than the DLL that has it built in.
`engine/data/maps_detect.txt` is derived from the DrawParam, so it is git-ignored and must be emitted from the same build that is packaged; `package.py` checks the hashes.
The shader builds need the git-ignored `vanilla_src/` tree, which is recovered locally from the game's own files.

## Evidence
| File | What it records |
|---|---|
| `data/sweep_sensitivity.json` | the effect of each DrawParam lever on exposure |
| `data/probe_constants.json` | which shader constant each DrawParam field drives |
| `data/session3_ab.json` | shader feature A/B; `s_LightBank` is not read by the engine |
| `data/filter_census.json` | the live tonemap (HDR_PBL_ColAdj), anti-aliasing (TAA) and AO (compute SSAO) passes |
| `looks/README.md`, `looks/G1..G5.md`, `looks/targets.json` | the look model and the per-bonfire targets |

## Testing tools
- `tools/test_session.ps1` swaps the test save in, then restores and hash-checks the real save and removes the DLL. Everything below runs inside it. It works from `saves/ledger.json`, a local record of the real save's hash and backup, and finds the save folder itself (`DSRLE_SAVE_DIR` overrides it).
- `tools/engine_ab.ps1` compares settings variants in one game session (they reload live); `tools/engine_soak.ps1` warps through every bonfire with the engine on. Sessions run in SDR unless given `-Hdr`, because the swap chain is made when the game starts. HDR shots are scRGB: read them with `dds2png.shot_image` (the same 8-bit image an SDR shot holds) and `engine/tools/hdr_stats.py` (nits, and a map of the highlights).
- When a run's state detection fails, its confirm presses can reach the game: one failed run kindled the Sen's bonfire in the test save, which changes that area's shots. Copies of the test save from before each session are in `saves/backup`.
- `tools/maps_session.ps1` warps through the bonfires with per-map detection logging but not applying (`[maps] apply = 0`), and marks each arrival and turn in the log; `engine_ab.ps1` turns per-map values off in every variant unless given `-Maps`.
- `engine/tools/ab_sheet.py` builds side-by-sides; `engine/tools/trace_report.py` and `dds2png.py` read a frame trace (`dsrle\trigger_trace`).
- Screens come from the DLL's back-buffer dump (`dsrle\trigger_shot`). **Do not judge from desktop captures on this machine:** the desktop is HDR with Auto HDR on the game, so GDI returns a brighter, clipped image. Everything captured that way before 2026-10-31, including `looks/targets.json`, is skewed.
- `tools/shader_session.ps1`, `tools/capture_set.ps1`, `tools/taa_burst_set.ps1`, `tools/check_looks.py`, `tools/ab_report.py` and `tools/contact_sheet.py` are from the 1.0 loose-file work.

## Repository layout
| Path | Contents |
|---|---|
| `vanilla/` | Pristine copies of the files we change. Only `manifest.sha256` is committed. |
| `vanilla_src/` | Shader source recovered from the game's own debug data. Git-ignored and never distributed. |
| `data/` | The measurements above. The tables extracted from the game (`data/vanilla/`, `live_rows.json`, `row_names_*.json`) are git-ignored. |
| `spec/` | The mod's lighting changes, written relative to vanilla, and the global rules (`spec/global.toml`). |
| `looks/` | Per-area look bibles and numeric targets. |
| `engine/` | The DLL: `src/` C++, `data/` what installs into `dsrle\`, `tools/` trace and build helpers, `tests/` standalone probes. |
| `shaders/` | Shader hooks, features, the filter rebuild and the builds. |
| `tools/` | Build, validate, package and capture scripts. |
| `release/` | The installer, uninstaller and user README; `release/build/` holds the built packages (ignored). |
| `dist/` | Build output, laid out like the game folder (ignored). |

## Compatibility
- Works alongside DSR-TPUP texture packs and UI mods.
- Conflicts with other DrawParam or shader mods, such as DSR Restored Lighting, DSR Shader Overhaul and Visual Overhaul.
