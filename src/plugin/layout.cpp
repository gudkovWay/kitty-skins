#include "layout.hpp"

#include <algorithm>
#include <cmath>
#include <string>

#include <hyprland/src/render/pass/TexPassElement.hpp>

namespace kitty_skins::plugin {

namespace {

using Hyprutils::Math::CBox;
using Hyprutils::Math::CRegion;
using Hyprutils::Math::Vector2D;

// A mirror edge is emulated with paired reversed quads because the GL sampler only
// offers clamp and repeat. Above this many tiles the edge falls back to a stretch.
constexpr int kMaxMirrorTiles = 64;

int toPhysical(double logical, double scale) {
    return static_cast<int>(std::lround(logical * scale));
}

int toPhysicalNonNegative(double logical, double scale) {
    return static_cast<int>(std::lround(std::max(0.0, logical) * scale));
}

int splitHalf(double value) {
    return static_cast<int>(std::lround(value / 2.0));
}

void addOp(std::vector<DrawOp>& ops, const TextureAsset& asset, const CBox& destination, const Vector2D& uvTopLeft, const Vector2D& uvBottomRight, uint8_t wrapX,
           uint8_t wrapY, int zIndex) {
    if (!asset.texture || destination.w < 1.0 || destination.h < 1.0)
        return;

    ops.push_back(DrawOp{asset.texture, destination, uvTopLeft, uvBottomRight, wrapX, wrapY, zIndex});
}

// One frame edge between the preserved corners. `horizontal` selects the repeating axis.
void addEdge(std::vector<DrawOp>& ops, const TextureAsset& asset, const CBox& destination, kitty_skins::EdgeMode mode, bool horizontal, double sliceScale) {
    if (!asset.texture || destination.w < 1.0 || destination.h < 1.0)
        return;

    const double span       = horizontal ? destination.w : destination.h;
    const double tileLength = (horizontal ? asset.sourceSize.x : asset.sourceSize.y) * sliceScale;

    const auto stretch = [&] { addOp(ops, asset, destination, Vector2D(0.0, 0.0), Vector2D(1.0, 1.0), WRAP_CLAMP_TO_EDGE, WRAP_CLAMP_TO_EDGE, 0); };

    if (mode == kitty_skins::EdgeMode::stretch || tileLength < 1.0) {
        stretch();
        return;
    }

    if (mode == kitty_skins::EdgeMode::tile) {
        const double repeats = span / tileLength;
        if (horizontal)
            addOp(ops, asset, destination, Vector2D(0.0, 0.0), Vector2D(repeats, 1.0), WRAP_REPEAT, WRAP_CLAMP_TO_EDGE, 0);
        else
            addOp(ops, asset, destination, Vector2D(0.0, 0.0), Vector2D(1.0, repeats), WRAP_CLAMP_TO_EDGE, WRAP_REPEAT, 0);
        return;
    }

    const int tiles = static_cast<int>(std::ceil(span / tileLength));
    if (tiles < 1 || tiles > kMaxMirrorTiles) {
        stretch();
        return;
    }

    for (int index = 0; index < tiles; ++index) {
        const double offset = index * tileLength;
        const double length = std::min(tileLength, span - offset);
        if (length < 1.0)
            break;

        const double fraction = std::min(1.0, length / tileLength);
        const bool   forward  = index % 2 == 0;

        const CBox piece = horizontal ? CBox(destination.x + offset, destination.y, length, destination.h)
                                      : CBox(destination.x, destination.y + offset, destination.w, length);

        if (horizontal)
            addOp(ops, asset, piece, forward ? Vector2D(0.0, 0.0) : Vector2D(1.0 - fraction, 0.0),
                  forward ? Vector2D(fraction, 1.0) : Vector2D(1.0, 1.0), WRAP_CLAMP_TO_EDGE, WRAP_CLAMP_TO_EDGE, 0);
        else
            addOp(ops, asset, piece, forward ? Vector2D(0.0, 0.0) : Vector2D(0.0, 1.0 - fraction),
                  forward ? Vector2D(1.0, fraction) : Vector2D(1.0, 1.0), WRAP_CLAMP_TO_EDGE, WRAP_CLAMP_TO_EDGE, 0);
    }
}

const kitty_skins::SpriteSpec* findSprite(const kitty_skins::SkinPack& pack, const std::string& id) {
    for (const kitty_skins::SpriteSpec& sprite : pack.sprites) {
        if (sprite.id == id)
            return &sprite;
    }

    return nullptr;
}

}

void buildLayout(const SkinRuntime& runtime, const kitty_skins::TierSpec& tier, double clientWidth, double clientHeight, float monitorScale, LayoutCache& out) {
    out.tier         = &tier;
    out.monitorScale = monitorScale;
    out.operations.clear();

    const double tierScale  = tier.scale;
    const double sliceScale = tierScale * static_cast<double>(monitorScale);

    const int clientW = std::max(1, toPhysicalNonNegative(clientWidth, monitorScale));
    const int clientH = std::max(1, toPhysicalNonNegative(clientHeight, monitorScale));
    const int left    = toPhysicalNonNegative(tier.extents.left, monitorScale);
    const int right   = toPhysicalNonNegative(tier.extents.right, monitorScale);
    const int top     = toPhysicalNonNegative(tier.extents.top, monitorScale);
    const int bottom  = toPhysicalNonNegative(tier.extents.bottom, monitorScale);

    out.outerBox       = CBox(-left, -top, clientW + left + right, clientH + top + bottom);
    out.decorationClip = CRegion(out.outerBox);
    out.decorationClip.subtract(CRegion(CBox(0, 0, clientW, clientH)));

    // With no band outside the client aperture there is nothing to paint: every layer is
    // clipped to this ring, so an empty ring must not fall through to unscissored draws.
    if (out.decorationClip.empty())
        return;

    const kitty_skins::Insets& slices     = runtime.pack.frame.slices;
    const int                  sliceLeft  = std::max(0, toPhysical(slices.left, sliceScale));
    const int                  sliceRight = std::max(0, toPhysical(slices.right, sliceScale));
    const int                  sliceTop   = std::max(0, toPhysical(slices.top, sliceScale));
    const int                  sliceBottom = std::max(0, toPhysical(slices.bottom, sliceScale));

    const CBox outer = out.outerBox;

    const TextureAsset& topLeft     = runtime.frameSlices[static_cast<size_t>(FrameSlice::topLeft)];
    const TextureAsset& topRight    = runtime.frameSlices[static_cast<size_t>(FrameSlice::topRight)];
    const TextureAsset& bottomRight = runtime.frameSlices[static_cast<size_t>(FrameSlice::bottomRight)];
    const TextureAsset& bottomLeft  = runtime.frameSlices[static_cast<size_t>(FrameSlice::bottomLeft)];
    const TextureAsset& topEdge     = runtime.frameSlices[static_cast<size_t>(FrameSlice::top)];
    const TextureAsset& rightEdge   = runtime.frameSlices[static_cast<size_t>(FrameSlice::right)];
    const TextureAsset& bottomEdge  = runtime.frameSlices[static_cast<size_t>(FrameSlice::bottom)];
    const TextureAsset& leftEdge    = runtime.frameSlices[static_cast<size_t>(FrameSlice::left)];

    // Corners keep their source proportions; edges fill the span between them.
    addOp(out.operations, topLeft, CBox(outer.x, outer.y, sliceLeft, sliceTop), Vector2D(0.0, 0.0), Vector2D(1.0, 1.0), WRAP_CLAMP_TO_EDGE, WRAP_CLAMP_TO_EDGE, 0);
    addOp(out.operations, topRight, CBox(outer.x + outer.w - sliceRight, outer.y, sliceRight, sliceTop), Vector2D(0.0, 0.0), Vector2D(1.0, 1.0), WRAP_CLAMP_TO_EDGE,
          WRAP_CLAMP_TO_EDGE, 0);
    addOp(out.operations, bottomRight, CBox(outer.x + outer.w - sliceRight, outer.y + outer.h - sliceBottom, sliceRight, sliceBottom), Vector2D(0.0, 0.0), Vector2D(1.0, 1.0),
          WRAP_CLAMP_TO_EDGE, WRAP_CLAMP_TO_EDGE, 0);
    addOp(out.operations, bottomLeft, CBox(outer.x, outer.y + outer.h - sliceBottom, sliceLeft, sliceBottom), Vector2D(0.0, 0.0), Vector2D(1.0, 1.0), WRAP_CLAMP_TO_EDGE,
          WRAP_CLAMP_TO_EDGE, 0);

    const double middleWidth  = outer.w - sliceLeft - sliceRight;
    const double middleHeight = outer.h - sliceTop - sliceBottom;

    addEdge(out.operations, topEdge, CBox(outer.x + sliceLeft, outer.y, middleWidth, sliceTop), runtime.pack.frame.edges[2], true, sliceScale);
    addEdge(out.operations, bottomEdge, CBox(outer.x + sliceLeft, outer.y + outer.h - sliceBottom, middleWidth, sliceBottom), runtime.pack.frame.edges[3], true, sliceScale);
    addEdge(out.operations, leftEdge, CBox(outer.x, outer.y + sliceTop, sliceLeft, middleHeight), runtime.pack.frame.edges[0], false, sliceScale);
    addEdge(out.operations, rightEdge, CBox(outer.x + outer.w - sliceRight, outer.y + sliceTop, sliceRight, middleHeight), runtime.pack.frame.edges[1], false, sliceScale);

    // Anchored sprites: one operation each, never repeated.
    for (const std::string& id : tier.visibleSprites) {
        const kitty_skins::SpriteSpec* spec = findSprite(runtime.pack, id);
        if (spec == nullptr)
            continue;

        const auto assetIt = runtime.sprites.find(id);
        if (assetIt == runtime.sprites.end())
            continue;

        const TextureAsset& asset = assetIt->second;
        if (!asset.texture || asset.sourceSize.x <= 0.0 || asset.sourceSize.y <= 0.0)
            continue;

        const double spriteScale = spec->scale * tierScale * static_cast<double>(monitorScale);
        const int    width       = std::max(1, toPhysical(asset.sourceSize.x, spriteScale));
        const int    height      = std::max(1, toPhysical(asset.sourceSize.y, spriteScale));

        int x = 0;
        int y = 0;
        switch (spec->anchor) {
            case kitty_skins::Anchor::top_left: y = 0; break;
            case kitty_skins::Anchor::top_center:
                x = splitHalf(outer.w - width);
                y = 0;
                break;
            case kitty_skins::Anchor::top_right:
                x = static_cast<int>(std::lround(outer.w)) - width;
                y = 0;
                break;
            case kitty_skins::Anchor::bottom_left: y = static_cast<int>(std::lround(outer.h)) - height; break;
            case kitty_skins::Anchor::bottom_center:
                x = splitHalf(outer.w - width);
                y = static_cast<int>(std::lround(outer.h)) - height;
                break;
            case kitty_skins::Anchor::bottom_right:
                x = static_cast<int>(std::lround(outer.w)) - width;
                y = static_cast<int>(std::lround(outer.h)) - height;
                break;
        }

        x += toPhysical(spec->offsetX, spriteScale);
        y += toPhysical(spec->offsetY, spriteScale);

        addOp(out.operations, asset, CBox(outer.x + x, outer.y + y, width, height), Vector2D(0.0, 0.0), Vector2D(1.0, 1.0), WRAP_CLAMP_TO_EDGE, WRAP_CLAMP_TO_EDGE,
              spec->zIndex);
    }

    std::stable_sort(out.operations.begin(), out.operations.end(), [](const DrawOp& a, const DrawOp& b) { return a.zIndex < b.zIndex; });
}

}
