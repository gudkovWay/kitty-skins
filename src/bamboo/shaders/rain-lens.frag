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
// Sparse wet-glass episodes for the approved rainlit-shoji configuration.
//
// This is the second and final stage of the Kitty weather pipeline. It runs
// over the full captured source (client, frame and embedded subsurfaces) that
// rain-light.frag already lit, and replaces the window render: the pixels
// below the glass are the real glyphs and UI, not a copy drawn on top. Water
// exists only under the sparse heads, the parts they separate into and their
// bounded tails, so dry pixels, other profiles, the chrome and the frame are
// returned untouched.
//
// Geometry is in logical pixels relative to the frame's top-left. The native
// client opening is x = border_px..W-border_px, y = border_px..H-border_px --
// the unified frame reserves the same 26 px on all four sides and has no
// titlebar -- and `border_px` is the consumer-supplied reserved border width.
// Outside that opening the captured colour is passed through unchanged. All
// displaced and blurred samples are clamped inside the opening with a
// half-texel inset, so nothing warps or samples the frame, the corners or the
// native header controls.
//
// The captured source arrives premultiplied (rain-light returns premultiplied
// RGB with the original alpha). `rl_straight` unpremultiplies safely -- zero
// alpha stays zero -- and the function returns straight RGB with the original
// per-pixel alpha, because the native final display wrapper multiplies RGB by
// alpha once. A zero-alpha pixel returns zero.
//
// Water is persistent, not a wall-clock schedule. rain-motion.frag advances a
// packed GPU state of up to 24 episode buckets, each started in its own four
// second slot of the 96 s phase loop and each living as long as its content
// controller and independent lifetime let it (a head holds under contact,
// releases with a 150 ms ramp and 450 ms recover, and breaks -- fading over
// .18/.30 s -- once its hashed age or actual contact dose threshold is hit;
// the 96 s bucket cycle never overwrites a living drop).
// This stage only *reads* that state: per active bucket it reconstructs the
// same parent plans, paths and split geometry as rain-motion.frag (the `rl_`
// helpers below are verbatim twins of the `rm_` helpers there and MUST stay in
// lockstep) and runs the unchanged droplet optics. The state carries, per
// head, the nominal cursor, the controller multiplier, the independent age /
// contact dose / break age, the frozen break cursor and multiplier, and a
// status; waiting children and dead heads are rejected before any optic work,
// and a bucket outside the live camera window is rejected whole by its
// bounding box. Bucket header1.z carries the layout magic (5); an
// incompatible value rejects the bucket whole.
//
// Families rotate with slot mod 3: the paired diagonal family (family 0) has
// been removed and its slots never spawn, so only family 1 (the centre
// splitter) and family 2 (the big side drop) remain. Two further eligible
// births are suppressed -- ordinals 3 and 10, the slots 5 and 16 -- so the two
// families birth 14 times per loop instead of 16: 7 centre, 5 right and 2 left
// (family 2's slot mod 12 == 8 slots):
//
//   1  one drop starts in the top centre and falls down the middle;
//   2  a bigger side drop creeps to a pin, then detaches into a freefall; it
//      starts on the right except on its two sparse left slots, whose start x
//      and horizontal lean mirror the right-side path.
// Every parent path now ends centered at 96% of the opening height, and a
// parent breaks at a hashed 80..90% impact height (never above 75%).
//
// Every start position, travel, speed, impact height, part count, part radius
// and lifetime is hashed from the slot index, so the geometry is deterministic
// for a slot; the two families and their hashes differ from slot to slot and
// the examples above are only the shape of the motion, not fixed coordinates.
//
// A head's motion is one monotone progress curve over its nominal cursor. An
// ordinary head uses p(s) = (2s + (r-1)s^2)/(1+r) with r the end/start speed
// ratio: r < 1 falls fast and then creeps, r > 1 crawls and then accelerates,
// r = 1 slides steadily. The right-side parent instead creeps to a dead stop
// at a fixed pin and then freefalls to its impact, so the detach reads as a
// genuine release rather than a gradual speed-up. The content controller
// scales the real speed and the cursor carries the global 1.5x motion scale,
// so the instantaneous velocity used for the tail and the speed-shaped
// silhouette is `Dsp * progd(s)/T * multiplier * 1.5 / 8`; a part's `Dsp`
// bakes its parent's frozen break velocity -- already scaled by the frozen
// break multiplier --
// exactly once, with no second inherited factor. Parent heads are elliptical,
// with independently hashed width and height and a shape that follows the
// instantaneous speed -- elongated while falling, squat while creeping. Part
// heads are born at exactly the parent's frozen break position with the
// parent's frozen break velocity plus a bounded angular kick, so the handoff
// is continuous and no part appears as a giant birth blob. A part is drawn as
// a thin tapered stroke (1..2 px wide, 8..20 px long) whose lifespan fade
// changes only its opacity, never its length; both parent and parts fade out
// temporally over .18/.30 s once their lifetime threshold triggers (breakAge),
// independent of the path schedule. Every parent path now ends centered at 96%
// of the opening height; a parent's temporal breakup is gated by the exact
// lower-quarter (75% height) crossing and a spatial impact breakup fires at a
// hashed 80..90% height before that endpoint, while children keep their own
// ungated clocks.
//
// Cost is bounded per pixel. One capture fetch, then an unrolled loop over at
// most 24 buckets that rejects inactive ones on one state read and out-of-box
// ones before any head work. A pixel inside a box evaluates at most five heads;
// a head that is not near the pixel fails one normalized-length test. The
// winner is the single largest-coverage feature across all buckets, so the
// optics run once and only a wet pixel pays for the displaced sample plus the
// fixed 5-tap cross blur -- five taps against a dry pixel's one. There is no
// per-particle loop over pixels, no whole-frame blur, tint or wash; the
// displacement is clamped to a small fraction of the head radius so the
// sampling can never fold over.

