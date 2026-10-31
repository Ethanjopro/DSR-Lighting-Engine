//==============================================================================
// dsrle_filter_features.fxh -- DSRLE feature definitions for the rebuilt
// FRPG_Filter_DX11 post-process shaders: FRPG_Fil_ResolveTAA.fpo (the final AA
// pass at Antialiasing=3; data/filter_census.json). Original DSRLE code (no game source).
//
// Included from shaders/hooks/dsrle_filter_hooks.fxh only with /D DSRLE_FEATURES, ahead of
// the identity defaults. build_filter.py already puts shaders/hooks and shaders/features on
// the include path. With DSRLE_FEATURES set but no switch below, this file defines nothing
// and the build stays identical to the identity build. Everything is compiled only into the
// pixel shader (_FRAGMENT_SHADER), so the shared VertexMain_Base vpo built from the same .fx
// never changes.
//
// SWITCHES
//   /D DSRLE_TAA_CATMULL   Catmull-Rom history fetch            (DSRLE_TAA_HOOK_HISTORY)
//   /D DSRLE_TAA_SHARPEN   adaptive sharpening of the current   (DSRLE_TAA_HOOK_CURRENT)
//                          sample before the history blend
//   /D DSRLE_TAA_ALL       both
// PARAMETERS (compile-time; the engine binds $Globals by offset, so there are no new constants)
//   DSRLE_TAA_CATMULL_TAPS    9 (default) exact separable Catmull-Rom as 9 bilinear taps;
//                             5: the four corner taps dropped and the rest renormalised
//                             (each corner weight is at most 0.0055, all four together at
//                             most 0.016 of the total);
//                             16: sampler-independent fallback, all 16 texels at exact texel
//                             centres (see CATMULL, Risks).
//   DSRLE_TAA_SHARPEN_AMOUNT  0.3 default. Range 0 (off) .. 1 (strong); see SHARPEN. Above 1 the
//                             neighbourhood clamp starts to flatten edge profiles; not advised.
//   DSRLE_TAA_SHARPEN_EDGE    0.6 default. Local luma (Michelson) contrast at which the sharpening
//                             has faded to zero. Lower = keep off more edges.
//
// WHERE THE TAA SOFTNESS COMES FROM (vanilla_src/rebuild/filter/NOTES.md section 5)
//   ResolveTAA keeps the history in one colour target (SV_Target0 = display AND next frame's
//   history; SV_Target1 only carries the motion-blur info) and blends the current frame in with
//   a small, adaptive weight (at most ~6% for a still pixel). Every frame the history is
//   re-fetched with ONE bilinear tap at the reprojected uv. At a fractional offset a bilinear
//   tap is a 2-tap low-pass (response cos(w/2) at half a texel), and because the result is fed
//   back, that blur compounds over the ~16-frame memory of the history whenever the camera or
//   the content moves by sub-pixel amounts. That is the main source of the 4K softness.
//
//------------------------------------------------------------------------------
// FEATURE: CATMULL  (DSRLE_TAA_HOOK_HISTORY)
//   Replaces the single bilinear history tap by a Catmull-Rom (cubic convolution, a = -1/2)
//   reconstruction of the 4x4 history texels around histUV. Catmull-Rom interpolates (it
//   returns the texel itself at zero offset, so a still camera is unchanged) and its
//   resampling response stays much closer to 1 at mid frequencies (0.88 vs 0.71 for bilinear
//   at w = pi/2, half-texel offset), so repeated resampling under motion loses far less detail.
//   Technique (written from the published descriptions, no code copied):
//     E. Catmull, R. Rom, "A class of local interpolating splines" (1974);
//     R. Keys, "Cubic convolution interpolation for digital image processing" (IEEE TASSP 1981);
//     merging two same-sign taps into one bilinear fetch at offset w2/(w1+w2): C. Sigg,
//     M. Hadwiger, "Fast third-order texture filtering" (GPU Gems 2, ch. 20, 2005); applied to
//     a Catmull-Rom history fetch with the corner taps dropped (5 taps): J. Jimenez, "Filmic
//     SMAA" (SIGGRAPH 2016, Advances in Real-Time Rendering).
//   Per axis the four weights are w0..w3 at texels i-1, i, i+1, i+2. The inner pair is always
//   positive with w1 + w2 = 1 + f(1-f)/2 >= 1, so it merges into ONE bilinear tap, giving 3 taps
//   per axis and a 3x3 grid of bilinear taps (9), or the 5-tap cross of it. The outer weights are
//   the negative lobes (each >= -0.075).
//   Kept vanilla: the m-space mapping c/(1+Y), the per-channel clamp to the rounded 3x3 box,
//   the blend weight, resets and the off-screen test. The fetch runs in linear HDR exactly like
//   the vanilla bilinear tap, so moving highlights keep their energy (an m-space reconstruction
//   would dim small moving speculars).
//   Guards
//   * negative lobes: the reconstruction is max(.., 0) per channel BEFORE the c/(1+Y) mapping.
//     A lobe next to a 10..50 highlight can be strongly negative; without the max the luma could
//     reach -1 and the mapping divides by ~0. What overshoot remains is then removed by the
//     vanilla box clamp (the history can never leave the current 3x3 box).
//   * addressing: every tap uv is clamped to [0.5, size - 0.5] texels. For bilinear taps this is
//     exactly CLAMP addressing, whatever address mode the engine's s1 uses, so the outer taps
//     never wrap (taa_warp_test.py: byte-identical output with WRAP, MIRROR and CLAMP samplers).
//     Off-screen history (histUV outside [0,1]) is still a full reset in vanilla.
//   * texel size: from gSMP_1.GetDimensions (one resinfo), so it does not assume the history
//     target has the screen size (gFC_ScreenSize), and reads no constant vanilla does not read.
//   Cost (fxc slots, vanilla fpo 194): 9 taps 261 (+67: +8 sample_l, +1 resinfo), 5 taps 244 (+50),
//   16 taps 287 (+93: +15 sample_l). At 3840x2160 the 9-tap form is ~66 M extra filtered fetches
//   of a cache-friendly 4x4 footprint per frame: well under 0.1 ms on current GPUs. Both features
//   together: 314 slots, 23 temps (10).
//   9 taps is the default: it is the exact separable filter, whose resampling gain is <= 1 at every
//   offset and frequency with exact bilinear weights, so it cannot amplify through the history
//   feedback. Hardware quantises the bilinear fraction to 8 bits, which lifts the per-axis worst
//   case to about 1.004 (fraction rounded to nearest) or 1.008 (truncating hardware): at near-zero
//   sub-pixel offsets (f below 1/256 or 1/128) the merged tap's fraction snaps to 0 while a tiny
//   negative lobe remains, gain 1 + f at the pixel Nyquist; about the square of that in 2-D. The
//   excess only beats the blend's damping for a history within ~6..13% of a box edge, and the box
//   clamp stops it there: immaterial. The 5-tap form reaches 1.0044 (diagonal, half-texel) and is
//   slightly sharper in exchange; the 16-tap form applies the exact weights in the shader (no
//   fraction quantisation), so its gain stays <= 1; a truncating sampler can only add the tiny
//   positive low-pass described below.
//   16 taps (fallback, built test-only by build_taa_variants.py): each of the 16 texels is taken at
//   its texel centre through s1, where a bilinear and a point tap both return the texel, so the
//   result depends on neither s1's filter nor its address mode (taa_warp_test.py on WARP:
//   byte-identical with a point / bilinear s1 at 64, 100 and 3840 texels wide, and with WRAP /
//   MIRROR / CLAMP; within 2.8e-4 of the 9-tap form with a bilinear s1). The centre uv is computed
//   in float, so at a non-power-of-two size it can sit a few 1e-4 texel off the centre; hardware
//   that truncates (rather than rounds) the 8-bit fraction could then blend up to 1/256 of a
//   neighbour into a tap, a bounded low-pass. A Load-only version would be exact too, but it leaves
//   s1 unused, so fxc drops the s1 binding from RDEF and the interface gate refuses the blob.
//   Risks
//   * The 9 / 5-tap forms need s1 to be a BILINEAR sampler (NOTES section 5: the vanilla shader
//     fetches t1 with one SampleLevel at a fractional uv; the sampler state is not observed in
//     game). With a point sampler the merged inner tap snaps to a texel while the negative lobes
//     stay: a sharpening kernel (per-axis gain up to 1.20, 2-D 1.44) applied to the history every
//     frame under motion. WARP, moving texture, s1 point: high-pass energy 3.0x the truth (vanilla
//     with a point s1 0.90, the 9-tap form with a bilinear s1 0.69): crunchy, over-sharpened
//     texture while panning, bounded by the box clamp. At zero velocity the fetch is exact with
//     either sampler, so only a pan can show it; the fix is DSRLE_TAA_CATMULL_TAPS=16. A point s1
//     is unlikely: it would also make vanilla's own reprojection nearest-neighbour (WARP, moving
//     4-px grating with a point s1: vanilla keeps 0.07 of the contrast and pulses strongly).
//   * Flicker if the engine's velocity includes the jitter (unknown offline; in-game check 1).
//     The history is then resampled at a sub-pixel offset every frame even at a standstill, and the
//     sharper fetch lets the image follow the jitter. WARP static_jitter_in_velocity, per-pixel
//     output temporal std vs vanilla: low-contrast texture 1.70x, 8-px grating 1.33x, 4-px grating
//     2.5x, thin lines 2.0x, soft edge 1.0x. At the same time the rmse against the converged
//     (jitter-mean) image falls to 0.47..0.80x and the detail energy rises 1.4..2.4x: the extra
//     flicker is the cost of the lower temporal blur and tracks the retained detail. Without jitter
//     in the velocity, motion alone adds no flicker: the detail-energy pulsing under 0.37 and
//     0.5 px/frame motion is lower than vanilla's (4-px grating 0.011 vs 0.038, thin lines 0.027 vs
//     0.079), while peak values follow the truth's own sampling-phase variation more closely (thin
//     lines 0.062 vs 0.048; the truth itself 0.17). In-game check 2 (railings, branches and fine
//     texture while panning) is the gating A/B for the cr and all* packs. If they shimmer, the
//     next knob is a compile-time mix between the vanilla bilinear tap and this fetch (not built).
//   * Inside the box the lobes can deepen thin dark/bright features under motion by a little.
//
//------------------------------------------------------------------------------
// FEATURE: SHARPEN  (DSRLE_TAA_HOOK_CURRENT)
//   Contrast-adaptive sharpening of the CURRENT-frame centre sample, in the same m-space the
//   shader clamps in, using only the 3x3 neighbourhood the shader already loaded (no extra taps).
//     hp       = centre - binomial 3x3 blur (1 2 1 / 2 4 2 / 1 2 1) / 16, written as a weighted
//                sum of (centre - neighbour) differences, so a flat neighbourhood gives exactly 0.
//                Response 1 - cos^2(wx/2) cos^2(wy/2): isotropic-ish and never above 1, unlike the
//                4-neighbour Laplacian, which doubles the pixel checkerboard (pure aliasing).
//     contrast = (Ymax - Ymin) / (Ymax + Ymin) over the 9 m-space lumas (Michelson contrast,
//                scale-free, so it reads the same in dark and bright areas, and dark noise counts
//                as high contrast and is left alone).
//     adapt    = 1 - smoothstep(0, DSRLE_TAA_SHARPEN_EDGE, contrast)
//                full strength on low-contrast texture detail, fading to nothing on strong edges.
//                Strong edges are exactly where TAA's anti-aliased gradients live; sharpening them
//                would bring the stair-steps back and make halos (the CAS idea: let local contrast
//                limit the sharpening, here with an original weight).
//     k        = 2 * DSRLE_TAA_SHARPEN_AMOUNT * adapt * (1 - blend)
//     cur'     = clamp(cur + k * hp, boxMin, boxMax)    (per channel, the rounded 3x3 box)
//   At adapt = 1 the high-pass gain is 1 + 2*amount at the pixel Nyquist and 1 + amount at a
//   4-pixel period: amount 0.3 gives 1.6 / 1.3, amount 0.5 gives 2.0 / 1.5.
//   Why the current sample and not the output (feedback / stability)
//     SV_Target0 is both what is shown and next frame's history, so sharpening the output would
//     apply the filter once per frame to the accumulated image: h' = S((1-b) h + b c). With a
//     high-pass gain g > 1 this diverges at every frequency where (1-b) g > 1, i.e. for any g
//     above ~1.06 at b = 0.06: accumulated over-sharpening, ringing and shimmer, held only by the
//     clamp. Instead the filter is applied to the FRESH t0 sample only: h' = (1-b) clamp(h) +
//     b S(c). The history is never filtered, so for a still image the fixed point is h = S(c),
//     the filter applied exactly once, however many frames accumulate. The blend weight, the box
//     and every reset decision are computed from the unmodified sample (the hook runs after
//     them), so the vanilla convergence logic is unchanged.
//   Bounds
//     * cur' is clamped to the same rounded box the history is clamped to. The output (a convex
//       blend of two values in that box) therefore always stays in the 3x3 box of the current
//       frame: no halo can exceed the local min/max, and next frame's clamp sees a value it
//       accepts, so the sharpened target and the clamp never fight (no flicker).
//     * (1 - blend): pixels whose history is being reset (off-screen reprojection, masked
//       disocclusion; blend -> 1) show the raw, aliased, jittered current frame; they are left
//       unsharpened. Steady state is unaffected (blend there is ~0.02..0.06).
//     * Flat areas: hp = 0 exactly and the box collapses to the pixel, so the output is
//       bit-identical to vanilla.
//   Cost: 250 fxc slots vs 194 (+56 ALU), no texture fetches.
//   Build-up: the sharpened sample enters only through the blend, and vanilla's blend weight
//   shrinks as the history nears a box edge, so the image firms up gradually after the camera
//   stops. WARP sharpen_buildup (60 fps), share of the target reached: no jitter 37% after 16
//   frames, 57% after 32 (0.5 s), 76% after 64 (1 s), 88% after 128 (2 s), 95% after 400; with
//   jitter 33 / 58 / 81 / 95% after 16 / 32 / 64 / 128 frames. So roughly 0.5 to 2 s when standing
//   still. In motion the history is resampled and refreshed, so the effect fades (moving texture
//   with jitter, detail energy vs the truth: all 0.74, cr 0.67, vanilla 0.43). A post-TAA sharpen
//   in the tonemap pass (HDR_PBL_ColAdj runs after ResolveTAA and does not feed the history) would
//   act immediately and in motion; not built.
//   Risks: the current sample is the raw jittered frame, so its frame-to-frame (sub-pixel)
//   variation is amplified by the high-pass gain before the history averages it; on
//   low-contrast fine texture this can raise residual shimmer a little (high-contrast edges are
//   protected by adapt; static jittered texture at 0.3: frame-to-frame variation +10%).
//==============================================================================
#ifndef DSRLE_FILTER_FEATURES_FXH
#define DSRLE_FILTER_FEATURES_FXH

