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
// Persistent droplet motion state for the content-responsive rain.
//
// This is the middle stage of the Kitty weather pipeline. It runs as a
// `renderTo()` side pipeline into the `rain-drop-motion-v5` rgba16f state at
// scale 1/8, input `stateSource(motion)` and leaves the outer raw capture
// untouched. Every output texel is one lane of a packed, persistent GPU
// simulation: the shader reads the whole bucket's previous state through `tex`
// and writes exactly one texel of the next state, so the update is a pure
// function of the previous state and the phase uniform. Re-running the same
// phase is a no-op (dt = 0) and never ages a head twice.
//
// Layout (RGB carries data, A is always 1 because preserve-materialize
// premultiplies RGB and alpha must never hold metadata):
//   texel 0           header0: previous phase hi, previous phase lo, last slot
//   texel 1           header1: episode slot, active, magic (5)
//   texel 2 + 4i + 0  head i record0: cursor hi, cursor lo, speed multiplier
//   texel 2 + 4i + 1  head i record1: continuous clear seconds [0, .85], the
//                                     multiplier latched at the .25 s crossing,
//                                     status
//   texel 2 + 4i + 2  head i record2: real age seconds, accumulated actual-
//                                     contact seconds, break age (0 = alive)
//   texel 2 + 4i + 3  head i record3: frozen break cursor hi, frozen break
//                                     cursor lo, frozen break multiplier
// Head 0 is the single parent; heads 1..4 are its parts. Status: 0 waiting
// (child has not split yet), 1 alive but clear, 1.25 alive
// and in actual contact (the swept footprint scan found occupancy), 2 dead or
// absent. A header magic other than 5 means the stored state is stale (an older
// texture, a restart), so the bucket is re-initialized on its next spawn.
// Bucket capacity is min(24, floor(total_texels / 22)); partial buckets and
// texels past capacity stay zero. One 96 s loop holds 24 four-second slots;
// each eligible slot starts one episode in bucket slot mod capacity, and a
// living bucket is never reset by a later birth (a busy bucket simply skips its
// birth). The paired diagonal family is gone: family 0 slots never spawn, and
// two further eligible slots are skipped so the remaining families birth 14
// times per loop instead of 16 -- 7 centre, 5 right and 2 left (family 2's
// slot mod 12 == 8 slots). A dead bucket stores an out-of-range episode
// slot so a later birth in slot 0 can never be mistaken for "already this
// slot".
//
// Cursor is nominal episode progress in [0, 1]: a head advances
// dt / 8 * multiplier * 1.5, so a full free sweep takes 8 / 1.5 s at nominal
// speed. Parent cursors
// start at 0 with the plan's t0 delay, so the drop becomes visible only once
// the cursor passes t0. Children stay waiting at cursor 0 until their parent
// actually breaks; they are then born at the parent's frozen break position
// and velocity (record3), consume only the remaining portion of that frame's
// dt, and carry no second copy of the parent's speed because the frozen
// multiplier is baked into their displacement exactly once. A step change in
// phase (tab hidden, clock restarted) rebases the timestamp with dt = 0 and
// keeps living drops; a normal wrap past 0.96 -> 0 is the small positive dt of
// one slot.
//
// Each head checks its complete elliptical body and the body swept along the
// next frame's bounded free trajectory. A 2 logical px raster over the cached,
// dilated content mask replaces the sparse point probes: thin strokes cannot
// fall between widely spaced probes. A hit stops the scan immediately.
//
// The release controller is continuous. Contact decays the multiplier
// exponentially toward 0.125 (tau 0.025 s) and renews the clear timer. While
// clear the timer advances and the multiplier keeps crawling until 0.25
// continuous clear seconds have passed, bridging glyph gaps; then it ramps
// from the multiplier latched at that crossing to 1.15 over 0.15 s and returns
// to 1.0 over 0.45 s, both with smoothstep curves, so no transition jumps.
// Contact during the release restarts the crawl. The timer saturates at 0.85 s
// and a newborn head starts saturated at 1.0, which is why the release curve
// ends exactly at nominal speed. Cursor integration uses the trapezoid of the
// old and new multipliers.
//
// Lifetime is independent of the multiplier and of the nominal cursor. Real
// age accrues over the visible portion of each frame, including the fractional
// frame that crosses t0, and keeps accruing once the cursor clamps at the path
// endpoint, which is a safety stop only, never a death or split condition.
// Actual-contact dose accrues on swept contact alone, never during the clear
// crawl. Each head carries deterministic thresholds: age = travel duration *
// 16 * mix(.70, .90, hash(slot, 811 + head * 13)), dose = mix(4, 8, hash(slot,
// 817 + head * 13)) for a parent and mix(.90, 1.80, same hash) for a child.
// When either is reached the earliest within-frame fraction is solved, the
// controller is integrated to exactly that instant, and the resulting cursor
// and multiplier are frozen into record3 along with the break age. A parent
// may only break at or below 75% of the opening height (the within-frame
// lower-zone crossing gates the temporal event) and always breaks at a hashed
// 80..90% impact height strictly before the 96% endpoint, so no parent ever
// fades out standing at the endpoint; children break on their own clocks with
// no lower gate. The head then fades over 0.18 s (parent) or 0.30 s (child) of
// real age before its status becomes 2, so a frozen break record stays
// readable for the whole fade and children of a dead parent are never dropped.
//
// Header lanes do no collision work; head lanes update only themselves and,
// for waiting children, their parent.
//
// The parent plan / path / family hashes are copied verbatim from
// rain-lens.frag (`rm_` vs `rl_` prefix) and MUST stay in lockstep: the lens
// reconstructs the same plans and maps the five stored heads back to the same
// roles. The content mask arrives as a state source, so it keeps its native
// ~capture * 0.5 dimensions and is sampled with raw normalized UV.

