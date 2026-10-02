#pragma once

#include <cstdint>
#include <optional>
#include <vector>

#include <hyprland/src/helpers/math/Math.hpp>
#include <hyprland/src/render/Texture.hpp>
#include <hyprutils/math/Box.hpp>
#include <hyprutils/math/Region.hpp>
#include <hyprutils/math/Vector2D.hpp>

#include "kitty_skins/model.hpp"
#include "texture.hpp"

namespace kitty_skins::plugin {

enum class LayoutMode { exact, adaptive };

// The rendering mode and the frame extents (logical) that follow from it. Solved
// only from the layout-assigned box, which is independent of the extents this
// decoration reserves, so the choice can never oscillate.
//
// `enabled` is false when the adaptive frame cannot fit: the extents are then
// zeroed and the decoration reserves and draws nothing.
struct LayoutGeometry {
    LayoutMode mode    = LayoutMode::adaptive;
    bool       enabled = true;
    Insets     extents;
    double     layoutWidth  = 0.0;
    double     layoutHeight = 0.0;
};

// One ready-to-draw textured quad, in outer-box-local physical pixels: (0, 0) is
// the top-left of the composed outer texture, so moving or animating the window
// never invalidates the cache.
struct DrawOp {
    SP<Render::ITexture>      texture;
    Hyprutils::Math::CBox     destination;
    Hyprutils::Math::Vector2D uvTopLeft{0.0, 0.0};
    Hyprutils::Math::Vector2D uvBottomRight{1.0, 1.0};
    uint8_t                   wrapX = 0;
    uint8_t                   wrapY = 0;
};

struct LayoutCache {
    LayoutMode          mode    = LayoutMode::adaptive;
    bool                enabled = true;
    std::vector<DrawOp> operations;
    kitty_skins::Insets extents;       // logical, as used for decoration reservation
    // Rounded outer frame box in outer-box-local device pixels: (0, 0) is its
    // top-left, matching the DrawOps' space.
    Hyprutils::Math::CBox outerBox;
    // Rounded client aperture in the same outer-box-local device pixels.
    Hyprutils::Math::CBox apertureBox;
    // Outer frame ring in outer-box-local device pixels: outerBox minus apertureBox.
    // Built once per layout; a render pass translates a single copy of it to the
    // absolute physical origin instead of rebuilding it from boxes per operation.
    Hyprutils::Math::CRegion outerRing;
    float                    monitorScale = 1.F;
    // Effect placements share the exact same source/anchor transform as their
    // ornament. `candleBox` is the candle overlay's box when the pack declares a
    // candle; `accentBoxes` holds one box per accent effect, parallel to
    // pack.accentEffects.
    std::optional<Hyprutils::Math::CBox> candleBox;
    std::vector<Hyprutils::Math::CBox>   accentBoxes;
    // One box per flower effect, parallel to pack.flowerEffects, in the same
    // outer-box-local device pixels. Flower regions emit no ordinary DrawOp; the
    // runtime composites the whole region and uses these boxes for damage and
    // clipping.
    std::vector<Hyprutils::Math::CBox>   flowerBoxes;
};

// Decide exact vs adaptive from the layout-assigned outer box (logical).
// `allowExact` opts into the source-aspect exact path; when false the decision
// is forced to adaptive, which a global "*" target uses to keep every ordinary
// window on a thin frame at any aspect. The dedicated preview window and a
// single-class target keep the default of true.
LayoutGeometry resolveGeometry(const kitty_skins::SkinPack& pack, double layoutWidth, double layoutHeight,
                               bool allowExact = true) noexcept;

// Rebuild `out` for the given geometry. `outerSize` is the rounded physical outer
// frame size and `apertureOffset`/`apertureSize` the rounded client aperture in
// outer-box-local physical pixels; all three come from the compositor's own
// rounded boxes, so the layout never re-rounds logical extents on its own. Never
// fails: degenerate spans and missing regions simply produce no operation.
void buildLayout(const SkinRuntime& runtime, const LayoutGeometry& geometry, const Hyprutils::Math::Vector2D& outerSize,
                 const Hyprutils::Math::Vector2D& apertureOffset, const Hyprutils::Math::Vector2D& apertureSize,
                 float monitorScale, LayoutCache& out);

}
