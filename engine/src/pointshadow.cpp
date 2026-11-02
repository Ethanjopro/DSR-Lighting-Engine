// Settings in dsrle.ini. Read live, except that face_size and max_lights rebuild the arrays and start the lights over.
//   pointshadow.enabled = 1
//   pointshadow.strength = 1            how dark a shadowed pixel gets; 0 leaves every light as it was
//   pointshadow.max_lights = 8          lights with a cube, at most 16; each costs 2 x 6 x face_size^2 x 4 bytes
//   pointshadow.face_size = 1024
//   pointshadow.faces_per_frame = 6     static faces rendered per frame, at least 6 so a light can start in one frame
//   pointshadow.draws_per_frame = 1500  no new static face is started past this many draws in a frame
//   pointshadow.dynamic_draws = 400     character and foliage draws per frame, nearest-scoring lights first
//   pointshadow.normal_offset = 2       texels along the surface normal, against acne
//   pointshadow.filter_radius = 1.5     texels: edge softness
//   pointshadow.bias = 0.01             metres toward the light
//   pointshadow.slope_bias = 1.5        rasterizer slope-scaled bias
//   pointshadow.near = 0.5              metres: nearer geometry never casts, which spares a torch its bracket and a bonfire its sword
//   pointshadow.two_sided = 1           both sides of a triangle cast: many walls are single shells
//   pointshadow.edge_fade = 0.2         part of a light's radius over which its shadow fades out
//   pointshadow.match_distance = 0.75   metres: a light this close to last frame's position is the same light
//   pointshadow.move_tolerance = 0.25   metres a light moves before its cube is rendered again; flicker stays below this
//   pointshadow.max_distance = 40       metres between the camera and a light's reach; farther lights get no cube
//   pointshadow.closeness_range = 12    metres over which the score falls to half with that gap
//   pointshadow.hysteresis = 1.5        a newcomer must score this many times the weakest holder
//   pointshadow.min_hold = 60           frames before a light can lose its cube to a better one
//   pointshadow.linger = 45             frames a light keeps its cube after it stops being uploaded or referenced
//   pointshadow.fade_frames = 20        frames over which a shadow appears and leaves
//   pointshadow.player_distance = 8     the player's own light: within this far of the camera...
//   pointshadow.player_radius = 4.5     ...with at most this radius (PointLightBank's player rows reach 2.5 to 4 m), a light
//                                       gets no cube until the camera has moved away from it and it stayed where it was
//   pointshadow.unbounded_wait = 64     no light starts while more stored draws than this have no bounds yet: they would
//                                       land in all six faces. An area's first second, while the bounds are read
//   pointshadow.log_every = 0           frames between stat lines in the log; 0 = none
#include "pointshadow.h"
#include "casters.h"
#include "common.h"
#include "config.h"
#include "state.h"
#include <algorithm>
#include <atomic>
#include <cfloat>
#include <cmath>
#include <cstring>
#include <immintrin.h>
#include <memory>
#include <mutex>
#include <vector>

namespace {

constexpr UINT kMaxSlots = 16;  // must match the arrays in dsrle_features.fxh
constexpr UINT kLights = 512;
constexpr UINT kLightFloats = 12;  // stride 48
constexpr UINT kCellBytes = 12288;
constexpr UINT kIdBytes = 786432;
constexpr UINT kParamBytes = kLights * kLightFloats * sizeof(float);
constexpr UINT kIdsKept = 16384;  // of the id buffer's 196608; past this, no attempt is made to say which lights are used
constexpr int kAllFaces = 0x3F;

enum UploadKind { kUploadNone = 0, kUploadCells = 1, kUploadIds = 2, kUploadParams = 3 };

// D3D's cube faces. Each has cross(right, up) == forward, the same handedness as the game's own view basis.
struct FaceBasis {
    float right[3], up[3], forward[3];
};
constexpr FaceBasis kFaces[6] = {
    {{0, 0, -1}, {0, 1, 0}, {1, 0, 0}},    // +X
    {{0, 0, 1}, {0, 1, 0}, {-1, 0, 0}},    // -X
    {{1, 0, 0}, {0, 0, -1}, {0, 1, 0}},    // +Y
    {{1, 0, 0}, {0, 0, 1}, {0, -1, 0}},    // -Y
    {{1, 0, 0}, {0, 1, 0}, {0, 0, 1}},     // +Z
    {{-1, 0, 0}, {0, 1, 0}, {0, 0, -1}},   // -Z
};

// Must match the DsrlePointShadow cbuffer in shaders/features/dsrle_features.fxh.
struct ShaderConstants {
    float global[4];            // x strength, y normal offset per metre of distance, z tap radius in cube direction units, w edge fade
    float pos[kMaxSlots][4];    // xyz where the cube was rendered from, camera-relative this frame; w this slot's strength
    float depth[kMaxSlots][4];  // x, y: depth = x + y / major axis; z bias in metres
};
static_assert(sizeof(ShaderConstants) % 16 == 0);

struct Settings {
    bool enabled = true, two_sided = true;
    float strength = 1.0f, normal_offset = 2.0f, filter_radius = 1.5f, bias = 0.01f, slope_bias = 1.5f, near_plane = 0.5f;
    float edge_fade = 0.2f, match_distance = 0.75f, move_tolerance = 0.25f, max_distance = 40.0f, closeness_range = 12.0f;
    float hysteresis = 1.5f, player_distance = 8.0f, player_radius = 4.5f;
    int max_lights = 8, face_size = 1024, faces_per_frame = 6, draws_per_frame = 1500, dynamic_draws = 400;
    int min_hold = 60, linger = 45, fade_frames = 20, unbounded_wait = 64, log_every = 0;
} g_set;

struct Slot {
    bool used = false, leaving = false, rendered = false;
    bool replaced = false;      // gave way to a better light: leaves even though its own light is still there
    float tracked[3] = {};      // where the light was last seen, store space
    float pos[3] = {};          // where the cube was rendered from
    float want_radius = 0.0f;   // the light's own
    float radius = 0.0f;        // what the cube covers
    float depth_a = 0.0f, depth_b = 0.0f;
    float score = 0.0f;
    float fade = 0.0f;
    int age = 0, unseen = 0;
    uint8_t dirty = 0;          // static faces that must be rendered again
    uint8_t dynamic_prev = 0;   // faces of the read array that hold last frame's characters
    uint32_t face_serial[6] = {};
    uint32_t last_render = 0;   // frame it last took any of the static budget
    CasterPlanes planes[6];
};

struct Candidate {
    float pos[3];
    float radius, lum, score;
    int slot;
    float sum[3];
    int members;
};

// A small light near the camera, watched until it shows whether it belongs to the world or travels with the player.
struct Tracker {
    float pos[3], first[3], camera_first[3];
    float distance_first;
    uint32_t last;
    bool fixed, follows;
};

ID3D11Device* g_device;
Slot g_slots[kMaxSlots];
std::vector<Candidate> g_candidates;
std::vector<Tracker> g_trackers;
std::vector<CasterEvent> g_events;
uint32_t g_generation;
uint32_t g_frame;
int g_waited;

// Resources. Replaced under g_publish_lock, since binding happens from any recording thread.
int g_res_face, g_res_slots;
bool g_failed;
ID3D11Texture2D* g_static;
ID3D11Texture2D* g_read;
std::vector<ID3D11DepthStencilView*> g_static_dsv, g_read_dsv;
ID3D11ShaderResourceView* g_read_srv;
ID3D11Buffer* g_cb;
ID3D11Buffer* g_map;
ID3D11ShaderResourceView* g_map_srv;
ID3D11DepthStencilState* g_depth;
ID3D11BlendState* g_blend;
ID3D11RasterizerState* g_raster[3][2];
float g_raster_slope = -1.0f;

// One list's light uploads, held from when they are recorded until that list executes.
struct ListUpload {
    const void* owner = nullptr;  // the recording context, then the finished list; null when free
    bool finished = false;
    bool have_cells = false, have_ids = false, have_lights = false;
    uint32_t age = 0;
    uint32_t cells[kCellBytes / 4];
    uint32_t ids[kIdsKept];
    float lights[kLights * kLightFloats];
};
constexpr UINT kListUploads = 6;
std::mutex g_list_lock;
std::unique_ptr<ListUpload> g_list_uploads[kListUploads];
uint32_t g_list_age;

// The upload of the game's lights that executed last, which is the one lit draws read from here on.
std::mutex g_upload_lock;
float g_lights[kLights * kLightFloats];
uint32_t g_lights_serial;
uint32_t g_lights_present = 0xFFFFFFFFu;  // the frame it executed in
bool g_lights_camera_known;
float g_lights_camera[3];
uint32_t g_cells[kCellBytes / 4];
uint32_t g_ids[kIdsKept];
bool g_cells_seen, g_ids_seen;
// Frames are counted by Present. Also under g_upload_lock:
std::atomic<uint32_t> g_present_serial{0};
bool g_anchor_known;
uint32_t g_anchor_present;
float g_anchor_camera[3];
std::atomic<uint64_t> g_binds, g_binds_before_anchor;

// What the shaders see, as of the last render.
struct Published {
    float camera[3] = {};
    bool active[kMaxSlots] = {};
    float pos[kMaxSlots][3] = {};
    float tracked[kMaxSlots][3] = {};
    float strength[kMaxSlots] = {};
    float depth_a[kMaxSlots] = {}, depth_b[kMaxSlots] = {};
    float global[4] = {};
    float bias = 0.0f;
    float match_distance = 0.0f;
};
std::mutex g_publish_lock;
Published g_published;
uint32_t g_published_serial;
// An anchor that came before the Present of a frame which already had one belongs to the next frame.
Published g_pending;
bool g_have_pending;
bool g_this_anchor_ahead;
// Touched by the render thread only.
int g_map_values[kLights];
uint32_t g_map_lights_serial = 0xFFFFFFFFu, g_map_published_serial = 0xFFFFFFFFu;
uint32_t g_flushed_published = 0xFFFFFFFFu;

PointShadowStats g_stats;

float dist_sq(const float a[3], const float b[3]) {
    const float x = a[0] - b[0], y = a[1] - b[1], z = a[2] - b[2];
    return x * x + y * y + z * z;
}
float dot3(const float a[3], const float b[3]) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }

