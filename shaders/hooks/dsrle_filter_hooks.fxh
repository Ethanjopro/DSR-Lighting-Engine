//==============================================================================
// dsrle_filter_hooks.fxh -- DSR Lighting Engine hook points for the rebuilt
// FRPG_Filter_DX11 post-process shaders (currently: FRPG_Fil_ResolveTAA.fpo).
//
// Original DSRLE code (no game source). The hooked, git-ignored TAA source in
// vanilla_src/rebuild/filter/src/FRPG_Filter/ includes this header once, after the
// filter common header and the TAA luma define, so a feature can read every
// gFC_* constant and gSMP_* texture/sampler the filter generation declares.
// The hook call sites are inserted by shaders/tools/wire_filter_hooks.py from a
// pristine snapshot (never by hand); build_filter.py puts shaders/hooks and
// shaders/features on fxc's include path for every compile.
//
// RULES FOR THE DEFAULTS (the zero-change rebuild gate depends on them)
//   * Every default is an EMPTY STATEMENT macro. The call sites are inserted next
//     to the vanilla statements, which stay untouched, so the identity build is the
//     vanilla token stream plus a few empty statements: fil_rebuild0 must keep sha256
//     6665a71533ee1bea9745f776df195e2375405e3ca20f96002220b1ae7c4cd95f.
//   * No functions, static consts, cbuffer variables or samplers here. Feature code
//     goes to shaders/features/dsrle_filter_features.fxh (included below only with
//     /D DSRLE_FEATURES, ahead of the defaults) behind its own switches, so
//     DSRLE_FEATURES alone still builds the identity.
//   * The engine binds $Globals by offset and build_filter.py compares RDEF raw
//     bytes, including the per-variable "used" flags. A feature may only read the
//     constants the vanilla blob already reads (ResolveTAA: c8, c12, c56, c57, c58..c61,
//     c74..c77, c78) and the resources it already binds (t0..t4, s1..s4).
//   * Only the pixel shader (FragmentMain) reaches the hooks. The same .fx also builds
//     ResolveTAA.vpo, the generic VertexMain_Base blob shared by 20 other archive
//     entries; it must stay EXACT in every feature build (check the vpo row of the report).
//   * The macro names and parameter order are a stable interface. New inputs are
//     only ever appended at the end.
//
// COLOUR SPACE AT THE HOOKS
//   ResolveTAA works on the pre-tonemap linear HDR scene colour (the tonemap pass,
//   HDR_PBL_ColAdj, runs after it). Its neighbourhood, clamp and blend use the
//   reversible luma-weighted compression m = c / (1 + Y(c)), Y = Rec.709 luma
//   (0.2126, 0.7152, 0.0722). Mapped LUMA is always in [0, 1) for c >= 0; a single
//   mapped channel can exceed 1 for saturated colours. The output is mapped back with
//   c = m / (1 - min(Y(m), 0.9999)) and written to SV_Target0, which is ALSO next
//   frame's history (one colour target; SV_Target1 is the motion-blur info pair).
//==============================================================================
#ifndef DSRLE_FILTER_HOOKS_FXH
#define DSRLE_FILTER_HOOKS_FXH

#ifdef DSRLE_FEATURES
	#include "dsrle_filter_features.fxh"
#endif

//------------------------------------------------------------------------------
// DSRLE_TAA_HOOK_HISTORY(hist, histUV, uv)                                [statement]
//   Point: right after the vanilla shader fetched the history colour (one bilinear
//   SampleLevel of t1/s1 at histUV) and mapped it to m-space, before it is clamped to
//   the current-frame neighbourhood box.
//   hist    inout float3  history colour in m-space (c / (1 + Y(c)))
//   histUV  float2        reprojected history uv = uv - velocity (may be outside [0,1];
//                         vanilla then forces a full reset, so the history is unused there)
//   uv      float2        this pixel's uv (centre of the output pixel)
//   A feature that overwrites hist makes the vanilla fetch dead code (fxc removes it).
//   Default: no-op.
#ifndef DSRLE_TAA_HOOK_HISTORY
	#define DSRLE_TAA_HOOK_HISTORY(hist, histUV, uv)
#endif

//------------------------------------------------------------------------------
// DSRLE_TAA_HOOK_CURRENT(cur, blend, hist, cN, cS, cW, cE, cNW, cNE, cSW, cSE,
//                        boxMin, boxMax, fullMin, fullMax)                 [statement]
//   Point: after the final (saturated) blend weight is known and before
//   res = lerp(hist, cur, blend). Everything that decides the blend (box, clamp,
//   history luma distance, resets) has already been computed from the UNMODIFIED
//   current sample, so changing cur here changes only what is blended in.
//   cur       inout float3  current-frame centre sample (t0 Load, m-space)
//   blend     float         final weight of cur, saturate()d: 0 keeps the history,
//                           1 = full reset (off-screen history, masked disocclusion)
//   hist      float3        history after the per-channel box clamp (m-space)
//   cN..cSE   float3        the 8 neighbours of the 3x3 t0 neighbourhood (m-space);
//                           N = y-1, S = y+1, W = x-1, E = x+1 in pixel coordinates
//   boxMin/Max float3       the rounded clamp box the history was clamped to:
//                           (min9 + minCross) / 2 and (maxCross + max9) / 2, per channel.
//                           cur always lies inside it.
//   fullMin/Max float3      plain per-channel min / max of the 9 samples
//   Whatever is written to cur ends up in SV_Target0 and therefore in next frame's
//   history: modify the fresh current sample only, never feed hist back through a filter.
//   Default: no-op.
#ifndef DSRLE_TAA_HOOK_CURRENT
	#define DSRLE_TAA_HOOK_CURRENT(cur, blend, hist, cN, cS, cW, cE, cNW, cNE, cSW, cSE, boxMin, boxMax, fullMin, fullMax)
#endif

#endif //DSRLE_FILTER_HOOKS_FXH
