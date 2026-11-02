// Settings in dsrle.ini, read on every run so they tune live.
//
//   ssgi.enabled = 1            handled by the caller
//   ssgi.strength = 1           1 is the physical one bounce; the probes already hold some, the gate below keeps that out
//   ssgi.gate = 0.85            how much of a surface's own surroundings to take off what it sees. 0 is plain bounce, and
//                               lifts rooms where nothing is lit. Hue stays that of what was hit
//   ssgi.gate_mip = 5           which colour mip stands for those surroundings: about 128 screen pixels. Much narrower and
//                               the surface's own texture is printed into the bounce, inverted; wider and the foot of a
//                               wall beside sunlit ground loses more of its bounce
//   ssgi.radius = 8             metres a ray reaches
//   ssgi.rays = 4               rays per trace pixel
//   ssgi.steps = 12             steps per ray
//   ssgi.thickness = 0.6        metres behind a surface that still count as hitting it
//   ssgi.downscale = 2          screen pixels per trace pixel, 2 or 4
//   ssgi.max_radiance = 2       scene luminance a surface can bounce at most, hue kept
//   ssgi.sky_weight = 0.02      how much of the sky's colour counts; the probes already carry the sky
//   ssgi.sky_z = 1000           metres past which a pixel is sky. It is geometry: 1600 to 2500 in the traces
//   ssgi.max_gi = 3             soft ceiling on the luminance of the result
//   ssgi.history = 16           frames of temporal accumulation at most; a change in the light takes this long to arrive
//   ssgi.denoise_passes = 3     blur passes, 1 to 3
//   ssgi.fade_start = 60        metres: the bounce fades from here...
//   ssgi.fade_end = 150         ...to nothing here
//   ssgi.normals = 0            0 from depth, 1 from last frame's G-buffer where one exists
//   ssgi.warp_distance = 10     metres in one frame past which history is dropped
#include "ssgi.h"
#include "common.h"
#include "config.h"
#include "hlsl.h"
#include "state.h"
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstring>
#include <mutex>
#include <unordered_map>
#include <vector>

