// Settings in dsrle.ini, read on every run so they tune live.
//
//   reflect.enabled = 1           handled at reflect_read_settings
//   reflect.strength = 1          0 turns the surfaces off and leaves the frame as it was; above 1 overshoots toward a pure trace
//   reflect.gain = 1              on the reflectance; 1 is energy conserving
//   reflect.rough_full = 0.4      surfaces at or under this roughness get the full effect
//   reflect.rough_cut = 0.7       and from this one up it is gone, and the pixel is not written
//   reflect.min_reflectance = 0.004  no ray for a surface that sends back less than this
//   reflect.max_distance = 400    metres a ray may travel
//   reflect.steps = 64            pyramid steps per ray
//   reflect.thickness = 0.25      metres a ray may be behind a surface and still count as hitting it
//   reflect.thickness_scale = 0.02  the same, per metre of the surface's view distance
//   reflect.self_plane = 0.3      a hit must lie this far along the ray's rise off its own plane; keeps grazing floors off themselves
//   reflect.edge_fade = 0.08      share of the screen over which hits fade out at its edges
//   reflect.far_start = 300       metres of view distance where the effect starts to fade
//   reflect.far_end = 700         and where it is gone
//   reflect.sky_z = 1500          view distance past which a surface counts as sky
//   reflect.sky_hits = 1          how much a ray that ends on visible sky takes that sky rather than the probe; 0 keeps the probe
//   reflect.toward_camera = 0.7   confidence lost by rays heading straight back at the camera
//   reflect.lobe = 0.65           width of the ray's random spread against the GGX lobe
//   reflect.cone = 0.5            width of the blurred scene fetch against the GGX lobe
//   reflect.max_radiance = 64     limit on any one traced light, unexposed scene units
//   reflect.spatial_radius = 12   half-resolution cells, at most
//   reflect.history = 0.92        share kept from the reprojected history; 0 turns it off
//   reflect.history_clip = 1.5    standard deviations of this frame's neighbourhood the history may stray
//   reflect.debug = 0             1 traced light, 2 confidence, 3 roughness, 4 reflectance, 5 what a hit takes out (RT3)
//   reflect.water = 1             strength of the screen-space reflection in the water shaders; 0 leaves them vanilla
//   reflect.water_steps = 32
//   reflect.water_thickness = 1.5
//   reflect.water_distance = 400
//   reflect.water_edge = 0.1
//   reflect.water_cube = 1        where the planar reflection was not rendered, fall back on the area's cube instead of its near-black clear colour
#include "reflect.h"
#include "common.h"
#include "config.h"
#include "hlsl.h"
#include "pack.h"
#include "shaderdb.h"
#include "state.h"
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstddef>
#include <cstring>
#include <mutex>
#include <shared_mutex>
#include <unordered_map>

namespace {

constexpr DXGI_FORMAT kSurfaceFormat = DXGI_FORMAT_R16G16B16A16_UNORM;
constexpr DXGI_FORMAT kProbeFormat = DXGI_FORMAT_R11G11B10_FLOAT;
constexpr UINT kHizLevels = 7;
constexpr UINT kPyramidLevels = 5;
constexpr UINT kComputeSrvs = 6;
constexpr UINT kComputeUavs = 3;
constexpr UINT kPixelSrvs = 7;

// Must match the cbuffer in reflect.hlsl; its first six rows are the DsrleReflect cbuffer in dsrle_features.fxh.
struct Constants {
    float row_x[4], row_y[4], row_w[4];
    float z_a, z_b, cells_x, cells_y;
    float water_strength, water_steps, water_thickness, water_distance;
    float water_edge, sky_depth, water_cube, top_level;

