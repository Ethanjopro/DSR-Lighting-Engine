# DSR Lighting Engine v3: the DLL engine

## v4 direction: the bold rework (2026-10-31, supersedes the look limits below)
After comparing vanilla and the beta, the user said: "I see no major difference between the vanilla and mod. I want a large change, not a small one. From this point on, you are able to reimagine the lighting to take it to a AAA level. I'd like to still keep the identity, but I want the visuals to be more akin to what was done in ds2:le or better."

**Why the beta read small.** Everything so far multiplies or lightly adds to vanilla's `(probe * lightmap + hemisphere) * AO + points`. There is no key light, so the scene stays flat-lit however good the AO is.

**What changes.**
- Identity now means each area's palette and mood, not its pixel values. Defaults are bold, and the user pulls back with presets.
- A real key light: sun diffuse and specular on all surfaces, lightmapped ones included, shadowed by world casters, with the flat fill pulled well down. Target a sunlit-to-shadow ratio of 4:1 or more on ground outdoors, against about 1.2:1 in vanilla.
- The sun's colour and direction come from each area's own LightBank `_0` light, authored for the bake and never uploaded. It reaches the shaders through `_s` (c5, c6), so the area's identity is its own sun. ShadowBank angles follow it.
- Air lit by that sun: volumetrics read the world shadow atlas, so light shafts through ruins and trees are real.
- Then: shadow-casting torches, a filmic tonemap and grade, HDR bloom, native HDR, reflections and bounce light.
- Show the user one round of captures per milestone, not eight rounds judged alone.

## Why this replaces v2
On 2026-10-31 the user asked for the lighting engine to be replaced "like ds2:le (with dll)", for "a fully functional remaster of the game, essentially (that still preserves its original look)". PLAN_v2 ruled the DLL tier out; that decision is reversed. The safety category is now DS2LE's: code runs in the game process. The docs may not say "injects nothing" any more, and the release makes no online-safety claim.

Acceptance is the user's eye on full-resolution vanilla vs engine captures. The 1.0 guardrails (±0.3 EV and so on) are what kept it invisible, and they do not apply.

## Architecture
`engine/` builds `d3d11.dll`, a proxy placed in the game folder. There are no third-party libraries.

- **Device and swap chain:** vtable patches. These vtables are static data in the system DLLs.
- **Contexts:** COM wrappers (`engine/src/context.cpp`, forwarders generated from the SDK header). The runtime keeps per-object context vtables in heap memory and can rewrite them, so vtable patches do not stick there (`engine/tests/vtable_probe.cpp`).
- **Shader identity:** DXBC checksum at creation, mapped to archive entry names (`engine/data/shader_names.tsv`).
- **Frame tracer:** `dsrle\trigger_trace` records one frame: every pass, shader names, targets, bindings, constants, and render-target dumps (`engine/tools/trace_report.py`, `dds2png.py`).
- **Engine passes:** runtime HLSL in `dsrle\hlsl`, compiled with the system d3dcompiler and hot-reloaded when a file changes. Settings in `dsrle\dsrle.ini` reload live, so tuning needs no game restart.
- **Replaced game shaders:** swapped at `*SetShader`, so vanilla and engine can be toggled at runtime, and the game's shader archives stay untouched on disk.

## Measured frame (Firelink, 4K, TAA, one frame)
1. Sun cascades: one 2048² R24G8 atlas, four 1024² viewports. **Only ~28-108 draws cast: the player and a few objects. Static world geometry barely casts at all.**
2. Depth prepass on the 4K D24S8 buffer, null PS, then the SSAO compute chain: PrepareDepthBuffers1, Render1 x2, BlurUpsampleBlendOut, BlurUpsample (RGBA8, consumed as t8.r).
3. Opaque forward into RG11B10F + BGRA8 (subsurface strength, water mask).
4. CopyResource of the scene (the refraction copy). Water.
5. Velocity (RG16F), blended Phn, SubsurfX/Y, blended 1-RT, SFX, DepthCopy, soft SFX, CopyResource, ResolveTAA, DOF, motion blur, luminance, bright pass, bloom, `FRPG_Fil_HDR_PBL_ColAdj`, UI, an unidentified blit to the back buffer, Present.

The first five lists record on a worker thread, the rest on the main thread, all on deferred contexts. So state must be re-bound per list, uploads use `Map(DISCARD)`, and readback inside a list is impossible.

Vanilla lighting is `(IBL * lightmap mask + hemisphere ambient) * AO + emissive + point lights`, with fog and scattering in 2.2-gamma space. **There is no sun term.** The world is lit by masked probes.

## Headline features, in build order
Each has a per-area strength and a runtime off switch.

1. **GTAO** replaces the SSAO dispatches and writes the game's own t8 texture, so no game shader changes are needed.
2. **Highlight roll-off and dither** in the composite. Identical to vanilla below a pivot, so the area look stays; sky and fire stop clipping (Firelink clips 16% of pixels).
3. **AO reaching point lights, specular occlusion, sun contact shadows.** t8's unused gba channels carry the contact mask and bent normal, so no new SRV is needed in the replaced FlverPBL shaders.
4. **Volumetrics:** additive froxels for local lights and vanilla-coloured haze at the first scene copy, plus FromSoftware's own LightShaft pass. Vanilla's per-object fog rows stay as the colour anchor, which avoids DS2LE's "muddy" and "lost palette" complaints.
5. **Water reflections** with SSR.
6. **Torch shadows from characters and world sun casters.** Replays of captured prepass draws: the VS camera is c0..c3 only, applied after the palette, so no new vertex shaders are needed. Riskier, so these come after the first impression is proven.

Second wave once those work: 4096 cascades with depth clip off, PCSS, optional RGBA16F scene.

## Look gate for every feature
From DS2LE's documented complaints:
- Daylight is not muddy.
- Each area keeps its palette.
- The player is not constantly dark.
- Fog does not box characters in.
- Water keeps its sparkle.

## Testing
Only through `tools/test_session.ps1` (swaps the real save out, removes the DLL afterwards). `tools/engine_session.ps1` runs variants `off` and `on`. Passthrough gate result (2026-10-31): `gate0`, DLL on vs off, is within run-to-run noise at Parish, Burg and Firelink.
