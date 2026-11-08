#version 320 es
precision highp float;
precision highp int;
precision highp sampler2D;
#define texture2D texture
in vec2 b_uv;
uniform vec2 b_origin;
uniform vec2 b_span;
uniform float b_scale;
layout(location=0) out vec4 b_output;
// Native PNG textures are premultiplied Cairo RGBA. Authored equations use
// straight samples; unpremultiply at the sample boundary, not twice at output.
vec4 b_straight(vec4 c) { return c.a > 0.00001 ? vec4(c.rgb / c.a, c.a) : vec4(0.0); }
// Rainlit generic native frame shader. One shader serves all four painterly
// packs: every pack-specific value arrives through uniforms bound once by
// rainlit/runtime.ts from the selected FramePack and its authored motion.
//
// Semantics (frozen):
// - `insets` (left, top, right, bottom logical px) reserves the frame ring;
//   the rectangular client opening is clipped BEFORE any art is evaluated and
//   stays perfectly transparent.
// - Structural drawing order: neutral full-depth rails (repeated along their
//   natural long axis, mitred at the corner squares), one-shot corner cells,
//   then bounded transformed atlas layers in pack compositing order.
// - Layer transforms arrive precomputed: `rl_pos` is the world top-left of the
//   unrotated layer (parent FK resolved per frame by the runtime over native
//   scalar signals), `rl_angle` is the accumulated chain angle in radians.
//   Rotation happens about the layer pivot; nothing is stretched.
// - `rl_reveal` drives a layer's reveal (0..1) selected by `rl_reveal_mode`:
//   mode 0 (clip, the default) keeps the frozen top-anchored wipe (visible
//   where local uv.y is at or above the reveal value). Mode 1 (slide)
//   translates the whole layer down from one layer-height above its anchor
//   (reveal 0, fully hidden behind the later stationary layer) to the anchor
//   (reveal 1), so material emerges with a real moving leading edge. Neither
//   mode ever scales geometry. `rl_deform` marks authored cloth: only the hem
//   (uv.y -> 1) sways with `wind`, the head (uv.y = 0) and root stay fixed.
// - `rl_emission` is an RGB light gain (neutral 1) over the sampled cell:
//   authored emissive overlay cells carry real region alpha, so the gain is
//   object-shaped, never a rectangle glow. Alpha is preserved.
// - The captured native header source (`tex`, premultiplied full window
//   capture) is composited source-over the painted art inside the top header
//   band only, so real native controls stay above the frame.
// - Alpha path identical to rain-frame.frag: straight atlas samples are
//   premultiplied exactly once into the accumulator; because this is the
//   final stage of the window effect, the accumulator is converted back to
//   straight RGB at the boundary (the native display wrapper multiplies RGB
//   by alpha).
// - Declared bound: the runtime validates pack layer counts against
//   RL_MAX_LAYERS and fails known-invalid packs instead of silently
//   dropping layers.

#ifndef RL_MAX_LAYERS
#define RL_MAX_LAYERS 16
#endif

uniform sampler2D frame_atlas;
uniform vec2 atlas_size;      // source pixels
uniform vec2 logical_size;    // root logical width/height
uniform vec4 insets;          // left, top, right, bottom logical px
uniform float focused;        // 0..1 smooth native focus
uniform float wind;           // shared wind scalar
uniform float layer_count;    // validated <= RL_MAX_LAYERS by the runtime
uniform vec4 rl_cell[RL_MAX_LAYERS];
uniform vec2 rl_size[RL_MAX_LAYERS];
uniform vec2 rl_pivot[RL_MAX_LAYERS];
uniform vec2 rl_pos[RL_MAX_LAYERS];
uniform float rl_angle[RL_MAX_LAYERS];
uniform float rl_reveal[RL_MAX_LAYERS];
uniform float rl_reveal_mode[RL_MAX_LAYERS];
uniform float rl_emission[RL_MAX_LAYERS];
uniform float rl_deform[RL_MAX_LAYERS];
uniform vec4 rl_corner_tl;
uniform vec4 rl_corner_tr;
uniform vec4 rl_corner_bl;
uniform vec4 rl_corner_br;
uniform vec2 rl_corner_top_size;    // logical
uniform vec2 rl_corner_bottom_size; // logical
uniform vec4 rl_rail_top;
uniform vec4 rl_rail_bottom;
uniform vec4 rl_rail_left;
uniform vec4 rl_rail_right;
uniform vec2 rl_rail_period; // horizontal, vertical logical

// Sample one atlas cell; `t` is local [0,1]^2. A half-source-texel inset
// keeps linear filtering inside the cell's transparent gutter.
vec4 rl_cell_sample(vec4 cell, vec2 t) {
    vec2 c = clamp(t, 0.0, 1.0);
    vec2 px = cell.xy + vec2(0.5) + c * (cell.zw - vec2(1.0));
    return b_straight(texture2D(frame_atlas, px / max(atlas_size, vec2(1.0))));
}

