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

uniform sampler2D b_backdrop;
// The caller converted the captured target to premultiplied sRGB BEFORE
// radius4/passes2 filtering. This is the visible crop of that padded result.
uniform vec4 b_backdrop_uv;
// Ambient backdrop compositions for the unified Shoji frame.
//
// This is the stage that runs after dualKawaseBlur over the native backdrop:
// `tex` is the already-blurred, premultiplied backdrop and is sampled exactly
// once, at the original UV, for every composition. Nothing here displaces,
// refracts, blurs or re-blurs the source, and no client/subsurface pixel is
// ever read -- the atmospheric layers are painted over the untouched blurred
// colour, so the centre stays calm and text is unaffected.
//
// The shader paints one of three named compositions, selected by the integer
// value in `composition` (0 rainlit-shoji, 1 lantern-dusk, 2 moonlit-ink).
// `phase` is a caller-owned loop in [0, 1] over 96 s.
//
// Content lanterns (profiles 0 and 1): two complete objects sampled from the
// straight-RGBA 512x1024 `content_lantern` sprite -- no analytic point glow.
// They are composited into the backdrop, beneath Kitty's ~85% opaque
// background; the separate rain-light stage lights the captured client so
// their local glow is not attenuated by that background. The first lamp is
// anchored in the upper-right background, the identical second (lower-left)
// lamp in the bottom-left corner; both keep at least a 12 logical-px inset and
// a proportional size W = min(104, logicalW*0.18, logicalH*0.22), H = 2W,
// preserving the 1:2 sprite aspect with no flip, crop or stretch. The
// bottom-left lamp is drawn only while the opening is large enough to hold
// both boxes without overlap. A local warm spill anchored to each emitted pane
// centre (sprite UV (0.501479, 0.625633)) stays inside that lamp's own near
// field. The same two boxes/panes are the shared definition consumed by the
// rain-light foreground stage.
//
// Each lamp has exactly one motion: the failing-bulb flicker
// (`flicker_intensity`). Eight 12 s slots per loop, each with an irregular
// 0.45..0.9 s brownout of two or three uneven dips, 10..14 s apart. The lower
// lamp is offset 3.225 slots (2.7 s into the sub-slot) with a distinct hash
// identity, giving it a separate event schedule. Flicker dims only the
// saturated luminous-pane pixels of the sprite -- the builder's own r>0.59,
// g>0.51, r-b>0.24 glow criterion -- and scales the local spill by the same
// value; the dark metal object never moves or changes. No sway, no sine
// breathing and no full-window wash.
//
// Rain (profile 0): two procedural depth layers, each a fixed grid of hashed
// cells evaluating at most one short streak per fragment (no per-particle
// loop). Streaks are 9..31 logical px long and ~1..2 logical px wide, descend
// at 96 and 150 logical px/s (integer descent cycles per 96 s loop), and are
// distributed across the whole background with a quiet-centre weight. The cold
// contrast is raised so the thin lines stay visible behind the ~85% opaque
// Kitty client. There are no stationary edge columns and no long beams.
//
// Logical coordinates: `logical_content_size` carries the content rect in
// logical px (frame size minus the 52px structural border), and the physical
// content rect supplies the scale, so the lantern and rain keep their real
// physical size at any render scale or DPR.
//
// Periodicity: every time-varying term is a periodic function of `phase`.
// Spatial cells use an integer number of descent cycles per loop and a
// statically hashed phase offset, so `fract(phase*cycles + offset)` is
// identical at phase 0 and phase 1; the sin(pi*u) envelope is zero at both
// ends of each streak lifecycle. The field is therefore seamless at the loop
// boundary, brightness is bounded, and there is no synchronous global
// breathing. All loops are fixed-bound; no allocation is possible.
//
// Coordinates: the existing smoke and moonlit terms work in "height units" --
// y spans 0..1 and x spans 0..aspect. The lantern and rain work in logical px.
//
// Premultiplication: `base` is premultiplied. We unpremultiply once (zero alpha
// never reaches the maths), apply the smoke base plus the composition in
// straight space, then re-premultiply. The returned RGB is therefore always
// <= alpha, alpha is preserved exactly, and a zero-alpha pixel returns zero.

uniform float phase;       // caller loop in [0, 1] over 96 s
uniform float composition; // 0 rainlit-shoji, 1 lantern-dusk, 2 moonlit-ink
uniform sampler2D content_lantern; // straight-RGBA 512x1024 lantern sprite
uniform vec2 logical_content_size; // content rect in logical px (frame - 52)

