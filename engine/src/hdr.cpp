// Settings in dsrle.ini. Starting points, not yet judged by eye on the game.
//
//   hdr.enabled = 1      1 = on when the display reports HDR, 0 = off. Takes full effect when the game starts, since the
//                        swap chain is made once. Set to 0 while running, the highlights go and the image stays in the
//                        HDR swap chain at paper white, which is a fair A/B against SDR
//   hdr.paper_white = 200  nits of SDR white, and so of the UI and everything below the composite's pivot. Live
//   hdr.peak_nits = 0      nits the highlights roll off toward; 0 = what the display reports. Live
//   hdr.warm_hue = 0.5     how much of the SDR hue fire keeps above paper white; 0 lets it go redder, 1 keeps all of it. Live
//
// What moving from the game's blit-model chain to flip model changes, and where each is dealt with:
// - The back buffer is float, so the game's 8-bit view descs would fail: hdr_fix_view_desc.
// - Its contents are undefined after Present. The last blit covers it every frame, so nothing depends on them. A Present
//   with no blit before it would show an old frame, and hdr_present_flags logs that if it ever happens.
// - It is unbound from the immediate context at Present. The last blit runs there and relies on the binding, so
//   hdr_after_present puts it back.
// - No MSAA, which leaves the game's own chain in place. Flags GDI cannot honour on float are dropped.
// - Windows Auto HDR only acts on SDR swap chains, so this replaces its guess rather than adding to it.
#include "hdr.h"
#include "common.h"
#include "config.h"
#include "engine.h"
#include "hlsl.h"
#include <dxgi1_6.h>
#include <algorithm>
#include <atomic>
#include <cstring>
#include <mutex>

