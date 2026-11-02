// Additive froxel volumetrics: glow around the game's clustered lights plus a faint haze in the area's own fog
// colour, integrated front to back and added over the opaque scene. Vanilla's per-object fog stays as the colour
// anchor, so transmittance is kept near 1 and nothing here can pull an area toward one palette.
// Froxel grid after Wronski 2014, integral form after Hillaire 2015.
//
// Every entry point has its own registers, so no two declarations ever land on one slot:
//   inject_main     t2 cells, t3 light ids, t4 lights, t6 shadow atlas, s1 shadow compare, u0 scatter
//   integrate_main  t5 scatter, u1 volume
//   apply_ps        t0 depth, t1 volume, s0 linear

cbuffer Constants : register(b0) {
    float3 g_right; float g_p00;
    float3 g_up; float g_p11;
    float3 g_forward; float g_z_a;
    float g_z_b, g_z_near, g_z_far, g_slice_scale;  // slice = ln(z / near) * scale
    float3 g_grid; float g_frame;
    float3 g_inv_grid; float g_dither;
    float3 g_jitter; float g_strength;              // jitter: this frame's lattice offset, in froxels
    float2 g_inv_size; float g_cluster_near, g_cluster_scale;
    float g_light_density, g_light_g, g_radius_scale, g_max_glow;
    float g_camera_fade_start, g_camera_fade_k, g_extinction, g_haze_density;
    float3 g_haze_color; float g_haze_falloff;
    float3 g_haze_sun; float g_haze_g;
    float3 g_haze_dir; float g_far_fade_k;
    float3 g_sun_color; float g_sun_density;
    float3 g_sun_dir; float g_sun_g;
    float4 g_shadow_start;
    float g_shadow_end, g_shadow_fade_k; uint g_light_list; float g_glow_core;
    float4 g_shadow_rows[16];                       // x, y, z, w rows per cascade
    float4 g_shadow_rects[4];
};

// The volumes hold per-kilometre values: per-metre haze sits in the denormal range of a 16-bit float.
static const float PER_KM = 1000.0f;

float slice_z(float slice) {
    return g_z_near * exp(slice / g_slice_scale);
}

// Henyey-Greenstein, scaled so that g = 0 gives 1: strengths stay plain per-metre numbers.
float phase(float cos_theta, float g) {
    float d = 1.0f + g * g - 2.0f * g * cos_theta;
    return (1.0f - g * g) / (d * sqrt(d));
}

//------------------------------------------------------------------------------
// Inject: in-scatter per metre and extinction per froxel.
//------------------------------------------------------------------------------
struct Light {
    float4 position;  // xyz camera-relative, w = 1 / (end - start)
    float4 color;     // rgb linear, w = radius
    float attenuation;
    uint falloff_mode;
    uint padding0;
    uint padding1;
};

StructuredBuffer<uint> t_cells : register(t2);
StructuredBuffer<uint> t_ids : register(t3);
StructuredBuffer<Light> t_lights : register(t4);
Texture2D<float> t_shadow : register(t6);
SamplerComparisonState s_shadow : register(s1);
RWTexture3D<float4> u_scatter : register(u0);

// Same cells as the forward shaders: y runs up the screen, z is log2 from the game's own near plane.
uint cell_slice(float view_z) {
    return uint(clamp(log2(view_z / g_cluster_near) * g_cluster_scale, 0.0f, 23.0f));
}

float cell_slice_start(uint slice) {
    return g_cluster_near * pow(2.0f, float(slice) / g_cluster_scale);
}