// --- smoked-glass base, preserved from the prior backdrop stage ---
// Per-channel dim/cool factors (< 1, so it can only darken/cool), the edge band
// width in physical px, and the interior/edge smoke blend bounds.
const vec3 AC_SMOKE_DIM = vec3(0.78, 0.86, 0.91);
const float AC_PERIMETER_PX = 120.0;
const float AC_SMOKE_MIN = 0.30;
const float AC_SMOKE_MAX = 0.55;

// Shared palette (from the cached dark-wood / cool-rain / amber-lantern input).
const vec3 AC_COLD = vec3(0.616, 0.796, 0.835);  // cool reflected #9CCBD5
const vec3 AC_WARM = vec3(0.961, 0.639, 0.239);  // lantern amber #F5A33D
const vec3 AC_LAMP_CORE = vec3(1.000, 0.862, 0.522); // lit pane, luminous yellow
const float AC_PI = 3.14159265;
const float AC_TWO_PI = 6.28318531;

// Deterministic, sin-free 1D hash in [0, 1). Cheap enough to call several
// times per pixel without dragging transcendentals through the loops.
float ac_hash(float n) {
    n = fract(n * 0.1031 + 0.11369);
    n *= n + 34.345;
    n *= n + n;
    return fract(n);
}

// Same hash keyed by a 2D cell index, so every rain cell gets a stable,
// independent offset.
float ac_hash2(vec2 c) {
    return ac_hash(c.x * 127.1 + c.y * 311.7 + 0.173);
}

// --- shared failing-bulb flicker ------------------------------------------
// Byte-identical twin of the function in rain-light.frag; keep the two copies
// in lockstep so every sprite pane emission and its local spill brown out on
// the same frame. `phase` is the 96 s loop in [0, 1]: eight 12 s slots, each
// with a hashed onset at 3 + 2*hash(slot mod 8) s, so consecutive onsets are
// 10..14 s apart. Inside a short 0.45..0.9 s window the intensity dips to 0.18
// through two or three uneven gaussian brownouts; between events it is exactly
// 1. `lampID` (0 top-right, 1 bottom-left) shifts the slot clock by 3.225
// slots -- 2.7 s into the sub-slot -- and seeds a distinct hash identity, so
// the two lamps have separate event schedules. Slot identity is keyed mod 8, so the
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

// Soft, aspect-correct coverage of a pointed-oval "leaf" silhouette.
// `d` is the offset from the leaf centre in height units, (ca, sa) its
// orientation, R half-length and W half-width. Width tapers to zero at both
// tips like a leaf/eye, and the profile is smoothed so overlapping leaves
// blend without hard cut lines.
float ac_leaf(vec2 d, float ca, float sa, float R, float W) {
    vec2 r = vec2(ca * d.x - sa * d.y, sa * d.x + ca * d.y);
    float a = r.x / max(R, 0.0001);
    float w = W * pow(max(1.0 - abs(a), 0.0), 0.62);
    float b = r.y / max(w, 0.0001);
    float raw = (1.0 - abs(a)) * max(0.0, 1.0 - b * b);
    return smoothstep(0.0, 0.45, raw);
}

// The shared placement of one content lantern in logical px. `lampID` 0 is the
// upper-right lamp, 1 the identical lower-left lamp; both keep a fixed >= 12px
// inset and the same proportional size that stays inside every root. `pane` is
// that lamp's emitted luminous-pane centre (sprite UV (0.501479, 0.625633),
// the worker's actual anchor at px (256.76, 640.65) in the 512x1024 sprite) and
// `reach` the bounded near-field radii still consumed by the lantern-dusk
// dust drift (the scatter field derives its own extent from h).
void ac_lamp_box(vec2 logical, float lampID, out vec2 boxMin, out vec2 boxMax,
                 out vec2 pane, out vec2 reach) {
    float w = min(104.0, min(logical.x * 0.18, logical.y * 0.22));
    float h = 2.0 * w;                       // sprite aspect is 1:2
    // Content y grows downward: lamp 0 sits 12px from the top-right edge, lamp
    // 1 the same distance from the bottom-left edge. The sprite stays upright
    // for both -- no flip, crop or stretch.
    boxMin = vec2(
        mix(logical.x - 12.0 - w, 12.0, lampID),
        mix(12.0, logical.y - 12.0 - h, lampID));
    boxMax = boxMin + vec2(w, h);
    pane = boxMin + vec2(0.501479, 0.625633) * vec2(w, h);
    reach = vec2(
        max(min(0.35 * h, logical.x * 0.40), 8.0),  // never wash the centre
        max(0.30 * h, 10.0));
}

