#include "trace.h"
#include "common.h"
#include "shaderdb.h"
#include <algorithm>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

bool g_trace_tracking;

namespace {

// Trace output layout, under dsrle\trace\f<frame>\:
//   trace.jsonl   metadata lines (resources, views, states, layouts, cbdata), event lines in execution order
//   cb.bin        unique constant-buffer contents; "cbdata" lines give offset and size per hash
//   *.dds         render-target snapshots; a "dump" line sits in the event stream where the copy was recorded
//
// Tracking starts two frames before the written frame, because workers may record a list before the
// Present that opens the frame the list is executed in. Lists executed before that frame are dropped.

std::recursive_mutex g_lock;  // the tracer only runs for a few frames, so one big lock is fine

std::wstring g_dir;
FILE* g_out;
FILE* g_cb;
uint64_t g_cb_offset;
uint64_t g_frame;
bool g_writing;               // the frame whose events reach the file
int g_dump_count;
int g_dump_limit = 500;
bool g_dump_targets = true;
ID3D11Device* g_device;

std::unordered_map<void*, int> g_ids;
int g_next_id = 1;
std::unordered_set<void*> g_described;
std::unordered_set<uint64_t> g_cb_written;

struct LayoutInfo {
    std::vector<D3D11_INPUT_ELEMENT_DESC> elems;
    std::vector<std::string> names;
};
std::unordered_map<void*, LayoutInfo> g_layouts;
std::mutex g_layout_lock;

struct CbShadow {
    uint64_t hash = 0;
    std::vector<uint8_t> bytes;
};
// Last upload seen per buffer. A deferred context's upload is keyed by context too, because the buffer's
// contents as a draw sees them come from that context's own Map.
std::unordered_map<void*, CbShadow> g_cb_global;

struct OutputSet {
    ID3D11RenderTargetView* rtv[8] = {};
    ID3D11DepthStencilView* dsv = nullptr;
    ID3D11UnorderedAccessView* uav[8] = {};
    bool operator==(const OutputSet& o) const { return !memcmp(this, &o, sizeof(OutputSet)); }
};

struct PendingDump {
    ID3D11Texture2D* staging;
    D3D11_TEXTURE2D_DESC desc;
    std::wstring file;
};

struct ContextState {
    std::string events;            // JSON lines waiting for FinishCommandList or, on the immediate context, the file
    std::vector<PendingDump> dumps;
    OutputSet outputs;
    std::unordered_map<void*, CbShadow> cb;
    std::unordered_map<void*, D3D11_MAPPED_SUBRESOURCE> open_maps;
    DWORD thread = 0;
    int index = 0;
};
std::unordered_map<ID3D11DeviceContext*, ContextState> g_contexts;

struct ListRecord {
    std::string events;
    std::vector<PendingDump> dumps;
    int ctx_index;
    DWORD thread;
};
std::unordered_map<ID3D11CommandList*, ListRecord> g_lists;

ContextState& state_of(ID3D11DeviceContext* ctx) {
    auto [it, inserted] = g_contexts.try_emplace(ctx);
    if (inserted) it->second.index = static_cast<int>(g_contexts.size());
    it->second.thread = GetCurrentThreadId();
    return it->second;
}

bool is_immediate(ID3D11DeviceContext* ctx) { return ctx->GetType() == D3D11_DEVICE_CONTEXT_IMMEDIATE; }

// Metadata lines go straight to the file: the reader does not care about their order.
void meta(const char* fmt, ...) {
    if (!g_out) return;
    va_list ap;
    va_start(ap, fmt);
    vfprintf(g_out, fmt, ap);
    va_end(ap);
}

void append(std::string& s, const char* fmt, ...) {
    char stack_buf[1024];
    va_list ap;
    va_start(ap, fmt);
    va_list ap2;
    va_copy(ap2, ap);
    int n = vsnprintf(stack_buf, sizeof(stack_buf), fmt, ap);
    va_end(ap);
    if (n >= 0 && n < static_cast<int>(sizeof(stack_buf))) {
        s.append(stack_buf, n);
    } else if (n > 0) {
        size_t old = s.size();
        s.resize(old + n + 1);
        vsnprintf(s.data() + old, n + 1, fmt, ap2);
        s.resize(old + n);
    }
    va_end(ap2);
}

int id_of(void* p) {
    if (!p) return 0;
    auto it = g_ids.find(p);
    if (it != g_ids.end()) return it->second;
    int id = g_next_id++;
    g_ids.emplace(p, id);
    return id;
}

int describe_resource(ID3D11Resource* res) {
    int id = id_of(res);
    if (!res || !g_described.insert(res).second) return id;
    D3D11_RESOURCE_DIMENSION dim;
    res->GetType(&dim);
    if (dim == D3D11_RESOURCE_DIMENSION_TEXTURE2D) {
        D3D11_TEXTURE2D_DESC d;
        static_cast<ID3D11Texture2D*>(res)->GetDesc(&d);
        meta("{\"res\":%d,\"type\":\"tex2d\",\"w\":%u,\"h\":%u,\"mips\":%u,\"arr\":%u,\"fmt\":%d,\"samples\":%u,\"usage\":%d,\"bind\":%u,\"cpu\":%u,\"misc\":%u}\n",
             id, d.Width, d.Height, d.MipLevels, d.ArraySize, d.Format, d.SampleDesc.Count, d.Usage, d.BindFlags, d.CPUAccessFlags, d.MiscFlags);
    } else if (dim == D3D11_RESOURCE_DIMENSION_BUFFER) {
        D3D11_BUFFER_DESC d;
        static_cast<ID3D11Buffer*>(res)->GetDesc(&d);
        meta("{\"res\":%d,\"type\":\"buffer\",\"size\":%u,\"usage\":%d,\"bind\":%u,\"cpu\":%u,\"misc\":%u,\"stride\":%u}\n",
             id, d.ByteWidth, d.Usage, d.BindFlags, d.CPUAccessFlags, d.MiscFlags, d.StructureByteStride);
    } else if (dim == D3D11_RESOURCE_DIMENSION_TEXTURE3D) {
        D3D11_TEXTURE3D_DESC d;
        static_cast<ID3D11Texture3D*>(res)->GetDesc(&d);
        meta("{\"res\":%d,\"type\":\"tex3d\",\"w\":%u,\"h\":%u,\"d\":%u,\"mips\":%u,\"fmt\":%d,\"usage\":%d,\"bind\":%u}\n",
             id, d.Width, d.Height, d.Depth, d.MipLevels, d.Format, d.Usage, d.BindFlags);
    } else {
        meta("{\"res\":%d,\"type\":\"other\",\"dim\":%d}\n", id, dim);
    }
    return id;
}

template <class View, class Desc>
int describe_view(View* view, const char* kind, Desc& desc) {
    int id = id_of(view);
    if (!view || !g_described.insert(view).second) return id;
    ID3D11Resource* res = nullptr;
    view->GetResource(&res);
    int rid = describe_resource(res);
    if (res) res->Release();
    view->GetDesc(&desc);
    const uint32_t* raw = reinterpret_cast<const uint32_t*>(&desc);
    meta("{\"view\":%d,\"kind\":\"%s\",\"res\":%d,\"fmt\":%d,\"dim\":%d,\"a\":[%u,%u,%u,%u]}\n", id, kind, rid, desc.Format, desc.ViewDimension,
         raw[2], raw[3], raw[4], raw[5]);
    return id;
}

int view_id(ID3D11ShaderResourceView* v) { D3D11_SHADER_RESOURCE_VIEW_DESC d; return describe_view(v, "srv", d); }
int view_id(ID3D11RenderTargetView* v) { D3D11_RENDER_TARGET_VIEW_DESC d; return describe_view(v, "rtv", d); }
int view_id(ID3D11UnorderedAccessView* v) { D3D11_UNORDERED_ACCESS_VIEW_DESC d; return describe_view(v, "uav", d); }
int view_id(ID3D11DepthStencilView* v) {
    int id = id_of(v);
    if (!v || !g_described.insert(v).second) return id;
    ID3D11Resource* res = nullptr;
    v->GetResource(&res);
    int rid = describe_resource(res);
    if (res) res->Release();
    D3D11_DEPTH_STENCIL_VIEW_DESC d;
    v->GetDesc(&d);
    const bool array = d.ViewDimension == D3D11_DSV_DIMENSION_TEXTURE2DARRAY;
    meta("{\"view\":%d,\"kind\":\"dsv\",\"res\":%d,\"fmt\":%d,\"dim\":%d,\"flags\":%u,\"mip\":%u,\"first\":%u,\"size\":%u}\n", id, rid, d.Format, d.ViewDimension, d.Flags,
         d.Texture2DArray.MipSlice, array ? d.Texture2DArray.FirstArraySlice : 0, array ? d.Texture2DArray.ArraySize : 1);
    return id;
}

int state_id(ID3D11BlendState* s) {
    int id = id_of(s);
    if (!s || !g_described.insert(s).second) return id;
    D3D11_BLEND_DESC d;
    s->GetDesc(&d);
    const auto& r = d.RenderTarget[0];
    meta("{\"state\":%d,\"kind\":\"blend\",\"a2c\":%d,\"indep\":%d,\"rt0\":[%d,%d,%d,%d,%d,%d,%d,%d]}\n", id, d.AlphaToCoverageEnable, d.IndependentBlendEnable,
         r.BlendEnable, r.SrcBlend, r.DestBlend, r.BlendOp, r.SrcBlendAlpha, r.DestBlendAlpha, r.BlendOpAlpha, r.RenderTargetWriteMask);
    return id;
}
int state_id(ID3D11DepthStencilState* s) {
    int id = id_of(s);
    if (!s || !g_described.insert(s).second) return id;
    D3D11_DEPTH_STENCIL_DESC d;
    s->GetDesc(&d);
    meta("{\"state\":%d,\"kind\":\"depth\",\"test\":%d,\"write\":%d,\"func\":%d,\"stencil\":%d}\n", id, d.DepthEnable, d.DepthWriteMask, d.DepthFunc, d.StencilEnable);
    return id;
}
int state_id(ID3D11RasterizerState* s) {
    int id = id_of(s);
    if (!s || !g_described.insert(s).second) return id;
    D3D11_RASTERIZER_DESC d;
    s->GetDesc(&d);
    meta("{\"state\":%d,\"kind\":\"raster\",\"fill\":%d,\"cull\":%d,\"ccw\":%d,\"bias\":%d,\"clamp\":%g,\"slope\":%g,\"clip\":%d,\"scissor\":%d,\"ms\":%d}\n", id, d.FillMode, d.CullMode,
         d.FrontCounterClockwise, d.DepthBias, d.DepthBiasClamp, d.SlopeScaledDepthBias, d.DepthClipEnable, d.ScissorEnable, d.MultisampleEnable);
    return id;
}
int state_id(ID3D11SamplerState* s) {
    int id = id_of(s);
    if (!s || !g_described.insert(s).second) return id;
    D3D11_SAMPLER_DESC d;
    s->GetDesc(&d);
    meta("{\"state\":%d,\"kind\":\"sampler\",\"filter\":%d,\"addr\":[%d,%d,%d],\"bias\":%g,\"aniso\":%u,\"cmp\":%d}\n", id, d.Filter, d.AddressU, d.AddressV, d.AddressW, d.MipLODBias, d.MaxAnisotropy, d.ComparisonFunc);
    return id;
}

int layout_id(ID3D11InputLayout* l) {
    int id = id_of(l);
    if (!l || !g_described.insert(l).second) return id;
    std::lock_guard<std::mutex> lock(g_layout_lock);
    auto it = g_layouts.find(l);
    if (it == g_layouts.end()) return id;
    std::string s;
    for (size_t i = 0; i < it->second.elems.size(); i++) {
        const auto& e = it->second.elems[i];
        append(s, "%s[\"%s\",%u,%d,%u,%u,%d,%u]", i ? "," : "", it->second.names[i].c_str(), e.SemanticIndex, e.Format, e.InputSlot, e.AlignedByteOffset, e.InputSlotClass, e.InstanceDataStepRate);
    }
    meta("{\"layout\":%d,\"elems\":[%s]}\n", id, s.c_str());
    return id;
}

const char* shader_name(void* obj) {
    if (!obj) return "";
    const ShaderInfo* info = shaderdb_find(obj);
    return info ? info->name.c_str() : "?unregistered";
}

template <class Get>
std::string srv_list(Get get) {
    ID3D11ShaderResourceView* v[128] = {};
    get(v);
    std::string s = "{";
    bool first = true;
    for (int i = 0; i < 128; i++) {
        if (!v[i]) continue;
        append(s, "%s\"%d\":%d", first ? "" : ",", i, view_id(v[i]));
        first = false;
        v[i]->Release();
    }
    return s + "}";
}

template <class Get>
std::string sampler_list(Get get) {
    ID3D11SamplerState* v[16] = {};
    get(v);
    std::string s = "{";
    bool first = true;
    for (int i = 0; i < 16; i++) {
        if (!v[i]) continue;
        append(s, "%s\"%d\":%d", first ? "" : ",", i, state_id(v[i]));
        first = false;
        v[i]->Release();
    }
    return s + "}";
}

const CbShadow* cb_lookup(ContextState& st, void* buffer) {
    auto it = st.cb.find(buffer);
    if (it != st.cb.end()) return &it->second;
    auto git = g_cb_global.find(buffer);
    return git == g_cb_global.end() ? nullptr : &git->second;
}

template <class Get>
std::string cb_list(ContextState& st, Get get) {
    ID3D11Buffer* v[14] = {};
    get(v);
    std::string s = "{";
    bool first = true;
    for (int i = 0; i < 14; i++) {
        if (!v[i]) continue;
        int id = describe_resource(v[i]);
        const CbShadow* shadow = cb_lookup(st, v[i]);
        unsigned long long hash = shadow ? shadow->hash : 0;
        if (shadow && g_cb && g_cb_written.insert(hash).second) {
            meta("{\"cbdata\":\"%016llx\",\"off\":%llu,\"size\":%zu}\n", hash, static_cast<unsigned long long>(g_cb_offset), shadow->bytes.size());
            fwrite(shadow->bytes.data(), 1, shadow->bytes.size(), g_cb);
            g_cb_offset += shadow->bytes.size();
        }
        append(s, "%s\"%d\":[%d,\"%016llx\"]", first ? "" : ",", i, id, hash);
        first = false;
        v[i]->Release();
    }
    return s + "}";
}

void write_dds(const std::wstring& path, ID3D11Texture2D* staging, const D3D11_TEXTURE2D_DESC& d, ID3D11DeviceContext* immediate) {
    D3D11_MAPPED_SUBRESOURCE m;
    if (FAILED(immediate->Map(staging, 0, D3D11_MAP_READ, 0, &m))) return;
    FILE* f = nullptr;
    _wfopen_s(&f, path.c_str(), L"wb");
    if (f) {
        uint32_t hdr[31] = {};
        hdr[0] = 124;                       // dwSize
        hdr[1] = 0x1 | 0x2 | 0x4 | 0x1000;  // CAPS | HEIGHT | WIDTH | PIXELFORMAT
        hdr[2] = d.Height;
        hdr[3] = d.Width;
        hdr[4] = m.RowPitch;
        hdr[18] = 32;                       // ddspf.dwSize
        hdr[19] = 0x4;                      // DDPF_FOURCC
        hdr[20] = MAKEFOURCC('D', 'X', '1', '0');
        hdr[26] = 0x1000;                   // DDSCAPS_TEXTURE
        uint32_t dx10[5] = {static_cast<uint32_t>(d.Format), 3, 0, 1, 0};
        fwrite("DDS ", 1, 4, f);
        fwrite(hdr, 4, 31, f);
        fwrite(dx10, 4, 5, f);
        // Rows stay at RowPitch; the header's pitch field says so, and the reader uses it.
        fwrite(m.pData, 1, static_cast<size_t>(m.RowPitch) * d.Height, f);
        fclose(f);
    }
    immediate->Unmap(staging, 0);
}

// Record a copy of the resource into a staging texture on this context. It is read back after the list
// has executed; on a deferred context the GPU has not done any of this work yet.
void snapshot(ID3D11DeviceContext* ctx, ContextState& st, ID3D11Resource* res, const char* tag) {
    if (!res || !g_dump_targets || !g_device || g_dump_count >= g_dump_limit) return;
    D3D11_RESOURCE_DIMENSION dim;
    res->GetType(&dim);
    if (dim != D3D11_RESOURCE_DIMENSION_TEXTURE2D) return;
    D3D11_TEXTURE2D_DESC d;
    static_cast<ID3D11Texture2D*>(res)->GetDesc(&d);
    if (d.SampleDesc.Count > 1) return;
    D3D11_TEXTURE2D_DESC s = d;
    s.Usage = D3D11_USAGE_STAGING;
    s.BindFlags = 0;
    s.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    s.MiscFlags = 0;
    ID3D11Texture2D* staging = nullptr;
    if (FAILED(g_device->CreateTexture2D(&s, nullptr, &staging))) return;
    ctx->CopyResource(staging, res);
    static int serial;
    wchar_t name[96];
    swprintf_s(name, L"d%05d_%S_r%d.dds", ++serial, tag, id_of(res));
    st.dumps.push_back(PendingDump{staging, d, name});
    g_dump_count++;
    append(st.events, "{\"dump\":\"%S\",\"res\":%d}\n", name, describe_resource(res));
}

void snapshot_outputs(ID3D11DeviceContext* ctx, ContextState& st) {
    const OutputSet& set = st.outputs;
    auto dump_view = [&](auto* view, const char* tag) {
        if (!view) return;
        ID3D11Resource* r = nullptr;
        view->GetResource(&r);
        snapshot(ctx, st, r, tag);
        if (r) r->Release();
    };
    for (auto* v : set.rtv) dump_view(v, "rt");
    dump_view(set.dsv, "ds");
    for (auto* v : set.uav) dump_view(v, "uav");
}

void release_outputs(OutputSet& set) {
    for (auto*& v : set.rtv) { if (v) v->Release(); v = nullptr; }
    if (set.dsv) { set.dsv->Release(); set.dsv = nullptr; }
    for (auto*& v : set.uav) { if (v) v->Release(); v = nullptr; }
}

// Snapshot the previous pass's outputs when the output set changes. References are held until the next
// change, so a pointer cannot be recycled between comparisons.
void outputs_transition(ID3D11DeviceContext* ctx, ContextState& st, OutputSet& now) {
    if (now == st.outputs) {
        release_outputs(now);
        return;
    }
    snapshot_outputs(ctx, st);
    release_outputs(st.outputs);
    st.outputs = now;
    now = OutputSet{};
}

void store_cb(ContextState& st, void* buffer, const void* data, uint32_t size) {
    CbShadow& shadow = st.cb[buffer];
    shadow.hash = fnv1a64(data, size);
    shadow.bytes.resize(size);
    memcpy(shadow.bytes.data(), data, size);
    g_cb_global[buffer] = shadow;
}

bool is_constant_buffer(ID3D11Resource* res) {
    D3D11_RESOURCE_DIMENSION dim;
    res->GetType(&dim);
    if (dim != D3D11_RESOURCE_DIMENSION_BUFFER) return false;
    D3D11_BUFFER_DESC d;
    static_cast<ID3D11Buffer*>(res)->GetDesc(&d);
    return (d.BindFlags & D3D11_BIND_CONSTANT_BUFFER) != 0;
}

bool file_exists(const std::wstring& path) { return GetFileAttributesW(path.c_str()) != INVALID_FILE_ATTRIBUTES; }

// Events on the immediate context go to the file directly once this frame is the written one.
void flush_immediate(ContextState& st, bool write) {
    if (write && g_out) fwrite(st.events.data(), 1, st.events.size(), g_out);
    st.events.clear();
}

void read_back(std::vector<PendingDump>& dumps, ID3D11DeviceContext* immediate, bool write) {
    for (auto& d : dumps) {
        if (write) write_dds(g_dir + d.file, d.staging, d.desc, immediate);
        d.staging->Release();
    }
    dumps.clear();
}

}  // namespace

