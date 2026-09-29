#!/usr/bin/env bash
# Reproducible Gothic Eclipse asset decomposition.
# Regenerates frame.png and the three ornaments from the pinned source asset.
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

# Strip metadata and date/tIME chunks so output bytes are deterministic.
magick_args=(-strip -define png:color-type=6 -define png:exclude-chunk=date,time)

magick "$src" -crop 440x240+548+0 +repage "${magick_args[@]}" "$tmp/eclipse.png"
magick "$src" -crop 320x190+1110+20 +repage "${magick_args[@]}" "$tmp/controls.png"
magick "$src" -crop 340x284+0+740 +repage "${magick_args[@]}" "$tmp/candles.png"

# Base frame: repair eclipse/controls regions with tiled clean-top material,
# repair the candles region with the mirrored bottom-right area, keep the
# transparent client aperture. The 1536x1024 base atlas is never resized.
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
    "${magick_args[@]}" "$tmp/frame.png"

for name in eclipse controls candles; do
    mv "$tmp/$name.png" "$pack/ornaments/$name.png"
done
mv "$tmp/frame.png" "$pack/frame.png"