template <class T>
void drop(T*& p) {
    if (p) p->Release();
    p = nullptr;
}

void reset_slots() {
    for (Slot& s : g_slots) s = Slot{};
    g_trackers.clear();
}

void release_arrays() {
    drop(g_static);
    drop(g_read);
    drop(g_read_srv);
    for (auto* v : g_static_dsv) v->Release();
    for (auto* v : g_read_dsv) v->Release();
    g_static_dsv.clear();
    g_read_dsv.clear();
    g_res_face = g_res_slots = 0;
}

bool create_array(UINT face, UINT slots, ID3D11Texture2D*& tex, std::vector<ID3D11DepthStencilView*>& dsv) {
    D3D11_TEXTURE2D_DESC d{};
    d.Width = d.Height = face;
    d.MipLevels = 1;
    d.ArraySize = slots * 6;
    d.Format = DXGI_FORMAT_R32_TYPELESS;
    d.SampleDesc.Count = 1;
    d.Usage = D3D11_USAGE_DEFAULT;
    d.BindFlags = D3D11_BIND_DEPTH_STENCIL | D3D11_BIND_SHADER_RESOURCE;
    d.MiscFlags = D3D11_RESOURCE_MISC_TEXTURECUBE;
    if (FAILED(g_device->CreateTexture2D(&d, nullptr, &tex))) return false;
    for (UINT i = 0; i < slots * 6; i++) {
        D3D11_DEPTH_STENCIL_VIEW_DESC v{};
        v.Format = DXGI_FORMAT_D32_FLOAT;
        v.ViewDimension = D3D11_DSV_DIMENSION_TEXTURE2DARRAY;
        v.Texture2DArray.FirstArraySlice = i;
        v.Texture2DArray.ArraySize = 1;
        ID3D11DepthStencilView* view = nullptr;
        if (FAILED(g_device->CreateDepthStencilView(tex, &v, &view))) return false;
        dsv.push_back(view);
    }
    return true;
}

bool ensure_fixed_objects() {
    if (g_cb) return true;
    D3D11_BUFFER_DESC b{};
    // Both start zero, which means no shadow for every light, and are written at execution: see pointshadow.h.
    static const ShaderConstants zero_constants{};
    static const int zero_map[kLights] = {};
    const D3D11_SUBRESOURCE_DATA init_constants{&zero_constants, 0, 0}, init_map{zero_map, 0, 0};
    b.ByteWidth = sizeof(ShaderConstants);
    b.Usage = D3D11_USAGE_DEFAULT;
    b.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    ID3D11Buffer* cb = nullptr;
    if (FAILED(g_device->CreateBuffer(&b, &init_constants, &cb))) return false;

    b.ByteWidth = kLights * sizeof(int);
    b.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    if (FAILED(g_device->CreateBuffer(&b, &init_map, &g_map))) {
        cb->Release();
        return false;
    }
    D3D11_SHADER_RESOURCE_VIEW_DESC sv{};
    sv.Format = DXGI_FORMAT_R32_SINT;
    sv.ViewDimension = D3D11_SRV_DIMENSION_BUFFER;
    sv.Buffer.FirstElement = 0;
    sv.Buffer.NumElements = kLights;
    if (FAILED(g_device->CreateShaderResourceView(g_map, &sv, &g_map_srv))) {
        cb->Release();
        return false;
    }

    D3D11_DEPTH_STENCIL_DESC ds{};
    ds.DepthEnable = TRUE;
    ds.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ALL;
    ds.DepthFunc = D3D11_COMPARISON_LESS_EQUAL;
    if (FAILED(g_device->CreateDepthStencilState(&ds, &g_depth))) {
        cb->Release();
        return false;
    }
    D3D11_BLEND_DESC bd{};
    bd.RenderTarget[0].RenderTargetWriteMask = 0;
    if (FAILED(g_device->CreateBlendState(&bd, &g_blend))) {
        cb->Release();
        return false;
    }
    g_cb = cb;
    return true;
}

