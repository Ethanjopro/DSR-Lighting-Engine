#include "context.h"
#include "bloom.h"
#include "common.h"
#include "engine.h"
#include "hdr.h"
#include "lightreach.h"
#include "reflect.h"
#include "shaderdb.h"
#include "pointshadow.h"
#include "sunshadow.h"
#include "trace.h"
#include <atomic>
#include <cstring>

namespace {

class WrappedContext final : public ID3D11DeviceContext {
public:
    explicit WrappedContext(ID3D11DeviceContext* real) : real_(real) {}

    // IUnknown. Newer interfaces (ID3D11DeviceContext1...) go to the real object; the game does not ask
    // for them, and anything that does (an overlay) then simply bypasses the wrapper.
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void** out) override {
        if (!out) return E_POINTER;
        if (riid == __uuidof(IUnknown) || riid == __uuidof(ID3D11DeviceChild) || riid == __uuidof(ID3D11DeviceContext)) {
            AddRef();
            *out = static_cast<ID3D11DeviceContext*>(this);
            return S_OK;
        }
        return real_->QueryInterface(riid, out);
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs_; }
    ULONG STDMETHODCALLTYPE Release() override {
        ULONG left = --refs_;
        if (left == 0) {
            real_->Release();
            delete this;
        }
        return left;
    }

    // ID3D11DeviceChild
    void STDMETHODCALLTYPE GetDevice(ID3D11Device** device) override { real_->GetDevice(device); }
    HRESULT STDMETHODCALLTYPE GetPrivateData(REFGUID guid, UINT* size, void* data) override { return real_->GetPrivateData(guid, size, data); }
    HRESULT STDMETHODCALLTYPE SetPrivateData(REFGUID guid, UINT size, const void* data) override { return real_->SetPrivateData(guid, size, data); }
    HRESULT STDMETHODCALLTYPE SetPrivateDataInterface(REFGUID guid, const IUnknown* data) override { return real_->SetPrivateDataInterface(guid, data); }

#include "context_forward.inc"

