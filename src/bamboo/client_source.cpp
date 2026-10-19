#include "client_source.hpp"

#include <algorithm>
#include <cmath>

#include <hyprland/src/config/ConfigValue.hpp>
#include <hyprland/src/output/Monitor.hpp>
#include <hyprland/src/desktop/view/Window.hpp>
#include <hyprland/src/layout/LayoutManager.hpp>
#include <hyprland/src/protocols/ColorManagement.hpp>
#include <hyprland/src/protocols/XDGShell.hpp>
#include <hyprland/src/protocols/core/Compositor.hpp>

namespace bamboo02 {

std::expected<ClientSource, std::string> clientSource(const PHLWINDOW& window, const PHLMONITOR& monitor, const Vector2D& projected) {
    if (!window || !window->m_isMapped)
        return std::unexpected("weather: mapped client is unavailable");
    if (!window->m_ruleApplicator || !window->m_ruleApplicator->noBlur().valueOrDefault())
        return std::unexpected("weather: target must use no_blur; bamboo02 supplies its own atmosphere blur");
    if (window->m_ruleApplicator->RGBX().valueOrDefault() || window->m_ruleApplicator->opaque().valueOrDefault())
        return std::unexpected("weather: forced opaque/RGBX client rendering is not qualified");
    const auto surface = window->resource();
    const auto owner = window->wlSurface();
    if (!surface || !owner || !surface->m_current.texture || !surface->m_current.texture->ok())
        return std::unexpected("weather: committed client texture is unavailable");
    if (owner->small())
        return std::unexpected("weather: centered small-surface rendering is not qualified");
    const auto& state = surface->m_current;
    const auto& texture = state.texture;
    if (surface->hasVisibleSubsurface())
        return std::unexpected("weather: subsurface composition requires a dedicated isolated capture");
    if (state.transform != WL_OUTPUT_TRANSFORM_NORMAL || texture->m_transform != HYPRUTILS_TRANSFORM_NORMAL)
        return std::unexpected("weather: transformed client buffer is not qualified");
    if (state.scale <= 0 || !std::isfinite(texture->m_size.x) || !std::isfinite(texture->m_size.y) ||
        texture->m_size.x <= 0 || texture->m_size.y <= 0)
        return std::unexpected("weather: invalid committed client buffer geometry");
    if (texture->m_type != Render::TEXTURE_RGBA && texture->m_type != Render::TEXTURE_RGBX &&
        texture->m_type != Render::TEXTURE_EXTERNAL)
        return std::unexpected("weather: client texture is not a qualified RGB(A) sampler");

    if (!monitor || !std::isfinite(monitor->m_scale) || monitor->m_scale <= 0)
        return std::unexpected("weather: invalid native monitor scale");
    if (state.bufferSize.x <= 0 || state.bufferSize.y <= 0)
        return std::unexpected("weather: native buffer size is unavailable");
    // Mirror pinned calculateUVForSurface. Native XDG UV cropping is disabled;
    // nontrivial geometry requires a separate native-placement qualification.
    if (const auto xdg = window->m_xdgSurface.lock(); xdg &&
        (xdg->m_current.geometry.x != 0 || xdg->m_current.geometry.y != 0 ||
         (xdg->m_current.geometry.w > 0 && xdg->m_current.geometry.size() != state.size)))
        return std::unexpected("weather: nontrivial XDG geometry/native placement is not qualified");
    Vector2D uvTL, uvBR(1,1);
    if (state.viewport.hasSource) {
        const auto& crop = state.viewport.source;
        uvTL = crop.pos() / state.bufferSize;
        uvBR = (crop.pos() + crop.size()) / state.bufferSize;
        if (uvBR.x < 0.00001 || uvBR.y < 0.00001) { uvTL = {}; uvBR = {1,1}; }
    }
    const bool resizing = g_layoutManager && g_layoutManager->dragController()->target() &&
        g_layoutManager->dragController()->mode() == MBIND_RESIZE;
    const bool fractionalMisalignment =
        std::floor(monitor->m_scale) != monitor->m_scale && state.scale == 1 &&
        projected != state.bufferSize && std::abs(projected.x-state.bufferSize.x) < 3 &&
        std::abs(projected.y-state.bufferSize.y) < 3 && !window->sizeAnimation()->isBeingAnimated() && !resizing;
    if (fractionalMisalignment) {
        // Native fractional-scale-v1 path cuts the one/two excess texels and
        // uses nearest sampling instead of stretching them across the opening.
        uvBR -= ((uvBR-uvTL)*state.bufferSize-projected) / state.bufferSize;
    } else {
        Vector2D expected = window->getReportedSize();
        if (state.viewport.hasDestination) expected = state.viewport.destination;
        else if (state.viewport.hasSource) expected = state.viewport.source.size();
        else if (expected != state.size) expected = state.size;
        expected = (expected * monitor->m_scale).round();
        if (expected.x <= 0 || expected.y <= 0 || projected.x <= 0 || projected.y <= 0)
            return std::unexpected("weather: invalid native projected/expected client size");
        const double wlScale = std::ceil(monitor->m_scale);
        const bool scaleUnaware = monitor->m_scale != 1.0 &&
            (wlScale == state.scale || !state.viewport.hasDestination);
        if (!scaleUnaware || wlScale == 1) {
            static const CConfigValue<Hyprlang::INT> expandEdges("render:expand_undersized_textures");
            const auto ratio = projected / expected;
            if (*expandEdges && !scaleUnaware && (ratio.x > 1 || ratio.y > 1))
                uvBR = uvBR * ratio.clamp(Vector2D(1,1),Vector2D(1000000,1000000));
            if (!window->m_animatingIn && (ratio.x < 1 || ratio.y < 1))
                uvBR = uvBR * ratio.clamp(Vector2D(0.0001,0.0001),Vector2D(1,1));
        }
    }
    if (!std::isfinite(uvTL.x) || !std::isfinite(uvTL.y) || !std::isfinite(uvBR.x) || !std::isfinite(uvBR.y) ||
        uvBR.x <= uvTL.x || uvBR.y <= uvTL.y)
        return std::unexpected("weather: invalid native client UV");
    ClientSource result;
    result.texture = texture;
    result.description = texture->m_imageDescription;
    if (!result.description && surface->m_colorManagement)
        result.description = NColorManagement::CImageDescription::from(surface->m_colorManagement->imageDescription());
    if (!result.description)
        result.description = NColorManagement::getDefaultImageDescription();
    if (!result.description)
        return std::unexpected("weather: native default client color description is unavailable");
    // Wayland client textures use source top-left UV as in native surface draws.
    result.uvTransform = {static_cast<float>(uvBR.x-uvTL.x), 0, 0, 0, static_cast<float>(uvBR.y-uvTL.y), 0,
                          static_cast<float>(uvTL.x), static_cast<float>(uvTL.y), 1};
    const float opacity = owner->m_alphaModifier * owner->m_overallOpacity;
    if (!std::isfinite(opacity)) return std::unexpected("weather: invalid surface opacity");
    result.opacity = std::clamp(opacity, 0.0F, 1.0F);
    result.opaque = texture->m_type == Render::TEXTURE_RGBX || texture->m_opaque;
    result.nearest = fractionalMisalignment || window->m_ruleApplicator->nearestNeighbor().valueOrDefault();
    result.rounding = std::max(0.0F, window->rounding());
    result.roundingPower = std::max(1.0F, window->roundingPower());
    result.colorMultiplier = window->m_ruleApplicator->noDim().valueOrDefault() || !window->m_dimPercent
        ? 1.0F : std::clamp(1.0F - window->m_dimPercent->value(), 0.0F, 1.0F);
    return result;
}

} // namespace bamboo02
