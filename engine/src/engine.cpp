#include "engine.h"
#include "bloom.h"
#include "casters.h"
#include "common.h"
#include "config.h"
#include "gtao.h"
#include "hdr.h"
#include "hlsl.h"
#include "pack.h"
#include "perf.h"
#include "reflect.h"
#include "pointshadow.h"
#include "sunshadow.h"
#include "ssgi.h"
#include "volumetrics.h"
#include <algorithm>
#include <atomic>
#include <cstddef>
#include <cstring>
#include <mutex>
#include <utility>

Role role_from_shader_name(const char* name) {
    struct Entry { const char* name; Role role; };
    // Entry names are exact archive names; a table row may list byte-identical aliases.
    static const Entry kEntries[] = {
        {"FRPG_Compute_SSAO_PrepareDepthBuffers1.cpo", Role::SsaoPrepare},
        {"FRPG_Compute_SSAO_Render1.cpo", Role::SsaoRender},
        {"FRPG_Compute_SSAO_BlurUpsampleBlendOut.cpo", Role::SsaoBlendOut},
        {"FRPG_Compute_SSAO_BlurUpsample.cpo", Role::SsaoBlurUpsample},
        {"FRPG_Fil_HDR_PBL_ColAdj.fpo", Role::Composite},
        {"FRPG_Fil_ResolveTAA.fpo", Role::ResolveTaa},
        {"FRPG_Fil_DepthCopy.fpo", Role::DepthCopy},
        {"FRPG_Dbg_PIN_D_PntNum.vpo", Role::NoCast},
        {"DbgPrim_Col.fpo", Role::NoCast},
    };
    for (const Entry& e : kEntries) {
        if (strstr(name, e.name)) return e.role;
    }
    if (strstr(name, "_Wind_") && strstr(name, ".vpo")) return Role::Foliage;
    if (strncmp(name, "FRPG_Menu_", 10) == 0 || strncmp(name, "DbgFont", 7) == 0) return Role::MenuUi;
    if (strstr(name, "Csd_HemEnv") && (strncmp(name, "FRPG_Phn_", 9) == 0 || strncmp(name, "FRPG_Gst_", 9) == 0)) return Role::ForwardLit;
    return Role::None;
}

