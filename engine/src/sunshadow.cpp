#include "sunshadow.h"
#include "casters.h"
#include "common.h"
#include "config.h"
#include "state.h"
#include "trace.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <mutex>

namespace {

constexpr UINT kCascades = 4;

ID3D11Device* g_device;
UINT g_atlas_size;
ID3D11Texture2D* g_atlas;
ID3D11DepthStencilView* g_dsv;
ID3D11ShaderResourceView* g_srv;
ID3D11DepthStencilState* g_depth_state;
ID3D11BlendState* g_blend_state;
ID3D11RasterizerState* g_raster[3][2];  // by cull mode (none, front, back) and winding
std::mutex g_create_lock;
bool g_ready;

std::mutex g_constants_lock;
SunShadowConstants g_constants;
bool g_enabled;
float g_far[kCascades];

struct V3 { float x, y, z; };
V3 operator-(V3 a, V3 b) { return {a.x - b.x, a.y - b.y, a.z - b.z}; }
V3 operator+(V3 a, V3 b) { return {a.x + b.x, a.y + b.y, a.z + b.z}; }
V3 operator*(V3 a, float s) { return {a.x * s, a.y * s, a.z * s}; }
float dot(V3 a, V3 b) { return a.x * b.x + a.y * b.y + a.z * b.z; }
V3 cross(V3 a, V3 b) { return {a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x}; }
V3 norm(V3 a) {
    float len = std::sqrt(dot(a, a));
    return len > 0 ? a * (1.0f / len) : a;
}

bool create_resources(UINT size) {
    std::lock_guard<std::mutex> lock(g_create_lock);
    if (g_ready && g_atlas_size == size) return true;
    auto drop = [](auto*& p) { if (p) p->Release(); p = nullptr; };
    drop(g_atlas);
    drop(g_dsv);
    drop(g_srv);
    // Until the new atlas is whole: a size that fails must not pass for the one before it.
    g_ready = false;
    D3D11_TEXTURE2D_DESC d{};
    d.Width = d.Height = size;
    d.MipLevels = 1;
    d.ArraySize = 1;
    d.Format = DXGI_FORMAT_R32_TYPELESS;
    d.SampleDesc.Count = 1;
    d.Usage = D3D11_USAGE_DEFAULT;
    d.BindFlags = D3D11_BIND_DEPTH_STENCIL | D3D11_BIND_SHADER_RESOURCE;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, &g_atlas))) return false;
    D3D11_DEPTH_STENCIL_VIEW_DESC dv{};
    dv.Format = DXGI_FORMAT_D32_FLOAT;
    dv.ViewDimension = D3D11_DSV_DIMENSION_TEXTURE2D;
    if (FAILED(g_device->CreateDepthStencilView(g_atlas, &dv, &g_dsv))) return false;
    D3D11_SHADER_RESOURCE_VIEW_DESC sv{};
    sv.Format = DXGI_FORMAT_R32_FLOAT;
    sv.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
    sv.Texture2D.MipLevels = 1;
    if (FAILED(g_device->CreateShaderResourceView(g_atlas, &sv, &g_srv))) return false;

    if (!g_depth_state) {
        D3D11_DEPTH_STENCIL_DESC ds{};
        ds.DepthEnable = TRUE;
        ds.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ALL;
        ds.DepthFunc = D3D11_COMPARISON_LESS_EQUAL;
        g_device->CreateDepthStencilState(&ds, &g_depth_state);
        D3D11_BLEND_DESC bd{};
        bd.RenderTarget[0].RenderTargetWriteMask = 0;
        g_device->CreateBlendState(&bd, &g_blend_state);
        const D3D11_CULL_MODE culls[3] = {D3D11_CULL_NONE, D3D11_CULL_FRONT, D3D11_CULL_BACK};
        for (int c = 0; c < 3; c++) {
            for (int w = 0; w < 2; w++) {
                D3D11_RASTERIZER_DESC r{};
                r.FillMode = D3D11_FILL_SOLID;
                r.CullMode = culls[c];
                r.FrontCounterClockwise = w;
                r.DepthClipEnable = TRUE;
                r.SlopeScaledDepthBias = 2.0f;
                r.DepthBiasClamp = 0.002f;
                g_device->CreateRasterizerState(&r, &g_raster[c][w]);
            }
        }
    }
    g_atlas_size = size;
    g_ready = true;
    logf("sunshadow: atlas %ux%u", size, size);
    return true;
}

