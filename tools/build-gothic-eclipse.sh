#!/usr/bin/env bash
# Reproducible Gothic Eclipse atlas generation (manifest schema 2, hi-res master).
#
# Inputs:
#   assets/source/gothic_eclipse_minimal_idle.png      the pinned original (unchanged, checksum-pinned)
#   assets/source/gothic_eclipse_minimal_idle_2x.png   the 3072x2048 neural 2x master
#
# Outputs (each published only after the whole run succeeds):
#   exact.png         deterministic stripped 8-bit RGBA copy of the master with
#                     the two painted candle flames locally repaired. Outside
#                     the candle source rectangle it is the untouched
#                     flame-repaired master. The candle rectangle itself is
#                     cleared and holds ONLY an alpha-matted static candle +
#                     foreground stone cluster (silhouette polygons multiplied
#                     by the source alpha): no rectangular opaque backdrop or
#                     upper column behind the flames. Supplies the candle
#                     foreground in both modes; the frame uses adaptive.png.
#   adaptive.png      frame base derived from the flame-repaired master whose
#                     candle wax bodies, pool and hanging drips are erased by
#                     a bounded feathered local material replacement (neutral
#                     column from the master above, stone texture sampled
#                     +150px right below), while the surrounding source
#                     stones/carving stay intact: no static candle duplicates.
#                     Four packed neutral tile cells are overlaid in its
#                     unused aperture area. Each cell is a mirrored pair so
#                     its opposite edges are identical: flexible runs tile
#                     seamlessly. The manifest samples ONLY these cells for
#                     rails/shafts; corners, caps and non-candle ornaments
#                     come from the erased master.
#   candle-flames.png flame-only straight-alpha RGBA overlay (680x568) extracted
#                     from the UNTOUCHED master crop; the two flames the plugin
#                     animates on top of the static atlas.
#   candle-light.png  warm straight-alpha RGBA glow (680x568) bounded by radial
#                     falloff around the two wicks and by the candle material
#                     alpha, so no light lands in the transparent aperture.
#   candle-wax-mask.png wax visibility (680x568): source material alpha clipped
#                       above the foreground stone, leaving the frame over wax.
#
# The immutable original and the immutable 2x master are only ever read.
set -eu

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
src="$repo_root/assets/source/gothic_eclipse_minimal_idle.png"
master="$repo_root/assets/source/gothic_eclipse_minimal_idle_2x.png"
pack="$repo_root/assets/skins/gothic-eclipse"
skin_json="$pack/skin.json"

# Original-source pin: must stay byte-identical.
original_expected=4e8b2bd7c5233956747dc24c1540214996d9178714556786efb0b464bdacd0dd
actual="$(sha256sum "$src" | awk '{print $1}')"
if [ "$actual" != "$original_expected" ]; then
    echo "checksum mismatch: $src" >&2
    echo "expected $original_expected, got $actual" >&2
    exit 1
fi

if [ ! -f "$master" ]; then
    echo "missing master atlas: $master" >&2
    exit 1
fi

# Master checksum must match what skin.json declares as the source hash.
master_expected="$(grep -m1 '"sha256"' "$skin_json" | sed -n 's/.*"sha256" *: *"\([0-9a-fA-F]\{64\}\)".*/\1/p')"
if [ -z "$master_expected" ]; then
    echo "cannot parse source.sha256 from $skin_json" >&2
    exit 1
fi
master_actual="$(sha256sum "$master" | awk '{print $1}')"
if [ "$master_actual" != "$master_expected" ]; then
    echo "checksum mismatch: $master" >&2
    echo "expected (skin.json source.sha256) $master_expected, got $master_actual" >&2
    echo "regenerate the master or update skin.json once the final hash is known." >&2
    exit 1
fi

master_dims="$(magick identify -format '%w %h' "$master")"
if [ "$master_dims" != "3072 2048" ]; then
    echo "wrong master dimensions: expected 3072 2048, got '$master_dims'" >&2
    exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Strip metadata and date/time chunks, and force straight RGBA, so output bytes
# are deterministic. Cairo premultiplies on load.
magick_args=(-strip -define png:color-type=6 -define png:exclude-chunk=date,time)

