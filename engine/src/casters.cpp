// Settings in dsrle.ini:
//   casters.enabled = 1              0: nothing persists, sun shadows go back to what the camera sees
//   casters.warp_distance = 200      metres the camera may jump in a frame before everything is dropped
//   casters.max_draws = 6000         stored draws; past this the longest unseen go first
//   casters.max_meshes = 12000       known meshes; past this the longest unseen with nothing stored go first
//   casters.keep_distance = 700      stored draws farther than this from the camera are dropped
//   casters.forget_distance = 12     an object stops being stored when it sits in clear view this close and is not drawn...
//   casters.forget_frames = 45       ...for this many frames: a broken crate, a gate taken away. 0 turns this off
//   casters.move_tolerance = 0.003   metres a static draw may differ between frames and still count as unmoved
//   casters.moves_to_dynamic = 2     moves within casters.move_window frames that make a mesh dynamic for good
//   casters.move_window = 240
//   casters.bounds_draws = 24        meshes whose vertices are read per batch; one batch every two or three frames
//   casters.bounds_bytes = 8388608   staging size for one batch
//   casters.bounds_indices = 131072  indices walked per batch
//   casters.idle_frames = 120        frames with the engine on and nothing ingested, as in a loading screen, before the store
//                                    lets go of everything so the game's unloading can work. 0 turns this off
//
// What counts as static, from the traced frames (prepass draws, by whether the game also draws them into its velocity
// pass, which only moving things enter; Firelink, Parish, Burg, Catacombs):
//   BLENDWEIGHT layout   in velocity 97, 34, 37, 24     not in velocity 0, 4, 0, 0
//   Wind VS              in velocity 33, 0, 16, 0       not in velocity 0
//   other layouts        in velocity 11, 8, 4, 4        not in velocity 128, 128, 322, 78
// So a bone-weight layout or a Wind VS means dynamic, and is never wrong by more than a few props that happen to hold
// still. The rest is static except for a handful: 4 to 11 rigid draws per frame that follow a character, such as a
// weapon. Their palette changes every frame, so they get caught by moving, within two sightings, and stay dynamic.
#include "casters.h"
#include "common.h"
#include "config.h"
#include "shaderdb.h"
#include <algorithm>
#include <atomic>
#include <cfloat>
#include <climits>
#include <cmath>
#include <cstring>
#include <deque>
#include <memory>
#include <mutex>
#include <shared_mutex>
#include <unordered_map>

