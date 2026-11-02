// HDR entry point for the composite: the SDR image as composite.hlsl makes it, plus the same image with its highlights
// extended to the display's peak. hdr.cpp binds this in place of composite.hlsl's main, with three targets.
//
// Per channel, like the game's curve. The curve's character is per channel, a luminance scheme would need its own hue
// rule above a seam at the pivot, and a channel crossing the pivot here has no seam. It keeps channel order, so fire
// stays orange and sky stays blue, and it stays more saturated than SDR, where the channels pile up against white.
//
// The extension is added to main()'s result as a difference that is exactly zero below the pivot, so tuning in
// composite.hlsl carries over: below the pivot this is its output in linear, grade, grain and dither included.

#include "composite.hlsl"

cbuffer HdrConstants : register(b4) {
    float g_peak;     // display peak; 1 is paper white in all of these
    float g_knee;     // where the display limit begins, always at or above 1
    float g_scale;    // used by hdr_final.hlsl
    float g_has_ext;  // used by hdr_final.hlsl
    float g_warm_hue; // how much of the SDR hue warm highlights keep
    float3 g_pad;
};

struct HdrOutput {
    float4 sdr : SV_Target0;       // what the UI draws over: main()'s result with the top code kept free
    float4 ext : SV_Target1;       // linear, 1 is paper white
    float4 sdr_copy : SV_Target2;  // same bytes as target 0, without the UI, so hdr_final.hlsl can see what the UI changed
};

// tonemap_rolloff's shape with the display's peak as its ceiling: same value and slope at the pivot, higher above it.
float3 extended_rolloff(float3 x) {
    const float white = curve(ADAPT.yyy).x;
    const float pivot_out = curve(float3(PIVOT, PIVOT, PIVOT)).x / white;
    const float slope = curve_slope(PIVOT) / white;
    const float span = max(g_peak - pivot_out, 1e-3f);
    float3 above = pivot_out + span * (1.0f - exp(-max(x - PIVOT, 0.0f) * slope / span));
    return x <= PIVOT ? curve(x) / white : above;
}

// The grade's gain above white can overshoot the display (Parish's takes neutral white to 3.2 with a peak of 2.3), and
// clipping there is what would turn highlights white. Everything at or below the knee passes untouched.
float3 display_limit(float3 e) {
    const float span = max(g_peak - g_knee, 1e-3f);
    float3 above = g_knee + span * (1.0f - exp(-max(e - g_knee, 0.0f) / span));
    return e <= g_knee ? e : above;
}

// Red gets headroom while green stays on the curve, which takes flames from orange toward red. Warm pixels keep part of
// their SDR hue; blue ones have no such problem and keep the extended one.
float3 keep_warm_hue(float3 e, float3 s) {
    const float smin = min(s.r, min(s.g, s.b)), smax = max(s.r, max(s.g, s.b));
    const float emin = min(e.r, min(e.g, e.b)), emax = max(e.r, max(e.g, e.b));
    const float w = g_warm_hue * saturate(min(s.r - s.b, e.r - e.b) / 0.1f);
    return lerp(e, emin + (s - smin) / max(smax - smin, 1e-4f) * (emax - emin), w);
}

// film_contrast's slope where the curve reaches the pivot. Above it the extension replaces that part of the S, so the same
// slope carries on and no kink shows where the highlights begin.
float film_slope_at_pivot() {
    const float v = pow(saturate(curve(float3(PIVOT, PIVOT, PIVOT)).x / curve(ADAPT.yyy).x), 1.0f / 2.2f);
    return v > FILM_PIVOT ? g_film.y * pow(max(1.0f - v, 1e-4f) / (1.0f - FILM_PIVOT), g_film.y - 1.0f) : g_film.x;
}

HdrOutput hdr_main(float4 pos : SV_Position, float4 uv : TEXCOORD1) {
    HdrOutput o;
    o.sdr = main(pos, uv);
    float3 graded = o.sdr.rgb;
    // The grade clips a lot of sky in SDR (Firelink from an exposed 3.5). White UI over a clipped pixel would change
    // nothing and read as no UI at all, so the top code is left to the UI. One step, and the blit never shows it bare.
    o.sdr.rgb = min(o.sdr.rgb, 254.0f / 255.0f);
    o.sdr_copy = o.sdr;

    if ((uint)OPERATOR == 0) {
        // The same exposed scene value main() starts from.
        float adapted;
        const float3 x = scene_colour(uv) * scene_exposure(adapted);

        // The grade lives in gamma space, so the extra goes through it there, without its offset: main() applied that.
        const float3 extra = film_slope_at_pivot() * (pow(max(extended_rolloff(x), 0.0f), 1.0f / 2.2f) - pow(saturate(tonemap_rolloff(x)), 1.0f / 2.2f));
        graded += float3(dot(extra, GRADE_R.rgb), dot(extra, GRADE_G.rgb), dot(extra, GRADE_B.rgb));
    }
    o.ext = float4(keep_warm_hue(display_limit(pow(max(graded, 0.0f), 2.2f)), pow(saturate(o.sdr.rgb), 2.2f)), 1.0f);
    return o;
}
