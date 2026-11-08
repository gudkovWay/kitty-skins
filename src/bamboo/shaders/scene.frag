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
// Rainlit bamboo-girl scene stage (pack 02, Kitty interiors only). Compiled
// by rainlit/runtime.ts and appended AFTER the generic frame shader
// (frame.frag) in the WINDOW frame-effect pipeline. Its input `tex` is the
// frame shader's own output: STRAIGHT (unpremultiplied) RGBA frame art — the
// painted ring outside the client opening, the captured native header band,
// and a perfectly transparent client opening (frame.frag hard-returns 0
// there). This stage therefore composites the story foreground OVER the
// frame art INSIDE the client opening, and returns the incoming frame art
// completely untouched anywhere outside it — EXCEPT the fixed suspension
// cords, which are the one overlay allowed across the native frame/client
// clip so the cable never breaks at the boundary.
//
// Frozen semantics:
// - The effect rect is the whole decorated window, so the shader recovers the
//   ROOT logical pixel exactly like frame.frag (effect_frame_px over
//   frame_rect_px / root logical size), then subtracts the ring insets ONCE
//   to reach the client-local space every position uniform is authored in.
// - The incoming straight frame art is premultiplied exactly once into the
//   accumulator; every sprite (bamboo layers, girl, button, impacts) is then
//   composited with a genuine source-over in premultiplied space, and the
//   accumulator is converted back to straight RGB at the boundary exactly
//   once (the compositor multiplies RGB by alpha at the final boundary).
//   Sprite alpha is NEVER weighted by the incoming alpha: inside the client
//   the incoming alpha is 0, so that would erase the whole foreground.
// - The four bamboo layers (slat, roll, tie-near, tie-far) are drawn from the
//   ORIGINAL pack atlas using the SAME cells/sizes/pivots/positions/angles/
//   reveal/translation the frame stage uses (runtime reuses the identical FK
//   signals), so the group reads as one continuous object across the frame
//   ring and the client.
// - Upper bindings stay on the beam. Two solid shaded burgundy braids emerge
//   behind their knots and bear against the moving bamboo's lower edge.
//   Diameter and texture come from the same authored binding material, not a
//   translucent sinusoidal hairline. The strand crosses in front of the roll
//   and terminates inside its lower silhouette; no moving tassel is duplicated.
//   Local impact slack is bounded, and all coordinates share the frame scale.
// - The girl is one indexed 16-cell atlas draw with a uniform whole-sprite
//   horizontal mirror (facing < -0.5 flips the local u axis); alpha comes
//   from the atlas's real transparency, never a luminance key. The authored
//   girl fade (`g_opacity`) and the button gate (`b_opacity`) are the SAME
//   signal, so the STORY overlay (girl + button) is exactly transparent at
//   rest / quiet / reduced.
// - There is NO ambient foreground weather and NO lamp spill here: the
//   rejected grey swaying lanes/drops were removed by owner imperative, and
//   the restored S-tier ambient (the legacy lantern backdrop plus the
//   content-aware weather `replace`) owns the interior lighting through its
//   own effects. This stage only composites the story foreground.
// - The decorative button and the floor/ceiling impacts are SMALL LOCALIZED
//   fields drawn as bounded source-over face/warm-dust sprites: no opaque
//   plate, no global flash, and no multiplicative shade over client pixels
//   (which are not sampled here).
// - Both atlases are preloaded image sources bound once at compile time.