namespace {

constexpr UINT kPaletteReg = 28;  // first register of the object palette in VS b0
constexpr UINT kPaletteSlots = 38;
constexpr UINT kRegs = kCasterVsBytes / 16;
constexpr UINT kPsB1Bytes = 48;
constexpr UINT kMaxEvents = 8192;
static_assert(kPaletteReg + kPaletteSlots * 3 <= kRegs);

struct V3 { float x, y, z; };
V3 operator-(V3 a, V3 b) { return {a.x - b.x, a.y - b.y, a.z - b.z}; }
V3 operator+(V3 a, V3 b) { return {a.x + b.x, a.y + b.y, a.z + b.z}; }
float len_sq(V3 a) { return a.x * a.x + a.y * a.y + a.z * a.z; }

// Palette entry `slot` of a register image: three registers, each a row of a 3x4 matrix, translation in .w.
const float* slot_rows(const float* regs, UINT slot) { return regs + (kPaletteReg + slot * 3) * 4; }
float* slot_rows(float* regs, UINT slot) { return regs + (kPaletteReg + slot * 3) * 4; }
V3 slot_translation(const float* regs, UINT slot) {
    const float* r = slot_rows(regs, slot);
    return {r[3], r[7], r[11]};
}

// What the store knows about a vertex layout.
struct LayoutInfo {
    int position = -1;  // byte offsets into the vertex, slot 0; -1 when absent or in a format this code cannot read
    int indices = -1;
    int weights = -1;
    bool skinned = false;
    bool per_instance = false;
};
std::unordered_map<const void*, LayoutInfo> g_layouts;
std::shared_mutex g_layout_lock;

struct MeshKey {
    const void* vb;
    const void* ib;
    const void* vs;
    UINT start, count;
    INT base;
    bool operator==(const MeshKey& o) const {
        return vb == o.vb && ib == o.ib && vs == o.vs && start == o.start && count == o.count && base == o.base;
    }
};
struct MeshKeyHash {
    size_t operator()(const MeshKey& k) const {
        uint64_t h = reinterpret_cast<uint64_t>(k.vb) * 0x9E3779B97F4A7C15ull;
        h ^= (reinterpret_cast<uint64_t>(k.ib) >> 4) * 0xC2B2AE3D27D4EB4Full;
        h ^= (reinterpret_cast<uint64_t>(k.vs) >> 4) * 0x165667B19E3779F9ull;
        h ^= (static_cast<uint64_t>(k.start) << 32 | k.count) * 0x27D4EB2F165667C5ull;
        h ^= static_cast<uint64_t>(static_cast<uint32_t>(k.base)) * 0x85EBCA77C2B2AE63ull;
        return static_cast<size_t>(h ^ (h >> 29));
    }
};

// The vertices bound to one palette slot, in the mesh's own space.
struct SlotBox {
    uint32_t slot;
    float min[3], max[3];
};

struct Placement;

enum class MeshClass : uint8_t {
    Static,   // kept
    Skinned,  // a layout with bone weights: for one frame only
    Wind,     // sways with c5 and c6: for one frame only
    SunOnly,  // instanced, CPU-written or view-dependent alpha: replayed in the frame they appear, for the sun alone
};

// One piece of geometry, wherever it is placed. The same mesh can stand in many places.
struct Mesh {
    MeshKey key{};
    uint64_t id = 0;
    ID3D11Buffer* vb = nullptr;  // references: the addresses in the key cannot be reused while this exists
    ID3D11Buffer* ib = nullptr;
    ID3D11VertexShader* vs = nullptr;
    MeshClass cls = MeshClass::Static;
    bool mover = false;  // a static layout seen moving: a weapon in a hand, a lift. Dynamic from then on
    bool boxes_ready = false;
    uint32_t rebase_slots = kPaletteSlots;  // palette entries worth rebasing; the rest of the image is stale registers
    uint64_t used = 1;  // slot 0 is always written, so until the vertices are read it stands for the object
    std::vector<SlotBox> boxes;
    uint32_t last_seen = 0, last_move = 0, last_new = 0;
    uint16_t moves = 0, new_streak = 0;
    std::vector<Placement*> placements;
    bool frame_only() const { return cls != MeshClass::Static || mover; }
};

struct Placement {
    Mesh* mesh = nullptr;
    CasterDraw draw;                // owns its references
    std::unique_ptr<float[]> regs;  // the VS b0 image with palette translations in store space
    float min[3] = {}, max[3] = {};
    float radius = 0.0f;
    bool bounded = false;
    bool object = false;  // its own transform, not the map's shared one
    uint32_t serial = 0, last_seen = 0;
    uint16_t misses = 0;
    uint32_t index = 0;  // in g_all
    bool doomed = false;
};

// The store as replays walk it, from the back: largest first, so a budget keeps what matters and a size floor stops the
// walk. The bounds sit here so that culling thousands of draws touches nothing else. A draw with no bounds yet has a radius
// of FLT_MAX, which also puts new draws at the cheap end to insert.
struct SortedPlacement {
    float radius;
    float min[3], max[3];
    Placement* placement;
};

struct FrameDraw {
    CasterDraw draw;
    const float* regs = nullptr;  // camera-relative, this frame; lives in g_frame_constants
    float min[3] = {}, max[3] = {};
    bool bounded = false;
    bool sun_only = false;
    bool character = false;  // bones, or a rigid thing that follows one
};

struct Settings {
    bool enabled = true;
    float warp = 200.0f, keep = 700.0f, forget_distance = 12.0f, move_tolerance = 0.003f;
    int forget_frames = 45, moves_to_dynamic = 2, move_window = 240, max_draws = 6000, max_meshes = 12000, idle_frames = 120;
    UINT bounds_draws = 24, bounds_bytes = 8u << 20, bounds_indices = 131072;
} g_set;

ID3D11Device* g_device;
std::once_flag g_create_once;
bool g_ready;
ID3D11Buffer* g_b0;     // replay copy of the vertex constants
ID3D11Buffer* g_b2;     // ClipPlaneBuffer, zero: no clip plane
ID3D11Buffer* g_ps_b1;  // alpha test constants

std::mutex g_store_lock;
std::unordered_map<MeshKey, std::unique_ptr<Mesh>, MeshKeyHash> g_meshes;
std::vector<Placement*> g_all;
std::vector<SortedPlacement> g_sorted;
std::vector<FrameDraw> g_frame;
std::vector<uint8_t> g_frame_constants;
std::vector<CasterEvent> g_events;
std::vector<Mesh*> g_scratch_meshes;
std::vector<V3> g_votes, g_rel;
std::vector<uint8_t> g_claimed;
CasterPlanes g_view_planes;  // this frame's camera view, in store space
bool g_have_view_planes;
uint32_t g_no_overlap;       // consecutive frames with static draws, a store, and nothing in common
std::atomic<int> g_idle_presents{0};
uint32_t g_frame_no;
uint32_t g_serial;
uint32_t g_generation;
uint64_t g_next_id = 1;
CasterStats g_stats;

std::mutex g_camera_lock;
V3 g_camera;
bool g_have_camera;
bool g_camera_certain;  // false when the votes disagree: nothing can be said about this frame's draws against the store
V3 g_taa_prev;
bool g_have_taa;
V3 g_origin;
bool g_have_origin;

// Bounds: the recording side asks, the render thread copies and reads.
struct BoundsRequest {
    MeshKey key{};
    uint64_t id = 0;
    ID3D11Buffer* vb = nullptr;
    ID3D11Buffer* ib = nullptr;
    UINT stride = 0, vb_offset = 0, ib_offset = 0, start = 0, count = 0;
    INT base = 0;
    DXGI_FORMAT ib_format = DXGI_FORMAT_UNKNOWN;
    int position = 0, indices = 0, weights = -1;
    UINT vb_bytes = 0, ib_width = 0, ib_bytes = 0, vb_at = 0, ib_at = 0;
};
struct BoundsResult {
    MeshKey key{};
    uint64_t id = 0;
    bool ok = false;
    uint64_t used = 0;
    std::vector<SlotBox> boxes;
};
std::mutex g_queue_lock;
std::deque<BoundsRequest> g_requests_first;  // dynamic meshes: without bounds they would land in every view
std::deque<BoundsRequest> g_requests;
std::vector<BoundsResult> g_results;

// Touched by the render thread only.
ID3D11Buffer* g_staging;
UINT g_staging_bytes;
std::vector<BoundsRequest> g_batch;

template <class T>
void drop(T*& p) {
    if (p) p->Release();
    p = nullptr;
}

void drop_request(BoundsRequest& r) {
    drop(r.vb);
    drop(r.ib);
}

void drop_game_constants(CasterDraw& d) {
    drop(d.vs_b0);
    drop(d.vs_b2);
    drop(d.ps_b1);
}

void drop_draw(CasterDraw& d) {
    drop(d.layout);
    drop(d.vb[0]);
    drop(d.vb[1]);
    drop(d.ib);
    drop(d.vs);
    drop(d.ps);
    drop(d.ps_t0);
    drop(d.ps_s0);
    drop_game_constants(d);
}

void create_resources() {
    D3D11_BUFFER_DESC b{};
    b.ByteWidth = kCasterVsBytes;
    b.Usage = D3D11_USAGE_DYNAMIC;
    b.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    b.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    bool ok = SUCCEEDED(g_device->CreateBuffer(&b, nullptr, &g_b0));
    b.ByteWidth = kPsB1Bytes;
    ok = ok && SUCCEEDED(g_device->CreateBuffer(&b, nullptr, &g_ps_b1));
    const uint8_t zero[kPsB1Bytes] = {};
    const D3D11_SUBRESOURCE_DATA init{zero, 0, 0};
    b.Usage = D3D11_USAGE_IMMUTABLE;
    b.CPUAccessFlags = 0;
    ok = ok && SUCCEEDED(g_device->CreateBuffer(&b, &init, &g_b2));
    g_ready = ok;
    logf("casters: replay buffers %s", ok ? "ready" : "FAILED");
}

void note_event(const float min[3], const float max[3], uint32_t serial, bool forced) {
    if (g_events.size() >= kMaxEvents) return;
    CasterEvent e;
    memcpy(e.min, min, sizeof(e.min));
    memcpy(e.max, max, sizeof(e.max));
    e.serial = serial;
    e.forced = forced;
    g_events.push_back(e);
}

// The union of each slot's box, carried through that slot's matrix. Skinning blends positions from those same matrices,
// so a blended vertex lies inside the union of what each bone alone would do.
bool transformed_bounds(const Mesh& m, const float* regs, float out_min[3], float out_max[3]) {
    if (!m.boxes_ready || m.boxes.empty()) return false;
    float lo[3] = {FLT_MAX, FLT_MAX, FLT_MAX}, hi[3] = {-FLT_MAX, -FLT_MAX, -FLT_MAX};
    for (const SlotBox& box : m.boxes) {
        const float* r = slot_rows(regs, box.slot);
        const float c[3] = {(box.min[0] + box.max[0]) * 0.5f, (box.min[1] + box.max[1]) * 0.5f, (box.min[2] + box.max[2]) * 0.5f};
        const float e[3] = {(box.max[0] - box.min[0]) * 0.5f, (box.max[1] - box.min[1]) * 0.5f, (box.max[2] - box.min[2]) * 0.5f};
        for (int i = 0; i < 3; i++) {
            const float* row = r + i * 4;
            const float centre = row[0] * c[0] + row[1] * c[1] + row[2] * c[2] + row[3];
            const float extent = std::fabs(row[0]) * e[0] + std::fabs(row[1]) * e[1] + std::fabs(row[2]) * e[2];
            lo[i] = std::min(lo[i], centre - extent);
            hi[i] = std::max(hi[i], centre + extent);
        }
    }
    memcpy(out_min, lo, sizeof(lo));
    memcpy(out_max, hi, sizeof(hi));
    return true;
}

float sort_key(const Placement& p) { return p.bounded ? p.radius : FLT_MAX; }

void sorted_remove(const Placement& p) {
    const float key = sort_key(p);
    auto it = std::lower_bound(g_sorted.begin(), g_sorted.end(), key, [](const SortedPlacement& e, float k) { return e.radius < k; });
    while (it != g_sorted.end() && it->radius == key) {
        if (it->placement == &p) {
            g_sorted.erase(it);
            return;
        }
        ++it;
    }
}

void sorted_insert(Placement& p) {
    SortedPlacement e;
    e.radius = sort_key(p);
    memcpy(e.min, p.min, sizeof(e.min));
    memcpy(e.max, p.max, sizeof(e.max));
    e.placement = &p;
    auto it = std::upper_bound(g_sorted.begin(), g_sorted.end(), e.radius, [](float k, const SortedPlacement& a) { return k < a.radius; });
    g_sorted.insert(it, e);
}

// Recompute a placement's bound from its matrices. It must not be in g_sorted.
void compute_bound(Placement& p) {
    p.bounded = transformed_bounds(*p.mesh, p.regs.get(), p.min, p.max);
    if (p.bounded) {
        const V3 half{(p.max[0] - p.min[0]) * 0.5f, (p.max[1] - p.min[1]) * 0.5f, (p.max[2] - p.min[2]) * 0.5f};
        p.radius = std::sqrt(len_sq(half));
    }
}

void update_bound(Placement& p) {
    sorted_remove(p);
    compute_bound(p);
    sorted_insert(p);
}

// A register image with its palette translations moved from camera-relative to store space.
void copy_rebased(float* dst, const float* src, V3 add) {
    memcpy(dst, src, kCasterVsBytes);
    for (UINT s = 0; s < kPaletteSlots; s++) {
        float* r = slot_rows(dst, s);
        r[3] += add.x;
        r[7] += add.y;
        r[11] += add.z;
    }
}

void remove_placement(Placement* p, bool event) {
    if (event && p->bounded) note_event(p->min, p->max, 0, true);
    sorted_remove(*p);
    drop_draw(p->draw);
    Placement* last = g_all.back();
    g_all[p->index] = last;
    last->index = p->index;
    g_all.pop_back();
    Mesh* m = p->mesh;
    auto it = std::find(m->placements.begin(), m->placements.end(), p);
    if (it != m->placements.end()) {
        *it = m->placements.back();
        m->placements.pop_back();
    }
    delete p;
}

// Many at once. One pass over the sorted list: an erase each would move all of it every time, and crossing the cap alone
// removes hundreds.
void remove_many(const std::vector<Placement*>& victims, bool event) {
    if (victims.empty()) return;
    for (Placement* p : victims) p->doomed = true;
    g_sorted.erase(std::remove_if(g_sorted.begin(), g_sorted.end(), [](const SortedPlacement& e) { return e.placement->doomed; }), g_sorted.end());
    for (Placement* p : victims) {
        if (event && p->bounded) note_event(p->min, p->max, 0, true);
        drop_draw(p->draw);
        Placement* last = g_all.back();
        g_all[p->index] = last;
        last->index = p->index;
        g_all.pop_back();
        Mesh* m = p->mesh;
        auto it = std::find(m->placements.begin(), m->placements.end(), p);
        if (it != m->placements.end()) {
            *it = m->placements.back();
            m->placements.pop_back();
        }
        delete p;
    }
}

void drop_mesh_refs(Mesh& m) {
    drop(m.vb);
    drop(m.ib);
    drop(m.vs);
}

void clear_store_locked() {
    for (Placement* p : g_all) {
        drop_draw(p->draw);
        delete p;
    }
    g_all.clear();
    g_sorted.clear();
    for (auto& kv : g_meshes) drop_mesh_refs(*kv.second);
    g_meshes.clear();
    g_events.clear();
    g_have_origin = false;
    g_generation++;
    std::vector<BoundsRequest> stale;
    {
        std::lock_guard<std::mutex> lock(g_queue_lock);
        stale.insert(stale.end(), g_requests_first.begin(), g_requests_first.end());
        stale.insert(stale.end(), g_requests.begin(), g_requests.end());
        g_requests_first.clear();
        g_requests.clear();
        g_results.clear();
    }
    for (BoundsRequest& r : stale) drop_request(r);
}

void release_frame_list() {
    for (FrameDraw& f : g_frame) drop_draw(f.draw);
    g_frame.clear();
    g_frame_constants.clear();
}

bool name_has(const char* name, const char* what) { return name && strstr(name, what) != nullptr; }

Mesh* mesh_for(const CasterDraw& d) {
    const MeshKey key{d.vb[0], d.ib, d.vs, d.start_index, d.index_count, d.base_vertex};
    auto it = g_meshes.find(key);
    if (it != g_meshes.end()) return it->second.get();

    auto owned = std::make_unique<Mesh>();
    Mesh* m = owned.get();
    m->key = key;
    m->id = g_next_id++;
    m->vb = d.vb[0];
    m->ib = d.ib;
    m->vs = d.vs;
    if (m->vb) m->vb->AddRef();
    if (m->ib) m->ib->AddRef();
    if (m->vs) m->vs->AddRef();

    LayoutInfo layout;
    bool known_layout = false;
    {
        std::shared_lock<std::shared_mutex> lock(g_layout_lock);
        auto found = g_layouts.find(d.layout);
        if (found != g_layouts.end()) {
            layout = found->second;
            known_layout = true;
        }
    }
    D3D11_BUFFER_DESC vb_desc{}, ib_desc{};
    if (d.vb[0]) d.vb[0]->GetDesc(&vb_desc);
    if (d.ib) d.ib->GetDesc(&ib_desc);
    const bool cpu_written = vb_desc.Usage == D3D11_USAGE_DYNAMIC || ib_desc.Usage == D3D11_USAGE_DYNAMIC;

    const ShaderInfo* vs_info = d.vs ? shaderdb_find(d.vs) : nullptr;
    const ShaderInfo* ps_info = d.ps ? shaderdb_find(d.ps) : nullptr;
    const char* vs_name = vs_info ? vs_info->name.c_str() : nullptr;
    const char* ps_name = ps_info ? ps_info->name.c_str() : nullptr;
    const bool wind = name_has(vs_name, "_Wind_") || name_has(vs_name, "_Ivy_");
    // NtoA's alpha reads the eye vector and PS b0, which only mean anything in the frame they were bound.
    const bool view_alpha = name_has(vs_name, "NtoA") || name_has(ps_name, "NtoA");

    if (d.instanced || cpu_written || view_alpha || !known_layout || layout.per_instance) {
        m->cls = MeshClass::SunOnly;
    } else if (layout.skinned) {
        m->cls = MeshClass::Skinned;
    } else if (wind) {
        m->cls = MeshClass::Wind;
    }

    // Instanced draws take their matrices from a stream, so a box per palette slot would mean nothing for them.
    const bool readable = known_layout && !d.instanced && !layout.per_instance && !cpu_written && layout.position >= 0 && layout.indices >= 0 && d.vb[0] && d.ib &&
                          d.stride[0] > 0 && (d.index_format == DXGI_FORMAT_R16_UINT || d.index_format == DXGI_FORMAT_R32_UINT);
    if (readable) {
        BoundsRequest r;
        r.key = key;
        r.id = m->id;
        r.vb = d.vb[0];
        r.ib = d.ib;
        r.vb->AddRef();
        r.ib->AddRef();
        r.stride = d.stride[0];
        r.vb_offset = d.offset[0];
        r.ib_offset = d.index_offset;
        r.start = d.start_index;
        r.count = d.index_count;
        r.base = d.base_vertex;
        r.ib_format = d.index_format;
        r.position = layout.position;
        r.indices = layout.indices;
        r.weights = layout.weights;
        r.vb_bytes = vb_desc.ByteWidth;
        r.ib_width = ib_desc.ByteWidth;
        std::lock_guard<std::mutex> lock(g_queue_lock);
        (m->frame_only() ? g_requests_first : g_requests).push_back(r);
    }
    g_meshes.emplace(key, std::move(owned));
    return m;
}

// The largest group of positions that agree to within 2 cm, as their mean. Samples up to 16 seeds, which is enough: this
// looks for the majority, and static map pieces outnumber everything else in every traced frame.
bool largest_group(const std::vector<V3>& items, V3& out, uint32_t& members) {
    members = 0;
    if (items.empty()) return false;
    const size_t step = std::max<size_t>(1, items.size() / 16);
    constexpr float kAgree = 0.02f * 0.02f;
    V3 best{};
    for (size_t i = 0; i < items.size(); i += step) {
        uint32_t n = 0;
        for (const V3& v : items) {
            if (len_sq(v - items[i]) <= kAgree) n++;
        }
        if (n > members) {
            members = n;
            best = items[i];
        }
    }
    double sum[3] = {};
    uint32_t n = 0;
    for (const V3& v : items) {
        if (len_sq(v - best) <= kAgree) {
            sum[0] += v.x;
            sum[1] += v.y;
            sum[2] += v.z;
            n++;
        }
    }
    out = {static_cast<float>(sum[0] / n), static_cast<float>(sum[1] / n), static_cast<float>(sum[2] / n)};
    return true;
}

void apply_bounds_results() {
    std::vector<BoundsResult> results;
    {
        std::lock_guard<std::mutex> lock(g_queue_lock);
        results.swap(g_results);
    }
    for (BoundsResult& r : results) {
        auto it = g_meshes.find(r.key);
        if (it == g_meshes.end() || it->second->id != r.id) continue;  // gone since the request
        Mesh& m = *it->second;
        if (!r.ok) {
            g_stats.bounds_failed++;
            continue;
        }
        g_stats.bounds_read++;
        m.boxes = std::move(r.boxes);
        m.used = r.used;
        m.boxes_ready = true;
        uint32_t highest = 0;
        for (uint32_t s = 0; s < kPaletteSlots; s++) {
            if (m.used & (1ull << s)) highest = s;
        }
        m.rebase_slots = highest + 1;
        for (Placement* p : m.placements) {
            update_bound(*p);
            if (p->bounded) note_event(p->min, p->max, p->serial, false);
        }
    }
}

// Whether a draw's matrices for every slot it uses match what is stored.
bool matches_stored(const Mesh& m, const float* stored, const float* rel, V3 camera, float tolerance) {
    constexpr float kRotationTolerance = 2e-3f;
    const float add[3] = {camera.x, camera.y, camera.z};
    for (uint32_t s = 0; s < kPaletteSlots; s++) {
        if (!(m.used & (1ull << s))) continue;
        const float* a = slot_rows(stored, s);
        const float* b = slot_rows(rel, s);
        for (int row = 0; row < 3; row++) {
            if (std::fabs(b[row * 4 + 3] + add[row] - a[row * 4 + 3]) > tolerance) return false;
            for (int c = 0; c < 3; c++) {
                if (std::fabs(b[row * 4 + c] - a[row * 4 + c]) > kRotationTolerance) return false;
            }
        }
    }
    return true;
}

void add_frame_draw(CasterDraw& d, const float* rel, const Mesh* m, V3 camera) {
    FrameDraw f;
    f.draw = d;
    d = CasterDraw{};
    f.regs = rel;
    f.sun_only = !m || m->cls == MeshClass::SunOnly;
    f.character = m && (m->cls == MeshClass::Skinned || m->mover);
    if (m && transformed_bounds(*m, rel, f.min, f.max)) {
        const float c[3] = {camera.x, camera.y, camera.z};
        for (int i = 0; i < 3; i++) {
            f.min[i] += c[i];
            f.max[i] += c[i];
        }
        f.bounded = true;
    }
    g_frame.push_back(f);
}

void make_mover(Mesh* m) {
    m->mover = true;
    g_stats.movers++;
    const std::vector<Placement*> victims(m->placements);
    remove_many(victims, true);
}

// A stored place that has just stopped being drawn, in view. Anything else says nothing about a draw near it: the frustum
// and the game's draw groups hide things for reasons of their own, and a thing that moves is drawn in every frame it does.
bool just_vacated(const Placement& p) {
    if (p.last_seen + 1 != g_frame_no) return false;
    return !p.bounded || !g_have_view_planes || !caster_box_outside(g_view_planes, p.min, p.max);
}

// First pass over a frame: draws exactly where a stored place is, so what is left over can be judged fairly. Without this,
// a newly visible copy drawn ahead of its neighbour would look like the neighbour having moved.
bool claim_exact(Mesh* m, CasterDraw& d, const float* rel, V3 camera) {
    const V3 abs0 = slot_translation(rel, 0) + camera;
    Placement* best = nullptr;
    float best_sq = FLT_MAX;
    for (Placement* p : m->placements) {
        if (p->last_seen == g_frame_no) continue;
        const float sq = len_sq(slot_translation(p->regs.get(), 0) - abs0);
        if (sq < best_sq) {
            best_sq = sq;
            best = p;
        }
    }
    if (!best || !matches_stored(*m, best->regs.get(), rel, camera, g_set.move_tolerance)) return false;
    best->last_seen = g_frame_no;
    best->misses = 0;
    drop_draw(d);
    return true;
}

void store_static(Mesh* m, CasterDraw& d, const float* rel, V3 camera) {
    const V3 abs0 = slot_translation(rel, 0) + camera;

    // The nearest stored place of this mesh, by its first slot.
    Placement* nearest = nullptr;
    Placement* nearest_unseen = nullptr;
    float nearest_sq = FLT_MAX, unseen_sq = FLT_MAX;
    for (Placement* p : m->placements) {
        const float sq = len_sq(slot_translation(p->regs.get(), 0) - abs0);
        if (sq < nearest_sq) {
            nearest_sq = sq;
            nearest = p;
        }
        if (p->last_seen != g_frame_no && sq < unseen_sq) {
            unseen_sq = sq;
            nearest_unseen = p;
        }
    }
    // The same object, drawn again.
    if (nearest && matches_stored(*m, nearest->regs.get(), rel, camera, g_set.move_tolerance)) {
        nearest->last_seen = g_frame_no;
        nearest->misses = 0;
        drop_draw(d);
        return;
    }

    // Close to where one was and now gone from there: it moved. Twice in a short while and it is not static.
    const bool vacated = nearest_unseen && just_vacated(*nearest_unseen);
    constexpr float kSameObject = 1.0f;
    if (vacated && unseen_sq <= kSameObject * kSameObject) {
        g_stats.moves++;
        m->moves = g_frame_no - m->last_move <= static_cast<uint32_t>(g_set.move_window) ? static_cast<uint16_t>(m->moves + 1) : 1;
        m->last_move = g_frame_no;
        if (m->moves >= g_set.moves_to_dynamic) {
            make_mover(m);
            add_frame_draw(d, rel, m, camera);
            return;
        }
        Placement* p = nearest_unseen;
        if (p->bounded) note_event(p->min, p->max, 0, true);
        copy_rebased(p->regs.get(), rel, camera);
        p->last_seen = g_frame_no;
        p->misses = 0;
        update_bound(*p);
        if (p->bounded) note_event(p->min, p->max, 0, true);
        drop_draw(d);
        return;
    }

    // Somewhere new in consecutive frames, leaving the last place empty, like something flying: not static either.
    // Identical pillars panning into view leave nothing behind, so they stay however many a frame arrive.
    constexpr float kFlyingReach = 8.0f;  // metres in a frame
    if (vacated && unseen_sq <= kFlyingReach * kFlyingReach) {
        if (m->last_new != g_frame_no) {
            m->new_streak = g_frame_no - m->last_new == 1 ? static_cast<uint16_t>(m->new_streak + 1) : 0;
            m->last_new = g_frame_no;
        }
        if (m->new_streak >= 8) {
            make_mover(m);
            add_frame_draw(d, rel, m, camera);
            return;
        }
    }

    auto* p = new Placement;
    p->mesh = m;
    p->draw = d;
    d = CasterDraw{};
    p->regs.reset(new float[kRegs * 4]);
    copy_rebased(p->regs.get(), rel, camera);
    p->serial = ++g_serial;
    p->last_seen = g_frame_no;
    p->object = g_have_origin && len_sq(abs0 - g_origin) > 0.01f * 0.01f;
    p->index = static_cast<uint32_t>(g_all.size());
    g_all.push_back(p);
    m->placements.push_back(p);
    compute_bound(*p);
    sorted_insert(*p);
    if (p->bounded) note_event(p->min, p->max, p->serial, false);
}

// Drop what is too far away, what has stopped being drawn in clear view, and whatever is over the caps.
void housekeeping(const ViewConstants& view, V3 camera) {
    const float cam[3] = {camera.x, camera.y, camera.z};
    CasterPlanes visible;
    const bool can_forget = view.valid && g_set.forget_frames > 0;
    if (can_forget) caster_planes_from_rows(view.vp, cam, visible);
    const float keep_sq = g_set.keep * g_set.keep;
    const float forget_sq = g_set.forget_distance * g_set.forget_distance;
    std::vector<Placement*> far_away;
    for (size_t i = g_all.size(); i-- > 0;) {
        Placement* p = g_all[i];
        if (p->last_seen == g_frame_no || !p->bounded) continue;
        const V3 centre{(p->min[0] + p->max[0]) * 0.5f, (p->min[1] + p->max[1]) * 0.5f, (p->min[2] + p->max[2]) * 0.5f};
        const float sq = len_sq(centre - camera);
        if (sq > keep_sq) {
            g_stats.far_evicted++;
            far_away.push_back(p);
            continue;
        }
        if (!can_forget || !p->object || sq > forget_sq) continue;
        // Every corner inside the view: the game's frustum culling cannot be why it is missing.
        bool inside = true;
        for (int plane = 0; plane < 6 && inside; plane++) {
            const float* n = visible.p[plane];
            const float x = n[0] >= 0 ? p->min[0] : p->max[0];
            const float y = n[1] >= 0 ? p->min[1] : p->max[1];
            const float z = n[2] >= 0 ? p->min[2] : p->max[2];
            inside = n[0] * x + n[1] * y + n[2] * z + n[3] >= 0;
        }
        if (!inside) continue;
        if (++p->misses > g_set.forget_frames) {
            g_stats.forgotten++;
            remove_placement(p, true);
        }
    }
    remove_many(far_away, false);
    if (g_all.size() > static_cast<size_t>(g_set.max_draws)) {
        std::vector<Placement*> order(g_all);
        const size_t victims = g_all.size() - static_cast<size_t>(g_set.max_draws) * 9 / 10;
        std::nth_element(order.begin(), order.begin() + victims, order.end(), [](const Placement* a, const Placement* b) { return a->last_seen < b->last_seen; });
        order.resize(victims);
        order.erase(std::remove_if(order.begin(), order.end(), [](const Placement* p) { return p->last_seen == g_frame_no; }), order.end());
        g_stats.cap_evicted += order.size();
        remove_many(order, true);
    }
    if (g_meshes.size() > static_cast<size_t>(g_set.max_meshes)) {
        std::vector<const Mesh*> order;
        for (auto& kv : g_meshes) {
            if (kv.second->placements.empty() && kv.second->last_seen != g_frame_no) order.push_back(kv.second.get());
        }
        const size_t over = g_meshes.size() - static_cast<size_t>(g_set.max_meshes) * 9 / 10;
        const size_t victims = std::min(over, order.size());
        if (victims > 0) {
            std::nth_element(order.begin(), order.begin() + (victims - 1), order.end(), [](const Mesh* a, const Mesh* b) { return a->last_seen < b->last_seen; });
            for (size_t i = 0; i < victims; i++) {
                const MeshKey key = order[i]->key;
                auto it = g_meshes.find(key);
                drop_mesh_refs(*it->second);
                g_meshes.erase(it);
            }
        }
    }
}

// Read POSITION and the bone indices out of the staged vertices. Render thread.
BoundsResult parse_batch_entry(const BoundsRequest& r, const uint8_t* staged) {
    BoundsResult out;
    out.key = r.key;
    out.id = r.id;
    const uint8_t* vb = staged + r.vb_at;
    const uint8_t* ib = staged + r.ib_at;
    const bool wide = r.ib_format == DXGI_FORMAT_R32_UINT;
    const uint32_t restart = wide ? 0xFFFFFFFFu : 0xFFFFu;  // the game draws strips, cut by this index
    float lo[kPaletteSlots][3], hi[kPaletteSlots][3];
    for (UINT s = 0; s < kPaletteSlots; s++) {
        lo[s][0] = lo[s][1] = lo[s][2] = FLT_MAX;
        hi[s][0] = hi[s][1] = hi[s][2] = -FLT_MAX;
    }
    uint64_t used = 0;
    if (r.vb_bytes < r.stride) return out;
    const int64_t last_vertex = static_cast<int64_t>(r.vb_bytes) - r.stride;
    for (UINT i = 0; i < r.count; i++) {
        uint32_t index;
        if (wide) {
            memcpy(&index, ib + static_cast<size_t>(i) * 4, 4);
        } else {
            uint16_t narrow;
            memcpy(&narrow, ib + static_cast<size_t>(i) * 2, 2);
            index = narrow;
        }
        if (index == restart) continue;
        const int64_t at = static_cast<int64_t>(r.vb_offset) + (static_cast<int64_t>(r.base) + index) * r.stride;
        if (at < 0 || at > last_vertex) continue;
        const uint8_t* v = vb + at;
        float p[3];
        memcpy(p, v + r.position, sizeof(p));
        const uint8_t* bones = v + r.indices;
        int16_t weights[4] = {1, 0, 0, 0};
        if (r.weights >= 0) memcpy(weights, v + r.weights, sizeof(weights));
        for (int b = 0; b < 4; b++) {
            const UINT slot = bones[b];
            if (weights[b] == 0 || slot >= kPaletteSlots) continue;
            used |= 1ull << slot;
            for (int c = 0; c < 3; c++) {
                lo[slot][c] = std::min(lo[slot][c], p[c]);
                hi[slot][c] = std::max(hi[slot][c], p[c]);
            }
        }
    }
    out.used = used;
    out.ok = used != 0;
    for (UINT s = 0; s < kPaletteSlots; s++) {
        if (!(used & (1ull << s))) continue;
        SlotBox box;
        box.slot = s;
        memcpy(box.min, lo[s], sizeof(box.min));
        memcpy(box.max, hi[s], sizeof(box.max));
        out.boxes.push_back(box);
    }
    return out;
}

}  // namespace

