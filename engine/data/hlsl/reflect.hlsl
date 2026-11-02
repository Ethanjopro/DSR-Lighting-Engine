// Screen-space reflections, once a frame at the first copy of the scene, so water and the refraction copy see them.
//
//   hiz_first, hiz_down      nearest-depth pyramid from the game's depth, half resolution upward
//   color_first, color_down  blurred half-resolution copies of the scene, for rough cones
//   trace_main               one ray per half-resolution pixel: a GGX direction, then a walk up and down the pyramid
//   spatial_main             edge-aware gather over a disk sized by the cone
//   temporal_main            reprojected history, clipped to what this frame's neighbourhood allows
//   composite_ps             full resolution: swaps the probe's specular for the traced one
//
// What ends up in the scene, per pixel, with C and A the filtered traced radiance (premultiplied) and its confidence:
//
//     scene + k * (R * C - A * (S + R * f0))
//
// R (RT2) is the part of the incoming light the probe's lobe sends to the eye: LightPower * DFG * horizon, clamped to 1. It
// has nothing of the air in it, because C does: C is read from the scene, so it has already been through this area's fog,
// scattering and terrain tint on its way to the camera.
// S (RT3, with f0) is the specular probe term this pixel really holds, after fog, as an exact difference: probe * DFG * horizon
// * LightPower * light map and shadow * AO * fill, then fog. f0 is what a black surface shows at this pixel, the pure haze
// between it and the camera. C holds its own haze and so does the rest of the pixel, so R * f0 keeps R * C from adding it twice.
//
// So where a ray hit (A = 1) the pixel becomes "without probe specular" + R * (C - f0): the probe is out and the light of the
// hit is in, through the same lobe, seen through about as much air as it really crosses. Where it missed (A = 0) nothing moves
// and the probe stays. R stays at or below 1, so no energy is added, and a dark hit in front of a bright probe makes the pixel
// darker, as it should. k is zero from reflect.rough_cut up, and those pixels are not written at all: rough stone stays as it was.
//
// Everything is premultiplied by confidence, so nothing divides by it and a pixel with no hit carries exact zeros.

cbuffer Constants : register(b0) {
    // These six rows are also read by the water shaders through b5 (DsrleReflect in dsrle_features.fxh). Keep in step.
    float4 g_row_x;             // clip x from a camera-relative position; carries this frame's TAA jitter
    float4 g_row_y;             // clip y
    float4 g_row_w;             // clip w, which is view Z
    float g_z_a, g_z_b;         // z_ndc = z_a + z_b / w
    float g_cells_x, g_cells_y; // level 0 of the pyramid is half the full size: one cell is two pixels
    float g_water_strength, g_water_steps, g_water_thickness, g_water_distance;
    float g_water_edge, g_sky_depth, g_water_cube, g_top_level;

    float4 g_prev_x;            // last frame's rows
    float4 g_prev_y;
    float4 g_prev_w;
    float3 g_camera_delta;      // this frame's camera minus last frame's
    float g_history_valid;
    float3 g_right;
    float g_p00;
    float3 g_up;
    float g_p11;
    float3 g_forward;
    float g_jx;
    float g_jy, g_frame, g_max_distance, g_near_w;
    float2 g_size, g_inv_size;
    int2 g_half_size;
    int2 g_pattern;             // which of a cell's four pixels supplies its surface this frame
    int g_steps;
    float g_thickness, g_thickness_scale, g_self_plane;
    float g_rough_full, g_rough_cut, g_min_reflectance, g_edge;
    float g_far_start, g_far_k, g_lobe, g_cone;
    float g_max_radiance, g_sky_hits, g_toward_k, g_spatial_radius;
    float g_history, g_clip, g_strength, g_gain;
    float g_debug, g_prev_jx, g_prev_jy, g_unused2;
};

static const float PI = 3.14159265f;
static const float3 LUMA = float3(0.2126f, 0.7152f, 0.0722f);

//------------------------------------------------------------------------------
// Shared helpers.
//------------------------------------------------------------------------------

// By bits rather than by comparison: a NaN must not survive into the TAA history, and this cannot be optimised away.
bool is_finite3(float3 v) {
    const uint3 e = asuint(v) & 0x7f800000u;
    return all(e != 0x7f800000u);
}
bool is_finite4(float4 v) {
    const uint4 e = asuint(v) & 0x7f800000u;
    return all(e != 0x7f800000u);
}