uniform sampler2D frame_atlas;
uniform vec2 frame_atlas_size; // source px
uniform sampler2D scene_atlas;
uniform vec2 scene_atlas_size; // source px
uniform vec2 logical_size;     // client opening logical size
uniform vec2 scene_frame_size; // ROOT logical size (frame.frag's logical_size)
uniform vec4 scene_insets;     // left, top, right, bottom logical px (ring)
uniform float scene_scale;     // sceneScale = girlScale / 0.20
uniform float focused;
uniform vec4 sc_cell[4];
uniform vec2 sc_size[4];
uniform vec2 sc_pivot[4];
uniform vec2 sc_pos[4];
uniform float sc_angle[4];
uniform float sc_reveal[4];
uniform float sc_reveal_mode[4];
uniform vec4 g_cell;
uniform vec2 g_pos;
uniform vec2 g_size;
uniform float g_facing;
uniform float g_opacity;
uniform float b_opacity;
uniform vec2 b_center;
uniform vec2 b_size;
uniform float b_press;
uniform float floor_impact;
uniform float ceiling_impact;
// Fixed knot centres, moving lower bearings, and scaled material width.
uniform vec2 cord_a0;
uniform vec2 cord_e0;
uniform vec2 cord_a1;
uniform vec2 cord_e1;
uniform float cord_slack;
uniform float cord_diameter;
uniform float cord_payout;

// Half-source-texel inset sample of one atlas cell (identical to the frame
// stage's helper): keeps linear filtering inside the cell's transparent
// gutter. `t` is local [0,1]^2.
vec4 rl_scene_cell_sample(sampler2D atlas, vec2 atlas_size, vec4 cell, vec2 t) {
    vec2 c = clamp(t, 0.0, 1.0);
    vec2 px = cell.xy + vec2(0.5) + c * (cell.zw - vec2(1.0));
    return b_straight(texture2D(atlas, px / max(atlas_size, vec2(1.0))));
}

// Premultiplied source-over: both operands are premultiplied.
vec4 rl_scene_over(vec4 dst, vec4 src) {
    return src + dst * (1.0 - src.a);
}

// Near-vertical load-bearing braid. Solve its lateral bow directly in y:
// no eight-segment distance loop on every covered fragment.
vec4 rl_scene_cord(vec2 p, vec2 a, vec2 e, float slack, float seed, float aa) {
    if (cord_payout <= 0.0 || e.y <= a.y) { return vec4(0.0); }
    float radius = 0.5 * cord_diameter;
    float bow = cord_diameter * 1.5 * clamp(slack, 0.0, 1.0) * (seed < 0.5 ? -1.0 : 1.0);
    float margin = radius + abs(bow) + aa;
    if (p.x < min(a.x, e.x) - margin || p.x > max(a.x, e.x) + margin ||
        p.y < a.y - radius - aa || p.y > e.y + radius + aa) {
        return vec4(0.0);
    }
    float dy = e.y - a.y;
    float u = clamp((p.y - a.y) / dy, 0.0, 1.0);
    float centre = mix(a.x, e.x, u) + 4.0 * bow * u * (1.0 - u);
    float slope = (e.x - a.x + 4.0 * bow * (1.0 - 2.0 * u)) / dy;
    float cross = (p.x - centre) / sqrt(1.0 + slope * slope);
    float cap = p.y - mix(a.y, e.y, u);
    float distance = length(vec2(cross, cap));
    float coverage = (1.0 - smoothstep(max(0.0, radius - aa), radius + aa, distance)) * cord_payout;
    if (coverage <= 1e-4) { return vec4(0.0); }
    // Reuse the burgundy strand in the original binding, excluding its
    // bamboo surround and lower tassel. Only colour varies; the core is solid.
    float across = clamp(cross / max(cord_diameter, 0.001) + 0.5, 0.0, 1.0);
    float along = fract((p.y - a.y) / max(5.0 * cord_diameter, 0.001) + seed * 0.37);
    vec4 cell = seed < 0.5 ? sc_cell[2] : sc_cell[3];
    vec3 material = rl_scene_cell_sample(frame_atlas, frame_atlas_size, cell,
        vec2(mix(0.38, 0.62, across), mix(0.12, 0.46, along))).rgb;
    float radial = 2.0 * across - 1.0;
    float roundLight = 0.78 + 0.22 * sqrt(max(0.0, 1.0 - radial * radial));
    return vec4(material * roundLight * coverage, coverage);
}