uniform float phase;       // caller-owned loop in [0, 1] over 96 s
uniform float composition; // 0 rainlit-shoji, 1 lantern-dusk, 2 moonlit-ink
uniform vec2 logical_size; // root logical size
uniform float border_px;   // reserved native border width (26)
uniform sampler2D drop_motion; // rain-drop-motion-v5 state (normalized raw UV)
uniform sampler2D content_mask; // rain-content-mask-v1 occupancy (diagnostic only)
uniform sampler2D content_background; // rain-content-background-v1 reference (mode2 bands)
uniform float diagnostic_mode; // 0: normal optics; 1: pure mask + winner overlay; 2: readable capture + mask overlay + top reference bands; 3: bounded 128x32 top-left state readout (normal optics outside the corner)

const float RL_SLOTS = 24.0;     // episode starts per 96 s loop (bucket cap)
const float RL_SLOT_SEC = 8.0;   // seconds of motion per episode life
const int RL_HEADS = 5;          // bounded candidates: 1 parent + 4 parts
const float RL_BUCKET = 22.0;    // state texels per episode bucket
const float RL_MAGIC = 5.0;      // rain-drop-motion-v5 layout marker
const float RL_SPEED = 1.5;      // global motion speed scale (matches producer)
const float RL_DEG = 0.0174532925;
const float RL_PI = 3.14159265;

const vec3 RL_COLD = vec3(0.616, 0.796, 0.835); // cool sky #9CCBD5
// Cold sky/paper key light: the specular sits on the upper-left of a drop.
// There is deliberately no warm reflection: the local lantern light already
// lives in the captured source this stage refracts.
const vec2 RL_LIGHT_DIR = vec2(-0.51, -0.86);

float rl_hash(vec2 c) {
    return fract(sin(dot(c, vec2(127.1, 311.7))) * 43758.5453);
}

float rl_h(float a, float b) {
    return rl_hash(vec2(a, b));
}

// Family 2's two sparse left slots: slot mod 12 == 8 (slots 8 and 20). They
// mirror the right-side family's start x and horizontal lean to the left while
// its other births stay on the right, so a 96 s loop has 7 centre, 5 right and
// 2 left births. Deterministic; a pure function of the slot. Verbatim twin of
// rm_is_left in rain-motion.frag.
bool rl_is_left(float slot) {
    return abs(mod(slot, 12.0) - 8.0) < 0.5;
}

// Unpremultiply safely: a zero-alpha sample stays transparent and can never
// become an opaque wash. Valid premultiplied RGB never exceeds alpha, so the
// min() only guards against quantized overshoot.
vec4 rl_straight(vec4 pm) {
    if (pm.a <= 0.0) {
        return vec4(0.0);
    }
    return vec4(min(pm.rgb / pm.a, vec3(1.0)), pm.a);
}

vec2 rl_to_uv(vec2 lp, vec2 frameOrigin, vec2 scale, vec2 texSize) {
    return (frameOrigin + lp * scale) / texSize;
}

// Monotone progress in [0, 1] over a head's life; p(0) = 0, p(1) = 1.
// For an ordinary head `r` is the end/start speed ratio: 1 slides evenly,
// r < 1 decelerates from a fast fall into a slow creep, r > 1 accelerates out
// of a crawl. The right-side parent (r > 2) instead uses its own piecewise
// curve: it decelerates to a dead stop -- the pin -- at the detach fraction
// `a`, then accelerates from rest into the freefall. Position and velocity are
// continuous at the pin, so a part born there inherits the parent exactly.
float rl_prog(float s, float r) {
    if (r > 2.0) {
        float a = 0.62; // detach fraction: the pin release
        float b = 0.56; // progress already made when it lets go
        if (s < a) {
            float x = s / a;
            return b * (2.0 * x - x * x); // creep, easing to zero speed
        }
        float z = (s - a) / (1.0 - a);
        return b + (1.0 - b) * z * z;     // freefall, accelerating from rest
    }
    return (2.0 * s + (r - 1.0) * s * s) / (1.0 + r);
}

// d(progress)/ds -- the shape factor of the instantaneous speed. It is zero at
// the right-side parent's pin, so the detach is a genuine stop, not a kink.
float rl_progd(float s, float r) {
    if (r > 2.0) {
        float a = 0.62;
        float b = 0.56;
        if (s < a) {
            return 2.0 * b / a * (1.0 - s / a);
        }
        float z = (s - a) / (1.0 - a);
        return 2.0 * (1.0 - b) / (1.0 - a) * z;
    }
    return (2.0 + 2.0 * (r - 1.0) * s) / (1.0 + r);
}

// Rotate a vector by a small angle (explicit 2x2, no matrix type needed).
vec2 rl_rot(vec2 v, float a) {
    float c = cos(a);
    float s = sin(a);
    return vec2(c * v.x - s * v.y, s * v.x + c * v.y);
}

// Parent position/velocity at time `tt` (u units), both exact for the same
// progress curve the pixel loop uses, so a part can be born at the parent's
// own split coordinates.
vec2 rl_ppos(vec2 p0, vec2 d, float tt, float t0, float t, float r) {
    return p0 + d * rl_prog(clamp((tt - t0) / max(t, 0.0001), 0.0, 1.0), r);
}