# ---------------------------------------------------------------------------
# Live-candle recipe.
#
# The candle ornament is master rect [0,1480,680,568]. Its two painted flames
# are removed from both static atlases and re-emitted as an animated overlay.
# All coordinates below are local to that 680x568 crop.
#
#   flame boxes:  left  40x64+216+116   right 44x64+266+146
#   wick points:  left  (236,178)       right (287,206)
#
# Local repair uses a clone of the background exactly 64px ABOVE each flame:
#   left  source 216,52  ->  dest 216,116
#   right source 266,82  ->  dest 266,146
# Anchored on the untouched patch only, so the clone is never fed back into
# itself. The repair window is padded a couple of pixels beyond each flame box
# so the feathered edge never leaves a hard rectangle, and it stops one row
# above each wick so the candle top and wax are never touched.
# ---------------------------------------------------------------------------

# Untouched candle patch: every extraction samples this.
magick "$master" -crop 680x568+0+1480 +repage "$tmp/patch-orig.png"

# --- flame-only overlays (straight alpha) ----------------------------------
#
# RGB is the painted flame; alpha is a warm/luminance mask multiplied by a soft
# local shape. The shape is feathered on the top/left/right and hard-cut one row
# above the wick, so it stays tight to the flame instead of becoming a deforming
# rectangle over the dark rock, and it never reaches the wax. The mask also
# preserves the source alpha (the trailing `*a`).
flame_left_crop=40x64+216+116
flame_right_crop=44x64+266+146

magick "$tmp/patch-orig.png" -crop "$flame_left_crop" +repage "$tmp/flame-left-rgb.png"
magick "$tmp/patch-orig.png" -crop "$flame_right_crop" +repage "$tmp/flame-right-rgb.png"

magick "$tmp/flame-left-rgb.png" -fx 'mx=max(r-g,0); lum=0.2126*r+0.7152*g+0.0722*b; gate=max(0,min(1,mx*8+max(0,lum-0.70)*3)); gate*max(0,min(1,min(i,w-1-i)/2))*max(0,min(1,j/2))*max(0,min(1,(61-j)/2))*a' -alpha off "$tmp/flame-left-mask.png"
magick "$tmp/flame-right-rgb.png" -fx 'mx=max(r-g,0); lum=0.2126*r+0.7152*g+0.0722*b; gate=max(0,min(1,mx*8+max(0,lum-0.70)*3)); gate*max(0,min(1,min(i,w-1-i)/2))*max(0,min(1,j/2))*max(0,min(1,(59-j)/2))*a' -alpha off "$tmp/flame-right-mask.png"

magick "$tmp/flame-left-rgb.png" -alpha off \
    \( "$tmp/flame-left-mask.png" -alpha off \) \
    -compose CopyOpacity -composite "${magick_args[@]}" "$tmp/flame-left.png"
magick "$tmp/flame-right-rgb.png" -alpha off \
    \( "$tmp/flame-right-mask.png" -alpha off \) \
    -compose CopyOpacity -composite "${magick_args[@]}" "$tmp/flame-right.png"

magick -size 680x568 xc:none \
    "$tmp/flame-left.png" -geometry +216+116 -composite \
    "$tmp/flame-right.png" -geometry +266+146 -composite \
    "${magick_args[@]}" "$tmp/candle-flames.png"

# --- feathered replacement mask --------------------------------------------
#
# Analytic mask (no blur): 1 over each flame box, easing to 0 a few pixels
# outside it, with a hard cut one row above each wick (y177 left, y205 right).
# `lb`/`rb` are that cut; the rest is the soft window. Built as grayscale RGB
# and turned into an alpha mask with CopyOpacity.
magick -size 680x568 xc:black -fx 'lval=min(min((i-213)/3,(258-i)/3),(j-113)/3); rval=min(min((i-263)/3,(312-i)/3),(j-143)/3); lc=max(0,min(1,lval)); rc=max(0,min(1,rval)); lb=max(0,min(1,(177.5-j)*2)); rb=max(0,min(1,(205.5-j)*2)); max(lc*lb,rc*rb)' -alpha off "$tmp/mask-gray.png"
magick -size 680x568 xc:none \
    \( "$tmp/mask-gray.png" -alpha off \) \
    -compose CopyOpacity -composite "$tmp/mask-full.png"