// Existing upper binding art occludes the emerging braid on either side of
// the frame/client boundary. The cells remain stationary and retain alpha.
float rl_scene_binding_alpha(vec2 p) {
    float coverage = 0.0;
    for (int i = 2; i < 4; i++) {
        vec2 uv = (p - sc_pos[i]) / max(sc_size[i], vec2(0.001));
        if (uv.x >= 0.0 && uv.x <= 1.0 && uv.y >= 0.0 && uv.y <= 1.0) {
            coverage = max(coverage,
                rl_scene_cell_sample(frame_atlas, frame_atlas_size, sc_cell[i], uv).a);
        }
    }
    return coverage;
}

vec4 bamboo_scene() {
    vec4 src = vec4(0.0); // Independent foreground, never samples client pixels.
    vec2 logical = max(logical_size, vec2(1.0));

    // Root logical pixel, recovered exactly like frame.frag; then ONE inset
    // subtraction into the client-local space the position uniforms use.
    vec2 root = max(scene_frame_size, vec2(1.0));
    vec2 framePx = root * b_scale;
    vec2 p = b_origin + b_uv * b_span - scene_insets.xy;
    float aa = max(0.25, 0.5 * root.x / framePx.x);
    float materialLight = mix(0.85, 1.0, clamp(focused, 0.0, 1.0));

    // Fixed suspension, drawn in BOTH spaces so the cable crosses the native
    // frame/client clip with no gap.
    vec4 cord = rl_scene_over(
        rl_scene_cord(p, cord_a0, cord_e0, cord_slack, 0.0, aa),
        rl_scene_cord(p, cord_a1, cord_e1, cord_slack, 1.0, aa)
    );
    if (cord.a > 0.0) {
        cord *= 1.0 - rl_scene_binding_alpha(p);
        cord.rgb *= materialLight;
    }

    // Outside the client opening (frame ring, header band) the incoming frame
    // art — rails, corners, real native controls — is returned untouched,
    // except where the suspension cord actually covers a pixel.
    if (p.x < 0.0 || p.y < 0.0 || p.x > logical.x || p.y > logical.y) {
        if (cord.a <= 1e-4) {
            return src;
        }
        vec4 pm = rl_scene_over(vec4(src.rgb * src.a, src.a), cord);
        float a = clamp(pm.a, 0.0, 1.0);
        if (a <= 1e-4) {
            return vec4(0.0);
        }
        return vec4(pm.rgb / a, a);
    }

    // Premultiply the incoming straight frame art once. Inside the client this
    // is exactly zero, so only the sprites below contribute coverage.
    vec4 acc = vec4(src.rgb * src.a, src.a);

    // Bamboo continuation: the exact frame-stage layer math, in client-local
    // space. Sampled from the ORIGINAL pack atlas so the material matches
    // the ring pixel for pixel. Genuine source-over: the layer's real alpha is
    // used directly, never weighted by the incoming coverage.
    for (int i = 0; i < 4; i++) {
        vec2 size = max(sc_size[i], vec2(0.001));
        vec2 pivot = sc_pivot[i];
        float ang = sc_angle[i];
        float ca = cos(ang);
        float sa = sin(ang);
        vec2 c = p - (sc_pos[i] + pivot);
        vec2 q = vec2(ca * c.x + sa * c.y, -sa * c.x + ca * c.y) + pivot;
        float rev = clamp(sc_reveal[i], 0.0, 1.0);
        bool slide = sc_reveal_mode[i] > 0.5;
        if (slide) {
            if (rev <= 0.0) { continue; }
            q.y += (1.0 - rev) * size.y;
        }
        vec2 t = q / size;
        if (t.x < 0.0 || t.x > 1.0 || t.y < 0.0 || t.y > 1.0) { continue; }
        vec4 cs = rl_scene_cell_sample(frame_atlas, frame_atlas_size, sc_cell[i], t);
        acc = rl_scene_over(acc, vec4(cs.rgb * materialLight * cs.a, cs.a));
    }
    // Bearing strands remain visibly connected across the front of the bundle.
    acc = rl_scene_over(acc, cord);

    // The girl: one indexed atlas draw with a uniform mirror. facing < -0.5
    // means the sheet was authored facing left and this draw is reflected to
    // face right; the reflection flips the local u axis only. Alpha is the
    // atlas's real transparency times the authored girl fade.
    {
        vec2 q = p - g_pos;
        if (g_facing < -0.5) { q.x = g_size.x - q.x; }
        vec2 t = q / max(g_size, vec2(0.001));
        float ga = clamp(g_opacity, 0.0, 1.0);
        if (t.x >= 0.0 && t.x <= 1.0 && t.y >= 0.0 && t.y <= 1.0 && ga > 0.001) {
            vec4 gs = rl_scene_cell_sample(scene_atlas, scene_atlas_size, g_cell, t);
            float a = gs.a * ga;
            acc = rl_scene_over(acc, vec4(gs.rgb * a, a));
        }
    }

    // Decorative press button: a small soft rounded face with a warm rim,
    // brightness following the same causal press channel that releases the
    // bamboo. Gated by the authored girl opacity so it never appears at rest,
    // quiet or reduced. Alpha stays far below opaque; no plate, no flash.
    {
        float gate = clamp(b_opacity, 0.0, 1.0);
        vec2 hb = b_size * 0.5;
        vec2 d = abs(p - b_center) / max(hb, vec2(0.001));
        float m = max(d.x, d.y);
        float face = (1.0 - smoothstep(0.85, 1.0, m));
        float rim = smoothstep(0.55, 0.85, m) * (1.0 - smoothstep(0.85, 1.0, m));
        vec3 faceCol = vec3(0.16, 0.13, 0.10) + vec3(0.55, 0.38, 0.18) * b_press * 0.45;
        float faceA = (0.30 * face + 0.35 * rim) * (0.55 + 0.45 * b_press) * gate;
        acc = rl_scene_over(acc, vec4(faceCol * faceA, faceA));
        // Bounded warm halo while pressed, lighting only EXISTING coverage.
        vec2 hd = (p - b_center) / max(hb * 3.0, vec2(0.001));
        float halo = exp(-dot(hd, hd)) * b_press * 0.30 * gate;
        acc.rgb += vec3(1.0, 0.72, 0.35) * halo * acc.a;
    }

    // Floor impact: a bounded warm dust kiss at the REAL client floor under the
    // bamboo bundle (the girl is offscreen during the drop, so the impact must
    // follow the bundle, not the girl). Drawn source-over, never a
    // multiplicative shade over client pixels.
    {
        float bx = sc_pos[1].x + sc_size[1].x * 0.5;
        vec2 c = vec2(bx, logical.y - 1.0);
        vec2 d = (p - c) / vec2(max(70.0 * scene_scale, 1.0), max(11.0 * scene_scale, 1.0));
        float f = exp(-dot(d, d)) * clamp(floor_impact, 0.0, 1.0);
        float da = f * 0.30;
        acc = rl_scene_over(acc, vec4(vec3(0.62, 0.55, 0.42) * da, da));
    }

    // Ceiling impact: the same warm dust at the nearest client-top point UNDER
    // the bamboo bundle (the resting bundle centre is above the client, so the
    // visible hit line is the client top edge beneath it).
    {
        float bx = sc_pos[1].x + sc_size[1].x * 0.5;
        vec2 c = vec2(bx, 0.0);
        vec2 d = (p - c) / vec2(max(60.0 * scene_scale, 1.0), max(16.0 * scene_scale, 1.0));
        float f = exp(-dot(d, d)) * clamp(ceiling_impact, 0.0, 1.0);
        float da = f * 0.24;
        acc = rl_scene_over(acc, vec4(vec3(0.66, 0.60, 0.46) * da, da));
    }

    float outA = clamp(acc.a, 0.0, 1.0);
    if (outA <= 1e-4) {
        return vec4(0.0);
    }
    // Final boundary: back to straight RGB carrying the original alpha.
    return vec4(acc.rgb / outA, outA);
}

void main() { vec4 c = bamboo_scene(); b_output = vec4(c.rgb * c.a, c.a); }
