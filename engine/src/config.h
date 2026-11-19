// dsrle\dsrle.ini: flat "name = value" settings, re-read whenever the file changes. A section names the keys under it
// ("[gtao]" followed by "radius = 2" gives the key "gtao.radius"). [map.mAA_BB] sections are kept apart: they hold one
// map's own values, as does dsrle\maps.ini, which every install replaces. maps.cpp lays the values of the map the player
// is in over the rest through an overlay, so cfg_float always reads the value in effect.
#pragma once
#include <cstddef>
#include <string>
#include <vector>

// The level [engine] quality = 0 stands for, 1 low to 3 high. Call before config_load.
void config_set_auto_quality(int level);
// The level in force, 1 to 3.
int config_quality();

void config_load();
// Call once per frame. Returns true when either file changed and both were reloaded this frame.
bool config_poll();

float cfg_float(const char* key, float fallback);
int cfg_int(const char* key, int fallback);
inline bool cfg_bool(const char* key, bool fallback) { return cfg_int(key, fallback ? 1 : 0) != 0; }

// Whether a key may be set per map: the values the engine reads live or on every change, never a switch.
bool config_map_key_allowed(const char* key);
// A map's own value: dsrle.ini's [map.<map>] first, then maps.ini's. False when neither sets it.
bool config_map_value(const char* map, const char* key, float* out);
// What dsrle.ini says, or the built-in defaults where it says nothing, after the quality level's limits and without any
// map's value over it. False when neither has the key.
bool config_base_value(const char* key, float* out);
// Every allowed key that some [map.*] section of either file names, sorted.
std::vector<std::string> config_map_keys();
struct ConfigMapSections {
    std::vector<std::string> shipped;  // maps.ini's map ids
    std::vector<std::string> user;     // dsrle.ini's
};
ConfigMapSections config_map_sections();

struct OverlayWrite {
    const char* key;
    bool present;  // false puts dsrle.ini's value back, or removes the key when the file has none
    float value;
};
// Render thread only. The values stay in force across reloads until written again.
void config_set_overlay(const OverlayWrite* writes, size_t count);
// True when cfg_float reads exactly dsrle.ini's keys, every value with the same bits.
bool config_effective_is_base();
