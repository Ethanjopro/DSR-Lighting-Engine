// Wide bloom. Down: a 13-tap filter per level, which stays stable as bright pixels move. Up: a tent over the level below,
// mixed into each level, so the blur widens smoothly and the total energy stays the scene's.

Texture2D<float4> t_src : register(t0);
Texture2D<float4> t_low : register(t1);
RWTexture2D<float4> u_out : register(u0);
SamplerState s_linear : register(s0);

cbuffer Constants : register(b0) {
    float2 g_src_texel;
    float2 g_dst_texel;
    float g_scatter;   // how much of each level comes from the wider one below it
    float g_strength;  // goes out in alpha: the composite's share of the bloom
    float g_first;     // the level that reads the scene
    float g_pad;
};

float3 tap(float2 uv) {
    return t_src.SampleLevel(s_linear, uv, 0.0f).rgb;
}

// A lone very bright pixel must not count for more than its neighbours, or it flickers through the whole chain.
float3 group(float3 a, float3 b, float3 c, float3 d) {
    if (g_first < 0.5f) return (a + b + c + d) * 0.25f;
    a = clamp(a, 0.0f, 1000.0f);  // also drops NaN, which a blur would spread across the screen
    b = clamp(b, 0.0f, 1000.0f);
    c = clamp(c, 0.0f, 1000.0f);
    d = clamp(d, 0.0f, 1000.0f);
    const float wa = 1.0f / (1.0f + max(a.r, max(a.g, a.b)));
    const float wb = 1.0f / (1.0f + max(b.r, max(b.g, b.b)));
    const float wc = 1.0f / (1.0f + max(c.r, max(c.g, c.b)));
    const float wd = 1.0f / (1.0f + max(d.r, max(d.g, d.b)));
    return (a * wa + b * wb + c * wc + d * wd) / (wa + wb + wc + wd);
}

[numthreads(8, 8, 1)]
void down_main(uint3 id : SV_DispatchThreadID) {
    const float2 uv = (id.xy + 0.5f) * g_dst_texel;
    const float2 t = g_src_texel;
    const float3 a = tap(uv + t * float2(-2, -2)), b = tap(uv + t * float2(0, -2)), c = tap(uv + t * float2(2, -2));
    const float3 d = tap(uv + t * float2(-2, 0)), e = tap(uv), f = tap(uv + t * float2(2, 0));
    const float3 g = tap(uv + t * float2(-2, 2)), h = tap(uv + t * float2(0, 2)), i = tap(uv + t * float2(2, 2));
    const float3 j = tap(uv + t * float2(-1, -1)), k = tap(uv + t * float2(1, -1));
    const float3 l = tap(uv + t * float2(-1, 1)), m = tap(uv + t * float2(1, 1));
    float3 col = group(j, k, l, m) * 0.5f;
    col += group(a, b, d, e) * 0.125f;
    col += group(b, c, e, f) * 0.125f;
    col += group(d, e, g, h) * 0.125f;
    col += group(e, f, h, i) * 0.125f;
    u_out[id.xy] = float4(col, 0.0f);
}

[numthreads(8, 8, 1)]
void up_main(uint3 id : SV_DispatchThreadID) {
    const float2 uv = (id.xy + 0.5f) * g_dst_texel;
    const float2 t = g_src_texel;
    float3 low = t_low.SampleLevel(s_linear, uv, 0.0f).rgb * 4.0f;
    low += (t_low.SampleLevel(s_linear, uv + t * float2(-1, 0), 0.0f).rgb + t_low.SampleLevel(s_linear, uv + t * float2(1, 0), 0.0f).rgb +
            t_low.SampleLevel(s_linear, uv + t * float2(0, -1), 0.0f).rgb + t_low.SampleLevel(s_linear, uv + t * float2(0, 1), 0.0f).rgb) * 2.0f;
    low += t_low.SampleLevel(s_linear, uv + t * float2(-1, -1), 0.0f).rgb + t_low.SampleLevel(s_linear, uv + t * float2(1, -1), 0.0f).rgb +
           t_low.SampleLevel(s_linear, uv + t * float2(-1, 1), 0.0f).rgb + t_low.SampleLevel(s_linear, uv + t * float2(1, 1), 0.0f).rgb;
    low /= 16.0f;
    const float3 here = t_src.SampleLevel(s_linear, uv, 0.0f).rgb;
    u_out[id.xy] = float4(lerp(here, low, g_scatter), g_strength);
}
