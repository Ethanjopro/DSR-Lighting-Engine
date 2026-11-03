// Offline test of the per-map settings: the detection table's reader, the resolver, the blend and config's layering, on
// a synthetic table whose maps, directions and light rows are all invented. run_maps_test.cmd builds and runs it.
//   maps_test.exe                   the cases; exit code 1 on any failure
//   maps_test.exe <table> [<dir>]   reads a real table and prints its counts; with dir, the folder holding the
//                                   aNN_DrawParam.parambnd.dcx files, also whether their hashes are the table's
#include "../src/common.h"
#include "../src/config.h"
#include "../src/maps.h"
#include <cmath>
#include <cstdio>
#include <cstring>
#include <initializer_list>
#include <share.h>
#include <string>
#include <utility>
#include <vector>

namespace {

int g_checks, g_failed;

void check(bool ok, const char* what, int line) {
    g_checks++;
    if (ok) return;
    g_failed++;
    printf("FAIL line %d: %s\n", line, what);
}
#define CHECK(x) check((x), #x, __LINE__)

bool same(float a, float b) {
    return memcmp(&a, &b, sizeof(float)) == 0;
}

// sha256("abc"), the FIPS 180-2 example, is each area's hash here: the glue test writes "abc" as every DrawParam file.
#define ABC "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"

// Maps: 0 m01_00 (values; unique in group 0; shares group 2 with walk rival m02_00), 1 m02_00 (no values),
// 2 m03_00 (values; shares group 3 with m04_00, which is behind a load), 3 m04_00, 4 m05_00 (values),
// 5 m06_00 (a claimant of group 3 with no evidence), 6 m09_00 (values in the table but no section in either file).
// Group 4 is global_only. Tuples: 0 m01_00's, 1 m02_00's, 2 m03_00's, 3 m04_00's, 4 shared by most, 5 m05_00's.
const char kTable[] =
    "dsrle_maps_detect 1\n"
    "dp a10 " ABC "\n"
    "dp a11 " ABC "\n"
    "dp a12 " ABC "\n"
    "dp a13 " ABC "\n"
    "dp a14 " ABC "\n"
    "dp a15 " ABC "\n"
    "dp a16 " ABC "\n"
    "dp a17 " ABC "\n"
    "dp a18 " ABC "\n"
    "tol 0.500000000 0.002000000 0.000001000\n"
    "counts 7 6 7 8 7\n"
    "map 0 m01_00 1\n"
    "map 1 m02_00 0\n"
    "map 2 m03_00 1\n"
    "map 3 m04_00 0\n"
    "map 4 m05_00 1\n"
    "map 5 m06_00 0\n"
    "map 6 m09_00 1\n"
    "tuple 0 0.000000000 0.000000000 1.000000000 0.100000000 0.200000000 0.300000000 0\n"
    "tuple 1 0.000000000 0.000000000 1.000000000 0.400000000 0.500000000 0.600000000 1\n"
    "tuple 2 0.000000000 0.000000000 1.000000000 0.700000000 0.800000000 0.900000000 2\n"
    "tuple 3 0.000000000 0.000000000 1.000000000 1.000000000 1.100000000 1.200000000 3\n"
    "tuple 4 0.000000000 0.000000000 1.000000000 0.000000000 0.000000000 0.000000000 0,1,2,3,4\n"
    "tuple 5 1.000000000 0.000000000 0.000000000 0.500000000 0.500000000 0.500000000 4\n"
    "group 0 0 0 -\n"
    "group 1 0 1 -\n"
    "group 2 0 0,1 -\n"
    "group 3 0 2,3,5 5\n"
    "group 4 1 0,4 -\n"
    "group 5 0 4 -\n"
    "group 6 0 6 -\n"
    "dir 0 0.000000000 -1.000000000 0.000000000\n"
    "dir 1 1.000000000 0.000000000 0.000000000\n"
    "dir 2 0.000000000 0.000000000 1.000000000\n"
    "dir 2 0.600000000 0.000000000 0.800000000\n"
    "dir 3 0.000000000 1.000000000 0.000000000\n"
    "dir 4 -1.000000000 0.000000000 0.000000000\n"
    "dir 5 0.000000000 0.000000000 -1.000000000\n"
    "dir 6 0.800000000 0.600000000 0.000000000\n"
    "ev 2 0 0 0 1 -\n"
    "ev 2 1 0 1 - -\n"
    "ev 3 2 1 2 - 3\n"
    "ev 3 3 0 3 - -\n"
    "ev 3 5 0 - - -\n"
    "ev 4 0 0 0 - 5\n"
    "ev 4 4 0 5 - 0\n"
    "end\n";

enum { kA, kB, kC, kD, kE, kF, kN };
enum { kTA, kTB, kTC, kTD, kTShared, kTE };
const float kDirA[3] = {0.0f, -1.0f, 0.0f};    // group 0
const float kDirB[3] = {1.0f, 0.0f, 0.0f};     // group 1
const float kDirAB[3] = {0.0f, 0.0f, 1.0f};    // group 2
const float kDirAB2[3] = {0.6f, 0.0f, 0.8f};   // group 2's second direction
const float kDirCD[3] = {0.0f, 1.0f, 0.0f};    // group 3
const float kDirGlobal[3] = {-1.0f, 0.0f, 0.0f};  // group 4, global_only
const float kDirE[3] = {0.0f, 0.0f, -1.0f};    // group 5
const float kDirN[3] = {0.8f, 0.6f, 0.0f};     // group 6
const float kDirUnknown[3] = {0.6f, 0.8f, 0.0f};

std::string replaced(std::string s, const std::string& from, const std::string& to) {
    const size_t at = s.find(from);
    CHECK(at != std::string::npos);
    if (at != std::string::npos) s.replace(at, from.size(), to);
    return s;
}

bool parses(const std::string& text) {
    MapsTable t;
    std::string error;
    return maps_parse_table(text.data(), text.size(), &t, &error);
}

void write_text(const std::wstring& path, const std::string& text) {
    FILE* f = nullptr;
    _wfopen_s(&f, path.c_str(), L"wb");
    CHECK(f != nullptr);
    if (!f) return;
    fwrite(text.data(), 1, text.size(), f);
    fclose(f);
}

std::string read_log() {
    std::string text;
    FILE* f = _wfsopen((data_dir() + L"dsrle.log").c_str(), L"rb", _SH_DENYNO);
    if (!f) return text;
    char buf[4096];
    size_t got;
    while ((got = fread(buf, 1, sizeof(buf), f)) > 0) text.append(buf, got);
    fclose(f);
    return text;
}

size_t occurrences(const std::string& text, const char* what) {
    size_t n = 0;
    for (size_t at = text.find(what); at != std::string::npos; at = text.find(what, at + 1)) n++;
    return n;
}

//------------------------------------------------------------------------------
// The reader and the matching
//------------------------------------------------------------------------------
void test_parse() {
    const std::string good = kTable;
    MapsTable t;
    std::string error;
    CHECK(maps_parse_table(good.data(), good.size(), &t, &error));
    CHECK(t.maps.size() == 7 && t.tuples.size() == 6 && t.groups.size() == 7 && t.dir_count == 8 && t.ev_count == 7);
    CHECK(t.groups[4].global_only && !t.groups[3].global_only);
    CHECK(t.groups[3].no_evidence.size() == 3 && t.groups[3].no_evidence[2] && !t.groups[3].no_evidence[0]);
    CHECK(t.groups[0].ev.empty() && t.groups[2].ev.size() == 2 && t.groups[3].ev[0].hold);
    CHECK(t.groups[2].dirs.size() == 2 && t.has_values[kA] && !t.has_values[kB]);
    CHECK(t.tuples[kTShared].owners.size() == 5 && same(t.tuples[kTA].c6[0], 0.1f));
    CHECK(std::fabs(t.cos_tol - std::cos(0.5 * 3.14159265358979323846 / 180.0)) < 1e-15);

    // Each of these breaks one rule of the format, and parse_detect refuses each.
    std::string crlf = good;
    for (size_t at = crlf.find('\n'); at != std::string::npos; at = crlf.find('\n', at + 2)) crlf.replace(at, 1, "\r\n");
    CHECK(!parses(crlf));
    CHECK(!parses(replaced(good, "end\n", "")));
    CHECK(!parses(good + "x\n"));
    CHECK(!parses(good.substr(0, good.size() - 1)));
    CHECK(!parses(replaced(good, "dsrle_maps_detect 1", "dsrle_maps_detect 2")));
    CHECK(!parses(replaced(good, "dp a10 ba7816bf", "dp a10 BA7816BF")));
    CHECK(!parses(replaced(replaced(good, "dp a10", "dp aXX"), "dp a11", "dp a10")));
    CHECK(!parses(replaced(good, "tuple 0 0.000000000", "tuple 0  0.000000000")));
    CHECK(!parses(replaced(good, "tuple 0 0.000000000", "tuple 0 -0.000000000")));
    CHECK(!parses(replaced(good, "0.100000000", "0.10000000")));
    CHECK(!parses(replaced(good, "0.100000000", "1.000000000e-1")));
    CHECK(!parses(replaced(good, "map 1 m02_00 0", "map 01 m02_00 0")));
    CHECK(!parses(replaced(good, "map 1 m02_00 0", "map 1 m02_00 2")));
    CHECK(!parses(replaced(good, "map 1 m02_00", "map 1 m02_0\xe9")));
    CHECK(!parses(replaced(good, "group 2 0 0,1 -", "group 2 0 1,0 -")));
    CHECK(!parses(replaced(good, "group 3 0 2,3,5 5", "group 3 0 2,3,5 6")));
    CHECK(!parses(replaced(good, "0,1,2,3,4\n", "0,1,2,3,7\n")));
    CHECK(!parses(replaced(replaced(good, "dir 0 0.000000000 -1", "dir 9 0.000000000 -1"), "dir 1 1", "dir 0 1")));
    CHECK(!parses(replaced(good, "dir 6 0.8", "dir 5 0.8")));
    CHECK(!parses(replaced(replaced(good, "counts 7 6 7 8 7", "counts 7 6 7 8 6"), "ev 2 1 0 1 - -\n", "")));
    CHECK(!parses(replaced(replaced(good, "counts 7 6 7 8 7", "counts 7 6 7 8 8"), "ev 2 0 0", "ev 0 0 0 0 - -\nev 2 0 0")));
    CHECK(!parses(replaced(replaced(good, "ev 2 0 0 0 1 -\n", ""), "ev 2 1 0 1 - -\n", "ev 2 1 0 1 - -\nev 2 0 0 0 1 -\n")));
    CHECK(!parses(replaced(good, "ev 3 3 0 3 - -", "ev 3 4 0 3 - -")));
    CHECK(!parses(replaced(good, "ev 2 0 0 0 1 -", "ev 2 0 0 0 9 -")));
    CHECK(!parses(replaced(good, "counts 7 6 7 8 7", "counts 7 6 7 8 7 0")));
}

void test_match(const MapsTable& t) {
    CHECK(maps_match_group(t, kDirA) == 0);
    CHECK(maps_match_group(t, kDirAB2) == 2);
    CHECK(maps_match_group(t, kDirUnknown) == -1);
    const double in = 0.4 * 3.14159265358979323846 / 180.0, out = 0.6 * 3.14159265358979323846 / 180.0;
    const float inside[3] = {static_cast<float>(std::sin(in)), 0.0f, static_cast<float>(std::cos(in))};
    const float outside[3] = {static_cast<float>(std::sin(out)), 0.0f, static_cast<float>(std::cos(out))};
    CHECK(maps_match_group(t, inside) == 2);
    CHECK(maps_match_group(t, outside) == -1);
    // Along a direction but too long or too short is none.
    const float near_unit[3] = {0.0f, 0.0f, 1.05f}, too_long[3] = {0.0f, 0.0f, 1.2f}, too_short[3] = {0.0f, 0.0f, 0.85f};
    CHECK(maps_match_group(t, near_unit) == 2 && maps_match_group(t, too_long) == -1 && maps_match_group(t, too_short) == -1);
    const float c6_in[3] = {0.1f * 1.0015f, 0.2f, 0.3f}, c6_out[3] = {0.1f * 1.003f, 0.2f, 0.3f};
    CHECK(maps_match_tuple(t, kDirAB, c6_in) == kTA);
    CHECK(maps_match_tuple(t, kDirAB, c6_out) == -1);
    CHECK(maps_match_tuple(t, outside, t.tuples[kTA].c6) == -1);
    const float off[3] = {1e-7f, 0.0f, 0.0f};
    CHECK(maps_match_tuple(t, kDirAB, off) == kTShared);
}

//------------------------------------------------------------------------------
// The resolver
//------------------------------------------------------------------------------
struct Sim {
    const MapsTable& t;
    MapsRules r;
    MapsState s;
    double now = 100.0;
    std::vector<uint32_t> votes, groups;
    explicit Sim(const MapsTable& table) : t(table), votes(table.tuples.size()), groups(table.groups.size()) {}

