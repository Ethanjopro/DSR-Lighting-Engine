// World sun shadows. The game's own atlas holds only the player and a few objects, out to as little as 10 m, with
// the world's shadows baked or absent. This renders the depth prepass's draws again into four stable orthographic
// cascades reaching a few hundred metres, and the engine's forward shaders read the result.
//
// The camera in the vertex constants is c0..c3 alone, applied after the object palette, so replaying a draw with
// those four registers swapped needs no new vertex shaders.
#pragma once
#include <d3d11.h>
#include <cstdint>
#include <vector>
#include "engine.h"

// One recorded prepass draw. Holds references until the replay releases them.
struct CasterDraw {
    ID3D11InputLayout* layout = nullptr;
    D3D11_PRIMITIVE_TOPOLOGY topology = D3D11_PRIMITIVE_TOPOLOGY_UNDEFINED;
    ID3D11Buffer* vb[2] = {};
    UINT stride[2] = {}, offset[2] = {};
    ID3D11Buffer* ib = nullptr;
    DXGI_FORMAT index_format = DXGI_FORMAT_UNKNOWN;
    UINT index_offset = 0;
    ID3D11VertexShader* vs = nullptr;
    ID3D11Buffer* vs_b0 = nullptr;
    ID3D11Buffer* vs_b2 = nullptr;
    ID3D11PixelShader* ps = nullptr;       // set only for alpha-tested draws
    ID3D11ShaderResourceView* ps_t0 = nullptr;
    ID3D11SamplerState* ps_s0 = nullptr;
    ID3D11Buffer* ps_b1 = nullptr;
    D3D11_CULL_MODE cull = D3D11_CULL_BACK;
    bool front_ccw = false;
    UINT constants_at = 0;                 // into CasterList::vs_constants, kCasterVsBytes long
    uint8_t ps_b1_bytes[48] = {};
    UINT index_count = 0, instance_count = 1, start_index = 0, start_instance = 0;
    INT base_vertex = 0;
    bool instanced = false;
};

constexpr UINT kCasterVsBytes = 2288;

struct CasterList {
    std::vector<CasterDraw> draws;
    std::vector<uint8_t> vs_constants;
};

void sunshadow_init(ID3D11Device* device);
bool sunshadow_enabled();
void sunshadow_read_settings();
// Render the cascades from the caster store, after casters_ingest. `camera` is the store's camera, used to keep shadow
// texels fixed in the world as the camera moves.
void sunshadow_render(ID3D11DeviceContext* ctx, const ViewConstants& view, const float sun_travel[3], const float camera[3]);
void caster_list_release(CasterList& list);

// Shader-visible results.
ID3D11ShaderResourceView* sunshadow_srv();
struct SunShadowConstants {
    float split[4];        // far view Z of each cascade; 0 in split[3] means no data yet
    float uvz[12][4];      // three rows per cascade: camera-relative position -> atlas u, v, depth
    float param[4];        // x normal offset in texels, y filter radius in texels, z 1/atlas size, w unused
};
SunShadowConstants sunshadow_constants();
