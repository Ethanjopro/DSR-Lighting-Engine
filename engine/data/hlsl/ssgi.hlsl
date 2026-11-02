// One bounce of light from what is on screen. Rays leave each surface at reduced resolution, march against this frame's
// depth, and take the colour of what they hit from last frame's linear scene, so the bounce is made of the area's own
// colours. Temporal accumulation and an edge-aware blur take the noise out; the result reaches the forward shaders at
// t25 at full resolution, with its strength already in it.
//
// Order at the first SSAO dispatch: prepare_main, normal_main, trace_main, temporal_main, atrous_main (1 to 3 times),
// upsample_main. At the refraction copy: capture_main, capture_mip_main per mip. All in cs_5_0 at feature level 11_0, so
// nothing loads through a typed UAV: every pass reads an SRV and writes another texture.

cbuffer Constants : register(b0) {
    float2 g_full_size, g_full_inv;
    float2 g_trace_size, g_trace_inv;
    float g_p00, g_p11, g_jx, g_jy;          // x_ndc = p00 * x / z + jx, likewise y; jitter included
    float g_z_a, g_z_b, g_radius, g_thickness;
    float4 g_prev_x, g_prev_y, g_prev_w;     // this frame's view space to last frame's clip x, y, w
    float g_frame, g_sky_z, g_sky_weight, g_max_radiance;
    float g_gate, g_strength, g_max_gi, g_history_cap;
    float g_fade_end, g_fade_k, g_history_ok, g_local_mip;
    int g_rays, g_steps, g_block, g_colour_ok;  // g_block: full-resolution pixels per side of a trace pixel
    float3 g_right; int g_normals;           // this frame's camera axes in the world's axes; 1: normals from t3
    float3 g_up; float g_pad0;
    float3 g_forward; float g_pad1;
};

cbuffer PassParams : register(b1) {
    int g_step;   // atrous_main: spacing of the taps
    int3 g_pad2;
};

static const float3 LUMA = float3(0.2126f, 0.7152f, 0.0722f);
static const float NEAR_LIMIT = 0.05f;
SamplerState s_point : register(s0);
SamplerState s_linear : register(s1);

float view_z(float depth) {
    // z_ndc = z_a + z_b / z. Sky is geometry out near the far plane, so this stays finite.
    return g_z_b / min(depth - g_z_a, -1e-7f);
}

float3 view_pos(float2 uv, float z) {
    const float2 ndc = float2(uv.x * 2.0f - 1.0f, 1.0f - uv.y * 2.0f);
    return float3((ndc.x - g_jx) / g_p00, (ndc.y - g_jy) / g_p11, 1.0f) * z;
}

float2 view_to_uv(float3 p) {
    const float2 ndc = float2(p.x * g_p00 / p.z + g_jx, p.y * g_p11 / p.z + g_jy);
    return float2(ndc.x * 0.5f + 0.5f, 0.5f - ndc.y * 0.5f);
}

// xy last frame's uv, z last frame's view depth. z <= 0 is behind last frame's camera.
float3 reproject(float3 p) {
    const float4 h = float4(p, 1.0f);
    const float w = dot(g_prev_w, h);
    const float2 ndc = float2(dot(g_prev_x, h), dot(g_prev_y, h)) / max(abs(w), 1e-4f);
    return float3(ndc.x * 0.5f + 0.5f, 0.5f - ndc.y * 0.5f, w);
}

float ign(float2 px) {
    return frac(52.9829189f * frac(dot(px, float2(0.06711056f, 0.00583715f))));
}

// NaN, infinity and negatives become black: anything else would stay in the history and in the game's TAA.
float3 finite3(float3 c) {
    return (all(c >= 0.0f) && all(c < 1e6f)) ? c : float3(0.0f, 0.0f, 0.0f);
}

//------------------------------------------------------------------------------
// This frame's depth at trace resolution: view Z with the closest sample kept, so thin things survive, and four more
// mips for long ray steps. One dispatch of 16x16 tiles.
//------------------------------------------------------------------------------
Texture2D<float> t_raw_depth : register(t0);
RWTexture2D<float> u_z0 : register(u0);
RWTexture2D<float> u_z1 : register(u1);
RWTexture2D<float> u_z2 : register(u2);
RWTexture2D<float> u_z3 : register(u3);
RWTexture2D<float> u_z4 : register(u4);

groupshared float gs_z[16][16];

