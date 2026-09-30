#include "layout.hpp"

#include <algorithm>
#include <array>
#include <cmath>

#include <hyprland/src/render/pass/TexPassElement.hpp>

namespace kitty_skins::plugin {

namespace {

using Hyprutils::Math::CBox;
using Hyprutils::Math::Vector2D;

// Slice the roles out of the manifest once per layout so role lookup is a direct
// array index, never a scan of the pack.
struct RoleIndex {
    std::array<const kitty_skins::RegionSpec*, 4>              corners{}; // TL, TR, BL, BR
    std::vector<const kitty_skins::RegionSpec*>                railTop;
    std::vector<const kitty_skins::RegionSpec*>                railBottom;
    std::array<const kitty_skins::RegionSpec*, 2>              columnTop{};    // left, right
    std::array<const kitty_skins::RegionSpec*, 2>              columnBottom{}; // left, right
    std::array<std::vector<const kitty_skins::RegionSpec*>, 2> columnMiddle;   // left, right
    std::vector<const kitty_skins::RegionSpec*>                ornaments;
};

void indexRoles(const kitty_skins::SkinPack& pack, RoleIndex& out) {
    for (const kitty_skins::RegionSpec& spec : pack.regions) {
        switch (spec.role) {
            case kitty_skins::RegionRole::corner_top_left: out.corners[0] = &spec; break;
            case kitty_skins::RegionRole::corner_top_right: out.corners[1] = &spec; break;
            case kitty_skins::RegionRole::corner_bottom_left: out.corners[2] = &spec; break;
            case kitty_skins::RegionRole::corner_bottom_right: out.corners[3] = &spec; break;
            case kitty_skins::RegionRole::rail_top: out.railTop.push_back(&spec); break;
            case kitty_skins::RegionRole::rail_bottom: out.railBottom.push_back(&spec); break;
            case kitty_skins::RegionRole::column_left_top: out.columnTop[0] = &spec; break;
            case kitty_skins::RegionRole::column_right_top: out.columnTop[1] = &spec; break;
            case kitty_skins::RegionRole::column_left_bottom: out.columnBottom[0] = &spec; break;
            case kitty_skins::RegionRole::column_right_bottom: out.columnBottom[1] = &spec; break;
            case kitty_skins::RegionRole::column_left_middle: out.columnMiddle[0].push_back(&spec); break;
            case kitty_skins::RegionRole::column_right_middle: out.columnMiddle[1].push_back(&spec); break;
            case kitty_skins::RegionRole::ornament: out.ornaments.push_back(&spec); break;
        }
    }
}

struct RegionUv {
    Vector2D topLeft;
    Vector2D bottomRight;
};

RegionUv sourceUv(const SkinRuntime& runtime, const kitty_skins::RegionSpec& spec) {
    const double atlasWidth  = static_cast<double>(runtime.pack.sourceWidth);
    const double atlasHeight = static_cast<double>(runtime.pack.sourceHeight);

    // Sample pixel centres, not the neighbouring atlas region. This preserves
    // linear filtering without requiring a copied texture per manifest region.
    return RegionUv{
        Vector2D((spec.rect.x + 0.5) / atlasWidth, (spec.rect.y + 0.5) / atlasHeight),
        Vector2D((spec.rect.x + spec.rect.width - 0.5) / atlasWidth, (spec.rect.y + spec.rect.height - 0.5) / atlasHeight),
    };
}

void addOp(std::vector<DrawOp>& ops, const SP<Render::ITexture>& texture, const CBox& destination, const Vector2D& uvTopLeft,
           const Vector2D& uvBottomRight, uint8_t wrapX = WRAP_CLAMP_TO_EDGE, uint8_t wrapY = WRAP_CLAMP_TO_EDGE) {
    if (!texture || !texture->ok() || destination.w < 1.0 || destination.h < 1.0)
        return;

    ops.push_back(DrawOp{texture, destination, uvTopLeft, uvBottomRight, wrapX, wrapY});
}

void addRegion(std::vector<DrawOp>& ops, const SkinRuntime& runtime, const kitty_skins::RegionSpec& spec, const CBox& destination) {
    const RegionUv uv = sourceUv(runtime, spec);
    addOp(ops, runtime.atlas(spec.exactAtlas), destination, uv.topLeft, uv.bottomRight);
}

constexpr int kMaxRepeatTiles = 256;

void addRepeatedRegion(std::vector<DrawOp>& ops, const SkinRuntime& runtime, const kitty_skins::RegionSpec& spec,
                       const CBox& destination, bool horizontal, double naturalLength) {
    const double span = horizontal ? destination.w : destination.h;
    if (span < 1.0 || naturalLength < 1.0) {
        addRegion(ops, runtime, spec, destination);
        return;
    }

    const int tiles = static_cast<int>(std::ceil(span / naturalLength));
    if (tiles < 1 || tiles > kMaxRepeatTiles) {
        addRegion(ops, runtime, spec, destination);
        return;
    }

    const SP<Render::ITexture>& texture = runtime.atlas(spec.exactAtlas);
    const RegionUv             uv      = sourceUv(runtime, spec);

    for (int index = 0; index < tiles; ++index) {
        const double offset = index * naturalLength;
        const double length = std::min(naturalLength, span - offset);
        if (length < 1.0)
            break;

        const double fraction = length / naturalLength;
        const CBox piece = horizontal ? CBox(destination.x + offset, destination.y, length, destination.h)
                                      : CBox(destination.x, destination.y + offset, destination.w, length);
        const Vector2D partialBottomRight =
            horizontal ? Vector2D(uv.topLeft.x + (uv.bottomRight.x - uv.topLeft.x) * fraction, uv.bottomRight.y)
                       : Vector2D(uv.bottomRight.x, uv.topLeft.y + (uv.bottomRight.y - uv.topLeft.y) * fraction);
        addOp(ops, texture, piece, uv.topLeft, partialBottomRight);
    }
}

// One flexible run: regions share `span` in proportion to their declared source
// length. A repeating region is emitted as bounded atlas-sampling tiles; a
// stretching one is widened.
void fillFlexibleRun(std::vector<DrawOp>& ops, const std::vector<const kitty_skins::RegionSpec*>& specs, const SkinRuntime& runtime,
                     double start, double span, bool horizontal, double step, double crossStart, double crossSize) {
    if (span < 1.0 || crossSize < 1.0 || specs.empty())
        return;

    double sourceSum = 0.0;
    for (const kitty_skins::RegionSpec* spec : specs)
        sourceSum += horizontal ? spec->rect.width : spec->rect.height;
    if (sourceSum < 1.0)
        return;

    double cursor = start;
    for (const kitty_skins::RegionSpec* spec : specs) {
        const double sourceLength = horizontal ? spec->rect.width : spec->rect.height;
        const double share        = span * (sourceLength / sourceSum);
        const double natural      = sourceLength * step;

        const kitty_skins::RepeatAxis wanted = horizontal ? kitty_skins::RepeatAxis::horizontal : kitty_skins::RepeatAxis::vertical;
        const bool repeat = spec->repeat == wanted;

        const CBox destination = horizontal ? CBox(cursor, crossStart, share, crossSize) : CBox(crossStart, cursor, crossSize, share);
        if (repeat)
            addRepeatedRegion(ops, runtime, *spec, destination, horizontal, natural);
        else
            addRegion(ops, runtime, *spec, destination);

        cursor += share;
    }
}

}

LayoutGeometry resolveGeometry(const kitty_skins::SkinPack& pack, double layoutWidth, double layoutHeight) noexcept {
    LayoutGeometry geometry;
    geometry.layoutWidth  = layoutWidth;
    geometry.layoutHeight = layoutHeight;

    const kitty_skins::Insets& aperture = pack.aperture;
    const double openingW = static_cast<double>(pack.sourceWidth) - aperture.left - aperture.right;
    const double openingH = static_cast<double>(pack.sourceHeight) - aperture.top - aperture.bottom;

    if (openingW > 0.0 && openingH > 0.0 && layoutWidth > 0.0 && layoutHeight > 0.0) {
        // The extents are a fixed fraction of the outer box, so the layout-assigned
        // box IS the implied outer box: the exact-mode test is its own aspect.
        const double aspect      = layoutWidth / layoutHeight;
        const bool   bigEnough   = layoutWidth >= pack.exact.minWidth && layoutHeight >= pack.exact.minHeight;
        const bool   closeEnough = std::abs(aspect - pack.exact.aspect) <= pack.exact.aspectTolerance;

        if (bigEnough && closeEnough) {
            geometry.mode = LayoutMode::exact;
            geometry.extents =
                kitty_skins::Insets{aperture.left / static_cast<double>(pack.sourceWidth) * layoutWidth,
                                    aperture.right / static_cast<double>(pack.sourceWidth) * layoutWidth,
                                    aperture.top / static_cast<double>(pack.sourceHeight) * layoutHeight,
                                    aperture.bottom / static_cast<double>(pack.sourceHeight) * layoutHeight};
            return geometry;
        }
    }

    geometry.mode    = LayoutMode::adaptive;
    const double s   = pack.adaptive.scale;
    geometry.extents = kitty_skins::Insets{aperture.left * s, aperture.right * s, aperture.top * s, aperture.bottom * s};

    // Adaptive frames only fit when the logical client left behind by the fixed
    // extents still meets the manifest minimum. The prospective client is the
    // layout-assigned box minus those extents, decided from layoutBox() alone so
    // the choice can never oscillate. Below the minimum the frame disables: zero
    // extents mean the positioner reserves nothing and no frame is drawn.
    const double clientWidth  = layoutWidth - (geometry.extents.left + geometry.extents.right);
    const double clientHeight = layoutHeight - (geometry.extents.top + geometry.extents.bottom);
    if (clientWidth < pack.adaptive.minClientWidth || clientHeight < pack.adaptive.minClientHeight) {
        geometry.enabled = false;
        geometry.extents = kitty_skins::Insets{0.0, 0.0, 0.0, 0.0};
    }

    return geometry;
}

void buildLayout(const SkinRuntime& runtime, const LayoutGeometry& geometry, const Vector2D& outerSize, const Vector2D& apertureOffset,
                 const Vector2D& apertureSize, float monitorScale, LayoutCache& out) {
    out.enabled      = geometry.enabled;
    out.mode         = geometry.mode;
    out.monitorScale = monitorScale;
    out.extents      = geometry.extents;
    out.operations.clear();

    const double scale = static_cast<double>(monitorScale);

    // Both boxes arrive already rounded by the compositor's own transform, so the
    // aperture sits exactly where the client surface is drawn: no edge is rounded
    // on its own here and the aperture can neither overlap nor gap the client.
    out.outerBox    = CBox(0.0, 0.0, outerSize.x, outerSize.y);
    out.apertureBox = CBox(apertureOffset.x, apertureOffset.y, apertureSize.x, apertureSize.y);

    // The clip that protects the client aperture: the whole outer frame minus the
    // opening. Built here, once per layout rebuild, and clipped out of every draw
    // op so no skin pixel can cover the terminal — even in exact mode where the
    // repaired atlas springs the opening on source alpha alone.
    // The ring shares the DrawOps' space: (0, 0) is the outer box's top-left, so
    // a single translation to the physical origin places both correctly.
    out.outerRing.clear();
    out.outerRing.add(out.outerBox);
    out.outerRing.subtract(Hyprutils::Math::CRegion(out.apertureBox));

    if (geometry.mode == LayoutMode::exact) {
        // Exactly one operation: the whole repaired source atlas over the whole
        // outer framebuffer. Its transparent aperture reveals the Kitty blit.
        if (runtime.exactAtlas && runtime.exactAtlas->ok())
            out.operations.push_back(DrawOp{runtime.exactAtlas, CBox(0.0, 0.0, out.outerBox.w, out.outerBox.h), Vector2D(0.0, 0.0),
                                            Vector2D(1.0, 1.0), WRAP_CLAMP_TO_EDGE, WRAP_CLAMP_TO_EDGE});
        return;
    }

    RoleIndex roles;
    indexRoles(runtime.pack, roles);
    if (!roles.corners[0] || !roles.corners[1] || !roles.corners[2] || !roles.corners[3])
        return;

    const double step       = runtime.pack.adaptive.scale * scale;
    const double outerW     = out.outerBox.w;
    const double outerH     = out.outerBox.h;
    const double bandTopH   = roles.corners[0]->rect.height * step;
    const double bandBotH   = roles.corners[2]->rect.height * step;
    const double bandLeftW  = roles.corners[0]->rect.width * step;
    const double bandRightW = roles.corners[1]->rect.width * step;

    // Corners: unique, placed once at their natural size.
    const auto placeCorner = [&](const kitty_skins::RegionSpec* spec, double x, double y) {
        addRegion(out.operations, runtime, *spec, CBox(x, y, spec->rect.width * step, spec->rect.height * step));
    };
    placeCorner(roles.corners[0], 0.0, 0.0);
    placeCorner(roles.corners[1], outerW - roles.corners[1]->rect.width * step, 0.0);
    placeCorner(roles.corners[2], 0.0, outerH - bandBotH);
    placeCorner(roles.corners[3], outerW - roles.corners[3]->rect.width * step, outerH - bandBotH);

    // Neutral rails fill only the span between the corners; anchored ornaments and
    // architecture are drawn over them afterwards.
    const double middleX = bandLeftW;
    const double middleW = std::max(0.0, outerW - bandLeftW - bandRightW);
    fillFlexibleRun(out.operations, roles.railTop, runtime, middleX, middleW, true, step, 0.0, bandTopH);
    fillFlexibleRun(out.operations, roles.railBottom, runtime, middleX, middleW, true, step, outerH - bandBotH, bandBotH);

    // Columns: a unique top cap, a stretched neutral shaft, then a unique bottom
    // cap flush with the bottom band.
    const double middleY = bandTopH;
    const double middleH = std::max(0.0, outerH - bandTopH - bandBotH);
    for (int side = 0; side < 2; ++side) {
        const double columnX  = side == 0 ? 0.0 : outerW - bandRightW;
        const double columnW  = side == 0 ? bandLeftW : bandRightW;
        const double capTopH  = roles.columnTop[side] ? roles.columnTop[side]->rect.height * step : 0.0;
        const double capBotH  = roles.columnBottom[side] ? roles.columnBottom[side]->rect.height * step : 0.0;
        const double shaftH   = std::max(0.0, middleH - capTopH - capBotH);

        if (roles.columnTop[side])
            addRegion(out.operations, runtime, *roles.columnTop[side], CBox(columnX, middleY, columnW, capTopH));

        fillFlexibleRun(out.operations, roles.columnMiddle[side], runtime, middleY + capTopH, shaftH, false, step, columnX, columnW);

        if (roles.columnBottom[side])
            addRegion(out.operations, runtime, *roles.columnBottom[side],
                      CBox(columnX, outerH - bandBotH - capBotH, columnW, capBotH));
    }

    // Anchored one-shot ornaments and architecture: emitted exactly once, never
    // tiled or repeated, lowest z first.
    std::stable_sort(roles.ornaments.begin(), roles.ornaments.end(),
                     [](const kitty_skins::RegionSpec* a, const kitty_skins::RegionSpec* b) { return a->zIndex < b->zIndex; });
    for (const kitty_skins::RegionSpec* spec : roles.ornaments) {

        const double width  = spec->rect.width * step;
        const double height = spec->rect.height * step;
        const double offX   = spec->offsetX * scale;
        const double offY   = spec->offsetY * scale;

        double x = 0.0;
        double y = 0.0;
        switch (spec->anchor) {
            case kitty_skins::Anchor::top_left: x = 0.0; y = 0.0; break;
            case kitty_skins::Anchor::top_center: x = (outerW - width) / 2.0; y = 0.0; break;
            case kitty_skins::Anchor::top_right: x = outerW - width; y = 0.0; break;
            case kitty_skins::Anchor::bottom_left: x = 0.0; y = outerH - height; break;
            case kitty_skins::Anchor::bottom_center: x = (outerW - width) / 2.0; y = outerH - height; break;
            case kitty_skins::Anchor::bottom_right: x = outerW - width; y = outerH - height; break;
        }

        addRegion(out.operations, runtime, *spec, CBox(x + offX, y + offY, width, height));
    }
}

}