void casters_init(ID3D11Device* device) {
    g_device = device;
    casters_read_settings();
}

void casters_read_settings() {
    g_set.enabled = cfg_bool("casters.enabled", true);
    g_set.warp = cfg_float("casters.warp_distance", 200.0f);
    g_set.keep = cfg_float("casters.keep_distance", 700.0f);
    g_set.forget_distance = cfg_float("casters.forget_distance", 12.0f);
    g_set.forget_frames = cfg_int("casters.forget_frames", 45);
    g_set.move_tolerance = cfg_float("casters.move_tolerance", 0.003f);
    g_set.moves_to_dynamic = std::max(cfg_int("casters.moves_to_dynamic", 2), 1);
    g_set.move_window = cfg_int("casters.move_window", 240);
    g_set.max_draws = std::max(cfg_int("casters.max_draws", 6000), 16);
    g_set.max_meshes = std::max(cfg_int("casters.max_meshes", 12000), 16);
    g_set.bounds_draws = static_cast<UINT>(std::max(cfg_int("casters.bounds_draws", 24), 1));
    g_set.bounds_bytes = static_cast<UINT>(std::clamp(cfg_int("casters.bounds_bytes", 8 << 20), 1 << 16, 64 << 20));
    g_set.bounds_indices = static_cast<UINT>(std::max(cfg_int("casters.bounds_indices", 131072), 1024));
    g_set.idle_frames = cfg_int("casters.idle_frames", 120);
}

