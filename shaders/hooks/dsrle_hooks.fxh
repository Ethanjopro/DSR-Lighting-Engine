//==============================================================================
// dsrle_hooks.fxh -- DSR Lighting Engine hook points for the unified 421b
// FlverPBL pixel shaders.
//
// Original DSRLE code (no game source). The unified 421b tree includes this
// header once, from its common header, after the constant / sampler
// declarations and before the shared lighting functions, so a hook body can
// read every gFC_* constant and gSMP_* sampler. Find it with fxc /I shaders/hooks.
//
// RULES FOR THE DEFAULTS (the zero-change rebuild gate depends on them)
//   * Every default is a pure macro: an identity expression, an empty statement
//     or a literal zero. There are no functions, no static const, no cbuffer
//     variables and no samplers. Unused globals alone are enough to change
//     fxc's register allocation in some blobs.
//   * A feature replaces a hook by #defining the macro first. Set
//     /D DSRLE_FEATURES and put the definitions in shaders/features/
//     dsrle_features.fxh (included below, ahead of the defaults). A feature may
//     declare its own globals or functions there. Its blobs are then no longer
//     vanilla, which is the point.
//   * Visibility: a hook MACRO body expands at its call site and can use everything there
//     (MATERIAL, Srgb2linear, shadow functions, In.*). A helper FUNCTION defined in
//     dsrle_features.fxh is parsed at the include point and sees only the gFC_* / gVC_*
//     constants and gSMP_* samplers, not ForwardPBL or ShadowFunc.
//   * The macro names and parameter order below are a stable interface.
//     New inputs are only ever appended at the end.
//
// INPUTS A VANILLA BLOB DID NOT READ
//   A hook may read any interpolant of the entry point's VTX_OUT struct, e.g. worldPos in an unlit
//   Non shader. fxc keeps every struct element in ISGN with the same register; only the "used"
//   mask changes. So the vanilla vertex shaders still link (checked on FRPG_Phn_Dif________________Non).
//
// SUN DATA AVAILABLE TO A HOOK (421b $Globals, the same layout in every blob)
//   Confirmed by the in-game probe (data/probe_constants.json). The engine binds by fixed
//   offset, not by name, and never uploads the LightBank directional lights (colA/colR/degRot _0.._2).
//   gFC_ShadowLightDir  c73   xyz = sun (shadow light) PROPAGATION direction, so L = -xyz. Source:
//                             ShadowBank lightDegRotX/Y (not LightBank dir 0). The same value for
//                             every draw in the frame, shadowed or not.
//   gFC_LsLightDir      c20   light-scattering sun direction (LightScatteringBank sunRotX/Y), w = distanceMul.
//                             Read by every blob. Interior banks use rotX = 90 (straight down).
//   gFC_LsSunColor      c19   scattering sun colour rgb (linear), a = blend (sunR/G/B/A)
//   gFC_SpcLightVec/Col c5/c6 LightBank degRotX/Y_s and pow(col*_s/255 * colA_s/100, 2.2), per map-piece
//                             light row. Only Water reads them in 421b, so they can carry a
//                             per-area sun to every other shader.
//   gFC_ShadowMapMtxArray c40..c55, gFC_ShadowMapClamp c56..c59, gFC_ShadowStartDist c23,
//   gFC_ShadowMapParam c21, gFC_ShadowColor c22: shadow lookups (Csd/Sdw/Ncs only).
//   gFC_FogCol c12 (rgb colour, a = max density). The fog factor arrives per vertex in
//   In.VecNrm.w (TEXCOORD2.w).
//
// COMMON INPUTS (all variants that reach the hook)
//   N         float3  shading normal, world space, normalised (after bump / detail bump / face flip)
//   V         float3  normalised surface->camera vector, world space (In.VecEye.xyz)
//   worldPos  float3  world-space position (In.VtxWld.xyz; In.VtxWld.w is view-space depth)
//   svPos     float4  SV_Position (In.VtxClp): xy = pixel centre, z = depth, w = clip w
//   flags     uint    compile-time literal DSRLE_VARIANT_FLAGS | per-function bits (see
//                     dsrle_variant.fxh). Test it with (flags & DSRLE_VF_xxx); it folds away.
//
// WHERE THE HOOKS ARE WIRED (unified tree, vanilla_src/rebuild/src/g421b_unified)
//   base               SHADOW  IBL  AMBIENT  DIRECT  POINTLIGHT  FINAL
//   HemEnv (Phn/Gst/Sfx) yes    yes   yes      yes     yes         yes
//   FaceEye (Phn/Gst)   yes     yes   yes      yes     yes         yes
//   Snow                yes     yes   yes      yes     yes         yes
//   Water               yes     -     -        -       -           yes  (custom water BRDF)
//   Non (unlit)         yes     -     -        -       -           yes
//   Also: the Gst ghost light goes through DSRLE_HOOK_POINTLIGHT. Water's own
//   specular-only point light loop does not.
//   The HemEnv "Dif + Mul" entry points are magenta placeholders in vanilla and have
//   no lighting, so they get no hooks.
//==============================================================================
#ifndef DSRLE_HOOKS_FXH
#define DSRLE_HOOKS_FXH

