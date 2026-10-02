#include "silver_effect.hpp"

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

// GLSL ES 3.x sources. The fragment shader works in normalized atlas UVs with
// y growing downwards (PNG row order, matching how the asset textures were
// uploaded), the same orientation the candle effect reconstructs from
// gl_FragCoord.
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

uniform sampler2D u_base; // premultiplied atlas (exact or adaptive)
uniform sampler2D u_mask; // atlas-sized RGBA; only alpha is used

uniform float u_time;     // seconds since the effect epoch
uniform float u_period;   // color cycle period in seconds
uniform float u_strength; // restrained tint amount in [0, 1]

const float PI = 3.14159265358979;

void main() {
    vec2  uv   = v_ndc * 0.5 + 0.5;
    vec4  base = textureLod(u_base, uv, 0.0);
    float m    = textureLod(u_mask, uv, 0.0).a;

    // One common phase for the whole atlas, so every region drawn from the
    // transformed texture shares it and no tile can show a phase seam.
    float p = fract(u_time / u_period);

    // Two smooth tint windows, each spanning exactly 2/3 of the cycle:
    // neutral -> cool pearl (peak at 1/3 of the period) -> faint warm ivory
    // (peak at 2/3) -> neutral. Both windows have zero value and zero slope at
    // their edges, so the loop is seamless and free of visible resets. This is
    // material hue motion only: there is no opacity pulse and no background term.
    float wCool  = p < 2.0 / 3.0 ? pow(sin(1.5 * PI * p), 2.0) : 0.0;
    float wIvory = p > 1.0 / 3.0 ? pow(sin(1.5 * PI * (p - 1.0 / 3.0)), 2.0) : 0.0;

    // Gains hover around 1.0 (about +-6%), so luminance detail survives and
    // neither shadows are flattened nor highlights saturated.
    vec3 cool  = vec3(0.930, 0.980, 1.055);
    vec3 ivory = vec3(1.050, 1.010, 0.925);
    vec3 tint  = vec3(1.0) * (1.0 - wCool - wIvory) + cool * wCool + ivory * wIvory;

    // Restrained motion, gated by the mask alpha so only painted silver moves.
    // The gain stays multiplicative on the premultiplied RGB, keeping carved
    // detail and shadow depth, and the alpha passes through exactly.
    vec3 rgb = base.rgb * mix(vec3(1.0), tint, u_strength * m);
    rgb = min(rgb, vec3(base.a)); // preserve the premultiplied invariant rgb <= a
    fragColor = vec4(rgb, base.a);
}
)";

// Saves every piece of context state this renderer touches on construction and
// restores it on destruction, including all early returns via RAII. Same
// convention as the candle effect renderer.
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
        error = "silver effect: glCreateShader failed";
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
        error = std::string("silver effect: shader compile failed: ") + log;
        glDeleteShader(shader);
        return 0;
    }
    return shader;
}

// Atlas assets are uploaded from Cairo's premultiplied ARGB32, whose
// little-endian byte order is BGRA. Swizzling R/B makes the sampler read true
// premultiplied RGBA without a CPU-side conversion. Same convention as the
// runtime's atlas uploads; effect rasters always sample linearly.
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

struct SilverMaterialRenderer::Impl {
    SP<Render::ITexture> mask;
    double period   = 12.0;
    double strength = 0.25;

    GLuint program = 0;
    GLuint vao    = 0;

    GLint locBase     = -1;
    GLint locMask     = -1;
    GLint locTime     = -1;
    GLint locPeriod   = -1;
    GLint locStrength = -1;

    // One preallocated output per base atlas (exact and adaptive): both
    // framebuffers are allocated once in initialize at the full atlas size,
    // and the per-tick pass only renders into the matching slot and shares the
    // result across every region and window. No per-region, per-window or
    // per-frame allocation ever happens.
    struct Slot {
        GLuint                   baseTexId = 0; // claimed by the first transformed() call
        SP<Render::IFramebuffer> fb;
        SP<Render::ITexture>     cached;
        uint64_t                 lastTick = 0;
        bool                     rendered = false;
    };
    std::array<Slot, 2> slots;

