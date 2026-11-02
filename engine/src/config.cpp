#include "config.h"
#include "common.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <shared_mutex>
#include <string>
#include <unordered_map>

namespace {

std::unordered_map<std::string, float> g_values;
std::shared_mutex g_values_lock;  // the recording threads read settings while the render thread reloads them
FILETIME g_stamp;
int g_poll;

std::wstring ini_path() { return data_dir() + L"dsrle.ini"; }

bool file_stamp(FILETIME* out) {
    WIN32_FILE_ATTRIBUTE_DATA d;
    if (!GetFileAttributesExW(ini_path().c_str(), GetFileExInfoStandard, &d)) return false;
    *out = d.ftLastWriteTime;
    return true;
}

char* trim(char* s) {
    while (*s == ' ' || *s == '\t') s++;
    size_t n = strlen(s);
    while (n && (s[n - 1] == ' ' || s[n - 1] == '\t' || s[n - 1] == '\r' || s[n - 1] == '\n')) s[--n] = 0;
    return s;
}

}  // namespace

void config_load() {
    file_stamp(&g_stamp);
    FILE* f = nullptr;
    _wfopen_s(&f, ini_path().c_str(), L"r");
    if (!f) {
        logf("config: dsrle.ini not found, using defaults");
        return;
    }
    std::unordered_map<std::string, float> values;
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
        values[key] = static_cast<float>(atof(trim(eq + 1)));
    }
    fclose(f);
    logf("config: %zu values loaded", values.size());
    std::unique_lock<std::shared_mutex> lock(g_values_lock);
    g_values.swap(values);
}

bool config_poll() {
    if (++g_poll % 30) return false;
    FILETIME now;
    if (!file_stamp(&now) || CompareFileTime(&now, &g_stamp) == 0) return false;
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
