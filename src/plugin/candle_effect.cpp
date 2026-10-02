#include "candle_effect.hpp"

#include <algorithm>
#include <array>

#include <GLES3/gl32.h>

#include <hyprland/src/render/Framebuffer.hpp>
#include <hyprland/src/render/OpenGL.hpp>
#include <hyprland/src/render/Renderer.hpp>

namespace kitty_skins::plugin {
using Render::GL::CHyprOpenGLImpl;
using Render::GL::g_pHyprOpenGL;
namespace {

constexpr double kTickSeconds = 0.034;

// GLSL ES 3.x sources. Coordinates: the fragment shader works in effect-space
// pixel coordinates with y growing downwards (PNG row order, matching how the
// asset textures were uploaded). gl_FragCoord-based reconstruction intentionally
// yields y=0 at the framebuffer texture's first sampled row, so the cached
// texture composites with the same orientation the ordinary renderTexture uses
// for the uploaded PNGs.
constexpr const char* VERTEX_SRC = R"(#version 320 es
precision highp float;
out vec2 v_ndc;
void main() {
    // Attribute-less fullscreen triangle: (-1,-1), (3,-1), (-1,3).
    vec2 p = vec2(gl_VertexID == 1 ? 3.0 : -1.0, gl_VertexID == 2 ? 3.0 : -1.0);
    v_ndc = p;
    gl_Position = vec4(p, 0.0, 1.0);
}
)";

constexpr const char* FRAGMENT_SRC = R"(#version 320 es
precision highp float;

in vec2 v_ndc;
out vec4 fragColor;

uniform sampler2D u_flames;
uniform sampler2D u_light;
uniform sampler2D u_waxMask; // RGBA visibility mask; only alpha is used

uniform vec2  u_size;      // effect size in source pixels (680x568)
uniform vec4  u_rect[2];   // flame source rects: xy = top-left, zw = size, y down
uniform vec2  u_wick[2];   // wick anchors, source pixels, y down
uniform float u_phase[2];  // independent phase offsets
uniform float u_time;      // seconds
uniform int   u_waxCount;  // 0 disables wax entirely (no mask sampling)
uniform vec4  u_waxStreams[4]; // x, startY, endY, width (source px, y down)
uniform vec2  u_waxTiming[4];  // period (s), phase [0,1)

const float kTipShift     = 4.0; // max lateral tip displacement, source px
const float kHeightSwing  = 0.08; // +-8% flame height variation
const float kExpandRadius = 9.0;  // destination evaluation padding, source px

// Smooth two-term sine mixture per flame: not obviously periodic, C1-continuous.
float flutter(float t, float phase) {
    return 0.62 * sin(11.94 * t + u_time * 3.1 + phase) + 0.38 * sin(23.12 * t - u_time * 4.7 + phase * 1.73);
}

float heightWave(float phase) {
    return 0.7 * sin(u_time * 1.3 + phase * 2.11) + 0.3 * sin(u_time * 2.9 - phase * 3.37);
}

// Flicker multiplier in [0.92, 1.08]-ish, subtle.
float flicker(float phase) {
    return 1.0 + 0.05 * sin(u_time * 5.3 + phase) + 0.03 * sin(u_time * 8.9 - phase * 1.61);
}

// Inverse-warped sample of one flame: stable at the wick, lateral displacement
// growing towards the tip, vertical scale oscillation about the wick.
vec4 sampleFlame(int i, vec2 local) {
    vec4  rect  = u_rect[i];
    float wickY = u_wick[i].y;
    float phase = u_phase[i];

    // t: 0 at the wick, 1 at the tip (flames burn upwards, i.e. -y).
    float t = clamp((wickY - local.y) / rect.w, 0.0, 1.0);
    float envelope = t * t * (3.0 - 2.0 * t); // smoothstep, zero slope at wick

    float dx     = kTipShift * envelope * flutter(t, phase);
    float hscale = 1.0 + kHeightSwing * heightWave(phase);

    // Height variation stretches the flame about the wick; inverse-map back.
    vec2 src = vec2(local.x - dx, wickY - (wickY - local.y) / hscale);

    // Strictly inside this flame's own source rect; outside is transparent.
    if (src.x < rect.x || src.y < rect.y || src.x >= rect.x + rect.z || src.y >= rect.y + rect.w)
        return vec4(0.0);

    return textureLod(u_flames, src / u_size, 0.0);
}

// One flowing wax stream: a rounded elongated bead falling monotonically from
// the drip tip (startY) past endY, with a thin trailing tail above the bead.
// Matte cream/peach shading, premultiplied; no glow component.
const float kTailLength = 35.0; // trailing tail length, source px

vec4 waxBead(vec4 st, vec2 timing, vec2 local) {
    float x = st.x, y0 = st.y, y1 = st.z, width = st.w;
    float period = timing.x, phase = timing.y;

    // Most fragments are nowhere near a stream; skip its timing and shading.
    float halfW = 0.5 * width + 1.5;
    if (local.x < x - halfW || local.x > x + halfW || local.y < y0)
        return vec4(0.0);

    // Monotonic travel: by p=1 the head is beadLen + tail below endY, so the
    // whole bead and tail advance fully behind the foreground stone mask
    // before the cycle resets. Per-stream phase avoids synchronized resets.
    float p      = fract(u_time / period + phase);
    float beadLen = 2.0 * width;
    float yHead   = y0 + p * ((y1 - y0) + beadLen + kTailLength);

    float yTop = max(y0, yHead - beadLen - kTailLength);
    if (local.y < yTop || local.y > yHead)
        return vec4(0.0);

    // Entry ramp at the drip tip hides the cycle reset; exit relies purely on
    // mask occlusion (wax is already behind the stone), so nothing fades out
    // prematurely near the visible foreground.
    float ramp = smoothstep(0.0, 0.07, p);

    float a = 0.0;
    float s = 0.0; // lateral shade parameter, ~[-1, 1]

    if (local.y >= yHead - beadLen) {
        // Rounded elongated bead: full width body, quarter-round caps.
        float u      = clamp((yHead - local.y) / beadLen, 0.0, 1.0);
        float capLen = 0.5 * width;
        float du     = min(u, 1.0 - u) * beadLen;
        float rf     = 1.0;
        if (du < capLen) {
            float t = 1.0 - du / capLen;
            rf = sqrt(max(0.0, 1.0 - t * t));
        }
        float r = 0.5 * width * rf;
        s = (local.x - x) / max(r, 0.001);
        a = 1.0 - smoothstep(0.65, 1.0, abs(s));
    } else {
        // Thin trailing tail above the bead, tapering towards the drip tip;
        // the taper also produces the natural entry as the bead departs.
        float ty = clamp((local.y - yTop) / kTailLength, 0.0, 1.0);
        float r  = 0.5 * width * (0.28 + 0.20 * ty);
        s = (local.x - x) / max(r, 0.001);
        a = (1.0 - smoothstep(0.6, 1.0, abs(s))) * smoothstep(0.0, 0.3, ty);
    }

    a *= ramp;
    if (a <= 0.003)
        return vec4(0.0);

    // Matte wax shading: cream highlight left of centre, darker peach right
    // edge, soft specular streak.
    float sh  = 0.5 + 0.5 * s;
    vec3  col = mix(vec3(0.96, 0.87, 0.72), vec3(0.82, 0.58, 0.40), smoothstep(0.45, 1.0, sh));
    col += vec3(0.10) * (1.0 - smoothstep(0.0, 0.3, abs(s + 0.35)));

    return vec4(min(col * a, vec3(a)), a);
}

void main() {
    // Effect-space pixel coords, PNG row order (y down, x right).
    vec2 local = (v_ndc * 0.5 + 0.5) * u_size;

    float f0 = flicker(u_phase[0]);
    float f1 = flicker(u_phase[1]);

    // Warm glow: sample the premultiplied light atlas and modulate it
    // spatially by inverse-distance contributions from both wicks and the
    // matching per-flame flicker, so the glow breathes with the flames.
    vec4 glow = textureLod(u_light, local / u_size, 0.0);
    float d0 = length(local - u_wick[0]);
    float d1 = length(local - u_wick[1]);
    float w0 = 1.0 / (1.0 + d0 * d0 / (46.0 * 46.0));
    float w1 = 1.0 / (1.0 + d1 * d1 / (46.0 * 46.0));
    float wsum = w0 + w1;
    float breathe = wsum > 0.0001 ? (f0 * w0 + f1 * w1) / wsum : 1.0;
    float reach = clamp(wsum * 1.6, 0.0, 1.0);
    glow *= (0.82 + 0.18 * breathe) * (0.35 + 0.65 * reach);

    // Premultiplied wax OVER the glow. Streams evaluated with early bounding;
    // the mask is sampled at most once and only when some wax is present.
    vec4 base = glow;
    if (u_waxCount > 0) {
        vec4 wax = vec4(0.0);
        bool hit = false;
        for (int i = 0; i < 4; ++i) {
            if (i >= u_waxCount)
                break;
            vec4 c = waxBead(u_waxStreams[i], u_waxTiming[i], local);
            if (c.a > 0.0) {
                wax = c + wax * (1.0 - c.a);
                hit = true;
            }
        }
        if (hit) {
            float m = textureLod(u_waxMask, local / u_size, 0.0).a;
            wax *= m; // mask alpha multiplies wax RGBA only
        }
        base = wax + glow * (1.0 - wax.a);
    }

    // Flames: only evaluate inside each destination rect expanded a few
    // pixels so a moving tip is never clipped by the static source bounds.
    vec4 flame = vec4(0.0);
    for (int i = 0; i < 2; ++i) {
        vec4 rect = u_rect[i];
        vec4 expanded = vec4(rect.xy - vec2(kExpandRadius, kExpandRadius * 0.5), rect.zw + vec2(2.0 * kExpandRadius, kExpandRadius * 1.5));
        expanded = clamp(expanded, vec4(0.0), vec4(u_size, u_size));
        if (local.x < expanded.x || local.y < expanded.y || local.x >= expanded.x + expanded.z || local.y >= expanded.y + expanded.w)
            continue;

        vec4 f = sampleFlame(i, local);
        // Subtle brightness modulation; keep the premultiplied invariant
        // rgb <= a so normal SDR compositing stays valid.
        float br = i == 0 ? f0 : f1;
        f.rgb = min(f.rgb * br, f.a);
        flame = flame + f * (1.0 - flame.a);
    }

    // Premultiplied OVER: flames composite over the wax-over-glow result.
    fragColor = flame + base * (1.0 - flame.a);
}
)";

