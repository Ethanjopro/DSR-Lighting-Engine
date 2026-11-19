#include "gtao.h"
#include "common.h"
#include "config.h"
#include "hlsl.h"
#include <cmath>
#include <mutex>

namespace {

constexpr UINT kDepthMips = 5;

struct Constants {
    float size[2], inv_size[2];
    float p00, p11, z_a, z_b;
    float radius, falloff, power, max_pixels;
    float thickness, fade_start, fade_end, frame;
    int slices, steps, step_scale, pad;
    float sun_view[3], contact_length;
    float contact_fade, pad2[3];
};
static_assert(sizeof(Constants) % 16 == 0);

ID3D11Device* g_device;
ID3D11Buffer* g_cb;
ID3D11SamplerState* g_point_clamp;

UINT g_width, g_height;
ID3D11Texture2D* g_depth_tex;
ID3D11ShaderResourceView* g_depth_srv;
ID3D11UnorderedAccessView* g_depth_uav[kDepthMips];
ID3D11Texture2D* g_work_tex[2];
ID3D11ShaderResourceView* g_work_srv[2];
ID3D11UnorderedAccessView* g_work_uav[2];

std::atomic<bool> g_targets_failed{false};  // the game's own chain runs from then on, all three dispatches of it

// The game's depth SRV, read at the SsaoPrepare dispatch of the same list.
ID3D11ShaderResourceView* g_scene_depth;
std::mutex g_scene_depth_lock;
uint32_t g_frame;

void release_targets() {
    auto drop = [](auto*& p) { if (p) p->Release(); p = nullptr; };
    drop(g_depth_tex);
    drop(g_depth_srv);
    for (auto*& u : g_depth_uav) drop(u);
    for (int i = 0; i < 2; i++) {
        drop(g_work_tex[i]);
        drop(g_work_srv[i]);
        drop(g_work_uav[i]);
    }
}

bool create_targets(UINT width, UINT height) {
    release_targets();
    D3D11_TEXTURE2D_DESC d{};
    d.Width = width;
    d.Height = height;
    d.MipLevels = kDepthMips;
    d.ArraySize = 1;
    d.Format = DXGI_FORMAT_R32_FLOAT;
    d.SampleDesc.Count = 1;
    d.Usage = D3D11_USAGE_DEFAULT;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, &g_depth_tex))) return false;
    if (FAILED(g_device->CreateShaderResourceView(g_depth_tex, nullptr, &g_depth_srv))) return false;
    for (UINT m = 0; m < kDepthMips; m++) {
        D3D11_UNORDERED_ACCESS_VIEW_DESC u{};
        u.Format = d.Format;
        u.ViewDimension = D3D11_UAV_DIMENSION_TEXTURE2D;
        u.Texture2D.MipSlice = m;
        if (FAILED(g_device->CreateUnorderedAccessView(g_depth_tex, &u, &g_depth_uav[m]))) return false;
    }
    d.MipLevels = 1;
    d.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    for (int i = 0; i < 2; i++) {
        if (FAILED(g_device->CreateTexture2D(&d, nullptr, &g_work_tex[i]))) return false;
        if (FAILED(g_device->CreateShaderResourceView(g_work_tex[i], nullptr, &g_work_srv[i]))) return false;
        if (FAILED(g_device->CreateUnorderedAccessView(g_work_tex[i], nullptr, &g_work_uav[i]))) return false;
    }
    g_width = width;
    g_height = height;
    logf("gtao: targets %ux%u", width, height);
    return true;
}

// Compute-stage state of the game's list, put back when the pass ends.
class ComputeBackup {
public:
    explicit ComputeBackup(ID3D11DeviceContext* ctx) : ctx_(ctx) {
        ctx->CSGetShader(&cs_, nullptr, nullptr);
        ctx->CSGetShaderResources(0, kSrvs, srv_);
        ctx->CSGetUnorderedAccessViews(0, kUavs, uav_);
        ctx->CSGetConstantBuffers(0, 1, &cb_);
        ctx->CSGetSamplers(0, 1, &smp_);
    }
    ~ComputeBackup() {
        ctx_->CSSetShader(cs_, nullptr, 0);
        ctx_->CSSetUnorderedAccessViews(0, kUavs, uav_, nullptr);
        ctx_->CSSetShaderResources(0, kSrvs, srv_);
        ctx_->CSSetConstantBuffers(0, 1, &cb_);
        ctx_->CSSetSamplers(0, 1, &smp_);
        if (cs_) cs_->Release();
        for (auto* p : srv_) if (p) p->Release();
        for (auto* p : uav_) if (p) p->Release();
        if (cb_) cb_->Release();
        if (smp_) smp_->Release();
    }
private:
    static constexpr UINT kSrvs = 5, kUavs = 5;
    ID3D11DeviceContext* ctx_;
    ID3D11ComputeShader* cs_ = nullptr;
    ID3D11ShaderResourceView* srv_[kSrvs] = {};
    ID3D11UnorderedAccessView* uav_[kUavs] = {};
    ID3D11Buffer* cb_ = nullptr;
    ID3D11SamplerState* smp_ = nullptr;
};

