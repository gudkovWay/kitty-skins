#!/usr/bin/env python3
"""Author the Frieren Flower Relief Joined kitty-skins pack from the immutable master.

This is art extraction, not rectangle cropping. The pack is built from two
independent atlases:

* ``adaptive.png`` carries *only* the reconstructed structural silver: four
  longitudinally constant beveled rail profiles sampled from the neutral
  centrelines of the real master, mitered at the four corners and painted out to
  the physical ``frame_insets`` thickness by inverse-mapping the source band
  through the renderer's cross-scale. The base centre is transparent by
  construction; no ornament, pearl or leaf is copied into it.
* ``exact.png`` carries *only* the fixed ornaments: the four authored envelopes
  multiplied by the studio background matte and by the authored coverage mask
  (``assets/source/frieren-flower-ornaments.svg``, white sculpture / black
  discard), so it is the parent-authored sculpture silhouette, never the original
  artwork re-emitted as rectangles and never a colour-similarity guess at what
  is structural rail. The same covered alpha gates every flower raster, so no
  rail pixel can leak back through a petal, foreground or occluder copy.

The two flower clusters (TL, BR) are decomposed into region-sized
``background``/``foreground``/``petal`` rasters: the stationary repaired
ornament, the fixed opals/jewels plus small root collars that occlude the
motion, and one extracted leaf blade per authored selection polygon. Nothing is
inpainted over a whole removed petal; an exposed hole reveals the rebuilt base
or the intentional transparent exterior, and only a small disc around each
pivot is locally repaired.

The frame geometry is measured directly from the master (neutral centreline
rail profiles), never through a monolithic opening gate. ``frame_insets`` is
grown minimally so the protected opening stays disjoint from the real ornament
content and from the kinematic petal sweep. Outputs are deterministic; the
master is never modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from collections import deque
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- declared geometry -------------------------------------------------------

SOURCE_WIDTH = 1536
SOURCE_HEIGHT = 1024

EXPECTED_SOURCE_SHA256 = "2fa7f55394b067208ec72bb10dfbe6e050a433bb90a952774176685d04bfe3c1"

PACK_ID = "frieren-flower-relief-joined"
PACK_NAME = "Frieren Flower Relief Joined"
PACK_FILTER = "linear"

ADAPTIVE_SCALE = 0.40

EXACT_ASPECT = SOURCE_WIDTH / SOURCE_HEIGHT  # 1.5
EXACT_ASPECT_TOLERANCE = 0.06
EXACT_MIN_WIDTH = 900
EXACT_MIN_HEIGHT = 600

#: Owner-approved physical inset candidates, in source-pixel units. The builder
#: keeps them when the real ornament content and petal sweep fit and otherwise
#: grows the offending side minimally (see derive_frame_insets). No arbitrary
#: aspect-drift padding is applied: uniform min(sx, sy) ornament scaling can
#: never reach further than the reservation it is scaled into.
INSET_CANDIDATES = {"left": 240, "right": 220, "top": 210, "bottom": 210}

#: A pixel is painted material once its alpha reaches this value.
PAINT_ALPHA = 16

# --- semantic background matte ----------------------------------------------

#: The studio background is flat neutral gray. Pixels within LO of the measured
#: centre reference are background seeds; at HI and above they are material. In
#: between, alpha is feathered only where a pixel touches removed background, so
#: interior silver shading is never thinned.
MATTE_DIST_LO = 0.06
MATTE_DIST_HI = 0.20

#: Geodesic reach (px) of the enclosed-pocket cleanup: background seeds within
#: this many steps of the removed region go too, so thin gaps between leaves
#: clear while larger enclosed shading (part of the design) survives.
MATTE_POCKET_REACH = 6

MATTE_FORMULA = (
    "background seeds = normalized Euclidean color distance to the measured "
    f"centre reference <= {MATTE_DIST_LO}; removal = seeds reachable from the "
    "raster border (border flood) UNION the explicitly seeded enclosed centre "
    "opening (so the whole client opening clears, not only the exterior), plus "
    f"seeds within a geodesic reach of {MATTE_POCKET_REACH} px of the removed "
    "region. Material alpha is preserved; pixels within the LO..HI distance "
    "band adjacent to removed background get alpha * smoothstep(LO, HI, "
    "distance) as an anti-alias feather."
)

# --- structural base ---------------------------------------------------------

#: Longest run of unpainted cross-section pixels tolerated inside a measured
#: band before the band is considered to have ended.
GAP_ALLOW = 2

#: Column cap height (px) declared in the adaptive partition; the caps are
#: one-shot constant-profile pieces at each end of the repeating shaft.
COLUMN_CAP = 32

# --- authored ornament coverage ---------------------------------------------

#: The only authored selection of what is real sculpture: a 1536x1024 SVG the
#: parent drew over the master, white = silver sculpture to retain, black = old
#: structural rail or other background to discard. It is a fixed repository
#: asset, never user input, and is rasterized here instead of any heuristic.
ORNAMENT_MASK_REL = "assets/source/frieren-flower-ornaments.svg"
ORNAMENT_MASK_ASSET = Path(ORNAMENT_MASK_REL).name

#: Supersample factor and the ImageMagick density that renders the SVG at it
#: (SVG intrinsic units are pixels at the default 96 DPI).
ORNAMENT_COVERAGE_SUPERSAMPLE = 4
ORNAMENT_MASK_DENSITY_DPI = 96 * ORNAMENT_COVERAGE_SUPERSAMPLE

ORNAMENT_COVERAGE_FORMULA = (
    "render the authored mask SVG at "
    f"{ORNAMENT_MASK_DENSITY_DPI} DPI (exactly {ORNAMENT_COVERAGE_SUPERSAMPLE}x the "
    "source pixels), downsample to the source grid for antialiased 8-bit gray, "
    "then exact alpha = semantic matte alpha * coverage / 255, RGB preserved; "
    "outside the ornament envelopes alpha stays 0"
)

# --- ornaments ---------------------------------------------------------------

#: Extraction envelopes from the shared plan. Opacity inside them is the
#: semantic matte times the authored coverage, never the rectangle.
ORNAMENT_ENVELOPES = {
    "flower-tl": [0, 0, 500, 480],
    "ornament-tr": [1200, 0, 336, 430],
    "flower-bl": [0, 650, 360, 374],
    "flower-br": [1100, 580, 436, 444],
}

#: Static (non-flower) ornaments are ordinary one-shot regions; flower clusters
#: are composited by the flower effect instead.
FLOWER_REGIONS = ("flower-tl", "flower-br")

# --- authored petal motion ---------------------------------------------------

#: Each petal: complete exposed blade polygon in SOURCE coordinates, pivot in
#: REGION coordinates (subtract the envelope origin), signed rest angle in
#: degrees, and phase offset within its flower. These are the parent's explicit
#: art selections from the master; regions/pixels outside them stay stationary.
PETALS = {
    "flower-tl": [
        {
            "name": "top-left exposed leaf",
            "polygon": [
                (28, 2), (52, 9), (80, 23), (96, 47), (98, 63),
                (86, 81), (73, 87), (57, 62), (45, 37),
            ],
            "pivot": (88.0, 74.0),
            "angle": -10.0,
            "phase": 0.0,
        },
        {
            "name": "lower descending leaf",
            "polygon": [
                (130, 178), (157, 180), (190, 196), (214, 220), (215, 244),
                (200, 273), (187, 256), (171, 235), (139, 216),
            ],
            "pivot": (145.0, 190.0),
            "angle": -10.0,
            "phase": 0.18,
        },
    ],
    "flower-br": [
        {
            "name": "lower-left exposed blade",
            "polygon": [
                (1160, 982), (1181, 944), (1220, 922), (1252, 922), (1304, 931),
                (1360, 956), (1390, 967), (1334, 965), (1277, 952), (1230, 954),
                (1184, 966),
            ],
            "pivot": (270.0, 386.0),
            "angle": 6.0,
            "phase": 0.0,
        },
        {
            "name": "right exposed leaf",
            "polygon": [
                (1465, 922), (1461, 877), (1480, 842), (1520, 820),
                (1504, 858), (1510, 888), (1490, 919),
            ],
            "pivot": (370.0, 340.0),
            "angle": -9.0,
            "phase": 0.18,
        },
    ],
}

#: Authored fixed foreground occluders per flower, in SOURCE coordinates: the
#: opals, beads and red inlay that sit in front of the petal bases and must
#: occlude the motion. Everything else stays in the stationary background.
OCCLUDERS = {
    "flower-tl": [
        {"name": "central pearl opal", "kind": "disc", "center": (142, 118), "radius": 62},
        {"name": "stem bead pearl", "kind": "disc", "center": (295, 143), "radius": 11},
    ],
    "flower-br": [
        {"name": "teardrop pearl opal", "kind": "ellipse", "center": (1408, 868), "rx": 55, "ry": 66},
        {"name": "upper stem bead", "kind": "disc", "center": (1392, 752), "radius": 11},
        {"name": "left stem bead", "kind": "disc", "center": (1282, 857), "radius": 10},
        {"name": "red inlay jewel", "kind": "disc", "center": (1357, 910), "radius": 14},
    ],
}

#: A fixed collar of original source pixels is added to the foreground around
#: every pivot so a rotating blade never floats off its root.
ROOT_COLLAR_RADIUS = 12.0
#: Removed petal pixels within this radius of a pivot are locally reconstructed
#: from their stationary neighbours (a small root repair only, never a full
#: petal inpaint).
ROOT_REPAIR_RADIUS = 12.0

FLOWER_PERIOD = 8.0
FLOWER_PHASES = {"flower-tl": 0.0, "flower-br": 0.5}

#: Step (degrees) for sampling the continuous kinematic sweep bound.
SWEEP_STEP_DEG = 0.25
#: The authored pack maximum, derived from PETALS so the provenance can never
#: claim a bound that disagrees with the angles actually emitted.
MOTION_MAX_ABS_ANGLE = max(abs(petal["angle"]) for petals in PETALS.values() for petal in petals)

# --- outputs -----------------------------------------------------------------

PNG_FLAGS = [
    "-strip",
    "-depth", "8",
    "-define", "png:color-type=6",
    "-define", "png:exclude-chunk=date,time",
]


class BuildError(RuntimeError):
    """Actionable failure; reported without a traceback."""


# --- generic helpers ---------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def magick() -> str:
    found = shutil.which("magick")
    if found is None:
        raise BuildError("ImageMagick 'magick' was not found in PATH")
    return found


def run(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise BuildError(f"command failed ({result.returncode}): {' '.join(argv)}\n{detail}")
    return result.stdout


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def publish(src: Path, dest: Path) -> None:
    """Atomically publish one staged file into the output directory."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    handle, staged = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent)
    os.close(handle)
    staged_path = Path(staged)
    try:
        shutil.copy(src, staged_path)
        os.replace(staged_path, dest)
    except BaseException:
        staged_path.unlink(missing_ok=True)
        raise


