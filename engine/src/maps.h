// Per-map settings. Which map the player is in comes from constants the voting draws already carry: every FRPG_Phn_ or
// FRPG_Gst_ pixel shader with HemEnv in its name, forward lit or light-mapped. c73 is the sun direction of the
// ShadowBank row the player's collision names, looked up in the archive of the piece being drawn, so pieces of
// different archives in one frame give different directions. The group most of a frame's draws name decides when it
// holds c73_share of them, and alone where no other map's collisions name that direction. Where some do, the light rows
// (c5, c6) of the frame's draws vote, against evidence sets that tools/maps_table.py --emit works out from the
// DrawParam and the MSBs and writes to dsrle\maps_detect.txt. The chosen map's [map.mAA_BB] values, dsrle.ini's over
// maps.ini's, are eased in over the global ones through config's overlay.
//
// Settings in dsrle.ini [maps]. Live.
//   enabled = 1          0 = the global values everywhere, and no draw votes
//   apply = 1            0 = find the map and log it, but leave every value as it is
//   blend_seconds = 1.5  how long one map's values take to ease into the next's
//   log = 0              1 = a status line every 60 frames, and the light rows and directions the table does not know
//   confirm_frames = 6   frames in a row a new map must be found before its values start
//   load_frames = 30     frames in a row with no voting draw that count as a loading screen
//   snap_seconds = 2     for this long after the first frame past a load, 2 frames confirm and values switch at once
//   c73_share = 0.75     the share of the draws whose c73 is in a group that the most named group must have; with less
//                        the frame is mixed and gets the globals. 0 to 1
//   min_votes = 3        draws of a map's own light rows in a frame before it counts as seen, at least 1
//   dominance = 4        and how many times they must outnumber those of a rival behind a loading screen
//   walk_share = 0.25    where a map may not be held (a rival next to it on foot could show a frame like its own), the
//                        share of all votes its own draws must reach
//   hold_frames = 900    sampled frames in a row with none of its own draws before a held map lets go
#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

// dsrle\maps_detect.txt, format 1. The format is written down in tools/maps_table.py, whose parse_detect is the reference
// reader; this one refuses the same files.
struct MapsEvidence {
    bool hold = false;     // no walk rival can show the group without a WALK draw: the map may be held there, and
                           // walk_share does not apply
    std::vector<int> pos;  // tuple indexes
    std::vector<int> walk;
    std::vector<int> sep;
};

struct MapsGroup {
    bool global_only = false;
    std::vector<int> claimants;                // map indexes, ascending
    std::vector<uint8_t> no_evidence;          // per claimant
    std::vector<std::array<float, 3>> dirs;
    std::vector<MapsEvidence> ev;              // per claimant; empty when the group has one
};

struct MapsTuple {
    float c5[3];
    float c6[3];
    std::vector<int> owners;  // for the log
};

struct MapsTable {
    std::string dp[9];  // sha256 of param\DrawParam\a10..a18_DrawParam.parambnd.dcx, lowercase hex
    double dir_deg = 0.0, c6_rel = 0.0, c6_abs = 0.0;
    double cos_tol = 1.0;
    std::vector<std::string> maps;
    std::vector<uint8_t> has_values;  // the maps maps.ini ships values for: the only ones a decision can name
    std::vector<MapsTuple> tuples;
    std::vector<MapsGroup> groups;
    size_t dir_count = 0;
    size_t ev_count = 0;
};

bool maps_parse_table(const char* text, size_t size, MapsTable* out, std::string* error);
// The first group with a direction within dir_deg of c73, or -1, as for anything that is not a finite vector of about
// unit length: one along a direction but longer would otherwise match it.
int maps_match_group(const MapsTable& table, const float* c73);
// The first tuple a forward draw's c5 and c6 match, or -1.
int maps_match_tuple(const MapsTable& table, const float* c5, const float* c6);
// Compares the table's dp hashes with the files in dir (game_dir()\param\DrawParam\ in the game). bad lists the areas
// that differ or are missing.
bool maps_check_hashes(const MapsTable& table, const std::wstring& dir, std::string* bad);

// The resolver: one call per present, no D3D or file access.
struct MapsRules {
    int min_votes = 3;
    float dominance = 4.0f;
    float walk_share = 0.25f;
    int confirm_frames = 6;
    int load_frames = 30;
    float snap_seconds = 2.0f;
    float c73_share = 0.75f;
    int hold_frames = 900;
    float warp_distance = 200.0f;  // casters.warp_distance: a TAA camera jump past this from one frame to the next is
                                   // a load
};