// The lower-left lamp is suppressed unless the opening can hold both complete
// boxes (12 px inset on every side) without the two footprints overlapping.
bool ac_lamp_pair_fits(vec2 logical) {
    float w = min(104.0, min(logical.x * 0.18, logical.y * 0.22));
    float h = 2.0 * w;
    // Both complete boxes must sit inside the opening...
    bool boxesFit = logical.x >= 24.0 + w && logical.y >= 24.0 + h;
    // ...and must be disjoint on at least one axis.
    bool disjoint = logical.x >= 24.0 + 2.0 * w || logical.y >= 24.0 + 2.0 * h;
    return boxesFit && disjoint;
}

// Shared coherent lamp scatter field: one soft wide lobe plus a tighter warm
// core, both derived only from the offset from the pane, the direction toward
// the content centre and the lamp sprite box height. Byte-identical twin of
// the helper in rain-light.frag -- keep the two copies in lockstep.
// Returns vec2(soft, core); both components are exactly zero outside the
// finite extent, so the caller can skip the light maths on dry pixels.
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

// Sample both complete lantern sprites source-over the backdrop at full object
// strength, then add each lamp's local warm spill anchored to its real pane
// centre. Each pane emission and its spill are scaled by that lamp's own
// failing-bulb flicker, so an object and the light it casts fail together,
// independently of the other lamp.
vec3 ac_content_lantern(vec3 rgb, vec2 lp, vec2 logical, float phase) {
    bool pair = ac_lamp_pair_fits(logical);
    for (int i = 0; i < 2; i++) {
        float lampID = float(i);
        // The lower-left lamp is gated off when both boxes cannot fit without
        // overlapping; the upper-right lamp is always drawn.
        float gate = (i == 1 && !pair) ? 0.0 : 1.0;

        vec2 boxMin, boxMax, pane, reach;
        ac_lamp_box(logical, lampID, boxMin, boxMax, pane, reach);
        float fl = flicker_intensity(phase, lampID);

        vec2 span = max(boxMax - boxMin, vec2(1.0));
        vec2 uv = (lp - boxMin) / span;
        vec2 aa = 1.0 / span;
        vec2 enter = smoothstep(vec2(0.0), aa, uv);
        vec2 leave = vec2(1.0) - smoothstep(vec2(1.0) - aa, vec2(1.0), uv);
        float cov = clamp(enter.x * enter.y * leave.x * leave.y, 0.0, 1.0) * gate;
        if (cov > 0.0) {
            // The sprite is sampled only here, i.e. strictly inside this box.
            vec4 spr = b_straight(texture2D(content_lantern, clamp(uv, 0.0, 1.0)));
            // Emitted luminous panes only: the same saturated-warm criterion
            // the builder used to locate the glow (r>0.59, g>0.51, r-b>0.24),
            // softened so the pane edge follows the flicker and bounded to the
            // sprite's UV y 0.40..0.88 band. The dark metal cage and every
            // other sprite pixel keep the mix() weight at 1 and stay static.
            float pane = smoothstep(0.55, 0.62, spr.r)
                       * smoothstep(0.47, 0.54, spr.g)
                       * smoothstep(0.18, 0.28, spr.r - spr.b)
                       * smoothstep(0.40, 0.45, uv.y)
                       * (1.0 - smoothstep(0.84, 0.88, uv.y));
            vec3 lampRgb = spr.rgb * mix(1.0, fl, pane);
            // Straight-RGBA source-over, full alpha weight: a real object, not
            // an amplitude-scaled glow.
            rgb = mix(rgb, lampRgb, spr.a * cov);
        }

        // Local warm spill from this pane position via the shared coherent
        // scatter helper, derived from the actual box height and scaled by the
        // same flicker value (no full-window wash).
        vec2 field = ac_lamp_scatter(lp - pane, logical * 0.5 - pane, span.y);
        if (field.x > 0.0 || field.y > 0.0) {
            rgb += (AC_WARM * (0.18 * field.x) + AC_LAMP_CORE * (0.10 * field.y))
                 * (gate * fl);
        }
    }
    return rgb;
}

