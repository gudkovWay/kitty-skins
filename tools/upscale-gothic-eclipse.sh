#!/usr/bin/env bash
# Reproducible neural 2x master for Gothic Eclipse.
#
# Produces assets/source/gothic_eclipse_minimal_idle_2x.png (3072x2048 RGBA)
# from the pinned original. This is a true neural upscale: the RGB channels are
# inferred by Real-ESRGAN, while the alpha channel is taken from the original
# source, resized 200% with the Triangle filter. The model NEVER sees or
# generates transparency -- it is fed the source flattened over black, so no
# opaque aperture or invented cutout can leak into the result.
#
# Requirements (nothing is downloaded or installed by this script):
#   * ImageMagick 7 (`magick`).
#   * Real-ESRGAN ncnn Vulkan v0.2.0 and realesr-animevideov3-x2 weights.
#     The official Real-ESRGAN v0.2.5.0 release bundles both in
#     realesrgan-ncnn-vulkan-20220424-ubuntu.zip.
#
# Usage:
#   tools/upscale-gothic-eclipse.sh
#   REAL_ESRGAN_BIN=/opt/realesrgan/realesrgan-ncnn-vulkan \
#   REAL_ESRGAN_MODELS=/opt/realesrgan/models \
#   tools/upscale-gothic-eclipse.sh
#
# Environment:
#   REAL_ESRGAN_BIN     executable name or path (default realesrgan-ncnn-vulkan)
#   REAL_ESRGAN_MODELS  models directory (default: `models` next to the binary)
#   REAL_ESRGAN_GPU     Vulkan device index; passed as -g only when set, so the
#                       tool's own default is used otherwise
#
# Model/CLI recorded for provenance:
#   realesrgan-ncnn-vulkan -i in.png -o out.png -s 2 -t 128 -j 1:1:1 \
#       -n realesr-animevideov3 -m <models> [-g <gpu>]
#
# Byte identity across different GPU drivers is NOT promised: inference output
# depends on the Vulkan backend. PNG metadata/date chunks are stripped for
# otherwise-deterministic output bytes.
set -eu

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
src="$repo_root/assets/source/gothic_eclipse_minimal_idle.png"
out="$repo_root/assets/source/gothic_eclipse_minimal_idle_2x.png"
expected=4e8b2bd7c5233956747dc24c1540214996d9178714556786efb0b464bdacd0dd

src_w=1536
src_h=1024
out_w=$((src_w * 2))
out_h=$((src_h * 2))

die() {
    echo "$*" >&2
    exit 1
}

# --- toolchain -------------------------------------------------------------

command -v magick >/dev/null 2>&1 || die "ImageMagick 7 (magick) not found"
case "$(magick -version)" in
    "Version: ImageMagick 7"*) ;;
    *) die "ImageMagick 7 required (found: $(magick -version | head -1))" ;;
esac

real_esrgan_bin="${REAL_ESRGAN_BIN:-realesrgan-ncnn-vulkan}"
real_esrgan_path="$(command -v "$real_esrgan_bin" || true)"
[ -n "$real_esrgan_path" ] || die \
    "Real-ESRGAN binary '$real_esrgan_bin' not found; set REAL_ESRGAN_BIN"

models="${REAL_ESRGAN_MODELS:-$(dirname "$real_esrgan_path")/models}"
[ -d "$models" ] || die "Real-ESRGAN models directory not found: $models"
for f in realesr-animevideov3-x2.param realesr-animevideov3-x2.bin; do
    [ -f "$models/$f" ] || die "missing model weights: $models/$f"
done

gpu_args=()
if [ -n "${REAL_ESRGAN_GPU:-}" ]; then
    gpu_args=(-g "$REAL_ESRGAN_GPU")
fi

# --- input -----------------------------------------------------------------

[ -f "$src" ] || die "missing source image: $src"
actual="$(sha256sum "$src" | awk '{print $1}')"
if [ "$actual" != "$expected" ]; then
    die "checksum mismatch: $src (expected $expected, got $actual)"
fi

dims="$(magick identify -format '%wx%h' "$src")"
[ "$dims" = "${src_w}x${src_h}" ] || die \
    "unexpected source dimensions: $src is $dims, expected ${src_w}x${src_h}"

# --- work ------------------------------------------------------------------

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Strip metadata and date/time chunks, and force straight RGBA, so output bytes
# are deterministic.
magick_args=(-strip -define png:color-type=6 -define png:exclude-chunk=date,time)

# Flat opaque RGB for inference; the original alpha is restored separately.
magick "$src" -background black -alpha remove -alpha off "$tmp/rgb.png"

"$real_esrgan_path" \
    -i "$tmp/rgb.png" -o "$tmp/rgb2x.png" \
    -s 2 -t 128 -j 1:1:1 -n realesr-animevideov3 -m "$models" \
    -f png "${gpu_args[@]}" \
    || die "Real-ESRGAN inference failed"

dims="$(magick identify -format '%wx%h' "$tmp/rgb2x.png")"
[ "$dims" = "${out_w}x${out_h}" ] || die \
    "unexpected inference dimensions: got $dims, expected ${out_w}x${out_h}"

# Alpha comes from the original, resized independently; the model never
# invents transparency or changes the aperture.
magick "$src" -alpha extract -filter Triangle -resize 200% "$tmp/alpha2x.png"

dims="$(magick identify -format '%wx%h' "$tmp/alpha2x.png")"
[ "$dims" = "${out_w}x${out_h}" ] || die \
    "unexpected alpha dimensions: got $dims, expected ${out_w}x${out_h}"

# Combine inferred RGB with the source-derived alpha.
magick \
    \( "$tmp/rgb2x.png" -alpha on \) \
    \( "$tmp/alpha2x.png" -alpha off \) \
    -compose CopyOpacity -composite -depth 8 \
    "${magick_args[@]}" "$tmp/master.png"

dims="$(magick identify -format '%wx%h' "$tmp/master.png")"
[ "$dims" = "${out_w}x${out_h}" ] || die \
    "unexpected master dimensions: got $dims, expected ${out_w}x${out_h}"

# Publish only after every check passed; a failed run leaves any existing
# master untouched.
mv "$tmp/master.png" "$out"
