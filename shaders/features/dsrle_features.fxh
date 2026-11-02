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
//   Snow (family or base)             -                        -     (/D DSRLE_SUN_ON_SNOW keeps it)
//   Non / Water / Dif+Mul placeholder (DIRECT not wired)       -
//   Snow is excluded because it has no light map and is already near white, so the sun
//   mostly clips it (Painted World: clipped pixels 32.6% -> 34.3% at the cap, data/sun_ab.json).
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
//   SPEC-ONLY ROWS (runtime gate, c6 carrier only): a row whose largest c6 channel is above
//   DSRLE_SUN_SPEC_ONLY (0.33, just above 0.6^2.2 = 0.325, i.e. a white colA_s above 60) gets no
//   sun. Those rows (vanilla colA_s 100-600) are water and lava specular settings, not sun intent,
//   so they keep their specular and get no sun without any DrawParam edit. spec/global.toml
//   [sun] spec_only_above = 60 is the same cutoff. /D DSRLE_SUN_NO_SPEC_GATE removes the gate.
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

#ifdef DSRLE_ENGINE
	// The engine's key light: strength comes from the row and b3, with no cap.
	#define DSRLE_SUN_UNCAPPED
	#define DSRLE_SUN_GAIN 1.0f
#endif

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

// Spec-only rows: 0 where the c6 row is a water/lava specular setting (see GAIN AND CAP).
#ifndef DSRLE_SUN_SPEC_ONLY
	#define DSRLE_SUN_SPEC_ONLY 0.33f
#endif
#if defined(DSRLE_SUN_CARRIER_SCATTER) || defined(DSRLE_SUN_NO_SPEC_GATE)
	#define DSRLE_SUN_ROW_GATE 1.0f
#else
	#define DSRLE_SUN_ROW_GATE ((max(max(gFC_SpcLightCol.r, gFC_SpcLightCol.g), gFC_SpcLightCol.b) <= (DSRLE_SUN_SPEC_ONLY)) ? 1.0f : 0.0f)
#endif
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
#ifdef DSRLE_ENGINE
	// The engine's cascades reach far past the game's shadow range, so the sun follows those instead. dsrleSunVisibility
	// is set by the ambient hook, which runs first. Past the last cascade the sun fades with the same distance.
	#define DSRLE_SUN_FADE (dsrleSunVisibility * gDsrleSunParam.w)
#else
#define DSRLE_SUN_FADE saturate((gFC_ShadowMapParam.y - In.VecEye.w) * gFC_ShadowMapParam.z)
#endif
#ifdef DSRLE_SUN_SHADOW_RENORM
	#define DSRLE_SUN_VIS(rate) \
		(1.0f - saturate(dot(float3(1.0f, 1.0f, 1.0f) - pow(abs(rate), 1.0f / max(gFC_DebugPointLightParams.z, 1e-3f)), float3(1.0f, 1.0f, 1.0f)) / \
		                 max(DSRLE_SUN_FADE * dot(gFC_ShadowColor.xyz, float3(1.0f, 1.0f, 1.0f)), 1e-4f)))
#else
	#define DSRLE_SUN_VIS(rate) (rate)
#endif
// Geometric terminator: no sun where the interpolated vertex normal faces away.
#ifdef DSRLE_ENGINE
#define DSRLE_SUN_GEO(vertexNormal) lerp(smoothstep(0.0f, 0.1f, dot(normalize(vertexNormal), DSRLE_SUN_L)), 1.0f, gDsrleDraw.x)
#else
#define DSRLE_SUN_GEO(vertexNormal) smoothstep(0.0f, 0.1f, dot(normalize(vertexNormal), DSRLE_SUN_L))
#endif

//---- compile-time gate ----------------------------------------------------------
#ifdef DSRLE_SUN_ON_SNOW
	#define DSRLE_SUN_NOT_SNOW(flags) (true)
#else
	#define DSRLE_SUN_NOT_SNOW(flags) ((((flags) & (DSRLE_VF_FAM_SNOW | DSRLE_VF_BASE_SNOW)) == 0u))
#endif
#ifdef DSRLE_ENGINE
// The engine's own atlas shadows every variant, so the key light reaches them all, baked ones included.
#define DSRLE_SUN_ELIGIBLE(flags) ((((flags) & DSRLE_VF_GHOST) == 0u) && DSRLE_SUN_NOT_SNOW(flags))
#define DSRLE_SUN_DIFFUSE_ON(flags) DSRLE_SUN_ELIGIBLE(flags)
#else
#define DSRLE_SUN_ELIGIBLE(flags) ((((flags) & DSRLE_VF_SHADOWED) != 0u) && (((flags) & DSRLE_VF_GHOST) == 0u) && DSRLE_SUN_NOT_SNOW(flags))
// Diffuse: shadowed, not a ghost, no light map (both A and B).
#define DSRLE_SUN_DIFFUSE_ON(flags) (DSRLE_SUN_ELIGIBLE(flags) && (((flags) & DSRLE_VF_LIT) == 0u))
#endif
#if defined(DSRLE_ENGINE)
	#define DSRLE_SUN_ON(flags) DSRLE_SUN_ELIGIBLE(flags)
#elif defined(DSRLE_SUN_B)
	// B: specular on every eligible variant, light-mapped ones included.
	#define DSRLE_SUN_ON(flags) DSRLE_SUN_ELIGIBLE(flags)
#else
	#define DSRLE_SUN_ON(flags) DSRLE_SUN_DIFFUSE_ON(flags)
#endif
// Light-mapped specular mask (1 where there is no light map): static point-light shadow
// times the baked light level.
#ifdef DSRLE_ENGINE
// The bake knows every static caster, including the ones the camera has not seen, so it still gates the sun; the
// exponent in b3 flattens it so the real-time shadow leads.
#define DSRLE_SUN_BAKED(lightmap, flags) \
	((((flags) & DSRLE_VF_LIT) != 0u) ? pow(saturate(CalcLuminance((lightmap).rgb) * gDsrleLight.y), gDsrleLight.z) : 1.0f)
#else
#define DSRLE_SUN_BAKED(lightmap, flags) \
	((((flags) & DSRLE_VF_LIT) != 0u) ? ((lightmap).a * saturate(CalcLuminance((lightmap).rgb) * DSRLE_SUN_BAKED_K)) : 1.0f)
#endif

//---- the light: point-light BRDF without falloff --------------------------------
// saturate(N.L) * (diffColor/PI [gated] + microfacets_brdf) * radiance * PI
#ifdef DSRLE_ENGINE
// A leaf is thin: the side away from the sun glows with what passes through it, and its mesh normals say little about
// where the leaf really faces. The DLL marks wind-animated meshes in b6.
#define DSRLE_SUN_LEAF(nl) (0.25f + 0.55f * saturate(nl) + 0.35f * saturate(-(nl)))
#define DSRLE_SUN_WRAP(N) lerp(saturate(dot((N), DSRLE_SUN_L)), DSRLE_SUN_LEAF(dot((N), DSRLE_SUN_L)), gDsrleDraw.x)
#define DSRLE_SUN_BRDF(N, V, diffColor, f0, roughness, specF90, flags) \
	(((DSRLE_SUN_DIFFUSE_ON(flags) ? ((diffColor) * M_INV_PI * DSRLE_SUN_WRAP(N)) : float3(0.0f, 0.0f, 0.0f)) + \
	  saturate(dot((N), DSRLE_SUN_L)) * microfacets_brdf((N), DSRLE_SUN_L, (V), (f0), (specF90), max((roughness), DSRLE_SUN_MIN_ROUGHNESS))) * DSRLE_SUN_RADIANCE * M_PI)
#else
#define DSRLE_SUN_BRDF(N, V, diffColor, f0, roughness, specF90, flags) \
	(saturate(dot((N), DSRLE_SUN_L)) * \
	 (((DSRLE_SUN_DIFFUSE_ON(flags) ? ((diffColor) * M_INV_PI) : float3(0.0f, 0.0f, 0.0f)) + \
	   microfacets_brdf((N), DSRLE_SUN_L, (V), (f0), (specF90), max((roughness), DSRLE_SUN_MIN_ROUGHNESS))) * DSRLE_SUN_RADIANCE * M_PI))
#endif

#define DSRLE_HOOK_DIRECT(N, V, worldPos, diffColor, f0, roughness, shadowRate, lightmap, flags, vertexNormal, svPos, specF90) \
	(DSRLE_SUN_ON(flags) \
		? (DSRLE_SUN_BRDF(N, V, diffColor, f0, roughness, specF90, flags) * DSRLE_SUN_VIS(shadowRate) \
		   * (DSRLE_SUN_FADE * DSRLE_SUN_GEO(vertexNormal) * DSRLE_SUN_BAKED(lightmap, flags) * DSRLE_SUN_ROW_GATE)) \
		: float3(0.0f, 0.0f, 0.0f))

#endif // DSRLE_SUN_A || DSRLE_SUN_B