void casters_note_input_layout(ID3D11InputLayout* layout, const D3D11_INPUT_ELEMENT_DESC* desc, UINT count) {
    if (!layout || !desc) return;
    LayoutInfo info;
    for (UINT i = 0; i < count; i++) {
        const D3D11_INPUT_ELEMENT_DESC& e = desc[i];
        if (e.InputSlotClass == D3D11_INPUT_PER_INSTANCE_DATA) info.per_instance = true;
        if (_stricmp(e.SemanticName, "BLENDWEIGHT") == 0) info.skinned = true;
        if (e.InputSlot != 0 || e.AlignedByteOffset == D3D11_APPEND_ALIGNED_ELEMENT || e.SemanticIndex != 0) continue;
        const int offset = static_cast<int>(e.AlignedByteOffset);
        if (_stricmp(e.SemanticName, "POSITION") == 0 && e.Format == DXGI_FORMAT_R32G32B32_FLOAT) info.position = offset;
        if (_stricmp(e.SemanticName, "BLENDINDICES") == 0 && e.Format == DXGI_FORMAT_R8G8B8A8_UINT) info.indices = offset;
        if (_stricmp(e.SemanticName, "BLENDWEIGHT") == 0 && e.Format == DXGI_FORMAT_R16G16B16A16_SNORM) info.weights = offset;
    }
    // Skinned, but with weights this code cannot read: it stays unbounded.
    if (info.skinned && info.weights < 0) info.position = -1;
    std::unique_lock<std::shared_mutex> lock(g_layout_lock);
    g_layouts[layout] = info;
}

