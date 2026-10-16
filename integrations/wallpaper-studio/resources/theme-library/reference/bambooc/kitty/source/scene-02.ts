import { computed, read } from "shoji_wm";
import {
  FRAME_02_BAMBOO_MAX_BOTTOM,
  FRAME_02_BUTTON_SPAN,
  FRAME_02_INSET,
  FRAME_02_MINIMUM_ROOT,
} from "./frame-02-geometry";
import type { BambooSceneMotion, MotionInput } from "./types";
import {
  SCENE_02_CELL_SIZE,
  SCENE_02_PRESS_CONTACT,
} from "./scene-02-assets";
import {
  bambooAngleAt,
  bambooDropEnd,
  bambooOffsetAt,
  bambooRevealAt,
} from "./bamboo-02-motion";

/**
 * Rainlit 02 daughter scene — the native 120s bamboo-girl story.
 *
 * Authored over the SAME single owned native clock as the frame pack
 * (`MotionInput.seconds`), so this module never introduces a second clock and
 * resolves every channel exactly once per window. The pack period is 120s;
 * the original 20s ambient gust/lamp harmonics are preserved in the frame
 * module, and 120 = 6 x 20 keeps their seam continuous at their real speed.
 *
 * Story (exact causal intervals, seconds):
 *   0..100    quiet        bamboo rolled home, girl hidden, all impacts 0
 *   100..105  creep        bounded slips beneath stationary upper bindings
 *   105..landing drop      height-aware accelerated fall (0.4..0.9s)
 *   landing..+0.6 impact    rigid recoil and rocking, no floor penetration
 *   settled..110 walk      girl enters right after the bundle settles
 *   110..110.6 stop/reach  girl stops, then begins reaching
 *   110.6..111.2 press     full index-finger press, then a distinct release
 *   111.2..114.5 lift      bamboo rises ONLY after the visible contact
 *   114.5..115 ceiling     ceiling impact spike + settle
 *   115..116.2 look        girl looks up as the bamboo returns, relaxes
 *   116.2..119.4 departure girl departs mirrored (uniform whole-sprite flip)
 *   119.4..120 rest        quiet boundary: everything back at rest
 *
 * Geometry is derived from the asset module's measured metadata (cell size
 * and pressed-finger contact) and from the pack's shared logical constants
 * (`./frame-02-geometry`: ring inset, minimum root, authored bamboo bottom,
 * button span) rather than from unrelated normalized numbers, and every
 * derived value is a reactive `computed` over the root geometry, so a resized
 * root recomputes without any per-frame allocation.
 */

/** Native scene period in seconds: one owned clock, no second clock. */
export const SCENE_02_PERIOD_SECONDS = 120;

// Shared logical geometry: ONE constants source with the frame pack and the
// runtime (`./frame-02-geometry`), so the painted ring, the real floor, the
// button anchor and the fixed suspension anchors can never drift apart.
/** Frame ring inset per side: the pack's 52px painted timber beam. */
const FRAME_INSET = FRAME_02_INSET;
/** Root minus the two horizontal / vertical insets (52 + 52). */
const CLIENT_INSET_TOTAL = 2 * FRAME_INSET;
/**
 * The decorative client floor sits this far above the root bottom: 1px above
 * the bottom ring, i.e. the client opening's own last row.
 */
const FLOOR_BELOW = FRAME_INSET + 1;
/** Declared minimum root, matching the pack (`minimumRoot`). */
const [MIN_ROOT_WIDTH, MIN_ROOT_HEIGHT] = FRAME_02_MINIMUM_ROOT;
/** Exposed bamboo's authored maximum bottom at rest, logical px. */
const BAMBOO_MAX_BOTTOM = FRAME_02_BAMBOO_MAX_BOTTOM;
/** Girl display scale ceiling at the largest supported root. */
const GIRL_MAX_SCALE = 0.2;
/** Button anchor near the root left edge, logical px at scene scale 1. */
const BUTTON_ROOT_X = FRAME_INSET;
/** Button span the scene scale opens between the smallest and full girl. */
const BUTTON_SPAN = FRAME_02_BUTTON_SPAN;
/**
 * Distance advanced by ONE walk pose at scene scale 1, logical px. A full
 * 8-frame gait therefore spans 8 * 8 = 64 logical px, matching the roughly
 * 100px-tall girl's real stride, so the cadence is bound to actual travel
 * (distance per pose) with no foot sliding and no rate cap or extra clock.
 */
