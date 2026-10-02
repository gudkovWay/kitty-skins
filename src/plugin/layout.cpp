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

// Source pixels to atlas UV. Samples pixel centres, not the neighbouring atlas
// region, which preserves linear filtering without requiring a copied texture per
// manifest region. The same rule serves both declared regions and the synthetic
// frame cuts of the opt-in exact path.
RegionUv atlasUv(const kitty_skins::SkinPack& pack, double x, double y, double width, double height) {
    const double atlasWidth  = static_cast<double>(pack.sourceWidth);
    const double atlasHeight = static_cast<double>(pack.sourceHeight);

    return RegionUv{
        Vector2D((x + 0.5) / atlasWidth, (y + 0.5) / atlasHeight),
        Vector2D((x + width - 0.5) / atlasWidth, (y + height - 0.5) / atlasHeight),
    };
}

RegionUv sourceUv(const SkinRuntime& runtime, const kitty_skins::RegionSpec& spec) {
    return atlasUv(runtime.pack, spec.rect.x, spec.rect.y, spec.rect.width, spec.rect.height);
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

// True when the runtime owns the region's compositing, so the layout must not
// emit an ordinary op for it. A flower pack declares at most four regions, so a
// linear scan is cheaper than any lookup structure.
bool referencesFlowerRegion(const kitty_skins::SkinPack& pack, const std::string& regionId) {
    for (const kitty_skins::FlowerEffectSpec& flower : pack.flowerEffects)
        if (flower.regionId == regionId)
            return true;
    return false;
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

// One flexible run: regions keep their manifest order. A fixed segment
// reserves its natural long-axis size (sourceLength * step); the remaining
// span is distributed proportionally among elastic segments by source length.
// If the span cannot fit the fixed reservations, fixed segments scale down
// proportionally to fit and elastic segments get zero — the boxes stay
// contiguous, never negative or overlapping. Fixed art is never tiled, so an
// all-fixed run with surplus span simply leaves the surplus unfilled.
// A repeating (always elastic) region is emitted as bounded atlas-sampling
// tiles; a stretching one is widened.
void fillFlexibleRun(std::vector<DrawOp>& ops, const std::vector<const kitty_skins::RegionSpec*>& specs, const SkinRuntime& runtime,
                     double start, double span, bool horizontal, double step, double crossStart, double crossSize) {
    if (span < 1.0 || crossSize < 1.0 || specs.empty())
        return;

    double fixedNatural = 0.0;  // sum of sourceLength * step over fixed segments
    double elasticSum   = 0.0;  // sum of source lengths over elastic segments
    for (const kitty_skins::RegionSpec* spec : specs) {
        const double sourceLength = horizontal ? spec->rect.width : spec->rect.height;
        if (spec->fixed)
            fixedNatural += sourceLength * step;
        else
            elasticSum += sourceLength;
    }

    const double fixedBudget = std::min(fixedNatural, span);
    const double elasticSpan = span - fixedBudget;
    const double fixedScale  = fixedNatural > 0.0 ? fixedBudget / fixedNatural : 0.0;

    // Degenerate run with no fixed reservations: nothing drawable, matching the
    // legacy early-out so old packs keep byte-identical geometry.
    if (fixedNatural <= 0.0 && elasticSum < 1.0)
        return;

    double cursor = start;
    for (const kitty_skins::RegionSpec* spec : specs) {
        const double sourceLength = horizontal ? spec->rect.width : spec->rect.height;

        double share = 0.0;
        if (spec->fixed)
            share = sourceLength * step * fixedScale;
        else if (elasticSpan > 0.0 && elasticSum > 0.0)
            share = elasticSpan * (sourceLength / elasticSum);

        if (share <= 0.0)
            continue; // undersized span: this segment gets no box

        const double natural = sourceLength * step;

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

LayoutGeometry resolveGeometry(const kitty_skins::SkinPack& pack, double layoutWidth, double layoutHeight, bool allowExact) noexcept {
    LayoutGeometry geometry;
    geometry.layoutWidth  = layoutWidth;
    geometry.layoutHeight = layoutHeight;

    // What has to be reserved is the physical frame, which an opt-in pack states
    // separately from the source bands it was measured on. Legacy packs have no
    // such field and keep reserving their aperture exactly as before.
    const kitty_skins::Insets& reserved = pack.frameInsets ? *pack.frameInsets : pack.aperture;
    const double openingW = static_cast<double>(pack.sourceWidth) - reserved.left - reserved.right;
    const double openingH = static_cast<double>(pack.sourceHeight) - reserved.top - reserved.bottom;

    if (openingW > 0.0 && openingH > 0.0 && layoutWidth > 0.0 && layoutHeight > 0.0) {
        // The extents are a fixed fraction of the outer box, so the layout-assigned
        // box IS the implied outer box: the exact-mode test is its own aspect.
        const double aspect      = layoutWidth / layoutHeight;
        const bool   bigEnough   = layoutWidth >= pack.exact.minWidth && layoutHeight >= pack.exact.minHeight;
        const bool   closeEnough = std::abs(aspect - pack.exact.aspect) <= pack.exact.aspectTolerance;

        if (allowExact && bigEnough && closeEnough) {
            geometry.mode = LayoutMode::exact;
            geometry.extents =
                kitty_skins::Insets{reserved.left / static_cast<double>(pack.sourceWidth) * layoutWidth,
                                    reserved.right / static_cast<double>(pack.sourceWidth) * layoutWidth,
                                    reserved.top / static_cast<double>(pack.sourceHeight) * layoutHeight,
                                    reserved.bottom / static_cast<double>(pack.sourceHeight) * layoutHeight};
            return geometry;
        }
    }

    geometry.mode    = LayoutMode::adaptive;
    const double s   = pack.adaptive.scale;
    geometry.extents = kitty_skins::Insets{reserved.left * s, reserved.right * s, reserved.top * s, reserved.bottom * s};

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
    out.candleBox.reset();
    out.accentBoxes.clear();
    out.flowerBoxes.clear();

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
        const double sourceW = static_cast<double>(runtime.pack.sourceWidth);
        const double sourceH = static_cast<double>(runtime.pack.sourceHeight);

        // Every legacy effect overlay sits at the same natural source-anchored box
        // in both exact sub-paths, so effect-local coordinates stay valid. The
        // ornament base itself is drawn only when the layout does not already
        // cover it: the legacy whole-atlas path contains it, while the opt-in
        // nine-slice frame never draws ornaments and needs it exactly once.
        const auto effectBox = [&](const std::string& regionId, kitty_skins::RegionSpec const** outRegion) {
            for (const kitty_skins::RegionSpec& region : runtime.pack.regions) {
                if (region.id != regionId)
                    continue;
                const double sx = out.outerBox.w / sourceW;
                const double sy = out.outerBox.h / sourceH;
                if (outRegion)
                    *outRegion = &region;
                return CBox(region.rect.x * sx, region.rect.y * sy, region.rect.width * sx, region.rect.height * sy);
            }
            return CBox();
        };

        // The layered frame is an exact-mode behaviour of the opt-in nine-slice
        // frame only: legacy packs keep their whole-atlas / effect-only placement.
        const bool layered = runtime.pack.frameInsets.has_value() && runtime.pack.layeredOrnaments;

        // Layered ornaments are placed once through their declared anchor with a
        // single uniform source scale, so their aspect survives an outer box that
        // deviates from the source aspect. They are never tiled or repeated.
        const double layeredScale = std::min(out.outerBox.w / sourceW, out.outerBox.h / sourceH);
        const auto   ornamentBox  = [&](const kitty_skins::RegionSpec& region) {
            const double width  = region.rect.width * layeredScale;
            const double height = region.rect.height * layeredScale;

            double x = 0.0;
            double y = 0.0;
            switch (region.anchor) {
                case kitty_skins::Anchor::top_left: x = 0.0; y = 0.0; break;
                case kitty_skins::Anchor::top_center: x = (out.outerBox.w - width) / 2.0; y = 0.0; break;
                case kitty_skins::Anchor::top_right: x = out.outerBox.w - width; y = 0.0; break;
                case kitty_skins::Anchor::bottom_left: x = 0.0; y = out.outerBox.h - height; break;
                case kitty_skins::Anchor::bottom_center: x = (out.outerBox.w - width) / 2.0; y = out.outerBox.h - height; break;
                case kitty_skins::Anchor::bottom_right: x = out.outerBox.w - width; y = out.outerBox.h - height; break;
                case kitty_skins::Anchor::center_left: x = 0.0; y = (out.outerBox.h - height) / 2.0; break;
                case kitty_skins::Anchor::center_right: x = out.outerBox.w - width; y = (out.outerBox.h - height) / 2.0; break;
            }
            return CBox(x + region.offsetX * scale, y + region.offsetY * scale, width, height);
        };

        // The runtime composites each flower region itself, so those regions must
        // never appear as ordinary draw ops (a ghost static ornament under the
        // moving layer). Their boxes still have to exist for damage and clipping,
        // one per effect in manifest order. A missing region is impossible after
        // validation; an empty box keeps the vector parallel regardless.
        const auto flowerRegionBox = [&](const std::string& regionId) {
            for (const kitty_skins::RegionSpec& region : runtime.pack.regions)
                if (region.id == regionId)
                    return ornamentBox(region);
            return CBox();
        };
        out.flowerBoxes.reserve(runtime.pack.flowerEffects.size());
        for (const kitty_skins::FlowerEffectSpec& flower : runtime.pack.flowerEffects)
            out.flowerBoxes.push_back(flowerRegionBox(flower.regionId));

        // At most one candle plus eight accents can reference regions, so a
        // fixed 9-entry stack table bounds the drawn-once check with no heap.
        constexpr size_t kMaxEffectRegions = 9;
        const kitty_skins::RegionSpec* baseDrawn[kMaxEffectRegions] = {};
        size_t                         baseDrawnCount               = 0;
        const auto placeEffect = [&](const std::string& regionId, bool isCandle) {
            const kitty_skins::RegionSpec* region = nullptr;
            const CBox                     box    = effectBox(regionId, &region);
            if (!region)
                return;
            if (runtime.pack.frameInsets) {
                bool alreadyDrawn = false;
                for (size_t index = 0; index < baseDrawnCount; ++index)
                    alreadyDrawn = alreadyDrawn || baseDrawn[index] == region;
                if (!alreadyDrawn && baseDrawnCount < kMaxEffectRegions) {
                    addRegion(out.operations, runtime, *region, box);
                    baseDrawn[baseDrawnCount++] = region;
                }
            }
            if (isCandle)
                out.candleBox = box;
            else
                out.accentBoxes.push_back(box);
        };

        if (!runtime.pack.frameInsets) {
            // Legacy pack: exactly one operation, the whole repaired source atlas
            // over the whole outer framebuffer. Its transparent aperture reveals
            // the Kitty blit.
            if (runtime.exactAtlas && runtime.exactAtlas->ok())
                out.operations.push_back(DrawOp{runtime.exactAtlas, CBox(0.0, 0.0, out.outerBox.w, out.outerBox.h),
                                                Vector2D(0.0, 0.0), Vector2D(1.0, 1.0), WRAP_CLAMP_TO_EDGE,
                                                WRAP_CLAMP_TO_EDGE});
        } else {
            // Opt-in pack: the frame is a physical set of stones, so the source bands
            // are cut at their measured aperture edges and stretched onto the actual
            // reserved bands (outerBox -> apertureBox). The centre is the client
            // opening and is never sampled, so no packed neutral tile can leak through.
            const SP<Render::ITexture>& frame = runtime.adaptiveAtlas;
            const kitty_skins::Insets&  src   = runtime.pack.aperture;

            const std::array<double, 4> srcX{0.0, src.left, sourceW - src.right, sourceW};
            const std::array<double, 4> srcY{0.0, src.top, sourceH - src.bottom, sourceH};
            const std::array<double, 4> dstX{out.outerBox.x, out.apertureBox.x,
                                             out.apertureBox.x + out.apertureBox.w, out.outerBox.x + out.outerBox.w};
            const std::array<double, 4> dstY{out.outerBox.y, out.apertureBox.y,
                                             out.apertureBox.y + out.apertureBox.h, out.outerBox.y + out.outerBox.h};

            for (int row = 0; row < 3; ++row) {
                for (int column = 0; column < 3; ++column) {
                    if (row == 1 && column == 1)
                        continue; // the opening belongs to the client, never to the atlas
                    const RegionUv uv = atlasUv(runtime.pack, srcX[column], srcY[row],
                                                srcX[column + 1] - srcX[column], srcY[row + 1] - srcY[row]);
                    addOp(out.operations, frame,
                          CBox(dstX[column], dstY[row], dstX[column + 1] - dstX[column], dstY[row + 1] - dstY[row]),
                          uv.topLeft, uv.bottomRight);
                }
            }

            if (layered) {
                // Opt-in layered frame: draw ALL ornament regions once, sorted by
                // z, each through its anchor and the uniform source scale. Flower
                // regions are skipped here: the runtime composites the full region
                // (stationary background, moving petals, fixed foreground), so an
                // ordinary op would ghost the stationary ornament underneath.
                std::vector<const kitty_skins::RegionSpec*> ornaments;
                ornaments.reserve(runtime.pack.regions.size());
                for (const kitty_skins::RegionSpec& region : runtime.pack.regions)
                    if (region.role == kitty_skins::RegionRole::ornament)
                        ornaments.push_back(&region);
                std::stable_sort(ornaments.begin(), ornaments.end(),
                                 [](const kitty_skins::RegionSpec* a, const kitty_skins::RegionSpec* b) {
                                     return a->zIndex < b->zIndex;
                                 });

                std::vector<std::optional<CBox>> accentPlaced(runtime.pack.accentEffects.size());
                for (const kitty_skins::RegionSpec* spec : ornaments) {
                    const CBox box = ornamentBox(*spec);
                    if (!referencesFlowerRegion(runtime.pack, spec->id))
                        addRegion(out.operations, runtime, *spec, box);
                    if (runtime.pack.candleEffect && spec->id == runtime.pack.candleEffect->regionId)
                        out.candleBox = box;
                    for (size_t accentIndex = 0; accentIndex < runtime.pack.accentEffects.size(); ++accentIndex)
                        if (spec->id == runtime.pack.accentEffects[accentIndex].regionId)
                            accentPlaced[accentIndex] = box;
                }
                for (std::optional<CBox>& placed : accentPlaced)
                    if (placed)
                        out.accentBoxes.push_back(*placed);
            }
        }

        // The layered pass above already placed candle/accent boxes from the same
        // anchor transform as the drawn ornaments, so the legacy effect-only
        // placement must not run and re-place them at a different box.
        if (!layered) {
            if (runtime.pack.candleEffect)
                placeEffect(runtime.pack.candleEffect->regionId, true);
            for (const kitty_skins::AccentEffectSpec& accent : runtime.pack.accentEffects)
                placeEffect(accent.regionId, false);
        }
        return;
    }

    RoleIndex roles;
    indexRoles(runtime.pack, roles);
    if (!roles.corners[0] || !roles.corners[1] || !roles.corners[2] || !roles.corners[3])
        return;

    const double step   = runtime.pack.adaptive.scale * scale;
    const double outerW = out.outerBox.w;
    const double outerH = out.outerBox.h;

    // An opt-in pack states a physical frame thicker (or thinner) than the source
    // bands it was measured on, so each band's CROSS dimension scales by
    // target/source: columns widen with the left/right factors, and horizontal
    // rails thicken with the top/bottom ones. A side with no source band cannot
    // grow and keeps factor 1; legacy packs have target == source, so every factor
    // is exactly 1 and nothing moves.
    const kitty_skins::Insets& sourceBands = runtime.pack.aperture;
    const kitty_skins::Insets& frameBands  = runtime.pack.frameInsets ? *runtime.pack.frameInsets : runtime.pack.aperture;
    const auto crossFactor = [](double source, double target) { return source > 0.0 ? target / source : 1.0; };
    const double factorLeft   = crossFactor(sourceBands.left, frameBands.left);
    const double factorRight  = crossFactor(sourceBands.right, frameBands.right);
    const double factorTop    = crossFactor(sourceBands.top, frameBands.top);
    const double factorBottom = crossFactor(sourceBands.bottom, frameBands.bottom);

    // Keep the existing shared band boundaries and each corner's own dimensions.
    // Factor 1 therefore preserves the layout even for asymmetric legacy packs.
    const double bandTopH   = roles.corners[0]->rect.height * step * factorTop;
    const double bandBotH   = roles.corners[2]->rect.height * step * factorBottom;
    const double bandLeftW  = roles.corners[0]->rect.width * step * factorLeft;
    const double bandRightW = roles.corners[1]->rect.width * step * factorRight;

    const auto placeCorner = [&](const kitty_skins::RegionSpec* spec, double x, double y, double factorX, double factorY) {
        addRegion(out.operations, runtime, *spec,
                  CBox(x, y, spec->rect.width * step * factorX, spec->rect.height * step * factorY));
    };
    placeCorner(roles.corners[0], 0.0, 0.0, factorLeft, factorTop);
    placeCorner(roles.corners[1], outerW - bandRightW, 0.0, factorRight, factorTop);
    placeCorner(roles.corners[2], 0.0, outerH - bandBotH, factorLeft, factorBottom);
    placeCorner(roles.corners[3], outerW - roles.corners[3]->rect.width * step * factorRight,
                outerH - bandBotH, factorRight, factorBottom);

    // Neutral rails fill only the span between the corners; anchored ornaments and
    // architecture are drawn over them afterwards.
    const double middleX = bandLeftW;
    const double middleW = std::max(0.0, outerW - bandLeftW - bandRightW);
    fillFlexibleRun(out.operations, roles.railTop, runtime, middleX, middleW, true, step, 0.0, bandTopH);
    fillFlexibleRun(out.operations, roles.railBottom, runtime, middleX, middleW, true, step, outerH - bandBotH, bandBotH);

    // Columns: a unique top cap, a tiled neutral shaft, then a unique bottom
    // cap flush with the scaled bottom band.
    const double columnY     = bandTopH;
    const double columnSpanH = std::max(0.0, outerH - bandTopH - bandBotH);
    for (int side = 0; side < 2; ++side) {
        const double columnX = side == 0 ? 0.0 : outerW - bandRightW;
        const double columnW = side == 0 ? bandLeftW : bandRightW;
        const double capTopH = roles.columnTop[side] ? roles.columnTop[side]->rect.height * step : 0.0;
        const double capBotH = roles.columnBottom[side] ? roles.columnBottom[side]->rect.height * step : 0.0;
        const double shaftH  = std::max(0.0, columnSpanH - capTopH - capBotH);

        if (roles.columnTop[side])
            addRegion(out.operations, runtime, *roles.columnTop[side], CBox(columnX, columnY, columnW, capTopH));

        fillFlexibleRun(out.operations, roles.columnMiddle[side], runtime, columnY + capTopH, shaftH, false, step, columnX, columnW);

        if (roles.columnBottom[side])
            addRegion(out.operations, runtime, *roles.columnBottom[side],
                      CBox(columnX, outerH - bandBotH - capBotH, columnW, capBotH));
    }

    // Anchored one-shot ornaments and architecture: emitted exactly once, never
    // tiled or repeated, lowest z first.
    std::stable_sort(roles.ornaments.begin(), roles.ornaments.end(),
                     [](const kitty_skins::RegionSpec* a, const kitty_skins::RegionSpec* b) { return a->zIndex < b->zIndex; });
    // Accent boxes are collected per accent, not per ornament, so the cache's
    // accentBoxes stay parallel to pack.accentEffects regardless of z order.
    std::vector<std::optional<CBox>> accentPlaced(runtime.pack.accentEffects.size());
    // Flower regions are composited whole by the runtime, so they emit no
    // ordinary op here either; their anchor-transformed boxes are collected per
    // effect to stay parallel to pack.flowerEffects.
    std::vector<std::optional<CBox>> flowerPlaced(runtime.pack.flowerEffects.size());
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
            case kitty_skins::Anchor::center_left: x = 0.0; y = (outerH - height) / 2.0; break;
            case kitty_skins::Anchor::center_right: x = outerW - width; y = (outerH - height) / 2.0; break;
        }

        const CBox box = CBox(x + offX, y + offY, width, height);

        bool flowerOwned = false;
        for (size_t flowerIndex = 0; flowerIndex < runtime.pack.flowerEffects.size(); ++flowerIndex)
            if (spec->id == runtime.pack.flowerEffects[flowerIndex].regionId) {
                flowerPlaced[flowerIndex] = box;
                flowerOwned               = true;
            }

        if (!flowerOwned)
            addRegion(out.operations, runtime, *spec, box);
        if (runtime.pack.candleEffect && spec->id == runtime.pack.candleEffect->regionId)
            out.candleBox = box;
        for (size_t accentIndex = 0; accentIndex < runtime.pack.accentEffects.size(); ++accentIndex)
            if (spec->id == runtime.pack.accentEffects[accentIndex].regionId)
                accentPlaced[accentIndex] = box;
    }

    for (const std::optional<CBox>& placed : flowerPlaced)
        out.flowerBoxes.push_back(placed.value_or(CBox()));
    for (std::optional<CBox>& placed : accentPlaced)
        if (placed)
            out.accentBoxes.push_back(*placed);
}

}
