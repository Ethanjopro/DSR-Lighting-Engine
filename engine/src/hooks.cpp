#include "hooks.h"
#include "casters.h"
#include "common.h"
#include "context.h"
#include "engine.h"
#include "hdr.h"
#include "pack.h"
#include "perf.h"
#include "shaderdb.h"
#include "slots.h"
#include "trace.h"
#include <cstring>

namespace {

ID3D11Device* g_device;
ID3D11DeviceContext* g_immediate_real;
ID3D11DeviceContext* g_immediate_wrapper;

template <class Fn>
void install(void** vt, int slot, void* hook, Fn& original) {
    if (void* prev = patch_slot(vt, slot, hook)) original = reinterpret_cast<Fn>(prev);
}

//------------------------------------------------------------------------------
// Device: shader identity, input layouts, and handing out wrapped contexts.
//------------------------------------------------------------------------------
#define DEVICE_SHADER_HOOK(Name, Iface, stage)                                                                       \
    using Name##_fn = HRESULT(STDMETHODCALLTYPE*)(ID3D11Device*, const void*, SIZE_T, ID3D11ClassLinkage*, Iface**); \
    Name##_fn o_##Name;                                                                                              \
    HRESULT STDMETHODCALLTYPE h_##Name(ID3D11Device* self, const void* code, SIZE_T size, ID3D11ClassLinkage* link,  \
                                       Iface** out) {                                                                \
        HRESULT hr = o_##Name(self, code, size, link, out);                                                          \
        if (SUCCEEDED(hr) && out && *out) shaderdb_register(*out, code, size, stage);                                \
        return hr;                                                                                                   \
    }

DEVICE_SHADER_HOOK(CreateVertexShader, ID3D11VertexShader, "vs")
DEVICE_SHADER_HOOK(CreateComputeShader, ID3D11ComputeShader, "cs")
DEVICE_SHADER_HOOK(CreateGeometryShader, ID3D11GeometryShader, "gs")
DEVICE_SHADER_HOOK(CreateHullShader, ID3D11HullShader, "hs")
DEVICE_SHADER_HOOK(CreateDomainShader, ID3D11DomainShader, "ds")

// Pixel shaders also get their engine replacement here, from the shader pack, so the wrapped contexts can swap
// between the two at runtime.
using CreatePixelShader_fn = HRESULT(STDMETHODCALLTYPE*)(ID3D11Device*, const void*, SIZE_T, ID3D11ClassLinkage*, ID3D11PixelShader**);
CreatePixelShader_fn o_CreatePixelShader;
HRESULT STDMETHODCALLTYPE h_CreatePixelShader(ID3D11Device* self, const void* code, SIZE_T size, ID3D11ClassLinkage* link, ID3D11PixelShader** out) {
    HRESULT hr = o_CreatePixelShader(self, code, size, link, out);
    if (FAILED(hr) || !out || !*out) return hr;
    shaderdb_register(*out, code, size, "ps");
    uint64_t lo, hi;
    memcpy(&lo, static_cast<const char*>(code) + 4, 8);
    memcpy(&hi, static_cast<const char*>(code) + 12, 8);
    size_t replacement_size = 0;
    if (const void* replacement = pack_find(lo, hi, &replacement_size)) {
        ID3D11PixelShader* shader = nullptr;
        if (SUCCEEDED(o_CreatePixelShader(self, replacement, replacement_size, nullptr, &shader))) {
            shaderdb_set_replacement(*out, shader);
        } else {
            logf("pack: replacement rejected for %016llx", static_cast<unsigned long long>(lo));
        }
    }
    return hr;
}

using CreateInputLayout_fn = HRESULT(STDMETHODCALLTYPE*)(ID3D11Device*, const D3D11_INPUT_ELEMENT_DESC*, UINT, const void*,
                                                         SIZE_T, ID3D11InputLayout**);
