#include "common.h"
#include <cstdarg>
#include <cstdio>
#include <mutex>
#include <share.h>

static std::wstring make_game_dir() {
    wchar_t path[MAX_PATH * 2] = {};
    GetModuleFileNameW(nullptr, path, MAX_PATH * 2);
    std::wstring s(path);
    return s.substr(0, s.find_last_of(L"\\/") + 1);
}

const std::wstring& game_dir() {
    static const std::wstring dir = make_game_dir();
    return dir;
}

const std::wstring& data_dir() {
    static const std::wstring dir = game_dir() + L"dsrle\\";
    return dir;
}

static FILE* g_log;
static std::mutex g_log_mutex;

void log_init() {
    std::lock_guard<std::mutex> lock(g_log_mutex);
    if (g_log) return;
    CreateDirectoryW(data_dir().c_str(), nullptr);
    g_log = _wfsopen((data_dir() + L"dsrle.log").c_str(), L"w", _SH_DENYNO);
}

void logf(const char* fmt, ...) {
    std::lock_guard<std::mutex> lock(g_log_mutex);
    if (!g_log) return;
    SYSTEMTIME t;
    GetLocalTime(&t);
    fprintf(g_log, "%02d:%02d:%02d.%03d [%5lu] ", t.wHour, t.wMinute, t.wSecond, t.wMilliseconds, GetCurrentThreadId());
    va_list ap;
    va_start(ap, fmt);
    vfprintf(g_log, fmt, ap);
    va_end(ap);
    fputc('\n', g_log);
    fflush(g_log);
}

void log_flush() {
    std::lock_guard<std::mutex> lock(g_log_mutex);
    if (g_log) fflush(g_log);
}

void* patch_slot(void** vtable, int slot, void* hook) {
    if (vtable[slot] == hook) return nullptr;
    DWORD old = 0;
    if (!VirtualProtect(&vtable[slot], sizeof(void*), PAGE_EXECUTE_READWRITE, &old)) {
        logf("VirtualProtect failed for vtable %p slot %d: %lu", vtable, slot, GetLastError());
        return nullptr;
    }
    void* prev = vtable[slot];
    vtable[slot] = hook;
    VirtualProtect(&vtable[slot], sizeof(void*), old, &old);
    return prev;
}

uint64_t fnv1a64(const void* data, size_t size) {
    uint64_t h = 0xcbf29ce484222325ull;
    const auto* p = static_cast<const uint8_t*>(data);
    for (size_t i = 0; i < size; i++) {
        h ^= p[i];
        h *= 0x100000001b3ull;
    }
    return h;
}
