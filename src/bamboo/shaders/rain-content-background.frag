#version 320 es
precision highp float;
precision highp int;
precision highp sampler2D;
uniform sampler2D tex;
uniform vec2 weather_size;
layout(location=0) out vec4 weather_output;
// Source geometry is top-left. All framebuffer textures use OpenGL bottom-left.
vec4 weather_sample(sampler2D s, vec2 uv) {
    return texture(s, vec2(uv.x, 1.0 - uv.y));
}
#define texture2D weather_sample
struct EffectContext {
    vec2 texture_uv;
    vec2 texture_size_px;
    vec4 content_rect_px;
    vec4 frame_rect_px;
};
vec2 effect_frame_px(EffectContext e) {
    return e.texture_uv * e.texture_size_px - e.frame_rect_px.xy;
}
// Cached background reference for content-responsive rain drops.
//
// Side pipeline stage 1 of 2 (this file, then rain-content-mask.frag). It
// runs once per capture over the raw premultiplied source via the stage's
// primary input texture. It writes a tiny
// rgba16f state that encodes the reference as two fixed half-width fields:
// the LEFT half of every row carries the straight (unpremultiplied) medoid
// reference RGB with A = 1, the RIGHT half carries that same winning
// sample's alpha in R, a producer status in G and the nine-probe maximum
// alpha in B, all with A = 1. The right-field G is 0 where the producer never
// wrote (a cleared state texel), 0.2 on an out-of-range texture UV, 0.4 on a
// degenerate opening and 1 + 0.5 * center alpha on the computed path (the
// opening-centre raw input alpha, so G in [1, 1.5]); B is the maximum alpha
// over the nine probes on the computed path and 0 on every reject, so a
// genuinely black computed reference is told apart from an unwritten or
// rejected field, and any non-zero probe alpha is told apart from an
// all-probes-zero opening. The
// left half stays zero RGB on every reject. Consumers read fixed normalized
// UVs -- (0.25, 0.5) for RGB, (0.75, 0.5) for the right field -- which is
// independent of the state's actual dimensions, DPI and rounding: no size
// derivation, no two-record index math. On a valid opening the state is at
// least 7 texels wide, so those fixed UVs never blend across the vertical
// half boundary.
// All outputs carry A = 1: the native preserve materialization premultiplies
// RGB by alpha and the payload must never live in, or be scaled by, the
// metadata channel.
//
// The reference is chosen from a fixed set of nine probes distributed along
// the opening's perimeter (3 logical px inset): the four corners, the left
// and right edge midpoints, the top quarter and three-quarter points and the
// bottom midpoint. Every output pixel of this tiny cached state (0.125 of
// the capture, rebuilt only on content damage) runs the same selection:
// probes are unpremultiplied safely, candidates within 0.015 of the minimum
// alpha are eligible, and the RGB medoid of the eligible set wins -- RGB
// *and* alpha are taken from that one winning sample, so opaque glyph probes
// can never leak into the reference alpha. The per-pixel cost is nine small
// fetches over 1/64 of the frame and is bounded by the cache. Minimum
// alpha plus medoid makes the reference robust against the occasional glyph,
// icon, border line or small filled element that happens to land on a probe,
// and it never assumes a hue, luminance or theme colour -- it tracks any
// content, any colour scheme.
//
// Limitation: the nine probes are fixed perimeter points. A UI that is fully
// opaque and covers every probe with differing content (a full-window image,
// a maximised card with per-probe colours) can distort the reference; the
// medoid bounds the damage but cannot eliminate it. The mask stage compares
// against this single reference, so such layouts are tolerated, not solved.
//
// Geometry is in logical px relative to the frame's top-left, exactly the
// convention of rain-light/rain-lens: scale comes from the captured frame
// rect, never from an assumed physical == logical identity, so the mapping
// is correct at any render scale or DPR. Raw samples are clamped inside the
// capture with a half-texel inset so no probe can read the frame exterior.

uniform vec2 logical_size; // root logical size
uniform float border_px;   // reserved native border width (26)

// Safe unpremultiply, twin of rl_straight in rain-lens.frag: zero alpha stays
// zero and quantized overshoot can never exceed 1.
vec4 rcb_straight(vec4 pm) {
    if (pm.a <= 0.0) {
        return vec4(0.0);
    }
    return vec4(min(pm.rgb / pm.a, vec3(1.0)), pm.a);
}

