// Runtime HLSL for the engine's own passes. Sources live in dsrle\hlsl\ and are compiled through
// d3dcompiler_47.dll on first use; sources are hot-reloaded when any file in the folder changes.
#pragma once
#include <d3d11.h>
#include <string>
#include <vector>

struct ShaderDefine { const char* name; const char* value; };

void hlsl_init(ID3D11Device* device);
// Once per frame. True when a source changed, so holders should drop their shaders and re-request them.
bool hlsl_poll();

// Return null on a compile failure (logged); the caller skips its pass that frame. The result is owned
// by the shader cache, stays valid until hlsl_poll() returns true, and needs no Release.
ID3D11ComputeShader* hlsl_cs(const char* file, const char* entry, const std::vector<ShaderDefine>& defines = {});
ID3D11PixelShader* hlsl_ps(const char* file, const char* entry, const std::vector<ShaderDefine>& defines = {});
ID3D11VertexShader* hlsl_vs(const char* file, const char* entry, const std::vector<ShaderDefine>& defines = {});
