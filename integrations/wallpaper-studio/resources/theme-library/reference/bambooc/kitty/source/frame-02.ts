import { computed, read } from "shoji_wm";
import { FRAME_02_ART } from "./frame-02-art";
import { FRAME_02_INSET, FRAME_02_MINIMUM_ROOT } from "./frame-02-geometry";
import type { FrameMotion, FramePack, MotionInput } from "./types";
import {
  SCENE_02_ATLAS,
  SCENE_02_ATLAS_SIZE,
  SCENE_02_BASELINE,
  SCENE_02_CELL_SIZE,
  SCENE_02_PRESS_CONTACT,
  SCENE_02_SOURCE_HEIGHT,
} from "./scene-02-assets";
import { SCENE_02_PERIOD_SECONDS, createBambooSceneMotion } from "./scene-02";

/**
 * Rainlit frame profile 02 — bamboo roll, paper crane, paper lamp.
 *
 * Design-faithful restore: the ring is one 52px painted timber beam on every
 * side (`FRAME_02_INSET`, shared through `./frame-02-geometry`), and the
 * measured atlas, corners, rails and the ten authored ornament layers come
 * from the generated `./frame-02-art` metadata. The interior paper-lamp
 * placement is the MEASURED real paper centre/spill lifted out of that same
 * module; nothing here re-derives artwork cells by hand.
 *
 * The frame owns one authored wind (`gust`) that also feeds the linked Kitty
 * interior, plus the bamboo-girl daughter scene: the bamboo roll and slat
 * creep down, drop and settle on the real floor before the girl's approach,
 * and rise again only after her visible button
 * press. The frame stays on ONE native clock (`MotionInput.seconds`, 120s
 * period); the original 20s ambient harmonics are preserved in a separate
 * constant, and 120 = 6 x 20 keeps their seam continuous at their real speed.
 * No second clock, no per-tick allocation, no asset loading or compilation at
 * module load.
 *
 * The two red bindings stay on the upper beam. Matching load-bearing braid
 * emerges behind their knots and follows the moving roll/slat bearings,
 * with bounded slack after contact. ONE irregular lamp pulse (`lampEnergy`)
 * drives the exterior paper emission, the interior reflection and the scene
 * stage's warm spill — a single source with a single consequence.
 */

const TAU = Math.PI * 2;

/**
 * The generated design-faithful artwork: one atlas, the measured corners,
 * rails and rail periods, and the ten authored layers (bamboo group, crane,
 * lamp). `lightPlacement` is the measured real paper-lamp centre/spill, which
 * belongs to the INTERIOR profile rather than the top-level frame geometry, so
 * it is separated from the spread and bound below.
 */
const { lightPlacement: FRAME_02_LIGHT_PLACEMENT, ...FRAME_02_ARTWORK } = FRAME_02_ART;

/**
 * The original authored ambient period, deliberately independent of the pack
 * period. Every ambient frequency stays a whole multiple of
 * TAU / AMBIENT_PERIOD_SECONDS so the lamp, crane and noise keep their exact
 * original speed and remain seam-continuous across the 120s pack loop.
 */
const AMBIENT_PERIOD_SECONDS = 20;

/**
 * Integer harmonics of the AMBIENT period: every frequency is a whole multiple
 * of TAU / AMBIENT_PERIOD_SECONDS, so each channel returns to exactly its
 * start value at every 20s boundary and the 120s loop stays seamless.
 * Arbitrary constant phase lags remain allowed.
 */
function gustAt(seconds: number, phase: number): number {
  return (
    0.58 * Math.sin(seconds * (TAU * 2 / AMBIENT_PERIOD_SECONDS) + phase) +
    0.27 * Math.sin(seconds * (TAU * 5 / AMBIENT_PERIOD_SECONDS) + 1.7 + phase * 0.6) +
    0.15 * Math.sin(seconds * (TAU * 9 / AMBIENT_PERIOD_SECONDS) + 0.6 + phase * 1.3)
  );
}

/** Slower, heavier breath used by the paper lamp (harmonics 1, 3, 7). */
function slowGustAt(seconds: number, phase: number): number {
  return (
    0.62 * Math.sin(seconds * (TAU * 1 / AMBIENT_PERIOD_SECONDS) + phase) +
    0.26 * Math.sin(seconds * (TAU * 3 / AMBIENT_PERIOD_SECONDS) + 2.2 + phase) +
    0.12 * Math.sin(seconds * (TAU * 7 / AMBIENT_PERIOD_SECONDS) + 0.4 + phase)
  );
}

/** Irregular, shallow warm-light variation in [-1, 1] (harmonics 3, 8). */
function lampNoiseAt(seconds: number): number {
  return (
    0.62 * Math.sin(seconds * (TAU * 3 / AMBIENT_PERIOD_SECONDS) + 0.3) +
    0.38 * Math.sin(seconds * (TAU * 8 / AMBIENT_PERIOD_SECONDS) + 2.1)
  );
}

