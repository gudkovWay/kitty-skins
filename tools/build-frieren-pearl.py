#!/usr/bin/env python3
"""Author the frieren-pearl-mixed kitty-skins pack from the pinned pearl master.

Pack: four growing/bursting pearl bubbles COMBINED with rocking foliate leaf
groups (one per corner) on the supplied symmetrical pale silver/rose frame.
The master is 1477x1065 with a clean black client opening. Nothing in the
master is re-drawn: recognisable sculpture (corner clusters, hanging bead
chains, midpoint stars, column bead strands) is retained once through an
explicit selection mask, and only the neutral structural moulding is
re-authored as constant cross-sections.

Design invariants (from .superpowers/plans/2026-10-02-pearl-reference-mixed.md):

* ``frame_insets`` equal the measured source aperture and the band renders at
  physical scale 1, so neutral material and original corner material are
  authored to share one physical profile (no band widening; visual seam-free
  behaviour is a runtime property and stays unverified here).
* Source/logical offset repair: the old logical RegionSpec offsets placed
  source-space art at the wrong screen position (a midpoint star sat inside
  the protected client and was clipped). Every ornament region now declares
  offset [0,0] and the small outward seats are BAKED into the region canvases,
  pivots and published centers: corner seats (-5,-8)/(5,-8)/(-5,10)/(5,10),
  top star -4 px, bottom star +10 px outward, side stars keep their source x
  on the silver rail and are re-centered vertically in their padded band
  canvases. Content is never blindly cropped; transfers are
  count-validated.
* Midpoint star canvases are the plan's padded band rectangles (top
  [668,0,141,175], bottom [668,867,141,198], left [0,460,135,128], right
  [1341,460,136,128]) so the stars keep their place in both exact and
  adaptive layouts.
* Every corner foliate artwork is allocated into coherent articulated leaf
  groups (assets/source/frieren-pearl-petals.svg, at most six per corner).
  Each group is extracted once as a full corner-sized RGBA petal texture with
  a region-local pivot at its attachment root, a signed 4-6 degree sway and a
  staggered phase; the runtime rocks all groups concurrently. The stationary
  background owns no moving pixels; fixed roots/gems/pendants and the socket
  stay behind the motion, and the fixed foreground (setting paths plus pivot
  collars) occludes the root joints.
* Each corner pearl is extracted once into a region-sized RGBA bubble texture
  at its seated position; the accepted bubble-body extraction and socket are
  unchanged, only the shared period changed (8 -> 32 seconds). Where the
  foreground covers the body, the bubble carries the pearl sample reflected
  across the body center so the moving mask stays a complete ellipse with no
  leaf notch.
* flower_effects entries carry ``bubble`` and one to six ``petals`` together
  (mixed contract; the runtime still accepts either alone).
* Motion itself lives in the native runtime; this tool only authors rasters
  and the manifest.

Deterministic: fixed constant tables, median-based measurement, one fixed
ImageMagick argument shape per step, uniform RGBA (png32) export, stdlib
only. All checks fail loudly; nothing is silently clamped, cleared or cut.

Run:
    python3 tools/build-frieren-pearl.py \
        --source assets/source/frieren-pearl-reference.png \
        --mask assets/source/frieren-pearl-ornaments.svg \
        --petal-mask assets/source/frieren-pearl-petals.svg \
        --output assets/skins/frieren-pearl-mixed \
        [--expect-mask-sha <sha256>] [--expect-petal-mask-sha <sha256>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import statistics
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path

# ---------------------------------------------------------------- constants

PACK_ID = "frieren-pearl-mixed"
PACK_NAME = "Frieren Pearl Mixed"

SOURCE_W = 1477
SOURCE_H = 1065
SOURCE_SHA = "edcb2f5aa2c935fb113069aac301ca584f754363c14bb28cd0e95e9613f37a40"
# Optional pin; the parent owns the mask and passes its SHA via
# --expect-mask-sha (the builder always records the actual mask SHA).
DEFAULT_MASK_SHA = "1404b74dc97b2b5186a97965ade63db37a7c56bf519be345bb06f9790dccf4d9"
# Same contract for the new moving-foliage selection mask.
DEFAULT_PETAL_MASK_SHA = "6cd49108a1ece54d8d093760b4964acc0c9b84f99293512aeff5620937eb3f24"

# Measured painted inner lip of the neutral bands, verified by
# measure_aperture against LIP_EXPECTED. Physical frame insets equal the
# measured aperture (no widening); sculpture alpha is never cleared to make
# the opening -- see the recorded authoring-risk note in the provenance.
NEUTRAL_LIP = {"left": 135, "right": 136, "top": 175, "bottom": 198}
APERTURE = dict(NEUTRAL_LIP)
LIP_EXPECTED = {
    "top": (160, 180),
    "bottom": (855, 875),
    "left": (125, 145),
    "right": (1331, 1351),
}
# Neutral sampling ranges away from corners, stars and strands.
PROFILE_RANGE = {
    "top": (380, 520),
    "bottom": (380, 520),
    "left": (570, 680),
    "right": (570, 680),
}
COLUMN_CAP = 32  # structural column end caps; middle tile repeats

# Pearl geometry. Published manifest values: center (region-local, the body
# center) and the enclosing axis-aligned radii. Builder-local extraction uses
# the actual tilted body ellipse (body_center/body_radii/body_angle). Parent-
# owned constants; replaced when the parent's extraction geometry lands.
PEARLS = {
    "tl": {"center": (113.5, 151.0), "radii": (27.0, 28.0),
           "body_center": (113.5, 151.0), "body_radii": (24.5, 24.5),
           "body_angle": 0.0, "canvas": (0, 0, 300, 450),
           "outward": (-1.0, -1.0), "phase": 0.0},
    "tr": {"center": (1360.5, 151.0), "radii": (27.0, 28.0),
           "body_center": (1360.5, 151.0), "body_radii": (24.5, 24.5),
           "body_angle": 0.0, "canvas": (1177, 0, 300, 450),
           "outward": (1.0, -1.0), "phase": 0.25},
    "br": {"center": (1366.0, 871.0), "radii": (22.0, 27.0),
           "body_center": (1366.0, 871.0), "body_radii": (19.5, 25.5),
           "body_angle": 22.0, "canvas": (1177, 700, 300, 365),
           "outward": (1.0, 1.0), "phase": 0.5},
    "bl": {"center": (108.0, 871.0), "radii": (22.0, 27.0),
           "body_center": (108.0, 871.0), "body_radii": (19.5, 25.5),
           "body_angle": -22.0, "canvas": (0, 700, 300, 365),
           "outward": (-1.0, 1.0), "phase": 0.75},
}
PEARL_SPREAD = 24.0
# Owner-accepted pearl growth/rupture/droplet behaviour, slowed 8 -> 32 s:
# the timing is supplied to the unchanged runtime math through spec.period.
PEARL_PERIOD = 32.0
BODY_ANTIALIAS_PX = 0.6  # extraction edge feather, in source pixels
EXCURSION_SLACK = 6.0  # runtime conservative bound: max(radii)+spread+6

# Fixed setting overlap, parent-owned: source-space SVG path data per corner.
# TL keeps the silver leaf crossing the lower-left pearl edge; TR is its
# mirror about x=737 (x' = 1474 - x). BL/BR stay transparent: the source
# settings sit outside the pearl body. Paths are clipped to the body ellipse.
PEARL_FOREGROUND_PATHS: dict[str, str] = {
    "tl": "M84,152 C90,162 103,171 116,178 L111,181 L91,175 Z",
    "tr": "M1390,152 C1384,162 1371,171 1358,178 L1363,181 L1383,175 Z",
}


def _pearl_excursion(pearl: dict) -> float:
    return max(pearl["radii"]) + PEARL_SPREAD + EXCURSION_SLACK


def burst_opening_overlap() -> dict[str, dict]:
    """Measured authoring risk: where the full conservative burst bound
    (square of the circular shader/FBO excursion) actually overlaps the real
    client opening rectangle. Reported as-is; the runtime keeps the full
    circular bound, while the rupture shader is restricted to the outward
    hemisphere, so this is a bound-level note, not a droplet claim."""
    L = APERTURE["left"]
    R = SOURCE_W - APERTURE["right"]
    T = APERTURE["top"]
    B = SOURCE_H - APERTURE["bottom"]
    report = {}
    for name, pearl in PEARLS.items():
        exc = _pearl_excursion(pearl)
        cx, cy = pearl["center"]
        x0, x1 = cx - exc, cx + exc
        y0, y1 = cy - exc, cy + exc
        ix0, ix1 = max(x0, L), min(x1, R)
        iy0, iy1 = max(y0, T), min(y1, B)
        if ix0 < ix1 and iy0 < iy1:
            report[name] = {
                "excursion": exc,
                "overlap_rect": [round(ix0, 1), round(iy0, 1),
                                 round(ix1, 1), round(iy1, 1)],
                "overlap_area_px": round((ix1 - ix0) * (iy1 - iy0), 1),
            }
    return report


# Sculpture ownership windows: meaningful, pairwise disjoint rectangles that
# must jointly own every nonzero selection-mask pixel exactly once. These are
# the ORIGINAL source-space selection extents -- the mask is validated here,
# never against the padded output canvases (placement lives in STAR_SPECS /
# ornament_regions).
SCULPTURE_WINDOWS = {
    "pearl-tl": (0, 0, 300, 450),
    "pearl-tr": (1177, 0, 300, 450),
    "pearl-bl": (0, 700, 300, 365),
    "pearl-br": (1177, 700, 300, 365),
    "star-top": (668, 100, 141, 80),
    "star-bottom": (668, 848, 141, 82),
    "star-left": (72, 460, 64, 128),
    "star-right": (1341, 460, 64, 128),
}

# Offset repair: source-space selection windows and their padded output
# canvases. All ornament regions declare offset [0,0]; the seat below is
# baked into the canvas pixels instead. Numeric seats are the plan's measured
# outward moves; the "center-y" seats keep the star's SOURCE x exactly (the
# padded 135/136 px band is mostly exterior blank left of the silver rail --
# the rail spans only ~x72..135) and re-center the coverage bounding box
# vertically inside the 128 px canvas with deterministic integer rounding.
STAR_SPECS = {
    "star-top": {"window": (668, 100, 141, 80),
                 "canvas": (668, 0, 141, 175), "seat": (0, -4)},
    "star-bottom": {"window": (668, 848, 141, 82),
                    "canvas": (668, 867, 141, 198), "seat": (0, 10)},
    "star-left": {"window": (72, 460, 64, 128),
                  "canvas": (0, 460, 135, 128), "seat": "center-y"},
    "star-right": {"window": (1341, 460, 64, 128),
                   "canvas": (1341, 460, 136, 128), "seat": "center-y"},
}

# Corner outward seats baked into every corner raster, pivot and published
# center (plan-measured; outward is away from the client center).
CORNER_SEATS = {"tl": (-5, -8), "tr": (5, -8), "bl": (-5, 10), "br": (5, 10)}

# Moving-foliage articulation. Magnitudes stay inside the runtime's signed
# 4-6 degree contract; the sign is derived per group from its coverage
# centroid so the initial deflection biases outward. Phases stagger the
# groups within the corner cycle on top of the corner's pearl phase.
PETAL_MAGNITUDES = (5.0, 4.0, 6.0, 4.5, 5.5, 4.25)
MIN_PETAL_PX = 400      # a coherent group owns at least this many mask pixels
ROOT_COLLAR_RADIUS = 12.0   # fixed original-source collar around every pivot
OWN = 128  # ownership threshold on antialiased gray coverage rasters

# Deliberately fixed material inside the corner windows, in SOURCE
# coordinates. Everything here stays in the stationary background/foreground
# and is subtracted from every petal mask:
# * chain corridors: hanging bead wire, beads and star pendant (plus the
#   few-pixel leaf fringe that is raster-inseparable from the wire);
# * the TL/TR silver root leaf over the pearl edge (parent-owned setting);
# * red gems, the BL/BR corner setting ball and the pendant pearl.
CHAIN_CORRIDORS = {
    "tl": [(40, 96, 64, 240), (40, 240, 68, 350), (40, 350, 68, 412)],
    "bl": [(50, 730, 76, 800)],
}
ROOT_RECTS = {"tl": [(78, 144, 124, 190)], "bl": []}
FIXED_DISCS = {
    "tl": [(152, 133, 12)],                                  # red gem
    "bl": [(88, 829, 12),            # red gem in the rising leaves
           (152, 875, 12),           # corner setting ball
           (135, 895, 11),           # lower red gem beside the rail leaves
           (166, 936, 16)],          # pendant pearl and its wire stub
}


def _mirror_x(rect):
    """Mirror a source-space (x0, y0, x1, y1) rectangle about x = 737."""
    x0, y0, x1, y1 = rect
    return (SOURCE_W - 3 - x1, y0, SOURCE_W - 3 - x0, y1)


for _side, _src in (("tr", "tl"), ("br", "bl")):
    CHAIN_CORRIDORS[_side] = [_mirror_x(r) for r in CHAIN_CORRIDORS[_src]]
for _side, _src in (("tr", "tl"), ("br", "bl")):
    ROOT_RECTS[_side] = [_mirror_x(r) for r in ROOT_RECTS.get(_src, [])]
    FIXED_DISCS[_side] = [(SOURCE_W - 3 - x, y, r)
                          for (x, y, r) in FIXED_DISCS[_src]]

MATTE_LO = 18.0   # luminance key on the near-black studio background
MATTE_HI = 46.0

EXACT_ASPECT = round(SOURCE_W / SOURCE_H, 6)  # 1.386851
EXACT_ASPECT_TOL = 0.005
EXACT_MIN = (900, 600)
ADAPTIVE_SCALE = 0.65
ADAPTIVE_MIN_CLIENT = (560.0, 360.0)

SUPERSCALE = 4  # SVG supersampling factor for ImageMagick rasterization


# ---------------------------------------------------------------- utilities

def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(argv: list[str], stdin: bytes | None = None) -> bytes:
    proc = subprocess.run(argv, input=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): "
                           f"{' '.join(argv)}\n{proc.stderr.decode(errors='replace')}")
    return proc.stdout


def magick() -> str:
    for candidate in ("magick", "convert"):
        if shutil.which(candidate):
            return candidate
    raise RuntimeError("ImageMagick not found")


def _render(svg_text: str, width: int, height: int, pixel_format: str,
            bytes_pp: int) -> bytearray:
    tool = magick()
    with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False) as handle:
        handle.write(svg_text)
        svg_path = Path(handle.name)
    # Rasterize the SVG natively at 384 DPI (4x the 96-DPI source grid), then
    # resize to the final grid. Gray works against black; RGBA needs a
    # transparent background so uncovered canvas stays transparent.
    background = "black" if pixel_format == "gray" else "none"
    try:
        raw = run([tool, "-density", str(96 * SUPERSCALE),
                   "-background", background,
                   str(svg_path),
                   "-resize", f"{width}x{height}!",
                   "-colorspace", "sRGB",
                   "-depth", "8", f"{pixel_format}:-"])
    finally:
        svg_path.unlink(missing_ok=True)
    if len(raw) != width * height * bytes_pp:
        raise RuntimeError(f"unexpected {pixel_format} raster size {len(raw)}")
    return bytearray(raw)


def render_gray(svg_text: str, width: int, height: int) -> bytearray:
    return _render(svg_text, width, height, "gray", 1)


def render_rgba(svg_text: str, width: int, height: int) -> bytearray:
    return _render(svg_text, width, height, "rgba", 4)


def load_rgba(path: Path, width: int, height: int) -> bytearray:
    tool = magick()
    raw = run([tool, str(path), "-resize", f"{width}x{height}!",
               "-depth", "8", "rgba:-"])
    if len(raw) != width * height * 4:
        raise RuntimeError(f"unexpected rgba raster size {len(raw)}")
    return bytearray(raw)


def write_png(data: bytes | bytearray, width: int, height: int, dest: Path) -> None:
    tool = magick()
    run([tool, "-size", f"{width}x{height}", "-depth", "8", "rgba:-",
         "-define", "png:compression-level=9", f"png32:{dest}"], stdin=bytes(data))


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def smoothstep(low: float, high: float, value: float) -> float:
    t = clamp((value - low) / (high - low), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def luminance(data: bytearray, x: int, y: int) -> float:
    i = (y * SOURCE_W + x) * 4
    return 0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2]


def outside_opening(x: int, y: int) -> bool:
    L, R = APERTURE["left"], APERTURE["right"]
    T, B = APERTURE["top"], APERTURE["bottom"]
    return not (L <= x < SOURCE_W - R and T <= y < SOURCE_H - B)


# ---------------------------------------------------------------- measurement

def _first_run(values: dict[int, float], start: int, stop: int, step: int,
               threshold: float, run_len: int = 4) -> int | None:
    """First coordinate (walking start->stop) opening a run of bright samples."""
    for pos in range(start, stop, step):
        good = 0
        for d in range(run_len):
            idx = pos + d * step
            if idx in values and values[idx] > threshold:
                good += 1
        if good == run_len:
            return pos
    return None


def measure_aperture(data: bytearray) -> dict[str, float]:
    """Median painted inner lip per side over neutral sample lines."""
    cx, cy = SOURCE_W // 2, (APERTURE["top"] + (SOURCE_H - APERTURE["bottom"])) // 2
    found: dict[str, list[float]] = {"top": [], "bottom": [], "left": [], "right": []}
    for x in (400, cx, 1100):
        col = {y: luminance(data, x, y) for y in range(SOURCE_H)}
        hit = _first_run(col, cy, 0, -1, 80.0)
        if hit is not None:
            found["top"].append(float(hit))
        hit = _first_run(col, cy, SOURCE_H - 1, 1, 80.0)
        if hit is not None:
            found["bottom"].append(float(hit))
    for y in (300, cy, 750):
        row = {x: luminance(data, x, y) for x in range(SOURCE_W)}
        hit = _first_run(row, cx, 0, -1, 80.0)
        if hit is not None:
            found["left"].append(float(hit))
        hit = _first_run(row, cx, SOURCE_W - 1, 1, 80.0)
        if hit is not None:
            found["right"].append(float(hit))
    lip = {}
    for side, hits in found.items():
        if not hits:
            raise RuntimeError(f"aperture measurement failed on side {side}")
        median = statistics.median(hits)
        low, high = LIP_EXPECTED[side]
        if not (low <= median <= high):
            raise RuntimeError(f"measured {side} lip {median:.1f} outside expected "
                               f"[{low}, {high}] -- master geometry changed?")
        lip[side] = median
    return lip


def measure_outer_edge(data: bytearray, side: str) -> int:
    """Outermost visible painted coordinate of a band, as the distance from the
    outer image edge (the alpha cut position used by band_alpha)."""
    limit = APERTURE[side]
    if side in ("top", "bottom"):
        x0, x1 = PROFILE_RANGE[side]
        rng = range(0, limit) if side == "top" else range(SOURCE_H - limit, SOURCE_H)
        counts: dict[int, int] = {}
        for cross in rng:
            for x in range(x0, x1):
                if luminance(data, x, cross) > 60.0:
                    counts[cross] = counts.get(cross, 0) + 1
        painted = [c for c, n in counts.items() if n > (x1 - x0) * 0.5]
        if not painted:
            raise RuntimeError(f"outer edge measurement failed on side {side}")
        if side == "top":
            return min(painted)
        return SOURCE_H - 1 - max(painted)
    y0, y1 = PROFILE_RANGE[side]
    rng = range(0, limit) if side == "left" else range(SOURCE_W - limit, SOURCE_W)
    counts = {}
    for cross in rng:
        for y in range(y0, y1):
            if luminance(data, cross, y) > 60.0:
                counts[cross] = counts.get(cross, 0) + 1
    painted = [c for c, n in counts.items() if n > (y1 - y0) * 0.5]
    if not painted:
        raise RuntimeError(f"outer edge measurement failed on side {side}")
    if side == "left":
        return min(painted)
    return SOURCE_W - 1 - max(painted)


# ---------------------------------------------------------------- structural

def build_profile(data: bytearray, side: str) -> list[tuple[int, int, int, int]]:
    """Median cross-section of a neutral band stretch (constant along axis).

    ``profile[cross]`` is indexed by the distance from the outer image edge on
    that side, matching the band row/column it will be extruded onto.
    """
    limit = APERTURE[side]
    a, b = PROFILE_RANGE[side]
    profile: list[tuple[int, int, int, int]] = []
    for cross in range(limit):
        samples = []
        for c in range(a, b):
            if side == "top":
                i = (cross * SOURCE_W + c) * 4
            elif side == "bottom":
                i = ((SOURCE_H - 1 - cross) * SOURCE_W + c) * 4
            elif side == "left":
                i = (c * SOURCE_W + cross) * 4
            else:  # right
                i = (c * SOURCE_W + (SOURCE_W - 1 - cross)) * 4
            samples.append((data[i], data[i + 1], data[i + 2], data[i + 3]))
        profile.append(tuple(int(round(statistics.median(s[k] for s in samples)))
                             for k in range(4)))
    return profile


def structural_canvas(data: bytearray, profiles: dict, edges: dict,
                      lips: dict) -> bytearray:
    """Reconstructed neutral structural silver on a full source canvas.

    Bands are extrusions of their measured profiles; corners miter the two
    adjoining profiles on the diagonal. Alpha fades in at the measured outer
    visible edge and out after the measured neutral lip.
    """
    out = bytearray(SOURCE_W * SOURCE_H * 4)
    L, R = APERTURE["left"], APERTURE["right"]
    T, B = APERTURE["top"], APERTURE["bottom"]

    def put(x: int, y: int, px: tuple[int, int, int, int], alpha: float) -> None:
        i = (y * SOURCE_W + x) * 4
        out[i] = px[0]
        out[i + 1] = px[1]
        out[i + 2] = px[2]
        out[i + 3] = int(round(255.0 * clamp(alpha, 0.0, 1.0)))

    def band_alpha(side: str, cross: int) -> float:
        """All cross coordinates are distances from the outer image edge:
        fade in at the measured outer visible edge, fade out after the
        measured neutral lip so no dark strip appears past the lip."""
        edge = edges[side]
        lip = lips[side]
        if side == "bottom":
            lip = SOURCE_H - 1 - lip
        elif side == "right":
            lip = SOURCE_W - 1 - lip
        outer = smoothstep(edge - 1.0, edge + 1.0, cross)
        inner = 1.0 - smoothstep(lip - 1.0, lip + 1.0, cross)
        return outer * inner

    for x in range(L, SOURCE_W - R):
        for y in list(range(T)) + list(range(SOURCE_H - B, SOURCE_H)):
            side = "top" if y < T else "bottom"
            cross = y if side == "top" else SOURCE_H - 1 - y
            put(x, y, profiles[side][cross], band_alpha(side, cross))
    for y in range(T, SOURCE_H - B):
        for x in list(range(L)) + list(range(SOURCE_W - R, SOURCE_W)):
            side = "left" if x < L else "right"
            cross = x if side == "left" else SOURCE_W - 1 - x
            put(x, y, profiles[side][cross], band_alpha(side, cross))

    corners = (
        (0, 0, L, T, profiles["top"], profiles["left"], "top", "left"),
        (SOURCE_W - R, 0, R, T, profiles["top"], profiles["right"], "top", "right"),
        (0, SOURCE_H - B, L, B, profiles["bottom"], profiles["left"], "bottom", "left"),
        (SOURCE_W - R, SOURCE_H - B, R, B, profiles["bottom"], profiles["right"],
         "bottom", "right"),
    )
    for ox, oy, cw, ch, hprof, vprof, hside, vside in corners:
        hspan = APERTURE[hside] - edges[hside]
        vspan = APERTURE[vside] - edges[vside]
        for x in range(ox, ox + cw):
            for y in range(oy, oy + ch):
                lx, ly = x - ox, y - oy
                hcross = ly if hside == "top" else ch - 1 - ly
                vcross = lx if vside == "left" else cw - 1 - lx
                ha = band_alpha(hside, hcross)
                va = band_alpha(vside, vcross)
                if (vcross - edges[vside]) * hspan >= (hcross - edges[hside]) * vspan:
                    px, alpha = hprof[hcross], ha
                else:
                    px, alpha = vprof[vcross], va
                put(x, y, px, alpha)
    return out


# ---------------------------------------------------------------- pearl masks

def pearl_field(pearl: dict, x: float, y: float) -> float:
    """Feathered coverage of the actual tilted pearl body ellipse.

    The extraction mask is the complete body (the moving bubble mask never
    carries a leaf notch); the ~0.6 px antialias is the only material
    transition outside the body.
    """
    cx, cy = pearl["body_center"]
    rx, ry = pearl["body_radii"]
    ang = math.radians(pearl["body_angle"])
    dx, dy = x - cx, y - cy
    u = dx * math.cos(ang) + dy * math.sin(ang)
    v = -dx * math.sin(ang) + dy * math.cos(ang)
    d = (u / rx) ** 2 + (v / ry) ** 2
    delta = (BODY_ANTIALIAS_PX / min(rx, ry)) * 1.5
    return smoothstep(1.0 + delta, 1.0 - delta, d)


# ---------------------------------------------------------------- authored art

def _body_ellipse_svg(pearl: dict, fill_ref: str) -> str:
    cx, cy = pearl["body_center"]
    rx, ry = pearl["body_radii"]
    ang = pearl["body_angle"]
    return (f'<ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" fill="{fill_ref}" '
            f'transform="rotate({ang} {cx} {cy})"/>')


def foreground_svg(name: str, pearl: dict, seat: tuple[float, float]) -> str:
    """Region-local foreground mask: the parent-owned source-space setting
    path clipped to the pearl body ellipse. Corners without a path (BL/BR)
    stay fully transparent. The raster pixel (rx, ry) must show source-space
    point (ox + rx - seat_x, oy + ry - seat_y), so the viewBox origin is the
    region origin minus the baked seat."""
    ox, oy, w, h = pearl["canvas"]
    vx, vy = ox - seat[0], oy - seat[1]
    path = PEARL_FOREGROUND_PATHS.get(name, "")
    body = _body_ellipse_svg(pearl, "white")
    clip = f'<clipPath id="body">{body}</clipPath>' if path else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="{vx} {vy} {w} {h}">'
        f'<rect x="{vx}" y="{vy}" width="{w}" height="{h}" fill="black"/>'
        f'<defs>{clip}</defs>'
        f'<path d="{path}" clip-path="url(#body)" fill="white"/>'
        f'</svg>')


def collar_svg(groups: list[dict], seat: tuple[float, float],
               w: int, h: int) -> str:
    """Region-local fixed pivot collars: one original-source disc per group
    pivot so a rocking leaf never floats off its root. The collars join the
    fixed foreground and are subtracted from every petal mask."""
    ox, oy, _, _ = groups[0]["canvas_rect"]
    vx, vy = ox - seat[0], oy - seat[1]
    circles = "".join(
        f'<circle cx="{g["pivot"][0]}" cy="{g["pivot"][1]}" '
        f'r="{ROOT_COLLAR_RADIUS}" fill="white"/>'
        for g in groups)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="{vx} {vy} {w} {h}">'
        f'<rect x="{vx}" y="{vy}" width="{w}" height="{h}" fill="black"/>'
        f'{circles}'
        f'</svg>')


def socket_svg(name: str, pearl: dict, seat: tuple[float, float]) -> str:
    """Region-local recessed socket, confined to the same body ellipse.
    ViewBox shifted by the baked seat like the foreground mask."""
    ox, oy, w, h = pearl["canvas"]
    vx, vy = ox - seat[0], oy - seat[1]
    body = _body_ellipse_svg(pearl, "url(#pit)")
    rim = _body_ellipse_svg(pearl, "url(#rimlight)")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="{vx} {vy} {w} {h}">'
        f'<defs>'
        f'<radialGradient id="pit" cx="42%" cy="38%" r="68%">'
        f'<stop offset="0" stop-color="#241f28" stop-opacity="0.97"/>'
        f'<stop offset="0.72" stop-color="#37303e" stop-opacity="0.93"/>'
        f'<stop offset="1" stop-color="#665a70" stop-opacity="0"/>'
        f'</radialGradient>'
        f'<radialGradient id="rimlight" cx="50%" cy="82%" r="42%">'
        f'<stop offset="0" stop-color="#b7a9c0" stop-opacity="0.5"/>'
        f'<stop offset="1" stop-color="#b7a9c0" stop-opacity="0"/>'
        f'</radialGradient>'
        f'</defs>'
        f'{body}{rim}'
        f'</svg>')


# ---------------------------------------------------------------- validation

def validate_pearls(data: bytearray) -> dict:
    stats = {}
    for name, pearl in PEARLS.items():
        cx, cy = pearl["body_center"]
        rx, ry = pearl["body_radii"]
        ang = math.radians(pearl["body_angle"])
        samples = []
        for y in range(int(cy - ry), int(cy + ry) + 1):
            for x in range(int(cx - rx), int(cx + rx) + 1):
                dx, dy = x - cx, y - cy
                u = dx * math.cos(ang) + dy * math.sin(ang)
                v = -dx * math.sin(ang) + dy * math.cos(ang)
                if (u / (rx * 0.7)) ** 2 + (v / (ry * 0.7)) ** 2 <= 1.0:
                    samples.append(luminance(data, x, y))
        mean = statistics.mean(samples)
        if mean < 110.0:
            raise RuntimeError(f"pearl {name}: inner-face mean luminance {mean:.1f} "
                               f"below 110 -- authored geometry does not sit on the "
                               f"pearl body")
        ox, oy, w, h = pearl["canvas"]
        exc = _pearl_excursion(pearl)
        lx, ly = pearl["center"][0] - ox, pearl["center"][1] - oy
        if not (exc <= lx <= w - exc and exc <= ly <= h - exc):
            raise RuntimeError(f"pearl {name}: excursion radius {exc:.1f} around "
                               f"region-local center ({lx:.1f}, {ly:.1f}) does not "
                               f"fit canvas {w}x{h} -- refusing to clamp")
        stats[name] = {"inner_mean_luminance": round(mean, 1),
                       "excursion_radius": round(exc, 1)}
    return stats


def validate_mask_ownership(coverage: bytearray) -> dict:
    """Every nonzero selection-mask pixel must be owned exactly once by one
    of the meaningful, pairwise disjoint ornament windows. No luminance or
    profile classification: ownership is a pure rectangle contract, so a
    mask that retains rail runs or spills between windows fails here instead
    of silently deforming the frame."""
    owner = bytearray(SOURCE_W * SOURCE_H)  # 0 = unowned, else window index+1
    counts = {name: 0 for name in SCULPTURE_WINDOWS}
    doubles = 0
    for index, (name, (ox, oy, w, h)) in enumerate(SCULPTURE_WINDOWS.items()):
        win = index + 1
        for y in range(oy, oy + h):
            row = y * SOURCE_W
            for x in range(ox, ox + w):
                i = row + x
                if coverage[i] == 0:
                    continue
                counts[name] += 1
                if owner[i]:
                    doubles += 1
                owner[i] = win
    orphans = 0
    for i in range(SOURCE_W * SOURCE_H):
        if coverage[i] and owner[i] == 0:
            orphans += 1
    if orphans or doubles:
        raise RuntimeError(f"selection mask ownership violated: {orphans} masked "
                           f"pixels outside every ornament window, {doubles} "
                           f"pixels with more than one owner -- the mask must "
                           f"cover only the authored windows")
    return {"window_px": counts, "orphans": 0, "double_owned": 0}


# ------------------------------------------------- moving foliage groups

def parse_petal_groups(svg_text: str) -> dict[str, list[dict]]:
    """Parse the authored foliage selection mask. Each corner carries 1..6
    ``g`` elements with id ``<corner>-<n>`` holding one polygon in SOURCE
    coordinates plus data-pivot/data-desc attributes. Document order defines
    the group order; the runtime's six-petal cap is enforced here."""
    root = ET.fromstring(svg_text)
    groups: dict[str, list[dict]] = {}
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] != "g":
            continue
        gid = el.get("id", "")
        corner = gid[:2]
        if len(gid) != 4 or gid[2] != "-" or corner not in PEARLS \
                or not gid[3].isdigit():
            continue
        polygons = [child for child in el
                    if child.tag.rsplit("}", 1)[-1] == "polygon"]
        if len(polygons) != 1:
            raise RuntimeError(f"petal group {gid}: expected exactly one polygon")
        points = []
        for token in polygons[0].get("points", "").split():
            xs, ys = token.split(",")
            points.append((int(float(xs)), int(float(ys))))
        if len(points) < 3:
            raise RuntimeError(f"petal group {gid}: degenerate polygon")
        pivot = el.get("data-pivot", "")
        try:
            pxs, pys = (float(v) for v in pivot.split(","))
        except ValueError:
            raise RuntimeError(f"petal group {gid}: bad data-pivot {pivot!r}")
        groups.setdefault(corner, []).append({
            "id": gid,
            "desc": el.get("data-desc", ""),
            "polygon": points,
            "pivot": (pxs, pys),
            "canvas_rect": PEARLS[corner]["canvas"],
        })
    for corner, pearl in PEARLS.items():
        gs = groups.get(corner, [])
        if not gs:
            raise RuntimeError(f"petal mask defines no groups for corner {corner}")
        if len(gs) > 6:
            raise RuntimeError(f"petal mask defines {len(gs)} groups for corner "
                               f"{corner}; the runtime caps petals at six")
        ox, oy, w, h = pearl["canvas"]
        for g in gs:
            for x, y in g["polygon"]:
                if not (ox <= x <= ox + w and oy <= y <= oy + h):
                    raise RuntimeError(f"petal group {g['id']}: polygon vertex "
                                       f"({x}, {y}) outside corner canvas")
    return groups


