# DSR Lighting Engine — plan

## Context
Build a "Lighting Engine"-style overhaul for Dark Souls Remastered (Steam, DX11). It is a deep per-area retune of the game's own lighting data plus real renderer upgrades in its shaders, keeping FromSoftware's art direction.
- It must coexist with the user's DSR-TPUP texture mod and UI mods.
- It must be no riskier online than the DS2 Lighting Engine.

**What the reference mods actually are (researched 2026-10-29)**
- **DS2LightingEngine** (Nexus darksouls2/1146, Ganaboy/RageVitamins) is a DX11 renderer replacement.
  - It ships a proxy `dxgi.dll` plus a replaced `shader` folder.
  - It adds GTAO, volumetric fog, shadowed point lights, PBR shading, LUT grading and a path-traced build.
  - It picks per-area presets from the game's original lighting data.
  - Its safety claim is "only the shader files are modified". "As safe as DS2LE" therefore means loose shader/visual files plus at most a d3d proxy DLL, and never gameplay data.
- **DS3LightingEngine** is unreleased. The shipped DS3 overhauls (fromsoftserve) are GPARAM/param + map edits.
- **DSR prior art**
  - *DSR Restored Lighting* (Nexus 1423, Dashsol): PTDE restoration via DrawParam edits, a ReShade-addon runtime, and redistributed PTDE assets. **It conflicts with this mod; don't install both.**
  - *DSR Shader Overhaul* (Altimor, GitHub `AltimorTASDK/dsr-shader-mods`, **no license**): proves FlverPBL shaders can be rebuilt from source with `fxc ps_5_0`. We use it as a reference only and copy no code.
  - DrawParam-only overhauls: Visual Overhaul, YALO, Darkest Souls.
- **How ours differs:** PTDE *moods* on a modern lighting model, all original work, no PTDE assets.

**User decisions (2026-10-29)**
- v1 = DrawParam retune + shader upgrades.
- Mostly offline. An optional ReShade preset is fine, off by default.
- Look target: **PTDE moods, DSR fidelity.**
- Testing is mostly automated. The user walks to new areas and approves looks.
- No PTDE ownership. Mood references come from public PTDE-vs-DSR comparison footage/screenshots and the user's sign-off.

## Verified facts
**Paths and configs**
- Game: `C:\Program Files (x86)\Steam\steamapps\common\DARK SOULS REMASTERED`.
- Config: `%LOCALAPPDATA%\FromSoftware\NBGI\DarkSouls\DarkSouls.ini` (WindowMode=2 borderless 3840×2160, Antialiasing=3, AO on).
- Key bindings: `DarkSoulsKeys.ini` (scan codes).
- Save: `%USERPROFILE%\Documents\NBGI\DARK SOULS REMASTERED\<Steam account folder>\DRAKS0005.sl2`. Steam Cloud is on.

**User mods**
- TPUP modifies `map/m10..m18` tpf, `chr`, `obj` and `menu_0..3`, leaving `.tpupbak` files beside them.
- DrawParam, shaders, mtd, GameParam and GI_EnvM are vanilla.
- The texture overrides contain no lightmaps or env maps, so there is no conflict.

**DrawParam layout**
- Files: `param/DrawParam/{a10..a18,a99,default}_DrawParam.parambnd.dcx`, packed as DCX zlib → BND3 → params.
- Row counts vary: most banks have 64, EnvLightTex 8, LensFlare 20. Read the counts; don't assume them.
- `a15` holds 24 params: `m15_*`, `m15_1_*`, `s15_*`, `s15_1_*`.
  - Sen's Fortress and Anor Londo share the `m15` row IDs.
  - Slot 1 (dark Anor Londo, switched by event `SetMapDrawParamSlot`) replaces the whole area.
- `s*_LightBank` differs from `m*_LightBank` in every row (same angles, different `colA`/ambient).