bool ensure_raster() {
    if (g_raster_slope == g_set.slope_bias && g_raster[0][0]) return true;
    const D3D11_CULL_MODE culls[3] = {D3D11_CULL_NONE, D3D11_CULL_FRONT, D3D11_CULL_BACK};
    for (int c = 0; c < 3; c++) {
        for (int w = 0; w < 2; w++) {
            drop(g_raster[c][w]);
            D3D11_RASTERIZER_DESC r{};
            r.FillMode = D3D11_FILL_SOLID;
            r.CullMode = culls[c];
            r.FrontCounterClockwise = w;
            r.DepthClipEnable = TRUE;
            r.SlopeScaledDepthBias = g_set.slope_bias;
            r.DepthBiasClamp = 0.001f;
            if (FAILED(g_device->CreateRasterizerState(&r, &g_raster[c][w]))) return false;
        }
    }
    g_raster_slope = g_set.slope_bias;
    return true;
}

bool ensure_resources() {
    const int want_face = g_set.face_size, want_slots = g_set.max_lights;
    if (g_res_face == want_face && g_res_slots == want_slots && g_read_srv) return ensure_raster();
    if (g_failed) return false;
    std::lock_guard<std::mutex> lock(g_publish_lock);
    release_arrays();
    reset_slots();
    g_published = Published{};
    g_published_serial++;
    bool ok = ensure_fixed_objects();
    ok = ok && create_array(static_cast<UINT>(want_face), static_cast<UINT>(want_slots), g_static, g_static_dsv);
    ok = ok && create_array(static_cast<UINT>(want_face), static_cast<UINT>(want_slots), g_read, g_read_dsv);
    if (ok) {
        D3D11_SHADER_RESOURCE_VIEW_DESC sv{};
        sv.Format = DXGI_FORMAT_R32_FLOAT;
        sv.ViewDimension = D3D11_SRV_DIMENSION_TEXTURECUBEARRAY;
        sv.TextureCubeArray.MipLevels = 1;
        sv.TextureCubeArray.NumCubes = static_cast<UINT>(want_slots);
        ok = SUCCEEDED(g_device->CreateShaderResourceView(g_read, &sv, &g_read_srv));
    }
    if (!ok) {
        release_arrays();
        g_failed = true;
        logf("pointshadow: arrays %dx%d x %d FAILED", want_face, want_face, want_slots * 6);
        return false;
    }
    g_res_face = want_face;
    g_res_slots = want_slots;
    logf("pointshadow: %d cubes of %dx%d", want_slots, want_face, want_face);
    return ensure_raster();
}

void face_rows(const FaceBasis& b, const float rel[3], float depth_a, float depth_b, float out[16]) {
    const float tx = -dot3(b.right, rel), ty = -dot3(b.up, rel), tw = -dot3(b.forward, rel);
    const float rows[16] = {
        b.right[0], b.right[1], b.right[2], tx,
        b.up[0], b.up[1], b.up[2], ty,
        b.forward[0] * depth_a, b.forward[1] * depth_a, b.forward[2] * depth_a, tw * depth_a + depth_b,
        b.forward[0], b.forward[1], b.forward[2], tw,
    };
    memcpy(out, rows, sizeof(rows));
}

void relative(const float pos[3], const float camera[3], float out[3]) {
    out[0] = pos[0] - camera[0];
    out[1] = pos[1] - camera[1];
    out[2] = pos[2] - camera[2];
}

// A small light that stays with the camera would shadow from about where the camera is, so every caster's shadow would
// fall directly away from the viewer. Until a small light near the camera has shown it stays put when the camera moves
// away, it gets no cube.
bool is_player_light(const float pos[3], float radius, float distance, const float camera[3]) {
    if (distance >= g_set.player_distance || radius > g_set.player_radius) return false;
    Tracker* t = nullptr;
    float best = 1.0f;
    for (Tracker& each : g_trackers) {
        const float sq = dist_sq(each.pos, pos);
        if (sq < best) {
            best = sq;
            t = &each;
        }
    }
    if (!t) {
        Tracker fresh{};
        memcpy(fresh.pos, pos, sizeof(fresh.pos));
        memcpy(fresh.first, pos, sizeof(fresh.first));
        memcpy(fresh.camera_first, camera, sizeof(fresh.camera_first));
        fresh.distance_first = distance;
        fresh.last = g_frame;
        g_trackers.push_back(fresh);
        return true;
    }
    t->last = g_frame;
    memcpy(t->pos, pos, sizeof(t->pos));
    if (dist_sq(pos, t->first) > 0.3f * 0.3f) {
        t->follows = true;
        t->fixed = false;
    }
    // A camera orbiting a standing player moves a long way and keeps its distance from the player's light. Only moving past
    // a light changes that distance, so it takes both.
    if (!t->follows && dist_sq(camera, t->camera_first) > 1.5f * 1.5f && std::fabs(distance - t->distance_first) > 1.0f) t->fixed = true;
    return !t->fixed;
}

// Which lights the clusters refer to. A cell holds an offset in the bits above 12, then a light count per list; an id
// entry holds one id per list. Past the game's light count the parameter buffer can hold anything, and this tells it apart.
// Call with g_upload_lock held. False when the clusters are not known.
bool referenced_lights(uint32_t used[kLights / 32]) {
    if (!g_cells_seen || !g_ids_seen) return false;
    memset(used, 0, kLights / 8);
    for (UINT i = 0; i < kCellBytes / 4; i++) {
        const uint32_t cell = g_cells[i];
        const uint32_t count0 = cell & 0x3F, count1 = (cell >> 6) & 0x3F, offset = cell >> 12;
        if (offset + std::max(count0, count1) > kIdsKept) return false;
        for (uint32_t k = 0; k < count0; k++) {
            const uint32_t id = g_ids[offset + k] & 0x1FF;
            used[id >> 5] |= 1u << (id & 31);
        }
        for (uint32_t k = 0; k < count1; k++) {
            const uint32_t id = (g_ids[offset + k] >> 9) & 0x1FF;
            used[id >> 5] |= 1u << (id & 31);
        }
    }
    return true;
}