#ifdef DSRLE_TAA_ALL
	#ifndef DSRLE_TAA_CATMULL
		#define DSRLE_TAA_CATMULL
	#endif
	#ifndef DSRLE_TAA_SHARPEN
		#define DSRLE_TAA_SHARPEN
	#endif
#endif

#if defined(_FRAGMENT_SHADER) && (defined(DSRLE_TAA_CATMULL) || defined(DSRLE_TAA_SHARPEN))

// Rec.709 luma, the weights ResolveTAA uses for its c/(1+Y) mapping.
#define DSRLE_TAA_Y float3(0.2126f, 0.7152f, 0.0722f)

#ifdef DSRLE_TAA_CATMULL
#ifndef DSRLE_TAA_CATMULL_TAPS
	#define DSRLE_TAA_CATMULL_TAPS 9
#endif
#if DSRLE_TAA_CATMULL_TAPS != 5 && DSRLE_TAA_CATMULL_TAPS != 9 && DSRLE_TAA_CATMULL_TAPS != 16
	#error DSRLE_TAA_CATMULL_TAPS must be 5, 9 or 16
#endif

#if DSRLE_TAA_CATMULL_TAPS == 16
// Sampler-independent fallback: the full 4x4 cubic convolution from 16 taps at EXACT texel centres
// (integer texel coordinates clamped to the texture = CLAMP addressing), same weights as below.
// A bilinear and a point tap both return the texel itself at its centre, so neither s1's filter
// nor its address mode matters. (Load would be exact too, but leaves s1 unused: fxc then drops the
// s1 binding from RDEF and the interface gate refuses the blob.)
float3 dsrle_taa_hist_texel(int2 t, float2 texel)
{
	return gSMP_1.SampleLevel(gSMP_1Sampler, (float2(t) + 0.5f) * texel, 0).rgb;
}

