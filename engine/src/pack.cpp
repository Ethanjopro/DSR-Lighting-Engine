#include "pack.h"
#include "common.h"
#include <cstdio>
#include <cstring>
#include <unordered_map>
#include <vector>

namespace {

struct Key {
    uint64_t lo, hi;
    bool operator==(const Key& o) const { return lo == o.lo && hi == o.hi; }
};
struct KeyHash {
    size_t operator()(const Key& k) const { return static_cast<size_t>(k.lo ^ (k.hi * 0x9E3779B97F4A7C15ull)); }
};
struct Blob {
    uint32_t offset, size;
};

std::vector<uint8_t> g_file;
std::unordered_map<Key, Blob, KeyHash> g_entries;  // filled once at startup, read-only after that

}  // namespace

void pack_load() {
    FILE* f = nullptr;
    _wfopen_s(&f, (data_dir() + L"shaders.pak").c_str(), L"rb");
    if (!f) {
        logf("pack: shaders.pak not found");
        return;
    }
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    g_file.resize(n);
    size_t got = fread(g_file.data(), 1, g_file.size(), f);
    fclose(f);
    uint32_t version = 0, count = 0;
    if (got < 12 || memcmp(g_file.data(), "DLPK", 4) != 0) {
        logf("pack: bad header");
        g_file.clear();
        return;
    }
    memcpy(&version, &g_file[4], 4);
    memcpy(&count, &g_file[8], 4);
    if (version != 1 || 12 + static_cast<uint64_t>(count) * 24 > got) {
        logf("pack: version %u count %u not usable", version, count);
        g_file.clear();
        return;
    }
    g_entries.reserve(count);
    for (uint32_t i = 0; i < count; i++) {
        const uint8_t* e = &g_file[12 + i * 24];
        Key k;
        Blob b;
        memcpy(&k.lo, e, 8);
        memcpy(&k.hi, e + 8, 8);
        memcpy(&b.offset, e + 16, 4);
        memcpy(&b.size, e + 20, 4);
        if (static_cast<uint64_t>(b.offset) + b.size <= got) g_entries.emplace(k, b);
    }
    logf("pack: %zu replacements, %.1f MB", g_entries.size(), g_file.size() / 1e6);
}

const void* pack_find(uint64_t hash_lo, uint64_t hash_hi, size_t* size) {
    auto it = g_entries.find(Key{hash_lo, hash_hi});
    if (it == g_entries.end()) return nullptr;
    *size = it->second.size;
    return &g_file[it->second.offset];
}