# --- raster helpers ----------------------------------------------------------


def raw_rgba_size(width: int = SOURCE_WIDTH, height: int = SOURCE_HEIGHT) -> int:
    return width * height * 4


def export_rgba(image: Path, dest: Path, *, width: int = SOURCE_WIDTH, height: int = SOURCE_HEIGHT) -> bytearray:
    """Export one RGBA raster verbatim (same shape the backend uses)."""
    run([magick(), str(image), "-depth", "8", f"RGBA:{dest}"])
    try:
        data = bytearray(Path(dest).read_bytes())
    finally:
        Path(dest).unlink(missing_ok=True)
    expected = raw_rgba_size(width, height)
    if len(data) < expected:
        raise BuildError(f"the RGBA export of {image} is {len(data)} bytes, expected {expected}")
    return data


def write_rgba(data: bytes | bytearray, dest: Path, width: int, height: int, *, strip: bool) -> None:
    """Write raw interleaved RGBA back to PNG through ImageMagick."""
    raw = dest.with_name(f".{dest.name}.rgba")
    raw.write_bytes(bytes(data))
    argv = [magick(), "-size", f"{width}x{height}", "-depth", "8", f"RGBA:{raw}"]
    if strip:
        argv += PNG_FLAGS
    argv.append(f"PNG32:{dest}")
    try:
        run(argv)
    finally:
        raw.unlink(missing_ok=True)


def index(x: int, y: int, width: int = SOURCE_WIDTH) -> int:
    return (y * width + x) * 4


# --- semantic background matte -----------------------------------------------


def _distance(reference: tuple[int, int, int], r: int, g: int, b: int) -> float:
    dr, dg, db = (r - reference[0]) / 255.0, (g - reference[1]) / 255.0, (b - reference[2]) / 255.0
    return math.sqrt(dr * dr + dg * dg + db * db)


def _smoothstep(low: float, high: float, value: float) -> float:
    if high <= low:
        return 1.0 if value >= high else 0.0
    t = (value - low) / (high - low)
    t = 0.0 if t <= 0.0 else (1.0 if t >= 1.0 else t)
    return t * t * (3.0 - 2.0 * t)


