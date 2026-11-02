// Reports each display output's colour space, luminance range and the desktop's SDR white level.
#include <dxgi1_6.h>
#include <cstdio>

int main() {
    IDXGIFactory6* factory = nullptr;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory)))) return 1;
    IDXGIAdapter1* adapter = nullptr;
    for (UINT a = 0; factory->EnumAdapters1(a, &adapter) == S_OK; a++) {
        DXGI_ADAPTER_DESC1 ad;
        adapter->GetDesc1(&ad);
        IDXGIOutput* output = nullptr;
        for (UINT o = 0; adapter->EnumOutputs(o, &output) == S_OK; o++) {
            IDXGIOutput6* out6 = nullptr;
            if (SUCCEEDED(output->QueryInterface(IID_PPV_ARGS(&out6)))) {
                DXGI_OUTPUT_DESC1 d;
                out6->GetDesc1(&d);
                printf("%ls | %ls | %ldx%ld | colorspace=%d (%s) bits=%u minL=%.3f maxL=%.1f maxFullFrame=%.1f attached=%d\n",
                       ad.Description, d.DeviceName, d.DesktopCoordinates.right - d.DesktopCoordinates.left, d.DesktopCoordinates.bottom - d.DesktopCoordinates.top,
                       d.ColorSpace, d.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020 ? "HDR10 on" : d.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709 ? "SDR" : "other",
                       d.BitsPerColor, d.MinLuminance, d.MaxLuminance, d.MaxFullFrameLuminance, d.AttachedToDesktop);
                out6->Release();
            }
            output->Release();
        }
        adapter->Release();
    }
    factory->Release();
    return 0;
}