float3 dsrle_taa_hist_catmull_rom(float2 histUV)
{
	float2 size;
	gSMP_1.GetDimensions(size.x, size.y);
	float2 texel = 1.0f / size;
	float2 pos = histUV * size - 0.5f;          // texel-centre coordinates: texel j sits at j
	float2 j1 = floor(pos);
	float2 f = pos - j1;
	float4 wx, wy;                              // texels j1-1 .. j1+2, sum exactly 1 per axis
	wx.x = f.x * (-0.5f + f.x * (1.0f - 0.5f * f.x));
	wx.y = 1.0f + f.x * f.x * (-2.5f + 1.5f * f.x);
	wx.z = f.x * (0.5f + f.x * (2.0f - 1.5f * f.x));
	wx.w = f.x * f.x * (-0.5f + 0.5f * f.x);
	wy.x = f.y * (-0.5f + f.y * (1.0f - 0.5f * f.y));
	wy.y = 1.0f + f.y * f.y * (-2.5f + 1.5f * f.y);
	wy.z = f.y * (0.5f + f.y * (2.0f - 1.5f * f.y));
	wy.w = f.y * f.y * (-0.5f + 0.5f * f.y);
	int2 first = int2(j1) - 1;
	int2 last = int2(size) - 1;
	int4 tx = clamp(first.xxxx + int4(0, 1, 2, 3), 0, last.xxxx);
	float3 sum = 0.0f;
	[unroll] for (int r = 0; r < 4; ++r)
	{
		int ty = clamp(first.y + r, 0, last.y);
		float3 row = dsrle_taa_hist_texel(int2(tx.x, ty), texel) * wx.x;
		row += dsrle_taa_hist_texel(int2(tx.y, ty), texel) * wx.y;
		row += dsrle_taa_hist_texel(int2(tx.z, ty), texel) * wx.z;
		row += dsrle_taa_hist_texel(int2(tx.w, ty), texel) * wx.w;
		sum += row * wy[r];
	}
	return max(sum, 0.0f);
}
#else
// one bilinear history tap; uv clamped to the texel centres of the border = CLAMP addressing
float3 dsrle_taa_hist_tap(float2 uv, float2 lo, float2 hi)
{
	return gSMP_1.SampleLevel(gSMP_1Sampler, clamp(uv, lo, hi), 0).rgb;
}