uniform float phase;       // caller-owned loop in [0, 1] over 96 s
uniform float composition; // 0 rainlit-shoji, 1 lantern-dusk, 2 moonlit-ink
uniform vec2 logical_size; // frame logical size
uniform float border_px;   // reserved native border width (26)
uniform sampler2D content_mask;

const float RM_SLOTS = 24.0;      // four-second episode starts per 96 s loop
const float RM_LOOP_SEC = 96.0;   // seconds per full phase sweep
const float RM_BUCKET = 22.0;     // texels per episode bucket: 2 + 5 * 4
const float RM_MAX_CAPACITY = 24.0;
const float RM_MAGIC = 5.0;         // state layout version in header1.z
const float RM_SPEED = 1.5;         // global motion speed scale for every clock
const float RM_ENTRY_TAU = 0.025;   // ~80 ms settle on actual contact
const float RM_CONTACT_MUL = 0.125; // crawled speed while in contact
const float RM_RELEASE_MUL = 1.15;  // peak speed just after the release
const float RM_CRAWL_SEC = 0.25;    // clear seconds before the release
const float RM_RAMP_SEC = 0.15;     // smoothstep ramp up to the peak
const float RM_RECOVER_SEC = 0.45;  // smoothstep return to nominal
const float RM_CLEAR_MAX = 0.85;    // crawl + ramp + recover, timer ceiling
const float RM_FADE_PARENT = 0.18;  // real fade seconds after a parent break
const float RM_FADE_CHILD = 0.30;   // real fade seconds after a child break

const float RM_DEG = 0.0174532925;

// --- shared geometry helpers (verbatim twins of rain-lens.frag) -------------

float rm_hash(vec2 c) {
    return fract(sin(dot(c, vec2(127.1, 311.7))) * 43758.5453);
}

float rm_h(float a, float b) {
    return rm_hash(vec2(a, b));
}

// Family 2's two sparse left slots: slot mod 12 == 8 (slots 8 and 20). They
// mirror the right-side family's start x and horizontal lean to the left while
// its other births stay on the right, so a 96 s loop has 7 centre, 5 right and
// 2 left births. Deterministic; a pure function of the slot.
bool rm_is_left(float slot) {
    return abs(mod(slot, 12.0) - 8.0) < 0.5;
}

float rm_prog(float s, float r) {
    if (r > 2.0) {
        float a = 0.62;
        float b = 0.56;
        if (s < a) {
            float x = s / a;
            return b * (2.0 * x - x * x);
        }
        float z = (s - a) / (1.0 - a);
        return b + (1.0 - b) * z * z;
    }
    return (2.0 * s + (r - 1.0) * s * s) / (1.0 + r);
}