// Saves every piece of context state this renderer touches on construction and
// restores it on destruction, including all early returns via RAII.
struct GlStateGuard {
    GLint    drawFbo = 0, readFbo = 0;
    GLint    viewport[4] = {};
    GLint    program = 0;
    GLint    vao = 0, arrayBuffer = 0;
    GLint    activeTexture = GL_TEXTURE0;
    GLint    tex0 = 0, tex1 = 0, tex2 = 0;
    GLboolean blend = GL_FALSE;
    GLint     blendSrcRgb = 0, blendDstRgb = 0, blendSrcAlpha = 0, blendDstAlpha = 0;
    GLint     blendEqRgb = 0, blendEqAlpha = 0;
    GLboolean scissor = GL_FALSE;
    GLint     scissorBox[4] = {};
    GLboolean stencil = GL_FALSE, depthTest = GL_FALSE, cullFace = GL_FALSE;
    GLboolean colorMask[4] = {};

    GlStateGuard() {
        glGetIntegerv(GL_DRAW_FRAMEBUFFER_BINDING, &drawFbo);
        glGetIntegerv(GL_READ_FRAMEBUFFER_BINDING, &readFbo);
        glGetIntegerv(GL_VIEWPORT, viewport);
        glGetIntegerv(GL_CURRENT_PROGRAM, &program);
        glGetIntegerv(GL_VERTEX_ARRAY_BINDING, &vao);
        glGetIntegerv(GL_ARRAY_BUFFER_BINDING, &arrayBuffer);
        glGetIntegerv(GL_ACTIVE_TEXTURE, &activeTexture);
        glActiveTexture(GL_TEXTURE0);
        glGetIntegerv(GL_TEXTURE_BINDING_2D, &tex0);
        glActiveTexture(GL_TEXTURE1);
        glGetIntegerv(GL_TEXTURE_BINDING_2D, &tex1);
        glActiveTexture(GL_TEXTURE2);
        glGetIntegerv(GL_TEXTURE_BINDING_2D, &tex2);
        // Allocation below must bind on a unit we captured, even if the caller
        // arrived with a different unit active.
        glActiveTexture(GL_TEXTURE0);
        blend = glIsEnabled(GL_BLEND);
        glGetIntegerv(GL_BLEND_SRC_RGB, &blendSrcRgb);
        glGetIntegerv(GL_BLEND_DST_RGB, &blendDstRgb);
        glGetIntegerv(GL_BLEND_SRC_ALPHA, &blendSrcAlpha);
        glGetIntegerv(GL_BLEND_DST_ALPHA, &blendDstAlpha);
        glGetIntegerv(GL_BLEND_EQUATION_RGB, &blendEqRgb);
        glGetIntegerv(GL_BLEND_EQUATION_ALPHA, &blendEqAlpha);
        scissor = glIsEnabled(GL_SCISSOR_TEST);
        glGetIntegerv(GL_SCISSOR_BOX, scissorBox);
        stencil = glIsEnabled(GL_STENCIL_TEST);
        depthTest = glIsEnabled(GL_DEPTH_TEST);
        cullFace = glIsEnabled(GL_CULL_FACE);
        glGetBooleanv(GL_COLOR_WRITEMASK, colorMask);
    }

