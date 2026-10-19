#pragma once

#include <array>
#include <expected>
#include <filesystem>
#include <map>
#include <memory>
#include <string>
#include <string_view>

#include <GLES3/gl32.h>
#include <hyprland/src/render/Framebuffer.hpp>
#include <hyprland/src/render/Texture.hpp>

#include "motion.hpp"

namespace bamboo02 {

// Raw GL work changes no Hyprland cached setter. Restoring the actual state
// exactly therefore leaves its viewport/cap/program caches valid as well.
class GLState {
  public:
    GLState();
    ~GLState();
    GLState(const GLState&) = delete;
    GLState& operator=(const GLState&) = delete;
  private:
    GLint program = 0, drawFB = 0, readFB = 0, vao = 0, arrayBuffer = 0, active = 0;
    std::array<GLint, 4> viewport{}, scissor{};
    std::array<GLint, 3> textures{}, samplers{};
    GLint srcRGB = 0, dstRGB = 0, srcA = 0, dstA = 0, eqRGB = 0, eqA = 0;
    std::array<GLboolean, 4> colorMask{};
    GLboolean blend = false, scissorOn = false, stencil = false, depth = false, cull = false;
    std::array<GLfloat, 4> clearColor{};
};

class Program {
  public:
    ~Program();
    bool load(const std::filesystem::path& file, std::string& error);
    void use() const;
    GLint uniform(std::string_view name) const;
    void one(std::string_view name, float value) const;
    void integer(std::string_view name, int value) const;
    void two(std::string_view name, float x, float y) const;
    void four(std::string_view name, float x, float y, float w, float h) const;
    void array(std::string_view name, const float* values, int count, int components) const;
  private:
    GLuint id = 0;
    std::map<std::string, GLint, std::less<>> uniforms;
};

struct Runtime {
    ~Runtime();
    SP<Render::ITexture> frameAtlas, sceneAtlas, lantern;
    SP<Render::ITexture> plate, plateHover;
    std::array<SP<Render::ITexture>,4> controlGlyphs;
    Program frame, scene, ambient;
    GLuint vao = 0;
    std::filesystem::path root;
    void quad() const;
};

std::expected<std::shared_ptr<Runtime>, std::string> loadRuntime(const std::filesystem::path& root);

// One RGBA8 sRGB intermediate, reused for all three serialized passes/window.
// No client source is substituted; native renderTexture does final CM/blend.
class Canvas {
  public:
    bool prepare(int width, int height);
    void begin() const;
    SP<Render::ITexture> texture() const;
  private:
    SP<Render::IFramebuffer> framebuffer;
};

void bindTexture(unsigned unit, const SP<Render::ITexture>& texture);
void bindFrameUniforms(const Program& p, const Motion& m, float rootW, float rootH, float scale, float focused);
void bindSceneUniforms(const Program& p, const Motion& m, float rootW, float rootH, float scale, float focused);

} // namespace bamboo02