void clear_bindings(ID3D11DeviceContext* ctx) {
    ID3D11ShaderResourceView* no_srv[5] = {};
    ID3D11UnorderedAccessView* no_uav[5] = {};
    ctx->CSSetShaderResources(0, 5, no_srv);
    ctx->CSSetUnorderedAccessViews(0, 5, no_uav, nullptr);
}

}  // namespace

void gtao_init(ID3D11Device* device) {
    g_device = device;
    D3D11_BUFFER_DESC b{};
    b.ByteWidth = sizeof(Constants);
    b.Usage = D3D11_USAGE_DYNAMIC;
    b.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    b.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    device->CreateBuffer(&b, nullptr, &g_cb);
    D3D11_SAMPLER_DESC s{};
    s.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
    s.AddressU = s.AddressV = s.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    s.MaxLOD = D3D11_FLOAT32_MAX;
    device->CreateSamplerState(&s, &g_point_clamp);
}

void gtao_on_present() {
    g_frame++;
}

void gtao_note_depth(ID3D11DeviceContext* ctx) {
    ID3D11ShaderResourceView* srv = nullptr;
    ctx->CSGetShaderResources(0, 1, &srv);
    std::lock_guard<std::mutex> lock(g_scene_depth_lock);
    if (g_scene_depth) g_scene_depth->Release();
    g_scene_depth = srv;  // takes the reference from the getter
}

bool gtao_usable(const ViewConstants& view) {
    return view.valid && !g_targets_failed && hlsl_cs("gtao.hlsl", "depth_main") && hlsl_cs("gtao.hlsl", "ao_main") &&
           hlsl_cs("gtao.hlsl", "denoise_main");
}