void gather_candidates(const float* lights, const uint32_t* used, bool used_valid, const float light_camera[3], const float camera[3]) {
    g_candidates.clear();
    const float merge_sq = g_set.match_distance * g_set.match_distance;
    // Light 0 is the soft light the game keeps on the player in every area logged so far, with a radius of 8 to 25 m. A
    // shadow from it only turns the player into a silhouette.
    for (UINT id = 1; id < kLights; id++) {
        const float* light = lights + id * kLightFloats;
        const float radius = light[7];
        if (!(radius > 0.01f && radius < 1000.0f)) continue;
        if (used_valid && !(used[id >> 5] & (1u << (id & 31)))) continue;
        if (!(std::fabs(light[0]) < 1e5f && std::fabs(light[1]) < 1e5f && std::fabs(light[2]) < 1e5f)) continue;
        const float pos[3] = {light[0] + light_camera[0], light[1] + light_camera[1], light[2] + light_camera[2]};
        const float distance = std::sqrt(dist_sq(pos, camera));
        const float gap = std::max(distance - radius, 0.0f);
        if (gap > g_set.max_distance) continue;
        const float lum = 0.2126f * std::max(light[4], 0.0f) + 0.7152f * std::max(light[5], 0.0f) + 0.0722f * std::max(light[6], 0.0f);
        if (!(lum > 0.0f && lum < 1e6f)) continue;
        // Against the camera these positions were uploaded with, which can be a frame older than this frame's.
        if (is_player_light(pos, radius, distance, light_camera)) continue;
        const float closeness = 1.0f / (1.0f + (gap / g_set.closeness_range) * (gap / g_set.closeness_range));
        const float score = std::sqrt(lum) * radius * closeness;
        // Fires stack several lights in one place, and those can share a cube.
        Candidate* same = nullptr;
        for (Candidate& c : g_candidates) {
            if (dist_sq(c.pos, pos) <= merge_sq) {
                same = &c;
                break;
            }
        }
        if (same) {
            same->radius = std::max(same->radius, radius);
            same->lum = std::max(same->lum, lum);
            same->score = std::max(same->score, score);
            for (int k = 0; k < 3; k++) same->sum[k] += pos[k];
            same->members++;
        } else {
            g_candidates.push_back(Candidate{{pos[0], pos[1], pos[2]}, radius, lum, score, -1, {pos[0], pos[1], pos[2]}, 1});
        }
    }
    // The middle of a stack, so its place does not depend on which of its ids came first.
    for (Candidate& c : g_candidates) {
        for (int k = 0; k < 3; k++) c.pos[k] = c.sum[k] / static_cast<float>(c.members);
    }
    g_trackers.erase(std::remove_if(g_trackers.begin(), g_trackers.end(), [](const Tracker& t) { return g_frame - t.last > 120; }), g_trackers.end());
}

void select_lights() {
    const float match_sq = g_set.match_distance * g_set.match_distance;
    // Last frame's lights first: this is what keeps a flickering or renumbered light in its slot.
    for (int s = 0; s < g_set.max_lights; s++) {
        Slot& slot = g_slots[s];
        if (!slot.used) continue;
        slot.age++;
        int best = -1;
        float best_sq = match_sq;
        for (size_t c = 0; c < g_candidates.size(); c++) {
            if (g_candidates[c].slot >= 0) continue;
            const float sq = dist_sq(g_candidates[c].pos, slot.tracked);
            if (sq <= best_sq) {
                best_sq = sq;
                best = static_cast<int>(c);
            }
        }
        if (best >= 0) {
            Candidate& c = g_candidates[best];
            c.slot = s;
            memcpy(slot.tracked, c.pos, sizeof(slot.tracked));
            slot.want_radius = c.radius;
            slot.score = c.score;
            slot.unseen = 0;
            if (!slot.replaced) slot.leaving = false;
        } else if (++slot.unseen > g_set.linger) {
            slot.leaving = true;
        }
    }

    std::sort(g_candidates.begin(), g_candidates.end(), [](const Candidate& a, const Candidate& b) { return a.score > b.score; });
    bool replaced = false;
    for (Candidate& c : g_candidates) {
        if (c.slot >= 0) continue;
        int free_slot = -1;
        for (int s = 0; s < g_set.max_lights; s++) {
            if (!g_slots[s].used) {
                free_slot = s;
                break;
            }
        }
        if (free_slot >= 0) {
            Slot& slot = g_slots[free_slot];
            slot = Slot{};
            slot.used = true;
            memcpy(slot.tracked, c.pos, sizeof(slot.tracked));
            slot.want_radius = c.radius;
            slot.score = c.score;
            c.slot = free_slot;
            g_stats.assigned++;
            continue;
        }
        if (replaced) break;
        // Full: the weakest settled light gives way to a clearly better one, once. It fades out, then the slot is free.
        int weakest = -1;
        float weakest_score = FLT_MAX;
        for (int s = 0; s < g_set.max_lights; s++) {
            const Slot& slot = g_slots[s];
            if (!slot.leaving && slot.age >= g_set.min_hold && slot.score < weakest_score) {
                weakest_score = slot.score;
                weakest = s;
            }
        }
        if (weakest >= 0 && c.score > weakest_score * g_set.hysteresis) {
            g_slots[weakest].leaving = true;
            g_slots[weakest].replaced = true;
        }
        replaced = true;
    }

    const float step = 1.0f / static_cast<float>(std::max(g_set.fade_frames, 1));
    for (int s = 0; s < g_set.max_lights; s++) {
        Slot& slot = g_slots[s];
        if (!slot.used) continue;
        if (slot.leaving) {
            slot.fade -= step;
            if (slot.fade <= 0.0f || !slot.rendered) {
                slot = Slot{};
                g_stats.freed++;
            }
        } else if (slot.rendered) {
            slot.fade = std::min(slot.fade + step, 1.0f);
        }
    }
}

bool needs_full_render(const Slot& s) {
    if (!s.rendered) return true;
    const float tolerance = g_set.move_tolerance;
    return dist_sq(s.tracked, s.pos) > tolerance * tolerance || s.want_radius > s.radius || s.want_radius < s.radius * 0.5f;
}

void apply_store_events() {
    casters_take_events(g_events);
    if (g_events.empty()) return;
    for (int s = 0; s < g_res_slots; s++) {
        Slot& slot = g_slots[s];
        if (!slot.used || !slot.rendered) continue;
        const float sphere[4] = {slot.pos[0], slot.pos[1], slot.pos[2], slot.radius};
        for (const CasterEvent& e : g_events) {
            if (slot.dirty == kAllFaces) break;
            if (caster_box_outside_sphere(sphere, e.min, e.max)) continue;
            for (int f = 0; f < 6; f++) {
                if (slot.dirty & (1 << f)) continue;
                if (!e.forced && slot.face_serial[f] >= e.serial) continue;
                if (!caster_box_outside(slot.planes[f], e.min, e.max)) slot.dirty |= static_cast<uint8_t>(1 << f);
            }
        }
    }
}