def petal_group_svg(group: dict) -> str:
    pts = " ".join(f"{x},{y}" for x, y in group["polygon"])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{SOURCE_W}" '
            f'height="{SOURCE_H}" viewBox="0 0 {SOURCE_W} {SOURCE_H}">'
            f'<rect width="{SOURCE_W}" height="{SOURCE_H}" fill="black"/>'
            f'<polygon points="{pts}" fill="white"/></svg>')


def fixed_kept_svg(corner: str, groups: list[dict]) -> str:
    """Source-space raster of everything deliberately fixed inside the corner
    window: chain corridors, root rectangles, gem/ball/pendant discs and the
    pivot collars. Subtracted from every petal mask and used as the
    completeness reference for the partition guard."""
    shapes = []
    for x0, y0, x1, y1 in CHAIN_CORRIDORS[corner]:
        shapes.append(f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" '
                      f'height="{y1 - y0}" fill="white"/>')
    for x0, y0, x1, y1 in ROOT_RECTS.get(corner, []):
        shapes.append(f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" '
                      f'height="{y1 - y0}" fill="white"/>')
    discs = list(FIXED_DISCS[corner])
    for g in groups:
        discs.append((g["pivot"][0], g["pivot"][1], ROOT_COLLAR_RADIUS))
    for x, y, r in discs:
        shapes.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="white"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{SOURCE_W}" '
            f'height="{SOURCE_H}" viewBox="0 0 {SOURCE_W} {SOURCE_H}">'
            f'<rect width="{SOURCE_W}" height="{SOURCE_H}" fill="black"/>'
            f'{"".join(shapes)}</svg>')


