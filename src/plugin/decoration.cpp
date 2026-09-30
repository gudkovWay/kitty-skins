#include "decoration.hpp"

#include <utility>

#include <hyprland/src/Compositor.hpp>
#include <hyprland/src/debug/log/Logger.hpp>
#include <hyprland/src/desktop/rule/windowRule/WindowRuleApplicator.hpp>
#include <hyprland/src/desktop/view/Window.hpp>
#include <hyprland/src/helpers/memory/Memory.hpp>
#include <hyprland/src/managers/fullscreen/FullscreenController.hpp>
#include <hyprland/src/render/OpenGL.hpp>
#include <hyprland/src/render/Renderer.hpp>
#include <hyprland/src/render/decorations/DecorationPositioner.hpp>

#include "kitty_skins/manifest.hpp"
#include "pass.hpp"
#include "state.hpp"

namespace kitty_skins::plugin {

namespace {

using Hyprutils::Math::Vector2D;

// Extents and mode come from the layout-assigned box, which is independent of the
// extents this decoration itself reserves, so the decision can never oscillate.
LayoutGeometry geometryFor(const SkinRuntime& runtime, const PHLWINDOW& window) {
    const CBox layout = window->layoutBox();
    return resolveGeometry(runtime.pack, layout.w, layout.h);
}

// The compositor's workspace render offset, applied to a global logical box of a
// non-pinned window exactly as the built-in border decoration does.
void applyWorkspaceOffset(CBox& box, const PHLWINDOW& window) {
    const auto workspace = window->m_workspace;
    if (!workspace || window->m_pinned)
        return;

    box.translate(workspace->m_renderOffset->value());
}

// Global logical -> monitor-local physical: add the floating offset the renderer
// applies during workspace animations, subtract the monitor origin, scale by the
// monitor scale and round to whole device pixels — the space the compositor
// draws in. Every box that must land on the client surface goes through this
// single transform, so no two boxes can round to a different pixel grid.
CBox toMonitorPhysical(PHLMONITOR monitor, CBox box, const PHLWINDOW& window) {
    box.translate(window->m_floatingOffset - monitor->m_position);
    box.scale(monitor->m_scale);
    box.round();
    return box;
}

}

CSkinDecoration::CSkinDecoration(PHLWINDOW window) : IHyprWindowDecoration(window), m_window(window) {
}

CSkinDecoration::~CSkinDecoration() {
    forgetDecoration(this);
}

PHLWINDOW CSkinDecoration::window() const {
    return m_window.lock();
}

bool CSkinDecoration::wanted() const {
    const PHLWINDOW window = m_window.lock();
    if (!window)
        return false;

    // Hyprland suppresses decoration drawing for X11 windows that opt out of
    // borders. Reserving extents for such a window would leave a blank frame band
    // around the client, so the frame is not wanted there either.
    if (window->m_X11DoesntWantBorders)
        return false;

    // Without a runtime there are no pixels to draw; reserving extents then
    // would leave an empty frame band around the client.
    if (currentRuntime() == nullptr)
        return false;

    // Unsupported rotated/flipped outputs reserve no extents: the frame is drawn
    // in the same physical space as the client.
    const auto monitor = window->m_monitor.lock();
    if (!monitor || monitor->m_transform != WL_OUTPUT_TRANSFORM_NORMAL)
        return false;

    if (!windowMatchesTarget(window))
        return false;

    if (!window->m_ruleApplicator || !window->m_ruleApplicator->decorate().valueOrDefault())
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
        const LayoutGeometry geometry = geometryFor(*runtime, window);
        info.desiredExtents.topLeft     = Vector2D(geometry.extents.left, geometry.extents.top);
        info.desiredExtents.bottomRight = Vector2D(geometry.extents.right, geometry.extents.bottom);
    }

    return info;
}

void CSkinDecoration::onPositioningReply(const SDecorationPositioningReply& reply) {
    m_assignedGeometry = reply.assignedGeometry;
    m_assignedValid    = reply.assignedGeometry.w > 0.0 && reply.assignedGeometry.h > 0.0;
    damageEntire();
}

void CSkinDecoration::draw(PHLMONITOR monitor, float const& alpha) {
    if (!monitor)
        return;

    // The frame is not wanted any more (fullscreen, hidden, undecorated, wrong
    // target, missing runtime): release the cached operations so the atlas
    // references they own do not outlive the transition, and enqueue no pass.
    if (!wanted()) {
        clearLayout();
        return;
    }

    // Enqueue exactly one pass per frame, and only when there is a frame to draw.
    if (alpha <= 0.F)
        return;

    if (!ensureLayout(monitor) || m_cache.operations.empty())
        return;

    g_pHyprRenderer->addPassElement(makeUnique<CSkinPassElement>(this, alpha));
}

