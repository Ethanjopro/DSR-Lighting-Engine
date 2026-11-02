// d3d11.dll proxy: loads the system DLL, forwards every export, and takes over device creation.
#include "common.h"
#include "engine.h"
#include "hooks.h"
#include "shaderdb.h"
#include <d3d11.h>
#include <dxgi.h>
#include <mutex>
#include <string>

#include "exports_table.inc"

extern "C" {
void* g_real_table[DSRLE_NUM_EXPORTS];
void ensure_real();
}

static HMODULE g_real;
static std::once_flag g_once;

static void load_real() {
    wchar_t path[MAX_PATH];
    GetSystemDirectoryW(path, MAX_PATH);
    wcscat_s(path, L"\\d3d11.dll");
    g_real = LoadLibraryW(path);
    if (!g_real) {
        logf("could not load %S: %lu", path, GetLastError());
        return;
    }
    for (int i = 0; i < DSRLE_NUM_EXPORTS; i++) {
        g_real_table[i] = reinterpret_cast<void*>(GetProcAddress(g_real, kExportNames[i]));
    }
    logf("real d3d11 loaded from %S", path);
}

void ensure_real() {
    std::call_once(g_once, load_real);
}

static void* real_export(const char* name) {
    ensure_real();
    return g_real ? reinterpret_cast<void*>(GetProcAddress(g_real, name)) : nullptr;
}

// The game creates its factory itself and the swap chain later, so patch the factory class now.
// All IDXGIFactory objects of this class share the vtable, including the game's.
static void hook_factory_class() {
    static bool done;
    if (done) return;
    done = true;
    wchar_t path[MAX_PATH];
    GetSystemDirectoryW(path, MAX_PATH);
    wcscat_s(path, L"\\dxgi.dll");
    HMODULE dxgi = LoadLibraryW(path);
    if (!dxgi) return;
    using CreateFactoryFn = HRESULT(WINAPI*)(REFIID, void**);
    auto create = reinterpret_cast<CreateFactoryFn>(GetProcAddress(dxgi, "CreateDXGIFactory"));
    IDXGIFactory* factory = nullptr;
    if (create && SUCCEEDED(create(IID_PPV_ARGS(&factory)))) {
        hooks_install_factory(factory);
        factory->Release();
    }
}

// Only the first real device gets the engine. The game creates exactly one; a later device (an overlay's
// own, say) stays plain.
static void after_device(ID3D11Device* device, ID3D11DeviceContext** context) {
    static bool done;
    if (done || !device || !context || !*context) return;
    done = true;
    logf("device created: feature level %x, flags %x", device->GetFeatureLevel(), device->GetCreationFlags());
    shaderdb_load();
    engine_init(device);
    *context = hooks_install_device(device, *context);
    hook_factory_class();
}

extern "C" HRESULT WINAPI hook_D3D11CreateDevice(IDXGIAdapter* adapter, D3D_DRIVER_TYPE type, HMODULE software, UINT flags,
                                                 const D3D_FEATURE_LEVEL* levels, UINT num_levels, UINT sdk,
                                                 ID3D11Device** device, D3D_FEATURE_LEVEL* level, ID3D11DeviceContext** context) {
    log_init();
    using Fn = decltype(&D3D11CreateDevice);
    static auto real_fn = reinterpret_cast<Fn>(real_export("D3D11CreateDevice"));
    if (!real_fn) return E_FAIL;
    logf("D3D11CreateDevice type=%d flags=%x levels=%u", type, flags, num_levels);
    HRESULT hr = real_fn(adapter, type, software, flags, levels, num_levels, sdk, device, level, context);
    logf("D3D11CreateDevice -> %08lx", hr);
    if (SUCCEEDED(hr) && device && *device && context && *context) after_device(*device, context);
    return hr;
}

extern "C" HRESULT WINAPI hook_D3D11CreateDeviceAndSwapChain(IDXGIAdapter* adapter, D3D_DRIVER_TYPE type, HMODULE software, UINT flags,
                                                             const D3D_FEATURE_LEVEL* levels, UINT num_levels, UINT sdk,
                                                             const DXGI_SWAP_CHAIN_DESC* desc, IDXGISwapChain** swapchain,
                                                             ID3D11Device** device, D3D_FEATURE_LEVEL* level, ID3D11DeviceContext** context) {
    log_init();
    using Fn = decltype(&D3D11CreateDeviceAndSwapChain);
    static auto real_fn = reinterpret_cast<Fn>(real_export("D3D11CreateDeviceAndSwapChain"));
    if (!real_fn) return E_FAIL;
    logf("D3D11CreateDeviceAndSwapChain type=%d flags=%x", type, flags);
    HRESULT hr = real_fn(adapter, type, software, flags, levels, num_levels, sdk, desc, swapchain, device, level, context);
    if (SUCCEEDED(hr) && device && *device && context && *context) after_device(*device, context);
    if (SUCCEEDED(hr) && swapchain && *swapchain) hooks_install_swapchain(*swapchain);
    return hr;
}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(instance);
    return TRUE;
}