namespace {

constexpr DXGI_FORMAT kFormat = DXGI_FORMAT_R16G16B16A16_FLOAT;
constexpr DXGI_COLOR_SPACE_TYPE kColorSpace = DXGI_COLOR_SPACE_RGB_FULL_G10_NONE_P709;
constexpr UINT kFlipForbidsFlags = DXGI_SWAP_CHAIN_FLAG_GDI_COMPATIBLE | DXGI_SWAP_CHAIN_FLAG_NONPREROTATED;
constexpr float kScRgbNits = 80.0f;
constexpr float kFallbackPeakNits = 400.0f;  // for a display that reports no luminance
constexpr float kLimitKnee = 0.75f;          // of the peak, where composite_hdr.hlsl's display limit begins
constexpr UINT kConstantsSlot = 4;           // b4, free in both passes
constexpr UINT kLutSamplerSlot = 2;          // s2, free in the blit
constexpr UINT kExtSrvSlot = 2;              // t2 and t3, free in the blit

// Must match the cbuffer in composite_hdr.hlsl and hdr_final.hlsl.
struct Constants {
    float peak;
    float knee;
    float scale;
    float has_ext;
    float warm_hue;
    float pad[3];
};

ID3D11Device* g_device;
std::atomic<bool> g_active{false};
IDXGISwapChain* g_swapchain;  // compared only: the vtable hooks see every swap chain in the process
HWND g_window;
std::atomic<float> g_display_peak{kFallbackPeakNits};
std::atomic<bool> g_display_is_hdr{true};
IDXGIFactory1* g_display_factory;
HMONITOR g_display_monitor;
int g_display_poll;
std::atomic<bool> g_blit_drawn{false};
std::atomic<bool> g_first_present_logged{false};
bool g_blit_ever_drawn;
int g_blitless_logged;

std::mutex g_lock;  // the composite's list can record on a worker while the blit runs on the immediate context
ID3D11Buffer* g_cb;
ID3D11BlendState* g_opaque;
ID3D11SamplerState* g_lut_sampler;
ID3D11Texture2D* g_ext_tex;
ID3D11Texture2D* g_sdr_tex;
ID3D11RenderTargetView* g_ext_rtv;
ID3D11RenderTargetView* g_sdr_rtv;
ID3D11ShaderResourceView* g_ext_srv;
ID3D11ShaderResourceView* g_sdr_srv;
ID3D11Resource* g_composite_target;  // compared only
UINT g_width, g_height;
DXGI_FORMAT g_target_format;
int g_ext_pending;  // composites drawn and not yet shown: a list can record before the last frame's blit runs

template <class T>
void drop(T*& p) {
    if (p) p->Release();
    p = nullptr;
}

// A fresh factory rather than the game's: this asks about the display as it is now. Loaded by name because the DLL
// does not link dxgi.lib.
bool find_output(HWND window, DXGI_OUTPUT_DESC1& out, IDXGIFactory1** keep = nullptr) {
    wchar_t path[MAX_PATH];
    GetSystemDirectoryW(path, MAX_PATH);
    wcscat_s(path, L"\\dxgi.dll");
    HMODULE dxgi = LoadLibraryW(path);
    if (!dxgi) return false;
    using CreateFactoryFn = HRESULT(WINAPI*)(REFIID, void**);
    auto create = reinterpret_cast<CreateFactoryFn>(GetProcAddress(dxgi, "CreateDXGIFactory1"));
    IDXGIFactory1* factory = nullptr;
    if (!create || FAILED(create(IID_PPV_ARGS(&factory)))) return false;
    const HMONITOR monitor = MonitorFromWindow(window, MONITOR_DEFAULTTOPRIMARY);
    bool found = false;
    IDXGIAdapter1* adapter = nullptr;
    for (UINT a = 0; !found && factory->EnumAdapters1(a, &adapter) == S_OK; a++) {
        IDXGIOutput* output = nullptr;
        for (UINT o = 0; !found && adapter->EnumOutputs(o, &output) == S_OK; o++) {
            IDXGIOutput6* output6 = nullptr;
            if (SUCCEEDED(output->QueryInterface(IID_PPV_ARGS(&output6)))) {
                DXGI_OUTPUT_DESC1 d;
                if (SUCCEEDED(output6->GetDesc1(&d)) && d.Monitor == monitor) {
                    out = d;
                    found = true;
                }
                output6->Release();
            }
            output->Release();
        }
        adapter->Release();
    }
    if (keep) {
        *keep = factory;
    } else {
        factory->Release();
    }
    return found;
}

bool wants_hdr(IUnknown* device, const DXGI_SWAP_CHAIN_DESC& desc) {
    if (!cfg_bool("engine.enabled", true) || !cfg_bool("hdr.enabled", true)) return false;
    // An overlay's own device and windows stay out of this.
    ID3D11Device* d3d = nullptr;
    if (!device || FAILED(device->QueryInterface(IID_PPV_ARGS(&d3d)))) return false;
    d3d->Release();
    if (d3d != g_device || (g_window && desc.OutputWindow != g_window)) return false;
    if (desc.SampleDesc.Count != 1) {
        logf("hdr: the game asked for %u samples, which flip model cannot do; staying SDR", desc.SampleDesc.Count);
        return false;
    }
    DXGI_OUTPUT_DESC1 output;
    if (!find_output(desc.OutputWindow, output)) {
        logf("hdr: no display found for the window; staying SDR");
        return false;
    }
    if (output.ColorSpace != DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020) {
        logf("hdr: display colour space %d is not HDR; staying SDR", output.ColorSpace);
        return false;
    }
    if (!hlsl_ps("hdr_final.hlsl", "main")) {
        logf("hdr: hdr_final.hlsl did not compile; staying SDR");
        return false;
    }
    g_display_peak.store(output.MaxLuminance > 0.0f ? output.MaxLuminance : kFallbackPeakNits, std::memory_order_relaxed);
    logf("hdr: display reports %.0f nits peak, %.0f full frame", output.MaxLuminance, output.MaxFullFrameLuminance);
    return true;
}

bool apply_color_space(IDXGISwapChain* swapchain) {
    IDXGISwapChain3* chain3 = nullptr;
    if (FAILED(swapchain->QueryInterface(IID_PPV_ARGS(&chain3)))) return false;
    UINT support = 0;
    bool ok = SUCCEEDED(chain3->CheckColorSpaceSupport(kColorSpace, &support)) && (support & DXGI_SWAP_CHAIN_COLOR_SPACE_SUPPORT_FLAG_PRESENT);
    ok = ok && SUCCEEDED(chain3->SetColorSpace1(kColorSpace));
    chain3->Release();
    return ok;
}

// Lets go of whatever keeps swap chain buffers alive on the immediate context.
void flush_immediate() {
    ID3D11DeviceContext* immediate = nullptr;
    g_device->GetImmediateContext(&immediate);
    if (!immediate) return;
    immediate->ClearState();
    immediate->Flush();
    immediate->Release();
}

bool create_shared_inner() {
    D3D11_BUFFER_DESC b{};
    b.ByteWidth = sizeof(Constants);
    b.Usage = D3D11_USAGE_DYNAMIC;
    b.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    b.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    if (FAILED(g_device->CreateBuffer(&b, nullptr, &g_cb))) return false;

    // The game's own state here has independent blend on, and says nothing useful about targets 1 and 2.
    D3D11_BLEND_DESC bl{};
    bl.RenderTarget[0].SrcBlend = D3D11_BLEND_ONE;
    bl.RenderTarget[0].DestBlend = D3D11_BLEND_ZERO;
    bl.RenderTarget[0].BlendOp = D3D11_BLEND_OP_ADD;
    bl.RenderTarget[0].SrcBlendAlpha = D3D11_BLEND_ONE;
    bl.RenderTarget[0].DestBlendAlpha = D3D11_BLEND_ZERO;
    bl.RenderTarget[0].BlendOpAlpha = D3D11_BLEND_OP_ADD;
    bl.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL;
    if (FAILED(g_device->CreateBlendState(&bl, &g_opaque))) return false;

    D3D11_SAMPLER_DESC s{};
    s.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    s.AddressU = s.AddressV = s.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    s.ComparisonFunc = D3D11_COMPARISON_NEVER;
    s.MaxLOD = D3D11_FLOAT32_MAX;
    return SUCCEEDED(g_device->CreateSamplerState(&s, &g_lut_sampler));
}

bool create_shared() {
    if (g_cb && g_opaque && g_lut_sampler) return true;
    if (create_shared_inner()) return true;
    drop(g_cb);
    drop(g_opaque);
    drop(g_lut_sampler);
    logf("hdr: pass state could not be created");
    return false;
}

void release_targets() {
    drop(g_ext_srv);
    drop(g_sdr_srv);
    drop(g_ext_rtv);
    drop(g_sdr_rtv);
    drop(g_ext_tex);
    drop(g_sdr_tex);
    g_width = g_height = 0;
}

bool create_target(UINT width, UINT height, DXGI_FORMAT format, ID3D11Texture2D** tex, ID3D11RenderTargetView** rtv, ID3D11ShaderResourceView** srv) {
    D3D11_TEXTURE2D_DESC t{};
    t.Width = width;
    t.Height = height;
    t.MipLevels = 1;
    t.ArraySize = 1;
    t.Format = format;
    t.SampleDesc.Count = 1;
    t.Usage = D3D11_USAGE_DEFAULT;
    t.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
    return SUCCEEDED(g_device->CreateTexture2D(&t, nullptr, tex)) && SUCCEEDED(g_device->CreateRenderTargetView(*tex, nullptr, rtv)) &&
           SUCCEEDED(g_device->CreateShaderResourceView(*tex, nullptr, srv));
}

// Sized and formatted after the composite's target each frame, so the game can resize it whenever it likes. A list
// that still refers to the old textures keeps them alive by itself.
bool match_targets(ID3D11RenderTargetView* rtv) {
    ID3D11Resource* res = nullptr;
    rtv->GetResource(&res);
    if (!res) return false;
    D3D11_RESOURCE_DIMENSION dimension = D3D11_RESOURCE_DIMENSION_UNKNOWN;
    res->GetType(&dimension);
    D3D11_TEXTURE2D_DESC desc{};
    if (dimension == D3D11_RESOURCE_DIMENSION_TEXTURE2D) static_cast<ID3D11Texture2D*>(res)->GetDesc(&desc);
    g_composite_target = res;
    res->Release();
    if (desc.Width == 0 || desc.SampleDesc.Count != 1) return false;
    D3D11_RENDER_TARGET_VIEW_DESC view;
    rtv->GetDesc(&view);
    if (desc.Width == g_width && desc.Height == g_height && view.Format == g_target_format && g_ext_rtv) return true;
    release_targets();
    if (!create_target(desc.Width, desc.Height, DXGI_FORMAT_R16G16B16A16_FLOAT, &g_ext_tex, &g_ext_rtv, &g_ext_srv) ||
        !create_target(desc.Width, desc.Height, view.Format, &g_sdr_tex, &g_sdr_rtv, &g_sdr_srv)) {
        logf("hdr: targets could not be created at %ux%u", desc.Width, desc.Height);
        release_targets();
        return false;
    }
    g_width = desc.Width;
    g_height = desc.Height;
    g_target_format = view.Format;
    logf("hdr: targets %ux%u", g_width, g_height);
    return true;
}

// The player can turn Windows HDR off or move the window to another monitor while the game runs. An SDR desktop clips
// scRGB at 1, so until a restart the image is mapped to fit that instead of blowing out.
void watch_display() {
    if (++g_display_poll % 60) return;
    const HMONITOR monitor = MonitorFromWindow(g_window, MONITOR_DEFAULTTOPRIMARY);
    if (g_display_factory && g_display_factory->IsCurrent() && monitor == g_display_monitor) return;
    drop(g_display_factory);
    g_display_monitor = monitor;
    DXGI_OUTPUT_DESC1 d;
    if (!find_output(g_window, d, &g_display_factory)) return;
    const bool hdr = d.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020;
    if (d.MaxLuminance > 0.0f) g_display_peak.store(d.MaxLuminance, std::memory_order_relaxed);
    if (g_display_is_hdr.exchange(hdr, std::memory_order_relaxed) != hdr) logf("hdr: display is %s now", hdr ? "HDR" : "SDR");
}

bool upload_constants(ID3D11DeviceContext* ctx, bool has_ext) {
    const bool hdr = g_display_is_hdr.load(std::memory_order_relaxed);
    const float paper = hdr ? std::max(cfg_float("hdr.paper_white", 200.0f), 1.0f) : kScRgbNits;
    const float set_peak = cfg_float("hdr.peak_nits", 0.0f);
    Constants c{};
    c.peak = hdr ? std::max((set_peak > 0.0f ? set_peak : g_display_peak.load(std::memory_order_relaxed)) / paper, 1.0f) : 1.0f;
    c.knee = std::max(c.peak * kLimitKnee, 1.0f);
    c.scale = paper / kScRgbNits;
    c.has_ext = has_ext ? 1.0f : 0.0f;
    c.warm_hue = std::clamp(cfg_float("hdr.warm_hue", 0.5f), 0.0f, 1.0f);
    D3D11_MAPPED_SUBRESOURCE mapped;
    if (FAILED(ctx->Map(g_cb, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) return false;
    memcpy(mapped.pData, &c, sizeof(c));
    ctx->Unmap(g_cb, 0);
    return true;
}

template <class Desc>
const Desc* fix_view_desc(ID3D11Resource* res, const Desc* desc, Desc* storage) {
    if (!g_active.load(std::memory_order_relaxed) || !res || !desc) return desc;
    if (desc->Format == kFormat || desc->Format == DXGI_FORMAT_UNKNOWN) return desc;
    D3D11_RESOURCE_DIMENSION dimension = D3D11_RESOURCE_DIMENSION_UNKNOWN;
    res->GetType(&dimension);
    if (dimension != D3D11_RESOURCE_DIMENSION_TEXTURE2D) return desc;
    D3D11_TEXTURE2D_DESC t;
    static_cast<ID3D11Texture2D*>(res)->GetDesc(&t);
    // A texture of this format takes no view of any other, so this cannot change a call that would have worked.
    if (t.Format != kFormat) return desc;
    *storage = *desc;
    storage->Format = kFormat;
    return storage;
}

}  // namespace

void hdr_init(ID3D11Device* device) {
    g_device = device;
}

bool hdr_is_final_blit(uint64_t hash_lo, uint64_t hash_hi) {
    return hash_lo == 0x4915b254c739f466ull && hash_hi == 0xee2460b79f289efcull;
}

HRESULT hdr_create_swapchain(IDXGIFactory* factory, IUnknown* device, DXGI_SWAP_CHAIN_DESC* desc, IDXGISwapChain** out, HdrCreateSwapChainFn create) {
    if (!g_device || !desc || !out || !wants_hdr(device, *desc)) {
        // The game replacing its own HDR chain with one that stays SDR.
        if (desc && desc->OutputWindow == g_window) {
            g_active.store(false, std::memory_order_relaxed);
            g_swapchain = nullptr;
        }
        return create(factory, device, desc, out);
    }
    DXGI_SWAP_CHAIN_DESC hdr = *desc;
    hdr.BufferDesc.Format = kFormat;
    hdr.BufferCount = std::max(desc->BufferCount, 2u);
    hdr.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
    hdr.Flags &= ~kFlipForbidsFlags;
    IDXGISwapChain* chain = nullptr;
    HRESULT hr = create(factory, device, &hdr, &chain);
    if (SUCCEEDED(hr) && apply_color_space(chain)) {
        *out = chain;
        g_swapchain = chain;
        g_window = desc->OutputWindow;
        g_active.store(true, std::memory_order_relaxed);
        logf("hdr: flip-model scRGB swap chain, %u buffers, flags %u", hdr.BufferCount, hdr.Flags);
        return hr;
    }
    logf("hdr: no flip-model scRGB swap chain (create %08lx, chain %p); staying SDR", static_cast<unsigned long>(hr), chain);
    // The window takes no blit-model chain until the flip-model one has really gone.
    if (chain) {
        chain->Release();
        flush_immediate();
    }
    g_active.store(false, std::memory_order_relaxed);
    g_swapchain = nullptr;
    return create(factory, device, desc, out);
}

HRESULT hdr_resize_buffers(IDXGISwapChain* swapchain, UINT count, UINT width, UINT height, DXGI_FORMAT format, UINT flags, HdrResizeBuffersFn resize) {
    if (!g_active.load(std::memory_order_relaxed) || swapchain != g_swapchain) return resize(swapchain, count, width, height, format, flags);
    // Count 0 keeps what is there.
    if (count == 1) count = 2;
    flags &= ~kFlipForbidsFlags;
    HRESULT hr = resize(swapchain, count, width, height, kFormat, flags);
    if (FAILED(hr)) {
        logf("hdr: ResizeBuffers failed %08lx", static_cast<unsigned long>(hr));
    } else if (!apply_color_space(swapchain)) {
        logf("hdr: colour space could not be set again after ResizeBuffers");
    }
    return hr;
}

UINT hdr_present_flags(IDXGISwapChain* swapchain, UINT sync, UINT flags) {
    if (!g_active.load(std::memory_order_relaxed) || swapchain != g_swapchain) return flags;
    if (!g_first_present_logged.exchange(true, std::memory_order_relaxed)) logf("hdr: first Present sync=%u flags=%x", sync, flags);
    watch_display();
    if (!(flags & DXGI_PRESENT_TEST)) {
        const bool drawn = g_blit_drawn.exchange(false, std::memory_order_relaxed);
        if (drawn) {
            g_blit_ever_drawn = true;
        } else if (g_blit_ever_drawn && g_blitless_logged < 8) {
            g_blitless_logged++;
            logf("hdr: a Present with no blit before it; flip model shows an old frame there");
        }
    }
    return flags & ~static_cast<UINT>(DXGI_PRESENT_DO_NOT_SEQUENCE);
}

ID3D11RenderTargetView* hdr_before_present(IDXGISwapChain* swapchain, ID3D11DeviceContext* immediate) {
    if (!g_active.load(std::memory_order_relaxed) || swapchain != g_swapchain) return nullptr;
    ID3D11RenderTargetView* target = nullptr;
    immediate->OMGetRenderTargets(1, &target, nullptr);
    return target;
}

void hdr_after_present(ID3D11DeviceContext* immediate, ID3D11RenderTargetView* target) {
    if (!target) return;
    immediate->OMSetRenderTargets(1, &target, nullptr);
    target->Release();
}

const D3D11_RENDER_TARGET_VIEW_DESC* hdr_fix_view_desc(ID3D11Resource* res, const D3D11_RENDER_TARGET_VIEW_DESC* desc, D3D11_RENDER_TARGET_VIEW_DESC* storage) {
    return fix_view_desc(res, desc, storage);
}

const D3D11_SHADER_RESOURCE_VIEW_DESC* hdr_fix_view_desc(ID3D11Resource* res, const D3D11_SHADER_RESOURCE_VIEW_DESC* desc, D3D11_SHADER_RESOURCE_VIEW_DESC* storage) {
    return fix_view_desc(res, desc, storage);
}

bool hdr_draw_composite(ID3D11DeviceContext* ctx, UINT count, UINT start) {
    if (!g_active.load(std::memory_order_relaxed)) return false;
    // With the game's own composite there is nothing to extend, and the blit shows its image at paper white. That is also
    // what the comparison key shows.
    if (!cfg_bool("hdr.enabled", true) || !engine_replace_ps(Role::Composite)) return false;
    ID3D11PixelShader* ps = hlsl_ps("composite_hdr.hlsl", "hdr_main");
    if (!ps) return false;

    std::lock_guard<std::mutex> lock(g_lock);
    ID3D11RenderTargetView* rtv[2] = {};
    ID3D11DepthStencilView* dsv = nullptr;
    ctx->OMGetRenderTargets(2, rtv, &dsv);
    bool drawn = false;
    if (rtv[0] && !rtv[1] && create_shared() && match_targets(rtv[0]) && upload_constants(ctx, false)) {
        ID3D11PixelShader* game_ps = nullptr;
        ID3D11Buffer* game_cb = nullptr;
        ID3D11BlendState* game_blend = nullptr;
        FLOAT game_factor[4];
        UINT game_mask = 0;
        ctx->PSGetShader(&game_ps, nullptr, nullptr);
        ctx->PSGetConstantBuffers(kConstantsSlot, 1, &game_cb);
        ctx->OMGetBlendState(&game_blend, game_factor, &game_mask);

        ID3D11RenderTargetView* targets[3] = {rtv[0], g_ext_rtv, g_sdr_rtv};
        ctx->OMSetRenderTargets(3, targets, dsv);
        ctx->OMSetBlendState(g_opaque, nullptr, 0xffffffffu);
        ctx->PSSetShader(ps, nullptr, 0);
        ctx->PSSetConstantBuffers(kConstantsSlot, 1, &g_cb);
        ctx->Draw(count, start);

        ctx->OMSetRenderTargets(1, rtv, dsv);
        ctx->OMSetBlendState(game_blend, game_factor, game_mask);
        ctx->PSSetShader(game_ps, nullptr, 0);
        ctx->PSSetConstantBuffers(kConstantsSlot, 1, &game_cb);
        drop(game_ps);
        drop(game_cb);
        drop(game_blend);
        g_ext_pending = std::min(g_ext_pending + 1, 2);  // a list that never ran must not outlast a blit or two
        drawn = true;
    }
    drop(rtv[0]);
    drop(rtv[1]);
    drop(dsv);
    return drawn;
}

bool hdr_draw_final(ID3D11DeviceContext* ctx, UINT count, UINT start) {
    if (!g_active.load(std::memory_order_relaxed)) return false;
    ID3D11PixelShader* ps = hlsl_ps("hdr_final.hlsl", "main");
    if (!ps) return false;

    std::lock_guard<std::mutex> lock(g_lock);
    if (!create_shared()) return false;
    // The extended image is only good for the composite target it was drawn with, and for the frame it was drawn in.
    bool has_ext = g_ext_pending > 0;
    if (has_ext) g_ext_pending--;
    ID3D11ShaderResourceView* input = nullptr;
    ctx->PSGetShaderResources(0, 1, &input);
    if (input) {
        ID3D11Resource* res = nullptr;
        input->GetResource(&res);
        has_ext = has_ext && res == g_composite_target;
        drop(res);
        drop(input);
    } else {
        has_ext = false;
    }
    if (!upload_constants(ctx, has_ext)) return false;

    ID3D11PixelShader* game_ps = nullptr;
    ID3D11Buffer* game_cb = nullptr;
    ID3D11SamplerState* game_sampler = nullptr;
    ID3D11ShaderResourceView* game_srv[2] = {};
    ctx->PSGetShader(&game_ps, nullptr, nullptr);
    ctx->PSGetConstantBuffers(kConstantsSlot, 1, &game_cb);
    ctx->PSGetSamplers(kLutSamplerSlot, 1, &game_sampler);
    ctx->PSGetShaderResources(kExtSrvSlot, 2, game_srv);

    ID3D11ShaderResourceView* inputs[2] = {has_ext ? g_ext_srv : nullptr, has_ext ? g_sdr_srv : nullptr};
    ctx->PSSetShader(ps, nullptr, 0);
    ctx->PSSetConstantBuffers(kConstantsSlot, 1, &g_cb);
    ctx->PSSetSamplers(kLutSamplerSlot, 1, &g_lut_sampler);
    ctx->PSSetShaderResources(kExtSrvSlot, 2, inputs);
    ctx->Draw(count, start);

    // Off the inputs again: these textures are targets again in the next frame's list.
    ctx->PSSetShaderResources(kExtSrvSlot, 2, game_srv);
    ctx->PSSetSamplers(kLutSamplerSlot, 1, &game_sampler);
    ctx->PSSetConstantBuffers(kConstantsSlot, 1, &game_cb);
    ctx->PSSetShader(game_ps, nullptr, 0);
    drop(game_ps);
    drop(game_cb);
    drop(game_sampler);
    drop(game_srv[0]);
    drop(game_srv[1]);
    g_blit_drawn.store(true, std::memory_order_relaxed);
    return true;
}