void trace_note_input_layout(ID3D11InputLayout* layout, const D3D11_INPUT_ELEMENT_DESC* desc, UINT count) {
    LayoutInfo info;
    info.elems.assign(desc, desc + count);
    for (UINT i = 0; i < count; i++) info.names.emplace_back(desc[i].SemanticName);
    std::lock_guard<std::mutex> lock(g_layout_lock);
    g_layouts[layout] = std::move(info);
}

void trace_draw(ID3D11DeviceContext* ctx, const char* kind, UINT count, UINT start, INT base, UINT inst, UINT start_inst) {
    std::lock_guard<std::recursive_mutex> lock(g_lock);
    ContextState& st = state_of(ctx);
    OutputSet set;
    ctx->OMGetRenderTargets(8, set.rtv, &set.dsv);
    outputs_transition(ctx, st, set);

    ID3D11VertexShader* vs = nullptr; ctx->VSGetShader(&vs, nullptr, nullptr);
    ID3D11PixelShader* ps = nullptr; ctx->PSGetShader(&ps, nullptr, nullptr);
    ID3D11GeometryShader* gs = nullptr; ctx->GSGetShader(&gs, nullptr, nullptr);
    ID3D11InputLayout* il = nullptr; ctx->IAGetInputLayout(&il);
    D3D11_PRIMITIVE_TOPOLOGY topo; ctx->IAGetPrimitiveTopology(&topo);
    ID3D11Buffer* vb[4] = {}; UINT strides[4] = {}, offs[4] = {};
    ctx->IAGetVertexBuffers(0, 4, vb, strides, offs);
    ID3D11Buffer* ib = nullptr; DXGI_FORMAT ifmt = DXGI_FORMAT_UNKNOWN; UINT ioff = 0;
    ctx->IAGetIndexBuffer(&ib, &ifmt, &ioff);

    D3D11_VIEWPORT vp[1] = {};
    UINT nvp = 1;
    ctx->RSGetViewports(&nvp, vp);
    ID3D11BlendState* bs = nullptr; FLOAT bf[4]; UINT mask;
    ctx->OMGetBlendState(&bs, bf, &mask);
    ID3D11DepthStencilState* dss = nullptr; UINT sref = 0;
    ctx->OMGetDepthStencilState(&dss, &sref);
    ID3D11RasterizerState* rs = nullptr;
    ctx->RSGetState(&rs);

    std::string rts;
    for (auto* v : st.outputs.rtv) {
        if (v) append(rts, "%s%d", rts.empty() ? "" : ",", view_id(v));
    }
    std::string vbs;
    for (int i = 0; i < 4; i++) {
        if (!vb[i]) continue;
        append(vbs, "%s[%d,%d,%u,%u]", vbs.empty() ? "" : ",", i, describe_resource(vb[i]), strides[i], offs[i]);
        vb[i]->Release();
    }
    int ibid = describe_resource(ib);
    if (ib) ib->Release();
    int ilid = layout_id(il);
    if (il) il->Release();

    auto srv_ps = srv_list([&](ID3D11ShaderResourceView** v) { ctx->PSGetShaderResources(0, 128, v); });
    auto srv_vs = srv_list([&](ID3D11ShaderResourceView** v) { ctx->VSGetShaderResources(0, 128, v); });
    auto smp_ps = sampler_list([&](ID3D11SamplerState** v) { ctx->PSGetSamplers(0, 16, v); });
    auto cb_ps = cb_list(st, [&](ID3D11Buffer** v) { ctx->PSGetConstantBuffers(0, 14, v); });
    auto cb_vs = cb_list(st, [&](ID3D11Buffer** v) { ctx->VSGetConstantBuffers(0, 14, v); });

    append(st.events, "{\"k\":\"%s\",\"n\":[%u,%u,%d,%u,%u],\"vs\":\"%s\",\"ps\":\"%s\",\"gs\":\"%s\",\"rt\":[%s],\"ds\":%d,\"il\":%d,\"topo\":%d,\"ib\":[%d,%d,%u],\"vb\":[%s],\"psrv\":%s,\"vsrv\":%s,\"psmp\":%s,\"pcb\":%s,\"vcb\":%s,\"vp\":[%g,%g,%g,%g,%g,%g],\"bs\":%d,\"dss\":%d,\"sref\":%u,\"rs\":%d}\n",
           kind, count, start, base, inst, start_inst, shader_name(vs), shader_name(ps), shader_name(gs), rts.c_str(), view_id(st.outputs.dsv), ilid, topo, ibid, ifmt, ioff, vbs.c_str(),
           srv_ps.c_str(), srv_vs.c_str(), smp_ps.c_str(), cb_ps.c_str(), cb_vs.c_str(),
           vp[0].TopLeftX, vp[0].TopLeftY, vp[0].Width, vp[0].Height, vp[0].MinDepth, vp[0].MaxDepth, state_id(bs), state_id(dss), sref, state_id(rs));
    if (bs) bs->Release();
    if (dss) dss->Release();
    if (rs) rs->Release();
    if (vs) vs->Release();
    if (ps) ps->Release();
    if (gs) gs->Release();
}

