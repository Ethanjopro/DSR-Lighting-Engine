// Save and restore the pipeline state around an engine-inserted pass, so the game never sees it.
#pragma once
#include <d3d11.h>

class StateBackup {
public:
    explicit StateBackup(ID3D11DeviceContext* ctx);
    ~StateBackup();
    StateBackup(const StateBackup&) = delete;
    StateBackup& operator=(const StateBackup&) = delete;

private:
    static constexpr UINT kSrvs = 32;
    static constexpr UINT kCbs = 6;
    static constexpr UINT kSamplers = 16;
    static constexpr UINT kUavs = 8;

    ID3D11DeviceContext* ctx_;

    ID3D11InputLayout* il_ = nullptr;
    D3D11_PRIMITIVE_TOPOLOGY topo_ = D3D11_PRIMITIVE_TOPOLOGY_UNDEFINED;
    ID3D11Buffer* vb_[2] = {};
    UINT vb_stride_[2] = {};
    UINT vb_offset_[2] = {};
    ID3D11Buffer* ib_ = nullptr;
    DXGI_FORMAT ib_format_ = DXGI_FORMAT_UNKNOWN;
    UINT ib_offset_ = 0;

    ID3D11VertexShader* vs_ = nullptr;
    ID3D11GeometryShader* gs_ = nullptr;
    ID3D11PixelShader* ps_ = nullptr;
    ID3D11ComputeShader* cs_ = nullptr;

    ID3D11ShaderResourceView* vs_srv_[kSrvs] = {};
    ID3D11ShaderResourceView* ps_srv_[kSrvs] = {};
    ID3D11ShaderResourceView* cs_srv_[kSrvs] = {};
    ID3D11Buffer* vs_cb_[kCbs] = {};
    ID3D11Buffer* ps_cb_[kCbs] = {};
    ID3D11Buffer* cs_cb_[kCbs] = {};
    ID3D11SamplerState* vs_smp_[kSamplers] = {};
    ID3D11SamplerState* ps_smp_[kSamplers] = {};
    ID3D11SamplerState* cs_smp_[kSamplers] = {};
    ID3D11UnorderedAccessView* cs_uav_[kUavs] = {};

    ID3D11RasterizerState* rs_ = nullptr;
    D3D11_VIEWPORT viewports_[D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE] = {};
    UINT num_viewports_ = D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE;
    D3D11_RECT scissors_[D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE] = {};
    UINT num_scissors_ = D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE;

    ID3D11RenderTargetView* rtv_[D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT] = {};
    ID3D11DepthStencilView* dsv_ = nullptr;
    ID3D11BlendState* blend_ = nullptr;
    FLOAT blend_factor_[4] = {};
    UINT sample_mask_ = 0;
    ID3D11DepthStencilState* depth_ = nullptr;
    UINT stencil_ref_ = 0;
};

// Unbind every output and the first SRV slots, so a pass can bind resources without hazards.
void unbind_all(ID3D11DeviceContext* ctx);
