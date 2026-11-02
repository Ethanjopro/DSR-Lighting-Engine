#include "shaderdb.h"
#include "common.h"
#include "config.h"
#include "hdr.h"
#include <cstdio>
#include <cstring>
#include <shared_mutex>
#include <unordered_map>

namespace {

struct Key {
    uint64_t lo, hi;
    bool operator==(const Key& o) const { return lo == o.lo && hi == o.hi; }
};
struct KeyHash {
    size_t operator()(const Key& k) const { return static_cast<size_t>(k.lo ^ (k.hi * 0x9E3779B97F4A7C15ull)); }
};

std::unordered_map<Key, std::string, KeyHash> g_names;
std::unordered_map<void*, ShaderInfo> g_objects;
std::shared_mutex g_lock;

uint64_t hex64(const char* s) {
    uint64_t v = 0;
    // The table stores the checksum bytes in file order, so read them back as little-endian bytes.
    for (int i = 0; i < 8; i++) {
        unsigned byte = 0;
        sscanf_s(s + i * 2, "%2x", &byte);
        v |= static_cast<uint64_t>(byte) << (i * 8);
    }
    return v;
}

}  // namespace

void shaderdb_load() {
    FILE* f = nullptr;
    _wfopen_s(&f, (data_dir() + L"shader_names.tsv").c_str(), L"r");
    if (!f) {
        logf("shaderdb: shader_names.tsv not found; shaders will be logged by checksum");
        return;
    }
    char line[8192];
    while (fgets(line, sizeof(line), f)) {
        char* tab1 = strchr(line, '\t');
        if (!tab1 || tab1 - line != 32) continue;
        char* tab2 = strchr(tab1 + 1, '\t');
        if (!tab2) continue;
        size_t len = strlen(tab2 + 1);
        while (len && (tab2[1 + len - 1] == '\n' || tab2[1 + len - 1] == '\r')) tab2[1 + --len] = 0;
        Key k{hex64(line), hex64(line + 16)};
        g_names.emplace(k, std::string(tab2 + 1));
    }
    fclose(f);
    logf("shaderdb: %zu names loaded", g_names.size());
}

void shaderdb_register(void* shader_object, const void* bytecode, size_t size, const char* stage) {
    if (!shader_object || !bytecode || size < 20) return;
    ShaderInfo info;
    memcpy(&info.hash_lo, static_cast<const char*>(bytecode) + 4, 8);
    memcpy(&info.hash_hi, static_cast<const char*>(bytecode) + 12, 8);
    info.stage = stage;
    std::unique_lock lock(g_lock);
    auto it = g_names.find(Key{info.hash_lo, info.hash_hi});
    if (it != g_names.end()) {
        info.name = it->second;
        info.role = role_from_shader_name(info.name.c_str());
    } else {
        char buf[64];
        snprintf(buf, sizeof(buf), "?%016llx%016llx", static_cast<unsigned long long>(info.hash_lo),
                 static_cast<unsigned long long>(info.hash_hi));
        info.name = buf;
        if (hdr_is_final_blit(info.hash_lo, info.hash_hi)) info.role = Role::FinalBlit;
        // Shaders outside the game's archives live in the exe. Keep a copy for inspection during development.
        if (cfg_bool("dev.dump_unknown", false)) {
            CreateDirectoryW((data_dir() + L"unknown").c_str(), nullptr);
            FILE* f = nullptr;
            wchar_t path[64];
            swprintf_s(path, L"%S_%S.dxbc", stage, buf + 1);
            _wfopen_s(&f, (data_dir() + L"unknown\\" + path).c_str(), L"wb");
            if (f) {
                fwrite(bytecode, 1, size, f);
                fclose(f);
            }
        }
    }
    g_objects[shader_object] = std::move(info);
}

void shaderdb_set_replacement(void* shader_object, ID3D11PixelShader* replacement) {
    std::unique_lock lock(g_lock);
    auto it = g_objects.find(shader_object);
    if (it == g_objects.end()) return;
    it->second.replacement = replacement;
    ShaderInfo copy = it->second;
    copy.name += " (engine)";
    copy.replacement = nullptr;
    g_objects[replacement] = std::move(copy);
}

const ShaderInfo* shaderdb_find(void* shader_object) {
    std::shared_lock lock(g_lock);
    auto it = g_objects.find(shader_object);
    return it == g_objects.end() ? nullptr : &it->second;
}