void trace_dispatch(ID3D11DeviceContext* ctx, UINT x, UINT y, UINT z) {
    std::lock_guard<std::recursive_mutex> lock(g_lock);
    ContextState& st = state_of(ctx);
    OutputSet set;
    ctx->CSGetUnorderedAccessViews(0, 8, set.uav);
    outputs_transition(ctx, st, set);
    ID3D11ComputeShader* cs = nullptr;
    ctx->CSGetShader(&cs, nullptr, nullptr);
    std::string uavs;
    for (int i = 0; i < 8; i++) {
        if (st.outputs.uav[i]) append(uavs, "%s\"%d\":%d", uavs.empty() ? "" : ",", i, view_id(st.outputs.uav[i]));
    }
    auto srv = srv_list([&](ID3D11ShaderResourceView** v) { ctx->CSGetShaderResources(0, 128, v); });
    auto cb = cb_list(st, [&](ID3D11Buffer** v) { ctx->CSGetConstantBuffers(0, 14, v); });
    auto smp = sampler_list([&](ID3D11SamplerState** v) { ctx->CSGetSamplers(0, 16, v); });
    append(st.events, "{\"k\":\"CS\",\"n\":[%u,%u,%u],\"cs\":\"%s\",\"uav\":{%s},\"csrv\":%s,\"ccb\":%s,\"csmp\":%s}\n", x, y, z, shader_name(cs), uavs.c_str(), srv.c_str(), cb.c_str(), smp.c_str());
    if (cs) cs->Release();
}

