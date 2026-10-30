//==============================================================================
// dsrle_features.fxh -- DSRLE feature definitions for the unified 421b FlverPBL
// pixel shaders. Original DSRLE code (no game source).
//
// Included from dsrle_hooks.fxh only when /D DSRLE_FEATURES is set, ahead of
// the identity defaults. Pass the include path with /I shaders/features (next
// to the /I shaders/hooks the mapping already carries). With DSRLE_FEATURES
// set but no feature switch below defined, this file defines nothing and every
// blob stays identical to the identity build.
//
//------------------------------------------------------------------------------
// FEATURE: SUN  (DSRLE_HOOK_DIRECT)       switches: /D DSRLE_SUN_A  or  /D DSRLE_SUN_B
//
// 421b has no directional-light loop and the engine never uploads the LightBank
// directional lights to these shaders, so the only real-time sun is the shadow
// light. This feature adds it as a direct light:
//
//   L        = -normalize(gFC_ShadowLightDir.xyz)       c73, ShadowBank lightDegRotX/Y.
//              This is the direction the real-time shadow maps are rendered from,
//              so lit faces and cast shadows agree. gFC_SpcLightVec (c5) is NOT used:
//              its angles are a water-specular setup (30/30, straight down, ...),
//              not a sun, and the _s rotations are fixed by our spec.
//   radiance = carrier.rgb * DSRLE_SUN_GAIN, then the largest channel is capped at
//              DSRLE_SUN_MAX with the hue kept (see GAIN AND CAP).
//              carrier = gFC_SpcLightCol (c6, default): the LightBank _s light of the
//              draw's light row, pow(col/255 * colA/100, 2.2), so it is already in the
//              same gamma-decoded linear scale as the hemisphere ambient c7/c8.
//              colA_s = 0 (229 of 578 vanilla rows) gives c6 = 0 and switches the sun off
//              for that row; this is the area spec's row-level off switch. Retuning colA_s
//              also moves the Water specular, which reads the same c5/c6.
//              With /D DSRLE_SUN_CARRIER_SCATTER the carrier is gFC_LsSunColor (c19)
//              instead. c19 is uploaded LINEAR (sunR/G/B/255 times a sunA scale, NOT put
//              through the 2.2 power like c6/c7/c8) and is much larger (Firelink 12.0 for
//              a 255 channel), so it gets its own, uncalibrated, default gain.
//
// BRDF: the game's own point-light BRDF (ForwardPBL: Lambert diffColor/PI plus
//   microfacets_brdf = GGX D * Schlick-SG F * Smith-Schlick V, with FitRoughness),
//   times saturate(N.L), times radiance * PI. This is PointLightContribution without
//   its distance falloff, so the sun and the point lights share one BRDF and one
//   energy scale. The hook is a macro, so it expands at the DIRECT call site where
//   microfacets_brdf, CalcLuminance, M_PI, M_INV_PI and In.VecEye are visible.
//   Two sun-only differences (point lights are unchanged):
//   * roughness is floored at DSRLE_SUN_MIN_ROUGHNESS (0.1) before the BRDF. The game
//     floors at 0.014, where the GGX peak 1/(PI r^4) is ~8e6; at 0.1 it is ~3e3. A global
//     delta light over every shadowed pixel would otherwise sparkle on smooth normal maps.
//   * N.L uses the bump-mapped normal, and is additionally multiplied by
//     smoothstep(0, 0.1, dot(normalize(vertexNormal), L)), so faces whose interpolated
//     vertex normal points away from the sun get nothing even where the normal map tilts
//     the shading normal towards it.
//
// DOUBLE-LIGHTING GATE (all compile-time; `flags` is a literal and folds away)
//   Baked light maps already contain the direct sun diffuse, and the baked sun
//   angles (LightBank degRot_0) differ from the ShadowBank angles in most areas.
//   A c73 diffuse sun on a light-mapped surface would light it twice and from a
//   second direction that disagrees with the baked shadows. Unshadowed variants
//   have no visibility term at all, so a sun there would leak indoors.
//
//   variant                          A (DSRLE_SUN_A)          B (DSRLE_SUN_B = A plus)
//   no light map, shadowed            diffuse + specular       diffuse + specular
//   light map, shadowed               -                        specular only, x baked mask
//   unshadowed (with or without LIT)  -                        -
//   ghost (Gst, WITH_GhostMap)        -                        -
//   Non / Water / Dif+Mul placeholder (DIRECT not wired)       -
//   Ghosts are excluded because the FaceEye/HemEnv ghost path adds the ghost tint after
//   DIRECT and saturates the result into it, so a sun turns a phantom opaque white.
//   Sfx (FS_TRANSLUCENT) keeps the sun: its 240 entry points use the same permutation
//   names as 240 of the 438 Phn ones, compiled translucent (plus SH statics). Judging
//   from those names only (not checked on meshes), excluding Sfx would seam alpha-blended
//   materials against their opaque counterparts; the inverse tone map there is only an
//   exposure rescale and the cap bounds the radiance.
//
//   Visibility, every term:  shadowRate (after DSRLE_HOOK_SHADOW) * saturate(N.L)
//                            * fade, fade = saturate((gFC_ShadowMapParam.y - In.VecEye.w)
//                            * gFC_ShadowMapParam.z).
//   That fade is the one the shadow function uses to fade the shadow OUT with camera
//   distance (beyond it the rate is 1 everywhere). Vanilla only scales the IBL with that
//   rate, so the fade is subtle there; an additive sun would pop to full strength past the
//   shadow distance (20 m in the a10 "B0 indoors" row, 30-40 m outdoors). Here the sun
//   fades out with the shadow instead, so past the shadow range a no-light-map surface
//   gets no sun (the vanilla look) rather than an unoccluded one.
//   B, light-mapped specular, additionally:  lightmap.a * saturate(CalcLuminance(lightmap.rgb)
//                            * DSRLE_SUN_BAKED_K). lightmap.a is the static POINT-light shadow
//                            (the source feeds it to the clustered point lights only), so on
//                            its own it does not keep the sun out of baked sun shadow. The
//                            baked light rgb (which vanilla itself uses as an IBL visibility,
//                            min(shadow, lightmap.rgb)) does: dark baked texels get no glint.
//                            K = 2 means full specular from a baked luminance of 0.5 up; it
//                            is uncalibrated.
//   Blobs outside the gate compile byte-identical to the identity build, and the A-set
//   blobs are SHEX-identical between A and B (the B-only term sits in the LIT branch).
//
// GAIN AND CAP (DSRLE_SUN_GAIN 2.0, DSRLE_SUN_MAX 0.6 for the c6 carrier)
//   With this BRDF PI cancels on the diffuse lobe: at N.L = 1 the sun adds
//   diffColor * radiance, the same form as the hemisphere ambient diffColor * lerp(c8, c7).
//   The common vanilla _s row (white, colA_s 50; 293 of 578 rows) gives c6 = 0.2176, so
//   gain 2 gives 0.435. The other rows are not on that scale: colA_s 100-600 gives c6 of
//   1 to 51 (a12 row 0, Darkroot, colA_s 300: 11.2, i.e. 22 at gain 2), so the largest
//   channel is capped at 0.6 with the hue kept (hue = c6 / max channel).
//   Result over the vanilla LightBank rows, weighted by their live map-piece + object +
//   character placements (data/live_rows.json):
//     max-channel radiance  0 for 21.8% (colA_s 0 rows), 0.435 from p25 to the median, 0.6 at p75+
//     sun / hemisphere luminance (avg of c7, c8)  p25 0.35, median 5.2, p75 9.1, p90 11.7;
//       13.7% of placements are above 10x. Characters: never 0, median 7.4x.
//   So against the hemisphere ambient alone the key:fill ratio is ~5-9:1 where the sun is on.
//   The hemisphere ambient is NOT the whole fill: the light-probe IBL (diffuse + specular,
//   shadow-modulated on no-light-map surfaces) adds more, and the sweep shows it is a strong
//   lever (envDif x2 = +0.5 to +1.2 EV). That term is not in this comparison, so gain and cap
//   are a starting point to be calibrated by capture (sun_A vs off at 4-5 bonfires, dEV and
//   clip on characters and props), not a derived value.
//   Overrides: /D DSRLE_SUN_GAIN=x, /D DSRLE_SUN_MAX=x (float literals, e.g. 1.5f),
//   /D DSRLE_SUN_UNCAPPED (no cap), /D DSRLE_SUN_MIN=x (floor on the largest channel, with a
//   white hue where the carrier is 0, so colA_s 0 rows get a weak sun too; off by default
//   because it overrides the row-level off switch).
//
// OPT-IN, OFF BY DEFAULT (compile-tested only)
//   DSRLE_SUN_MIN=x          see above.
//   DSRLE_SUN_SHADOW_RENORM  hard sun occlusion. The shadow rate is
//                            pow(|1 - fade * ShadowColor * occ|, e), e = gFC_DebugPointLightParams.z,
//                            so full shadow still passes (1 - ShadowColor) of the sun (more
//                            in rows with ShadowBank densityRatio < 100, if ShadowColor follows
//                            it). This inverts that exactly: occ = sum(1 - rate^(1/e)) /
//                            (fade * sum(ShadowColor)), visibility = 1 - occ (still times the
//                            fade). It assumes DSRLE_HOOK_SHADOW is the identity. Rows whose
//                            ShadowColor is 0 have no recoverable occlusion and stay unoccluded.
//
// Constraints kept: no new cbuffer variables, samplers or functions (only macros
// over existing gFC_* constants and the ForwardPBL helpers), so the $Globals layout
// and the resource bindings stay those of vanilla; only the RDEF "used" flags of
// the constants the sun reads change.
//==============================================================================
#ifndef DSRLE_FEATURES_FXH
#define DSRLE_FEATURES_FXH