void casters_on_present(ID3D11DeviceContext* immediate) {
    if (!g_device || !immediate) return;
    // Nothing is being drawn and the engine has not been turned off for a comparison: a loading screen. The old map's
    // references go now, so its memory really is freed while the next one loads. The hitch is behind the loading screen.
    if (g_set.idle_frames > 0 && engine_enabled() && g_idle_presents.fetch_add(1, std::memory_order_relaxed) >= g_set.idle_frames) {
        g_idle_presents.store(0, std::memory_order_relaxed);
        bool any;
        {
            std::lock_guard<std::mutex> lock(g_store_lock);
            any = !g_meshes.empty();
            if (any) g_stats.idle_clears++;
        }
        if (any) casters_clear();
    }
    if (!g_batch.empty()) {
        D3D11_MAPPED_SUBRESOURCE mapped;
        const HRESULT hr = immediate->Map(g_staging, 0, D3D11_MAP_READ, D3D11_MAP_FLAG_DO_NOT_WAIT, &mapped);
        if (hr == DXGI_ERROR_WAS_STILL_DRAWING) return;
        std::vector<BoundsResult> results;
        results.reserve(g_batch.size());
        for (const BoundsRequest& r : g_batch) {
            if (SUCCEEDED(hr)) {
                results.push_back(parse_batch_entry(r, static_cast<const uint8_t*>(mapped.pData)));
            } else {
                BoundsResult failed;
                failed.key = r.key;
                failed.id = r.id;
                results.push_back(std::move(failed));
            }
        }
        if (SUCCEEDED(hr)) immediate->Unmap(g_staging, 0);
        for (BoundsRequest& r : g_batch) drop_request(r);
        g_batch.clear();
        std::lock_guard<std::mutex> lock(g_queue_lock);
        for (BoundsResult& r : results) g_results.push_back(std::move(r));
    }

    {
        std::lock_guard<std::mutex> lock(g_queue_lock);
        if (g_requests_first.empty() && g_requests.empty()) return;
    }
    if (!g_staging || g_staging_bytes != g_set.bounds_bytes) {
        drop(g_staging);
        g_staging_bytes = 0;
        D3D11_BUFFER_DESC b{};
        b.ByteWidth = g_set.bounds_bytes;
        b.Usage = D3D11_USAGE_STAGING;
        b.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        if (FAILED(g_device->CreateBuffer(&b, nullptr, &g_staging))) return;
        g_staging_bytes = g_set.bounds_bytes;
    }

    UINT at = 0, indices = 0;
    std::vector<BoundsRequest> failed;
    {
        std::lock_guard<std::mutex> lock(g_queue_lock);
        while (g_batch.size() < g_set.bounds_draws) {
            std::deque<BoundsRequest>& queue = g_requests_first.empty() ? g_requests : g_requests_first;
            if (queue.empty()) break;
            BoundsRequest r = queue.front();
            const UINT index_size = r.ib_format == DXGI_FORMAT_R32_UINT ? 4 : 2;
            r.ib_bytes = r.count * index_size;
            const uint64_t needed = static_cast<uint64_t>(r.vb_bytes) + r.ib_bytes + 32;
            const uint64_t index_end = static_cast<uint64_t>(r.ib_offset) + static_cast<uint64_t>(r.start) * index_size + r.ib_bytes;
            if (needed > g_staging_bytes || index_end > r.ib_width) {
                // Larger than a batch can ever hold: it stays unbounded, visible to everything.
                failed.push_back(r);
                queue.pop_front();
                continue;
            }
            if (at + needed > g_staging_bytes) break;
            if (!g_batch.empty() && indices + r.count > g_set.bounds_indices) break;
            queue.pop_front();
            indices += r.count;
            r.vb_at = at;
            at = (at + r.vb_bytes + 15) & ~15u;
            r.ib_at = at;
            at = (at + r.ib_bytes + 15) & ~15u;
            g_batch.push_back(r);
        }
    }
    for (BoundsRequest& r : g_batch) {
        const D3D11_BOX vb_box{0, 0, 0, r.vb_bytes, 1, 1};
        immediate->CopySubresourceRegion(g_staging, 0, r.vb_at, 0, 0, r.vb, 0, &vb_box);
        const UINT index_size = r.ib_format == DXGI_FORMAT_R32_UINT ? 4 : 2;
        const UINT from = r.ib_offset + r.start * index_size;
        const D3D11_BOX ib_box{from, 0, 0, from + r.ib_bytes, 1, 1};
        immediate->CopySubresourceRegion(g_staging, 0, r.ib_at, 0, 0, r.ib, 0, &ib_box);
    }
    if (!failed.empty()) {
        std::vector<BoundsResult> results;
        for (BoundsRequest& r : failed) {
            BoundsResult result;
            result.key = r.key;
            result.id = r.id;
            results.push_back(std::move(result));
            drop_request(r);
        }
        std::lock_guard<std::mutex> lock(g_queue_lock);
        for (BoundsResult& r : results) g_results.push_back(std::move(r));
    }
}

