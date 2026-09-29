#pragma once

#include <filesystem>

#include "kitty_skins/model.hpp"

namespace kitty_skins {

Result<SkinPack> loadAndValidatePack(const std::filesystem::path& root);
const TierSpec& selectTier(const SkinPack&, LogicalSize clientSize) noexcept;

}
