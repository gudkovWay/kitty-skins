#version 320 es
precision highp float;
precision highp int;
precision highp sampler2D;
in vec2 b_uv;
uniform sampler2D tex;
// GL texture UV of the clipped source rectangle: bottom-left storage to
// top-left authored orientation. The caller includes source-FB projection offsets.
uniform vec4 source_uv;
uniform mat3 convert_primaries;
uniform int source_tf;
uniform int source_rgbx;
uniform vec2 source_range;
uniform vec2 target_range;
layout(location=0) out vec4 result;

vec3 from_srgb(vec3 c) {
    c = max(c, vec3(0.0));
    return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), greaterThan(c, vec3(0.04045)));
}
vec3 to_srgb(vec3 c) {
    c = max(c, vec3(0.0));
    return mix(12.92 * c, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, greaterThan(c, vec3(0.0031308)));
}
void main() {
    vec4 raw = texture(tex, source_uv.xy + b_uv * source_uv.zw);
    if (source_rgbx != 0) raw.a = 1.0;
    if (raw.a <= 0.0) { result = vec4(0.0); return; }
    vec3 c = raw.rgb / raw.a;
    // Match Hyprland CM's transfer/range order, but terminate in Shoji's
    // normalized sRGB RGBA8 BEFORE the first filtering/downsample operation.
    vec3 linear;
    if (source_tf == 0 || source_tf == 5) linear = c;
    else if (source_tf == 9) linear = from_srgb(c);
    else if (source_tf == 10) linear = sign(c) * from_srgb(abs(c));
    else linear = pow(max(c, vec3(0.0)), vec3(2.2)); // caller admits GAMMA22 only
    vec3 nits = convert_primaries * linear;
    if (source_tf != 0) nits = nits * (source_range.y - source_range.x) + source_range.x;
    vec3 srgb = to_srgb((nits - target_range.x) / (target_range.y - target_range.x));
    // Shoji capture and all blur levels are ABGR8888. Saturate in straight
    // space before premultiplication so the entire chain remains RGB<=alpha.
    result = vec4(clamp(srgb, 0.0, 1.0) * raw.a, raw.a);
}
