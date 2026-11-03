#include "config.h"
#include "common.h"
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iterator>
#include <mutex>
#include <shared_mutex>
#include <string>
#include <unordered_map>
#include <unordered_set>

namespace {

using Values = std::unordered_map<std::string, float>;
using MapValues = std::unordered_map<std::string, Values>;  // map id, then key

Values g_values;     // what cfg_float reads: g_base with g_overlay over it
Values g_base;       // dsrle.ini, apart from its [map.*] sections
Values g_overlay;    // maps.cpp's values for the map the player is in
MapValues g_user;    // dsrle.ini's [map.*] sections
MapValues g_shipped; // maps.ini's
std::shared_mutex g_values_lock;  // the recording threads read settings while the render thread reloads them
FILETIME g_stamp;
FILETIME g_maps_stamp;
int g_poll;

// Debug views and list switches are left out: easing them would pass through values they do not have.
const char* const kMapKeys[] = {
    // engine.cpp read_pack_values, called whenever one of them changes
    "lighting.ao_on_points", "lighting.contact_shadow", "sunshadow.world_strength", "sunshadow.lit_strength",
    "lighting.sun_gain", "lighting.fill_probe_sun", "lighting.fill_hemi_sun", "lighting.fill_probe_dark",
    "lighting.fill_hemi_dark", "lighting.point_gain", "lighting.baked_gate_gain", "lighting.baked_gate_power",
    "lighting.point_falloff", "composite.film_shadow", "composite.film_highlight", "composite.exposure",
    "composite.black_floor", "composite.dark_exposure", "lighting.bent_normal", "lighting.ao_bounce",
    // lightreach.cpp, read at each light upload
    "lightreach.reach", "lightreach.max_radius", "lightreach.stretch_limit", "lightreach.full_distance",
    "lightreach.none_distance",
    // volumetrics.cpp, read every frame
    "volumetrics.strength", "volumetrics.near", "volumetrics.far", "volumetrics.jitter", "volumetrics.dither",
    "volumetrics.light_density", "volumetrics.light_phase", "volumetrics.radius_scale", "volumetrics.glow_core",
    "volumetrics.max_glow", "volumetrics.camera_fade_start", "volumetrics.camera_fade_end", "volumetrics.extinction",
    "volumetrics.haze_fog_cap", "volumetrics.haze_smooth", "volumetrics.haze_sun", "volumetrics.haze_density",
    "volumetrics.haze_phase", "volumetrics.haze_height", "volumetrics.sun",
};

std::wstring ini_path() { return data_dir() + L"dsrle.ini"; }
std::wstring maps_path() { return data_dir() + L"maps.ini"; }

bool file_stamp(const std::wstring& path, FILETIME* out) {
    WIN32_FILE_ATTRIBUTE_DATA d;
    if (!GetFileAttributesExW(path.c_str(), GetFileExInfoStandard, &d)) return false;
    *out = d.ftLastWriteTime;
    return true;
}

char* trim(char* s) {
    while (*s == ' ' || *s == '\t') s++;
    size_t n = strlen(s);
    while (n && (s[n - 1] == ' ' || s[n - 1] == '\t' || s[n - 1] == '\r' || s[n - 1] == '\n')) s[--n] = 0;
    return s;
}

// A file is re-read on every change, so a key it keeps wrong would otherwise be logged each time.
void log_once(const char* file, const std::string& key, const char* why) {
    static std::unordered_set<std::string> s_logged;
    if (s_logged.insert(std::string(file) + ":" + key).second) logf("config: %s: %s %s, ignored", file, key.c_str(), why);
}

// Keys under [map.<id>] go to maps, the rest to values, or nowhere when values is null. False when the file is missing.
bool parse(const std::wstring& path, Values* values, MapValues* maps, const char* file) {
    FILE* f = nullptr;
    _wfopen_s(&f, path.c_str(), L"r");
    if (!f) return false;
    std::string section;
    char line[512];
    while (fgets(line, sizeof(line), f)) {
        char* s = trim(line);
        if (!*s || *s == ';' || *s == '#') continue;
        if (*s == '[') {
            char* end = strchr(s, ']');
            if (end) *end = 0;
            section = s + 1;
            continue;
        }
        char* eq = strchr(s, '=');
        if (!eq) continue;
        *eq = 0;
        std::string key = trim(s);
        if (!section.empty()) key = section + "." + key;
        char* comment = strpbrk(eq + 1, ";#");
        if (comment) *comment = 0;
        const float value = static_cast<float>(atof(trim(eq + 1)));
        if (key.compare(0, 4, "map.") == 0) {
            const size_t dot = key.find('.', 4);
            if (dot == std::string::npos || dot == 4 || dot + 1 == key.size()) {
                log_once(file, key, "names no map and setting");
                continue;
            }
            const std::string setting = key.substr(dot + 1);
            if (!config_map_key_allowed(setting.c_str())) {
                log_once(file, key, "is not a setting a map can have");
                continue;
            }
            (*maps)[key.substr(4, dot - 4)][setting] = value;
        } else if (values) {
            (*values)[key] = value;
        } else {
            log_once(file, key, "is outside a [map.*] section");
        }
    }
    fclose(f);
    return true;
}

std::vector<std::string> keys_of(const MapValues& a, const MapValues& b) {
    std::vector<std::string> keys;
    for (const MapValues* maps : {&a, &b}) {
        for (const auto& section : *maps) {
            for (const auto& kv : section.second) keys.push_back(kv.first);
        }
    }
    std::sort(keys.begin(), keys.end());
    keys.erase(std::unique(keys.begin(), keys.end()), keys.end());
    return keys;
}

std::vector<std::string> ids_of(const MapValues& maps) {
    std::vector<std::string> ids;
    for (const auto& section : maps) ids.push_back(section.first);
    std::sort(ids.begin(), ids.end());
    return ids;
}

bool find(const MapValues& maps, const char* map, const char* key, float* out) {
    auto section = maps.find(map);
    if (section == maps.end()) return false;
    auto it = section->second.find(key);
    if (it == section->second.end()) return false;
    *out = it->second;
    return true;
}

}  // namespace

