// HDR composite with the game's own tonemap below a pivot and a smooth highlight roll-off above it, in place of
// the hard clip at the curve's white point. Bloom, exposure, grade and grain inputs are the game's, read from the
// same registers it uploads for this pass.

Texture2D<float4> t_scene : register(t0);
Texture2D<float4> t_bloom : register(t1);
Texture2D<float4> t_noise : register(t2);
Texture2D<float4> t_glare : register(t3);
Texture2D<float4> t_wide : register(t4);  // bloom.hlsl: rgb the blurred scene, a its share. Reads as none when not bound
Texture2D<float> t_adapted : register(t5);
SamplerState s_scene : register(s0);
SamplerState s_bloom : register(s1);
SamplerState s_noise : register(s2);
SamplerState s_glare : register(s3);
SamplerState s_adapted : register(s5);

cbuffer Globals : register(b0) {
    float4 g_regs[129];
};
#define ADAPT g_regs[54]        // x key, y white, z toe numerator, w toe denominator
#define CURVE g_regs[55]        // x shoulder, y linear strength, z linear angle, w toe strength
#define SCALE g_regs[56]        // x near bloom, y glare, z lum min, w lum max
#define OPERATOR g_regs[57].x
#define GRADE_R g_regs[62]
#define GRADE_G g_regs[63]
#define GRADE_B g_regs[64]
#define NOISE_MIX g_regs[68].x
#define FAR_BLOOM g_regs[71].y

cbuffer DsrleEngine : register(b3) {
    float4 g_pack[17];
    float4 g_film;  // x shadow contrast, y highlight contrast, z exposure, w debug view
    float4 g_black;  // x black floor, y dark scene exposure
};

// Exposed scene value above which the roll-off takes over. Below it the output is exactly the game's curve.
static const float PIVOT = 2.0f;
static const float DITHER_LSB = 1.0f;
// Filmic contrast around a display-referred mid grey, before the area's own grade, with the grade's colour left as the
// art direction set it.
static const float FILM_PIVOT = 0.38f;
// A scene this many stops under the game's luminance floor starts to count as very dark, and this many more gets the full lift.
static const float DARK_FROM = 1.5f;
static const float DARK_SPAN = 2.0f;

float3 curve(float3 x) {
    const float A = CURVE.x, B = CURVE.y, C = CURVE.z, D = CURVE.w, E = ADAPT.z, F = ADAPT.w;
    return (x * (A * x + C * B) + D * E) / (x * (A * x + B) + D * F) - E / F;
}

float curve_slope(float x) {
    const float A = CURVE.x, B = CURVE.y, C = CURVE.z, D = CURVE.w, E = ADAPT.z, F = ADAPT.w;
    const float n = x * (A * x + C * B) + D * E, d = x * (A * x + B) + D * F;
    return ((2.0f * A * x + C * B) * d - n * (2.0f * A * x + B)) / (d * d);
}

float3 tonemap_rolloff(float3 x) {
    const float white = curve(ADAPT.yyy).x;
    const float pivot_out = curve(float3(PIVOT, PIVOT, PIVOT)).x / white;
    const float slope = curve_slope(PIVOT) / white;
    const float span = 1.0f - pivot_out;
    float3 below = curve(x) / white;
    float3 above = pivot_out + span * (1.0f - exp(-max(x - PIVOT, 0.0f) * slope / span));
    return x <= PIVOT ? below : above;
}

// An S around the pivot. A plain power would lift bright values too, and the grade's gain then clips them: skies went white.
// The dark half eases back to no change at black: a power's slope reaches zero there, which flattened dark areas to black.
float3 film_contrast(float3 v) {
    const float3 below = lerp(v, FILM_PIVOT * pow(v / FILM_PIVOT, g_film.x), smoothstep(0.0f, FILM_PIVOT, v));
    const float3 above = 1.0f - (1.0f - FILM_PIVOT) * pow((1.0f - v) / (1.0f - FILM_PIVOT), g_film.y);
    return v <= FILM_PIVOT ? below : above;
}

