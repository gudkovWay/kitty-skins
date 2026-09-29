#pragma once

#include <cstdint>
#include <vector>

#include <hyprland/src/helpers/math/Math.hpp>
#include <hyprland/src/render/Texture.hpp>
#include <hyprutils/math/Box.hpp>
#include <hyprutils/math/Region.hpp>
#include <hyprutils/math/Vector2D.hpp>

#include "kitty_skins/model.hpp"
#include "texture.hpp"

namespace kitty_skins::plugin {

// One cached, ready-to-draw textured quad. `destination` is in monitor-local physical
// pixels relative to the decorated window's client-box origin, so moving or animating
// the window never invalidates the cache: the draw path only translates by the rounded
// physical origin.
struct DrawOp {
    SP<Render::ITexture>       texture;
    Hyprutils::Math::CBox      destination;
    Hyprutils::Math::Vector2D  uvTopLeft;
    Hyprutils::Math::Vector2D  uvBottomRight;
    uint8_t                    wrapX  = 0;
    uint8_t                    wrapY  = 0;
    int                        zIndex = 0;
};

// Fully precomputed layout for one window, one tier and one monitor scale.
struct LayoutCache {
    const kitty_skins::TierSpec* tier           = nullptr;
    std::vector<DrawOp>          operations;
    Hyprutils::Math::CRegion     decorationClip; // outer box minus client aperture, physical, client-relative
    Hyprutils::Math::CBox        outerBox;       // physical, client-relative
    float                        monitorScale   = 1.F;
};

// Rebuild `out` for the given tier and logical client-box size. Never fails: degenerate
// spans and empty slices simply produce no operation.
void buildLayout(const SkinRuntime& runtime, const kitty_skins::TierSpec& tier, double clientWidth, double clientHeight, float monitorScale,
                 LayoutCache& out);

}