// Non-finite values go to zero, the rest into [0, reflect.max_radiance] with the hue kept.
float3 clean_radiance(float3 c) {
    if (!is_finite3(c)) return float3(0.0f, 0.0f, 0.0f);
    c = max(c, 0.0f);
    const float peak = max(c.r, max(c.g, c.b));
    return peak > g_max_radiance ? c * (g_max_radiance / peak) : c;
}

float view_z(float depth) {
    return g_z_b / min(depth - g_z_a, -1e-7f);
}

// Camera-relative position of a screen point, by inverting the rows: the jitter is a multiple of the forward axis in x and y.
float3 position_at(float2 uv, float z) {
    const float2 ndc = float2(uv.x * 2.0f - 1.0f, 1.0f - uv.y * 2.0f);
    return (g_right * ((ndc.x - g_jx) / g_p00) + g_up * ((ndc.y - g_jy) / g_p11) + g_forward) * z;
}

float3 to_cells(float3 p) {
    const float w = dot(g_row_w, float4(p, 1.0f));
    const float x = dot(g_row_x, float4(p, 1.0f)) / w;
    const float y = dot(g_row_y, float4(p, 1.0f)) / w;
    return float3((x * 0.5f + 0.5f) * g_cells_x, (0.5f - y * 0.5f) * g_cells_y, g_z_a + g_z_b / w);
}

// Where a point was last frame, without last frame's jitter: the history is an average over many jitters, on the cell centres.
float2 to_prev_uv(float3 p, out float w) {
    const float4 h = float4(p, 1.0f);
    w = dot(g_prev_w, h);
    const float inv = w > 1e-4f ? 1.0f / w : 0.0f;
    const float2 ndc = float2(dot(g_prev_x, h) * inv - g_prev_jx, dot(g_prev_y, h) * inv - g_prev_jy);
    return float2(ndc.x * 0.5f + 0.5f, 0.5f - ndc.y * 0.5f);
}

float3 decode_normal(float2 packed) {
    const float2 o = packed * 2.0f - 1.0f;
    float3 n = float3(o, 1.0f - abs(o.x) - abs(o.y));
    if (n.z < 0.0f) {
        const float2 s = float2(o.x >= 0.0f ? 1.0f : -1.0f, o.y >= 0.0f ? 1.0f : -1.0f);
        n.xy = (1.0f - abs(o.yx)) * s;
    }
    return normalize(n);
}

// RT2, R16G16B16A16_UNORM: x, y octahedral normal; z high byte roughness, low byte sqrt(R.r); w high byte sqrt(R.g),
// low byte sqrt(R.b). Cleared to zero, which reads as "not written".
struct Surface {
    float2 packed;
    float rough;
    float3 refl;
    bool written;
};

Surface unpack_surface(float4 raw) {
    Surface s;
    const uint z = (uint)round(raw.z * 65535.0f);
    const uint w = (uint)round(raw.w * 65535.0f);
    s.packed = raw.xy;
    s.rough = (float)(z >> 8) / 255.0f;
    const float3 e = float3((float)(z & 255u), (float)(w >> 8), (float)(w & 255u)) / 255.0f;
    s.refl = e * e;
    s.written = raw.x > 0.0f || raw.y > 0.0f;
    return s;
}

float hash12(uint2 p) {
    uint v = p.x * 1597334677u ^ p.y * 3812015801u;
    v *= 1597334677u;
    v ^= v >> 16;
    v *= 2857740885u;
    v ^= v >> 15;
    return (float)(v >> 8) * (1.0f / 16777216.0f);
}

// Fixed per pixel, moved along a low-discrepancy sequence per frame, so the history sees well-spread samples.
float2 random2(uint2 px) {
    const float2 base = float2(hash12(px), hash12(px + uint2(4231u, 7919u)));
    return frac(base + g_frame * float2(0.7548776662f, 0.5698402910f));
}

//------------------------------------------------------------------------------
// Nearest-depth pyramid. Level 0 is half the full size and the texture is padded to a multiple of its top cell, so a
// cell at any level covers exactly 2^level level 0 cells. Padding reads as far, so it never stops a ray.
// Water's depth is in the buffer by now, from its mask pass, but what RT0 holds there is the bed below it. It reads as far
// too, so rays do not stop on a surface the scene has no colour for yet, and water's own rays do not catch their own plane.
//------------------------------------------------------------------------------
Texture2D<float> t_hiz_depth : register(t0);
Texture2D<float4> t_hiz_mask : register(t1);
Texture2D<float> t_hiz_prev : register(t0);
RWTexture2D<float> u_hiz : register(u0);