void casters_ingest(CasterList& list, const ViewConstants& view, const float taa_camera[3]) {
    std::lock_guard<std::mutex> lock(g_store_lock);
    const bool enabled = g_set.enabled;  // settings change on the render thread
    g_idle_presents.store(0, std::memory_order_relaxed);
    if (g_device) std::call_once(g_create_once, create_resources);
    g_frame_no++;
    g_stats.replayed = 0;
    release_frame_list();
    apply_bounds_results();

    const V3 taa{taa_camera[0], taa_camera[1], taa_camera[2]};
    const bool taa_known = taa.x != 0.0f || taa.y != 0.0f || taa.z != 0.0f;
    V3 camera_prev;
    bool have_prev;
    {
        std::lock_guard<std::mutex> camera_lock(g_camera_lock);
        camera_prev = g_camera;
        have_prev = g_have_camera;
    }
    const float warp_sq = g_set.warp * g_set.warp;
    const bool taa_jumped = taa_known && g_have_taa && len_sq(taa - g_taa_prev) > warp_sq;
    // The camera moved by what TAA moved. When nothing can vote this is the best guess, a frame late.
    V3 camera = taa_known ? taa : camera_prev;
    if (have_prev && taa_known && g_have_taa) camera = camera_prev + (taa - g_taa_prev);
    if (taa_jumped || (!enabled && !g_meshes.empty())) {
        clear_store_locked();
        camera = taa;
        have_prev = false;
    }

    g_votes.clear();
    g_rel.clear();
    g_scratch_meshes.clear();
    g_camera_certain = true;  // with nothing to vote there is nothing to contradict: this frame starts the store's frame
    if (enabled) {
        g_scratch_meshes.reserve(list.draws.size());
        for (CasterDraw& d : list.draws) {
            Mesh* m = mesh_for(d);
            m->last_seen = g_frame_no;
            g_scratch_meshes.push_back(m);
            if (m->frame_only()) continue;
            const V3 rel0 = slot_translation(reinterpret_cast<const float*>(&list.vs_constants[d.constants_at]), 0);
            g_rel.push_back(rel0);
            // A mesh standing in several places votes from the one nearest where the guess puts it.
            const Placement* closest = nullptr;
            float closest_sq = FLT_MAX;
            for (const Placement* p : m->placements) {
                const float sq = len_sq(slot_translation(p->regs.get(), 0) - (rel0 + camera));
                if (sq < closest_sq) {
                    closest_sq = sq;
                    closest = p;
                }
            }
            if (closest) g_votes.push_back(slot_translation(closest->regs.get(), 0) - rel0);
        }
        V3 voted;
        uint32_t members = 0;
        if (largest_group(g_votes, voted, members)) {
            // A clear group, or a majority of a few. Anything less and this frame's draws stay out of the store.
            const uint32_t votes = static_cast<uint32_t>(g_votes.size());
            g_camera_certain = (members >= 3 && members * 4 >= votes) || members * 2 > votes;
            if (g_camera_certain) {
                if (have_prev && len_sq(voted - camera_prev) > warp_sq) {
                    // Everything the store knows says the camera jumped: start over.
                    clear_store_locked();
                    have_prev = false;
                    g_scratch_meshes.clear();
                    for (CasterDraw& d : list.draws) {
                        Mesh* m = mesh_for(d);
                        m->last_seen = g_frame_no;
                        g_scratch_meshes.push_back(m);
                    }
                }
                camera = voted;
            }
        }
        // Static draws, a store, and not one draw in common. Walking and turning always keep something in view, so this is a
        // cut or a reload, or at most a tiny view of something new. The guess goes by TAA, a frame late, so right now it can be
        // off by the whole cut, and anything placed against it would split the store into two frames: this frame stays out.
        // Next frame TAA has caught up, and either shows the cut, or shows normal motion and the guess can be trusted again.
        // With no TAA pass at all there is nothing to catch up with, so a whole world still unmatched next frame starts over.
        if (g_votes.empty() && !g_rel.empty() && !g_all.empty()) {
            constexpr size_t kWorldDraws = 8;
            constexpr float kCutPerFrame = 2.0f;
            g_no_overlap++;
            g_stats.no_overlap_frames++;
            const bool have_motion = taa_known && g_have_taa;
            const bool cut = have_motion && len_sq(taa - g_taa_prev) > kCutPerFrame * kCutPerFrame;
            if (g_rel.size() >= kWorldDraws && (cut || (!have_motion && g_no_overlap >= 2))) {
                g_stats.cut_clears++;
                clear_store_locked();
                if (taa_known) camera = taa;
                have_prev = false;
                g_scratch_meshes.clear();
                for (CasterDraw& d : list.draws) {
                    Mesh* m = mesh_for(d);
                    m->last_seen = g_frame_no;
                    g_scratch_meshes.push_back(m);
                }
                g_no_overlap = 0;
            } else if (g_no_overlap == 1 || !have_motion) {
                g_camera_certain = false;
            }
        } else {
            g_no_overlap = 0;
        }
        g_stats.votes = static_cast<uint32_t>(g_votes.size());
        g_stats.inliers = members;
        V3 shared;
        uint32_t shared_members = 0;
        if (largest_group(g_rel, shared, shared_members) && shared_members >= 8) {
            g_origin = shared + camera;
            g_have_origin = true;
        }
    }
    {
        std::lock_guard<std::mutex> camera_lock(g_camera_lock);
        g_camera = camera;
        g_have_camera = true;
    }
    if (taa_known) {
        g_taa_prev = taa;
        g_have_taa = true;
    }

    g_have_view_planes = view.valid;
    if (view.valid) {
        const float cam[3] = {camera.x, camera.y, camera.z};
        caster_planes_from_rows(view.vp, cam, g_view_planes);
    }
    g_claimed.assign(list.draws.size(), 0);
    for (size_t i = 0; i < list.draws.size(); i++) {
        CasterDraw& d = list.draws[i];
        drop_game_constants(d);
        if (!enabled || !g_camera_certain || g_scratch_meshes[i]->frame_only()) continue;
        g_claimed[i] = claim_exact(g_scratch_meshes[i], d, reinterpret_cast<const float*>(&list.vs_constants[d.constants_at]), camera) ? 1 : 0;
    }
    for (size_t i = 0; i < list.draws.size(); i++) {
        if (g_claimed[i]) continue;
        CasterDraw& d = list.draws[i];
        const float* rel = reinterpret_cast<const float*>(&list.vs_constants[d.constants_at]);
        Mesh* m = enabled ? g_scratch_meshes[i] : nullptr;
        if (!m) {
            add_frame_draw(d, rel, nullptr, camera);
        } else if (m->frame_only() || !g_camera_certain) {
            add_frame_draw(d, rel, m, camera);
        } else {
            store_static(m, d, rel, camera);
        }
    }
    list.draws.clear();
    // Frame draws point into this storage, and a swap keeps it where it is.
    g_frame_constants.swap(list.vs_constants);
    list.vs_constants.clear();

    if (enabled) housekeeping(view, camera);
}

