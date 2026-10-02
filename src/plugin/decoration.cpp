#include "decoration.hpp"

#include <cmath>
#include <numbers>
#include <utility>

#include <hyprland/src/Compositor.hpp>
#include <hyprland/src/debug/log/Logger.hpp>
#include <hyprland/src/desktop/rule/windowRule/WindowRuleApplicator.hpp>
#include <hyprland/src/desktop/view/Window.hpp>
#include <hyprland/src/helpers/memory/Memory.hpp>
#include <hyprland/src/managers/SessionLockManager.hpp>
#include <hyprland/src/render/OpenGL.hpp>
#include <hyprland/src/render/Renderer.hpp>
#include <hyprland/src/render/decorations/DecorationPositioner.hpp>

#include "kitty_skins/manifest.hpp"
#include "pass.hpp"
#include "state.hpp"

namespace kitty_skins::plugin {

namespace {

using Hyprutils::Math::Vector2D;
using NativeIntVar = Desktop::Types::COverridableVar<Config::INTEGER>;

// The native border size and rounding of one window are Hyprland-owned
// overridable variables with a fixed priority ladder; the plugin writes into the
// top slot only.
constexpr Desktop::Types::eOverridePriority NATIVE_SLOT = Desktop::Types::PRIORITY_SET_PROP;

// Write zero into the plugin's slot and report the value the slot already held,
// if any. Only a value that is already in the top slot is captured: a value from
// a lower-priority rule (or layout) must never be copied up, because clearing
// the top slot is what lets that rule take effect again.
bool takeNativeSlot(NativeIntVar& var, Config::INTEGER& prior) {
    const bool hadPrior = var.hasValue() && var.getPriority() == NATIVE_SLOT;
    if (hadPrior)
        prior = var.value();

    var.set(Config::INTEGER{0}, NATIVE_SLOT);
    return hadPrior;
}

// Give the plugin's slot back, but only while it still holds the zero the plugin
// wrote. A value anything else set in the meantime is left in place.
bool releaseNativeSlot(NativeIntVar& var, bool hadPrior, Config::INTEGER prior) {
    if (!var.hasValue() || var.getPriority() != NATIVE_SLOT || var.value() != 0)
        return false;

    if (hadPrior)
        var.set(prior, NATIVE_SLOT);
    else
        var.unset(NATIVE_SLOT);

    return true;
}

// Extents and mode come from the layout-assigned box, which is independent of the
// extents this decoration itself reserves, so the decision can never oscillate.
//
// Exactness is opt-in per window: the dedicated preview window keeps the
// auto-exact source-aspect test, and so does an ordinary window while the target
// names a single class. A global "*" target frames arbitrary windows, where a
// thin adaptive band is the only sane frame, so those ordinary windows never
// take the exact path. Routing every decision through this one helper keeps the
// reservation (positioning info) and the drawn layout in the same mode.
bool allowExactLayout(const PHLWINDOW& window) {
    return windowIsPreview(window) || !globalTargetEnabled();
}

LayoutGeometry geometryFor(const SkinRuntime& runtime, const PHLWINDOW& window) {
    const CBox layout = window->layoutBox();
    return resolveGeometry(runtime.pack, layout.w, layout.h, allowExactLayout(window));
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
    // Safety net for a frame destroyed while its window keeps living: give the
    // native slots back without re-evaluating the window, whose surrounding
    // state may be mid-update here. Every explicit removal path restores with a
    // refresh before the decoration is taken away, so this is normally a no-op.
    restoreNativeZeroing(false);
    forgetDecoration(this);
}

PHLWINDOW CSkinDecoration::window() const {
    return m_window.lock();
}

bool CSkinDecoration::wanted() const {
    const PHLWINDOW window = m_window.lock();
    if (!window)
        return false;

    // Without a runtime there are no pixels to draw; reserving extents then
    // would leave an empty frame band around the client. The runtime is resolved
    // per window, so the preview window draws the candidate pack even though the
    // global runtime is a different one.
    const SkinRuntime* runtime = runtimeFor(window).runtime;
    if (runtime == nullptr)
        return false;

    // The configured target decides which ordinary windows are framed; the
    // dedicated preview window is admitted by identity instead of by class.
    if (!windowMatchesTarget(window))
        return false;

    // Unsupported rotated/flipped outputs reserve no extents: the frame is drawn
    // in the same physical space as the client.
    const auto monitor = window->m_monitor.lock();
    if (!monitor || monitor->m_transform != WL_OUTPUT_TRANSFORM_NORMAL)
        return false;

    // Structural policy: hidden/unmapped, X11 border opt-out and
    // override-redirect, modal or transient dialogs, pinned/PiP surfaces, game
    // content types, the decoration rule opt-out, fullscreen/maximized content
    // and a locked session all exclude the window — attachment, extents and
    // drawing share this one predicate.
    if (!windowFramingEligible(window))
        return false;

    // A geometry that reserves and draws nothing — a client below the manifest
    // minimum, or an unsupported layout — is not "wanted": the native border and
    // rounding must stay. The decoration remains attached, so a later resize that
    // makes the frame fit re-enters this same path and takes the native slots.
    return geometryFor(*runtime, window).enabled;
}


SDecorationPositioningInfo CSkinDecoration::getPositioningInfo() {
    SDecorationPositioningInfo info;
    info.policy   = DECORATION_POSITION_STICKY;
    info.edges    = DECORATION_EDGE_TOP | DECORATION_EDGE_BOTTOM | DECORATION_EDGE_LEFT | DECORATION_EDGE_RIGHT;
    info.priority = 5000;
    info.reserved = true;

    const PHLWINDOW    window  = m_window.lock();
    const SkinRuntime* runtime = runtimeFor(window).runtime;

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
    // Re-evaluate the native slot ownership and the reserved extents with the
    // same predicate that decides drawing: a window that stopped being framed
    // (fullscreen, maximized, hidden, pinned, undecorated, locked session) gives
    // the native border and rounding back, a window that started being framed
    // takes them. Only a change acts, so a window that is merely moved or resized
    // pays for one predicate call.
    const bool frameWanted = wanted();
    if (frameWanted != m_lastWanted) {
        m_lastWanted = frameWanted;

        if (frameWanted) {
            applyNativeZeroing();
        } else {
            restoreNativeZeroing(false);

            // The frame band stops being reserved here: the positioner only learns
            // about that when it re-queries this decoration's positioning info.
            if (const PHLWINDOW window = m_window.lock(); window && g_pDecorationPositioner)
                g_pDecorationPositioner->forceRecalcFor(window);
        }
    }

    // Damage only besides that. The cached operations are outer-box-local, so a
    // moved or animating window stays valid and must not rebuild the layout; the
    // cache key already covers every size, scale, layout or generation change
    // that does need one.
    damageEntire();
}

void CSkinDecoration::applyNativeZeroing() {
    const PHLWINDOW window = m_window.lock();
    if (!window || !window->m_ruleApplicator)
        return;

    bool changed = false;

    if (!m_borderOwned) {
        m_borderHadPrior = takeNativeSlot(window->m_ruleApplicator->borderSize(), m_borderPrior);
        m_borderOwned    = true;
        changed          = true;
    }

    if (!m_roundingOwned) {
        m_roundingHadPrior = takeNativeSlot(window->m_ruleApplicator->rounding(), m_roundingPrior);
        m_roundingOwned    = true;
        changed            = true;
    }

    if (!changed)
        return;

    // Both values are cached on the window, so the write is inert until the
    // window is re-evaluated; the positioner is then asked for a fresh layout,
    // because the frame's own extents no longer compete with a native border.
    window->updateWindowData();
    if (g_pDecorationPositioner)
        g_pDecorationPositioner->forceRecalcFor(window);
}

void CSkinDecoration::restoreNativeZeroing(bool refresh) {
    const PHLWINDOW window = m_window.lock();
    if (!window || !window->m_ruleApplicator) {
        m_borderOwned = m_roundingOwned = false;
        m_borderHadPrior = m_roundingHadPrior = false;
        return;
    }

    bool changed = false;

    if (m_borderOwned) {
        if (releaseNativeSlot(window->m_ruleApplicator->borderSize(), m_borderHadPrior, m_borderPrior))
            changed = true;

        m_borderOwned    = false;
        m_borderHadPrior = false;
    }

    if (m_roundingOwned) {
        if (releaseNativeSlot(window->m_ruleApplicator->rounding(), m_roundingHadPrior, m_roundingPrior))
            changed = true;

        m_roundingOwned    = false;
        m_roundingHadPrior = false;
    }

    // A window that is closing has nothing left to re-evaluate; the slots are
    // still given back so no ownerless zero can outlive the frame.
    if (!changed || !refresh || !window->m_isMapped)
        return;

    window->updateWindowData();
    if (g_pDecorationPositioner)
        g_pDecorationPositioner->forceRecalcFor(window);
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
    m_lastEffectDraw = {};
    m_effectMonitor.reset();
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
    const PHLWINDOW     window   = m_window.lock();
    const SWindowRuntime resolved = runtimeFor(window);
    const SkinRuntime*  runtime  = resolved.runtime;

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
    const LayoutGeometry geometry = geometryFor(*runtime, window);
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
    const uint64_t generation = resolved.generation;

    // Every input the layout depends on takes part in the key, including each
    // physical quantity absolute rounding can change: the rounded outer size and
    // the rounded outer-local aperture offset and size, alongside the
    // layout-assigned size, monitor scale, mode/enabled state and the generation
    // of the runtime that owns this window. Position alone is deliberately
    // absent: the operations are outer-box-local, so a moved or animated window
    // stays valid.
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

bool CSkinDecoration::damageEffects(std::chrono::steady_clock::time_point now) {
    // A short render lease also stops animation behind fully occluding windows:
    // a culled pass no longer renews it, even if the workspace remains visible.
    const bool hasCandle = m_cache.candleBox.has_value();
    // Silver material motion leases the same clock: it only repaints while the
    // frame was actually drawn recently, and packs without it never enter this
    // branch.
    const bool hasSilver = [&] {
        if (!m_cacheValid || !wanted())
            return false;
        const PHLWINDOW window = m_window.lock();
        return window && silverFor(runtimeFor(window).runtime) != nullptr;
    }();
    // Layered flower regions repaint their whole box once per effect tick.
    const bool hasFlowers = !m_cache.flowerBoxes.empty();
    if (!m_cacheValid || (!hasCandle && m_cache.accentBoxes.empty() && !hasSilver && !hasFlowers) ||
        now - m_lastEffectDraw > std::chrono::milliseconds(120) || !wanted())
        return false;
    const auto          window   = m_window.lock();
    const SWindowRuntime resolved = runtimeFor(window);
    const auto          monitor  = m_effectMonitor.lock();
    if (!window || !resolved.runtime ||
        (resolved.runtime->accentTextures.empty() && !resolved.runtime->candles && resolved.runtime->flowers.empty() &&
         silverFor(resolved.runtime) == nullptr) || !monitor ||
        !monitor->m_dpmsStatus || !g_pHyprRenderer ||
        (g_pSessionLockManager && g_pSessionLockManager->isSessionLocked()) ||
        !g_pHyprRenderer->shouldRenderMonitor(monitor) ||
        !g_pHyprRenderer->shouldRenderWindow(window, monitor))
        return false;
    if (!window->m_pinned && (!window->m_workspace || !window->m_workspace->m_visible))
        return false;

    CBox outerPhysical;
    if (!computeOuterPhysical(monitor, outerPhysical))
        return false;
    CRegion damage;
    if (hasCandle)
        damage.add(*m_cache.candleBox);
    for (const CBox& box : m_cache.accentBoxes)
        damage.add(box);
    for (const CBox& box : m_cache.flowerBoxes)
        damage.add(box);
    // Silver motion repaints the whole frame ring once per effect tick.
    if (hasSilver)
        damage.add(m_cache.outerRing);
    damage.intersect(m_cache.outerRing);
    if (damage.empty())
        return false;
    damage.translate(outerPhysical.pos());
    damage.scale(1.0F / monitor->m_scale).translate(monitor->m_position).expand(1.0);
    g_pHyprRenderer->damageRegion(damage);
    return true;
}

void CSkinDecoration::renderPass(PHLMONITOR monitor, float const& alpha) {
    const PHLWINDOW    window  = m_window.lock();
    const SkinRuntime* runtime = runtimeFor(window).runtime;

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

    // Silver material motion: one cached transformed atlas per effect tick, so
    // every region of every window drawn this pass shares the same temporal
    // phase and no allocation happens per region, window or frame. Only the
    // sampled texture is substituted; layout, UVs, clipping and filter are
    // untouched. When a pack declares no silver effect this stays null and the
    // static path below is byte-for-byte the old one.
    SilverMaterialRenderer* silver = silverFor(runtime);
    const uint64_t          tick   = silver ? effectTick() : 0;

    // The silver pass can only renew the effect lease while the frame is
    // actually visible this frame: a fully culled pass never re-arms the clock.
    bool silverVisible = false;
    if (silver) {
        CRegion visible(ring);
        visible.intersect(R.damage);
        silverVisible = !visible.empty();
    }

    Render::GL::CHyprOpenGLImpl::STextureRenderData data;
    data.a             = alpha;
    data.allowCustomUV = true;
    data.allowDim      = false;
    data.clipRegion    = ring;

    for (const DrawOp& op : m_cache.operations) {
        if (!op.texture || !op.texture->ok())
            continue;

        SP<Render::ITexture> texture = op.texture;
        if (silver) {
            const auto transformed = silver->transformed(*op.texture, tick);
            if (transformed && transformed->ok())
                texture = transformed;
        }

        CBox destination = op.destination;
        destination.translate(origin);

        data.primarySurfaceUVTopLeft     = op.uvTopLeft;
        data.primarySurfaceUVBottomRight = op.uvBottomRight;
        data.wrapX                       = op.wrapX;
        data.wrapY                       = op.wrapY;

        Render::GL::g_pHyprOpenGL->renderTexture(texture, destination, data);
    }

    if (silver && silverVisible) {
        m_lastEffectDraw = std::chrono::steady_clock::now();
        m_effectMonitor  = monitor;
        armEffectClock();
    }

    // Layered flower effects: the pack declares one compositor per flower region,
    // and each cached composite already carries the stationary background and the
    // fixed root/opal foreground, so the ordinary operations above deliberately
    // emit no static base for these regions. One cached region composite per
    // effect tick, drawn with the whole-window alpha and the outer-ring/damage
    // clip exactly like the candle and accent overlays.
    if (!runtime->flowers.empty() && m_cache.flowerBoxes.size() == runtime->flowers.size()) {
        const uint64_t flowerTick = effectTick();
        for (size_t index = 0; index < runtime->flowers.size(); ++index) {
            FlowerRenderer* flower = runtime->flowers[index].get();
            if (!flower)
                continue;

            CBox destination = m_cache.flowerBoxes[index];
            destination.translate(origin);
            CRegion visible(destination);
            visible.intersect(ring).intersect(R.damage);
            if (visible.empty())
                continue;

            const auto composite = flower->frame(flowerTick);
            if (!composite || !composite->ok())
                continue;

            R.useNearestNeighbor = false;
            data.primarySurfaceUVTopLeft     = Vector2D(0.0, 0.0);
            data.primarySurfaceUVBottomRight = Vector2D(1.0, 1.0);
            data.wrapX = WRAP_CLAMP_TO_EDGE;
            data.wrapY = WRAP_CLAMP_TO_EDGE;
            Render::GL::g_pHyprOpenGL->renderTexture(composite, destination, data);

            m_lastEffectDraw = std::chrono::steady_clock::now();
            m_effectMonitor  = monitor;
            armEffectClock();
        }
    }

    if (runtime->candles && m_cache.candleBox) {
        CBox destination = *m_cache.candleBox;
        destination.translate(origin);
        CRegion visible(destination);
        visible.intersect(ring).intersect(R.damage);
        if (!visible.empty()) {
            const auto effect = runtime->candles->frame(effectTick());
            if (effect && effect->ok()) {
                R.useNearestNeighbor = false;
                data.primarySurfaceUVTopLeft = Vector2D(0.0, 0.0);
                data.primarySurfaceUVBottomRight = Vector2D(1.0, 1.0);
                data.wrapX = WRAP_CLAMP_TO_EDGE;
                data.wrapY = WRAP_CLAMP_TO_EDGE;
                Render::GL::g_pHyprOpenGL->renderTexture(effect, destination, data);
                m_lastEffectDraw = std::chrono::steady_clock::now();
                m_effectMonitor  = monitor;
                armEffectClock();
            }
        }
    }

    // Pulsing accents: each accent texture is drawn once over its ornament box,
    // clipped to the frame ring and this pass's damage, with an independent
    // cosine alpha from the pack-declared period, phase and opacity bounds. The
    // compositor's own alpha (fade, fullscreen, occlusion and monitor state
    // already folded into it by the caller) multiplies the pulse, and the
    // texture size always equals the destination, so the UVs stay unit.
    if (!runtime->accentTextures.empty() && m_cache.accentBoxes.size() == runtime->accentTextures.size()) {
        const uint64_t tick    = effectTick();
        const double   seconds = static_cast<double>(tick) * 0.034;
        for (size_t index = 0; index < runtime->accentTextures.size(); ++index) {
            const SP<Render::ITexture>&   texture = runtime->accentTextures[index];
            const kitty_skins::AccentEffectSpec& accent = runtime->pack.accentEffects[index];
            if (!texture || !texture->ok())
                continue;

            CBox destination = m_cache.accentBoxes[index];
            destination.translate(origin);
            CRegion visible(destination);
            visible.intersect(ring).intersect(R.damage);
            if (visible.empty())
                continue;

            const double pulse = 0.5 + 0.5 * std::cos(2.0 * std::numbers::pi * (seconds / accent.period + accent.phase));
            const float  fade  = static_cast<float>(accent.minOpacity +
                                                    (accent.maxOpacity - accent.minOpacity) * pulse);

            const float savedAlpha   = data.a;
            data.a                   = savedAlpha * fade;
            data.primarySurfaceUVTopLeft     = Vector2D(0.0, 0.0);
            data.primarySurfaceUVBottomRight = Vector2D(1.0, 1.0);
            data.wrapX = WRAP_CLAMP_TO_EDGE;
            data.wrapY = WRAP_CLAMP_TO_EDGE;
            R.useNearestNeighbor = false;
            Render::GL::g_pHyprOpenGL->renderTexture(texture, destination, data);
            data.a = savedAlpha;

            m_lastEffectDraw = std::chrono::steady_clock::now();
            m_effectMonitor  = monitor;
            armEffectClock();
        }
    }

    R.useNearestNeighbor = savedNearest;
}

}