# --- clone the background above each flame ---------------------------------
#
# Repair windows: left 46x65+213+113, right 50x63+263+143 (each 64px below its
# clone source). The clone keeps its own alpha, then is multiplied by the
# feather, so a transparent clone pixel carries alpha 0.
left_win=46x65+213+113
right_win=50x63+263+143
left_src=46x65+213+49
right_src=50x63+263+79

magick "$tmp/patch-orig.png" -crop "$left_src" +repage "$tmp/clone-left-raw.png"
magick "$tmp/patch-orig.png" -crop "$right_src" +repage "$tmp/clone-right-raw.png"

magick "$tmp/mask-gray.png" -crop "$left_win" +repage "$tmp/feather-left.png"
magick "$tmp/mask-gray.png" -crop "$right_win" +repage "$tmp/feather-right.png"

magick "$tmp/clone-left-raw.png" -alpha extract "$tmp/clone-left-A.png"
magick "$tmp/clone-left-A.png" "$tmp/feather-left.png" -compose Multiply -composite "$tmp/clone-left-mask.png"
magick "$tmp/clone-left-raw.png" -alpha off \
    \( "$tmp/clone-left-mask.png" -alpha off \) \
    -compose CopyOpacity -composite "$tmp/clone-left.png"

magick "$tmp/clone-right-raw.png" -alpha extract "$tmp/clone-right-A.png"
magick "$tmp/clone-right-A.png" "$tmp/feather-right.png" -compose Multiply -composite "$tmp/clone-right-mask.png"
magick "$tmp/clone-right-raw.png" -alpha off \
    \( "$tmp/clone-right-mask.png" -alpha off \) \
    -compose CopyOpacity -composite "$tmp/clone-right.png"

# Clear the painted flames (DstOut honours the mask alpha, so dissolving the
# destination to transparent), then lay the masked clones back with a feathered
# Over. Transparent clone pixels therefore genuinely clear the old flame instead
# of leaving a phantom fire underneath.
magick "$tmp/patch-orig.png" "$tmp/mask-full.png" -compose DstOut -composite "$tmp/patch-cleared.png"
magick "$tmp/patch-cleared.png" \
    "$tmp/clone-left.png" -geometry +213+113 -compose Over -composite \
    "$tmp/clone-right.png" -geometry +263+143 -compose Over -composite \
    "$tmp/patch-clean.png"

# --- static candle + foreground stone matte (exact atlas) -------------------
#
# The exact atlas must hold the static candle and the foreground stones as an
# alpha-matted cutout, NOT the opaque source rectangle (which also contains the
# upper column/backdrop behind the flames). Coverage is a union of polygons
# following the cluster's upper silhouette (main envelope) plus the two candle
# wax bodies. The matte is built from the FLAME-CLEAN patch, so no flame pixels
# survive in the top rows where the bodies meet the (animated) flames; its
# alpha is the polygon coverage multiplied by the ORIGINAL patch alpha, so RGB
# and the stone silhouette are preserved exactly.
magick -size 680x568 xc:black -fill white \
    -draw "polygon 0,568 0,442 34,399 39,381 39,260 52,252 53,176 66,155 116,155 140,168 155,192 160,230 208,231 217,273 256,276 264,210 308,207 318,212 376,211 391,222 395,255 510,255 529,268 532,323 591,323 614,338 680,325 680,568" \
    -draw "polygon 217,175 249,175 254,185 254,277 217,279" \
    -draw "polygon 263,204 310,204 314,278 263,279" \
    -alpha off "$tmp/fg-cov.png"
magick "$tmp/fg-cov.png" \
    \( "$tmp/patch-orig.png" -alpha extract \) \
    -compose Multiply -composite "$tmp/fg-mask.png"
magick "$tmp/patch-clean.png" -alpha off \
    \( "$tmp/fg-mask.png" -alpha off \) \
    -compose CopyOpacity -composite "$tmp/fg-foreground.png"