vec2 rl_pvel(vec2 d, float tt, float t0, float t, float r) {
    return d * (rl_progd(clamp((tt - t0) / max(t, 0.0001), 0.0, 1.0), r) / max(t, 0.0001));
}

// Episode plan for start time, death time, speed ratio and base half size.
// Slot-hashed so the whole 96 s loop repeats exactly. Verbatim twin of
// rm_parent_plan in rain-motion.frag.
vec4 rl_parent_plan(float slot, float fam) {
    float h1 = rl_h(slot, 301.0);
    float h2 = rl_h(slot, 307.0);
    float h4 = rl_h(slot, 319.0);
    float h5 = rl_h(slot, 325.0);
    float t0 = mix(0.02, 0.07, h1);
    float dead = mix(0.62, 0.76, h2); // fades out well before its life ends
    float r = 0.95;
    float rbase = mix(11.0, 19.0, h4); // main parents: 22-38 px diameter
    if (fam < 1.5) {
        r = mix(0.92, 1.10, h5);          // near-constant fall to mid height
    } else {
        r = mix(2.6, 3.8, h5);            // slow creep that detaches into a fall
        rbase = mix(18.0, 28.0, h4);      // the bigger right-side drop: 36-56 px
    }
    return vec4(t0, dead, r, rbase);
}

// Start position of a parent episode, split into opening-relative x and y.
// Hashes 331/337 verbatim twins of rain-motion.frag.
float rl_parent_start_x(float slot, float fam) {
    float h1 = rl_h(slot, 331.0);
    if (fam < 1.5) {
        return mix(0.42, 0.58, h1);
    }
    // Family 2's sparse left slots mirror the right-side start x = 1 - x, so
    // the right .76.. .90 span becomes the left .10.. .24 span.
    if (rl_is_left(slot)) {
        return 1.0 - mix(0.76, 0.90, h1);
    }
    return mix(0.76, 0.90, h1);
}

float rl_parent_start_y(float slot, float fam) {
    float h2 = rl_h(slot, 337.0);
    if (fam < 1.5) {
        return mix(0.0, 0.04, h2);
    }
    return mix(0.05, 0.12, h2);
}

// Where the episode starts: family 1 uses the top centre, family 2 the side --
// the right span for its five right slots and the mirrored left span for its
// two sparse left slots (rl_is_left).
vec2 rl_parent_p0(float slot, float fam, vec2 o, vec2 cs) {
    return o + vec2(cs.x * rl_parent_start_x(slot, fam),
                    cs.y * rl_parent_start_y(slot, fam));
}

// Total travel of a parent over its whole life. Every parent path now ends
// centered at 96% of the opening height (disp.y = .96 * cs.y - startY from the
// same
// hash337 family mix), the family direction (the hash357 angle) is retained as
// the dx/dy ratio and dx is clamped so the final centre x stays inside 2..98% of
// the opening.
// Verbatim twin of rm_parent_disp in rain-motion.frag.
vec2 rl_parent_disp(float slot, float fam, vec2 cs) {
    float h2 = rl_h(slot, 357.0);
    float sy = rl_parent_start_y(slot, fam);
    float dy = cs.y * (0.96 - sy);
    float dx;
    if (fam < 1.5) {
        float th = mix(-5.0, 5.0, h2) * RL_DEG;
        dx = clamp(dy * (sin(th) / cos(th)), -cs.x * 0.12, cs.x * 0.12);
    } else {
        float th = mix(-4.0, 4.0, h2) * RL_DEG;
        dx = clamp(dy * (sin(th) / cos(th)), -cs.x * 0.12, cs.x * 0.12);
        // Mirror the side-family lean on the sparse left slots before the
        // opening clamp, so the left path is the right path reflected in x.
        if (rl_is_left(slot)) {
            dx = -dx;
        }
    }
    float sx = rl_parent_start_x(slot, fam);
    dx = clamp(dx, cs.x * (0.02 - sx), cs.x * (0.98 - sx));
    return vec2(dx, dy);
}

// Exact-center texel addressing for the packed motion state.
vec2 rl_texel_uv(float index, vec2 size) {
    float ix = mod(index, size.x);
    float iy = floor(index / size.x);
    return (vec2(ix, iy) + 0.5) / size;
}

vec4 rl_read(sampler2D st, float index, vec2 size) {
    return texture2D(st, rl_texel_uv(index, size));
}