**Field semantics** (paramdef `paramdef/paramdef.paramdefbnd.dcx`, matching Paramdex DS1R)
- `colR/G/B` are 0-255 hue values. `colA*` is intensity in percent.
- `envDif`, `envSpc_0..3` and `resNameId_*` are **cubemap IDs: never change them**. `envDif_col*` and `envSpc_col*` are multipliers.
- FogBank `degRotW` is fog *strength* in percent.
- ShadowBank has its own `lightDegRotX/Y`.
- Auto-exposure works like exposure ≈ `grayKeyValue / clamp(lum, minAdaptedLum, maxAdapredLum)`. **Raising `minAdaptedLum` keeps dark scenes dark.** Vanilla uses 0.04 in m10 and 0.10-0.16 in m13.
- `lightShaftPower` is 0 in most rows, but m10 rows 4, 10 and 52 use small values. Those rows are calibration anchors.
- Firelink shadow `endDist` is 10.

**Row selection** (verified by parsing MSBs in memory)
- Map pieces and objects choose their own Light/Fog/Scatter rows.
- Collisions choose the scene-wide Shadow/ToneMap/ToneCorrect/Dof/LensFlare rows, plus the character Light rows and the Lantern (PointLightBank) row.
- So the live rows per map can be extracted by script. `PolyG-scn*` rows are cutscene-only.
- Placed torch/bonfire lights are SFX-driven and out of scope.

**Renderer** (QLOC slides + embedded source)
- Clustered forward with a depth prepass.
- Hable tonemap.
- 3 directional lights + clustered point lights (`LAMP_FALLOFF_QLOC`).
- Hemisphere ambient, env cubemaps, **baked lightmaps**, GGX specular.
- Shadows: perspective shadow maps, 4 cascades at `SHADOWMAP_SIZE 2048`, 9-tap PCF.
- AO has three paths: pixel `SAO_*`, compute `SAO_*`, and compute `SSAO_*` (MiniEngine-style, CPU-filled weights).
- Also present: SSS, bloom, adaptation, DOF, light shafts, FXAA/TAA.

**Shader source is only partly recoverable from our own files**
- SPDB chunks embed full commented HLSL in 12 FlverPBL `FRPG_Sfx_*` blobs, `FRPG_Fil_DepthCopy_SingleFragment` and `FRPG_Deferred_LightProbe/PointLight`.
- That covers 15 FlverPBL files: `frpg_common*.fx` (incl. `_forwardpbl`, `_tonemap`, `_vtx_*`), `frpg_shadowfunc.fx`, `frpg_fs_hemenv_base.fx` and `frpg_fs_sfx.fx`. It also covers the Deferred sources and `frpg_fil_common.fx`.
- **Top-level sources for the main families are missing.**
  - fpo families: `Phn` 929, `Gst` 684, `Sfx` 288 (source present), Water 26, Snow 25, DeferredDisney 16, other 20. The 676 vpo have no SPDB.
  - Altimor's repo shows a hand-reconstructed `FRPG_FS_HemEnv.fx` can "perfectly match" the `Phn` binaries. Reconstruction is feasible, but it is work.
- Most Filter blobs have no source but are small (rewrite from disassembly).
- All recovered source is FromSoftware/QLOC IP. It stays git-ignored and local; only compiled binaries ever ship.

**Machine**
- RTX 5090 @ 4K, 24 threads.
- .NET runtimes only (no SDK), VS 2022, Windows SDK `fxc`/`dxc`, git. No Python.

## Online safety model
- **The default install is loose visual files only:** `param/DrawParam/a10..a18_*.parambnd.dcx` and `shader/*.shaderbnd.dcx`. That is DS2LE's category, minus its DLL.
- **Never touched:** DLLs, memory, GameParam, regulation, MSB, events, saves, `default`/`a99` DrawParam.
- **Evidence for safety**
  - DSR "Data fraud has been detected" bans are reported as server-side save scans for illegal items, stats or soul memory. Our files never change save data.
  - We found no evidence of client integrity checks on DrawParam, shader or texture files.
  - The community rates visual loose files as safe; there are scattered unverified counter-claims.
  - The README will say: "same category as texture mods, no guarantee".
- **ReShade (optional tier):** the normal build disables add-on depth while there is network traffic, i.e. online. The full add-on build "isn't meant for multiplayer". Label the preset offline-oriented.
- **Process**
  - All development and testing run offline: Steam offline plus the in-game "Start offline" setting, confirmed with the user before the first launch.
  - The first online check uses a throwaway character.
  - `uninstall.ps1` restores vanilla bytes (hash-verified). Steam "verify integrity" is a second path. TPUP "Restore" never touches our files.