//==============================================================================
// FEATURE: ENGINE                                            switch: /D DSRLE_ENGINE
//
// Only for shaders the DLL swaps in at runtime (engine/src/pack.cpp); those can read data the DLL binds,
// which the loose-file builds cannot.
//   b3                     DsrleEngine: runtime strengths, written by the DLL each frame.
//   t8 (gSMP_AOMap)        written by the DLL's GTAO: r ambient occlusion, g sun contact shadow.
//   t19                    the DLL's world sun shadow atlas (engine/src/sunshadow.cpp), four ortho cascades.
//   b4, t20, t21           the DLL's point light shadows (engine/src/pointshadow.cpp): a cube array and a map from
//                          light id to cube. b4 and the map are written just before each list executes.
//   t25                    the DLL's bounce light (engine/src/ssgi.cpp): rgb at full resolution, strength included.
// AO on point lights: vanilla multiplies AO into the probe and hemisphere terms only, so torches and bonfires
// light every crease evenly. The AMBIENT hook remembers the AO for the point-light hook that runs after it.
// Contact shadow: scales the probe term (the part vanilla already scales by its shadow rate and light map) and
// leaves the hemisphere ambient alone, so contact darkening cannot turn black.
// World sun shadow: the same darkening the game applies for its own shadow map, pow(1 - c22 * occlusion, c101.z), so
// a world shadow is exactly as dark as the game's art already makes a shadow. It also gates the direct sun.
// The row gate keeps it off where the light row has no sun (colA_s 0: interiors, caves, night, in the area spec).
//==============================================================================
#ifdef DSRLE_ENGINE
cbuffer DsrleEngine : register(b3)
{
	float4 gDsrleStrength;   // x AO on point lights, y contact shadow, z world shadow, w same on light-mapped draws
	float4 gDsrleSunSplit;   // far view Z of each cascade
	float4 gDsrleSunUvz[12]; // three rows per cascade, camera-relative position -> atlas u, v, depth
	float4 gDsrleSunParam;   // x normal offset in texels, y filter radius in texels, z 1/atlas size, w direct sun gain
	float4 gDsrleFill;       // probe (x) and hemisphere (y) fill where the row has a sun, the same (z, w) where it has none
	float4 gDsrleLight;      // x point light gain, y and z baked gate on the sun: saturate(lum * y) ^ z, w point light falloff
};
Texture2D<float> gDsrleSunAtlas : register(t19);
Texture2D<float4> gDsrleBounce : register(t25);
cbuffer DsrleDraw : register(b6)
{
	float4 gDsrleDraw;       // x 1 on a wind-animated mesh: foliage. An unbound buffer reads 0
};

static float dsrleAoForPoints = 1.0f;
static float dsrleSunVisibility = 1.0f;

// 0 lit .. 1 occluded. 8 comparison taps on a rotated Vogel disk; TAA smooths the rotation noise.
float dsrle_WorldOcclusion(float3 worldPos, float viewZ, float3 N, float2 pixel)
{
	if (viewZ >= gDsrleSunSplit.w || gDsrleSunSplit.w <= 0.0f) return 0.0f;
	const int c = (int)dot(float4(viewZ >= gDsrleSunSplit.x, viewZ >= gDsrleSunSplit.y, viewZ >= gDsrleSunSplit.z, viewZ >= gDsrleSunSplit.w), float4(1, 1, 1, 1));
	// Texel size in world units doubles the normal offset per stored cascade radius; x row length gives it directly.
	const float worldPerUv = 1.0f / length(gDsrleSunUvz[c * 3].xyz);
	const float3 p = worldPos + N * gDsrleSunParam.x * gDsrleSunParam.z * worldPerUv;
	const float4 wp = float4(p, 1.0f);
	float3 uvz = float3(dot(wp, gDsrleSunUvz[c * 3]), dot(wp, gDsrleSunUvz[c * 3 + 1]), dot(wp, gDsrleSunUvz[c * 3 + 2]));
	uvz.z = min(uvz.z, 0.99999f);  // past the range counts as unoccluded against the cleared depth of 1
	const float noise = frac(52.9829189f * frac(dot(pixel, float2(0.06711056f, 0.00583715f))));
	float s, co;
	sincos(noise * 6.2831853f, s, co);
	const float r = gDsrleSunParam.y * gDsrleSunParam.z;
	const float2 ax = float2(co, s) * r, ay = float2(-s, co) * r;
	// Stay inside this cascade's quadrant.
	const float2 lo = float2((c & 1) ? 0.5f : 0.0f, (c & 2) ? 0.5f : 0.0f) + r * 1.5f;
	const float2 hi = lo + 0.5f - r * 3.0f;
	const float2 uv = clamp(uvz.xy, lo, hi);
	float acc = 0.0f;
	acc += gDsrleSunAtlas.SampleCmpLevelZero(gSMP_ShadowMapSampler, uv + 0.1768f * ax + 0.0000f * ay, uvz.z).x;
	acc += gDsrleSunAtlas.SampleCmpLevelZero(gSMP_ShadowMapSampler, uv - 0.2234f * ax + 0.2047f * ay, uvz.z).x;
	acc += gDsrleSunAtlas.SampleCmpLevelZero(gSMP_ShadowMapSampler, uv + 0.0475f * ax - 0.5570f * ay, uvz.z).x;
	acc += gDsrleSunAtlas.SampleCmpLevelZero(gSMP_ShadowMapSampler, uv + 0.4630f * ax + 0.4630f * ay, uvz.z).x;
	acc += gDsrleSunAtlas.SampleCmpLevelZero(gSMP_ShadowMapSampler, uv - 0.7410f * ax - 0.1260f * ay, uvz.z).x;
	acc += gDsrleSunAtlas.SampleCmpLevelZero(gSMP_ShadowMapSampler, uv + 0.7560f * ax - 0.3920f * ay, uvz.z).x;
	acc += gDsrleSunAtlas.SampleCmpLevelZero(gSMP_ShadowMapSampler, uv - 0.2540f * ax + 0.9070f * ay, uvz.z).x;
	acc += gDsrleSunAtlas.SampleCmpLevelZero(gSMP_ShadowMapSampler, uv - 0.4920f * ax - 0.8340f * ay, uvz.z).x;
	float occ = acc * 0.125f;
	// The last cascade fades out, so there is no line where the data ends.
	const float fadeFrom = gDsrleSunSplit.w * 0.8f;
	return occ * saturate((gDsrleSunSplit.w - viewZ) / (gDsrleSunSplit.w - fadeFrom));
}

// Point light shadows. Must match ShaderConstants in engine/src/pointshadow.cpp.
cbuffer DsrlePointShadow : register(b4)
{
	float4 gDsrlePsGlobal;    // x strength, y normal offset per metre of distance, z tap radius in cube direction units, w edge fade
	float4 gDsrlePsPos[16];   // xyz where the cube was rendered from, camera-relative this frame; w this cube's strength
	float4 gDsrlePsDepth[16]; // x, y: depth = x + y / major axis; z bias in metres
};
TextureCubeArray<float> gDsrlePointCubes : register(t20);
Buffer<int> gDsrlePointMap : register(t21);  // light id -> cube + 1. 0 is none, which is also what an unbound buffer reads
static float2 dsrlePointRot = float2(1.0f, 0.0f);  // set by the ambient hook: In.VtxClp is out of reach in the light loop

// 1 lit .. 0 shadowed. worldPos and N as the light loop has them. A negative id (ghost and legacy lights) folds away.
float dsrle_PointVisibility(int lightId, float3 worldPos, float3 N, float dist, float radius)
{
	float vis = 1.0f;
	const int cube = (lightId >= 0) ? (gDsrlePointMap.Load(lightId) - 1) : -1;
	[branch] if (cube >= 0)
	{
		const float4 place = gDsrlePsPos[cube];
		const float4 depth = gDsrlePsDepth[cube];
		float3 v = worldPos - place.xyz;
		v += N * (max(max(abs(v.x), abs(v.y)), abs(v.z)) * gDsrlePsGlobal.y);
		const float major = max(max(max(abs(v.x), abs(v.y)), abs(v.z)), 1e-3f);
		// The cube's stored depth is projected z along its face's axis, which is the major axis of this vector. Past the
		// far plane counts as unoccluded against the cleared depth of 1.
		const float reference = min(depth.x + depth.y / max(major - depth.z, 1e-3f), 0.99999f);
		const float3 dir = v / major;
		const float3 t = normalize(cross((abs(dir.y) < 0.9f) ? float3(0.0f, 1.0f, 0.0f) : float3(1.0f, 0.0f, 0.0f), dir));
		const float3 b = normalize(cross(dir, t));
		const float3 ax = (t * dsrlePointRot.x + b * dsrlePointRot.y) * gDsrlePsGlobal.z;
		const float3 ay = (b * dsrlePointRot.x - t * dsrlePointRot.y) * gDsrlePsGlobal.z;
		float occ = gDsrlePointCubes.SampleCmpLevelZero(gSMP_ShadowMapSampler, float4(dir + 0.2185f * ax - 0.0920f * ay, (float)cube), reference);
		occ += gDsrlePointCubes.SampleCmpLevelZero(gSMP_ShadowMapSampler, float4(dir - 0.5867f * ax + 0.3215f * ay, (float)cube), reference);
		occ += gDsrlePointCubes.SampleCmpLevelZero(gSMP_ShadowMapSampler, float4(dir - 0.0658f * ax - 0.8797f * ay, (float)cube), reference);
		occ += gDsrlePointCubes.SampleCmpLevelZero(gSMP_ShadowMapSampler, float4(dir + 0.4340f * ax + 0.6504f * ay, (float)cube), reference);
		// Shadows leave before the light does, so no line shows where its reach ends.
		const float edge = saturate((radius - dist) / max(radius * gDsrlePsGlobal.w, 1e-3f));
		vis = 1.0f - occ * 0.25f * edge * edge * (3.0f - 2.0f * edge) * gDsrlePsGlobal.x * place.w;
	}
	return vis;
}

