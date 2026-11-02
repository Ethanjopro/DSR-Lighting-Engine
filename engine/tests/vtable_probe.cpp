// Standalone probe: how does this machine's d3d11.dll lay out context vtables?
#include <d3d11.h>
#include <cstdio>
#include <initializer_list>

static void describe(const char* label, void* obj) {
    void** vt = *static_cast<void***>(obj);
    HMODULE vt_mod = nullptr;
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT, reinterpret_cast<LPCWSTR>(vt), &vt_mod);
    wchar_t name[MAX_PATH] = L"(heap)";
    if (vt_mod) GetModuleFileNameW(vt_mod, name, MAX_PATH);
    printf("%-10s obj=%p vtable=%p (obj%+lld) in %ls\n", label, obj, vt, static_cast<long long>(reinterpret_cast<char*>(vt) - static_cast<char*>(obj)), name);
    for (int slot : {0, 3, 8, 9, 12, 13, 58, 113, 114}) {
        void* fn = vt[slot];
        HMODULE m = nullptr;
        GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT, reinterpret_cast<LPCWSTR>(fn), &m);
        wchar_t mn[MAX_PATH] = L"(heap)";
        if (m) GetModuleFileNameW(m, mn, MAX_PATH);
        printf("    slot %3d -> %p %ls\n", slot, fn, mn);
    }
}

int main() {
    ID3D11Device* dev = nullptr;
    ID3D11DeviceContext* imm = nullptr;
    D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
    HRESULT hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &imm);
    printf("create: %08lx\n", hr);
    if (FAILED(hr)) return 1;
    describe("device", dev);
    describe("immediate", imm);
    for (int i = 0; i < 3; i++) {
        ID3D11DeviceContext* def = nullptr;
        hr = dev->CreateDeferredContext(0, &def);
        printf("deferred %d: %08lx\n", i, hr);
        if (SUCCEEDED(hr)) describe("deferred", def);
    }
    D3D11_FEATURE_DATA_THREADING th{};
    dev->CheckFeatureSupport(D3D11_FEATURE_THREADING, &th, sizeof(th));
    printf("DriverConcurrentCreates=%d DriverCommandLists=%d\n", th.DriverConcurrentCreates, th.DriverCommandLists);
    return 0;
}
