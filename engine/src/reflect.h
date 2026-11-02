// Screen-space reflections. Two halves:
//
// A surface target. While the game has opaque forward bound, two more targets go on the output merger behind its two:
// RT2 (normal, roughness, reflectance) and RT3 (the specular probe term the shader added). Opaque forward is told apart
// from the later blended forward draws, which bind the same RT0 + RT1, by the blend state: every traced frame has
// IndependentBlendEnable 1 and RT0 unblended with mask RGB on the one, and IndependentBlendEnable 0 with blending on the
// other (captures/ab1, ab2, ab5, e2: Burg, Catacombs, Firelink, Parish). The depth buffer must also be the scene's, which
// keeps out the planar reflection pass. Pixel shaders that do not write these targets get a write mask of 0 for them.
//
// The pass. At the refraction copy it traces against the depth, composites into RT0 and leaves the pyramid and its constants
// bound for the water draws of the same list.
//
// Which copy. The game also copies the scene in its main list, just before TAA, and it records its lists in any order, so
// "the first copy recorded" is sometimes that one (captures e2/parish, ab2/burg). What tells them apart is what the list did
// before its copy. In all 11 usable traces the refraction copy's list has done nothing but draw the water mask on the scene's
// targets, or nothing at all, and the main list opens by clearing the velocity target and drawing on it. All of this holds
// whatever the recording order, because the lists only ever execute in one: opaque, then this copy, then the main list.
#pragma once
#include <d3d11.h>
#include "engine.h"

struct ShaderInfo;

// One per wrapped context: what the game last asked of the output merger.
struct ReflectTrack {
    bool armed = false;                      // the game has opaque forward's targets bound, and the feature is on
    bool bound = false;                      // this context really has RT2 and RT3 on it right now
    bool scene_targets = false;              // the game has RT0, RT1 and the scene's depth bound, feature on or off

    // What the list being recorded has done so far. Outlives a ClearState, not the list.
    struct {
        bool elsewhere = false;              // has cleared a target or drawn on anything else: not the copy's list
        bool begun = false;                  // has cleared the surfaces
        bool view_fresh = false;             // the wrapper has read camera rows in this list, not just kept the last list's
        bool view_noted = false;             // has told the pass about them
    } list;

    ID3D11RenderTargetView* targets[2] = {}; // not owned: bound on the context for as long as armed is set
    ID3D11DepthStencilView* depth = nullptr; // same

    ID3D11BlendState* blend = nullptr;       // not owned: what the game set, bound unless applied says otherwise
    FLOAT factor[4] = {1.0f, 1.0f, 1.0f, 1.0f};
    UINT sample_mask = 0xffffffffu;
    ID3D11BlendState* applied = nullptr;     // the variant standing in for it right now, or null

    ID3D11BlendState* memo_blend = nullptr;  // last lookup: this state's variants
    ID3D11BlendState* memo_with = nullptr;
    ID3D11BlendState* memo_without = nullptr;
    bool memo_opaque = false;

    const ShaderInfo* ps = nullptr;          // replacement in use right now, or null for the game's own shader
    const ShaderInfo* memo_ps = nullptr;     // last lookup
    bool memo_writes = false;
};

void reflect_init(ID3D11Device* device);
// Whether the feature may run: the forward replacements have to be in use for anything to write the targets.
void reflect_read_settings(bool allowed);
// Once per frame. `camera` is the last published absolute camera, `fresh` whether it was published during this frame.
void reflect_on_present(const float camera[3], bool fresh);

inline void reflect_track_shader(ReflectTrack& t, const ShaderInfo* replaced) { t.ps = replaced; }
void reflect_track_reset(ReflectTrack& t);
// Called before the game's own call is forwarded. They may put the game's blend state back on ctx.
void reflect_on_targets(ReflectTrack& t, ID3D11DeviceContext* ctx, UINT count, ID3D11RenderTargetView* const* views, ID3D11DepthStencilView* depth);
void reflect_on_blend(ReflectTrack& t, ID3D11BlendState* state, const FLOAT factor[4], UINT sample_mask);
// While t.armed: adds the targets and picks the blend variant, or takes them off again. `view` is the list's camera rows.
void reflect_before_draw(ReflectTrack& t, ID3D11DeviceContext* ctx, const ViewConstants& view);
// Before every draw the game makes.
inline void reflect_on_draw(ReflectTrack& t, ID3D11DeviceContext* ctx, const ViewConstants& view) {
    if (t.armed) reflect_before_draw(t, ctx, view);
    if (!t.scene_targets) t.list.elsewhere = true;
}
// The game clearing any render target.
inline void reflect_on_clear(ReflectTrack& t) { t.list.elsewhere = true; }
// The wrapper has just read camera rows from a mapped constant block.
inline void reflect_on_view(ReflectTrack& t, const ViewConstants& view) { t.list.view_fresh = view.valid; }
// The list is finished; with restore the bindings live on, but what the next list has done starts over.
inline void reflect_track_list_end(ReflectTrack& t) { t.list = {}; }

// Cheap check for the anchor, once the caller knows it is in the right list: is this a copy of the scene, not yet run this frame?
bool reflect_wants_copy(ID3D11Resource* src);
// Runs the pass into ctx, and leaves the water shaders' inputs bound. Skips silently when anything is missing.
// `view` is the anchor's rows, for when the opaque list has not recorded its first draw yet.
// `camera` is the absolute camera as published so far and `fresh` whether that happened during this frame.
void reflect_on_scene_copy(ID3D11DeviceContext* ctx, ID3D11ShaderResourceView* depth, const ViewConstants& view, const float camera[3], bool fresh);
