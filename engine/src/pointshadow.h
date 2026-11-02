// Shadow-casting torches and bonfires. Vanilla's only point-light shadow is a baked term on lightmapped surfaces, so
// nothing casts dynamically. This picks the lights that matter most each frame and gives each one a depth cube.
//
// A cube array, not tiles in one big texture. The hardware picks the face from a direction and filters across face edges,
// so there is no seam to guard against. And each face is its own subresource, where D3D11 only copies a depth surface
// whole, so one face can be copied at all.
//
// Two arrays, read as one. The static array holds the world from the caster store and is re-rendered only when a light
// is new or moved, or the store grew inside its radius. The array the shaders read starts as a copy of it, and each frame
// the faces that have a character in them are copied again and the character drawn on top. Depth testing then gives the
// nearer of the two at every texel, which is exactly the worse of two lookups and costs no extra shader taps.
//
// Order. The game records its lists in any order and on any thread: in every traced frame the forward list is recorded
// largely before the prepass list reaches the anchor. It only ever executes them in one order, and it finishes recording a
// frame before it presents that frame. So anything the lit draws read beside the cubes goes by execution, not recording:
// b4 and the slot map are default buffers written on the immediate context, just before each list that could read them,
// and a light upload only counts once the list holding it has executed. The cubes themselves are drawn at the anchor, in
// the prepass list, so at any lit draw they and their constants come from the same anchor.
#pragma once
#include <d3d11.h>
#include <cstdint>

void pointshadow_init(ID3D11Device* device);
void pointshadow_read_settings();
bool pointshadow_enabled();

// The game's light buffers, on any context: which one a Map targets (0 for none of them), then its contents at Unmap.
int pointshadow_upload_kind(ID3D11Resource* res);
// Reads a mapped buffer that the driver may have put in write-combined memory.
void pointshadow_copy_mapping(void* out, const void* mapped, size_t bytes);
void pointshadow_on_upload(ID3D11DeviceContext* ctx, int kind, const void* data);
// The context's uploads now belong to this list.
void pointshadow_on_finish(ID3D11DeviceContext* ctx, ID3D11CommandList* list);
// Just before each ExecuteCommandList, on the immediate context: the list's uploads take effect, then b4 and the slot map
// are brought up to date if anything changed.
void pointshadow_on_execute(ID3D11DeviceContext* immediate, ID3D11CommandList* list);
// After each Present.
void pointshadow_on_present();

// Once per frame, after casters_ingest and before opaque forward, on the list being recorded.
void pointshadow_render(ID3D11DeviceContext* ctx);
// Once per list, before the first engine pixel shader draws: binds b4, t20, t21.
void pointshadow_bind(ID3D11DeviceContext* ctx);

struct PointShadowStats {
    uint32_t active = 0;
    uint64_t assigned = 0, freed = 0, full_renders = 0;
    uint64_t faces_static = 0, faces_copied = 0, faces_dynamic = 0;
    uint64_t draws_static = 0, draws_dynamic = 0;
    uint32_t frame_draws_static = 0, frame_draws_dynamic = 0, frame_faces_static = 0, frame_faces_dynamic = 0;
    // What the order assumptions look like in the running game; the traces cannot show the light uploads.
    uint64_t uploads_executed = 0;    // parameter uploads, each counted when its list executed
    uint64_t anchors = 0;
    uint64_t anchors_old_lights = 0;  // anchors whose newest executed upload came from an earlier frame
    uint64_t anchors_ahead = 0;       // a second anchor before the Present: held back until it, never seen in a trace
    uint64_t frames_no_anchor = 0;    // frames with no anchor: no lit draw may use a cube in the next one until there is one
    uint64_t uploads_dropped = 0;
};
PointShadowStats pointshadow_stats();