const WALK_STRIDE = 8;

/** Interval boundaries in scene seconds (see the header story). */
const FLOOR_SETTLE_SECONDS = 0.6;
const WALK_END = 110;
const STOP_SETTLE_END = 110.35;
const STOP_END = 110.6;
/** Button edge times inside the 110.6..111.2 press/release window. */
const PRESS_SET = 110.72;
const RELEASE_AT = 111.0;
const PRESS_END = 111.2;
const HOLD_IDLE = 111.9;
const LIFT_END = 114.5;
const CEILING_END = 115;
const LOOK_START = 115.0;
const LOOK_RELAX = 115.7;
const LOOK_END = 116.2;
const DEPART_WALK_START = 116.9;
const DEPART_END = 119.4;
/** Girl fade-in / fade-out durations. */
const FADE_IN = 0.5;
const FADE_OUT_START = 118.9;

function smoothstep(value: number): number {
  const x = value < 0 ? 0 : value > 1 ? 1 : value;
  return x * x * (3 - 2 * x);
}


/** Button contact 0..1: fast press edge, hold, then a distinct release edge. */
function buttonPressAt(t: number): number {
  if (t < STOP_END || t >= PRESS_END) return 0;
  if (t < PRESS_SET) return smoothstep((t - STOP_END) / (PRESS_SET - STOP_END));
  if (t < RELEASE_AT) return 1;
  return 1 - smoothstep((t - RELEASE_AT) / (PRESS_END - RELEASE_AT));
}

/** Local impact spike after a contact, decaying over its settle window. */
function impactAt(t: number, from: number, to: number): number {
  if (t < from || t >= to) return 0;
  const u = (t - from) / (to - from);
  return (1 - u) * (1 - u);
}

/** Girl horizontal position (LOGICAL ROOT cell left) over the story. */
function girlXAt(t: number, parkX: number, pressX: number, settled: number): number {
  if (t <= settled) return parkX;
  if (t < WALK_END) {
    const u = (t - settled) / (WALK_END - settled);
    return parkX + (pressX - parkX) * u;
  }
  if (t < DEPART_WALK_START) return pressX;
  if (t < DEPART_END) {
    const u = (t - DEPART_WALK_START) / (DEPART_END - DEPART_WALK_START);
    return pressX + (parkX - pressX) * u;
  }
  return parkX;
}

/** Walk pose index advanced by actually walked distance, then wrapped 0..7. */
function walkFrame(distance: number, stride: number): number {
  const index = Math.floor(distance / stride) % 8;
  return index < 0 ? index + 8 : index;
}

/** Girl atlas cell index over the story (walking 0..7, action 8..15). */
function girlFrameAt(
  t: number,
  stride: number,
  parkX: number,
  pressX: number,
  settled: number,
): number {
  if (t <= settled) return 0;
  if (t < WALK_END) {
    return walkFrame(parkX - girlXAt(t, parkX, pressX, settled), stride);
  }
  if (t < STOP_SETTLE_END) return 9; // stopped after walking, feet planted
  if (t < STOP_END) return 10; // begin reaching
  if (t < RELEASE_AT) return 11; // full index-finger press
  if (t < HOLD_IDLE) return 12; // release, hand withdrawing
  if (t < LOOK_START) return 8; // calm standing idle, waiting
  if (t < LOOK_RELAX) return 13; // head and eyes look up
  if (t < LOOK_END) return 14; // head returns level, relaxed
  if (t < DEPART_WALK_START) return 15; // departure step, still facing LEFT
  if (t < DEPART_END) {
    return walkFrame(girlXAt(t, parkX, pressX, settled) - pressX, stride);
  }
  return 0;
}

/** Girl opacity: fade in on approach, fade out on departure, 0 at rest. */
function girlOpacityAt(t: number, settled: number): number {
  if (t <= settled) return 0;
  if (t < settled + FADE_IN) {
    return smoothstep((t - settled) / FADE_IN);
  }
  if (t < FADE_OUT_START) return 1;
  if (t < DEPART_END) {
    return 1 - smoothstep((t - FADE_OUT_START) / (DEPART_END - FADE_OUT_START));
  }
  return 0;
}