vec4 shader_main(EffectContext effect) {
    vec4 src = texture2D(tex, effect.texture_uv);
    if (src.a <= 0.0) {
        return vec4(0.0);
    }
    vec4 straight = rl_straight(src); // exact-passthrough value

    // Water belongs to the rainlit profile only; lantern-dusk and moonlit-ink
    // (and any future profile index) pass the captured source through exactly.
    if (composition > 0.5) {
        return straight;
    }

    vec2 logical = max(logical_size, vec2(1.0));
    vec2 framePx = max(effect.frame_rect_px.zw, vec2(1.0));
    vec2 texPx = max(effect.texture_size_px, vec2(1.0));
    vec2 scale = framePx / logical;
    vec2 frameOrigin = effect.frame_rect_px.xy;
    vec2 p = effect_frame_px(effect) / scale;

    // Reserved border only: the unified frame has no titlebar, so the opening
    // is the same 26 px band on all four sides. Clamped at zero so a bad
    // binding can never open the protected frame.
    float border = max(border_px, 0.0);
    float x0 = border;
    float x1 = logical.x - border;
    float y0 = border;
    float y1 = logical.y - border;

    // Degenerate opening or outside the client area: exact passthrough.
    if (x1 - x0 <= 1.0 || y1 - y0 <= 1.0) {
        return straight;
    }
    if (p.x < x0 || p.x > x1 || p.y < y0 || p.y > y1) {
        return straight;
    }

    // Valid sample range for this opening: the client area mapped to texture
    // coordinates, inset by half a texel so the frame is never touched by a
    // displaced or blurred tap.
    vec2 halfTexel = vec2(0.5) / texPx;
    vec2 openLo = max(rl_to_uv(vec2(x0, y0), frameOrigin, scale, texPx) + halfTexel, halfTexel);
    vec2 openHi = min(rl_to_uv(vec2(x1, y1), frameOrigin, scale, texPx) - halfTexel,
                      vec2(1.0) - halfTexel);
    if (openLo.x >= openHi.x || openLo.y >= openHi.y) {
        return straight; // opening smaller than a texel: passthrough
    }

    vec2 o = vec2(x0, y0);
    vec2 cs = vec2(max(x1 - x0, 1.0), max(y1 - y0, 1.0));

    // The packed motion state keeps native ~capture * 0.125 dimensions; derive
    // them the same way the producer did (quarter-pixel rounding tolerance).
    vec2 motionSize = max(vec2(1.0), floor(texPx * 0.125 + 0.5));
    float motionTexels = motionSize.x * motionSize.y;
    float capacity = min(RL_SLOTS, floor(motionTexels / RL_BUCKET));

    // Diagnostic mode 3 (RAIN_DIAGNOSTIC_MODE in rain-weather.ts): a bounded
    // 128 x 32 logical-pixel readout in the top-left corner *inside* the
    // protected opening, reporting the actual motion-state facts rather than
    // the pixel's own visibility. It is deliberately not a full-screen mask, a
    // winner recolour or reference bands: only pixels inside that corner
    // return here, every other pixel falls through to the unchanged normal
    // optics below, and the original alpha is preserved. Four 8-px rows, each
    // a gray baseline with a coloured fill of width 128 * value:
    //   row 1 blue   -- caller phase, 0..1 (no state fetch at all);
    //   row 2 green  -- bucket 0's header1.z / RL_MAGIC, i.e. the layout magic
    //                   (full = expected magic 5; a stale magic 4 reads as 4/5
    //                   of the bar; zero = missing state; no fetch when no
    //                   bucket exists);
    //   row 3 cyan   -- alive head count / 12, saturated, across buckets whose
    //                   header is active, not a dead sentinel and magic 5;
    //   row 4 yellow -- maximum actual age / 8 s over those same alive heads.
    // Rows 3/4 share one bounded 24-bucket x 5-head scan that reads only
    // headers and statuses; an age record is read only after a valid active
    // header and an alive status. The scan never consults the current pixel's
    // wet bounding boxes -- it reports the whole state, not the pixel's own
    // visibility. No state is written, read back, logged or duplicated.
    if (diagnostic_mode > 2.5 && p.x - x0 < 128.0 && p.y - y0 < 32.0) {
        float rowIdx = p.y - y0;
        float xoff = p.x - x0;
        float value = 0.0;
        vec3 fill = vec3(0.4);
        if (rowIdx < 8.0) {
            // Row 1: the caller's own phase. No state fetch.
            fill = vec3(0.15, 0.45, 1.0);
            value = clamp(phase, 0.0, 1.0);
        } else if (rowIdx < 16.0) {
            // Row 2: exactly one header fetch, bucket 0's layout magic.
            fill = vec3(0.10, 0.90, 0.25);
            if (capacity >= 1.0) {
                vec4 hdr0 = rl_read(drop_motion, 1.0, motionSize);
                value = clamp(hdr0.z / RL_MAGIC, 0.0, 1.0);
            }
        } else {
            // Rows 3/4: the bounded 24 x 5 scan. Row 3 pays status reads only;
            // row 4 additionally pays an age read per alive head.
            float wantAge = (rowIdx >= 24.0) ? 1.0 : 0.0;
            fill = (wantAge > 0.5) ? vec3(1.00, 0.85, 0.10)
                                   : vec3(0.10, 0.90, 0.95);
            float count = 0.0;
            float maxAge = 0.0;
            for (int bi = 0; bi < 24; bi++) {
                float bf = float(bi);
                if (bf >= capacity) {
                    continue;
                }
                float dbase = bf * RL_BUCKET;
                vec4 hdr = rl_read(drop_motion, dbase + 1.0, motionSize);
                if (hdr.y < 0.5 || hdr.x < -0.5 || abs(hdr.z - RL_MAGIC) > 0.01) {
                    continue;
                }
                for (int i = 0; i < RL_HEADS; i++) {
                    float fi = float(i);
                    vec4 d1 = rl_read(drop_motion, dbase + 3.0 + fi * 4.0,
                                      motionSize);
                    if (d1.z < 0.5 || d1.z > 1.5) {
                        continue; // waiting child or dead/absent head
                    }
                    count += 1.0;
                    if (wantAge > 0.5) {
                        vec4 d2 = rl_read(drop_motion, dbase + 4.0 + fi * 4.0,
                                          motionSize);
                        maxAge = max(maxAge, d2.x);
                    }
                }
            }
            value = (wantAge > 0.5)
                ? clamp(maxAge / 8.0, 0.0, 1.0)
                : clamp(count / 12.0, 0.0, 1.0);
        }
        vec3 band = (xoff < 128.0 * value) ? fill : vec3(0.4);
        return vec4(band, src.a);
    }

    // --- dominant feature across all live buckets: largest coverage wins ---
    float bestW = 0.0;
    float bestKind = 0.0; // 0 = head, 1 = tail
    vec2 bestC = vec2(0.0);
    vec2 bestRad = vec2(1.0);
    vec2 bestDir = vec2(0.0, 1.0);
    float bestHalf = 1.0;
    float bestAmp = 1.0;
    float bestBlur = 1.0;
    float bestSpec = 0.5; // body/rim-highlight hash of the winning head
    float bestRim = 0.5;  // dark-rim strength hash of the winning head
    float bestContact = 0.0; // winning head's stored status r1.z (diagnostic)
    float bestHold = 0.0;    // winning head's remaining release hold (diagnostic)

    for (int bi = 0; bi < 24; bi++) {
        float bf = float(bi);
        if (bf >= capacity) {
            continue; // no bucket allocated for this lane
        }
        float base = bf * RL_BUCKET;
        vec4 hdr = rl_read(drop_motion, base + 1.0, motionSize);
        if (hdr.y < 0.5) {
            continue; // inactive bucket
        }
        float slot = hdr.x;
        if (slot < -0.5) {
            continue; // dead sentinel
        }
        if (abs(hdr.z - RL_MAGIC) > 0.01) {
            continue; // incompatible producer layout
        }
        float fam = mod(slot, 3.0);

        // --- the slot's single parent (head 0) ----------------------------
        vec4 paPlan = rl_parent_plan(slot, fam);
        vec2 paP0 = rl_parent_p0(slot, fam, o, cs);
        vec2 paD = rl_parent_disp(slot, fam, cs);

        // --- wet bounds: the fixed box the episode can possibly reach ------
        // Every part is born inside its parent's swept box and separates by at
        // most `reach`, so this box plus the head radius and the bounded tail
        // contains all water. radMax = rbase * 1.60 covers the worst per-axis
        // aspect 1.508 (0.82-1.30 speed factor x 0.88-1.16 shape) of a 28 px
        // right-side parent, and the +34 px covers the 30 px tail plus slack.
        // Pixels outside it never enter this bucket's head loop.
        vec2 reach = vec2(cs.x * 0.18, cs.y * 0.22);
        vec2 bbLo = min(paP0, paP0 + paD);
        vec2 bbHi = max(paP0, paP0 + paD);
        float radMax = paPlan.w * 1.60 + 2.0;
        vec2 bpad = reach + vec2(radMax + 34.0); // 34 px: longest tail
        if (p.x < bbLo.x - bpad.x || p.x > bbHi.x + bpad.x
            || p.y < bbLo.y - bpad.y || p.y > bbHi.y + bpad.y) {
            continue; // dry for this bucket, but another may be wet
        }

        // --- five stored heads: 0 the parent, 1..4 the separate parts -----
        // The winner is the largest-coverage feature across every live bucket,
        // so overlaps resolve without summing into an over-strong lens and
        // every stored optic field is written together on a win.
        for (int i = 0; i < RL_HEADS; i++) {
            float fi = float(i);
            // Four records per head: r0 = cursor hi/lo + speed multiplier,
            // r1 = continuous clear seconds + release-start multiplier +
            // status, r2 = independent age + actual contact dose + break age,
            // r3 = frozen break cursor hi/lo + frozen break multiplier.
            vec4 r0 = rl_read(drop_motion, base + 2.0 + fi * 4.0, motionSize);
            vec4 r1 = rl_read(drop_motion, base + 3.0 + fi * 4.0, motionSize);
            float status = r1.z;
            // Status 0 is a still-waiting child, 2 a dead/absent head; 1 clear
            // and 1.25 actual contact are both alive.
            if (status < 0.5 || status > 1.5) {
                continue;
            }
            vec4 r2 = rl_read(drop_motion, base + 4.0 + fi * 4.0, motionSize);
            float cursor = r0.x + r0.y;
            float mult = r0.z;      // content controller multiplier
            float age = r2.x;       // independent real age seconds
            float breakAge = r2.z;  // 0: lifetime threshold not yet hit

            // The single parent is always head 0. This mirrors
            // rain-motion.frag exactly.
            vec4 pp = paPlan;
            vec2 pp0 = paP0;
            vec2 pdp = paD;
            float pT = pp.y - pp.x;

            float t0 = 0.0;
            float T = 1.0;
            float rr = 1.0;
            float rbase = 1.0;
            vec2 P0 = pp0;
            vec2 Dsp = pdp;

            if (fi < 0.5) {
                // Whole drop: head 0 is the single parent. It follows the plan
                // path with the live controller multiplier and fades
                // temporally from its break age.
                t0 = pp.x;
                T = pT;
                rr = pp.z;
                rbase = pp.w;
                P0 = pp0;
                Dsp = pdp;
            } else {
                // A part separated from its parent at the actual lifetime
                // trigger: born at the parent's frozen break cursor with the
                // frozen break velocity, whose frozen multiplier is applied
                // exactly once here, plus a bounded kick.
                float k2 = rl_h(slot, 402.0 + fi * 11.0);
                float k3 = rl_h(slot, 403.0 + fi * 11.0);
                float k4 = rl_h(slot, 404.0 + fi * 11.0);
                float k5 = rl_h(slot, 405.0 + fi * 11.0);
                // The frozen break record is always the single parent's r3.
                vec4 pr3 = rl_read(drop_motion, base + 5.0, motionSize);
                float split = pr3.x + pr3.y;
                vec2 splitPos = rl_ppos(pp0, pdp, split, pp.x, pT, pp.z);
                // A parent frozen exactly at its clamped endpoint is
                // stationary: no inherited velocity for the part.
                vec2 splitVel = (split < pp.x + pT)
                    ? rl_pvel(pdp, split, pp.x, pT, pp.z) * pr3.z
                    : vec2(0.0);
                vec2 v0 = rl_rot(splitVel, mix(-0.40, 0.40, k5))
                    * mix(1.10, 1.60, k2);
                T = mix(0.16, 0.28, k2);                    // gone by its own age
                rr = mix(0.16, 0.42, k3);                   // fast fall, then creep
                rbase = clamp(pp.w * mix(0.25, 0.45, k4), 4.0, 10.0); // varied
                P0 = splitPos;
                // Exact displacement for this progress curve, then a per-axis
                // cap that keeps the water a local feature on any window
                // aspect. No second inherited multiplier: the frozen break
                // velocity above already carries it.
                Dsp = v0 * (T * (1.0 + rr) * 0.5);
                Dsp.x = clamp(Dsp.x, -reach.x, reach.x);
                Dsp.y = clamp(Dsp.y, -reach.y, reach.y);
            }

            if (fi < 0.5 && cursor < t0) {
                continue; // still inside the parent's planned start delay
            }
            float tau = fi < 0.5 ? (cursor - t0) : cursor;
            float s = tau / max(T, 0.0001);
            if (s <= 0.0) {
                continue;
            }
            s = min(s, 1.0); // alive past nominal end: clamp, do not reject

            // Birth ramp on the head's visible real age, not on the path
            // cursor: the controller slows the cursor, so gating opacity and
            // growth on `s` would stretch a slowed drop's fade-in far past its
            // real lifetime. Normalized by the nominal traversal time
            // T * RL_SLOT_SEC, so the ramp is the same shape at any speed.
            float birthProgress = max(age, 0.0) / (max(T, 0.0001) * RL_SLOT_SEC);

            // Optic hashes, only paid for a head that is live on the pixel.
            float hAmp = rl_h(slot, 701.0 + fi * 3.0);
            float hBlur = rl_h(slot, 709.0 + fi * 3.0);
            // Blur scales with the drop: small fragments stay sharp, big
            // bodies spread over up to 2.5 px.
            float bscale = clamp((rbase - 3.0) / 22.0, 0.55, 1.0);

            vec2 c = P0 + Dsp * rl_prog(s, rr);
            // A head clamped at its nominal endpoint stays alive but
            // stationary: no tail, no moving-silhouette speed.
            vec2 vel = (tau < max(T, 0.0001))
                ? Dsp * (rl_progd(s, rr) / max(T, 0.0001)) // px per u
                : vec2(0.0);
            float spdU = length(vel);
            // Real speed: the cursor integrates the controller multiplier and
            // the global 1.5x motion scale, so the per-second speed carries
            // both (and, for a part, the frozen break multiplier already
            // inside Dsp -- never a second inherited factor).
            float spd = spdU * mult * RL_SPEED / RL_SLOT_SEC;

            // Opacity ramps on the head's age-based birth progress; death is
            // temporal, driven by the head's independent age against its
            // frozen break age: .18 s for a parent, .30 s for a part.
            float fadeDur = fi < 0.5 ? 0.18 : 0.30;
            float env = smoothstep(0.0, 0.13, birthProgress)
                * (breakAge > 0.0
                    ? 1.0 - smoothstep(0.0, fadeDur, age - breakAge)
                    : 1.0);

            if (fi < 0.5) {
                // Parent: unchanged elliptical body plus its bounded tail. The
                // speed shape elongates a falling head and squats a creeping
                // one; a slot/head-hashed per-axis factor folds in independent
                // width and height on top of rbase.
                float hSpec = rl_h(slot, 760.0 + fi * 3.0);
                float fe = clamp((spd - 14.0) / 105.0, 0.0, 1.0);
                float grow = mix(0.42, 1.0,
                                 smoothstep(0.0, 0.22, birthProgress));
                vec2 shape = vec2(
                    mix(0.88, 1.16, rl_h(slot, 727.0 + fi * 3.0)),
                    mix(0.88, 1.16, rl_h(slot, 739.0 + fi * 3.0)));
                vec2 rad = rbase * grow * shape
                    * vec2(mix(1.22, 0.78, fe), mix(0.82, 1.30, fe));
                vec2 rv = p - c;
                float qn = length(rv / max(rad, vec2(0.02)));
                float cov = 0.0;
                if (qn < 1.0) {
                    cov = (1.0 - smoothstep(0.60, 1.0, qn)) * env;
                }
                if (cov > bestW) {
                    bestW = cov;
                    bestKind = 0.0;
                    bestC = c;
                    bestRad = rad;
                    bestDir = (spdU > 0.0001) ? (vel / spdU) : vec2(0.0, 1.0);
                    bestAmp = mix(2.0, 6.0, hAmp);    // 2-6 logical px
                    bestBlur = mix(0.8, 2.5, hBlur) * bscale;
                    bestSpec = hSpec;
                    bestRim = hBlur;
                    bestContact = status;
                    bestHold = max(0.25 - r1.x, 0.0);
                }

                // Tapered tail behind a head that is actually moving: bounded
                // in length, fading to exactly zero at both ends.
                if (cov < 0.98 && spd > 15.0) {
                    vec2 tdir = vel / spdU;
                    float tlen = clamp(spd * 0.24, 5.0, 30.0);
                    float along = -dot(rv, tdir);
                    if (along > 0.0 && along < tlen) {
                        float at = along / tlen;
                        float halfw = rad.x * mix(0.85, 0.24, at);
                        float tw = (1.0 - smoothstep(0.4, 1.0, at))
                            * (1.0 - smoothstep(0.45, 1.0,
                                abs(dot(rv, vec2(-tdir.y, tdir.x)))
                                / max(halfw, 0.4)));
                        tw *= env;
                        if (tw > bestW) {
                            bestW = tw;
                            bestKind = 1.0;
                            bestC = c;
                            bestDir = tdir;
                            bestHalf = halfw;
                            bestAmp = mix(1.2, 3.2, hAmp);
                            bestBlur = mix(0.5, 1.6, hBlur) * bscale;
                            bestContact = status;
                            bestHold = max(0.25 - r1.x, 0.0);
                        }
                    }
                }
            } else {
                // Child: a thin tapered stroke only, never a round body. Width
                // 1..2 logical px and length 8..20 logical px are stable per
                // slot/head; the orientation follows the actual velocity with a
                // downward fallback. The lifespan fade changes opacity, never
                // the stroke length, and the stroke is not gated by the parent
                // tail's speed threshold. It reuses the winner 'tail' optics
                // (bestKind = 1), so no new compositing pipeline is needed and
                // the producer's circular enclosing stroke footprint covers the
                // same geometry.
                float cLen = mix(8.0, 20.0, rl_h(slot, 833.0 + fi * 13.0));
                float cWid = mix(1.0, 2.0, rl_h(slot, 839.0 + fi * 13.0));
                vec2 cdir = (spdU > 0.0001) ? (vel / spdU) : vec2(0.0, 1.0);
                float halfL = cLen * 0.5;
                float halfW = cWid * 0.5;
                vec2 rv = p - c;
                float along = dot(rv, cdir);
                float across = abs(dot(rv, vec2(-cdir.y, cdir.x)));
                float an = clamp(along / max(halfL, 0.001), -1.0, 1.0);
                float hw = halfW * mix(0.25, 1.0, (an + 1.0) * 0.5);
                float scov = (1.0 - smoothstep(0.45 * hw, hw + 0.35, across))
                    * (1.0 - smoothstep(0.55 * halfL, halfL, abs(along)))
                    * env;
                if (scov > bestW) {
                    bestW = scov;
                    bestKind = 1.0;
                    bestC = c;
                    bestDir = cdir;
                    bestHalf = halfW;
                    bestAmp = mix(0.4, 1.2, hAmp);
                    bestBlur = mix(0.3, 0.9, hBlur) * bscale;
                    bestContact = status;
                    bestHold = max(0.25 - r1.x, 0.0);
                }
            }
        }
    }

    // Temporary diagnostic view (RAIN_DIAGNOSTIC_MODE in rain-weather.ts):
    // 0 normal optics; 1 pure grayscale occupancy mask over the client
    // opening overlaid where the winning head/tail covers the pixel -- cyan
    // for actual contact (status > 1.125 from rm_step_head's swept
    // footprint, never hold), yellow for a clear head whose continuous clear
    // is still under the .25 s release hold (max(.25 - clear, 0) > 0), red
    // for clear and past the hold; 2 readable: original
    // straight capture with a faint magenta mask overlay plus the same
    // winner overlay, and the top 32 logical px inside the opening are four
    // 8-px bands (mask reason code G, background reference RGB, per-field
    // status G palette over the left quarter then chosen alpha / max probe
    // alpha / computed center alpha grayscale across the remaining
    // quarters, and mask raw-alpha grayscale)
    // returned without the drop overlay
    // so the flags stay readable. Optics are skipped entirely.
    // Modes 1 and 2 only: mode 3 already returned inside its own corner above
    // and falls through to the normal optics everywhere else.
    if (diagnostic_mode > 0.5 && diagnostic_mode < 2.5) {
        vec4 maskData = texture2D(content_mask, effect.texture_uv);
        float m = maskData.r;
        vec3 diag = (bestContact > 1.125) ? vec3(0.0, 1.0, 1.0)
                  : (bestHold > 0.0) ? vec3(1.0, 1.0, 0.0)
                  : vec3(1.0, 0.0, 0.0);
        vec3 base = vec3(m);
        if (diagnostic_mode > 1.5) {
            base = mix(straight.rgb, vec3(1.0, 0.0, 1.0), 0.28 * m);
            float band = p.y - y0;
            if (band < 32.0) {
                vec3 bandc;
                if (band < 8.0) {
                    float g = maskData.g;
                    bandc = (g < 0.1) ? vec3(0.4)          // unwritten
                          : (g < 0.3) ? vec3(0.6, 0.0, 1.0) // degenerate opening
                          : (g < 0.5) ? vec3(0.0, 0.3, 1.0) // outside opening
                          : (g < 0.7) ? vec3(1.0, 0.4, 0.0) // invalid reference
                          : (g < 0.9) ? vec3(1.0, 0.0, 0.6) // invalid sample range
                          : vec3(0.0, 1.0, 0.0);            // computed
                } else if (band < 16.0) {
                    bandc = texture2D(content_background, vec2(0.25, 0.5)).rgb;
                } else if (band < 24.0) {
                    // Four quarters across the client: producer status
                    // palette, chosen alpha, max probe alpha and the
                    // computed center alpha -- one reference fetch total.
                    vec4 referenceData = texture2D(content_background, vec2(0.75, 0.5));
                    if (p.x - x0 < cs.x * 0.25) {
                        // Left quarter: the background producer's per-field
                        // status G -- unwritten (no producer output at all),
                        // reject (output UV / degenerate opening) or completed
                        // medoid calculation; alpha alone does not explain the
                        // raw input.
                        bandc = (referenceData.g < 0.1) ? vec3(0.4)
                              : (referenceData.g < 0.3) ? vec3(0.0, 0.3, 1.0)
                              : (referenceData.g < 0.5) ? vec3(0.6, 0.0, 1.0)
                              : vec3(0.0, 1.0, 0.0);
                    } else if (p.x - x0 < cs.x * 0.5) {
                        bandc = vec3(referenceData.r);
                    } else if (p.x - x0 < cs.x * 0.75) {
                        bandc = vec3(referenceData.b);
                    } else {
                        bandc = vec3(clamp(2.0 * (referenceData.g - 1.0), 0.0, 1.0));
                    }
                } else {
                    bandc = vec3(maskData.b);
                }
                return vec4(bandc, src.a);
            }
        }
        return vec4(mix(base, diag, clamp(bestW, 0.0, 1.0)), src.a);
    }

    if (bestW <= 0.002) {
        return straight; // dry in every bucket, no additional fetch
    }

    // --- local optics: fisheye pull plus a bounded cross blur -------------
    vec2 pSample = p;
    float q = 0.0;
    if (bestKind < 0.5) {
        vec2 rv = p - bestC;
        float d = max(length(rv), 0.0001);
        vec2 dir = rv / d;
        q = clamp(length(rv / max(bestRad, vec2(0.02))), 0.0, 1.0);
        float prof = sin(RL_PI * q); // zero at centre and rim, peak mid-drop
        // Pull the sample toward the head centre. Capped below 1/pi of the
        // smaller radius so sin(pi*q)'s own slope can never cancel the pull --
        // no fold-over across the centre -- and inside bestAmp's 2..6 logical
        // px, so a small head magnifies without folding glyphs over itself.
        float amp = min(bestAmp, min(bestRad.x, bestRad.y) * 0.28);
        pSample = p - dir * amp * prof * bestW;
    } else {
        // Tail: magnify across its axis, bounded to a couple of logical px.
        vec2 perp = vec2(-bestDir.y, bestDir.x);
        float rel = clamp(dot(p - bestC, perp) / max(bestHalf, 0.001), -1.5, 1.5);
        float amp = min(bestAmp, max(bestHalf, 0.5) * 0.6);
        pSample = p - perp * (rel * amp * 0.9 * bestW);
    }
    pSample = clamp(pSample, vec2(x0, y0), vec2(x1, y1));

    vec2 uvD = clamp(rl_to_uv(pSample, frameOrigin, scale, texPx), openLo, openHi);
    vec4 samplePm = texture2D(tex, uvD);
    vec3 srgb = rl_straight(samplePm).rgb;

    // Fixed 5-tap cross, wet pixels only, all inside the opening.
    vec2 off = vec2(bestBlur) * scale / texPx;
    vec4 blurPm = (samplePm
        + texture2D(tex, clamp(uvD + vec2(off.x, 0.0), openLo, openHi))
        + texture2D(tex, clamp(uvD - vec2(off.x, 0.0), openLo, openHi))
        + texture2D(tex, clamp(uvD + vec2(0.0, off.y), openLo, openHi))
        + texture2D(tex, clamp(uvD - vec2(0.0, off.y), openLo, openHi))) * 0.2;
    vec3 rgb = mix(srgb, rl_straight(blurPm).rgb, clamp(bestW * 0.9, 0.0, 1.0));

    if (bestKind < 0.5) {
        // Readable glass on flat dark content: a faint transparent body
        // brightness tinted by the cold key light, bounded and scaled by the
        // local wet coverage -- never an opaque badge.
        vec2 dir = (p - bestC) / max(length(p - bestC), 0.0001);
        float lit = max(dot(dir, RL_LIGHT_DIR), 0.0);
        float body = mix(0.06, 0.10, bestSpec) * (1.0 - q * q) * bestW;
        rgb += RL_COLD * body;

        // Darker convex rim just inside the silhouette, weighted toward the
        // side opposite the key light.
        float tr = (q - 0.86) / 0.14;
        float rimBand = exp(-tr * tr) * bestW;
        float opp = max(dot(dir, -RL_LIGHT_DIR), 0.0);
        rgb *= (1.0 - mix(0.35, 0.45, bestRim) * rimBand * (0.55 + 0.45 * opp));

        // Strong, narrow, curved cold rim highlight on the lit side.
        float tb = (q - 0.72) / 0.10;
        float spec = exp(-tb * tb) * pow(lit, 6.0) * bestW;
        rgb += RL_COLD * (mix(0.85, 1.0, bestSpec) * spec);
    } else {
        // Tail: faint darker edges and a cool sheen keep it watery.
        vec2 perp = vec2(-bestDir.y, bestDir.x);
        float across = abs(dot(p - bestC, perp)) / max(bestHalf, 0.001);
        float edge = smoothstep(0.35, 1.0, across) * (1.0 - smoothstep(1.0, 1.3, across));
        rgb *= (1.0 - 0.20 * edge * bestW);
        rgb += RL_COLD * (0.14 * bestW);
    }

    // Final stage: straight RGB with the untouched original alpha.
    return vec4(mix(straight.rgb, clamp(rgb, 0.0, 1.0), bestW), src.a);
}

void main() {
    vec2 uv = vec2(gl_FragCoord.x / weather_size.x, 1.0 - gl_FragCoord.y / weather_size.y);
    EffectContext e = EffectContext(uv, weather_size, vec4(0.0, 0.0, weather_size), vec4(0.0, 0.0, weather_size));
    vec4 result = shader_main(e);
    vec4 original = texture2D(tex, uv);
    // Avoid a divide/multiply round trip on the original dry passthrough.
    if (all(equal(result, rl_straight(original)))) {
        weather_output = original;
    } else {
        result.rgb *= result.a; // Source final lens returns straight RGB.
        weather_output = result;
    }
}
