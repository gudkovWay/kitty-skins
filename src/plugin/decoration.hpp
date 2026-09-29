#pragma once

#include <cstdint>
#include <string>

#include <hyprland/src/desktop/DesktopTypes.hpp>
#include <hyprland/src/helpers/math/Math.hpp>
#include <hyprland/src/render/decorations/IHyprWindowDecoration.hpp>

#include "layout.hpp"

namespace kitty_skins::plugin {

// The Kitty frame for one window. Owns the cached layout; the cached draw operations
// keep shared references to the textures of the runtime generation they were built for.
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

    // Called by CSkinPassElement while the renderer state is prepared for this draw.
    void drawPass(PHLMONITOR monitor, float const& alpha);

    // Global logical decoration box, used for damage and pass bounding boxes.
    CBox globalBoundingBox();

    // Drop the cached layout; the next draw rebuilds it against the current runtime.
    void invalidate();

    PHLWINDOW window() const;
    bool      wanted() const;

  private:
    bool ensureLayout(PHLMONITOR monitor);

    PHLWINDOWREF              m_window;
    LayoutCache               m_cache;
    kitty_skins::LogicalSize  m_cachedClient{0.0, 0.0};
    uint64_t                  m_cachedGeneration = 0;
    bool                      m_cacheValid       = false;
};

}