#if defined(DSRLE_SUN_A) || defined(DSRLE_SUN_B)

//---- carrier, gain and cap -------------------------------------------------------
#ifdef DSRLE_SUN_CARRIER_SCATTER
	#define DSRLE_SUN_CARRIER (gFC_LsSunColor.rgb)    // c19, linear
	#ifndef DSRLE_SUN_GAIN
		#define DSRLE_SUN_GAIN 0.04f                  // ~0.48 for a 12.0 channel (one Firelink sample; uncalibrated)
	#endif
#else
	#define DSRLE_SUN_CARRIER (gFC_SpcLightCol.rgb)   // c6, pow(.., 2.2) like c7/c8
	#ifndef DSRLE_SUN_GAIN
		#define DSRLE_SUN_GAIN 2.0f
	#endif
#endif
#ifndef DSRLE_SUN_MAX
	#define DSRLE_SUN_MAX 0.6f
#endif

#define DSRLE_SUN_RAW (DSRLE_SUN_CARRIER * DSRLE_SUN_GAIN)
#define DSRLE_SUN_RAWMAX (max(max(DSRLE_SUN_RAW.r, DSRLE_SUN_RAW.g), DSRLE_SUN_RAW.b))
#if defined(DSRLE_SUN_MIN)
	// hue of the carrier (white where it is 0), largest channel clamped to [MIN, MAX]
	#ifdef DSRLE_SUN_UNCAPPED
		#define DSRLE_SUN_MAG (max(DSRLE_SUN_RAWMAX, (DSRLE_SUN_MIN)))
	#else
		#define DSRLE_SUN_MAG (clamp(DSRLE_SUN_RAWMAX, (DSRLE_SUN_MIN), (DSRLE_SUN_MAX)))
	#endif
	#define DSRLE_SUN_RADIANCE (((DSRLE_SUN_RAWMAX > 1e-4f) ? (DSRLE_SUN_RAW / DSRLE_SUN_RAWMAX) : float3(1.0f, 1.0f, 1.0f)) * DSRLE_SUN_MAG)