namespace {

constexpr UINT kDepthMips = 5;
constexpr UINT kColourMips = 6;
constexpr UINT kComputeSrvs = 4;
constexpr UINT kComputeUavs = 5;
constexpr UINT kForwardSlot = 25;
constexpr size_t kVoteDraws = 256;

// Must match the cbuffer in ssgi.hlsl.
struct Constants {
    float full_size[2], full_inv[2];
    float trace_size[2], trace_inv[2];
    float p00, p11, jx, jy;
    float z_a, z_b, radius, thickness;
    float prev_x[4], prev_y[4], prev_w[4];
    float frame, sky_z, sky_weight, max_radiance;
    float gate, strength, max_gi, history_cap;
    float fade_end, fade_k, history_ok, local_mip;
    int rays, steps, block, colour_ok;
    float right[3];
    int normals;
    float up[3];
    float pad0;
    float forward[3];
    float pad1;
};
static_assert(sizeof(Constants) % 16 == 0);

struct Camera {
    float right[3], up[3], forward[3];
    float p00, p11, jx, jy, z_a, z_b;
};

// A prepass draw of last frame, to compare its palette against this frame's.
struct DrawKey {
    ID3D11Buffer* vb;
    ID3D11Buffer* ib;
    UINT start_index;
    INT base_vertex;
    bool operator==(const DrawKey& o) const { return vb == o.vb && ib == o.ib && start_index == o.start_index && base_vertex == o.base_vertex; }
};
struct DrawKeyHash {
    size_t operator()(const DrawKey& k) const {
        size_t h = reinterpret_cast<size_t>(k.vb) * 31u ^ reinterpret_cast<size_t>(k.ib);
        return h * 0x9E3779B97F4A7C15ull ^ (static_cast<size_t>(k.start_index) << 20) ^ static_cast<size_t>(static_cast<UINT>(k.base_vertex));
    }
};
struct SeenDraw {
    float translation[3];
    uint32_t stamp;
    bool ambiguous;
};

ID3D11Device* g_device;

// The worker that reaches an anchor records there, and either anchor can be first, so this guards everything below for
// the whole of each recording. The atomics are what the frame boundary and the fast paths touch without it.
std::mutex g_lock;
std::atomic<uint32_t> g_presents{0};
std::atomic<bool> g_ran{false};
std::atomic<bool> g_captured{false};
std::atomic<bool> g_ran_last_frame{false};
std::atomic<bool> g_captured_last_frame{false};
uint32_t g_last_run_present;
bool g_ever_ran;

UINT g_full_w, g_full_h, g_block;
UINT g_failed_w, g_failed_h, g_failed_block;
UINT g_trace_w, g_trace_h;
ID3D11Buffer* g_cb;
ID3D11Buffer* g_pass_cb[3];  // immutable: the three a-trous spacings
ID3D11SamplerState* g_point_clamp;
ID3D11SamplerState* g_linear_clamp;

ID3D11Texture2D* g_z;
ID3D11ShaderResourceView* g_z_srv;
ID3D11UnorderedAccessView* g_z_uav[kDepthMips];
ID3D11Texture2D* g_z_prev;
ID3D11ShaderResourceView* g_z_prev_srv;

struct Target {
    ID3D11Texture2D* tex = nullptr;
    ID3D11ShaderResourceView* srv = nullptr;
    ID3D11UnorderedAccessView* uav = nullptr;
};
Target g_guide, g_raw, g_tmp[2], g_history[2], g_output;
UINT g_history_index;

ID3D11Texture2D* g_colour;
ID3D11ShaderResourceView* g_colour_srv;
ID3D11ShaderResourceView* g_colour_mip_srv[kColourMips];
ID3D11UnorderedAccessView* g_colour_uav[kColourMips];
UINT g_colour_w, g_colour_h;

ID3D11ShaderResourceView* g_depth_srv;
ID3D11Resource* g_scene_res;
ID3D11ShaderResourceView* g_scene_srv;
ID3D11ShaderResourceView* g_gbuffer_srv;

bool g_need_clear;
bool g_have_rows;
float g_prev_rows[16];
Camera g_camera;
bool g_have_camera;
uint32_t g_frame;

std::unordered_map<DrawKey, SeenDraw, DrawKeyHash> g_seen;
std::vector<float> g_votes[3];
uint32_t g_seen_stamp = 1;  // a new entry starts at stamp 0, which must not look like last frame
bool g_vote_ok;
float g_vote[3];

float g_taa_last[3], g_taa_before[3];
bool g_taa_known;
int g_taa_unchanged;

SsgiStats g_stats;

template <class T>
void drop(T*& p) {
    if (p) p->Release();
    p = nullptr;
}

void drop_target(Target& t) {
    drop(t.tex);
    drop(t.srv);
    drop(t.uav);
}

void release_targets() {
    drop(g_z);
    drop(g_z_srv);
    for (auto*& u : g_z_uav) drop(u);
    drop(g_z_prev);
    drop(g_z_prev_srv);
    drop_target(g_guide);
    drop_target(g_raw);
    for (Target& t : g_tmp) drop_target(t);
    for (Target& t : g_history) drop_target(t);
    drop_target(g_output);
    drop(g_colour);
    drop(g_colour_srv);
    for (auto*& s : g_colour_mip_srv) drop(s);
    for (auto*& u : g_colour_uav) drop(u);
    g_full_w = g_full_h = 0;
}

bool make_target(Target& t, UINT w, UINT h, DXGI_FORMAT format) {
    D3D11_TEXTURE2D_DESC d{};
    d.Width = w;
    d.Height = h;
    d.MipLevels = 1;
    d.ArraySize = 1;
    d.Format = format;
    d.SampleDesc.Count = 1;
    d.Usage = D3D11_USAGE_DEFAULT;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS;
    return SUCCEEDED(g_device->CreateTexture2D(&d, nullptr, &t.tex)) && SUCCEEDED(g_device->CreateShaderResourceView(t.tex, nullptr, &t.srv)) &&
           SUCCEEDED(g_device->CreateUnorderedAccessView(t.tex, nullptr, &t.uav));
}

bool create_targets_inner(UINT full_w, UINT full_h, UINT block) {
    const UINT trace_w = std::max((full_w + block - 1) / block, 16u);
    const UINT trace_h = std::max((full_h + block - 1) / block, 16u);

    D3D11_TEXTURE2D_DESC d{};
    d.Width = trace_w;
    d.Height = trace_h;
    d.MipLevels = kDepthMips;
    d.ArraySize = 1;
    d.Format = DXGI_FORMAT_R32_FLOAT;
    d.SampleDesc.Count = 1;
    d.Usage = D3D11_USAGE_DEFAULT;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, &g_z))) return false;
    if (FAILED(g_device->CreateShaderResourceView(g_z, nullptr, &g_z_srv))) return false;
    for (UINT m = 0; m < kDepthMips; m++) {
        D3D11_UNORDERED_ACCESS_VIEW_DESC u{};
        u.Format = d.Format;
        u.ViewDimension = D3D11_UAV_DIMENSION_TEXTURE2D;
        u.Texture2D.MipSlice = m;
        if (FAILED(g_device->CreateUnorderedAccessView(g_z, &u, &g_z_uav[m]))) return false;
    }
    d.MipLevels = 1;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, &g_z_prev))) return false;
    if (FAILED(g_device->CreateShaderResourceView(g_z_prev, nullptr, &g_z_prev_srv))) return false;

    const DXGI_FORMAT hdr = DXGI_FORMAT_R16G16B16A16_FLOAT;
    if (!make_target(g_guide, trace_w, trace_h, hdr) || !make_target(g_raw, trace_w, trace_h, hdr)) return false;
    for (Target& t : g_tmp) {
        if (!make_target(t, trace_w, trace_h, hdr)) return false;
    }
    for (Target& t : g_history) {
        if (!make_target(t, trace_w, trace_h, hdr)) return false;
    }
    if (!make_target(g_output, full_w, full_h, hdr)) return false;

    // The scene at half resolution, whatever the trace's own resolution is: it is sampled by position only.
    g_colour_w = std::max(full_w / 2, 1u);
    g_colour_h = std::max(full_h / 2, 1u);
    d.Width = g_colour_w;
    d.Height = g_colour_h;
    d.MipLevels = kColourMips;
    d.Format = hdr;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, &g_colour))) return false;
    if (FAILED(g_device->CreateShaderResourceView(g_colour, nullptr, &g_colour_srv))) return false;
    for (UINT m = 0; m < kColourMips; m++) {
        D3D11_SHADER_RESOURCE_VIEW_DESC s{};
        s.Format = hdr;
        s.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
        s.Texture2D.MostDetailedMip = m;
        s.Texture2D.MipLevels = 1;
        if (FAILED(g_device->CreateShaderResourceView(g_colour, &s, &g_colour_mip_srv[m]))) return false;
        D3D11_UNORDERED_ACCESS_VIEW_DESC u{};
        u.Format = hdr;
        u.ViewDimension = D3D11_UAV_DIMENSION_TEXTURE2D;
        u.Texture2D.MipSlice = m;
        if (FAILED(g_device->CreateUnorderedAccessView(g_colour, &u, &g_colour_uav[m]))) return false;
    }
    g_full_w = full_w;
    g_full_h = full_h;
    g_trace_w = trace_w;
    g_trace_h = trace_h;
    g_block = block;
    return true;
}

