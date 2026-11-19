// A notice: one line of text in a 5 x 7 dot font, in a dark box, over the finished frame. osd.cpp sends the dots of
// each character, so there is no font here.

cbuffer Notice : register(b0) {
    float4 g_rect;       // x, y of the text's top left in pixels, z the size of a dot in pixels, w characters
    float4 g_colour;     // x a lit dot's white, y the box's alpha, z the fade, w 1 when the target is linear
    uint4 g_glyph[32];   // two characters each: rows 0-3 and 4-6 of the first, then of the second, five bits a row
};

static const float MARGIN = 4.0f;  // dots of box around the text

float4 fullscreen_vs(uint id : SV_VertexID) : SV_Position {
    float2 uv = float2((id << 1) & 2, id & 2);
    return float4(uv.x * 2.0f - 1.0f, 1.0f - uv.y * 2.0f, 0.0f, 1.0f);
}

float4 main(float4 pos : SV_Position) : SV_Target0 {
    const float2 p = (pos.xy - g_rect.xy) / g_rect.z;  // in dots
    const float2 extent = float2(g_rect.w * 6.0f - 1.0f, 7.0f);
    if (any(p < -MARGIN) || any(p >= extent + MARGIN)) discard;
    float lit = 0.0f;
    if (all(p >= 0.0f) && all(p < extent)) {
        const uint column = (uint)p.x, row = (uint)p.y;
        const uint index = column / 6, x = column % 6;
        if (x < 5) {
            const uint4 pair = g_glyph[index >> 1];
            const uint2 rows = (index & 1) ? pair.zw : pair.xy;
            const uint bits = row < 4 ? rows.x >> (row * 5) : rows.y >> ((row - 4) * 5);
            lit = (float)((bits >> (4 - x)) & 1);
        }
    }
    // Off-white on near black. In a linear target the same greys, at the brightness the UI has there.
    float3 colour = lerp(0.02f, 0.92f, lit);
    if (g_colour.w > 0.5f) colour = pow(colour, 2.2f);
    return float4(colour * g_colour.x, lerp(g_colour.y, 1.0f, lit) * g_colour.z);
}