eDecorationType CSkinDecoration::getDecorationType() {
    return DECORATION_CUSTOM;
}

void CSkinDecoration::updateWindow(PHLWINDOW) {
    // Damage only. The cached operations are outer-box-local, so a moved or
    // animating window stays valid and must not rebuild the layout; the cache key
    // already covers every size, scale, layout or generation change that does
    // need one.
    damageEntire();
}

void CSkinDecoration::damageEntire() {
    const CBox bounds = globalBoundingBox();
    if (bounds.w < 1.0 || bounds.h < 1.0)
        return;

    g_pHyprRenderer->damageBox(bounds.copy().expand(2.0));
}

eDecorationLayer CSkinDecoration::getDecorationLayer() {
    return DECORATION_LAYER_OVER;
}

uint64_t CSkinDecoration::getDecorationFlags() {
    // The atlas is intentionally transparent outside its carved pixels. Treating
    // it as a seamless part of the client makes Hyprland's opaque shadow fill the
    // entire reserved frame rectangle behind those transparent pixels.
    return 0;
}

std::string CSkinDecoration::getDisplayName() {
    return "Kitty skin";
}

void CSkinDecoration::invalidate() {
    clearLayout();
}

void CSkinDecoration::clearLayout() {
    // Replacing the cache releases the DrawOps' shared atlas references right
    // away; resetting the key fields forces the next frame to rebuild.
    m_cache                = LayoutCache{};
    m_cacheValid           = false;
    m_cachedLayout         = Vector2D(-1.0, -1.0);
    m_cachedOuter          = Vector2D(-1.0, -1.0);
    m_cachedApertureOffset = Vector2D(-1.0, -1.0);
    m_cachedApertureSize   = Vector2D(-1.0, -1.0);
    m_cachedScale          = 0.F;
    m_cachedGeneration     = 0;
}

CBox CSkinDecoration::globalBoundingBox() {
    return assignedBoxGlobal();
}

CBox CSkinDecoration::monitorLogicalBoundingBox(PHLMONITOR monitor) const {
    const PHLWINDOW window = m_window.lock();
    if (!monitor || !window)
        return {};

    CBox box = assignedBoxGlobal();
    box.translate(window->m_floatingOffset - monitor->m_position);
    return box;
}

CBox CSkinDecoration::assignedBoxGlobal() const {
    CBox box = m_assignedGeometry;

    const PHLWINDOW window = m_window.lock();
    if (!window)
        return box;

    box.translate(g_pDecorationPositioner->getEdgeDefinedPoint(
        DECORATION_EDGE_TOP | DECORATION_EDGE_BOTTOM | DECORATION_EDGE_LEFT | DECORATION_EDGE_RIGHT, m_window));

    applyWorkspaceOffset(box, window);

    return box;
}

bool CSkinDecoration::computeOuterPhysical(PHLMONITOR monitor, CBox& out) const {
    const PHLWINDOW window = m_window.lock();
    if (!monitor || !window || !m_assignedValid)
        return false;

    if (monitor->m_transform != WL_OUTPUT_TRANSFORM_NORMAL)
        return false;

    if (monitor->m_scale <= 0.0)
        return false;

    CBox box = assignedBoxGlobal();
    if (box.w < 1.0 || box.h < 1.0)
        return false;

    box = toMonitorPhysical(monitor, box, window);

    if (box.w < 1.0 || box.h < 1.0)
        return false;

    out = box;
    return true;
}

bool CSkinDecoration::computeClientPhysical(PHLMONITOR monitor, CBox& out) const {
    const PHLWINDOW window = m_window.lock();
    if (!monitor || !window)
        return false;

    if (monitor->m_transform != WL_OUTPUT_TRANSFORM_NORMAL)
        return false;

    if (monitor->m_scale <= 0.0)
        return false;

    CBox box = window->getWindowMainSurfaceBox();
    if (box.w < 1.0 || box.h < 1.0)
        return false;

    applyWorkspaceOffset(box, window);
    box = toMonitorPhysical(monitor, box, window);

    if (box.w < 1.0 || box.h < 1.0)
        return false;

    out = box;
    return true;
}