bool create_targets(UINT full_w, UINT full_h, UINT block) {
    release_targets();
    const bool ok = create_targets_inner(full_w, full_h, block);
    if (!ok) release_targets();
    g_need_clear = ok;
    g_have_rows = false;
    logf("ssgi: %ux%u, tracing at %ux%u%s", full_w, full_h, g_trace_w, g_trace_h, ok ? "" : ": FAILED");
    return ok;
}

bool create_shared() {
    if (g_cb) return true;
    D3D11_BUFFER_DESC b{};
    b.ByteWidth = sizeof(Constants);
    b.Usage = D3D11_USAGE_DYNAMIC;
    b.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    b.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    if (FAILED(g_device->CreateBuffer(&b, nullptr, &g_cb))) return false;
    for (int i = 0; i < 3; i++) {
        const int pass[4] = {1 << i, 0, 0, 0};
        D3D11_BUFFER_DESC p{};
        p.ByteWidth = sizeof(pass);
        p.Usage = D3D11_USAGE_IMMUTABLE;
        p.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
        D3D11_SUBRESOURCE_DATA init{pass, 0, 0};
        if (FAILED(g_device->CreateBuffer(&p, &init, &g_pass_cb[i]))) return false;
    }
    D3D11_SAMPLER_DESC s{};
    s.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
    s.AddressU = s.AddressV = s.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    s.ComparisonFunc = D3D11_COMPARISON_NEVER;
    s.MaxLOD = D3D11_FLOAT32_MAX;
    if (FAILED(g_device->CreateSamplerState(&s, &g_point_clamp))) return false;
    s.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    return SUCCEEDED(g_device->CreateSamplerState(&s, &g_linear_clamp));
}

float dot3(const float* a, const float* b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }

