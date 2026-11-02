// Wide bloom: the scene blurred down a mip chain and back up. It has no threshold, since a real lens scatters a little of
// everything, so it stays in proportion whatever the exposure. The game's own bright-pass bloom still runs on top.
#pragma once
#include <d3d11.h>

void bloom_init(ID3D11Device* device);
// Right before the composite's draw, with its inputs bound: reads the scene at t0, leaves the result at t4.
void bloom_run(ID3D11DeviceContext* ctx);
// After that draw, so the result can be written again.
void bloom_unbind(ID3D11DeviceContext* ctx);