    float prev_x[4], prev_y[4], prev_w[4];
    float camera_delta[3], history_valid;
    float right[3], p00;
    float up[3], p11;
    float forward[3], jx;
    float jy, frame, max_distance, near_w;
    float size[2], inv_size[2];
    int32_t half_size[2];
    int32_t pattern[2];
    int32_t steps;
    float thickness, thickness_scale, self_plane;
    float rough_full, rough_cut, min_reflectance, edge;
    float far_start, far_k, lobe, cone;
    float max_radiance, sky_hits, toward_k, spatial_radius;
    float history, clip, strength, gain;
    float debug, prev_jx, prev_jy, unused2;
};
static_assert(sizeof(Constants) % 16 == 0);
static_assert(offsetof(Constants, prev_x) == 6 * 16);

ID3D11Device* g_device;
std::atomic<bool> g_on{false};

// Surface targets. Made by the recording thread that first draws opaque forward; the main thread's list never gets there.
std::mutex g_targets_lock;
UINT g_width, g_height;
ID3D11Texture2D* g_surface_tex;
ID3D11RenderTargetView* g_surface_rtv;
ID3D11ShaderResourceView* g_surface_srv;
ID3D11Texture2D* g_probe_tex;
ID3D11RenderTargetView* g_probe_rtv;
ID3D11ShaderResourceView* g_probe_srv;

// The frame's scene targets, as opaque forward bound them.
ID3D11RenderTargetView* g_scene_rtv;
ID3D11Resource* g_scene_res;
ID3D11ShaderResourceView* g_mask_srv;
ID3D11Resource* g_mask_res;

// The pass runs once between presents, and the game finishes recording a frame before it presents it.
std::atomic<bool> g_ran{false};
std::atomic<ID3D11Resource*> g_scene_res_atomic{nullptr};
// The opaque list's rows, under g_targets_lock. The prepass may not have reached the anchor when the copy list records, and
// then the anchor's rows are last frame's; these are this frame's for certain if the stamp says so.
ViewConstants g_opaque_view;
uint32_t g_opaque_view_frame = 0xffffffffu;

// Blend variants of the game's opaque blend states. The game's state is kept alive, since its address is the key.
struct Variants {
    ID3D11BlendState* with = nullptr;
    ID3D11BlendState* without = nullptr;
    bool opaque = false;
};
std::mutex g_blend_lock;
std::unordered_map<ID3D11BlendState*, Variants> g_blends;

// Replacement blobs that declare SV_Target2.
std::shared_mutex g_writes_lock;
std::unordered_map<uint64_t, bool> g_writes;

// The pass's own resources, used only by the single run per frame.
ID3D11Buffer* g_cb;
ID3D11SamplerState* g_linear_clamp;
ID3D11BlendState* g_opaque_blend;
ID3D11DepthStencilState* g_no_depth;
ID3D11RasterizerState* g_raster;

struct PassTargets {
    UINT width = 0, height = 0;
    ID3D11Texture2D* scene_copy = nullptr;
    ID3D11ShaderResourceView* scene_copy_srv = nullptr;
    ID3D11Texture2D* hiz = nullptr;
    ID3D11ShaderResourceView* hiz_srv = nullptr;
    ID3D11ShaderResourceView* hiz_level_srv[kHizLevels] = {};
    ID3D11UnorderedAccessView* hiz_level_uav[kHizLevels] = {};
    ID3D11Texture2D* pyramid = nullptr;
    ID3D11ShaderResourceView* pyramid_srv = nullptr;
    ID3D11ShaderResourceView* pyramid_level_srv[kPyramidLevels] = {};
    ID3D11UnorderedAccessView* pyramid_level_uav[kPyramidLevels] = {};
    ID3D11Texture2D* ray = nullptr;
    ID3D11ShaderResourceView* ray_srv = nullptr;
    ID3D11UnorderedAccessView* ray_uav = nullptr;
    ID3D11Texture2D* len = nullptr;
    ID3D11ShaderResourceView* len_srv = nullptr;
    ID3D11UnorderedAccessView* len_uav = nullptr;
    ID3D11Texture2D* spatial = nullptr;
    ID3D11ShaderResourceView* spatial_srv = nullptr;
    ID3D11UnorderedAccessView* spatial_uav = nullptr;
    ID3D11Texture2D* gbuf[2] = {};
    ID3D11ShaderResourceView* gbuf_srv[2] = {};
    ID3D11UnorderedAccessView* gbuf_uav[2] = {};
    ID3D11Texture2D* history[2] = {};
    ID3D11ShaderResourceView* history_srv[2] = {};
    ID3D11UnorderedAccessView* history_uav[2] = {};
};
PassTargets g_pass;
bool g_pass_failed;
UINT g_pass_failed_w, g_pass_failed_h;

std::mutex g_camera_lock;
float g_camera_prev[3], g_camera_prev2[3];
int g_camera_known;  // how many of those two hold a published value
std::atomic<uint32_t> g_frame{0};

// Written and read only inside the single run per frame.
float g_prev_vp[16];
float g_prev_jx, g_prev_jy;
bool g_prev_valid;
uint32_t g_prev_frame;
uint32_t g_index;

template <class T>
void drop(T*& p) {
    if (p) p->Release();
    p = nullptr;
}

float dot3(const float* a, const float* b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }

void release_pass(PassTargets& p) {
    drop(p.scene_copy);
    drop(p.scene_copy_srv);
    drop(p.hiz);
    drop(p.hiz_srv);
    for (auto*& v : p.hiz_level_srv) drop(v);
    for (auto*& v : p.hiz_level_uav) drop(v);
    drop(p.pyramid);
    drop(p.pyramid_srv);
    for (auto*& v : p.pyramid_level_srv) drop(v);
    for (auto*& v : p.pyramid_level_uav) drop(v);
    drop(p.ray);
    drop(p.ray_srv);
    drop(p.ray_uav);
    drop(p.len);
    drop(p.len_srv);
    drop(p.len_uav);
    drop(p.spatial);
    drop(p.spatial_srv);
    drop(p.spatial_uav);
    for (int i = 0; i < 2; i++) {
        drop(p.gbuf[i]);
        drop(p.gbuf_srv[i]);
        drop(p.gbuf_uav[i]);
        drop(p.history[i]);
        drop(p.history_srv[i]);
        drop(p.history_uav[i]);
    }
    p.width = p.height = 0;
}

bool make_compute_target(UINT w, UINT h, DXGI_FORMAT format, ID3D11Texture2D** tex, ID3D11ShaderResourceView** srv, ID3D11UnorderedAccessView** uav) {
    D3D11_TEXTURE2D_DESC d{};
    d.Width = w;
    d.Height = h;
    d.MipLevels = 1;
    d.ArraySize = 1;
    d.Format = format;
    d.SampleDesc.Count = 1;
    d.Usage = D3D11_USAGE_DEFAULT;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, tex))) return false;
    if (FAILED(g_device->CreateShaderResourceView(*tex, nullptr, srv))) return false;
    if (FAILED(g_device->CreateUnorderedAccessView(*tex, nullptr, uav))) return false;
    return true;
}

bool make_chain(UINT w, UINT h, UINT levels, DXGI_FORMAT format, ID3D11Texture2D** tex, ID3D11ShaderResourceView** srv, ID3D11ShaderResourceView** level_srv, ID3D11UnorderedAccessView** level_uav) {
    D3D11_TEXTURE2D_DESC d{};
    d.Width = w;
    d.Height = h;
    d.MipLevels = levels;
    d.ArraySize = 1;
    d.Format = format;
    d.SampleDesc.Count = 1;
    d.Usage = D3D11_USAGE_DEFAULT;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, tex))) return false;
    if (FAILED(g_device->CreateShaderResourceView(*tex, nullptr, srv))) return false;
    for (UINT m = 0; m < levels; m++) {
        D3D11_SHADER_RESOURCE_VIEW_DESC s{};
        s.Format = format;
        s.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
        s.Texture2D.MostDetailedMip = m;
        s.Texture2D.MipLevels = 1;
        if (FAILED(g_device->CreateShaderResourceView(*tex, &s, &level_srv[m]))) return false;
        D3D11_UNORDERED_ACCESS_VIEW_DESC u{};
        u.Format = format;
        u.ViewDimension = D3D11_UAV_DIMENSION_TEXTURE2D;
        u.Texture2D.MipSlice = m;
        if (FAILED(g_device->CreateUnorderedAccessView(*tex, &u, &level_uav[m]))) return false;
    }
    return true;
}

