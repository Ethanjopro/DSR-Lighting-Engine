// Device and swap-chain interception (vtable patches; these vtables are static data in the system DLLs).
#pragma once
#include <d3d11.h>
#include <dxgi.h>

void hooks_install_factory(IDXGIFactory* factory);
// Takes over the immediate context: returns the wrapper to hand to the game in place of `context`.
ID3D11DeviceContext* hooks_install_device(ID3D11Device* device, ID3D11DeviceContext* context);
void hooks_install_swapchain(IDXGISwapChain* swapchain);