[numthreads(8, 8, 1)]
void hiz_first(uint3 id : SV_DispatchThreadID) {
    const int2 cell = int2(id.xy);
    const int2 f = cell * 2;
    if (any(f >= int2(g_size))) {
        u_hiz[cell] = 1.0f;
        return;
    }
    const int2 last = int2(g_size) - 1;
    float nearest = 1.0f;
    for (int y = 0; y < 2; y++) {
        for (int x = 0; x < 2; x++) {
            const int3 at = int3(min(f + int2(x, y), last), 0);
            if (t_hiz_mask.Load(at).a <= 0.5f) nearest = min(nearest, t_hiz_depth.Load(at));
        }
    }
    u_hiz[cell] = nearest;
}

[numthreads(8, 8, 1)]
void hiz_down(uint3 id : SV_DispatchThreadID) {
    const int2 f = int2(id.xy) * 2;
    const float a = t_hiz_prev.Load(int3(f, 0));
    const float b = t_hiz_prev.Load(int3(f + int2(1, 0), 0));
    const float c = t_hiz_prev.Load(int3(f + int2(0, 1), 0));
    const float d = t_hiz_prev.Load(int3(f + int2(1, 1), 0));
    u_hiz[id.xy] = min(a, min(b, min(c, d)));
}

//------------------------------------------------------------------------------
// Blurred scene copies. Very bright pixels count for less, so one highlight does not flood a whole mip; ordinary contrast
// keeps its mean, since this light goes back into the frame.
//------------------------------------------------------------------------------
Texture2D<float3> t_color_src : register(t0);
Texture2D<float4> t_color_prev : register(t0);
RWTexture2D<float4> u_color : register(u0);

float3 average4(float3 a, float3 b, float3 c, float3 d) {
    const float wa = 1.0f / (1.0f + 0.1f * dot(a, LUMA));
    const float wb = 1.0f / (1.0f + 0.1f * dot(b, LUMA));
    const float wc = 1.0f / (1.0f + 0.1f * dot(c, LUMA));
    const float wd = 1.0f / (1.0f + 0.1f * dot(d, LUMA));
    return (a * wa + b * wb + c * wc + d * wd) / (wa + wb + wc + wd);
}

[numthreads(8, 8, 1)]
void color_first(uint3 id : SV_DispatchThreadID) {
    const int2 f = int2(id.xy) * 2;
    const int2 last = int2(g_size) - 1;
    const float3 a = clean_radiance(t_color_src.Load(int3(min(f, last), 0)));
    const float3 b = clean_radiance(t_color_src.Load(int3(min(f + int2(1, 0), last), 0)));
    const float3 c = clean_radiance(t_color_src.Load(int3(min(f + int2(0, 1), last), 0)));
    const float3 d = clean_radiance(t_color_src.Load(int3(min(f + int2(1, 1), last), 0)));
    u_color[id.xy] = float4(average4(a, b, c, d), 1.0f);
}

[numthreads(8, 8, 1)]
void color_down(uint3 id : SV_DispatchThreadID) {
    uint w, h;
    t_color_prev.GetDimensions(w, h);
    const int2 f = int2(id.xy) * 2;
    const int2 last = int2(w, h) - 1;
    const float3 a = t_color_prev.Load(int3(min(f, last), 0)).rgb;
    const float3 b = t_color_prev.Load(int3(min(f + int2(1, 0), last), 0)).rgb;
    const float3 c = t_color_prev.Load(int3(min(f + int2(0, 1), last), 0)).rgb;
    const float3 d = t_color_prev.Load(int3(min(f + int2(1, 1), last), 0)).rgb;
    u_color[id.xy] = float4(average4(a, b, c, d), 1.0f);
}

//------------------------------------------------------------------------------
// The trace.
//------------------------------------------------------------------------------
Texture2D<float> t_depth : register(t0);
Texture2D<float4> t_surface : register(t1);
Texture2D<float3> t_scene : register(t2);
Texture2D<float4> t_pyramid : register(t3);
Texture2D<float> t_hiz : register(t4);
Texture2D<float4> t_mask : register(t5);
SamplerState s_linear : register(s0);
RWTexture2D<float4> u_ray : register(u0);
RWTexture2D<float4> u_gbuf : register(u1);
RWTexture2D<float> u_len : register(u2);