UINT replay_into(ID3D11DeviceContext* ctx, ID3D11DepthStencilView* dsv, const float rows[16], const float sphere[4], bool store, bool frame) {
    ctx->OMSetRenderTargets(0, nullptr, dsv);
    CasterReplay r;
    r.rows = rows;
    r.sphere = sphere;
    r.store = store;
    r.frame = frame;
    r.two_sided = g_set.two_sided;
    r.raster = g_raster;
    // A body around its own light would shut everything out. sphere starts with the cube's position.
    r.skip_containing = frame ? sphere : nullptr;
    const UINT drawn = casters_replay(ctx, r);
    ctx->OMSetRenderTargets(0, nullptr, nullptr);
    return drawn;
}

UINT render_static_face(ID3D11DeviceContext* ctx, int s, int f, const float camera[3]) {
    Slot& slot = g_slots[s];
    const UINT sub = static_cast<UINT>(s) * 6 + f;
    float rel[3], rows[16];
    relative(slot.pos, camera, rel);
    face_rows(kFaces[f], rel, slot.depth_a, slot.depth_b, rows);
    const float sphere[4] = {slot.pos[0], slot.pos[1], slot.pos[2], slot.radius};
    ctx->ClearDepthStencilView(g_static_dsv[sub], D3D11_CLEAR_DEPTH, 1.0f, 0);
    const UINT drawn = replay_into(ctx, g_static_dsv[sub], rows, sphere, true, false);
    slot.face_serial[f] = casters_serial();
    ctx->CopySubresourceRegion(g_read, sub, 0, 0, 0, g_static, sub, nullptr);
    g_stats.faces_static++;
    g_stats.faces_copied++;
    g_stats.frame_faces_static++;
    g_stats.draws_static += drawn;
    g_stats.frame_draws_static += drawn;
    return drawn;
}

void place_slot(Slot& slot, const float camera[3]) {
    memcpy(slot.pos, slot.tracked, sizeof(slot.pos));
    slot.radius = slot.want_radius * 1.15f;  // radius flicker stays inside this
    const float near_z = std::max(g_set.near_plane, 0.01f);
    const float far_z = std::max(slot.radius, near_z * 4.0f);
    slot.radius = far_z;
    slot.depth_a = far_z / (far_z - near_z);
    slot.depth_b = -near_z * far_z / (far_z - near_z);
    float rel[3], rows[16];
    relative(slot.pos, camera, rel);
    for (int f = 0; f < 6; f++) {
        face_rows(kFaces[f], rel, slot.depth_a, slot.depth_b, rows);
        caster_planes_from_rows(rows, camera, slot.planes[f]);
    }
}

void publish(const float camera[3]) {
    Published p;
    memcpy(p.camera, camera, sizeof(p.camera));
    const float texel = 2.0f / static_cast<float>(g_res_face > 0 ? g_res_face : 1);
    p.global[0] = g_set.strength;
    p.global[1] = g_set.normal_offset * texel;
    p.global[2] = g_set.filter_radius * texel;
    p.global[3] = g_set.edge_fade;
    p.bias = g_set.bias;
    p.match_distance = g_set.match_distance;
    uint32_t active = 0;
    for (int s = 0; s < g_res_slots; s++) {
        const Slot& slot = g_slots[s];
        if (!slot.used || !slot.rendered) continue;
        p.active[s] = true;
        memcpy(p.pos[s], slot.pos, sizeof(p.pos[s]));
        memcpy(p.tracked[s], slot.tracked, sizeof(p.tracked[s]));
        p.strength[s] = slot.fade;
        p.depth_a[s] = slot.depth_a;
        p.depth_b[s] = slot.depth_b;
        active++;
    }
    g_stats.active = active;
    std::lock_guard<std::mutex> lock(g_publish_lock);
    if (g_this_anchor_ahead) {
        g_pending = p;
        g_have_pending = true;
    } else {
        g_published = p;
        g_published_serial++;
    }
}

// A dynamic buffer's mapping is write-combined, and an ordinary read of 100 KB of it took 0.8 ms on this game's kind of machine.
// This load is made for such memory, and on any other it is an ordinary load.
void copy_from_mapping(void* dst, const void* src, size_t bytes) {
    static const bool have_streaming_load = IsProcessorFeaturePresent(PF_SSE4_1_INSTRUCTIONS_AVAILABLE) != 0;
    if (!have_streaming_load || (reinterpret_cast<uintptr_t>(src) & 15) != 0 || (bytes & 15) != 0) {
        memcpy(dst, src, bytes);
        return;
    }
    _mm_sfence();  // what the game has just written may still be in the write-combining buffers
    auto* from = reinterpret_cast<__m128i*>(const_cast<void*>(src));
    auto* to = static_cast<__m128i*>(dst);
    for (size_t i = 0; i < bytes / 16; i++) _mm_storeu_si128(to + i, _mm_stream_load_si128(from + i));
}

// Call with g_list_lock held.
ListUpload* list_upload_for(const void* owner) {
    ListUpload* oldest = nullptr;
    for (auto& u : g_list_uploads) {
        if (u && u->owner == owner && !u->finished) return u.get();
    }
    for (auto& u : g_list_uploads) {
        if (!u) u = std::make_unique<ListUpload>();
        if (!u->owner) {
            oldest = u.get();
            break;
        }
        if (u->finished && (!oldest || static_cast<int32_t>(u->age - oldest->age) < 0)) oldest = u.get();
    }
    // A finished list nobody executed, most likely.
    if (!oldest) return nullptr;
    if (oldest->owner) g_stats.uploads_dropped++;
    oldest->owner = owner;
    oldest->finished = false;
    oldest->have_cells = oldest->have_ids = oldest->have_lights = false;
    oldest->age = ++g_list_age;
    return oldest;
}

// Call with g_upload_lock held. The lights take effect now, in the frame this is.
void lights_took_effect() {
    g_lights_serial++;
    g_lights_present = g_present_serial.load(std::memory_order_relaxed);
    g_stats.uploads_executed++;
    // Relative to this frame's camera, which the anchor finds if it has not yet.
    g_lights_camera_known = g_anchor_known && g_anchor_present == g_lights_present;
    if (g_lights_camera_known) memcpy(g_lights_camera, g_anchor_camera, sizeof(g_lights_camera));
}

}  // namespace

void pointshadow_init(ID3D11Device* device) {
    g_device = device;
    pointshadow_read_settings();
    // At startup, not at the first light: a few hundred megabytes at once would be a hitch in the middle of play.
    if (g_set.enabled) ensure_resources();
}

