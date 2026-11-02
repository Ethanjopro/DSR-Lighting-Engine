#include "bloom.h"
#include "common.h"
#include "config.h"
#include "hlsl.h"
#include "state.h"
#include <algorithm>
#include <cstring>

namespace {

constexpr UINT kLevels = 7;
constexpr UINT kResultSlot = 4;  // t4, unused by composite.hlsl's other inputs

struct Constants {
    float src_texel[2], dst_texel[2];
    float scatter, strength, first, pad;
};
static_assert(sizeof(Constants) % 16 == 0);

struct Level {
    UINT width = 0, height = 0;
    ID3D11Texture2D* tex = nullptr;
    ID3D11ShaderResourceView* srv = nullptr;
    ID3D11UnorderedAccessView* uav = nullptr;
};

ID3D11Device* g_device;
ID3D11Buffer* g_cb;
ID3D11SamplerState* g_linear_clamp;
Level g_down[kLevels];
Level g_up[kLevels - 1];
UINT g_width, g_height;

void release_level(Level& l) {
    auto drop = [](auto*& p) { if (p) p->Release(); p = nullptr; };
    drop(l.tex);
    drop(l.srv);
    drop(l.uav);
    l.width = l.height = 0;
}

void release_targets() {
    for (auto& l : g_down) release_level(l);
    for (auto& l : g_up) release_level(l);
    g_width = g_height = 0;
}

bool create_level(Level& l, UINT width, UINT height) {
    D3D11_TEXTURE2D_DESC d{};
    d.Width = width;
    d.Height = height;
    d.MipLevels = 1;
    d.ArraySize = 1;
    d.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;  // the blur is wide and dark, where R11G11B10 would band and tint
    d.SampleDesc.Count = 1;
    d.Usage = D3D11_USAGE_DEFAULT;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, &l.tex))) return false;
    if (FAILED(g_device->CreateShaderResourceView(l.tex, nullptr, &l.srv))) return false;
    if (FAILED(g_device->CreateUnorderedAccessView(l.tex, nullptr, &l.uav))) return false;
    l.width = width;
    l.height = height;
    return true;
}

bool create_targets(UINT width, UINT height) {
    release_targets();
    UINT w = width, h = height;
    for (UINT i = 0; i < kLevels; i++) {
        w = std::max(w / 2, 1u);
        h = std::max(h / 2, 1u);
        if (!create_level(g_down[i], w, h)) return false;
        if (i + 1 < kLevels && !create_level(g_up[i], w, h)) return false;
    }
    g_width = width;
    g_height = height;
    logf("bloom: targets from %ux%u", width, height);
    return true;
}