// Antialiased coverage of an axis-aligned logical rectangle.
float rl_rect(vec2 p, vec2 lo, vec2 hi, vec2 aa) {
    vec2 enter = smoothstep(lo - aa, lo + aa, p);
    vec2 leave = vec2(1.0) - smoothstep(hi - aa, hi + aa, p);
    return clamp(enter.x * enter.y * leave.x * leave.y, 0.0, 1.0);
}

// Straight atlas sample -> premultiplied, scaled by coverage.
vec4 rl_pm(vec4 c, float cov) {
    float a = c.a * cov;
    return vec4(c.rgb * a, a);
}

// Premultiplied source-over.
vec4 rl_over(vec4 dst, vec4 src) {
    return src + dst * (1.0 - src.a);
}

// One-shot corner cell with a quiet 2-logical-px tip blend into the running
// rail: only the wood tip fades, the hardware itself never fades. `inner`
// measures distance from the corner tip along both arms.
float rl_tip(float innerA, float innerB) {
    return smoothstep(0.0, 2.0, innerA) * smoothstep(0.0, 2.0, innerB);
}

vec4 bamboo_frame() {
    vec2 logical = max(logical_size, vec2(1.0));
    vec2 framePx = logical * b_scale;
    vec2 scale = framePx / logical;
    vec2 aa = vec2(1.0) / max(scale, vec2(1.0));
    // Logical-pixel position relative to the frame's top-left.
    vec2 p = b_origin + b_uv * b_span;

    float left = max(insets.x, 0.0);
    float top = max(insets.y, 0.0);
    float right = max(insets.z, 0.0);
    float bottom = max(insets.w, 0.0);

    // Hard invariant: the protected client opening is perfectly transparent,
    // evaluated before any rail, corner or layer.
    if (p.x >= left && p.x <= logical.x - right && p.y >= top && p.y <= logical.y - bottom) {
        return vec4(0.0);
    }

    vec4 acc = vec4(0.0);

    // Mitred rail turn, normalized per arm. Each distance is measured in units
    // of that arm's own thickness, so unequal rail weights (07: a 48px top
    // beam over 26px sides) still tile the whole ring. A pixel inside exactly
    // one arm has that arm's normalized distance < 1 while the perpendicular
    // arm's is >= 1 (it lies outside that arm), so its weight is a solid 1;
    // the smooth diagonal only blends where both arms actually overlap.
    float dvN = min(p.x / max(left, 1.0), max(logical.x - p.x, 0.0) / max(right, 1.0));
    float dhN = min(p.y / max(top, 1.0), max(logical.y - p.y, 0.0) / max(bottom, 1.0));
    float mitreAA = clamp(aa.x / max(left, 1.0) + aa.y / max(top, 1.0), 0.02, 0.5);
    float wh = smoothstep(-mitreAA, mitreAA, dvN - dhN);
    float wv = 1.0 - wh;

    float rTop = rl_rect(p, vec2(0.0, 0.0), vec2(logical.x, top), aa) * wh;
    if (rTop > 0.0 && top > 0.0) {
        vec4 c = rl_cell_sample(rl_rail_top, vec2(fract(p.x / max(rl_rail_period.x, 1.0)), p.y / top));
        acc = rl_over(acc, rl_pm(c, rTop));
    }
    float rBottom = rl_rect(p, vec2(0.0, logical.y - bottom), vec2(logical.x, logical.y), aa) * wh;
    if (rBottom > 0.0 && bottom > 0.0) {
        vec4 c = rl_cell_sample(
            rl_rail_bottom,
            vec2(fract(p.x / max(rl_rail_period.x, 1.0)),
                 (p.y - (logical.y - bottom)) / bottom));
        acc = rl_over(acc, rl_pm(c, rBottom));
    }
    float rLeft = rl_rect(p, vec2(0.0, 0.0), vec2(left, logical.y), aa) * wv;
    if (rLeft > 0.0 && left > 0.0) {
        vec4 c = rl_cell_sample(rl_rail_left, vec2(p.x / left, fract(p.y / max(rl_rail_period.y, 1.0))));
        acc = rl_over(acc, rl_pm(c, rLeft));
    }
    float rRight = rl_rect(p, vec2(logical.x - right, 0.0), vec2(logical.x, logical.y), aa) * wv;
    if (rRight > 0.0 && right > 0.0) {
        vec4 c = rl_cell_sample(
            rl_rail_right,
            vec2((p.x - (logical.x - right)) / right,
                 fract(p.y / max(rl_rail_period.y, 1.0))));
        acc = rl_over(acc, rl_pm(c, rRight));
    }

    // Four complete one-shot corner cells over the rails. Top and bottom cap
    // sizes may differ (07 top beam). Sample coordinates are corner-local.
    vec2 ct = max(rl_corner_top_size, vec2(0.001));
    vec2 cb = max(rl_corner_bottom_size, vec2(0.001));
    float cTL = rl_rect(p, vec2(0.0, 0.0), ct, aa);
    if (cTL > 0.0) {
        vec4 c = rl_cell_sample(rl_corner_tl, p / ct);
        acc = rl_over(acc, rl_pm(c, cTL * rl_tip(ct.x - p.x, ct.y - p.y)));
    }
    vec2 trLo = vec2(logical.x - ct.x, 0.0);
    float cTR = rl_rect(p, trLo, vec2(logical.x, ct.y), aa);
    if (cTR > 0.0) {
        vec2 lp = p - trLo;
        vec4 c = rl_cell_sample(rl_corner_tr, lp / ct);
        acc = rl_over(acc, rl_pm(c, cTR * rl_tip(lp.x, ct.y - lp.y)));
    }
    vec2 blLo = vec2(0.0, logical.y - cb.y);
    float cBL = rl_rect(p, blLo, vec2(cb.x, logical.y), aa);
    if (cBL > 0.0) {
        vec2 lp = p - blLo;
        vec4 c = rl_cell_sample(rl_corner_bl, lp / cb);
        acc = rl_over(acc, rl_pm(c, cBL * rl_tip(cb.x - lp.x, lp.y)));
    }
    vec2 brLo = logical - cb;
    float cBR = rl_rect(p, brLo, logical, aa);
    if (cBR > 0.0) {
        vec2 lp = p - brLo;
        vec4 c = rl_cell_sample(rl_corner_br, lp / cb);
        acc = rl_over(acc, rl_pm(c, cBR * rl_tip(lp.x, lp.y)));
    }

    // Bounded transformed atlas layers, compositing order. Per layer: rotate
    // the fragment about the pivot by the accumulated chain angle, apply the
    // selected reveal (clip or slide) and the hem-only cloth deformation,
    // then add the object-shaped light gain.
    int count = int(min(layer_count, float(RL_MAX_LAYERS)));
    for (int i = 0; i < RL_MAX_LAYERS; i++) {
        if (i >= count) { break; }
        vec2 size = max(rl_size[i], vec2(0.001));
        vec2 pivot = rl_pivot[i];
        float ang = rl_angle[i];
        float ca = cos(ang);
        float sa = sin(ang);
        vec2 c = p - (rl_pos[i] + pivot);
        vec2 q = vec2(ca * c.x + sa * c.y, -sa * c.x + ca * c.y) + pivot;
        float rev = clamp(rl_reveal[i], 0.0, 1.0);
        bool slide = rl_reveal_mode[i] > 0.5;
        // Slide reveal: sample the layer one full height above its anchor at
        // rest (rev 0, entirely hidden behind the later stationary layer) and
        // at its authored anchor at full reveal (rev 1). The shift is applied
        // BEFORE the local uv bounds test, so the visible band is the real
        // translated geometry; the old top-anchored wipe is never applied in
        // slide mode, and a layer at rest is skipped outright.
        if (slide) {
            if (rev <= 0.0) { continue; }
            q.y += (1.0 - rev) * size.y;
        }
        vec2 uv = q / size;
        if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) { continue; }
        if (rl_deform[i] > 0.5) {
            // Cloth hem: head fixed at uv.y = 0, bounded quadratic sway.
            float hem = uv.y * uv.y;
            uv.x += wind * 0.08 * hem;
            if (uv.x < 0.0 || uv.x > 1.0) { continue; }
        }
        if (slide || rev >= 1.0) {
            vec4 cs = rl_cell_sample(rl_cell[i], uv);
            cs.rgb *= clamp(rl_emission[i], 0.0, 4.0);
            acc = rl_over(acc, rl_pm(cs, 1.0));
        } else {
            float edge = aa.y / size.y;
            float vis = 1.0 - smoothstep(rev - edge, rev + edge, uv.y);
            if (vis <= 0.0) { continue; }
            vec4 cs = rl_cell_sample(rl_cell[i], uv);
            cs.rgb *= clamp(rl_emission[i], 0.0, 4.0);
            acc = rl_over(acc, rl_pm(cs, vis));
        }
    }

    // Modest focus trim on the painted art only, never a wash over content.
    float focus = clamp(focused, 0.0, 1.0);
    acc.rgb *= mix(0.85, 1.0, focus);

    // Kitty native decorations/client are never captured or replaced here.

    acc.a = clamp(acc.a, 0.0, 1.0);
    if (acc.a <= 1e-4) {
        return vec4(0.0);
    }
    // Final boundary: back to straight RGB carrying the original alpha.
    return vec4(acc.rgb / acc.a, acc.a);
}

void main() { vec4 c = bamboo_frame(); b_output = vec4(c.rgb * c.a, c.a); }
