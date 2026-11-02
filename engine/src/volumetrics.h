// Additive froxel volumetrics over the opaque scene: glow around the game's clustered lights and a faint haze in
// the area's fog colour. Vanilla's per-object fog is left alone and stays as the colour anchor.
#pragma once
#include <d3d11.h>
#include "engine.h"

void volumetrics_init(ID3D11Device* device);
void volumetrics_on_present();
// Once per frame, after opaque forward and before water and blended draws, on the list being recorded. depth_srv is
// the hardware depth view, not the linear Z texture: linear values would read as far depth and fog the whole scene.
// lights are the game's t16, t17 and t18, read as they stand when this list executes: a list that executes before the
// one that maps them shows the previous frame's lights. Does nothing if anything it needs is missing.
void volumetrics_run(ID3D11DeviceContext* ctx, ID3D11RenderTargetView* scene_rtv, ID3D11ShaderResourceView* depth_srv,
                     ID3D11ShaderResourceView* const lights[3], const ViewConstants& view, const ForwardConstants& forward);
// Only needed for sun in-scatter, which is off by default: offer the game's t7 when a forward draw has it bound.
// A list that starts after forward has no t7 of its own to read. Views of any other format are ignored.
void volumetrics_note_shadow_atlas(ID3D11ShaderResourceView* srv);
