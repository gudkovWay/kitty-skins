#pragma once

#include <filesystem>
#include <memory>
#include <string>
#include <hyprland/src/defines.hpp>
#include <hyprutils/math/Box.hpp>

namespace bamboo02 {
struct Runtime;

// Per-window, fixed Shoji radius4/passes2 atmosphere. This reads only the
// compositor target BEHIND the client, from the native UNDER decoration pass.
class Ambient {
  public:
    explicit Ambient(std::filesystem::path packRoot);
    ~Ambient();
    Ambient(const Ambient&) = delete;
    Ambient& operator=(const Ambient&) = delete;
    // A current EGL context is required; call once at attachment, not in render.
    bool prepare();
    bool render(PHLMONITOR monitor, const CBox& clientPhysical,
                Vector2D clientLogical, double ambientSeconds, float alpha,
                const Runtime& runtime);
    const std::string& error() const;

  private:
    struct Impl;
    std::unique_ptr<Impl> impl;
};
} // namespace bamboo02
