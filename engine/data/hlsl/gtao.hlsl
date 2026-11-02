// Ground-truth ambient occlusion: horizon search per slice with a cosine-weighted arc integral
// (after Jimenez et al., "Practical Real-Time Strategies for Accurate Indirect Occlusion", 2016).
// Output goes into the game's AO texture; the forward shaders read its red channel.

cbuffer Constants : register(b0) {
    float2 g_size, g_inv_size;
    float g_p00, g_p11, g_z_a, g_z_b;
    float g_radius, g_falloff, g_power, g_max_pixels;
    float g_thickness, g_fade_start, g_fade_end, g_frame;
    int g_slices, g_steps, g_unused0, g_unused1;
    float3 g_sun_view;      // toward the sun, view space
    float g_contact_length; // world units; 0 turns the contact shadow off
    float g_contact_fade;   // view distance at which it is gone
    float3 g_unused2;
};

static const float PI = 3.14159265f;
static const float SKY_Z = 1500.0f;

//------------------------------------------------------------------------------
// Depth pyramid: view-space Z, 5 mips, one dispatch of 16x16 tiles.
//------------------------------------------------------------------------------
Texture2D<float> t_raw_depth : register(t0);
RWTexture2D<float> u_mip0 : register(u0);
RWTexture2D<float> u_mip1 : register(u1);
RWTexture2D<float> u_mip2 : register(u2);
RWTexture2D<float> u_mip3 : register(u3);
RWTexture2D<float> u_mip4 : register(u4);

float view_z(float depth) {
    // z_ndc = z_a + z_b / z, so z = z_b / (z_ndc - z_a).
    return g_z_b / min(depth - g_z_a, -1e-7f);
}

// Average the samples near the closest one, so a mip keeps a real surface instead of inventing one between
// a foreground and a background.
float filter4(float a, float b, float c, float d) {
    float nearest = min(min(a, b), min(c, d));
    float range = max(nearest * 0.08f, 0.25f);
    float wa = a - nearest < range, wb = b - nearest < range, wc = c - nearest < range, wd = d - nearest < range;
    return (a * wa + b * wb + c * wc + d * wd) / (wa + wb + wc + wd);
}

groupshared float gs_z[16][16];

[numthreads(16, 16, 1)]
void depth_main(uint3 group : SV_GroupID, uint3 local : SV_GroupThreadID, uint index : SV_GroupIndex) {
    uint2 base = group.xy * 16;
    uint2 px = base + local.xy;
    // Tiles can overhang the right and bottom edges; clamp so an out-of-range load does not read as near depth.
    float z = view_z(t_raw_depth.Load(int3(min(px, uint2(g_size) - 1), 0)));
    u_mip0[px] = z;
    gs_z[local.y][local.x] = z;
    GroupMemoryBarrierWithGroupSync();
    if ((local.x & 1) == 0 && (local.y & 1) == 0) {
        float v = filter4(gs_z[local.y][local.x], gs_z[local.y][local.x + 1], gs_z[local.y + 1][local.x], gs_z[local.y + 1][local.x + 1]);
        u_mip1[px >> 1] = v;
        gs_z[local.y][local.x] = v;
    }
    GroupMemoryBarrierWithGroupSync();
    if ((local.x & 3) == 0 && (local.y & 3) == 0) {
        float v = filter4(gs_z[local.y][local.x], gs_z[local.y][local.x + 2], gs_z[local.y + 2][local.x], gs_z[local.y + 2][local.x + 2]);
        u_mip2[px >> 2] = v;
        gs_z[local.y][local.x] = v;
    }
    GroupMemoryBarrierWithGroupSync();
    if ((local.x & 7) == 0 && (local.y & 7) == 0) {
        float v = filter4(gs_z[local.y][local.x], gs_z[local.y][local.x + 4], gs_z[local.y + 4][local.x], gs_z[local.y + 4][local.x + 4]);
        u_mip3[px >> 3] = v;
        gs_z[local.y][local.x] = v;
    }
    GroupMemoryBarrierWithGroupSync();
    if (local.x == 0 && local.y == 0) {
        u_mip4[px >> 4] = filter4(gs_z[0][0], gs_z[0][8], gs_z[8][0], gs_z[8][8]);
    }
}

//------------------------------------------------------------------------------
// AO.
//------------------------------------------------------------------------------
Texture2D<float> t_z : register(t0);
SamplerState s_point : register(s0);
RWTexture2D<unorm float4> u_ao : register(u0);

