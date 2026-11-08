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
// Content occupancy mask for content-responsive rain drops.
//
// Side pipeline stage 2 of 2. Runs over the raw premultiplied source -- which
// the host resamples to this pass's output size (0.5 of the capture), still
// pre-light and pre-lens -- and marks every pixel whose colour or opacity
// departs from the cached background reference. The output is rgba8 at 0.5
// capture scale: R occupancy in [0, 1], A 1. G and B carry a diagnostic
// side-channel (the native preserve materialization premultiplies RGB, and the
// payload must not live in the alpha metadata channel): G is the producer
// reason code -- 0 never written, 0.2 degenerate opening, 0.4 outside opening,
// 0.6 invalid reference (no computed marker), 0.8 opening smaller than a texel, 1
// occupancy computed -- and B is the raw premultiplied source alpha at
// effect.texture_uv on a reject, or at the centre tap (i = j = 0) of the 3x3
// occupancy loop on the computed path. Consumers that only need occupancy read
// R; the codes let a zero-G texel be told apart from an executed 0-R reject.
// There is no phase uniform, so the mask cache is
// never invalidated by the animation loop; only real content changes rebuild.
//
// The reference lives in the rain-content-background state as two fixed
// half-width fields: the left half carries the straight medoid reference RGB
// (A = 1), the right half carries the producer's reference payload: R is the
// winning sample's alpha, G is the computed marker (completed 1..1.5, reject
// 0/0.2/0.4). It is
// read at fixed normalized UVs -- (0.25, 0.5) for RGB, (0.75, 0.5) for the
// payload -- independent of the state's actual dimensions, DPI and rounding;
// no background size is derived here. On a valid opening the state is at
// least 7 texels wide, so the fixed UVs never blend the half boundary.
// Reference validity is the computed G marker, never the payload's RGB or
// alpha: a computed fully transparent black reference (R = 0, G = 1) is valid,
// while an unwritten or rejected state is invalid regardless of its colour.
//
// Comparison is contrast in any colour: max absolute RGB channel delta plus
// the absolute alpha difference against the reference, combined through
// smooth thresholds RGB 0.015..0.045 and alpha 0.015..0.06. The thresholds
// are deliberately low because the input is premultiplied and downsampled to
// half resolution: antialiased thin strokes and glyph edges attenuate on the
// way in. Interior pixels of flat filled cards differ from the flat reference
// exactly like text does -- the comparison is against the reference colour
// itself, not against local gradients -- so filled areas occupy solidly and
// edge-detection failure modes do not apply.
//
// Coverage is centre plus a bounded 3x3 max neighbourhood. The neighbour
// step is 2 logical px (converted to texel units through the frame scale), so
// the dilation radius is exactly 2 logical px at any DPI or render scale, and
// neighbour taps are clamped to the opening so frame pixels never trigger.
//
// Limitations (by contract, reported not hidden): the reference comes from
// nine fixed perimeter probes at 1/8 capture resolution, so a UI that is
// fully opaque and covers every probe with differing content can skew the
// reference, and sub-0.015 low-contrast content is invisible by design.
// There is no lamplight or backdrop reference here: only the raw capture and
// the cached background state.
//
// Geometry is in logical px relative to the frame's top-left, exactly the
// rain-light/rain-lens convention: scale comes from the captured frame rect,
// never from an assumed physical == logical identity, so the mapping is
// correct at any render scale or DPR. Outside the opening, on a degenerate
// opening or on an invalid reference the occupancy is 0.

uniform vec2 logical_size; // root logical size
uniform float border_px;   // reserved native border width (26)

uniform sampler2D content_background; // rain-content-background state

// Safe unpremultiply, twin of rl_straight in rain-lens.frag.
vec4 rcm_straight(vec4 pm) {
    if (pm.a <= 0.0) {
        return vec4(0.0);
    }
    return vec4(min(pm.rgb / pm.a, vec3(1.0)), pm.a);
}

// Occupancy contribution of one premultiplied sample against the reference.
float rcm_delta(vec4 pm, vec3 refRGB, float refA) {
    vec4 s = rcm_straight(pm);
    float rgbDelta = max(max(abs(s.r - refRGB.r), abs(s.g - refRGB.g)),
                         abs(s.b - refRGB.b));
    float aDelta = abs(s.a - refA);
    return max(smoothstep(0.015, 0.045, rgbDelta),
               smoothstep(0.015, 0.060, aDelta));
}