def render_petal_masks(petal_svg_text: str, ornament: bytearray
                       ) -> tuple[dict[str, list[dict]], dict[str, bytearray]]:
    """Rasterize per-corner fixed sets and petal group coverages. Effective
    group coverage = polygon render x ornament selection, restricted to the
    corner window, with the fixed set cleared and the pearl body removed, so
    guards, group counts and sweep checks see foliage only."""
    groups = parse_petal_groups(petal_svg_text)
    fixed: dict[str, bytearray] = {}
    for corner in PEARLS:
        raster = render_gray(fixed_kept_svg(corner, groups[corner]),
                             SOURCE_W, SOURCE_H)
        for i in range(SOURCE_W * SOURCE_H):
            if raster[i] >= OWN:
                raster[i] = 255
            else:
                raster[i] = 0
        fixed[corner] = raster
    for corner in groups:
        pearl = PEARLS[corner]
        ox, oy, w, h = pearl["canvas"]
        for g in groups[corner]:
            raw = render_gray(petal_group_svg(g), SOURCE_W, SOURCE_H)
            fix = fixed[corner]
            for y in range(oy, oy + h):
                row = y * SOURCE_W
                for x in range(ox, ox + w):
                    i = row + x
                    if fix[i] == 255:
                        raw[i] = 0
                    else:
                        # ornament selection minus the pearl body: the body
                        # belongs to the bubble, never to a rocking leaf, so
                        # guards and group counts see foliage only
                        raw[i] = int(round(
                            raw[i] * (ornament[i] / 255.0)
                            * (1.0 - pearl_field(pearl, x, y))))
            g["coverage"] = raw
    return groups, fixed