// Orthographic rows for one cascade, camera-relative, with the centre snapped to whole texels of a grid fixed in
// the world, so edges do not crawl as the camera moves or turns.
void cascade_matrix(int index, V3 forward, V3 sun, const float camera[3], float tan_x, float tan_y, float near_z, float far_z, float reach, float behind,
                    UINT cascade_texels, float vp_rows[16], float uvz_rows[3][4]) {
    const float half_len = (far_z - near_z) * 0.5f;
    const float radius = std::sqrt(half_len * half_len + far_z * far_z * (tan_x * tan_x + tan_y * tan_y));
    // The game's own view basis has cross(right, up) == forward, so triangle winding only survives if the light's does too.
    const V3 light_forward = sun * -1.0f;
    V3 reference = std::fabs(sun.y) < 0.95f ? V3{0, 1, 0} : V3{1, 0, 0};
    V3 right = norm(cross(reference, light_forward));
    V3 up = norm(cross(light_forward, right));

    V3 center = forward * (near_z + half_len);
    const float texel = 2.0f * radius / cascade_texels;
    const V3 cam{camera[0], camera[1], camera[2]};
    // World-fixed coordinate of the centre along each light axis, rounded to the texel grid.
    const float cx = dot(center + cam, right), cy = dot(center + cam, up);
    center = center + right * (std::round(cx / texel) * texel - cx) + up * (std::round(cy / texel) * texel - cy);

    const float tx = -dot(right, center), ty = -dot(up, center), tz = -dot(light_forward, center);
    // Depth 0 sits `reach` toward the sun from the cascade centre, and anything farther up is clipped. The game's sun
    // was never meant to be blocked by the cliffs and distant areas around a map, only by what stands close by.
    const float zs = 1.0f / (reach + behind);
    // Rows as the game's vertex shaders read them: clip.x = dot(c0, float4(p, 1)), and so on. w stays 1, so c3 is (0,0,0,1).
    const float xs = 1.0f / radius;
    const float rows[4][4] = {
        {right.x * xs, right.y * xs, right.z * xs, tx * xs},
        {up.x * xs, up.y * xs, up.z * xs, ty * xs},
        {light_forward.x * zs, light_forward.y * zs, light_forward.z * zs, (tz + reach) * zs},
        {0, 0, 0, 1},
    };
    memcpy(vp_rows, rows, sizeof(rows));
    // The same transform into atlas coordinates, where this cascade is one quadrant.
    const float ox = (index & 1) ? 0.5f : 0.0f, oy = (index & 2) ? 0.5f : 0.0f;
    for (int c = 0; c < 4; c++) {
        uvz_rows[0][c] = rows[0][c] * 0.25f + (c == 3 ? 0.25f + ox : 0.0f);
        uvz_rows[1][c] = rows[1][c] * -0.25f + (c == 3 ? 0.25f + oy : 0.0f);
        uvz_rows[2][c] = rows[2][c];
    }
}

}  // namespace

void sunshadow_init(ID3D11Device* device) {
    g_device = device;
    sunshadow_read_settings();
}

void sunshadow_read_settings() {
    g_enabled = cfg_bool("sunshadow.enabled", true);
    g_far[0] = cfg_float("sunshadow.far0", 12.0f);
    g_far[1] = cfg_float("sunshadow.far1", 40.0f);
    g_far[2] = cfg_float("sunshadow.far2", 120.0f);
    g_far[3] = cfg_float("sunshadow.far3", 350.0f);
}

bool sunshadow_enabled() {
    return g_enabled;
}

void caster_list_release(CasterList& list) {
    auto drop = [](auto* p) { if (p) p->Release(); };
    for (CasterDraw& d : list.draws) {
        drop(d.layout);
        drop(d.vb[0]);
        drop(d.vb[1]);
        drop(d.ib);
        drop(d.vs);
        drop(d.vs_b0);
        drop(d.vs_b2);
        drop(d.ps);
        drop(d.ps_t0);
        drop(d.ps_s0);
        drop(d.ps_b1);
    }
    list.draws.clear();
    list.vs_constants.clear();
}