vec4 shader_main(EffectContext effect) {
    // Cheap reject first: outside the client opening or a degenerate opening
    // is always occupancy 0, before any sampling.
    vec2 logical = max(logical_size, vec2(1.0));
    vec2 framePx = max(effect.frame_rect_px.zw, vec2(1.0));
    vec2 scale = framePx / logical;
    vec2 frameOrigin = effect.frame_rect_px.xy;
    vec2 p = effect_frame_px(effect) / scale;

    float border = max(border_px, 0.0);
    float x0 = border;
    float x1 = logical.x - border;
    float y0 = border;
    float y1 = logical.y - border;
    if (x1 - x0 <= 8.0 || y1 - y0 <= 8.0) {
        return vec4(0.0, 0.2, texture2D(tex, effect.texture_uv).a, 1.0); // degenerate opening
    }
    if (p.x < x0 || p.x > x1 || p.y < y0 || p.y > y1) {
        return vec4(0.0, 0.4, texture2D(tex, effect.texture_uv).a, 1.0); // frame, chrome, outside
    }

    // Reference: fixed normalized UVs into the background state's two
    // half-fields -- (0.25, 0.5) for the medoid RGB, (0.75, 0.5) for the
    // producer's reference payload. Dimension-independent by construction;
    // no derived background size that rounding could break.
    vec3 refRGB = texture2D(content_background, vec2(0.25, 0.5)).rgb;
    vec4 referenceData = texture2D(content_background, vec2(0.75, 0.5));
    float refA = referenceData.r;
    // G >= 1 marks a completed reference, including transparent black.
    // Cleared G=0 and producer rejects G=0.2/0.4 are invalid; payload
    // colour and alpha are not validity sentinels.
    if (referenceData.g < 1.0) {
        return vec4(0.0, 0.6, texture2D(tex, effect.texture_uv).a, 1.0);
    }

    // Occupancy: max over the neighbourhood, any-colour contrast. The
    // neighbour step is 2 logical px converted to this pass's texel units via
    // the frame scale -- `vec2(2.0) * scale / effect.texture_size_px` -- so
    // the dilation radius stays 2 logical px across DPI and render scales.
    // Bounded 3x3, nine taps total. Neighbour UVs are clamped to the opening
    // with a half-texel inset so a tap can never land on frame chrome and
    // trigger false occupancy.
    vec2 halfTexel = vec2(0.5) / max(effect.texture_size_px, vec2(1.0));
    vec2 stepUV = vec2(2.0) * scale / effect.texture_size_px;
    vec2 openLo = (frameOrigin + vec2(x0, y0) * scale) / effect.texture_size_px + halfTexel;
    vec2 openHi = (frameOrigin + vec2(x1, y1) * scale) / effect.texture_size_px - halfTexel;
    openLo = max(openLo, halfTexel);
    openHi = min(openHi, vec2(1.0) - halfTexel);
    if (openLo.x >= openHi.x || openLo.y >= openHi.y) {
        return vec4(0.0, 0.8, texture2D(tex, effect.texture_uv).a, 1.0); // opening smaller than a texel
    }
    float occ = 0.0;
    float rawAlpha = 0.0;
    for (int j = -1; j <= 1; j++) {
        for (int i = -1; i <= 1; i++) {
            vec2 uv = effect.texture_uv + vec2(float(i), float(j)) * stepUV;
            uv = clamp(uv, openLo, openHi);
            vec4 pm = texture2D(tex, uv);
            if (i == 0 && j == 0) {
                rawAlpha = pm.a;
            }
            occ = max(occ, rcm_delta(pm, refRGB, refA));
        }
    }

    // rgba8: R occupancy, G reason 1 (computed), B centre raw alpha, A 1
    // (never put payload in alpha).
    return vec4(clamp(occ, 0.0, 1.0), 1.0, rawAlpha, 1.0);
}

void main() {
    vec2 uv = vec2(gl_FragCoord.x / weather_size.x, 1.0 - gl_FragCoord.y / weather_size.y);
    EffectContext e = EffectContext(uv, weather_size, vec4(0.0, 0.0, weather_size), vec4(0.0, 0.0, weather_size));
    vec4 result = shader_main(e);
    weather_output = result;
}