bool create_pass_inner(PassTargets& p, UINT w, UINT h, DXGI_FORMAT scene_format) {
    D3D11_TEXTURE2D_DESC d{};
    d.Width = w;
    d.Height = h;
    d.MipLevels = 1;
    d.ArraySize = 1;
    d.Format = scene_format;
    d.SampleDesc.Count = 1;
    d.Usage = D3D11_USAGE_DEFAULT;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, &p.scene_copy))) return false;
    if (FAILED(g_device->CreateShaderResourceView(p.scene_copy, nullptr, &p.scene_copy_srv))) return false;

    const UINT hw = (w + 1) / 2, hh = (h + 1) / 2;
    const UINT top = 1u << (kHizLevels - 1);
    const UINT pad_w = (hw + top - 1) / top * top, pad_h = (hh + top - 1) / top * top;
    if (!make_chain(pad_w, pad_h, kHizLevels, DXGI_FORMAT_R32_FLOAT, &p.hiz, &p.hiz_srv, p.hiz_level_srv, p.hiz_level_uav)) return false;
    if (!make_chain(hw, hh, kPyramidLevels, DXGI_FORMAT_R16G16B16A16_FLOAT, &p.pyramid, &p.pyramid_srv, p.pyramid_level_srv, p.pyramid_level_uav)) return false;
    if (!make_compute_target(hw, hh, DXGI_FORMAT_R16G16B16A16_FLOAT, &p.ray, &p.ray_srv, &p.ray_uav)) return false;
    if (!make_compute_target(hw, hh, DXGI_FORMAT_R16_FLOAT, &p.len, &p.len_srv, &p.len_uav)) return false;
    if (!make_compute_target(hw, hh, DXGI_FORMAT_R16G16B16A16_FLOAT, &p.spatial, &p.spatial_srv, &p.spatial_uav)) return false;
    for (int i = 0; i < 2; i++) {
        if (!make_compute_target(hw, hh, DXGI_FORMAT_R16G16B16A16_FLOAT, &p.gbuf[i], &p.gbuf_srv[i], &p.gbuf_uav[i])) return false;
        if (!make_compute_target(hw, hh, DXGI_FORMAT_R16G16B16A16_FLOAT, &p.history[i], &p.history_srv[i], &p.history_uav[i])) return false;
    }
    p.width = w;
    p.height = h;
    return true;
}

bool ensure_pass_targets(UINT w, UINT h, DXGI_FORMAT scene_format) {
    if (g_pass.width == w && g_pass.height == h) return true;
    if (g_pass_failed && g_pass_failed_w == w && g_pass_failed_h == h) return false;
    release_pass(g_pass);
    g_prev_valid = false;
    const bool ok = create_pass_inner(g_pass, w, h, scene_format);
    if (!ok) {
        release_pass(g_pass);
        g_pass_failed = true;
        g_pass_failed_w = w;
        g_pass_failed_h = h;
    }
    logf("reflect: pass targets %ux%u %s", w, h, ok ? "ready" : "FAILED");
    return ok;
}

// RT2 and RT3, at the size of the scene target.
bool ensure_surface_targets(UINT w, UINT h) {
    std::lock_guard<std::mutex> lock(g_targets_lock);
    if (g_width == w && g_height == h) return g_surface_rtv && g_probe_rtv;
    drop(g_surface_tex);
    drop(g_surface_rtv);
    drop(g_surface_srv);
    drop(g_probe_tex);
    drop(g_probe_rtv);
    drop(g_probe_srv);
    g_width = w;
    g_height = h;
    D3D11_TEXTURE2D_DESC d{};
    d.Width = w;
    d.Height = h;
    d.MipLevels = 1;
    d.ArraySize = 1;
    d.Format = kSurfaceFormat;
    d.SampleDesc.Count = 1;
    d.Usage = D3D11_USAGE_DEFAULT;
    d.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
    bool ok = SUCCEEDED(g_device->CreateTexture2D(&d, nullptr, &g_surface_tex)) && SUCCEEDED(g_device->CreateRenderTargetView(g_surface_tex, nullptr, &g_surface_rtv)) &&
              SUCCEEDED(g_device->CreateShaderResourceView(g_surface_tex, nullptr, &g_surface_srv));
    d.Format = kProbeFormat;
    ok = ok && SUCCEEDED(g_device->CreateTexture2D(&d, nullptr, &g_probe_tex)) && SUCCEEDED(g_device->CreateRenderTargetView(g_probe_tex, nullptr, &g_probe_rtv)) &&
         SUCCEEDED(g_device->CreateShaderResourceView(g_probe_tex, nullptr, &g_probe_srv));
    if (!ok) {
        drop(g_surface_rtv);
        drop(g_probe_rtv);
    }
    logf("reflect: surface targets %ux%u %s", w, h, ok ? "ready" : "FAILED");
    return ok;
}

