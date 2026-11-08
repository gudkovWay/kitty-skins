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
// Local lamp light, the intermediate effect stage over Kitty's own captured
// content.
//
// This stage runs directly over the window's full captured source (client,
// frame and embedded subsurfaces), and adds the bounded warm illumination the
// two content lanterns cast onto the content near them: a finite elliptical
// near field around each pane that reaches exactly zero at its edge.
// It is the *intermediate* stage of the weather pipeline: rain-lens runs after
// it and unpremultiplies for the native final display wrapper, so this shader
// MUST return premultiplied RGB (light added as light * alpha) with the
// original alpha preserved. Returning straight RGB here would make rain-lens
// unpremultiply it twice. Dry paths return the source vec4 exactly, and the
// lit path re-fits under the unchanged alpha with min(rgb, alpha).
//
// Everything outside the protected content opening, outside both lamps' finite
// warm fields, on a degenerate opening, on a zero-alpha pixel or in the
// moonlit-ink profile is returned byte-for-byte from the source: the frame
// border, buttons, titlebar and quiet content are never tinted. There is no
// image sampler, no sprite overlay on glyphs, no displacement and no blur here
// -- only additive/reflected warm light on real content pixels.
//
// Geometry is in logical px relative to the frame's top-left. `border_px` is
// the reserved native border (26); the content opening is therefore
// (border, border) .. (logical - border). The two lamp boxes, panes and flicker
// are the exact shared definitions of ambient-content.frag: the same
// W = min(104, contentW*0.18, contentH*0.22), H = 2W, 12 px inset, pane UV
// (0.501479, 0.625633), upper-right + lower-left anchors and the same overlap
// rule that suppresses the lower lamp in a small opening. The light field
// originates at those panes, matching the backdrop sprites.

uniform float phase;       // caller-owned loop in [0, 1] over 96 s
uniform float composition; // 0 rainlit-shoji, 1 lantern-dusk, 2 moonlit-ink
uniform vec2 logical_size; // root logical size
uniform float border_px;   // reserved native border width (26)

const vec3 RL_WARM = vec3(0.961, 0.639, 0.239); // lantern amber #F5A33D
const vec3 RL_CORE = vec3(1.000, 0.862, 0.522); // lit pane, luminous yellow
const vec2 RL_PANE_UV = vec2(0.501479, 0.625633);

// Shared coherent lamp scatter field: one soft wide lobe plus a tighter warm
// core, both derived only from the offset from the pane, the direction toward
// the content centre and the lamp sprite box height. Byte-identical twin of
// the helper in ambient-content.frag -- keep the two copies in lockstep.
// Returns vec2(soft, core); both components are exactly zero outside the
// finite extent, so the caller can skip flicker and light maths on dry
// pixels.
vec2 ac_lamp_scatter(vec2 d, vec2 inward, float h) {
    vec2 extent = clamp(vec2(0.95 * h, 0.78 * h),
                        vec2(100.0, 90.0), vec2(220.0, 190.0));
    vec2 q = d / extent;
    float r2 = dot(q, q);
    if (r2 >= 1.0) {
        return vec2(0.0);
    }
    float outerfade = 1.0 - smoothstep(0.70, 1.0, sqrt(r2));
    float soft = exp(-2.3 * r2) * outerfade;
    vec2 inwardUnit = inward / max(length(inward), 0.001);
    vec2 dUnit = d / max(length(d), 0.001);
    float angular = 0.65 + 0.35 * smoothstep(-0.15, 0.75, dot(dUnit, inwardUnit));
    float directionweight = mix(1.0, angular,
                                smoothstep(0.0, max(0.18 * h, 4.0), length(d)));
    soft *= directionweight;
    vec2 coreq = d / max(vec2(0.18 * h, 0.16 * h), vec2(4.0));
    float core = exp(-2.0 * dot(coreq, coreq)) * outerfade;
    return vec2(soft, core);
}

// --- hash + flicker: byte-identical twins of ambient-content.frag ----------
// Keep these two definitions in lockstep with that file: the sprite pane
// emission and this local spill must brown out on exactly the same frames.
float ac_hash(float n) {
    n = fract(n * 0.1031 + 0.11369);
    n *= n + 34.345;
    n *= n + n;
    return fract(n);
}

// Shared failing-bulb flicker. Eight 12 s slots per 96 s loop, each with a
// hashed onset at 3 + 2*hash(slot mod 8) s, so consecutive onsets are 10..14 s
// apart. Inside a short 0.45..0.9 s window the intensity dips to 0.18 through
// two or three uneven gaussian brownouts; between events it is exactly 1.
// `lampID` (0 top-right, 1 bottom-left) shifts the slot clock by 3.225 slots
// -- 2.7 s into the sub-slot -- and seeds a distinct hash identity, so the two
// lamps have separate event schedules. Slot identity is keyed mod 8, so the
// function is seamless at the 96 s wrap. No sine breathing: outside an event
// the lamp is perfectly steady.
float flicker_intensity(float phase, float lampID) {
    float slotf = phase * 8.0 + lampID * 3.225; // 2.7 s sub-slot offset
    float slot = floor(slotf);
    float t = fract(slotf) * 12.0;            // seconds into the slot
    float id = mod(slot, 8.0);                // seamless at the 96 s wrap
    float seed = id + lampID * 41.7;          // distinct hash identity
    float onset = 3.0 + 2.0 * ac_hash(seed);  // [3, 5] s
    float dur = mix(0.45, 0.9, ac_hash(seed + 5.3));
    float u = (t - onset) / max(dur, 0.0001); // event occupies u in [0, 1]
    if (u <= 0.0 || u >= 1.0) {
        return 1.0;
    }
    float n = ac_hash(seed + 11.7) < 0.5 ? 2.0 : 3.0;  // double or triple dip
    float dip = 0.0;
    for (int i = 0; i < 3; i++) {
        if (float(i) < n) {
            float h = ac_hash(seed + float(i) * 7.1 + 3.3);
            float c = (float(i) + 0.5) / n;
            float w = (0.62 / n) * mix(0.65, 1.35, h);
            float d = (u - c) / max(w, 0.0001);
            dip = max(dip, exp(-d * d));
        }
    }
    float envelope = smoothstep(0.0, 0.08, u) * (1.0 - smoothstep(0.92, 1.0, u));
    return mix(1.0, 0.18, clamp(dip * envelope, 0.0, 1.0));
}