namespace {

// Must match the DsrleEngine cbuffer in shaders/features/dsrle_features.fxh.
struct PackConstants {
    float strength[4];   // x AO on point lights, y contact shadow, z world shadow, w same on light-mapped draws
    float split[4];
    float uvz[12][4];
    float param[4];      // x normal offset in texels, y filter radius in texels, z 1/atlas size, w direct sun gain
    float fill[4];       // probe (x) and hemisphere (y) where the row has a sun, the same (z, w) where it has none
    float light[4];      // x point light gain, y and z baked gate on the sun: saturate(lum * y) ^ z
    float film[4];       // composite.hlsl: x shadow contrast, y highlight contrast, z exposure, w debug view
    float black[4];      // composite.hlsl: x black floor, y dark scene exposure
};
static_assert(sizeof(PackConstants) % 16 == 0);
static_assert(offsetof(PackConstants, film) == 17 * 16 && sizeof(PackConstants) == 19 * 16);  // composite.hlsl's DsrleEngine

ID3D11Device* g_device;
ID3D11Buffer* g_pack_cb;
bool g_enabled;
bool g_gtao;
bool g_composite;
bool g_pack;
bool g_hide_ui;
bool g_toggled_off;
bool g_toggle_key_down;
float g_strength[4];
float g_fill[4];
float g_light[4];
float g_film[4];
float g_black[4];
ID3D11Buffer* g_draw_kind[2];
float g_sun_gain;

std::atomic<bool> g_want_forward{true};
std::mutex g_forward_lock;
std::mutex g_key_lock;
std::atomic<float> g_key_peak{0.0f};
float g_key_next[3];
float g_key_current[3];
ForwardConstants g_forward;

std::atomic<bool> g_want_camera{true};
std::atomic<bool> g_taa_fresh{false};  // the TAA pass ran last frame; with anti-aliasing off nothing publishes the camera
std::mutex g_camera_lock;
float g_camera[3];

std::atomic<void*> g_scene_depth_resource{nullptr};

bool g_volumetrics;
bool g_ssgi;
std::atomic<bool> g_want_vol_inputs{true};
std::atomic<bool> g_vol_ran{false};
std::mutex g_vol_lock;
struct VolInputs {
    ID3D11RenderTargetView* rtv = nullptr;
    ID3D11ShaderResourceView* depth = nullptr;
    ID3D11ShaderResourceView* lights[3] = {};
    ViewConstants view;
};
VolInputs g_vol;

void read_settings() {
    g_enabled = cfg_bool("engine.enabled", true) && !g_toggled_off;
    g_gtao = g_enabled && cfg_bool("gtao.enabled", true);
    g_composite = g_enabled && cfg_bool("composite.enabled", true);
    g_pack = g_enabled && cfg_bool("lighting.enabled", true);
    g_hide_ui = cfg_bool("engine.hide_ui", false);
    g_volumetrics = g_enabled && cfg_bool("volumetrics.enabled", true);
    g_ssgi = g_pack && cfg_bool("ssgi.enabled", true);  // only the engine's forward shaders read it
    reflect_read_settings(g_pack);
    sunshadow_read_settings();
    casters_read_settings();
    pointshadow_read_settings();
    const bool sun = g_pack && sunshadow_enabled();
    g_strength[0] = cfg_float("lighting.ao_on_points", 1.0f);
    g_strength[1] = cfg_float("lighting.contact_shadow", 1.0f);
    g_strength[2] = sun ? cfg_float("sunshadow.world_strength", 0.3f) : 0.0f;
    g_strength[3] = sun ? cfg_float("sunshadow.lit_strength", 0.3f) : 0.0f;
    g_sun_gain = cfg_float("lighting.sun_gain", 14.0f);
    g_fill[0] = cfg_float("lighting.fill_probe_sun", 0.6f);
    g_fill[1] = cfg_float("lighting.fill_hemi_sun", 0.6f);
    g_fill[2] = cfg_float("lighting.fill_probe_dark", 1.0f);
    g_fill[3] = cfg_float("lighting.fill_hemi_dark", 1.0f);
    g_light[0] = cfg_float("lighting.point_gain", 1.6f);
    g_light[1] = cfg_float("lighting.baked_gate_gain", 8.0f);
    g_light[2] = cfg_float("lighting.baked_gate_power", 0.6f);
    g_light[3] = cfg_float("lighting.point_falloff", 0.18f);
    g_film[0] = std::clamp(cfg_float("composite.film_shadow", 1.12f), 0.01f, 4.0f);
    g_film[1] = std::clamp(cfg_float("composite.film_highlight", 1.12f), 0.01f, 4.0f);
    g_film[2] = cfg_float("composite.exposure", 1.0f);
    g_film[3] = cfg_float("composite.debug", 0.0f);
    g_black[0] = cfg_float("composite.black_floor", 0.03f);
    g_black[1] = std::max(cfg_float("composite.dark_exposure", 1.35f), 0.01f);
}

}  // namespace

void engine_init(ID3D11Device* device) {
    g_device = device;
    config_load();
    sunshadow_init(device);
    casters_init(device);
    pointshadow_init(device);
    read_settings();
    pack_load();
    hlsl_init(device);
    for (int i = 0; i < 2; i++) {
        const float kind[4] = {static_cast<float>(i), 0.0f, 0.0f, 0.0f};
        D3D11_BUFFER_DESC b{};
        b.ByteWidth = sizeof(kind);
        b.Usage = D3D11_USAGE_IMMUTABLE;
        b.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
        D3D11_SUBRESOURCE_DATA data{};
        data.pSysMem = kind;
        device->CreateBuffer(&b, &data, &g_draw_kind[i]);
    }
    gtao_init(device);
    bloom_init(device);
    volumetrics_init(device);
    hdr_init(device);
    ssgi_init(device);
    reflect_init(device);
    reflect_read_settings(g_pack);
    perf_init(device);
    D3D11_BUFFER_DESC b{};
    b.ByteWidth = sizeof(PackConstants);
    b.Usage = D3D11_USAGE_DEFAULT;
    b.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    static const PackConstants zero{};
    const D3D11_SUBRESOURCE_DATA init{&zero, 0, 0};
    device->CreateBuffer(&b, &init, &g_pack_cb);
    logf("engine: enabled=%d gtao=%d composite=%d lighting=%d", g_enabled, g_gtao, g_composite, g_pack);
}

