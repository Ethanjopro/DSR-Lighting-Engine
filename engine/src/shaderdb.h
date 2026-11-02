// Identity of every shader object the game creates, keyed by object pointer.
#pragma once
#include "engine.h"
#include <cstdint>
#include <string>

struct ShaderInfo {
    uint64_t hash_lo = 0;      // first 8 bytes of the 16-byte DXBC checksum
    uint64_t hash_hi = 0;      // last 8 bytes
    std::string name;          // archive entry name, or "?<hex>" when not in the table
    const char* stage = "";    // "vs", "ps", "cs", ...
    Role role = Role::None;
    ID3D11PixelShader* replacement = nullptr;  // engine version of this pixel shader, from the shader pack
};

void shaderdb_load();
void shaderdb_register(void* shader_object, const void* bytecode, size_t size, const char* stage);
// Attach an engine replacement to a registered pixel shader. The tracer reports it under the original's name.
void shaderdb_set_replacement(void* shader_object, ID3D11PixelShader* replacement);
const ShaderInfo* shaderdb_find(void* shader_object);