def estimate_background(data: bytearray) -> tuple[int, int, int]:
    """The studio background colour: the per-channel median of the raster centre.

    The master's frame surrounds the flat client opening, whose centre is
    background by construction. Refuse a master whose centre is not flat.
    """
    cx, cy = SOURCE_WIDTH // 2, SOURCE_HEIGHT // 2
    samples: list[tuple[int, int, int]] = []
    for y in range(cy - 10, cy + 11):
        for x in range(cx - 10, cx + 11):
            offset = index(x, y)
            samples.append((data[offset], data[offset + 1], data[offset + 2]))
    median = tuple(  # type: ignore[assignment]
        sorted(sample[channel] for sample in samples)[len(samples) // 2] for channel in range(3)
    )
    close = sum(1 for sample in samples if _distance(median, *sample) <= MATTE_DIST_LO)
    if close < len(samples) // 2:
        raise BuildError(
            "cannot determine the studio background: the centre of the master is not flat "
            f"neutral gray (median {tuple(median)}, {close}/{len(samples)} pixels near it)"
        )
    return median  # type: ignore[return-value]


def apply_background_matte(data: bytearray, reference: tuple[int, int, int]) -> dict:
    """Remove the studio background before any structural base assembly.

    The exterior border flood alone cannot reach the enclosed client opening, so
    the centre is seeded explicitly as well; thin gaps between leaves clear
    through the bounded pocket reach.
    """
    width, height = SOURCE_WIDTH, SOURCE_HEIGHT
    total = width * height
    seeds = bytearray(total)
    distances = bytearray(total)
    for pixel in range(total):
        offset = pixel * 4
        distance = _distance(reference, data[offset], data[offset + 1], data[offset + 2])
        distances[pixel] = min(255, int(round(distance * 255.0)))
        if distance <= MATTE_DIST_LO:
            seeds[pixel] = 1

    removed = bytearray(total)
    queue: deque[int] = deque()

    def push(pixel: int) -> None:
        if seeds[pixel] and not removed[pixel]:
            removed[pixel] = 1
            queue.append(pixel)

    for x in range(width):
        push(x)
        push((height - 1) * width + x)
    for y in range(height):
        push(y * width)
        push(y * width + width - 1)

    # Seed the enclosed centre explicitly: the opening must clear even when the
    # exterior border flood can never reach it.
    centre_seeded = 0
    cx, cy = width // 2, height // 2
    for y in range(max(0, cy - 8), min(height, cy + 9)):
        for x in range(max(0, cx - 8), min(width, cx + 9)):
            pixel = y * width + x
            if seeds[pixel] and not removed[pixel]:
                removed[pixel] = 1
                queue.append(pixel)
                centre_seeded += 1
    if centre_seeded == 0:
        raise BuildError(
            "the centre of the master is not background: the measured reference does not "
            "describe this master, so the enclosed opening cannot be cleared safely"
        )

    while queue:
        pixel = queue.popleft()
        x, y = pixel % width, pixel // width
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < width and 0 <= ny < height:
                neighbour = ny * width + nx
                if seeds[neighbour] and not removed[neighbour]:
                    removed[neighbour] = 1
                    queue.append(neighbour)

    # Bounded geodesic dilation: thin enclosed gray pockets (gaps between
    # leaves) clear; larger enclosed shading deeper than the reach is kept.
    frontier = deque(pixel for pixel in range(total) if removed[pixel])
    for _ in range(MATTE_POCKET_REACH):
        grown: deque[int] = deque()
        while frontier:
            pixel = frontier.popleft()
            x, y = pixel % width, pixel // width
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < width and 0 <= ny < height:
                    neighbour = ny * width + nx
                    if seeds[neighbour] and not removed[neighbour]:
                        removed[neighbour] = 1
                        grown.append(neighbour)
        if not grown:
            break
        frontier = grown

    removed_count = sum(removed)
    if removed_count < total // 100:
        raise BuildError(
            "the semantic matte removed almost nothing "
            f"({removed_count} of {total} pixels); the centre reference "
            f"{reference} does not describe this master's background"
        )
    if removed_count > total * 9 // 10:
        raise BuildError(
            "the semantic matte removed nearly the whole master "
            f"({removed_count} of {total} pixels); refusing to strip the artwork"
        )

    feathered = 0
    for pixel in range(total):
        offset = pixel * 4
        if removed[pixel]:
            data[offset + 3] = 0
            continue
        alpha = data[offset + 3]
        if alpha == 0:
            continue
        distance = distances[pixel] / 255.0
        if distance >= MATTE_DIST_HI:
            continue
        x, y = pixel % width, pixel // width
        touches_removed = False
        for nx in (x - 1, x, x + 1):
            for ny in (y - 1, y, y + 1):
                if 0 <= nx < width and 0 <= ny < height and removed[ny * width + nx]:
                    touches_removed = True
                    break
            if touches_removed:
                break
        if touches_removed:
            data[offset + 3] = int(round(alpha * _smoothstep(MATTE_DIST_LO, MATTE_DIST_HI, distance)))
            feathered += 1

    return {
        "reference": list(reference),
        "dist_lo": MATTE_DIST_LO,
        "dist_hi": MATTE_DIST_HI,
        "pocket_reach_px": MATTE_POCKET_REACH,
        "centre_seeded_px": centre_seeded,
        "removed_px": removed_count,
        "feathered_px": feathered,
        "formula": MATTE_FORMULA,
    }


# --- measured neutral centrelines --------------------------------------------


def _side_scan(data: bytearray, side: str) -> tuple[list[int], "callable"]:
    """Alpha along the neutral centreline, from the outer edge inward, plus the
    coordinate callback that maps a scan index back to a source pixel."""
    width, height = SOURCE_WIDTH, SOURCE_HEIGHT
    if side == "top":
        const = width // 2
        values = [data[index(const, k) + 3] for k in range(height // 2)]
        return values, lambda k: (const, k)
    if side == "bottom":
        const = width // 2
        values = [data[index(const, height - 1 - k) + 3] for k in range(height // 2)]
        return values, lambda k: (const, height - 1 - k)
    if side == "left":
        const = height // 2
        values = [data[index(k, const) + 3] for k in range(width // 2)]
        return values, lambda k: (k, const)
    const = height // 2
    values = [data[index(width - 1 - k, const) + 3] for k in range(width // 2)]
    return values, lambda k: (width - 1 - k, const)


def _find_run(values: list[int]) -> tuple[int, int]:
    """Return (origin, end) of the first painted run, tolerating GAP_ALLOW px."""
    origin: int | None = None
    for position, alpha in enumerate(values):
        if alpha >= PAINT_ALPHA:
            origin = position
            break
    if origin is None:
        raise BuildError("a neutral centreline contains no painted material; the master is unusable")
    gap = 0
    last = origin
    for position in range(origin, len(values)):
        if values[position] >= PAINT_ALPHA:
            last = position
            gap = 0
        else:
            gap += 1
            if gap > GAP_ALLOW:
                break
    return origin, last + 1


def measure_bands(data: bytearray) -> dict[str, dict]:
    """Measure the aperture directly: first/last painted run on each half's
    neutral centreline (x=768 for top/bottom, y=512 for left/right)."""
    measured: dict[str, dict] = {}
    for side in ("left", "right", "top", "bottom"):
        values, coord = _side_scan(data, side)
        origin, end = _find_run(values)
        measured[side] = {"band": end, "origin": origin, "coord": coord}
    return measured


def build_profiles(data: bytearray, measured: dict[str, dict]) -> dict[str, list[tuple[int, int, int, int]]]:
    """Real painted metal cross-sections over [origin, band), unpainted samples
    carried forward, normalized to opaque material."""
    profiles: dict[str, list[tuple[int, int, int, int]]] = {}
    for side, info in measured.items():
        profile: list[tuple[int, int, int, int]] = []
        previous: tuple[int, int, int, int] | None = None
        for position in range(info["origin"], info["band"]):
            x, y = info["coord"](position)
            offset = index(x, y)
            rgba = (data[offset], data[offset + 1], data[offset + 2], data[offset + 3])
            if rgba[3] >= PAINT_ALPHA:
                previous = (rgba[0], rgba[1], rgba[2], 255)
            elif previous is None:
                previous = (rgba[0], rgba[1], rgba[2], 255)
            profile.append(previous)
        profiles[side] = profile
    return profiles


# --- structural base authoring ----------------------------------------------


def _profile_parameter(side_info: dict, target: int, cross_index: int) -> float | None:
    """Physical-distance inverse map of one atlas cross coordinate.

    Atlas coordinate ``i`` lands at physical distance ``d = i * target / band``
    once the renderer stretches the measured band to the reserved thickness.
    Below the original outer visible coordinate the base is transparent; above
    it the original painted cross-section is sampled linearly.
    """
    band = side_info["band"]
    origin = side_info["origin"]
    distance = cross_index * target / band
    if distance < origin:
        return None
    span = target - origin
    if span <= 0:
        return 1.0
    return (distance - origin) / span


def _profile_sample(profile: list[tuple[int, int, int, int]], parameter: float) -> tuple[int, int, int, int]:
    last = len(profile) - 1
    position = int(round(parameter * last))
    if position < 0:
        position = 0
    elif position > last:
        position = last
    sample = profile[position]
    return (sample[0], sample[1], sample[2], 255)


def author_structural_base(profiles: dict, measured: dict, insets: dict) -> bytearray:
    """Paint the reconstructed structural silver into a full-size RGBA canvas.

    Each side is a longitudinally constant extrusion of its real profile, and
    each corner is an ordinary miter: the pixel takes the profile of the side
    whose normalized thickness parameter is smaller. The centre stays
    transparent by construction.
    """
    width, height = SOURCE_WIDTH, SOURCE_HEIGHT
    out = bytearray(width * height * 4)
    left, right = measured["left"], measured["right"]
    top, bottom = measured["top"], measured["bottom"]

    def parameter(side: str, cross_index: int) -> float | None:
        return _profile_parameter(measured[side], insets[side], cross_index)

    for y in range(height):
        in_top = y < top["band"]
        in_bottom = y >= height - bottom["band"]
        for x in range(width):
            in_left = x < left["band"]
            in_right = x >= width - right["band"]
            if not (in_top or in_bottom or in_left or in_right):
                continue

            vertical: tuple[str, int] | None = None
            horizontal: tuple[str, int] | None = None
            if in_top:
                vertical = ("top", y)
            elif in_bottom:
                vertical = ("bottom", height - 1 - y)
            if in_left:
                horizontal = ("left", x)
            elif in_right:
                horizontal = ("right", width - 1 - x)

            if vertical is not None and horizontal is not None:
                tv = parameter(vertical[0], vertical[1])
                th = parameter(horizontal[0], horizontal[1])
                if tv is None or th is None:
                    continue
                side, cross = vertical if tv <= th else horizontal
            elif vertical is not None:
                side, cross = vertical
            else:
                assert horizontal is not None
                side, cross = horizontal

            t = parameter(side, cross)
            if t is None:
                continue
            rgba = _profile_sample(profiles[side], t)
            offset = (y * width + x) * 4
            out[offset] = rgba[0]
            out[offset + 1] = rgba[1]
            out[offset + 2] = rgba[2]
            out[offset + 3] = 255
    return out


# --- authored ornament coverage materialization ------------------------------


def rasterize_ornament_coverage(mask_path: Path, dest: Path) -> bytearray:
    """Rasterize the authored coverage mask to one 8-bit gray byte per pixel.

    The SVG is rendered at ORNAMENT_COVERAGE_SUPERSAMPLE times the source size
    and downsampled to the source grid, so coverage carries the authored
    antialiasing rather than a nearest sample. Dimensions and byte length are
    asserted; there is no fallback path.
    """
    high = dest.with_name(f".{dest.name}.{ORNAMENT_COVERAGE_SUPERSAMPLE}x.png")
    try:
        run([magick(), "-background", "black", "-density", str(ORNAMENT_MASK_DENSITY_DPI),
             str(mask_path), "-flatten", "-depth", "8", f"PNG24:{high}"])
        dims = run([magick(), "identify", "-format", "%w %h", str(high)]).strip()
        expected_dims = (f"{SOURCE_WIDTH * ORNAMENT_COVERAGE_SUPERSAMPLE} "
                         f"{SOURCE_HEIGHT * ORNAMENT_COVERAGE_SUPERSAMPLE}")
        if dims != expected_dims:
            raise BuildError(
                f"the authored coverage mask rendered at {dims}, expected {expected_dims} "
                f"({ORNAMENT_COVERAGE_SUPERSAMPLE}x the source); the mask SVG does not "
                f"declare {SOURCE_WIDTH}x{SOURCE_HEIGHT} pixel units"
            )
        run([magick(), str(high), "-resize", f"{SOURCE_WIDTH}x{SOURCE_HEIGHT}!",
             "-colorspace", "Gray", "-depth", "8", f"GRAY:{dest}"])
    finally:
        high.unlink(missing_ok=True)
    try:
        coverage = bytearray(Path(dest).read_bytes())
    finally:
        Path(dest).unlink(missing_ok=True)
    expected_bytes = SOURCE_WIDTH * SOURCE_HEIGHT
    if len(coverage) != expected_bytes:
        raise BuildError(
            f"the coverage raster is {len(coverage)} bytes, expected exactly "
            f"{expected_bytes} ({SOURCE_WIDTH}x{SOURCE_HEIGHT} 8-bit gray)"
        )
    return coverage


def apply_ornament_coverage(data: bytearray, coverage: bytearray) -> tuple[bytearray, dict]:
    """Build the exact ornament atlas from matte alpha times authored coverage.

    ``data`` is the background-matted master. Only the ornament envelopes are
    emitted; inside them RGB is preserved and alpha is ``matte_alpha * coverage
    / 255``, so everything the authored mask marks black never enters the pack.
    """
    if len(coverage) != SOURCE_WIDTH * SOURCE_HEIGHT:
        raise BuildError(
            f"coverage raster is {len(coverage)} bytes, expected {SOURCE_WIDTH * SOURCE_HEIGHT}"
        )
    exact = bytearray(SOURCE_WIDTH * SOURCE_HEIGHT * 4)
    retained = 0
    coverage_removed = 0
    envelope_px = 0
    for _region_id, (rx, ry, rw, rh) in ORNAMENT_ENVELOPES.items():
        for row in range(ry, ry + rh):
            for column in range(rx, rx + rw):
                offset = index(column, row)
                envelope_px += 1
                matte_alpha = data[offset + 3]
                if matte_alpha == 0:
                    continue
                cov = coverage[row * SOURCE_WIDTH + column]
                if cov == 0:
                    if matte_alpha >= PAINT_ALPHA:
                        coverage_removed += 1
                    continue
                alpha = matte_alpha * cov // 255
                if alpha == 0:
                    continue
                exact[offset] = data[offset]
                exact[offset + 1] = data[offset + 1]
                exact[offset + 2] = data[offset + 2]
                exact[offset + 3] = alpha
                if alpha >= PAINT_ALPHA:
                    retained += 1
    return exact, {
        "envelope_px": envelope_px,
        "retained_px": retained,
        "coverage_removed_px": coverage_removed,
    }


# --- petal motion and insets -------------------------------------------------


def point_in_polygon(x: float, y: float, polygon: list[tuple[int, int]]) -> bool:
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            x_cross = xi + (y - yi) * (xj - xi) / (yj - yi)
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def polygon_mask(width: int, height: int, polygon: list[tuple[int, int]], dilate: int = 0) -> bytearray:
    """Polygon interior, optionally dilated by `dilate` px (Chebyshev)."""
    xs = [point[0] for point in polygon]
    ys = [point[1] for point in polygon]
    x0 = max(0, min(xs) - dilate)
    x1 = min(width - 1, max(xs) + dilate)
    y0 = max(0, min(ys) - dilate)
    y1 = min(height - 1, max(ys) + dilate)
    mask = bytearray(width * height)
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            if point_in_polygon(x + 0.5, y + 0.5, polygon):
                mask[y * width + x] = 1
    for _ in range(dilate):
        grown: list[int] = []
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                pixel = y * width + x
                if mask[pixel]:
                    continue
                for nx in (x - 1, x, x + 1):
                    for ny in (y - 1, y, y + 1):
                        if 0 <= nx < width and 0 <= ny < height and mask[ny * width + nx]:
                            grown.append(pixel)
                            break
                    else:
                        continue
                    break
        for pixel in grown:
            mask[pixel] = 1
    return mask


def disc_mask(width: int, height: int, cx: float, cy: float, radius: float) -> bytearray:
    mask = bytearray(width * height)
    x0 = max(0, int(cx - radius) - 1)
    x1 = min(width - 1, int(cx + radius) + 1)
    y0 = max(0, int(cy - radius) - 1)
    y1 = min(height - 1, int(cy + radius) + 1)
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            dx, dy = x - cx, y - cy
            if dx * dx + dy * dy <= radius * radius:
                mask[y * width + x] = 1
    return mask


def occluder_mask(width: int, height: int, occluder: dict, ox: int, oy: int) -> bytearray:
    mask = bytearray(width * height)
    cx, cy = occluder["center"][0] - ox, occluder["center"][1] - oy
    if occluder["kind"] == "disc":
        rx = ry = float(occluder["radius"])
    else:
        rx, ry = float(occluder["rx"]), float(occluder["ry"])
    for y in range(max(0, int(cy - ry) - 1), min(height, int(cy + ry) + 2)):
        for x in range(max(0, int(cx - rx) - 1), min(width, int(cx + rx) + 2)):
            dx = (x - cx) / (rx + 0.5)
            dy = (y - cy) / (ry + 0.5)
            if dx * dx + dy * dy <= 1.0:
                mask[y * width + x] = 1
    return mask


def sweep_bounds(polygon: list[tuple[int, int]], pivot: tuple[float, float], angle_deg: float,
                 ox: int, oy: int) -> list[int]:
    """Continuous kinematic sweep bound in unclamped SOURCE coordinates.

    The runtime applies ``angle * 0.5 * (1 - cos(...))``, so the applied angle
    stays on the signed interval between zero and the authored angle; the bound
    unions that whole continuous interval, not merely the two endpoints.
    """
    low = min(0.0, math.radians(angle_deg))
    high = max(0.0, math.radians(angle_deg))
    steps = max(2, int(math.ceil(abs(angle_deg) / SWEEP_STEP_DEG)) + 1)
    xs: list[float] = []
    ys: list[float] = []
    for step in range(steps + 1):
        theta = low + (high - low) * step / steps
        cos, sin = math.cos(theta), math.sin(theta)
        for x, y in polygon:
            dx, dy = x - ox - pivot[0], y - oy - pivot[1]
            xs.append(ox + pivot[0] + dx * cos - dy * sin)
            ys.append(oy + pivot[1] + dx * sin + dy * cos)
    return [math.floor(min(xs)), math.floor(min(ys)), math.ceil(max(xs)), math.ceil(max(ys))]


def build_content_masks(exact: bytearray, sweeps: dict[str, list[list[int]]]) -> dict[str, bytearray]:
    """Per-envelope content = ornament matte UNION petal sweep, clamped."""
    masks: dict[str, bytearray] = {}
    for region_id, (rx, ry, rw, rh) in ORNAMENT_ENVELOPES.items():
        mask = bytearray(rw * rh)
        for row in range(rh):
            start = index(rx, ry + row)
            for column in range(rw):
                if exact[start + column * 4 + 3] >= PAINT_ALPHA:
                    mask[row * rw + column] = 1
        for bbox in sweeps.get(region_id, []):
            x0 = max(rx, bbox[0])
            x1 = min(rx + rw - 1, bbox[2])
            y0 = max(ry, bbox[1])
            y1 = min(ry + rh - 1, bbox[3])
            for y in range(y0, y1 + 1):
                for x in range(x0, x1 + 1):
                    mask[(y - ry) * rw + (x - rx)] = 1
        masks[region_id] = mask
    return masks


def _opening_violation(content: bytearray, rw: int, rh: int, origin: tuple[int, int],
                       insets: dict, region_id: str) -> dict | None:
    """Minimal growth targets when content intrudes into the protected opening."""
    width, height = SOURCE_WIDTH, SOURCE_HEIGHT
    open_x0, open_x1 = insets["left"], width - 1 - insets["right"]
    open_y0, open_y1 = insets["top"], height - 1 - insets["bottom"]
    ox, oy = origin
    left_facing = region_id in ("flower-tl", "flower-bl")
    top_facing = region_id in ("flower-tl", "ornament-tr")

    extreme_x: int | None = None
    extreme_y: int | None = None
    for row in range(rh):
        y = oy + row
        y_open = open_y0 <= y <= open_y1
        for column in range(rw):
            if not content[row * rw + column]:
                continue
            x = ox + column
            if not (open_x0 <= x <= open_x1):
                continue
            if y_open and (extreme_x is None or (x > extreme_x if left_facing else x < extreme_x)):
                extreme_x = x
            if open_y0 <= y <= open_y1 and (extreme_y is None or (y > extreme_y if top_facing else y < extreme_y)):
                extreme_y = y

    if extreme_x is None or extreme_y is None:
        return None
    horizontal_need = extreme_x + 1 if left_facing else width - extreme_x
    vertical_need = extreme_y + 1 if top_facing else height - extreme_y
    return {
        "horizontal": {"side": "left" if left_facing else "right", "target": horizontal_need},
        "vertical": {"side": "top" if top_facing else "bottom", "target": vertical_need},
    }


def derive_frame_insets(content: dict[str, bytearray], bands: dict,
                        sweeps: dict[str, list[list[int]]]) -> dict:
    """Keep the approved candidates; grow minimally until the protected opening
    is disjoint from the real ornament content and the petal sweep.

    No aspect-drift padding is applied: the uniform ``min(sx, sy)`` ornament
    scale cannot reach further than the reservation it is placed into, so the
    candidate/grown insets are exact. Meaningful art that would leave the canvas
    or its envelope is a hard failure, never silently clamped.
    """
    insets = {side: max(INSET_CANDIDATES[side], bands[side])
              for side in ("left", "right", "top", "bottom")}
    log: list[dict] = [{"stage": "candidates-and-measured-bands", "insets": dict(insets)}]

    for _ in range(64):
        found: tuple[str, dict] | None = None
        for region_id, (rx, ry, rw, rh) in ORNAMENT_ENVELOPES.items():
            violation = _opening_violation(content[region_id], rw, rh, (rx, ry), insets, region_id)
            if violation is not None:
                found = (region_id, violation)
                break
        if found is None:
            break
        region_id, violation = found
        horizontal, vertical = violation["horizontal"], violation["vertical"]
        horizontal_cost = abs(horizontal["target"] - insets[horizontal["side"]])
        vertical_cost = abs(vertical["target"] - insets[vertical["side"]])
        choice = horizontal if horizontal_cost <= vertical_cost else vertical
        if choice["target"] <= insets[choice["side"]]:
            raise BuildError(f"cannot clear {region_id} from the protected opening: {insets}")
        insets[choice["side"]] = choice["target"]
        log.append({
            "stage": "grow",
            "corner": region_id,
            "grew": choice["side"],
            "to": choice["target"],
            "reason": "protected opening intersected ornament or swept petal content",
        })
    else:
        raise BuildError("frame inset derivation did not converge")

    for region_id, (rx, ry, rw, rh) in ORNAMENT_ENVELOPES.items():
        for bbox in sweeps.get(region_id, []):
            if bbox[0] < 0 or bbox[1] < 0 or bbox[2] >= SOURCE_WIDTH or bbox[3] >= SOURCE_HEIGHT:
                raise BuildError(f"{region_id} petal sweep {bbox} exceeds the source canvas")
            if bbox[0] < rx or bbox[1] < ry or bbox[2] >= rx + rw or bbox[3] >= ry + rh:
                raise BuildError(f"{region_id} petal sweep {bbox} exceeds its ornament envelope")

    opening_w = SOURCE_WIDTH - insets["left"] - insets["right"]
    opening_h = SOURCE_HEIGHT - insets["top"] - insets["bottom"]
    if opening_w < 560 or opening_h < 360:
        raise BuildError(
            f"derived frame insets {insets} leave only a {opening_w}x{opening_h} source opening; "
            "the ornaments and the declared client minima cannot both fit this master"
        )
    return {"insets": insets, "log": log, "opening": {"width": opening_w, "height": opening_h}}


# --- flower rasters ----------------------------------------------------------


def repair_holes(region: bytearray, width: int, height: int, hole: bytearray, zone: bytearray) -> dict:
    """Locally reconstruct hole pixels inside `zone` from painted neighbours.

    Only pixels that actually have painted neighbours are filled; anything over
    former transparent exterior stays transparent (no invented flat backing).
    """
    known = bytearray(width * height)
    for pixel in range(width * height):
        if not hole[pixel]:
            known[pixel] = 1

    queue: deque[int] = deque()
    for pixel in range(width * height):
        if not (hole[pixel] and zone[pixel]):
            continue
        x, y = pixel % width, pixel // width
        for nx in (x - 1, x, x + 1):
            for ny in (y - 1, y, y + 1):
                if 0 <= nx < width and 0 <= ny < height and known[ny * width + nx]:
                    queue.append(pixel)
                    break
            else:
                continue
            break

    filled = 0
    while queue:
        pixel = queue.popleft()
        if known[pixel]:
            continue
        x, y = pixel % width, pixel // width
        sums = [0, 0, 0, 0]
        for nx in (x - 1, x, x + 1):
            for ny in (y - 1, y, y + 1):
                if not (0 <= nx < width and 0 <= ny < height):
                    continue
                neighbour = ny * width + nx
                if not known[neighbour]:
                    continue
                offset = neighbour * 4
                alpha = region[offset + 3]
                if alpha == 0:
                    continue
                for channel in range(3):
                    sums[channel] += region[offset + channel] * alpha
                sums[3] += alpha
        if sums[3] == 0:
            continue
        offset = pixel * 4
        for channel in range(3):
            region[offset + channel] = min(255, sums[channel] // sums[3])
        region[offset + 3] = 255
        known[pixel] = 1
        filled += 1
        for nx in (x - 1, x, x + 1):
            for ny in (y - 1, y, y + 1):
                if 0 <= nx < width and 0 <= ny < height:
                    neighbour = ny * width + nx
                    if hole[neighbour] and zone[neighbour] and not known[neighbour]:
                        queue.append(neighbour)

    zone_holes = sum(1 for pixel in range(width * height) if hole[pixel] and zone[pixel])
    return {"zone_px": sum(zone), "hole_in_zone_px": zone_holes, "filled_px": filled}


def build_flower_rasters(data: bytearray, exact: bytearray) -> tuple[dict, dict]:
    """Produce background/foreground/petal rasters and their provenance.

    Background: the covered ornament atlas with every authored petal removed;
    only a small disc around each pivot is locally repaired, so an exposed hole
    shows the rebuilt base or the intentional transparent exterior rather than a
    translucent ghost of the whole blade. Foreground: the fixed opals/jewels plus
    a small original-source root collar per pivot, source RGB under the covered
    alpha. Petals: one complete exposed blade each, source RGB under the covered
    alpha, region-sized with transparent surroundings. The covered alpha is the
    single selector, so no heuristic rail pixel can return through these layers.
    """
    effects: dict[str, tuple[bytearray, int, int]] = {}
    provenance: dict[str, dict] = {}

    for region_id in FLOWER_REGIONS:
        rx, ry, rw, rh = ORNAMENT_ENVELOPES[region_id]
        petals = PETALS[region_id]

        occluder_combined = bytearray(rw * rh)
        occluder_parts = []
        for occluder in OCCLUDERS[region_id]:
            mask = occluder_mask(rw, rh, occluder, rx, ry)
            occluder_parts.append({
                "name": occluder["name"],
                "kind": occluder["kind"],
                "center_region": [occluder["center"][0] - rx, occluder["center"][1] - ry],
                "px": sum(mask),
            })
            for pixel in range(rw * rh):
                if mask[pixel]:
                    occluder_combined[pixel] = 1

        collar_parts = []
        for petal in petals:
            px, py = petal["pivot"]
            mask = disc_mask(rw, rh, px, py, ROOT_COLLAR_RADIUS)
            collar_parts.append({"center_region": [px, py], "radius": ROOT_COLLAR_RADIUS, "px": sum(mask)})
            for pixel in range(rw * rh):
                if mask[pixel]:
                    occluder_combined[pixel] = 1

        petal_masks: list[bytearray] = []
        petal_parts = []
        for petal_index, petal in enumerate(petals):
            polygon_region = [(x - rx, y - ry) for x, y in petal["polygon"]]
            sweep_source = sweep_bounds(petal["polygon"], petal["pivot"], petal["angle"], rx, ry)
            mask = polygon_mask(rw, rh, polygon_region)
            painted = 0
            occluder_subtracted = 0
            for pixel in range(rw * rh):
                if not mask[pixel]:
                    continue
                x = rx + pixel % rw
                y = ry + pixel // rw
                if exact[index(x, y) + 3] < PAINT_ALPHA:
                    mask[pixel] = 0
                    continue
                if occluder_combined[pixel]:
                    mask[pixel] = 0
                    occluder_subtracted += 1
                    continue
                painted += 1
            if painted < 200:
                raise BuildError(
                    f"{region_id} petal {petal_index} ({petal['name']}) resolved to only {painted} painted px; "
                    "the authored polygon does not sit on a leaf in this master"
                )
            for other in petal_masks:
                for pixel in range(rw * rh):
                    if mask[pixel] and other[pixel]:
                        raise BuildError(
                            f"{region_id} petal {petal_index} ({petal['name']}) overlaps another moving petal; "
                            "moving masks must be disjoint"
                        )
            petal_masks.append(mask)
            petal_parts.append({
                "name": petal["name"],
                "polygon_region": [list(point) for point in polygon_region],
                "pivot_region": list(petal["pivot"]),
                "angle_deg": petal["angle"],
                "phase": petal["phase"],
                "mask_px": painted,
                "occluder_subtracted_px": occluder_subtracted,
                "sweep_bbox_region": [
                    value - (rx if axis in (0, 2) else ry)
                    for axis, value in enumerate(sweep_source)
                ],
                "sweep_bbox_source": sweep_source,
                "max_abs_angle_authored": MOTION_MAX_ABS_ANGLE,
            })

        for petal_index, mask in enumerate(petal_masks):
            canvas = bytearray(rw * rh * 4)
            for pixel in range(rw * rh):
                if mask[pixel]:
                    offset = index(rx + pixel % rw, ry + pixel // rw)
                    canvas[pixel * 4] = data[offset]
                    canvas[pixel * 4 + 1] = data[offset + 1]
                    canvas[pixel * 4 + 2] = data[offset + 2]
                    canvas[pixel * 4 + 3] = exact[offset + 3]
            effects[f"{region_id}-petal-{petal_index}.png"] = (canvas, rw, rh)

        # Stationary background: the ornament matte minus the moving petals.
        background = bytearray(rw * rh * 4)
        for row in range(rh):
            start = index(rx, ry + row)
            background[row * rw * 4:(row + 1) * rw * 4] = exact[start:start + rw * 4]
        hole = bytearray(rw * rh)
        for mask in petal_masks:
            for pixel in range(rw * rh):
                if mask[pixel]:
                    offset = pixel * 4
                    background[offset:offset + 4] = b"\0\0\0\0"
                    hole[pixel] = 1

        zone = bytearray(rw * rh)
        for petal in petals:
            mask = disc_mask(rw, rh, petal["pivot"][0], petal["pivot"][1], ROOT_REPAIR_RADIUS)
            for pixel in range(rw * rh):
                if mask[pixel]:
                    zone[pixel] = 1
        repair = repair_holes(background, rw, rh, hole, zone)

        # Fixed foreground: occluders plus the root collars, source RGB under
        # the covered alpha so no original rail pixel rides in front.
        foreground = bytearray(rw * rh * 4)
        for pixel in range(rw * rh):
            if occluder_combined[pixel]:
                offset = index(rx + pixel % rw, ry + pixel // rw)
                foreground[pixel * 4] = data[offset]
                foreground[pixel * 4 + 1] = data[offset + 1]
                foreground[pixel * 4 + 2] = data[offset + 2]
                foreground[pixel * 4 + 3] = exact[offset + 3]

        effects[f"{region_id}-background.png"] = (background, rw, rh)
        effects[f"{region_id}-foreground.png"] = (foreground, rw, rh)
        provenance[region_id] = {
            "region_rect": ORNAMENT_ENVELOPES[region_id],
            "petals": petal_parts,
            "occluders": occluder_parts,
            "root_collars": collar_parts,
            "root_repair": repair,
            "policy": (
                "background = covered ornament atlas minus the moving blades, with only a "
                "small disc around each pivot locally repaired; foreground = fixed "
                "opals/jewels plus a small original-source root collar per pivot, source RGB "
                "under the covered alpha; petal masks are the authored polygon intersected "
                "with the covered alpha and with every occluder/collar subtracted, so no "
                "moving copy of a fixed opal exists and the masks are disjoint"
            ),
        }

    return effects, provenance


# --- manifest -----------------------------------------------------------------


def structural_regions(bands: dict) -> list[dict]:
    width, height = SOURCE_WIDTH, SOURCE_HEIGHT
    left, right, top, bottom = bands["left"], bands["right"], bands["top"], bands["bottom"]
    cap = COLUMN_CAP
    shaft = height - top - bottom - 2 * cap
    if shaft <= 0:
        raise BuildError(f"measured bands {bands} leave no room for the {cap} px column caps")
    return [
        {"id": "corner-top-left", "atlas": "adaptive", "rect": [0, 0, left, top],
         "role": "corner-top-left", "anchor": "top-left", "offset": [0, 0], "repeat": "none", "z": 0},
        {"id": "corner-top-right", "atlas": "adaptive", "rect": [width - right, 0, right, top],
         "role": "corner-top-right", "anchor": "top-right", "offset": [0, 0], "repeat": "none", "z": 0},
        {"id": "corner-bottom-left", "atlas": "adaptive", "rect": [0, height - bottom, left, bottom],
         "role": "corner-bottom-left", "anchor": "bottom-left", "offset": [0, 0], "repeat": "none", "z": 0},
        {"id": "corner-bottom-right", "atlas": "adaptive", "rect": [width - right, height - bottom, right, bottom],
         "role": "corner-bottom-right", "anchor": "bottom-right", "offset": [0, 0], "repeat": "none", "z": 0},
        {"id": "rail-top", "atlas": "adaptive", "rect": [left, 0, width - left - right, top],
         "role": "rail-top", "anchor": "top-left", "offset": [0, 0], "repeat": "none", "z": 0},
        {"id": "rail-bottom", "atlas": "adaptive", "rect": [left, height - bottom, width - left - right, bottom],
         "role": "rail-bottom", "anchor": "top-left", "offset": [0, 0], "repeat": "none", "z": 0},
        {"id": "column-left-top", "atlas": "adaptive", "rect": [0, top, left, cap],
         "role": "column-left-top", "anchor": "top-left", "offset": [0, 0], "repeat": "none", "z": 0},
        {"id": "column-left-middle", "atlas": "adaptive", "rect": [0, top + cap, left, shaft],
         "role": "column-left-middle", "anchor": "top-left", "offset": [0, 0], "repeat": "y", "z": 0},
        {"id": "column-left-bottom", "atlas": "adaptive", "rect": [0, height - bottom - cap, left, cap],
         "role": "column-left-bottom", "anchor": "bottom-left", "offset": [0, 0], "repeat": "none", "z": 0},
        {"id": "column-right-top", "atlas": "adaptive", "rect": [width - right, top, right, cap],
         "role": "column-right-top", "anchor": "top-right", "offset": [0, 0], "repeat": "none", "z": 0},
        {"id": "column-right-middle", "atlas": "adaptive", "rect": [width - right, top + cap, right, shaft],
         "role": "column-right-middle", "anchor": "top-right", "offset": [0, 0], "repeat": "y", "z": 0},
        {"id": "column-right-bottom", "atlas": "adaptive", "rect": [width - right, height - bottom - cap, right, cap],
         "role": "column-right-bottom", "anchor": "bottom-right", "offset": [0, 0], "repeat": "none", "z": 0},
    ]


def ornament_regions() -> list[dict]:
    anchors = {
        "flower-tl": "top-left",
        "ornament-tr": "top-right",
        "flower-bl": "bottom-left",
        "flower-br": "bottom-right",
    }
    return [
        {
            "id": region_id,
            "atlas": "exact",
            "rect": ORNAMENT_ENVELOPES[region_id],
            "role": "ornament",
            "anchor": anchors[region_id],
            "offset": [0, 0],
            "repeat": "none",
            "z": 10,
        }
        for region_id in ORNAMENT_ENVELOPES
    ]


def flower_effects_json() -> list[dict]:
    effects: list[dict] = []
    for region_id in FLOWER_REGIONS:
        effects.append({
            "region": region_id,
            "background": f"{region_id}-background.png",
            "foreground": f"{region_id}-foreground.png",
            "period": FLOWER_PERIOD,
            "phase": FLOWER_PHASES[region_id],
            "petals": [
                {
                    "texture": f"{region_id}-petal-{index}.png",
                    "pivot": [petal["pivot"][0], petal["pivot"][1]],
                    "angle": petal["angle"],
                    "phase": petal["phase"],
                }
                for index, petal in enumerate(PETALS[region_id])
            ],
        })
    return effects


def client_minima(bands: dict) -> tuple[float, float]:
    """Fixed one-shot pieces plus one full neutral cell per axis, at the floor.

    The rails and shafts are longitudinally constant, so one full neutral span
    per axis is the largest cell the frame needs to place before the fixed
    caps; the historical 560x360 floor is never undercut.
    """
    min_w = max(560.0, float(math.ceil(ADAPTIVE_SCALE * (SOURCE_WIDTH - bands["left"] - bands["right"]))))
    min_h = max(360.0, float(math.ceil(ADAPTIVE_SCALE * (SOURCE_HEIGHT - bands["top"] - bands["bottom"]))))
    return min_w, min_h


def skin_manifest(source_sha: str, bands: dict, regions: list[dict],
                  min_client_width: float, min_client_height: float, frame_insets: dict) -> dict:
    return {
        "schema": 2,
        "id": PACK_ID,
        "name": PACK_NAME,
        "filter": PACK_FILTER,
        "source": {"width": SOURCE_WIDTH, "height": SOURCE_HEIGHT, "sha256": source_sha},
        "aperture": {
            "left": bands["left"],
            "right": bands["right"],
            "top": bands["top"],
            "bottom": bands["bottom"],
        },
        "frame_insets": {
            "left": frame_insets["left"],
            "right": frame_insets["right"],
            "top": frame_insets["top"],
            "bottom": frame_insets["bottom"],
        },
        "layered_ornaments": True,
        "exact": {
            "atlas": "exact.png",
            "aspect": round(EXACT_ASPECT, 6),
            "aspect_tolerance": EXACT_ASPECT_TOLERANCE,
            "min_width": EXACT_MIN_WIDTH,
            "min_height": EXACT_MIN_HEIGHT,
        },
        "adaptive": {
            "atlas": "adaptive.png",
            "scale": ADAPTIVE_SCALE,
            "min_client_width": min_client_width,
            "min_client_height": min_client_height,
        },
        "flower_effects": flower_effects_json(),
        "regions": regions,
    }


# --- provenance ---------------------------------------------------------------


def content_bbox(mask: bytearray, rw: int, rh: int, origin: tuple[int, int]) -> dict | None:
    ox, oy = origin
    min_x, min_y, max_x, max_y = rw, rh, -1, -1
    count = 0
    for row in range(rh):
        for column in range(rw):
            if mask[row * rw + column]:
                count += 1
                min_x = min(min_x, column)
                max_x = max(max_x, column)
                min_y = min(min_y, row)
                max_y = max(max_y, row)
    if max_x < 0:
        return None
    return {
        "bbox": [ox + min_x, oy + min_y, ox + max_x, oy + max_y],
        "px": count,
    }


def build_provenance(*, source_path: Path, source_sha: str, normalized_sha: str,
                     bands: dict, measured: dict, profiles: dict, matte: dict,
                     ornament_coverage: dict, insets: dict, sweeps: dict, content: dict,
                     flowers: dict, min_client_width: float, min_client_height: float,
                     regions: list[dict], artifacts: dict) -> dict:
    """Complete, factual record of what this builder actually authored.

    No renderer/runtime acceptance is claimed: only what is measurably done here
    is marked authored, everything else stays unverified.
    """
    base_sides = {}
    for side in ("left", "right", "top", "bottom"):
        info = measured[side]
        base_sides[side] = {
            "source_band_px": info["band"],
            "outer_visible_origin_px": info["origin"],
            "target_inset_px": insets["insets"][side],
            "profile_samples": len(profiles[side]),
        }

    ornament_content = {}
    for region_id in ORNAMENT_ENVELOPES:
        ornament_content[region_id] = content_bbox(
            content[region_id],
            ORNAMENT_ENVELOPES[region_id][2],
            ORNAMENT_ENVELOPES[region_id][3],
            (ORNAMENT_ENVELOPES[region_id][0], ORNAMENT_ENVELOPES[region_id][1]),
        )

    return {
        "kind": "frieren-flower-relief-pack-provenance",
        "schema_version": 1,
        "target": "kitty-skins schema 2, layered_ornaments + flower_effects",
        "id": PACK_ID,
        "name": PACK_NAME,
        "generator": "tools/build-frieren-flower.py",
        "source": {
            "file": str(source_path),
            "immutable": True,
            "width": SOURCE_WIDTH,
            "height": SOURCE_HEIGHT,
            "sha256": source_sha,
            "expected_sha256": EXPECTED_SOURCE_SHA256,
            "normalized_sha256": normalized_sha,
            "note": (
                "The two atlases are authored independently: exact.png carries only the "
                "authored sculpture silhouette (background matte times the authored coverage "
                "mask), adaptive.png only the reconstructed structural silver. "
                "The master file itself is never written."
            ),
        },
        "matte": matte,
        "geometry": {
            "aperture": {side: bands[side] for side in ("left", "right", "top", "bottom")},
            "aperture_basis": (
                "measured directly from the neutral centreline profiles (x=768 for "
                "top/bottom, y=512 for left/right): the first/last painted run, recorded "
                "as-is; the aperture is measurement, not a design choice"
            ),
            "frame_insets": insets["insets"],
            "frame_insets_candidates": INSET_CANDIDATES,
            "frame_insets_basis": (
                "owner-approved candidates kept when the real ornament content and the "
                "petal sweep fit, otherwise the offending side grown minimally until the "
                "protected opening is disjoint; no aspect-drift padding"
            ),
            "frame_insets_log": insets["log"],
            "reserved_opening_source": insets["opening"],
            "adaptive_scale": ADAPTIVE_SCALE,
            "rendered_band_px_at_scale": {
                side: round(bands[side] * ADAPTIVE_SCALE, 3) for side in ("left", "right", "top", "bottom")
            },
            "rendered_inset_px_at_scale": {
                side: round(insets["insets"][side] * ADAPTIVE_SCALE, 3) for side in ("left", "right", "top", "bottom")
            },
            "min_client": {"width": min_client_width, "height": min_client_height},
            "exact": {
                "aspect": round(EXACT_ASPECT, 6),
                "aspect_tolerance": EXACT_ASPECT_TOLERANCE,
                "min_width": EXACT_MIN_WIDTH,
                "min_height": EXACT_MIN_HEIGHT,
            },
            "ornament_envelopes": ORNAMENT_ENVELOPES,
            "ornament_content": ornament_content,
            "petal_sweeps_source": sweeps,
        },
        "authored": {
            "structural_base": {
                "canvas": "adaptive.png (full source size, centre transparent)",
                "sides": base_sides,
                "construction": (
                    "each side is a longitudinally constant extrusion of its measured real "
                    "profile; atlas coordinate i lands at physical distance i*T/b once the "
                    "renderer stretches the measured band b to the reserved thickness T. "
                    "Below the original outer visible coordinate o the base is transparent; "
                    "at or above it the original painted bevel cross-section is sampled over "
                    "[o, b) and normalized opaque, so the exterior edge stays put while the "
                    "painted inner edge physically reaches T"
                ),
                "corners": (
                    "ordinary miter: a corner pixel takes the profile of the side with the "
                    "smaller normalized thickness parameter, matching both side profiles at "
                    "the adjoining edges"
                ),
                "column_cap_px": COLUMN_CAP,
                "regions": [region["id"] for region in regions if region["atlas"] == "adaptive"],
            },
            "ornament_coverage": ornament_coverage,
            "petals": flowers,
            "motion": {
                "period_s": FLOWER_PERIOD,
                "flower_phases": FLOWER_PHASES,
                "max_abs_angle_deg": MOTION_MAX_ABS_ANGLE,
                "sweep_step_deg": SWEEP_STEP_DEG,
                "rationale": (
                    "depicted object: silver flower blossoms; behaviour: the authored exposed "
                    "blades unfold slightly and return (kinematic cosine, no opacity "
                    "modulation); moving part: complete exposed leaf blades only; stationary "
                    "anchor: the pivot at the blossom root, reinforced by a fixed original-source "
                    "collar; occluding foreground: the pearl opals, beads and red inlay"
                ),
            },
            "artistic_choices": (
                "the two petal selections per flower are the parent's explicit art selections "
                "from the master (TL top-left exposed leaf and lower descending leaf; BR "
                "lower-left exposed blade and right exposed leaf); the ornamental envelopes, "
                "occluders and the coverage mask are the shared plan's authored selections. "
                "Pixels outside the selected blades remain stationary."
            ),
        },
        "assembly": {
            "tool": "ImageMagick",
            "atlases": {
                "exact": (
                    "full source canvas carrying only the four ornament envelopes at their "
                    "original coordinates, alpha = studio matte alpha * authored coverage / "
                    "255; the studio background and the mask's discarded rail/background never "
                    "enter, and the whole artwork is never re-emitted as rectangles"
                ),
                "adaptive": (
                    "full source canvas carrying only the reconstructed structural silver: "
                    "four constant profiles plus four mitered corners; no leaves, pearls or "
                    "jewels"
                ),
            },
            "regions": regions,
        },
        "artifacts": artifacts,
        "acceptance": {
            "authored": True,
            "contract_validated": False,
            "rendered": "unverified",
            "visually_accepted": "unverified",
            "note": "no runtime, screenshot or acceptance claim is made by this pack",
        },
    }


# --- output handling -----------------------------------------------------------


def guard_output(output: Path) -> None:
    if output.is_symlink():
        raise BuildError(f"refusing to write through a symlink: {output}")
    resolved = output.resolve()
    if resolved == REPO_ROOT or REPO_ROOT.is_relative_to(resolved):
        raise BuildError(f"refusing to write into the repository root or one of its parents: {resolved}")
    if resolved.parent == resolved:
        raise BuildError(f"refusing to write to a filesystem root: {resolved}")
    if resolved == Path.home():
        raise BuildError(f"refusing to write to the home directory: {resolved}")
    if resolved.exists():
        if not resolved.is_dir():
            raise BuildError(f"output exists and is not a directory: {resolved}")
        existing = sorted(entry.name for entry in resolved.iterdir())
        if existing:
            raise BuildError(
                f"refusing to overwrite a non-empty output directory: {resolved}\n"
                f"it already contains: {', '.join(existing)}"
            )


# --- entry point ---------------------------------------------------------------


def pack_files() -> list[str]:
    names = ["skin.json", "source.json", "kitty.conf", "exact.png", "adaptive.png", ORNAMENT_MASK_ASSET]
    for region_id in FLOWER_REGIONS:
        names.append(f"{region_id}-background.png")
        names.append(f"{region_id}-foreground.png")
        for index in range(len(PETALS[region_id])):
            names.append(f"{region_id}-petal-{index}.png")
    return names


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Author the Frieren Flower Relief Joined kitty-skins pack from the immutable master.",
    )
    parser.add_argument("--source", required=True, help="immutable 1536x1024 frame master (required)")
    parser.add_argument("--output", required=True, help="pack directory to write (required)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    source = Path(args.source)
    output = Path(args.output)

    magick()
    if not source.is_file():
        raise BuildError(f"source master is missing: {source}")
    source_sha = sha256_file(source)
    if source_sha != EXPECTED_SOURCE_SHA256:
        raise BuildError(
            f"source digest {source_sha} does not match the approved master "
            f"{EXPECTED_SOURCE_SHA256}; refusing to extract from unapproved art"
        )
    dims = run([magick(), "identify", "-format", "%w %h", str(source)]).strip()
    if dims != f"{SOURCE_WIDTH} {SOURCE_HEIGHT}":
        raise BuildError(f"source is {dims}, expected {SOURCE_WIDTH} {SOURCE_HEIGHT}")

    mask_path = REPO_ROOT / ORNAMENT_MASK_REL
    if not mask_path.is_file():
        raise BuildError(f"authored ornament coverage mask is missing: {mask_path}")
    mask_sha = sha256_file(mask_path)
    guard_output(output)

    source_asset = f"source{source.suffix.lower()}"
    files = pack_files() + [source_asset]

    with tempfile.TemporaryDirectory(prefix="frieren-flower-") as handle:
        tmp = Path(handle)

        # 1. Normalize the master and export its RGBA raster.
        normalized = tmp / "master.png"
        run([magick(), str(source), "-resize", f"{SOURCE_WIDTH}x{SOURCE_HEIGHT}!", "+repage",
             "-depth", "8", f"PNG32:{normalized}"])
        normalized_sha = sha256_file(normalized)
        data = export_rgba(normalized, tmp / "master.rgba")

        # 2. Semantic matte BEFORE any assembly: the interior is seeded too, so
        #    the enclosed client opening clears (never client-clipped artwork).
        reference = estimate_background(data)
        matte = apply_background_matte(data, reference)

        # 3. Measure the neutral centrelines directly (no opening gate).
        measured = measure_bands(data)
        bands = {side: measured[side]["band"] for side in ("left", "right", "top", "bottom")}
        profiles = build_profiles(data, measured)

        # 4. Ornament-only exact atlas: the fixed authored coverage mask is
        #    rasterized at 4x and multiplied into the matte alpha. No heuristic
        #    rail/colour removal exists any more.
        coverage = rasterize_ornament_coverage(mask_path, tmp / "ornament-coverage.gray")
        exact, coverage_stats = apply_ornament_coverage(data, coverage)

        # 5. Petal sweeps, content masks and the physical insets they require.
        sweeps: dict[str, list[list[int]]] = {}
        for region_id in FLOWER_REGIONS:
            rx, ry = ORNAMENT_ENVELOPES[region_id][:2]
            sweeps[region_id] = [
                sweep_bounds(petal["polygon"], petal["pivot"], petal["angle"], rx, ry)
                for petal in PETALS[region_id]
            ]
        content = build_content_masks(exact, sweeps)
        insets = derive_frame_insets(content, bands, sweeps)

        # 6. Structural base: four real profiles, mitered, painted to the
        #    reserved thickness; the centre is transparent by construction.
        adaptive = author_structural_base(profiles, measured, insets["insets"])

        exact_path = tmp / "exact.png"
        write_rgba(exact, exact_path, SOURCE_WIDTH, SOURCE_HEIGHT, strip=True)
        adaptive_path = tmp / "adaptive.png"
        write_rgba(adaptive, adaptive_path, SOURCE_WIDTH, SOURCE_HEIGHT, strip=True)

        # 7. Flower rasters: repaired background, occluding foreground, petals.
        flower_rasters, flower_provenance = build_flower_rasters(data, exact)
        artifacts: dict[str, dict] = {
            "exact.png": {"kind": "authored-coverage exact atlas", "sha256": sha256_file(exact_path)},
            "adaptive.png": {"kind": "reconstructed structural silver atlas", "sha256": sha256_file(adaptive_path)},
            ORNAMENT_MASK_ASSET: {
                "kind": "authored ornament coverage mask (white retain, black discard)",
                "sha256": mask_sha,
                "method": ORNAMENT_COVERAGE_FORMULA,
            },
        }
        for name, (raster, raster_w, raster_h) in flower_rasters.items():
            staged = tmp / name
            write_rgba(raster, staged, raster_w, raster_h, strip=True)
            artifacts[name] = {"kind": "flower effect raster", "sha256": sha256_file(staged)}

        # 8. Region table, manifest and provenance; nothing touches the output
        #    until the whole pack is staged.
        regions = structural_regions(bands) + ornament_regions()
        min_client_width, min_client_height = client_minima(bands)
        write_json(tmp / "skin.json", skin_manifest(
            source_sha, bands, regions, min_client_width, min_client_height, insets["insets"],
        ))
        write_json(tmp / "source.json", build_provenance(
            source_path=source, source_sha=source_sha, normalized_sha=normalized_sha,
            bands=bands, measured=measured, profiles=profiles, matte=matte,
            ornament_coverage={
                "file": ORNAMENT_MASK_REL,
                "asset": ORNAMENT_MASK_ASSET,
                "sha256": mask_sha,
                "width": SOURCE_WIDTH,
                "height": SOURCE_HEIGHT,
                "supersample": ORNAMENT_COVERAGE_SUPERSAMPLE,
                "density_dpi": ORNAMENT_MASK_DENSITY_DPI,
                "formula": ORNAMENT_COVERAGE_FORMULA,
                **coverage_stats,
            },
            insets=insets, sweeps=sweeps, content=content,
            flowers=flower_provenance, min_client_width=min_client_width,
            min_client_height=min_client_height, regions=regions, artifacts=artifacts,
        ))
        (tmp / "kitty.conf").write_text(
            f"# {PACK_ID} frame pack: global application and kitty frame material.\n"
            "# This file intentionally declares no Kitty colour keys; the active system\n"
            "# theme include owns every colour.\n",
            encoding="utf-8",
        )
        shutil.copyfile(source, tmp / source_asset)
        shutil.copyfile(mask_path, tmp / ORNAMENT_MASK_ASSET)

        for name in files:
            publish(tmp / name, output / name)

    print(f"{PACK_ID}: {output}")
    for name in files:
        print(f"  {name}")
    print(f"  command {' '.join(sys.argv)}")
    print(f"  source sha256 {source_sha}")
    print(f"  coverage mask {ORNAMENT_MASK_REL} sha256 {mask_sha}")
    print(f"  ornament coverage retained {coverage_stats['retained_px']} px, "
          f"mask-removed {coverage_stats['coverage_removed_px']} px, "
          f"envelopes {coverage_stats['envelope_px']} px")
    print(f"  aperture left {bands['left']} right {bands['right']} top {bands['top']} bottom {bands['bottom']}")
    print(f"  frame_insets left {insets['insets']['left']} right {insets['insets']['right']} "
          f"top {insets['insets']['top']} bottom {insets['insets']['bottom']}")
    print(f"  reserved opening {insets['opening']['width']}x{insets['opening']['height']} source px")
    print(f"  adaptive scale {ADAPTIVE_SCALE} min client {min_client_width:.0f}x{min_client_height:.0f}")
    print(f"  flower effects: {', '.join(FLOWER_REGIONS)} period {FLOWER_PERIOD}s "
          f"(BR phase {FLOWER_PHASES['flower-br']})")
    print("  acceptance: authored; rendered/visually accepted unverified")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BuildError as error:
        print(f"build-frieren-flower: error: {error}", file=sys.stderr)
        sys.exit(1)
    except (RuntimeError, ValueError, OSError) as error:
        print(f"build-frieren-flower: error: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