    // Intercepted methods.
    void STDMETHODCALLTYPE CSSetShader(ID3D11ComputeShader* shader, ID3D11ClassInstance* const* instances, UINT count) override {
        const ShaderInfo* info = shader ? shaderdb_find(shader) : nullptr;
        cs_role_ = info ? info->role : Role::None;
        real_->CSSetShader(shader, instances, count);
    }
    void STDMETHODCALLTYPE PSSetShader(ID3D11PixelShader* shader, ID3D11ClassInstance* const* instances, UINT count) override {
        const ShaderInfo* info = shader ? shaderdb_find(shader) : nullptr;
        ps_role_ = info ? info->role : Role::None;
        if (ps_role_ == Role::DepthCopy) after_effects_list_ = true;
        ID3D11PixelShader* replacement = nullptr;
        if (info && info->replacement && engine_pack_enabled()) {
            replacement = info->replacement;
            if (!pack_constants_bound_) {
                engine_bind_pack(real_);
                pack_constants_bound_ = true;
            }
        } else if (ps_role_ != Role::None) {
            replacement = engine_replace_ps(ps_role_);
            if (replacement) engine_bind_composite(real_);
        }
        reflect_track_shader(reflect_, (info && replacement && replacement == info->replacement) ? info : nullptr);
        real_->PSSetShader(replacement ? replacement : shader, instances, count);
    }
    void STDMETHODCALLTYPE VSSetConstantBuffers(UINT start, UINT count, ID3D11Buffer* const* buffers) override {
        if (start == 0 && count > 0) vs_b0_ = buffers[0];
        real_->VSSetConstantBuffers(start, count, buffers);
    }
    void STDMETHODCALLTYPE PSSetConstantBuffers(UINT start, UINT count, ID3D11Buffer* const* buffers) override {
        if (start == 0 && count > 0) ps_b0_ = buffers[0];
        if (start <= 1 && start + count > 1) ps_b1_ = buffers[1 - start];
        real_->PSSetConstantBuffers(start, count, buffers);
    }
    void STDMETHODCALLTYPE VSSetShader(ID3D11VertexShader* shader, ID3D11ClassInstance* const* instances, UINT count) override {
        const ShaderInfo* info = shader ? shaderdb_find(shader) : nullptr;
        vs_role_ = info ? info->role : Role::None;
        const bool foliage = vs_role_ == Role::Foliage;
        if (foliage != foliage_bound_) {
            engine_bind_draw_kind(real_, foliage);
            foliage_bound_ = foliage;
        }
        real_->VSSetShader(shader, instances, count);
    }
    void STDMETHODCALLTYPE OMSetRenderTargets(UINT count, ID3D11RenderTargetView* const* views, ID3D11DepthStencilView* depth) override {
        // The depth prepass is the only pass that binds the scene depth with no colour target.
        scene_depth_only_ = false;
        if (depth && (sunshadow_enabled() || pointshadow_enabled()) && engine_enabled()) {
            bool any_colour = false;
            for (UINT i = 0; i < count; i++) any_colour = any_colour || views[i];
            if (!any_colour) {
                ID3D11Resource* res = nullptr;
                depth->GetResource(&res);
                scene_depth_only_ = res == engine_scene_depth_resource();
                if (res) res->Release();
            }
        }
        reflect_on_targets(reflect_, real_, count, views, depth);
        real_->OMSetRenderTargets(count, views, depth);
    }
    void STDMETHODCALLTYPE OMSetRenderTargetsAndUnorderedAccessViews(UINT rtv_count, ID3D11RenderTargetView* const* views, ID3D11DepthStencilView* depth, UINT uav_start, UINT uav_count, ID3D11UnorderedAccessView* const* uavs, const UINT* initial_counts) override {
        reflect_on_targets(reflect_, real_, 0, nullptr, nullptr);
        real_->OMSetRenderTargetsAndUnorderedAccessViews(rtv_count, views, depth, uav_start, uav_count, uavs, initial_counts);
    }
    void STDMETHODCALLTYPE OMSetBlendState(ID3D11BlendState* state, const FLOAT factor[4], UINT sample_mask) override {
        reflect_on_blend(reflect_, state, factor, sample_mask);
        real_->OMSetBlendState(state, factor, sample_mask);
    }
    void STDMETHODCALLTYPE Draw(UINT count, UINT start) override {
        reflect_on_draw(reflect_, real_, view_);
        if (trace_tracking()) trace_draw(real_, "D", count, start, 0, 0, 0);
        if (ps_role_ == Role::MenuUi && engine_hide_ui()) return;
        if (ps_role_ == Role::ResolveTaa && ps_b0_copied_) {
            engine_publish_camera(ps_b0_copy_[78]);
            ps_b0_copied_ = false;
        }
        const bool bloom = ps_role_ == Role::Composite && engine_replace_ps(Role::Composite);
        if (bloom) bloom_run(real_);
        const bool drawn = (ps_role_ == Role::Composite && hdr_draw_composite(real_, count, start)) ||
                           (ps_role_ == Role::FinalBlit && hdr_draw_final(real_, count, start));
        if (!drawn) real_->Draw(count, start);
        if (bloom) bloom_unbind(real_);
    }
    void STDMETHODCALLTYPE DrawIndexed(UINT count, UINT start, INT base) override {
        reflect_on_draw(reflect_, real_, view_);
        if (trace_tracking()) trace_draw(real_, "DI", count, start, base, 1, 0);
        if (ps_role_ == Role::MenuUi && engine_hide_ui()) return;
        if (ps_role_ == Role::ForwardLit && ps_b0_copied_) {
            engine_note_key_light(ps_b0_copy_[6]);
            engine_publish_forward_constants(ps_b0_copy_);
            ps_b0_copied_ = false;
        }
        if (ps_role_ == Role::ForwardLit && engine_wants_volumetrics_inputs()) engine_offer_volumetrics_inputs(real_);
        if (scene_depth_only_) record_caster(count, start, base, 1, 0, false);
        real_->DrawIndexed(count, start, base);
    }
    void STDMETHODCALLTYPE DrawInstanced(UINT count, UINT inst, UINT start, UINT start_inst) override {
        reflect_on_draw(reflect_, real_, view_);
        if (trace_tracking()) trace_draw(real_, "DInst", count, start, 0, inst, start_inst);
        real_->DrawInstanced(count, inst, start, start_inst);
    }
    void STDMETHODCALLTYPE DrawIndexedInstanced(UINT count, UINT inst, UINT start, INT base, UINT start_inst) override {
        reflect_on_draw(reflect_, real_, view_);
        if (trace_tracking()) trace_draw(real_, "DIInst", count, start, base, inst, start_inst);
        if (scene_depth_only_) record_caster(count, start, base, inst, start_inst, true);
        real_->DrawIndexedInstanced(count, inst, start, base, start_inst);
    }
    void STDMETHODCALLTYPE DrawAuto() override {
        reflect_on_draw(reflect_, real_, view_);
        if (trace_tracking()) trace_draw(real_, "DAuto", 0, 0, 0, 0, 0);
        real_->DrawAuto();
    }
    void STDMETHODCALLTYPE DrawIndexedInstancedIndirect(ID3D11Buffer* args, UINT offset) override {
        reflect_on_draw(reflect_, real_, view_);
        if (trace_tracking()) trace_draw(real_, "DIIndirect", 0, offset, 0, 0, 0);
        real_->DrawIndexedInstancedIndirect(args, offset);
    }
    void STDMETHODCALLTYPE DrawInstancedIndirect(ID3D11Buffer* args, UINT offset) override {
        reflect_on_draw(reflect_, real_, view_);
        if (trace_tracking()) trace_draw(real_, "DIndirect", 0, offset, 0, 0, 0);
        real_->DrawInstancedIndirect(args, offset);
    }
    void STDMETHODCALLTYPE Dispatch(UINT x, UINT y, UINT z) override {
        if (cs_role_ != Role::None && engine_on_dispatch(real_, cs_role_, x, y, z, view_, casters_)) {
            if (trace_tracking()) {
                trace_note(real_, "engine pass replaced a dispatch");
                trace_snapshot_uav0(real_);
            }
            return;
        }
        if (trace_tracking()) trace_dispatch(real_, x, y, z);
        real_->Dispatch(x, y, z);
    }
    void STDMETHODCALLTYPE DispatchIndirect(ID3D11Buffer* args, UINT offset) override {
        if (trace_tracking()) trace_dispatch(real_, 0, 0, offset);
        real_->DispatchIndirect(args, offset);
    }
    HRESULT STDMETHODCALLTYPE Map(ID3D11Resource* res, UINT sub, D3D11_MAP type, UINT flags, D3D11_MAPPED_SUBRESOURCE* mapped) override {
        HRESULT hr = real_->Map(res, sub, type, flags, mapped);
        if (SUCCEEDED(hr)) {
            if (type == D3D11_MAP_WRITE_DISCARD) {
                // These slots also hold smaller blocks (the menu's PS b0 is 80 bytes), so check before keeping
                // the pointer: Unmap copies a fixed size out of it.
                if (res == vs_b0_) {
                    const UINT width = byte_width(res);
                    if (width >= sizeof(view_.vp)) vs_b0_data_ = mapped->pData;
                    vs_b0_full_ = width >= kCasterVsBytes;
                } else if (res == ps_b0_ && (engine_wants_forward_constants() || engine_wants_camera())) {
                    if (byte_width(res) >= sizeof(ps_b0_copy_)) ps_b0_data_ = mapped->pData;
                } else if (res == ps_b1_ && scene_depth_only_) {
                    if (byte_width(res) >= sizeof(ps_b1_latest_)) ps_b1_data_ = mapped->pData;
                } else if (res != ps_b0_ && res != ps_b1_ && (pointshadow_enabled() || lightreach_enabled()) && upload_count_ < kUploads) {
                    // The game's light buffers: copied at Unmap, when the mapping holds what was written. They take
                    // effect when this list executes.
                    if (const int kind = pointshadow_upload_kind(res)) {
                        upload_[upload_count_++] = Upload{res, mapped->pData, kind};
                    }
                }
            }
            if (trace_tracking()) trace_map(real_, res, sub, type, mapped);
        }
        return hr;
    }
    void STDMETHODCALLTYPE Unmap(ID3D11Resource* res, UINT sub) override {
        // The camera constants are the first registers of the vertex constant block. On a deferred context
        // the mapping is ordinary memory, so reading it back costs nothing.
        if (vs_b0_data_ && res == vs_b0_) {
            memcpy(view_.vp, vs_b0_data_, sizeof(view_.vp));
            view_.valid = view_.vp[15] == 0.0f && view_.vp[0] + view_.vp[1] + view_.vp[2] != 0.0f;
            reflect_on_view(reflect_, view_);
            if (scene_depth_only_ && vs_b0_full_) {
                memcpy(vs_b0_latest_, vs_b0_data_, kCasterVsBytes);
                vs_b0_latest_valid_ = true;
            }
            vs_b0_data_ = nullptr;
        }
        bool light_upload = false;
        for (UINT i = 0; i < upload_count_; i++) {
            if (upload_[i].res != res) continue;
            // In this order: the shadows must see the stretched radii.
            lightreach_on_upload(real_, upload_[i].kind, res, upload_[i].data);
            pointshadow_on_upload(real_, upload_[i].kind, upload_[i].data);
            upload_[i] = upload_[--upload_count_];
            light_upload = true;
            break;
        }
        if (ps_b1_data_ && res == ps_b1_) {
            memcpy(ps_b1_latest_, ps_b1_data_, sizeof(ps_b1_latest_));
            ps_b1_data_ = nullptr;
        }
        if (ps_b0_data_ && res == ps_b0_) {
            // Copied now, offered at the draw: only then is it known which shader family this block belongs to.
            memcpy(ps_b0_copy_, ps_b0_data_, sizeof(ps_b0_copy_));
            ps_b0_copied_ = true;
            ps_b0_data_ = nullptr;
        }
        if (trace_tracking()) trace_unmap(real_, res, sub);
        real_->Unmap(res, sub);
        if (light_upload) lightreach_finish(real_);
    }
    void STDMETHODCALLTYPE UpdateSubresource(ID3D11Resource* res, UINT sub, const D3D11_BOX* box, const void* data, UINT row, UINT depth) override {
        if (trace_tracking()) trace_update_subresource(real_, res, sub, box, data, row);
        real_->UpdateSubresource(res, sub, box, data, row, depth);
    }
    void STDMETHODCALLTYPE CopyResource(ID3D11Resource* dst, ID3D11Resource* src) override {
        if (trace_tracking()) trace_copy(real_, "CopyResource", dst, src);
        // A list that has already cleared or drawn elsewhere holds the game's other copy of the scene, whatever order they were recorded in.
        if (!reflect_.list.elsewhere) engine_reflect_on_copy(real_, src);
        // The game copies the scene to one target twice a frame, and either list can be recorded first. The second copy's
        // list has already drawn the depth copy, and what it copies holds water and effects.
        if (!after_effects_list_) engine_on_opaque_copy(real_, src);
        // Before the copy: the refraction copy must hold the volumetrics result.
        engine_on_copy_resource(real_, src);
        real_->CopyResource(dst, src);
    }
    void STDMETHODCALLTYPE CopySubresourceRegion(ID3D11Resource* dst, UINT dsub, UINT x, UINT y, UINT z, ID3D11Resource* src, UINT ssub, const D3D11_BOX* box) override {
        if (trace_tracking()) trace_copy(real_, "CopyRegion", dst, src);
        real_->CopySubresourceRegion(dst, dsub, x, y, z, src, ssub, box);
    }
    void STDMETHODCALLTYPE ResolveSubresource(ID3D11Resource* dst, UINT dsub, ID3D11Resource* src, UINT ssub, DXGI_FORMAT format) override {
        if (trace_tracking()) trace_copy(real_, "Resolve", dst, src);
        real_->ResolveSubresource(dst, dsub, src, ssub, format);
    }
    void STDMETHODCALLTYPE ClearRenderTargetView(ID3D11RenderTargetView* view, const FLOAT color[4]) override {
        reflect_on_clear(reflect_);
        if (trace_tracking()) trace_clear_rtv(real_, view, color);
        real_->ClearRenderTargetView(view, color);
    }
    void STDMETHODCALLTYPE ClearDepthStencilView(ID3D11DepthStencilView* view, UINT flags, FLOAT depth, UINT8 stencil) override {
        if (trace_tracking()) trace_clear_dsv(real_, view, flags, depth, stencil);
        real_->ClearDepthStencilView(view, flags, depth, stencil);
    }
    void STDMETHODCALLTYPE GenerateMips(ID3D11ShaderResourceView* view) override {
        if (trace_tracking()) trace_generate_mips(real_, view);
        real_->GenerateMips(view);
    }
    void STDMETHODCALLTYPE ClearState() override {
        if (trace_tracking()) trace_clear_state(real_);
        reset_tracked_state();
        real_->ClearState();
    }
    void STDMETHODCALLTYPE ExecuteCommandList(ID3D11CommandList* list, BOOL restore) override {
        if (trace_tracking()) trace_execute_before(real_, list);
        // Lists are recorded in any order and executed in one, so what they read beside their own state is written here.
        engine_on_execute(real_, list);
        real_->ExecuteCommandList(list, restore);
        if (trace_tracking()) trace_execute_after(real_, list);
    }
    HRESULT STDMETHODCALLTYPE FinishCommandList(BOOL restore, ID3D11CommandList** out) override {
        if (trace_tracking()) trace_finish_before(real_);
        HRESULT hr = real_->FinishCommandList(restore, out);
        if (trace_tracking() && SUCCEEDED(hr) && out && *out) trace_finish_after(real_, *out);
        // Without restore, the context comes back with cleared state.
        if (SUCCEEDED(hr) && out && *out) pointshadow_on_finish(real_, *out);
        if (!casters_.draws.empty()) caster_list_release(casters_);
        reflect_track_list_end(reflect_);
        after_effects_list_ = false;
        if (!restore) reset_tracked_state();
        return hr;
    }

private:
    static UINT byte_width(ID3D11Resource* res) {
        D3D11_BUFFER_DESC d;
        static_cast<ID3D11Buffer*>(res)->GetDesc(&d);
        return d.ByteWidth;
    }

