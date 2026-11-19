#include "osd.h"
#include "common.h"
#include "config.h"
#include "hlsl.h"
#include "state.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <mutex>

namespace {

constexpr int kMaxChars = 64;
constexpr float kFadeSeconds = 0.5f;

// 5 x 7 dots a character, seven rows of five from the top. Drawn in upper case.
struct Glyph {
    char c;
    const char* rows;
};
const Glyph kFont[] = {
    {'A', ".###.#...##...#######...##...##...#"}, {'B', "####.#...##...#####.#...##...#####."},
    {'C', ".###.#...##....#....#....#...#.###."}, {'D', "####.#...##...##...##...##...#####."},
    {'E', "######....#....####.#....#....#####"}, {'F', "######....#....####.#....#....#...."},
    {'G', ".###.#...##....#.####...##...#.###."}, {'H', "#...##...##...#######...##...##...#"},
    {'I', ".###...#....#....#....#....#...###."}, {'J', "..###...#....#....#....#.#..#..##.."},
    {'K', "#...##..#.#.#..##...#.#..#..#.#...#"}, {'L', "#....#....#....#....#....#....#####"},
    {'M', "#...###.###.#.##.#.##...##...##...#"}, {'N', "#...###..##.#.##..###...##...##...#"},
    {'O', ".###.#...##...##...##...##...#.###."}, {'P', "####.#...##...#####.#....#....#...."},
    {'Q', ".###.#...##...##...##.#.##..#..##.#"}, {'R', "####.#...##...#####.#.#..#..#.#...#"},
    {'S', ".#####....#.....###.....#....#####."}, {'T', "#####..#....#....#....#....#....#.."},
    {'U', "#...##...##...##...##...##...#.###."}, {'V', "#...##...##...##...##...#.#.#...#.."},
    {'W', "#...##...##...##.#.##.#.###.###...#"}, {'X', "#...##...#.#.#...#...#.#.#...##...#"},
    {'Y', "#...##...#.#.#...#....#....#....#.."}, {'Z', "#####....#...#...#...#...#....#####"},
    {'0', ".###.#...##..###.#.###..##...#.###."}, {'1', "..#...##....#....#....#....#...###."},
    {'2', ".###.#...#....#...#...#...#...#####"}, {'3', "####.....#....#.###.....#....#####."},
    {'4', "...#...##..#.#.#..#.#####...#....#."}, {'5', "######....####.....#....##...#.###."},
    {'6', ".###.#....#....####.#...##...#.###."}, {'7', "#####....#...#...#...#....#....#..."},
    {'8', ".###.#...##...#.###.#...##...#.###."}, {'9', ".###.#...##...#.####....#....#.###."},
    {'.', ".........................##...##..."}, {',', "....................##....#...#...."},
    {':', "......##...##........##...##......."}, {'-', "...............#####..............."},
    {'/', "....#....#...#...#...#...#....#...."}, {'%', "##..###..#...#...#...#...#..###..##"},
    {'(', "...#...#...#....#....#.....#.....#."}, {')', ".#.....#.....#....#....#...#...#..."},
    {'+', "......#....#..#####..#....#........"}, {'!', "..#....#....#....#....#.........#.."},
    {'=', "..........#####.....#####.........."}, {'?', ".###.#...#....#...#...#.........#.."},
    {'\'', "..#....#...#......................."},
};

// Must match osd.hlsl.
struct Constants {
    float rect[4];    // x, y of the text's top left in pixels, z the size of a dot in pixels, w characters
    float colour[4];  // a lit dot's white, the box's alpha, the fade, and whether the target is linear
    uint32_t glyph[kMaxChars / 2][4];  // two characters each: rows 0-3 and 4-6 of the first, then of the second
};
static_assert(sizeof(Constants) % 16 == 0);

ID3D11Device* g_device;
ID3D11Buffer* g_cb;
ID3D11BlendState* g_blend;
ID3D11RasterizerState* g_raster;
ID3D11DepthStencilState* g_no_depth;
bool g_ready;

std::mutex g_lock;  // the text, set from wherever something happens
char g_text[kMaxChars + 1];
float g_seconds;
bool g_fresh;       // the clock starts at the first frame the notice is drawn in
LARGE_INTEGER g_start, g_freq;

void glyph_bits(char c, uint32_t out[2]) {
    out[0] = out[1] = 0;
    if (c >= 'a' && c <= 'z') c = static_cast<char>(c - 'a' + 'A');
    if (c == ' ') return;
    const Glyph* found = nullptr;
    for (const Glyph& g : kFont) {
        if (g.c == c) found = &g;
    }
    // What is not in the font.
    for (const Glyph& g : kFont) {
        if (!found && g.c == '?') found = &g;
    }
    if (!found) return;
    for (int row = 0; row < 7; row++) {
        uint32_t bits = 0;
        for (int x = 0; x < 5; x++) bits = bits << 1 | (found->rows[row * 5 + x] == '#' ? 1u : 0u);
        if (row < 4) {
            out[0] |= bits << (row * 5);
        } else {
            out[1] |= bits << ((row - 4) * 5);
        }
    }
}

bool create_resources() {
    D3D11_BUFFER_DESC b{};
    b.ByteWidth = sizeof(Constants);
    b.Usage = D3D11_USAGE_DYNAMIC;
    b.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    b.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    if (FAILED(g_device->CreateBuffer(&b, nullptr, &g_cb))) return false;

    D3D11_BLEND_DESC bl{};
    bl.RenderTarget[0].BlendEnable = TRUE;
    bl.RenderTarget[0].SrcBlend = D3D11_BLEND_SRC_ALPHA;
    bl.RenderTarget[0].DestBlend = D3D11_BLEND_INV_SRC_ALPHA;
    bl.RenderTarget[0].BlendOp = D3D11_BLEND_OP_ADD;
    bl.RenderTarget[0].SrcBlendAlpha = D3D11_BLEND_ZERO;
    bl.RenderTarget[0].DestBlendAlpha = D3D11_BLEND_ONE;
    bl.RenderTarget[0].BlendOpAlpha = D3D11_BLEND_OP_ADD;
    bl.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_RED | D3D11_COLOR_WRITE_ENABLE_GREEN | D3D11_COLOR_WRITE_ENABLE_BLUE;
    if (FAILED(g_device->CreateBlendState(&bl, &g_blend))) return false;

    D3D11_DEPTH_STENCIL_DESC d{};
    d.DepthEnable = FALSE;
    d.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ZERO;
    d.DepthFunc = D3D11_COMPARISON_ALWAYS;
    if (FAILED(g_device->CreateDepthStencilState(&d, &g_no_depth))) return false;

    D3D11_RASTERIZER_DESC r{};
    r.FillMode = D3D11_FILL_SOLID;
    r.CullMode = D3D11_CULL_NONE;
    r.DepthClipEnable = TRUE;
    return SUCCEEDED(g_device->CreateRasterizerState(&r, &g_raster));
}

}  // namespace