// Catmull-Rom reconstruction of the history (linear HDR) at histUV, negative lobes clipped.
float3 dsrle_taa_hist_catmull_rom(float2 histUV)
{
	float2 size;
	gSMP_1.GetDimensions(size.x, size.y);
	float2 texel = 1.0f / size;

	float2 pos = histUV * size - 0.5f;          // texel-centre coordinates: texel j sits at j
	float2 j1 = floor(pos);
	float2 f = pos - j1;                        // 0..1 between texels j1 and j1+1

	// cubic convolution weights, a = -1/2, for the texels j1-1, j1, j1+1, j1+2
	float2 w0 = f * (-0.5f + f * (1.0f - 0.5f * f));
	float2 w1 = 1.0f + f * f * (-2.5f + 1.5f * f);
	float2 w2 = f * (0.5f + f * (2.0f - 1.5f * f));
	float2 w3 = f * f * (-0.5f + 0.5f * f);
	float2 wIn = w1 + w2;                       // >= 1: the inner pair as one bilinear tap

	float2 uA = (j1 - 0.5f) * texel;            // centre of texel j1-1
	float2 uB = (j1 + 0.5f + w2 / wIn) * texel; // between texels j1 and j1+1
	float2 uC = (j1 + 2.5f) * texel;            // centre of texel j1+2
	float2 lo = 0.5f * texel;
	float2 hi = 1.0f - 0.5f * texel;

	float3 sum = dsrle_taa_hist_tap(uB, lo, hi) * (wIn.x * wIn.y);
	sum += dsrle_taa_hist_tap(float2(uB.x, uA.y), lo, hi) * (wIn.x * w0.y);
	sum += dsrle_taa_hist_tap(float2(uB.x, uC.y), lo, hi) * (wIn.x * w3.y);
	sum += dsrle_taa_hist_tap(float2(uA.x, uB.y), lo, hi) * (w0.x * wIn.y);
	sum += dsrle_taa_hist_tap(float2(uC.x, uB.y), lo, hi) * (w3.x * wIn.y);
	float wSum = wIn.x * (w0.y + wIn.y + w3.y) + wIn.y * (w0.x + w3.x);
#if DSRLE_TAA_CATMULL_TAPS == 9
	sum += dsrle_taa_hist_tap(uA, lo, hi) * (w0.x * w0.y);
	sum += dsrle_taa_hist_tap(float2(uC.x, uA.y), lo, hi) * (w3.x * w0.y);
	sum += dsrle_taa_hist_tap(float2(uA.x, uC.y), lo, hi) * (w0.x * w3.y);
	sum += dsrle_taa_hist_tap(uC, lo, hi) * (w3.x * w3.y);
	wSum += (w0.x + w3.x) * (w0.y + w3.y);
#endif
	// wSum is 1 for 9 taps (up to rounding) and 0.984..1 for 5 taps; never near 0
	return max(sum / wSum, 0.0f);
}
#endif // DSRLE_TAA_CATMULL_TAPS == 16

