#pragma once

#include <cstdint>
#include <memory>
#include <string>

#include <hyprland/src/helpers/memory/Memory.hpp>

#include "kitty_skins/model.hpp"

namespace Render {
    class ITexture;
}

namespace kitty_skins::plugin {

// Renders one full-atlas silver material transform per effect tick. The base
// atlas is sampled premultiplied together with the atlas-sized silver mask;
// the mask alpha gates the effect, the RGB follows one smooth neutral -> cool
// pearl -> faint warm ivory -> neutral cycle, and the original alpha passes
// through unchanged. The result is a cached premultiplied texture with the
// same pixel dimensions as the source atlas, so the ordinary renderer can
// substitute it for the base atlas without any layout or UV change.
class SilverMaterialRenderer final {
  public:
    SilverMaterialRenderer();
    SilverMaterialRenderer(const SilverMaterialRenderer&)          = delete;
    SilverMaterialRenderer& operator=(const SilverMaterialRenderer&) = delete;
    SilverMaterialRenderer(SilverMaterialRenderer&&)                 = delete;
    SilverMaterialRenderer& operator=(SilverMaterialRenderer&&) = delete;

    // Must be called with the shared EGL context current (caller guarantees
    // via makeEGLCurrent). Decodes and uploads the mask, allocates both
    // full-atlas output framebuffers, creates the GL program and all cached
    // resources; returns false and fills \p error on any failure, so a declared
    // effect can never fail later at draw time.
    bool initialize(const SilverEffectSpec& spec, std::string& error);

    // Renders the transformed atlas for \p baseAtlas at most once per tick and
    // returns the cached framebuffer texture; the result is shared across every
    // region and window drawn from that atlas. Allocation-free: both output
    // framebuffers were preallocated in initialize, so null is returned only
    // for genuinely unexpected invalid draw input.
    SP<Render::ITexture> transformed(Render::ITexture& baseAtlas, uint64_t tick);

    ~SilverMaterialRenderer();

  private:
    struct Impl;
    std::unique_ptr<Impl> impl;
};

}
