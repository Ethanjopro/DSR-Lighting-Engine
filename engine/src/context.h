// COM wrapper around every ID3D11DeviceContext the game holds. The runtime keeps a per-object vtable in
// heap memory for contexts and can rewrite it, so vtable patches do not stick (engine/tests/vtable_probe.cpp).
// The game gets this object instead; it forwards to the real context and sees every call.
#pragma once
#include <d3d11.h>

ID3D11DeviceContext* context_wrap(ID3D11DeviceContext* real);