[numthreads(16, 16, 1)]
void prepare_main(uint3 group : SV_GroupID, uint3 local : SV_GroupThreadID) {
    const uint2 px = group.xy * 16 + local.xy;
    // Tiles can overhang the edges; clamped so an out-of-range load does not read as near depth.
    const int2 base = min(int2(px) * g_block, int2(g_full_size) - 1);
    const int2 last = min(base + (g_block - 1), int2(g_full_size) - 1);
    const float d = min(min(t_raw_depth.Load(int3(base, 0)), t_raw_depth.Load(int3(last.x, base.y, 0))),
                        min(t_raw_depth.Load(int3(base.x, last.y, 0)), t_raw_depth.Load(int3(last, 0))));
    const float z = view_z(d);
    u_z0[px] = z;
    gs_z[local.y][local.x] = z;
    GroupMemoryBarrierWithGroupSync();
    if ((local.x & 1) == 0 && (local.y & 1) == 0) {
        const float v = min(min(gs_z[local.y][local.x], gs_z[local.y][local.x + 1]), min(gs_z[local.y + 1][local.x], gs_z[local.y + 1][local.x + 1]));
        u_z1[px >> 1] = v;
        gs_z[local.y][local.x] = v;
    }
    GroupMemoryBarrierWithGroupSync();
    if ((local.x & 3) == 0 && (local.y & 3) == 0) {
        const float v = min(min(gs_z[local.y][local.x], gs_z[local.y][local.x + 2]), min(gs_z[local.y + 2][local.x], gs_z[local.y + 2][local.x + 2]));
        u_z2[px >> 2] = v;
        gs_z[local.y][local.x] = v;
    }
    GroupMemoryBarrierWithGroupSync();
    if ((local.x & 7) == 0 && (local.y & 7) == 0) {
        const float v = min(min(gs_z[local.y][local.x], gs_z[local.y][local.x + 4]), min(gs_z[local.y + 4][local.x], gs_z[local.y + 4][local.x + 4]));
        u_z3[px >> 3] = v;
        gs_z[local.y][local.x] = v;
    }
    GroupMemoryBarrierWithGroupSync();
    if (local.x == 0 && local.y == 0) {
        u_z4[px >> 4] = min(min(gs_z[0][0], gs_z[0][8]), min(gs_z[8][0], gs_z[8][8]));
    }
}

//------------------------------------------------------------------------------
// Guide: view-space normal in rgb, view Z in a. The depth-derived normal by default; last frame's G-buffer where there
// is one and the setting asks for it. A camera-facing normal is what the depth gives; the G-buffer adds nothing to a
// surface the depth already describes well, so a miss there falls back without a seam.
//------------------------------------------------------------------------------
Texture2D<float> t_z : register(t0);
Texture2D<float4> t_gbuffer : register(t3);  // world normal * 0.5 + 0.5 in rgb, last frame
Texture2D<float4> t_colour : register(t2);   // last frame's scene: rgb, closest view Z in a
RWTexture2D<float4> u_out : register(u0);

float3 depth_normal(int2 px, float z, float3 p) {
    const int2 hi = int2(g_trace_size) - 1;
    const int2 xl = int2(max(px.x - 1, 0), px.y), xr = int2(min(px.x + 1, hi.x), px.y);
    const int2 yu = int2(px.x, max(px.y - 1, 0)), yd = int2(px.x, min(px.y + 1, hi.y));
    const float zl = t_z.Load(int3(xl, 0)), zr = t_z.Load(int3(xr, 0)), zu = t_z.Load(int3(yu, 0)), zd = t_z.Load(int3(yd, 0));
    // Each axis takes the side that continues the surface.
    const float3 dx = abs(zl - z) < abs(zr - z) ? p - view_pos((xl + 0.5f) * g_trace_inv, zl) : view_pos((xr + 0.5f) * g_trace_inv, zr) - p;
    const float3 dy = abs(zu - z) < abs(zd - z) ? view_pos((yu + 0.5f) * g_trace_inv, zu) - p : p - view_pos((yd + 0.5f) * g_trace_inv, zd);
    float3 n = -cross(dx, dy);  // +x right, +y up, +z away: facing the camera
    const float len = dot(n, n);
    if (!(len > 1e-20f)) return float3(0.0f, 0.0f, -1.0f);
    n /= sqrt(len);
    return dot(n, p) > 0.0f ? -n : n;
}