void pointshadow_read_settings() {
    g_set.enabled = cfg_bool("pointshadow.enabled", true);
    g_set.strength = cfg_float("pointshadow.strength", 1.0f);
    g_set.max_lights = std::clamp(cfg_int("pointshadow.max_lights", 8), 1, static_cast<int>(kMaxSlots));
    g_set.face_size = std::clamp(cfg_int("pointshadow.face_size", 1024), 32, 4096);
    g_set.faces_per_frame = std::max(cfg_int("pointshadow.faces_per_frame", 6), 6);
    g_set.draws_per_frame = std::max(cfg_int("pointshadow.draws_per_frame", 1500), 1);
    g_set.dynamic_draws = std::max(cfg_int("pointshadow.dynamic_draws", 400), 0);
    g_set.normal_offset = cfg_float("pointshadow.normal_offset", 2.0f);
    g_set.filter_radius = cfg_float("pointshadow.filter_radius", 1.5f);
    g_set.bias = cfg_float("pointshadow.bias", 0.01f);
    g_set.slope_bias = cfg_float("pointshadow.slope_bias", 1.5f);
    g_set.near_plane = cfg_float("pointshadow.near", 0.5f);
    g_set.two_sided = cfg_bool("pointshadow.two_sided", true);
    g_set.edge_fade = std::clamp(cfg_float("pointshadow.edge_fade", 0.2f), 0.01f, 1.0f);
    g_set.match_distance = std::max(cfg_float("pointshadow.match_distance", 0.75f), 0.01f);
    g_set.move_tolerance = std::max(cfg_float("pointshadow.move_tolerance", 0.25f), 0.0f);
    g_set.max_distance = cfg_float("pointshadow.max_distance", 40.0f);
    g_set.closeness_range = std::max(cfg_float("pointshadow.closeness_range", 12.0f), 0.1f);
    g_set.hysteresis = std::max(cfg_float("pointshadow.hysteresis", 1.5f), 1.0f);
    g_set.min_hold = cfg_int("pointshadow.min_hold", 60);
    g_set.linger = cfg_int("pointshadow.linger", 45);
    g_set.fade_frames = cfg_int("pointshadow.fade_frames", 20);
    g_set.player_distance = cfg_float("pointshadow.player_distance", 8.0f);
    g_set.player_radius = cfg_float("pointshadow.player_radius", 4.5f);
    g_set.unbounded_wait = cfg_int("pointshadow.unbounded_wait", 64);
    g_set.log_every = cfg_int("pointshadow.log_every", 0);
    g_failed = false;  // a settings change may fit in memory where the last one did not
}

bool pointshadow_enabled() {
    return g_set.enabled;
}

void pointshadow_copy_mapping(void* out, const void* mapped, size_t bytes) {
    copy_from_mapping(out, mapped, bytes);
}

int pointshadow_upload_kind(ID3D11Resource* res) {
    D3D11_RESOURCE_DIMENSION dimension = D3D11_RESOURCE_DIMENSION_UNKNOWN;
    res->GetType(&dimension);
    if (dimension != D3D11_RESOURCE_DIMENSION_BUFFER) return kUploadNone;
    D3D11_BUFFER_DESC d;
    static_cast<ID3D11Buffer*>(res)->GetDesc(&d);
    if (!(d.BindFlags & D3D11_BIND_SHADER_RESOURCE)) return kUploadNone;
    if (d.StructureByteStride == 48 && d.ByteWidth == kParamBytes) return kUploadParams;
    if (d.StructureByteStride == 4 && d.ByteWidth == kCellBytes) return kUploadCells;
    if (d.StructureByteStride == 4 && d.ByteWidth == kIdBytes) return kUploadIds;
    return kUploadNone;
}

void pointshadow_on_upload(ID3D11DeviceContext* ctx, int kind, const void* data) {
    if (!ctx || !data || !g_set.enabled) return;
    if (ctx->GetType() == D3D11_DEVICE_CONTEXT_IMMEDIATE) {
        // Already in execution order.
        std::lock_guard<std::mutex> lock(g_upload_lock);
        if (kind == kUploadCells) {
            copy_from_mapping(g_cells, data, sizeof(g_cells));
            g_cells_seen = true;
        } else if (kind == kUploadIds) {
            copy_from_mapping(g_ids, data, sizeof(g_ids));
            g_ids_seen = true;
        } else if (kind == kUploadParams) {
            copy_from_mapping(g_lights, data, kParamBytes);
            lights_took_effect();
        }
        return;
    }
    std::lock_guard<std::mutex> lock(g_list_lock);
    ListUpload* u = list_upload_for(ctx);
    if (!u) {
        g_stats.uploads_dropped++;
        return;
    }
    if (kind == kUploadCells) {
        copy_from_mapping(u->cells, data, sizeof(u->cells));
        u->have_cells = true;
    } else if (kind == kUploadIds) {
        copy_from_mapping(u->ids, data, sizeof(u->ids));
        u->have_ids = true;
    } else if (kind == kUploadParams) {
        copy_from_mapping(u->lights, data, kParamBytes);
        u->have_lights = true;
    }
}

void pointshadow_on_finish(ID3D11DeviceContext* ctx, ID3D11CommandList* list) {
    std::lock_guard<std::mutex> lock(g_list_lock);
    for (auto& u : g_list_uploads) {
        if (u && u->owner == ctx && !u->finished) {
            u->owner = list;
            u->finished = true;
            return;
        }
    }
}