// Camera basis and projection from the camera-relative view-projection. c0 and c1 carry the TAA jitter as a multiple of
// the forward axis, so taking that out leaves P00 * right and P11 * up, and the jitter is kept to add back in the shader.
bool read_camera(const float* m, Camera& c) {
    const float w_len = std::sqrt(dot3(m + 12, m + 12));
    if (!(w_len > 0.0f)) return false;
    const float f[3] = {m[12] / w_len, m[13] / w_len, m[14] / w_len};
    c.jx = dot3(m + 0, f);
    c.jy = dot3(m + 4, f);
    const float rx[3] = {m[0] - f[0] * c.jx, m[1] - f[1] * c.jx, m[2] - f[2] * c.jx};
    const float uy[3] = {m[4] - f[0] * c.jy, m[5] - f[1] * c.jy, m[6] - f[2] * c.jy};
    c.p00 = std::sqrt(dot3(rx, rx));
    c.p11 = std::sqrt(dot3(uy, uy));
    if (!(c.p00 > 0.0f) || !(c.p11 > 0.0f)) return false;
    for (int i = 0; i < 3; i++) {
        c.right[i] = rx[i] / c.p00;
        c.up[i] = uy[i] / c.p11;
        c.forward[i] = f[i];
    }
    c.z_a = std::sqrt(dot3(m + 8, m + 8)) / w_len;
    c.z_b = m[11] / w_len;
    return c.z_b != 0.0f;
}

// How far the camera moved since the last run. The draws are exact and of this frame. The TAA pass's camera is a frame
// late and can be late by two, so as a stand-in it assumes last frame's motion carries on: one unchanged value can just
// be lateness, two in a row mean the camera has stopped.
void camera_delta(const float taa_camera[3], float out[3], bool& from_draws) {
    const bool changed = !g_taa_known || memcmp(taa_camera, g_taa_last, sizeof(g_taa_last)) != 0;
    if (changed) {
        memcpy(g_taa_before, g_taa_known ? g_taa_last : taa_camera, sizeof(g_taa_before));
        memcpy(g_taa_last, taa_camera, sizeof(g_taa_last));
        g_taa_known = true;
        g_taa_unchanged = 0;
    } else {
        g_taa_unchanged++;
    }
    from_draws = g_vote_ok;
    for (int i = 0; i < 3; i++) out[i] = g_vote_ok ? g_vote[i] : (g_taa_unchanged < 2 ? g_taa_last[i] - g_taa_before[i] : 0.0f);
    g_vote_ok = false;
}

void clear_compute(ID3D11DeviceContext* ctx) {
    ID3D11ShaderResourceView* no_srv[kComputeSrvs] = {};
    ID3D11UnorderedAccessView* no_uav[kComputeUavs] = {};
    ctx->CSSetUnorderedAccessViews(0, kComputeUavs, no_uav, nullptr);
    ctx->CSSetShaderResources(0, kComputeSrvs, no_srv);
}

