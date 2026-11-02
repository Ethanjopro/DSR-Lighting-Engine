#include "state.h"

template <class T, UINT N>
static void release_all(T* (&items)[N]) {
    for (T*& item : items) {
        if (item) item->Release();
        item = nullptr;
    }
}

template <class T>
static void release_one(T*& item) {
    if (item) item->Release();
    item = nullptr;
}

StateBackup::StateBackup(ID3D11DeviceContext* ctx) : ctx_(ctx) {
    ctx->IAGetInputLayout(&il_);
    ctx->IAGetPrimitiveTopology(&topo_);
    ctx->IAGetVertexBuffers(0, 2, vb_, vb_stride_, vb_offset_);
    ctx->IAGetIndexBuffer(&ib_, &ib_format_, &ib_offset_);

    ctx->VSGetShader(&vs_, nullptr, nullptr);
    ctx->GSGetShader(&gs_, nullptr, nullptr);
    ctx->PSGetShader(&ps_, nullptr, nullptr);
    ctx->CSGetShader(&cs_, nullptr, nullptr);

    ctx->VSGetShaderResources(0, kSrvs, vs_srv_);
    ctx->PSGetShaderResources(0, kSrvs, ps_srv_);
    ctx->CSGetShaderResources(0, kSrvs, cs_srv_);
    ctx->VSGetConstantBuffers(0, kCbs, vs_cb_);
    ctx->PSGetConstantBuffers(0, kCbs, ps_cb_);
    ctx->CSGetConstantBuffers(0, kCbs, cs_cb_);
    ctx->VSGetSamplers(0, kSamplers, vs_smp_);
    ctx->PSGetSamplers(0, kSamplers, ps_smp_);
    ctx->CSGetSamplers(0, kSamplers, cs_smp_);
    ctx->CSGetUnorderedAccessViews(0, kUavs, cs_uav_);

    ctx->RSGetState(&rs_);
    ctx->RSGetViewports(&num_viewports_, viewports_);
    ctx->RSGetScissorRects(&num_scissors_, scissors_);

    ctx->OMGetRenderTargets(D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT, rtv_, &dsv_);
    ctx->OMGetBlendState(&blend_, blend_factor_, &sample_mask_);
    ctx->OMGetDepthStencilState(&depth_, &stencil_ref_);
}

StateBackup::~StateBackup() {
    ID3D11DeviceContext* ctx = ctx_;
    // Outputs first: restoring an input whose resource is still bound as an output would silently fail.
    ID3D11UnorderedAccessView* no_uav[kUavs] = {};
    ctx->CSSetUnorderedAccessViews(0, kUavs, no_uav, nullptr);
    ctx->OMSetRenderTargets(D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT, rtv_, dsv_);
    ctx->OMSetBlendState(blend_, blend_factor_, sample_mask_);
    ctx->OMSetDepthStencilState(depth_, stencil_ref_);

    ctx->IASetInputLayout(il_);
    ctx->IASetPrimitiveTopology(topo_);
    ctx->IASetVertexBuffers(0, 2, vb_, vb_stride_, vb_offset_);
    ctx->IASetIndexBuffer(ib_, ib_format_, ib_offset_);

    ctx->VSSetShader(vs_, nullptr, 0);
    ctx->GSSetShader(gs_, nullptr, 0);
    ctx->PSSetShader(ps_, nullptr, 0);
    ctx->CSSetShader(cs_, nullptr, 0);

    ctx->VSSetShaderResources(0, kSrvs, vs_srv_);
    ctx->PSSetShaderResources(0, kSrvs, ps_srv_);
    ctx->CSSetShaderResources(0, kSrvs, cs_srv_);
    ctx->VSSetConstantBuffers(0, kCbs, vs_cb_);
    ctx->PSSetConstantBuffers(0, kCbs, ps_cb_);
    ctx->CSSetConstantBuffers(0, kCbs, cs_cb_);
    ctx->VSSetSamplers(0, kSamplers, vs_smp_);
    ctx->PSSetSamplers(0, kSamplers, ps_smp_);
    ctx->CSSetSamplers(0, kSamplers, cs_smp_);
    ctx->CSSetUnorderedAccessViews(0, kUavs, cs_uav_, nullptr);

    ctx->RSSetState(rs_);
    ctx->RSSetViewports(num_viewports_, viewports_);
    ctx->RSSetScissorRects(num_scissors_, scissors_);

    release_one(il_);
    release_all(vb_);
    release_one(ib_);
    release_one(vs_);
    release_one(gs_);
    release_one(ps_);
    release_one(cs_);
    release_all(vs_srv_);
    release_all(ps_srv_);
    release_all(cs_srv_);
    release_all(vs_cb_);
    release_all(ps_cb_);
    release_all(cs_cb_);
    release_all(vs_smp_);
    release_all(ps_smp_);
    release_all(cs_smp_);
    release_all(cs_uav_);
    release_one(rs_);
    release_all(rtv_);
    release_one(dsv_);
    release_one(blend_);
    release_one(depth_);
}

void unbind_all(ID3D11DeviceContext* ctx) {
    ID3D11RenderTargetView* no_rtv[D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT] = {};
    ctx->OMSetRenderTargets(D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT, no_rtv, nullptr);
    ID3D11UnorderedAccessView* no_uav[8] = {};
    ctx->CSSetUnorderedAccessViews(0, 8, no_uav, nullptr);
    ID3D11ShaderResourceView* no_srv[32] = {};
    ctx->VSSetShaderResources(0, 32, no_srv);
    ctx->PSSetShaderResources(0, 32, no_srv);
    ctx->CSSetShaderResources(0, 32, no_srv);
}