    ~GlStateGuard() {
        glBindFramebuffer(GL_DRAW_FRAMEBUFFER, drawFbo);
        glBindFramebuffer(GL_READ_FRAMEBUFFER, readFbo);
        glViewport(viewport[0], viewport[1], viewport[2], viewport[3]);
        glUseProgram(static_cast<GLuint>(program)); // raw restore; Hypr cache value untouched
        glBindVertexArray(static_cast<GLuint>(vao));
        glBindBuffer(GL_ARRAY_BUFFER, static_cast<GLuint>(arrayBuffer));
        glActiveTexture(GL_TEXTURE0);
        glBindTexture(GL_TEXTURE_2D, static_cast<GLuint>(tex0));
        glActiveTexture(GL_TEXTURE1);
        glBindTexture(GL_TEXTURE_2D, static_cast<GLuint>(tex1));
        glActiveTexture(GL_TEXTURE2);
        glBindTexture(GL_TEXTURE_2D, static_cast<GLuint>(tex2));
        glActiveTexture(activeTexture);
        if (blend)
            glEnable(GL_BLEND);
        else
            glDisable(GL_BLEND);
        if (scissor)
            glEnable(GL_SCISSOR_TEST);
        else
            glDisable(GL_SCISSOR_TEST);
        glScissor(scissorBox[0], scissorBox[1], scissorBox[2], scissorBox[3]);
        if (stencil)
            glEnable(GL_STENCIL_TEST);
        else
            glDisable(GL_STENCIL_TEST);
        if (depthTest)
            glEnable(GL_DEPTH_TEST);
        else
            glDisable(GL_DEPTH_TEST);
        if (cullFace)
            glEnable(GL_CULL_FACE);
        else
            glDisable(GL_CULL_FACE);
        glColorMask(colorMask[0], colorMask[1], colorMask[2], colorMask[3]);

        // Raw restores above leave actual state exact, but internal Hypr calls
        // (e.g. framebuffer alloc/bind) may have updated the renderer's cached
        // viewport/blend/cap values along the way. Re-sync the caches to the
        // just-restored state so no later cached fast-path diverges from GL.
        g_pHyprOpenGL->setViewport(viewport[0], viewport[1], viewport[2], viewport[3]);
        g_pHyprOpenGL->blend(blend == GL_TRUE);
        g_pHyprOpenGL->setCapStatus(CHyprOpenGLImpl::CAP_STATUS_SCISSOR_TEST, scissor == GL_TRUE);
        g_pHyprOpenGL->setCapStatus(CHyprOpenGLImpl::CAP_STATUS_STENCIL_TEST, stencil == GL_TRUE);
        glBlendFuncSeparate(blendSrcRgb, blendDstRgb, blendSrcAlpha, blendDstAlpha);
        glBlendEquationSeparate(blendEqRgb, blendEqAlpha);
    }
};

