#include "flower_effect.hpp"

#include <algorithm>
#include <array>
#include <cmath>

#include <GLES3/gl32.h>
#include <cairo/cairo.h>
#include <drm_fourcc.h>

#include <hyprland/src/render/Framebuffer.hpp>
#include <hyprland/src/render/OpenGL.hpp>
#include <hyprland/src/render/Renderer.hpp>

namespace kitty_skins::plugin {
using Render::GL::CHyprOpenGLImpl;
using Render::GL::g_pHyprOpenGL;
namespace {

constexpr double kTickSeconds = 0.034;
constexpr int    kMaxPetals   = 6;
// Texture units: 0 background, 1 foreground, 2..7 the petals, 8 the pearl body.
// The bubble has its own unit so a mixed effect samples six rotating petals and
// the pearl in the same pass, never aliasing a petal slot.
constexpr int    kPetalUnitBase = 2;
constexpr int    kBubbleUnit    = 8;

// GLSL ES 3.x sources. Coordinates: the fragment shader works in region-space
// pixel coordinates with y growing downwards (PNG row order, matching how the
// asset textures were uploaded), the same reconstruction the candle effect uses
// from gl_FragCoord so the cached texture composites with the orientation the
// ordinary renderTexture uses for uploaded PNGs.
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

uniform sampler2D u_background; // premultiplied repaired stationary ornament
uniform sampler2D u_foreground; // premultiplied fixed root/opal overlaps
uniform sampler2D u_petal0;
uniform sampler2D u_petal1;
uniform sampler2D u_petal2;
uniform sampler2D u_petal3;
uniform sampler2D u_petal4;
uniform sampler2D u_petal5;
uniform sampler2D u_bubbleTexture; // premultiplied pearl body, own unit 8

uniform vec2  u_size;         // region size in source pixels
uniform vec2  u_pivot[6];     // petal pivots, region pixels, y down
uniform float u_angle[6];     // signed authored swing amplitude, degrees
uniform float u_phase[6];     // per-petal loop offset in [0, 1)
uniform float u_flowerPhase;  // per-flower loop offset in [0, 1)
uniform float u_period;       // seconds
uniform float u_time;         // seconds since the effect epoch
uniform int   u_count;        // declared petals, 0..6

// Bubble layer (u_bubble == 1): a pearl raster with its own sampler and unit,
// independent of the six petals, so it composes on top of the petal animation.
uniform int   u_bubble;        // 1 = grow/rupture pearl, 0 = no bubble
uniform vec2  u_bubbleCenter;  // pearl anchor in region pixels, y down
uniform vec2  u_bubbleRadii;   // enclosing axis-aligned pearl radii, source px
uniform vec2  u_bubbleOutward; // nonzero outward direction, normalized on CPU
uniform float u_bubbleSpread;  // radial droplet excursion budget, source px

const float PI = 3.14159265358979;

// Source coordinate of a petal fragment: inverse rotation of the destination
// pixel about the declared pivot, by the kinematic unfold angle
//   a(t) = angle * 0.5 * (1 - cos(2*pi*(t/period + flowerPhase + petalPhase))).
// The factor 0.5*(1-cos) is 0 at the cycle start, rises to 1 at half the period
// and returns to 0, so every petal unfolds in turn and settles back with zero
// velocity at both ends. Never opacity, never color: rotation only.
vec2 petalSrc(vec2 pivot, float angle, float phase, vec2 local) {
    float progress = 0.5 * (1.0 - cos(2.0 * PI * (u_time / u_period + u_flowerPhase + phase)));
    float a = radians(angle) * progress;
    float ca = cos(a);
    float sa = sin(a);
    vec2  d  = local - pivot;
    return pivot + vec2(ca * d.x + sa * d.y, -sa * d.x + ca * d.y);
}

// A petal raster is region-sized with transparent surroundings; a sample outside
// the canvas is transparent, never a clamped edge smear.
bool inside(vec2 s) {
    return s.x >= 0.0 && s.y >= 0.0 && s.x < u_size.x && s.y < u_size.y;
}

// Smooth Hermite ramp for every phase boundary, so growth both starts and
// settles with zero velocity; also clamps the phase parameter to [0, 1].
float smooth01(float x) {
    x = clamp(x, 0.0, 1.0);
    return x * x * (3.0 - 2.0 * x);
}

// Premultiplied pearl-body sample at region-space coordinate s. Outside the
// region canvas the sample is transparent, never a clamped edge smear.
vec4 pearl(vec2 s) {
    if (!inside(s))
        return vec4(0.0);
    return textureLod(u_bubbleTexture, s / u_size, 0.0);
}

// One corner pearl, phase t in [0, 1):
//   0..0.72  smooth growth from the anchor   (scale 0 -> 1)
//   0.72..0.78  outward rupture rim, no intact pearl left
//   0.72..0.95  ten separated droplets in a deterministic outward fan
//   0.95..1  brief empty socket
// The loop resets continuously because the next growth starts invisible.
vec4 bubbleLayer(vec2 local) {
    float t    = fract(u_time / u_period + u_flowerPhase);
    float maxR = max(u_bubbleRadii.x, u_bubbleRadii.y);
    vec2  outw = u_bubbleOutward;
    vec2  tang = vec2(-outw.y, outw.x);
    vec4  acc  = vec4(0.0);

    if (t < 0.72) {
        float g = smooth01(t / 0.72);
        if (g > 0.0) {
            // Inverse sample about the anchor: the destination disc [0, g*maxR]
            // reads the pearl at native scale, so the body grows in place and
            // the division is never singular (the branch is skipped at g == 0).
            acc = pearl(u_bubbleCenter + (local - u_bubbleCenter) / g);
        }
    }

    if (t >= 0.72 && t < 0.78) {
        // The intact pearl is already gone; a thin shell of pearl material
        // peels off the original rim and expands outward past it.
        float ru   = (t - 0.72) / 0.06;
        float w    = maxR * 0.24 * (1.0 - 0.55 * ru);
        // The shell is clamped to the same conservative envelope the CPU
        // validates, so containment never depends on the declaration's
        // magnitudes (normally 1.30 * maxR stays well inside).
        float rimR = min(maxR * (0.90 + 0.40 * ru), maxR + u_bubbleSpread + 6.0 - w);
        float band = 1.0 - smooth01((abs(length(local - u_bubbleCenter) - rimR) - 0.35 * w) / (0.65 * w));
        // Rupture travels away from the client, like the droplets, rather than
        // expanding into the protected opening and relying on clipping there.
        band *= smoothstep(0.0, 1.0, dot(local - u_bubbleCenter, outw));
        if (band > 0.0) {
            // The rim maps into the pearl body (0.78 * maxR) rather than its
            // outer edge, so the shell reads as solid pearl material instead of
            // the near-transparent silhouette boundary.
            vec4 p = pearl(u_bubbleCenter + (local - u_bubbleCenter) * (0.78 * maxR / rimR));
            p *= band; // premultiplied: RGB follows alpha
            acc = p;
        }
    }

    if (t >= 0.72 && t < 0.95) {
        // Ten droplets fan outward deterministically; each carries a scaled copy
        // of the pearl body so it reads as pearl material, not a flat disc.
        float dp     = (t - 0.72) / 0.23;
        float travel = (maxR + u_bubbleSpread) * smooth01(dp);
        float fade   = smooth01(dp / 0.2) * (1.0 - smooth01((dp - 0.55) / 0.45));
        for (int i = 0; i < 10; ++i) {
            float k   = -0.85 + 1.70 * float(i) / 9.0;
            vec2  dir = normalize(outw + tang * k);
            vec2  pos = u_bubbleCenter + dir * travel;
            float rd  = 5.0 * (0.55 + 0.45 * (1.0 - dp)) * (1.0 - 0.22 * float(i % 3));
            float aa  = 1.0 - smoothstep(rd * 0.55, rd, length(local - pos));
            if (aa > 0.0 && fade > 0.0) {
                vec4 p = pearl(u_bubbleCenter + (local - pos) * (maxR / max(rd, 0.001)));
                p *= aa * fade;
                acc = p + acc * (1.0 - p.a);
            }
        }
    }

    return acc;
}

void main() {
    vec2 local = (v_ndc * 0.5 + 0.5) * u_size;
    vec2 uv    = local / u_size;

    // Background first, then every petal premultiplied OVER in array order, then
    // the fixed foreground OVER the result so the stationary root/opal occludes
    // the moving layers.
    vec4 c = textureLod(u_background, uv, 0.0);

    if (u_count > 0) {
        vec2 s = petalSrc(u_pivot[0], u_angle[0], u_phase[0], local);
        if (inside(s)) { vec4 p = textureLod(u_petal0, s / u_size, 0.0); c = p + c * (1.0 - p.a); }
    }
    if (u_count > 1) {
        vec2 s = petalSrc(u_pivot[1], u_angle[1], u_phase[1], local);
        if (inside(s)) { vec4 p = textureLod(u_petal1, s / u_size, 0.0); c = p + c * (1.0 - p.a); }
    }
    if (u_count > 2) {
        vec2 s = petalSrc(u_pivot[2], u_angle[2], u_phase[2], local);
        if (inside(s)) { vec4 p = textureLod(u_petal2, s / u_size, 0.0); c = p + c * (1.0 - p.a); }
    }
    if (u_count > 3) {
        vec2 s = petalSrc(u_pivot[3], u_angle[3], u_phase[3], local);
        if (inside(s)) { vec4 p = textureLod(u_petal3, s / u_size, 0.0); c = p + c * (1.0 - p.a); }
    }
    if (u_count > 4) {
        vec2 s = petalSrc(u_pivot[4], u_angle[4], u_phase[4], local);
        if (inside(s)) { vec4 p = textureLod(u_petal4, s / u_size, 0.0); c = p + c * (1.0 - p.a); }
    }
    if (u_count > 5) {
        vec2 s = petalSrc(u_pivot[5], u_angle[5], u_phase[5], local);
        if (inside(s)) { vec4 p = textureLod(u_petal5, s / u_size, 0.0); c = p + c * (1.0 - p.a); }
    }

    // Bubble layer composed after the petals and before the fixed foreground;
    // the pearl body is sampled from its dedicated u_bubbleTexture, never a
    // petal slot, so it works alongside any number of petals.
    if (u_bubble == 1) {
        vec4 b = bubbleLayer(local);
        c = b + c * (1.0 - b.a);
    }

    vec4 fg = textureLod(u_foreground, uv, 0.0);
    c = fg + c * (1.0 - fg.a);
    fragColor = c;
}
)";

// Saves every piece of context state this renderer touches on construction and
// restores it on destruction, including all early returns via RAII. Same
// convention as the candle and silver effect renderers, extended to the nine
// texture units the layered composition samples (background, foreground, six
// petals and the dedicated pearl unit).
struct GlStateGuard {
    GLint    drawFbo = 0, readFbo = 0;
    GLint    viewport[4] = {};
    GLint    program = 0;
    GLint    vao = 0, arrayBuffer = 0;
    GLint    activeTexture = GL_TEXTURE0;
    std::array<GLint, 9> tex = {};
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
        for (int unit = 0; unit < 9; ++unit) {
            glActiveTexture(GL_TEXTURE0 + unit);
            glGetIntegerv(GL_TEXTURE_BINDING_2D, &tex[unit]);
        }
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
        for (int unit = 0; unit < 9; ++unit) {
            glActiveTexture(GL_TEXTURE0 + unit);
            glBindTexture(GL_TEXTURE_2D, static_cast<GLuint>(tex[unit]));
        }
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
        error = "flower effect: glCreateShader failed";
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
        error = std::string("flower effect: shader compile failed: ") + log;
        glDeleteShader(shader);
        return 0;
    }
    return shader;
}