// What the part of the view ray from a to b, measured from the froxel's centre, adds to the froxel's mean. Far slices
// are metres thick, against light radii of 2-5 m: a point sample would pulse as slices sweep past a light, and TAA
// follows such slow flicker because its history clamps to the current neighbourhood. Everything here that depends on
// a and b is an integral along the ray, so summed down a column it does not depend on where the slices fall.
float3 light_glow(Light light, float3 center, float3 view, float a, float b, float half_len) {
    float radius = light.color.w * g_radius_scale;
    if (radius <= 0.0f || light.position.w <= 0.0f) return float3(0.0f, 0.0f, 0.0f);
    float3 to_light = light.position.xyz - center;
    float along = dot(to_light, view);
    float miss_sq = max(dot(to_light, to_light) - along * along, 0.0f);
    float radius_sq = radius * radius;
    if (miss_sq >= radius_sq) return float3(0.0f, 0.0f, 0.0f);

    float k = light.position.w / g_radius_scale;
    float miss = sqrt(miss_sq);
    float x = saturate((radius - miss) * k);
    float reach = sqrt(radius_sq - miss_sq);

    // Glow concentrates around the source as an inverse square, softened by a core so it stays finite. The game's own
    // falloff is flat out to most of the radius, which reads as a veil filling the room and lifts the darkness that
    // defines these areas; this way a flame gets a halo and the dark stays dark. Closed form along the ray, so summed
    // down a column it does not depend on where the slices fall. Density is what is left free: for a ray through the
    // centre the whole path counts for about PI * core metres, however big the light is.
    float core_sq = g_glow_core * g_glow_core;
    float q = sqrt(core_sq + miss_sq);
    float scale = core_sq / q;
    float lo = clamp(a - along, -reach, reach), hi = clamp(b - along, -reach, reach);
    float ahead = scale * (atan(min(hi, 0.0f) / q) - atan(min(lo, 0.0f) / q));
    float behind = scale * (atan(max(hi, 0.0f) / q) - atan(max(lo, 0.0f) / q));

    // Forward scatter while the light is ahead, back scatter once it is behind. One angle per side for the whole ray:
    // an angle that moved with the stretch would make the column's sum move with the lattice.
    float side = 0.3f * reach;
    float cos_side = side / max(sqrt(side * side + miss_sq), 1e-3f);
    float mean = x * x * x * (ahead * phase(cos_side, g_light_g) + behind * phase(-cos_side, g_light_g)) / (2.0f * half_len);

    // A light around the camera would veil the whole screen, and the game keeps a small light on the player (the
    // PointLightBank rows named for it), which would wear an aura everywhere. So nearby lights give way.
    float fade = saturate((length(light.position.xyz) - g_camera_fade_start) * g_camera_fade_k);
    fade = fade * fade * (3.0f - 2.0f * fade);

    // Lights near the far end leave gently, rather than popping off at the edge of the volume. Judged per light:
    // per froxel it would move with the slab, which swings by metres out there.
    fade *= saturate((g_z_far - dot(light.position.xyz, g_forward)) * g_far_fade_k);

    return max(light.color.rgb, 0.0f) * mean * fade;
}

// 1 where the world atlas says a caster blocks the sun: four quadrants, three rows each, chosen by view depth as the
// forward shaders choose them. Past the last cascade the sun is taken as unblocked.
float sun_occlusion(float3 p, float view_z) {
    if (view_z >= g_shadow_start.w) return 0.0f;
    int c = int(dot(float4(view_z >= g_shadow_start.x, view_z >= g_shadow_start.y, view_z >= g_shadow_start.z, view_z >= g_shadow_start.w), float4(1.0f, 1.0f, 1.0f, 1.0f)));
    float4 q = float4(p, 1.0f);
    float3 uvz = float3(dot(q, g_shadow_rows[c * 3]), dot(q, g_shadow_rows[c * 3 + 1]), dot(q, g_shadow_rows[c * 3 + 2]));
    return t_shadow.SampleCmpLevelZero(s_shadow, uvz.xy, min(uvz.z, 0.99999f));
}