bool gtao_run(ID3D11DeviceContext* ctx, const ViewConstants& view) {
    if (!view.valid) return false;
    ID3D11UnorderedAccessView* out = nullptr;
    ctx->CSGetUnorderedAccessViews(0, 1, &out);
    if (!out) return false;
    ID3D11Resource* out_res = nullptr;
    out->GetResource(&out_res);
    D3D11_TEXTURE2D_DESC out_desc{};
    static_cast<ID3D11Texture2D*>(out_res)->GetDesc(&out_desc);
    out_res->Release();

    ID3D11ShaderResourceView* depth = nullptr;
    {
        std::lock_guard<std::mutex> lock(g_scene_depth_lock);
        depth = g_scene_depth;
        if (depth) depth->AddRef();
    }
    auto* cs_depth = hlsl_cs("gtao.hlsl", "depth_main");
    auto* cs_main = hlsl_cs("gtao.hlsl", "ao_main");
    auto* cs_denoise = hlsl_cs("gtao.hlsl", "denoise_main");
    bool ready = depth && cs_depth && cs_main && cs_denoise;
    if (ready && (g_width != out_desc.Width || g_height != out_desc.Height)) {
        ready = create_targets(out_desc.Width, out_desc.Height);
        if (!ready && !g_targets_failed.exchange(true)) logf("gtao: targets of %ux%u failed; the game's own ambient occlusion runs instead", out_desc.Width, out_desc.Height);
    }
    if (!ready) {
        if (depth) depth->Release();
        out->Release();
        return false;
    }

    // Projection from the camera-relative view-projection rows: the x and y rows have the camera's right and
    // up axes scaled by P00 and P11, and z_ndc = z_a + z_b / view_z.
    const float* m = view.vp;
    Constants c{};
    c.size[0] = static_cast<float>(g_width);
    c.size[1] = static_cast<float>(g_height);
    c.inv_size[0] = 1.0f / c.size[0];
    c.inv_size[1] = 1.0f / c.size[1];
    c.p00 = std::sqrt(m[0] * m[0] + m[1] * m[1] + m[2] * m[2]);
    c.p11 = std::sqrt(m[4] * m[4] + m[5] * m[5] + m[6] * m[6]);
    float w_len = std::sqrt(m[12] * m[12] + m[13] * m[13] + m[14] * m[14]);
    float z_len = std::sqrt(m[8] * m[8] + m[9] * m[9] + m[10] * m[10]);
    c.z_a = w_len > 0 ? z_len / w_len : 1.0f;
    c.z_b = m[11];
    c.radius = cfg_float("gtao.radius", 1.6f);
    c.falloff = cfg_float("gtao.falloff", 0.6f);
    c.power = cfg_float("gtao.power", 1.35f);
    c.max_pixels = cfg_float("gtao.max_pixels", 220.0f);
    c.thickness = cfg_float("gtao.thickness", 0.35f);
    c.fade_start = cfg_float("gtao.fade_start", 120.0f);
    c.fade_end = cfg_float("gtao.fade_end", 400.0f);
    c.frame = static_cast<float>(g_frame % 1024);
    c.slices = cfg_int("gtao.slices", 4);
    c.steps = cfg_int("gtao.steps", 6);

    // Sun direction in view space, for the contact shadow march. The camera axes are the normalised x, y and w
    // rows of the camera-relative view-projection; the forward constants give the sun's direction of travel.
    const ForwardConstants forward = engine_forward_constants();
    if (forward.valid && c.p00 > 0 && c.p11 > 0 && w_len > 0) {
        const float* travel = forward.regs[73];
        const float lx = -travel[0], ly = -travel[1], lz = -travel[2];
        c.sun_view[0] = (m[0] * lx + m[1] * ly + m[2] * lz) / c.p00;
        c.sun_view[1] = (m[4] * lx + m[5] * ly + m[6] * lz) / c.p11;
        c.sun_view[2] = (m[12] * lx + m[13] * ly + m[14] * lz) / w_len;
        c.contact_length = cfg_float("gtao.contact_length", 0.7f);
        c.contact_fade = cfg_float("gtao.contact_fade", 50.0f);
    }

    D3D11_MAPPED_SUBRESOURCE mapped;
    if (FAILED(ctx->Map(g_cb, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) {
        depth->Release();
        out->Release();
        return false;
    }
    memcpy(mapped.pData, &c, sizeof(c));
    ctx->Unmap(g_cb, 0);

    {
        ComputeBackup backup(ctx);
        ctx->CSSetConstantBuffers(0, 1, &g_cb);
        ctx->CSSetSamplers(0, 1, &g_point_clamp);

        clear_bindings(ctx);
        ctx->CSSetShader(cs_depth, nullptr, 0);
        ctx->CSSetShaderResources(0, 1, &depth);
        ctx->CSSetUnorderedAccessViews(0, kDepthMips, g_depth_uav, nullptr);
        ctx->Dispatch((g_width + 15) / 16, (g_height + 15) / 16, 1);

        clear_bindings(ctx);
        ctx->CSSetShader(cs_main, nullptr, 0);
        ctx->CSSetShaderResources(0, 1, &g_depth_srv);
        ctx->CSSetUnorderedAccessViews(0, 1, &g_work_uav[0], nullptr);
        ctx->Dispatch((g_width + 7) / 8, (g_height + 7) / 8, 1);

        clear_bindings(ctx);
        ctx->CSSetShader(cs_denoise, nullptr, 0);
        ID3D11ShaderResourceView* srvs_a[2] = {g_depth_srv, g_work_srv[0]};
        ctx->CSSetShaderResources(0, 2, srvs_a);
        ctx->CSSetUnorderedAccessViews(0, 1, &g_work_uav[1], nullptr);
        ctx->Dispatch((g_width + 7) / 8, (g_height + 7) / 8, 1);

        clear_bindings(ctx);
        ID3D11ShaderResourceView* srvs_b[2] = {g_depth_srv, g_work_srv[1]};
        ctx->CSSetShaderResources(0, 2, srvs_b);
        ctx->CSSetUnorderedAccessViews(0, 1, &out, nullptr);
        ctx->Dispatch((g_width + 7) / 8, (g_height + 7) / 8, 1);
        clear_bindings(ctx);
    }
    depth->Release();
    out->Release();
    return true;
}