// A half vector from the visible part of the GGX lobe (Heitz 2018), in a frame whose z is the normal.
float3 sample_visible_normal(float3 ve, float alpha, float2 u) {
    const float3 vh = normalize(float3(alpha * ve.x, alpha * ve.y, ve.z));
    const float len2 = vh.x * vh.x + vh.y * vh.y;
    const float3 t1 = len2 > 1e-7f ? float3(-vh.y, vh.x, 0.0f) / sqrt(len2) : float3(1.0f, 0.0f, 0.0f);
    const float3 t2 = cross(vh, t1);
    const float r = sqrt(u.x);
    const float phi = 2.0f * PI * u.y;
    const float p1 = r * cos(phi);
    const float s = 0.5f * (1.0f + vh.z);
    const float p2 = (1.0f - s) * sqrt(max(1.0f - p1 * p1, 0.0f)) + s * r * sin(phi);
    const float3 nh = p1 * t1 + p2 * t2 + sqrt(max(1.0f - p1 * p1 - p2 * p2, 0.0f)) * vh;
    return normalize(float3(alpha * nh.x, alpha * nh.y, max(nh.z, 0.0f)));
}

float3 sample_direction(float3 N, float3 V, float rough, float2 u) {
    const float3 mirror = reflect(-V, N);
    const float alpha = rough * rough * g_lobe;
    if (alpha < 1e-3f) return mirror;
    const float3 up = abs(N.y) < 0.999f ? float3(0.0f, 1.0f, 0.0f) : float3(1.0f, 0.0f, 0.0f);
    const float3 tx = normalize(cross(up, N));
    const float3 ty = cross(N, tx);
    const float3 ve = float3(dot(V, tx), dot(V, ty), dot(V, N));
    const float3 h = sample_visible_normal(ve, alpha, u);
    const float3 R = reflect(-V, tx * h.x + ty * h.y + N * h.z);
    return dot(R, N) > 0.02f ? normalize(R) : mirror;
}

// Walks o + d * t over [0, t_end] in cells and raw depth, all of which are linear along the ray. A surface stops the ray
// when it reaches the surface from in front, or when it enters a cell behind the surface by less than the thickness.
// Deeper than that it has passed behind something, and goes on one cell at a time until it comes out again.
// Points in or under the plane the ray started on are the surface seeing itself at a grazing angle; those do not count.
bool march(float3 o, float3 d, float t_end, float3 P0, float3 N, float3 R, out float t_hit, out float z_hit) {
    t_hit = 0.0f;
    z_hit = 1.0f;
    const float2 big = float2(abs(d.x) > 1e-6f ? d.x : 1e-6f, abs(d.y) > 1e-6f ? d.y : 1e-6f);
    const float2 inv = 1.0f / big;
    const float2 up = float2(big.x > 0.0f ? 1.0f : 0.0f, big.y > 0.0f ? 1.0f : 0.0f);
    const float t_per_cell = 1.0f / max(max(abs(d.x), abs(d.y)), 1e-3f);
    const float nudge = 0.02f * t_per_cell;
    const float above_plane = g_self_plane * max(dot(N, R), 0.02f);
    float t = min(1.5f * t_per_cell, 0.1f * t_end);  // off the cell it started in, unless the ray barely moves on screen
    int level = 0;
    const int top = (int)g_top_level;
    [loop] for (int i = 0; i < g_steps; i++) {
        if (t >= t_end) return false;
        const float3 p = o + d * t;
        const int2 cell = int2(p.xy) >> level;
        const float size = (float)(1 << level);
        const float2 face = ((float2)cell + up) * size;
        const float2 tf = (face - o.xy) * inv;
        const float t_face = min(tf.x, tf.y);
        const float zmin = t_hiz.Load(int3(cell, level));
        if (p.z < zmin) {
            const float t_plane = d.z > 0.0f ? (zmin - o.z) / d.z : 1e30f;
            if (t_plane >= t_face) {
                t = max(t_face, t) + nudge;
                level = min(level + 1, top);
                continue;
            }
            t = max(t_plane, t);
            if (level > 0) {
                level--;
                continue;
            }
        } else if (level > 0) {
            level--;
            continue;
        }
        // Level 0: at or behind the nearest surface of this cell.
        if (zmin >= g_sky_depth) return false;
        const float3 q = o + d * t;
        const float zq = view_z(q.z), zs = view_z(zmin);
        const float slope = abs(view_z(min(q.z + abs(d.z) * t_per_cell, 0.9999999f)) - zq);
        // The real depth under the ray, not the cell's nearest: on a grazing floor that one sits well off the plane.
        // At that pixel's own centre, where that depth is true: anywhere else in the pixel it is off the plane, the more so on a grazing floor.
        const int2 px_hit = min(int2(q.xy / float2(g_cells_x, g_cells_y) * g_size), int2(g_size) - 1);
        const float real_z = view_z(t_depth.Load(int3(px_hit, 0)));
        const float3 away = position_at((float2(px_hit) + 0.5f) * g_inv_size, real_z) - P0;
        const bool near_enough = zq - zs <= g_thickness + g_thickness_scale * zs + slope;
        if (near_enough && dot(N, away) > above_plane * length(away)) {
            t_hit = t;
            z_hit = zmin;
            return true;
        }
        t = max(t_face, t) + nudge;
    }
    return false;
}

