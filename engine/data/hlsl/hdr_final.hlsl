// Stands in for the game's last blit while the swap chain is HDR. That blit samples t0, the composite target with the
// UI on it, looks each channel up in t1, the brightness table, and writes the back buffer. This writes scRGB instead:
// linear Rec.709, 1.0 = 80 nits. Game content is Rec.709 primaries, so there is no colour matrix.
//
// A linear value here is relative to paper white. Without UI on a pixel it is the composite's extended value; with UI
// it is the 8-bit pixel decoded with the same 2.2 the composite encoded with, so UI sits at paper white and below.

Texture2D<float4> t_now : register(t0);  // the game's, composite plus UI
Texture1D<float4> t_lut : register(t1);  // the game's, 256 entries
Texture2D<float4> t_ext : register(t2);  // composite_hdr.hlsl target 1
Texture2D<float4> t_sdr : register(t3);  // composite_hdr.hlsl target 2: t0's bytes before the UI
SamplerState s_now : register(s0);       // the game's
SamplerState s_lut : register(s2);       // linear: the game's table sampler is point, which would band a float input

cbuffer HdrConstants : register(b4) {
    float g_peak;     // used by composite_hdr.hlsl
    float g_knee;     // used by composite_hdr.hlsl
    float g_scale;    // paper white / 80
    float g_has_ext;  // 0 when no composite ran for this image: menus and loading screens are all UI
    float g_warm_hue; // used by composite_hdr.hlsl
    float3 g_pad;
};

// Unchanged pixels compare equal exactly: same format, same filter, same coordinates. This only absorbs float noise.
static const float SAME = 0.25f / 255.0f;
// Reaches full coverage at 25% alpha. See ui_coverage.
static const float COVERAGE_GAIN = 4.0f;

float max3(float3 v) {
    return max(v.r, max(v.g, v.b));
}

// How much of the extension to take away where the UI changed the pixel. The UI blends in gamma space, so
//   now = sdr + alpha * (ui - sdr)
// and the smallest alpha that explains a change is (sdr - now) / sdr going down, (now - sdr) / (1 - sdr) going up. That
// is exact for a black veil and a white element, so fades and soft panel edges stay continuous instead of snapping
// highlights to SDR on their first frame, which a threshold would do. For mid greys it is only a lower bound and a solid
// panel would let a highlight show through, hence the gain, at the price of veils dimming highlights a little early.
// Only highlights have an extension to remove, and there sdr is large, so the divisions are well behaved where it counts.
// A UI pixel equal to the SDR pixel cannot be told from no UI, which is why composite_hdr.hlsl keeps the top code free
// for white UI over clipped sky. Off-white over clipped sky is the weak case: at 85% it gets 60% coverage, so it comes
// out brighter than it should, though still clearly darker than the sky behind it, which SDR could not manage at all.
float ui_coverage(float3 now, float3 sdr) {
    float3 d = now - sdr;
    float3 up = max(d - SAME, 0.0f) / max(1.0f - sdr, SAME);
    float3 down = max(-d - SAME, 0.0f) / max(sdr, SAME);
    return saturate(COVERAGE_GAIN * max3(max(up, down)));
}

// The brightness slider, as the game applies it: per channel on the gamma-encoded value. It only reaches up to paper
// white; what lies above is added back on top, so a table that lowers white still leaves no step there.
float3 apply_brightness(float3 e) {
    float3 v = pow(saturate(e), 1.0f / 2.2f) * 0.996094f + 0.001953f;  // the game's remap to texel centres
    float3 table = float3(t_lut.SampleLevel(s_lut, v.r, 0.0f).r, t_lut.SampleLevel(s_lut, v.g, 0.0f).g, t_lut.SampleLevel(s_lut, v.b, 0.0f).b);
    return pow(saturate(table), 2.2f) + max(e - 1.0f, 0.0f);
}

float4 main(float4 pos : SV_Position, float2 uv : TEXCOORD0) : SV_Target0 {
    float3 now = saturate(t_now.Sample(s_now, uv).rgb);
    float3 e = pow(now, 2.2f);
    if (g_has_ext > 0.5f) {
        float3 sdr = saturate(t_sdr.Sample(s_now, uv).rgb);
        float3 ext = t_ext.Sample(s_now, uv).rgb;
        // No UI: exactly ext, keeping its float precision. Full coverage: exactly the 8-bit pixel. The difference is
        // near zero below the pivot, so UI over anything but a highlight needs no coverage estimate at all.
        e = max(e + (1.0f - ui_coverage(now, sdr)) * (ext - pow(sdr, 2.2f)), 0.0f);
    }
    return float4(apply_brightness(e) * g_scale, 1.0f);
}
