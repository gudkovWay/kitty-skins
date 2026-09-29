#pragma once

#include <optional>
#include <vector>

#include <hyprland/src/helpers/math/Math.hpp>
#include <hyprland/src/helpers/memory/Memory.hpp>
#include <hyprland/src/render/pass/PassElement.hpp>

namespace kitty_skins::plugin {

class CSkinDecoration;

// Stable pass name, also used to remove our elements from the render pass on unload.
inline constexpr const char* kSkinPassName = "CSkinPassElement";

// A single render-pass element per decorated window. It carries only a non-owning
// decoration pointer and the frame alpha; the decoration owns the cached operations
// and the shared textures.
class CSkinPassElement final : public IPassElement {
  public:
    struct SData {
        CSkinDecoration* decoration = nullptr;
        float            alpha      = 1.F;
    };

    explicit CSkinPassElement(const SData& data);

    std::vector<UP<IPassElement>> draw() override;
    bool                          needsLiveBlur() override;
    bool                          needsPrecomputeBlur() override;
    const char*                   passName() override;
    ePassElementType              type() override;
    std::optional<CBox>           boundingBox() override;

  private:
    SData m_data;
};

}