CreateInputLayout_fn o_CreateInputLayout;
HRESULT STDMETHODCALLTYPE h_CreateInputLayout(ID3D11Device* self, const D3D11_INPUT_ELEMENT_DESC* desc, UINT count,
                                              const void* code, SIZE_T size, ID3D11InputLayout** out) {
    HRESULT hr = o_CreateInputLayout(self, desc, count, code, size, out);
    if (SUCCEEDED(hr) && out && *out) {
        trace_note_input_layout(*out, desc, count);
        casters_note_input_layout(*out, desc, count);
    }
    return hr;
}

using CreateDeferredContext_fn = HRESULT(STDMETHODCALLTYPE*)(ID3D11Device*, UINT, ID3D11DeviceContext**);
CreateDeferredContext_fn o_CreateDeferredContext;
HRESULT STDMETHODCALLTYPE h_CreateDeferredContext(ID3D11Device* self, UINT flags, ID3D11DeviceContext** out) {
    HRESULT hr = o_CreateDeferredContext(self, flags, out);
    if (SUCCEEDED(hr) && out && *out) *out = context_wrap(*out);
    return hr;
}

using GetImmediateContext_fn = void(STDMETHODCALLTYPE*)(ID3D11Device*, ID3D11DeviceContext**);
GetImmediateContext_fn o_GetImmediateContext;
void STDMETHODCALLTYPE h_GetImmediateContext(ID3D11Device* self, ID3D11DeviceContext** out) {
    if (self == g_device && g_immediate_wrapper && out) {
        g_immediate_wrapper->AddRef();
        *out = g_immediate_wrapper;
        return;
    }
    o_GetImmediateContext(self, out);
}

// The game asks for 8-bit views of its back buffer, which is float while HDR is on.
using CreateRenderTargetView_fn = HRESULT(STDMETHODCALLTYPE*)(ID3D11Device*, ID3D11Resource*, const D3D11_RENDER_TARGET_VIEW_DESC*, ID3D11RenderTargetView**);
CreateRenderTargetView_fn o_CreateRenderTargetView;
HRESULT STDMETHODCALLTYPE h_CreateRenderTargetView(ID3D11Device* self, ID3D11Resource* res, const D3D11_RENDER_TARGET_VIEW_DESC* desc, ID3D11RenderTargetView** out) {
    D3D11_RENDER_TARGET_VIEW_DESC fixed;
    return o_CreateRenderTargetView(self, res, hdr_fix_view_desc(res, desc, &fixed), out);
}

using CreateShaderResourceView_fn = HRESULT(STDMETHODCALLTYPE*)(ID3D11Device*, ID3D11Resource*, const D3D11_SHADER_RESOURCE_VIEW_DESC*, ID3D11ShaderResourceView**);
CreateShaderResourceView_fn o_CreateShaderResourceView;
HRESULT STDMETHODCALLTYPE h_CreateShaderResourceView(ID3D11Device* self, ID3D11Resource* res, const D3D11_SHADER_RESOURCE_VIEW_DESC* desc, ID3D11ShaderResourceView** out) {
    D3D11_SHADER_RESOURCE_VIEW_DESC fixed;
    return o_CreateShaderResourceView(self, res, hdr_fix_view_desc(res, desc, &fixed), out);
}

//------------------------------------------------------------------------------
// Swap chain: frame boundary.
//------------------------------------------------------------------------------
using Present_fn = HRESULT(STDMETHODCALLTYPE*)(IDXGISwapChain*, UINT, UINT);
Present_fn o_Present;
HRESULT STDMETHODCALLTYPE h_Present(IDXGISwapChain* self, UINT sync, UINT flags) {
    trace_before_present(self, g_immediate_real);
    trace_shot_poll(self, g_immediate_real, g_device);
    perf_before_present(g_immediate_real);
    ID3D11RenderTargetView* hdr_target = hdr_before_present(self, g_immediate_real);
    HRESULT hr = o_Present(self, sync, hdr_present_flags(self, sync, flags));
    hdr_after_present(g_immediate_real, hdr_target);
    if (FAILED(hr)) {
        static int s_failed_logged;
        if (s_failed_logged < 8) {
            s_failed_logged++;
            logf("Present failed %08lx", static_cast<unsigned long>(hr));
        }
    }
    perf_after_present(g_immediate_real);
    trace_after_present(g_device);
    engine_on_present(g_immediate_real);
    return hr;
}

