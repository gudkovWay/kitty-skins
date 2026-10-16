import type { FramePack } from "./types";

/**
 * Design-faithful artwork metadata for rainlit pack 02.
 *
 * Authored by `tools/build-rainlit-02.py` from the real `gpt-image-2`
 * component masters under
 * `assets/rainlit-frames/02/design-faithful/source/`; measured cell
 * rectangles, cuts and provenance live in
 * `assets/rainlit-frames/02/design-faithful/source.json`. The 52 logical px
 * ring doubles every ornament's geometry while the accepted bamboo cells
 * keep their authored pixels.
 *
 * Imported pure: no asset loading, effect compilation or selection side
 * effects.
 *
 * `lightPlacement` is the measured real paper-lamp centre and bounded
 * spill. It belongs to the pack's INTERIOR profile rather than the
 * top-level frame geometry, so `frame-02.ts` spreads the rest of the
 * artwork and binds this field under `interior`.
 */
export interface Frame02Art
  extends Pick<
    FramePack,
    "atlas" | "atlasSize" | "corners" | "cornerSize" | "rails" | "railPeriod" | "layers"
  > {
  readonly lightPlacement: NonNullable<FramePack["interior"]["lightPlacement"]>;
}

export const FRAME_02_ART: Frame02Art = {
  atlas: "./assets/rainlit-frames/02/design-faithful/frame-atlas.png",
  atlasSize: [2048, 2048],
  corners: {
    tl: [428, 4, 416, 416],
    tr: [848, 4, 416, 416],
    bl: [1268, 4, 416, 416],
    br: [4, 1032, 416, 416],
  },
  cornerSize: {
    top: [104, 104],
    bottom: [104, 104],
  },
  rails: {
    top: [792, 1612, 1024, 208],
    bottom: [4, 1824, 1024, 208],
    left: [4, 4, 208, 1024],
    right: [216, 4, 208, 1024],
  },
  railPeriod: { horizontal: 256, vertical: 256 },
  layers: [
    {
      id: "bamboo-slat",
      cell: [339, 1612, 331, 52],
      anchor: "tl",
      offset: [40.5, 18.5],
      size: [165.5, 26],
      pivot: [82.75, -17],
      reveal: "bambooReveal",
      revealMode: "slide",
      translateY: "bambooTranslateY",
      angle: "bambooAngle",
    },
    {
      id: "bamboo-roll",
      cell: [4, 1612, 331, 68],
      anchor: "tl",
      offset: [40.5, -15.5],
      size: [165.5, 34],
      pivot: [82.75, 17],
      translateY: "bambooTranslateY",
      angle: "bambooAngle",
    },
    {
      id: "tie-near",
      cell: [674, 1612, 38, 114],
      anchor: "tl",
      offset: [61, -17],
      size: [19, 57],
      pivot: [9.5, 2],
    },
    {
      id: "tie-far",
      cell: [716, 1612, 38, 114],
      anchor: "tl",
      offset: [165, -17],
      size: [19, 57],
      pivot: [9.5, 2],
    },
    {
      id: "crane",
      cell: [1243, 1032, 264, 189],
      anchor: "tr",
      offset: [0.582, 48],
      size: [66, 47.306],
      pivot: [32.918, 0],
      angle: "craneAngle",
    },
    {
      id: "crane-hook",
      cell: [428, 424, 176, 49],
      anchor: "tr",
      offset: [-8.262, 37.628],
      size: [44, 12.146],
      pivot: [41.762, 10.372],
    },
    {
      id: "lamp-halo",
      cell: [424, 1032, 576, 576],
      anchor: "br",
      offset: [-40.9, -159.2],
      size: [144, 144],
      pivot: [70.9, 9.2],
      angle: "lampAngle",
      emission: "lampEnergy",
    },
    {
      id: "lamp-bracket",
      cell: [1567, 1032, 222, 146],
      anchor: "br",
      offset: [-10.635, -173.415],
      size: [55.44, 36.435],
      pivot: [40.635, 23.415],
    },
    {
      id: "lamp",
      cell: [1688, 4, 235, 509],
      anchor: "br",
      offset: [1.86, -152.1],
      size: [58.695, 127.365],
      pivot: [28.14, 2.1],
      angle: "lampAngle",
    },
    {
      id: "lamp-paper",
      cell: [1004, 1032, 235, 509],
      anchor: "br",
      offset: [1.86, -152.1],
      size: [58.695, 127.365],
      pivot: [28.14, 2.1],
      angle: "lampAngle",
      emission: "lampEnergy",
    },
  ],
  lightPlacement: {
    anchor: "br",
    offset: [31.1, -87.2],
    spread: [114.4, 88.4],
  },
};