GLuint compileShader(GLenum type, const char* source, std::string& error) {
    GLuint shader = glCreateShader(type);
    if (shader == 0) {
        error = "candle effect: glCreateShader failed";
        return 0;
    }
    glShaderSource(shader, 1, &source, nullptr);
    glCompileShader(shader);
    GLint ok = GL_FALSE;
    glGetShaderiv(shader, GL_COMPILE_STATUS, &ok);
    if (ok != GL_TRUE) {
        char log[1024] = {};
        GLsizei len = 0;
        glGetShaderInfoLog(shader, sizeof(log) - 1, &len, log);
        error = std::string("candle effect: shader compile failed: ") + log;
        glDeleteShader(shader);
        return 0;
    }
    return shader;
}

}

struct CandleRenderer::Impl {
    SP<Render::IFramebuffer> fb;
    SP<Render::ITexture>     flames;
    SP<Render::ITexture>     light;
    SP<Render::ITexture>     waxMask;
    int waxCount = 0;

    GLuint program = 0;
    GLuint vao = 0;

    // Cached uniform locations; metadata uploaded once at init.
    GLint locSize = -1;
    GLint locRect = -1;
    GLint locWick = -1;
    GLint locPhase = -1;
    GLint locTime = -1;
    GLint locFlames = -1;
    GLint locLight  = -1;
    GLint locWaxStreams = -1;
    GLint locWaxTiming  = -1;
    GLint locWaxCount   = -1;
    GLint locWaxMask    = -1;