// The row gate of the sun: colA_s 0 is an interior or a night, and above DSRLE_SUN_SPEC_ONLY it is water or lava.
#define DSRLE_ENGINE_ROW_HAS_SUN (max(max(gFC_SpcLightCol.r, gFC_SpcLightCol.g), gFC_SpcLightCol.b) > 0.0f && DSRLE_SUN_ROW_GATE > 0.0f)
#define DSRLE_ENGINE_AMBIENT(diffAmb, specAmb, N, flags) \
	{ \
		const bool dsrleSnow = ((flags) & (DSRLE_VF_FAM_SNOW | DSRLE_VF_BASE_SNOW)) != 0u; \
		const bool dsrleSunRow = DSRLE_ENGINE_ROW_HAS_SUN; \
		/* The rotation of the point lights' shadow taps: interleaved gradient noise, as the sun's filter uses. */ \
		sincos(6.2831853f * frac(52.9829189f * frac(dot(In.VtxClp.xy, float2(0.06711056f, 0.00583715f)))), dsrlePointRot.y, dsrlePointRot.x); \
		float dsrleContact = 1.0f; \
		if (gFC_SAOParams.w != 0.0f) { \
			const float4 dsrleT8 = tex2Dlod(gSMP_AOMap, float4(In.VtxClp.xy * gFC_SAOParams.xy, 0.0f, 0.0f)); \
			dsrleAoForPoints = lerp(1.0f, dsrleT8.r, gDsrleStrength.x); \
			dsrleContact = lerp(1.0f, dsrleT8.g, gDsrleStrength.y); \
		} \
		/* Fill comes down so the key light has something to stand out against. */ \
		/* Snow takes no sun, so it keeps the fill of a row without one. */ \
		(specAmb) *= dsrleContact * lerp((dsrleSunRow && !dsrleSnow) ? gDsrleFill.x : gDsrleFill.z, 1.0f, gDsrleDraw.x); \
		(diffAmb) *= lerp((dsrleSunRow && !dsrleSnow) ? gDsrleFill.y : gDsrleFill.w, 1.0f, gDsrleDraw.x); \
		/* Bounce light joins the ambient, so AO shapes it. An empty slot reads black, and the planar reflection, which is */ \
		/* another camera and draws with AO off, gets none. Ghosts and snow stay as they are, as with the sun. */ \
		if (gFC_SAOParams.w != 0.0f && ((flags) & (DSRLE_VF_GHOST | DSRLE_VF_FAM_SNOW | DSRLE_VF_BASE_SNOW)) == 0u) { \
			(diffAmb) += Mtl.DiffuseColor * gDsrleBounce.Load(int3((int2)In.VtxClp.xy, 0)).rgb; \
		} \
		dsrleSunVisibility = dsrleContact; \
		/* Snow is lit almost entirely by the probe, so a cast shadow would strip its white; it stays vanilla. */ \
		const float dsrleWs = dsrleSnow ? 0.0f : (((flags) & DSRLE_VF_LIT) != 0u) ? gDsrleStrength.w : gDsrleStrength.z; \
		[branch] if (dsrleWs > 0.0f && dsrleSunRow) { \
			const float dsrleOcc = dsrle_WorldOcclusion(In.VtxWld.xyz, In.VtxWld.w, (N), In.VtxClp.xy); \
			dsrleSunVisibility *= 1.0f - dsrleOcc; \
			/* A face turned from the sun is not in a cast shadow: its probe light is the game's own look. */ \
			const float dsrleFacing = smoothstep(0.0f, 0.3f, dot((N), -gFC_ShadowLightDir.xyz)); \
			const float3 dsrleRate = pow(abs(1.0f - gFC_ShadowColor.xyz * dsrleOcc * dsrleFacing), gFC_DebugPointLightParams.z); \
			(specAmb) *= lerp(float3(1.0f, 1.0f, 1.0f), dsrleRate, dsrleWs); \
		} \
	}
#else
#define DSRLE_ENGINE_AMBIENT(diffAmb, specAmb, N, flags)
#endif

//==============================================================================
// FEATURE: REFLECT                                       switch: /D DSRLE_ENGINE
//
// Screen-space reflections (engine/src/reflect.cpp, engine/data/hlsl/reflect.hlsl).
//   SV_Target2  R16G16B16A16_UNORM. xy octahedral normal; z high byte roughness, low byte sqrt(R.r); w high byte sqrt(R.g),
//               low byte sqrt(R.b). R = LightPower * (F0 * DFG.x + F90 * DFG.y) * horizon^2, clamped to 1: what the probe's
//               own lobe sends to the eye of each unit of incoming light. No fog in it: the traced light is read from the
//               scene, so it has already been through the air.
//   SV_Target3  R11G11B10_FLOAT. S + R * f0. S: the specular probe term this pixel really holds, after everything that touches
//               it: LightPower, light map and shadow, specular occlusion, the engine's fill and world shadow, AO, fog.
//               f0: what a black surface shows at this pixel, the haze alone.
//   The composite then does scene + k * (R * traced - confidence * (S + R * f0)), so a hit swaps the probe for the trace, keeping
//   one layer of haze, and a miss leaves the pixel alone. Only Phn and Snow write them; blended draws bind neither, and what
//   they output there is discarded by D3D. The DLL masks them for every other shader.
//   Fog: fog and scattering run in gamma space as out = (A * col^(1/2.2) + B)^2.2. A is known, and out and col give B, so
//   what the pixel would have been without the probe specular has a closed form, S is the exact difference after fog, and
//   f0 is B^2.2.
//   b5 and t24 are bound by the DLL at the first scene copy, for the water draws that follow in that list. An unbound b5
//   reads 0 and turns all of this off.
//==============================================================================
#ifdef DSRLE_ENGINE
#define DSRLE_REFLECT
cbuffer DsrleReflect : register(b5)
{
	float4 gDsrleRefRowX;   // the camera's clip x, y and w rows, camera-relative
	float4 gDsrleRefRowY;
	float4 gDsrleRefRowW;
	float4 gDsrleRefZ;      // xy: z_ndc = x + y / w; zw: size of the pyramid's level 0, half the full size
	float4 gDsrleRefWater;  // x strength, y steps, z thickness in metres, w ray length in metres
	float4 gDsrleRefWater2; // x edge fade, y raw depth from which a pixel counts as sky, z cube fallback, w top level of the pyramid
};
Texture2D<float> gDsrleHiz : register(t24);

static float3 dsrleReflectance = float3(0.0f, 0.0f, 0.0f);
static float3 dsrleProbeSpec = float3(0.0f, 0.0f, 0.0f);
static float3 dsrleLitBeforeFog = float3(1.0f, 1.0f, 1.0f);
static float3 dsrleFogPass = float3(1.0f, 1.0f, 1.0f);
static bool dsrleHasSpecular = false;  // a literal per entry point, so a variant without a specular map folds all of it away

// The probe's lobe, from the same terms the probe function uses. It stays a capture: both inputs go on unchanged.
#define DSRLE_HOOK_IBL(diffIBL, specIBL, N, V, R, roughness) \
	{ \
		const float2 dsrleDfg = CalcSpecularDFG(NdotV, (roughness)); \
		const float dsrleHoriz = saturate(1.0f + 1.3f * dot(vertexNormal, (R))); \
		dsrleReflectance = Mtl.LightPower * (Mtl.SpecularColor * dsrleDfg.x + specularF90 * dsrleDfg.y) * dsrleHoriz * dsrleHoriz; \
		dsrleProbeSpec = Mtl.LightPower * (specIBL); \
	}
#define DSRLE_REFLECT_PROBE_AFTER_OCCLUSION(scale) dsrleProbeSpec *= (scale)
// Whatever the ambient hook scales the probe term by also scales its specular share.
#define DSRLE_REFLECT_PROBE_BEFORE_FILL(specAmb) const float3 dsrleAmbBeforeFill = (specAmb)
#define DSRLE_REFLECT_PROBE_AFTER_FILL(specAmb) \
	dsrleProbeSpec *= (specAmb) / max(dsrleAmbBeforeFill, 1e-6f); \
	if (gFC_SAOParams.w != 0.0f) dsrleProbeSpec *= tex2Dlod(gSMP_AOMap, float4(In.VtxClp.xy * gFC_SAOParams.xy, 0.0f, 0.0f)).r