// The comparison key: the whole engine flips on and off live, only while the game has focus.
static void poll_toggle_key() {
    const int key = cfg_int("engine.toggle_key", VK_F10);
    if (key <= 0) return;
    const bool down = (GetAsyncKeyState(key) & 0x8000) != 0;
    if (down == g_toggle_key_down) return;
    g_toggle_key_down = down;
    if (!down) return;
    DWORD foreground_process = 0;
    GetWindowThreadProcessId(GetForegroundWindow(), &foreground_process);
    if (foreground_process != GetCurrentProcessId()) return;
    g_toggled_off = !g_toggled_off;
    read_settings();
    logf("engine: toggled %s", g_toggled_off ? "off" : "on");
}

void engine_on_present(ID3D11DeviceContext* immediate) {
    if (config_poll()) read_settings();
    poll_toggle_key();
    hlsl_poll();
    gtao_on_present();
    ssgi_on_present();
    {
        std::lock_guard<std::mutex> lock(g_key_lock);
        memcpy(g_key_current, g_key_next, sizeof(g_key_current));
        memset(g_key_next, 0, sizeof(g_key_next));
        g_key_peak.store(0.0f, std::memory_order_relaxed);
    }
    volumetrics_on_present();
    casters_on_present(immediate);
    pointshadow_on_present();
    g_taa_fresh.store(!g_want_camera.load(std::memory_order_relaxed), std::memory_order_relaxed);
    g_want_vol_inputs.store(true, std::memory_order_relaxed);
    g_vol_ran.store(false, std::memory_order_relaxed);
    g_want_forward.store(true, std::memory_order_relaxed);
    {
        float camera[3];
        {
            std::lock_guard<std::mutex> lock(g_camera_lock);
            memcpy(camera, g_camera, sizeof(camera));
        }
        reflect_on_present(camera, !g_want_camera.load(std::memory_order_relaxed));
    }
    g_want_camera.store(true, std::memory_order_relaxed);
}

bool engine_enabled() {
    return g_enabled;
}

bool engine_on_dispatch(ID3D11DeviceContext* ctx, Role role, UINT, UINT, UINT, const ViewConstants& view, CasterList& casters) {
    switch (role) {
    case Role::SsaoPrepare: {
        // Ahead of the shadow passes, which use the list up.
        if (g_ssgi) ssgi_note_casters(casters);
        ID3D11ShaderResourceView* depth = nullptr;
        ctx->CSGetShaderResources(0, 1, &depth);
        if (depth) {
            ID3D11Resource* res = nullptr;
            depth->GetResource(&res);
            g_scene_depth_resource.store(res, std::memory_order_relaxed);
            if (res) res->Release();
            {
                std::lock_guard<std::mutex> lock(g_vol_lock);
                if (g_vol.depth) g_vol.depth->Release();
                depth->AddRef();
                g_vol.depth = depth;
                if (view.valid) g_vol.view = view;
            }
            depth->Release();
        }
        if (!casters.draws.empty()) {
            const ForwardConstants forward = engine_forward_constants();
            const bool sun = forward.valid && g_enabled && sunshadow_enabled();
            const bool points = g_pack && pointshadow_enabled();
            if (sun || points) {
                float taa_camera[3];
                {
                    std::lock_guard<std::mutex> lock(g_camera_lock);
                    memcpy(taa_camera, g_camera, sizeof(taa_camera));
                }
                // A value left over from when it last ran would read as a camera standing still.
                if (!g_taa_fresh.load(std::memory_order_relaxed)) memset(taa_camera, 0, sizeof(taa_camera));
                casters_ingest(casters, view, taa_camera);
                float camera[3];
                casters_camera(camera);
                if (sun) sunshadow_render(ctx, view, forward.regs[73], camera);
                if (points) pointshadow_render(ctx);
                casters_end_frame();
            } else {
                caster_list_release(casters);
            }
        }
        if (g_gtao) gtao_note_depth(ctx);
        if (g_ssgi) {
            float taa_camera[3];
            {
                std::lock_guard<std::mutex> lock(g_camera_lock);
                memcpy(taa_camera, g_camera, sizeof(taa_camera));
            }
            ssgi_run(ctx, view, taa_camera);
        }
        return false;
    }
    case Role::SsaoRender:
    case Role::SsaoBlendOut:
        return g_gtao;
    case Role::SsaoBlurUpsample:
        return g_gtao && gtao_run(ctx, view);
    default:
        return false;
    }
}