void trace_copy(ID3D11DeviceContext* ctx, const char* kind, ID3D11Resource* dst, ID3D11Resource* src) {
    std::lock_guard<std::recursive_mutex> lock(g_lock);
    ContextState& st = state_of(ctx);
    int d = describe_resource(dst), s = describe_resource(src);
    append(st.events, "{\"k\":\"%s\",\"dst\":%d,\"src\":%d}\n", kind, d, s);
}

void trace_clear_rtv(ID3D11DeviceContext* ctx, ID3D11RenderTargetView* v, const FLOAT c[4]) {
    std::lock_guard<std::recursive_mutex> lock(g_lock);
    ContextState& st = state_of(ctx);
    int id = view_id(v);
    append(st.events, "{\"k\":\"ClearRTV\",\"v\":%d,\"c\":[%g,%g,%g,%g]}\n", id, c[0], c[1], c[2], c[3]);
}

void trace_clear_dsv(ID3D11DeviceContext* ctx, ID3D11DepthStencilView* v, UINT flags, FLOAT depth, UINT8 stencil) {
    std::lock_guard<std::recursive_mutex> lock(g_lock);
    ContextState& st = state_of(ctx);
    int id = view_id(v);
    append(st.events, "{\"k\":\"ClearDSV\",\"v\":%d,\"f\":%u,\"d\":%g,\"s\":%u}\n", id, flags, depth, stencil);
}