// One procedural rain depth layer. Each fragment resolves its grid cell and
// evaluates a single short descending streak for it, so the cost is constant
// regardless of how much rain is on screen.
vec3 ac_rain_layer(vec3 rgb, vec2 lp, vec2 logical, float phase,
                   float cellW, float cellH, float cycles,
                   float lenMin, float lenMax, float widthPx, float amp) {
    vec2 cell = floor(lp / vec2(cellW, cellH));
    vec2 local = lp - cell * vec2(cellW, cellH);

    // Upper-right corner thinning: deterministically reject ~25% of whole
    // streak lifecycles in the rightmost ~20% width / top ~25% height, with
    // smooth shoulders fading to zero by 70% width / 40% height. The decision
    // is keyed by the stable cell anchor (cell centre), not the fragment
    // pixel, so a rejected streak disappears whole -- no cropped endpoints or
    // screen-space masks. The unique hash seed keeps this independent of the
    // streak's own offset/lifecycle/length hashes, and the decision carries no
    // phase term, so the 96 s wrap stays seamless.
    vec2 anchor = (cell + 0.5) * vec2(cellW, cellH);
    vec2 af = anchor / max(logical, vec2(1.0));  // anchor fractions in [0, 1]
    float wx = smoothstep(0.70, 0.80, af.x);
    float wy = 1.0 - smoothstep(0.25, 0.40, af.y);
    float rejectP = 0.25 * wx * wy;
    if (rejectP > 0.0 && ac_hash2(cell + vec2(11.3, 17.9)) < rejectP) {
        return rgb;  // whole streak removed before any shape/lighting work
    }

    float hx = ac_hash2(cell + vec2(0.7, 1.3));  // x offset inside the cell
    float hy = ac_hash2(cell + vec2(3.1, 5.9));  // lifecycle offset
    float hl = ac_hash2(cell + vec2(7.7, 2.3));  // length choice

    float halfLen = 0.5 * mix(lenMin, lenMax, hl);
    float xc = (0.15 + 0.70 * hx) * cellW;
    // Integer `cycles` makes `u`, and therefore the whole streak field,
    // identical at phase 0 and phase 1: the 96 s loop is seamless.
    float u = fract(phase * cycles + hy);
    float head = -halfLen + u * (cellH + 2.0 * halfLen);
    float env = sin(AC_PI * u);                 // zero at both lifecycle ends

    float across = 1.0 - smoothstep(0.0, widthPx, abs(local.x - xc));
    float along = 1.0 - smoothstep(halfLen, halfLen + 1.0, abs(local.y - head));

    // Quiet-centre weight: the middle of the content gets a fraction of the
    // background rain, the sides get the full field.
    float cxf = abs(lp.x / max(logical.x, 1.0) - 0.5) * 2.0;
    float quiet = mix(0.40, 1.0, smoothstep(0.12, 0.70, cxf));

    rgb += AC_COLD * (amp * (env * across * along * quiet));
    return rgb;
}

// Rainlit-shoji: two depth layers of short descending streaks distributed over
// the whole content with a quiet centre, plus the actual content lantern. No
// stationary edge columns, no long beams, and no point-glow substitute.
vec3 ac_rainlit(vec3 rgb, vec2 lp, vec2 logical, float phase) {
    // Far layer: smaller cells, fainter, fastest (224 descents per 96 s loop),
    // ~1.2 px wide, 9..17 px long.
    rgb = ac_rain_layer(rgb, lp, logical, phase,
                        44.0, 60.0, 224.0, 9.0, 17.0, 0.6, 0.846);
    // Near layer: larger, brighter (90 descents per 96 s loop),
    // ~2 px wide, 18..31 px long.
    rgb = ac_rain_layer(rgb, lp, logical, phase,
                        70.0, 96.0, 90.0, 18.0, 31.0, 1.0, 1.175);
    return ac_content_lantern(rgb, lp, logical, phase);
}

// Lantern-dusk: both visible lamps with at most three rare analytic motes
// confined to the top-right lamp's near field. The motes keep the top anchor;
// the lower-left lamp adds no extra dust. No rain.
vec3 ac_lantern_dusk(vec3 rgb, vec2 lp, vec2 logical, float phase) {
    rgb = ac_content_lantern(rgb, lp, logical, phase);

    vec2 boxMin, boxMax, pane, reach;
    ac_lamp_box(logical, 0.0, boxMin, boxMax, pane, reach);

    for (int i = 0; i < 3; i++) {
        float fi = float(i);
        float ho = ac_hash(fi * 9.1 + 3.7);
        float hp = ac_hash(fi * 4.3 + 17.3);
        float hd = ac_hash(fi * 6.9 + 29.1);
        float duty = 0.34;
        float lt = fract(phase + ho);

        if (lt < duty) {
            float u = lt / duty;
            float env = sin(AC_PI * u);
            // Drift stays inside the lamp's near field ellipse.
            vec2 pos = pane + vec2(
                reach.x * (0.60 * u - 0.35 - 0.20 * hp),
                reach.y * (-0.55 + 1.10 * u));
            float dd = length(lp - pos);
            float cov = 1.0 - smoothstep(0.0, 2.0 + 2.0 * hd, dd);
            vec2 q = (pos - pane) / reach;
            float lit = exp(-1.6 * dot(q, q));
            rgb += AC_LAMP_CORE * (0.10 * env * cov * lit);
        }
    }

    return rgb;
}

