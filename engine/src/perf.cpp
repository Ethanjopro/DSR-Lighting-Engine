#include "perf.h"
#include "common.h"
#include <algorithm>

namespace {

constexpr int kFramesInFlight = 4;
constexpr int kReportEvery = 300;

struct FrameQueries {
    ID3D11Query* disjoint = nullptr;
    ID3D11Query* start = nullptr;
    ID3D11Query* end = nullptr;
    bool open = false;
    bool pending = false;
};

FrameQueries g_frames[kFramesInFlight];
int g_index;
double g_gpu_sum, g_gpu_max;
int g_gpu_count;
LARGE_INTEGER g_freq, g_last;
double g_cpu_sum, g_cpu_max;
int g_cpu_count;
bool g_ready;

// Non-blocking: results come back a few frames late, or the frame is dropped from the average.
void collect(ID3D11DeviceContext* ctx, FrameQueries& f) {
    if (!f.pending) return;
    D3D11_QUERY_DATA_TIMESTAMP_DISJOINT disjoint;
    UINT64 start, end;
    if (ctx->GetData(f.disjoint, &disjoint, sizeof(disjoint), D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK) return;
    if (ctx->GetData(f.start, &start, sizeof(start), D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK) return;
    if (ctx->GetData(f.end, &end, sizeof(end), D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK) return;
    f.pending = false;
    if (disjoint.Disjoint || disjoint.Frequency == 0) return;
    const double ms = static_cast<double>(end - start) * 1000.0 / static_cast<double>(disjoint.Frequency);
    g_gpu_sum += ms;
    g_gpu_max = std::max(g_gpu_max, ms);
    g_gpu_count++;
}

}  // namespace

void perf_init(ID3D11Device* device) {
    QueryPerformanceFrequency(&g_freq);
    QueryPerformanceCounter(&g_last);
    D3D11_QUERY_DESC disjoint{D3D11_QUERY_TIMESTAMP_DISJOINT, 0};
    D3D11_QUERY_DESC stamp{D3D11_QUERY_TIMESTAMP, 0};
    for (FrameQueries& f : g_frames) {
        if (FAILED(device->CreateQuery(&disjoint, &f.disjoint))) return;
        if (FAILED(device->CreateQuery(&stamp, &f.start))) return;
        if (FAILED(device->CreateQuery(&stamp, &f.end))) return;
    }
    g_ready = true;
}

void perf_before_present(ID3D11DeviceContext* immediate) {
    if (!g_ready) return;
    FrameQueries& f = g_frames[g_index];
    if (f.open) {
        immediate->End(f.end);
        immediate->End(f.disjoint);
        f.open = false;
        f.pending = true;
    }
}

void perf_after_present(ID3D11DeviceContext* immediate) {
    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);
    const double frame_ms = static_cast<double>(now.QuadPart - g_last.QuadPart) * 1000.0 / static_cast<double>(g_freq.QuadPart);
    g_last = now;
    g_cpu_sum += frame_ms;
    g_cpu_max = std::max(g_cpu_max, frame_ms);
    if (++g_cpu_count >= kReportEvery) {
        logf("perf: frame %.2f ms avg, %.1f max; gpu %.2f ms avg, %.2f max (%d samples)", g_cpu_sum / g_cpu_count, g_cpu_max,
             g_gpu_count ? g_gpu_sum / g_gpu_count : 0.0, g_gpu_max, g_gpu_count);
        g_cpu_sum = g_cpu_max = g_gpu_sum = g_gpu_max = 0.0;
        g_cpu_count = g_gpu_count = 0;
    }
    if (!g_ready) return;
    for (FrameQueries& f : g_frames) collect(immediate, f);
    g_index = (g_index + 1) % kFramesInFlight;
    FrameQueries& f = g_frames[g_index];
    if (f.pending) return;  // not back yet: skip this frame rather than reuse a query in flight
    immediate->Begin(f.disjoint);
    immediate->End(f.start);
    f.open = true;
}