bool upload(ID3D11DeviceContext* ctx, const Constants& c) {
    D3D11_MAPPED_SUBRESOURCE mapped;
    if (FAILED(ctx->Map(g_cb, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) return false;
    memcpy(mapped.pData, &c, sizeof(c));
    ctx->Unmap(g_cb, 0);
    return true;
}

// The fields both anchors share.
void fill_common(Constants& c, const Camera& cam) {
    c.full_size[0] = static_cast<float>(g_full_w);
    c.full_size[1] = static_cast<float>(g_full_h);
    c.full_inv[0] = 1.0f / c.full_size[0];
    c.full_inv[1] = 1.0f / c.full_size[1];
    c.trace_size[0] = static_cast<float>(g_trace_w);
    c.trace_size[1] = static_cast<float>(g_trace_h);
    c.trace_inv[0] = 1.0f / c.trace_size[0];
    c.trace_inv[1] = 1.0f / c.trace_size[1];
    c.p00 = cam.p00;
    c.p11 = cam.p11;
    c.jx = cam.jx;
    c.jy = cam.jy;
    c.z_a = cam.z_a;
    c.z_b = cam.z_b;
    c.sky_z = cfg_float("ssgi.sky_z", 1000.0f);
    c.sky_weight = std::max(cfg_float("ssgi.sky_weight", 0.02f), 0.0f);
    c.max_radiance = std::max(cfg_float("ssgi.max_radiance", 2.0f), 1e-3f);
    c.block = static_cast<int>(g_block);
}

}  // namespace

void ssgi_init(ID3D11Device* device) {
    g_device = device;
}

void ssgi_on_present() {
    g_presents.fetch_add(1, std::memory_order_relaxed);
    g_ran_last_frame.store(g_ran.exchange(false, std::memory_order_relaxed), std::memory_order_relaxed);
    g_captured_last_frame.store(g_captured.exchange(false, std::memory_order_relaxed), std::memory_order_relaxed);
}

void ssgi_note_casters(const CasterList& list) {
    if (list.draws.empty()) return;
    std::lock_guard<std::mutex> lock(g_lock);
    const uint32_t stamp = ++g_seen_stamp;
    for (auto& votes : g_votes) votes.clear();
    size_t considered = 0;
    for (const CasterDraw& d : list.draws) {
        if (d.instanced) continue;  // their transforms are in a vertex stream
        // Plenty to agree on, and the order of a frame's draws changes slowly enough that most of these met last frame.
        if (++considered > kVoteDraws) break;
        const float* regs = reinterpret_cast<const float*>(&list.vs_constants[d.constants_at]);
        // Palette entry 0, row-major float3x4 from c28: the translation is camera-relative.
        const float t[3] = {regs[28 * 4 + 3], regs[29 * 4 + 3], regs[30 * 4 + 3]};
        SeenDraw& seen = g_seen[DrawKey{d.vb[0], d.ib, d.start_index, d.base_vertex}];
        if (seen.stamp == stamp) {
            // The same mesh twice in a frame: which is which cannot be told.
            seen.ambiguous = true;
            continue;
        }
        if (seen.stamp + 1 == stamp && !seen.ambiguous) {
            for (int i = 0; i < 3; i++) g_votes[i].push_back(seen.translation[i] - t[i]);
        }
        memcpy(seen.translation, t, sizeof(t));
        seen.stamp = stamp;
        seen.ambiguous = false;
    }
    if (g_seen.size() > 16384) g_seen.clear();

    // Whatever moved on its own, and whatever a pointer now names, is out in the tails.
    g_vote_ok = false;
    const size_t n = g_votes[0].size();
    if (n < 6) return;
    float median[3];
    for (int i = 0; i < 3; i++) {
        std::vector<float> sorted = g_votes[i];
        std::nth_element(sorted.begin(), sorted.begin() + n / 2, sorted.end());
        median[i] = sorted[n / 2];
    }
    size_t agree = 0;
    for (size_t v = 0; v < n; v++) {
        if (std::fabs(g_votes[0][v] - median[0]) < 0.01f && std::fabs(g_votes[1][v] - median[1]) < 0.01f && std::fabs(g_votes[2][v] - median[2]) < 0.01f) agree++;
    }
    if (agree * 2 < n) return;
    memcpy(g_vote, median, sizeof(g_vote));
    g_vote_ok = true;
}

void ssgi_run(ID3D11DeviceContext* ctx, const ViewConstants& view, const float taa_camera[3]) {
    if (!g_device || !ctx || !view.valid) return;
    const float strength = cfg_float("ssgi.strength", 2.0f);
    if (!(strength > 0.0f)) return;

    auto* cs_prepare = hlsl_cs("ssgi.hlsl", "prepare_main");
    auto* cs_normal = hlsl_cs("ssgi.hlsl", "normal_main");
    auto* cs_trace = hlsl_cs("ssgi.hlsl", "trace_main");
    auto* cs_temporal = hlsl_cs("ssgi.hlsl", "temporal_main");
    auto* cs_atrous = hlsl_cs("ssgi.hlsl", "atrous_main");
    auto* cs_upsample = hlsl_cs("ssgi.hlsl", "upsample_main");
    if (!cs_prepare || !cs_normal || !cs_trace || !cs_temporal || !cs_atrous || !cs_upsample) return;

    Camera cam;
    if (!read_camera(view.vp, cam)) return;

    ID3D11ShaderResourceView* depth = nullptr;
    ctx->CSGetShaderResources(0, 1, &depth);
    if (!depth) return;
    D3D11_TEXTURE2D_DESC depth_desc{};
    {
        ID3D11Resource* res = nullptr;
        depth->GetResource(&res);
        D3D11_RESOURCE_DIMENSION dimension = D3D11_RESOURCE_DIMENSION_UNKNOWN;
        if (res) res->GetType(&dimension);
        if (dimension == D3D11_RESOURCE_DIMENSION_TEXTURE2D) static_cast<ID3D11Texture2D*>(res)->GetDesc(&depth_desc);
        if (res) res->Release();
    }
    if (depth_desc.Width == 0 || depth_desc.Height == 0) {
        depth->Release();
        return;
    }

    std::lock_guard<std::mutex> lock(g_lock);
    if (g_ran.exchange(true, std::memory_order_relaxed)) {
        depth->Release();
        return;
    }
    drop(g_depth_srv);
    g_depth_srv = depth;  // takes the reference from the getter; the capture uses it too

    const UINT block = cfg_int("ssgi.downscale", 2) >= 4 ? 4u : 2u;
    const bool fresh = g_full_w != depth_desc.Width || g_full_h != depth_desc.Height || g_block != block;
    if (fresh) {
        // A size that failed once is not tried again every frame.
        if (g_failed_w == depth_desc.Width && g_failed_h == depth_desc.Height && g_failed_block == block) return;
        if (!create_shared() || !create_targets(depth_desc.Width, depth_desc.Height, block)) {
            g_failed_w = depth_desc.Width;
            g_failed_h = depth_desc.Height;
            g_failed_block = block;
            return;
        }
    }

    const uint32_t presents = g_presents.load(std::memory_order_relaxed);
    const bool continuous = g_ever_ran && g_ran_last_frame.load(std::memory_order_relaxed) && !fresh;
    g_last_run_present = presents;
    g_ever_ran = true;
    g_frame++;

    float delta[3];
    bool from_draws;
    camera_delta(taa_camera, delta, from_draws);
    const float warp = std::max(cfg_float("ssgi.warp_distance", 10.0f), 0.1f);
    const bool warped = !(dot3(delta, delta) <= warp * warp);
    if (warped) delta[0] = delta[1] = delta[2] = 0.0f;

    const bool history_ok = continuous && g_have_rows && !warped;
    const bool colour_ok = history_ok && g_captured_last_frame.load(std::memory_order_relaxed);
    if (!history_ok) g_stats.resets++;

    Constants c{};
    fill_common(c, cam);
    c.radius = std::max(cfg_float("ssgi.radius", 8.0f), 0.1f);
    c.thickness = std::max(cfg_float("ssgi.thickness", 0.6f), 0.01f);
    c.frame = static_cast<float>(g_frame % 1024);
    c.gate = std::clamp(cfg_float("ssgi.gate", 0.85f), 0.0f, 1.0f);
    c.strength = strength;
    c.max_gi = std::max(cfg_float("ssgi.max_gi", 3.0f), 1e-3f);
    c.history_cap = std::max(cfg_float("ssgi.history", 16.0f), 1.0f);
    const float fade_start = cfg_float("ssgi.fade_start", 60.0f);
    c.fade_end = std::max(cfg_float("ssgi.fade_end", 150.0f), fade_start + 1.0f);
    c.fade_k = 1.0f / (c.fade_end - fade_start);
    c.history_ok = history_ok ? 1.0f : 0.0f;
    // Colour mip for a surface's surroundings; mip 0 is half resolution. Wide enough to average out texture, close enough
    // to be the surface's own light level.
    c.local_mip = std::clamp(cfg_float("ssgi.gate_mip", 5.0f), 0.0f, static_cast<float>(kColourMips - 1));
    c.rays = std::clamp(cfg_int("ssgi.rays", 4), 1, 16);
    c.steps = std::clamp(cfg_int("ssgi.steps", 12), 2, 48);
    c.colour_ok = colour_ok ? 1 : 0;
    c.normals = cfg_int("ssgi.normals", 0) == 1 && g_gbuffer_srv ? 1 : 0;
    memcpy(c.right, cam.right, sizeof(c.right));
    memcpy(c.up, cam.up, sizeof(c.up));
    memcpy(c.forward, cam.forward, sizeof(c.forward));

    // This frame's view space to last frame's clip space: the camera axes, the move, then last frame's rows.
    const float* rows = g_have_rows ? g_prev_rows : view.vp;
    float* const prev[3] = {c.prev_x, c.prev_y, c.prev_w};
    const float* const source[3] = {rows + 0, rows + 4, rows + 12};
    for (int i = 0; i < 3; i++) {
        prev[i][0] = dot3(source[i], cam.right);
        prev[i][1] = dot3(source[i], cam.up);
        prev[i][2] = dot3(source[i], cam.forward);
        prev[i][3] = dot3(source[i], delta) + source[i][3];
    }

    if (!upload(ctx, c)) return;

    {
        StateBackup backup(ctx);
        unbind_all(ctx);
        if (g_need_clear) {
            const float zero[4] = {};
            ctx->ClearUnorderedAccessViewFloat(g_output.uav, zero);
            ctx->ClearUnorderedAccessViewFloat(g_history[0].uav, zero);
            ctx->ClearUnorderedAccessViewFloat(g_history[1].uav, zero);
            for (auto* u : g_colour_uav) ctx->ClearUnorderedAccessViewFloat(u, zero);
            g_need_clear = false;
        }
        ctx->CSSetConstantBuffers(0, 1, &g_cb);
        ID3D11SamplerState* samplers[2] = {g_point_clamp, g_linear_clamp};
        ctx->CSSetSamplers(0, 2, samplers);
        const UINT gx = (g_trace_w + 7) / 8, gy = (g_trace_h + 7) / 8;

        ctx->CSSetShader(cs_prepare, nullptr, 0);
        ctx->CSSetShaderResources(0, 1, &g_depth_srv);
        ctx->CSSetUnorderedAccessViews(0, kDepthMips, g_z_uav, nullptr);
        ctx->Dispatch((g_trace_w + 15) / 16, (g_trace_h + 15) / 16, 1);
        clear_compute(ctx);

        ctx->CSSetShader(cs_normal, nullptr, 0);
        ID3D11ShaderResourceView* normal_inputs[4] = {g_z_srv, nullptr, g_colour_srv, c.normals ? g_gbuffer_srv : nullptr};
        ctx->CSSetShaderResources(0, 4, normal_inputs);
        ctx->CSSetUnorderedAccessViews(0, 1, &g_guide.uav, nullptr);
        ctx->Dispatch(gx, gy, 1);
        clear_compute(ctx);

        ctx->CSSetShader(cs_trace, nullptr, 0);
        ID3D11ShaderResourceView* trace_inputs[3] = {g_z_srv, g_guide.srv, g_colour_srv};
        ctx->CSSetShaderResources(0, 3, trace_inputs);
        ctx->CSSetUnorderedAccessViews(0, 1, &g_raw.uav, nullptr);
        ctx->Dispatch(gx, gy, 1);
        clear_compute(ctx);

        const UINT write = g_history_index ^ 1;
        ctx->CSSetShader(cs_temporal, nullptr, 0);
        ID3D11ShaderResourceView* temporal_inputs[4] = {g_raw.srv, g_guide.srv, g_history[g_history_index].srv, g_z_prev_srv};
        ctx->CSSetShaderResources(0, 4, temporal_inputs);
        ctx->CSSetUnorderedAccessViews(0, 1, &g_tmp[0].uav, nullptr);
        ctx->Dispatch(gx, gy, 1);
        clear_compute(ctx);

        // The first blur's result is next frame's history, so spatial and temporal work together without the wide
        // passes smearing the history a little further every frame.
        const int passes = std::clamp(cfg_int("ssgi.denoise_passes", 3), 1, 3);
        ctx->CSSetShader(cs_atrous, nullptr, 0);
        Target* in = &g_tmp[0];
        Target* out = &g_history[write];
        ID3D11ShaderResourceView* final_srv = nullptr;
        for (int pass = 0; pass < passes; pass++) {
            ID3D11ShaderResourceView* inputs[2] = {in->srv, g_guide.srv};
            ctx->CSSetConstantBuffers(1, 1, &g_pass_cb[pass]);
            ctx->CSSetShaderResources(0, 2, inputs);
            ctx->CSSetUnorderedAccessViews(0, 1, &out->uav, nullptr);
            ctx->Dispatch(gx, gy, 1);
            clear_compute(ctx);
            final_srv = out->srv;
            in = out;
            out = pass == 0 ? &g_tmp[0] : &g_tmp[1];
        }
        g_history_index = write;

        ctx->CSSetShader(cs_upsample, nullptr, 0);
        ID3D11ShaderResourceView* upsample_inputs[3] = {g_depth_srv, g_guide.srv, final_srv};
        ctx->CSSetShaderResources(0, 3, upsample_inputs);
        ctx->CSSetUnorderedAccessViews(0, 1, &g_output.uav, nullptr);
        ctx->Dispatch((g_full_w + 7) / 8, (g_full_h + 7) / 8, 1);
        clear_compute(ctx);

        ctx->CopySubresourceRegion(g_z_prev, 0, 0, 0, 0, g_z, 0, nullptr);
    }

    memcpy(g_prev_rows, view.vp, sizeof(g_prev_rows));
    g_have_rows = true;
    g_camera = cam;
    g_have_camera = true;

    memcpy(g_stats.camera_delta, delta, sizeof(delta));
    g_stats.delta_from_draws = from_draws;
    g_stats.history_ok = history_ok;
    g_stats.colour_ok = colour_ok;
    g_stats.runs++;
}

void ssgi_on_copy_resource(ID3D11DeviceContext* ctx, ID3D11Resource* src) {
    if (!src || !ctx || g_captured.load(std::memory_order_relaxed)) return;
    if (!(cfg_float("ssgi.strength", 2.0f) > 0.0f)) return;
    D3D11_RESOURCE_DIMENSION dimension = D3D11_RESOURCE_DIMENSION_UNKNOWN;
    src->GetType(&dimension);
    if (dimension != D3D11_RESOURCE_DIMENSION_TEXTURE2D) return;
    D3D11_TEXTURE2D_DESC d{};
    static_cast<ID3D11Texture2D*>(src)->GetDesc(&d);
    if (d.Format != DXGI_FORMAT_R11G11B10_FLOAT && d.Format != DXGI_FORMAT_R16G16B16A16_FLOAT) return;
    if ((d.BindFlags & D3D11_BIND_SHADER_RESOURCE) == 0 || d.SampleDesc.Count != 1 || d.ArraySize != 1) return;

    auto* cs_capture = hlsl_cs("ssgi.hlsl", "capture_main");
    auto* cs_mip = hlsl_cs("ssgi.hlsl", "capture_mip_main");
    if (!cs_capture || !cs_mip) return;

    std::lock_guard<std::mutex> lock(g_lock);
    if (!g_cb || !g_have_camera || !g_depth_srv || d.Width != g_full_w || d.Height != g_full_h) return;
    // With the game's AO off, or the pass failing, nothing will read it. This frame's run can record after this.
    if (g_presents.load(std::memory_order_relaxed) - g_last_run_present > 1) return;
    if (g_captured.exchange(true, std::memory_order_relaxed)) return;

    if (g_scene_res != src) {
        drop(g_scene_srv);
        drop(g_scene_res);
        if (FAILED(g_device->CreateShaderResourceView(src, nullptr, &g_scene_srv))) return;
        src->AddRef();
        g_scene_res = src;
    }

    Constants c{};
    fill_common(c, g_camera);
    if (!upload(ctx, c)) return;

    {
        StateBackup backup(ctx);
        // Takes the scene and depth off the output stage first, or binding them as inputs would silently fail.
        unbind_all(ctx);
        ctx->CSSetConstantBuffers(0, 1, &g_cb);

        ctx->CSSetShader(cs_capture, nullptr, 0);
        ID3D11ShaderResourceView* inputs[2] = {g_scene_srv, g_depth_srv};
        ctx->CSSetShaderResources(0, 2, inputs);
        ctx->CSSetUnorderedAccessViews(0, 1, &g_colour_uav[0], nullptr);
        ctx->Dispatch((g_colour_w + 7) / 8, (g_colour_h + 7) / 8, 1);
        clear_compute(ctx);

        ctx->CSSetShader(cs_mip, nullptr, 0);
        for (UINT m = 1; m < kColourMips; m++) {
            ctx->CSSetShaderResources(0, 1, &g_colour_mip_srv[m - 1]);
            ctx->CSSetUnorderedAccessViews(0, 1, &g_colour_uav[m], nullptr);
            const UINT w = std::max(g_colour_w >> m, 1u), h = std::max(g_colour_h >> m, 1u);
            ctx->Dispatch((w + 7) / 8, (h + 7) / 8, 1);
            clear_compute(ctx);
        }
    }
    g_stats.captures++;
}

void ssgi_bind_forward(ID3D11DeviceContext* ctx, bool active) {
    std::lock_guard<std::mutex> lock(g_lock);
    ID3D11ShaderResourceView* srv = nullptr;
    // A frame or two out of date is still the right light, and this list may record ahead of this frame's run. Past
    // that, the game's AO is off or the pass is failing, and nothing is added at all.
    if (active && g_ever_ran && g_output.srv && g_presents.load(std::memory_order_relaxed) - g_last_run_present <= 2) srv = g_output.srv;
    ctx->PSSetShaderResources(kForwardSlot, 1, &srv);
}

void ssgi_set_gbuffer(ID3D11ShaderResourceView* srv) {
    std::lock_guard<std::mutex> lock(g_lock);
    if (srv == g_gbuffer_srv) return;
    if (srv) srv->AddRef();
    drop(g_gbuffer_srv);
    g_gbuffer_srv = srv;
}

SsgiStats ssgi_stats() {
    std::lock_guard<std::mutex> lock(g_lock);
    return g_stats;
}

ID3D11ShaderResourceView* ssgi_output() {
    std::lock_guard<std::mutex> lock(g_lock);
    if (g_output.srv) g_output.srv->AddRef();
    return g_output.srv;
}
