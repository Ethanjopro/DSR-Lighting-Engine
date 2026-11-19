// Native HDR output. On an HDR desktop the swap chain becomes flip-model scRGB, the composite also writes an extended
// version of its image, and the game's last blit is replaced by one that shows that image, with the UI at paper white.
//
// Every call here does nothing but forward until hdr_create_swapchain has really made an HDR swap chain, so on an SDR
// desktop, with hdr.enabled = 0, or after any failure, the game's own path runs untouched.
#pragma once
#include <d3d11.h>
#include <dxgi.h>
#include <cstdint>

using HdrCreateSwapChainFn = HRESULT(STDMETHODCALLTYPE*)(IDXGIFactory*, IUnknown*, DXGI_SWAP_CHAIN_DESC*, IDXGISwapChain**);
using HdrResizeBuffersFn = HRESULT(STDMETHODCALLTYPE*)(IDXGISwapChain*, UINT, UINT, UINT, DXGI_FORMAT, UINT);

void hdr_init(ID3D11Device* device);

// Identifies the game's last blit, which is not in the shader archives, by its DXBC checksum.
bool hdr_is_final_blit(uint64_t hash_lo, uint64_t hash_hi);

// Stand in for the real calls, which are passed in. If the HDR swap chain cannot be made, the game's own desc is used.
HRESULT hdr_create_swapchain(IDXGIFactory* factory, IUnknown* device, DXGI_SWAP_CHAIN_DESC* desc, IDXGISwapChain** out, HdrCreateSwapChainFn create);
HRESULT hdr_resize_buffers(IDXGISwapChain* swapchain, UINT count, UINT width, UINT height, DXGI_FORMAT format, UINT flags, HdrResizeBuffersFn resize);
// Once per Present, before it: the flags to present with.
UINT hdr_present_flags(IDXGISwapChain* swapchain, UINT sync, UINT flags);
// Flip model unbinds the back buffer at Present, and the last blit runs on the immediate context with whatever is bound
// there. Returns what to bind again, with a reference, or null. The second call takes it whether or not Present worked.
ID3D11RenderTargetView* hdr_before_present(IDXGISwapChain* swapchain, ID3D11DeviceContext* immediate);
void hdr_after_present(ID3D11DeviceContext* immediate, ID3D11RenderTargetView* target);

// For a notice drawn on the back buffer. False for the game's own 8-bit one; true for scRGB, where *white is paper white.
bool hdr_notice_white(IDXGISwapChain* swapchain, float* white);

// The game asks for 8-bit views of its back buffer, which fail on a float one. Returns the desc to create the view with.
const D3D11_RENDER_TARGET_VIEW_DESC* hdr_fix_view_desc(ID3D11Resource* res, const D3D11_RENDER_TARGET_VIEW_DESC* desc, D3D11_RENDER_TARGET_VIEW_DESC* storage);
const D3D11_SHADER_RESOURCE_VIEW_DESC* hdr_fix_view_desc(ID3D11Resource* res, const D3D11_SHADER_RESOURCE_VIEW_DESC* desc, D3D11_SHADER_RESOURCE_VIEW_DESC* storage);

// At the composite's and the last blit's draws, with the game's state bound. True means the draw was made here, with
// state left as the game set it, and the game's must not run.
bool hdr_draw_composite(ID3D11DeviceContext* ctx, UINT count, UINT start);
bool hdr_draw_final(ID3D11DeviceContext* ctx, UINT count, UINT start);
