#pragma once

#include <expected>
#include <filesystem>
#include <memory>
#include <string>

#include <hyprland/src/helpers/memory/Memory.hpp>
#include <hyprland/src/render/Texture.hpp>

#include "candle_effect.hpp"
#include "flower_effect.hpp"
#include "kitty_skins/model.hpp"

namespace kitty_skins::plugin {

// One successfully loaded skin: the validated pack and its two source atlases.
// Adaptive regions sample the shared atlas directly through source UVs, avoiding
// per-region GPU copies and their driver-dependent failure modes.
struct SkinRuntime {
    kitty_skins::SkinPack pack;

    SP<Render::ITexture> exactAtlas;
    SP<Render::ITexture> adaptiveAtlas;

    // Present only for packs that declare a candle effect; null otherwise, so a
    // static pack never touches the animated path.
    std::unique_ptr<CandleRenderer> candles;

    // One preloaded RGBA texture per accent effect, parallel to
    // pack.accentEffects; empty for packs without accents. Each texture matches
    // its referenced region's exact pixel dimensions.
    std::vector<SP<Render::ITexture>> accentTextures;

    // One layered flower compositor per declared flower effect, parallel to
    // pack.flowerEffects; empty for a pack without flowers, so a static or
    // merely animated legacy pack allocates no flower GPU resources. Each
    // renderer owns a region-sized cached framebuffer.
    std::vector<std::unique_ptr<FlowerRenderer>> flowers;

    const SP<Render::ITexture>& atlas(bool exact) const {
        return exact ? exactAtlas : adaptiveAtlas;
    }
};

// Transactionally load a pack: manifest validation, PNG decode and GPU upload.
// Returns an error string on failure; nothing is retained then.
std::expected<std::shared_ptr<SkinRuntime>, std::string> loadRuntime(const std::filesystem::path& packRoot);

}
