// Bounce light from what is on screen. The forward shaders read the result at t25 and add albedo * t25 to their ambient,
// ahead of the AO multiply, so a pixel's bounce is shaped by its own material and occlusion and coloured by whatever lit
// surfaces it can see.
//
// Passes record at two anchors of one frame: the trace at the first SSAO dispatch, and the capture of the scene at the
// refraction copy. The game records them on whichever worker threads it likes and in either order, but they execute in
// list order, so each trace reads the capture of the frame before.
#pragma once
#include <d3d11.h>
#include <cstdint>
#include "engine.h"
#include "sunshadow.h"

void ssgi_init(ID3D11Device* device);
void ssgi_on_present();

// At the first SSAO dispatch, while the prepass draws are still there: their camera-relative palettes say how far the
// camera really moved this frame. Reads the list and leaves it as it is.
void ssgi_note_casters(const CasterList& list);
// At the first SSAO dispatch, whose t0 is the scene depth. taa_camera is the last absolute camera the TAA pass published.
void ssgi_run(ID3D11DeviceContext* ctx, const ViewConstants& view, const float taa_camera[3]);
// Before each CopyResource in a list that has not drawn the depth copy: the refraction copy, which is where the capture
// runs. The game's other copy of the scene, in the main list, holds water and effects, and can be recorded first.
void ssgi_on_copy_resource(ID3D11DeviceContext* ctx, ID3D11Resource* src);
// Once per recorded list, with what the forward shaders read. Binds nothing usable when off or out of date; an empty
// slot reads as black, which adds no light.
void ssgi_bind_forward(ID3D11DeviceContext* ctx, bool active);

// Last frame's G-buffer, for ssgi.normals = 1: world normal * 0.5 + 0.5 in rgb, screen sized or not. Null when there is
// none, and the depth-derived normal is used.
void ssgi_set_gbuffer(ID3D11ShaderResourceView* srv);

// For tests and the log.
struct SsgiStats {
    float camera_delta[3] = {};   // how far the camera moved, as used for reprojection
    bool delta_from_draws = false;
    bool history_ok = false;
    bool colour_ok = false;
    uint32_t runs = 0, captures = 0, resets = 0;
};
SsgiStats ssgi_stats();
// A new reference, or null.
ID3D11ShaderResourceView* ssgi_output();
