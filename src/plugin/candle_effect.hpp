#pragma once

#include <cstdint>
#include <memory>
#include <string>

#include <hyprland/src/helpers/memory/Memory.hpp>

#include "kitty_skins/model.hpp"

namespace Render {
    class IFramebuffer;
    class ITexture;
}

namespace kitty_skins::plugin {

// Renders the two-flame candle overlay into a small offscreen framebuffer at
// the ornament's source size. The result is a premultiplied RGBA texture that
// the ordinary renderer composites over the static decoration.
class CandleRenderer final {
  public:
    CandleRenderer();
    CandleRenderer(const CandleRenderer&)          = delete;
    CandleRenderer& operator=(const CandleRenderer&) = delete;
    CandleRenderer(CandleRenderer&&)                 = delete;
    CandleRenderer& operator=(CandleRenderer&&) = delete;

    // Must be called with the shared EGL context current (caller guarantees
    // via makeEGLCurrent). Creates the effect framebuffer, the GL program and
    // all cached resources; returns false and fills \p error on any failure.
    bool initialize(const CandleEffectSpec& spec, SP<Render::ITexture> flames, SP<Render::ITexture> light, SP<Render::ITexture> waxMask, std::string& error);

    // Renders at most once per tick (34 ms per unit) and returns the cached
    // framebuffer texture. Allocation-free on the cached path.
    SP<Render::ITexture> frame(uint64_t tick);

    ~CandleRenderer();

  private:
    struct Impl;
    std::unique_ptr<Impl> impl;
};

}