Variants variants_of(ID3D11BlendState* game) {
    std::lock_guard<std::mutex> lock(g_blend_lock);
    auto it = g_blends.find(game);
    if (it != g_blends.end()) return it->second;
    Variants v;
    D3D11_BLEND_DESC d{};
    game->GetDesc(&d);
    const auto& rt0 = d.RenderTarget[0];
    v.opaque = d.IndependentBlendEnable && !rt0.BlendEnable &&
               rt0.RenderTargetWriteMask == (D3D11_COLOR_WRITE_ENABLE_RED | D3D11_COLOR_WRITE_ENABLE_GREEN | D3D11_COLOR_WRITE_ENABLE_BLUE);
    if (v.opaque) {
        for (int with = 0; with < 2; with++) {
            D3D11_BLEND_DESC e = d;
            for (int i = 2; i < 4; i++) {
                auto& rt = e.RenderTarget[i];
                rt.BlendEnable = FALSE;
                rt.SrcBlend = D3D11_BLEND_ONE;
                rt.DestBlend = D3D11_BLEND_ZERO;
                rt.BlendOp = D3D11_BLEND_OP_ADD;
                rt.SrcBlendAlpha = D3D11_BLEND_ONE;
                rt.DestBlendAlpha = D3D11_BLEND_ZERO;
                rt.BlendOpAlpha = D3D11_BLEND_OP_ADD;
                rt.RenderTargetWriteMask = with ? static_cast<UINT8>(D3D11_COLOR_WRITE_ENABLE_ALL) : static_cast<UINT8>(0);
            }
            g_device->CreateBlendState(&e, with ? &v.with : &v.without);
        }
        if (!v.with || !v.without) v.opaque = false;
    }
    game->AddRef();
    g_blends[game] = v;
    return v;
}

// Whether a blob's output signature has SV_Target2. The layout is the plain DXBC container.
bool blob_writes_target2(const void* code, size_t size) {
    const auto* b = static_cast<const uint8_t*>(code);
    if (size < 32 || memcmp(b, "DXBC", 4) != 0) return false;
    uint32_t chunks = 0;
    memcpy(&chunks, b + 28, 4);
    if (32 + static_cast<uint64_t>(chunks) * 4 > size) return false;
    for (uint32_t i = 0; i < chunks; i++) {
        uint32_t at = 0;
        memcpy(&at, b + 32 + i * 4, 4);
        if (static_cast<uint64_t>(at) + 8 > size) return false;
        if (memcmp(b + at, "OSGN", 4) != 0) continue;
        uint32_t bytes = 0, count = 0;
        memcpy(&bytes, b + at + 4, 4);
        if (bytes < 8 || static_cast<uint64_t>(at) + 8 + bytes > size) return false;
        const uint8_t* body = b + at + 8;
        memcpy(&count, body, 4);
        if (8 + static_cast<uint64_t>(count) * 24 > bytes) return false;
        for (uint32_t e = 0; e < count; e++) {
            uint32_t reg = 0;
            memcpy(&reg, body + 8 + e * 24 + 16, 4);
            if (reg == 2) return true;
        }
        return false;
    }
    return false;
}

bool shader_writes_surface(const ShaderInfo* info) {
    if (!info) return false;
    {
        std::shared_lock lock(g_writes_lock);
        auto it = g_writes.find(info->hash_lo);
        if (it != g_writes.end()) return it->second;
    }
    size_t size = 0;
    const void* code = pack_find(info->hash_lo, info->hash_hi, &size);
    const bool writes = code && blob_writes_target2(code, size);
    std::unique_lock lock(g_writes_lock);
    g_writes[info->hash_lo] = writes;
    return writes;
}

bool scene_format(ID3D11RenderTargetView* view) {
    D3D11_RENDER_TARGET_VIEW_DESC d{};
    view->GetDesc(&d);
    return d.Format == DXGI_FORMAT_R11G11B10_FLOAT;
}

void put_game_targets_back(ReflectTrack& t, ID3D11DeviceContext* ctx) {
    ctx->OMSetRenderTargets(2, t.targets, t.depth);
    t.bound = false;
}

void put_game_blend_back(ReflectTrack& t, ID3D11DeviceContext* ctx) {
    if (!t.applied) return;
    ctx->OMSetBlendState(t.blend, t.factor, t.sample_mask);
    t.applied = nullptr;
}

// The opaque list's first draw: an empty slate, and a note of what the game bound.
void begin_surfaces(ReflectTrack& t, ID3D11DeviceContext* ctx) {
    const FLOAT zero[4] = {};
    ctx->ClearRenderTargetView(g_surface_rtv, zero);
    ctx->ClearRenderTargetView(g_probe_rtv, zero);

    ID3D11Resource* res = nullptr;
    ID3D11Resource* mask_res = nullptr;
    t.targets[0]->GetResource(&res);
    t.targets[1]->GetResource(&mask_res);
    std::lock_guard<std::mutex> lock(g_targets_lock);
    if (g_scene_rtv != t.targets[0]) {
        t.targets[0]->AddRef();
        drop(g_scene_rtv);
        g_scene_rtv = t.targets[0];
    }
    if (g_scene_res != res) {
        drop(g_scene_res);
        g_scene_res = res;
        res = nullptr;
    }
    if (g_mask_res != mask_res) {
        drop(g_mask_srv);
        D3D11_RENDER_TARGET_VIEW_DESC rd{};
        t.targets[1]->GetDesc(&rd);
        D3D11_SHADER_RESOURCE_VIEW_DESC sd{};
        sd.Format = rd.Format;
        sd.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
        sd.Texture2D.MipLevels = 1;
        g_device->CreateShaderResourceView(mask_res, &sd, &g_mask_srv);
        drop(g_mask_res);
        g_mask_res = mask_res;
        mask_res = nullptr;
    }
    drop(res);
    drop(mask_res);
    g_scene_res_atomic.store(g_scene_res, std::memory_order_relaxed);
}

// Everything the shaders need about the camera, from the jittered rows. The jitter is a multiple of the forward axis
// in c0 and c1; taking it out leaves exactly P00 * right and P11 * up.
bool fill_camera(Constants& c, const ViewConstants& view) {
    const float* m = view.vp;
    const float w_len = std::sqrt(dot3(m + 12, m + 12));
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
    c.jx = jx;
    c.jy = jy;
    memcpy(c.row_x, m + 0, sizeof(c.row_x));
    memcpy(c.row_y, m + 4, sizeof(c.row_y));
    memcpy(c.row_w, m + 12, sizeof(c.row_w));
    // z_ndc = z_a + z_b / w, as gtao.cpp derives it.
    c.z_a = std::sqrt(dot3(m + 8, m + 8)) / w_len;
    c.z_b = m[11] / w_len;
    return c.z_b != 0.0f;
}

