// Longer reach for the game's point lights. Its lights are small (most end at about 3 m), which leaves their shadows little
// to fall on. Scaling a light's radius in its upload is not enough, because the shaders only look at a light in the cluster
// cells the game listed it in, so the cells are rebuilt for the new spheres and uploaded again in the same list.
#pragma once
#include <d3d11.h>

bool lightreach_enabled();
// At Unmap of one of the game's light buffers, before the mapping closes: kind is pointshadow_upload_kind's.
void lightreach_on_upload(ID3D11DeviceContext* ctx, int kind, ID3D11Resource* res, void* data);
// After that Unmap: once a list has uploaded all three buffers, replaces the cells and ids.
void lightreach_finish(ID3D11DeviceContext* ctx);