// The grade's offsets take a channel below zero in the darkest pixels. Under the floor it fades toward zero instead of
// clipping there, so those pixels keep their detail and no channel drops out.
float3 soft_black(float3 v) {
    const float k = g_black.x;
    return k > 0.0f && any(v < k) ? (v >= k ? v : k * exp((v - k) / k)) : v;
}

float ign(float2 px) {
    return frac(52.9829189f * frac(dot(px, float2(0.06711056f, 0.00583715f))));
}

float4 main(float4 pos : SV_Position, float4 uv : TEXCOORD1) : SV_Target0 {
    float3 scene = max(t_scene.Sample(s_scene, uv.xy).rgb, 1e-6f);
    float4 bloom = t_bloom.Sample(s_bloom, uv.xy);
    bloom.rgb = max(bloom.rgb, 1e-6f);
    float bloom_mul = lerp(SCALE.x, FAR_BLOOM, saturate(bloom.a));
    float3 col = scene + bloom_mul * bloom.rgb + SCALE.y * t_glare.Sample(s_glare, uv.xy).rgb;
    const float4 wide = t_wide.Sample(s_bloom, uv.xy);
    col = lerp(col, wide.rgb, wide.a);

    const float adapted = t_adapted.Sample(s_adapted, float2(0.5f, 0.5f));
    float exposure = g_film.z * ADAPT.x / (clamp(adapted, SCALE.z, SCALE.w) + 1e-4f);
    // The game stops raising exposure at its luminance floor, so a very dark scene stays underexposed however far under it
    // goes. Many sunlit scenes sit just under the floor as well, and they keep the game's exposure.
    exposure *= lerp(1.0f, g_black.y, saturate((log2(SCALE.z / max(adapted, 1e-5f)) - DARK_FROM) / DARK_SPAN));
    // Test readouts, each as a flat colour: 1 log2 of exposure, adapted luminance and key; 2 the grade's offsets; 3 its gain on grey.
    if (g_film.w == 1.0f) return float4((log2(float3(exposure, max(adapted, 1e-5f), ADAPT.x)) + 12.0f) / 16.0f, 1.0f);
    if (g_film.w == 2.0f) return float4(0.5f + 2.0f * float3(GRADE_R.w, GRADE_G.w, GRADE_B.w), 1.0f);
    if (g_film.w == 3.0f) return float4(0.5f * float3(dot(GRADE_R.xyz, 1.0f), dot(GRADE_G.xyz, 1.0f), dot(GRADE_B.xyz, 1.0f)), 1.0f);
    float3 x = col * exposure;
    float3 tm;
    if ((uint)OPERATOR == 0) {
        tm = tonemap_rolloff(x);
    } else {
        // The other operators the game's shader supports; it does not use them in any frame traced so far.
        float L = max(dot(x, float3(0.2126729f, 0.7151522f, 0.0721750f)), 1e-4f);
        float Ld = (uint)OPERATOR == 1 ? L / (1.0f + L) : L * (1.0f + L / (ADAPT.y * ADAPT.y)) / (1.0f + L);
        tm = x * Ld / L;
    }
    tm = pow(saturate(tm), 1.0f / 2.2f);
    tm = film_contrast(saturate(tm));
    float4 v = float4(tm, 1.0f);
    tm = soft_black(float3(dot(v, GRADE_R), dot(v, GRADE_G), dot(v, GRADE_B)));

    float3 n = t_noise.Sample(s_noise, uv.zw).rgb;
    float3 overlay = n <= 0.5f ? 2.0f * tm * n : 1.0f - 2.0f * (1.0f - tm) * (1.0f - n);
    tm = lerp(tm, overlay, NOISE_MIX);

    // The output target is 8-bit and the curve is steep in the dark: triangular dither hides the banding.
    float d = ign(pos.xy) + ign(pos.yx + 37.0f) - 1.0f;
    tm += d * DITHER_LSB / 255.0f;
    return float4(tm, dot(tm, float3(0.299f, 0.587f, 0.114f)));
}