#define DSRLE_HOOK_FINAL(color, N, V, worldPos, svPos, flags) \
	dsrleHasSpecular = ((flags) & DSRLE_VF_SPC) != 0u; \
	dsrleLitBeforeFog = (color).rgb; \
	dsrleFogPass = (1.0f - gFC_FogCol.a * saturate(In.VecNrm.w)) * lerp(float3(1.0f, 1.0f, 1.0f), \
		exp(-gFC_LsBeta1PlusBeta2.xyz * In.VecEye.w * gFC_LsLightDir.w * 1.4426950f) * gFC_LsTerrainReflectance.rgb, gFC_LsSunColor.a)

float4 dsrle_PackSurface(float3 N, float rough, float3 refl)
{
	const float3 n = N / max(abs(N.x) + abs(N.y) + abs(N.z), 1e-5f);
	float2 o = n.xy;
	if (n.z < 0.0f) {
		o = (1.0f - abs(n.yx)) * float2(n.x >= 0.0f ? 1.0f : -1.0f, n.y >= 0.0f ? 1.0f : -1.0f);
	}
	const float3 e = round(sqrt(saturate(refl)) * 255.0f);
	const float r = round(saturate(rough) * 255.0f);
	// Zero means not written, so the normal keeps off the very corner of the square.
	return float4(max(o * 0.5f + 0.5f, 1.0f / 65535.0f), (r * 256.0f + e.r) / 65535.0f, (e.g * 256.0f + e.b) / 65535.0f);
}

#define DSRLE_REFLECT_OUT(Out, mtl) \
	(Out).DsrleSurface = dsrle_PackSurface((mtl).Normal, (mtl).Roughness, float3(0.0f, 0.0f, 0.0f)); \
	(Out).DsrleProbe = float4(0.0f, 0.0f, 0.0f, 1.0f); \
	if (dsrleHasSpecular) { \
		const float3 dsrleLit = max((mtl).LitColor.rgb, 0.0f); \
		const float3 dsrleB = pow(dsrleLit, 0.45454545f) - dsrleFogPass * pow(max(dsrleLitBeforeFog, 0.0f), 0.45454545f); \
		const float3 dsrleNoSpec = pow(max(dsrleFogPass * pow(max(dsrleLitBeforeFog - dsrleProbeSpec, 0.0f), 0.45454545f) + dsrleB, 0.0f), 2.2f); \
		const float3 dsrleProbeOut = max(dsrleLit - dsrleNoSpec, 0.0f); \
		const float3 dsrleR = saturate(dsrleReflectance); \
		(Out).DsrleSurface = dsrle_PackSurface((mtl).Normal, (mtl).Roughness, dsrleR); \
		(Out).DsrleProbe = float4(dsrleProbeOut + dsrleR * pow(max(dsrleB, 0.0f), 2.2f), 1.0f); \
	}

#ifdef FRPG_FAMILY_WATER
// The planar target's clear colour, which is all it holds when the game did not render it.
#define DSRLE_WATER_PLANAR_EMPTY(col) all(abs((col) - 0.00784314f) < 0.0005f)

float3 dsrle_RefCells(float3 p)
{
	const float4 h = float4(p, 1.0f);
	const float w = dot(gDsrleRefRowW, h);
	return float3((dot(gDsrleRefRowX, h) / w * 0.5f + 0.5f) * gDsrleRefZ.z, (0.5f - dot(gDsrleRefRowY, h) / w * 0.5f) * gDsrleRefZ.w, gDsrleRefZ.x + gDsrleRefZ.y / w);
}

float dsrle_RefEdge(float2 uv)
{
	const float2 e = saturate(min(uv, 1.0f - uv) / gDsrleRefWater2.x);
	const float2 s = e * e * (3.0f - 2.0f * e);
	return s.x * s.y;
}

// The water's reflection ray against the solid depth pyramid (water itself is not in it). Returns the weight of the hit,
// 0 for a miss, and where on screen. Rays that end on visible sky take that sky.
float dsrle_WaterTrace(float3 P, float3 vecRef, out float2 uv)
{
	uv = float2(0.0f, 0.0f);
	float weight = 0.0f;
	bool hit = false;
	// A wave can tip the mirror direction under the surface. It still has to leave the water.
	const float3 R = normalize(float3(vecRef.x, max(vecRef.y, 0.02f), vecRef.z));
	const float w0 = dot(gDsrleRefRowW.xyz, P);
	const float wd = dot(gDsrleRefRowW.xyz, R);
	float len = gDsrleRefWater.w;
	if (wd < 0.0f) len = min(len, (0.1f - w0) / wd);
	if (len > 0.0f) {
		const float3 start = P + float3(0.0f, 0.02f + 0.0005f * w0, 0.0f);
		const float3 o = dsrle_RefCells(start);
		const float3 d = dsrle_RefCells(start + R * len) - o;
		const float2 big = float2(abs(d.x) > 1e-6f ? d.x : 1e-6f, abs(d.y) > 1e-6f ? d.y : 1e-6f);
		const float2 up = float2(big.x > 0.0f ? 1.0f : 0.0f, big.y > 0.0f ? 1.0f : 0.0f);
		const float2 tb = (up * gDsrleRefZ.zw - o.xy) / big;
		const float tEnd = min(1.0f, min(tb.x, tb.y));
		const float tPerCell = 1.0f / max(max(abs(d.x), abs(d.y)), 1e-3f);
		const float nudge = 0.02f * tPerCell;
		const int top = (int)gDsrleRefWater2.w;
		const int steps = (int)gDsrleRefWater.y;
		float t = min(1.5f * tPerCell, 0.1f * tEnd);
		int level = 0;
		float tHit = -1.0f;
		[loop] for (int i = 0; i < steps; i++) {
			if (t >= tEnd || tHit >= 0.0f) break;
			const float2 pxy = o.xy + d.xy * t;
			const float pz = o.z + d.z * t;
			const int2 cell = int2(pxy) >> level;
			const float2 tf = (((float2)cell + up) * (float)(1 << level) - o.xy) / big;
			const float tFace = min(tf.x, tf.y);
			const float zmin = gDsrleHiz.Load(int3(cell, level));
			const float tPlane = (pz < zmin && d.z > 0.0f) ? (zmin - o.z) / d.z : 1e30f;
			if (pz < zmin && tPlane >= tFace) {
				t = max(tFace, t) + nudge;
				level = min(level + 1, top);
			} else {
				if (pz < zmin) t = max(tPlane, t);
				if (level > 0) {
					level--;
				} else {
					const float qz = o.z + d.z * t;
					const float zq = gDsrleRefZ.y / min(qz - gDsrleRefZ.x, -1e-7f);
					const float zs = gDsrleRefZ.y / min(zmin - gDsrleRefZ.x, -1e-7f);
					const float slope = abs(gDsrleRefZ.y / min(min(qz + abs(d.z) * tPerCell, 0.9999999f) - gDsrleRefZ.x, -1e-7f) - zq);
					if (zmin >= gDsrleRefWater2.y) {
						t = tEnd;
					} else if (zq - zs <= gDsrleRefWater.z + slope) {
						tHit = t;
					} else {
						t = max(tFace, t) + nudge;
					}
				}
			}
		}
		if (tHit >= 0.0f) {
			uv = (o.xy + d.xy * tHit) / gDsrleRefZ.zw;
			weight = dsrle_RefEdge(uv);
			hit = true;
		}
	}
	if (!hit && wd > 1e-3f) {
		const float2 onSky = float2(dot(gDsrleRefRowX.xyz, R) / wd * 0.5f + 0.5f, 0.5f - dot(gDsrleRefRowY.xyz, R) / wd * 0.5f);
		if (all(onSky > 0.0f) && all(onSky < 1.0f)) {
			if (gDsrleHiz.Load(int3(int2(onSky * gDsrleRefZ.zw), 0)) >= gDsrleRefWater2.y) {
				uv = onSky;
				weight = dsrle_RefEdge(uv);
			}
		}
	}
	return weight;
}
#endif // FRPG_FAMILY_WATER
#else
#define DSRLE_REFLECT_PROBE_AFTER_OCCLUSION(scale)
#define DSRLE_REFLECT_PROBE_BEFORE_FILL(specAmb)
#define DSRLE_REFLECT_PROBE_AFTER_FILL(specAmb)
#endif // DSRLE_ENGINE

