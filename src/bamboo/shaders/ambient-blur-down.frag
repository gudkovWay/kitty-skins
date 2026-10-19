#version 320 es
precision highp float;
precision highp sampler2D;
in vec2 b_uv;
uniform sampler2D tex;
uniform vec2 half_pixel;
layout(location=0) out vec4 result;

// ShojiWM backdrop_blur_down.frag: radius=4, half_pixel=0.5/DST size.
// Filter premultiplied sRGB RGBA, including alpha; do not linearize here.
void main() {
    vec2 o = half_pixel * 4.0;
    vec4 sum = texture(tex, b_uv) * 4.0;
    sum += texture(tex, b_uv + vec2(-o.x, -o.y));
    sum += texture(tex, b_uv + vec2( o.x, -o.y));
    sum += texture(tex, b_uv + vec2(-o.x,  o.y));
    sum += texture(tex, b_uv + vec2( o.x,  o.y));
    result = sum / 8.0;
}