// Moonlit-ink: broad, slowly drifting pointed leaf silhouettes that overlap
// into a soft dapple, multiplied over the backdrop, plus a subtle low-contrast
// mist veil and a faint cool lift from the upper edge. Structural shadow, not a
// flat colour swap.
vec3 ac_moonlit(vec3 src, vec2 p, float ar, float phase) {
    vec3 rgb = src;
    float w = AC_TWO_PI * phase;

    // Four broad leaves on independent slow Lissajous paths. `c.x` is seeded
    // across [0, ar] so coverage spans the whole width at any aspect.
    float shadow = 0.0;
    for (int i = 0; i < 4; i++) {
        float fi = float(i);
        float h1 = ac_hash(fi * 2.1 + 1.3);
        float h2 = ac_hash(fi * 3.7 + 8.9);
        float h3 = ac_hash(fi * 5.3 + 14.7);
        float h4 = ac_hash(fi * 7.9 + 27.1);

        vec2 c = vec2(h1 * ar, h2);
        c += 0.055 * vec2(sin(w + h3 * AC_TWO_PI), cos(w + h4 * AC_TWO_PI));

        float ang = h3 * AC_TWO_PI + 0.35 * sin(w);
        float R = (0.34 + 0.14 * h4) * (0.90 + 0.10 * sin(w + h1 * AC_TWO_PI));
        float W = 0.10 + 0.06 * h2;
        shadow += ac_leaf(p - c, cos(ang), sin(ang), R, W) * (0.55 + 0.45 * h1);
    }

    // Bounded local darkening (never a full-screen dim).
    shadow = clamp(shadow, 0.0, 1.0);
    rgb *= 1.0 - 0.30 * shadow;

    // Low-contrast mist: one broad, slowly travelling spatial phase. Cool bias.
    float mist = 0.5 + 0.5 * sin(w + (p.x * 0.7 + p.y * 0.4) * 1.5);
    rgb += vec3(0.55, 0.66, 0.72) * (0.045 * mist);

    // Faint background light from the upper edge, slowly breathing.
    float moon = (1.0 - smoothstep(0.0, 0.55, p.y))
               * (0.5 + 0.5 * sin(w + 2.0));
    rgb += vec3(0.62, 0.72, 0.80) * (0.03 * moon);

    return rgb;
}

vec4 bamboo_ambient() {
    vec4 base = texture(b_backdrop, b_backdrop_uv.xy + b_uv * b_backdrop_uv.zw);
    if (base.a <= 0.0) {
        return vec4(0.0);
    }

    // Straight-space working colour (the min() guards quantization overshoot).
    vec3 src = min(base.rgb / base.a, vec3(1.0));

    vec2 csize = max(logical_content_size * b_scale, vec2(1.0));
    vec2 logical = max(logical_content_size, vec2(1.0));
    float ar = csize.x / csize.y;
    vec2 cp = b_uv * csize;
    vec2 p = cp / csize.y;                  // aspect-correct height units
    // Content-rect physical -> logical px scale, for the lamp and rain grid.
    vec2 lp = cp * logical / csize;

    // Smoked-glass base: edge-weighted dim/cool, identical bounds to the
    // stage this shader replaces, so nothing is dimmed beyond the existing
    // smoke floor.
    float dEdge = min(min(cp.x, csize.x - cp.x), min(cp.y, csize.y - cp.y));
    float periph = 1.0 - clamp(dEdge / AC_PERIMETER_PX, 0.0, 1.0);
    float smoke = mix(AC_SMOKE_MIN, AC_SMOKE_MAX, periph);
    src = mix(src, src * AC_SMOKE_DIM, smoke);

    vec3 rgb;
    if (composition < 0.5) {
        rgb = ac_rainlit(src, lp, logical, phase);
    } else if (composition < 1.5) {
        rgb = ac_lantern_dusk(src, lp, logical, phase);
    } else {
        rgb = ac_moonlit(src, p, ar, phase);
    }

    rgb = clamp(rgb, 0.0, 1.0);
    // Re-premultiply: RGB <= alpha by construction, alpha preserved exactly.
    return vec4(rgb * base.a, base.a);
}

void main() { b_output = bamboo_ambient(); }