void sunshadow_render(ID3D11DeviceContext* ctx, const ViewConstants& view, const float sun_travel[3], const float camera[3]) {
    // The atlas is 2 x 2 cascades, and 16384 is as large as a texture gets.
    const UINT cascade_size = static_cast<UINT>(std::clamp(cfg_int("sunshadow.cascade_size", 4096), 256, 8192));
    if (!view.valid || !create_resources(cascade_size * 2)) return;
    const float* m = view.vp;
    const float p00 = std::sqrt(m[0] * m[0] + m[1] * m[1] + m[2] * m[2]);
    const float p11 = std::sqrt(m[4] * m[4] + m[5] * m[5] + m[6] * m[6]);
    V3 forward = norm({m[12], m[13], m[14]});
    V3 sun = norm({-sun_travel[0], -sun_travel[1], -sun_travel[2]});
    if (p00 <= 0 || p11 <= 0) return;

    SunShadowConstants constants{};
    float vp[kCascades][16];
    const float reach = cfg_float("sunshadow.caster_reach", 60.0f);
    const float behind = cfg_float("sunshadow.behind", 800.0f);
    float near_z = 0.05f;
    for (UINT i = 0; i < kCascades; i++) {
        cascade_matrix(static_cast<int>(i), forward, sun, camera, 1.0f / p00, 1.0f / p11, near_z, g_far[i], reach, behind, cascade_size, vp[i], reinterpret_cast<float(*)[4]>(constants.uvz[i * 3]));
        constants.split[i] = g_far[i];
        near_z = g_far[i];
    }
    constants.param[0] = cfg_float("sunshadow.normal_offset", 2.5f);
    constants.param[1] = cfg_float("sunshadow.filter_radius", 2.5f);
    constants.param[2] = 1.0f / (cascade_size * 2);

    {
        StateBackup backup(ctx);
        unbind_all(ctx);
        ctx->ClearDepthStencilView(g_dsv, D3D11_CLEAR_DEPTH, 1.0f, 0);
        ctx->OMSetRenderTargets(0, nullptr, g_dsv);
        ctx->OMSetDepthStencilState(g_depth_state, 0);
        const float factor[4] = {};
        ctx->OMSetBlendState(g_blend_state, factor, 0xffffffff);
        ctx->GSSetShader(nullptr, nullptr, 0);

        // From the store, culled per cascade, so what casts no longer depends on what the camera sees. The largest draws go
        // first; those under a few of a cascade's texels and those past its share are left out, so a big stored area costs
        // fine detail in the distance, not frame time.
        const UINT share = static_cast<UINT>(std::max(cfg_int("sunshadow.draws_per_cascade", 400), 16));
        const float floor_texels = cfg_float("sunshadow.min_caster_texels", 2.0f);
        for (UINT i = 0; i < kCascades; i++) {
            D3D11_VIEWPORT vport{(i & 1) ? static_cast<float>(cascade_size) : 0.0f, (i & 2) ? static_cast<float>(cascade_size) : 0.0f,
                                 static_cast<float>(cascade_size), static_cast<float>(cascade_size), 0.0f, 1.0f};
            ctx->RSSetViewports(1, &vport);
            const float world_per_clip = 1.0f / std::sqrt(vp[i][0] * vp[i][0] + vp[i][1] * vp[i][1] + vp[i][2] * vp[i][2]);
            CasterReplay replay;
            replay.rows = vp[i];
            replay.min_radius = floor_texels * 2.0f * world_per_clip / static_cast<float>(cascade_size);
            replay.budget = share;
            replay.sun_only = true;
            replay.raster = g_raster;
            casters_replay(ctx, replay);
        }
    }
    if (trace_tracking()) trace_snapshot_resource(ctx, g_atlas, "sunatlas");

    std::lock_guard<std::mutex> lock(g_constants_lock);
    g_constants = constants;
}

ID3D11ShaderResourceView* sunshadow_srv() {
    return g_srv;
}

SunShadowConstants sunshadow_constants() {
    std::lock_guard<std::mutex> lock(g_constants_lock);
    return g_constants;
}