    std::array<float, 2> phases = {};

    uint64_t             lastTick  = 0;
    bool                 rendered  = false;
    SP<Render::ITexture> cachedTex;

    ~Impl() {
        // Destruction requires the shared EGL context to be current.
        if (g_pHyprOpenGL)
            g_pHyprOpenGL->makeEGLCurrent();
        if (program != 0) {
            glDeleteProgram(program);
            program = 0;
        }
        if (vao != 0) {
            glDeleteVertexArrays(1, &vao);
            vao = 0;
        }
        if (fb)
            fb->release();
        cachedTex.reset();
        flames.reset();
        light.reset();
        waxMask.reset();
    }
};

CandleRenderer::CandleRenderer() = default;

bool CandleRenderer::initialize(const CandleEffectSpec& spec, SP<Render::ITexture> flames, SP<Render::ITexture> light, SP<Render::ITexture> waxMask, std::string& error) {
    if (spec.width <= 0 || spec.height <= 0) {
        error = "candle effect: invalid effect size";
        return false;
    }
    if (!flames || !flames->ok() || !light || !light->ok()) {
        error = "candle effect: missing or invalid flame/light textures";
        return false;
    }
    // The wax mask is accepted between light and error: non-null exactly when
    // the spec declares wax. A declared-but-invalid mask is a hard failure —
    // no silent fallback to the flame-only effect.
    if (spec.wax) {
        if (!waxMask || !waxMask->ok()) {
            error = "candle effect: missing or invalid wax mask texture";
            return false;
        }
    } else if (waxMask) {
        error = "candle effect: unexpected wax mask without wax spec";
        return false;
    }

    if (!g_pHyprOpenGL || !g_pHyprRenderer) {
        error = "candle effect: OpenGL renderer unavailable";
        return false;
    }
    g_pHyprOpenGL->makeEGLCurrent();

    impl                 = std::make_unique<Impl>();
    impl->flames         = std::move(flames);
    impl->light          = std::move(light);
    impl->waxMask        = std::move(waxMask);

    GlStateGuard guard; // covers FBO allocation, program/link and VBO setup below

    impl->fb = g_pHyprRenderer->createFB("kitty_skins_candle");
    if (!impl->fb) {
        error = "candle effect: framebuffer creation failed";
        impl.reset();
        return false;
    }
    if (!impl->fb->alloc(spec.width, spec.height)) {
        error = "candle effect: framebuffer allocation failed";
        impl.reset();
        return false;
    }
    auto outTex = impl->fb->getTexture();
    if (!outTex || !outTex->ok()) {
        error = "candle effect: framebuffer texture missing";
        impl.reset();
        return false;
    }

    const GLuint vs = compileShader(GL_VERTEX_SHADER, VERTEX_SRC, error);
    if (vs == 0) {
        impl.reset();
        return false;
    }
    const GLuint fs = compileShader(GL_FRAGMENT_SHADER, FRAGMENT_SRC, error);
    if (fs == 0) {
        glDeleteShader(vs);
        impl.reset();
        return false;
    }

    impl->program = glCreateProgram();
    if (impl->program == 0) {
        error = "candle effect: glCreateProgram failed";
        glDeleteShader(vs);
        glDeleteShader(fs);
        impl.reset();
        return false;
    }
    glAttachShader(impl->program, vs);
    glAttachShader(impl->program, fs);
    glLinkProgram(impl->program);
    // Shaders are deleted on all paths after linking, per GL semantics the
    // program keeps them alive until detachment at program deletion.
    glDeleteShader(vs);
    glDeleteShader(fs);

    GLint linked = GL_FALSE;
    glGetProgramiv(impl->program, GL_LINK_STATUS, &linked);
    if (linked != GL_TRUE) {
        char log[1024] = {};
        GLsizei len = 0;
        glGetProgramInfoLog(impl->program, sizeof(log) - 1, &len, log);
        error = std::string("candle effect: program link failed: ") + log;
        impl.reset();
        return false;
    }

    impl->locSize  = glGetUniformLocation(impl->program, "u_size");
    impl->locRect  = glGetUniformLocation(impl->program, "u_rect");
    impl->locWick  = glGetUniformLocation(impl->program, "u_wick");
    impl->locPhase = glGetUniformLocation(impl->program, "u_phase");
    impl->locTime = glGetUniformLocation(impl->program, "u_time");
    impl->locFlames = glGetUniformLocation(impl->program, "u_flames");
    impl->locLight  = glGetUniformLocation(impl->program, "u_light");
    if (impl->locSize < 0 || impl->locRect < 0 || impl->locWick < 0 || impl->locPhase < 0 || impl->locTime < 0 || impl->locFlames < 0 || impl->locLight < 0) {
        error = "candle effect: required uniforms missing after link";
        impl.reset();
        return false;
    }
    if (spec.wax) {
        impl->locWaxStreams = glGetUniformLocation(impl->program, "u_waxStreams");
        impl->locWaxTiming  = glGetUniformLocation(impl->program, "u_waxTiming");
        impl->locWaxCount   = glGetUniformLocation(impl->program, "u_waxCount");
        impl->locWaxMask    = glGetUniformLocation(impl->program, "u_waxMask");
        if (impl->locWaxStreams < 0 || impl->locWaxTiming < 0 || impl->locWaxCount < 0 || impl->locWaxMask < 0) {
            error = "candle effect: wax uniforms missing after link";
            impl.reset();
            return false;
        }
    }

    glGenVertexArrays(1, &impl->vao);
    if (impl->vao == 0) {
        error = "candle effect: vertex array creation failed";
        impl.reset();
        return false;
    }
    glBindVertexArray(impl->vao);

    // Upload static metadata once.
    glUseProgram(impl->program);
    glUniform2f(impl->locSize, static_cast<float>(spec.width), static_cast<float>(spec.height));
    std::array<float, 8> rects = {};
    std::array<float, 4> wicks = {};
    for (int i = 0; i < 2; ++i) {
        rects[i * 4 + 0] = static_cast<float>(spec.flameRects[i].x);
        rects[i * 4 + 1] = static_cast<float>(spec.flameRects[i].y);
        rects[i * 4 + 2] = static_cast<float>(spec.flameRects[i].width);
        rects[i * 4 + 3] = static_cast<float>(spec.flameRects[i].height);
        wicks[i * 2 + 0] = static_cast<float>(spec.wicks[i][0]);
        wicks[i * 2 + 1] = static_cast<float>(spec.wicks[i][1]);
        impl->phases[i]  = i == 0 ? 0.0f : 2.399963f; // golden-angle split
    }
    glUniform4fv(impl->locRect, 2, rects.data());
    glUniform2fv(impl->locWick, 2, wicks.data());
    glUniform1fv(impl->locPhase, 2, impl->phases.data());
    glUniform1f(impl->locTime, 0.0f);
    glUniform1i(impl->locFlames, 0);
    glUniform1i(impl->locLight, 1);

    // Wax metadata is static: uploaded once here, nothing per frame.
    if (spec.wax) {
        impl->waxCount = static_cast<int>(std::min<size_t>(spec.wax->streams.size(), 4));
        std::array<float, 16> streams = {};
        std::array<float, 8>  timing  = {};
        for (int i = 0; i < impl->waxCount; ++i) {
            const auto& s = spec.wax->streams[static_cast<size_t>(i)];
            streams[i * 4 + 0] = static_cast<float>(s.x);
            streams[i * 4 + 1] = static_cast<float>(s.startY);
            streams[i * 4 + 2] = static_cast<float>(s.endY);
            streams[i * 4 + 3] = static_cast<float>(s.width);
            timing[i * 2 + 0]  = static_cast<float>(s.period);
            timing[i * 2 + 1]  = static_cast<float>(s.phase);
        }
        glUniform4fv(impl->locWaxStreams, 4, streams.data());
        glUniform2fv(impl->locWaxTiming, 4, timing.data());
        glUniform1i(impl->locWaxCount, impl->waxCount);
        glUniform1i(impl->locWaxMask, 2);
    }

    return true;
}

