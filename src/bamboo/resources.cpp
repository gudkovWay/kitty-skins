#include "resources.hpp"

#include <fstream>
#include <iterator>
#include <utility>
#include <vector>

#include <cairo/cairo.h>
#include <drm_fourcc.h>
#include <hyprland/src/render/OpenGL.hpp>
#include <hyprland/src/render/Renderer.hpp>
#include <hyprland/src/render/gl/GLFramebuffer.hpp>

namespace bamboo02 {
namespace {
constexpr const char* kVertex = R"GLSL(#version 320 es
precision highp float;
out vec2 b_uv;
void main() {
    vec2 uv = vec2(float((gl_VertexID >> 1) & 1), float(gl_VertexID & 1));
    b_uv = uv;
    // Store top-left authored texels at UV(0,0), like the PNG textures.
    gl_Position = vec4(uv * 2.0 - 1.0, 0.0, 1.0);
}
)GLSL";

NColorManagement::PImageDescription srgbDescription() {
    static const auto desc = [] {
        NColorManagement::SImageDescription d;
        d.transferFunction = NColorManagement::CM_TRANSFER_FUNCTION_SRGB;
        d.primariesNameSet = true;
        d.primariesNamed = NColorManagement::CM_PRIMARIES_SRGB;
        return NColorManagement::CImageDescription::from(d);
    }();
    return desc;
}

GLuint compile(GLenum kind, const char* bytes, std::string& error) {
    const GLuint shader = glCreateShader(kind);
    if (!shader) { error = "GL shader allocation failed"; return 0; }
    glShaderSource(shader, 1, &bytes, nullptr);
    glCompileShader(shader);
    GLint ok = 0;
    glGetShaderiv(shader, GL_COMPILE_STATUS, &ok);
    if (ok) return shader;
    std::array<char, 8192> log{};
    glGetShaderInfoLog(shader, log.size(), nullptr, log.data());
    error = log.data();
    glDeleteShader(shader);
    return 0;
}

SP<Render::ITexture> loadPng(const std::filesystem::path& file, int width, int height, std::string& error) {
    cairo_surface_t* source = cairo_image_surface_create_from_png(file.c_str());
    if (!source || cairo_surface_status(source) != CAIRO_STATUS_SUCCESS) {
        if (source) cairo_surface_destroy(source);
        error = file.string() + ": PNG decode failed";
        return nullptr;
    }
    if (cairo_image_surface_get_width(source) != width || cairo_image_surface_get_height(source) != height) {
        cairo_surface_destroy(source);
        error = file.string() + ": unexpected source dimensions";
        return nullptr;
    }
    if (cairo_image_surface_get_format(source) != CAIRO_FORMAT_ARGB32) {
        cairo_surface_t* converted = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, width, height);
        if (!converted || cairo_surface_status(converted) != CAIRO_STATUS_SUCCESS) {
            if (converted) cairo_surface_destroy(converted);
            cairo_surface_destroy(source);
            error = file.string() + ": ARGB conversion failed";
            return nullptr;
        }
        cairo_t* cr = cairo_create(converted);
        cairo_set_operator(cr, CAIRO_OPERATOR_SOURCE);
        cairo_set_source_surface(cr, source, 0, 0);
        cairo_paint(cr);
        cairo_destroy(cr);
        cairo_surface_destroy(source);
        source = converted;
    }
    auto texture = g_pHyprRenderer->createTexture(source);
    cairo_surface_destroy(source);
    if (!texture || !texture->ok()) {
        error = file.string() + ": native texture upload failed";
        return nullptr;
    }
    texture->m_drmFormat = DRM_FORMAT_ARGB8888;
    texture->m_imageDescription = srgbDescription();
    texture->bind();
    texture->setTexParameter(GL_TEXTURE_SWIZZLE_R, GL_BLUE);
    texture->setTexParameter(GL_TEXTURE_SWIZZLE_B, GL_RED);
    texture->setTexParameter(GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    texture->setTexParameter(GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    texture->setTexParameter(GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    texture->setTexParameter(GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    texture->unbind();
    return texture;
}
}

GLState::GLState() {
    glGetIntegerv(GL_CURRENT_PROGRAM, &program);
    glGetIntegerv(GL_DRAW_FRAMEBUFFER_BINDING, &drawFB);
    glGetIntegerv(GL_READ_FRAMEBUFFER_BINDING, &readFB);
    glGetIntegerv(GL_VERTEX_ARRAY_BINDING, &vao);
    glGetIntegerv(GL_ARRAY_BUFFER_BINDING, &arrayBuffer);
    glGetIntegerv(GL_ACTIVE_TEXTURE, &active);
    glGetIntegerv(GL_VIEWPORT, viewport.data());
    glGetIntegerv(GL_SCISSOR_BOX, scissor.data());
    for (unsigned i = 0; i < textures.size(); ++i) {
        glActiveTexture(GL_TEXTURE0 + i);
        glGetIntegerv(GL_TEXTURE_BINDING_2D, &textures[i]);
        glGetIntegerv(GL_SAMPLER_BINDING, &samplers[i]);
    }
    glActiveTexture(active);
    glGetIntegerv(GL_BLEND_SRC_RGB, &srcRGB); glGetIntegerv(GL_BLEND_DST_RGB, &dstRGB);
    glGetIntegerv(GL_BLEND_SRC_ALPHA, &srcA); glGetIntegerv(GL_BLEND_DST_ALPHA, &dstA);
    glGetIntegerv(GL_BLEND_EQUATION_RGB, &eqRGB); glGetIntegerv(GL_BLEND_EQUATION_ALPHA, &eqA);
    glGetBooleanv(GL_COLOR_WRITEMASK, colorMask.data());
    glGetFloatv(GL_COLOR_CLEAR_VALUE, clearColor.data());
    blend = glIsEnabled(GL_BLEND); scissorOn = glIsEnabled(GL_SCISSOR_TEST);
    stencil = glIsEnabled(GL_STENCIL_TEST); depth = glIsEnabled(GL_DEPTH_TEST); cull = glIsEnabled(GL_CULL_FACE);
}
GLState::~GLState() {
    glBindFramebuffer(GL_DRAW_FRAMEBUFFER, drawFB); glBindFramebuffer(GL_READ_FRAMEBUFFER, readFB);
    glUseProgram(program);
    glBindVertexArray(vao); glBindBuffer(GL_ARRAY_BUFFER, arrayBuffer);
    for (unsigned i = 0; i < textures.size(); ++i) {
        glActiveTexture(GL_TEXTURE0 + i);
        glBindTexture(GL_TEXTURE_2D, textures[i]); glBindSampler(i, samplers[i]);
    }
    glActiveTexture(active);
    glViewport(viewport[0], viewport[1], viewport[2], viewport[3]);
    glScissor(scissor[0], scissor[1], scissor[2], scissor[3]);
    const auto cap = [](GLenum c, GLboolean enabled) { if (enabled) glEnable(c); else glDisable(c); };
    cap(GL_BLEND, blend); cap(GL_SCISSOR_TEST, scissorOn); cap(GL_STENCIL_TEST, stencil);
    cap(GL_DEPTH_TEST, depth); cap(GL_CULL_FACE, cull);
    glBlendFuncSeparate(srcRGB, dstRGB, srcA, dstA); glBlendEquationSeparate(eqRGB, eqA);
    glColorMask(colorMask[0], colorMask[1], colorMask[2], colorMask[3]);
    glClearColor(clearColor[0], clearColor[1], clearColor[2], clearColor[3]);
}

Program::~Program() { if (id) glDeleteProgram(id); }
bool Program::load(const std::filesystem::path& file, std::string& error) {
    std::error_code ec;
    const auto size = std::filesystem::file_size(file, ec);
    if (ec || size == 0 || size > 512 * 1024) { error = file.string() + ": missing or oversized GLSL source"; return false; }
    std::ifstream input(file);
    const std::string source((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
    GLuint vertex = compile(GL_VERTEX_SHADER, kVertex, error);
    if (!vertex) return false;
    GLuint fragment = compile(GL_FRAGMENT_SHADER, source.c_str(), error);
    if (!fragment) { glDeleteShader(vertex); error = file.string() + ": " + error; return false; }
    id = glCreateProgram();
    glAttachShader(id, vertex); glAttachShader(id, fragment); glLinkProgram(id);
    glDeleteShader(vertex); glDeleteShader(fragment);
    GLint ok = 0; glGetProgramiv(id, GL_LINK_STATUS, &ok);
    if (!ok) {
        std::array<char, 8192> log{}; glGetProgramInfoLog(id, log.size(), nullptr, log.data());
        error = file.string() + ": " + log.data(); return false;
    }
    GLint count = 0; glGetProgramiv(id, GL_ACTIVE_UNIFORMS, &count);
    for (GLint i = 0; i < count; ++i) {
        std::array<char, 256> name{}; GLint length = 0, elements = 0; GLenum type = 0;
        glGetActiveUniform(id, i, name.size(), &length, &elements, &type, name.data());
        std::string key(name.data(), length);
        if (key.ends_with("[0]")) key.resize(key.size() - 3);
        uniforms.emplace(std::move(key), glGetUniformLocation(id, name.data()));
    }
    return true;
}
void Program::use() const { glUseProgram(id); }
GLint Program::uniform(std::string_view name) const {
    const auto it = uniforms.find(name); return it == uniforms.end() ? -1 : it->second;
}
void Program::one(std::string_view n, float v) const { glUniform1f(uniform(n), v); }
void Program::integer(std::string_view n, int v) const { glUniform1i(uniform(n), v); }
void Program::two(std::string_view n, float x, float y) const { glUniform2f(uniform(n), x, y); }
void Program::four(std::string_view n, float x, float y, float w, float h) const { glUniform4f(uniform(n), x, y, w, h); }
void Program::array(std::string_view n, const float* v, int count, int components) const {
    if (components == 1) glUniform1fv(uniform(n), count, v);
    else if (components == 2) glUniform2fv(uniform(n), count, v);
    else if (components == 4) glUniform4fv(uniform(n), count, v);
}

Runtime::~Runtime() { if (vao) glDeleteVertexArrays(1, &vao); }
void Runtime::quad() const { glBindVertexArray(vao); glDrawArrays(GL_TRIANGLE_STRIP, 0, 4); }
std::expected<std::shared_ptr<Runtime>, std::string> loadRuntime(const std::filesystem::path& root) {
    if (!root.is_absolute()) return std::unexpected("plugin:bamboo02:root must be an absolute pack directory");
    if (!g_pHyprRenderer || !Render::GL::g_pHyprOpenGL) return std::unexpected("OpenGL renderer unavailable");
    Render::GL::g_pHyprOpenGL->makeEGLCurrent();
    GLState saved;
    glActiveTexture(GL_TEXTURE0);
    auto runtime = std::make_shared<Runtime>(); runtime->root = root;
    std::string error;
    runtime->frameAtlas = loadPng(root / "frame-atlas.png", 2048, 2048, error);
    if (!runtime->frameAtlas) return std::unexpected(error);
    runtime->sceneAtlas = loadPng(root / "character-atlas.png", 1536, 2048, error);
    if (!runtime->sceneAtlas) return std::unexpected(error);
    runtime->lantern = loadPng(root / "content-lantern.png", 512, 1024, error);
    if (!runtime->lantern) return std::unexpected(error);
    runtime->plate = loadPng(root / "controls/plate.png",144,144,error);
    if (!runtime->plate) return std::unexpected(error);
    runtime->plateHover = loadPng(root / "controls/plate-hover.png",144,144,error);
    if (!runtime->plateHover) return std::unexpected(error);
    constexpr std::array glyphs = {"minus.png","maximize.png","restore.png","close.png"};
    for (size_t i=0; i<glyphs.size(); ++i) {
        runtime->controlGlyphs[i] = loadPng(root / "controls" / glyphs[i],14,14,error);
        if (!runtime->controlGlyphs[i]) return std::unexpected(error);
    }
    if (!runtime->frame.load(root / "shaders/frame.frag", error) || !runtime->scene.load(root / "shaders/scene.frag", error) ||
        !runtime->ambient.load(root / "shaders/ambient.frag", error)) return std::unexpected(error);
    glGenVertexArrays(1, &runtime->vao);
    if (!runtime->vao) return std::unexpected("GL vertex array allocation failed");
    return runtime;
}

bool Canvas::prepare(int width, int height) {
    if (width < 1 || height < 1) return false;
    if (framebuffer && framebuffer->isAllocated() && framebuffer->m_size == Vector2D(width, height)) return true;
    // Bound each per-window texture to 64MiB RGBA8 and the driver limit.
    GLint limit = 0; glGetIntegerv(GL_MAX_TEXTURE_SIZE, &limit);
    if (width > limit || height > limit || uint64_t(width) * height > 16ULL * 1024 * 1024) return false;
    if (!framebuffer) framebuffer = g_pHyprRenderer->createFB("bamboo02-canvas");
    if (!framebuffer) return false;
    if (!framebuffer->alloc(width, height, DRM_FORMAT_ABGR8888)) return false;
    framebuffer->setImageDescription(srgbDescription());
    framebuffer->getTexture()->m_imageDescription = srgbDescription();
    return true;
}
void Canvas::begin() const {
    glBindFramebuffer(GL_FRAMEBUFFER, static_cast<Render::GL::CGLFramebuffer*>(framebuffer.get())->getFBID());
    glViewport(0, 0, framebuffer->m_size.x, framebuffer->m_size.y);
    glDisable(GL_SCISSOR_TEST); glDisable(GL_STENCIL_TEST); glDisable(GL_DEPTH_TEST); glDisable(GL_CULL_FACE); glDisable(GL_BLEND);
    glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
    glClearColor(0, 0, 0, 0); glClear(GL_COLOR_BUFFER_BIT);
}
SP<Render::ITexture> Canvas::texture() const { return framebuffer ? framebuffer->getTexture() : nullptr; }
void bindTexture(unsigned unit, const SP<Render::ITexture>& texture) {
    glActiveTexture(GL_TEXTURE0 + unit); glBindSampler(unit, 0);
    glBindTexture(GL_TEXTURE_2D, texture ? texture->m_texID : 0);
}

void bindFrameUniforms(const Program& p, const Motion& m, float w, float h, float scale, float focused) {
    p.use(); p.two("logical_size", w, h); p.two("atlas_size", 2048, 2048);
    p.two("b_origin", 0, -24); p.two("b_span", w + 112, h + 24); p.one("b_scale", scale);
    p.four("insets", 52, 52, 52, 52); p.one("focused", focused); p.one("wind", m.wind); p.one("layer_count", 10);
    p.integer("frame_atlas", 0);
    p.array("rl_cell", m.cells.data(), 16, 4); p.array("rl_size", m.sizes.data(), 16, 2);
    p.array("rl_pivot", m.pivots.data(), 16, 2); p.array("rl_pos", m.positions.data(), 16, 2);
    p.array("rl_angle", m.angles.data(), 16, 1); p.array("rl_reveal", m.reveals.data(), 16, 1);
    p.array("rl_reveal_mode", m.modes.data(), 16, 1); p.array("rl_emission", m.emissions.data(), 16, 1); p.array("rl_deform", m.deforms.data(), 16, 1);
    p.four("rl_corner_tl", 428,4,416,416); p.four("rl_corner_tr", 848,4,416,416);
    p.four("rl_corner_bl", 1268,4,416,416); p.four("rl_corner_br", 4,1032,416,416);
    p.two("rl_corner_top_size",104,104); p.two("rl_corner_bottom_size",104,104);
    p.four("rl_rail_top",792,1612,1024,208); p.four("rl_rail_bottom",4,1824,1024,208);
    p.four("rl_rail_left",4,4,208,1024); p.four("rl_rail_right",216,4,208,1024); p.two("rl_rail_period",256,256);
}
void bindSceneUniforms(const Program& p, const Motion& m, float w, float h, float scale, float focused) {
    p.use(); p.two("logical_size", w-104,h-104); p.two("scene_frame_size",w,h); p.four("scene_insets",52,52,52,52);
    p.two("b_origin",0,-24); p.two("b_span",w+112,h+24); p.one("b_scale",scale); p.one("focused",focused);
    p.one("scene_scale",m.sceneScale); p.integer("frame_atlas",0); p.integer("scene_atlas",1);
    p.two("frame_atlas_size",2048,2048); p.two("scene_atlas_size",1536,2048);
    p.array("sc_cell",m.cells.data(),4,4); p.array("sc_size",m.sizes.data(),4,2); p.array("sc_pivot",m.pivots.data(),4,2);
    p.array("sc_pos",m.scenePositions.data(),4,2); p.array("sc_angle",m.angles.data(),4,1);
    p.array("sc_reveal",m.reveals.data(),4,1); p.array("sc_reveal_mode",m.modes.data(),4,1);
    p.array("g_cell",m.girlCell.data(),1,4); p.array("g_pos",m.girlPos.data(),1,2); p.array("g_size",m.girlSize.data(),1,2);
    p.one("g_facing",m.girlFacing); p.one("g_opacity",m.girlOpacity); p.one("b_opacity",m.girlOpacity);
    p.array("b_center",m.buttonCenter.data(),1,2); p.array("b_size",m.buttonSize.data(),1,2); p.one("b_press",m.buttonPress);
    p.one("floor_impact",m.floorImpact); p.one("ceiling_impact",m.ceilingImpact);
    p.array("cord_a0",m.anchor0.data(),1,2); p.array("cord_a1",m.anchor1.data(),1,2);
    p.array("cord_e0",m.end0.data(),1,2); p.array("cord_e1",m.end1.data(),1,2);
    p.one("cord_slack",m.cordSlack); p.one("cord_diameter",m.cordDiameter); p.one("cord_payout",m.cordPayout);
}

} // namespace bamboo02
