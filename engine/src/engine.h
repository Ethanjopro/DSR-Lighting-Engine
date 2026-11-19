// The lighting engine proper: decides what happens at each anchor the wrapped contexts report.
//
// The game records on deferred contexts from two threads. Anything here that runs at an anchor records
// into the caller's context, keeps its state to itself and must not read back from the GPU.
#pragma once
#include <d3d11.h>
#include <cstdint>

enum class Role : uint8_t {
    None,
    SsaoPrepare,
    SsaoRender,
    Foliage,
    SsaoBlendOut,
    SsaoBlurUpsample,
    Composite,
    ResolveTaa,
    MenuUi,
    ForwardLit,   // forward pixel shader with a cascaded sun lookup: its constants carry the sun direction
    NoCast,       // depth prepass draws that must not cast sun shadows: the sky dome, debug primitives
    FinalBlit,    // the game's last draw, composite target to back buffer through the brightness table
    DepthCopy,    // the half-resolution depth copy ahead of the soft effects: only the main post list draws it
};

Role role_from_shader_name(const char* name);

// VS $Globals c0..c3: the jittered, camera-relative view-projection of the list being recorded.
struct ViewConstants {
    float vp[16];
    bool valid = false;
};

// The registers of the forward pixel constants the engine reads. Valid until the next frame's copy.
constexpr int kForwardRegs = 129;
struct ForwardConstants {
    float regs[kForwardRegs][4];
    bool valid = false;
};

struct CasterList;

void engine_init(ID3D11Device* device);
void engine_on_present(ID3D11DeviceContext* immediate);  // once per frame, between frames, on the render thread
bool engine_enabled();

// Returns true when the engine took over the dispatch and the game's must not run.
bool engine_on_dispatch(ID3D11DeviceContext* ctx, Role role, UINT x, UINT y, UINT z, const ViewConstants& view, CasterList& casters);
// The pixel shader to bind instead of the game's, or null to keep it.
ID3D11PixelShader* engine_replace_ps(Role role);
// One frame's draws use many rows, some with a sun and some without. The air follows the brightest of the last frame's.
void engine_note_key_light(const float* c6);
void engine_key_light(float out[3]);
// Whether shader-pack replacements are in use right now.
bool engine_pack_enabled();
// Called just before each list executes, on the immediate context. The game records its lists in any order, and in every
// traced frame the forward list is recorded before the prepass list reaches the anchor, but it executes them in one order,
// so constants that depend on the anchor reach the GPU from here.
void engine_on_execute(ID3D11DeviceContext* immediate, ID3D11CommandList* list);
// Called once per recorded list, before the first replacement shader draws: binds what they read.
void engine_bind_pack(ID3D11DeviceContext* ctx);
// The composite's settings travel with the pack's, and its list may have bound nothing else of the pack's.
void engine_bind_composite(ID3D11DeviceContext* ctx);
// Tells the pack's shaders whether the mesh about to draw is foliage.
void engine_bind_draw_kind(ID3D11DeviceContext* ctx, bool foliage);
// Every hemisphere-lit draw, whatever its target.
void engine_note_lit_draw();
// Forward draws offer their constants; the first offer each frame is kept. The wants check is cheap.
bool engine_wants_forward_constants();
void engine_publish_forward_constants(const void* regs);
ForwardConstants engine_forward_constants();
// The TAA resolve's constants hold the absolute camera position.
bool engine_wants_camera();
void engine_publish_camera(const float* xyz);
// The resource behind the depth SRV the game binds for its first SSAO pass. Compared, never dereferenced.
void* engine_scene_depth_resource();
// The volumetrics pass needs what a forward draw has bound, and runs at the first copy of the scene.
bool engine_wants_volumetrics_inputs();
void engine_offer_volumetrics_inputs(ID3D11DeviceContext* ctx);
void engine_on_copy_resource(ID3D11DeviceContext* ctx, ID3D11Resource* src);
// The refraction copy only: opaque surfaces, before water and effects.
void engine_on_opaque_copy(ID3D11DeviceContext* ctx, ID3D11Resource* src);
// The same copy, for the reflections, which only run in the list of the refraction copy.
void engine_reflect_on_copy(ID3D11DeviceContext* ctx, ID3D11Resource* src);
// Test captures only: skip HUD and menu draws, so comparisons show the scene alone.
bool engine_hide_ui();