// Flower rasters are uploaded from Cairo's premultiplied ARGB32, whose
// little-endian byte order is BGRA. Swizzling R/B makes the sampler read true
// premultiplied RGBA without a CPU-side conversion; they always sample linearly
// like the other effect rasters.
SP<Render::ITexture> loadPngTexture(const std::filesystem::path& file, std::string& error) {
    cairo_surface_t* surface = cairo_image_surface_create_from_png(file.c_str());
    if (surface == nullptr || cairo_surface_status(surface) != CAIRO_STATUS_SUCCESS) {
        error = file.string() + ": cannot decode PNG asset";
        if (surface)
            cairo_surface_destroy(surface);
        return nullptr;
    }

    if (cairo_image_surface_get_width(surface) < 1 || cairo_image_surface_get_height(surface) < 1) {
        error = file.string() + ": PNG asset has an empty size";
        cairo_surface_destroy(surface);
        return nullptr;
    }

    SP<Render::ITexture> texture = g_pHyprRenderer->createTexture(surface);
    cairo_surface_destroy(surface);
    if (!texture || !texture->ok()) {
        error = file.string() + ": GPU texture allocation failed";
        return nullptr;
    }

    texture->m_drmFormat = DRM_FORMAT_ARGB8888;
    texture->magFilter = GL_LINEAR;
    texture->minFilter = GL_LINEAR;
    texture->bind();
    texture->setTexParameter(GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    texture->setTexParameter(GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    texture->setTexParameter(GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    texture->setTexParameter(GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    texture->setTexParameter(GL_TEXTURE_SWIZZLE_R, GL_BLUE);
    texture->setTexParameter(GL_TEXTURE_SWIZZLE_B, GL_RED);
    texture->unbind();
    return texture;
}

}

struct FlowerRenderer::Impl {
    SP<Render::ITexture> background;
    SP<Render::ITexture> foreground;
    std::array<SP<Render::ITexture>, kMaxPetals> petals;

    // Bubble layer: the extracted pearl body, bound on its own unit (8) and
    // sampled by u_bubbleTexture. Independent of `petals`, so a mixed effect
    // runs up to six rotating petals and the pearl in the same pass.
    SP<Render::ITexture> bubble;
    bool                 hasBubble = false;

    int    count = 0;

    SP<Render::IFramebuffer> fb;
    SP<Render::ITexture>     cachedTex;

    GLuint program = 0;
    GLuint vao     = 0;

    GLint locBackground = -1;
    GLint locForeground = -1;
    std::array<GLint, kMaxPetals> locPetal = {};
    GLint locSize        = -1;
    GLint locPivot       = -1;
    GLint locAngle       = -1;
    GLint locPhase       = -1;
    GLint locFlowerPhase = -1;
    GLint locPeriod      = -1;
    GLint locTime        = -1;
    GLint locCount       = -1;
    GLint locBubbleTexture = -1;
    GLint locBubble        = -1;
    GLint locBubbleCenter  = -1;
    GLint locBubbleRadii   = -1;
    GLint locBubbleOutward = -1;
    GLint locBubbleSpread  = -1;

    uint64_t lastTick = 0;
    bool     rendered = false;

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
        for (auto& petal : petals)
            petal.reset();
        bubble.reset();
        foreground.reset();
        background.reset();
    }
};

FlowerRenderer::FlowerRenderer() = default;

bool FlowerRenderer::initialize(const FlowerEffectSpec& spec, std::string& error) {
    if (spec.width <= 0 || spec.height <= 0) {
        error = "flower effect: invalid region size";
        return false;
    }
    // Petals and bubble are independent layers: a bubble may be declared with
    // zero to six petals, and a petal-only effect must declare at least one.
    const bool hasBubble = spec.bubble.has_value();
    if (spec.petals.empty() && !hasBubble) {
        error = "flower effect: needs a bubble or at least one petal";
        return false;
    }
    if (spec.petals.size() > static_cast<size_t>(kMaxPetals)) {
        error = "flower effect: petal count out of range";
        return false;
    }
    if (!std::isfinite(spec.period) || spec.period < 2.0 || spec.period > 60.0) {
        error = "flower effect: invalid period";
        return false;
    }
    if (!std::isfinite(spec.phase) || spec.phase < 0.0 || spec.phase >= 1.0) {
        error = "flower effect: invalid phase";
        return false;
    }
    const double width  = static_cast<double>(spec.width);
    const double height = static_cast<double>(spec.height);
    for (const FlowerPetalSpec& petal : spec.petals) {
        if (!std::isfinite(petal.pivot[0]) || !std::isfinite(petal.pivot[1]) || petal.pivot[0] < 0.0 || petal.pivot[1] < 0.0 ||
            petal.pivot[0] >= width || petal.pivot[1] >= height) {
            error = "flower effect: petal pivot outside region";
            return false;
        }
        if (!std::isfinite(petal.angle) || petal.angle == 0.0 || std::abs(petal.angle) > 12.0) {
            error = "flower effect: petal angle out of range";
            return false;
        }
        if (!std::isfinite(petal.phase) || petal.phase < 0.0 || petal.phase >= 1.0) {
            error = "flower effect: petal phase out of range";
            return false;
        }
    }
    if (hasBubble) {
        const BubbleSpec& bubble = *spec.bubble;
        if (!std::isfinite(bubble.center[0]) || !std::isfinite(bubble.center[1]) || !std::isfinite(bubble.radii[0]) ||
            !std::isfinite(bubble.radii[1]) || !std::isfinite(bubble.outward[0]) || !std::isfinite(bubble.outward[1]) ||
            !std::isfinite(bubble.spread)) {
            error = "flower effect: bubble metadata is not finite";
            return false;
        }
        if (bubble.radii[0] <= 0.0 || bubble.radii[1] <= 0.0) {
            error = "flower effect: bubble radii must be positive";
            return false;
        }
        if (bubble.spread <= 0.0 || bubble.spread > 128.0) {
            error = "flower effect: bubble spread out of range";
            return false;
        }
        const double outwardNorm = std::hypot(bubble.outward[0], bubble.outward[1]);
        if (!std::isfinite(outwardNorm) || outwardNorm <= 0.0) {
            error = "flower effect: bubble outward direction norm must be finite and non-zero";
            return false;
        }
        // Conservative circular excursion envelope around the anchor. Bounding
        // each phase algebraically, with R = max(radii), S = spread:
        //   growth   |p - center| <= R
        //   rim      rimR + w <= R + S + 6 (the shader clamps the shell radius
        //            to exactly this envelope, so the raw 1.30*R <= 1.30*R
        //            expansion can never escape the validated disc)
        //   droplets |pos - center| = (R + S) * smooth01(dp) <= R + S, and the
        //            largest droplet radius is 5 < 6
        // so no fragment can leave R + S + 6 source pixels of the anchor. The
        // envelope must fit inside the region on both axes; an over-large
        // declaration is rejected, never clamped to the canvas. Core applies the
        // identical R + S + 6 test when parsing, so runtime and manifest agree.
        const double maxRadius = std::max(bubble.radii[0], bubble.radii[1]);
        const double envelope  = maxRadius + bubble.spread + 6.0;
        if (bubble.center[0] - envelope < 0.0 || bubble.center[0] + envelope > width || bubble.center[1] - envelope < 0.0 ||
            bubble.center[1] + envelope > height) {
            error = "flower effect: bubble excursion does not fit inside the region";
            return false;
        }
    }
    if (!g_pHyprOpenGL || !g_pHyprRenderer) {
        error = "flower effect: OpenGL renderer unavailable";
        return false;
    }
    g_pHyprOpenGL->makeEGLCurrent();

    impl              = std::make_unique<Impl>();
    impl->count       = static_cast<int>(spec.petals.size());
    impl->hasBubble   = hasBubble;

    // One guard covers every texture upload, the framebuffer allocation, the
    // program/link and the VAO setup: all GL resources this renderer owns are
    // created inside it, and every early return restores the context.
    GlStateGuard guard;

    impl->background = loadPngTexture(spec.background, error);
    if (!impl->background) {
        impl.reset();
        return false;
    }
    impl->foreground = loadPngTexture(spec.foreground, error);
    if (!impl->foreground) {
        impl.reset();
        return false;
    }
    // Petals load independently of the bubble, so a mixed effect owns both sets
    // and a bubble-only effect simply loads none.
    for (int i = 0; i < impl->count; ++i) {
        impl->petals[static_cast<size_t>(i)] = loadPngTexture(spec.petals[static_cast<size_t>(i)].texture, error);
        if (!impl->petals[static_cast<size_t>(i)]) {
            impl.reset();
            return false;
        }
    }
    if (hasBubble) {
        // The pearl body has its own texture and unit, never a petal slot.
        impl->bubble = loadPngTexture(spec.bubble->texture, error);
        if (!impl->bubble) {
            impl.reset();
            return false;
        }
    }

    // The output framebuffer is allocated once, at the exact region size, so the
    // load is transactional and the per-tick pass performs no allocation.
    impl->fb = g_pHyprRenderer->createFB("kitty_skins_flower");
    if (!impl->fb) {
        error = "flower effect: framebuffer creation failed";
        impl.reset();
        return false;
    }
    if (!impl->fb->alloc(spec.width, spec.height)) {
        error = "flower effect: framebuffer allocation failed";
        impl.reset();
        return false;
    }
    auto outTex = impl->fb->getTexture();
    if (!outTex || !outTex->ok()) {
        error = "flower effect: framebuffer texture missing";
        impl.reset();
        return false;
    }
    // The composited result is a ready premultiplied RGBA image: no swizzle, and
    // linear sampling so it composites identically to the effect rasters.
    outTex->magFilter = GL_LINEAR;
    outTex->minFilter = GL_LINEAR;
    outTex->bind();
    outTex->setTexParameter(GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    outTex->setTexParameter(GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    outTex->setTexParameter(GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    outTex->setTexParameter(GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    outTex->unbind();

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
        error = "flower effect: glCreateProgram failed";
        glDeleteShader(vs);
        glDeleteShader(fs);
        impl.reset();
        return false;
    }
    glAttachShader(impl->program, vs);
    glAttachShader(impl->program, fs);
    glLinkProgram(impl->program);
    glDeleteShader(vs);
    glDeleteShader(fs);

    GLint linked = GL_FALSE;
    glGetProgramiv(impl->program, GL_LINK_STATUS, &linked);
    if (linked != GL_TRUE) {
        char log[1024] = {};
        GLsizei len = 0;
        glGetProgramInfoLog(impl->program, sizeof(log) - 1, &len, log);
        error = std::string("flower effect: program link failed: ") + log;
        impl.reset();
        return false;
    }

    impl->locBackground  = glGetUniformLocation(impl->program, "u_background");
    impl->locForeground  = glGetUniformLocation(impl->program, "u_foreground");
    impl->locPetal[0]    = glGetUniformLocation(impl->program, "u_petal0");
    impl->locPetal[1]    = glGetUniformLocation(impl->program, "u_petal1");
    impl->locPetal[2]    = glGetUniformLocation(impl->program, "u_petal2");
    impl->locPetal[3]    = glGetUniformLocation(impl->program, "u_petal3");
    impl->locPetal[4]    = glGetUniformLocation(impl->program, "u_petal4");
    impl->locPetal[5]    = glGetUniformLocation(impl->program, "u_petal5");
    impl->locSize        = glGetUniformLocation(impl->program, "u_size");
    impl->locPivot       = glGetUniformLocation(impl->program, "u_pivot");
    impl->locAngle       = glGetUniformLocation(impl->program, "u_angle");
    impl->locPhase       = glGetUniformLocation(impl->program, "u_phase");
    impl->locFlowerPhase = glGetUniformLocation(impl->program, "u_flowerPhase");
    impl->locPeriod      = glGetUniformLocation(impl->program, "u_period");
    impl->locTime        = glGetUniformLocation(impl->program, "u_time");
    impl->locCount       = glGetUniformLocation(impl->program, "u_count");
    impl->locBubbleTexture = glGetUniformLocation(impl->program, "u_bubbleTexture");
    impl->locBubble        = glGetUniformLocation(impl->program, "u_bubble");
    impl->locBubbleCenter  = glGetUniformLocation(impl->program, "u_bubbleCenter");
    impl->locBubbleRadii   = glGetUniformLocation(impl->program, "u_bubbleRadii");
    impl->locBubbleOutward = glGetUniformLocation(impl->program, "u_bubbleOutward");
    impl->locBubbleSpread  = glGetUniformLocation(impl->program, "u_bubbleSpread");
    if (impl->locBackground < 0 || impl->locForeground < 0 || impl->locSize < 0 || impl->locPivot < 0 || impl->locAngle < 0 ||
        impl->locPhase < 0 || impl->locFlowerPhase < 0 || impl->locPeriod < 0 || impl->locTime < 0 || impl->locCount < 0 ||
        impl->locBubbleTexture < 0 || impl->locBubble < 0 || impl->locBubbleCenter < 0 || impl->locBubbleRadii < 0 ||
        impl->locBubbleOutward < 0 || impl->locBubbleSpread < 0) {
        error = "flower effect: required uniforms missing after link";
        impl.reset();
        return false;
    }
    for (GLint location : impl->locPetal) {
        if (location < 0) {
            error = "flower effect: petal samplers missing after link";
            impl.reset();
            return false;
        }
    }

    glGenVertexArrays(1, &impl->vao);
    if (impl->vao == 0) {
        error = "flower effect: vertex array creation failed";
        impl.reset();
        return false;
    }
    glBindVertexArray(impl->vao);

    // Static metadata once; only u_time changes per tick.
    glUseProgram(impl->program);
    glUniform1i(impl->locBackground, 0);
    glUniform1i(impl->locForeground, 1);
    for (int i = 0; i < kMaxPetals; ++i)
        glUniform1i(impl->locPetal[static_cast<size_t>(i)], kPetalUnitBase + i);
    glUniform1i(impl->locBubbleTexture, kBubbleUnit);

    glUniform2f(impl->locSize, static_cast<float>(spec.width), static_cast<float>(spec.height));
    std::array<float, kMaxPetals * 2> pivots = {};
    std::array<float, kMaxPetals>     angles = {};
    std::array<float, kMaxPetals>     phases = {};
    for (int i = 0; i < impl->count; ++i) {
        const FlowerPetalSpec& petal = spec.petals[static_cast<size_t>(i)];
        pivots[static_cast<size_t>(i) * 2 + 0] = static_cast<float>(petal.pivot[0]);
        pivots[static_cast<size_t>(i) * 2 + 1] = static_cast<float>(petal.pivot[1]);
        angles[static_cast<size_t>(i)]         = static_cast<float>(petal.angle);
        phases[static_cast<size_t>(i)]         = static_cast<float>(petal.phase);
    }
    glUniform2fv(impl->locPivot, kMaxPetals, pivots.data());
    glUniform1fv(impl->locAngle, kMaxPetals, angles.data());
    glUniform1fv(impl->locPhase, kMaxPetals, phases.data());
    glUniform1f(impl->locFlowerPhase, static_cast<float>(spec.phase));
    glUniform1f(impl->locPeriod, static_cast<float>(spec.period));
    glUniform1f(impl->locTime, 0.0f);
    glUniform1i(impl->locCount, impl->count);

    // Bubble metadata is static too; only u_time changes per tick. The outward
    // direction is normalized once here so the shader never divides by a
    // possibly zero norm at draw time.
    glUniform1i(impl->locBubble, hasBubble ? 1 : 0);
    if (hasBubble) {
        const BubbleSpec& bubble = *spec.bubble;
        const double      norm   = std::hypot(bubble.outward[0], bubble.outward[1]);
        glUniform2f(impl->locBubbleCenter, static_cast<float>(bubble.center[0]), static_cast<float>(bubble.center[1]));
        glUniform2f(impl->locBubbleRadii, static_cast<float>(bubble.radii[0]), static_cast<float>(bubble.radii[1]));
        glUniform2f(impl->locBubbleOutward, static_cast<float>(bubble.outward[0] / norm),
                    static_cast<float>(bubble.outward[1] / norm));
        glUniform1f(impl->locBubbleSpread, static_cast<float>(bubble.spread));
    }

    return true;
}

SP<Render::ITexture> FlowerRenderer::frame(uint64_t tick) {
    if (!impl || !impl->fb || !impl->program)
        return nullptr;

    // Identical tick: the cached result is still valid, skip the GPU pass.
    if (impl->rendered && impl->lastTick == tick)
        return impl->cachedTex;

    g_pHyprOpenGL->makeEGLCurrent();
    GlStateGuard guard;

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
    glUniform1f(impl->locTime, static_cast<float>(static_cast<double>(tick) * kTickSeconds));

    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, impl->background->m_texID);
    glActiveTexture(GL_TEXTURE1);
    glBindTexture(GL_TEXTURE_2D, impl->foreground->m_texID);
    for (int i = 0; i < impl->count; ++i) {
        glActiveTexture(GL_TEXTURE0 + kPetalUnitBase + i);
        glBindTexture(GL_TEXTURE_2D, impl->petals[static_cast<size_t>(i)]->m_texID);
    }
    // The pearl body is bound on its own unit, so it composes with any number of
    // petals without aliasing a petal slot.
    if (impl->hasBubble) {
        glActiveTexture(GL_TEXTURE0 + kBubbleUnit);
        glBindTexture(GL_TEXTURE_2D, impl->bubble->m_texID);
    }

    glBindVertexArray(impl->vao);
    glDrawArrays(GL_TRIANGLES, 0, 3);

    impl->rendered  = true;
    impl->lastTick  = tick;
    impl->cachedTex = impl->fb->getTexture();
    return impl->cachedTex;
}

FlowerRenderer::~FlowerRenderer() = default;

}
