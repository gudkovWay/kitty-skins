#!/usr/bin/env python3
"""Deterministic ImageMagick assembly of the Frieren Silver kitty-skins pack.

One immutable production master, ``assets/source/frieren-silver.webp`` (1536x1024
RGBA, an illustrated frame ring around a transparent client opening), is the only
product source. The measured aperture is asymmetric — left 136, right 136, top 166,
bottom 169 source pixels — because the painted ring is thicker at the top and
bottom than at the sides.

Both atlases are that same full master with its own alpha preserved and only the
client aperture hard cleared:

* ``exact.png``     the complete artwork with the opening cleared to full
                    transparency (CopyOpacity of the master's own alpha with the
                    client rectangle painted black in it, not a stroke), so the
                    alpha silhouette outside the opening is preserved pixel for
                    pixel.
* ``adaptive.png``  a byte-for-byte copy of ``exact.png``. Every adaptive region
                    indexes this artwork directly at its authored source
                    rectangle, so there is no shelf atlas, no mirrored tile and no
                    second composition to keep in sync.

The adaptive manifest is a contiguous, ordered partition of the original frame
bands, so neighbouring artwork stays adjacent and no unique motif is ever
repeated. Corners are unchanged. Each column is top cap (320 px) → middle gap (the
original 49 px between the caps, y486..535, repeat none) → bottom cap (320 px).
The top and bottom rails partition the band between the two corners (source
x136..1400) into the authored runs below: a run is either ``fixed`` — it keeps its
natural source length (source px * adaptive scale), so an ornament-bearing stretch
of artwork is never distorted — or elastic, sharing the remaining rail length
proportionally. Nothing is tiled or mirrored, and the central ornaments appear
exactly once, as fixed rail segments.

Deterministic: no timestamps, no locale-dependent formatting, uniform PNG options,
one fixed ImageMagick argument shape per step, and nothing is published into the
final directory until the whole run has succeeded.

Offline: only the local ImageMagick binary is used. No network, no model calls.

Run:  python3 tools/build-frieren-silver.py
      python3 tools/build-frieren-silver.py --output /tmp/frieren-silver
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- pinned source ----------------------------------------------------------

SOURCE_REL = "assets/source/frieren-silver.webp"
SOURCE_SHA256 = "bc3bfc71d83a65d72d4e5621fdd5a4b98d5db53a35d4cc4e059a5da2961e84f4"
SOURCE_WIDTH = 1536
SOURCE_HEIGHT = 1024

MASTER_ORIGIN = {
    "pipeline_content_id": "d101a3e5ba379fa8248d6c3714c6de465af33f0130b3828210e0b15e1d55de48",
    "pipeline_frame": "225922d876dd4a91967cb5e543c36293",
    "file": "generated.webp",
    "note": "last transparent frame of the quality-trial generation, copied byte for byte into assets/source",
}

# --- reviewed geometry ------------------------------------------------------

#: Measured source aperture. The cut is one number both as source bands and as the
#: physical reserved frame: no frame_insets are declared, so the legacy exact path
#: draws the complete exact atlas and the adaptive extents are exactly these bands
#: times the adaptive scale.
APERTURE_LEFT = 136
APERTURE_RIGHT = 136
APERTURE_TOP = 166
APERTURE_BOTTOM = 169

#: Adaptive frame scale. 0.40 keeps the frame a companion margin rather than a
#: window: the rendered bands are ~54 logical px at the sides, ~66-68 top/bottom.
ADAPTIVE_SCALE = 0.40

MIN_CLIENT_WIDTH = 560
#: The two 320 px caps (256 logical px on screen) plus the bands must fit above the
#: minimum client, so the minimum client height is raised to match.
MIN_CLIENT_HEIGHT = 320

EXACT_ASPECT = SOURCE_WIDTH / SOURCE_HEIGHT  # 1.5
EXACT_ASPECT_TOLERANCE = 0.06
EXACT_MIN_WIDTH = 900
EXACT_MIN_HEIGHT = 600

CORNER_WIDTH = APERTURE_LEFT
CORNER_HEIGHT_TOP = APERTURE_TOP
CORNER_HEIGHT_BOTTOM = APERTURE_BOTTOM

#: One-shot column cap depth. The reviewed depth that reaches the side pearls and
#: ribbons (the source's side embellishments at y318, y450, y553, y598) so each
#: appears exactly once: top caps cover y166..485, bottom caps y535..854, and the
#: quiet gap between them (y486..534) is the column middle.
CAP = 320

COLUMN_MIDDLE_Y = APERTURE_TOP + CAP                             # 486
COLUMN_CAP_BOTTOM_Y = SOURCE_HEIGHT - APERTURE_BOTTOM - CAP      # 535
COLUMN_MIDDLE_HEIGHT = COLUMN_CAP_BOTTOM_Y - COLUMN_MIDDLE_Y     # 49

# --- authored cuts ----------------------------------------------------------

#: Top and bottom rail partitions, authored in visual order. Each entry is
#: ``(source_x0, source_x1, fixed)``: the ranges are contiguous and together span
#: the band between the two corners, source x136..1400. Fixed runs carry the
#: ornament-bearing artwork and keep their natural source length; the elastic runs
#: between them are the connecting ribbon material that absorbs the remainder.
#: Equal total elastic length to either side of each centrepiece keeps the
#: source centre at the window centre as the rails grow.
RAIL_TOP_SPANS = (
    (136, 256, True),
    (256, 280, False),
    (280, 312, True),
    (312, 488, False),
    (488, 928, True),
    (928, 992, False),
    (992, 1168, True),
    (1168, 1304, False),
    (1304, 1400, True),
)
RAIL_BOTTOM_SPANS = (
    (136, 208, True),
    (208, 272, False),
    (272, 464, True),
    (464, 560, False),
    (560, 976, True),
    (976, 1080, False),
    (1080, 1272, True),
    (1272, 1328, False),
    (1328, 1400, True),
)

# --- outputs ----------------------------------------------------------------

PACK_ID = "frieren-silver"
PACK_NAME = "Frieren Silver"

PACK_FILES = (
    "skin.json",
    "exact.png",
    "adaptive.png",
    "kitty.conf",
    "source.json",
)

#: Effect files this pack owned before the static rebuild. They are accepted in an
#: existing output directory for migration but are never produced again, and they
#: are retired only after every new file has been published successfully.
LEGACY_FILES = (
    "accent-glint.png",
    "accent-rose.png",
    "companion.json",
)

PNG_FLAGS = [
    "-strip",
    "-depth", "8",
    "-define", "png:color-type=6",
    "-define", "png:exclude-chunk=date,time",
]

#: `txt:` prints `#RRGGBB` for opaque pixels and `#RRGGBBAA` once an alpha channel
#: exists; both shapes must parse, so a missing alpha reads as fully opaque.
HEX_PIXEL = re.compile(r"#([0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?)(?![0-9A-Fa-f])")

#: Geometry guard thresholds for the authored cut.
GUARD_EDGE_MIN_RUN = 0.70      # longest painted run along each inner edge
GUARD_OPENING_MAX_ALPHA = 8.0  # the client opening must be empty


class BuildError(RuntimeError):
    """Actionable failure; reported without a traceback."""


# --- helpers ----------------------------------------------------------------


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


def read_pixels(path: Path, width: int, height: int, x: int, y: int) -> list[tuple[int, int, int, int]]:
    text = run([magick(), str(path), "-crop", f"{width}x{height}+{x}+{y}", "+repage", "-depth", "8", "txt:-"])
    pixels: list[tuple[int, int, int, int]] = []
    for line in text.splitlines():
        match = HEX_PIXEL.search(line)
        if match is None:
            continue
        value = match.group(1)
        if len(value) == 6:
            value += "FF"
        pixels.append(tuple(int(value[i:i + 2], 16) for i in (0, 2, 4, 6)))
    if len(pixels) != width * height:
        raise BuildError(f"pixel probe returned {len(pixels)} of {width * height} pixels for {path}")
    return pixels


def alpha_mean(path: Path, width: int, height: int, x: int, y: int) -> float:
    value = run([
        magick(), str(path), "-crop", f"{width}x{height}+{x}+{y}", "+repage",
        "-alpha", "extract", "-format", "%[fx:mean]", "info:",
    ]).strip()
    return float(value)


#: Decorative-fringe material floor, in 8-bit alpha. Expressed as a percentage for
#: ``-threshold`` so the result never depends on ImageMagick's internal quantum
#: depth; strictly-above semantics match a plain `alpha > PAINT_FLOOR_ALPHA` scan.
PAINT_FLOOR_ALPHA = 32
PAINT_FLOOR_PERCENT = f"{PAINT_FLOOR_ALPHA / 255 * 100:.2f}%"


def painted_fraction(path: Path, width: int, height: int, x: int, y: int) -> float:
    """Fraction of pixels with alpha strictly above the decorative-fringe floor."""
    value = run([
        magick(), str(path), "-crop", f"{width}x{height}+{x}+{y}", "+repage",
        "-alpha", "extract", "-threshold", PAINT_FLOOR_PERCENT, "-format", "%[fx:mean]", "info:",
    ]).strip()
    return float(value)


def longest_painted_run(sequence: list[int]) -> int:
    best = current = 0
    for alpha in sequence:
        current = current + 1 if alpha >= 128 else 0
        best = max(best, current)
    return best


# --- manifest ---------------------------------------------------------------


def region_rows() -> list[dict]:
    """The schema-2 region table: one contiguous cut of the original artwork.

    Rail regions are emitted in visual order — flexible runs keep manifest order —
    with ``repeat`` none so the renderer never tiles them. ``fixed`` is declared
    only on the flexible roles: a fixed run keeps its authored source length, an
    elastic run absorbs the remaining rail length.
    """
    rows: list[dict] = []

    def add(region_id: str, role: str, rect: tuple[int, int, int, int], fixed: bool | None = None) -> None:
        row = {
            "id": region_id,
            "atlas": "adaptive",
            "rect": list(rect),
            "role": role,
            "anchor": "top-left",
            "offset": [0, 0],
            "repeat": "none",
            "z": 0,
        }
        if fixed is not None:
            row["fixed"] = fixed
        rows.append(row)

    add("corner-top-left", "corner-top-left", (0, 0, CORNER_WIDTH, CORNER_HEIGHT_TOP))
    add("corner-top-right", "corner-top-right", (SOURCE_WIDTH - CORNER_WIDTH, 0, CORNER_WIDTH, CORNER_HEIGHT_TOP))
    add("corner-bottom-left", "corner-bottom-left", (0, SOURCE_HEIGHT - CORNER_HEIGHT_BOTTOM, CORNER_WIDTH, CORNER_HEIGHT_BOTTOM))
    add("corner-bottom-right", "corner-bottom-right", (SOURCE_WIDTH - CORNER_WIDTH, SOURCE_HEIGHT - CORNER_HEIGHT_BOTTOM, CORNER_WIDTH, CORNER_HEIGHT_BOTTOM))

    for index, (x0, x1, fixed) in enumerate(RAIL_TOP_SPANS, start=1):
        add(f"rail-top-{index:02d}", "rail-top", (x0, 0, x1 - x0, CORNER_HEIGHT_TOP), fixed)
    for index, (x0, x1, fixed) in enumerate(RAIL_BOTTOM_SPANS, start=1):
        add(f"rail-bottom-{index:02d}", "rail-bottom", (x0, SOURCE_HEIGHT - CORNER_HEIGHT_BOTTOM, x1 - x0, CORNER_HEIGHT_BOTTOM), fixed)

    add("column-left-top", "column-left-top", (0, APERTURE_TOP, CORNER_WIDTH, CAP))
    add("column-left-middle", "column-left-middle", (0, COLUMN_MIDDLE_Y, CORNER_WIDTH, COLUMN_MIDDLE_HEIGHT), False)
    add("column-left-bottom", "column-left-bottom", (0, COLUMN_CAP_BOTTOM_Y, CORNER_WIDTH, CAP))
    add("column-right-top", "column-right-top", (SOURCE_WIDTH - CORNER_WIDTH, APERTURE_TOP, CORNER_WIDTH, CAP))
    add("column-right-middle", "column-right-middle", (SOURCE_WIDTH - CORNER_WIDTH, COLUMN_MIDDLE_Y, CORNER_WIDTH, COLUMN_MIDDLE_HEIGHT), False)
    add("column-right-bottom", "column-right-bottom", (SOURCE_WIDTH - CORNER_WIDTH, COLUMN_CAP_BOTTOM_Y, CORNER_WIDTH, CAP))

    return rows


def skin_manifest(source_sha: str) -> dict:
    return {
        "schema": 2,
        "id": PACK_ID,
        "name": PACK_NAME,
        "filter": "linear",
        "source": {"width": SOURCE_WIDTH, "height": SOURCE_HEIGHT, "sha256": source_sha},
        "aperture": {
            "left": APERTURE_LEFT,
            "right": APERTURE_RIGHT,
            "top": APERTURE_TOP,
            "bottom": APERTURE_BOTTOM,
        },
        "exact": {
            "atlas": "exact.png",
            "aspect": EXACT_ASPECT,
            "aspect_tolerance": EXACT_ASPECT_TOLERANCE,
            "min_width": EXACT_MIN_WIDTH,
            "min_height": EXACT_MIN_HEIGHT,
        },
        "adaptive": {
            "atlas": "adaptive.png",
            "scale": ADAPTIVE_SCALE,
            "min_client_width": MIN_CLIENT_WIDTH,
            "min_client_height": MIN_CLIENT_HEIGHT,
        },
        "regions": region_rows(),
    }


# --- source verification ----------------------------------------------------


def verify_source(path: Path) -> None:
    if not path.is_file():
        raise BuildError(f"production source is missing: {path}")
    actual = sha256_file(path)
    if actual != SOURCE_SHA256:
        raise BuildError(
            f"source checksum changed: {path}\n     expected {SOURCE_SHA256}\n          got {actual}\n"
            "The production source is immutable; restore it rather than rebuilding from a new file."
        )
    dims = run([magick(), "identify", "-format", "%w %h", str(path)]).strip()
    if dims != f"{SOURCE_WIDTH} {SOURCE_HEIGHT}":
        raise BuildError(f"source is {dims}, expected {SOURCE_WIDTH} {SOURCE_HEIGHT}")


def geometry_guard(master: Path) -> dict:
    """Prove the reviewed cut lands on material and that the client opening is empty.

    The inner edge of the reserved frame is the line at x = left-1 / x = W-right and
    y = top-1 / y = H-bottom. The painted ring is organic, so each edge is scored by
    its longest contiguous painted run rather than by total coverage: a painted
    opening ("moat") would cut that run to almost nothing, while the artwork's own
    transparent outer margins only trim its ends. The client opening itself is
    sampled with a centre patch and must be empty before anything is written.
    """
    opening_w = SOURCE_WIDTH - APERTURE_LEFT - APERTURE_RIGHT
    opening_h = SOURCE_HEIGHT - APERTURE_TOP - APERTURE_BOTTOM

    patch = 256
    patch_x = (SOURCE_WIDTH - patch) // 2
    patch_y = (SOURCE_HEIGHT - patch) // 2
    centre_alpha = alpha_mean(master, patch, patch, patch_x, patch_y)

    edges = {
        "left": read_pixels(master, 1, SOURCE_HEIGHT, APERTURE_LEFT - 1, 0),
        "right": read_pixels(master, 1, SOURCE_HEIGHT, SOURCE_WIDTH - APERTURE_RIGHT, 0),
        "top": read_pixels(master, SOURCE_WIDTH, 1, 0, APERTURE_TOP - 1),
        "bottom": read_pixels(master, SOURCE_WIDTH, 1, 0, SOURCE_HEIGHT - APERTURE_BOTTOM),
    }

    edge_report: dict[str, dict] = {}
    for name, strip in edges.items():
        alphas = [px[3] for px in strip]
        painted_run = longest_painted_run(alphas)
        edge_report[name] = {
            "samples": len(alphas),
            "longest_run": painted_run,
            "run_ratio": round(painted_run / len(alphas), 6),
            "painted_px": sum(1 for alpha in alphas if alpha >= 128),
        }

    # Painted slivers the straight cut removes: material strictly inside the measured
    # aperture. They are the organic inner fringe of the ring and are hard cleared in
    # exact.png so no skin pixel can cover the terminal.
    sliver_fraction = painted_fraction(master, opening_w, opening_h, APERTURE_LEFT, APERTURE_TOP)
    sliver = round(sliver_fraction * opening_w * opening_h)
    sliver_bbox: list[int] | None = None
    if sliver:
        box = run([
            magick(), str(master), "-crop", f"{opening_w}x{opening_h}+{APERTURE_LEFT}+{APERTURE_TOP}", "+repage",
            "-alpha", "extract", "-threshold", PAINT_FLOOR_PERCENT, "-format", "%@", "info:",
        ]).strip()
        match = re.fullmatch(r"(\d+)x(\d+)\+(\d+)\+(\d+)", box)
        if match is None:
            raise BuildError(f"cannot parse the cleared-sliver bounding box: {box!r}")
        bw, bh, bx, by = (int(value) for value in match.groups())
        sliver_bbox = [bx + APERTURE_LEFT, by + APERTURE_TOP, bw, bh]

    report = {
        "aperture": {
            "left": APERTURE_LEFT,
            "right": APERTURE_RIGHT,
            "top": APERTURE_TOP,
            "bottom": APERTURE_BOTTOM,
        },
        "inner_edges": edge_report,
        "opening": {
            "patch": patch,
            "rect": [patch_x, patch_y, patch, patch],
            "alpha_mean": round(centre_alpha, 3),
            "kind": "transparent" if centre_alpha < GUARD_OPENING_MAX_ALPHA else "painted",
        },
        "cleared_slivers": {
            "pixels": sliver,
            "alpha_floor": PAINT_FLOOR_ALPHA,
            "bbox": sliver_bbox,
            "note": "organic inner fringe removed by the straight aperture cut; recorded, never re-added",
        },
    }

    failures: list[str] = []
    for name, edge in edge_report.items():
        if edge["run_ratio"] < GUARD_EDGE_MIN_RUN:
            failures.append(f"{name} inner edge painted run {edge['run_ratio']:.3f} < {GUARD_EDGE_MIN_RUN}")
    if centre_alpha >= GUARD_OPENING_MAX_ALPHA:
        failures.append(f"client opening is not empty (mean alpha {centre_alpha:.1f})")
    if failures:
        raise BuildError("geometry guard failed: " + "; ".join(failures))
    return report


# --- atlas assembly ---------------------------------------------------------


def aperture_draw(width: int, height: int) -> str:
    return f"rectangle {APERTURE_LEFT},{APERTURE_TOP},{width - APERTURE_RIGHT - 1},{height - APERTURE_BOTTOM - 1}"


def build_exact(master: Path, tmp: Path) -> Path:
    """The complete artwork with the client aperture hard cleared to transparency.

    The mask is the master's own alpha channel with the client rectangle painted
    black in it, then CopyOpacity applies that alpha. Forcing the mask opaque
    instead would overwrite the artwork's alpha silhouette outside the opening with
    full opacity and reveal the hidden RGB of partially transparent pixels.
    """
    exact = tmp / "exact.png"
    run([
        magick(), "(", str(master), "-alpha", "set", ")",
        "(", str(master), "-alpha", "extract",
        "-fill", "black", "-draw", aperture_draw(SOURCE_WIDTH, SOURCE_HEIGHT), "-alpha", "off", ")",
        "-compose", "CopyOpacity", "-composite",
        *PNG_FLAGS, f"PNG32:{exact}",
    ])
    return exact


def build_adaptive(exact: Path, tmp: Path) -> Path:
    """Both atlases are the same alpha-preserved full master.

    The adaptive regions index the artwork directly at their authored source
    rectangles, so the adaptive atlas is the exact atlas byte for byte and the two
    can never drift apart.
    """
    adaptive = tmp / "adaptive.png"
    shutil.copyfile(exact, adaptive)
    return adaptive


# --- provenance -------------------------------------------------------------


def span_partition(spans: tuple[tuple[int, int, bool], ...], y: int, height: int) -> list[dict]:
    """The authored rail partition, as declared in the manifest and in the contract."""
    return [
        {
            "source_range": [x0, x1],
            "rect": [x0, y, x1 - x0, height],
            "natural_logical_px": round((x1 - x0) * ADAPTIVE_SCALE, 3),
            "fixed": fixed,
        }
        for x0, x1, fixed in spans
    ]


def build_provenance(source_sha: str, decoded_sha: str, guard: dict) -> dict:
    rendered = {
        "left": round(APERTURE_LEFT * ADAPTIVE_SCALE, 3),
        "right": round(APERTURE_RIGHT * ADAPTIVE_SCALE, 3),
        "top": round(APERTURE_TOP * ADAPTIVE_SCALE, 3),
        "bottom": round(APERTURE_BOTTOM * ADAPTIVE_SCALE, 3),
    }
    return {
        "kind": "frieren-silver-pack-provenance",
        "schema_version": 1,
        "target": "kitty-skins schema 2",
        "source": {
            "file": SOURCE_REL,
            "immutable": True,
            "width": SOURCE_WIDTH,
            "height": SOURCE_HEIGHT,
            "sha256": source_sha,
            "decoded_rgba_sha256": decoded_sha,
            "format": "WebP with alpha, decoded to RGBA PNG for assembly",
            "origin": MASTER_ORIGIN,
        },
        "geometry": {
            "mode": "measured",
            "aperture": {
                "left": APERTURE_LEFT,
                "right": APERTURE_RIGHT,
                "top": APERTURE_TOP,
                "bottom": APERTURE_BOTTOM,
            },
            "frame_insets": None,
            "frame_insets_note": (
                "Deliberately omitted. The legacy exact path then draws the complete exact atlas "
                "once, so the genuine artwork survives in exact mode; the frame_insets path would "
                "instead resample an atlas as an eight-piece ring."
            ),
            "adaptive_scale": ADAPTIVE_SCALE,
            "rendered_band_px": rendered,
            "min_client": {"width": MIN_CLIENT_WIDTH, "height": MIN_CLIENT_HEIGHT},
            "exact": {
                "aspect": EXACT_ASPECT,
                "aspect_tolerance": EXACT_ASPECT_TOLERANCE,
                "min_width": EXACT_MIN_WIDTH,
                "min_height": EXACT_MIN_HEIGHT,
            },
            "note": (
                "The ring is organic: its inner fringe intrudes a little past the measured aperture. "
                "The straight cut is intentional and the removed slivers are recorded in guard."
            ),
        },
        "guard": guard,
        "assembly": {
            "tool": "ImageMagick",
            "generator": "tools/build-frieren-silver.py",
            "source_width": SOURCE_WIDTH,
            "source_height": SOURCE_HEIGHT,
            "adaptive_scale": ADAPTIVE_SCALE,
            "cap": CAP,
            "atlases": {
                "exact": "the whole decoded master with its own alpha preserved and the client aperture hard cleared",
                "adaptive": "byte-identical copy of exact.png; every region samples this artwork at its authored source rectangle",
            },
            "atlases_identical": True,
            "partition": {
                "method": (
                    "contiguous ordered cuts of the original artwork; each region rect is the authored source "
                    "span, so neighbouring bands stay adjacent and nothing is tiled, mirrored or repeated"
                ),
                "corners": [
                    {"rect": [0, 0, CORNER_WIDTH, CORNER_HEIGHT_TOP]},
                    {"rect": [SOURCE_WIDTH - CORNER_WIDTH, 0, CORNER_WIDTH, CORNER_HEIGHT_TOP]},
                    {"rect": [0, SOURCE_HEIGHT - CORNER_HEIGHT_BOTTOM, CORNER_WIDTH, CORNER_HEIGHT_BOTTOM]},
                    {"rect": [SOURCE_WIDTH - CORNER_WIDTH, SOURCE_HEIGHT - CORNER_HEIGHT_BOTTOM, CORNER_WIDTH, CORNER_HEIGHT_BOTTOM]},
                ],
                "top_rail": {
                    "source_range": [APERTURE_LEFT, SOURCE_WIDTH - APERTURE_RIGHT],
                    "y": 0,
                    "height": CORNER_HEIGHT_TOP,
                    "spans": span_partition(RAIL_TOP_SPANS, 0, CORNER_HEIGHT_TOP),
                },
                "bottom_rail": {
                    "source_range": [APERTURE_LEFT, SOURCE_WIDTH - APERTURE_RIGHT],
                    "y": SOURCE_HEIGHT - CORNER_HEIGHT_BOTTOM,
                    "height": CORNER_HEIGHT_BOTTOM,
                    "spans": span_partition(RAIL_BOTTOM_SPANS, SOURCE_HEIGHT - CORNER_HEIGHT_BOTTOM, CORNER_HEIGHT_BOTTOM),
                },
                "columns": {
                    "top_cap": {"y": APERTURE_TOP, "height": CAP},
                    "middle": {"y": COLUMN_MIDDLE_Y, "height": COLUMN_MIDDLE_HEIGHT, "repeat": "none"},
                    "bottom_cap": {"y": COLUMN_CAP_BOTTOM_Y, "height": CAP},
                },
            },
            "fixed_rule": (
                "a fixed rail span keeps its natural source length (source px * adaptive scale); elastic spans "
                "share the remaining rail length proportionally; the column middle is elastic"
            ),
            "authored_note": "the source cuts above are explicit authoring decisions, not claims of visual verification",
        },
        "render_notes": {
            "exact_mode": "draws exact.png once over the whole outer box; the aperture stays transparent",
            "adaptive_mode": "composes the declared regions at their authored spans; no region has repeat x or y",
            "cut": "straight aperture cut through the organic inner fringe, recorded in guard.cleared_slivers",
            "client_safe": "no pack pixel can cover the aperture: the clear is a CopyOpacity mask, not a stroke",
        },
    }


# --- output handling --------------------------------------------------------


def guard_output(output: Path) -> None:
    resolved = output.resolve()
    if resolved == REPO_ROOT or REPO_ROOT.is_relative_to(resolved):
        raise BuildError(f"refusing to write into the repository root or one of its parents: {resolved}")
    if resolved.parent == resolved:
        raise BuildError(f"refusing to write to a filesystem root: {resolved}")
    if resolved == Path.home():
        raise BuildError(f"refusing to write to the home directory: {resolved}")
    if resolved.exists():
        if resolved.is_symlink():
            raise BuildError(f"refusing to write through a symlink: {resolved}")
        if not resolved.is_dir():
            raise BuildError(f"output exists and is not a directory: {resolved}")
        known = set(PACK_FILES) | set(LEGACY_FILES)
        unknown = sorted(entry.name for entry in resolved.iterdir() if entry.name not in known)
        if unknown:
            raise BuildError(
                f"refusing to overwrite: {resolved} contains files this pack does not own: {', '.join(unknown)}\n"
                f"known pack files: {', '.join(PACK_FILES)}"
            )


def retire_legacy(output: Path) -> list[str]:
    """Remove only this pack's own obsolete effect files, after a clean publish."""
    removed: list[str] = []
    for name in LEGACY_FILES:
        candidate = output / name
        if candidate.is_symlink() or not candidate.is_file():
            continue
        candidate.unlink()
        removed.append(name)
    return removed