#define DSRLE_TAA_HOOK_HISTORY(hist, histUV, uv) \
	{ float3 dsrleCR = dsrle_taa_hist_catmull_rom(histUV); hist = dsrleCR / (dot(dsrleCR, DSRLE_TAA_Y) + 1.0f); }
#endif // DSRLE_TAA_CATMULL

#ifdef DSRLE_TAA_SHARPEN
#ifndef DSRLE_TAA_SHARPEN_AMOUNT
	#define DSRLE_TAA_SHARPEN_AMOUNT 0.3
#endif
#ifndef DSRLE_TAA_SHARPEN_EDGE
	#define DSRLE_TAA_SHARPEN_EDGE 0.6
#endif

// cur and the 8 neighbours are m-space colours; boxMin/boxMax the rounded clamp box.
float3 dsrle_taa_sharpen(float3 c, float blend, float3 n, float3 s, float3 w, float3 e,
                         float3 nw, float3 ne, float3 sw, float3 se, float3 boxMin, float3 boxMax)
{
	// centre minus the 1-2-1 binomial blur, as differences (exactly 0 when flat)
	float3 dCross = (c - n) + (c - s) + (c - w) + (c - e);
	float3 dDiag = (c - nw) + (c - ne) + (c - sw) + (c - se);
	float3 hp = (2.0f * dCross + dDiag) * (1.0f / 16.0f);

	// local luma contrast over the 3x3 (m-space luma is in [0,1))
	float yC = dot(c, DSRLE_TAA_Y);
	float yN = dot(n, DSRLE_TAA_Y), yS = dot(s, DSRLE_TAA_Y), yW = dot(w, DSRLE_TAA_Y), yE = dot(e, DSRLE_TAA_Y);
	float yNW = dot(nw, DSRLE_TAA_Y), yNE = dot(ne, DSRLE_TAA_Y), ySW = dot(sw, DSRLE_TAA_Y), ySE = dot(se, DSRLE_TAA_Y);
	float yLo = min(min(min(yC, yN), min(yS, yW)), min(min(yE, yNW), min(yNE, min(ySW, ySE))));
	float yHi = max(max(max(yC, yN), max(yS, yW)), max(max(yE, yNW), max(yNE, max(ySW, ySE))));
	float contrast = (yHi - yLo) / max(yHi + yLo, 1e-4f);
	float adapt = 1.0f - smoothstep(0.0f, (float)(DSRLE_TAA_SHARPEN_EDGE), contrast);

	float k = (2.0f * (float)(DSRLE_TAA_SHARPEN_AMOUNT)) * adapt * (1.0f - blend);
	return clamp(c + k * hp, boxMin, boxMax);
}

#define DSRLE_TAA_HOOK_CURRENT(cur, blend, hist, cN, cS, cW, cE, cNW, cNE, cSW, cSE, boxMin, boxMax, fullMin, fullMax) \
	cur = dsrle_taa_sharpen(cur, blend, cN, cS, cW, cE, cNW, cNE, cSW, cSE, boxMin, boxMax)
#endif // DSRLE_TAA_SHARPEN

#endif // _FRAGMENT_SHADER && a TAA switch
#endif //DSRLE_FILTER_FEATURES_FXH
