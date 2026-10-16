#!/usr/bin/env python3
"""Conservative measurement of the painted opening of a generated frame master.

The generator asks the image model for a frame: a solid material band around one
flat, contrasting central opening. A model rarely obeys the requested band
exactly, and a wrong assumption here ships the model's own painted void to the
client as a strip between the frame and the terminal. So the band is *measured*
from the produced raster instead of assumed:

1. the master is exported as raw 8-bit RGB through the same cancellable
   ImageMagick pipeline every other step uses (no Pillow, no new dependency),
2. a 32x32 patch at the canvas centre must be flat within :data:`FLAT_SPREAD`
   per channel — otherwise there is no uniform opening to find,
3. the centre-connected region of pixels within :data:`COLOR_TOLERANCE` per
   channel of that centre colour is flood filled in pure Python,
4. the nearest distance from any opening pixel to the canvas edge, minus the
   :data:`ANTIALIAS_MARGIN`, is the conservative uniform material band: the
   material provably covers that many pixels on *every* side,
5. anything else is rejected: an opening that reaches the canvas border, an
   opening whose shape is not the centred rectangle the prompt asked for, and a
   band outside ``[MIN_SAFE_BAND, min(width, height) // 3]``.

For the asymmetric client rectangle used by the semantic decomposition the
original alpha is exported too: painted material penetrating more than
:data:`ANTIALIAS_MARGIN` pixels inside that rectangle rejects the measurement
instead of being silently cleared later.

Every rejection carries a bounded human reason; nothing is guessed and no
rejected measurement is ever used. :func:`measure` returns a record, it does not
raise for a bad but readable raster — the caller decides whether to fail the
request (generation) or to fall back to an explicitly reviewed band (offline
repair).
"""

from __future__ import annotations

import os
from pathlib import Path

from process import Cancelled, run

__all__ = ["measure", "max_safe_band", "client_intrusion", "CENTER_PATCH", "MIN_SAFE_BAND"]

#: Edge of the flatness probe patch taken at the canvas centre.
CENTER_PATCH = 32
#: Maximum per-channel spread of that patch that still counts as flat.
FLAT_SPREAD = 8
#: Per-channel tolerance that keeps a pixel inside the centre-connected opening.
COLOR_TOLERANCE = 12
#: Pixels cut inside the measured opening edge, covering the antialiased seam.
ANTIALIAS_MARGIN = 2
#: Narrowest material band worth keeping: below this a frame is a hairline.
MIN_SAFE_BAND = 24
#: One raw export of one 1536x1024 master is ~4.5 MiB and is written to the
#: job's own tmp directory, never to a shared location.
RAW_NAME = "opening.rgb"
#: Alpha-preserving companion export of the same master. Read only to tell the
#: transparent opening (alpha 0) from painted material the RGB matte above
#: cannot distinguish.
RAW_RGBA_NAME = "opening.rgba"
MAGICK_TIMEOUT = 300.0
#: Pixels between cancellation checks inside the fill loop.
CANCEL_INTERVAL = 1 << 18
#: A filled opening must cover at least this share of its bounding box: a ragged
#: blob means the tolerance caught painted texture, not a flat opening.
MIN_FILL_RATIO = 0.8


