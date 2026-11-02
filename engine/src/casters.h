// The world seen so far. The game only submits what its camera can see, so a shadow that depends on this frame's draws
// loses every caster behind the player. This store keeps each static prepass draw it has ever seen, holding references
// to what the draw needs, and replays them into any view. Skinned draws and wind foliage are kept for one frame only.
//
// Coordinates. The game's palettes are relative to the camera, so a static object's numbers change whenever the camera
// moves. The store holds them in its own fixed frame, in which the camera of frame N sits at camera(N). That position
// comes from the static draws themselves: each draw already stored votes stored - incoming, and they agree to a fraction
// of a millimetre, where the TAA pass's absolute camera is a frame late. TAA only seeds the frame and spots warps.
//
// Threads. Ingest and replay run at the first SSAO dispatch of each frame, on whichever recording thread reaches it.
// casters_on_present runs on the render thread and mostly talks to them through a queue; its one use of the store is
// letting go of everything when no frame has been ingested for a while, as during a loading screen.
#pragma once
#include <d3d11.h>
#include <cstdint>
#include <vector>
#include "engine.h"
#include "sunshadow.h"

void casters_init(ID3D11Device* device);
void casters_read_settings();
// From the device's CreateInputLayout: the store needs to know where POSITION and the bone data sit in a vertex.
void casters_note_input_layout(ID3D11InputLayout* layout, const D3D11_INPUT_ELEMENT_DESC* desc, UINT count);
// Once per frame on the immediate context, between frames: staging copies and readbacks for bounds, on a small budget.
void casters_on_present(ID3D11DeviceContext* immediate);

// Once per frame after the depth prepass. Takes over every reference in `list` and leaves it empty. taa_camera is the
// last absolute camera the TAA pass published. Follow the frame's replays with casters_end_frame.
void casters_ingest(CasterList& list, const ViewConstants& view, const float taa_camera[3]);
void casters_end_frame();
// Drop everything and release every reference, as a warp does.
void casters_clear();

// The camera of the frame last ingested, in store space. False before the first ingest.
bool casters_camera(float out[3]);
// Bumped by every clear: all earlier store-space positions are void.
uint32_t casters_generation();
// Every stored placement has a serial. A view rendered at serial S already holds every placement up to S.
uint32_t casters_serial();

// Where the store changed since the last call. Consumed once per frame by whoever caches renders of it.
struct CasterEvent {
    float min[3], max[3];
    uint32_t serial;  // the placement's serial; a cached view rendered at or past it already has it
    bool forced;      // something moved away or was removed: the cached view is stale whatever its serial
};
void casters_take_events(std::vector<CasterEvent>& out);

// A view volume in store space.
struct CasterPlanes {
    float p[6][4];
};
void caster_planes_from_rows(const float rows[16], const float camera[3], CasterPlanes& out);
bool caster_box_outside(const CasterPlanes& planes, const float min[3], const float max[3]);
bool caster_box_outside_sphere(const float sphere[4], const float min[3], const float max[3]);

struct CasterReplay {
    const float* rows = nullptr;    // c0..c3 for this view: rows x, y, z, w of a camera-relative matrix
    const float* sphere = nullptr;  // optional x, y, z, radius in store space: only draws touching it
    float min_radius = 0.0f;        // stored draws with a known bound smaller than this are skipped
    UINT budget = 0;                // at most this many stored draws, largest first; 0 = no limit. Frame draws always go
    bool store = true;              // the persistent draws
    bool frame = true;              // this frame's dynamic draws
    bool sun_only = false;          // also instanced and NtoA draws, which only replay correctly against the same frame's state
    bool two_sided = false;         // ignore the recorded cull mode
    const float* skip_containing = nullptr;  // a character's frame draw whose box holds this point is left out
    ID3D11RasterizerState* (*raster)[2] = nullptr;  // [none, front, back][front_ccw]
};
// Records the matching draws. The caller owns targets, viewport, depth state and saving the pipeline. Returns draws recorded.
UINT casters_replay(ID3D11DeviceContext* ctx, const CasterReplay& replay);
// Which of `count` volumes (at most 32) hold at least one of this frame's dynamic draws. Bit i set for volume i.
// skip_containing as in CasterReplay.
uint32_t casters_frame_overlap(const CasterPlanes* planes, UINT count, const float sphere[4], const float* skip_containing = nullptr);

struct CasterStats {
    uint32_t meshes = 0, placements = 0, unbounded = 0, frame_draws = 0;
    uint32_t generation = 0, serial = 0;
    uint32_t votes = 0, inliers = 0, requests_pending = 0;
    uint32_t replayed = 0;  // draws recorded since the last ingest, by every view
    uint64_t moves = 0, movers = 0, forgotten = 0, far_evicted = 0, cap_evicted = 0;
    uint64_t no_overlap_frames = 0, cut_clears = 0, idle_clears = 0;
    uint64_t bounds_read = 0, bounds_failed = 0;
};
CasterStats casters_stats();
