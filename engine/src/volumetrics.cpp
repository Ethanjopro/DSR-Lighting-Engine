// Settings in dsrle.ini, read on every call so they tune live. The defaults are untuned starting points: they come
// from DrawParam light rows and one Firelink exposure, not from looking at the result.
//
//   volumetrics.enabled = 1            handled by the caller
//   volumetrics.strength = 1           master for everything below; 0 leaves the scene exactly as it was
//   volumetrics.near = 0.5             start of the volume, metres
//   volumetrics.far = 200              end of the volume; the sky stops accumulating here
//   volumetrics.light_density = 0.03   scatter per metre per unit of light colour; 0 turns the glow off
//   volumetrics.light_phase = 0.2      forward lobe of the glow, -0.9..0.9; 0 is even in all directions
//   volumetrics.light_list = 0         which of the game's two cluster lists to walk, 0 or 1
//   volumetrics.radius_scale = 1       glow radius against the light's radius, up to 1 (the cells only reach that far)
//   volumetrics.camera_fade_start = 2  lights this close to the camera give no glow
//   volumetrics.camera_fade_end = 7    lights from here on give full glow; keeps the player's own light from an aura
//   volumetrics.glow_core = 0.8        metres: the glow around a light falls off as an inverse square outside this core
//   volumetrics.max_glow = 0.3         soft limit on the total, unexposed scene units
//   volumetrics.haze_density = 0.0012  haze per metre; 0 turns it off
//   volumetrics.haze_sun = 0.01        how much of the sun scatter colour goes into the haze
//   volumetrics.haze_phase = 0.4       forward lobe of that sun part
//   volumetrics.haze_height = 40       metres above the camera at which haze thins to 1/e; 0 = uniform
//   volumetrics.haze_fog_cap = 0.1     luminance limit on the fog colour; a few rows hold very bright values
//   volumetrics.haze_smooth = 0.05     per-frame blend toward the sampled fog row; the sample can change row per frame
//   volumetrics.extinction = 0.0002    per metre, scaled with the haze's height; transmittance stays at 0.9 or above
//   volumetrics.jitter = 1             per-frame lattice offset, in froxels
//   volumetrics.dither = 1             per-pixel lookup noise, in froxels
//   volumetrics.sun = 0.0006           sun in-scatter per metre, shadowed by the world atlas: the light shafts
//   volumetrics.sun_phase = 0.5        forward lobe of the sun in-scatter
#include "volumetrics.h"
#include "common.h"
#include "config.h"
#include "hlsl.h"
#include "state.h"
#include "sunshadow.h"
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstring>
#include <mutex>