float edge_fade(float2 uv) {
    const float2 e = saturate(min(uv, 1.0f - uv) / g_edge);
    const float2 s = e * e * (3.0f - 2.0f * e);
    return s.x * s.y;
}

float3 scene_at(float2 uv, float footprint) {
    // The footprint is a width in full-resolution pixels; the pyramid starts at two.
    const float3 sharp = t_scene.SampleLevel(s_linear, uv, 0.0f);
    const float3 soft = t_pyramid.SampleLevel(s_linear, uv, log2(max(footprint * 0.5f, 1.0f))).rgb;
    return clean_radiance(footprint <= 2.0f ? sharp : soft);
}

[numthreads(8, 8, 1)]
void trace_main(uint3 id : SV_DispatchThreadID) {
    const int2 cell = int2(id.xy);
    if (any(cell >= g_half_size)) return;
    const int2 full = min(cell * 2 + g_pattern, int2(g_size) - 1);
    const Surface s = unpack_surface(t_surface.Load(int3(full, 0)));
    const float depth = t_depth.Load(int3(full, 0));
    const float vz = view_z(depth);
    const bool water = t_mask.Load(int3(full, 0)).a > 0.5f;
    const float far_fade = 1.0f - saturate((vz - g_far_start) * g_far_k);
    const bool wanted = s.written && !water && s.rough < g_rough_cut && depth < g_sky_depth && far_fade > 0.0f
                        && max(s.refl.r, max(s.refl.g, s.refl.b)) >= g_min_reflectance;
    u_gbuf[cell] = float4(s.packed, wanted ? s.rough : -1.0f, vz);
    u_ray[cell] = float4(0.0f, 0.0f, 0.0f, 0.0f);
    u_len[cell] = 0.0f;
    if (!wanted) return;

    const float2 uv = (float2(full) + 0.5f) * g_inv_size;
    const float3 P = position_at(uv, vz);
    const float3 V = -normalize(P);
    const float3 N = decode_normal(s.packed);
    const float3 R = sample_direction(N, V, s.rough, random2(id.xy));

    // Off the surface a little, so depth precision does not catch the start of the ray.
    const float3 P0 = P + N * (0.002f + 0.0008f * vz);
    float len = g_max_distance;
    const float w0 = dot(g_row_w.xyz, P0);
    const float wd = dot(g_row_w.xyz, R);
    if (wd < 0.0f) len = min(len, (g_near_w - w0) / wd);
    if (len <= 0.0f) return;

    const float3 o = to_cells(P0);
    const float3 d = to_cells(P0 + R * len) - o;
    const float2 tb = float2(d.x > 0.0f ? (g_cells_x - o.x) / max(d.x, 1e-6f) : o.x / max(-d.x, 1e-6f),
                             d.y > 0.0f ? (g_cells_y - o.y) / max(d.y, 1e-6f) : o.y / max(-d.y, 1e-6f));
    const float t_end = min(1.0f, min(tb.x, tb.y));

    // Half of the lobe's width is already in the ray's own spread; the cone covers the rest.
    const float alpha = s.rough * s.rough;
    const float cone = alpha * g_cone;
    const float toward = saturate(1.0f + min(dot(g_forward, R), 0.0f) * g_toward_k);
    float t_hit = 0.0f, z_hit = 1.0f;
    float conf = 0.0f, ray_len = 0.0f;
    float3 light = float3(0.0f, 0.0f, 0.0f);
    if (t_end > 0.0f && march(o, d, t_end, P0, N, R, t_hit, z_hit)) {
        const float2 hit_uv = (o.xy + d.xy * t_hit) / float2(g_cells_x, g_cells_y);
        const float zs = view_z(z_hit);
        const float3 H = position_at(hit_uv, zs);
        ray_len = length(H - P0);
        const float width = 2.0f * ray_len * cone * g_p11 * g_size.y * 0.5f / max(zs, 1e-3f);
        const int2 hit_full = clamp(int2(hit_uv * g_size), int2(0, 0), int2(g_size) - 1);
        const Surface hs = unpack_surface(t_surface.Load(int3(hit_full, 0)));
        const bool back_face = hs.written && dot(decode_normal(hs.packed), R) > 0.2f;
        if (!back_face) {
            conf = edge_fade(hit_uv) * toward * (1.0f - smoothstep(0.75f, 1.0f, ray_len / g_max_distance));
            light = scene_at(hit_uv, width);
        }
    } else if (g_sky_hits > 0.0f && wd > 1e-3f) {
        // Nothing in the way up to the end of the ray: if its direction lands on visible sky, that is what it sees.
        const float2 inf_uv = float2(dot(g_row_x.xyz, R) / wd * 0.5f + 0.5f, 0.5f - dot(g_row_y.xyz, R) / wd * 0.5f);
        if (all(inf_uv > 0.0f) && all(inf_uv < 1.0f)) {
            const int2 inf_cell = int2(inf_uv * float2(g_cells_x, g_cells_y));
            // The pyramid reads water as far, the depth buffer does not.
            if (t_hiz.Load(int3(inf_cell, 0)) >= g_sky_depth && t_depth.Load(int3(int2(inf_uv * g_size), 0)) >= g_sky_depth) {
                conf = edge_fade(inf_uv) * g_sky_hits;
                ray_len = g_max_distance;
                light = scene_at(inf_uv, 2.0f * cone * g_p11 * g_size.y * 0.5f);
            }
        }
    }
    conf *= far_fade;
    if (conf <= 0.0f) return;
    u_ray[cell] = float4(light * conf, conf);
    u_len[cell] = min(ray_len, 8000.0f);
}