/**
 * Build the 120s daughter-scene channels for one window. All geometry inputs
 * are read reactively from the shared root signals; every returned channel is
 * a derived signal, computed lazily and allocated once per window.
 */
export function createBambooSceneMotion(input: MotionInput): BambooSceneMotion {
  const [cellWidth, cellHeight] = SCENE_02_CELL_SIZE;
  const [pressContactX] = SCENE_02_PRESS_CONTACT;

  const rootWidth = computed(() => read(input.rootWidth));
  const rootHeight = computed(() => read(input.rootHeight));
  const seconds = computed(() => read(input.seconds));
  const reduced = computed(() =>
    input.bambooSceneEnabled === false || read(input.reduced) === true,
  );

  // Client opening, real floor and the ornament scale the frame shares.
  const clientWidth = computed(() => Math.max(0, rootWidth() - CLIENT_INSET_TOTAL));
  const clientHeight = computed(() =>
    Math.max(0, rootHeight() - CLIENT_INSET_TOTAL),
  );
  const floorRoot = computed(() => rootHeight() - FLOOR_BELOW);
  const ornamentScale = computed(() =>
    Math.min(1, rootWidth() / MIN_ROOT_WIDTH, rootHeight() / MIN_ROOT_HEIGHT),
  );
  // Distance the exposed assembly travels from home to the real floor.
  const fall = computed(() =>
    Math.max(0, floorRoot() - BAMBOO_MAX_BOTTOM * ornamentScale()),
  );
  const landing = computed(() => bambooDropEnd(fall(), ornamentScale()));
  const settled = computed(() => landing() + FLOOR_SETTLE_SECONDS);

  // Uniform girl scale: capped, then shrunk to always fit the client opening
  // with a 1px margin per side so hands and hair are never silently cropped.
  const girlScale = computed(() =>
    Math.min(
      GIRL_MAX_SCALE,
      Math.max(0, clientHeight() - 2) / cellHeight,
      Math.max(0, clientWidth() - 2) / cellWidth,
    ),
  );
  const sceneScale = computed(() => girlScale() / GIRL_MAX_SCALE);

  const buttonRootX = computed(() => BUTTON_ROOT_X + BUTTON_SPAN * sceneScale());
  // Press cell left so the measured finger contact lands on the button.
  const pressX = computed(() => buttonRootX() - pressContactX * girlScale());
  // Offscreen-right parking point: both the entrance and the exit reach it.
  const parkX = computed(() =>
    FRAME_INSET + clientWidth() + cellWidth * girlScale(),
  );
  // Distance per walk pose, scaled with the girl so the gait stays bound to
  // real travel at every root size.
  const stride = computed(() => Math.max(1, WALK_STRIDE * sceneScale()));

  // Independent scalar tracks on the existing clock; bindings do not move.
  const bambooOffsetY = computed(() => reduced() ? 0 :
    bambooOffsetAt(seconds(), fall(), ornamentScale(), landing()));

  return {
    girlX: computed(() =>
      reduced() ? parkX() : girlXAt(seconds(), parkX(), pressX(), settled()),
    ),
    girlFrame: computed(() =>
      reduced() ? 0 : girlFrameAt(seconds(), stride(), parkX(), pressX(), settled()),
    ),
    // 1 = authored sheet facing LEFT (approach, unmirrored press, depart step);
    // -1 = uniform whole-sprite mirror for the rightward departure walk.
    girlFacing: computed(() =>
      reduced() || seconds() < DEPART_WALK_START ? 1 : -1,
    ),
    girlOpacity: computed(() => (reduced() ? 0 : girlOpacityAt(seconds(), settled()))),
    bambooOffsetY,
    bambooReveal: computed(() => reduced() ? 0 : bambooRevealAt(seconds(), landing())),
    bambooAngle: computed(() => reduced() ? 0 : bambooAngleAt(seconds(), landing())),
    buttonPress: computed(() => (reduced() ? 0 : buttonPressAt(seconds()))),
    floorImpact: computed(() =>
      reduced() ? 0 : impactAt(seconds(), landing(), settled()),
    ),
    ceilingImpact: computed(() =>
      reduced() ? 0 : impactAt(seconds(), LIFT_END, CEILING_END),
    ),
  };
}
