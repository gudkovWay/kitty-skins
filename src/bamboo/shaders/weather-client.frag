#version 320 es
// Compile the external-sampler branch only when this GLES context exposes it.
// The ordinary sampler remains usable without the external-image extension.
#ifdef GL_OES_EGL_image_external_essl3
#extension GL_OES_EGL_image_external_essl3 : require
#endif
precision highp float;
precision highp int;
precision highp sampler2D;
#ifdef GL_OES_EGL_image_external_essl3
precision highp samplerExternalOES;
uniform samplerExternalOES client_external;
#endif
in vec2 b_uv;
layout(location=0) out vec4 weather_output;
uniform sampler2D client_rgba;
uniform int external_sampler;
uniform int opaque_source;
uniform int transfer_function;
uniform vec2 transfer_range;
uniform vec2 working_range;
uniform vec2 root_size;
uniform vec2 logical_client;
uniform vec4 client_rect; // bottom-left physical origin, physical dimensions
uniform mat3 client_uv;

vec3 from_srgb(vec3 x) {
    x = max(x, vec3(0.0));
    return mix(pow((x + 0.055) / 1.055, vec3(2.4)),
               x / 12.92, lessThanEqual(x, vec3(0.04045)));
}
vec3 to_srgb(vec3 x) {
    x = max(x, vec3(0.0));
    return mix(1.055 * pow(x, vec3(1.0 / 2.4)) - 0.055,
               12.92 * x, lessThanEqual(x, vec3(0.0031308)));
}
vec3 source_nits(vec3 x) {
    if (transfer_function == 0) return x; // native internal LINEAR is cd/m²
    vec3 linear = x;
    if (transfer_function == 2) linear = pow(max(x, vec3(0.0)), vec3(2.2));
    if (transfer_function == 3) linear = pow(max(x, vec3(0.0)), vec3(2.8));
    if (transfer_function == 9) linear = from_srgb(x);
    // EXT_LINEAR is normalised scRGB (native range 0..80 nits), NOT LINEAR.
    return linear * (transfer_range.y - transfer_range.x) + transfer_range.x;
}
void main() {
    vec2 point = (b_uv * root_size - client_rect.xy) / client_rect.zw;
    point.y = 1.0 - point.y;
    vec2 logical_point = point * logical_client;
    if (any(lessThan(logical_point, vec2(0.0))) ||
        any(greaterThanEqual(logical_point, logical_client))) {
        weather_output = vec4(0.0); // the synthetic 52-logical-pixel ring
        return;
    }
    // Native client UV is top-left. The matrix already includes protocol scale,
    // viewport and geometry; do not apply the framebuffer-texture Y flip here.
    vec2 uv = (client_uv * vec3(logical_point / logical_client, 1.0)).xy;
    vec4 source;
#ifdef GL_OES_EGL_image_external_essl3
    if (external_sampler != 0) source = texture(client_external, uv);
    else
#endif
    source = texture(client_rgba, uv);
    if (opaque_source != 0) source.a = 1.0; // RGBX/native opaque, never opacity
    if (source.a <= 0.0) {
        weather_output = vec4(0.0);
        return;
    }
    // Same-transfer/range identity avoids a needless pow round trip before 8-bit
    // staging. Neither compositor opacity nor native rounding enters this input.
    if (transfer_function == 9 && all(equal(transfer_range, working_range))) {
        weather_output = source;
        return;
    }
    vec3 nits = source_nits(source.rgb / source.a);
    vec3 working = to_srgb((nits - working_range.x) / (working_range.y - working_range.x));
    weather_output = vec4(working * source.a, source.a);
}