ID3D11PixelShader* engine_replace_ps(Role role) {
    if (role == Role::Composite && g_composite) return hlsl_ps("composite.hlsl", "main");
    return nullptr;
}

bool engine_pack_enabled() {
    return g_pack;
}

void engine_on_execute(ID3D11DeviceContext* immediate, ID3D11CommandList* list) {
    if (immediate->GetType() != D3D11_DEVICE_CONTEXT_IMMEDIATE) return;  // a list inside a list: its parent's turn counts
    pointshadow_on_execute(immediate, list);
    if (!g_pack_cb) return;
    // The sun's matrices are relative to this frame's camera and its atlas is drawn in the prepass list, so a forward list
    // that bound them while recording could read last frame's against this frame's atlas.
    PackConstants c{};
    memcpy(c.strength, g_strength, sizeof(c.strength));
    const SunShadowConstants sun = sunshadow_constants();
    memcpy(c.split, sun.split, sizeof(c.split));
    memcpy(c.uvz, sun.uvz, sizeof(c.uvz));
    memcpy(c.param, sun.param, sizeof(c.param));
    c.param[3] = g_sun_gain;
    memcpy(c.fill, g_fill, sizeof(c.fill));
    memcpy(c.light, g_light, sizeof(c.light));
    memcpy(c.film, g_film, sizeof(c.film));
    memcpy(c.black, g_black, sizeof(c.black));
    static PackConstants last;
    static bool have_last;
    if (have_last && memcmp(&c, &last, sizeof(c)) == 0) return;
    immediate->UpdateSubresource(g_pack_cb, 0, nullptr, &c, 0, 0);
    last = c;
    have_last = true;
}

void engine_bind_pack(ID3D11DeviceContext* ctx) {
    ctx->PSSetConstantBuffers(3, 1, &g_pack_cb);
    ssgi_bind_forward(ctx, g_ssgi);
    ID3D11ShaderResourceView* srv = sunshadow_srv();
    ctx->PSSetShaderResources(19, 1, &srv);
    pointshadow_bind(ctx);
}

void engine_bind_composite(ID3D11DeviceContext* ctx) {
    ctx->PSSetConstantBuffers(3, 1, &g_pack_cb);
}

void engine_bind_draw_kind(ID3D11DeviceContext* ctx, bool foliage) {
    ctx->PSSetConstantBuffers(6, 1, &g_draw_kind[foliage ? 1 : 0]);
}

bool engine_wants_forward_constants() {
    return g_want_forward.load(std::memory_order_relaxed);
}

void engine_publish_forward_constants(const void* regs) {
    if (!g_want_forward.exchange(false, std::memory_order_relaxed)) return;
    std::lock_guard<std::mutex> lock(g_forward_lock);
    memcpy(g_forward.regs, regs, sizeof(g_forward.regs));
    g_forward.valid = true;
}

void engine_note_key_light(const float* c6) {
    const float peak = std::max({c6[0], c6[1], c6[2]});
    // Above the gate a row's value is the specular of water or lava, not a sun.
    if (peak > 0.33f || peak <= g_key_peak.load(std::memory_order_relaxed)) return;
    std::lock_guard<std::mutex> lock(g_key_lock);
    if (peak <= g_key_peak.load(std::memory_order_relaxed)) return;
    g_key_peak.store(peak, std::memory_order_relaxed);
    memcpy(g_key_next, c6, sizeof(g_key_next));
}

void engine_key_light(float out[3]) {
    std::lock_guard<std::mutex> lock(g_key_lock);
    memcpy(out, g_key_current, sizeof(g_key_current));
}

ForwardConstants engine_forward_constants() {
    std::lock_guard<std::mutex> lock(g_forward_lock);
    return g_forward;
}

bool engine_wants_camera() {
    return g_want_camera.load(std::memory_order_relaxed);
}

