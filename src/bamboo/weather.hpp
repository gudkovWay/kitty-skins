#pragma once

#include <filesystem>
#include <memory>
#include <string>
#include <hyprland/src/defines.hpp>
#include <hyprutils/math/Box.hpp>

#include "client_source.hpp"

namespace bamboo02 {

// One instance per window: motion feedback must never be shared by clients.
class Weather {
  public:
    explicit Weather(std::filesystem::path packRoot);
    ~Weather();
    Weather(const Weather&) = delete;
    Weather& operator=(const Weather&) = delete;
    // Compile once at attachment with a current EGL context, never per frame.
    bool prepare();
    // Client opacity is separate from the caller's native window/workspace fade.
    bool render(PHLMONITOR monitor, const CBox& clientPhysical,
                const Vector2D& clientLogical, const ClientSource& source,
                double ambientSeconds, float nativeFade);
    const std::string& error() const;

  private:
    struct Impl;
    std::unique_ptr<Impl> impl;
};

} // namespace bamboo02
