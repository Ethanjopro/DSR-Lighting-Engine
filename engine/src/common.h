// Shared helpers for the DSRLE engine DLL.
#pragma once
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <cstdint>
#include <string>

// Directory holding the game exe, and <game>\dsrle, both with a trailing backslash.
const std::wstring& game_dir();
const std::wstring& data_dir();

void log_init();
void logf(const char* fmt, ...);
void log_flush();

// Patch one vtable slot. Returns the previous function. Safe to call twice on one slot: the second
// call is a no-op and returns nullptr, so callers only overwrite their saved original when non-null.
void* patch_slot(void** vtable, int slot, void* hook);

inline void** vtable_of(void* com_object) { return *reinterpret_cast<void***>(com_object); }

uint64_t fnv1a64(const void* data, size_t size);
