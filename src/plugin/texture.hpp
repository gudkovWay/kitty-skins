#pragma once

#include <array>
#include <expected>
#include <filesystem>
#include <memory>
#include <string>
#include <unordered_map>

#include <hyprland/src/helpers/memory/Memory.hpp>
#include <hyprland/src/render/Texture.hpp>
#include <hyprutils/math/Vector2D.hpp>

#include "kitty_skins/model.hpp"

namespace kitty_skins::plugin {

// A GPU texture plus the source-pixel size of the asset region it holds.
struct TextureAsset {
    SP<Render::ITexture>             texture;
    Hyprutils::Math::Vector2D        sourceSize;
};

// Slice order of SkinRuntime::frameSlices.
enum class FrameSlice : size_t {
    topLeft = 0,
    topRight,
    bottomRight,
    bottomLeft,
    top,
    right,
    bottom,
    left,
};

// One successfully loaded skin: the validated pack and all GPU resources it needs.
// A candidate runtime is built completely before it replaces the active one.
struct SkinRuntime {
    kitty_skins::SkinPack                          pack;
    std::array<TextureAsset, 8>                    frameSlices;
    std::unordered_map<std::string, TextureAsset>  sprites;
};

// Transactionally load a pack: manifest validation, PNG decode through Cairo, GPU upload
// through the Hyprland texture allocator and one-time nine-slice copying with
// glCopyImageSubData. Returns an error string on any failure; nothing is retained then.
std::expected<std::shared_ptr<SkinRuntime>, std::string> loadRuntime(const std::filesystem::path& packRoot);

}