void casters_end_frame() {
    std::lock_guard<std::mutex> lock(g_store_lock);
    release_frame_list();
}

void casters_clear() {
    std::lock_guard<std::mutex> lock(g_store_lock);
    release_frame_list();
    clear_store_locked();
}

bool casters_camera(float out[3]) {
    std::lock_guard<std::mutex> lock(g_camera_lock);
    out[0] = g_camera.x;
    out[1] = g_camera.y;
    out[2] = g_camera.z;
    return g_have_camera;
}

uint32_t casters_generation() {
    return g_generation;
}

uint32_t casters_serial() {
    return g_serial;
}

void casters_take_events(std::vector<CasterEvent>& out) {
    std::lock_guard<std::mutex> lock(g_store_lock);
    out.swap(g_events);
    g_events.clear();
}

void caster_planes_from_rows(const float rows[16], const float camera[3], CasterPlanes& out) {
    const float* x = rows;
    const float* y = rows + 4;
    const float* z = rows + 8;
    const float* w = rows + 12;
    for (int i = 0; i < 4; i++) {
        out.p[0][i] = w[i] + x[i];
        out.p[1][i] = w[i] - x[i];
        out.p[2][i] = w[i] + y[i];
        out.p[3][i] = w[i] - y[i];
        out.p[4][i] = z[i];
        out.p[5][i] = w[i] - z[i];
    }
    // The rows take camera-relative positions; the store's bounds are in store space.
    for (auto& p : out.p) p[3] -= p[0] * camera[0] + p[1] * camera[1] + p[2] * camera[2];
}

bool caster_box_outside(const CasterPlanes& planes, const float min[3], const float max[3]) {
    for (const auto& n : planes.p) {
        const float x = n[0] >= 0 ? max[0] : min[0];
        const float y = n[1] >= 0 ? max[1] : min[1];
        const float z = n[2] >= 0 ? max[2] : min[2];
        if (n[0] * x + n[1] * y + n[2] * z + n[3] < 0) return true;
    }
    return false;
}

static bool box_holds(const float min[3], const float max[3], const float p[3]) {
    for (int i = 0; i < 3; i++) {
        if (p[i] < min[i] || p[i] > max[i]) return false;
    }
    return true;
}