float3 view_pos(float2 uv, float z) {
    float2 ndc = float2(uv.x * 2.0f - 1.0f, 1.0f - uv.y * 2.0f);
    return float3(ndc.x / g_p00, ndc.y / g_p11, 1.0f) * z;
}

float ign(float2 px) {
    return frac(52.9829189f * frac(dot(px, float2(0.06711056f, 0.00583715f))));
}

// Normal from depth: for each axis, use the side whose depth continues the surface best.
float3 depth_normal(int2 px, float3 p) {
    float zl = t_z.Load(int3(px + int2(-1, 0), 0)), zr = t_z.Load(int3(px + int2(1, 0), 0));
    float zu = t_z.Load(int3(px + int2(0, -1), 0)), zd = t_z.Load(int3(px + int2(0, 1), 0));
    float2 uv = (px + 0.5f) * g_inv_size;
    float3 pl = view_pos(uv - float2(g_inv_size.x, 0), zl), pr = view_pos(uv + float2(g_inv_size.x, 0), zr);
    float3 pu = view_pos(uv - float2(0, g_inv_size.y), zu), pd = view_pos(uv + float2(0, g_inv_size.y), zd);
    float3 dx = abs(zl - p.z) < abs(zr - p.z) ? p - pl : pr - p;
    float3 dy = abs(zu - p.z) < abs(zd - p.z) ? pu - p : p - pd;
    // +x right, +y up, +z away from the camera: right x up points away, so flip to face the camera.
    return normalize(cross(dx, dy)) * -1.0f;
}

// Screen-space ray toward the sun. The real-time shadow map holds only the player and a few objects, and the
// bake knows nothing of them, so this is what seats things on the ground. 1 = lit.
float contact_shadow(float3 p, float3 n, float z, float noise) {
    if (g_contact_length <= 0.0f || z >= g_contact_fade) return 1.0f;
    const float facing = dot(n, g_sun_view);
    if (facing <= 0.0f) return 1.0f;
    const int STEPS = 14;
    const float thickness = max(0.3f, z * 0.02f);
    const float3 origin = p + n * max(0.012f, z * 0.0015f);
    float occlusion = 0.0f;
    for (int i = 0; i < STEPS; i++) {
        float t = (i + noise) / STEPS;
        float3 q = origin + g_sun_view * g_contact_length * t * t;
        if (q.z <= 0.05f) break;
        float2 quv = float2(q.x * g_p00 / q.z * 0.5f + 0.5f, 0.5f - q.y * g_p11 / q.z * 0.5f);
        if (any(quv < 0.0f) || any(quv > 1.0f)) break;
        float dz = q.z - t_z.SampleLevel(s_point, quv, 0.0f);
        if (dz > 0.0f && dz < thickness) {
            // A hit near the receiver is a hard contact; farther along the ray the penumbra has opened up.
            occlusion = 1.0f - t * t;
            break;
        }
    }
    return 1.0f - occlusion * saturate(1.0f - z / g_contact_fade) * saturate(facing * 6.0f);
}