bool upload(ID3D11DeviceContext* ctx, const Constants& c) {
    D3D11_MAPPED_SUBRESOURCE mapped;
    if (FAILED(ctx->Map(g_cb, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) return false;
    memcpy(mapped.pData, &c, sizeof(c));
    ctx->Unmap(g_cb, 0);
    return true;
}

void pass(ID3D11DeviceContext* ctx, ID3D11ComputeShader* cs, ID3D11ShaderResourceView* src, ID3D11ShaderResourceView* low, const Level& dst) {
    ID3D11ShaderResourceView* no_srv[2] = {};
    ID3D11UnorderedAccessView* no_uav = nullptr;
    ctx->CSSetUnorderedAccessViews(0, 1, &no_uav, nullptr);
    ctx->CSSetShaderResources(0, 2, no_srv);
    ID3D11ShaderResourceView* srvs[2] = {src, low};
    ctx->CSSetShader(cs, nullptr, 0);
    ctx->CSSetShaderResources(0, 2, srvs);
    ctx->CSSetUnorderedAccessViews(0, 1, &dst.uav, nullptr);
    ctx->Dispatch((dst.width + 7) / 8, (dst.height + 7) / 8, 1);
}

}  // namespace

void bloom_init(ID3D11Device* device) {
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
    s.MaxLOD = D3D11_FLOAT32_MAX;
    device->CreateSamplerState(&s, &g_linear_clamp);
}

void bloom_run(ID3D11DeviceContext* ctx) {
    const float strength = cfg_float("bloom.strength", 0.08f);
    if (!cfg_bool("bloom.enabled", true) || strength <= 0.0f || !g_cb || !g_linear_clamp) return;
    auto* cs_down = hlsl_cs("bloom.hlsl", "down_main");
    auto* cs_up = hlsl_cs("bloom.hlsl", "up_main");
    if (!cs_down || !cs_up) return;

    ID3D11ShaderResourceView* scene = nullptr;
    ctx->PSGetShaderResources(0, 1, &scene);
    if (!scene) return;
    ID3D11Resource* res = nullptr;
    scene->GetResource(&res);
    D3D11_RESOURCE_DIMENSION dimension = D3D11_RESOURCE_DIMENSION_UNKNOWN;
    res->GetType(&dimension);
    D3D11_TEXTURE2D_DESC desc{};
    if (dimension == D3D11_RESOURCE_DIMENSION_TEXTURE2D) static_cast<ID3D11Texture2D*>(res)->GetDesc(&desc);
    res->Release();
    if (desc.Width == 0 || ((desc.Width != g_width || desc.Height != g_height) && !create_targets(desc.Width, desc.Height))) {
        scene->Release();
        return;
    }

    Constants c{};
    c.scatter = std::clamp(cfg_float("bloom.scatter", 0.75f), 0.0f, 1.0f);
    c.strength = strength;
    bool ok = true;
    {
        StateBackup backup(ctx);
        unbind_all(ctx);
        ctx->CSSetConstantBuffers(0, 1, &g_cb);
        ctx->CSSetSamplers(0, 1, &g_linear_clamp);

        UINT src_w = desc.Width, src_h = desc.Height;
        ID3D11ShaderResourceView* src = scene;
        for (UINT i = 0; ok && i < kLevels; i++) {
            const Level& dst = g_down[i];
            c.src_texel[0] = 1.0f / src_w;
            c.src_texel[1] = 1.0f / src_h;
            c.dst_texel[0] = 1.0f / dst.width;
            c.dst_texel[1] = 1.0f / dst.height;
            c.first = i == 0 ? 1.0f : 0.0f;
            ok = upload(ctx, c);
            if (ok) pass(ctx, cs_down, src, nullptr, dst);
            src = dst.srv;
            src_w = dst.width;
            src_h = dst.height;
        }
        ID3D11ShaderResourceView* low = g_down[kLevels - 1].srv;
        UINT low_w = g_down[kLevels - 1].width, low_h = g_down[kLevels - 1].height;
        for (int i = static_cast<int>(kLevels) - 2; ok && i >= 0; i--) {
            const Level& dst = g_up[i];
            c.src_texel[0] = 1.0f / low_w;
            c.src_texel[1] = 1.0f / low_h;
            c.dst_texel[0] = 1.0f / dst.width;
            c.dst_texel[1] = 1.0f / dst.height;
            ok = upload(ctx, c);
            if (ok) pass(ctx, cs_up, g_down[i].srv, low, dst);
            low = dst.srv;
            low_w = dst.width;
            low_h = dst.height;
        }
        ID3D11ShaderResourceView* no_srv[2] = {};
        ID3D11UnorderedAccessView* no_uav = nullptr;
        ctx->CSSetUnorderedAccessViews(0, 1, &no_uav, nullptr);
        ctx->CSSetShaderResources(0, 2, no_srv);
    }
    scene->Release();
    if (ok) ctx->PSSetShaderResources(kResultSlot, 1, &g_up[0].srv);
}

void bloom_unbind(ID3D11DeviceContext* ctx) {
    ID3D11ShaderResourceView* none = nullptr;
    ctx->PSSetShaderResources(kResultSlot, 1, &none);
}