struct MapsFrame {
    const uint32_t* groups = nullptr;  // this frame's voting draws per group their c73 is in
    uint32_t c73_miss = 0;             // and those whose c73 is in none
    bool taa_fresh = false;            // the TAA resolve published the camera this frame; never with anti-aliasing off
    float camera[3] = {};              // a camera at the origin is no reading, as a frame that is not fresh
    const uint32_t* votes = nullptr;   // this frame's draws per tuple
    double now = 0.0;                  // seconds
};

struct MapsState {
    int target = -1;     // map index, or -1 for the globals
    int held = -1;       // L: the last map found since the last load
    int candidate = -2;  // the candidate being confirmed; -2 before the first
    int run = 0;
    bool run_blank = true;
    int blank = 0;
    int hold_zero = 0;   // sampled frames in a row in a shared group with no POS draw for L
    bool taa_prev = false;  // the last frame had a camera reading: fresh, and not at the origin
    float camera_prev[3] = {};
    bool window_armed = false;
    bool window_open = false;
    double window_start = 0.0;
};

struct MapsDecision {
    int group = -2;      // the most named group; -2 blank, -1 no draw's c73 in a group
    bool mixed = false;  // that group had less than c73_share of the draws in one
    int candidate = -1;  // -1 for the globals
    const char* why = "blank";
    bool load = false;
    int blank_run = 0;
    float taa_jump = -1.0f;  // metres, or -1 without two cameras in a row, each fresh and not at the origin
    bool changed = false;    // the target changed
    bool snap = false;
    bool expired = false;    // L let go after hold_frames
    int released = -1;       // which map it was
};

MapsDecision maps_resolve(const MapsTable& table, const MapsRules& rules, MapsState& state, const MapsFrame& frame);

// The eased values: one per key some [map.*] section names, written over dsrle.ini's through config_set_overlay.
struct MapsBlendKey {
    std::string key;
    bool cur_p = false, from_p = false, to_p = false, base_p = false, written_p = false;
    float cur = 0.0f, from = 0.0f, to = 0.0f, base = 0.0f, written = 0.0f;
    bool snap = false;
};

struct MapsBlend {
    std::vector<MapsBlendKey> keys;
    std::string target;  // map id, empty for the globals
    double t = 1.0;
    bool settled = true;
};

// Keys kept keep their values; new ones start at dsrle.ini's, which is what is in effect for a key not eased before.
void maps_blend_set_keys(MapsBlend& blend, const std::vector<std::string>& keys);
void maps_blend_retarget(MapsBlend& blend, const std::string& map, bool snap);
// Returns true when a value engine.cpp's read_pack_values reads has changed.
bool maps_blend_step(MapsBlend& blend, double dt, double seconds);

// Whether draws with this pixel shader vote: FRPG_Phn_ or FRPG_Gst_ with HemEnv in the name. A wider family than
// Role::ForwardLit's Csd_HemEnv, which alone publishes the forward constants: light-mapped pieces carry the same block.
bool maps_shader_votes(const char* name);

// The engine's side. Render thread, apart from the vote calls, which the recording threads make.
void maps_init();  // after config_load
struct MapsPresent {
    bool taa_fresh = false;
    float camera[3] = {};
};
// Once per present, right after config_poll. Returns true when read_pack_values must run.
bool maps_on_present(const MapsPresent& present, bool reloaded);

// Whether voting draws should vote: the table is loaded, its DrawParam is the installed one, and [maps] enabled.
bool maps_wants_votes();
// Counts presents. A copy of the pixel constants votes only in the frame it was taken.
uint32_t maps_frame();
// Every voting draw while votes are wanted, whether it has a copy of this frame to vote with or not.
void maps_note_forward_draw();

struct MapsVoteCache {
    struct Entry {
        uint32_t bits[9];  // c5.xyz, c6.xyz and c73.xyz
        int32_t tuple;
        int32_t group;
        bool used;
    };
    Entry entries[64] = {};
};
constexpr int32_t kMapsUnresolved = INT32_MIN;
// What a copy's lookup found, kept for later draws with the same copy: start tuple at kMapsUnresolved.
struct MapsVoteResult {
    int32_t tuple;
    int32_t group;
};
// One voting draw's vote from registers 5, 6 and 73 of its pixel constants (regs[0..3] c5, regs[4..7] c6, regs[8..10]
// c73): a tuple for the evidence sets and a direction for the frame's group.
void maps_vote(MapsVoteCache& cache, const float* regs, MapsVoteResult& result, bool instanced);