#include "dsrle_flags.fxh"

#ifdef DSRLE_FEATURES
	#include "dsrle_features.fxh"
#endif

//------------------------------------------------------------------------------
// DSRLE_HOOK_SHADOW(rate, worldPos, N, eyeVec, flags) -> float3          [expression]
//   Point: the shadow-map visibility right after CalcGetShadowRate*() and before any
//   min() with the light map (Lit+Csd / Lit+Sdw use min(shadow, lightmap)).
//   rate     float3  shadow visibility, 1 = lit, 0 = shadowed (ShadowColor already mixed in)
//   worldPos float4  position the lookup used (In.VtxWld; water/snow pass their wave- or
//                    snow-lifted position). xyz world, w view depth
//   N        float3  normal used for the slope bias
//   eyeVec   float4  xyz surface->camera, w camera distance
//   flags    which lookup: DSRLE_VF_SDW (VS light-space, In.VtxLit), DSRLE_VF_CSD
//            (cascaded PS lookup), DSRLE_VF_NCS (water/snow single map). Only shadowed variants call it.
//   Default: identity.
#ifndef DSRLE_HOOK_SHADOW
	#define DSRLE_HOOK_SHADOW(rate, worldPos, N, eyeVec, flags) (rate)
#endif

//------------------------------------------------------------------------------
// DSRLE_HOOK_IBL(diffIBL, specIBL, N, V, R, roughness)                    [statement]
//   Point: inside CalcEnvIBL, after both light-probe terms exist and before
//   LightPower * (diffIBL + specIBL). Shared function, so there are no per-variant flags.
//   diffIBL  inout float3  diffuse probe irradiance * albedo (gSMP_EnvDifMap; two-probe lerp for Phn *Lerp)
//   specIBL  inout float3  specular probe * DFG * horizon fade (gSMP_EnvSpcMap)
//   N, V     float3  shading normal / surface->camera
//   R        float3  reflection vector (not normalised: 2*dot(N,V)*N - V)
//   roughness float  material roughness
//   Default: no-op.
#ifndef DSRLE_HOOK_IBL
	#define DSRLE_HOOK_IBL(diffIBL, specIBL, N, V, R, roughness)
#endif

//------------------------------------------------------------------------------
// DSRLE_HOOK_AMBIENT(diffAmb, specAmb, N, V, worldPos, lightRate, flags)  [statement]
//   Point: after the hemisphere ambient and the light-probe environment term are both
//   computed, before they are summed, multiplied by SSAO (gFC_SAOParams.w) and added to emissive.
//   diffAmb  inout float3  hemisphere ambient: albedo * lerp(gFC_HemAmbCol_d, gFC_HemAmbCol_u, N.y*.5+.5)
//   specAmb  inout float3  probe environment term: LightPower*(albedo*probeDiffuse + probeSpecular)
//                          already multiplied by lightRate
//   lightRate float3       lightmap / shadow factor that was applied to specAmb (1 when neither)
//   vanilla result = diffAmb + specAmb, then the AO multiply.
//   Default: no-op.
#ifndef DSRLE_HOOK_AMBIENT
	#define DSRLE_HOOK_AMBIENT(diffAmb, specAmb, N, V, worldPos, lightRate, flags)
