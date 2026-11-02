// Frame tracer: logs every pass of one frame with shader names, targets, bindings and constants, and
// dumps render targets. Armed by creating the file dsrle\trigger_trace.
//
// The game records its rendering on deferred contexts from worker threads, so events are buffered per
// context, attached to the command list at FinishCommandList, and written in ExecuteCommandList order.
#pragma once
#include <d3d11.h>
#include <dxgi.h>

extern bool g_trace_tracking;  // recording: events and constant-buffer contents are captured
inline bool trace_tracking() { return g_trace_tracking; }

void trace_note_input_layout(ID3D11InputLayout* layout, const D3D11_INPUT_ELEMENT_DESC* desc, UINT count);

void trace_draw(ID3D11DeviceContext* ctx, const char* kind, UINT count, UINT start, INT base, UINT inst, UINT start_inst);
void trace_dispatch(ID3D11DeviceContext* ctx, UINT x, UINT y, UINT z);
void trace_copy(ID3D11DeviceContext* ctx, const char* kind, ID3D11Resource* dst, ID3D11Resource* src);
void trace_clear_rtv(ID3D11DeviceContext* ctx, ID3D11RenderTargetView* view, const FLOAT color[4]);
void trace_clear_dsv(ID3D11DeviceContext* ctx, ID3D11DepthStencilView* view, UINT flags, FLOAT depth, UINT8 stencil);
void trace_generate_mips(ID3D11DeviceContext* ctx, ID3D11ShaderResourceView* view);
void trace_clear_state(ID3D11DeviceContext* ctx);
void trace_note(ID3D11DeviceContext* ctx, const char* text);

void trace_map(ID3D11DeviceContext* ctx, ID3D11Resource* res, UINT sub, D3D11_MAP type, const D3D11_MAPPED_SUBRESOURCE* mapped);
void trace_unmap(ID3D11DeviceContext* ctx, ID3D11Resource* res, UINT sub);
void trace_update_subresource(ID3D11DeviceContext* ctx, ID3D11Resource* res, UINT sub, const D3D11_BOX* box, const void* data, UINT row_pitch);

void trace_finish_before(ID3D11DeviceContext* ctx);
void trace_finish_after(ID3D11DeviceContext* ctx, ID3D11CommandList* list);
void trace_execute_before(ID3D11DeviceContext* ctx, ID3D11CommandList* list);
void trace_execute_after(ID3D11DeviceContext* ctx, ID3D11CommandList* list);

// Dump the back buffer as DDS to the path named in dsrle\trigger_shot, when that file exists. This is the
// image the game outputs: desktop capture on an HDR display adds the compositor's tone mapping and clips.
void trace_shot_poll(IDXGISwapChain* swapchain, ID3D11DeviceContext* immediate, ID3D11Device* device);
// After an engine pass replaced a dispatch: dump the UAV it wrote.
void trace_snapshot_uav0(ID3D11DeviceContext* ctx);
void trace_snapshot_resource(ID3D11DeviceContext* ctx, ID3D11Resource* res, const char* tag);

void trace_before_present(IDXGISwapChain* swapchain, ID3D11DeviceContext* immediate);
void trace_after_present(ID3D11Device* device);
