// Standalone smoke test for the proxy DLL: links against d3d11.lib, runs with the proxy beside the exe.
// Creates a device and swap chain, records a deferred list that clears a render target and draws, executes
// it, presents, and triggers one frame trace.
#include <d3d11_1.h>
#include <dxgi.h>
#include <cstdio>
#include <cstring>

static const char* kVs = "float4 main(uint id : SV_VertexID) : SV_Position { float2 p = float2((id << 1) & 2, id & 2); return float4(p * float2(2, -2) + float2(-1, 1), 0, 1); }";
static const char* kPs = "cbuffer C : register(b0) { float4 col; }; float4 main() : SV_Target { return col; }";

using D3DCompileFn = HRESULT(WINAPI*)(LPCVOID, SIZE_T, LPCSTR, const void*, void*, LPCSTR, LPCSTR, UINT, UINT, ID3DBlob**, ID3DBlob**);

int main() {
    HMODULE compiler = LoadLibraryW(L"d3dcompiler_47.dll");
    auto compile = reinterpret_cast<D3DCompileFn>(GetProcAddress(compiler, "D3DCompile"));

    IDXGIFactory* factory = nullptr;
    CreateDXGIFactory(IID_PPV_ARGS(&factory));

    ID3D11Device* dev = nullptr;
    ID3D11DeviceContext* imm = nullptr;
    D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
    HRESULT hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, &fl, 1, D3D11_SDK_VERSION, &dev, nullptr, &imm);
    printf("device %08lx\n", hr);
    if (FAILED(hr)) return 1;

    HWND wnd = CreateWindowExW(0, L"STATIC", L"smoke", WS_OVERLAPPEDWINDOW, 0, 0, 320, 240, nullptr, nullptr, nullptr, nullptr);
    DXGI_SWAP_CHAIN_DESC sd{};
    sd.BufferDesc.Width = 320;
    sd.BufferDesc.Height = 240;
    sd.BufferDesc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    sd.SampleDesc.Count = 1;
    sd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    sd.BufferCount = 2;
    sd.OutputWindow = wnd;
    sd.Windowed = TRUE;
    sd.SwapEffect = DXGI_SWAP_EFFECT_DISCARD;
    IDXGISwapChain* swap = nullptr;
    hr = factory->CreateSwapChain(dev, &sd, &swap);
    printf("swapchain %08lx\n", hr);
    if (FAILED(hr)) return 1;

    ID3D11Texture2D* bb = nullptr;
    swap->GetBuffer(0, IID_PPV_ARGS(&bb));
    ID3D11RenderTargetView* rtv = nullptr;
    dev->CreateRenderTargetView(bb, nullptr, &rtv);

    ID3DBlob* vsb = nullptr, *psb = nullptr, *err = nullptr;
    compile(kVs, strlen(kVs), "vs", nullptr, nullptr, "main", "vs_5_0", 0, 0, &vsb, &err);
    compile(kPs, strlen(kPs), "ps", nullptr, nullptr, "main", "ps_5_0", 0, 0, &psb, &err);
    ID3D11VertexShader* vs = nullptr;
    ID3D11PixelShader* ps = nullptr;
    dev->CreateVertexShader(vsb->GetBufferPointer(), vsb->GetBufferSize(), nullptr, &vs);
    dev->CreatePixelShader(psb->GetBufferPointer(), psb->GetBufferSize(), nullptr, &ps);

    D3D11_BUFFER_DESC bd{};
    bd.ByteWidth = 16;
    bd.Usage = D3D11_USAGE_DYNAMIC;
    bd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    bd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    ID3D11Buffer* cb = nullptr;
    dev->CreateBuffer(&bd, nullptr, &cb);

    ID3D11DeviceContext* def = nullptr;
    hr = dev->CreateDeferredContext(0, &def);
    printf("deferred %08lx\n", hr);

    ID3D11DeviceContext* imm2 = nullptr;
    dev->GetImmediateContext(&imm2);
    printf("GetImmediateContext same as created: %d\n", imm2 == imm);
    imm2->Release();

    ID3D11DeviceContext1* imm1 = nullptr;
    printf("QI ID3D11DeviceContext1: %08lx\n", imm->QueryInterface(IID_PPV_ARGS(&imm1)));
    if (imm1) imm1->Release();

    // Ask the DLL for a trace partway through.
    CreateDirectoryW(L"dsrle", nullptr);
    for (int frame = 0; frame < 40; frame++) {
        if (frame == 4) {
            FILE* f = nullptr;
            _wfopen_s(&f, L"dsrle\\trigger_trace", L"wb");
            if (f) { fputc('f', f); fclose(f); }
        }
        D3D11_MAPPED_SUBRESOURCE m;
        def->Map(cb, 0, D3D11_MAP_WRITE_DISCARD, 0, &m);
        float col[4] = {frame / 40.0f, 0.2f, 0.4f, 1.0f};
        memcpy(m.pData, col, sizeof(col));
        def->Unmap(cb, 0);

        const float clear[4] = {0.1f, 0.1f, 0.1f, 1};
        def->ClearRenderTargetView(rtv, clear);
        def->OMSetRenderTargets(1, &rtv, nullptr);
        D3D11_VIEWPORT vp{0, 0, 320, 240, 0, 1};
        def->RSSetViewports(1, &vp);
        def->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        def->VSSetShader(vs, nullptr, 0);
        def->PSSetShader(ps, nullptr, 0);
        def->PSSetConstantBuffers(0, 1, &cb);
        def->Draw(3, 0);
        ID3D11CommandList* list = nullptr;
        def->FinishCommandList(FALSE, &list);
        imm->ExecuteCommandList(list, FALSE);
        list->Release();
        swap->Present(0, 0);
    }
    def->Release();
    printf("done\n");
    return 0;
}
