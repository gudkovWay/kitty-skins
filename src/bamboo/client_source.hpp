#pragma once

#include <array>
#include <expected>
#include <string>

#include <hyprland/src/desktop/DesktopTypes.hpp>
#include <hyprland/src/render/Texture.hpp>

namespace bamboo02 {

// Borrowed committed client pixels; no texture mutation or native pass hook.
// The shared reference keeps the committed image alive for this render call.
struct ClientSource {
    SP<Render::ITexture> texture;
    NColorManagement::PImageDescription description;
    // Column-major homogeneous mapping from top-left client UV to texture UV.
    std::array<float, 9> uvTransform{1, 0, 0, 0, 1, 0, 0, 0, 1};
    // Surface protocol opacity only; native window fade is supplied by caller.
    float opacity = 1.0F;
    bool opaque = false;
    bool nearest = false;
    float rounding = 0.0F;
    float roundingPower = 2.0F;
    float colorMultiplier = 1.0F;
};

std::expected<ClientSource, std::string> clientSource(const PHLWINDOW& window, const PHLMONITOR& monitor, const Vector2D& projected);

} // namespace bamboo02