    // A frame whose pieces all come from one archive: every draw, one per light-row vote and at least one, has c73 as
    // its sun direction. Not fresh: no voting draw at all.
    MapsDecision frame(const float* c73, const std::vector<std::pair<int, uint32_t>>& v = {}, bool fresh = true,
                       bool taa = false, const float* camera = nullptr) {
        std::vector<std::pair<int, uint32_t>> named;
        uint32_t miss = 0;
        if (fresh) {
            uint32_t n = 0;
            for (const auto& p : v) n += p.second;
            if (n == 0) n = 1;
            const int g = maps_match_group(t, c73);
            if (g >= 0) {
                named.push_back({g, n});
            } else {
                miss = n;
            }
        }
        return tally(named, miss, v, taa, camera);
    }
    // The draws per group their c73 is in, and those in none, as given.
    MapsDecision tally(const std::vector<std::pair<int, uint32_t>>& named, uint32_t miss, const std::vector<std::pair<int, uint32_t>>& v = {},
                       bool taa = false, const float* camera = nullptr) {
        std::fill(votes.begin(), votes.end(), 0u);
        for (const auto& p : v) votes[static_cast<size_t>(p.first)] = p.second;
        std::fill(groups.begin(), groups.end(), 0u);
        for (const auto& p : named) groups[static_cast<size_t>(p.first)] += p.second;
        MapsFrame f;
        f.groups = groups.data();
        f.c73_miss = miss;
        f.taa_fresh = taa;
        if (camera) memcpy(f.camera, camera, sizeof(f.camera));
        f.votes = votes.data();
        f.now = now;
        now += 1.0 / 60.0;
        return maps_resolve(t, r, s, f);
    }
    MapsDecision blank() {
        static const float zero[3] = {};
        return frame(zero, {}, false);
    }
    void reach(const float* c73, int target) {
        for (int i = 0; i < 6 && s.target != target; i++) frame(c73);
    }
};

void test_unique(const MapsTable& t) {
    Sim m(t);
    for (int i = 0; i < 5; i++) {
        const MapsDecision d = m.frame(kDirA);
        CHECK(!d.changed && m.s.target == -1 && d.candidate == kA && d.group == 0);
    }
    MapsDecision d = m.frame(kDirA);
    CHECK(d.changed && !d.snap && m.s.target == kA && m.s.held == kA && strcmp(d.why, "unique") == 0);
    // A map with no values of its own: the globals, but it is where the player is.
    for (int i = 0; i < 6; i++) d = m.frame(kDirB);
    CHECK(d.changed && m.s.target == -1 && m.s.held == kB && d.candidate == -1);
}

void test_walk_group(const MapsTable& t) {
    Sim m(t);
    m.reach(kDirA, kA);
    // One draw only the walk rival carries rejects m01_00, however much of its own is drawn.
    MapsDecision d;
    for (int i = 0; i < 6; i++) {
        d = m.frame(kDirAB, {{kTA, 100}, {kTB, 1}});
        CHECK(d.candidate == -1);
    }
    CHECK(m.s.target == -1 && m.s.held == kA);
    // No hold where the walk rival could show the group without a WALK draw (hold 0), L or not.
    d = m.frame(kDirAB, {{kTShared, 20}});
    CHECK(d.candidate == -1 && strcmp(d.why, "none") == 0);
    // walk_share: 3 of 23 is too few, 6 of 24 is enough.
    Sim w(t);
    CHECK(w.frame(kDirAB, {{kTA, 3}, {kTShared, 20}}).candidate == -1);
    d = w.frame(kDirAB, {{kTA, 6}, {kTShared, 18}});
    CHECK(d.candidate == kA && strcmp(d.why, "vote") == 0 && w.s.held == kA);
    // The other direction of the same group.
    CHECK(w.frame(kDirAB2, {{kTA, 6}, {kTShared, 18}}).candidate == kA);
    // As from the Depths into Blighttown: L is m01_00, the straight-down group shows none of its draws, the globals
    // apply and the walk rival, which fills the frame, becomes L.
    Sim b(t);
    b.reach(kDirA, kA);
    d = b.frame(kDirAB, {{kTB, 10}, {kTShared, 5}});
    CHECK(d.candidate == -1 && b.s.held == kB && strcmp(d.why, "vote") == 0);
}

// As at the Darkroot Garden: the walk rival in the group is a cross-archive claimant, which shows the group only in the
// map's own parts and stands there on a body that is WALK evidence, so the table gives hold 1 beside a WALK list. A
// vote then needs no walk_share, a frame with none of the map's own draws holds it, and WALK and SEP still rule.
void test_cross_archive_hold() {
    std::string text = replaced(kTable, "group 2 0 0,1 -", "group 2 0 0,1 1");
    text = replaced(replaced(text, "ev 2 0 0 0 1 -", "ev 2 0 1 0 1 5"), "ev 2 1 0 1 - -", "ev 2 1 0 - - -");
    MapsTable t;
    std::string error;
    CHECK(maps_parse_table(text.data(), text.size(), &t, &error));
    CHECK(t.groups[2].ev[0].hold && t.groups[2].ev[0].walk.size() == 1 && t.groups[2].no_evidence[1]);
    // 15 of 250 is far below walk_share, and finds m01_00.
    Sim m(t);
    MapsDecision d = m.frame(kDirAB, {{kTA, 15}, {kTShared, 235}});
    CHECK(d.candidate == kA && strcmp(d.why, "vote") == 0 && m.s.held == kA);
    // The view turned from its own pieces: none of its draws, and it holds.
    for (int i = 0; i < 6; i++) d = m.frame(kDirAB, {{kTShared, 90}});
    CHECK(d.candidate == kA && strcmp(d.why, "hold") == 0 && m.s.target == kA);
    // Without L the same frame finds nothing.
    Sim f(t);
    d = f.frame(kDirAB, {{kTShared, 90}});
    CHECK(d.candidate == -1 && strcmp(d.why, "none") == 0 && f.s.held == -1);
    // One draw of the walk rival's own, as its body gives wherever its frames show the group: the globals, held or not.
    d = m.frame(kDirAB, {{kTShared, 90}, {kTB, 1}});
    CHECK(d.candidate == -1 && strcmp(d.why, "none") == 0 && m.s.held == kA);
    CHECK(m.frame(kDirAB, {{kTA, 100}, {kTB, 1}}).candidate == -1);
    // A SEP draw ends the hold, and dominance still weighs SEP against a vote: 20 to 5 is enough, 20 to 6 is not.
    CHECK(m.frame(kDirAB, {{kTShared, 90}, {kTE, 1}}).candidate == -1);
    CHECK(m.frame(kDirAB, {{kTA, 20}, {kTShared, 230}, {kTE, 6}}).candidate == -1);
    d = m.frame(kDirAB, {{kTA, 20}, {kTShared, 230}, {kTE, 5}});
    CHECK(d.candidate == kA && strcmp(d.why, "vote") == 0);
}

void test_separated_group(const MapsTable& t) {
    Sim m(t);
    // 3:1 against a rival behind a load is not enough, 4:1 is.
    MapsDecision d = m.frame(kDirCD, {{kTC, 12}, {kTD, 4}});
    CHECK(d.candidate == -1 && m.s.held == -1);
    d = m.frame(kDirCD, {{kTC, 12}, {kTD, 3}});
    CHECK(d.candidate == kC && m.s.held == kC);

    Sim s(t);
    d = s.frame(kDirCD, {{kTShared, 10}});
    CHECK(d.candidate == -1 && strcmp(d.why, "none") == 0 && s.s.held == -1);
    d = s.frame(kDirCD, {{kTC, 5}});
    CHECK(d.candidate == kC && strcmp(d.why, "vote") == 0 && s.s.held == kC);
    for (int i = 0; i < 5; i++) d = s.frame(kDirCD, {{kTShared, 10}});
    CHECK(d.candidate == kC && strcmp(d.why, "hold") == 0 && s.s.target == kC);
    d = s.frame(kDirCD, {{kTD, 1}, {kTShared, 10}});
    CHECK(d.candidate == -1 && s.s.held == kC);
    for (int i = 0; i < 29; i++) CHECK(!s.blank().load);
    d = s.blank();
    CHECK(d.load && d.snap && s.s.held == -1 && s.s.target == -1 && strcmp(d.why, "load") == 0);
    d = s.frame(kDirCD, {{kTShared, 10}});
    CHECK(d.candidate == -1);
    // A map with no values that clearly fills the frame becomes L; the no-evidence claimant never counts.
    d = s.frame(kDirCD, {{kTD, 12}});
    CHECK(d.candidate == -1 && s.s.held == kD);

    // min_votes below 1 counts as 1: a frame with none of a map's own draws never finds it, nor makes a rival L.
    for (int k : {0, -1}) {
        Sim z(t);
        z.r.min_votes = k;
        d = z.frame(kDirCD, {{kTShared, 10}});
        CHECK(d.candidate == -1 && z.s.held == -1);
        CHECK(z.frame(kDirAB).candidate == -1 && z.s.held == -1);
    }
}

void test_unknown(const MapsTable& t) {
    Sim m(t);
    m.reach(kDirA, kA);
    MapsDecision d;
    for (int i = 0; i < 5; i++) {
        d = m.frame(kDirUnknown);
        CHECK(d.group == -1 && d.candidate == -1 && strcmp(d.why, "unknown") == 0 && m.s.target == kA);
    }
    d = m.frame(kDirUnknown);
    CHECK(d.changed && !d.snap && m.s.target == -1 && m.s.held == kA);
}

void test_blank_and_load(const MapsTable& t) {
    Sim m(t);
    m.reach(kDirA, kA);
    MapsDecision d;
    for (int i = 1; i <= 29; i++) {
        d = m.blank();
        CHECK(!d.load);
        // Blank frames confirm the globals like any other candidate, and a change they confirm snaps.
        if (i == 6) CHECK(d.changed && d.snap && m.s.target == -1);
    }
    for (int i = 0; i < 5; i++) CHECK(!m.frame(kDirA).changed);
    CHECK(m.frame(kDirA).changed);

    Sim l(t);
    for (int i = 0; i < 29; i++) l.blank();
    d = l.blank();
    CHECK(d.load && d.blank_run == 30);
    CHECK(!l.frame(kDirA).changed);
    d = l.frame(kDirA);
    CHECK(d.changed && d.snap && l.s.target == kA);
    // The window ends snap_seconds after the first sample; then six frames again, easing.
    for (int i = 0; i < 130; i++) l.frame(kDirA);
    for (int i = 0; i < 5; i++) CHECK(!l.frame(kDirB).changed);
    d = l.frame(kDirB);
    CHECK(d.changed && !d.snap);

    // Not a unit vector, or not a number: a direction in no group. The draw is still a voting draw, so it is no blank.
    Sim b(t);
    const float nan3[3] = {std::nanf(""), 0.0f, 1.0f}, zero3[3] = {}, long3[3] = {0.0f, 0.0f, 2.0f}, inf3[3] = {INFINITY, 0.0f, 0.0f};
    for (const float* v : {nan3, zero3, long3, inf3}) {
        d = b.frame(v);
        CHECK(d.group == -1 && strcmp(d.why, "unknown") == 0 && d.candidate == -1 && d.blank_run == 0);
    }
    CHECK(b.frame(kDirA).group == 0);
}

void test_taa(const MapsTable& t) {
    // With anti-aliasing off the camera never comes, and nothing but the TAA load changes.
    struct Step { const float* dir; std::vector<std::pair<int, uint32_t>> votes; bool fresh; };
    const Step steps[] = {{kDirA, {}, false}, {kDirA, {}, true}, {kDirCD, {{kTC, 5}}, true}, {kDirCD, {{kTShared, 9}}, true},
                          {kDirUnknown, {}, true}, {kDirAB, {{kTA, 9}}, true}, {kDirB, {}, true}, {kDirCD, {}, false}};
    const int repeats[] = {31, 8, 3, 12, 7, 9, 6, 40};
    Sim on(t), off(t);
    const float camera[3] = {5.0f, 6.0f, 7.0f};
    bool all_same = true;
    for (int k = 0; k < 8; k++) {
        for (int i = 0; i < repeats[k]; i++) {
            const MapsDecision a = on.frame(steps[k].dir, steps[k].votes, steps[k].fresh, true, camera);
            const MapsDecision b = off.frame(steps[k].dir, steps[k].votes, steps[k].fresh, false, nullptr);
            all_same = all_same && a.candidate == b.candidate && strcmp(a.why, b.why) == 0 && a.load == b.load && a.changed == b.changed &&
                       a.snap == b.snap && on.s.target == off.s.target && on.s.held == off.s.held;
        }
    }
    CHECK(all_same);

    Sim j(t);
    const float c1[3] = {1.0f, 0.0f, 0.0f}, c101[3] = {101.0f, 0.0f, 0.0f}, c401[3] = {401.0f, 0.0f, 0.0f}, c900[3] = {900.0f, 0.0f, 0.0f};
    for (int i = 0; i < 6; i++) j.frame(kDirA, {}, true, true, c1);
    CHECK(j.s.target == kA);
    MapsDecision d = j.frame(kDirA, {}, true, true, c101);
    CHECK(!d.load && std::fabs(d.taa_jump - 100.0f) < 1e-3f);
    d = j.frame(kDirA, {}, true, true, c401);
    CHECK(d.load && d.changed && d.snap && j.s.target == -1);
    d = j.frame(kDirA, {}, true, true, c401);
    CHECK(d.changed && d.snap && j.s.target == kA);
    // A camera after a frame without one is no jump.
    j.frame(kDirA, {}, true, false, nullptr);
    d = j.frame(kDirA, {}, true, true, c900);
    CHECK(!d.load && d.taa_jump < 0.0f);

    // A camera at the origin, as the TAA resolve publishes on some frames at some bonfires, is no reading: between two
    // real ones 255 m from it, neither side is a jump, and L and the target stay.
    const float origin[3] = {0.0f, 0.0f, 0.0f}, near_origin[3] = {5e-5f, -5e-5f, 0.0f}, garden[3] = {254.6f, -1.9f, -14.0f};
    Sim z(t);
    for (int i = 0; i < 6; i++) z.frame(kDirA, {}, true, true, garden);
    CHECK(z.s.target == kA && z.s.held == kA);
    // One zero frame between two real ones, which gave the logged pairs of loads a frame apart, and a run of them,
    // which gave a load at each end.
    bool none = true;
    for (const float* zero : {origin, near_origin}) {
        for (int run : {1, 3}) {
            for (int i = 0; i < run; i++) {
                d = z.frame(kDirA, {}, true, true, zero);
                none = none && !d.load && d.taa_jump < 0.0f;
            }
            d = z.frame(kDirA, {}, true, true, garden);
            none = none && !d.load && d.taa_jump < 0.0f;
        }
    }
    d = z.frame(kDirA, {}, true, true, garden);
    CHECK(none && !d.load && std::fabs(d.taa_jump) < 1e-3f && z.s.target == kA && z.s.held == kA);
    // A component of 1e-3 m is a reading, so this one is 255 m from the last and a load.
    const float off_origin[3] = {0.0f, 1e-3f, 0.0f};
    d = z.frame(kDirA, {}, true, true, off_origin);
    CHECK(d.load && d.changed && d.taa_jump > 254.0f && z.s.target == -1);
    // A real 300 m jump is still a load.
    Sim w(t);
    const float c301[3] = {301.0f, 0.0f, 0.0f};
    for (int i = 0; i < 6; i++) w.frame(kDirA, {}, true, true, c1);
    d = w.frame(kDirA, {}, true, true, c301);
    CHECK(d.load && d.changed && std::fabs(d.taa_jump - 300.0f) < 1e-3f && w.s.target == -1);
    // Zero, zero, then real: the real one has no reading before it.
    Sim y(t);
    y.frame(kDirA, {}, true, true, origin);
    y.frame(kDirA, {}, true, true, origin);
    d = y.frame(kDirA, {}, true, true, c900);
    CHECK(!d.load && d.taa_jump < 0.0f);
}

void test_hold_expiry(const MapsTable& t) {
    Sim m(t);
    m.frame(kDirCD, {{kTC, 5}});
    CHECK(m.s.held == kC);
    MapsDecision d;
    bool held = true;
    for (int i = 0; i < 900; i++) {
        d = m.frame(kDirCD, {{kTShared, 10}});
        held = held && d.candidate == kC;
    }
    CHECK(held && m.s.held == kC);
    d = m.frame(kDirCD, {{kTShared, 10}});
    CHECK(d.candidate == -1 && d.expired && d.released == kC && m.s.held == -1 && strcmp(d.why, "expired") == 0);

    // A single draw of its own starts the count again.
    Sim r(t);
    r.frame(kDirCD, {{kTC, 5}});
    for (int i = 0; i < 899; i++) r.frame(kDirCD, {{kTShared, 10}});
    CHECK(r.frame(kDirCD, {{kTC, 1}}).candidate == kC);
    held = true;
    for (int i = 0; i < 900; i++) held = held && r.frame(kDirCD, {{kTShared, 10}}).candidate == kC;
    CHECK(held);
    CHECK(r.frame(kDirCD, {{kTShared, 10}}).expired);
}

void test_global_only(const MapsTable& t) {
    Sim m(t);
    m.reach(kDirA, kA);
    MapsDecision d;
    for (int i = 0; i < 6; i++) {
        d = m.frame(kDirGlobal, {{kTA, 100}, {kTE, 100}});
        CHECK(d.candidate == -1 && strcmp(d.why, "global_only") == 0);
    }
    CHECK(m.s.target == -1 && m.s.held == kA);
    Sim f(t);
    f.frame(kDirGlobal, {{kTE, 100}});
    CHECK(f.s.held == -1);
    CHECK(f.frame(kDirE).candidate == kE);
}

void test_c73_tally(const MapsTable& t) {
    // Pieces of one archive fill the frame and a few of another's are in view: the first one's group decides.
    Sim m(t);
    MapsDecision d;
    for (int i = 0; i < 6; i++) d = m.tally({{0, 90}, {5, 10}}, 0);
    CHECK(d.group == 0 && !d.mixed && strcmp(d.why, "unique") == 0 && m.s.target == kA && m.s.held == kA);
    // 60 to 40: mixed, which confirms the globals like any other candidate and leaves L as it is.
    for (int i = 0; i < 5; i++) {
        d = m.tally({{0, 60}, {5, 40}}, 0);
        CHECK(d.group == 0 && d.mixed && d.candidate == -1 && strcmp(d.why, "mixed") == 0 && m.s.target == kA && m.s.held == kA);
    }
    d = m.tally({{0, 60}, {5, 40}}, 0);
    CHECK(d.changed && !d.snap && m.s.target == -1 && m.s.held == kA);
    d = m.tally({{0, 40}, {5, 60}}, 0);
    CHECK(d.group == 5 && d.mixed && m.s.held == kA);

    // Nor does a mixed frame count towards a hold's expiry.
    Sim h(t);
    h.frame(kDirCD, {{kTC, 5}});
    for (int i = 0; i < 10; i++) h.frame(kDirCD, {{kTShared, 10}});
    CHECK(h.s.held == kC && h.s.hold_zero == 10);
    bool kept = true;
    for (int i = 0; i < 2000; i++) {
        d = h.tally({{3, 60}, {0, 40}}, 0, {{kTShared, 10}});
        kept = kept && d.mixed && !d.expired && h.s.held == kC && h.s.hold_zero == 10;
    }
    CHECK(kept && h.s.target == -1);
    d = h.tally({{3, 100}}, 0, {{kTShared, 10}});
    CHECK(d.candidate == kC && strcmp(d.why, "hold") == 0 && h.s.hold_zero == 11);

    // The share is a floor: 75 of 100 is enough, 74 is not, and a tie is mixed.
    Sim e(t);
    CHECK(!e.tally({{0, 75}, {5, 25}}, 0).mixed && e.tally({{0, 74}, {5, 26}}, 0).mixed && e.tally({{5, 50}, {0, 50}}, 0).mixed);
    // Draws whose c73 is in no group are left out of the share; alone they are an unknown direction, not a blank.
    d = e.tally({{0, 3}}, 100);
    CHECK(d.group == 0 && !d.mixed && d.candidate == kA);
    d = e.tally({}, 100);
    CHECK(d.group == -1 && !d.mixed && strcmp(d.why, "unknown") == 0 && d.blank_run == 0);
    d = e.tally({}, 0);
    CHECK(d.group == -2 && strcmp(d.why, "blank") == 0 && d.blank_run == 1);
    // Only frames with no voting draw count towards a load.
    Sim l(t);
    bool no_load = true;
    for (int i = 0; i < 40; i++) no_load = no_load && !l.tally({}, 5).load;
    for (int i = 0; i < 29; i++) no_load = no_load && !l.tally({}, 0).load;
    CHECK(no_load && l.tally({}, 0).load);

    // 1: every draw in a group names the same one. 0: the most named decides alone. A tie goes to the lower index.
    Sim u(t);
    u.r.c73_share = 1.0f;
    CHECK(u.tally({{0, 99}, {5, 1}}, 0).mixed && !u.tally({{0, 100}}, 7).mixed);
    Sim z(t);
    z.r.c73_share = 0.0f;
    d = z.tally({{0, 30}, {5, 35}, {1, 35}}, 0);
    CHECK(d.group == 1 && !d.mixed && d.candidate == -1 && strcmp(d.why, "unique") == 0);
    Sim half(t);
    half.r.c73_share = 0.5f;
    d = half.tally({{5, 50}, {0, 50}}, 0);
    CHECK(d.group == 0 && !d.mixed && d.candidate == kA);
}

void test_shader_names() {
    // Names as the shader table gives them, aliases joined by '|'. The Csd_HemEnv ones are Role::ForwardLit, which alone
    // publishes the forward constants; the light-mapped ones beside them carry the same block and vote too. The Gst
    // name is made up.
    for (const char* name : {"FRPG_Phn_DifSpcBmp___LitCsd_HemEnv.fpo", "FRPG_Phn_DifSpcBmp______Csd_HemEnv.fpo",
                             "FRPG_Phn_DifSpcBmp___LitCsd_HemEnvLerpPntS.fpo|FRPG_Phn_DifSpcBmp___LitCsd_HemEnvPntS.fpo",
                             "FRPG_Phn_DifSpcBmp___LitSdw_HemEnv.fpo", "FRPG_Phn_DifSpcBmp___Lit____HemEnv.fpo",
                             "FRPG_Phn_DifSpcBmp______Sdw_HemEnv.fpo", "FRPG_Phn_DifSpcBmp__________HemEnv.fpo",
                             "FRPG_Phn_DifSpcBmp___LitSdw_HemEnvParallax.fpo", "FRPG_Gst_Dif______LitSdw_HemEnv.fpo"}) {
        CHECK(maps_shader_votes(name));
    }
    for (const char* name : {"FRPG_Fil_HDR_PBL_ColAdj.fpo", "FRPG_Menu_Col.fpo", "FRPG_Phn_DifSpcBmp___LitSdw_.fpo",
                             "X_FRPG_Phn_DifSpcBmp___LitSdw_HemEnv.fpo", "?0123456789abcdef0123456789abcdef", ""}) {
        CHECK(!maps_shader_votes(name));
    }
}

//------------------------------------------------------------------------------
// Config and the blend
//------------------------------------------------------------------------------
const char kIniBase[] =
    "; synthetic\n"
    "[lightreach]\nreach = 1.8\n"
    "[lighting]\nenabled = 1\npoint_gain = 1.6          ; a comment\n"
    "[volumetrics]\nstrength = 1\nhaze_density = 0.00005\n"
    "[gtao]\nradius = 2.5\n";

std::string ini(int enabled, int apply, const char* blend_seconds, const char* extra = "") {
    char maps[256];
    snprintf(maps, sizeof(maps), "[maps]\nenabled = %d\napply = %d\nlog = 1\nblend_seconds = %s\nsnap_seconds = 0\n", enabled, apply, blend_seconds);
    return std::string(kIniBase) + maps + "[map.m01_00]\nlighting.point_gain = 1.2\nlighting.enabled = 0\ngtao.radius = 1\n" + extra;
}

const char kMapsIni[] =
    "; synthetic per-map values\n"
    "stray = 1\n"
    "[map.m01_00]\nlightreach.reach = 1\nlighting.point_gain = 1.3\n"
    "[map.m03_00]\nvolumetrics.haze_density = 0.000025\nvolumetrics.haze_height = 20\n"
    "[map.m05_00]\nlighting.point_gain = 1.0\n"
    "[map.m07_00]\nlightreach.reach = 1\nlighting.point_gain = 1.3\n";

void test_config() {
    write_text(data_dir() + L"dsrle.ini", ini(1, 1, "0"));
    write_text(data_dir() + L"maps.ini", kMapsIni);
    config_load();
    float v = 0.0f;
    CHECK(config_map_value("m01_00", "lighting.point_gain", &v) && same(v, 1.2f));
    CHECK(config_map_value("m01_00", "lightreach.reach", &v) && same(v, 1.0f));
    CHECK(config_map_value("m05_00", "lighting.point_gain", &v) && same(v, 1.0f));
    CHECK(!config_map_value("m01_00", "lighting.enabled", &v) && !config_map_value("m01_00", "gtao.radius", &v));
    CHECK(!config_map_value("m09_00", "lighting.point_gain", &v) && !config_map_value("m01_00", "volumetrics.strength", &v));
    CHECK(same(cfg_float("lighting.point_gain", 0.0f), 1.6f) && same(cfg_float("gtao.radius", 0.0f), 2.5f) && cfg_bool("lighting.enabled", false));
    CHECK(cfg_float("map.m01_00.lighting.point_gain", -1.0f) == -1.0f && cfg_float("stray", -1.0f) == -1.0f);
    CHECK(config_base_value("lighting.point_gain", &v) && same(v, 1.6f) && !config_base_value("volumetrics.haze_height", &v));
    CHECK(config_effective_is_base());
    const std::vector<std::string> keys = config_map_keys();
    CHECK((keys == std::vector<std::string>{"lighting.point_gain", "lightreach.reach", "volumetrics.haze_density", "volumetrics.haze_height"}));
    const ConfigMapSections sections = config_map_sections();
    CHECK(sections.shipped.size() == 4 && sections.user == std::vector<std::string>{"m01_00"});
    CHECK(config_map_key_allowed("lightreach.reach") && !config_map_key_allowed("lightreach.enabled") && !config_map_key_allowed("composite.debug"));
    config_load();
    const std::string log = read_log();
    CHECK(occurrences(log, "map.m01_00.lighting.enabled is not a setting a map can have") == 1);
    CHECK(occurrences(log, "map.m01_00.gtao.radius is not a setting a map can have") == 1);
    CHECK(occurrences(log, "maps.ini: stray is outside a [map.*] section") == 1);

    // The overlay: set over the base, then back to it bit for bit, and kept across a reload.
    const OverlayWrite set[] = {{"lighting.point_gain", true, 1.25f}, {"volumetrics.haze_height", true, 9.0f}};
    config_set_overlay(set, 2);
    CHECK(same(cfg_float("lighting.point_gain", 0.0f), 1.25f) && same(cfg_float("volumetrics.haze_height", 0.0f), 9.0f) && !config_effective_is_base());
    config_load();
    CHECK(same(cfg_float("lighting.point_gain", 0.0f), 1.25f) && same(cfg_float("volumetrics.haze_height", 0.0f), 9.0f));
    const OverlayWrite unset[] = {{"lighting.point_gain", false, 0.0f}, {"volumetrics.haze_height", false, 0.0f}};
    config_set_overlay(unset, 2);
    CHECK(same(cfg_float("lighting.point_gain", 0.0f), 1.6f) && cfg_float("volumetrics.haze_height", -1.0f) == -1.0f && config_effective_is_base());
    // An overlay key no section names, before or after a reload, is dropped by it.
    const OverlayWrite stray[] = {{"volumetrics.strength", true, 0.5f}};
    config_set_overlay(stray, 1);
    config_load();
    CHECK(same(cfg_float("volumetrics.strength", 0.0f), 1.0f) && config_effective_is_base());
}

void steps_until_settled(MapsBlend& b, std::vector<float>* reach, int limit = 1000) {
    for (int i = 0; i < limit && !b.settled; i++) {
        maps_blend_step(b, 1.0 / 60.0, 1.5);
        if (reach) reach->push_back(cfg_float("lightreach.reach", 0.0f));
    }
}

const MapsBlendKey* key_of(const MapsBlend& b, const char* key) {
    for (const MapsBlendKey& k : b.keys) {
        if (k.key == key) return &k;
    }
    return nullptr;
}

void test_blend() {
    MapsBlend b;
    maps_blend_set_keys(b, config_map_keys());
    CHECK(b.keys.size() == 4 && b.settled);

    // Into a map whose reach is 1: down all the way, reach > 1 until rest, then exactly the parsed value.
    maps_blend_retarget(b, "m07_00", false);
    std::vector<float> reach;
    steps_until_settled(b, &reach);
    bool down = true, above = true;
    for (size_t i = 1; i < reach.size(); i++) down = down && reach[i] <= reach[i - 1];
    for (size_t i = 0; i + 1 < reach.size(); i++) above = above && reach[i] > 1.0f;
    CHECK(reach.size() >= 89 && reach.size() <= 91 && down && above && same(reach.back(), 1.0f));
    CHECK(same(cfg_float("lighting.point_gain", 0.0f), 1.3f));
    // And back: the globals exactly, the table the file's.
    maps_blend_retarget(b, "", false);
    reach.clear();
    steps_until_settled(b, &reach);
    bool up = true;
    for (size_t i = 1; i < reach.size(); i++) up = up && reach[i] >= reach[i - 1];
    CHECK(up && same(reach.back(), 1.8f) && config_effective_is_base());

    // Halfway into {reach 1, gain 1.3}, then to {gain 1.0}: both start where they are, reach lands on the base.
    maps_blend_retarget(b, "m07_00", false);
    for (int i = 0; i < 45; i++) maps_blend_step(b, 1.0 / 60.0, 1.5);
    const float mid_reach = cfg_float("lightreach.reach", 0.0f), mid_gain = cfg_float("lighting.point_gain", 0.0f);
    CHECK(std::fabs(b.t - 0.5) < 1e-9 && mid_reach > 1.0f && mid_reach < 1.8f && mid_gain > 1.3f && mid_gain < 1.6f);
    maps_blend_retarget(b, "m05_00", false);
    CHECK(same(key_of(b, "lightreach.reach")->from, mid_reach) && same(key_of(b, "lighting.point_gain")->from, mid_gain));
    maps_blend_step(b, 1.0 / 60.0, 1.5);
    const float next_reach = cfg_float("lightreach.reach", 0.0f), next_gain = cfg_float("lighting.point_gain", 0.0f);
    CHECK(next_reach > mid_reach && next_reach - mid_reach < 0.01f && next_gain < mid_gain && mid_gain - next_gain < 0.01f);
    steps_until_settled(b, nullptr);
    CHECK(same(cfg_float("lightreach.reach", 0.0f), 1.8f) && same(cfg_float("lighting.point_gain", 0.0f), 1.0f) && !config_effective_is_base());
    maps_blend_retarget(b, "", true);
    maps_blend_step(b, 0.0, 1.5);
    CHECK(config_effective_is_base());

    // A key dsrle.ini lacks switches at once, says so once, and is gone again at the globals.
    const size_t before = occurrences(read_log(), "volumetrics.haze_height has no value in dsrle.ini");
    for (int round = 0; round < 2; round++) {
        maps_blend_retarget(b, "m03_00", false);
        maps_blend_step(b, 1.0 / 60.0, 1.5);
        CHECK(same(cfg_float("volumetrics.haze_height", -1.0f), 20.0f));
        const float haze = cfg_float("volumetrics.haze_density", 0.0f);
        CHECK(haze < 0.00005f && haze > 0.000025f);
        steps_until_settled(b, nullptr);
        CHECK(same(cfg_float("volumetrics.haze_density", 0.0f), 0.000025f));
        maps_blend_retarget(b, "", false);
        maps_blend_step(b, 1.0 / 60.0, 1.5);
        CHECK(cfg_float("volumetrics.haze_height", -1.0f) == -1.0f);
        steps_until_settled(b, nullptr);
        CHECK(config_effective_is_base());
    }
    CHECK(occurrences(read_log(), "volumetrics.haze_height has no value in dsrle.ini") == before + 1 && before == 0);

    // A map with no section changes nothing.
    maps_blend_retarget(b, "m09_00", false);
    steps_until_settled(b, nullptr);
    CHECK(config_effective_is_base());
}

//------------------------------------------------------------------------------
// The engine's side, with the synthetic table installed as the engine would find it
//------------------------------------------------------------------------------
// Nothing here publishes a forward block, so every frame that samples is one only voting draws show, as with the
// light-mapped pieces of Darkroot.
struct Glue {
    const MapsTable& t;
    MapsVoteCache cache;
    explicit Glue(const MapsTable& table) : t(table) {}