def _centre_tolerance(length: int) -> int:
    """How far the opening's bounding box may sit off centre on one axis.

    The prompt demands a rectangle centred in the canvas; a measurement whose
    bounding box is far from centred describes a different shape (a painted
    patch beside the opening, say) and must not be trusted.
    """
    return max(8, length // 64)


class _Rejected(Exception):
    """Internal control flow: this raster has no usable measured opening."""


def max_safe_band(width: int, height: int) -> int:
    """Widest band the conservative rule accepts for this canvas."""
    return min(int(width), int(height)) // 3


def _raw_export(image: Path, tmp: Path, *, cancel, env: dict) -> Path:
    """Export the master as interleaved 8-bit RGB through `magick`.

    Composite transparency onto a flat contrasting matte before reading colour.
    Generated transparent openings can contain arbitrary hidden RGB; exposing
    those bytes invents painted detail that is absent from the visible master.
    This measurement-only export leaves the original raster and alpha intact.
    """
    target = Path(tmp) / RAW_NAME
    try:
        run(
            ["magick", str(image), "-background", "#ff00ff", "-alpha", "remove",
             "-alpha", "off", "-depth", "8", f"RGB:{target}"],
            timeout=MAGICK_TIMEOUT,
            cancel=cancel,
            cwd=Path(tmp),
            env=env,
        )
    except Cancelled:
        raise
    except RuntimeError as error:
        raise RuntimeError(f"cannot export the frame master as raw RGB: {error}") from error
    try:
        if target.stat().st_size <= 0:
            raise RuntimeError(f"the raw RGB export {target} is empty")
    except OSError as error:
        raise RuntimeError(f"the raw RGB export {target} is unreadable: {error}") from error
    return target


def _raw_export_rgba(image: Path, tmp: Path, *, cancel, env: dict) -> Path:
    """Export the master as interleaved 8-bit RGBA through `magick`.

    Unlike :func:`_raw_export` this keeps the original alpha verbatim, so the
    measurement can tell a genuinely transparent opening from painted material
    without inventing colour from hidden RGB.
    """
    target = Path(tmp) / RAW_RGBA_NAME
    try:
        run(
            ["magick", str(image), "-depth", "8", f"RGBA:{target}"],
            timeout=MAGICK_TIMEOUT,
            cancel=cancel,
            cwd=Path(tmp),
            env=env,
        )
    except Cancelled:
        raise
    except RuntimeError as error:
        raise RuntimeError(f"cannot export the frame master as raw RGBA: {error}") from error
    try:
        if target.stat().st_size <= 0:
            raise RuntimeError(f"the raw RGBA export {target} is empty")
    except OSError as error:
        raise RuntimeError(f"the raw RGBA export {target} is unreadable: {error}") from error
    return target


def client_intrusion(data, width: int, height: int, client_rect, reference,
                     *, cancel=None) -> dict:
    """Count original material penetrating the proposed client rectangle.

    ``data`` is the raw RGBA export of the same master the measurement came
    from. A pixel deeper than :data:`ANTIALIAS_MARGIN` inside ``client_rect``
    whose original alpha is nonzero and whose RGB is outside
    :data:`COLOR_TOLERANCE` of the measured opening ``reference`` is painted
    material that clearing the rectangle would silently destroy. The seam
    allowance is the only admitted slack: there is no percentage budget, so a
    single orphaned ornament pixel rejects the rectangle.

    Returns ``{"count": int, "bbox": [min_x, min_y, max_x, max_y] | None}``.
    A clean rectangle yields ``count`` 0 and ``bbox`` None.
    """
    width = int(width)
    height = int(height)
    x, y, w, h = (int(value) for value in client_rect)
    # Depth 0 is the rectangle border; the antialiased seam covers
    # ANTIALIAS_MARGIN pixels, so only pixels strictly deeper than that count.
    inset = ANTIALIAS_MARGIN + 1
    left = x + inset
    top = y + inset
    right = x + w - 1 - inset
    bottom = y + h - 1 - inset

    tolerance = COLOR_TOLERANCE
    low = tuple(max(0, value - tolerance) for value in reference)
    high = tuple(min(255, value + tolerance) for value in reference)

    count = 0
    min_x = 1 << 30
    min_y = 1 << 30
    max_x = -1
    max_y = -1
    since_check = 0
    for row in range(top, bottom + 1):
        base = row * width
        for column in range(left, right + 1):
            offset = (base + column) * 4
            if data[offset + 3] == 0:
                continue
            if (low[0] <= data[offset] <= high[0]
                    and low[1] <= data[offset + 1] <= high[1]
                    and low[2] <= data[offset + 2] <= high[2]):
                continue
            count += 1
            if column < min_x:
                min_x = column
            if column > max_x:
                max_x = column
            if row < min_y:
                min_y = row
            if row > max_y:
                max_y = row
            since_check += 1
            if since_check >= CANCEL_INTERVAL:
                since_check = 0
                if cancel is not None and cancel():
                    raise Cancelled("opening intrusion scan cancelled")

    bbox = None if count == 0 else [min_x, min_y, max_x, max_y]
    return {"count": count, "bbox": bbox}


def _centre_reference(data: bytes, width: int, height: int) -> tuple[tuple[int, int, int], int]:
    """Flat reference colour of the centre patch and its worst channel spread."""
    patch = min(CENTER_PATCH, width, height)
    left = (width - patch) // 2
    top = (height - patch) // 2
    low = [255, 255, 255]
    high = [0, 0, 0]
    sums = [0, 0, 0]
    for y in range(top, top + patch):
        base = (y * width + left) * 3
        row = data[base:base + patch * 3]
        for index in range(patch):
            offset = index * 3
            for channel in range(3):
                value = row[offset + channel]
                if value < low[channel]:
                    low[channel] = value
                if value > high[channel]:
                    high[channel] = value
                sums[channel] += value
    count = patch * patch
    reference = tuple(int(round(total / count)) for total in sums)
    spread = max(high[channel] - low[channel] for channel in range(3))
    return reference, spread


def _fill_opening(data: bytes, width: int, height: int, reference, *, cancel) -> dict:
    """Flood fill the centre-connected opening; return its shape statistics."""
    tolerance = COLOR_TOLERANCE
    low = tuple(max(0, value - tolerance) for value in reference)
    high = tuple(min(255, value + tolerance) for value in reference)

    total_pixels = width * height
    visited = bytearray(total_pixels)
    # The reference is the mean of the flat centre patch, so the centre pixel
    # always matches it; no extra guard is needed here.
    start = (height // 2) * width + (width // 2)

    stack = [start]
    visited[start] = 1
    area = 0
    min_x, max_x = width, -1
    min_y, max_y = height, -1
    nearest = 1 << 30
    edge_connected = False
    since_check = 0

    while stack:
        index = stack.pop()
        x = index % width
        y = index // width
        base = index * 3
        area += 1
        if x < min_x:
            min_x = x
        if x > max_x:
            max_x = x
        if y < min_y:
            min_y = y
        if y > max_y:
            max_y = y

        distance = x if x < y else y
        opposite = width - 1 - x
        if opposite < distance:
            distance = opposite
        opposite = height - 1 - y
        if opposite < distance:
            distance = opposite
        if distance < nearest:
            nearest = distance
        if distance == 0:
            edge_connected = True

        since_check += 1
        if since_check >= CANCEL_INTERVAL:
            since_check = 0
            if cancel is not None and cancel():
                raise Cancelled("opening measurement cancelled")

        if x > 0:
            neighbour = index - 1
            if not visited[neighbour]:
                visited[neighbour] = 1
                offset = neighbour * 3
                if (low[0] <= data[offset] <= high[0]
                        and low[1] <= data[offset + 1] <= high[1]
                        and low[2] <= data[offset + 2] <= high[2]):
                    stack.append(neighbour)
        if x + 1 < width:
            neighbour = index + 1
            if not visited[neighbour]:
                visited[neighbour] = 1
                offset = neighbour * 3
                if (low[0] <= data[offset] <= high[0]
                        and low[1] <= data[offset + 1] <= high[1]
                        and low[2] <= data[offset + 2] <= high[2]):
                    stack.append(neighbour)
        if y > 0:
            neighbour = index - width
            if not visited[neighbour]:
                visited[neighbour] = 1
                offset = neighbour * 3
                if (low[0] <= data[offset] <= high[0]
                        and low[1] <= data[offset + 1] <= high[1]
                        and low[2] <= data[offset + 2] <= high[2]):
                    stack.append(neighbour)
        if y + 1 < height:
            neighbour = index + width
            if not visited[neighbour]:
                visited[neighbour] = 1
                offset = neighbour * 3
                if (low[0] <= data[offset] <= high[0]
                        and low[1] <= data[offset + 1] <= high[1]
                        and low[2] <= data[offset + 2] <= high[2]):
                    stack.append(neighbour)

    return {
        "area": area,
        "bbox": [min_x, min_y, max_x, max_y],
        "nearest_edge": nearest,
        "edge_connected": edge_connected,
    }


def measure(image, width: int, height: int, tmp, *, cancel=None, env: dict | None = None,
            asymmetric: bool = False) -> dict:
    """Measure the conservative uniform material band of one frame master.

    Returns a record that is always shaped the same way, with ``accepted``
    saying whether the numbers may be used and ``reason`` explaining a refusal.
    Raises only for infrastructure problems (a failed export, a truncated raw
    file); a readable raster that cannot be measured is a rejection, not an
    exception.

    With ``asymmetric=True`` the centring requirement is dropped: the opening
    may sit anywhere inside the canvas, and the record carries per-side
    ``bands`` and the resulting ``client_rect`` instead of one scalar band.
    Every side is held to the same minimum/maximum as the scalar path, and the
    remaining centre must stay large enough to be a usable client area. The
    proposed client rectangle is also checked against the original alpha: any
    painted pixel deeper than the seam allowance that sits outside the measured
    colour tolerance rejects the measurement instead of being cleared later.
    """
    image = Path(image)
    tmp = Path(tmp)
    width = int(width)
    height = int(height)
    environment = dict(os.environ) if env is None else env
    record: dict = {
        "patch": CENTER_PATCH,
        "tolerance": COLOR_TOLERANCE,
        "antialias_margin": ANTIALIAS_MARGIN,
        "min_band": MIN_SAFE_BAND,
        "max_band": max_safe_band(width, height),
        "width": width,
        "height": height,
        "accepted": False,
        "reason": "",
        "safe_band": None,
        "opening_distance": None,
        "center_color": None,
        "center_spread": None,
        "opening_area": None,
        "opening_bbox": None,
        "edge_connected": None,
        "asymmetric": bool(asymmetric),
        "bands": None,
        "client_rect": None,
        "intrusion_count": None,
        "intrusion_bbox": None,
    }
    raw = _raw_export(image, tmp, cancel=cancel, env=environment)
    try:
        data = raw.read_bytes()
    finally:
        try:
            raw.unlink()
        except OSError:
            pass
    expected = width * height * 3
    if len(data) < expected:
        raise RuntimeError(
            f"the raw RGB export is {len(data)} bytes, expected {expected} for {width}x{height}"
        )

    reference, spread = _centre_reference(data, width, height)
    record["center_color"] = list(reference)
    record["center_spread"] = spread
    try:
        if spread > FLAT_SPREAD:
            raise _Rejected(
                f"the canvas centre is not flat ({spread} shades of spread, at most {FLAT_SPREAD} allowed); "
                "no uniform opening colour can be measured"
            )
        shape = _fill_opening(data, width, height, reference, cancel=cancel)
        record["opening_area"] = shape["area"]
        record["opening_bbox"] = shape["bbox"]
        record["edge_connected"] = shape["edge_connected"]
        if shape["edge_connected"]:
            raise _Rejected("the opening colour reaches the canvas border, so the frame does not enclose it")

        min_x, min_y, max_x, max_y = shape["bbox"]
        box_width = max_x - min_x + 1
        box_height = max_y - min_y + 1
        ratio = shape["area"] / float(box_width * box_height)
        if ratio < MIN_FILL_RATIO:
            raise _Rejected(
                f"the opening fills only {ratio:.2f} of its bounding box "
                f"(at least {MIN_FILL_RATIO:.2f} expected); its shape is not a flat rectangle"
            )
        offset_x = abs(min_x - (width - 1 - max_x))
        offset_y = abs(min_y - (height - 1 - max_y))
        tolerance_x = _centre_tolerance(width)
        tolerance_y = _centre_tolerance(height)
        if not asymmetric and (offset_x > tolerance_x or offset_y > tolerance_y):
            raise _Rejected(
                f"the opening is off centre by {offset_x}x{offset_y} pixels "
                f"(at most {tolerance_x}x{tolerance_y} allowed)"
            )

        if asymmetric:
            min_x, min_y, max_x, max_y = shape["bbox"]
            bands = {
                "left": min_x - ANTIALIAS_MARGIN,
                "top": min_y - ANTIALIAS_MARGIN,
                "right": (width - 1 - max_x) - ANTIALIAS_MARGIN,
                "bottom": (height - 1 - max_y) - ANTIALIAS_MARGIN,
            }
            for side in ("left", "right", "top", "bottom"):
                band = bands[side]
                if band < MIN_SAFE_BAND:
                    raise _Rejected(
                        f"the measured {side} material band is {band} pixels, "
                        f"below the {MIN_SAFE_BAND} pixel minimum"
                    )
                if band > record["max_band"]:
                    raise _Rejected(
                        f"the measured {side} material band is {band} pixels, above the "
                        f"{record['max_band']} pixel canvas third"
                    )
            centre_width = width - bands["left"] - bands["right"]
            centre_height = height - bands["top"] - bands["bottom"]
            min_centre = max(CENTER_PATCH, min(width, height) // 4)
            if centre_width < min_centre or centre_height < min_centre:
                raise _Rejected(
                    f"the remaining client centre is {centre_width}x{centre_height} pixels, "
                    f"below the {min_centre}-pixel minimum on one axis"
                )
            record["bands"] = bands
            record["client_rect"] = [
                bands["left"],
                bands["top"],
                centre_width,
                centre_height,
            ]
            rgba = _raw_export_rgba(image, tmp, cancel=cancel, env=environment)
            try:
                rgba_data = rgba.read_bytes()
            finally:
                try:
                    rgba.unlink()
                except OSError:
                    pass
            expected_rgba = width * height * 4
            if len(rgba_data) < expected_rgba:
                raise RuntimeError(
                    f"the raw RGBA export is {len(rgba_data)} bytes, "
                    f"expected {expected_rgba} for {width}x{height}"
                )
            intrusion = client_intrusion(
                rgba_data, width, height, record["client_rect"], reference, cancel=cancel,
            )
            record["intrusion_count"] = intrusion["count"]
            record["intrusion_bbox"] = intrusion["bbox"]
            if intrusion["count"]:
                raise _Rejected(
                    f"{intrusion['count']} material pixels penetrate the proposed client "
                    f"rectangle more than {ANTIALIAS_MARGIN} pixels inside it "
                    f"(bbox {intrusion['bbox']}); clearing it would destroy painted material, "
                    "so the aperture must be recomposed — never widened or whitened"
                )
        else:
            distance = shape["nearest_edge"]
            record["opening_distance"] = distance
            band = distance - ANTIALIAS_MARGIN
            record["safe_band"] = band
            if band < MIN_SAFE_BAND:
                raise _Rejected(
                    f"the measured material band is {band} pixels, below the {MIN_SAFE_BAND} pixel minimum"
                )
            if band > record["max_band"]:
                raise _Rejected(
                    f"the measured material band is {band} pixels, above the "
                    f"{record['max_band']} pixel canvas third"
                )
    except _Rejected as rejection:
        record["reason"] = str(rejection)
        return record

    record["accepted"] = True
    return record
