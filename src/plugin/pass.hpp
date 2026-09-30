#pragma once

#include <optional>
#include <vector>

#include <hyprland/src/helpers/math/Math.hpp>
#include <hyprland/src/helpers/memory/Memory.hpp>
#include <hyprland/src/render/pass/PassElement.hpp>
#include <hyprutils/math/Region.hpp>

namespace kitty_skins::plugin {

class CSkinDecoration;

// One frame draw queued in the compositor's render pass for a decorated window.
// It owns no pixels and no geometry of its own: it borrows the decoration and
// alpha, and forwards draw() to CSkinDecoration::renderPass, which paints the
// cached schema-2 operations into whatever target the compositor currently has
// bound. It never touches the client's pass element, source texture, UVs,
// framebuffer or damage.
//
// The stable pass name identifies this element to the render pass machinery.
class CSkinPassElement final : public IPassElement {
  public:
    static constexpr const char* kPassName = "CSkinPassElement";

    CSkinPassElement(CSkinDecoration* decoration, float alpha);
    ~CSkinPassElement() override = default;

    std::vector<UP<IPassElement>> draw() override;
    bool                          needsLiveBlur() override;
    bool                          needsPrecomputeBlur() override;
    const char*                   passName() override;
    ePassElementType              type() override;
    std::optional<CBox>           boundingBox() override;
    CRegion                       opaqueRegion() override;

  private:
    CSkinDecoration* m_decoration = nullptr;
    float            m_alpha      = 1.F;
};

// Clear every queued pass element, including plugin-defined ones nested inside a
// transformed-window pass, from the live and the global render passes. Called on
// plugin exit while plugin code is still mapped, so no CSkinPassElement can run a
// virtual destructor after unload or call into freed decoration/runtime state.
void clearAllPendingPassElements();

}
