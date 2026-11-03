#include "maps.h"
#include "common.h"
#include "config.h"
#include <bcrypt.h>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <memory>
#include <mutex>
#include <string_view>
#include <unordered_set>

#pragma comment(lib, "bcrypt")

namespace {

const char* const kAreas[9] = {"a10", "a11", "a12", "a13", "a14", "a15", "a16", "a17", "a18"};
constexpr double kPi = 3.14159265358979323846;

//------------------------------------------------------------------------------
// The table
//------------------------------------------------------------------------------
using Fields = std::vector<std::string_view>;

struct Lines {
    std::vector<std::string_view> lines;
    size_t pos = 0;
    std::string* error;

    bool fail(const char* what) {
        char buf[160];
        snprintf(buf, sizeof(buf), "line %zu: %s", pos, what);
        *error = buf;
        return false;
    }
    // The next line, which must be tag and exactly n fields, each separated by one space.
    bool take(const char* tag, size_t n, Fields* out) {
        out->clear();
        if (pos >= lines.size()) {
            pos++;
            return fail("the file ends early");
        }
        std::string_view line = lines[pos++];
        size_t start = 0;
        while (true) {
            const size_t space = line.find(' ', start);
            out->push_back(line.substr(start, space == std::string_view::npos ? std::string_view::npos : space - start));
            if (out->back().empty()) return fail("an empty field");
            if (space == std::string_view::npos) break;
            start = space + 1;
        }
        if (out->front() != tag || out->size() != n + 1) {
            char what[64];
            snprintf(what, sizeof(what), "expected '%s' with %zu fields", tag, n);
            return fail(what);
        }
        out->erase(out->begin());
        return true;
    }
};

bool to_uint(std::string_view s, uint32_t* out) {
    if (s.empty() || s.size() > 9 || (s.size() > 1 && s[0] == '0')) return false;
    uint32_t v = 0;
    for (char c : s) {
        if (c < '0' || c > '9') return false;
        v = v * 10 + static_cast<uint32_t>(c - '0');
    }
    *out = v;
    return true;
}

bool to_flag(std::string_view s, bool* out) {
    if (s != "0" && s != "1") return false;
    *out = s == "1";
    return true;
}

// Nine digits after the point, an optional minus and never -0.000000000. Read as an integer over 1e9, which rounds once,
// as a correctly rounded decimal reader would, and does not depend on the C locale.
bool to_double(std::string_view s, double* out) {
    const bool negative = !s.empty() && s[0] == '-';
    if (negative) s.remove_prefix(1);
    const size_t dot = s.find('.');
    if (dot == std::string_view::npos || dot == 0 || dot > 9 || s.size() - dot - 1 != 9) return false;
    int64_t v = 0;
    for (size_t i = 0; i < s.size(); i++) {
        if (i == dot) continue;
        if (s[i] < '0' || s[i] > '9') return false;
        v = v * 10 + (s[i] - '0');
    }
    if (negative && v == 0) return false;
    *out = static_cast<double>(negative ? -v : v) / 1e9;
    return true;
}

bool to_float(std::string_view s, float* out) {
    double v;
    if (!to_double(s, &v)) return false;
    *out = static_cast<float>(v);
    return true;
}

// Indexes below limit in strictly ascending order joined by commas, or "-".
bool to_list(std::string_view s, uint32_t limit, std::vector<int>* out) {
    out->clear();
    if (s == "-") return true;
    size_t start = 0;
    while (true) {
        const size_t comma = s.find(',', start);
        uint32_t v;
        if (!to_uint(s.substr(start, comma == std::string_view::npos ? std::string_view::npos : comma - start), &v) || v >= limit) return false;
        if (!out->empty() && static_cast<int>(v) <= out->back()) return false;
        out->push_back(static_cast<int>(v));
        if (comma == std::string_view::npos) return true;
        start = comma + 1;
    }
}

double dot3(const float* a, const float* b) {
    return static_cast<double>(a[0]) * b[0] + static_cast<double>(a[1]) * b[1] + static_cast<double>(a[2]) * b[2];
}

uint32_t count(const std::vector<int>& set, const uint32_t* votes) {
    uint32_t n = 0;
    for (int t : set) n += votes[t];
    return n;
}

//------------------------------------------------------------------------------
// The DrawParam check
//------------------------------------------------------------------------------
bool sha256_file(const std::wstring& path, std::string* hex) {
    HANDLE file = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
    if (file == INVALID_HANDLE_VALUE) return false;
    BCRYPT_ALG_HANDLE alg = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    bool ok = BCRYPT_SUCCESS(BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, nullptr, 0)) &&
              BCRYPT_SUCCESS(BCryptCreateHash(alg, &hash, nullptr, 0, nullptr, 0, 0));
    std::vector<uint8_t> buf(1 << 16);
    while (ok) {
        DWORD got = 0;
        if (!ReadFile(file, buf.data(), static_cast<DWORD>(buf.size()), &got, nullptr)) ok = false;
        if (!ok || got == 0) break;
        ok = BCRYPT_SUCCESS(BCryptHashData(hash, buf.data(), got, 0));
    }
    uint8_t digest[32];
    ok = ok && BCRYPT_SUCCESS(BCryptFinishHash(hash, digest, sizeof(digest), 0));
    if (hash) BCryptDestroyHash(hash);
    if (alg) BCryptCloseAlgorithmProvider(alg, 0);
    CloseHandle(file);
    if (!ok) return false;
    static const char kHex[] = "0123456789abcdef";
    hex->clear();
    for (uint8_t b : digest) {
        hex->push_back(kHex[b >> 4]);
        hex->push_back(kHex[b & 15]);
    }
    return true;
}

}  // namespace