#elif defined(DSRLE_SUN_UNCAPPED)
	#define DSRLE_SUN_RADIANCE (DSRLE_SUN_RAW)
#else
	// largest channel capped at MAX, hue kept
	#define DSRLE_SUN_RADIANCE (DSRLE_SUN_RAW * min(1.0f, (DSRLE_SUN_MAX) / max(DSRLE_SUN_RAWMAX, 1e-4f)))
#endif

#ifndef DSRLE_SUN_MIN_ROUGHNESS
	#define DSRLE_SUN_MIN_ROUGHNESS 0.1f
#endif
#ifndef DSRLE_SUN_BAKED_K
	#define DSRLE_SUN_BAKED_K 2.0f
#endif

//---- direction (surface -> sun) -----------------------------------------------
#define DSRLE_SUN_L (-normalize(gFC_ShadowLightDir.xyz))

//---- visibility ---------------------------------------------------------------
// Shadow distance fade, the same expression the shadow function uses (In.VecEye.w is
// the camera distance it is given at every DIRECT call site).
#define DSRLE_SUN_FADE saturate((gFC_ShadowMapParam.y - In.VecEye.w) * gFC_ShadowMapParam.z)
#ifdef DSRLE_SUN_SHADOW_RENORM
	#define DSRLE_SUN_VIS(rate) \
		(1.0f - saturate(dot(float3(1.0f, 1.0f, 1.0f) - pow(abs(rate), 1.0f / max(gFC_DebugPointLightParams.z, 1e-3f)), float3(1.0f, 1.0f, 1.0f)) / \
		                 max(DSRLE_SUN_FADE * dot(gFC_ShadowColor.xyz, float3(1.0f, 1.0f, 1.0f)), 1e-4f)))