//==============================================================================
// QUALITY FEATURES (shadow filter, specular occlusion, point-light falloff)
//
// Three independent compile-time features, each OFF unless its switch is set:
//   /D DSRLE_SHADOW_SOFT    DSRLE_HOOK_SHADOW      rotated Vogel-disk PCF + cascade cross-fade
//   /D DSRLE_SPEC_OCC       DSRLE_HOOK_AMBIENT     specular occlusion of the light-probe specular
//   /D DSRLE_PL_SMOOTH      DSRLE_HOOK_POINTLIGHT  smooth, bounded point-light falloff
//   /D DSRLE_ALL            all three (the sun is NOT included; add /D DSRLE_SUN_A or _B for it)
// Build, gate, RDEF check and pack: shaders/tools/build_features.py.
// The helper functions below read only gFC_* constants and gSMP_* resources (they are
// parsed at the include point); everything that needs a call-site name is in the hook macro.
// No new cbuffer variables, samplers, textures or globals: the $Globals layout and the
// resource bindings stay those of vanilla, only RDEF "used" flags can change.
// Baseline note: the identity rebuild is SHEX-exact to vanilla on 890 of 902 blobs. The 12 FRPG_Snow_*
// blobs differ from vanilla only in register / swizzle allocation (same instruction count, STAT,
// ISGN and OSGN). Every feature that changes a Snow blob therefore ships that rebuild; the reports
// list them as changed_nonexact_in_identity.
//==============================================================================
#ifdef DSRLE_ALL
	#ifndef DSRLE_SHADOW_SOFT
		#define DSRLE_SHADOW_SOFT
	#endif
	#ifndef DSRLE_SPEC_OCC
		#define DSRLE_SPEC_OCC
	#endif
	#ifndef DSRLE_PL_SMOOTH
		#define DSRLE_PL_SMOOTH
	#endif
#endif

//------------------------------------------------------------------------------
// FEATURE: SHADOW_SOFT  (DSRLE_HOOK_SHADOW)                 switch: /D DSRLE_SHADOW_SOFT
//
// VANILLA (ShadowFunc, verified on the vanilla disassembly of FRPG_Phn_DifSpcBmp______Csd_HemEnv)
//   cascade   slice = (number of gFC_ShadowStartDist (c23) splits below the view depth In.VtxWld.w) - 1,
//             matrix gFC_ShadowMapMtxArray[slice] (c40..c55), rectangle gFC_ShadowMapClamp[slice]
//             (c56..c59; the probe shows the four cascades are the four quadrants of one atlas, t7).
//   clamp     a light-space position outside its cascade rectangle is pushed one atlas width off the
//             map, so the border/address mode of the comparison sampler decides it.
//   kernel    9 SampleCmp (sample_c) at integer texel offsets -1..1, weight 1/9: a 3x3 box of
//             comparison taps (a 4x4-texel tent if the s7 comparison sampler filters linearly; the
//             sampler state is set by the engine and is not visible in the blob).
//   rate      occ = saturate(pcf + saturate((dot(c73, N) + c21.x) * c21.w))   (slope term),
//             fade = saturate((c21.y - eyeVec.w) * c21.z),
//             rate = pow(|1 - fade * c22.rgb * occ|, gFC_DebugPointLightParams.z).
//   The square 3x3 footprint of one shadow texel steps as the texel grid moves under the camera
//   (stair-steps and crawling on edges), and the cascade switch is a hard line.
//
// NEW, the same lookup with three changes (everything else, including the rate composition
// above, the slope term, the fade, ShadowColor/densityRatio and the clamp, is reproduced, so the
// result is a drop-in replacement for the vanilla visibility the hook receives):
//   kernel    DSRLE_SHADOW_SOFT_TAPS (16, or 12) SampleCmpLevelZero taps on a Vogel (golden-angle)
//             disk, centroid removed, radius DSRLE_SHADOW_SOFT_RADIUS = 1.45 texels of the atlas
//             (t7 GetDimensions, so the radius follows the shadow-quality setting). LevelZero is the
//             same texel as vanilla's sample_c on a single-mip depth map, and it is legal inside the
//             cross-fade branch.
//             SIZE, two measures (16 taps; the farthest tap is at 0.967 R):
//               R      reach (largest tap offset along a direction)   per-axis variance
//                      min / mean / max over directions, texels       texel^2
//               box    1.00 / 1.27 / 1.41  (vanilla 3x3)              0.667
//               1.45   1.05 / 1.24 / 1.40                             0.525
//               1.633  1.18 / 1.40 / 1.58                             0.667  (variance match)
//               1.75   1.26 / 1.50 / 1.69                             0.766
//             Reach is what matters for self-shadowing: there is no receiver-plane bias, so a tap
//             offset along the receiver's depth gradient compares against a depth up to slope * reach
//             away, and the bias that keeps vanilla clean was set (in the cascade matrices) for
//             vanilla's reach. R = 1.45 keeps both the mean and the max reach at or below the vanilla
//             box's (the max-reach bound is R <= 1.46), so no tap reaches farther than vanilla's
//             farthest (diagonal) tap, and the kernel is no wider than vanilla's (variance 21% lower,
//             about 11% narrower; with a LINEAR comparison sampler both kernels gain the same 2x2
//             footprint). A round disk exceeds the box's 1.0 axis reach in some directions at any
//             radius above 1.03,
//             so on receivers whose depth gradient is axis-aligned in the atlas the onset of acne can
//             still come slightly earlier than vanilla. /D DSRLE_SHADOW_SOFT_RADIUS=1.75f gives the
//             softer kernel (reach up to 1.69 texels, +20% over vanilla's diagonal, +69% over its axes).
//             The 16-tap table itself is slightly anisotropic (per-axis 0.229 / 0.270 R^2); the
//             rotation averages that out, with NO_ROTATE it shows.
//   rotation  the disk is rotated per pixel. Seed: interleaved gradient noise of the pixel position
//             (In.VtxClp.xy, SV_Position, which every shadowed variant except the unlit Non ones
//             already reads for SSAO or refraction). The pattern is fixed on screen: it is still with a
//             still camera and does not re-randomise when the camera moves (a world-position hash
//             would, since a pixel's interpolated position changes with any camera motion), and the
//             grain does not follow the water waves. It is a fine grain in penumbrae only (fully lit
//             and fully shadowed pixels are unaffected). The unlit Non shaders read no SV_Position
//             in vanilla and their ISGN must not change, so they keep a hash of the position the
//             lookup already reads (In.VtxWld for Csd, In.VtxLit for Sdw); there the grain is still
//             with a still camera but re-randomises every frame while it moves.
//             /D DSRLE_SHADOW_SOFT_NO_ROTATE uses a fixed disk.
//   rim       inside its rectangle the disk centre is clamped (R + 1) texels away from the rectangle
//             edge, so no tap reads a neighbouring cascade's quadrant (vanilla's 3x3 reads up to 2).
//   cascades  (Csd only) over the last DSRLE_SHADOW_SOFT_XFADE = 10% of each cascade's depth range the
//             occlusion is blended towards the next cascade (lerp of the PCF value, before the slope
//             term), so the resolution change is a ramp instead of a line. Only pixels in that band
//             pay for a second lookup ([branch]). The last cascade has no successor; the vanilla
//             distance fade covers it. /D DSRLE_SHADOW_SOFT_NO_XFADE removes the blend.
// LOOKUPS
//   Csd (cascaded PS lookup)  : as above.
//   Ncs (water/snow, one map)  : cascade 0 matrix and rectangle, as vanilla; no cross-fade.
//   Sdw (VS light-space)       : In.VtxLit with rectangle 0, as vanilla; no cross-fade. The hook does
//                                not receive In.VtxLit, so DSRLE_SS_SDWPOS picks it by pasting the
//                                variant's WITH_ShadowMap value (1 = CalcLispPos_VS), and it never
//                                names In.VtxLit in a variant that lacks it.
//   /D DSRLE_SHADOW_SOFT_CSD_ONLY keeps Sdw and Ncs vanilla.
// COST  measured (fxc slots vs the off build): Csd +131..+132 (16 sample_c_lz per lookup instead of
//       9 sample_c, plus the second lookup in the cross-fade branch, taken only in the band),
//       Sdw / Ncs +49..+50; the Non blobs, which hash a position instead, Sdw +58, Csd +139.
//       Per tap: 1 sample + 1 mad.
// RISKS self-shadowing on grazing receivers (N.L about 0.1 to 0.3 against c73) whose depth gradient
//       is close to an atlas axis: there the disk reaches up to 1.40 texels where vanilla reaches 1.0
//       (see SIZE). Not measured in game; if acne shows, lower DSRLE_SHADOW_SOFT_RADIUS (1.03 is the
//       axis-safe limit) rather than raising it.
//       The rotation noise has no temporal filter to hide it (no TAA); it is limited to penumbrae
//       and is a fixed screen pattern (a slight screen-door look in a moving penumbra), except in
//       the 64 unlit Non blobs, where it re-randomises while the camera moves.
//       SampleCmpLevelZero equals vanilla's sample_c only on a single-mip shadow atlas.
//       If the engine's s7 comparison sampler is POINT rather than LINEAR, each tap is binary and
//       the grain is coarser (17 levels instead of a continuous ramp); vanilla would be 10 levels.
//------------------------------------------------------------------------------
#ifdef DSRLE_SHADOW_SOFT

#ifndef DSRLE_SHADOW_SOFT_TAPS
	#define DSRLE_SHADOW_SOFT_TAPS 16