//------------------------------------------------------------------------------
// Spatial filter: a rotating disk over this frame's rays, in half-resolution cells. Width follows the cone, so a contact
// point stays sharp and a mirror is not touched.
//------------------------------------------------------------------------------
Texture2D<float4> t_sp_ray : register(t0);
Texture2D<float4> t_sp_gbuf : register(t1);
Texture2D<float> t_sp_len : register(t2);
RWTexture2D<float4> u_sp : register(u0);

static const float2 kDisk[8] = {
    float2(0.2500f, 0.0000f), float2(-0.3013f, 0.2760f), float2(0.0543f, -0.5564f), float2(0.4358f, 0.4950f),
    float2(-0.7339f, -0.1216f), float2(0.6949f, -0.4426f), float2(-0.2263f, 0.8892f), float2(-0.4603f, -0.8870f),
};

[numthreads(8, 8, 1)]
void spatial_main(uint3 id : SV_DispatchThreadID) {
    const int2 cell = int2(id.xy);
    if (any(cell >= g_half_size)) return;
    const float4 g = t_sp_gbuf.Load(int3(cell, 0));
    float4 center = t_sp_ray.Load(int3(cell, 0));
    if (g.z < 0.0f) {
        u_sp[cell] = float4(0.0f, 0.0f, 0.0f, 0.0f);
        return;
    }
    const float alpha = g.z * g.z;
    const float len = t_sp_len.Load(int3(cell, 0));
    // A missed ray has no length; use the far end of what the cone could cover.
    const float reach = len > 0.0f ? len : g_max_distance * 0.25f;
    const float width = reach * alpha * g_p11 * g_cells_y * 0.5f / max(g.w + reach, 1e-3f);
    const float radius = min(width, g_spatial_radius * saturate(g.z * 4.0f));
    if (radius < 0.5f) {
        u_sp[cell] = center;
        return;
    }
    const float3 N = decode_normal(g.xy);
    const float angle = hash12(id.xy) * 6.2831853f + g_frame * 2.3999632f;
    const float co = cos(angle), si = sin(angle);
    float4 sum = center;
    float weight = 1.0f;
    for (int i = 0; i < 8; i++) {
        const float2 off = float2(kDisk[i].x * co - kDisk[i].y * si, kDisk[i].x * si + kDisk[i].y * co) * radius;
        const int2 q = clamp(cell + int2(round(off)), int2(0, 0), g_half_size - 1);
        const float4 h = t_sp_gbuf.Load(int3(q, 0));
        if (h.z < 0.0f) continue;
        const float wz = 1.0f / (1.0f + abs(h.w - g.w) / (0.01f * g.w + 0.02f));
        const float nd = saturate(dot(N, decode_normal(h.xy)));
        const float nd2 = nd * nd, nd4 = nd2 * nd2;
        const float wr = 1.0f / (1.0f + 16.0f * abs(h.z - g.z));
        const float w = wz * wz * nd4 * nd4 * wr;
        sum += t_sp_ray.Load(int3(q, 0)) * w;
        weight += w;
    }
    u_sp[cell] = sum / weight;
}

