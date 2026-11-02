// Vtable slot numbers, computed from the SDK's C interface declarations in slots.cpp so they can
// never disagree with the headers.
#pragma once

#define DSRLE_DEVICE_SLOTS(X) \
    X(CreateBuffer) X(CreateTexture2D) X(CreateTexture3D) X(CreateShaderResourceView) \
    X(CreateUnorderedAccessView) X(CreateRenderTargetView) X(CreateDepthStencilView) \
    X(CreateInputLayout) X(CreateVertexShader) X(CreateGeometryShader) \
    X(CreateGeometryShaderWithStreamOutput) X(CreatePixelShader) X(CreateHullShader) \
    X(CreateDomainShader) X(CreateComputeShader) X(CreateSamplerState) X(CreateDeferredContext) \
    X(GetImmediateContext)

#define DSRLE_DECLARE_DEVICE_SLOT(name) extern const int slot_dev_##name;
DSRLE_DEVICE_SLOTS(DSRLE_DECLARE_DEVICE_SLOT)

extern const int slot_factory_CreateSwapChain;
extern const int slot_swapchain_Present;
extern const int slot_swapchain_ResizeBuffers;