#else
	#define DSRLE_SUN_VIS(rate) (rate)
#endif
// Geometric terminator: no sun where the interpolated vertex normal faces away.
#define DSRLE_SUN_GEO(vertexNormal) smoothstep(0.0f, 0.1f, dot(normalize(vertexNormal), DSRLE_SUN_L))

//---- compile-time gate ----------------------------------------------------------
#define DSRLE_SUN_ELIGIBLE(flags) ((((flags) & DSRLE_VF_SHADOWED) != 0u) && (((flags) & DSRLE_VF_GHOST) == 0u))
// Diffuse: shadowed, not a ghost, no light map (both A and B).
#define DSRLE_SUN_DIFFUSE_ON(flags) (DSRLE_SUN_ELIGIBLE(flags) && (((flags) & DSRLE_VF_LIT) == 0u))
#ifdef DSRLE_SUN_B
	// B: specular on every eligible variant, light-mapped ones included.
	#define DSRLE_SUN_ON(flags) DSRLE_SUN_ELIGIBLE(flags)
#else
	#define DSRLE_SUN_ON(flags) DSRLE_SUN_DIFFUSE_ON(flags)
#endif
// Light-mapped specular mask (1 where there is no light map): static point-light shadow
// times the baked light level.
#define DSRLE_SUN_BAKED(lightmap, flags) \
	((((flags) & DSRLE_VF_LIT) != 0u) ? ((lightmap).a * saturate(CalcLuminance((lightmap).rgb) * DSRLE_SUN_BAKED_K)) : 1.0f)

//---- the light: point-light BRDF without falloff --------------------------------
// saturate(N.L) * (diffColor/PI [gated] + microfacets_brdf) * radiance * PI
#define DSRLE_SUN_BRDF(N, V, diffColor, f0, roughness, specF90, flags) \
	(saturate(dot((N), DSRLE_SUN_L)) * \
	 (((DSRLE_SUN_DIFFUSE_ON(flags) ? ((diffColor) * M_INV_PI) : float3(0.0f, 0.0f, 0.0f)) + \
	   microfacets_brdf((N), DSRLE_SUN_L, (V), (f0), (specF90), max((roughness), DSRLE_SUN_MIN_ROUGHNESS))) * DSRLE_SUN_RADIANCE * M_PI))

#define DSRLE_HOOK_DIRECT(N, V, worldPos, diffColor, f0, roughness, shadowRate, lightmap, flags, vertexNormal, svPos, specF90) \
	(DSRLE_SUN_ON(flags) \
		? (DSRLE_SUN_BRDF(N, V, diffColor, f0, roughness, specF90, flags) * DSRLE_SUN_VIS(shadowRate) \
		   * (DSRLE_SUN_FADE * DSRLE_SUN_GEO(vertexNormal) * DSRLE_SUN_BAKED(lightmap, flags))) \
		: float3(0.0f, 0.0f, 0.0f))

#endif // DSRLE_SUN_A || DSRLE_SUN_B

#endif // DSRLE_FEATURES_FXH