def validate_petal_partition(ornament: bytearray, groups, fixed) -> dict:
    """The ornament selection inside each corner window must partition into
    moving petal groups and the declared fixed set: no masked pixel may be
    lost, and no pixel may belong to both. Guards represent the full
    anticipated artwork; nothing is silently deleted."""
    report = {}
    for corner, pearl in PEARLS.items():
        ox, oy, w, h = pearl["canvas"]
        covs = [g["coverage"] for g in groups[corner]]
        fix = fixed[corner]
        orphans = doubles = overlaps = petal_px = bubble_px = 0
        sample = None
        for y in range(oy, oy + h):
            row = y * SOURCE_W
            for x in range(ox, ox + w):
                i = row + x
                if ornament[i] < OWN:
                    continue
                if pearl_field(pearl, x, y) >= 0.5:
                    # the pearl body belongs to the moving bubble, not to
                    # the foliage partition
                    bubble_px += 1
                    continue
                # Shared antialiased edges split coverage between groups;
                # neither half need reach OWN. Compare conserved coverage,
                # allowing only one-byte raster rounding per group.
                covered = sum(c[i] for c in covs)
                expected = ornament[i] * (1.0 - pearl_field(pearl, x, y))
                rounding = len(covs)
                if fix[i] >= OWN:
                    doubles += covered > rounding
                elif covered > expected + rounding:
                    overlaps += 1
                elif covered + rounding < expected:
                    orphans += 1
                    if sample is None:
                        sample = (x, y)
                else:
                    petal_px += 1
        if orphans or doubles or overlaps:
            raise RuntimeError(
                f"foliage partition violated in corner {corner}: {orphans} "
                f"ornament pixels are neither petal nor fixed (first at "
                f"{sample}), {doubles} pixels both moving and fixed, "
                f"{overlaps} pixels claimed by two groups -- extend the "
                f"authored polygon or the fixed set; leaves are never deleted")
        per_group = []
        for g in groups[corner]:
            px = sum(1 for y in range(oy, oy + h) for x in range(ox, ox + w)
                     if g["coverage"][y * SOURCE_W + x] >= OWN)
            if px < MIN_PETAL_PX:
                raise RuntimeError(f"petal group {g['id']}: only {px} mask "
                                   f"pixels -- not a coherent leaf group")
            per_group.append(px)
        report[corner] = {"moving_px": petal_px, "bubble_body_px": bubble_px,
                          "group_px": per_group}
    return report