    Slot* slotFor(const Render::ITexture& base) {
        for (auto& slot : slots)
            if (slot.baseTexId == base.m_texID)
                return &slot;
        for (auto& slot : slots) {
            if (slot.baseTexId == 0) {
                slot.baseTexId = base.m_texID;
                // Match the base atlas's sampling exactly, so substituting the
                // transformed texture cannot change the look of any region.
                // Runs once per atlas, inside its own state guard: the bind/
                // set/unbind below must not leak into the caller's GL state.
                GlStateGuard guard;
                slot.cached->magFilter = base.magFilter;
                slot.cached->minFilter = base.minFilter;
                slot.cached->bind();
                slot.cached->setTexParameter(GL_TEXTURE_MAG_FILTER, base.magFilter);
                slot.cached->setTexParameter(GL_TEXTURE_MIN_FILTER, base.minFilter);
                slot.cached->setTexParameter(GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
                slot.cached->setTexParameter(GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
                slot.cached->unbind();
                return &slot;
            }
        }
        return nullptr;
    }

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
        for (auto& slot : slots) {
            if (slot.fb)
                slot.fb->release();
            slot.cached.reset();
            slot.fb.reset();
        }
        mask.reset();
    }
};

SilverMaterialRenderer::SilverMaterialRenderer() = default;

bool SilverMaterialRenderer::initialize(const SilverEffectSpec& spec, std::string& error) {
    if (!std::isfinite(spec.period) || spec.period <= 0.0 || !std::isfinite(spec.strength) ||
        spec.strength < 0.0 || spec.strength > 1.0) {
        error = "silver effect: invalid period or strength";
        return false;
    }
    if (!g_pHyprOpenGL || !g_pHyprRenderer) {
        error = "silver effect: OpenGL renderer unavailable";
        return false;
    }
    g_pHyprOpenGL->makeEGLCurrent();

    // One guard covers the mask upload, both framebuffer allocations, the
    // program/link and the VAO setup: every GL resource this renderer owns is
    // created inside it, and every early return restores the context.
    GlStateGuard guard;

    auto mask = loadPngTexture(spec.mask, error);
    if (!mask)
        return false;

    impl           = std::make_unique<Impl>();
    impl->mask     = std::move(mask);
    impl->period   = spec.period;
    impl->strength = spec.strength;

    // Preallocate both full-atlas outputs here: the load is transactional, so a
    // failure is a bounded error now, never a deferred allocation failure at
    // draw time. Slots stay unclaimed (baseTexId == 0) until transformed() sees
    // the matching base atlas.
    for (auto& slot : impl->slots) {
        slot.fb = g_pHyprRenderer->createFB("kitty_skins_silver");
        if (!slot.fb || !slot.fb->alloc(static_cast<int>(impl->mask->m_size.x), static_cast<int>(impl->mask->m_size.y))) {
            error = "silver effect: framebuffer allocation failed";
            impl.reset();
            return false;
        }
        slot.cached = slot.fb->getTexture();
        if (!slot.cached || !slot.cached->ok()) {
            error = "silver effect: framebuffer texture missing";
            impl.reset();
            return false;
        }
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
        error = "silver effect: glCreateProgram failed";
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
        error = std::string("silver effect: program link failed: ") + log;
        impl.reset();
        return false;
    }

    impl->locBase     = glGetUniformLocation(impl->program, "u_base");
    impl->locMask     = glGetUniformLocation(impl->program, "u_mask");
    impl->locTime     = glGetUniformLocation(impl->program, "u_time");
    impl->locPeriod   = glGetUniformLocation(impl->program, "u_period");
    impl->locStrength = glGetUniformLocation(impl->program, "u_strength");
    if (impl->locBase < 0 || impl->locMask < 0 || impl->locTime < 0 || impl->locPeriod < 0 || impl->locStrength < 0) {
        error = "silver effect: required uniforms missing after link";
        impl.reset();
        return false;
    }

    glGenVertexArrays(1, &impl->vao);
    if (impl->vao == 0) {
        error = "silver effect: vertex array creation failed";
        impl.reset();
        return false;
    }
    glBindVertexArray(impl->vao);

    // Static metadata once; only u_time changes per tick.
    glUseProgram(impl->program);
    glUniform1i(impl->locBase, 0);
    glUniform1i(impl->locMask, 1);
    glUniform1f(impl->locTime, 0.0f);
    glUniform1f(impl->locPeriod, static_cast<float>(impl->period));
    glUniform1f(impl->locStrength, static_cast<float>(impl->strength));

    return true;
}

SP<Render::ITexture> SilverMaterialRenderer::transformed(Render::ITexture& baseAtlas, uint64_t tick) {
    if (!impl || !impl->program || !impl->mask || !baseAtlas.ok())
        return nullptr;

    // Claims one preallocated slot for this atlas on first use (texture filter
    // setup only, no allocation) and finds it again afterwards.
    Impl::Slot* slot = impl->slotFor(baseAtlas);
    if (slot == nullptr || !slot->fb)
        return nullptr;

    // Identical tick: the cached result is still valid, skip the GPU pass.
    if (slot->rendered && slot->lastTick == tick)
        return slot->cached;

    g_pHyprOpenGL->makeEGLCurrent();
    GlStateGuard guard;

    slot->fb->bind();
    glViewport(0, 0, static_cast<GLsizei>(slot->fb->m_size.x), static_cast<GLsizei>(slot->fb->m_size.y));

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
    glBindTexture(GL_TEXTURE_2D, baseAtlas.m_texID);
    glActiveTexture(GL_TEXTURE1);
    glBindTexture(GL_TEXTURE_2D, impl->mask->m_texID);

    glBindVertexArray(impl->vao);
    glDrawArrays(GL_TRIANGLES, 0, 3);

    slot->rendered = true;
    slot->lastTick = tick;
    return slot->cached;
}

SilverMaterialRenderer::~SilverMaterialRenderer() = default;

}
