#pragma once

#include <filesystem>

#include "kitty_skins/model.hpp"

namespace kitty_skins {

// Parse and validate a schema-2 skin pack rooted at `root`. Schema 1 is rejected
// with an explicit incompatibility error.
Result<SkinPack> loadAndValidatePack(const std::filesystem::path& root);

}