[numthreads(8, 8, 1)]
void normal_main(uint3 id : SV_DispatchThreadID) {
    const int2 px = int2(id.xy);
    const float z = t_z.Load(int3(px, 0));
    if (!(z < g_sky_z)) {
        u_out[px] = float4(0.0f, 0.0f, 0.0f, min(z, 60000.0f));
        return;
    }
    const float3 p = view_pos((px + 0.5f) * g_trace_inv, z);
    float3 n = depth_normal(px, z, p);
    if (g_normals == 1 && g_colour_ok != 0) {
        const float3 prev = reproject(p);
        if (all(prev.xy > 0.0f) && all(prev.xy < 1.0f) && prev.z > NEAR_LIMIT) {
            float gw, gh;
            t_gbuffer.GetDimensions(gw, gh);
            const float3 w = t_gbuffer.Load(int3(prev.xy * float2(gw, gh), 0)).rgb * 2.0f - 1.0f;
            const float len = dot(w, w);
            const float seen = t_colour.SampleLevel(s_point, prev.xy, 0.0f).a;
            // Draws that write no normal leave the clear colour, which decodes to no unit vector.
            if (len > 0.5f && len < 1.5f && abs(seen - prev.z) < 0.1f + 0.03f * prev.z) {
                const float3 g = float3(dot(w, g_right), dot(w, g_up), dot(w, g_forward)) / sqrt(len);
                if (dot(g, p) < 0.0f) n = g;
            }
        }
    }
    u_out[px] = float4(n, z);
}

//------------------------------------------------------------------------------
// Trace: cosine-weighted rays, so the mean of what they hit is the bounce's outgoing light per unit of albedo.
//------------------------------------------------------------------------------
Texture2D<float4> t_guide : register(t1);

float3 cosine_dir(float3 n, float u1, float u2) {
    const float z = 1.0f - 2.0f * u1;
    const float r = sqrt(saturate(1.0f - z * z));
    const float phi = 6.2831853f * u2;
    // A normal plus a point on the unit sphere is cosine-distributed around the normal.
    return normalize(n + 0.999f * float3(r * cos(phi), r * sin(phi), z));
}