[numthreads(8, 8, 1)]
void ao_main(uint3 id : SV_DispatchThreadID) {
    int2 px = int2(id.xy);
    float z = t_z.Load(int3(px, 0));
    if (z >= SKY_Z || z >= g_fade_end) {
        u_ao[px] = float4(1, 1, 0.5f, 0.5f);
        return;
    }
    float2 uv = (px + 0.5f) * g_inv_size;
    float3 p = view_pos(uv, z);
    float3 n = depth_normal(px, p);
    float3 v = normalize(-p);

    // World radius in pixels at this depth.
    float radius_px = g_radius * g_p11 * 0.5f * g_size.y / z;
    float scale = min(1.0f, g_max_pixels / max(radius_px, 1.0f));
    float radius = g_radius * scale;
    radius_px *= scale;

    float noise_angle = ign(px + 5.588238f * g_frame);
    float noise_step = ign(px.yx + float2(47.0f, 17.0f) * 5.588238f * (g_frame + 1.0f));

    float visibility = 0.0f;
    float weight_sum = 0.0f;
    const float falloff_from = radius * (1.0f - g_falloff);
    const float falloff_mul = 1.0f / max(radius - falloff_from, 1e-4f);

    for (int slice = 0; slice < g_slices; slice++) {
        float phi = (slice + noise_angle) / g_slices * PI;
        float2 omega = float2(cos(phi), sin(phi));
        float3 dir = float3(omega.x, omega.y, 0.0f);
        // Slice plane contains v; its normal is ortho x v.
        float3 ortho = dir - dot(dir, v) * v;
        float3 axis = normalize(cross(ortho, v));
        float3 proj_n = n - axis * dot(n, axis);
        float proj_len = length(proj_n);
        if (proj_len < 1e-4f) continue;
        float cos_n = saturate(dot(proj_n / proj_len, v));
        float sign_n = sign(dot(ortho, proj_n));
        float n_angle = sign_n * acos(cos_n);

        float horizon[2];
        for (int side = 0; side < 2; side++) {
            float side_sign = side == 0 ? 1.0f : -1.0f;
            float best = -1.0f;
            for (int s = 0; s < g_steps; s++) {
                // Quadratic spacing favours close occluders, which is where contact detail lives.
                float t = (s + noise_step) / g_steps;
                t *= t;
                float offset_px = t * radius_px + 1.0f;
                float2 sample_uv = uv + side_sign * float2(omega.x, -omega.y) * offset_px * g_inv_size;
                if (any(sample_uv < 0.0f) || any(sample_uv > 1.0f)) break;
                float mip = clamp(log2(offset_px) - 3.0f, 0.0f, 4.0f);
                float sz = t_z.SampleLevel(s_point, sample_uv, mip);
                float3 d = view_pos(sample_uv, sz) - p;
                float len = length(d);
                if (len < 1e-4f) continue;
                float cos_h = dot(d / len, v);
                // Distant samples and thin foreground both lose their hold on the horizon.
                float weight = saturate((radius - len) * falloff_mul);
                if (d.z < -g_thickness * radius) weight *= saturate(1.0f + (d.z + g_thickness * radius) / max(radius, 1e-3f));
                float low = sign_n * 0.0f + cos(n_angle + side_sign * (PI * 0.5f));
                cos_h = lerp(low, cos_h, weight);
                best = max(best, cos_h);
            }
            float low_limit = cos(n_angle + side_sign * (PI * 0.5f));
            horizon[side] = acos(clamp(max(best, low_limit), -1.0f, 1.0f));
        }
        // Clamp horizons to the hemisphere about the projected normal and integrate.
        float h1 = n_angle + max(-horizon[1] - n_angle, -PI * 0.5f);
        float h2 = n_angle + min(horizon[0] - n_angle, PI * 0.5f);
        float sin_n = sin(n_angle);
        float arc = 0.25f * (-cos(2.0f * h1 - n_angle) + cos_n + 2.0f * h1 * sin_n) +
                    0.25f * (-cos(2.0f * h2 - n_angle) + cos_n + 2.0f * h2 * sin_n);
        visibility += proj_len * saturate(arc);
        weight_sum += proj_len;
    }
    float ao = weight_sum > 0.0f ? visibility / weight_sum : 1.0f;
    ao = pow(saturate(ao), g_power);
    ao = lerp(ao, 1.0f, saturate((z - g_fade_start) / max(g_fade_end - g_fade_start, 1.0f)));
    float contact = contact_shadow(p, n, z, noise_step);
    u_ao[px] = float4(ao, contact, n.x * 0.5f + 0.5f, n.y * 0.5f + 0.5f);
}

//------------------------------------------------------------------------------
// Edge-aware denoise, two passes. TAA takes the rest of the noise out over time.
//------------------------------------------------------------------------------
Texture2D<float4> t_in : register(t1);
RWTexture2D<unorm float4> u_out : register(u0);

[numthreads(8, 8, 1)]
void denoise_main(uint3 id : SV_DispatchThreadID) {
    int2 px = int2(id.xy);
    float z = t_z.Load(int3(px, 0));
    float4 center = t_in.Load(int3(px, 0));
    float tolerance = max(z * 0.015f, 0.02f);
    float2 sum = center.rg * 0.25f;
    float weights = 0.25f;
    for (int y = -2; y <= 2; y++) {
        for (int x = -2; x <= 2; x++) {
            if (x == 0 && y == 0) continue;
            int2 q = px + int2(x, y);
            if (any(q < 0) || any(q >= int2(g_size))) continue;
            float dz = abs(t_z.Load(int3(q, 0)) - z);
            float w = saturate(1.0f - dz / tolerance) * (1.0f / (1.0f + x * x + y * y));
            sum += t_in.Load(int3(q, 0)).rg * w;
            weights += w;
        }
    }
    u_out[px] = float4(sum / weights, center.ba);
}