/**
 * Rest emission gain.  The emission channel is an RGB light gain (~1), so the
 * lamp paper keeps its authored alpha and only its brightness breathes.
 */
const ENERGY_REST = 1;
/** Shared warm energy for the exterior paper and the interior reflection. */
const LIGHT_REST = 0.5;

/** Shared name of the moving roll/slat translateY channel. */
const BAMBOO_TRANSLATE_Y = "bambooTranslateY";

export const FRAME_02: FramePack = {
  // Generated design-faithful artwork metadata (atlas, measured cells, rails
  // and the ten authored layers), spread as the pack's geometry base.
  ...FRAME_02_ARTWORK,
  id: "02",
  title: "Bamboo roll, paper crane, paper lamp",
  version: 2,

  // One 52px painted timber beam per side, so the protected client opening is
  // the root minus 2 x 52. The rolled bundle doubles over the top beam and the
  // crane and lamp hang outside the right rail, so the R/T outsets exceed the
  // ring; the client opening itself is never relaxed.
  insets: {
    left: FRAME_02_INSET,
    right: FRAME_02_INSET,
    top: FRAME_02_INSET,
    bottom: FRAME_02_INSET,
  },
  // Contains the circular lantern spill, including the existing shallow sway.
  // Exterior drawing only: the protected client reservation remains52px.
  outsets: { left: 0, right: 112, top: 24, bottom: 0 },

  periodSeconds: SCENE_02_PERIOD_SECONDS,
  minimumRoot: FRAME_02_MINIMUM_ROOT,

  interior: {
    // The old interior rain lived in the blurred Kitty BACKDROP, so it was
    // invisible under opaque client panels. For 02 it is authored OFF: the
    // foreground weather now lives in the scene stage, which composites over
    // the real client pixels. Leaving both on would double the rain.
    rainStrength: 0,
    coolness: 0.55,
    // Measured real paper-lamp centre and spill from the generated artwork:
    // the runtime converts this logical-root placement to client-local
    // normalized coordinates and feeds BOTH the interior reflection and the
    // scene stage's warm spill, so exterior and interior stay one source.
    lightPlacement: FRAME_02_LIGHT_PLACEMENT,
    lightColor: [1, 0.87, 0.64],
    edgeShade: 0.3,
  },

  // Exact daughter-scene authoring metadata: cell layout, measured pressed
  // finger contact and foot baseline the motion geometry is derived from.
  scene: {
    atlas: SCENE_02_ATLAS,
    atlasSize: SCENE_02_ATLAS_SIZE,
    cellSize: SCENE_02_CELL_SIZE,
    pressContact: SCENE_02_PRESS_CONTACT,
    baseline: SCENE_02_BASELINE,
    sourceHeight: SCENE_02_SOURCE_HEIGHT,
  },

  createMotion(input: MotionInput): FrameMotion {
    const seconds = computed(() => read(input.seconds));
    const reduced = computed(() => read(input.reduced) === true);

    // the 120s daughter scene: one causal driver for the bamboo descent and
    // exposure, the girl's walk/press/departure and the local impacts
    const scene = createBambooSceneMotion(input);
    const reveal = scene.bambooReveal;

    // one authored gust shared by the crane, the interior and the
    // scene stage's foreground weather, published as the signed [-1, 1] wind
    // scalar; reduced motion rests at 0
    const wind = computed(() => (reduced() ? 0 : gustAt(seconds(), 0)));
    // heavier, phase-lagged response of the paper lamp
    const lampAngle = computed(() =>
      reduced() ? 0 : 0.017 * slowGustAt(seconds() - 2.2, 0.8),
    );
    // The complete paper pendant follows a slower gust about its brass eye.
    const craneAngle = computed(() =>
      reduced() ? 0 : 0.024 * slowGustAt(seconds() - 0.9, 0.35),
    );
    // ONE irregular lamp pulse drives every consequence of the same physical
    // source: the exterior paper emission (lampEnergy), the interior reflection
    // (light) and the foreground spill the scene stage reads. It is allocated
    // once; a second oscillator would desync source and effect.
    const lampPulse = computed(() => (reduced() ? 0 : lampNoiseAt(seconds())));
    // Visible but comfortable amplitude: ~20% irregular variation around the
    // rest gain, never a global flash (bounded by the authored +-1 pulse).
    const energy = computed(() => ENERGY_REST + 0.20 * read(lampPulse));
    const light = computed(() => LIGHT_REST + 0.20 * read(lampPulse));
    // the descent may only deepen the cool edge shade in the upper periphery
    const shade = computed(() => 0.12 * (reduced() ? 0 : read(reveal)));

    return {
      angles: { bambooAngle: scene.bambooAngle, craneAngle, lampAngle },
      reveals: { bambooReveal: reveal },
      translations: { [BAMBOO_TRANSLATE_Y]: scene.bambooOffsetY },
      scene,
      emissions: { lampEnergy: energy },
      wind,
      light,
      shade,
      eyes: [0, 0, 0],
    };
  },
};