float rm_progd(float s, float r) {
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

vec2 rm_rot(vec2 v, float a) {
    float c = cos(a);
    float s = sin(a);
    return vec2(c * v.x - s * v.y, s * v.x + c * v.y);
}

vec2 rm_ppos(vec2 p0, vec2 d, float tt, float t0, float t, float r) {
    return p0 + d * rm_prog(clamp((tt - t0) / max(t, 0.0001), 0.0, 1.0), r);
}

vec2 rm_pvel(vec2 d, float tt, float t0, float t, float r) {
    return d * (rm_progd(clamp((tt - t0) / max(t, 0.0001), 0.0, 1.0), r) / max(t, 0.0001));
}

vec4 rm_parent_plan(float slot, float fam) {
    float h1 = rm_h(slot, 301.0);
    float h2 = rm_h(slot, 307.0);
    float h4 = rm_h(slot, 319.0);
    float h5 = rm_h(slot, 325.0);
    float t0 = mix(0.02, 0.07, h1);
    float dead = mix(0.62, 0.76, h2);
    float r = 0.95;
    float rbase = mix(11.0, 19.0, h4);
    if (fam < 1.5) {
        r = mix(0.92, 1.10, h5);
    } else {
        r = mix(2.6, 3.8, h5);
        rbase = mix(18.0, 28.0, h4);
    }
    return vec4(t0, dead, r, rbase);
}

float rm_parent_start_x(float slot, float fam) {
    float h1 = rm_h(slot, 331.0);
    if (fam < 1.5) {
        return mix(0.42, 0.58, h1);
    }
    // Family 2's sparse left slots mirror the right-side start x = 1 - x, so
    // the right .76.. .90 span becomes the left .10.. .24 span.
    if (rm_is_left(slot)) {
        return 1.0 - mix(0.76, 0.90, h1);
    }
    return mix(0.76, 0.90, h1);
}

float rm_parent_start_y(float slot, float fam) {
    float h2 = rm_h(slot, 337.0);
    if (fam < 1.5) {
        return mix(0.0, 0.04, h2);
    }
    return mix(0.05, 0.12, h2);
}

// Where the episode starts: family 1 uses the top centre, family 2 the side --
// the right span for its five right slots and the mirrored left span for its
// two sparse left slots (rm_is_left).
vec2 rm_parent_p0(float slot, float fam, vec2 o, vec2 cs) {
    return o + vec2(cs.x * rm_parent_start_x(slot, fam),
                    cs.y * rm_parent_start_y(slot, fam));
}

// Total travel of a parent over its whole life. Every parent path now ends
// centered at 96% of the opening height: the start height comes from the same
// hash337 family mix as rm_parent_p0, so disp.y is exactly .96 * cs.y -
// startY. The
// family direction (the hash357 angle) is retained as the dx/dy ratio; dx is
// then clamped so the final centre x stays inside 2..98% of the opening and the
// drop never leaves through the side.
vec2 rm_parent_disp(float slot, float fam, vec2 cs) {
    float h2 = rm_h(slot, 357.0);
    float sy = rm_parent_start_y(slot, fam);
    float dy = cs.y * (0.96 - sy);
    float dx;
    if (fam < 1.5) {
        float th = mix(-5.0, 5.0, h2) * RM_DEG;
        dx = clamp(dy * (sin(th) / cos(th)), -cs.x * 0.12, cs.x * 0.12);
    } else {
        float th = mix(-4.0, 4.0, h2) * RM_DEG;
        dx = clamp(dy * (sin(th) / cos(th)), -cs.x * 0.12, cs.x * 0.12);
        // Mirror the side-family lean on the sparse left slots before the
        // opening clamp, so the left path is the right path reflected in x.
        if (rm_is_left(slot)) {
            dx = -dx;
        }
    }
    float sx = rm_parent_start_x(slot, fam);
    dx = clamp(dx, cs.x * (0.02 - sx), cs.x * (0.98 - sx));
    return vec2(dx, dy);
}

// --- packed state addressing -------------------------------------------------

vec2 rm_texel_uv(float index, vec2 size) {
    float ix = mod(index, size.x);
    float iy = floor(index / size.x);
    return (vec2(ix, iy) + 0.5) / size;
}

vec4 rm_read(sampler2D st, float index, vec2 size) {
    return texture2D(st, rm_texel_uv(index, size));
}

void rm_split_hi_lo(float value, out float hi, out float lo) {
    hi = floor(value * 1024.0) / 1024.0;
    lo = value - hi;
}

// --- content controller -----------------------------------------------------

// Occupancy at one clamped point of the protected opening, in mask UV. The
// mask state keeps native ~capture * 0.5 dimensions, so its normalized UV
// spans the whole window exactly like the logical frame.
float rm_probe(vec2 pLogical, vec2 logical, float border) {
    vec2 lo = vec2(border, border);
    vec2 hi = vec2(logical.x - border, logical.y - border);
    if (hi.x - lo.x <= 1.0 || hi.y - lo.y <= 1.0) {
        return 0.0;
    }
    vec2 uv = clamp(pLogical, lo, hi) / logical;
    return texture2D(content_mask, uv).r;
}

// Rasterize the swept ellipse in logical coordinates. Both motion curves are
// straight spatial paths, so closest-point projection covers the whole segment.
// The mask already dilates content by 2 logical px. A 2 px grid plus a 1 px
// contour allowance conservatively covers its cells, including thin strokes.
// Constant ES100 loop guards cover a 16k logical canvas; the clipped local
// bounds normally terminate each axis after only a few dozen iterations.
float rm_engage(vec2 pos, vec2 nextPos, vec2 radius,
                vec2 logical, float border) {
    vec2 openingLo = vec2(border);
    vec2 openingHi = logical - vec2(border);
    if (openingHi.x - openingLo.x <= 1.0
        || openingHi.y - openingLo.y <= 1.0) {
        return 0.0;
    }
    vec2 rad = max(radius, vec2(1.0)) + vec2(1.0);
    vec2 lo = max(min(pos, nextPos) - rad, openingLo);
    vec2 hi = min(max(pos, nextPos) + rad, openingHi);
    if (lo.x > hi.x || lo.y > hi.y) {
        return 0.0;
    }
    // Cheap common case: text or a filled card beneath the head centre.
    if (rm_probe(pos, logical, border) > 0.02) {
        return 1.0;
    }
    vec2 origin = floor(lo * 0.5) * 2.0 + vec2(1.0);
    vec2 segment = (nextPos - pos) / rad;
    float segment2 = dot(segment, segment);
    for (int y = 0; y < 8192; y++) {
        float py = origin.y + float(y) * 2.0;
        if (py > hi.y) {
            break;
        }
        for (int x = 0; x < 8192; x++) {
            float px = origin.x + float(x) * 2.0;
            if (px > hi.x) {
                break;
            }
            vec2 point = vec2(px, py);
            vec2 q = (point - pos) / rad;
            float t = segment2 > 0.000001
                ? clamp(dot(q, segment) / segment2, 0.0, 1.0) : 0.0;
            vec2 nearest = q - segment * t;
            if (dot(nearest, nearest) <= 1.0
                && rm_probe(point, logical, border) > 0.02) {
                return 1.0;
            }
        }
    }
    return 0.0;
}

// Same speed-shaped silhouette as rain-lens.frag, not a circular approximation.
// `birthProgress` is the head's visible real age normalized by its nominal
// traversal time (lifeT * 8), matching the lens birth ramp exactly; it is not
// the path cursor, which the controller slows.
vec2 rm_head_radius(float slot, float head, float birthProgress, float rbase,
                    float speed) {
    float fe = clamp((speed - 14.0) / 105.0, 0.0, 1.0);
    float grow = mix(0.42, 1.0, smoothstep(0.0, 0.22, birthProgress));
    vec2 shape = vec2(mix(0.88, 1.16, rm_h(slot, 727.0 + head * 3.0)),
                      mix(0.88, 1.16, rm_h(slot, 739.0 + head * 3.0)));
    return rbase * grow * shape
        * vec2(mix(1.22, 0.78, fe), mix(0.82, 1.30, fe));
}

// --- continuous release controller and independent lifetime -----------------

// Release curve as a closed form of the clear timer, so the multiplier never
// jumps at a crawl/ramp/recover handoff. The timer saturates at .85 s, which is
// where the curve reaches nominal speed exactly.
float rm_release_mul(float clear, float startMul) {
    if (clear < RM_CRAWL_SEC + RM_RAMP_SEC) {
        float x = (clear - RM_CRAWL_SEC) / RM_RAMP_SEC;
        return mix(startMul, RM_RELEASE_MUL, smoothstep(0.0, 1.0, x));
    }
    float y = clamp((clear - RM_CRAWL_SEC - RM_RAMP_SEC) / RM_RECOVER_SEC,
                    0.0, 1.0);
    return mix(RM_RELEASE_MUL, 1.0, smoothstep(0.0, 1.0, y));
}

// Advance the release controller over dt seconds. `clear` counts continuous
// seconds since the last actual contact and saturates at .85: the multiplier
// crawls toward .125 for the first .25 s of it, ramps to 1.15 over .15 s and
// returns to 1.0 over .45 s. The release-start multiplier is latched from the
// crawl segment of the frame that crosses .25, never from the stale frame
// entry, so the ramp begins exactly where the crawl ended.
void rm_ctrl(float oldMul, float oldClear, float oldStart, float dt,
             bool contacted, out float newMul, out float newClear,
             out float newStart) {
    newMul = oldMul;
    newClear = oldClear;
    newStart = oldStart;
    if (dt <= 0.0) {
        return;
    }
    if (contacted) {
        newClear = 0.0; // renewed contact restarts the crawl
        newMul = oldMul
            + (RM_CONTACT_MUL - oldMul) * (1.0 - exp(-dt / RM_ENTRY_TAU));
        return;
    }
    float remain = dt;
    if (newClear < RM_CRAWL_SEC) {
        float seg = min(remain, RM_CRAWL_SEC - newClear);
        newMul = newMul
            + (RM_CONTACT_MUL - newMul) * (1.0 - exp(-seg / RM_ENTRY_TAU));
        newClear += seg;
        remain -= seg;
        newStart = newMul;
    }
    if (remain > 0.0) {
        newClear = min(newClear + remain, RM_CLEAR_MAX);
        newMul = rm_release_mul(newClear, newStart);
    }
}

// Deterministic per-head thresholds. Age is independent of the multiplier and
// of the cursor: it is a doubled age budget measured against the original
// 8 * lifeT reference, i.e. 2 * mix(.70, .90) * 8 * lifeT; the actual motion
// clock runs at its own independent 1.5x rate and does not enter this budget.
// The dose is real seconds of actual swept contact, doubled and far shorter
// for a small child.
float rm_age_thr(float lifeT, float slot, float head) {
    return max(lifeT, 0.0001) * 16.0
        * mix(0.70, 0.90, rm_h(slot, 811.0 + head * 13.0));
}

float rm_dose_thr(float slot, float head, bool parent) {
    float k = rm_h(slot, 817.0 + head * 13.0);
    return parent ? mix(4.0, 8.0, k) : mix(0.90, 1.80, k);
}

// Height fraction (relative to the opening) of a parent's reconstructed
// position at within-frame fraction `f`. It integrates the same release
// controller and 1.5x cursor clock as the step itself, so any solved crossing
// and the actual frozen break use identical arithmetic. Parent paths descend,
// so the height is monotone in f. This is only paid inside the bisection loop;
// the frame endpoints are supplied by the caller.
float rm_pos_frac_y(float f, float oldCur, float oldMul, float oldClear,
                    float oldStart, float dt, float birthFrac,
                    float t0Off, float lifeT, float ratio,
                    vec2 p0, vec2 disp, vec2 o, vec2 cs, bool contacted) {
    float dtF = max(f - birthFrac, 0.0) * dt;
    float mF;
    float cF;
    float sF;
    rm_ctrl(oldMul, oldClear, oldStart, dtF, contacted, mF, cF, sF);
    float curF = oldCur + dtF * RM_SPEED / 8.0 * 0.5 * (oldMul + mF);
    curF = min(curF, t0Off + lifeT);
    float s = clamp((curF - t0Off) / lifeT, 0.0, 1.0);
    return ((p0 + disp * rm_prog(s, ratio)).y - o.y) / max(cs.y, 0.0001);
}

// Fixed 12-iteration bisection for the within-frame fraction at which a
// descending parent path first reaches `target` height. It is a bounded
// approximation, not an exact solve: the caller passes the already-integrated
// frame endpoints (yBegin at birthFrac, yEnd at fraction 1) so no redundant
// controller integration happens just to bracket, and the result is the upper
// bracket of the crossing (conservative: never reported early). Returns
// birthFrac when already reached, or 2.0 when the whole frame stays above the
// target.
float rm_cross_y(float target, float oldCur, float oldMul, float oldClear,
                 float oldStart, float dt, float birthFrac,
                 float t0Off, float lifeT, float ratio,
                 vec2 p0, vec2 disp, vec2 o, vec2 cs, bool contacted,
                 float yBegin, float yEnd) {
    if (yBegin >= target) {
        return birthFrac;
    }
    if (yEnd < target) {
        return 2.0;
    }
    float lo = birthFrac;
    float hi = 1.0;
    for (int i = 0; i < 12; i++) {
        float mid = 0.5 * (lo + hi);
        if (rm_pos_frac_y(mid, oldCur, oldMul, oldClear, oldStart, dt,
                          birthFrac, t0Off, lifeT, ratio, p0, disp, o, cs,
                          contacted) >= target) {
            hi = mid;
        } else {
            lo = mid;
        }
    }
    return hi;
}

// Parent head geometry: t0Off, travel duration, progress ratio, base radius.
// A pure function of the slot and family, so the spawn frame, every later frame
// and rain-lens.frag agree exactly.
vec4 rm_parent_geom(float slot, float efam, vec2 o, vec2 cs,
                    out vec2 p0, out vec2 disp) {
    vec4 pp = rm_parent_plan(slot, efam);
    p0 = rm_parent_p0(slot, efam, o, cs);
    disp = rm_parent_disp(slot, efam, cs);
    return vec4(pp.x, pp.y - pp.x, pp.z, pp.w);
}

// Child geometry, reconstructed from the parent's frozen break record: the
// child starts where the parent actually broke, at any cursor, and carries the
// parent's frozen velocity exactly once, baked into its own displacement. The
// old endpoint schedule cap is gone because the break no longer happens at a
// planned fraction of the path. The single parent (head 0) is the only source,
// so no second-parent variant exists.
vec4 rm_child_geom(float slot, float efam, float head, float breakCur,
                   float breakMul, vec2 o, vec2 cs, vec2 reach,
                   out vec2 p0, out vec2 disp) {
    vec4 pp = rm_parent_plan(slot, efam);
    vec2 pp0 = rm_parent_p0(slot, efam, o, cs);
    vec2 pdp = rm_parent_disp(slot, efam, cs);
    float pT = pp.y - pp.x;
    float k2 = rm_h(slot, 402.0 + head * 11.0);
    float k3 = rm_h(slot, 403.0 + head * 11.0);
    float k4 = rm_h(slot, 404.0 + head * 11.0);
    float k5 = rm_h(slot, 405.0 + head * 11.0);
    // A break at or past the parent's path endpoint carries no velocity: the
    // parent was already stopped there by the safety clamp.
    vec2 splitVel = (breakCur >= pp.x + pT)
        ? vec2(0.0) : rm_pvel(pdp, breakCur, pp.x, pT, pp.z) * breakMul;
    vec2 v0 = rm_rot(splitVel, mix(-0.40, 0.40, k5)) * mix(1.10, 1.60, k2);
    float cT = mix(0.16, 0.28, k2);
    float cRr = mix(0.16, 0.42, k3);
    disp = v0 * (cT * (1.0 + cRr) * 0.5);
    disp.x = clamp(disp.x, -reach.x, reach.x);
    disp.y = clamp(disp.y, -reach.y, reach.y);
    p0 = rm_ppos(pp0, pdp, breakCur, pp.x, pT, pp.z);
    return vec4(0.0, cT, cRr, clamp(pp.w * mix(0.25, 0.45, k4), 4.0, 10.0));
}

// One head over one frame. `st` is (cursor, multiplier, clear seconds, release
// start), `life` is (real age, actual-contact seconds, break age, unused),
// `geom` comes from rm_parent_geom or rm_child_geom and `oldBk` is the stored
// frozen break record (cursor, multiplier). `birthFrac` is the fraction of the
// frame before which the head did not exist, nonzero only for a child born at
// its parent's break, so a newborn ages, doses and integrates on the remainder
// of the frame alone. `isParent` selects the parent breakup rules (lower-zone
// gate plus hashed impact height) and the parent body vs the child stroke
// collision silhouette. A head that already broke keeps its record and its age
// keeps running until the fade ends.
void rm_step_head(vec4 st, vec4 life, vec4 geom, vec2 p0, vec2 disp,
                  vec2 oldBk, float dt, float birthFrac,
                  float slot, float head, float ageThr, float doseThr,
                  float fadeDur, vec2 logical, float border, bool isParent,
                  out vec4 nst, out vec4 nlife, out vec4 nbk,
                  out float newStatus) {
    nst = st;
    nlife = life;
    nbk = vec4(oldBk, 0.0, 0.0);
    float dtA = dt * clamp(1.0 - birthFrac, 0.0, 1.0);
    if (dt <= 0.0 || dtA <= 0.0) {
        newStatus = (life.z > 0.0 && life.x >= life.z + fadeDur) ? 2.0 : 1.0;
        return;
    }

    float oldCur = st.x;
    float oldMul = st.y;
    float t0Off = geom.x;
    float lifeT = max(geom.y, 0.0001);
    float ratio = geom.z;
    float rbase = geom.w;

    // 1.15 is the largest possible controller value. Scan the full free path
    // before integrating the contacted (slower) motion, preventing tunnelling.
    // An invisible head has no body to sweep: the scan starts at t0.
    float freeCur = min(oldCur + dtA * RM_SPEED / 8.0
                        * max(oldMul, RM_RELEASE_MUL), t0Off + lifeT);
    float sNew = clamp((freeCur - t0Off) / lifeT, 0.0, 1.0);
    float sScan = clamp((max(oldCur, t0Off) - t0Off) / lifeT, 0.0, 1.0);
    vec2 pos = p0 + disp * rm_prog(sScan, ratio);
    vec2 nextPos = p0 + disp * rm_prog(sNew, ratio);
    // The path endpoint is a safety stop: the position stops there, so the
    // actual velocity is zero even though the progress derivative is not.
    float speed = (oldCur >= t0Off + lifeT) ? 0.0
        : length(disp) * rm_progd(sScan, ratio) * oldMul * RM_SPEED
            / (lifeT * 8.0);
    float nextSpeed = (freeCur >= t0Off + lifeT) ? 0.0
        : length(disp) * rm_progd(sNew, ratio) * RM_RELEASE_MUL * RM_SPEED
            / (lifeT * 8.0);
    // Birth growth uses the head's visible real age normalized by its nominal
    // traversal time, the same clock the lens uses for the silhouette. This
    // frame's growth is the stored age; the prospective bound adds the frame's
    // real remainder dtA (after any birth fraction) as a conservative upper
    // limit on the age the head can reach within the frame. The bound is
    // collision-only: neither value is ever stored back as age or dose.
    float curGrowth = max(life.x, 0.0) / (lifeT * 8.0);
    float nextGrowth = (max(life.x, 0.0) + dtA) / (lifeT * 8.0);
    vec2 o = vec2(border);
    vec2 cs = vec2(max(logical.x - 2.0 * border, 1.0),
                   max(logical.y - 2.0 * border, 1.0));
    vec2 radius;
    if (isParent) {
        radius = max(
            rm_head_radius(slot, head, curGrowth, rbase, speed),
            rm_head_radius(slot, head, nextGrowth, rbase, nextSpeed));
    } else {
        // A child is a thin tapered stroke, never a round body. Its swept
        // collision silhouette is a circular enclosing radius
        // (sqrt(2) * the larger half extent) that contains the stroke at any
        // orientation, which the existing swept-ellipse scan conservatively
        // covers.
        float childLen = mix(8.0, 20.0, rm_h(slot, 833.0 + head * 13.0));
        float childWid = mix(1.0, 2.0, rm_h(slot, 839.0 + head * 13.0));
        radius = vec2(max(childLen, childWid) * 0.70710678);
    }
    float occ = (freeCur > t0Off)
        ? rm_engage(pos, nextPos, radius, logical, border) : 0.0;
    bool contacted = occ > 0.02;

    float mul1;
    float clear1;
    float start1;
    rm_ctrl(oldMul, st.z, st.w, dtA, contacted, mul1, clear1, start1);
    float cur1 = oldCur + dtA * RM_SPEED / 8.0 * 0.5 * (oldMul + mul1);
    if (cur1 >= t0Off + lifeT) {
        cur1 = t0Off + lifeT; // safety stop only, not a death condition
    }

    // gVis is the fraction of the frame at which the head becomes visible; 1.0
    // means it stayed invisible (before t0, or still waiting) for the whole
    // frame, so neither age nor dose may grow.
    float gVis = birthFrac;
    if (oldCur < t0Off) {
        if (cur1 <= t0Off) {
            gVis = 1.0;
        } else {
            gVis = birthFrac + (1.0 - birthFrac)
                * clamp((t0Off - oldCur) / (cur1 - oldCur), 0.0, 1.0);
        }
    }
    // Age and dose accrue over the visible part of the frame only, so a
    // parent's t0 delay contributes neither; the dose never grows during the
    // clear hold, only on actual swept contact.
    float visDt = dt * (1.0 - gVis);
    float age1 = life.x + visDt;
    float dose1 = life.y + (contacted ? visDt : 0.0);

    // Solve the earliest eligible within-frame fraction, then integrate the
    // controller to exactly that instant so the frozen cursor and multiplier
    // match the controller's own arithmetic. A threshold already reached by the
    // stored value (f16 rounding, or contact that ended exactly at it) fires at
    // the start of the visible part instead of never: without that the head
    // would live forever and pin its bucket. Parents additionally gate the
    // temporal event behind the exact lower-zone crossing and always fire a
    // hashed 80..90% impact breakup before the 96% endpoint.
    float frac = 2.0;
    if (life.z <= 0.0) {
        float temporalFrac = 2.0;
        if (age1 >= ageThr) {
            temporalFrac = min(temporalFrac,
                               gVis + max(ageThr - life.x, 0.0) / dt);
        }
        if ((contacted && dose1 >= doseThr) || life.y >= doseThr) {
            temporalFrac = min(temporalFrac,
                               gVis + max(doseThr - life.y, 0.0) / dt);
        }
        if (isParent) {
            // Parents may only break at or below 75% of the opening height:
            // the within-frame lower-zone crossing gates the temporal event,
            // and a hashed 80..90% impact height forces the breakup strictly
            // before the 96% endpoint even if the doubled clocks have not
            // elapsed, so no parent ever fades out standing at the endpoint.
            // A candidate already past a height fires immediately (the solve
            // returns birthFrac), which handles stored values and endpoints.
            // The frame's opening-relative height bounds are already known:
            // yBegin is the reconstructed start position and yEnd the
            // integrated cursor at the end of the frame, so no redundant
            // controller integration is paid to bracket the crossing.
            float impactY = mix(0.80, 0.90, rm_h(slot, 823.0 + head * 13.0));
            float yBegin = (pos.y - o.y) / max(cs.y, 0.0001);
            float sEnd = clamp((cur1 - t0Off) / lifeT, 0.0, 1.0);
            float yEnd = ((p0 + disp * rm_prog(sEnd, ratio)).y - o.y)
                / max(cs.y, 0.0001);
            float temporalEvent = 2.0;
            if (temporalFrac <= 1.0) {
                float lowFrac = rm_cross_y(
                    0.75, oldCur, oldMul, st.z, st.w, dt, birthFrac, t0Off,
                    lifeT, ratio, p0, disp, o, cs, contacted, yBegin, yEnd);
                temporalEvent = max(temporalFrac, lowFrac);
            }
            float impactFrac = rm_cross_y(
                impactY, oldCur, oldMul, st.z, st.w, dt, birthFrac, t0Off,
                lifeT, ratio, p0, disp, o, cs, contacted, yBegin, yEnd);
            frac = min(temporalEvent, impactFrac);
        } else {
            frac = temporalFrac;
        }
    }
    if (frac <= 1.0) {
        frac = clamp(frac, 0.0, 1.0);
        float dtTrig = max(frac - birthFrac, 0.0) * dt;
        float mulT;
        float clearT;
        float startT;
        rm_ctrl(oldMul, st.z, st.w, dtTrig, contacted, mulT, clearT, startT);
        float curT = oldCur + dtTrig * RM_SPEED / 8.0 * 0.5 * (oldMul + mulT);
        if (curT >= t0Off + lifeT) {
            curT = t0Off + lifeT;
        }
        nbk = vec4(curT, mulT, frac, 0.0);
        nlife.z = max(life.x + dt * (frac - gVis), 0.0);
    }

    nst = vec4(cur1, mul1, clear1, start1);
    nlife.x = age1;
    nlife.y = dose1;
    newStatus = (nlife.z > 0.0 && age1 >= nlife.z + fadeDur)
        ? 2.0 : (contacted ? 1.25 : 1.0);
}


vec4 shader_main(EffectContext effect) {
    vec2 size = max(effect.texture_size_px, vec2(1.0));
    float total = size.x * size.y;
    float capacity = min(RM_MAX_CAPACITY, floor(total / RM_BUCKET));

    vec2 texel = effect.texture_uv * size;
    float index = floor(texel.y) * size.x + floor(texel.x);
    float bucketF = floor(index / RM_BUCKET);
    float slotInBucket = index - bucketF * RM_BUCKET;

    if (bucketF >= capacity) {
        return vec4(0.0, 0.0, 0.0, 1.0);
    }

    float base = bucketF * RM_BUCKET;

    // --- read the whole bucket's previous state -----------------------------
    vec4 h0 = rm_read(tex, base + 0.0, size);
    vec4 h1 = rm_read(tex, base + 1.0, size);

    vec4 st[5];    // cursor, multiplier, continuous clear seconds, release start
    vec4 life[5];  // real age, actual-contact dose, break age, unused
    vec4 bk[5];    // frozen break cursor, frozen break multiplier, -, -
    float stat[5];
    for (int i = 0; i < 5; i++) {
        vec4 r0 = rm_read(tex, base + 2.0 + float(i) * 4.0, size);
        vec4 r1 = rm_read(tex, base + 3.0 + float(i) * 4.0, size);
        vec4 r2 = rm_read(tex, base + 4.0 + float(i) * 4.0, size);
        vec4 r3 = rm_read(tex, base + 5.0 + float(i) * 4.0, size);
        st[i] = vec4(r0.x + r0.y, r0.z, r1.x, r1.y);
        stat[i] = r1.z;
        life[i] = vec4(r2.x, r2.y, r2.z, 0.0);
        bk[i] = vec4(r3.x + r3.y, r3.z, 0.0, 0.0);
    }

    // --- timing: dt from the stored previous phase --------------------------
    float currentPhase = phase - floor(phase); // guard a stray 1.0
    float prevPhase = h0.x + h0.y;
    float lastSlot = h0.z;
    float newSlot = floor(currentPhase * RM_SLOTS);

    float delta = currentPhase - prevPhase;
    if (delta < -0.5) {
        delta += 1.0; // 0.96 -> 0.00 is a small positive advance
    }
    bool fresh = abs(h1.z - RM_MAGIC) > 0.5; // incompatible magic resets
    bool gap = fresh || delta < 0.0 || (delta * RM_LOOP_SEC > 0.5);
    float dt = gap ? 0.0 : delta * RM_LOOP_SEC;

    float epPrev = h1.x;
    bool sameSlot = !fresh && (abs(epPrev - newSlot) < 0.5);
    // An incompatible magic means the whole stored bucket is stale, so its
    // active flag cannot be trusted either: an older bucket must really reset
    // on its spawn frame instead of being relabeled with the current magic.
    bool busy = !fresh && (h1.y > 0.5) && !sameSlot;
    bool isTarget = abs(bucketF - mod(newSlot, capacity)) < 0.5;
    bool newSlotEvent = abs(newSlot - lastSlot) > 0.5;
    // Birth eligibility: the paired diagonal family (family 0) never spawns,
    // and two eligible ordinals are suppressed -- ordinals 3 and 10, i.e. slots
    // 5 and 16, one per remaining family. The ordinal counts births across the
    // loop, so only those slots change: the remaining families birth 14 times
    // per 96 s instead of 16 -- 7 centre, 5 right and 2 left. The gate rejects
    // the birth alone; the
    // phase/lastSlot headers still refresh and live buckets keep simulating
    // across a skipped slot.
    float newFam = mod(newSlot, 3.0);
    float birthOrdinal = floor(newSlot / 3.0) * 2.0 + (newFam - 1.0);
    bool eligible = (newFam > 0.5)
        && abs(birthOrdinal - 3.0) > 0.5 && abs(birthOrdinal - 10.0) > 0.5;
    bool spawn = isTarget && !sameSlot && !busy && (fresh || newSlotEvent)
        && eligible;

    float epSlot = spawn ? newSlot : epPrev;
    bool live = spawn || (!fresh && h1.y > 0.5);

    // Headers retain the previous head-status summary for one extra frame.
    // No live head can be discarded early; this avoids repeating five dense
    // collision scans just to calculate a bucket's active flag.
    float phaseHi;
    float phaseLo;
    rm_split_hi_lo(currentPhase, phaseHi, phaseLo);
    if (slotInBucket < 0.5) {
        return vec4(phaseHi, phaseLo, newSlot, 1.0);
    }
    if (slotInBucket < 1.5) {
        float anyLive = spawn ? 1.0 : 0.0;
        if (live && !spawn) {
            for (int i = 0; i < 5; i++) {
                if (stat[i] < 1.5) {
                    anyLive = 1.0;
                }
            }
        }
        return vec4(live ? epSlot : -1.0, anyLive, RM_MAGIC, 1.0);
    }
    if (!live) {
        return vec4(0.0, 0.0, 0.0, 1.0);
    }
    // Same-phase re-runs and timestamp rebases (dt = 0) must neither age a
    // head nor drop its recorded contact: echo the previous lane verbatim.
    // Headers above already refreshed, and a spawn below still re-initializes.
    if (!spawn && dt <= 0.0) {
        return rm_read(tex, base + slotInBucket, size);
    }
    float headF = floor((slotInBucket - 2.0) / 4.0);
    float rec = mod(slotInBucket - 2.0, 4.0);

    // --- logical opening and the per-head episode geometry ------------------
    vec2 logical = max(logical_size, vec2(1.0));
    float border = max(border_px, 0.0);
    vec2 o = vec2(border, border);
    vec2 cs = vec2(max(logical.x - 2.0 * border, 1.0),
                   max(logical.y - 2.0 * border, 1.0));
    vec2 reach = vec2(cs.x * 0.18, cs.y * 0.22);

    float slot = epSlot;
    float efam = mod(slot, 3.0);

    // This lane's own head, gathered without dynamic array indexing (which
    // ES100 does not guarantee for local arrays). The single parent is always
    // head 0, so a waiting child reads it directly.
    vec4 ownSt = vec4(0.0, 1.0, RM_CLEAR_MAX, 1.0);
    vec4 ownLife = vec4(0.0);
    vec4 ownBk = vec4(0.0);
    float ownStat = 2.0;
    for (int i = 0; i < 5; i++) {
        if (abs(float(i) - headF) < 0.5) {
            ownSt = st[i];
            ownLife = life[i];
            ownBk = bk[i];
            ownStat = stat[i];
        }
    }
    vec4 parSt = st[0];
    vec4 parLife = life[0];
    vec4 parBk = bk[0];
    float parStat = stat[0];

    // The lane echoes its previous record by default, which is also what keeps
    // a dead head's frozen break record readable; spawn and step overwrite it.
    vec4 nstL = ownSt;
    vec4 nlifeL = ownLife;
    vec4 nbkL = ownBk;
    float nstatL = ownStat;

    // --- step the living bucket --------------------------------------------
    if (spawn) {
        // Every head is re-initialized: nominal speed, saturated (fully
        // released) clear timer, no age, no break. Parents are alive from the
        // start, children wait for their parent's actual break, and the heads
        // past the family's part count start dead.
        nstL = vec4(0.0, 1.0, RM_CLEAR_MAX, 1.0);
        nlifeL = vec4(0.0);
        nbkL = vec4(0.0);
        if (headF < 0.5) {
            nstatL = 1.0;
        } else {
            float k1 = rm_h(slot, 401.0 + headF * 11.0);
            float fid = headF - 1.0;
            // The first two fragments are guaranteed (fid 0 and 1); the third
            // is the .72 candidate and the fourth the .56 one, within the
            // 5-head storage.
            float pprob = (fid < 1.5) ? 1.0
                : ((fid < 2.5) ? 0.72 : 0.56);
            // Family 1's centre splitter yields at most three parts, so its
            // last child slot (head 4) stays dead; family 2 may use all four.
            float famOk = (efam >= 0.5 && efam < 1.5 && headF >= 4.0) ? 0.0 : 1.0;
            nstatL = (famOk * step(k1, pprob) > 0.5) ? 0.0 : 2.0;
        }
    } else {
        // Each lane evaluates only its own head; a waiting child also evaluates
        // its parent from the same previous state, so a break landing in this
        // frame is never missed and the child geometry is rebuilt from the
        // parent's frozen record instead of a pre-planned split.
        vec4 geom;
        vec2 g0;
        vec2 gd;
        bool haveGeom = false;
        bool orphan = false;
        float birthFrac = 0.0;
        if (headF < 0.5) {
            geom = rm_parent_geom(slot, efam, o, cs, g0, gd);
            haveGeom = true;
        } else {
            float splitCur = parBk.x;
            float splitMul = parBk.y;
            if (parLife.z > 0.0) {
                // The parent already broke and its frozen record stays
                // readable, even once the parent itself has faded away, so a
                // split child is never dropped because of the parent's fade.
                haveGeom = true;
            } else if (ownStat < 0.5 && parStat > 0.5 && parStat < 1.5) {
                vec2 pg0;
                vec2 pgd;
                vec4 pg = rm_parent_geom(slot, efam, o, cs, pg0, pgd);
                vec4 pnst;
                vec4 pnlife;
                vec4 pnbk;
                float pnstat;
                rm_step_head(parSt, parLife, pg, pg0, pgd, parBk.xy, dt, 0.0,
                             slot, 0.0,
                             rm_age_thr(pg.y, slot, 0.0),
                             rm_dose_thr(slot, 0.0, true),
                             RM_FADE_PARENT, logical, border, true,
                             pnst, pnlife, pnbk, pnstat);
                if (pnlife.z > 0.0) {
                    splitCur = pnbk.x;
                    splitMul = pnbk.y;
                    birthFrac = pnbk.z;
                    haveGeom = true;
                }
            } else {
                // No break record and no parent left that could produce one.
                orphan = true;
            }
            if (haveGeom) {
                geom = rm_child_geom(slot, efam, headF, splitCur, splitMul,
                                     o, cs, reach, g0, gd);
            }
        }

        if (ownStat > 1.5) {
            // Dead: keep echoing, which preserves a frozen break record that a
            // child lane may still be reading.
        } else if (!haveGeom && ownStat > 0.5) {
            nstatL = 2.0; // an alive head without a usable path cannot continue
        } else if (haveGeom && ownStat > 0.5) {
            float nstat;
            rm_step_head(ownSt, ownLife, geom, g0, gd, ownBk.xy, dt, 0.0,
                         slot, headF, rm_age_thr(geom.y, slot, headF),
                         rm_dose_thr(slot, headF, headF < 0.5),
                         headF < 0.5 ? RM_FADE_PARENT : RM_FADE_CHILD,
                         logical, border, headF < 0.5, nstL, nlifeL, nbkL,
                         nstat);
            nstatL = nstat;
        } else if (orphan) {
            nstatL = 2.0; // nothing left to split from: release the bucket
        } else if (haveGeom) {
            // Waiting child born exactly at its parent's break: it ages, doses
            // and integrates on the remaining part of this frame only.
            float nstat;
            rm_step_head(vec4(0.0, 1.0, RM_CLEAR_MAX, 1.0), vec4(0.0),
                         geom, g0, gd, vec2(0.0), dt, birthFrac,
                         slot, headF, rm_age_thr(geom.y, slot, headF),
                         rm_dose_thr(slot, headF, false), RM_FADE_CHILD,
                         logical, border, false, nstL, nlifeL, nbkL, nstat);
            nstatL = nstat;
        }
        // Otherwise the parent is still falling and the child stays waiting.
    }

    // --- write this fragment's lane -----------------------------------------
    if (rec < 0.5) {
        float chi;
        float clo;
        rm_split_hi_lo(nstL.x, chi, clo);
        return vec4(chi, clo, nstL.y, 1.0);
    }
    if (rec < 1.5) {
        return vec4(nstL.z, nstL.w, nstatL, 1.0);
    }
    if (rec < 2.5) {
        return vec4(nlifeL.x, nlifeL.y, nlifeL.z, 1.0);
    }
    float bhi;
    float blo;
    rm_split_hi_lo(nbkL.x, bhi, blo);
    return vec4(bhi, blo, nbkL.y, 1.0);
}

void main() {
    vec2 uv = vec2(gl_FragCoord.x / weather_size.x, 1.0 - gl_FragCoord.y / weather_size.y);
    EffectContext e = EffectContext(uv, weather_size, vec4(0.0, 0.0, weather_size), vec4(0.0, 0.0, weather_size));
    vec4 result = shader_main(e);
    weather_output = result;
}
