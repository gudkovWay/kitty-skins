#pragma once

#include <filesystem>
#include <string>
#include <string_view>
#include <vector>

#include "kitty_skins/model.hpp"

namespace kitty_skins {

// Exclusive per-store advisory lock (flock on `<root>/.lock`). Serializes CLI
// use/reload/clear against each other; blocks until acquired.
class StoreLock {
  public:
    explicit StoreLock(const std::filesystem::path& root);
    ~StoreLock();
    StoreLock(const StoreLock&)            = delete;
    StoreLock& operator=(const StoreLock&) = delete;

  private:
    int fd_ = -1;
};

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