## Approach: engine first, then art
| Tier | What | Files | Ships |
|---|---|---|---|
| 1 | Per-area DrawParam retune: live rows, relevant banks, `m15` slot 0+1, s-LightBank in lockstep | `a10..a18_DrawParam` | v1 default |
| 2a | Post-process: tonemap retune (Hable params / hue preservation), AO quality, light-shaft sampling, CAS sharpening in the actual final AA pass (TAA vs FXAA found by census) | `FRPG_Filter_DX11` | v1 default |
| 2b | Lighting model from recovered source: shadow filtering (rotated Poisson PCF, contact hardening, cascade cross-fade at 2048), height-aware fog with sun inscatter driven by the existing Fog/Scatter constants, smoother point-light falloff + AO on point lights, specular/horizon occlusion | `FRPG_FlverPBL_fpo` (`Phn` first, then `Gst`/`Sfx`; other families stay vanilla blobs unless matched), maybe `FRPG_Deferred` | **v1 stretch, gated on Spike B**: each feature build-flagged, shipped only if it passes A/B + perf. Fog prefers a pixel-side change, because vpo has no source. |
| 4 | Optional ReShade preset (offline-oriented) | `dxgi.dll` + preset | v1 optional, off by default |
| v2 | DLL tier (4096 shadow maps, true volumetric fog), cubemap rebakes | — | later |

Shader features change the look, so they land **before** per-area tuning; otherwise retuning the curve would invalidate approved areas. Only polish (CAS strength, shaft sample counts) comes after.

### Tuning rules (`spec/global.yaml` field-class table, enforced by `validate.py`)
- **Spec format.** Edits are relative to vanilla (`mul`/`add`/`set` with absolute caps; `set` is needed for zero fields like `lightShaftPower`). Builds always regenerate from pristine vanilla.
- **Hard limits.**
  - Clamp to paramdef min/max.
  - Reject no-op edits that round back to the same s16 value.
  - Cubemap-ID fields are immutable.
- **Lightmaps limit the light rig.** Never rotate map directional lights; their intensity stays within ±20% of vanilla. The mood levers are ToneCorrect, ToneMap, Fog/Scatter and the `envDif_col`/`envSpc_col` multipliers.
- **Lockstep.**
  - Character/object Light rows and s-LightBank rows track their map rows (vanilla ratio kept to ±10%).
  - Slot-1 rows that equal slot 0 in vanilla mirror slot-0 edits.
  - ShadowBank angles keep their vanilla relation to the LightBank.
- **Grading bands** are relative to each vanilla row: |Δcontrast| ≤ 0.10 per channel, saturation 0.9-1.15× vanilla.
- **Shadow range.** `endDist` ≤ 2× vanilla, with a shimmer check (perspective shadow maps trade resolution for range).
- **Darkness.** Darken by raising `minAdaptedLum` or lowering `grayKeyValue`, never by crushing ToneCorrect. The readability floor is a cap on `grayKeyValue/minAdaptedLum` plus the capture P90 check below.
- **Capture guardrails.** Starting values, calibrated on the first two areas:
  - Geometric-mean luminance within ±0.3 EV of vanilla. Dark areas may go down to −0.8 EV, subject to the P90 floor.
  - Clipped pixels ≤ vanilla +1 point; crushed blacks ≤ vanilla +3 points.
  - Midtone Oklab a/b shifts only in the look bible's stated direction.
  - The exposure jump at an area boundary ≤ the vanilla jump + 0.3 EV.
  - Look bibles state targets as numbers.

## Repository (`this repository`, git, local/private)
- `vanilla/` holds the pristine archives + `manifest.sha256`. Binaries are ignored; the manifest is committed.
- `vanilla_src/` (**ignored**) holds the SPDB-extracted HLSL and Filter disassembly.
- `data/`
  - Vanilla tables (JSON) with translated row names (`row_names_en.json`, produced outside the script and merged by `extract.py`).
  - `live_rows.json` from **all** MSBs, including `m12_00_00_01`, with rows shared across maps flagged.
  - `locations.json`: collision → PlaceName (`msg/ENGLISH`) and nearest bonfire.