SP<Render::ITexture> CandleRenderer::frame(uint64_t tick) {
    if (!impl || !impl->fb)
        return nullptr;

    // Identical tick: the cached result is still valid, skip the GPU pass.
    if (impl->rendered && impl->lastTick == tick)
        return impl->cachedTex;

    g_pHyprOpenGL->makeEGLCurrent();
    GlStateGuard guard;

    const float time = static_cast<float>(static_cast<double>(tick) * kTickSeconds);

    impl->fb->bind();
    glViewport(0, 0, static_cast<GLsizei>(impl->fb->m_size.x), static_cast<GLsizei>(impl->fb->m_size.y));

    // Fullscreen output replaces every channel, irrespective of caller state.
    glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
    glDisable(GL_BLEND);
    glDisable(GL_SCISSOR_TEST);
    glDisable(GL_STENCIL_TEST);
    glDisable(GL_DEPTH_TEST);
    glDisable(GL_CULL_FACE);

    glUseProgram(impl->program);
    glUniform1f(impl->locTime, time);

    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, impl->flames->m_texID);
    glActiveTexture(GL_TEXTURE1);
    glBindTexture(GL_TEXTURE_2D, impl->light->m_texID);
    if (impl->waxCount > 0 && impl->waxMask) {
        glActiveTexture(GL_TEXTURE2);
        glBindTexture(GL_TEXTURE_2D, impl->waxMask->m_texID);
    }

    glBindVertexArray(impl->vao);
    glDrawArrays(GL_TRIANGLES, 0, 3);

    impl->rendered  = true;
    impl->lastTick  = tick;
    impl->cachedTex = impl->fb->getTexture();
    return impl->cachedTex;
}

CandleRenderer::~CandleRenderer() = default;

}