void pointshadow_on_execute(ID3D11DeviceContext* immediate, ID3D11CommandList* list) {
    if (!g_device || !immediate) return;
    {
        std::lock_guard<std::mutex> lock(g_list_lock);
        for (auto& u : g_list_uploads) {
            if (!u || u->owner != list || !u->finished) continue;
            std::lock_guard<std::mutex> upload_lock(g_upload_lock);
            if (u->have_cells) {
                memcpy(g_cells, u->cells, sizeof(g_cells));
                g_cells_seen = true;
            }
            if (u->have_ids) {
                memcpy(g_ids, u->ids, sizeof(g_ids));
                g_ids_seen = true;
            }
            if (u->have_lights) {
                memcpy(g_lights, u->lights, sizeof(g_lights));
                lights_took_effect();
            }
            u->owner = nullptr;
            u->finished = false;
            break;
        }
    }

    ShaderConstants c{};
    bool write_constants = false, write_map = false;
    ID3D11Buffer* cb;
    {
        std::lock_guard<std::mutex> lock(g_publish_lock);
        cb = g_cb;
        if (!cb) return;
        const Published& p = g_published;
        if (g_flushed_published != g_published_serial) {
            g_flushed_published = g_published_serial;
            write_constants = true;
            memcpy(c.global, p.global, sizeof(c.global));
            for (UINT s = 0; s < kMaxSlots; s++) {
                if (!p.active[s]) continue;
                c.pos[s][0] = p.pos[s][0] - p.camera[0];
                c.pos[s][1] = p.pos[s][1] - p.camera[1];
                c.pos[s][2] = p.pos[s][2] - p.camera[2];
                c.pos[s][3] = p.strength[s];
                c.depth[s][0] = p.depth_a[s];
                c.depth[s][1] = p.depth_b[s];
                c.depth[s][2] = p.bias;
            }
        }
        // Ids may change every frame, so the map goes by position. It waits for the anchor to say where these lights are.
        std::lock_guard<std::mutex> upload_lock(g_upload_lock);
        if ((g_map_lights_serial != g_lights_serial || g_map_published_serial != g_published_serial) && (g_lights_camera_known || !g_lights_serial)) {
            g_map_lights_serial = g_lights_serial;
            g_map_published_serial = g_published_serial;
            write_map = true;
            const float match_sq = p.match_distance * p.match_distance;
            for (UINT id = 0; id < kLights; id++) {
                g_map_values[id] = 0;
                const float* light = g_lights + id * kLightFloats;
                if (!(light[7] > 0.01f && light[7] < 1000.0f)) continue;
                const float pos[3] = {light[0] + g_lights_camera[0], light[1] + g_lights_camera[1], light[2] + g_lights_camera[2]};
                float best = match_sq;
                for (UINT s = 0; s < kMaxSlots; s++) {
                    if (!p.active[s]) continue;
                    const float sq = dist_sq(pos, p.tracked[s]);
                    if (sq <= best) {
                        best = sq;
                        g_map_values[id] = static_cast<int>(s) + 1;
                    }
                }
            }
        }
    }
    if (write_constants) immediate->UpdateSubresource(cb, 0, nullptr, &c, 0, 0);
    if (write_map) immediate->UpdateSubresource(g_map, 0, nullptr, g_map_values, 0, 0);
}

void pointshadow_on_present() {
    std::lock_guard<std::mutex> lock(g_publish_lock);
    std::lock_guard<std::mutex> upload_lock(g_upload_lock);
    const uint32_t ended = g_present_serial.fetch_add(1, std::memory_order_relaxed);
    if (g_have_pending) {
        // The anchor of the frame now starting has already run.
        g_published = g_pending;
        g_published_serial++;
        g_have_pending = false;
        memcpy(g_anchor_camera, g_pending.camera, sizeof(g_anchor_camera));
        g_anchor_present = ended + 1;
        return;
    }
    if (g_anchor_known && g_anchor_present == ended) return;
    // No anchor this frame, so the next frame's camera may never be found. Until one comes, no lit draw uses a cube.
    g_stats.frames_no_anchor++;
    bool any = false;
    for (bool active : g_published.active) any = any || active;
    if (any) {
        g_published = Published{};
        g_published_serial++;
    }
}