void trace_generate_mips(ID3D11DeviceContext* ctx, ID3D11ShaderResourceView* v) {
    std::lock_guard<std::recursive_mutex> lock(g_lock);
    ContextState& st = state_of(ctx);
    int id = view_id(v);
    append(st.events, "{\"k\":\"GenMips\",\"v\":%d}\n", id);
}

void trace_clear_state(ID3D11DeviceContext* ctx) {
    std::lock_guard<std::recursive_mutex> lock(g_lock);
    append(state_of(ctx).events, "{\"k\":\"ClearState\"}\n");
}

void trace_note(ID3D11DeviceContext* ctx, const char* text) {
    std::lock_guard<std::recursive_mutex> lock(g_lock);
    append(state_of(ctx).events, "{\"k\":\"Note\",\"text\":\"%s\"}\n", text);
}

void trace_map(ID3D11DeviceContext* ctx, ID3D11Resource* res, UINT, D3D11_MAP type, const D3D11_MAPPED_SUBRESOURCE* mapped) {
    if (type == D3D11_MAP_READ || !is_constant_buffer(res)) return;
    std::lock_guard<std::recursive_mutex> lock(g_lock);
    state_of(ctx).open_maps[res] = *mapped;
}

void trace_unmap(ID3D11DeviceContext* ctx, ID3D11Resource* res, UINT) {
    std::lock_guard<std::recursive_mutex> lock(g_lock);
    ContextState& st = state_of(ctx);
    auto it = st.open_maps.find(res);
    if (it == st.open_maps.end()) return;
    D3D11_BUFFER_DESC d;
    static_cast<ID3D11Buffer*>(res)->GetDesc(&d);
    // Reading a write-combined mapping is slow but valid for these few traced frames.
    store_cb(st, res, it->second.pData, d.ByteWidth);
    st.open_maps.erase(it);
}

