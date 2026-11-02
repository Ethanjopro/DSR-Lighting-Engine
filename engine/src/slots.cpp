// CINTERFACE makes the SDK declare ID3D11DeviceVtbl and friends as plain structs.
#define CINTERFACE
#define COBJMACROS
#define D3D11_NO_HELPERS  // the CD3D11_* helpers call C++ methods, which CINTERFACE removes
#include <d3d11.h>
#include <dxgi.h>
#include <cstddef>
#include "slots.h"

#define SLOT_OF(type, member) static_cast<int>(offsetof(type, member) / sizeof(void*))

#define DEFINE_DEVICE_SLOT(name) extern const int slot_dev_##name = SLOT_OF(ID3D11DeviceVtbl, name);
DSRLE_DEVICE_SLOTS(DEFINE_DEVICE_SLOT)

extern const int slot_factory_CreateSwapChain = SLOT_OF(IDXGIFactoryVtbl, CreateSwapChain);
extern const int slot_swapchain_Present = SLOT_OF(IDXGISwapChainVtbl, Present);
extern const int slot_swapchain_ResizeBuffers = SLOT_OF(IDXGISwapChainVtbl, ResizeBuffers);