- `spec/global.yaml`, `spec/areas/<map>.yaml`, and `looks/<map>.md` (look bibles: mood, numeric targets, references, captures).
- `shaders/`
  - `hooks/`: vanilla source + `#include "dsrle_hooks.fxh"` hook points whose defaults equal vanilla, so feature work never edits shared files.
  - `features/<feat>.fxh` and `filter/<shader>.hlsl`.
  - `build.ps1`: parallel fxc over every permutation, plus a signature/cbuffer/slot/`numthreads` diff against vanilla.
- `tools/`
  - `spdb_extract.py` (our own MSF/PDB reader), `extract.py`, `build.py`, `validate.py`, `imgstats.py`.
  - `install.ps1` / `uninstall.ps1`: allowlist-only, hash-checked.
  - `capture.ps1` + `input/` (C# SendInput via `Add-Type`; DXGI Desktop Duplication capture).
- `dist/` (game-relative tree + zip), `reshade/`, `saves/` (ignored) + `saves/ledger.json` (hash ledger).
- **Tooling.**
  - Python 3.13 venv + Soulstruct 2.6. WitchyBND 3.x is the cross-check/fallback; SoulsFormats + .NET 8 SDK is the last resort.
  - `fxc` for shaders. No homegrown BND/DCX writer.
  - Every install or download is confirmed with the user at the moment it's needed.

## Execution
### Phase 0: foundation and spikes (main loop, solo)
1. **Repo and backups.** `git init` and layout. Back up vanilla archives and the save with hashes. Ask the user to **turn off Steam Cloud for DSR** for the whole project (expect a conflict prompt when it's re-enabled).
2. **Tooling.** Install Python 3.13 + Soulstruct (ask first). Round-trip gate: **semantic equality** of every DrawParam (values, row IDs, names, BND entry IDs/flags/order) plus "the game loads it". Byte identity isn't required.
3. **Spike C, capture harness (first).**
   - Confirm offline mode with the user.
   - Launch with `steam://rungameid/570940` and force the game window to the foreground.
   - Send scan codes read from `DarkSoulsKeys.ini`, with `KEYEVENTF_EXTENDEDKEY` for arrows.
   - Detect menu states from screenshots, not fixed sleeps.
   - Load the slot, hide the HUD, wait for exposure to settle, capture the median of 3 frames, then quit.
   - Save safety: hash ledger, re-copy the snapshot before each launch, restore in `finally`, check the game isn't running.
   - Fallback if scan codes fail: a virtual XInput pad, only with the user's OK.
4. **Spike A, reload rule.** Make an obvious DrawParam change and confirm it via the harness. Expected: it reloads on map load (DrawParam isn't in `InGameStay.loadlist`), while shaders need a full restart.
5. **Coverage and baselines.**
   - With the user, list the bonfires the save can reach (incl. DLC, Painted World, Kiln). The user walks to the missing ones once.
   - Snapshot `saves/<area>.sl2`.
   - Capture **vanilla baselines for every area**.
6. **Field sensitivity sweep** (one indoor + one outdoor area): push each key field to an extreme and measure. This confirms the exposure sign, which LightBank fields hit level vs characters, and how s-LightBank rows are picked. Dark Anor Londo is tested via a dev-only build that copies `m15_1` into slot 0.
7. **Spike B, shader rebuild fidelity.**
   - SPDB-extract the source.
   - **Reconstruct the missing top-level `Phn` source.** Use `frpg_fs_hemenv_base.fx` + `FRPG_FS_Sfx.fx` as the template. Infer defines from permutation names. Iterate until each permutation's disassembly matches vanilla (instruction-level diff, an `fxc /dumpbin` script). Then do the same for `Gst`, or keep `Gst` vanilla.
   - Build the hook scaffold on the matched source.
   - Rebuild the matched families **unmodified**: the diff must pass, and a zero-change rebuild must match the vanilla captures at 3 bonfires.
   - Then confirm a deliberate tint loads.
   - Same for one Filter shader rewritten from disassembly. `HDR_PBL` and `_ColAdj` must match exactly on neutral ToneCorrect and honor the per-area `inverseToneMapMul`.
   - **Tint census:** distinct tints per candidate shader identify which AO path and which final AA pass actually run.
   - If `Phn` can't be matched within a reasonable effort, 2b moves to v2 and 2a continues.
8. **Extraction.** `extract.py` produces tables, live rows, locations and the untranslated row-name list. The main loop translates that list (or a 1-agent haiku step), and `extract.py` merges the resulting JSON.
- **Exit criteria:** semantic round-trip OK; reload rule known; harness produces A/B pairs; baselines for all reachable areas; sweep notes; rebuild fidelity proven (or 2b deferred).

### Ultracode workflows (~25 agents total)
- **Model policy.** Main model for art direction, HLSL and synthesis; `sonnet` for critique and screenshot review; `haiku`/`effort:'low'` only for translation. Validators and builds are scripts that authoring agents run themselves, not separate agents.
- **Area groups.** One agent owns one group's files, so there are no conflicts and no worktrees.
  - G1 = m10 (Depths, Burg/Parish, Firelink)
  - G2 = m11+m12 (Painted World, Darkroot, Oolacile)
  - G3 = m13+m14 (Catacombs, Tomb, Ash Lake, Blighttown, Izalith)
  - G4 = m15 slot 0+1 (Sen's, Anor Londo, dark AL)
  - G5 = m16+m17+m18 (New Londo, Duke's/Crystal Cave, Kiln, Asylum)

| WF | Shape | Agents |
|---|---|---|
| WS shaders-core (after Phase 0) | pipeline over features (tonemap, AO, shadows, fog, point lights, spec occlusion). Each: main-model author analyzes vanilla code, writes `features/*.fxh` or `filter/*.hlsl`, runs `build.ps1` itself → 1 sonnet reviewer over all patches. Then a serial in-game A/B + perf per feature (main loop) | 7 |
| W1 look-bible + global model | pipeline G1-G5: main writes the look bible (numeric targets, PTDE references from public comparisons) → barrier → 1 sonnet critic (cross-area consistency, missing rows) → 1 main synthesizes `global.yaml` | 7 |
| W3 tune | pipeline G1-G5: main authors `spec/areas/*.yaml`, iterating on `validate.py` itself → 1 sonnet art-direction skeptic tries to refute each group → authors apply fixes (resume) | 6 |
| W4 screenshot-review (per round) | 1 sonnet critic over all A/B pairs + `imgstats` + continuity → spec deltas applied in the main loop | 1/round |
| W6 release-review | 1 sonnet agent: allowlist/safety audit, install/uninstall round-trip on a scratch copy, README + compatibility notes | 1 |

**Order:** Phase 0 → WS (+ serial per-feature A/B **and one all-features-on integration A/B**) → **freeze shaders + re-baseline** → W1 → W3 → capture → W4 (loop until the user approves each area) → shader polish (CAS, shaft samples; main loop) → optional ReShade preset → W6.

**Re-baseline step.** Recapture every area with vanilla DrawParam + the frozen new shaders. These captures are the reference for the W3/W4 guardrails ("±0.3 EV" etc.), so shader-induced shifts aren't blamed on param edits. The Phase-0 vanilla captures stay the reference for the zero-change-rebuild gate and for the final user-facing before/after.

## Verification (end to end)
- **Data.** `validate.py` passes: every live row is covered or explicitly vanilla, the field-class rules hold, and the `dist/` file list equals the allowlist.
- **Shaders.** The interface diff passes for every rebuilt blob, the zero-change rebuild matches the vanilla captures, and each feature has an on/off A/B. DSR caps at 60 fps, so perf is measured as GPU time per frame (PresentMon if the user approves the download; otherwise `nvidia-smi` GPU utilization at a fixed scene). The target is ≤ +10% GPU time and never dropping below the 60 fps cap at 4K.
- **Stability.** Boot plus loading every area snapshot works, with 5+ minutes idle per area and no crash or hang.
- **Looks.** Every area has vanilla vs mod captures meeting the numeric guardrails, and the user signs off per area.
- **Install.** An install → uninstall round-trip leaves files hash-identical to `vanilla/manifest.sha256`, and TPUP texture hashes are unchanged.