void trace_update_subresource(ID3D11DeviceContext* ctx, ID3D11Resource* res, UINT, const D3D11_BOX* box, const void* data, UINT) {
    if (!data || !is_constant_buffer(res)) return;
    std::lock_guard<std::recursive_mutex> lock(g_lock);
    D3D11_BUFFER_DESC d;
    static_cast<ID3D11Buffer*>(res)->GetDesc(&d);
    store_cb(state_of(ctx), res, data, box ? box->right - box->left : d.ByteWidth);
}

void trace_finish_before(ID3D11DeviceContext* ctx) {
    std::lock_guard<std::recursive_mutex> lock(g_lock);
    ContextState& st = state_of(ctx);
    snapshot_outputs(ctx, st);
    release_outputs(st.outputs);
}

void trace_finish_after(ID3D11DeviceContext* ctx, ID3D11CommandList* list) {
    std::lock_guard<std::recursive_mutex> lock(g_lock);
    ContextState& st = state_of(ctx);
    ListRecord rec;
    rec.events = std::move(st.events);
    rec.dumps = std::move(st.dumps);
    rec.ctx_index = st.index;
    rec.thread = st.thread;
    st.events.clear();
    st.dumps.clear();
    st.cb.clear();
    g_lists[list] = std::move(rec);
}