bool maps_parse_table(const char* text, size_t size, MapsTable* out, std::string* error) {
    *out = MapsTable{};
    if (size == 0 || text[size - 1] != '\n') {
        *error = "not ended by a line end";
        return false;
    }
    for (size_t i = 0; i < size; i++) {
        const unsigned char c = static_cast<unsigned char>(text[i]);
        if (c >= 0x80 || c == '\r' || c == 0) {
            *error = "not ASCII text with \\n line ends";
            return false;
        }
    }
    Lines in;
    in.error = error;
    for (size_t start = 0; start < size;) {
        const size_t end = static_cast<size_t>(static_cast<const char*>(memchr(text + start, '\n', size - start)) - text);
        in.lines.push_back(std::string_view(text + start, end - start));
        start = end + 1;
    }
    Fields f;
    if (in.lines[0] != "dsrle_maps_detect 1") {
        in.pos = 1;
        return in.fail("not dsrle_maps_detect 1");
    }
    in.pos = 1;
    MapsTable& t = *out;
    for (int i = 0; i < 9; i++) {
        if (!in.take("dp", 2, &f)) return false;
        bool hex = f[1].size() == 64;
        for (char c : f[1]) hex = hex && ((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'));
        if (f[0] != kAreas[i] || !hex) return in.fail("expected the next area's hash");
        t.dp[i] = std::string(f[1]);
    }
    if (!in.take("tol", 3, &f)) return false;
    if (!to_double(f[0], &t.dir_deg) || !to_double(f[1], &t.c6_rel) || !to_double(f[2], &t.c6_abs)) return in.fail("bad tolerance");
    t.cos_tol = std::cos(t.dir_deg * kPi / 180.0);
    uint32_t n_maps, n_tuples, n_groups, n_dirs, n_ev;
    if (!in.take("counts", 5, &f)) return false;
    if (!to_uint(f[0], &n_maps) || !to_uint(f[1], &n_tuples) || !to_uint(f[2], &n_groups) || !to_uint(f[3], &n_dirs) ||
        !to_uint(f[4], &n_ev) || n_maps > 4096 || n_tuples > 65536 || n_groups > 4096 || n_dirs > 65536 || n_ev > 65536) {
        return in.fail("bad counts");
    }
    for (uint32_t i = 0; i < n_maps; i++) {
        uint32_t index;
        bool values;
        if (!in.take("map", 3, &f)) return false;
        if (!to_uint(f[0], &index) || index != i || f[1].size() != 6 || !to_flag(f[2], &values)) return in.fail("bad map record");
        t.maps.push_back(std::string(f[1]));
        t.has_values.push_back(values ? 1 : 0);
    }
    for (uint32_t i = 0; i < n_tuples; i++) {
        uint32_t index;
        MapsTuple tuple;
        if (!in.take("tuple", 8, &f)) return false;
        if (!to_uint(f[0], &index) || index != i) return in.fail("tuple out of order");
        for (int k = 0; k < 3; k++) {
            if (!to_float(f[1 + k], &tuple.c5[k]) || !to_float(f[4 + k], &tuple.c6[k])) return in.fail("bad number");
        }
        if (!to_list(f[7], n_maps, &tuple.owners)) return in.fail("bad list");
        t.tuples.push_back(std::move(tuple));
    }
    for (uint32_t g = 0; g < n_groups; g++) {
        uint32_t index;
        MapsGroup group;
        std::vector<int> no_evidence;
        if (!in.take("group", 4, &f)) return false;
        if (!to_uint(f[0], &index) || index != g || !to_flag(f[1], &group.global_only)) return in.fail("bad group record");
        if (!to_list(f[2], n_maps, &group.claimants) || !to_list(f[3], n_maps, &no_evidence) || group.claimants.empty()) {
            return in.fail("bad claimants");
        }
        group.no_evidence.assign(group.claimants.size(), 0);
        for (int m : no_evidence) {
            auto it = std::find(group.claimants.begin(), group.claimants.end(), m);
            if (it == group.claimants.end()) return in.fail("no_evidence holds a map that is not a claimant");
            group.no_evidence[static_cast<size_t>(it - group.claimants.begin())] = 1;
        }
        t.groups.push_back(std::move(group));
    }
    uint32_t last = 0;
    for (uint32_t i = 0; i < n_dirs; i++) {
        uint32_t g;
        std::array<float, 3> dir;
        if (!in.take("dir", 4, &f)) return false;
        if (!to_uint(f[0], &g) || g < last || g >= n_groups) return in.fail("dir out of order");
        for (int k = 0; k < 3; k++) {
            if (!to_float(f[1 + k], &dir[static_cast<size_t>(k)])) return in.fail("bad number");
        }
        last = g;
        t.groups[g].dirs.push_back(dir);
    }
    std::vector<std::vector<uint8_t>> have(n_groups);
    for (uint32_t g = 0; g < n_groups; g++) have[g].assign(t.groups[g].claimants.size(), 0);
    uint32_t last_g = 0, last_m = 0;
    for (uint32_t i = 0; i < n_ev; i++) {
        uint32_t g, m;
        MapsEvidence ev;
        if (!in.take("ev", 6, &f)) return false;
        if (!to_uint(f[0], &g) || !to_uint(f[1], &m) || g >= n_groups || !to_flag(f[2], &ev.hold)) return in.fail("bad ev record");
        if (i > 0 && (g < last_g || (g == last_g && m <= last_m))) return in.fail("ev out of order");
        last_g = g;
        last_m = m;
        MapsGroup& group = t.groups[g];
        auto it = std::find(group.claimants.begin(), group.claimants.end(), static_cast<int>(m));
        if (it == group.claimants.end()) return in.fail("ev for a map that is not a claimant");
        if (!to_list(f[3], n_tuples, &ev.pos) || !to_list(f[4], n_tuples, &ev.walk) || !to_list(f[5], n_tuples, &ev.sep)) {
            return in.fail("bad list");
        }
        const size_t k = static_cast<size_t>(it - group.claimants.begin());
        if (group.ev.empty()) group.ev.resize(group.claimants.size());
        group.ev[k] = std::move(ev);
        have[g][k] = 1;
    }
    if (!in.take("end", 0, &f)) return false;
    if (in.pos != in.lines.size()) return in.fail("lines after 'end'");
    for (uint32_t g = 0; g < n_groups; g++) {
        const MapsGroup& group = t.groups[g];
        const bool shared = group.claimants.size() > 1;
        const bool complete = std::all_of(have[g].begin(), have[g].end(), [](uint8_t h) { return h != 0; });
        const bool none = std::none_of(have[g].begin(), have[g].end(), [](uint8_t h) { return h != 0; });
        if (group.dirs.empty() || (shared ? !complete : !none)) {
            char buf[96];
            snprintf(buf, sizeof(buf), "group %u has no direction or an incomplete ev block", g);
            *error = buf;
            return false;
        }
    }
    t.dir_count = n_dirs;
    t.ev_count = n_ev;
    return true;
}

int maps_match_group(const MapsTable& table, const float* c73) {
    const double len2 = dot3(c73, c73);
    if (!std::isfinite(len2) || len2 < 0.81 || len2 > 1.21) return -1;
    for (size_t g = 0; g < table.groups.size(); g++) {
        for (const auto& dir : table.groups[g].dirs) {
            if (dot3(c73, dir.data()) >= table.cos_tol) return static_cast<int>(g);
        }
    }
    return -1;
}

int maps_match_tuple(const MapsTable& table, const float* c5, const float* c6) {
    for (size_t i = 0; i < table.tuples.size(); i++) {
        const MapsTuple& t = table.tuples[i];
        if (dot3(c5, t.c5) < table.cos_tol) continue;
        bool same = true;
        for (int k = 0; k < 3 && same; k++) {
            const double a = c6[k], b = t.c6[k];
            same = std::fabs(a - b) <= table.c6_abs + table.c6_rel * std::max(std::fabs(a), std::fabs(b));
        }
        if (same) return static_cast<int>(i);
    }
    return -1;
}

bool maps_shader_votes(const char* name) {
    return (strncmp(name, "FRPG_Phn_", 9) == 0 || strncmp(name, "FRPG_Gst_", 9) == 0) && strstr(name, "HemEnv");
}

bool maps_check_hashes(const MapsTable& table, const std::wstring& dir, std::string* bad) {
    bad->clear();
    for (int i = 0; i < 9; i++) {
        wchar_t name[40];
        swprintf_s(name, L"a%d_DrawParam.parambnd.dcx", 10 + i);
        std::string hex;
        const bool read = sha256_file(dir + name, &hex);
        if (read && hex == table.dp[i]) continue;
        if (!bad->empty()) bad->push_back(',');
        *bad += kAreas[i];
        if (!read) *bad += " missing";
    }
    return bad->empty();
}

//------------------------------------------------------------------------------
// The resolver: steps 0 to 5 of the design's detection rules
//------------------------------------------------------------------------------
namespace {

// Metres: a TAA camera with every component nearer the origin than this is no reading.
constexpr float kCameraNone = 1e-4f;

void set_held(MapsState& s, int map) {
    if (s.held != map) s.hold_zero = 0;
    s.held = map;
}

// Step 0's group: the one the most voting draws name by their c73, the lower index on a tie, or -1 when no draw names
// one. Mixed when it has less than c73_share of the draws that name a group; those whose c73 is in none are left out.
int frame_group(const MapsTable& t, const MapsRules& r, const uint32_t* groups, uint64_t* named, bool* mixed) {
    *named = 0;
    *mixed = false;
    if (!groups) return -1;
    uint32_t best = 0;
    int g = -1;
    for (size_t i = 0; i < t.groups.size(); i++) {
        *named += groups[i];
        if (groups[i] > best) {
            best = groups[i];
            g = static_cast<int>(i);
        }
    }
    *mixed = g >= 0 && static_cast<double>(best) < static_cast<double>(r.c73_share) * static_cast<double>(*named);
    return g;
}

// Steps 1 to 3 for a sampled frame. The same rules, in the same order, as resolve() in tools/maps_table.py, which
// --simulate replays over every collision before a table is written.
int candidate_of(const MapsTable& t, const MapsRules& r, MapsState& s, MapsDecision& d, const uint32_t* votes) {
    if (d.group < 0) {
        d.why = "unknown";
        return -1;
    }
    const MapsGroup& g = t.groups[static_cast<size_t>(d.group)];
    if (g.global_only) {
        d.why = "global_only";
        return -1;
    }
    if (g.claimants.size() == 1) {
        const int p = g.claimants[0];
        set_held(s, p);
        s.hold_zero = 0;
        d.why = "unique";
        return t.has_values[static_cast<size_t>(p)] ? p : -1;
    }
    uint32_t total = 0;
    for (size_t i = 0; i < t.tuples.size(); i++) total += votes[i];
    std::vector<uint32_t> pos(g.claimants.size());
    for (size_t k = 0; k < g.claimants.size(); k++) pos[k] = count(g.ev[k].pos, votes);

    // A held map lets go after hold_frames sampled frames in a row here with none of its POS draws, whatever else they
    // show: with anti-aliasing off a load that is not blank is missed, and nothing else would end a hold carried into
    // the rival's map before the rival's own geometry is drawn.
    if (s.held >= 0) {
        auto it = std::find(g.claimants.begin(), g.claimants.end(), s.held);
        const uint32_t mine = it == g.claimants.end() ? 0 : pos[static_cast<size_t>(it - g.claimants.begin())];
        if (mine > 0) {
            s.hold_zero = 0;
        } else if (++s.hold_zero > r.hold_frames) {
            d.released = s.held;
            s.held = -1;
            s.hold_zero = 0;
            d.expired = true;
        }
    }

    int chosen = -1, n_chosen = 0;
    const char* chosen_why = "";
    for (size_t k = 0; k < g.claimants.size(); k++) {
        const int m = g.claimants[k];
        const MapsEvidence& ev = g.ev[k];
        // One draw only a walk rival carries rules M out. That, not an empty POS, keeps the Depths' values out of
        // Blighttown: nearly all its pieces and the player's body there carry its own light rows.
        if (!t.has_values[static_cast<size_t>(m)] || count(ev.walk, votes) > 0) continue;
        const uint32_t sep = count(ev.sep, votes);
        // At least one draw: with none, a frame showing nothing of M's own would find M.
        if (pos[k] >= static_cast<uint32_t>(std::max(r.min_votes, 1)) && pos[k] >= static_cast<double>(r.dominance) * sep &&
            (ev.hold || pos[k] >= static_cast<double>(r.walk_share) * total)) {
            chosen = m;
            chosen_why = "vote";
            n_chosen++;
        } else if (s.held == m && ev.hold && sep == 0) {
            chosen = m;
            chosen_why = "hold";
            n_chosen++;
        }
    }
    if (n_chosen == 1) {
        set_held(s, chosen);
        d.why = chosen_why;
        return chosen;
    }
    // A map with no values of its own that clearly fills the frame is where the player is, though it keeps the globals.
    for (size_t k = 0; k < g.claimants.size(); k++) {
        const int x = g.claimants[k];
        if (t.has_values[static_cast<size_t>(x)] || pos[k] < static_cast<uint32_t>(std::max(r.min_votes, 1))) continue;
        bool dominant = true;
        for (size_t j = 0; j < g.claimants.size(); j++) {
            if (j != k && pos[k] < static_cast<double>(r.dominance) * pos[j]) dominant = false;
        }
        if (dominant) {
            set_held(s, x);
            d.why = "vote";
            return -1;
        }
    }
    d.why = d.expired ? "expired" : "none";
    return -1;
}

}  // namespace

MapsDecision maps_resolve(const MapsTable& table, const MapsRules& r, MapsState& s, const MapsFrame& in) {
    MapsDecision d;
    // Step 0. A sample is a frame with a voting draw, whether or not its c73 is in a group; anything else is blank.
    uint64_t named;
    bool mixed;
    const int group = frame_group(table, r, in.groups, &named, &mixed);
    const bool sample = named > 0 || in.c73_miss > 0;
    // At some bonfires the TAA resolve publishes the origin on some frames, and the next real camera would then read
    // as a jump of its distance from it. A camera at the origin is no reading, as for the casters' taa_known, and a
    // jump is measured only between readings on two frames in a row.
    const bool known = in.taa_fresh && !(std::fabs(in.camera[0]) < kCameraNone && std::fabs(in.camera[1]) < kCameraNone &&
                                         std::fabs(in.camera[2]) < kCameraNone);
    if (known && s.taa_prev) {
        const float dx = in.camera[0] - s.camera_prev[0], dy = in.camera[1] - s.camera_prev[1], dz = in.camera[2] - s.camera_prev[2];
        d.taa_jump = std::sqrt(dx * dx + dy * dy + dz * dz);
    }
    s.taa_prev = known;
    if (known) memcpy(s.camera_prev, in.camera, sizeof(s.camera_prev));
    d.blank_run = sample ? s.blank : s.blank + 1;
    s.blank = sample ? 0 : s.blank + 1;
    // Once per blank run, or at a camera jump. The second does not exist with anti-aliasing off, nor for a move with a
    // camera at the origin on either side of it: only a blank run then marks the load.
    if ((!sample && s.blank == std::max(r.load_frames, 1)) || d.taa_jump > r.warp_distance) {
        d.load = true;
        d.changed = s.target != -1;
        d.snap = true;
        s.target = -1;
        s.held = -1;
        s.hold_zero = 0;
        s.candidate = -2;
        s.run = 0;
        s.window_armed = true;
        s.window_open = false;
    }
    if (sample && s.window_armed) {
        s.window_armed = false;
        s.window_open = true;
        s.window_start = in.now;
    }
    if (s.window_open && in.now - s.window_start >= r.snap_seconds) s.window_open = false;

    int cand = -1;
    if (sample) {
        d.group = group;
        d.mixed = mixed;
        if (mixed) {
            // Pieces of several archives in view, each reading the collision's ShadowBank row from its own DrawParam,
            // and none clearly the frame's: the globals, with L and its count of frames without its own draws as they are.
            d.why = "mixed";
        } else {
            cand = candidate_of(table, r, s, d, in.votes);
        }
    } else {
        d.group = -2;
        d.why = "blank";
    }
    if (d.load) d.why = "load";
    d.candidate = cand;

    // Step 4. Blank frames confirm the globals like any other candidate; a change they confirm alone snaps.
    if (cand == s.candidate) {
        s.run++;
    } else {
        s.candidate = cand;
        s.run = 1;
        s.run_blank = true;
    }
    if (sample) s.run_blank = false;
    const int confirm = std::max(r.confirm_frames, 1);
    const int need = s.window_open ? std::min(confirm, 2) : confirm;
    if (cand != s.target && s.run >= need) {
        // Step 5.
        d.snap = d.snap || s.run_blank || s.window_open;
        d.changed = true;
        s.target = cand;
    }
    return d;
}

//------------------------------------------------------------------------------
// The blend
//------------------------------------------------------------------------------
namespace {

bool same_bits(float a, float b) {
    return memcmp(&a, &b, sizeof(float)) == 0;
}

// The keys engine.cpp's read_pack_values reads; the others are read live where they are used.
bool pack_key(const std::string& key) {
    return key.compare(0, 9, "lighting.") == 0 || key.compare(0, 10, "composite.") == 0 || key.compare(0, 10, "sunshadow.") == 0;
}

void log_snap_once(const std::string& key) {
    static std::unordered_set<std::string> s_logged;
    if (s_logged.insert(key).second) logf("maps: %s has no value in dsrle.ini, so it changes at once instead of easing", key.c_str());
}

}  // namespace

void maps_blend_set_keys(MapsBlend& blend, const std::vector<std::string>& keys) {
    std::vector<MapsBlendKey> next;
    for (const std::string& key : keys) {
        auto it = std::find_if(blend.keys.begin(), blend.keys.end(), [&](const MapsBlendKey& k) { return k.key == key; });
        if (it != blend.keys.end()) {
            next.push_back(*it);
            continue;
        }
        MapsBlendKey k;
        k.key = key;
        k.base_p = config_base_value(key.c_str(), &k.base);
        k.cur_p = k.from_p = k.to_p = k.base_p;
        k.cur = k.from = k.to = k.base;
        next.push_back(k);
    }
    blend.keys.swap(next);
}

void maps_blend_retarget(MapsBlend& blend, const std::string& map, bool snap) {
    blend.target = map;
    blend.t = snap ? 1.0 : 0.0;
    blend.settled = false;
    for (MapsBlendKey& k : blend.keys) {
        // From what is on screen now, mid-ease or not, so no key keeps a value from a map before the last.
        k.from_p = k.cur_p;
        k.from = k.cur;
        k.base_p = config_base_value(k.key.c_str(), &k.base);
        float v;
        if (!map.empty() && config_map_value(map.c_str(), k.key.c_str(), &v)) {
            k.to_p = true;
            k.to = v;
        } else {
            k.to_p = k.base_p;
            k.to = k.base;
        }
        // Without dsrle.ini's value the call site's own fallback is in effect, which config cannot see to ease from.
        k.snap = snap || !k.base_p || !k.from_p || !k.to_p;
        if (!snap && k.snap && !(k.from_p == k.to_p && (!k.to_p || same_bits(k.from, k.to)))) log_snap_once(k.key);
    }
}

bool maps_blend_step(MapsBlend& blend, double dt, double seconds) {
    if (blend.settled) return false;
    blend.t = seconds > 0.0 ? std::min(1.0, blend.t + std::max(dt, 0.0) / seconds) : 1.0;
    const double t = blend.t, s = t * t * (3.0 - 2.0 * t);
    std::vector<OverlayWrite> writes;
    bool pack = false;
    for (MapsBlendKey& k : blend.keys) {
        if (k.snap || t >= 1.0) {
            // The parsed value, bit for bit: lightreach tests reach > 1, and the globals must come back exactly.
            k.cur_p = k.to_p;
            k.cur = k.to;
        } else {
            float v = static_cast<float>(k.from + (static_cast<double>(k.to) - k.from) * s);
            v = std::clamp(v, std::min(k.from, k.to), std::max(k.from, k.to));
            // Only rest reaches the end value, so a test against it changes only when the ease is over.
            if (same_bits(v, k.to) && !same_bits(k.from, k.to)) v = std::nextafter(k.to, k.from);
            k.cur_p = true;
            k.cur = v;
        }
        // dsrle.ini's own value goes back as itself, so at the globals the table is the file's, bit for bit.
        const bool want = k.cur_p && !(k.base_p && same_bits(k.cur, k.base));
        if (want == k.written_p && (!want || same_bits(k.cur, k.written))) continue;
        writes.push_back(OverlayWrite{k.key.c_str(), want, k.cur});
        k.written_p = want;
        k.written = k.cur;
        pack = pack || pack_key(k.key);
    }
    if (!writes.empty()) config_set_overlay(writes.data(), writes.size());
    if (t >= 1.0) blend.settled = true;
    return pack;
}

//------------------------------------------------------------------------------
// The engine's side
//------------------------------------------------------------------------------
namespace {

struct Settings {
    bool enabled = true;
    bool apply = true;
    bool log = false;
    double blend_seconds = 1.5;
    MapsRules rules;
};

constexpr int kMissRecords = 32;
constexpr int32_t kMissUnrecorded = -1 - kMissRecords;

struct MissRecord {
    float c5[3];
    float c6[3];
    std::atomic<uint32_t> draws{0};
    bool logged = false;  // render thread
};

struct DirMiss {
    float c73[3];
    std::atomic<uint32_t> draws{0};
    bool logged = false;  // render thread
};

MapsTable g_table;
bool g_have_table;
std::unique_ptr<std::atomic<uint32_t>[]> g_votes;   // per tuple, for the frame being recorded
std::unique_ptr<std::atomic<uint32_t>[]> g_groups;  // per group, the same
std::atomic<uint32_t> g_di{0}, g_vi{0}, g_miss{0}, g_fwd{0}, g_c73_miss{0};
std::atomic<uint32_t> g_frame{1};
std::atomic<bool> g_votes_on{false};
std::atomic<int> g_hash{0};  // 0 still hashing, 1 the installed DrawParam is the table's, 2 it is not
std::string g_hash_bad;      // written by the hash thread before g_hash
std::mutex g_miss_lock;
MissRecord g_misses[kMissRecords];
std::atomic<int> g_miss_count{0};
DirMiss g_dir_misses[kMissRecords];
std::atomic<int> g_dir_miss_count{0};

// Render thread only.
Settings g_set;
bool g_dp_ok;
bool g_table_logged;
MapsState g_state;
MapsBlend g_blend;
std::vector<uint32_t> g_frame_votes;
std::vector<uint32_t> g_frame_groups;
LARGE_INTEGER g_freq, g_last;

DWORD WINAPI hash_thread(void*) {
    std::string bad;
    const bool ok = maps_check_hashes(g_table, game_dir() + L"param\\DrawParam\\", &bad);
    g_hash_bad = bad;
    g_hash.store(ok ? 1 : 2, std::memory_order_release);
    return 0;
}

void read_maps_settings() {
    g_set.enabled = cfg_bool("maps.enabled", true);
    g_set.apply = cfg_bool("maps.apply", true);
    g_set.log = cfg_bool("maps.log", false);
    g_set.blend_seconds = cfg_float("maps.blend_seconds", 1.5f);
    MapsRules& r = g_set.rules;
    r.min_votes = std::max(cfg_int("maps.min_votes", 3), 1);
    r.dominance = cfg_float("maps.dominance", 4.0f);
    r.walk_share = cfg_float("maps.walk_share", 0.25f);
    r.confirm_frames = std::max(cfg_int("maps.confirm_frames", 6), 1);
    r.load_frames = std::max(cfg_int("maps.load_frames", 30), 1);
    r.snap_seconds = cfg_float("maps.snap_seconds", 2.0f);
    r.c73_share = std::clamp(cfg_float("maps.c73_share", 0.75f), 0.0f, 1.0f);
    r.hold_frames = std::max(cfg_int("maps.hold_frames", 900), 0);
    r.warp_distance = cfg_float("casters.warp_distance", 200.0f);
}

// A section for a map the table cannot tell apart is never used, which is worth saying once.
void check_sections() {
    if (!g_have_table) return;
    static std::unordered_set<std::string> s_logged;
    const ConfigMapSections sections = config_map_sections();
    for (const std::vector<std::string>* ids : {&sections.shipped, &sections.user}) {
        for (const std::string& id : *ids) {
            auto it = std::find(g_table.maps.begin(), g_table.maps.end(), id);
            const bool usable = it != g_table.maps.end() && g_table.has_values[static_cast<size_t>(it - g_table.maps.begin())];
            if (!usable && s_logged.insert(id).second) logf("maps: [map.%s] is not a map the table can find, so its values are never used", id.c_str());
        }
    }
}

void appendf(std::string& s, const char* fmt, ...) {
    char buf[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    s += buf;
}

const char* map_id(int map) {
    return map >= 0 ? g_table.maps[static_cast<size_t>(map)].c_str() : "global";
}

// X and Y as the DrawParam gives them: c73 = (cos X sin Y, -sin X, cos X cos Y).
void angles(const float* v, double* x, double* y) {
    // Adding 0 turns -0 into 0, which would otherwise print as -0.0.
    *x = std::asin(std::clamp(-static_cast<double>(v[1]), -1.0, 1.0)) * 180.0 / kPi + 0.0;
    *y = std::atan2(static_cast<double>(v[0]), static_cast<double>(v[2])) * 180.0 / kPi + 0.0;
}

void log_status(uint32_t frame, const MapsPresent& p, const MapsDecision& d, uint32_t di, uint32_t vi, uint32_t miss, uint32_t fwd,
                uint32_t c73_miss) {
    std::string s;
    appendf(s, "maps: f=%u", frame);
    // The four groups the most draws named by their c73, the lower index first on a tie.
    std::vector<size_t> named;
    for (size_t g = 0; g < g_frame_groups.size(); g++) {
        if (g_frame_groups[g]) named.push_back(g);
    }
    std::stable_sort(named.begin(), named.end(), [](size_t a, size_t b) { return g_frame_groups[a] > g_frame_groups[b]; });
    std::string c73s;
    for (size_t i = 0; i < named.size() && i < 4; i++) appendf(c73s, "%s%zu:%u", i ? "," : "", named[i], g_frame_groups[named[i]]);
    appendf(s, " c73s=%s c73miss=%u", c73s.empty() ? "-" : c73s.c_str(), c73_miss);
    if (d.group >= 0) {
        appendf(s, " grp=%d", d.group);
    } else {
        s += d.group == -1 ? " grp=unknown" : " grp=blank";
    }
    std::string ids, pos, walk, sep;
    if (d.group >= 0) {
        const MapsGroup& g = g_table.groups[static_cast<size_t>(d.group)];
        for (size_t k = 0; k < g.claimants.size(); k++) {
            const char* id = map_id(g.claimants[k]);
            appendf(ids, "%s%s", ids.empty() ? "" : ",", id);
            if (g.ev.empty()) continue;
            const MapsEvidence& ev = g.ev[k];
            appendf(pos, "%s%s:%u", pos.empty() ? "" : ",", id, count(ev.pos, g_frame_votes.data()));
            if (!g_table.has_values[static_cast<size_t>(g.claimants[k])] || g.no_evidence[k]) continue;
            appendf(walk, "%s%s:%u", walk.empty() ? "" : ",", id, count(ev.walk, g_frame_votes.data()));
            appendf(sep, "%s%s:%u", sep.empty() ? "" : ",", id, count(ev.sep, g_frame_votes.data()));
        }
    }
    uint32_t votes = 0;
    for (uint32_t n : g_frame_votes) votes += n;
    appendf(s, " S=%s L=%s pos=%s walk=%s sep=%s", ids.empty() ? "-" : ids.c_str(), g_state.held >= 0 ? map_id(g_state.held) : "-",
            pos.empty() ? "-" : pos.c_str(), walk.empty() ? "-" : walk.c_str(), sep.empty() ? "-" : sep.c_str());
    appendf(s, " votes=%u di=%u vi=%u miss=%u blank=%d cand=%s tgt=%s why=%s taa=%d", votes, di, vi, miss, g_state.blank,
            map_id(d.candidate), map_id(g_state.target), d.why, p.taa_fresh ? 1 : 0);
    if (p.taa_fresh) {
        appendf(s, " cam=%.1f,%.1f,%.1f", p.camera[0], p.camera[1], p.camera[2]);
    } else {
        s += " cam=-";
    }
    appendf(s, " fwd=%u voted=%u", fwd, di + vi);
    logf("%s", s.c_str());
}

void log_misses() {
    const int n = g_miss_count.load(std::memory_order_acquire);
    for (int i = 0; i < n; i++) {
        MissRecord& m = g_misses[i];
        if (m.logged) continue;
        m.logged = true;
        logf("maps: miss c5=%.6f,%.6f,%.6f c6=%.6f,%.6f,%.6f n=%u", m.c5[0], m.c5[1], m.c5[2], m.c6[0], m.c6[1], m.c6[2],
             m.draws.load(std::memory_order_relaxed));
    }
    const int n_dir = g_dir_miss_count.load(std::memory_order_acquire);
    for (int i = 0; i < n_dir; i++) {
        DirMiss& m = g_dir_misses[i];
        if (m.logged) continue;
        m.logged = true;
        double x, y;
        angles(m.c73, &x, &y);
        logf("maps: dir-miss c73=%.6f,%.6f,%.6f ang=%.1f,%.1f n=%u", m.c73[0], m.c73[1], m.c73[2], x, y,
             m.draws.load(std::memory_order_relaxed));
    }
}

// The session script writes a label to dsrle\trigger_mark, read and deleted like trigger_shot in trace.cpp.
bool poll_mark(std::string* label) {
    static const std::wstring trigger = data_dir() + L"trigger_mark";
    if (GetFileAttributesW(trigger.c_str()) == INVALID_FILE_ATTRIBUTES) return false;
    wchar_t text[256] = {};
    FILE* f = nullptr;
    _wfopen_s(&f, trigger.c_str(), L"rt, ccs=UTF-8");
    if (f) {
        fgetws(text, 256, f);
        fclose(f);
    }
    DeleteFileW(trigger.c_str());
    size_t n = wcslen(text);
    while (n && (text[n - 1] == L'\n' || text[n - 1] == L'\r' || text[n - 1] == L' ')) text[--n] = 0;
    char utf8[512] = {};
    WideCharToMultiByte(CP_UTF8, 0, text, -1, utf8, sizeof(utf8), nullptr, nullptr);
    *label = n ? utf8 : "-";
    return true;
}

int32_t record_miss(const float* c5, const float* c6) {
    std::lock_guard<std::mutex> lock(g_miss_lock);
    const int n = g_miss_count.load(std::memory_order_relaxed);
    for (int i = 0; i < n; i++) {
        if (memcmp(g_misses[i].c5, c5, sizeof(g_misses[i].c5)) == 0 && memcmp(g_misses[i].c6, c6, sizeof(g_misses[i].c6)) == 0) return -1 - i;
    }
    if (n >= kMissRecords) return kMissUnrecorded;
    memcpy(g_misses[n].c5, c5, sizeof(g_misses[n].c5));
    memcpy(g_misses[n].c6, c6, sizeof(g_misses[n].c6));
    g_miss_count.store(n + 1, std::memory_order_release);
    return -1 - n;
}

int32_t record_dir_miss(const float* c73) {
    std::lock_guard<std::mutex> lock(g_miss_lock);
    const int n = g_dir_miss_count.load(std::memory_order_relaxed);
    for (int i = 0; i < n; i++) {
        // The same bits as well: a vector that is no direction, zero say, is within no angle of itself.
        const float* m = g_dir_misses[i].c73;
        if (memcmp(m, c73, sizeof(g_dir_misses[i].c73)) == 0 || dot3(m, c73) >= g_table.cos_tol) return -1 - i;
    }
    if (n >= kMissRecords) return kMissUnrecorded;
    memcpy(g_dir_misses[n].c73, c73, sizeof(g_dir_misses[n].c73));
    g_dir_miss_count.store(n + 1, std::memory_order_release);
    return -1 - n;
}

void lookup(MapsVoteCache& cache, const float* regs, MapsVoteResult& out) {
    uint32_t bits[9];
    memcpy(bits, regs, 3 * sizeof(float));
    memcpy(bits + 3, regs + 4, 3 * sizeof(float));
    memcpy(bits + 6, regs + 8, 3 * sizeof(float));
    MapsVoteCache::Entry& e = cache.entries[fnv1a64(bits, sizeof(bits)) & 63];
    if (!e.used || memcmp(e.bits, bits, sizeof(bits)) != 0) {
        e.tuple = maps_match_tuple(g_table, regs, regs + 4);
        if (e.tuple < 0) e.tuple = record_miss(regs, regs + 4);
        e.group = maps_match_group(g_table, regs + 8);
        if (e.group < 0) e.group = record_dir_miss(regs + 8);
        memcpy(e.bits, bits, sizeof(bits));
        e.used = true;
    }
    out.tuple = e.tuple;
    out.group = e.group;
}

}  // namespace

void maps_init() {
    QueryPerformanceFrequency(&g_freq);
    read_maps_settings();
    maps_blend_set_keys(g_blend, config_map_keys());
    std::string text;
    FILE* f = nullptr;
    _wfopen_s(&f, (data_dir() + L"maps_detect.txt").c_str(), L"rb");
    if (!f) {
        logf("maps: no table (dsrle\\maps_detect.txt not found), per-map settings off");
        return;
    }
    char buf[4096];
    size_t got;
    while ((got = fread(buf, 1, sizeof(buf), f)) > 0) text.append(buf, got);
    fclose(f);
    std::string error;
    if (!maps_parse_table(text.data(), text.size(), &g_table, &error)) {
        g_table = MapsTable{};
        logf("maps: no table (maps_detect.txt %s), per-map settings off", error.c_str());
        return;
    }
    g_votes.reset(new std::atomic<uint32_t>[g_table.tuples.size()]());
    g_groups.reset(new std::atomic<uint32_t>[g_table.groups.size()]());
    g_have_table = true;
    check_sections();
    // Nine files of a few hundred kilobytes: hashed off the render thread, with the globals in force until it is done.
    HANDLE thread = CreateThread(nullptr, 0, hash_thread, nullptr, 0, nullptr);
    if (thread) {
        CloseHandle(thread);
    } else {
        g_hash_bad = "the hash thread did not start";
        g_hash.store(2, std::memory_order_release);
    }
}

bool maps_wants_votes() {
    return g_votes_on.load(std::memory_order_acquire);
}

uint32_t maps_frame() {
    return g_frame.load(std::memory_order_relaxed);
}

void maps_note_forward_draw() {
    g_fwd.fetch_add(1, std::memory_order_relaxed);
}

void maps_vote(MapsVoteCache& cache, const float* regs, MapsVoteResult& result, bool instanced) {
    if (result.tuple == kMapsUnresolved) lookup(cache, regs, result);
    (instanced ? g_vi : g_di).fetch_add(1, std::memory_order_relaxed);
    if (result.group >= 0) {
        g_groups[static_cast<size_t>(result.group)].fetch_add(1, std::memory_order_relaxed);
    } else {
        g_c73_miss.fetch_add(1, std::memory_order_relaxed);
        if (result.group != kMissUnrecorded) g_dir_misses[-1 - result.group].draws.fetch_add(1, std::memory_order_relaxed);
    }
    if (result.tuple >= 0) {
        g_votes[static_cast<size_t>(result.tuple)].fetch_add(1, std::memory_order_relaxed);
        return;
    }
    g_miss.fetch_add(1, std::memory_order_relaxed);
    if (result.tuple != kMissUnrecorded) g_misses[-1 - result.tuple].draws.fetch_add(1, std::memory_order_relaxed);
}

bool maps_on_present(const MapsPresent& p, bool reloaded) {
    // Copies stamped from here on belong to the next frame.
    const uint32_t frame = g_frame.fetch_add(1, std::memory_order_relaxed);
    g_frame_votes.assign(g_table.tuples.size(), 0);
    for (size_t i = 0; i < g_frame_votes.size(); i++) g_frame_votes[i] = g_votes[i].exchange(0, std::memory_order_relaxed);
    g_frame_groups.assign(g_table.groups.size(), 0);
    for (size_t i = 0; i < g_frame_groups.size(); i++) g_frame_groups[i] = g_groups[i].exchange(0, std::memory_order_relaxed);
    const uint32_t di = g_di.exchange(0, std::memory_order_relaxed), vi = g_vi.exchange(0, std::memory_order_relaxed);
    const uint32_t miss = g_miss.exchange(0, std::memory_order_relaxed), fwd = g_fwd.exchange(0, std::memory_order_relaxed);
    const uint32_t c73_miss = g_c73_miss.exchange(0, std::memory_order_relaxed);
    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);
    const double dt = g_last.QuadPart ? std::clamp(static_cast<double>(now.QuadPart - g_last.QuadPart) / static_cast<double>(g_freq.QuadPart), 0.0, 0.1) : 0.0;
    g_last = now;
    if (reloaded) {
        read_maps_settings();
        check_sections();
    }

    if (g_have_table && !g_table_logged) {
        const int hash = g_hash.load(std::memory_order_acquire);
        if (hash != 0) {
            g_table_logged = true;
            g_dp_ok = hash == 1;
            const ConfigMapSections sections = config_map_sections();
            logf("maps: table maps=%zu groups=%zu tuples=%zu dp=%s maps_ini=%zu user=%zu%s%s%s", g_table.maps.size(), g_table.groups.size(),
                 g_table.tuples.size(), g_dp_ok ? "match" : "mismatch", sections.shipped.size(), sections.user.size(),
                 g_dp_ok ? "" : " (", g_dp_ok ? "" : g_hash_bad.c_str(), g_dp_ok ? "" : "), per-map settings off");
        }
    }
    const bool on = g_have_table && g_dp_ok && g_set.enabled;
    g_votes_on.store(on, std::memory_order_release);

    std::string mark;
    const bool marked = frame % 4 == 0 && poll_mark(&mark);

    MapsDecision d;
    if (on) {
        MapsFrame in;
        in.groups = g_frame_groups.data();
        in.c73_miss = c73_miss;
        in.taa_fresh = p.taa_fresh;
        memcpy(in.camera, p.camera, sizeof(in.camera));
        in.votes = g_frame_votes.data();
        in.now = static_cast<double>(now.QuadPart) / static_cast<double>(g_freq.QuadPart);
        d = maps_resolve(g_table, g_set.rules, g_state, in);
    } else {
        // Detection starts afresh when it comes back on, and its first map switches in at once, as after a load. No
        // draw votes meanwhile, so there is no group to show.
        d.changed = g_state.target != -1;
        d.snap = true;
        g_state = MapsState{};
        g_state.window_armed = true;
        d.group = -2;
        d.why = "off";
    }

    // A reload eases nothing: an edit shows at once. A key no section names any more is written back to dsrle.ini's value
    // with the rest before it is dropped.
    const int applied = on && g_set.apply ? g_state.target : -1;
    const std::string target = applied >= 0 ? g_table.maps[static_cast<size_t>(applied)] : std::string();
    std::vector<std::string> keys;
    if (reloaded) {
        keys = config_map_keys();
        std::vector<std::string> both = keys;
        for (const MapsBlendKey& k : g_blend.keys) both.push_back(k.key);
        std::sort(both.begin(), both.end());
        both.erase(std::unique(both.begin(), both.end()), both.end());
        maps_blend_set_keys(g_blend, both);
    }
    if (reloaded || d.load || target != g_blend.target) maps_blend_retarget(g_blend, target, reloaded || d.snap);
    const bool pack = maps_blend_step(g_blend, dt, g_set.blend_seconds);
    if (reloaded) maps_blend_set_keys(g_blend, keys);

    if (d.load) {
        if (d.taa_jump >= 0.0f) {
            logf("maps: load f=%u blank=%d taa_jump=%.1f", frame, d.blank_run, d.taa_jump);
        } else {
            logf("maps: load f=%u blank=%d taa_jump=-", frame, d.blank_run);
        }
    }
    if (d.expired) logf("maps: f=%u %s let go after %d frames with none of its own draws", frame, map_id(d.released), g_set.rules.hold_frames);
    const bool tick = g_set.log && frame % 60 == 0;
    if (tick) log_misses();
    if (marked) logf("maps: mark %s f=%u", mark.c_str(), frame);
    if (tick || d.changed || marked) log_status(frame, p, d, di, vi, miss, fwd, c73_miss);
    return pack;
}