[numthreads(8, 8, 1)]
void trace_main(uint3 id : SV_DispatchThreadID) {
    const int2 px = int2(id.xy);
    const float4 guide = t_guide.Load(int3(px, 0));
    const float z = guide.a;
    if (g_colour_ok == 0 || !(z < g_sky_z) || z >= g_fade_end) {
        u_out[px] = float4(0.0f, 0.0f, 0.0f, 0.0f);
        return;
    }
    const float3 n = guide.rgb;
    const float3 p = view_pos((px + 0.5f) * g_trace_inv, z);
    const float3 origin = p + n * max(0.02f, z * 0.002f);
    const float plane_bias = max(0.03f, z * 0.003f);

    const float seed = g_frame * 5.588238f;
    const float n0 = ign(px + seed), n1 = ign(px.yx + float2(47.0f, 17.0f) * (seed + 5.588238f)), n2 = ign(px + float2(13.0f, 71.0f) * (seed + 11.176476f));

    // How bright it already is around this surface. Light already here is what the probes and lightmaps hold, or light
    // this surface shares with what it sees; only what is brighter than here brings anything new. That is what keeps a
    // room with nothing lit from lifting, and it also stops light running in circles between surfaces. A surface new on
    // screen takes the nearest edge of last frame, or the leading edge of a turn would come up ungated.
    float local = 0.0f;
    {
        const float3 prev = reproject(p);
        if (prev.z > NEAR_LIMIT) {
            local = dot(t_colour.SampleLevel(s_linear, saturate(prev.xy), g_local_mip).rgb, LUMA);
        }
    }

    const float inv_steps = 1.0f / g_steps;
    float3 sum = float3(0.0f, 0.0f, 0.0f);
    float known = 0.0f;
    [loop] for (int ray = 0; ray < g_rays; ray++) {
        const float3 dir = cosine_dir(n, frac(n0 + ray * 0.6180339f), frac(n1 + ray * 0.7548777f));
        const float jitter = frac(n2 + ray * 0.5698403f);
        float t_last = 0.0f;
        float2 uv_last = (px + 0.5f) * g_trace_inv;
        float z_last = 0.0f;
        bool off_screen = false;
        bool hit = false;
        [loop] for (int s = 0; s < g_steps; s++) {
            // Quadratic spacing: bounce is strongest close by.
            const float a = (s + jitter) * inv_steps;
            const float t = a * a * g_radius;
            const float3 q = origin + dir * t;
            if (q.z <= NEAR_LIMIT) { off_screen = true; break; }
            const float2 uv = view_to_uv(q);
            if (any(uv <= 0.0f) || any(uv >= 1.0f)) { off_screen = true; break; }
            const float span_px = length((uv - uv_last) * g_trace_size);
            const float mip = clamp(log2(max(span_px, 1.0f)) - 1.0f, 0.0f, 3.0f);
            const float sz = t_z.SampleLevel(s_point, uv, mip);
            z_last = sz;
            const float dz = q.z - sz;
            const float span = t - t_last;
            uv_last = uv;
            t_last = t;
            if (dz > 0.0f && sz < g_sky_z) {
                const float3 h = view_pos(uv, sz);
                const float3 hn = t_guide.SampleLevel(s_point, uv, 0.0f).rgb;
                // How far behind the surface, measured across it and not along the view ray: the camera sees most ground at
                // a shallow angle, where a ray only just under it is already metres behind it along the view.
                const float across = dz * abs(dot(hn, h)) / max(length(h), 1e-3f);
                if (across >= max(g_thickness, abs(dot(dir, hn)) * span * 1.5f) + sz * 0.005f) continue;
                // Its own surface, seen again at a shallow angle, is not a bounce.
                if (dot(h - p, n) <= plane_bias) continue;
                hit = true;
                // Going the way the surface faces, this ray has come up behind it. The screen shows only the front, and the
                // back is some other brightness, so the ray knows nothing, as if it had left the screen. Judged by the
                // depth's own normal: a G-buffer's normal map could turn a face round.
                float3 facing = hn;
                if (g_normals != 0) {
                    const int2 hit_px = int2(uv * g_trace_size);
                    const float hit_z = t_z.Load(int3(hit_px, 0));
                    facing = depth_normal(hit_px, hit_z, view_pos((hit_px + 0.5f) * g_trace_inv, hit_z));
                }
                if (dot(dir, facing) > 0.0f) break;
                const float3 prev = reproject(h);
                if (prev.z > NEAR_LIMIT && all(prev.xy > 0.0f) && all(prev.xy < 1.0f)) {
                    // What last frame had there must be this surface, not something that has since moved away.
                    const float seen = t_colour.SampleLevel(s_point, prev.xy, 1.0f).a;
                    if (abs(seen - prev.z) < 0.15f + 0.05f * prev.z) {
                        const float dist = length(h - p);
                        const float cone_px = 0.25f * dist * g_p11 * g_full_size.y * 0.25f / max(sz, 0.1f);
                        const float3 c = t_colour.SampleLevel(s_linear, prev.xy, clamp(log2(max(cone_px, 1.0f)), 0.0f, 4.0f)).rgb;
                        const float lum = dot(c, LUMA);
                        const float gain = saturate((lum - g_gate * local) / max(lum, 1e-6f));
                        const float2 border = saturate(min(prev.xy, 1.0f - prev.xy) * 20.0f);
                        const float sure = border.x * border.y;
                        const float reach = saturate((g_radius - t) / (g_radius * 0.3f));
                        sum += c * gain * sure * reach;
                        known += sure;
                    }
                }
                break;
            }
        }
        // Past everything and out into the sky: the capture has already turned it down.
        if (!hit && !off_screen) {
            known += 1.0f;
            if (z_last >= g_sky_z) {
                const float3 prev = reproject(view_pos(uv_last, min(z_last, 60000.0f)));
                if (prev.z > NEAR_LIMIT && all(prev.xy > 0.0f) && all(prev.xy < 1.0f)) {
                    sum += t_colour.SampleLevel(s_linear, prev.xy, 2.0f).rgb;
                }
            }
        }
    }
    // A ray that left the screen says nothing about what is out there. Averaging over the rays that do know keeps the
    // bounce from fading toward the edges of the screen, up to a point: half the rays count whatever happened.
    u_out[px] = float4(finite3(sum / max(known, g_rays * 0.5f)), 1.0f);
}

//------------------------------------------------------------------------------
// Temporal accumulation: history follows the surface by the camera matrices, and is dropped where last frame had
// something else at that place. a is how many frames the pixel has seen, up to the cap. History is not clamped to this
// frame's neighbourhood: a few rays often all miss a small lit surface, and a clamp would turn that into flicker. A
// change in the light takes the length of the history to arrive, a quarter of a second by default.
//------------------------------------------------------------------------------
Texture2D<float4> t_raw : register(t0);
Texture2D<float4> t_history : register(t2);
Texture2D<float> t_z_prev : register(t3);

