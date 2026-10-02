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

// Renders one layered flower ornament into a region-sized offscreen framebuffer:
// the repaired stationary background first, then every declared petal rotated in
// pixel space about its own pivot by a signed small angle, then the optional
// pearl bubble, then the fixed root/opal foreground on top. The cached
// premultiplied RGBA result replaces the
// whole flower region in the ordinary decoration pass, so the stationary parts
// and the moving petals are composited exactly once and no duplicate base copy
// can ghost behind the motion. Motion is rotation only — never opacity or color.
//
// The optional bubble layer adds a single corner pearl that grows in place from
// its anchor, ruptures into a fan of ten outward droplets and then regrows on
// the same loop; it is sampled from its own texture unit and composes after the
// petals, so it may be declared alone or alongside zero to six rocking petals.
// At least one of the two layers is always present.
class FlowerRenderer final {
  public:
    FlowerRenderer();
    FlowerRenderer(const FlowerRenderer&)          = delete;
    FlowerRenderer& operator=(const FlowerRenderer&) = delete;
    FlowerRenderer(FlowerRenderer&&)                 = delete;
    FlowerRenderer& operator=(FlowerRenderer&&) = delete;

    // Must be called with the shared EGL context current (caller guarantees
    // via makeEGLCurrent). Decodes and uploads the background, foreground and
    // every moving raster (the petals, or the single bubble pearl), validates
    // the conservative bubble excursion, allocates the region-sized output
    // framebuffer, creates the GL program and uploads all static metadata;
    // returns false and fills \p error on any failure, so a declared effect can
    // never fail later at draw time.
    bool initialize(const FlowerEffectSpec& spec, std::string& error);

    // Renders the composed region at most once per tick and returns the cached
    // framebuffer texture. Allocation- and compile-free on the cached path: the
    // framebuffer was preallocated in initialize, so null is returned only for
    // genuinely unexpected invalid draw input.
    SP<Render::ITexture> frame(uint64_t tick);

    ~FlowerRenderer();

  private:
    struct Impl;
    std::unique_ptr<Impl> impl;
};

}
