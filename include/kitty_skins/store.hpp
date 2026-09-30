#pragma once

#include <filesystem>
#include <string>
#include <string_view>
#include <vector>

#include "kitty_skins/model.hpp"

namespace kitty_skins {

class SkinStore {
  public:
    explicit SkinStore(std::filesystem::path root);
    std::vector<std::string> list() const;
    Result<std::filesystem::path> resolve(std::string_view id) const;
    Result<std::filesystem::path> active() const;
    Result<void> switchActive(std::string_view id) const;
    const std::filesystem::path& root() const noexcept;

  private:
    std::filesystem::path root_;
};

}