[numthreads(8, 8, 1)]
void temporal_main(uint3 id : SV_DispatchThreadID) {
    const int2 px = int2(id.xy);
    const float4 guide = t_guide.Load(int3(px, 0));
    const float z = guide.a;
    if (!(z < g_sky_z)) {
        u_out[px] = float4(0.0f, 0.0f, 0.0f, 0.0f);
        return;
    }
    const int2 hi = int2(g_trace_size) - 1;
    const float3 cur = finite3(t_raw.Load(int3(px, 0)).rgb);

    float3 result = cur;
    float age = 0.0f;
    if (g_history_ok != 0.0f) {
        const float3 prev = reproject(view_pos((px + 0.5f) * g_trace_inv, z));
        if (prev.z > NEAR_LIMIT && all(prev.xy > 0.0f) && all(prev.xy < 1.0f)) {
            const float2 st = prev.xy * g_trace_size - 0.5f;
            const int2 b = int2(floor(st));
            const float2 f = st - b;
            const float tolerance = 0.05f + 0.02f * prev.z;
            float3 hist = float3(0.0f, 0.0f, 0.0f);
            float hist_age = 0.0f, weight = 0.0f;
            for (int k = 0; k < 4; k++) {
                const int2 o = int2(k & 1, k >> 1);
                const int2 q = clamp(b + o, int2(0, 0), hi);
                const float w = (o.x ? f.x : 1.0f - f.x) * (o.y ? f.y : 1.0f - f.y) * (abs(t_z_prev.Load(int3(q, 0)) - prev.z) < tolerance ? 1.0f : 0.0f);
                if (w <= 0.0f) continue;
                const float4 h = t_history.Load(int3(q, 0));
                hist += finite3(h.rgb) * w;
                hist_age += h.a * w;
                weight += w;
            }
            if (weight > 0.05f) {
                hist /= weight;
                hist_age /= weight;
                age = min(hist_age + 1.0f, g_history_cap);
                result = lerp(hist, cur, 1.0f / (age + 1.0f));
            }
        }
        // The camera's motion says nothing about what moves with it, and the player is on screen in the same place every
        // frame. So what the matrices lost is looked for at the same pixel, at the same depth, before giving up on it.
        if (age == 0.0f && abs(t_z_prev.Load(int3(px, 0)) - z) < 0.05f + 0.02f * z) {
            const float4 h = t_history.Load(int3(px, 0));
            age = min(h.a + 1.0f, g_history_cap);
            result = lerp(finite3(h.rgb), cur, 1.0f / (age + 1.0f));
        }
    }
    u_out[px] = float4(finite3(result), age);
}

//------------------------------------------------------------------------------
// A-trous blur stopped by surface position and normal. Colour edges stay out of it: bounce has none worth keeping and
// a colour test would keep the noise. A pixel with no history reaches twice as far, so a camera cut or a newly revealed
// surface comes up soft instead of as a burst of noise.
//------------------------------------------------------------------------------
Texture2D<float4> t_in : register(t0);

[numthreads(8, 8, 1)]
void atrous_main(uint3 id : SV_DispatchThreadID) {
    const int2 px = int2(id.xy);
    const float4 c = t_in.Load(int3(px, 0));
    const float4 guide = t_guide.Load(int3(px, 0));
    const float z = guide.a;
    if (!(z < g_sky_z)) {
        u_out[px] = c;
        return;
    }
    const float3 p = view_pos((px + 0.5f) * g_trace_inv, z);
    const int spacing = g_step * (c.a < 4.0f ? 2 : 1);
    const float plane_tolerance = 0.02f + 0.02f * z;
    const int2 hi = int2(g_trace_size) - 1;
    float3 sum = c.rgb * 4.0f;
    float total = 4.0f;
    for (int y = -1; y <= 1; y++) {
        for (int x = -1; x <= 1; x++) {
            if (x == 0 && y == 0) continue;
            const int2 q = px + int2(x, y) * spacing;
            if (any(q < 0) || any(q > hi)) continue;
            const float4 g = t_guide.Load(int3(q, 0));
            if (!(g.a < g_sky_z)) continue;
            const float plane = abs(dot(guide.rgb, view_pos((q + 0.5f) * g_trace_inv, g.a) - p));
            const float wn = saturate(dot(guide.rgb, g.rgb));
            const float w = (x == 0 || y == 0 ? 2.0f : 1.0f) * saturate(1.0f - plane / plane_tolerance) * wn * wn * wn * wn;
            if (w <= 0.0f) continue;
            sum += t_in.Load(int3(q, 0)).rgb * w;
            total += w;
        }
    }
    u_out[px] = float4(sum / total, c.a);
}