#endif
#ifndef DSRLE_SHADOW_SOFT_RADIUS
	#define DSRLE_SHADOW_SOFT_RADIUS 1.45f
#endif
#ifndef DSRLE_SHADOW_SOFT_XFADE
	#define DSRLE_SHADOW_SOFT_XFADE 0.1f
#endif

// Rotation seeds in [0, 1).
// Interleaved gradient noise (Jimenez 2014) of the pixel centre: fixed on screen, neighbouring
// pixels get well-spread values.
float dsrle_ss_Ign(float2 pixel)
{
	return frac(52.9829189f * frac(dot(pixel, float2(0.06711056f, 0.00583715f))));
}
// Hash of a position's bit pattern (only for the Non shaders, which read no SV_Position): constant
// for a given position, so still with a still camera.
float dsrle_ss_Hash(float3 p)
{
	const uint3 u = asuint(p);
	uint h = (u.x * 73856093u) ^ (u.y * 19349663u) ^ (u.z * 83492791u);
	h ^= h >> 16;
	h *= 0x7feb352du;
	h ^= h >> 15;
	h *= 0x846ca68bu;
	h ^= h >> 16;
	return (float)(h >> 8) * (1.0f / 16777216.0f);
}

// Disk basis in atlas UV (ax, ay = rotated unit axes times the radius), and the rim margin.
// rot = rotation in turns, [0, 1).
void dsrle_ss_Basis(float rot, out float2 ax, out float2 ay, out float2 margin)
{
	float w, h;
	gSMP_ShadowMap.GetDimensions(w, h);
	const float2 texel = 1.0f / float2(w, h);
#ifdef DSRLE_SHADOW_SOFT_NO_ROTATE
	const float s = 0.0f;
	const float c = 1.0f;
#else
	float s, c;
	sincos(rot * 6.2831853f, s, c);
#endif
	ax = float2(c, s) * texel * DSRLE_SHADOW_SOFT_RADIUS;
	ay = float2(-s, c) * texel * DSRLE_SHADOW_SOFT_RADIUS;
	margin = texel * (DSRLE_SHADOW_SOFT_RADIUS + 1.0f);
}

// Vogel disk, unit radius, centroid removed (generated: r = sqrt((i + 0.5) / N), angle = i * 2.39996).
#define DSRLE_SS_TAP(kx, ky) acc += gSMP_ShadowMap.SampleCmpLevelZero(gSMP_ShadowMapSampler, uvz.xy + (kx) * ax + (ky) * ay, uvz.z).x;
float dsrle_ss_Pcf(float3 uvz, float2 ax, float2 ay)
{
	float acc = 0.0f;
#if (DSRLE_SHADOW_SOFT_TAPS == 12)
	DSRLE_SS_TAP(+0.17227f, -0.03891f) DSRLE_SS_TAP(-0.29255f, +0.19991f) DSRLE_SS_TAP(+0.00805f, -0.49360f) DSRLE_SS_TAP(+0.29674f, +0.38969f)
	DSRLE_SS_TAP(-0.63487f, -0.14557f) DSRLE_SS_TAP(+0.53937f, -0.40227f) DSRLE_SS_TAP(-0.22292f, +0.67184f) DSRLE_SS_TAP(-0.39623f, -0.74050f)
	DSRLE_SS_TAP(+0.75870f, +0.24980f) DSRLE_SS_TAP(-0.85430f, +0.30058f) DSRLE_SS_TAP(+0.36462f, -0.88614f) DSRLE_SS_TAP(+0.26113f, +0.89517f)
	return acc * (1.0f / 12.0f);
#else
	DSRLE_SS_TAP(+0.18994f, +0.02709f) DSRLE_SS_TAP(-0.21261f, +0.23391f) DSRLE_SS_TAP(+0.04772f, -0.36668f) DSRLE_SS_TAP(+0.29773f, +0.39826f)
	DSRLE_SS_TAP(-0.50906f, -0.06529f) DSRLE_SS_TAP(+0.50786f, -0.28760f) DSRLE_SS_TAP(-0.15231f, +0.64261f) DSRLE_SS_TAP(-0.30240f, -0.58051f)
	DSRLE_SS_TAP(+0.69780f, +0.27712f) DSRLE_SS_TAP(-0.69910f, +0.32110f) DSRLE_SS_TAP(+0.35651f, -0.70664f) DSRLE_SS_TAP(+0.26689f, +0.83602f)
	DSRLE_SS_TAP(-0.75159f, -0.41610f) DSRLE_SS_TAP(+0.91029f, -0.17015f) DSRLE_SS_TAP(-0.53435f, +0.80586f) DSRLE_SS_TAP(-0.11333f, -0.94900f)
	return acc * (1.0f / 16.0f);
#endif
}
#undef DSRLE_SS_TAP

// One lookup: projective divide, the vanilla off-the-map push for positions outside the cascade
// rectangle, the rim clamp, then the disk.
float dsrle_ss_Lookup(float4 posLight, float4 rect, float2 ax, float2 ay, float2 margin)
{
	float3 uvz = posLight.xyz / posLight.w;
	const float2 push = float2(uvz.xy > rect.zw) - float2(uvz.xy < rect.xy);
	uvz.xy = clamp(uvz.xy, rect.xy + margin, rect.zw - margin) + push;
	return dsrle_ss_Pcf(uvz, ax, ay);
}

// The vanilla visibility composition around a PCF value.
float3 dsrle_ss_Rate(float pcf, float3 N, float4 eyeVec)
{
	const float slope = saturate((dot(gFC_ShadowLightDir.xyz, N) + gFC_ShadowMapParam.x) * gFC_ShadowMapParam.w);
	const float fade = saturate((gFC_ShadowMapParam.y - eyeVec.w) * gFC_ShadowMapParam.z);
	const float3 rate = 1.0f - ((float3)fade) * gFC_ShadowColor.xyz * saturate(pcf + slope);
	return pow(abs(rate), gFC_DebugPointLightParams.z);
}

// Csd: cascade selection as vanilla, plus the cross-fade into the next cascade.
float3 dsrle_ss_Csd(float4 worldPos, float rot, float3 N, float4 eyeVec)
{
	float2 ax, ay, margin;
	dsrle_ss_Basis(rot, ax, ay, margin);
	const float4 wp = float4(worldPos.xyz, 1.0f);
	const float viewZ = worldPos.w;
	const float4 past = float4(gFC_ShadowStartDist < viewZ);
	const int slice = (int)(dot(past, float4(1.0f, 1.0f, 1.0f, 1.0f)) - 1.0f);
	float pcf = dsrle_ss_Lookup(mul(wp, gFC_ShadowMapMtxArray[slice]), gFC_ShadowMapClamp[slice], ax, ay, margin);
#ifndef DSRLE_SHADOW_SOFT_NO_XFADE
	const float4 one = float4(slice == int4(0, 1, 2, 3));
	const float zStart = dot(gFC_ShadowStartDist, one);
	const float zEnd = dot(float4(gFC_ShadowStartDist.yzw, 0.0f), one);
	const float band = max((zEnd - zStart) * DSRLE_SHADOW_SOFT_XFADE, 1e-4f);
	const float t = (slice < 3) ? saturate((viewZ - (zEnd - band)) / band) : 0.0f;
	[branch] if (t > 0.0f)
	{
		const int next = min(slice + 1, 3);
		pcf = lerp(pcf, dsrle_ss_Lookup(mul(wp, gFC_ShadowMapMtxArray[next]), gFC_ShadowMapClamp[next], ax, ay, margin), t);
	}
#endif
	return dsrle_ss_Rate(pcf, N, eyeVec);
}

// Sdw / Ncs: one light-space position, rectangle 0 (as vanilla).
float3 dsrle_ss_Single(float4 posLight, float rot, float3 N, float4 eyeVec)
{
	float2 ax, ay, margin;
	dsrle_ss_Basis(rot, ax, ay, margin);
	return dsrle_ss_Rate(dsrle_ss_Lookup(posLight, gFC_ShadowMapClamp[0], ax, ay, margin), N, eyeVec);
}

// In.VtxLit for the Sdw variants only (WITH_ShadowMap: 1 = CalcLispPos_VS, 2 = _PS / _PS_Csd,
// 3 = _PS_NoCsd, undefined = unshadowed).
#define DSRLE_SS__CAT(a, b) a##b
#define DSRLE_SS_CAT(a, b) DSRLE_SS__CAT(a, b)
#define DSRLE_SS_LITPOS_1 (In.VtxLit)
#define DSRLE_SS_LITPOS_2 float4(0.0f, 0.0f, 0.0f, 1.0f)
#define DSRLE_SS_LITPOS_3 float4(0.0f, 0.0f, 0.0f, 1.0f)
#define DSRLE_SS_LITPOS_WITH_ShadowMap float4(0.0f, 0.0f, 0.0f, 1.0f)
#define DSRLE_SS_SDWPOS DSRLE_SS_CAT(DSRLE_SS_LITPOS_, WITH_ShadowMap)
// Rotation seed: the pixel position (In.VtxClp.xy), except in the unlit Non shaders, which read no
// SV_Position in vanilla (reading it would change the ISGN used mask). They hash a position their
// vanilla lookup already reads: the world position for Csd, the light-space position for Sdw (their
// Sdw lookup reads no world position either). `flags` is a literal, so the unused arm folds away.
#define DSRLE_SS_SEED(hashPos, flags) ((((flags) & DSRLE_VF_BASE_NON) != 0u) ? dsrle_ss_Hash(hashPos) : dsrle_ss_Ign(In.VtxClp.xy))