void config_load() {
    file_stamp(ini_path(), &g_stamp);
    if (!file_stamp(maps_path(), &g_maps_stamp)) g_maps_stamp = FILETIME{};
    Values base;
    MapValues user, shipped;
    const bool have_ini = parse(ini_path(), &base, &user, "dsrle.ini");
    if (have_ini) {
        logf("config: %zu values loaded", base.size());
    } else {
        logf("config: dsrle.ini not found, using defaults");
    }
    parse(maps_path(), nullptr, &shipped, "maps.ini");
    logf("config: per-map values for %zu maps in maps.ini and %zu in dsrle.ini", shipped.size(), have_ini ? user.size() : g_user.size());
    std::unique_lock<std::shared_mutex> lock(g_values_lock);
    const std::vector<std::string> old_keys = keys_of(g_user, g_shipped);
    if (have_ini) {
        g_base.swap(base);
        g_user.swap(user);
    }
    g_shipped.swap(shipped);
    // A key no section names any more is still set until maps.cpp, told of the reload, writes it back to dsrle.ini's
    // value. Anything that was never a map's key has no business here.
    const std::vector<std::string> new_keys = keys_of(g_user, g_shipped);
    for (auto it = g_overlay.begin(); it != g_overlay.end();) {
        const bool known = std::binary_search(old_keys.begin(), old_keys.end(), it->first) ||
                           std::binary_search(new_keys.begin(), new_keys.end(), it->first);
        it = known ? std::next(it) : g_overlay.erase(it);
    }
    // The current map's values go over the new table before anyone can read it, so no frame sees the globals alone.
    Values values = g_base;
    for (const auto& kv : g_overlay) values[kv.first] = kv.second;
    g_values.swap(values);
}

bool config_poll() {
    if (++g_poll % 30) return false;
    FILETIME ini, maps{};
    const bool ini_changed = file_stamp(ini_path(), &ini) && CompareFileTime(&ini, &g_stamp) != 0;
    file_stamp(maps_path(), &maps);
    if (!ini_changed && CompareFileTime(&maps, &g_maps_stamp) == 0) return false;
    config_load();
    return true;
}

float cfg_float(const char* key, float fallback) {
    std::shared_lock<std::shared_mutex> lock(g_values_lock);
    auto it = g_values.find(key);
    return it == g_values.end() ? fallback : it->second;
}

int cfg_int(const char* key, int fallback) {
    std::shared_lock<std::shared_mutex> lock(g_values_lock);
    auto it = g_values.find(key);
    return it == g_values.end() ? fallback : static_cast<int>(it->second);
}

bool config_map_key_allowed(const char* key) {
    for (const char* k : kMapKeys) {
        if (strcmp(k, key) == 0) return true;
    }
    return false;
}

bool config_map_value(const char* map, const char* key, float* out) {
    std::shared_lock<std::shared_mutex> lock(g_values_lock);
    return find(g_user, map, key, out) || find(g_shipped, map, key, out);
}

bool config_base_value(const char* key, float* out) {
    std::shared_lock<std::shared_mutex> lock(g_values_lock);
    auto it = g_base.find(key);
    if (it == g_base.end()) return false;
    *out = it->second;
    return true;
}

std::vector<std::string> config_map_keys() {
    std::shared_lock<std::shared_mutex> lock(g_values_lock);
    return keys_of(g_user, g_shipped);
}

ConfigMapSections config_map_sections() {
    std::shared_lock<std::shared_mutex> lock(g_values_lock);
    return {ids_of(g_shipped), ids_of(g_user)};
}

void config_set_overlay(const OverlayWrite* writes, size_t count) {
    std::unique_lock<std::shared_mutex> lock(g_values_lock);
    for (size_t i = 0; i < count; i++) {
        const OverlayWrite& w = writes[i];
        if (w.present) {
            g_overlay[w.key] = w.value;
            g_values[w.key] = w.value;
            continue;
        }
        g_overlay.erase(w.key);
        auto base = g_base.find(w.key);
        if (base != g_base.end()) {
            g_values[w.key] = base->second;
        } else {
            g_values.erase(w.key);
        }
    }
}

bool config_effective_is_base() {
    std::shared_lock<std::shared_mutex> lock(g_values_lock);
    if (g_values.size() != g_base.size()) return false;
    for (const auto& kv : g_base) {
        auto it = g_values.find(kv.first);
        if (it == g_values.end() || memcmp(&it->second, &kv.second, sizeof(float)) != 0) return false;
    }
    return true;
}