    void reset_tracked_state() {
        reflect_track_reset(reflect_);
        cs_role_ = ps_role_ = vs_role_ = Role::None;
        foliage_bound_ = false;
        vs_b0_ = ps_b0_ = ps_b1_ = nullptr;
        vs_b0_data_ = ps_b0_data_ = ps_b1_data_ = nullptr;
        ps_b0_copied_ = false;
        pack_constants_bound_ = false;
        scene_depth_only_ = false;
        vs_b0_latest_valid_ = false;
        upload_count_ = 0;
    }

    // Remember a prepass draw for replay into the sun cascades. Getters take references; the replay drops them.
    void record_caster(UINT index_count, UINT start_index, INT base_vertex, UINT instances, UINT start_instance, bool instanced) {
        if (vs_role_ == Role::NoCast || ps_role_ == Role::NoCast || !vs_b0_latest_valid_) return;
        CasterDraw d;
        real_->IAGetInputLayout(&d.layout);
        real_->IAGetPrimitiveTopology(&d.topology);
        real_->IAGetVertexBuffers(0, 2, d.vb, d.stride, d.offset);
        real_->IAGetIndexBuffer(&d.ib, &d.index_format, &d.index_offset);
        real_->VSGetShader(&d.vs, nullptr, nullptr);
        real_->VSGetConstantBuffers(0, 1, &d.vs_b0);
        real_->VSGetConstantBuffers(2, 1, &d.vs_b2);
        real_->PSGetShader(&d.ps, nullptr, nullptr);
        if (d.ps) {
            real_->PSGetShaderResources(0, 1, &d.ps_t0);
            real_->PSGetSamplers(0, 1, &d.ps_s0);
            real_->PSGetConstantBuffers(1, 1, &d.ps_b1);
            memcpy(d.ps_b1_bytes, ps_b1_latest_, sizeof(d.ps_b1_bytes));
        }
        ID3D11RasterizerState* rs = nullptr;
        real_->RSGetState(&rs);
        if (rs) {
            D3D11_RASTERIZER_DESC desc;
            rs->GetDesc(&desc);
            d.cull = desc.CullMode;
            d.front_ccw = desc.FrontCounterClockwise != 0;
            rs->Release();
        }
        d.index_count = index_count;
        d.start_index = start_index;
        d.base_vertex = base_vertex;
        d.instance_count = instances;
        d.start_instance = start_instance;
        d.instanced = instanced;
        d.constants_at = static_cast<UINT>(casters_.vs_constants.size());
        casters_.vs_constants.insert(casters_.vs_constants.end(), vs_b0_latest_, vs_b0_latest_ + kCasterVsBytes);
        casters_.draws.push_back(d);
    }