    // n voting draws of one tuple with c73 as their sun direction, from one copy, as when a draw reuses a block it did
    // not map again.
    void draws(const float* c73, int tuple, int n) {
        if (!maps_wants_votes()) return;
        const MapsTuple& tp = t.tuples[static_cast<size_t>(tuple)];
        const float regs[12] = {tp.c5[0], tp.c5[1], tp.c5[2], 0.0f, tp.c6[0], tp.c6[1], tp.c6[2], 0.0f, c73[0], c73[1], c73[2], 0.0f};
        MapsVoteResult result{kMapsUnresolved, 0};
        for (int i = 0; i < n; i++) {
            maps_note_forward_draw();
            maps_vote(cache, regs, result, false);
        }
    }
    void present(bool reloaded = false) {
        MapsPresent p;
        maps_on_present(p, reloaded);
    }
    // Not fresh: no voting draw. With no light rows asked for, one draw of the shared tuple, which nothing here decides by.
    void frame(const float* c73, std::initializer_list<std::pair<int, int>> votes = {}, bool reloaded = false, bool fresh = true) {
        if (fresh) {
            for (const auto& v : votes) draws(c73, v.first, v.second);
            if (votes.size() == 0) draws(c73, kTShared, 1);
        }
        present(reloaded);
    }
    void frames(int n, const float* c73, std::initializer_list<std::pair<int, int>> votes = {}) {
        for (int i = 0; i < n; i++) frame(c73, votes);
    }
    void reload(const std::string& dsrle_ini, const float* c73) {
        write_text(data_dir() + L"dsrle.ini", dsrle_ini);
        config_load();
        frame(c73, {}, true);
    }
};

void test_glue(const MapsTable& t) {
    CreateDirectoryW((game_dir() + L"param").c_str(), nullptr);
    CreateDirectoryW((game_dir() + L"param\\DrawParam").c_str(), nullptr);
    for (int i = 10; i <= 18; i++) {
        wchar_t name[64];
        swprintf_s(name, L"param\\DrawParam\\a%d_DrawParam.parambnd.dcx", i);
        write_text(game_dir() + name, "abc");
    }
    write_text(data_dir() + L"maps_detect.txt", kTable);
    write_text(data_dir() + L"dsrle.ini", ini(1, 1, "0"));
    write_text(data_dir() + L"maps.ini", kMapsIni);
    config_load();
    maps_init();
    Glue g(t);
    static const float none[3] = {};
    for (int i = 0; i < 5000 && !maps_wants_votes(); i++) {
        g.frame(none, {}, false, false);
        Sleep(1);
    }
    CHECK(maps_wants_votes());
    std::string log = read_log();
    // The log is a text-mode file.
    CHECK(occurrences(log, "maps: table maps=7 groups=7 tuples=6 dp=match maps_ini=4 user=1\r\n") == 1);
    CHECK(occurrences(log, "maps: [map.m07_00] is not a map the table can find") == 1);

    // A load, then a unique group: its values, dsrle.ini's over maps.ini's.
    for (int i = 0; i < 30; i++) g.frame(none, {}, false, false);
    CHECK(occurrences(read_log(), "maps: load f=") == 1);
    g.frames(5, kDirA);
    CHECK(same(cfg_float("lightreach.reach", 0.0f), 1.8f));
    g.frame(kDirA);
    CHECK(same(cfg_float("lightreach.reach", 0.0f), 1.0f) && same(cfg_float("lighting.point_gain", 0.0f), 1.2f));
    // A map with no section changes nothing.
    g.frames(6, kDirN);
    CHECK(config_effective_is_base());
    g.frames(6, kDirA);
    CHECK(same(cfg_float("lightreach.reach", 0.0f), 1.0f));

    // apply = 0: the map is still found and logged, but nothing is written.
    g.reload(ini(1, 0, "0"), kDirA);
    bool base = config_effective_is_base();
    for (int i = 0; i < 8; i++) {
        g.frame(i < 4 ? kDirA : kDirAB, {{kTA, 10}});
        base = base && config_effective_is_base();
    }
    g.frames(6, kDirA);
    CHECK(base && config_effective_is_base());
    g.reload(ini(1, 1, "0"), kDirA);
    CHECK(same(cfg_float("lightreach.reach", 0.0f), 1.0f));

    // enabled = 0 inside the map: every entry erased, no votes, and the table is the file's.
    g.reload(ini(0, 1, "0"), kDirA);
    CHECK(config_effective_is_base() && !maps_wants_votes());
    g.frames(8, kDirA);
    CHECK(config_effective_is_base());
    // The reload's own frame was drawn before the votes came back on, so it is blank.
    g.reload(ini(1, 1, "0"), kDirA);
    g.frames(5, kDirA);
    CHECK(maps_wants_votes() && config_effective_is_base());
    g.frame(kDirA);
    CHECK(same(cfg_float("lightreach.reach", 0.0f), 1.0f));
    // Back on with an ease set, as after a load: two frames confirm and the values switch at once.
    g.reload(ini(0, 1, "1.5"), kDirA);
    CHECK(config_effective_is_base());
    g.reload(ini(1, 1, "1.5", "[maps]\nsnap_seconds = 2\n"), kDirA);
    g.frame(kDirA);
    CHECK(config_effective_is_base());
    g.frame(kDirA);
    CHECK(same(cfg_float("lightreach.reach", 0.0f), 1.0f) && same(cfg_float("lighting.point_gain", 0.0f), 1.2f));

    // A key added for the map in dsrle.ini and then taken out again goes back to the global value.
    g.reload(ini(1, 1, "0", "volumetrics.strength = 0.5\n"), kDirA);
    CHECK(same(cfg_float("volumetrics.strength", 0.0f), 0.5f));
    g.reload(ini(1, 1, "0"), kDirA);
    CHECK(same(cfg_float("volumetrics.strength", 0.0f), 1.0f) && same(cfg_float("lightreach.reach", 0.0f), 1.0f));
    g.frames(6, kDirB);
    CHECK(config_effective_is_base());

    // A reload mid-ease: the old values stay over the new table until the reparsed ones snap in, the same present.
    g.reload(ini(1, 1, "1"), kDirB);
    g.frames(6, kDirA);
    for (int i = 0; i < 2; i++) {
        Sleep(150);
        g.frame(kDirA);
    }
    const float mid = cfg_float("lightreach.reach", 0.0f);
    CHECK(mid > 1.0f && mid < 1.8f);
    write_text(data_dir() + L"maps.ini", replaced(kMapsIni, "[map.m01_00]\nlightreach.reach = 1\n", "[map.m01_00]\nlightreach.reach = 1.25\n"));
    config_load();
    CHECK(same(cfg_float("lightreach.reach", 0.0f), mid));
    g.frame(kDirA, {}, true);
    CHECK(same(cfg_float("lightreach.reach", 0.0f), 1.25f));
    write_text(data_dir() + L"maps.ini", kMapsIni);
    g.reload(ini(1, 1, "0"), kDirA);
    CHECK(same(cfg_float("lightreach.reach", 0.0f), 1.0f));

    // The vote, through the per-context cache: its own draws and enough of them, then one of the walk rival's.
    g.frames(6, kDirB);
    g.frames(6, kDirAB, {{kTA, 10}, {kTShared, 5}});
    CHECK(same(cfg_float("lightreach.reach", 0.0f), 1.0f));
    g.frames(6, kDirAB, {{kTA, 10}, {kTShared, 5}, {kTB, 1}});
    CHECK(config_effective_is_base());

    // Each draw's own c73: pieces of two archives, 6 to 4, are mixed and give the globals; 9 to 1 is the first's group.
    g.frames(6, kDirA);
    CHECK(same(cfg_float("lightreach.reach", 0.0f), 1.0f));
    for (int i = 0; i < 6; i++) {
        g.draws(kDirA, kTShared, 6);
        g.draws(kDirE, kTShared, 4);
        g.present();
    }
    CHECK(config_effective_is_base());
    log = read_log();
    CHECK(occurrences(log, " c73s=0:6,5:4 c73miss=0 grp=0 ") > 0 && occurrences(log, " why=mixed ") > 0);
    for (int i = 0; i < 6; i++) {
        g.draws(kDirA, kTShared, 9);
        g.draws(kDirE, kTShared, 1);
        g.draws(kDirUnknown, kTShared, 30);
        g.present();
    }
    CHECK(same(cfg_float("lightreach.reach", 0.0f), 1.0f));
    CHECK(occurrences(read_log(), " c73s=0:9,5:1 c73miss=30 grp=0 ") > 0);
    g.frames(6, kDirB);
    CHECK(config_effective_is_base());

    // A mark: logged with the present, followed by the status line, and the file deleted.
    const std::wstring trigger = data_dir() + L"trigger_mark";
    write_text(trigger, "spot_a");
    for (int i = 0; i < 8 && GetFileAttributesW(trigger.c_str()) != INVALID_FILE_ATTRIBUTES; i++) g.frame(kDirB);
    CHECK(GetFileAttributesW(trigger.c_str()) == INVALID_FILE_ATTRIBUTES);
    log = read_log();
    const size_t mark = log.find("maps: mark spot_a f=");
    CHECK(mark != std::string::npos);
    if (mark != std::string::npos) {
        const size_t next = log.find('\n', mark) + 1;
        CHECK(log.find("maps: f=", next) == log.find("] ", next) + 2);
    }

    // What the table does not know is logged with log = 1: a light row and a direction.
    const float unknown_regs[12] = {0.0f, 0.0f, 1.0f, 0.0f, 9.0f, 9.0f, 9.0f, 0.0f, kDirUnknown[0], kDirUnknown[1], kDirUnknown[2], 0.0f};
    MapsVoteResult result{kMapsUnresolved, 0};
    maps_vote(g.cache, unknown_regs, result, true);
    g.frame(kDirUnknown);
    for (int i = 0; i < 60; i++) g.frame(kDirB);
    log = read_log();
    CHECK(occurrences(log, "maps: miss c5=0.000000,0.000000,1.000000 c6=9.000000,9.000000,9.000000 n=1") == 1);
    // Once, however many draws named it: those of the 9 to 1 frames before, the draw above and the frame's own.
    CHECK(occurrences(log, "maps: dir-miss c73=0.600000,0.800000,0.000000 ang=") == 1);
    CHECK(occurrences(log, " fwd=") > 0 && occurrences(log, " voted=") > 0 && occurrences(log, " why=vote") > 0);
    CHECK(config_effective_is_base());

    // The DrawParam check: one area that differs, and a folder without the files.
    MapsTable other = t;
    other.dp[2][0] = other.dp[2][0] == '0' ? '1' : '0';
    std::string bad;
    CHECK(maps_check_hashes(t, game_dir() + L"param\\DrawParam\\", &bad) && bad.empty());
    CHECK(!maps_check_hashes(other, game_dir() + L"param\\DrawParam\\", &bad) && bad == "a12");
    CHECK(!maps_check_hashes(t, game_dir() + L"nowhere\\", &bad) && bad.find("a10 missing") == 0);
}

// A real table: counts, and the generator's first-match promise for directions.
int read_table(const wchar_t* path, const wchar_t* dir) {
    FILE* f = nullptr;
    _wfopen_s(&f, path, L"rb");
    if (!f) {
        printf("cannot open %ls\n", path);
        return 1;
    }
    std::string text;
    char buf[4096];
    size_t got;
    while ((got = fread(buf, 1, sizeof(buf), f)) > 0) text.append(buf, got);
    fclose(f);
    MapsTable t;
    std::string error;
    if (!maps_parse_table(text.data(), text.size(), &t, &error)) {
        printf("refused: %s\n", error.c_str());
        return 1;
    }
    size_t overrides = 0, global_only = 0, own_dirs = 0, own_tuples = 0;
    for (uint8_t v : t.has_values) overrides += v;
    for (size_t g = 0; g < t.groups.size(); g++) {
        global_only += t.groups[g].global_only;
        for (const auto& d : t.groups[g].dirs) {
            bool only = true;
            for (size_t h = 0; h < t.groups.size(); h++) {
                for (const auto& e : t.groups[h].dirs) {
                    if (h != g && static_cast<double>(d[0]) * e[0] + static_cast<double>(d[1]) * e[1] + static_cast<double>(d[2]) * e[2] >= t.cos_tol) only = false;
                }
            }
            own_dirs += only ? 1 : 0;
        }
    }
    for (size_t i = 0; i < t.tuples.size(); i++) own_tuples += maps_match_tuple(t, t.tuples[i].c5, t.tuples[i].c6) == static_cast<int>(i) ? 1 : 0;
    printf("parsed: maps=%zu tuples=%zu groups=%zu dirs=%zu evs=%zu, %zu with values, %zu global_only\n", t.maps.size(), t.tuples.size(),
           t.groups.size(), t.dir_count, t.ev_count, overrides, global_only);
    printf("directions matching their own group only: %zu of %zu; tuples matching themselves first: %zu of %zu\n", own_dirs, t.dir_count,
           own_tuples, t.tuples.size());
    if (dir) {
        std::string bad;
        const bool ok = maps_check_hashes(t, std::wstring(dir) + L"\\", &bad);
        printf("DrawParam: %s%s%s\n", ok ? "match" : "mismatch (", bad.c_str(), ok ? "" : ")");
        if (!ok) return 1;
    }
    return own_dirs == t.dir_count && own_tuples == t.tuples.size() ? 0 : 1;
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    if (argc > 1) return read_table(argv[1], argc > 2 ? argv[2] : nullptr);
    log_init();
    MapsTable t;
    std::string error;
    if (!maps_parse_table(kTable, sizeof(kTable) - 1, &t, &error)) {
        printf("FAIL: the synthetic table is refused: %s\n", error.c_str());
        return 1;
    }
    test_parse();
    test_match(t);
    test_unique(t);
    test_walk_group(t);
    test_cross_archive_hold();
    test_separated_group(t);
    test_unknown(t);
    test_blank_and_load(t);
    test_taa(t);
    test_hold_expiry(t);
    test_global_only(t);
    test_c73_tally(t);
    test_shader_names();
    test_config();
    test_blend();
    test_glue(t);
    printf("%d checks, %d failed\n", g_checks, g_failed);
    return g_failed ? 1 : 0;
}