def outward_sign(pearl: dict, group: dict) -> int:
    """Signed sway bias: choose the rotation direction whose initial
    displacement carries the group centroid toward the corner's outward
    vector (rotation by +A in y-down screen coordinates moves a point at
    offset d by A * (-dy, dx))."""
    ox, oy, w, h = pearl["canvas"]
    cov = group["coverage"]
    sx = sy = n = 0
    for y in range(oy, oy + h):
        row = y * SOURCE_W
        for x in range(ox, ox + w):
            if cov[row + x] >= OWN:
                sx += x
                sy += y
                n += 1
    dx, dy = sx / n - group["pivot"][0], sy / n - group["pivot"][1]
    oxv, oyv = pearl["outward"]
    return 1 if (-dy * oxv + dx * oyv) >= 0 else -1


def validate_petal_sweep(groups) -> dict:
    """Client and source safety of the full anticipated leaf sweep: every
    owned mask pixel, rotated by plus AND minus the authored amplitude about
    its pivot and displaced by the baked corner seat, must stay inside its
    corner canvas and clear of the unchanged client opening."""
    report = {}
    for corner, pearl in PEARLS.items():
        ox, oy, w, h = pearl["canvas"]
        sx, sy = CORNER_SEATS[corner]
        for index, g in enumerate(groups[corner]):
            amplitude = PETAL_MAGNITUDES[index % len(PETAL_MAGNITUDES)]
            pivot_px, pivot_py = g["pivot"]
            worst = 0.0
            for y in range(oy, oy + h):
                row = y * SOURCE_W
                for x in range(ox, ox + w):
                    if g["coverage"][row + x] < OWN:
                        continue
                    for sign in (1.0, -1.0):
                        ang = math.radians(sign * amplitude)
                        dx, dy = x - pivot_px, y - pivot_py
                        rx_ = (pivot_px + sx + dx * math.cos(ang)
                               - dy * math.sin(ang))
                        ry_ = (pivot_py + sy + dx * math.sin(ang)
                               + dy * math.cos(ang))
                        if not (ox <= rx_ < ox + w and oy <= ry_ < oy + h):
                            raise RuntimeError(
                                f"petal group {g['id']}: pixel ({x}, {y}) "
                                f"leaves its canvas under the "
                                f"{amplitude:+.1f} deg sweep")
                        if not outside_opening(rx_ + 0.5, ry_ + 0.5):
                            raise RuntimeError(
                                f"petal group {g['id']}: pixel ({x}, {y}) "
                                f"enters the client opening under the "
                                f"{amplitude:+.1f} deg sweep")
                        worst = max(worst, math.hypot(dx, dy))
            g["angle"] = outward_sign(pearl, g) * amplitude
            g["radius"] = worst
            report[g["id"]] = {"amplitude_deg": amplitude,
                               "signed_angle": g["angle"],
                               "max_radius_px": round(worst, 1)}
    return report


# ---------------------------------------------------------------- assembly

