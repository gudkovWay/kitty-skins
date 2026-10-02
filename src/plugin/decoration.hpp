#pragma once

#include <chrono>
#include <cstdint>
#include <string>

#include <hyprland/src/config/shared/Types.hpp>
#include <hyprland/src/desktop/DesktopTypes.hpp>
#include <hyprland/src/helpers/math/Math.hpp>
#include <hyprland/src/render/decorations/IHyprWindowDecoration.hpp>

#include "layout.hpp"

namespace kitty_skins::plugin {

// The frame for one decorated window. It owns the cached semantic layout and
// reserves extents through the positioner; rendering itself happens in one
// custom render-pass element per frame, which paints the cached operations
// directly into the compositor's current target. It never touches the client's
// surface, pass element or framebuffer.
class CSkinDecoration final : public IHyprWindowDecoration {
  public:
    explicit CSkinDecoration(PHLWINDOW window);
    ~CSkinDecoration() override;

    SDecorationPositioningInfo getPositioningInfo() override;
    void                       onPositioningReply(const SDecorationPositioningReply& reply) override;
    void                       draw(PHLMONITOR monitor, float const& alpha) override;
    eDecorationType            getDecorationType() override;
    void                       updateWindow(PHLWINDOW window) override;
    void                       damageEntire() override;
    eDecorationLayer           getDecorationLayer() override;
    uint64_t                   getDecorationFlags() override;
    std::string                getDisplayName() override;

    PHLWINDOW window() const;
    bool      wanted() const;

    // Global logical box of the frame, used for damage.
    CBox globalBoundingBox();

    // Monitor-local logical box of the frame, as reported to the render pass so
    // it can be simplified and culled like any other element.
    CBox monitorLogicalBoundingBox(PHLMONITOR monitor) const;

    // Paint the cached schema-2 operations into the monitor's current target.
    // Only reads layout state; never mutates the client's render data.
    void renderPass(PHLMONITOR monitor, float const& alpha);
    // Drop caches and release the shared atlas references held by the cached
    // operations immediately. Called when the runtime generation changes or the
    // pack reloads.
    void invalidate();

    // Damage only the animated overlay areas (candle and accents) if an effect
    // was actually visible recently.
    bool damageEffects(std::chrono::steady_clock::time_point now);

    // The framed window gives up the native border and rounding: the frame is the
    // visible edge, and a native border would sit between the client and the
    // frame band. Both are written as zero into the highest-priority override
    // slot and the window is re-evaluated, so the change takes effect in the
    // current frame. Idempotent: the slots are captured once per ownership.
    void applyNativeZeroing();

    // Hand the native border and rounding slots back. A slot is only written
    // when it still holds the zero this frame put there, so a value set by
    // anything else in the meantime survives untouched. `refresh` re-evaluates
    // the window so a restored value takes effect immediately; it is false on
    // paths where the compositor is already rebuilding the window.
    void restoreNativeZeroing(bool refresh);

  private:
    // The compositor's own outer box for this decoration: the stored reply, the
    // four-edge defined point and, for a non-pinned window, the workspace render
    // offset. Mirrors the built-in border decoration exactly.
    CBox assignedBoxGlobal() const;

    // Translate the global frame box into monitor-local physical pixels, adding
    // the workspace render offset and the floating offset and applying the
    // monitor scale. False when there is no valid frame to draw.
    bool computeOuterPhysical(PHLMONITOR monitor, CBox& out) const;

    // Translate the compositor's own main-surface (client) box into monitor-local
    // physical pixels with the identical transform used for the outer frame. The
    // aperture is derived from this rounded box, never from the reserved logical
    // extents rounded on their own, so the two boxes cannot disagree by a pixel.
    bool computeClientPhysical(PHLMONITOR monitor, CBox& out) const;

    bool ensureLayout(PHLMONITOR monitor);

    // Drop the cached layout and every shared atlas reference its operations
    // hold. Used by explicit invalidation and by every branch that stops drawing.
    void clearLayout();

    PHLWINDOWREF m_window;
    LayoutCache  m_cache;

    CBox m_assignedGeometry;
    bool m_assignedValid = false;

    Hyprutils::Math::Vector2D m_cachedLayout{-1.0, -1.0};
    // Rounded outer physical size and rounded outer-local aperture offset and
    // size: every physical quantity that absolute rounding can change, so the
    // key detects a real resize or scale change without a per-call invalidation.
    Hyprutils::Math::Vector2D m_cachedOuter{-1.0, -1.0};
    Hyprutils::Math::Vector2D m_cachedApertureOffset{-1.0, -1.0};
    Hyprutils::Math::Vector2D m_cachedApertureSize{-1.0, -1.0};
    float                     m_cachedScale      = 0.F;
    uint64_t                  m_cachedGeneration = 0;
    bool                      m_cacheValid       = false;
    PHLMONITORREF m_effectMonitor;
    std::chrono::steady_clock::time_point m_lastEffectDraw{};

    // Last eligibility decision, so update() only acts on a real change.
    bool m_lastWanted = false;

    // Native border/rounding ownership. `Owned` marks the slots this frame has
    // written; the optional prior value is captured only when the highest slot
    // already held one at that moment, never copied from a lower-priority rule,
    // so giving the slot back cannot clobber a changing window rule.
    bool            m_borderOwned      = false;
    bool            m_borderHadPrior   = false;
    Config::INTEGER m_borderPrior      = 0;
    bool            m_roundingOwned    = false;
    bool            m_roundingHadPrior = false;
    Config::INTEGER m_roundingPrior    = 0;
};

}
