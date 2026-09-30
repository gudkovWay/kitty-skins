#!/usr/bin/env bash
# Reproducible Gothic Eclipse atlas generation (manifest schema 2).
#
#   exact.png    the complete pinned source: transparency and every ornament kept,
#                drawn in exact mode as a single full-atlas operation.
#   adaptive.png the same atlas with ONLY the regions behind the separately drawn
#                one-shot ornaments repaired with neutral material, so those
#                ornaments are not duplicated underneath when placed over the base.
#
# The side columns, rails, corners and bottom architecture are never synthesised
# or removed: adaptive.png differs from the source only inside the three ornament
# rectangles below.
set -eu

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
src="$repo_root/assets/source/gothic_eclipse_minimal_idle.png"
pack="$repo_root/assets/skins/gothic-eclipse"
expected=4e8b2bd7c5233956747dc24c1540214996d9178714556786efb0b464bdacd0dd

actual="$(sha256sum "$src" | awk '{print $1}')"
if [ "$actual" != "$expected" ]; then
    echo "checksum mismatch: $src" >&2
    echo "expected $expected, got $actual" >&2
    exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Strip metadata and date/time chunks, and force straight RGBA, so output bytes
# are deterministic. Cairo premultiplies on load.
magick_args=(-strip -define png:color-type=6 -define png:exclude-chunk=date,time)

# exact.png: the faithful source atlas.
magick "$src" "${magick_args[@]}" "$tmp/exact.png"

# Neutral material used to repair the ornament regions.
magick "$src" -crop 340x284+1196+740 +repage -flop "$tmp/bottom-repair.png"
magick "$src" -crop 256x220+270+0 +repage "$tmp/top-sample.png"
magick -size 440x240 tile:"$tmp/top-sample.png" "$tmp/eclipse-repair.png"
magick -size 320x190 tile:"$tmp/top-sample.png" "$tmp/controls-repair.png"

# The repair layers carry transparency, so compositing them over the untouched
# base would let the original eclipse, controls plaque and candles show through.
# Clear the three repaired rectangles to transparent first: DstOut erases the
# base wherever the opaque white mask covers it.
magick "$src" \
    \( -size 1536x1024 xc:none -fill white \
       -draw "rectangle 548,0 987,239" \
       -draw "rectangle 1110,20 1429,209" \
       -draw "rectangle 0,740 339,1023" \) \
    -compose DstOut -composite -compose over \
    "$tmp/eclipse-repair.png" -geometry +548+0 -composite \
    "$tmp/controls-repair.png" -geometry +1110+20 -composite \
    "$tmp/bottom-repair.png" -geometry +0+740 -composite \
    "${magick_args[@]}" "$tmp/adaptive.png"

mv "$tmp/exact.png" "$pack/exact.png"
mv "$tmp/adaptive.png" "$pack/adaptive.png"