void clear_compute(ID3D11DeviceContext* ctx) {
    ID3D11ShaderResourceView* no_srv[kComputeSrvs] = {};
    ID3D11UnorderedAccessView* no_uav[kComputeUavs] = {};
    ctx->CSSetUnorderedAccessViews(0, kComputeUavs, no_uav, nullptr);
    ctx->CSSetShaderResources(0, kComputeSrvs, no_srv);
}

}  // namespace

void reflect_init(ID3D11Device* device) {
    g_device = device;
    D3D11_BUFFER_DESC b{};
    b.ByteWidth = sizeof(Constants);
    b.Usage = D3D11_USAGE_DYNAMIC;
    b.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    b.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    device->CreateBuffer(&b, nullptr, &g_cb);
    D3D11_SAMPLER_DESC s{};
    s.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    s.AddressU = s.AddressV = s.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    s.ComparisonFunc = D3D11_COMPARISON_NEVER;
    s.MaxLOD = D3D11_FLOAT32_MAX;
    device->CreateSamplerState(&s, &g_linear_clamp);
    D3D11_BLEND_DESC bl{};
    bl.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_RED | D3D11_COLOR_WRITE_ENABLE_GREEN | D3D11_COLOR_WRITE_ENABLE_BLUE;
    device->CreateBlendState(&bl, &g_opaque_blend);
    D3D11_DEPTH_STENCIL_DESC d{};
    d.DepthEnable = FALSE;
    d.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ZERO;
    d.DepthFunc = D3D11_COMPARISON_ALWAYS;
    device->CreateDepthStencilState(&d, &g_no_depth);
    D3D11_RASTERIZER_DESC r{};
    r.FillMode = D3D11_FILL_SOLID;
    r.CullMode = D3D11_CULL_NONE;
    r.DepthClipEnable = TRUE;
    device->CreateRasterizerState(&r, &g_raster);
}

void reflect_read_settings(bool allowed) {
    g_on.store(allowed && g_device && cfg_bool("reflect.enabled", true), std::memory_order_relaxed);
}

void reflect_on_present(const float camera[3], bool fresh) {
    g_frame.fetch_add(1, std::memory_order_relaxed);
    g_ran.store(false, std::memory_order_relaxed);
    std::lock_guard<std::mutex> lock(g_camera_lock);
    if (!fresh) {
        g_camera_known = 0;  // no TAA, so no camera to go on: the history stays off
        return;
    }
    memcpy(g_camera_prev2, g_camera_prev, sizeof(g_camera_prev));
    memcpy(g_camera_prev, camera, sizeof(g_camera_prev));
    g_camera_known = std::min(g_camera_known + 1, 2);
}

void reflect_track_reset(ReflectTrack& t) {
    const auto list = t.list;
    t = ReflectTrack{};
    t.list = list;
}

void reflect_on_targets(ReflectTrack& t, ID3D11DeviceContext* ctx, UINT count, ID3D11RenderTargetView* const* views, ID3D11DepthStencilView* depth) {
    put_game_blend_back(t, ctx);
    t.bound = false;
    t.armed = false;
    t.scene_targets = false;
    if (count < 2 || !views || !views[0] || !views[1] || !depth) return;
    for (UINT i = 2; i < count; i++) {
        if (views[i]) return;
    }
    if (!scene_format(views[0])) return;
    ID3D11Resource* res = nullptr;
    depth->GetResource(&res);
    const bool scene_depth = res && res == engine_scene_depth_resource();
    if (res) res->Release();
    if (!scene_depth) return;
    t.targets[0] = views[0];
    t.targets[1] = views[1];
    t.depth = depth;
    t.scene_targets = true;
    t.armed = g_on.load(std::memory_order_relaxed);
}

void reflect_on_blend(ReflectTrack& t, ID3D11BlendState* state, const FLOAT factor[4], UINT sample_mask) {
    t.blend = state;
    if (factor) {
        memcpy(t.factor, factor, sizeof(t.factor));
    } else {
        t.factor[0] = t.factor[1] = t.factor[2] = t.factor[3] = 1.0f;
    }
    t.sample_mask = sample_mask;
    t.applied = nullptr;
}

void reflect_before_draw(ReflectTrack& t, ID3D11DeviceContext* ctx, const ViewConstants& view) {
    if (t.blend != t.memo_blend) {
        t.memo_blend = t.blend;
        const Variants v = t.blend ? variants_of(t.blend) : Variants{};
        t.memo_with = v.with;
        t.memo_without = v.without;
        t.memo_opaque = v.opaque;
    }
    if (!t.memo_opaque || !g_on.load(std::memory_order_relaxed)) {
        // Blended forward or the like, on the same two targets.
        if (t.bound) put_game_targets_back(t, ctx);
        put_game_blend_back(t, ctx);
        return;
    }
    if (t.list.view_fresh && !t.list.view_noted) {
        std::lock_guard<std::mutex> lock(g_targets_lock);
        g_opaque_view = view;
        g_opaque_view_frame = g_frame.load(std::memory_order_relaxed);
        t.list.view_noted = true;
    }
    if (!t.bound) {
        ID3D11Resource* res = nullptr;
        t.targets[0]->GetResource(&res);
        D3D11_TEXTURE2D_DESC d{};
        static_cast<ID3D11Texture2D*>(res)->GetDesc(&d);
        res->Release();
        if (!ensure_surface_targets(d.Width, d.Height)) return;
        if (!t.list.begun) {
            begin_surfaces(t, ctx);
            t.list.begun = true;
        }
        ID3D11RenderTargetView* all[4] = {t.targets[0], t.targets[1], g_surface_rtv, g_probe_rtv};
        ctx->OMSetRenderTargets(4, all, t.depth);
        t.bound = true;
    }
    if (t.ps != t.memo_ps) {
        t.memo_ps = t.ps;
        t.memo_writes = shader_writes_surface(t.ps);
    }
    ID3D11BlendState* want = t.memo_writes ? t.memo_with : t.memo_without;
    if (t.applied != want) {
        ctx->OMSetBlendState(want, t.factor, t.sample_mask);
        t.applied = want;
    }
}