//------------------------------------------------------------------------------
// Temporal filter. The velocity target does not exist yet at this point of the frame, so the camera's own motion is
// reprojected by matrices. Moving objects are held in check by the neighbourhood clamp alone.
//------------------------------------------------------------------------------
Texture2D<float4> t_tm_cur : register(t0);
Texture2D<float4> t_tm_gbuf : register(t1);
Texture2D<float> t_tm_len : register(t2);
Texture2D<float4> t_tm_hist : register(t3);
Texture2D<float4> t_tm_prev_gbuf : register(t4);
RWTexture2D<float4> u_tm : register(u0);

[numthreads(8, 8, 1)]
void temporal_main(uint3 id : SV_DispatchThreadID) {
    const int2 cell = int2(id.xy);
    if (any(cell >= g_half_size)) return;
    const float4 g = t_tm_gbuf.Load(int3(cell, 0));
    if (g.z < 0.0f) {
        u_tm[cell] = float4(0.0f, 0.0f, 0.0f, 0.0f);
        return;
    }
    const float4 cur = t_tm_cur.Load(int3(cell, 0));
    float blend = 0.0f;
    float4 hist = cur;
    if (g_history_valid > 0.0f) {
        // The cell's centre with no jitter, so that a still camera lands exactly on a history texel and bilinear adds no blur.
        const float2 uv = (float2(cell) + 0.5f) / float2(g_half_size);
        const float2 ndc_c = float2(uv.x * 2.0f - 1.0f, 1.0f - uv.y * 2.0f);
        const float3 P = (g_right * (ndc_c.x / g_p00) + g_up * (ndc_c.y / g_p11) + g_forward) * g.w;
        float w_surface, w_virtual;
        const float2 uv_surface = to_prev_uv(P + g_camera_delta, w_surface);
        // Seen in a smooth surface, a reflection sits behind the mirror by the ray's length, and moves like that point.
        const float len = t_tm_len.Load(int3(cell, 0));
        const float2 uv_virtual = to_prev_uv(P + normalize(P) * len + g_camera_delta, w_virtual);
        const float mirror = (len > 0.0f && w_virtual > 0.0f) ? 1.0f - saturate(g.z / 0.3f) : 0.0f;
        const float2 uv_prev = lerp(uv_surface, uv_virtual, mirror);
        if (w_surface > 0.0f && all(uv_surface > 0.0f) && all(uv_surface < 1.0f) && all(uv_prev > 0.0f) && all(uv_prev < 1.0f)) {
            const int2 q = clamp(int2(uv_surface * float2(g_half_size)), int2(0, 0), g_half_size - 1);
            const float4 was = t_tm_prev_gbuf.Load(int3(q, 0));
            // The pattern moves the sample by a pixel every frame, and a cell of a tilted surface spans this much view depth.
            const float3 n_now = decode_normal(g.xy);
            const float facing = max(abs(dot(n_now, normalize(P))), 0.02f);
            const float per_cell = g.w * sqrt(max(1.0f - facing * facing, 0.0f)) / facing / (g_p11 * g_cells_y * 0.5f);
            const bool same = was.z >= 0.0f && abs(was.w - w_surface) <= 0.06f + 0.03f * w_surface + 1.5f * per_cell
                              && dot(n_now, decode_normal(was.xy)) > 0.7f;
            if (same) {
                // Clamp to the spread of this frame's neighbourhood, among cells that take part.
                float4 m1 = cur, m2 = cur * cur;
                float n = 1.0f;
                for (int y = -1; y <= 1; y++) {
                    for (int x = -1; x <= 1; x++) {
                        if (x == 0 && y == 0) continue;
                        const int2 c2 = clamp(cell + int2(x, y), int2(0, 0), g_half_size - 1);
                        if (t_tm_gbuf.Load(int3(c2, 0)).z < 0.0f) continue;
                        const float4 v = t_tm_cur.Load(int3(c2, 0));
                        m1 += v;
                        m2 += v * v;
                        n += 1.0f;
                    }
                }
                m1 /= n;
                m2 /= n;
                const float4 sigma = sqrt(max(m2 - m1 * m1, 0.0f)) * g_clip + 1e-4f;
                hist = clamp(t_tm_hist.SampleLevel(s_linear, uv_prev, 0.0f), m1 - sigma, m1 + sigma);
                // A mirror has next to no ray noise for the history to take out, and each resample softens it a little.
                blend = g_history * saturate(0.5f + g.z * 2.5f);
            }
        }
    }
    float4 result = lerp(cur, hist, blend);
    // History can only pass on what this filter wrote, but a reset must not depend on that.
    if (!is_finite4(result)) result = is_finite4(cur) ? cur : float4(0.0f, 0.0f, 0.0f, 0.0f);
    result.rgb = clean_radiance(result.rgb);
    result.a = saturate(result.a);
    u_tm[cell] = result;
}