bool CSkinDecoration::ensureLayout(PHLMONITOR monitor) {
    const PHLWINDOW    window  = m_window.lock();
    const SkinRuntime* runtime = currentRuntime();

    if (!window || !monitor || !runtime) {
        clearLayout();
        return false;
    }

    if (monitor->m_transform != WL_OUTPUT_TRANSFORM_NORMAL || monitor->m_scale <= 0.0) {
        clearLayout();
        return false;
    }

    // The outer frame and the client aperture are two global logical boxes put
    // through the identical monitor-local physical transform. The aperture is
    // derived from the compositor's own rounded client box, never by rounding the
    // reserved extents on their own, so a fractional scale or a subpixel position
    // cannot open a one-pixel gap or overlap between frame and client.
    CBox outerPhysical;
    if (!computeOuterPhysical(monitor, outerPhysical)) {
        clearLayout();
        return false;
    }

    CBox clientPhysical;
    if (!computeClientPhysical(monitor, clientPhysical)) {
        clearLayout();
        return false;
    }

    const CBox           layout   = window->layoutBox();
    const LayoutGeometry geometry = resolveGeometry(runtime->pack, layout.w, layout.h);
    const double         scale    = monitor->m_scale;

    // A disabled adaptive frame (client below the manifest minimum) reserves and
    // draws nothing: drop any previously cached layout so no stale operations —
    // and no atlas reference they hold — survive, and report no layout to draw.
    if (!geometry.enabled) {
        clearLayout();
        return false;
    }

    const Vector2D outerSize(outerPhysical.w, outerPhysical.h);
    const Vector2D apertureOffset(clientPhysical.x - outerPhysical.x, clientPhysical.y - outerPhysical.y);
    const Vector2D apertureSize(clientPhysical.w, clientPhysical.h);
    const uint64_t generation = g_pState ? g_pState->generation : 0;

    // Every input the layout depends on takes part in the key, including each
    // physical quantity absolute rounding can change: the rounded outer size and
    // the rounded outer-local aperture offset and size, alongside the
    // layout-assigned size, monitor scale, mode/enabled state and runtime
    // generation. Position alone is deliberately absent: the operations are
    // outer-box-local, so a moved or animated window stays valid.
    if (m_cacheValid && m_cache.enabled == geometry.enabled && m_cache.mode == geometry.mode && m_cachedLayout.x == layout.w && m_cachedLayout.y == layout.h &&
        m_cachedScale == scale && m_cachedGeneration == generation && m_cachedOuter == outerSize && m_cachedApertureOffset == apertureOffset &&
        m_cachedApertureSize == apertureSize)
        return true;

    buildLayout(*runtime, geometry, outerSize, apertureOffset, apertureSize, static_cast<float>(scale), m_cache);

    m_cachedLayout         = Vector2D(layout.w, layout.h);
    m_cachedOuter          = outerSize;
    m_cachedApertureOffset = apertureOffset;
    m_cachedApertureSize   = apertureSize;
    m_cachedScale          = static_cast<float>(scale);
    m_cachedGeneration     = generation;
    m_cacheValid           = true;
    return true;
}

void CSkinDecoration::renderPass(PHLMONITOR monitor, float const& alpha) {
    const PHLWINDOW    window  = m_window.lock();
    const SkinRuntime* runtime = currentRuntime();

    if (!monitor || !window || !runtime || alpha <= 0.F)
        return;

    if (!ensureLayout(monitor) || m_cache.operations.empty())
        return;

    CBox outerPhysical;
    if (!computeOuterPhysical(monitor, outerPhysical))
        return;

    if (!Render::GL::g_pHyprOpenGL || !g_pHyprRenderer)
        return;

    const Vector2D origin = outerPhysical.pos();

    // One copy of the cached ring per pass, moved to the absolute physical origin.
    // It is never rebuilt from boxes here, and never per operation.
    Hyprutils::Math::CRegion ring = m_cache.outerRing;
    ring.translate(origin);

    auto& R = g_pHyprRenderer->m_renderData;

    const bool savedNearest = R.useNearestNeighbor;
    R.useNearestNeighbor    = runtime->pack.filter == kitty_skins::Filter::nearest;

    Render::GL::CHyprOpenGLImpl::STextureRenderData data;
    data.a             = alpha;
    data.allowCustomUV = true;
    data.allowDim      = false;
    data.clipRegion    = ring;

    for (const DrawOp& op : m_cache.operations) {
        if (!op.texture || !op.texture->ok())
            continue;

        CBox destination = op.destination;
        destination.translate(origin);

        data.primarySurfaceUVTopLeft     = op.uvTopLeft;
        data.primarySurfaceUVBottomRight = op.uvBottomRight;
        data.wrapX                       = op.wrapX;
        data.wrapY                       = op.wrapY;

        Render::GL::g_pHyprOpenGL->renderTexture(op.texture, destination, data);
    }

    R.useNearestNeighbor = savedNearest;
}

}