# --- erase static wax for the adaptive frame base ---------------------------
#
# The adaptive base must keep the surrounding stones but lose the static wax:
# bodies, pool and persistent hanging drips are replaced by local background
# material. The mask is a union of the two body rectangles and the hanging wax
# silhouette polygon, dilated 1px then softly blurred, so the original wax is
# still fully covered while the replacement feather blends at the edges.
magick -size 680x568 xc:black -fill white \
    -draw "rectangle 216,175 255,280" \
    -draw "rectangle 262,203 314,281" \
    -draw "polygon 207,275 219,271 253,272 260,277 272,272 278,276 329,274 332,282 328,309 315,310 312,293 311,353 308,371 297,371 294,294 277,294 277,369 272,379 263,378 259,296 244,297 244,326 242,344 232,345 229,330 227,297 207,293" \
    -alpha off "$tmp/wax-bin.png"
magick "$tmp/wax-bin.png" -morphology Dilate Diamond -blur 0x0.5 -alpha off "$tmp/wax-mask.png"

# Replacement material: above y271, a neutral column from the master before
# the candle area; below y271, original stone sampled 150px to the right.
# Both crops occupy disjoint rows. All masked pixels fit inside their coverage.
magick -size 680x568 xc:none \
    \( "$master" -crop 680x271+0+912 +repage \) \
    -geometry +0+0 -compose Over -composite \
    \( "$tmp/patch-orig.png" -crop 530x297+150+271 +repage \) \
    -geometry +0+271 -compose Over -composite "$tmp/wax-repl.png"
# Multiply the replacement's own alpha by the feathered mask, then clear the
# masked area of the flame-clean patch (DstOut) before the alpha Over — a
# transparent replacement pixel can never leave an original behind.
magick "$tmp/wax-repl.png" -alpha extract \
    "$tmp/wax-mask.png" -compose Multiply -composite "$tmp/wax-repl-A.png"
magick "$tmp/wax-repl.png" -alpha off \
    \( "$tmp/wax-repl-A.png" -alpha off \) \
    -compose CopyOpacity -composite "$tmp/wax-repl-masked.png"
magick "$tmp/patch-clean.png" \
    \( "$tmp/wax-mask.png" -alpha copy \) \
    -compose DstOut -composite "$tmp/patch-erased.png"
magick "$tmp/patch-erased.png" \
    "$tmp/wax-repl-masked.png" -compose Over -composite \
    "$tmp/patch-adaptive.png"

# --- warm candle light -----------------------------------------------------
#
# Bounded radial falloff around both wick points, scaled by the CLEANED
# material's own luminance (so the glow follows stone/wax detail, not a flat
# disc) and multiplied by its alpha (so nothing lands in the transparent
# aperture). Peak straight alpha is clamped to 0.18; the plugin multiplies this
# by its own per-flame phase strengths.
magick "$tmp/patch-clean.png" -fx 'dl=sqrt((i-236)*(i-236)+(j-178)*(j-178)); dr=sqrt((i-287)*(i-287)+(j-206)*(j-206)); fall=max(0,1-min(dl,dr)/150); lum=0.2126*r+0.7152*g+0.0722*b; 0.18*fall*fall*(0.25+0.75*lum)*a' -alpha off "$tmp/light-cov.png"
magick -size 680x568 xc:'#ff9a3c' \
    \( "$tmp/light-cov.png" -alpha off \) \
    -compose CopyOpacity -composite "${magick_args[@]}" "$tmp/candle-light.png"

# --- wax visibility behind the foreground stone ----------------------------
#
# These vertices follow the upper silhouette of the bottom stone in the
# original candle crop. Keep the whole stone opaque to wax, including its top
# bevel, rather than fading the falling bead before it reaches the frame.
# The three streams lie at x237, x268 and x302; their paths continue below this
# contour in the shader. Painted dry wax stays intact underneath the flow.
magick -size 680x568 xc:black -fill white \
    -draw "polygon 194,280 368,280 368,417 359,407 344,400 339,397 332,395 220,395 209,399 194,411" \
    -alpha off "$tmp/wax-visible.png"
magick "$tmp/patch-orig.png" -alpha extract \
    "$tmp/wax-visible.png" -compose Multiply -composite "$tmp/wax-coverage.png"