namespace {

constexpr UINT kGridX = 240, kGridY = 135, kGridZ = 96;
constexpr UINT kInjectGroupX = 8, kInjectGroupY = 8, kInjectGroupZ = 4;
constexpr UINT kIntegrateGroup = 8;
constexpr UINT kComputeSrvs = 7;  // inject and integrate use up to t6
constexpr UINT kComputeUavs = 2;

// Must match the cbuffer in volumetrics.hlsl.
struct Constants {
    float right[3], p00;
    float up[3], p11;
    float forward[3], z_a;
    float z_b, z_near, z_far, slice_scale;
    float grid[3], frame;
    float inv_grid[3], dither;
    float jitter[3], strength;
    float inv_size[2], cluster_near, cluster_scale;
    float light_density, light_g, radius_scale, max_glow;
    float camera_fade_start, camera_fade_k, extinction, haze_density;
    float haze_color[3], haze_falloff;
    float haze_sun[3], haze_g;
    float haze_dir[3], far_fade_k;
    float sun_color[3], sun_density;
    float sun_dir[3], sun_g;
    float shadow_start[4];
    float shadow_end, shadow_fade_k;
    uint32_t light_list;
    float glow_core;
    float shadow_rows[16][4];
    float shadow_rects[4][4];
};
static_assert(sizeof(Constants) == 37 * 16);  // the registers the shader declares

// The per-frame constants are a sample of one object's fog row, and which row comes first can change from frame to
// frame (4 distinct rows at both traced bonfires), so the haze follows them slowly.
struct HazeRow {
    float fog[3];
    float density;
    float sun[3];
    float toward_sun[3];
};

ID3D11Device* g_device;
std::atomic<uint32_t> g_frame;

std::once_flag g_create_once;
bool g_ready;
ID3D11Buffer* g_cb;
ID3D11Texture3D* g_tex[2];  // [0] scatter, [1] integrated
ID3D11ShaderResourceView* g_srv[2];
ID3D11UnorderedAccessView* g_uav[2];
ID3D11SamplerState* g_linear_clamp;
ID3D11SamplerState* g_shadow_compare;
ID3D11BlendState* g_blend;
ID3D11DepthStencilState* g_no_depth;
ID3D11RasterizerState* g_raster;

std::mutex g_lock;  // the list being recorded and the frame boundary can be on different threads
ID3D11ShaderResourceView* g_atlas;
HazeRow g_row;
bool g_row_seeded;

void release_resources() {
    auto drop = [](auto*& p) { if (p) p->Release(); p = nullptr; };
    drop(g_cb);
    for (int i = 0; i < 2; i++) {
        drop(g_tex[i]);
        drop(g_srv[i]);
        drop(g_uav[i]);
    }
    drop(g_linear_clamp);
    drop(g_shadow_compare);
    drop(g_blend);
    drop(g_no_depth);
    drop(g_raster);
}

bool create_resources_inner() {
    D3D11_BUFFER_DESC b{};
    b.ByteWidth = sizeof(Constants);
    b.Usage = D3D11_USAGE_DYNAMIC;
    b.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    b.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    if (FAILED(g_device->CreateBuffer(&b, nullptr, &g_cb))) return false;

    // Two textures: feature level 11_0 has no typed UAV load of RGBA16F, so the integration cannot work in place.
    D3D11_TEXTURE3D_DESC t{};
    t.Width = kGridX;
    t.Height = kGridY;
    t.Depth = kGridZ;
    t.MipLevels = 1;
    t.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
    t.Usage = D3D11_USAGE_DEFAULT;
    t.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS;
    for (int i = 0; i < 2; i++) {
        if (FAILED(g_device->CreateTexture3D(&t, nullptr, &g_tex[i]))) return false;
        if (FAILED(g_device->CreateShaderResourceView(g_tex[i], nullptr, &g_srv[i]))) return false;
        if (FAILED(g_device->CreateUnorderedAccessView(g_tex[i], nullptr, &g_uav[i]))) return false;
    }

    D3D11_SAMPLER_DESC s{};
    s.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    s.AddressU = s.AddressV = s.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    s.ComparisonFunc = D3D11_COMPARISON_NEVER;
    s.MaxLOD = D3D11_FLOAT32_MAX;
    if (FAILED(g_device->CreateSamplerState(&s, &g_linear_clamp))) return false;

    // The game's atlas convention: 1 from the compare means occluded. A border depth of 1 never occludes.
    s.Filter = D3D11_FILTER_COMPARISON_MIN_MAG_LINEAR_MIP_POINT;
    s.AddressU = s.AddressV = s.AddressW = D3D11_TEXTURE_ADDRESS_BORDER;
    s.ComparisonFunc = D3D11_COMPARISON_GREATER;
    s.BorderColor[0] = s.BorderColor[1] = s.BorderColor[2] = s.BorderColor[3] = 1.0f;
    if (FAILED(g_device->CreateSamplerState(&s, &g_shadow_compare))) return false;

    D3D11_BLEND_DESC bl{};
    bl.RenderTarget[0].BlendEnable = TRUE;
    bl.RenderTarget[0].SrcBlend = D3D11_BLEND_ONE;
    bl.RenderTarget[0].DestBlend = D3D11_BLEND_SRC_ALPHA;
    bl.RenderTarget[0].BlendOp = D3D11_BLEND_OP_ADD;
    bl.RenderTarget[0].SrcBlendAlpha = D3D11_BLEND_ZERO;
    bl.RenderTarget[0].DestBlendAlpha = D3D11_BLEND_ONE;
    bl.RenderTarget[0].BlendOpAlpha = D3D11_BLEND_OP_ADD;
    bl.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_RED | D3D11_COLOR_WRITE_ENABLE_GREEN | D3D11_COLOR_WRITE_ENABLE_BLUE;
    if (FAILED(g_device->CreateBlendState(&bl, &g_blend))) return false;

    D3D11_DEPTH_STENCIL_DESC d{};
    d.DepthEnable = FALSE;
    d.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ZERO;
    d.DepthFunc = D3D11_COMPARISON_ALWAYS;
    d.StencilEnable = FALSE;
    if (FAILED(g_device->CreateDepthStencilState(&d, &g_no_depth))) return false;

    D3D11_RASTERIZER_DESC r{};
    r.FillMode = D3D11_FILL_SOLID;
    r.CullMode = D3D11_CULL_NONE;
    r.DepthClipEnable = TRUE;
    r.ScissorEnable = FALSE;
    if (FAILED(g_device->CreateRasterizerState(&r, &g_raster))) return false;
    return true;
}

// Runs once, on whichever recording thread gets here first; the device is free-threaded.
void create_resources() {
    g_ready = create_resources_inner();
    if (!g_ready) release_resources();
    logf("volumetrics: %ux%ux%u froxels %s", kGridX, kGridY, kGridZ, g_ready ? "ready" : "FAILED");
}

float dot3(const float* a, const float* b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
float clamp01(float x) { return std::clamp(x, 0.0f, 1.0f); }

float halton(uint32_t index, uint32_t base) {
    float f = 1.0f, r = 0.0f;
    while (index) {
        f /= static_cast<float>(base);
        r += f * static_cast<float>(index % base);
        index /= base;
    }
    return r;
}

// Camera basis and projection from the camera-relative view-projection. c0 and c1 also carry the TAA jitter as a
// multiple of the forward axis; taking that out leaves exactly P00 * right and P11 * up.
bool fill_camera(Constants& c, const ViewConstants& view) {
    const float* m = view.vp;
    float w_len = std::sqrt(dot3(m + 12, m + 12));
    if (w_len <= 0.0f) return false;
    const float f[3] = {m[12] / w_len, m[13] / w_len, m[14] / w_len};
    const float jx = dot3(m + 0, f), jy = dot3(m + 4, f);
    const float rx[3] = {m[0] - f[0] * jx, m[1] - f[1] * jx, m[2] - f[2] * jx};
    const float uy[3] = {m[4] - f[0] * jy, m[5] - f[1] * jy, m[6] - f[2] * jy};
    c.p00 = std::sqrt(dot3(rx, rx));
    c.p11 = std::sqrt(dot3(uy, uy));
    if (c.p00 <= 0.0f || c.p11 <= 0.0f) return false;
    for (int i = 0; i < 3; i++) {
        c.right[i] = rx[i] / c.p00;
        c.up[i] = uy[i] / c.p11;
        c.forward[i] = f[i];
    }
    // z_ndc = z_a + z_b / view_z, as gtao.cpp derives it.
    c.z_a = std::sqrt(dot3(m + 8, m + 8)) / w_len;
    c.z_b = m[11] / w_len;
    return c.z_b != 0.0f;
}

HazeRow sample_row(const ForwardConstants& forward, float fog_cap) {
    static const float kLuma[3] = {0.2126f, 0.7152f, 0.0722f};
    HazeRow row{};
    // c12 rgb is 2.2-gamma encoded, c12 a the row's maximum fog density. c20 is the sun's direction of travel.
    const float* fog = forward.regs[12];
    for (int i = 0; i < 3; i++) row.fog[i] = std::pow(std::fabs(fog[i]), 2.2f);
    const float lum = dot3(row.fog, kLuma);
    if (lum > fog_cap && lum > 0.0f) {
        for (float& v : row.fog) v *= fog_cap / lum;
    }
    row.density = clamp01(fog[3]);
    // The game adds c19 to gamma-encoded colour, so its hue needs decoding. Its size only means something alongside
    // vanilla's scatter factor and stays as it is: a plain decode would make Parish's row about 25x brighter.
    const float* sun = forward.regs[19];
    const float peak = std::max({sun[0], sun[1], sun[2], 1e-6f});
    for (int i = 0; i < 3; i++) row.sun[i] = peak * std::pow(std::max(sun[i], 0.0f) / peak, 2.2f) * clamp01(sun[3]);
    const float* travel = forward.regs[20];
    for (int i = 0; i < 3; i++) row.toward_sun[i] = -travel[i];
    return row;
}

HazeRow smoothed_row(const HazeRow& target, float blend) {
    std::lock_guard<std::mutex> lock(g_lock);
    if (!g_row_seeded) {
        g_row = target;
        g_row_seeded = true;
    } else {
        auto follow = [blend](float& v, float t) { v += (t - v) * blend; };
        for (int i = 0; i < 3; i++) {
            follow(g_row.fog[i], target.fog[i]);
            follow(g_row.sun[i], target.sun[i]);
            follow(g_row.toward_sun[i], target.toward_sun[i]);
        }
        follow(g_row.density, target.density);
    }
    HazeRow out = g_row;
    const float len = std::sqrt(dot3(out.toward_sun, out.toward_sun));
    if (len > 0.0f) {
        for (float& v : out.toward_sun) v /= len;
    }
    return out;
}

void clear_compute(ID3D11DeviceContext* ctx) {
    ID3D11ShaderResourceView* no_srv[kComputeSrvs] = {};
    ID3D11UnorderedAccessView* no_uav[kComputeUavs] = {};
    ctx->CSSetUnorderedAccessViews(0, kComputeUavs, no_uav, nullptr);
    ctx->CSSetShaderResources(0, kComputeSrvs, no_srv);
}

}  // namespace

void volumetrics_init(ID3D11Device* device) {
    g_device = device;
}

void volumetrics_on_present() {
    g_frame.fetch_add(1, std::memory_order_relaxed);
}

void volumetrics_note_shadow_atlas(ID3D11ShaderResourceView* srv) {
    if (!srv) return;
    D3D11_SHADER_RESOURCE_VIEW_DESC d{};
    srv->GetDesc(&d);
    if (d.Format != DXGI_FORMAT_R24_UNORM_X8_TYPELESS) return;
    std::lock_guard<std::mutex> lock(g_lock);
    if (srv == g_atlas) return;
    srv->AddRef();
    if (g_atlas) g_atlas->Release();
    g_atlas = srv;
}

void volumetrics_run(ID3D11DeviceContext* ctx, ID3D11RenderTargetView* scene_rtv, ID3D11ShaderResourceView* depth_srv,
                     ID3D11ShaderResourceView* const lights[3], const ViewConstants& view, const ForwardConstants& forward) {
    if (!g_device || !ctx || !scene_rtv || !depth_srv || !lights) return;
    if (!lights[0] || !lights[1] || !lights[2]) return;
    if (!view.valid || !forward.valid) return;

    auto* cs_inject = hlsl_cs("volumetrics.hlsl", "inject_main");
    auto* cs_integrate = hlsl_cs("volumetrics.hlsl", "integrate_main");
    auto* vs = hlsl_vs("volumetrics.hlsl", "fullscreen_vs");
    auto* ps = hlsl_ps("volumetrics.hlsl", "apply_ps");
    if (!cs_inject || !cs_integrate || !vs || !ps) return;

    std::call_once(g_create_once, create_resources);
    if (!g_ready) return;

    // Asked for each frame: nothing but the viewport and the dither scale depend on it, so a resize needs no rebuild.
    ID3D11Resource* rt = nullptr;
    scene_rtv->GetResource(&rt);
    if (!rt) return;
    D3D11_RESOURCE_DIMENSION dimension = D3D11_RESOURCE_DIMENSION_UNKNOWN;
    rt->GetType(&dimension);
    D3D11_TEXTURE2D_DESC rt_desc{};
    if (dimension == D3D11_RESOURCE_DIMENSION_TEXTURE2D) static_cast<ID3D11Texture2D*>(rt)->GetDesc(&rt_desc);
    rt->Release();
    if (rt_desc.Width == 0 || rt_desc.Height == 0) return;

    Constants c{};
    if (!fill_camera(c, view)) return;

    // The cluster grid is defined by the game's near plane and z scale, not by this volume's range.
    c.cluster_near = forward.regs[86][0];
    c.cluster_scale = forward.regs[87][0];
    if (c.cluster_near <= 0.0f || c.cluster_scale <= 0.0f) return;

    const uint32_t frame = g_frame.load(std::memory_order_relaxed);
    c.strength = cfg_float("volumetrics.strength", 1.0f);
    if (c.strength <= 0.0f) return;
    c.z_near = std::max(cfg_float("volumetrics.near", 0.5f), 0.05f);
    c.z_far = std::max(cfg_float("volumetrics.far", 200.0f), c.z_near * 2.0f);
    c.slice_scale = static_cast<float>(kGridZ) / std::log(c.z_far / c.z_near);
    c.far_fade_k = 1.0f / (0.25f * c.z_far);
    c.grid[0] = static_cast<float>(kGridX);
    c.grid[1] = static_cast<float>(kGridY);
    c.grid[2] = static_cast<float>(kGridZ);
    for (int i = 0; i < 3; i++) c.inv_grid[i] = 1.0f / c.grid[i];
    c.inv_size[0] = 1.0f / static_cast<float>(rt_desc.Width);
    c.inv_size[1] = 1.0f / static_cast<float>(rt_desc.Height);
    c.frame = static_cast<float>(frame % 1024);

    // The grid moves within its froxels from frame to frame, and the lookup takes the same offset back out.
    const float jitter = clamp01(cfg_float("volumetrics.jitter", 1.0f));
    const uint32_t index = frame % 16 + 1;
    c.jitter[0] = (halton(index, 2) - 0.5f) * jitter;
    c.jitter[1] = (halton(index, 3) - 0.5f) * jitter;
    c.jitter[2] = (halton(index, 5) - 0.5f) * jitter;
    c.dither = std::max(cfg_float("volumetrics.dither", 1.0f), 0.0f);

    c.light_density = cfg_float("volumetrics.light_density", 0.03f);
    c.light_g = std::clamp(cfg_float("volumetrics.light_phase", 0.2f), -0.9f, 0.9f);
    c.light_list = static_cast<uint32_t>(std::clamp(cfg_int("volumetrics.light_list", 0), 0, 1));
    // The cell lists only reach as far as the light's own radius, so a larger glow would be cut off at cell edges.
    c.radius_scale = std::clamp(cfg_float("volumetrics.radius_scale", 1.0f), 0.05f, 1.0f);
    c.glow_core = std::max(cfg_float("volumetrics.glow_core", 0.8f), 1e-3f);
    c.max_glow = std::max(cfg_float("volumetrics.max_glow", 0.3f), 1e-3f);
    c.camera_fade_start = cfg_float("volumetrics.camera_fade_start", 2.0f);
    c.camera_fade_k = 1.0f / std::max(cfg_float("volumetrics.camera_fade_end", 7.0f) - c.camera_fade_start, 1e-3f);

    c.extinction = std::max(cfg_float("volumetrics.extinction", 0.0002f), 0.0f);
    const HazeRow row = smoothed_row(sample_row(forward, cfg_float("volumetrics.haze_fog_cap", 0.1f)), clamp01(cfg_float("volumetrics.haze_smooth", 0.05f)));
    const float haze_sun = cfg_float("volumetrics.haze_sun", 0.01f);
    c.haze_density = cfg_float("volumetrics.haze_density", 0.0012f) * row.density;
    for (int i = 0; i < 3; i++) {
        c.haze_color[i] = row.fog[i];
        c.haze_sun[i] = row.sun[i] * haze_sun;
        c.haze_dir[i] = row.toward_sun[i];
    }
    c.haze_g = std::clamp(cfg_float("volumetrics.haze_phase", 0.4f), -0.9f, 0.9f);
    const float haze_height = cfg_float("volumetrics.haze_height", 40.0f);
    c.haze_falloff = haze_height > 0.0f ? 1.0f / haze_height : 0.0f;

    // Sun in-scatter, shadowed by the world atlas: where the air is lit and where a caster blocks it is what makes shafts.
    // The colour is the area's key light as the forward shaders see it. Walking into a place with no sun fades it out.
    const float sun = cfg_float("volumetrics.sun", 0.00008f);
    ID3D11ShaderResourceView* atlas = sunshadow_srv();
    if (atlas) atlas->AddRef();  // the exit paths below release it
    const SunShadowConstants shadow = sunshadow_constants();
    if (sun > 0.0f && atlas && shadow.split[3] > 0.0f && sunshadow_enabled()) {
        float key[3];
        engine_key_light(key);
        const float* travel = forward.regs[73];
        const float gain = cfg_float("lighting.sun_gain", 14.0f);
        const float len = std::sqrt(dot3(travel, travel));
        static float s_sun[3];
        for (int i = 0; i < 3; i++) {
            s_sun[i] += (key[i] * gain - s_sun[i]) * 0.05f;
            c.sun_color[i] = s_sun[i];
            c.sun_dir[i] = len > 0.0f ? -travel[i] / len : 0.0f;
        }
        c.sun_density = sun;
        c.sun_g = std::clamp(cfg_float("volumetrics.sun_phase", 0.5f), -0.9f, 0.9f);
        memcpy(c.shadow_start, shadow.split, sizeof(c.shadow_start));
        memcpy(c.shadow_rows, shadow.uvz, sizeof(shadow.uvz));
    }

    D3D11_MAPPED_SUBRESOURCE mapped;
    if (FAILED(ctx->Map(g_cb, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) {
        if (atlas) atlas->Release();
        return;
    }
    memcpy(mapped.pData, &c, sizeof(c));
    ctx->Unmap(g_cb, 0);

    {
        StateBackup backup(ctx);
        // Takes the depth buffer off the output stage first, or binding it as an input would silently fail.
        unbind_all(ctx);

        ctx->CSSetConstantBuffers(0, 1, &g_cb);

        ctx->CSSetShader(cs_inject, nullptr, 0);
        ctx->CSSetShaderResources(2, 3, lights);
        if (atlas) ctx->CSSetShaderResources(6, 1, &atlas);
        ctx->CSSetSamplers(1, 1, &g_shadow_compare);
        ctx->CSSetUnorderedAccessViews(0, 1, &g_uav[0], nullptr);
        ctx->Dispatch((kGridX + kInjectGroupX - 1) / kInjectGroupX, (kGridY + kInjectGroupY - 1) / kInjectGroupY, (kGridZ + kInjectGroupZ - 1) / kInjectGroupZ);
        clear_compute(ctx);

        ctx->CSSetShader(cs_integrate, nullptr, 0);
        ctx->CSSetShaderResources(5, 1, &g_srv[0]);
        ctx->CSSetUnorderedAccessViews(1, 1, &g_uav[1], nullptr);
        ctx->Dispatch((kGridX + kIntegrateGroup - 1) / kIntegrateGroup, (kGridY + kIntegrateGroup - 1) / kIntegrateGroup, 1);
        clear_compute(ctx);

        D3D11_VIEWPORT viewport{};
        viewport.Width = static_cast<float>(rt_desc.Width);
        viewport.Height = static_cast<float>(rt_desc.Height);
        viewport.MaxDepth = 1.0f;

        ctx->IASetInputLayout(nullptr);
        ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        ctx->VSSetShader(vs, nullptr, 0);
        ctx->GSSetShader(nullptr, nullptr, 0);
        ctx->PSSetShader(ps, nullptr, 0);
        ctx->PSSetConstantBuffers(0, 1, &g_cb);
        ID3D11ShaderResourceView* inputs[2] = {depth_srv, g_srv[1]};
        ctx->PSSetShaderResources(0, 2, inputs);
        ctx->PSSetSamplers(0, 1, &g_linear_clamp);
        ctx->RSSetState(g_raster);
        ctx->RSSetViewports(1, &viewport);
        ctx->OMSetBlendState(g_blend, nullptr, 0xffffffffu);
        ctx->OMSetDepthStencilState(g_no_depth, 0);
        ctx->OMSetRenderTargets(1, &scene_rtv, nullptr);
        ctx->Draw(3, 0);

        // The backup rebinds the game's depth buffer as an output before it restores the inputs.
        ID3D11ShaderResourceView* no_inputs[2] = {};
        ctx->PSSetShaderResources(0, 2, no_inputs);
    }
    if (atlas) atlas->Release();
}