//------------------------------------------------------------------------------
// Up to full resolution, keeping to the surface each pixel belongs to. The forward shaders take one Load per pixel,
// with no sampler, and the strength is applied here so a live change needs no constant in the forward path.
//------------------------------------------------------------------------------
Texture2D<float4> t_denoised : register(t2);

[numthreads(8, 8, 1)]
void upsample_main(uint3 id : SV_DispatchThreadID) {
    const int2 px = int2(id.xy);
    const float z = view_z(t_raw_depth.Load(int3(px, 0)));
    if (!(z < g_sky_z) || z >= g_fade_end) {
        u_out[px] = float4(0.0f, 0.0f, 0.0f, 0.0f);
        return;
    }
    const float2 st = (px + 0.5f) * g_full_inv * g_trace_size - 0.5f;
    const int2 b = int2(floor(st));
    const float2 f = st - b;
    const int2 hi = int2(g_trace_size) - 1;
    const float tolerance = 0.02f + 0.01f * z;
    float3 sum = float3(0.0f, 0.0f, 0.0f);
    float total = 0.0f;
    float3 nearest = float3(0.0f, 0.0f, 0.0f);
    float nearest_dz = 1e30f;
    for (int k = 0; k < 4; k++) {
        const int2 o = int2(k & 1, k >> 1);
        const int2 q = clamp(b + o, int2(0, 0), hi);
        const float zq = t_guide.Load(int3(q, 0)).a;
        const float dz = abs(zq - z);
        const float3 c = t_denoised.Load(int3(q, 0)).rgb;
        if (dz < nearest_dz) {
            nearest_dz = dz;
            nearest = c;
        }
        const float w = (o.x ? f.x : 1.0f - f.x) * (o.y ? f.y : 1.0f - f.y) * saturate(1.0f - dz / tolerance);
        sum += c * w;
        total += w;
    }
    float3 gi = total > 1e-3f ? sum / total : nearest;
    gi *= g_strength * saturate((g_fade_end - z) * g_fade_k);
    // A soft ceiling, far above normal values: no path through the feedback from the scene can run away past it.
    gi *= g_max_gi / (g_max_gi + dot(gi, LUMA));
    u_out[px] = float4(finite3(gi), 1.0f);
}

//------------------------------------------------------------------------------
// The scene at the refraction copy: surfaces only, before volumetrics, water and effects. Half resolution with mips,
// luminance capped (a single emissive or glint pixel can hold 400 against a median of 0.01), and sky turned down because
// the probes already carry it. The caps come first, so a sun disc counts no more than a bright wall.
//------------------------------------------------------------------------------
Texture2D<float4> t_scene : register(t0);
Texture2D<float> t_scene_depth : register(t1);

[numthreads(8, 8, 1)]
void capture_main(uint3 id : SV_DispatchThreadID) {
    const int2 base = int2(id.xy) * 2;
    const int2 hi = int2(g_full_size) - 1;
    float3 sum = float3(0.0f, 0.0f, 0.0f);
    float closest = 1e30f;
    for (int k = 0; k < 4; k++) {
        const int3 q = int3(min(base + int2(k & 1, k >> 1), hi), 0);
        float3 c = finite3(t_scene.Load(q).rgb);
        const float lum = dot(c, LUMA);
        if (lum > g_max_radiance) c *= g_max_radiance / lum;
        const float z = view_z(t_scene_depth.Load(q));
        if (!(z < g_sky_z)) c *= g_sky_weight;
        sum += c;
        closest = min(closest, z);
    }
    u_out[id.xy] = float4(sum * 0.25f, min(closest, 60000.0f));
}

[numthreads(8, 8, 1)]
void capture_mip_main(uint3 id : SV_DispatchThreadID) {
    float w, h;
    t_in.GetDimensions(w, h);
    const int2 hi = int2(w, h) - 1;
    const int2 base = int2(id.xy) * 2;
    const float4 a = t_in.Load(int3(min(base, hi), 0));
    const float4 b = t_in.Load(int3(min(base + int2(1, 0), hi), 0));
    const float4 c = t_in.Load(int3(min(base + int2(0, 1), hi), 0));
    const float4 d = t_in.Load(int3(min(base + int2(1, 1), hi), 0));
    u_out[id.xy] = float4((a.rgb + b.rgb + c.rgb + d.rgb) * 0.25f, min(min(a.a, b.a), min(c.a, d.a)));
}