bool caster_box_outside_sphere(const float s[4], const float min[3], const float max[3]) {
    float sq = 0.0f;
    for (int i = 0; i < 3; i++) {
        const float d = s[i] < min[i] ? min[i] - s[i] : s[i] > max[i] ? s[i] - max[i] : 0.0f;
        sq += d * d;
    }
    return sq > s[3] * s[3];
}

UINT casters_replay(ID3D11DeviceContext* ctx, const CasterReplay& r) {
    if (!ctx || !r.rows || !r.raster || !g_ready) return 0;
    std::lock_guard<std::mutex> lock(g_store_lock);
    float camera[3];
    {
        std::lock_guard<std::mutex> camera_lock(g_camera_lock);
        camera[0] = g_camera.x;
        camera[1] = g_camera.y;
        camera[2] = g_camera.z;
    }
    CasterPlanes planes;
    caster_planes_from_rows(r.rows, camera, planes);

    ctx->VSSetConstantBuffers(0, 1, &g_b0);
    ctx->VSSetConstantBuffers(2, 1, &g_b2);
    ctx->PSSetConstantBuffers(1, 1, &g_ps_b1);

    // Only what changes gets set; most neighbouring draws share a shader and a state.
    ID3D11InputLayout* last_layout = nullptr;
    ID3D11VertexShader* last_vs = nullptr;
    ID3D11PixelShader* last_ps = nullptr;
    ID3D11RasterizerState* last_raster = nullptr;
    ID3D11ShaderResourceView* last_t0 = nullptr;
    ID3D11SamplerState* last_s0 = nullptr;
    D3D11_PRIMITIVE_TOPOLOGY last_topology = D3D11_PRIMITIVE_TOPOLOGY_UNDEFINED;
    uint8_t last_b1[kPsB1Bytes];
    bool have_b1 = false;
    bool first = true;
    UINT drawn = 0;

    auto record = [&](const CasterDraw& d, const float* regs, bool rebase, UINT slots) {
        D3D11_MAPPED_SUBRESOURCE mapped;
        if (FAILED(ctx->Map(g_b0, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) return;
        // The mapping may be slow to read, so everything is computed from the source and only written there.
        float* out = static_cast<float*>(mapped.pData);
        memcpy(out, r.rows, 16 * sizeof(float));
        memcpy(out + 16, regs + 16, kCasterVsBytes - 16 * sizeof(float));
        if (rebase) {
            for (UINT s = 0; s < slots; s++) {
                const float* from = slot_rows(regs, s);
                float* to = slot_rows(out, s);
                to[3] = from[3] - camera[0];
                to[7] = from[7] - camera[1];
                to[11] = from[11] - camera[2];
            }
        }
        ctx->Unmap(g_b0, 0);

        if (first || d.layout != last_layout) {
            ctx->IASetInputLayout(d.layout);
            last_layout = d.layout;
        }
        if (first || d.topology != last_topology) {
            ctx->IASetPrimitiveTopology(d.topology);
            last_topology = d.topology;
        }
        ctx->IASetVertexBuffers(0, 2, d.vb, d.stride, d.offset);
        ctx->IASetIndexBuffer(d.ib, d.index_format, d.index_offset);
        if (first || d.vs != last_vs) {
            ctx->VSSetShader(d.vs, nullptr, 0);
            last_vs = d.vs;
        }
        if (first || d.ps != last_ps) {
            ctx->PSSetShader(d.ps, nullptr, 0);
            last_ps = d.ps;
        }
        if (d.ps) {
            if (d.ps_t0 != last_t0) {
                ctx->PSSetShaderResources(0, 1, &d.ps_t0);
                last_t0 = d.ps_t0;
            }
            if (d.ps_s0 != last_s0) {
                ctx->PSSetSamplers(0, 1, &d.ps_s0);
                last_s0 = d.ps_s0;
            }
            if (!have_b1 || memcmp(last_b1, d.ps_b1_bytes, kPsB1Bytes) != 0) {
                if (SUCCEEDED(ctx->Map(g_ps_b1, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) {
                    memcpy(mapped.pData, d.ps_b1_bytes, kPsB1Bytes);
                    ctx->Unmap(g_ps_b1, 0);
                    memcpy(last_b1, d.ps_b1_bytes, kPsB1Bytes);
                    have_b1 = true;
                }
            }
        }
        const int cull = r.two_sided || d.cull == D3D11_CULL_NONE ? 0 : d.cull == D3D11_CULL_FRONT ? 1 : 2;
        ID3D11RasterizerState* raster = r.raster[cull][d.front_ccw ? 1 : 0];
        if (first || raster != last_raster) {
            ctx->RSSetState(raster);
            last_raster = raster;
        }
        first = false;
        if (d.instanced) {
            ctx->DrawIndexedInstanced(d.index_count, d.instance_count, d.start_index, d.base_vertex, d.start_instance);
        } else {
            ctx->DrawIndexed(d.index_count, d.start_index, d.base_vertex);
        }
        drawn++;
    };

    if (r.frame) {
        for (const FrameDraw& f : g_frame) {
            if (f.sun_only && !r.sun_only) continue;
            if (f.bounded) {
                if (r.skip_containing && f.character && box_holds(f.min, f.max, r.skip_containing)) continue;
                if (r.sphere && caster_box_outside_sphere(r.sphere, f.min, f.max)) continue;
                if (caster_box_outside(planes, f.min, f.max)) continue;
            }
            record(f.draw, f.regs, false, 0);
        }
    }
    if (r.store) {
        const UINT limit = r.budget ? drawn + r.budget : UINT_MAX;
        for (auto it = g_sorted.rbegin(); it != g_sorted.rend(); ++it) {
            const SortedPlacement& e = *it;
            if (e.radius < r.min_radius || drawn >= limit) break;
            // Until its bound is known a draw is treated as visible to everything.
            if (e.radius != FLT_MAX) {
                if (r.sphere && caster_box_outside_sphere(r.sphere, e.min, e.max)) continue;
                if (caster_box_outside(planes, e.min, e.max)) continue;
            }
            record(e.placement->draw, e.placement->regs.get(), true, e.placement->mesh->rebase_slots);
        }
    }
    g_stats.replayed += drawn;
    return drawn;
}

uint32_t casters_frame_overlap(const CasterPlanes* planes, UINT count, const float sphere[4], const float* skip_containing) {
    std::lock_guard<std::mutex> lock(g_store_lock);
    uint32_t mask = 0;
    const uint32_t all = count >= 32 ? 0xFFFFFFFFu : (1u << count) - 1u;
    for (const FrameDraw& f : g_frame) {
        if (f.sun_only) continue;
        if (!f.bounded) return all;  // visible to everything until its bound is known
        if (skip_containing && f.character && box_holds(f.min, f.max, skip_containing)) continue;
        if (sphere && caster_box_outside_sphere(sphere, f.min, f.max)) continue;
        for (UINT i = 0; i < count; i++) {
            if (!(mask & (1u << i)) && !caster_box_outside(planes[i], f.min, f.max)) mask |= 1u << i;
        }
        if (mask == all) break;
    }
    return mask;
}

CasterStats casters_stats() {
    std::lock_guard<std::mutex> lock(g_store_lock);
    CasterStats s = g_stats;
    s.meshes = static_cast<uint32_t>(g_meshes.size());
    s.placements = static_cast<uint32_t>(g_all.size());
    for (const Placement* p : g_all) {
        if (!p->bounded) s.unbounded++;
    }
    s.frame_draws = static_cast<uint32_t>(g_frame.size());
    s.generation = g_generation;
    s.serial = g_serial;
    {
        std::lock_guard<std::mutex> queue_lock(g_queue_lock);
        s.requests_pending = static_cast<uint32_t>(g_requests.size() + g_requests_first.size());
    }
    return s;
}