vec4 shader_main(EffectContext effect) {
    // Source is premultiplied; every dry path returns this vec4 exactly.
    vec4 src = texture2D(tex, effect.texture_uv);

    // Moonlit-ink (profile 2) never carries the lamp: exact passthrough. The
    // same holds for a fully transparent pixel.
    if (composition > 1.5) {
        return src;
    }
    if (src.a <= 0.0) {
        return src;
    }

    // Physical -> logical: the frame rect gives the scale, so the lamp keeps
    // its real physical size at any render scale or DPR.
    vec2 logical = max(logical_size, vec2(1.0));
    vec2 framePx = max(effect.frame_rect_px.zw, vec2(1.0));
    vec2 scale = framePx / logical;
    vec2 p = effect_frame_px(effect) / scale;

    float border = max(border_px, 0.0);
    float x0 = border;
    float x1 = logical.x - border;
    float y0 = border;
    float y1 = logical.y - border;

    // Degenerate opening: unchanged. Outside the client opening (frame,
    // titlebar, buttons): unchanged.
    if (x1 - x0 <= 1.0 || y1 - y0 <= 1.0) {
        return src;
    }
    if (p.x < x0 || p.x > x1 || p.y < y0 || p.y > y1) {
        return src;
    }

    // Content opening in content-space logical px, plus the fragment in that
    // space.
    vec2 content = vec2(x1 - x0, y1 - y0);
    vec2 cp = vec2(p.x - x0, p.y - y0);

    // The lower-left lamp exists only while the opening holds both complete
    // boxes (12px inset each side) without overlap -- the exact rule of
    // ambient-content.frag.
    float w = min(104.0, min(content.x * 0.18, content.y * 0.22));
    float h = 2.0 * w;
    bool boxesFit = content.x >= 24.0 + w && content.y >= 24.0 + h;
    bool disjoint = content.x >= 24.0 + 2.0 * w || content.y >= 24.0 + 2.0 * h;
    float pair = (boxesFit && disjoint) ? 1.0 : 0.0;

    // At most two lamp evaluations, with no additional source samples.
    vec3 light = vec3(0.0);
    float fielded = 0.0;
    for (int i = 0; i < 2; i++) {
        float lampID = float(i);
        float gate = (i == 1) ? pair : 1.0;
        // The exact shared box/pane placement of ambient-content.frag, so the
        // cast field originates at the backdrop sprites.
        vec2 boxMin = vec2(
            mix(content.x - 12.0 - w, 12.0, lampID),
            mix(12.0, content.y - 12.0 - h, lampID));
        vec2 pane = boxMin + RL_PANE_UV * vec2(w, h);

        // Coherent near field from the shared scatter helper: finite, derived
        // from the actual box height, and exactly zero outside its extent, so
        // beyond it the source is returned unchanged.
        vec2 field = ac_lamp_scatter(cp - pane, content * 0.5 - pane, h);
        field *= gate;
        fielded += (field.x > 0.0 || field.y > 0.0) ? 1.0 : 0.0;

        if (field.x > 0.0 || field.y > 0.0) {
            float fl = flicker_intensity(phase, lampID);
            light += (RL_WARM * (0.18 * field.x) + RL_CORE * (0.10 * field.y)) * fl;
        }
    }

    if (fielded <= 0.0) {
        return src; // outside both finite fields: exact passthrough
    }

    // Reflected weight: the light picks out real content instead of reading as
    // flat fog. Additive and alpha-preserving, so text stays legible.
    vec3 straight = min(src.rgb / src.a, vec3(1.0));
    float luma = dot(straight, vec3(0.299, 0.587, 0.114));
    light *= 0.45 + 0.55 * clamp(luma, 0.0, 1.0);

    // Alpha is untouched; the premultiplied RGB is re-fitted under it.
    vec3 rgb = min(src.rgb + light * src.a, vec3(src.a));
    return vec4(rgb, src.a);
}

void main() {
    vec2 uv = vec2(gl_FragCoord.x / weather_size.x, 1.0 - gl_FragCoord.y / weather_size.y);
    EffectContext e = EffectContext(uv, weather_size, vec4(0.0, 0.0, weather_size), vec4(0.0, 0.0, weather_size));
    vec4 result = shader_main(e);
    weather_output = result;
}
