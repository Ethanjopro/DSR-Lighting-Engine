// Ground-truth ambient occlusion in place of the game's SSAO chain. It writes the game's own AO texture,
// so the forward shaders keep reading t8.r unchanged.
#pragma once
#include <d3d11.h>
#include "engine.h"

void gtao_init(ID3D11Device* device);
void gtao_on_present();
// At the first SSAO dispatch: remember the scene depth SRV the game binds there.
void gtao_note_depth(ID3D11DeviceContext* ctx);
// At the final upsample dispatch. Returns false to let the game's dispatch run instead.
bool gtao_run(ID3D11DeviceContext* ctx, const ViewConstants& view);