[numthreads(8, 8, 4)]
void inject_main(uint3 id : SV_DispatchThreadID) {
    // Groups overhang the grid in y.
    if (any(id >= uint3(g_grid))) return;

    float2 uv = (float2(id.xy) + 0.5f + g_jitter.xy) * g_inv_grid.xy;
    float2 ndc = float2(uv.x * 2.0f - 1.0f, 1.0f - uv.y * 2.0f);
    float z0 = slice_z(float(id.z) + g_jitter.z);
    float z1 = slice_z(float(id.z) + 1.0f + g_jitter.z);
    float view_z = 0.5f * (z0 + z1);

    float3 ray = g_right * (ndc.x / g_p00) + g_up * (ndc.y / g_p11) + g_forward;  // reaches view z = 1
    float ray_len = length(ray);
    float3 view = ray / ray_len;
    float3 center = ray * view_z;
    float half_len = 0.5f * (z1 - z0) * ray_len;

    float3 scatter = float3(0.0f, 0.0f, 0.0f);

    if (g_light_density > 0.0f) {
        // A light is only listed in the cells its sphere touches, and a far slab can straddle two cells. So each part of
        // the stretch walks its own cell's list: going by the slab's centre would lose the light on frames when jitter
        // puts that centre in the neighbouring cell.
        float2 cell_xy = clamp(floor((ndc * 0.5f + 0.5f) * float2(16.0f, 8.0f)), float2(0.0f, 0.0f), float2(15.0f, 7.0f));
        uint first_slice = cell_slice(z0), last_slice = cell_slice(z1);
        float3 glow = float3(0.0f, 0.0f, 0.0f);
        for (uint s = first_slice; s <= last_slice; s++) {
            float a = ((s == first_slice ? z0 : cell_slice_start(s)) - view_z) * ray_len;
            float b = ((s == last_slice ? z1 : cell_slice_start(s + 1)) - view_z) * ray_len;
            uint cell = t_cells[(s * 8 + uint(cell_xy.y)) * 16 + uint(cell_xy.x)];
            uint count = (cell >> (g_light_list * 6)) & 0x3f;
            uint first = cell >> 12;
            for (uint i = 0; i < count; i++) {
                uint light = (t_ids[first + i] >> (g_light_list * 9)) & 0x1ff;
                // Light 0 is the soft light the game keeps on the player. In the air it reads as a veil that follows them.
                if (light != 0) glow += light_glow(t_lights[light], center, view, a, b, half_len);
            }
        }
        scatter += glow * g_light_density;
    }

    // Relative to the camera's height, because that is the only height known here: haze thins out overhead, so the
    // sky stays clean, and pools a little below.
    float height = min(exp(-center.y * g_haze_falloff), 2.0f);
    if (g_haze_density > 0.0f) {
        scatter += (g_haze_color + g_haze_sun * phase(dot(view, g_haze_dir), g_haze_g)) * g_haze_density * height;
    }

    if (g_sun_density > 0.0f) {
        scatter += g_sun_color * g_sun_density * phase(dot(view, g_sun_dir), g_sun_g) * (1.0f - sun_occlusion(center, view_z));
    }

    // A NaN reaching the scene would live on in the TAA history for good. Tested on the bits, as fxc removes isnan.
    if (any((asuint(scatter) & 0x7f800000) == 0x7f800000)) scatter = float3(0.0f, 0.0f, 0.0f);
    u_scatter[id] = float4(max(scatter, 0.0f) * PER_KM, g_extinction * height * PER_KM);
}

//------------------------------------------------------------------------------
// Integrate each column front to back. A slice stores the integral up to its own centre, so looking up a depth
// gives what lies in front of that depth, with no half-slice bias.
//------------------------------------------------------------------------------
Texture3D<float4> t_scatter : register(t5);
RWTexture3D<float4> u_volume : register(u1);

// (1 - e^-x) / x, stable for the tiny optical depths used here.
float slab_gain(float x) {
    return x < 1e-3f ? 1.0f - 0.5f * x : (1.0f - exp(-x)) / x;
}