using ResizeBuffers_fn = HRESULT(STDMETHODCALLTYPE*)(IDXGISwapChain*, UINT, UINT, UINT, DXGI_FORMAT, UINT);
ResizeBuffers_fn o_ResizeBuffers;
HRESULT STDMETHODCALLTYPE h_ResizeBuffers(IDXGISwapChain* self, UINT count, UINT w, UINT h, DXGI_FORMAT fmt, UINT flags) {
    logf("ResizeBuffers count=%u %ux%u fmt=%d flags=%u", count, w, h, fmt, flags);
    return hdr_resize_buffers(self, count, w, h, fmt, flags, o_ResizeBuffers);
}

using CreateSwapChain_fn = HRESULT(STDMETHODCALLTYPE*)(IDXGIFactory*, IUnknown*, DXGI_SWAP_CHAIN_DESC*, IDXGISwapChain**);
CreateSwapChain_fn o_CreateSwapChain;
HRESULT STDMETHODCALLTYPE h_CreateSwapChain(IDXGIFactory* self, IUnknown* device, DXGI_SWAP_CHAIN_DESC* desc, IDXGISwapChain** out) {
    HRESULT hr = hdr_create_swapchain(self, device, desc, out, o_CreateSwapChain);
    if (desc) {
        logf("CreateSwapChain hr=%08lx %ux%u fmt=%d count=%u windowed=%d swapeffect=%d flags=%u", hr, desc->BufferDesc.Width,
             desc->BufferDesc.Height, desc->BufferDesc.Format, desc->BufferCount, desc->Windowed, desc->SwapEffect, desc->Flags);
    }
    if (SUCCEEDED(hr) && out && *out) hooks_install_swapchain(*out);
    return hr;
}

}  // namespace

void hooks_install_factory(IDXGIFactory* factory) {
    install(vtable_of(factory), slot_factory_CreateSwapChain, reinterpret_cast<void*>(&h_CreateSwapChain), o_CreateSwapChain);
    logf("factory hooked");
}

void hooks_install_swapchain(IDXGISwapChain* swapchain) {
    void** vt = vtable_of(swapchain);
    install(vt, slot_swapchain_Present, reinterpret_cast<void*>(&h_Present), o_Present);
    install(vt, slot_swapchain_ResizeBuffers, reinterpret_cast<void*>(&h_ResizeBuffers), o_ResizeBuffers);
    logf("swap chain hooked");
}

ID3D11DeviceContext* hooks_install_device(ID3D11Device* device, ID3D11DeviceContext* context) {
    g_device = device;
    g_immediate_real = context;
    // The wrapper takes over the caller's reference to the real context, and we keep one on the wrapper
    // for the life of the process, so GetImmediateContext can always hand it out.
    g_immediate_wrapper = context_wrap(context);
    g_immediate_wrapper->AddRef();

    void** dv = vtable_of(device);
#define HOOK_DEV(name) install(dv, slot_dev_##name, reinterpret_cast<void*>(&h_##name), o_##name)
    HOOK_DEV(CreateVertexShader);
    HOOK_DEV(CreatePixelShader);
    HOOK_DEV(CreateComputeShader);
    HOOK_DEV(CreateGeometryShader);
    HOOK_DEV(CreateHullShader);
    HOOK_DEV(CreateDomainShader);
    HOOK_DEV(CreateInputLayout);
    HOOK_DEV(CreateDeferredContext);
    HOOK_DEV(GetImmediateContext);
    HOOK_DEV(CreateRenderTargetView);
    HOOK_DEV(CreateShaderResourceView);
#undef HOOK_DEV
    logf("device %p hooked, context %p wrapped", device, context);
    return g_immediate_wrapper;
}