void trace_execute_before(ID3D11DeviceContext* ctx, ID3D11CommandList*) {
    std::lock_guard<std::recursive_mutex> lock(g_lock);
    ContextState& st = state_of(ctx);
    snapshot_outputs(ctx, st);
    release_outputs(st.outputs);
    flush_immediate(st, g_writing);
}

void trace_execute_after(ID3D11DeviceContext* ctx, ID3D11CommandList* list) {
    std::lock_guard<std::recursive_mutex> lock(g_lock);
    ContextState& st = state_of(ctx);
    read_back(st.dumps, ctx, g_writing);
    auto it = g_lists.find(list);
    if (it == g_lists.end()) {
        if (g_writing && g_out) fprintf(g_out, "{\"k\":\"ExecList\",\"ctx\":-1,\"note\":\"recorded before tracking\"}\n");
        return;
    }
    if (g_writing && g_out) {
        fprintf(g_out, "{\"k\":\"ExecList\",\"ctx\":%d,\"thread\":%lu,\"bytes\":%zu}\n", it->second.ctx_index, it->second.thread, it->second.events.size());
        fwrite(it->second.events.data(), 1, it->second.events.size(), g_out);
    }
    read_back(it->second.dumps, ctx, g_writing);
    g_lists.erase(it);
}