[numthreads(8, 8, 1)]
void integrate_main(uint3 id : SV_DispatchThreadID) {
    if (any(id.xy >= uint2(g_grid.xy))) return;
    float2 uv = (float2(id.xy) + 0.5f + g_jitter.xy) * g_inv_grid.xy;
    float ray_len = length(float3((uv.x * 2.0f - 1.0f) / g_p00, (1.0f - uv.y * 2.0f) / g_p11, 1.0f));

    float3 total = float3(0.0f, 0.0f, 0.0f);
    float transmittance = 1.0f;
    uint slices = uint(g_grid.z);
    [loop] for (uint k = 0; k < slices; k++) {
        float4 s = t_scatter.Load(int4(id.xy, k, 0));
        float half_km = 0.5f * (slice_z(float(k) + 1.0f + g_jitter.z) - slice_z(float(k) + g_jitter.z)) * ray_len / PER_KM;
        float optical = s.a * half_km;
        float half_trans = exp(-optical);
        float3 half_slab = s.rgb * half_km * slab_gain(optical) * transmittance;
        u_volume[int3(id.xy, k)] = float4(total + half_slab, transmittance * half_trans);
        total += half_slab * (1.0f + half_trans);
        transmittance *= half_trans * half_trans;
    }
}

//------------------------------------------------------------------------------
// Apply: fullscreen triangle, blended ONE + SRC_ALPHA over the scene.
//------------------------------------------------------------------------------
Texture2D<float> t_depth : register(t0);
Texture3D<float4> t_volume : register(t1);
SamplerState s_linear : register(s0);

float4 fullscreen_vs(uint id : SV_VertexID) : SV_Position {
    float2 uv = float2((id << 1) & 2, id & 2);
    return float4(uv.x * 2.0f - 1.0f, 1.0f - uv.y * 2.0f, 0.0f, 1.0f);
}

float ign(float2 px) {
    return frac(52.9829189f * frac(dot(px, float2(0.06711056f, 0.00583715f))));
}

float4 apply_ps(float4 pos : SV_Position) : SV_Target {
    float depth = t_depth.Load(int3(int2(pos.xy), 0));
    // The sky dome is in depth at thousands of metres; clamping stops it at the end of the volume.
    float z = clamp(g_z_b / min(depth - g_z_a, -1e-7f), g_z_near, g_z_far);

    float2 seed = pos.xy + 5.588238f * g_frame;
    float3 noise = float3(ign(seed), ign(seed.yx + 47.0f), ign(seed + float2(113.0f, 17.0f)));

    // Sub-froxel dither hides the slices, and TAA takes the noise out. Taking off the lattice offset here is what
    // keeps the volume still on screen while the sample positions move underneath it.
    float3 texel = float3(pos.xy * g_inv_size * g_grid.xy, log(z / g_z_near) * g_slice_scale);
    texel += (noise.xyz - 0.5f) * g_dither - g_jitter;
    float4 fog = t_volume.SampleLevel(s_linear, texel * g_inv_grid, 0.0f);

    // Soft limit, hue kept: a very strong light gets a bright core, not a white veil.
    float3 color = max(fog.rgb, 0.0f) * g_strength;
    // The sky is painted with its own air already, and the whole volume in front of it would wash out its colour. A long
    // ramp, because z is what this follows: a short one draws a straight line across a dark lake.
    color *= lerp(1.0f, 0.35f, saturate((z / g_z_far - 0.4f) / 0.6f));
    float peak = max(max(color.r, color.g), color.b);
    color *= g_max_glow * (1.0f - exp(-peak / g_max_glow)) / max(peak, 1e-6f);

    // R11G11B10 rounds to about 1.5% of the value: break up the contours a smooth gradient would leave.
    color *= 1.0f + (noise.z - 0.5f) * 0.03f;

    return float4(color, lerp(1.0f, saturate(fog.a), saturate(g_strength)));
}