void pointshadow_render(ID3D11DeviceContext* ctx) {
    if (!g_device || !ctx || !g_set.enabled) return;
    float camera[3];
    if (!casters_camera(camera)) return;
    g_frame++;
    g_stats.anchors++;
    g_stats.frame_draws_static = g_stats.frame_draws_dynamic = g_stats.frame_faces_static = g_stats.frame_faces_dynamic = 0;
    if (casters_generation() != g_generation) {
        g_generation = casters_generation();
        reset_slots();
        std::lock_guard<std::mutex> lock(g_upload_lock);
        g_lights_serial++;
    }

    static float s_lights[kLights * kLightFloats];
    float light_camera[3];
    uint32_t used[kLights / 32] = {};
    bool used_valid;
    {
        std::lock_guard<std::mutex> lock(g_upload_lock);
        const uint32_t present = g_present_serial.load(std::memory_order_relaxed);
        memcpy(s_lights, g_lights, sizeof(s_lights));
        used_valid = referenced_lights(used);
        g_this_anchor_ahead = g_anchor_known && g_anchor_present == present;
        if (g_this_anchor_ahead) g_stats.anchors_ahead++;
        if (g_lights_present != present) g_stats.anchors_old_lights++;
        if (!g_lights_camera_known) {
            // Executed this frame, this camera. If not, it was a frame with no anchor, and the last one is the best there is.
            memcpy(g_lights_camera, g_lights_present == present || !g_anchor_known ? camera : g_anchor_camera, sizeof(g_lights_camera));
            g_lights_camera_known = true;
        }
        memcpy(light_camera, g_lights_camera, sizeof(light_camera));
        // An anchor ahead of its frame only counts from that frame's start: see pointshadow_on_present.
        if (!g_this_anchor_ahead) {
            memcpy(g_anchor_camera, camera, sizeof(g_anchor_camera));
            g_anchor_present = present;
            g_anchor_known = true;
        }
    }

    gather_candidates(s_lights, used, used_valid, light_camera, camera);
    select_lights();

    bool any = false;
    for (int s = 0; s < g_set.max_lights; s++) any = any || g_slots[s].used;
    if (!any && !g_read_srv) {
        casters_take_events(g_events);  // nothing is cached, so nothing can go stale
        return;
    }
    // A light can still hold its slot while the arrays are rebuilt: rebuilding empties the slots.
    if (!ensure_resources()) {
        casters_take_events(g_events);
        return;
    }
    apply_store_events();

    // Strongest first, so the budget goes where it shows.
    int order[kMaxSlots];
    int count = 0;
    for (int s = 0; s < g_res_slots; s++) {
        if (g_slots[s].used) order[count++] = s;
    }
    std::sort(order, order + count, [](int a, int b) { return g_slots[a].score > g_slots[b].score; });
    // World faces go to whoever has waited longest, so a light that keeps moving cannot hold every other one off. Strongest
    // still breaks ties, and a light's first render counts as the longest wait.
    int waiting[kMaxSlots];
    std::copy(order, order + count, waiting);
    std::stable_sort(waiting, waiting + count, [](int a, int b) { return g_slots[a].last_render < g_slots[b].last_render; });

    bool want_full = false, want_faces = false;
    for (int i = 0; i < count; i++) {
        const Slot& slot = g_slots[order[i]];
        want_full = want_full || (!slot.leaving && needs_full_render(slot));
        want_faces = want_faces || slot.dirty != 0;
    }
    // Until most bounds are in, a new cube would draw everything six times over, so wait; but not forever.
    bool may_start = true;
    if (want_full) {
        may_start = casters_stats().unbounded <= static_cast<uint32_t>(g_set.unbounded_wait) || ++g_waited > 300;
        if (may_start) g_waited = 0;
    }

    uint8_t dynamic_now[kMaxSlots] = {};
    bool want_dynamic = false;
    for (int i = 0; i < count; i++) {
        const Slot& slot = g_slots[order[i]];
        if (!slot.rendered) continue;
        const float sphere[4] = {slot.pos[0], slot.pos[1], slot.pos[2], slot.radius};
        dynamic_now[order[i]] = g_set.dynamic_draws > 0 ? static_cast<uint8_t>(casters_frame_overlap(slot.planes, 6, sphere, sphere)) : 0;
        want_dynamic = want_dynamic || dynamic_now[order[i]] != 0 || slot.dynamic_prev != 0;
    }

    if ((want_full && may_start) || want_faces || want_dynamic) {
        StateBackup backup(ctx);
        unbind_all(ctx);
        ctx->OMSetDepthStencilState(g_depth, 0);
        const float factor[4] = {};
        ctx->OMSetBlendState(g_blend, factor, 0xffffffff);
        ctx->GSSetShader(nullptr, nullptr, 0);
        const float size = static_cast<float>(g_res_face);
        const D3D11_VIEWPORT viewport{0.0f, 0.0f, size, size, 0.0f, 1.0f};
        ctx->RSSetViewports(1, &viewport);

        int faces_left = g_set.faces_per_frame;
        int draws_left = g_set.draws_per_frame;
        uint8_t fresh[kMaxSlots] = {};
        for (int i = 0; i < count; i++) {
            const int s = waiting[i];
            Slot& slot = g_slots[s];
            if (slot.leaving) continue;
            if (needs_full_render(slot)) {
                // All six in one frame: every face of a cube must come from one position, and the read array still holds
                // the old cube until this one is complete.
                if (!may_start || faces_left < 6 || draws_left <= 0) continue;
                place_slot(slot, camera);
                for (int f = 0; f < 6; f++) draws_left -= static_cast<int>(render_static_face(ctx, s, f, camera));
                faces_left -= 6;
                slot.rendered = true;
                slot.last_render = g_frame;
                slot.dirty = 0;
                slot.dynamic_prev = 0;
                fresh[s] = kAllFaces;
                g_stats.full_renders++;
                // It starts from here, so the characters must be recomputed for its new place.
                const float sphere[4] = {slot.pos[0], slot.pos[1], slot.pos[2], slot.radius};
                dynamic_now[s] = g_set.dynamic_draws > 0 ? static_cast<uint8_t>(casters_frame_overlap(slot.planes, 6, sphere, sphere)) : 0;
            } else if (slot.dirty) {
                for (int f = 0; f < 6 && faces_left > 0 && draws_left > 0; f++) {
                    if (!(slot.dirty & (1 << f))) continue;
                    draws_left -= static_cast<int>(render_static_face(ctx, s, f, camera));
                    faces_left--;
                    slot.dirty &= static_cast<uint8_t>(~(1 << f));
                    fresh[s] |= static_cast<uint8_t>(1 << f);
                    slot.last_render = g_frame;
                }
            }
        }

        int dynamic_left = g_set.dynamic_draws;
        for (int i = 0; i < count; i++) {
            const int s = order[i];
            Slot& slot = g_slots[s];
            if (!slot.rendered) continue;
            const uint8_t want = dynamic_left > 0 ? dynamic_now[s] : static_cast<uint8_t>(0);
            // Last frame's characters come out, and this frame's start from the clean world.
            const uint8_t refresh = static_cast<uint8_t>((slot.dynamic_prev | want) & ~fresh[s]);
            for (int f = 0; f < 6; f++) {
                if (!(refresh & (1 << f))) continue;
                const UINT sub = static_cast<UINT>(s) * 6 + f;
                ctx->CopySubresourceRegion(g_read, sub, 0, 0, 0, g_static, sub, nullptr);
                g_stats.faces_copied++;
            }
            if (want) {
                float rel[3], rows[16];
                relative(slot.pos, camera, rel);
                const float sphere[4] = {slot.pos[0], slot.pos[1], slot.pos[2], slot.radius};
                for (int f = 0; f < 6; f++) {
                    if (!(want & (1 << f))) continue;
                    face_rows(kFaces[f], rel, slot.depth_a, slot.depth_b, rows);
                    const UINT drawn = replay_into(ctx, g_read_dsv[static_cast<UINT>(s) * 6 + f], rows, sphere, false, true);
                    dynamic_left -= static_cast<int>(drawn);
                    g_stats.faces_dynamic++;
                    g_stats.frame_faces_dynamic++;
                    g_stats.draws_dynamic += drawn;
                    g_stats.frame_draws_dynamic += drawn;
                }
            }
            slot.dynamic_prev = want;
        }
    }

    publish(camera);
    if (g_set.log_every > 0 && g_frame % static_cast<uint32_t>(g_set.log_every) == 0) {
        const CasterStats store = casters_stats();
        logf("pointshadow: order: %llu anchors, %llu uploads executed, %llu anchors on an older upload, %llu ahead, %llu frames with no anchor, %llu of %llu binds before the anchor, %llu uploads dropped",
             static_cast<unsigned long long>(g_stats.anchors), static_cast<unsigned long long>(g_stats.uploads_executed), static_cast<unsigned long long>(g_stats.anchors_old_lights),
             static_cast<unsigned long long>(g_stats.anchors_ahead), static_cast<unsigned long long>(g_stats.frames_no_anchor), static_cast<unsigned long long>(g_binds_before_anchor.load()),
             static_cast<unsigned long long>(g_binds.load()), static_cast<unsigned long long>(g_stats.uploads_dropped));
        logf("pointshadow: lights %u, faces %u+%u, draws %u+%u | store: %u draws (%u unbounded), %u meshes, %u this frame, %u replayed, votes %u/%u, %llu movers",
             g_stats.active, g_stats.frame_faces_static, g_stats.frame_faces_dynamic, g_stats.frame_draws_static, g_stats.frame_draws_dynamic, store.placements,
             store.unbounded, store.meshes, store.frame_draws, store.replayed, store.inliers, store.votes, static_cast<unsigned long long>(store.movers));
    }
}

void pointshadow_bind(ID3D11DeviceContext* ctx) {
    g_binds.fetch_add(1, std::memory_order_relaxed);
    {
        std::lock_guard<std::mutex> lock(g_upload_lock);
        if (!g_anchor_known || g_anchor_present != g_present_serial.load(std::memory_order_relaxed)) g_binds_before_anchor.fetch_add(1, std::memory_order_relaxed);
    }
    ID3D11ShaderResourceView* views[2] = {};
    std::lock_guard<std::mutex> lock(g_publish_lock);
    if (g_set.enabled && g_read_srv && g_cb) {
        ctx->PSSetConstantBuffers(4, 1, &g_cb);
        views[0] = g_read_srv;
        views[1] = g_map_srv;
    }
    // An unbound map reads as 0, which means no shadow for every light.
    ctx->PSSetShaderResources(20, 2, views);
}

PointShadowStats pointshadow_stats() {
    return g_stats;
}
