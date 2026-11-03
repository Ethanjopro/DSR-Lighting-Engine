// Settings in dsrle.ini. Live.
//
//   lightreach.enabled = 1
//   lightreach.reach = 1.8          how many times farther a light reaches. 1 leaves the game's lights and cells untouched
//   lightreach.max_radius = 18      metres: a light is not stretched past this, and one already past it keeps its radius
//   lightreach.stretch_limit = 4    metres of a light's radius that get stretched: a 3 m torch gets all of it, a 12 m fire a third
//   lightreach.full_distance = 18  lights within this far of the camera get the full reach...
//   lightreach.none_distance = 36   ...fading to none at this distance. Only the nearest lights have a shadow cube, and a
//                                   long reach with no shadow shines through walls
#include "lightreach.h"
#include "common.h"
#include "config.h"
#include "engine.h"
#include "pointshadow.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <mutex>

namespace {

constexpr UINT kLights = 512;
constexpr UINT kLightFloats = 12;
constexpr UINT kGridX = 16, kGridY = 8, kGridZ = 24;
constexpr UINT kCells = kGridX * kGridY * kGridZ;
constexpr UINT kIdsRead = 16384;      // of the id buffer's 196608; the game's own lists have never come near this
constexpr UINT kIdsWritten = 196608;  // the id buffer's size
constexpr UINT kPerCell = 63;         // the count fields are 6 bits
constexpr UINT kPlayerLight = 0;      // light 0 is the soft light the game keeps on the player

enum Kind { kCellsKind = 1, kIdsKind = 2, kParamsKind = 3 };  // pointshadow_upload_kind's values

std::mutex g_lock;
ID3D11DeviceContext* g_owner;
ID3D11Resource* g_cells_res;  // compared and mapped only within the list that uploaded them
ID3D11Resource* g_ids_res;
bool g_have_cells, g_have_ids, g_have_params;
// The reach can change between two uploads of one list while a map's values ease, so the first upload of a sequence
// fixes it for the other two: cells rebuilt for one reach must not meet radii stretched by another.
bool g_seq_latched, g_seq_on;
float g_seq_reach;
uint32_t g_cells[kCells];
uint32_t g_ids[kIdsRead];
float g_lights[kLights * kLightFloats];
float g_old_radius[kLights];

uint16_t g_out[2][kCells][kPerCell];
uint8_t g_out_count[2][kCells];
uint32_t g_new_cells[kCells];
uint32_t g_new_ids[kIdsWritten];

void start_for(ID3D11DeviceContext* ctx) {
    if (g_owner == ctx) return;
    g_owner = ctx;
    g_have_cells = g_have_ids = g_have_params = false;
    g_seq_latched = false;
}

// A sequence never spans lists, and nothing kept from one list may be mapped in another.
void forget(ID3D11DeviceContext* ctx) {
    if (g_owner != ctx) return;
    g_owner = nullptr;
    g_cells_res = g_ids_res = nullptr;
    g_have_cells = g_have_ids = g_have_params = false;
    g_seq_latched = false;
}

float dot_row(const float* row, const float* v) {
    return row[0] * v[0] + row[1] * v[1] + row[2] * v[2];
}

int cell_of(float ndc, UINT count) {
    return static_cast<int>(std::floor((ndc * 0.5f + 0.5f) * static_cast<float>(count)));
}

void add(UINT list, UINT cell, uint16_t id) {
    uint16_t* out = g_out[list][cell];
    uint8_t& n = g_out_count[list][cell];
    if (n >= kPerCell) return;
    for (UINT i = 0; i < n; i++) {
        if (out[i] == id) return;
    }
    out[n++] = id;
}

// Every cell the light's new sphere can touch, one cell wider all round: the matrix is a frame old by now, and a list of a
// few too many is only a little slower.
void stamp(const float* light, uint16_t id, bool in0, bool in1, const float* rx, const float* ry, const float* rw, float z_near, float z_scale) {
    const float radius = light[7] + 0.5f;
    const float p00 = std::sqrt(dot_row(rx, rx)), p11 = std::sqrt(dot_row(ry, ry));
    const float cx = dot_row(rx, light), cy = dot_row(ry, light), cz = dot_row(rw, light) + rw[3];
    const float x0 = cx - p00 * radius, x1 = cx + p00 * radius, y0 = cy - p11 * radius, y1 = cy + p11 * radius;
    for (UINT s = 0; s < kGridZ; s++) {
        const float slice_from = s == 0 ? 0.0f : z_near * std::pow(2.0f, static_cast<float>(s) / z_scale);
        const float slice_to = s == kGridZ - 1 ? 1e9f : z_near * std::pow(2.0f, static_cast<float>(s + 1) / z_scale);
        const float a = std::max({slice_from, cz - radius, z_near}), b = std::min(slice_to, cz + radius);
        if (a > b) continue;
        const float nx0 = x0 / (x0 < 0.0f ? a : b), nx1 = x1 / (x1 > 0.0f ? a : b);
        const float ny0 = y0 / (y0 < 0.0f ? a : b), ny1 = y1 / (y1 > 0.0f ? a : b);
        const int cx0 = std::max(cell_of(nx0, kGridX) - 1, 0), cx1 = std::min(cell_of(nx1, kGridX) + 1, static_cast<int>(kGridX) - 1);
        const int cy0 = std::max(cell_of(ny0, kGridY) - 1, 0), cy1 = std::min(cell_of(ny1, kGridY) + 1, static_cast<int>(kGridY) - 1);
        for (int y = cy0; y <= cy1; y++) {
            for (int x = cx0; x <= cx1; x++) {
                const UINT cell = (s * kGridY + static_cast<UINT>(y)) * kGridX + static_cast<UINT>(x);
                if (in0) add(0, cell, id);
                if (in1) add(1, cell, id);
            }
        }
    }
}

bool rebuild(ID3D11DeviceContext* ctx) {
    const ForwardConstants forward = engine_forward_constants();
    if (!forward.valid) return false;
    const float z_near = forward.regs[86][0], z_scale = forward.regs[87][0];
    if (!(z_near > 0.0f && z_scale > 0.0f)) return false;

    // The game's own lists come first, so a full cell loses only stretched lights.
    bool in_list[2][kLights] = {};
    for (UINT c = 0; c < kCells; c++) {
        const uint32_t cell = g_cells[c];
        const uint32_t n0 = cell & 0x3F, n1 = (cell >> 6) & 0x3F, offset = cell >> 12;
        if (offset + std::max(n0, n1) > kIdsRead) return false;
        g_out_count[0][c] = static_cast<uint8_t>(n0);
        g_out_count[1][c] = static_cast<uint8_t>(n1);
        for (uint32_t k = 0; k < n0; k++) {
            const uint16_t id = static_cast<uint16_t>(g_ids[offset + k] & 0x1FF);
            g_out[0][c][k] = id;
            in_list[0][id] = true;
        }
        for (uint32_t k = 0; k < n1; k++) {
            const uint16_t id = static_cast<uint16_t>((g_ids[offset + k] >> 9) & 0x1FF);
            g_out[1][c][k] = id;
            in_list[1][id] = true;
        }
    }
    for (UINT id = 1; id < kLights; id++) {
        if (!in_list[0][id] && !in_list[1][id]) continue;
        const float* light = g_lights + id * kLightFloats;
        if (light[7] <= g_old_radius[id]) continue;
        stamp(light, static_cast<uint16_t>(id), in_list[0][id], in_list[1][id], forward.regs[63], forward.regs[64], forward.regs[66], z_near, z_scale);
    }

    uint32_t written = 0;
    for (UINT c = 0; c < kCells; c++) {
        const uint32_t n0 = g_out_count[0][c], n1 = g_out_count[1][c], n = std::max(n0, n1);
        g_new_cells[c] = written << 12 | n1 << 6 | n0;
        for (uint32_t k = 0; k < n; k++) {
            const uint32_t a = k < n0 ? g_out[0][c][k] : 0, b = k < n1 ? g_out[1][c][k] : 0;
            g_new_ids[written++] = a | b << 9;
        }
    }

    D3D11_MAPPED_SUBRESOURCE mapped;
    if (FAILED(ctx->Map(g_ids_res, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) return false;
    memcpy(mapped.pData, g_new_ids, written * sizeof(uint32_t));
    ctx->Unmap(g_ids_res, 0);
    if (FAILED(ctx->Map(g_cells_res, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) return false;
    memcpy(mapped.pData, g_new_cells, sizeof(g_new_cells));
    ctx->Unmap(g_cells_res, 0);
    return true;
}

}  // namespace

bool lightreach_enabled() {
    return cfg_bool("lightreach.enabled", true) && cfg_float("lightreach.reach", 1.8f) > 1.0f && engine_enabled();
}

void lightreach_on_upload(ID3D11DeviceContext* ctx, int kind, ID3D11Resource* res, void* data) {
    if (!ctx || !data) return;
    std::lock_guard<std::mutex> lock(g_lock);
    start_for(ctx);
    if (!g_seq_latched) {
        g_seq_reach = cfg_float("lightreach.reach", 1.8f);
        g_seq_on = cfg_bool("lightreach.enabled", true) && g_seq_reach > 1.0f && engine_enabled();
        g_seq_latched = true;
    }
    // Off for the sequence: whatever an earlier one left half done must not be finished with this one's buffers.
    if (!g_seq_on) {
        g_have_cells = g_have_ids = g_have_params = false;
        return;
    }
    if (kind == kCellsKind) {
        pointshadow_copy_mapping(g_cells, data, sizeof(g_cells));
        g_cells_res = res;
        g_have_cells = true;
    } else if (kind == kIdsKind) {
        pointshadow_copy_mapping(g_ids, data, sizeof(g_ids));
        g_ids_res = res;
        g_have_ids = true;
    } else if (kind == kParamsKind) {
        pointshadow_copy_mapping(g_lights, data, sizeof(g_lights));
        const float reach = g_seq_reach;
        const float max_radius = cfg_float("lightreach.max_radius", 18.0f);
        const float stretch_limit = cfg_float("lightreach.stretch_limit", 4.0f);
        const float full = cfg_float("lightreach.full_distance", 18.0f);
        const float span = std::max(cfg_float("lightreach.none_distance", 36.0f) - full, 0.01f);
        float* out = static_cast<float*>(data);
        for (UINT id = 0; id < kLights; id++) {
            float* light = g_lights + id * kLightFloats;
            const float radius = light[7];
            g_old_radius[id] = radius;
            if (id == kPlayerLight || !(radius > 0.01f && radius < max_radius)) continue;
            // Positions are relative to the camera.
            const float distance = std::sqrt(light[0] * light[0] + light[1] * light[1] + light[2] * light[2]);
            const float t = std::clamp((distance - full) / span, 0.0f, 1.0f);
            const float scale = reach + (1.0f - reach) * t * t * (3.0f - 2.0f * t);
            // A light that is already large was placed to fill a room, so only its first few metres count.
            const float stretched = std::min(radius + (scale - 1.0f) * std::min(radius, stretch_limit), max_radius);
            if (stretched <= radius) continue;
            light[7] = stretched;
            light[3] *= radius / stretched;
            out[id * kLightFloats + 7] = light[7];
            out[id * kLightFloats + 3] = light[3];
        }
        g_have_params = true;
    }
}

void lightreach_finish(ID3D11DeviceContext* ctx) {
    std::lock_guard<std::mutex> lock(g_lock);
    if (g_owner != ctx || !g_have_cells || !g_have_ids || !g_have_params) return;
    g_have_cells = g_have_ids = g_have_params = false;
    g_seq_latched = false;
    static bool s_logged;
    const bool ok = rebuild(ctx);
    if (!s_logged && ok) {
        logf("lightreach: first cells rebuilt");
        s_logged = true;
    }
}

void lightreach_end_list(ID3D11DeviceContext* ctx) {
    std::lock_guard<std::mutex> lock(g_lock);
    forget(ctx);
}

void lightreach_on_present(ID3D11DeviceContext* immediate) {
    std::lock_guard<std::mutex> lock(g_lock);
    forget(immediate);
}
