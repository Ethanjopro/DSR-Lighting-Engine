// dsrle\dsrle.ini: flat "name = value" settings, re-read whenever the file changes. Sections are
// cosmetic ("[gtao]" followed by "radius = 2" gives the key "gtao.radius").
#pragma once

void config_load();
// Call once per frame. Returns true when the file changed and was reloaded this frame.
bool config_poll();

float cfg_float(const char* key, float fallback);
int cfg_int(const char* key, int fallback);
inline bool cfg_bool(const char* key, bool fallback) { return cfg_int(key, fallback ? 1 : 0) != 0; }
