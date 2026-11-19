// Notices: one line of text over the game for a few seconds. That the engine loaded, what the comparison key did, and
// why the engine turned itself off are otherwise only in dsrle.log.
//
// Settings in dsrle.ini. Live.
//   engine.notices = 1   0 = never draw one
#pragma once
#include <d3d11.h>
#include <dxgi.h>

void osd_init(ID3D11Device* device);
// Replaces the notice on screen. Upper case, digits and . , : - / % ( ) + ! = ? ' are drawn; at most 64 characters.
void osd_show(const char* text, float seconds);
// Before Present, on the swap chain's back buffer. white is the value of a lit dot's white there: 1 for an 8-bit
// buffer, paper white over 80 nits for scRGB, where linear is true.
void osd_draw(IDXGISwapChain* swapchain, ID3D11DeviceContext* immediate, float white, bool linear);