void trace_shot_poll(IDXGISwapChain* swapchain, ID3D11DeviceContext* immediate, ID3D11Device* device) {
    static const std::wstring trigger = data_dir() + L"trigger_shot";
    if ((g_frame & 3) != 0 || !file_exists(trigger)) return;
    wchar_t path[MAX_PATH] = {};
    FILE* f = nullptr;
    _wfopen_s(&f, trigger.c_str(), L"rt, ccs=UTF-8");
    if (f) {
        fgetws(path, MAX_PATH, f);
        fclose(f);
    }
    DeleteFileW(trigger.c_str());
    size_t n = wcslen(path);
    while (n && (path[n - 1] == L'\n' || path[n - 1] == L'\r')) path[--n] = 0;
    if (!n) return;
    ID3D11Texture2D* bb = nullptr;
    if (FAILED(swapchain->GetBuffer(0, IID_PPV_ARGS(&bb)))) return;
    D3D11_TEXTURE2D_DESC d;
    bb->GetDesc(&d);
    D3D11_TEXTURE2D_DESC s = d;
    s.Usage = D3D11_USAGE_STAGING;
    s.BindFlags = 0;
    s.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    s.MiscFlags = 0;
    ID3D11Texture2D* staging = nullptr;
    if (SUCCEEDED(device->CreateTexture2D(&s, nullptr, &staging))) {
        immediate->CopyResource(staging, bb);
        write_dds(path, staging, d, immediate);
        staging->Release();
    }
    bb->Release();
}

void trace_snapshot_uav0(ID3D11DeviceContext* ctx) {
    std::lock_guard<std::recursive_mutex> lock(g_lock);
    ID3D11UnorderedAccessView* uav = nullptr;
    ctx->CSGetUnorderedAccessViews(0, 1, &uav);
    if (!uav) return;
    ID3D11Resource* res = nullptr;
    uav->GetResource(&res);
    snapshot(ctx, state_of(ctx), res, "engine");
    if (res) res->Release();
    uav->Release();
}

void trace_snapshot_resource(ID3D11DeviceContext* ctx, ID3D11Resource* res, const char* tag) {
    std::lock_guard<std::recursive_mutex> lock(g_lock);
    snapshot(ctx, state_of(ctx), res, tag);
}

void trace_before_present(IDXGISwapChain* swapchain, ID3D11DeviceContext* immediate) {
    g_frame++;
    if (!g_trace_tracking) return;
    std::lock_guard<std::recursive_mutex> lock(g_lock);
    ContextState& st = state_of(immediate);
    snapshot_outputs(immediate, st);
    release_outputs(st.outputs);
    if (g_writing) {
        ID3D11Texture2D* bb = nullptr;
        if (SUCCEEDED(swapchain->GetBuffer(0, IID_PPV_ARGS(&bb)))) {
            append(st.events, "{\"k\":\"Present\"}\n");
            snapshot(immediate, st, bb, "backbuffer");
            bb->Release();
        }
    }
    flush_immediate(st, g_writing);
    read_back(st.dumps, immediate, g_writing);
}

void trace_after_present(ID3D11Device* device) {
    g_device = device;
    // Tracking runs for two frames before the written one, and stops right after it.
    static int stage;
    std::lock_guard<std::recursive_mutex> lock(g_lock);
    if (stage == 3) {
        stage = 0;
        g_writing = false;
        g_trace_tracking = false;
        if (g_out) { fclose(g_out); g_out = nullptr; }
        if (g_cb) { fclose(g_cb); g_cb = nullptr; }
        for (auto& kv : g_lists) {
            for (auto& d : kv.second.dumps) d.staging->Release();
        }
        g_lists.clear();
        for (auto& kv : g_contexts) {
            release_outputs(kv.second.outputs);
            for (auto& d : kv.second.dumps) d.staging->Release();
        }
        g_contexts.clear();
        g_cb_global.clear();
        logf("trace finished: %d dumps", g_dump_count);
        return;
    }
    if (stage == 2) {
        stage = 3;
        g_writing = true;
        return;
    }
    if (stage == 1) {
        stage = 2;
        return;
    }
    const std::wstring trigger = data_dir() + L"trigger_trace";
    if ((g_frame & 7) != 0 || !file_exists(trigger)) return;
    // The trigger's first byte selects the dump depth: "n" = no render-target dumps.
    char mode = 0;
    FILE* tf = nullptr;
    _wfopen_s(&tf, trigger.c_str(), L"rb");
    if (tf) {
        fread(&mode, 1, 1, tf);
        fclose(tf);
    }
    DeleteFileW(trigger.c_str());
    g_dump_targets = mode != 'n';

    wchar_t sub[64];
    swprintf_s(sub, L"trace\\f%llu\\", static_cast<unsigned long long>(g_frame));
    CreateDirectoryW((data_dir() + L"trace").c_str(), nullptr);
    g_dir = data_dir() + sub;
    CreateDirectoryW(g_dir.c_str(), nullptr);
    _wfopen_s(&g_out, (g_dir + L"trace.jsonl").c_str(), L"wb");
    _wfopen_s(&g_cb, (g_dir + L"cb.bin").c_str(), L"wb");
    if (g_out) setvbuf(g_out, nullptr, _IOFBF, 1 << 20);
    g_cb_offset = 0;
    g_dump_count = 0;
    g_described.clear();
    g_cb_written.clear();
    g_trace_tracking = true;
    stage = 1;
    logf("trace armed: frame %llu dir %S", static_cast<unsigned long long>(g_frame), g_dir.c_str());
}