void engine_publish_camera(const float* xyz) {
    if (!g_want_camera.exchange(false, std::memory_order_relaxed)) return;
    std::lock_guard<std::mutex> lock(g_camera_lock);
    memcpy(g_camera, xyz, sizeof(g_camera));
}

void* engine_scene_depth_resource() {
    return g_scene_depth_resource.load(std::memory_order_relaxed);
}

bool engine_wants_volumetrics_inputs() {
    return g_volumetrics && g_want_vol_inputs.load(std::memory_order_relaxed);
}

void engine_offer_volumetrics_inputs(ID3D11DeviceContext* ctx) {
    ID3D11RenderTargetView* rtv = nullptr;
    ID3D11DepthStencilView* dsv = nullptr;
    ctx->OMGetRenderTargets(1, &rtv, &dsv);
    // Forward shaders also draw a reflection, into its own target and depth.
    bool scene = false;
    if (rtv && dsv) {
        ID3D11Resource* res = nullptr;
        dsv->GetResource(&res);
        scene = res && res == engine_scene_depth_resource();
        if (res) res->Release();
    }
    if (dsv) dsv->Release();
    ID3D11ShaderResourceView* lights[3] = {};
    if (scene) ctx->PSGetShaderResources(16, 3, lights);
    if (scene && lights[0] && lights[1] && lights[2] && g_want_vol_inputs.exchange(false, std::memory_order_relaxed)) {
        ID3D11ShaderResourceView* atlas = nullptr;
        ctx->PSGetShaderResources(7, 1, &atlas);
        volumetrics_note_shadow_atlas(atlas);
        if (atlas) atlas->Release();
        std::lock_guard<std::mutex> lock(g_vol_lock);
        std::swap(g_vol.rtv, rtv);
        for (int i = 0; i < 3; i++) std::swap(g_vol.lights[i], lights[i]);
    }
    if (rtv) rtv->Release();
    for (ID3D11ShaderResourceView* light : lights) {
        if (light) light->Release();
    }
}

// Called ahead of engine_on_copy_resource: what a surface reflects is its own business, and the air in front of it is added afterwards.
void engine_reflect_on_copy(ID3D11DeviceContext* ctx, ID3D11Resource* src) {
    if (!reflect_wants_copy(src)) return;
    ID3D11ShaderResourceView* depth = nullptr;
    ViewConstants view;
    {
        std::lock_guard<std::mutex> lock(g_vol_lock);
        depth = g_vol.depth;
        if (depth) depth->AddRef();
        view = g_vol.view;
    }
    float camera[3];
    {
        std::lock_guard<std::mutex> lock(g_camera_lock);
        memcpy(camera, g_camera, sizeof(camera));
    }
    reflect_on_scene_copy(ctx, depth, view, camera, !g_want_camera.load(std::memory_order_relaxed));
    if (depth) depth->Release();
}

void engine_on_opaque_copy(ID3D11DeviceContext* ctx, ID3D11Resource* src) {
    // Bounce is taken from lit surfaces and not from the air in front of them: the wrapper calls this ahead of the
    // volumetrics at the same copy.
    if (g_ssgi) ssgi_on_copy_resource(ctx, src);
}

void engine_on_copy_resource(ID3D11DeviceContext* ctx, ID3D11Resource* src) {
    if (!g_volumetrics || g_vol_ran.load(std::memory_order_relaxed)) return;
    VolInputs in;
    {
        std::lock_guard<std::mutex> lock(g_vol_lock);
        if (!g_vol.rtv || !g_vol.depth || !g_vol.lights[0]) return;
        in = g_vol;
        in.rtv->AddRef();
        in.depth->AddRef();
        for (ID3D11ShaderResourceView* light : in.lights) light->AddRef();
    }
    ID3D11Resource* scene = nullptr;
    in.rtv->GetResource(&scene);
    if (scene == src && !g_vol_ran.exchange(true, std::memory_order_relaxed)) {
        const ForwardConstants forward = engine_forward_constants();
        volumetrics_run(ctx, in.rtv, in.depth, in.lights, in.view, forward);
    }
    if (scene) scene->Release();
    in.rtv->Release();
    in.depth->Release();
    for (ID3D11ShaderResourceView* light : in.lights) light->Release();
}

bool engine_hide_ui() {
    return g_hide_ui;
}