magick -size 680x568 xc:white \
    \( "$tmp/wax-coverage.png" -alpha off \) \
    -compose CopyOpacity -composite "${magick_args[@]}" "$tmp/candle-wax-mask.png"

# Rebuild the full flame-repaired master once; the exact atlas is cut from it.
magick "$master" \
    \( -size 680x568 xc:white \) -geometry +0+1480 -compose DstOut -composite \
    -compose Over "$tmp/patch-clean.png" -geometry +0+1480 -composite \
    "$tmp/master-clean.png"

# --- static atlases --------------------------------------------------------
#
# exact.png: flame-repaired master OUTSIDE the candle rectangle. The whole
# candle rectangle is cleared (DstOut) and only the alpha-matted static
# candle + foreground stone cluster is composited back at its original
# position, so no opaque upper/background rectangle survives behind the
# flames. Built from the flame-repaired master, NEVER from the wax-erased
# adaptive base.
magick "$tmp/master-clean.png" \
    \( -size 680x568 xc:white \) -geometry +0+1480 -compose DstOut -composite \
    -compose Over "$tmp/fg-foreground.png" -geometry +0+1480 -composite \
    "${magick_args[@]}" "$tmp/exact.png"

# adaptive.png: flame-repaired AND wax-erased master (stones in the candle
# area kept; no static candle duplicates). Built the same clear-then-Over way
# from patch-adaptive.
magick "$master" \
    \( -size 680x568 xc:white \) -geometry +0+1480 -compose DstOut -composite \
    -compose Over "$tmp/patch-adaptive.png" -geometry +0+1480 -composite \
    "$tmp/master-adaptive.png"

# Neutral mirrored tile cells, packed into the unused aperture of the erased
# master. Tile coordinates below are already in 3072x2048 master space (2x the
# base 1536x1024 recipe). Mirroring (-flop horizontal, -flip vertical) makes
# the opposite tile edges identical, so repeat runs are seamless.
#
# top rail:    crop 512x450+560+0   + mirrored -> 1024x450  at +400+500
# bottom rail: crop 348x246+1904+1802 + mirrored -> 696x246 at +1500+1000
# left column: crop 322x240+0+1070  + mirrored -> 322x480   at +400+1000
# right column: crop 324x240+2748+1070 + mirrored -> 324x480 at +800+1000

magick "$tmp/master-adaptive.png" -crop 512x450+560+0 +repage \( +clone -flop \) +append +repage "$tmp/tile-rail-top.png"
magick "$tmp/master-adaptive.png" -crop 348x246+1904+1802 +repage \( +clone -flop \) +append +repage "$tmp/tile-rail-bottom.png"
magick "$tmp/master-adaptive.png" -crop 322x240+0+1070 +repage \( +clone -flip \) -append +repage "$tmp/tile-column-left.png"
magick "$tmp/master-adaptive.png" -crop 324x240+2748+1070 +repage \( +clone -flip \) -append +repage "$tmp/tile-column-right.png"

# Clear only the packed cells with an explicit full-size mask, then composite
# their pixels over the cleared slots. This keeps all original corner/cap/
# ornament pixels outside the four cells untouched, including their alpha.
magick "$tmp/master-adaptive.png" \
    \( -size 3072x2048 xc:none -fill white \
       -draw "rectangle 400,500 1423,949" \
       -draw "rectangle 1500,1000 2195,1245" \
       -draw "rectangle 400,1000 721,1479" \
       -draw "rectangle 800,1000 1123,1479" \) \
    -compose DstOut -composite -compose Over \
    "$tmp/tile-rail-top.png" -geometry +400+500 -composite \
    "$tmp/tile-rail-bottom.png" -geometry +1500+1000 -composite \
    "$tmp/tile-column-left.png" -geometry +400+1000 -composite \
    "$tmp/tile-column-right.png" -geometry +800+1000 -composite \
    "${magick_args[@]}" "$tmp/adaptive.png"

mv "$tmp/exact.png" "$pack/exact.png"
mv "$tmp/adaptive.png" "$pack/adaptive.png"
mv "$tmp/candle-flames.png" "$pack/candle-flames.png"
mv "$tmp/candle-light.png" "$pack/candle-light.png"
mv "$tmp/candle-wax-mask.png" "$pack/candle-wax-mask.png"