# --- entry point ------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the Frieren Silver kitty-skins pack from its pinned master.",
    )
    parser.add_argument(
        "--output",
        default=str(REPO_ROOT / "assets" / "skins" / PACK_ID),
        help=f"pack directory to write (default: assets/skins/{PACK_ID})",
    )
    parser.add_argument(
        "--source",
        default=str(REPO_ROOT / SOURCE_REL),
        help="pinned master to read; must match the recorded digest",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    source = Path(args.source)
    output = Path(args.output)

    magick()
    guard_output(output)
    verify_source(source)

    with tempfile.TemporaryDirectory(prefix="frieren-silver-") as handle:
        tmp = Path(handle)
        master = tmp / "master.png"
        run([magick(), str(source), "-alpha", "set", *PNG_FLAGS, f"PNG32:{master}"])
        decoded_sha = sha256_file(master)

        guard = geometry_guard(master)
        exact = build_exact(master, tmp)
        adaptive = build_adaptive(exact, tmp)

        # The hard clear must really be transparent; a stroked rectangle would only
        # composite Over and leave the opening partially painted.
        corner_pixels = read_pixels(exact, 8, 8, APERTURE_LEFT, APERTURE_TOP)
        if any(px[3] != 0 for px in corner_pixels):
            raise BuildError("exact atlas aperture is not fully transparent")

        # The adaptive atlas is the same artwork, so its bytes must match exactly.
        if sha256_file(adaptive) != sha256_file(exact):
            raise BuildError("adaptive atlas is not a byte-identical copy of the exact atlas")

        write_json(tmp / "skin.json", skin_manifest(SOURCE_SHA256))
        write_json(tmp / "source.json", build_provenance(SOURCE_SHA256, decoded_sha, guard))

        (tmp / "kitty.conf").write_text(
            "# frieren-silver frame pack: global application and kitty frame material.\n"
            "# This file intentionally declares no Kitty colour keys; the active system\n"
            "# theme include owns every colour.\n",
            encoding="utf-8",
        )

        for name in PACK_FILES:
            publish(tmp / name, output / name)

        removed = retire_legacy(output)

    print(f"frieren-silver: {output}")
    for name in PACK_FILES:
        print(f"  {name}")
    for name in removed:
        print(f"  retired {name}")
    print(f"  command {' '.join(sys.argv)}")
    print(f"  source sha256 {SOURCE_SHA256}")
    print(f"  exact  sha256 {sha256_file(output / 'exact.png')}")
    print(f"  adapt  sha256 {sha256_file(output / 'adaptive.png')}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BuildError as error:
        print(f"build-frieren-silver: error: {error}", file=sys.stderr)
        sys.exit(1)