#ifdef DSRLE_SHADOW_SOFT_CSD_ONLY
	#define DSRLE_SS_NCS_ON(flags) false
	#define DSRLE_SS_SDW_ON(flags) false
#else
	#define DSRLE_SS_NCS_ON(flags) (((flags) & DSRLE_VF_NCS) != 0u)
	#define DSRLE_SS_SDW_ON(flags) (((flags) & DSRLE_VF_SDW) != 0u)
#endif

#define DSRLE_HOOK_SHADOW(rate, worldPos, N, eyeVec, flags) \
	((((flags) & DSRLE_VF_CSD) != 0u) ? dsrle_ss_Csd((worldPos), DSRLE_SS_SEED((worldPos).xyz, flags), (N), (eyeVec)) : \
	 DSRLE_SS_NCS_ON(flags) ? dsrle_ss_Single(mul(float4((worldPos).xyz, 1.0f), gFC_ShadowMapMtxArray[0]), DSRLE_SS_SEED((worldPos).xyz, flags), (N), (eyeVec)) : \
	 DSRLE_SS_SDW_ON(flags) ? dsrle_ss_Single(DSRLE_SS_SDWPOS, DSRLE_SS_SEED(DSRLE_SS_SDWPOS.xyz, flags), (N), (eyeVec)) : \
	 (rate))

#endif // DSRLE_SHADOW_SOFT

//------------------------------------------------------------------------------
// FEATURE: SPEC_OCC  (DSRLE_HOOK_AMBIENT)                    switch: /D DSRLE_SPEC_OCC
//
// VANILLA  probe term = LightPower * (diffuse probe * albedo + specular probe * DFG * horizon fade),
//   then * lightRate (light map, min(shadow, light map) or shadow), then (with the hemisphere ambient)
//   * SSAO (gSMP_AOMap at In.VtxClp.xy * gFC_SAOParams.xy, when gFC_SAOParams.w != 0). So the probe
//   specular is occluded exactly like the diffuse: linearly, whatever the view angle and roughness.
//   A glossy surface in a crease or a dark baked corner therefore keeps its bright grazing-angle
//   (Fresnel-boosted) sky reflection at the same fraction as its diffuse fill; that rim is the glow.
//
// VISIBILITY AVAILABLE AT THE AMBIENT CALL SITE (the hook macro expands there)
//   lightRate          light map (and real-time shadow) -- already applied by vanilla, rgb.
//   dsrleLightmap.rgb  baked light only (no real-time shadow), 1 without a light map.
//   SSAO               re-sampled here; vanilla applies it after the hook.
//   vertex colour      folded into the albedo (In.ColVtx) with unknown meaning (tint, blend
//                      weights); not separable, not used.
//   LightPower         already squared onto the probe term by vanilla; not used.
//   USED: vis = saturate(CalcLuminance(dsrleLightmap.rgb) * DSRLE_SPEC_OCC_BAKED_K) * SSAO.
//   The real-time shadow is left out on purpose: it is the sun's visibility, not the sky's, and
//   vanilla already darkens the probe with it.
//   K = 2: baked luminance >= 0.5 counts as open; only darker baked texels count as occluded.
//
// NEW  only the specular share of the probe term is rescaled by
//        f = min(1, SO / vis),  SO = saturate((NdotV + vis)^(2^(-16 a - 1)) - 1 + vis),  a = Roughness^2
//   (SO: Lagarde & de Rousiers 2014, specular occlusion from AO; a = GGX alpha, the game's
//   Mtl.Roughness is perceptual). Vanilla already multiplies the specular by vis' (a superset of
//   vis), so f only reshapes that occlusion by view angle and roughness, and min(1, .) means it
//   never adds energy:
//     vis = 1             f = 1 exactly (open areas unchanged, any angle or roughness)
//     NdotV + vis >= 1    f = 1 (facing the camera, or only lightly occluded)
//     rough materials     f >= 0.985 at every NdotV for roughness 0.7 when vis >= 0.22, 0.8 when
//                         vis >= 0.07, 0.9 when vis >= 0.02, 1.0 when vis >= 0.003. Below those
//                         visibilities f falls (roughness 0.7 at NdotV 0: 0.95 at vis 0.1, 0.87 at 0.05,
//                         0.75 at 0.03, 0 at 0.01): pow(NdotV + vis, tiny exponent) stays just under 1,
//                         so SO lags vis when vis is small. That specular is already at vis times its
//                         open value, so the extra loss, (1 - f) * vis, is at most 0.010 (roughness 0.7),
//                         0.0025 (0.8), 0.0004 (0.9) of the unoccluded probe specular.
//     roughness 0.5       vis 0.2: f 0.79 / 0.86 / 0.95 at NdotV 0.05 / 0.2 / 0.5
//     roughness 0.3       vis 0.5: f 0.79 / 0.87 / 1 at NdotV 0.05 / 0.2 / 0.5;
//                         vis 0.2: f 0 / 0.22 / 0.68
//     roughness 0.1       vis 0.5: f 0.53 / 0.71 / 1;  vis 0.8: f 0.91 / 1 / 1
//   The diffuse probe share is recomputed at the call site (CalcDiffuseLD(N), the same cube sample
//   CalcEnvIBL took; fxc merges the two), so specular = probe term - diffuse share. The SSAO read
//   is also merged with the vanilla one (one t8 sample, hoisted out of the vanilla branch).
// GATE  variants with a PBL/specular map only (DSRLE_VF_SPC: without one, roughness is 1 and f = 1).
// PARAMETERS  DSRLE_SPEC_OCC_BAKED_K (2.0f), /D DSRLE_SPEC_OCC_NO_SSAO, /D DSRLE_SPEC_OCC_NO_BAKED.
//   With NO_SSAO only the light-mapped variants change (vis is 1 elsewhere and f folds to 1).
// COST  +15..+22 fxc slots, no extra texture reads.
// RISKS  SSAO halos (dark rims around characters on walls) also cut the grazing glossy
//   reflection inside the halo. A dim but open baked area (luminance < 0.5) counts as partly
//   occluded, so glossy floors there lose some grazing sheen; K = 2 keeps that mild (f >= 0.89 at
//   luminance 0.3, roughness 0.3, NdotV 0.1).
// ASSUMES  DSRLE_HOOK_IBL is the identity (checked below).
//------------------------------------------------------------------------------
#ifdef DSRLE_SPEC_OCC

#if defined(DSRLE_HOOK_IBL) && !defined(DSRLE_REFLECT)
	#error "DSRLE_SPEC_OCC recomputes the diffuse probe share and assumes DSRLE_HOOK_IBL is the identity"
#endif
#ifndef DSRLE_SPEC_OCC_BAKED_K
	#define DSRLE_SPEC_OCC_BAKED_K 2.0f
#endif

// Extra factor on the probe specular, given the visibility vanilla already applied.
float dsrle_so_Factor(float NdotV, float vis, float roughness)
{
	const float alpha = roughness * roughness;
	const float so = saturate(pow(NdotV + vis, exp2(-16.0f * alpha - 1.0f)) - 1.0f + vis);
	return min(1.0f, so / max(vis, 1e-3f));
}

#ifdef DSRLE_SPEC_OCC_NO_BAKED
	#define DSRLE_SO_BAKED(flags) 1.0f
#else
	#define DSRLE_SO_BAKED(flags) ((((flags) & DSRLE_VF_LIT) != 0u) ? saturate(CalcLuminance(dsrleLightmap.rgb) * DSRLE_SPEC_OCC_BAKED_K) : 1.0f)
#endif
#ifdef DSRLE_SPEC_OCC_NO_SSAO
	#define DSRLE_SO_SSAO 1.0f
#else
	#define DSRLE_SO_SSAO ((gFC_SAOParams.w != 0.0f) ? tex2Dlod(gSMP_AOMap, float4(In.VtxClp.xy * gFC_SAOParams.xy, 0.0f, 0.0f)).r : 1.0f)
#endif

#define DSRLE_HOOK_AMBIENT(diffAmb, specAmb, N, V, worldPos, lightRate, flags) \
	if (((flags) & DSRLE_VF_SPC) != 0u) { \
		const float3 dsrleSoDiff = Mtl.LightPower * (CalcDiffuseLD(N) * Mtl.DiffuseColor) * (lightRate); \
		const float dsrleSoF = dsrle_so_Factor(saturate(dot((N), (V))), DSRLE_SO_BAKED(flags) * DSRLE_SO_SSAO, Mtl.Roughness); \
		(specAmb) = dsrleSoDiff + dsrleSoF * ((specAmb) - dsrleSoDiff); \
		DSRLE_REFLECT_PROBE_AFTER_OCCLUSION((lightRate) * dsrleSoF); \
	} \
	DSRLE_REFLECT_PROBE_BEFORE_FILL(specAmb); \
	DSRLE_ENGINE_AMBIENT(diffAmb, specAmb, N, flags) \
	DSRLE_REFLECT_PROBE_AFTER_FILL(specAmb);