#endif

//------------------------------------------------------------------------------
// DSRLE_HOOK_DIRECT(N, V, worldPos, diffColor, f0, roughness, shadowRate, lightmap, flags,
//                   vertexNormal, svPos, specF90) -> float3                [expression]
//   NEW direct (sun / key) light entry point. 421b has no directional-light loop, so vanilla
//   has nothing here. The result is added to the point-light sum: after SSAO, not scaled by
//   AO, before the ghost light, ghost tint, fog and light scattering.
//   diffColor float3  linear diffuse albedo (Mtl.DiffuseColor, LightPower already applied)
//   f0        float3  linear specular colour / F0 (Mtl.SpecularColor)
//   roughness float   Mtl.Roughness (FitRoughness not applied)
//   shadowRate float3 real-time shadow visibility (after DSRLE_HOOK_SHADOW); 1 for variants
//                     with no shadow map
//   lightmap  float4  light-map sample after gFC_DebugPointLightParams.y scaling
//                     (rgb = baked light incl. baked sun, a = static-light shadow); 1 for
//                     variants with no light map. Lit+shadow variants also have min(shadow, lightmap)
//                     baked into the vanilla lightRate
//   vertexNormal float3 interpolated vertex normal (face-flipped, not normalised)
//   svPos     float4  SV_Position
//   specF90   float   specular F90 (calcSpecularF90)
//   Default: float3(0,0,0), which fxc removes completely (verified by the gate).
#ifndef DSRLE_HOOK_DIRECT
	#define DSRLE_HOOK_DIRECT(N, V, worldPos, diffColor, f0, roughness, shadowRate, lightmap, flags, vertexNormal, svPos, specF90) float3(0.0f, 0.0f, 0.0f)
#endif

//------------------------------------------------------------------------------
// DSRLE_HOOK_POINTLIGHT(contrib, L, dist, lightColor, N, V, worldPos, lightShadow) [statement]
//   Point: once per point light, after its BRDF result and before it joins the sum.
//   Clustered loop (CalcPointLightsClustered), legacy loop (FaceEye PntS) and Gst ghost light.
//   contrib    inout float3  lightShadow * PointLightContribution(...) (ghost: no lightShadow)
//   L          float3        surface->light direction (normalised; ghost light: unnormalised vector)
//   dist       float         distance to the light
//   lightColor float4        rgb colour*intensity, w = falloff end radius
//   lightShadow float        lerp(lightmapShadow, 1, light.attenuation) (1 for legacy/ghost)
//   Default: no-op.
#ifndef DSRLE_HOOK_POINTLIGHT
	#define DSRLE_HOOK_POINTLIGHT(contrib, L, dist, lightColor, N, V, worldPos, lightShadow)
#endif

//------------------------------------------------------------------------------
// DSRLE_HOOK_FINAL(color, N, V, worldPos, svPos, flags)                   [statement]
//   Point: final linear lit colour, before ghost tint, fog (sRGB), light scattering,
//   back-to-linear and the FS_TRANSLUCENT inverse tone map / output packing.
//   color   inout float4  rgb linear HDR radiance, a = output alpha
//   Non (unlit): color = albedo * lightmap/shadow (+ spec * vertex colour * lightmap/shadow).
//   Water: color.rgb = the blended refraction/reflection/specular colour * vertex colour, before
//   linear->sRGB and fog (a = 1, unused). The distance fade back to raw refraction comes after.
//   Default: no-op.
#ifndef DSRLE_HOOK_FINAL
	#define DSRLE_HOOK_FINAL(color, N, V, worldPos, svPos, flags)
#endif

#endif //DSRLE_HOOKS_FXH
