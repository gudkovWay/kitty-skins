#pragma once

#include <expected>
#include <filesystem>
#include <memory>
#include <string>

#include <hyprland/src/helpers/memory/Memory.hpp>
#include <hyprland/src/render/Texture.hpp>

#include "kitty_skins/model.hpp"

namespace kitty_skins::plugin {

// One successfully loaded skin: the validated pack and its two source atlases.
// Adaptive regions sample the shared atlas directly through source UVs, avoiding
// per-region GPU copies and their driver-dependent failure modes.
struct SkinRuntime {
    kitty_skins::SkinPack pack;

    SP<Render::ITexture> exactAtlas;
    SP<Render::ITexture> adaptiveAtlas;

    const SP<Render::ITexture>& atlas(bool exact) const {
        return exact ? exactAtlas : adaptiveAtlas;
    }
};

// Transactionally load a pack: manifest validation, PNG decode and GPU upload.
// Returns an error string on failure; nothing is retained then.
std::expected<std::shared_ptr<SkinRuntime>, std::string> loadRuntime(const std::filesystem::path& packRoot);

}