def build_exact(data: bytearray, coverage: bytearray) -> bytearray:
    """Raw sculpture atlas: source pixels, luminance matte times the parent
    selection mask, at the untouched source positions. Seats and moving
    pixels are baked afterwards (bake_stars / bake_corner_windows); opening
    safety is checked after ornament placement. Pearl bodies move in
    separate bubble textures.

    The luminance key alone would punch holes into dark recesses fully
    surrounded by material. Candidate holes (low keyed alpha inside the
    selection mask) are flooded from the canvas border; only border-reachable
    candidates stay transparent, enclosed recesses are restored to opaque
    material so the sculpture silhouette stays solid.
    """
    n = SOURCE_W * SOURCE_H
    keyed = bytearray(n)
    for i in range(n):
        x, y = i % SOURCE_W, i // SOURCE_W
        matte = smoothstep(MATTE_LO, MATTE_HI, luminance(data, x, y))
        keyed[i] = int(round(255.0 * matte * (coverage[i] / 255.0)))


    # border flood over transparent, mask-covered candidates
    visited = bytearray(n)
    queue: deque[int] = deque()
    border = (list(range(SOURCE_W))
              + list(range(n - SOURCE_W, n))
              + [y * SOURCE_W for y in range(SOURCE_H)]
              + [y * SOURCE_W + SOURCE_W - 1 for y in range(SOURCE_H)])
    for i in border:
        if keyed[i] < 128 and not visited[i]:
            visited[i] = 1
            queue.append(i)
    while queue:
        i = queue.popleft()
        x, y = i % SOURCE_W, i // SOURCE_W
        for j in ((i - 1) if x > 0 else -1,
                  (i + 1) if x < SOURCE_W - 1 else -1,
                  (i - SOURCE_W) if y > 0 else -1,
                  (i + SOURCE_W) if y < SOURCE_H - 1 else -1):
            if 0 <= j < n and not visited[j] and keyed[j] < 128:
                visited[j] = 1
                queue.append(j)

    exact = bytearray(data)
    for i in range(n):
        pearlm = 0.0
        for pearl in PEARLS.values():
            pearlm = max(pearlm, pearl_field(pearl, i % SOURCE_W,
                                             i // SOURCE_W))
        if keyed[i] >= 128:
            a = keyed[i] / 255.0
        elif visited[i]:
            a = keyed[i] / 255.0
        else:
            a = coverage[i] / 255.0  # enclosed recess: solid material
        exact[i * 4 + 3] = int(round(255.0 * clamp(a * (1.0 - pearlm), 0.0, 1.0)))
    return exact


def _center_seat(bbox, canvas_size: int) -> int:
    """Deterministic integer offset centering a coverage bounding box
    (canvas-local inclusive min/max) inside its canvas."""
    xmin, xmax = bbox
    return int(round((canvas_size - (xmax - xmin + 1)) / 2.0)) - xmin


def bake_stars(exact: bytearray, coverage: bytearray) -> dict:
    """Re-seat the four midpoint stars inside the plan's padded band canvases
    (offset [0,0] placement). The original window content is transferred
    whole -- never cropped -- and the pixel count is validated before and
    after, so a star can never be lost or duplicated."""
    report = {}
    for name, spec in STAR_SPECS.items():
        wx, wy, ww, wh = spec["window"]
        cx, cy, cw, ch = spec["canvas"]
        buffer = bytearray(ww * wh * 4)
        before = 0
        for y in range(wh):
            for x in range(ww):
                si = ((wy + y) * SOURCE_W + wx + x) * 4
                di = (y * ww + x) * 4
                buffer[di:di + 4] = exact[si:si + 4]
                if buffer[di + 3] >= OWN:
                    before += 1
        for y in range(wh):
            for x in range(ww):
                exact[((wy + y) * SOURCE_W + wx + x) * 4 + 3] = 0

        seat = spec["seat"]
        off_x, off_y = wx - cx, wy - cy  # window origin in canvas coordinates
        if seat == "center-y":
            # source x preserved; vertical bbox centering only
            owned_ys = [y for y in range(wh) for x in range(ww)
                        if coverage[(wy + y) * SOURCE_W + wx + x] >= OWN]
            if not owned_ys:
                raise RuntimeError(f"{name}: selection mask has no owned "
                                   f"pixels in the source window")
            dx = 0
            dy = (_center_seat((off_y + min(owned_ys),
                                off_y + max(owned_ys)), ch))
        else:
            dx, dy = seat
        after = 0
        for y in range(wh):
            for x in range(ww):
                # canvas-local destination of this window pixel
                lx, ly = off_x + x + dx, off_y + y + dy
                if not (0 <= lx < cw and 0 <= ly < ch):
                    if buffer[(y * ww + x) * 4 + 3] >= OWN:
                        raise RuntimeError(f"{name}: owned pixel would be "
                                           f"cropped by the baked seat")
                    continue
                si = (y * ww + x) * 4
                di = ((cy + ly) * SOURCE_W + cx + lx) * 4
                exact[di:di + 4] = buffer[si:si + 4]
                if buffer[si + 3] >= OWN:
                    after += 1
        if after != before:
            raise RuntimeError(f"{name}: star transfer lost pixels "
                               f"({before} -> {after})")
        report[name] = {"source_window": list(spec["window"]),
                        "canvas_rect": list(spec["canvas"]),
                        "seat": [dx, dy], "content_px": after}
    return report


def bake_corner_windows(exact: bytearray, petal_union: dict) -> dict:
    """Bake the outward corner seats into the exact-atlas corner windows and
    subtract the moving foliage, so the published atlas owns no stale static
    copy of any rocking leaf. Canvas rectangles stay unchanged."""
    report = {}
    for name, pearl in PEARLS.items():
        ox, oy, w, h = pearl["canvas"]
        sx, sy = CORNER_SEATS[name]
        union = petal_union[name]
        buffer = bytearray(w * h * 4)
        for y in range(h):
            si = ((oy + y) * SOURCE_W + ox) * 4
            buffer[y * w * 4:(y + 1) * w * 4] = exact[si:si + w * 4]
        for y in range(h):
            for x in range(w):
                exact[((oy + y) * SOURCE_W + ox + x) * 4 + 3] = 0
        lost = 0
        for y in range(h):
            for x in range(w):
                bx, by = x - sx, y - sy
                if not (0 <= bx < w and 0 <= by < h):
                    continue
                si = (by * w + bx) * 4
                di = ((oy + y) * SOURCE_W + ox + x) * 4
                exact[di:di + 4] = buffer[si:si + 4]
                # moving pixels never sit in the stationary atlas
                alpha = buffer[si + 3] / 255.0
                moving = union[(oy + y - sy) * SOURCE_W + (ox + x - sx)] / 255.0
                exact[di + 3] = int(round(255.0 * clamp(alpha * (1.0 - moving),
                                                        0.0, 1.0)))
        # any owned content pushed out of the window by the seat is a crop
        for y in range(h):
            for x in range(w):
                bx, by = x + sx, y + sy
                if 0 <= bx < w and 0 <= by < h:
                    continue
                if buffer[(y * w + x) * 4 + 3] >= OWN:
                    lost += 1
        if lost:
            raise RuntimeError(f"corner {name}: baked seat would crop "
                               f"{lost} owned atlas pixels")
        report[name] = {"canvas_rect": [ox, oy, w, h], "seat": [sx, sy]}
    return report


def build_corner_textures(data: bytearray, exact: bytearray,
                          sculpt: bytearray, name: str,
                          pearl: dict, groups: list[dict], tmp: Path) -> None:
    """Author background/foreground/bubble/petal rasters for one corner.

    The exact atlas corner window arrives already seated (outward shift) and
    with the moving foliage subtracted. Canvas pixel (rx, ry) displays
    source-space point (ox + rx - seat_x, oy + ry - seat_y); bubble, petals
    and foreground sample the raw source there, so every moving element
    shares the same baked seat as the static art.

    bubble: complete tilted body-ellipse mask (no leaf notch). Where the
    fixed foreground covers the body, the pearl sample is replaced by the
    pearl sample reflected across the body center x, crossfaded by the
    foreground coverage, so the shrinking pearl stays a full body while the
    fixed foreground keeps the original source pixels on top.
    background: seated static ornament (body and moving leaves subtracted)
    over the socket, composited with the over-operator.
    foreground: original source pixels under the parent-owned setting path
    plus a fixed collar disc around every petal pivot (root occlusion); the
    collar alpha is intersected with the extracted sculpture alpha so the
    disc never paints past the artwork silhouette while dark interior
    material stays opaque.
    petals: one full corner-sized RGBA texture per foliate group, alpha =
    authored coverage x extracted sculpture alpha x (1 - pearl body) -- the
    original matte, never the raw polygon, so no black patches appear.
    """
    ox, oy, w, h = pearl["canvas"]
    sx, sy = CORNER_SEATS[name]
    fg_path = render_gray(foreground_svg(name, pearl, (sx, sy)), w, h)
    fg_collar = render_gray(collar_svg(groups, (sx, sy), w, h), w, h)
    sock_rgba = render_rgba(socket_svg(name, pearl, (sx, sy)), w, h)
    bcx = pearl["body_center"][0]
    coverages = [g["coverage"] for g in groups]

    background = bytearray(w * h * 4)
    foreground = bytearray(w * h * 4)
    bubble = bytearray(w * h * 4)
    petals = [bytearray(w * h * 4) for _ in groups]
    for ry in range(h):
        for rx in range(w):
            # seated sample coordinates in the raw source
            x, y = ox + rx - sx, oy + ry - sy
            if not (0 <= x < SOURCE_W and 0 <= y < SOURCE_H):
                continue  # outside the master: canvases are padded, stay clear
            si = (y * SOURCE_W + x) * 4
            ei = ((oy + ry) * SOURCE_W + (ox + rx)) * 4
            di = (ry * w + rx) * 4
            ki = ry * w + rx
            # pre-baked sculpture alpha at the seated source position: the
            # original luminance matte, keeping dark interior material opaque
            sculpt_a = sculpt[si + 3] / 255.0

            src_a = exact[ei + 3] / 255.0
            sock_a = sock_rgba[di + 3] / 255.0
            bg_a = src_a * (1.0 - sock_a) + sock_a
            if bg_a > 0.0:
                wa = src_a * (1.0 - sock_a)
                for k in range(3):
                    value = exact[ei + k] * wa + sock_rgba[di + k] * sock_a
                    background[di + k] = int(round(value / bg_a))
                background[di + 3] = int(round(255.0 * bg_a))

            collar_a = (fg_collar[ki] / 255.0) * sculpt_a
            fg_a = max(fg_path[ki] / 255.0, collar_a)
            if fg_a > 0.0:
                for k in range(3):
                    foreground[di + k] = data[si + k]
                foreground[di + 3] = int(round(255.0 * fg_a))

            pm = pearl_field(pearl, x, y)
            if pm > 0.0:
                refl_x = min(max(int(round(2.0 * bcx - x)), 0), SOURCE_W - 1)
                ri = (y * SOURCE_W + refl_x) * 4
                for k in range(3):
                    value = data[si + k] * (1.0 - fg_a) + data[ri + k] * fg_a
                    bubble[di + k] = int(round(value))
                bubble[di + 3] = int(round(255.0 * pm))

            body_out = 1.0 - pm
            for cov, tex in zip(coverages, petals):
                pa = (cov[si // 4] / 255.0) * sculpt_a * body_out
                if pa > 0.0:
                    for k in range(3):
                        tex[di + k] = data[si + k]
                    tex[di + 3] = int(round(255.0 * pa))

    write_png(background, w, h, tmp / f"pearl-{name}-background.png")
    write_png(foreground, w, h, tmp / f"pearl-{name}-foreground.png")
    write_png(bubble, w, h, tmp / f"pearl-{name}-bubble.png")
    for index, tex in enumerate(petals):
        write_png(tex, w, h, tmp / f"pearl-{name}-petal-{index}.png")


def structural_regions() -> list[dict]:
    L, R, T, B = (APERTURE[k] for k in ("left", "right", "top", "bottom"))
    mid_h = SOURCE_H - T - B - 2 * COLUMN_CAP
    rx = SOURCE_W - R
    return [
        {"id": "corner-top-left", "atlas": "adaptive",
         "rect": [0, 0, L, T], "role": "corner-top-left", "anchor": "top-left",
         "offset": [0, 0], "repeat": "none", "z": 0},
        {"id": "corner-top-right", "atlas": "adaptive",
         "rect": [rx, 0, R, T], "role": "corner-top-right", "anchor": "top-right",
         "offset": [0, 0], "repeat": "none", "z": 0},
        {"id": "corner-bottom-left", "atlas": "adaptive",
         "rect": [0, SOURCE_H - B, L, B], "role": "corner-bottom-left",
         "anchor": "bottom-left", "offset": [0, 0], "repeat": "none", "z": 0},
        {"id": "corner-bottom-right", "atlas": "adaptive",
         "rect": [rx, SOURCE_H - B, R, B], "role": "corner-bottom-right",
         "anchor": "bottom-right", "offset": [0, 0], "repeat": "none", "z": 0},
        {"id": "rail-top", "atlas": "adaptive",
         "rect": [L, 0, SOURCE_W - L - R, T], "role": "rail-top",
         "anchor": "top-left", "offset": [0, 0], "repeat": "none", "z": 0},
        {"id": "rail-bottom", "atlas": "adaptive",
         "rect": [L, SOURCE_H - B, SOURCE_W - L - R, B], "role": "rail-bottom",
         "anchor": "top-left", "offset": [0, 0], "repeat": "none", "z": 0},
        {"id": "column-left-top", "atlas": "adaptive",
         "rect": [0, T, L, COLUMN_CAP], "role": "column-left-top",
         "anchor": "top-left", "offset": [0, 0], "repeat": "none", "z": 0},
        {"id": "column-left-middle", "atlas": "adaptive",
         "rect": [0, T + COLUMN_CAP, L, mid_h], "role": "column-left-middle",
         "anchor": "top-left", "offset": [0, 0], "repeat": "y", "z": 0},
        {"id": "column-left-bottom", "atlas": "adaptive",
         "rect": [0, SOURCE_H - B - COLUMN_CAP, L, COLUMN_CAP],
         "role": "column-left-bottom", "anchor": "bottom-left",
         "offset": [0, 0], "repeat": "none", "z": 0},
        {"id": "column-right-top", "atlas": "adaptive",
         "rect": [rx, T, R, COLUMN_CAP], "role": "column-right-top",
         "anchor": "top-right", "offset": [0, 0], "repeat": "none", "z": 0},
        {"id": "column-right-middle", "atlas": "adaptive",
         "rect": [rx, T + COLUMN_CAP, R, mid_h], "role": "column-right-middle",
         "anchor": "top-right", "offset": [0, 0], "repeat": "y", "z": 0},
        {"id": "column-right-bottom", "atlas": "adaptive",
         "rect": [rx, SOURCE_H - B - COLUMN_CAP, R, COLUMN_CAP],
         "role": "column-right-bottom", "anchor": "bottom-right",
         "offset": [0, 0], "repeat": "none", "z": 0},
    ]


def ornament_regions() -> list[dict]:
    # Offset repair: every region declares offset [0,0]; the outward seats
    # are baked into the canvases (STAR_SPECS / CORNER_SEATS), so authored
    # art and placement can never disagree again. Star canvases are the
    # plan's padded band rectangles.
    return [
        {"id": "pearl-tl", "atlas": "exact", "rect": [0, 0, 300, 450],
         "role": "ornament", "anchor": "top-left", "offset": [0, 0],
         "repeat": "none", "z": 10},
        {"id": "pearl-tr", "atlas": "exact", "rect": [1177, 0, 300, 450],
         "role": "ornament", "anchor": "top-right", "offset": [0, 0],
         "repeat": "none", "z": 10},
        {"id": "pearl-bl", "atlas": "exact", "rect": [0, 700, 300, 365],
         "role": "ornament", "anchor": "bottom-left", "offset": [0, 0],
         "repeat": "none", "z": 10},
        {"id": "pearl-br", "atlas": "exact", "rect": [1177, 700, 300, 365],
         "role": "ornament", "anchor": "bottom-right", "offset": [0, 0],
         "repeat": "none", "z": 10},
        {"id": "star-top", "atlas": "exact", "rect": [668, 0, 141, 175],
         "role": "ornament", "anchor": "top-center", "offset": [0, 0],
         "repeat": "none", "z": 10},
        {"id": "star-bottom", "atlas": "exact", "rect": [668, 867, 141, 198],
         "role": "ornament", "anchor": "bottom-center", "offset": [0, 0],
         "repeat": "none", "z": 10},
        {"id": "star-left", "atlas": "exact", "rect": [0, 460, 135, 128],
         "role": "ornament", "anchor": "center-left", "offset": [0, 0],
         "repeat": "none", "z": 10},
        {"id": "star-right", "atlas": "exact", "rect": [1341, 460, 136, 128],
         "role": "ornament", "anchor": "center-right", "offset": [0, 0],
         "repeat": "none", "z": 10},
    ]


def validate_placed_opening(exact: bytearray) -> None:
    """Reject actual painted intrusion after fixed ornament placement."""
    errors = []
    for region in ornament_regions():
        ox, oy, w, h = region["rect"]
        anchor = region["anchor"]
        x = (SOURCE_W - w) / 2 if "center" in anchor and anchor not in ("center-left", "center-right") else (
            SOURCE_W - w if anchor.endswith("right") else 0)
        y = (SOURCE_H - h) / 2 if anchor.startswith("center-") else (
            SOURCE_H - h if anchor.startswith("bottom-") else 0)
        x += region["offset"][0]
        y += region["offset"][1]
        count = 0
        for ry in range(h):
            for rx in range(w):
                if exact[((oy + ry) * SOURCE_W + ox + rx) * 4 + 3] and not outside_opening(x + rx + 0.5, y + ry + 0.5):
                    count += 1
        if count:
            errors.append(f"{region['id']}: {count}")
    if errors:
        raise RuntimeError("painted ornament intersects client opening after placement: " + ", ".join(errors))


def flower_effects_json(groups) -> list[dict]:
    effects = []
    for name in ("tl", "tr", "br", "bl"):
        pearl = PEARLS[name]
        ox, oy, _, _ = pearl["canvas"]
        cx, cy = pearl["center"]
        sx, sy = CORNER_SEATS[name]
        corner_groups = groups[name]
        n = len(corner_groups)
        petals = []
        for index, g in enumerate(corner_groups):
            petals.append({
                "texture": f"pearl-{name}-petal-{index}.png",
                "pivot": [round(g["pivot"][0] + sx - ox, 1),
                          round(g["pivot"][1] + sy - oy, 1)],
                "angle": g["angle"],
                "phase": round((pearl["phase"] + index / n) % 1.0, 3),
            })
        effects.append({
            "region": f"pearl-{name}",
            "background": f"pearl-{name}-background.png",
            "foreground": f"pearl-{name}-foreground.png",
            "period": PEARL_PERIOD,
            "phase": pearl["phase"],
            "petals": petals,
            "bubble": {
                "texture": f"pearl-{name}-bubble.png",
                "center": [round(cx + sx - ox, 1), round(cy + sy - oy, 1)],
                "radii": [float(pearl["radii"][0]), float(pearl["radii"][1])],
                "outward": [pearl["outward"][0], pearl["outward"][1]],
                "spread": PEARL_SPREAD,
            },
        })
    return effects


def skin_manifest(source_sha: str, groups) -> dict:
    return {
        "schema": 2,
        "id": PACK_ID,
        "name": PACK_NAME,
        "filter": "linear",
        "source": {"width": SOURCE_W, "height": SOURCE_H, "sha256": source_sha},
        "aperture": dict(APERTURE),
        "frame_insets": dict(APERTURE),
        "layered_ornaments": True,
        "exact": {"atlas": "exact.png", "aspect": EXACT_ASPECT,
                  "aspect_tolerance": EXACT_ASPECT_TOL,
                  "min_width": EXACT_MIN[0], "min_height": EXACT_MIN[1]},
        "adaptive": {"atlas": "adaptive.png", "scale": ADAPTIVE_SCALE,
                     "min_client_width": ADAPTIVE_MIN_CLIENT[0],
                     "min_client_height": ADAPTIVE_MIN_CLIENT[1]},
        "flower_effects": flower_effects_json(groups),
        "regions": structural_regions() + ornament_regions(),
    }


def provenance(source_path: Path, source_sha: str, mask_path: Path, mask_sha: str,
               petal_mask_path: Path, petal_mask_sha: str,
               lip: dict, edges: dict, pearl_report: dict,
               ownership_report: dict, groups, star_report: dict,
               corner_report: dict, partition_report: dict,
               sweep_report: dict) -> dict:
    pearls = {}
    for name, pearl in PEARLS.items():
        ox, oy, w, h = pearl["canvas"]
        cx, cy = pearl["center"]
        sx, sy = CORNER_SEATS[name]
        pearls[name] = {
            "source_center": [cx, cy],
            "published_radii": list(pearl["radii"]),
            "body_ellipse": {"center": list(pearl["body_center"]),
                             "radii": list(pearl["body_radii"]),
                             "angle_deg": pearl["body_angle"]},
            "region_local_center": [round(cx + sx - ox, 1),
                                    round(cy + sy - oy, 1)],
            "canvas_rect": [ox, oy, w, h],
            "baked_seat": [sx, sy],
            "outward": list(pearl["outward"]),
            "phase": pearl["phase"],
            "validation": pearl_report[name],
        }
    foliage = {}
    for name in PEARLS:
        ox, oy, _, _ = PEARLS[name]["canvas"]
        sx, sy = CORNER_SEATS[name]
        foliage[name] = []
        for index, g in enumerate(groups[name]):
            foliage[name].append({
                "id": g["id"],
                "description": g["desc"],
                "source_polygon": [list(p) for p in g["polygon"]],
                "source_pivot": list(g["pivot"]),
                "region_pivot": [round(g["pivot"][0] + sx - ox, 1),
                                 round(g["pivot"][1] + sy - oy, 1)],
                "texture": f"pearl-{name}-petal-{index}.png",
                "signed_angle_deg": g["angle"],
                "phase": round((PEARLS[name]["phase"]
                                + index / len(groups[name])) % 1.0, 3),
                "sweep": sweep_report[g["id"]],
            })
    return {
        "kind": "frieren-pearl-mixed-pack-provenance",
        "schema_version": 2,
        "target": "kitty-skins schema 2, layered_ornaments + flower_effects "
                  "mixed bubble and petals",
        "id": PACK_ID,
        "name": PACK_NAME,
        "generator": "tools/build-frieren-pearl.py",
        "source": {
            "file": str(source_path),
            "immutable": True,
            "width": SOURCE_W,
            "height": SOURCE_H,
            "sha256": source_sha,
            "expected_sha256": SOURCE_SHA,
            "note": "the master is never written; output is derivative art only; "
                    "source RGB is preserved -- no recolor, no desaturation, "
                    "no generated imagery",
        },
        "selection_mask": {
            "file": str(mask_path),
            "sha256": mask_sha,
            "expected_sha256": DEFAULT_MASK_SHA,
            "supersample": SUPERSCALE,
            "rasterization": "the mask SVG is rasterized by ImageMagick/librsvg "
                             "at 384 DPI (4x the source grid) and resized to the "
                             "source grid",
            "ownership": ownership_report,
            "method": "sculpture alpha = source alpha * luminance matte * "
                      "coverage / 255, pearl body footprints subtracted; dark "
                      "internal recesses stay opaque via the border flood; the "
                      "client opening is never cleared",
        },
        "petal_mask": {
            "file": str(petal_mask_path),
            "sha256": petal_mask_sha,
            "expected_sha256": DEFAULT_PETAL_MASK_SHA,
            "groups_per_corner": {n: len(groups[n]) for n in PEARLS},
            "partition": partition_report,
            "fixed_set": {
                "chain_corridors": CHAIN_CORRIDORS,
                "root_rects": ROOT_RECTS,
                "discs": FIXED_DISCS,
                "collar_radius_px": ROOT_COLLAR_RADIUS,
                "note": "hanging bead wire, beads, star pendant, red gems "
                        "(including the lower gems beside the bottom rails), "
                        "the BL/BR corner setting ball, the pendant pearl and "
                        "the fixed root leaf never move; the corridors keep a "
                        "few raster-inseparable leaf-fringe pixels fixed "
                        "beside the wire -- recorded, not silently deleted",
            },
            "foliage_groups": foliage,
        },
        "offset_repair": {
            "problem": "RegionSpec.offset was logical px while the builder "
                       "placed source px; a midpoint star landed inside the "
                       "protected client and was clipped",
            "fix": "every ornament region now declares offset [0,0]; the small "
                   "outward seats are baked into the canvases, pivots and "
                   "published centers so art and placement scale together",
            "corner_seats": CORNER_SEATS,
            "stars": star_report,
            "corner_atlas_bakes": corner_report,
        },
        "geometry": {
            "neutral_lip": dict(NEUTRAL_LIP),
            "neutral_lip_basis": f"painted inner lip measured on neutral lines "
                                 f"(median over three samples per side): {lip}; "
                                 f"accepted ranges {LIP_EXPECTED}",
            "aperture": dict(APERTURE),
            "frame_insets": dict(APERTURE),
            "frame_insets_basis": "equal to the measured source aperture; the "
                                  "band is rendered at physical scale 1, so the "
                                  "neutral structural cross-section and the "
                                  "original corner material share one physical "
                                  "profile (design intent; visual seam-free "
                                  "behaviour is unverified until rendered)",
            "burst_opening_overlap_risk": burst_opening_overlap(),
            "burst_opening_overlap_note": "measured rectangles where the full "
                                          "conservative burst bound "
                                          "(max(radii)+spread+6 square) crosses "
                                          "the real client opening; the runtime "
                                          "keeps the full circular bound and "
                                          "gates the rupture rim to the outward "
                                          "hemisphere, so this records the "
                                          "residual bound-level risk only",
            "pearls": pearls,
            "structural": {
                "profile_ranges": PROFILE_RANGE,
                "outer_visible_edges": edges,
                "construction": "bands are extrusions of their measured median "
                                "cross-sections; corners miter the two adjoining "
                                "profiles on the diagonal; alpha fades in at the "
                                "measured outer visible edge and out after the "
                                "measured neutral lip",
                "column_cap_px": COLUMN_CAP,
            },
            "exact": {"aspect": EXACT_ASPECT, "aspect_tolerance": EXACT_ASPECT_TOL,
                      "min": list(EXACT_MIN)},
            "adaptive": {"scale": ADAPTIVE_SCALE,
                         "min_client": list(ADAPTIVE_MIN_CLIENT)},
        },
        "authored": {
            "matte": {"lo": MATTE_LO, "hi": MATTE_HI,
                      "formula": "alpha = smoothstep(lo, hi, luminance) on the "
                                 "near-black studio background, times the "
                                 "selection mask; enclosed recesses restored "
                                 "opaque by border flood"},
            "socket": "locally authored recessed socket (dark radial pit plus a "
                      "quiet lower rim light), confined to the same tilted body "
                      "ellipse; no backing strip elsewhere; accepted burst "
                      "socket settings unchanged",
            "foreground": "parent-owned source-space setting paths clipped to "
                          "the body ellipse (TL silver leaf over the lower-left "
                          "edge, TR its mirror about x=737), plus a fixed "
                          "original-source collar disc around every petal "
                          "pivot; collars occlude the root joints so a rocking "
                          "leaf never floats off its stem",
            "bubble": "complete tilted body-ellipse alpha (no leaf notch); "
                      "original pearl pixels, crossfaded to the reflection "
                      "across the body center x wherever the fixed foreground "
                      "covers the body; accepted burst extraction unchanged, "
                      "only re-seated by the baked corner seat",
            "petals": "one full corner-sized RGBA texture per authored foliate "
                      "group: source RGB under alpha = polygon x ornament "
                      "selection x extracted sculpture matte, with the fixed "
                      "set (gems, ball, pendant, chain corridors, root leaf, "
                      "collars) and the pearl body subtracted; the stationary "
                      "background carries no moving pixel and no duplicate "
                      "silhouette",
            "mask_ownership": "every nonzero mask pixel is owned exactly once by "
                              "one disjoint ornament window; validated, never "
                              "assumed",
            "policy": "every recognisable form (corner clusters, chains, "
                      "midpoint stars, side stars) is one-shot in the exact "
                      "atlas through its own anchor; only profile-constant "
                      "neutral rails stretch",
        },
        "motion": {
            "period_s": PEARL_PERIOD,
            "period_note": "owner-accepted pearl growth/rupture/droplet "
                           "behaviour slowed 8 -> 32 s by the shared "
                           "spec.period; the runtime math is unchanged",
            "phases": {n: PEARLS[n]["phase"] for n in ("tl", "tr", "br", "bl")},
            "petals": "all foliate groups rock concurrently: signed 4-6 degree "
                      "cosine sway about the attachment-root pivot, phases "
                      "staggered across the groups on top of the corner's "
                      "pearl phase; the sway sign biases the initial "
                      "deflection outward (derived from each group's coverage "
                      "centroid and the corner outward vector)",
            "sweep_guard": sweep_report,
            "bubble_contract": "native runtime: growth 0..0.72, rupture rim "
                               ".72...78 gated to the outward hemisphere, ten "
                               "outward droplets .72...95, brief empty socket "
                               ".95..1; the region envelope keeps the full "
                               "circular conservative bound "
                               "max(radii)+spread+6, validated to fit each "
                               "region",
            "spread": PEARL_SPREAD,
        },
        "assembly": {
            "tool": "ImageMagick",
            "atlases": {
                "exact": "full source canvas carrying the selection-mask "
                         "sculpture with pearl bodies subtracted, midpoint "
                         "stars re-seated into the padded band canvases, "
                         "corner windows seated and moving foliage removed",
                "adaptive": "full source canvas carrying only the reconstructed "
                            "neutral structural silver",
            },
        },
        "acceptance": {
            "authored": True,
            "contract_validated": False,
            "rendered": "unverified",
            "visually_accepted": "unverified",
            "note": "no runtime, screenshot or acceptance claim is made by this "
                    "pack; source-aligned profiles and masks are authoring "
                    "intent, not visual proof",
        },
    }


KITTY_CONF = """\
# frieren-pearl-mixed frame pack: global application and kitty frame material.
# This file intentionally declares no Kitty colour keys; the active system
# theme include owns every colour.
"""


def build(source: Path, mask_path: Path, petal_mask_path: Path, output: Path,
          expect_mask_sha: str | None = None,
          expect_petal_mask_sha: str | None = None) -> dict:
    source_sha = sha256_file(source)
    if source_sha != SOURCE_SHA:
        raise RuntimeError(f"source sha mismatch: {source}")
    mask_sha = sha256_file(mask_path)
    if expect_mask_sha and mask_sha != expect_mask_sha:
        raise RuntimeError(f"selection mask sha mismatch: {mask_path}")
    petal_mask_sha = sha256_file(petal_mask_path)
    if expect_petal_mask_sha and petal_mask_sha != expect_petal_mask_sha:
        raise RuntimeError(f"petal selection mask sha mismatch: {petal_mask_path}")
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"refusing nonempty output directory: {output}")

    data = load_rgba(source, SOURCE_W, SOURCE_H)
    lip = measure_aperture(data)
    coverage = render_gray(mask_path.read_text(), SOURCE_W, SOURCE_H)

    groups, fixed = render_petal_masks(petal_mask_path.read_text(), coverage)
    partition_report = validate_petal_partition(coverage, groups, fixed)
    sweep_report = validate_petal_sweep(groups)

    profiles = {side: build_profile(data, side) for side in APERTURE}
    edges = {side: measure_outer_edge(data, side) for side in APERTURE}
    ownership_report = validate_mask_ownership(coverage)
    pearl_report = validate_pearls(data)

    petal_union = {
        corner: _union_coverage(groups[corner]) for corner in PEARLS
    }
    exact = build_exact(data, coverage)
    star_report = bake_stars(exact, coverage)
    # sculpture alpha snapshot BEFORE the corner seats/foliage subtraction:
    # the original matte drives petal and collar opacity
    sculpt = bytearray(exact)
    corner_report = bake_corner_windows(exact, petal_union)
    validate_placed_opening(exact)
    adaptive = structural_canvas(data, profiles, edges, lip)

    artifacts: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="frieren-pearl-") as tmp_s:
        tmp = Path(tmp_s)
        write_png(adaptive, SOURCE_W, SOURCE_H, tmp / "adaptive.png")
        write_png(exact, SOURCE_W, SOURCE_H, tmp / "exact.png")
        shutil.copyfile(source, tmp / "source.png")
        shutil.copyfile(mask_path, tmp / "frieren-pearl-ornaments.svg")
        shutil.copyfile(petal_mask_path, tmp / "frieren-pearl-petals.svg")
        for name in PEARLS:
            build_corner_textures(data, exact, sculpt, name, PEARLS[name],
                                  groups[name], tmp)

        (tmp / "skin.json").write_text(
            json.dumps(skin_manifest(source_sha, groups), indent=2) + "\n")
        (tmp / "kitty.conf").write_text(KITTY_CONF)

        output.mkdir(parents=True, exist_ok=True)
        for item in sorted(tmp.iterdir()):
            dest = output / item.name
            shutil.copyfile(item, dest)
            if item.suffix in (".png", ".json", ".svg", ".conf"):
                artifacts[dest.name] = {"sha256": sha256_file(dest)}

    prov = provenance(source_path=source, source_sha=source_sha,
                      mask_path=mask_path, mask_sha=mask_sha,
                      petal_mask_path=petal_mask_path,
                      petal_mask_sha=petal_mask_sha, lip=lip, edges=edges,
                      pearl_report=pearl_report,
                      ownership_report=ownership_report, groups=groups,
                      star_report=star_report, corner_report=corner_report,
                      partition_report=partition_report,
                      sweep_report=sweep_report)
    prov["artifacts"] = {
        name: info for name, info in sorted(artifacts.items())
        if name not in ("source.json",)
    }
    (output / "source.json").write_text(json.dumps(prov, indent=2) + "\n")
    return prov


def _union_coverage(corner_groups: list[dict]) -> bytearray:
    """Sum disjoint petal coverages, conserving shared antialiased edges.
    Drives stationary subtraction in atlas and corner rasters."""
    union = bytearray(SOURCE_W * SOURCE_H)
    for g in corner_groups:
        cov = g["coverage"]
        for i in range(SOURCE_W * SOURCE_H):
            union[i] = min(255, union[i] + cov[i])
    return union


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Author the frieren-pearl-mixed pack")
    parser.add_argument("--source", type=Path,
                        default=Path("assets/source/frieren-pearl-reference.png"))
    parser.add_argument("--mask", type=Path,
                        default=Path("assets/source/frieren-pearl-ornaments.svg"))
    parser.add_argument("--petal-mask", type=Path,
                        default=Path("assets/source/frieren-pearl-petals.svg"))
    parser.add_argument("--output", type=Path,
                        default=Path("assets/skins/frieren-pearl-mixed"))
    parser.add_argument("--expect-mask-sha", default=DEFAULT_MASK_SHA,
                        help="expected sha256 of the selection mask SVG")
    parser.add_argument("--expect-petal-mask-sha", default=DEFAULT_PETAL_MASK_SHA,
                        help="expected sha256 of the petal selection mask SVG")
    args = parser.parse_args(argv)
    try:
        build(args.source, args.mask, args.petal_mask, args.output,
              args.expect_mask_sha, args.expect_petal_mask_sha)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