vec4 shader_main(EffectContext effect) {
    // Cheap reject first: a state texel whose UV falls outside the texture
    // footprint has no meaningful reference. The left half stays zero RGB and
    // the right half records status G = 0.2. (No texel-index reject: every
    // in-range state pixel computes the reference, because the half-field
    // layout must be uniform across the whole state.)
    vec2 outSize = max(effect.texture_size_px, vec2(1.0));
    vec2 halfTexel = vec2(0.5) / outSize;
    vec2 uvIn = clamp(effect.texture_uv, halfTexel, vec2(1.0) - halfTexel);
    if (uvIn != effect.texture_uv) {
        // Out-of-range state pixel: right-half status G = 0.2 (B = 0); the
        // left half stays zero RGB, both with A = 1.
        return (effect.texture_uv.x < 0.5)
            ? vec4(0.0, 0.0, 0.0, 1.0)
            : vec4(0.0, 0.2, 0.0, 1.0);
    }

    vec2 logical = max(logical_size, vec2(1.0));
    vec2 framePx = max(effect.frame_rect_px.zw, vec2(1.0));
    vec2 scale = framePx / logical;
    vec2 frameOrigin = effect.frame_rect_px.xy;

    float border = max(border_px, 0.0);
    float x0 = border;
    float x1 = logical.x - border;
    float y0 = border;
    float y1 = logical.y - border;

    // Degenerate opening: the reference is meaningless; left half zero RGB,
    // right-half status G = 0.4 (B = 0). The mask stage reports zero
    // occupancy for degenerate openings.
    if (x1 - x0 <= 8.0 || y1 - y0 <= 8.0) {
        return (effect.texture_uv.x < 0.5)
            ? vec4(0.0, 0.0, 0.0, 1.0)
            : vec4(0.0, 0.4, 0.0, 1.0);
    }

    // Logical -> capture UV. The raw capture arrives as the stage's primary
    // input and is premultiplied and resampled by the host to this pass's
    // output size (0.125 of the capture), so normalized UV geometry is
    // unchanged but the effective resolution is the target size: clamp a
    // half *target* texel inside so a probe can never touch the frame
    // edge. Probes are read at 1/8 capture resolution -- thin detail may
    // attenuate, which the medoid and the mask stage's low thresholds absorb.
    vec2 probe[9];
    float inset = 3.0; // logical px inside the opening
    vec2 lo = vec2(x0 + inset, y0 + inset);
    vec2 hi = vec2(x1 - inset, y1 - inset);
    probe[0] = vec2(lo.x, lo.y);              // top-left corner
    probe[1] = vec2(hi.x, lo.y);              // top-right corner
    probe[2] = vec2(lo.x, hi.y);              // bottom-left corner
    probe[3] = vec2(hi.x, hi.y);              // bottom-right corner
    probe[4] = vec2(lo.x, (lo.y + hi.y) * 0.5); // left edge midpoint
    probe[5] = vec2(hi.x, (lo.y + hi.y) * 0.5); // right edge midpoint
    probe[6] = vec2(lo.x + (hi.x - lo.x) * 0.25, lo.y); // top quarter
    probe[7] = vec2(lo.x + (hi.x - lo.x) * 0.75, lo.y); // top three-quarter
    probe[8] = vec2(lo.x + (hi.x - lo.x) * 0.5, hi.y);  // bottom midpoint

    vec4 straight[9];
    float alphaMin = 1.0;
    float alphaMax = 0.0;
    for (int i = 0; i < 9; i++) {
        vec2 uv = (frameOrigin + probe[i] * scale) / framePx;
        uv = clamp(uv, halfTexel, vec2(1.0) - halfTexel);
        straight[i] = rcb_straight(texture2D(tex, uv));
        alphaMin = min(alphaMin, straight[i].a);
        alphaMax = max(alphaMax, straight[i].a);
    }

    // Opening-centre diagnostic: one extra cached fetch of the raw capture at
    // the midpoint of the opening box, using the same coordinate mapping and
    // half-texel clamp as the probes. The alpha is read directly
    // (no straight conversion needed: alpha is premultiply-invariant), so the
    // consumer can separate a fully transparent opening centre from an all-
    // probes-zero perimeter.
    vec2 centerUV = (frameOrigin + (0.5 * (lo + hi)) * scale) / framePx;
    centerUV = clamp(centerUV, halfTexel, vec2(1.0) - halfTexel);
    float centerAlpha = texture2D(tex, centerUV).a;

    // Medoid over the eligible probes: minimum eligible alpha plus the 0.015
    // tolerance, with the RGB and alpha taken from the same winning sample so
    // opaque probes never raise the reference.
    // GLSL ES 100 fragment-portable form: constant loop indices only, no
    // dynamic array indexing; the winner's RGB and alpha are tracked as
    // scalars through the loop. The result is identical for every state
    // pixel, so the left/right half fields are coherent by construction.
    vec3 bestRGB = vec3(0.0);
    float bestAlpha = 0.0;
    float bestSum = -1.0;
    for (int i = 0; i < 9; i++) {
        if (straight[i].a > alphaMin + 0.015) { continue; }
        float sum = 0.0;
        for (int j = 0; j < 9; j++) {
            if (straight[j].a <= alphaMin + 0.015) {
                sum += length(straight[i].rgb - straight[j].rgb);
            }
        }
        if (bestSum < 0.0 || sum < bestSum) {
            bestSum = sum;
            bestRGB = straight[i].rgb;
            bestAlpha = straight[i].a;
        }
    }

    // Half-field layout: left half = medoid RGB (A = 1), right half = medoid
    // alpha in R, status/centre alpha in G = 1 + 0.5 * centerAlpha (computed
    // range [1, 1.5]) and the nine-probe maximum alpha in B (A = 1). Fixed UV
    // boundary at x = 0.5; consumers read (0.25, 0.5) and (0.75, 0.5) and the
    // state is >= 7 texels wide on a valid opening, so no consumer tap can
    // blend the boundary.
    if (effect.texture_uv.x < 0.5) {
        return vec4(bestRGB, 1.0);
    }
    return vec4(bestAlpha, 1.0 + 0.5 * centerAlpha, alphaMax, 1.0);
}

void main() {
    vec2 uv = vec2(gl_FragCoord.x / weather_size.x, 1.0 - gl_FragCoord.y / weather_size.y);
    EffectContext e = EffectContext(uv, weather_size, vec4(0.0, 0.0, weather_size), vec4(0.0, 0.0, weather_size));
    vec4 result = shader_main(e);
    weather_output = result;
}
