#version 320 es
precision highp float;
precision highp sampler2D;
in vec2 b_uv;
uniform sampler2D tex;
uniform vec2 half_pixel;
layout(location=0) out vec4 result;

// ShojiWM backdrop_blur_up.frag: radius=4, half_pixel=0.5/SRC size.
void main() {
    vec2 o = half_pixel * 4.0;
    vec4 sum = vec4(0.0);
    sum += texture(tex, b_uv + vec2(-o.x * 2.0, 0.0));
    sum += texture(tex, b_uv + vec2( o.x * 2.0, 0.0));
    sum += texture(tex, b_uv + vec2(0.0, -o.y * 2.0));
    sum += texture(tex, b_uv + vec2(0.0,  o.y * 2.0));
    sum += texture(tex, b_uv + vec2(-o.x,  o.y)) * 2.0;
    sum += texture(tex, b_uv + vec2( o.x,  o.y)) * 2.0;
    sum += texture(tex, b_uv + vec2(-o.x, -o.y)) * 2.0;
    sum += texture(tex, b_uv + vec2( o.x, -o.y)) * 2.0;
    result = sum / 12.0;
}
