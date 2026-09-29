#include "decoration.hpp"
#include "kitty_skins/manifest.hpp"

#include <algorithm>
#include <utility>

#include <hyprland/src/Compositor.hpp>
#include <hyprland/src/debug/log/Logger.hpp>
#include <hyprland/src/desktop/rule/windowRule/WindowRuleApplicator.hpp>
#include <hyprland/src/desktop/view/Window.hpp>
#include <hyprland/src/managers/fullscreen/FullscreenController.hpp>
#include <hyprland/src/render/OpenGL.hpp>
#include <hyprland/src/render/Renderer.hpp>
#include <hyprland/src/render/pass/TexPassElement.hpp>

#include "pass.hpp"
#include "state.hpp"

namespace kitty_skins::plugin {

namespace {

using Hyprutils::Math::Vector2D;

// Tier selection always measures the layout-assigned box, which is independent of the
// extents this decoration itself reserves. Selecting from the reserved client box would
// let a tier change shrink the client box, re-select a smaller tier and oscillate.
kitty_skins::LogicalSize tierSelectionSize(const PHLWINDOW& window) {
    const CBox layout = window->layoutBox();
    if (layout.w > 0.0 && layout.h > 0.0)
        return kitty_skins::LogicalSize{layout.w, layout.h};

    const CBox client = window->getWindowMainSurfaceBox();
    return kitty_skins::LogicalSize{client.w, client.h};
}

const kitty_skins::TierSpec& selectedTier(const SkinRuntime& runtime, const PHLWINDOW& window) {
    return kitty_skins::selectTier(runtime.pack, tierSelectionSize(window));
}

}

CSkinDecoration::CSkinDecoration(PHLWINDOW window) : IHyprWindowDecoration(window), m_window(window) {
}

CSkinDecoration::~CSkinDecoration() {
    if (g_pState)
        std::erase(g_pState->decorations, this);
}

PHLWINDOW CSkinDecoration::window() const {
    return m_window.lock();
}

bool CSkinDecoration::wanted() const {
    const PHLWINDOW window = m_window.lock();
    if (!window)
        return false;

    if (!window->m_isMapped || window->isHidden())
        return false;

    if (window->m_class != configuredClass())
        return false;

    if (!window->m_ruleApplicator->decorate().valueOrDefault())
        return false;

    return !Fullscreen::controller()->isFullscreen(window, Fullscreen::FSMODE_FULLSCREEN);
}

SDecorationPositioningInfo CSkinDecoration::getPositioningInfo() {
    SDecorationPositioningInfo info;
    info.policy   = DECORATION_POSITION_STICKY;
    info.edges    = DECORATION_EDGE_TOP | DECORATION_EDGE_BOTTOM | DECORATION_EDGE_LEFT | DECORATION_EDGE_RIGHT;
    info.priority = 5000;
    info.reserved = true;

    const PHLWINDOW    window  = m_window.lock();
    const SkinRuntime* runtime = currentRuntime();

    if (window && runtime && wanted()) {
        const kitty_skins::TierSpec& tier     = selectedTier(*runtime, window);
        info.desiredExtents.topLeft           = Vector2D(tier.extents.left, tier.extents.top);
        info.desiredExtents.bottomRight       = Vector2D(tier.extents.right, tier.extents.bottom);
    }

    return info;
}

void CSkinDecoration::onPositioningReply(const SDecorationPositioningReply&) {
    // The decoration geometry is derived from the client box plus the tier extents, so
    // the reply only needs to force a redraw of the reserved band.
    damageEntire();
}

void CSkinDecoration::draw(PHLMONITOR monitor, float const& alpha) {
    if (!wanted() || !ensureLayout(monitor))
        return;

    if (m_cache.operations.empty())
        return;

    g_pHyprRenderer->addPassElement(makeUnique<CSkinPassElement>(CSkinPassElement::SData{this, alpha}));
}

eDecorationType CSkinDecoration::getDecorationType() {
    return DECORATION_CUSTOM;
}

void CSkinDecoration::updateWindow(PHLWINDOW) {
    // Only the client size, the selected tier, the monitor scale and the runtime
    // generation require a rebuild; ensureLayout() compares exactly those keys, so a
    // pure position change is handled by the draw-time translation.
    damageEntire();
}

void CSkinDecoration::damageEntire() {
    const CBox bounds = globalBoundingBox();
    if (bounds.w < 1.0 || bounds.h < 1.0)
        return;

    g_pHyprRenderer->damageBox(bounds);
}

eDecorationLayer CSkinDecoration::getDecorationLayer() {
    return DECORATION_LAYER_OVER;
}

uint64_t CSkinDecoration::getDecorationFlags() {
    return DECORATION_PART_OF_MAIN_WINDOW;
}

std::string CSkinDecoration::getDisplayName() {
    return "Kitty skin";
}

void CSkinDecoration::invalidate() {
    m_cacheValid = false;
    m_cache.tier = nullptr;
    m_cache.operations.clear();
}

bool CSkinDecoration::ensureLayout(PHLMONITOR monitor) {
    const PHLWINDOW    window  = m_window.lock();
    const SkinRuntime* runtime = currentRuntime();

    if (!window || !monitor || !runtime) {
        m_cacheValid = false;
        return false;
    }

    const CBox                    client   = window->getWindowMainSurfaceBox();
    const kitty_skins::LogicalSize clientSize{client.w, client.h};
    const kitty_skins::TierSpec&   tier       = selectedTier(*runtime, window);
    const float                    scale      = monitor->m_scale;
    const uint64_t                 generation = g_pState->generation;

    if (m_cacheValid && m_cache.tier == &tier && m_cachedClient.width == clientSize.width && m_cachedClient.height == clientSize.height &&
        m_cache.monitorScale == scale && m_cachedGeneration == generation)
        return true;

    buildLayout(*runtime, tier, clientSize.width, clientSize.height, scale, m_cache);
    m_cachedClient     = clientSize;
    m_cachedGeneration = generation;
    m_cacheValid       = true;
    return true;
}

CBox CSkinDecoration::globalBoundingBox() {
    const PHLWINDOW    window  = m_window.lock();
    const SkinRuntime* runtime = currentRuntime();
    if (!window || !runtime)
        return {};

    const kitty_skins::TierSpec& tier   = selectedTier(*runtime, window);
    const CBox                   client = window->getWindowMainSurfaceBox();

    return CBox(client.x - tier.extents.left, client.y - tier.extents.top, client.w + tier.extents.left + tier.extents.right,
                client.h + tier.extents.top + tier.extents.bottom);
}

void CSkinDecoration::drawPass(PHLMONITOR monitor, float const& alpha) {
    if (!wanted() || !ensureLayout(monitor) || m_cache.operations.empty())
        return;

    const PHLWINDOW    window  = m_window.lock();
    const SkinRuntime* runtime = currentRuntime();
    if (!window || !runtime)
        return;

    CRegion&   renderDataDamage = g_pHyprRenderer->m_renderData.damage;
    const bool previousNearest  = g_pHyprRenderer->m_renderData.useNearestNeighbor;
    const bool nearest          = runtime->pack.filter == kitty_skins::Filter::nearest;

    // The cached operations are client-relative; only the rounded physical origin is
    // resolved here, so moving the window never rebuilds the layout.
    Vector2D origin = (window->getWindowMainSurfaceBox().pos() + window->m_floatingOffset - monitor->m_position) * monitor->m_scale;
    origin          = origin.round();

    CRegion clip = m_cache.decorationClip;
    clip.translate(origin);

    g_pHyprRenderer->m_renderData.useNearestNeighbor = nearest;

    for (const DrawOp& operation : m_cache.operations) {
        if (!operation.texture || !operation.texture->ok())
            continue;

        CBox destination = operation.destination;
        destination.translate(origin);
        if (destination.w < 1.0 || destination.h < 1.0)
            continue;

        Render::GL::CHyprOpenGLImpl::STextureRenderData textureData;
        textureData.damage                      = &renderDataDamage;
        textureData.a                           = alpha;
        textureData.allowCustomUV               = true;
        textureData.wrapX                       = operation.wrapX;
        textureData.wrapY                       = operation.wrapY;
        textureData.primarySurfaceUVTopLeft     = operation.uvTopLeft;
        textureData.primarySurfaceUVBottomRight = operation.uvBottomRight;
        textureData.clipRegion                  = clip;
        textureData.discardMode                 = DISCARD_ALPHA;

        Render::GL::g_pHyprOpenGL->renderTexture(operation.texture, destination, textureData);
    }

    g_pHyprRenderer->m_renderData.useNearestNeighbor = previousNearest;
}

}