void osd_init(ID3D11Device* device) {
    g_device = device;
    QueryPerformanceFrequency(&g_freq);
    g_ready = create_resources();
    if (!g_ready) logf("notices: resources failed, none will be drawn");
}

void osd_show(const char* text, float seconds) {
    std::lock_guard<std::mutex> lock(g_lock);
    strncpy_s(g_text, text, _TRUNCATE);
    g_seconds = seconds;
    g_fresh = true;
}

void osd_draw(IDXGISwapChain* swapchain, ID3D11DeviceContext* immediate, float white, bool linear) {
    char text[kMaxChars + 1];
    float fade;
    {
        std::lock_guard<std::mutex> lock(g_lock);
        if (!g_text[0]) return;
        LARGE_INTEGER now;
        QueryPerformanceCounter(&now);
        if (g_fresh) {
            g_start = now;
            g_fresh = false;
        }
        const float left = g_seconds - static_cast<float>(now.QuadPart - g_start.QuadPart) / static_cast<float>(g_freq.QuadPart);
        if (left <= 0.0f) {
            g_text[0] = 0;
            return;
        }
        fade = std::min(left / kFadeSeconds, 1.0f);
        memcpy(text, g_text, sizeof(text));
    }
    if (!g_ready || !cfg_bool("engine.notices", true)) return;
    ID3D11VertexShader* vs = hlsl_vs("osd.hlsl", "fullscreen_vs");
    ID3D11PixelShader* ps = hlsl_ps("osd.hlsl", "main");
    if (!vs || !ps) return;

    ID3D11Texture2D* back = nullptr;
    if (FAILED(swapchain->GetBuffer(0, IID_PPV_ARGS(&back)))) return;
    D3D11_TEXTURE2D_DESC desc;
    back->GetDesc(&desc);
    ID3D11RenderTargetView* rtv = nullptr;
    const HRESULT hr = g_device->CreateRenderTargetView(back, nullptr, &rtv);
    back->Release();
    if (FAILED(hr)) return;

    const int count = static_cast<int>(strlen(text));
    Constants c{};
    // Three pixels a dot at 1080 lines, six at 2160: a 64-character line then takes six tenths of a 16:9 screen.
    const float dot = std::max(2.0f, std::round(static_cast<float>(desc.Height) / 360.0f));
    c.rect[0] = std::floor((static_cast<float>(desc.Width) - (static_cast<float>(count) * 6.0f - 1.0f) * dot) * 0.5f);
    c.rect[1] = std::floor(static_cast<float>(desc.Height) * 0.045f);
    c.rect[2] = dot;
    c.rect[3] = static_cast<float>(count);
    c.colour[0] = white;
    c.colour[1] = 0.62f;
    c.colour[2] = fade;
    c.colour[3] = linear ? 1.0f : 0.0f;
    for (int i = 0; i < count; i++) glyph_bits(text[i], &c.glyph[i / 2][(i & 1) * 2]);

    D3D11_MAPPED_SUBRESOURCE mapped;
    if (SUCCEEDED(immediate->Map(g_cb, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) {
        memcpy(mapped.pData, &c, sizeof(c));
        immediate->Unmap(g_cb, 0);
        StateBackup backup(immediate);
        unbind_all(immediate);
        const D3D11_VIEWPORT vp{0.0f, 0.0f, static_cast<float>(desc.Width), static_cast<float>(desc.Height), 0.0f, 1.0f};
        const FLOAT factor[4] = {};
        immediate->OMSetRenderTargets(1, &rtv, nullptr);
        immediate->OMSetBlendState(g_blend, factor, 0xFFFFFFFFu);
        immediate->OMSetDepthStencilState(g_no_depth, 0);
        immediate->RSSetState(g_raster);
        immediate->RSSetViewports(1, &vp);
        immediate->IASetInputLayout(nullptr);
        immediate->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        immediate->VSSetShader(vs, nullptr, 0);
        immediate->GSSetShader(nullptr, nullptr, 0);
        immediate->PSSetShader(ps, nullptr, 0);
        immediate->PSSetConstantBuffers(0, 1, &g_cb);
        immediate->Draw(3, 0);
    }
    rtv->Release();
}