//------------------------------------------------------------------------------
// Composite, at full resolution, over a copy of the scene taken before this pass: the target has no signed values, so a
// signed blend is not something to lean on.
//------------------------------------------------------------------------------
Texture2D<float4> c_resolved : register(t0);
Texture2D<float4> c_gbuf : register(t1);
Texture2D<float4> c_surface : register(t2);
Texture2D<float3> c_probe : register(t3);     // RT3: S + R * f0
Texture2D<float> c_depth : register(t4);
Texture2D<float4> c_mask : register(t5);
Texture2D<float3> c_scene : register(t6);

float4 fullscreen_vs(uint id : SV_VertexID) : SV_Position {
    const float2 p = float2((id << 1) & 2, id & 2);
    return float4(p * float2(2.0f, -2.0f) + float2(-1.0f, 1.0f), 0.0f, 1.0f);
}

float4 composite_ps(float4 pos : SV_Position) : SV_Target0 {
    const int2 px = int2(pos.xy);
    const Surface s = unpack_surface(c_surface.Load(int3(px, 0)));
    const bool wanted = s.written && s.rough < g_rough_cut && c_mask.Load(int3(px, 0)).a <= 0.5f;

    float4 c = float4(0.0f, 0.0f, 0.0f, 0.0f);
    if (wanted) {
        const float vz = view_z(c_depth.Load(int3(px, 0)));
        const float3 N = decode_normal(s.packed);
        const float2 f = (float2(px) + 0.5f) * 0.5f - 0.5f;
        const int2 base = int2(floor(f));
        const float2 frac_part = f - float2(base);
        float weight = 0.0f;
        for (int j = 0; j < 2; j++) {
            for (int i = 0; i < 2; i++) {
                const int2 q = clamp(base + int2(i, j), int2(0, 0), g_half_size - 1);
                const float4 g = c_gbuf.Load(int3(q, 0));
                if (g.z < 0.0f) continue;
                const float bilinear = (i != 0 ? frac_part.x : 1.0f - frac_part.x) * (j != 0 ? frac_part.y : 1.0f - frac_part.y) + 1e-3f;
                const float wz = 1.0f / (1.0f + abs(g.w - vz) / (0.01f * vz + 0.02f));
                const float nd = saturate(dot(N, decode_normal(g.xy)));
                const float nd2 = nd * nd, nd4 = nd2 * nd2;
                const float w = bilinear * wz * wz * nd4 * nd4;
                c += c_resolved.Load(int3(q, 0)) * w;
                weight += w;
            }
        }
        c = weight > 1e-5f ? c / weight : float4(0.0f, 0.0f, 0.0f, 0.0f);
    }

    if (g_debug > 0.0f) {
        if (g_debug < 1.5f) return float4(c.rgb, 1.0f);                                    // the traced light
        if (g_debug < 2.5f) return float4(c.aaa, 1.0f);                                    // confidence
        if (g_debug < 3.5f) return float4(s.written ? s.rough : 0.0f, wanted ? 1.0f : 0.0f, 0.0f, 1.0f);  // roughness, kept or not
        if (g_debug < 4.5f) return float4(s.refl, 1.0f);                                   // reflectance
        return float4(c_probe.Load(int3(px, 0)), 1.0f);                                    // what a hit takes out: S + R * f0
    }

    if (!wanted) discard;
    const float t = saturate((s.rough - g_rough_full) / max(g_rough_cut - g_rough_full, 1e-3f));
    const float k = g_strength * (1.0f - t * t * (3.0f - 2.0f * t));
    const float3 probe = clean_radiance(c_probe.Load(int3(px, 0)));
    const float3 delta = k * (min(s.refl * g_gain, 1.0f) * c.rgb - c.a * probe);
    if (!is_finite3(delta) || all(abs(delta) < 1e-6f)) discard;
    const float3 scene = c_scene.Load(int3(px, 0));
    return float4(max((is_finite3(scene) ? scene : float3(0.0f, 0.0f, 0.0f)) + delta, 0.0f), 1.0f);
}