bool reflect_wants_copy(ID3D11Resource* src) {
    return src && g_on.load(std::memory_order_relaxed) && !g_ran.load(std::memory_order_relaxed) && src == g_scene_res_atomic.load(std::memory_order_relaxed);
}

void reflect_on_scene_copy(ID3D11DeviceContext* ctx, ID3D11ShaderResourceView* depth, const ViewConstants& anchor_view, const float camera[3], bool fresh) {
    if (g_ran.exchange(true, std::memory_order_relaxed)) return;
    if (!g_on.load(std::memory_order_relaxed) || !g_device || !ctx || !depth || !g_cb) return;

    ViewConstants view = anchor_view;
    ID3D11RenderTargetView* scene_rtv = nullptr;
    ID3D11Resource* scene_res = nullptr;
    ID3D11ShaderResourceView* mask = nullptr;
    ID3D11ShaderResourceView* surface = nullptr;
    ID3D11ShaderResourceView* probe = nullptr;
    ID3D11RenderTargetView* surface_rtv = nullptr;
    ID3D11RenderTargetView* probe_rtv = nullptr;
    {
        std::lock_guard<std::mutex> lock(g_targets_lock);
        if (g_opaque_view_frame == g_frame.load(std::memory_order_relaxed)) view = g_opaque_view;
        if (g_scene_rtv && g_scene_res && g_mask_srv && g_surface_srv && g_probe_srv && g_surface_rtv && g_probe_rtv) {
            scene_rtv = g_scene_rtv;
            scene_rtv->AddRef();
            scene_res = g_scene_res;
            scene_res->AddRef();
            mask = g_mask_srv;
            mask->AddRef();
            surface = g_surface_srv;
            surface->AddRef();
            probe = g_probe_srv;
            probe->AddRef();
            surface_rtv = g_surface_rtv;
            surface_rtv->AddRef();
            probe_rtv = g_probe_rtv;
            probe_rtv->AddRef();
        }
    }
    const auto release_inputs = [&] {
        drop(scene_rtv);
        drop(scene_res);
        drop(mask);
        drop(surface);
        drop(probe);
        drop(surface_rtv);
        drop(probe_rtv);
    };
    if (!scene_rtv || !view.valid) {
        release_inputs();
        return;
    }

    const float strength = std::max(cfg_float("reflect.strength", 1.0f), 0.0f);
    const float water = std::max(cfg_float("reflect.water", 1.0f), 0.0f);
    const float debug = cfg_float("reflect.debug", 0.0f);
    const bool surfaces = strength > 0.0f || debug > 0.0f;
    if (!surfaces && water <= 0.0f) {
        g_prev_valid = false;
        release_inputs();
        return;
    }

    auto* cs_hiz_first = hlsl_cs("reflect.hlsl", "hiz_first");
    auto* cs_hiz_down = hlsl_cs("reflect.hlsl", "hiz_down");
    auto* cs_color_first = hlsl_cs("reflect.hlsl", "color_first");
    auto* cs_color_down = hlsl_cs("reflect.hlsl", "color_down");
    auto* cs_trace = hlsl_cs("reflect.hlsl", "trace_main");
    auto* cs_spatial = hlsl_cs("reflect.hlsl", "spatial_main");
    auto* cs_temporal = hlsl_cs("reflect.hlsl", "temporal_main");
    auto* vs = hlsl_vs("reflect.hlsl", "fullscreen_vs");
    auto* ps = hlsl_ps("reflect.hlsl", "composite_ps");
    if (!cs_hiz_first || !cs_hiz_down || !cs_color_first || !cs_color_down || !cs_trace || !cs_spatial || !cs_temporal || !vs || !ps) {
        release_inputs();
        return;
    }

    D3D11_TEXTURE2D_DESC scene_desc{};
    {
        D3D11_RESOURCE_DIMENSION dim = D3D11_RESOURCE_DIMENSION_UNKNOWN;
        scene_res->GetType(&dim);
        if (dim == D3D11_RESOURCE_DIMENSION_TEXTURE2D) static_cast<ID3D11Texture2D*>(scene_res)->GetDesc(&scene_desc);
    }
    if (scene_desc.Width == 0 || scene_desc.Height == 0 || !ensure_pass_targets(scene_desc.Width, scene_desc.Height, scene_desc.Format)) {
        release_inputs();
        return;
    }

    Constants c{};
    if (!fill_camera(c, view)) {
        release_inputs();
        return;
    }
    const UINT hw = (scene_desc.Width + 1) / 2, hh = (scene_desc.Height + 1) / 2;
    c.cells_x = scene_desc.Width * 0.5f;
    c.cells_y = scene_desc.Height * 0.5f;
    c.size[0] = static_cast<float>(scene_desc.Width);
    c.size[1] = static_cast<float>(scene_desc.Height);
    c.inv_size[0] = 1.0f / c.size[0];
    c.inv_size[1] = 1.0f / c.size[1];
    c.half_size[0] = static_cast<int32_t>(hw);
    c.half_size[1] = static_cast<int32_t>(hh);
    // Counted per run, so the sequence goes on through frames where the pass did not run.
    c.frame = static_cast<float>(g_index % 4096);
    static const int32_t kPattern[4][2] = {{0, 0}, {1, 1}, {1, 0}, {0, 1}};
    c.pattern[0] = kPattern[g_index & 3][0];
    c.pattern[1] = kPattern[g_index & 3][1];
    c.top_level = static_cast<float>(kHizLevels - 1);

    c.water_strength = water;
    c.water_steps = std::max(cfg_float("reflect.water_steps", 32.0f), 1.0f);
    c.water_thickness = std::max(cfg_float("reflect.water_thickness", 1.5f), 0.0f);
    c.water_distance = std::max(cfg_float("reflect.water_distance", 400.0f), 1.0f);
    c.water_edge = std::max(cfg_float("reflect.water_edge", 0.1f), 1e-3f);
    c.water_cube = cfg_float("reflect.water_cube", 1.0f);
    const float sky_z = std::max(cfg_float("reflect.sky_z", 1500.0f), 1.0f);
    c.sky_depth = c.z_a + c.z_b / sky_z;

    c.steps = std::clamp(cfg_int("reflect.steps", 64), 1, 512);
    c.max_distance = std::max(cfg_float("reflect.max_distance", 400.0f), 1.0f);
    c.near_w = 0.1f;
    c.thickness = std::max(cfg_float("reflect.thickness", 0.25f), 0.0f);
    c.thickness_scale = std::max(cfg_float("reflect.thickness_scale", 0.02f), 0.0f);
    c.self_plane = std::clamp(cfg_float("reflect.self_plane", 0.3f), 0.0f, 1.0f);
    c.rough_cut = std::clamp(cfg_float("reflect.rough_cut", 0.7f), 0.0f, 1.01f);
    c.rough_full = std::clamp(cfg_float("reflect.rough_full", 0.4f), 0.0f, c.rough_cut);
    c.min_reflectance = std::max(cfg_float("reflect.min_reflectance", 0.004f), 0.0f);
    c.edge = std::max(cfg_float("reflect.edge_fade", 0.08f), 1e-3f);
    c.far_start = cfg_float("reflect.far_start", 300.0f);
    c.far_k = 1.0f / std::max(cfg_float("reflect.far_end", 700.0f) - c.far_start, 1e-3f);
    c.lobe = std::clamp(cfg_float("reflect.lobe", 0.65f), 0.0f, 1.0f);
    c.cone = std::max(cfg_float("reflect.cone", 0.5f), 0.0f);
    c.max_radiance = std::max(cfg_float("reflect.max_radiance", 64.0f), 1e-3f);
    c.sky_hits = std::clamp(cfg_float("reflect.sky_hits", 1.0f), 0.0f, 1.0f);
    c.toward_k = std::clamp(cfg_float("reflect.toward_camera", 0.7f), 0.0f, 1.0f);
    c.spatial_radius = std::max(cfg_float("reflect.spatial_radius", 12.0f), 0.0f);
    c.history = std::clamp(cfg_float("reflect.history", 0.92f), 0.0f, 0.99f);
    c.clip = std::max(cfg_float("reflect.history_clip", 1.5f), 0.0f);
    c.strength = strength;
    c.gain = std::max(cfg_float("reflect.gain", 1.0f), 0.0f);
    c.debug = debug;
    c.prev_jx = g_prev_jx;
    c.prev_jy = g_prev_jy;

    // History needs last frame's rows and the camera's move since. The move comes from the TAA constants: those of this
    // frame if the main thread has got that far already, otherwise last frame's move again, which only a sudden change
    // of speed can tell apart, and the neighbourhood clamp covers that for a frame.
    float now[3], prev[3], prev2[3];
    int known;
    {
        std::lock_guard<std::mutex> lock(g_camera_lock);
        memcpy(now, camera, sizeof(now));
        memcpy(prev, g_camera_prev, sizeof(prev));
        memcpy(prev2, g_camera_prev2, sizeof(prev2));
        known = g_camera_known;
    }
    // Normally one present since the last run. Much more than that and the history is stale.
    const uint32_t frame = g_frame.load(std::memory_order_relaxed);
    const bool consecutive = g_prev_valid && frame - g_prev_frame <= 3u;
    bool have_move = false;
    if (fresh && known >= 1) {
        for (int i = 0; i < 3; i++) c.camera_delta[i] = now[i] - prev[i];
        have_move = true;
    } else if (known >= 2) {
        for (int i = 0; i < 3; i++) c.camera_delta[i] = prev[i] - prev2[i];
        have_move = true;
    }
    c.history_valid = (consecutive && have_move && c.history > 0.0f) ? 1.0f : 0.0f;
    if (g_prev_valid) {
        memcpy(c.prev_x, g_prev_vp + 0, sizeof(c.prev_x));
        memcpy(c.prev_y, g_prev_vp + 4, sizeof(c.prev_y));
        memcpy(c.prev_w, g_prev_vp + 12, sizeof(c.prev_w));
    }

    D3D11_MAPPED_SUBRESOURCE mapped;
    if (FAILED(ctx->Map(g_cb, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) {
        release_inputs();
        return;
    }
    memcpy(mapped.pData, &c, sizeof(c));
    ctx->Unmap(g_cb, 0);

    const UINT cur = g_index & 1, old = cur ^ 1;
    {
        StateBackup backup(ctx);
        // Takes RT0, RT1 and the depth buffer off the output stage first, or binding them as inputs would silently fail.
        unbind_all(ctx);
        ctx->CSSetConstantBuffers(0, 1, &g_cb);

        ctx->CSSetShader(cs_hiz_first, nullptr, 0);
        ID3D11ShaderResourceView* hiz_in[2] = {depth, mask};
        ctx->CSSetShaderResources(0, 2, hiz_in);
        ctx->CSSetUnorderedAccessViews(0, 1, &g_pass.hiz_level_uav[0], nullptr);
        {
            D3D11_TEXTURE2D_DESC hd{};
            g_pass.hiz->GetDesc(&hd);
            ctx->Dispatch((hd.Width + 7) / 8, (hd.Height + 7) / 8, 1);
            clear_compute(ctx);
            ctx->CSSetShader(cs_hiz_down, nullptr, 0);
            for (UINT m = 1; m < kHizLevels; m++) {
                ctx->CSSetShaderResources(0, 1, &g_pass.hiz_level_srv[m - 1]);
                ctx->CSSetUnorderedAccessViews(0, 1, &g_pass.hiz_level_uav[m], nullptr);
                ctx->Dispatch(((hd.Width >> m) + 7) / 8, ((hd.Height >> m) + 7) / 8, 1);
                clear_compute(ctx);
            }
        }

        if (surfaces) {
            ctx->CopyResource(g_pass.scene_copy, scene_res);
            ctx->CSSetSamplers(0, 1, &g_linear_clamp);

            ctx->CSSetShader(cs_color_first, nullptr, 0);
            ctx->CSSetShaderResources(0, 1, &g_pass.scene_copy_srv);
            ctx->CSSetUnorderedAccessViews(0, 1, &g_pass.pyramid_level_uav[0], nullptr);
            ctx->Dispatch((hw + 7) / 8, (hh + 7) / 8, 1);
            clear_compute(ctx);
            ctx->CSSetShader(cs_color_down, nullptr, 0);
            for (UINT m = 1; m < kPyramidLevels; m++) {
                ctx->CSSetShaderResources(0, 1, &g_pass.pyramid_level_srv[m - 1]);
                ctx->CSSetUnorderedAccessViews(0, 1, &g_pass.pyramid_level_uav[m], nullptr);
                ctx->Dispatch((std::max(hw >> m, 1u) + 7) / 8, (std::max(hh >> m, 1u) + 7) / 8, 1);
                clear_compute(ctx);
            }

            ctx->CSSetShader(cs_trace, nullptr, 0);
            ID3D11ShaderResourceView* trace_in[kComputeSrvs] = {depth, surface, g_pass.scene_copy_srv, g_pass.pyramid_srv, g_pass.hiz_srv, mask};
            ctx->CSSetShaderResources(0, kComputeSrvs, trace_in);
            ID3D11UnorderedAccessView* trace_out[kComputeUavs] = {g_pass.ray_uav, g_pass.gbuf_uav[cur], g_pass.len_uav};
            ctx->CSSetUnorderedAccessViews(0, kComputeUavs, trace_out, nullptr);
            ctx->Dispatch((hw + 7) / 8, (hh + 7) / 8, 1);
            clear_compute(ctx);

            ctx->CSSetShader(cs_spatial, nullptr, 0);
            ID3D11ShaderResourceView* spatial_in[3] = {g_pass.ray_srv, g_pass.gbuf_srv[cur], g_pass.len_srv};
            ctx->CSSetShaderResources(0, 3, spatial_in);
            ctx->CSSetUnorderedAccessViews(0, 1, &g_pass.spatial_uav, nullptr);
            ctx->Dispatch((hw + 7) / 8, (hh + 7) / 8, 1);
            clear_compute(ctx);

            ctx->CSSetShader(cs_temporal, nullptr, 0);
            ID3D11ShaderResourceView* temporal_in[5] = {g_pass.spatial_srv, g_pass.gbuf_srv[cur], g_pass.len_srv, g_pass.history_srv[old], g_pass.gbuf_srv[old]};
            ctx->CSSetShaderResources(0, 5, temporal_in);
            ctx->CSSetUnorderedAccessViews(0, 1, &g_pass.history_uav[cur], nullptr);
            ctx->Dispatch((hw + 7) / 8, (hh + 7) / 8, 1);
            clear_compute(ctx);

            D3D11_VIEWPORT viewport{};
            viewport.Width = static_cast<float>(scene_desc.Width);
            viewport.Height = static_cast<float>(scene_desc.Height);
            viewport.MaxDepth = 1.0f;
            ctx->IASetInputLayout(nullptr);
            ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
            ctx->VSSetShader(vs, nullptr, 0);
            ctx->GSSetShader(nullptr, nullptr, 0);
            ctx->PSSetShader(ps, nullptr, 0);
            ctx->PSSetConstantBuffers(0, 1, &g_cb);
            ID3D11ShaderResourceView* inputs[kPixelSrvs] = {g_pass.history_srv[cur], g_pass.gbuf_srv[cur], surface, probe, depth, mask, g_pass.scene_copy_srv};
            ctx->PSSetShaderResources(0, kPixelSrvs, inputs);
            ctx->RSSetState(g_raster);
            ctx->RSSetViewports(1, &viewport);
            ctx->OMSetBlendState(g_opaque_blend, nullptr, 0xffffffffu);
            ctx->OMSetDepthStencilState(g_no_depth, 0);
            ctx->OMSetRenderTargets(1, &scene_rtv, nullptr);
            ctx->Draw(3, 0);

            // The backup rebinds the game's depth buffer as an output before it restores the inputs.
            ID3D11ShaderResourceView* no_inputs[kPixelSrvs] = {};
            ctx->PSSetShaderResources(0, kPixelSrvs, no_inputs);
        }
    }

    // For the water shaders further down this list. Context state ends with the list, so no other list sees these.
    if (water > 0.0f) {
        ctx->PSSetShaderResources(24, 1, &g_pass.hiz_srv);
        ctx->PSSetConstantBuffers(5, 1, &g_cb);
    }

    // Spent. A frame that copies the scene without having drawn opaque forward must find nothing here, not this frame's.
    {
        const FLOAT zero[4] = {};
        ctx->ClearRenderTargetView(surface_rtv, zero);
        ctx->ClearRenderTargetView(probe_rtv, zero);
    }

    if (surfaces) {
        memcpy(g_prev_vp, view.vp, sizeof(g_prev_vp));
        g_prev_jx = c.jx;
        g_prev_jy = c.jy;
        g_prev_valid = true;
        g_prev_frame = frame;
        g_index++;
    } else {
        g_prev_valid = false;
    }
    release_inputs();
}
