// dsrle\shaders.pak: replacement shaders for game shaders, keyed by the vanilla DXBC checksum.
// shaders/tools/build_pack.py documents the format.
#pragma once
#include <cstddef>
#include <cstdint>

void pack_load();
// Replacement bytecode for the vanilla blob with this checksum, or null.
const void* pack_find(uint64_t hash_lo, uint64_t hash_hi, size_t* size);
