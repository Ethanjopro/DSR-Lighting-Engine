// Frame timing for the log: GPU time from timestamp queries and CPU frame pacing, averaged over a few seconds.
#pragma once
#include <d3d11.h>

void perf_init(ID3D11Device* device);
// On the immediate context, either side of Present.
void perf_before_present(ID3D11DeviceContext* immediate);
void perf_after_present(ID3D11DeviceContext* immediate);