    struct Upload {
        ID3D11Resource* res;  // not owned: only compared against Unmap targets
        void* data;
        int kind;
    };
    static constexpr UINT kUploads = 3;

    ID3D11DeviceContext* const real_;
    std::atomic<ULONG> refs_{1};
    Role cs_role_ = Role::None;
    Role ps_role_ = Role::None;
    Role vs_role_ = Role::None;
    ID3D11Buffer* vs_b0_ = nullptr;   // not owned: only compared against Map targets
    ID3D11Buffer* ps_b0_ = nullptr;   // same
    ID3D11Buffer* ps_b1_ = nullptr;   // same
    void* vs_b0_data_ = nullptr;
    void* ps_b0_data_ = nullptr;
    void* ps_b1_data_ = nullptr;
    bool vs_b0_full_ = false;
    bool ps_b0_copied_ = false;
    bool pack_constants_bound_ = false;
    bool after_effects_list_ = false;  // for the whole list, whatever state the game clears along the way
    bool foliage_bound_ = false;
    bool scene_depth_only_ = false;
    bool vs_b0_latest_valid_ = false;
    float ps_b0_copy_[kForwardRegs][4];
    uint8_t vs_b0_latest_[kCasterVsBytes];
    uint8_t ps_b1_latest_[48] = {};
    Upload upload_[kUploads] = {};
    UINT upload_count_ = 0;
    ViewConstants view_;
    CasterList casters_;
    ReflectTrack reflect_;
};

}  // namespace

ID3D11DeviceContext* context_wrap(ID3D11DeviceContext* real) {
    return new WrappedContext(real);
}