#endif // DSRLE_SPEC_OCC

//------------------------------------------------------------------------------
// FEATURE: PL_SMOOTH  (DSRLE_HOOK_POINTLIGHT)                switch: /D DSRLE_PL_SMOOTH
//
// VANILLA  this generation has ONE falloff, not LAMP_FALLOFF_QLOC: PointLightContribution calls
//   perceivedLinear (rebuild note "single falloff curve, no falloffMode switch", and the vanilla
//   disassembly of the clustered loop: add r - d, mul by position.w, x*x, mul_sat x*x^2):
//     old(d) = saturate(x^3),  x = (r - d) * k,  r = lightColor.w (falloff end),
//     k = lightPosition.w = 1 / (end - start).
//   So the light is flat (old = 1) out to the falloff start, then a cubic to 0 at r. The flat top
//   has no distance falloff at all, and its edge is a slope discontinuity (0 -> -3k) that shows as
//   a ring on floors and walls. The tail (x^3, zero slope and curvature at r) is already smooth.
//   DrawParam PointLightBank (636 rows): start/end median 0.25 (0.5 / 2.0 m is 46% of rows),
//   11% >= 1/3, end radius median 3 m.
//
// WHY NOT A PLAIN UE4 CURVE  saturate(1 - (d/r)^4)^2 / (d^2 + 1) normalised to old at r/3 decays far
//   more slowly than the vanilla cubic near r (its window is quadratic there, vanilla's is cubic):
//   over the PointLightBank rows it is a median 865x vanilla near the edge, and a 15% cap would bind
//   over 61% of the radius, i.e. it would just be vanilla + 15% with a new kink. It also has an
//   absolute 1 m^2 bias, so it is gentle for a 2 m torch and steep for a 15 m bonfire.
//
// NEW  a windowed inverse-square whose window is the art-directed vanilla cubic:
//     y(d) = min(x^3, 64)                    vanilla window without its clip at 1
//     I(d) = CAP / (1 + (d / (BETA r))^2)    inverse-square near field (sphere-light form), I(0) = CAP
//     new  = y / (1 + (y / I)^8)^(1/8)       smooth minimum of y and I (p = 8, C-infinity)
//     contrib *= min(new / max(old, 1e-6), CAP)
//   CAP = DSRLE_PL_SMOOTH_CAP = 1.15, BETA = DSRLE_PL_SMOOTH_BETA = 1.0.
//   Bounds, analytically: new <= min(y, I) <= min(y, CAP) and old = min(y, 1), so new <= CAP * old
//   at every distance (the min(., CAP) is only a float guard). new = 0 where old = 0, the ratio is
//   finite (old floored at 1e-6, y clamped at 64, y/I clamped at 1e4), contrib stays finite.
//   Shape: the flat top becomes a gentle inverse-square peak (+15% at the source, I(r/3) = 1.035),
//   the ring at the falloff start is rounded off, and the tail is vanilla (y << I there).
//   Normalisation at r/3 (y0 = old(r/3) for start/end <= 1/3, I0 = 1.15 / (1 + 1/9) = 1.035):
//     new/old = 1 / (1 + (y0/I0)^8)^(1/8): 0.995 for start/end 0.25 (y0 = (0.667/0.75)^3 = 0.702),
//     1.000 for start/end <= 0.125 (y0 <= 0.43); a light still flat at r/3 (start/end > 1/3) gets
//     smooth-min(y0 > 1, 1.035) = 0.93..1.035.
//   Over the 636 PointLightBank rows (shaders/tools/build_features.py --pl-table):
//     new/old at r/3   p10 0.995, median 0.995, p90 1.000, range 0.93..1.035
//     at the source    median +15%, never above +15%; 19% of rows below +10% (start 0: -3.5%)
//     dip at the old falloff start  median -5% (p10 -7%); worst -15..-24% for the 6 rows with
//                      start/end >= 0.6, whose long flat top now falls off like a light
//     beyond r/2       identical to vanilla (ratio >= 0.999) for 92% of rows
//   Most common row (start 0.5, end 2.0 m): ratio 1.15 / 1.12 / 0.95 / 0.995 / 1.000 at
//   d = 0 / 0.25 / 0.5 / 0.67 / >= 1.0 m.
// CALL SITES  the hook does not receive k; it is read from the light's position .w by pasting the
//   lightColor argument (lightColor -> lightPosition, gFC_PntLightCol[i] -> gFC_PntLightPos[i]).
//   A new call site with another spelling fails to compile. Covered: the clustered loop and the
//   legacy loop (FaceEye PntS), i.e. the map's PointLightBank lights the statistics above describe.
//   lightID is the id in the clustered light buffers, or -1 where there is none (legacy and ghost lights).
//   NOT covered by default: the Gst ghost light (gFC_GhostLightCol / gFC_GhostLightPos, the light the
//   phantom shaders carry). Its falloff (begin, end) is set by the engine, not by PointLightBank, and
//   the probe only saw it idle (rgb 0), so none of the numbers above are known to apply to it; a
//   ghost whose flat top covers most of its radius would be lit up to +15% over that whole top. It
//   keeps the vanilla falloff: its spelling selects an overload of dsrle_pl_Factor that returns 1,
//   which fxc folds away. /D DSRLE_PL_SMOOTH_GHOST opts it in.
// COST  +21 fxc slots per light loop (ALU only); +42 in Gst with DSRLE_PL_SMOOTH_GHOST.
// RISKS  the +15% hotspot right at the source is visible on surfaces touching a torch or bonfire;
//   lower it with /D DSRLE_PL_SMOOTH_CAP=1.1f. Point-light SFX that rely on a long flat top
//   (start/end >= 0.6) lose 15-24% at the old falloff start.
//------------------------------------------------------------------------------
#ifdef DSRLE_PL_SMOOTH

#ifndef DSRLE_PL_SMOOTH_CAP
	#define DSRLE_PL_SMOOTH_CAP 1.15f
#endif
#ifdef DSRLE_ENGINE
	#define DSRLE_PL_SMOOTH_BETA gDsrleLight.w  // the engine stretches its lights, so their falloff is a setting
#endif
#ifndef DSRLE_PL_SMOOTH_BETA
	#define DSRLE_PL_SMOOTH_BETA 1.0f
#endif

// new falloff / vanilla falloff, in [0, CAP].
float dsrle_pl_Factor(float dist, float radius, float invRange)
{
	const float x = max((radius - dist) * invRange, 0.0f);
	const float x3 = x * x * x;
	const float old = saturate(x3);
	const float y = min(x3, 64.0f);
	const float t = dist / max(DSRLE_PL_SMOOTH_BETA * radius, 1e-4f);
	const float I = DSRLE_PL_SMOOTH_CAP / (1.0f + t * t);
	const float q = min(y / I, 1e4f);
	const float q2 = q * q;
	const float q4 = q2 * q2;
	const float smin = y / sqrt(sqrt(sqrt(1.0f + q4 * q4)));
	return min(smin / max(old, 1e-6f), DSRLE_PL_SMOOTH_CAP);
}

// Vanilla falloff (factor 1). Selected by an int third argument; see DSRLE_PL__POS_gFC_GhostLightCol.
float dsrle_pl_Factor(float dist, float radius, int keepVanilla)
{
	return 1.0f;
}

#define DSRLE_PL__POS_lightColor lightPosition
#define DSRLE_PL__POS_gFC_PntLightCol gFC_PntLightPos
#ifdef DSRLE_PL_SMOOTH_GHOST
	#define DSRLE_PL__POS_gFC_GhostLightCol gFC_GhostLightPos
#else
	// .w of this is an int, which picks the vanilla overload above for the ghost light.
	#define DSRLE_PL__POS_gFC_GhostLightCol int4(0, 0, 0, 0)
#endif

#ifdef DSRLE_ENGINE
#define DSRLE_HOOK_POINTLIGHT(contrib, L, dist, lightColor, N, V, worldPos, lightShadow, lightID) \
	(contrib) *= dsrle_pl_Factor((dist), (lightColor).w, (DSRLE_PL__POS_##lightColor).w) * dsrleAoForPoints * gDsrleLight.x * \
		dsrle_PointVisibility((int)(lightID), (worldPos), (N), (dist), (lightColor).w)
#else
#define DSRLE_HOOK_POINTLIGHT(contrib, L, dist, lightColor, N, V, worldPos, lightShadow, lightID) \
	(contrib) *= dsrle_pl_Factor((dist), (lightColor).w, (DSRLE_PL__POS_##lightColor).w)
#endif

#endif // DSRLE_PL_SMOOTH

#endif // DSRLE_FEATURES_FXH
