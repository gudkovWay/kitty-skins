#version 320 es
precision highp float;
precision highp int;
precision highp sampler2D;
in vec2 b_uv;
layout(location=0) out vec4 weather_output;
uniform sampler2D tex; // immutable current-composite RAW, never the draw target
uniform sampler2D source_srgb;
uniform sampler2D processed_srgb;
uniform int transfer_function;
uniform vec2 transfer_range;
uniform vec2 working_range;
uniform float effect_alpha; // native window/workspace fade * protocol opacity
uniform float color_multiplier; // native post-CM dim, not a source-alpha change
uniform vec2 root_size;
uniform vec4 client_rect; // physical bottom-left origin and dimensions in root
// RAW stays in native target orientation; isolated weather targets have
// their own GL-bottom-left layout. Convert only SOURCE/LENS coordinates.
uniform vec2 source_flip;
uniform float rounding;
uniform float rounding_power;

vec3 to_srgb(vec3 x) {
    x = max(x, vec3(0.0));
    return mix(1.055 * pow(x, vec3(1.0 / 2.4)) - 0.055,
               12.92 * x, lessThanEqual(x, vec3(0.0031308)));
}
vec3 from_srgb(vec3 x) {
    x = max(x, vec3(0.0));
    return mix(pow((x + 0.055) / 1.055, vec3(2.4)),
               x / 12.92, lessThanEqual(x, vec3(0.04045)));
}
vec3 working_to_target(vec3 working) {
    if (transfer_function == 9 && all(equal(transfer_range, working_range))) return working;
    vec3 nits = from_srgb(working) * (working_range.y - working_range.x) + working_range.x;
    if (transfer_function == 0) return nits; // native LINEAR is absolute nits
    vec3 linear = (nits - transfer_range.x) / (transfer_range.y - transfer_range.x);
    if (transfer_function == 5) return linear; // native EXT_LINEAR range 0..80
    if (transfer_function == 2) return pow(max(linear, vec3(0.0)), vec3(1.0 / 2.2));
    if (transfer_function == 3) return pow(max(linear, vec3(0.0)), vec3(1.0 / 2.8));
    return to_srgb(linear);
}
float shape_coverage(vec2 source_uv) {
    vec2 local = source_uv * root_size - client_rect.xy;
    if (any(lessThan(local, vec2(0.0))) || any(greaterThanEqual(local, client_rect.zw))) return 0.0;
    if (rounding <= 0.0) return 1.0;
    // Installed Hyprland rounding.glsl.inc, in physical pixels. Its corner
    // coverage gates ONLY the contribution delta, never the source/mask taps.
    vec2 pixel = abs(local - client_rect.zw * 0.5);
    pixel -= client_rect.zw * 0.5 - rounding;
    pixel += vec2(1.0) / client_rect.zw;
    if (pixel.x + pixel.y <= rounding) return 1.0;
    float distance = pow(pow(pixel.x, rounding_power) +
                         pow(pixel.y, rounding_power), 1.0 / rounding_power);
    const float smoothing = 3.14159265358979323846 / 5.34665792551;
    if (distance > rounding + smoothing) return 0.0;
    return 1.0 - smoothstep(0.0, 1.0, (distance - rounding + smoothing) / (smoothing * 2.0));
}
void main() {
    // Pixel-centre fetches avoid filtering away SOURCE8/LENS8 dry equality.
    vec2 source_uv = mix(b_uv, vec2(1.0) - b_uv, source_flip);
    ivec2 pixel = ivec2(source_uv * root_size);
    vec4 source = texelFetch(source_srgb, pixel, 0);
    vec4 wet = texelFetch(processed_srgb, pixel, 0);
    if (all(equal(source, wet)) || source.a <= 0.0 || effect_alpha <= 0.0) discard;
    float coverage = shape_coverage(source_uv);
    if (coverage <= 0.0 || color_multiplier <= 0.0) discard;
    // Light and lens preserve the original alpha. Use that alpha for both
    // straight colours and keep it OUT of the native fade/protocol coefficient.
    vec3 dry_target = working_to_target(source.rgb / source.a);
    vec3 wet_target = working_to_target(wet.rgb / source.a);
    vec3 delta = source.a * effect_alpha * coverage * color_multiplier * (wet_target - dry_target);
    if (all(equal(delta, vec3(0.0)))) discard;
    vec4 raw = texelFetch(tex, ivec2(b_uv * root_size), 0);
    // Native CM precedes source-over in the destination representation:
    // R = a*F(dry) + (1-a)*B, R' = a*F(wet) + (1-a)*B.
    // Hence R' = R + a*(F(wet)-F(dry)). Convert both endpoints, NOT the signed
    // delta. Encoding decode(R)+linearDelta would change native nonlinear blend.
    weather_output = vec4(raw.rgb + delta, raw.a);
}
