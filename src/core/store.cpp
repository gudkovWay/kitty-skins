#include "kitty_skins/store.hpp"

#include <algorithm>
#include <cerrno>
#include <string>
#include <system_error>
#include <utility>

#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>

namespace kitty_skins {
namespace {

ValidationError fail(std::filesystem::path path, std::string field, std::string message) {
    return ValidationError{std::move(path), std::move(field), std::move(message)};
}

bool isWithin(const std::filesystem::path& root, const std::filesystem::path& target) {
    auto rootIt = root.begin();
    auto targetIt = target.begin();
    for (; rootIt != root.end(); ++rootIt, ++targetIt) {
        if (targetIt == target.end() || *rootIt != *targetIt)
            return false;
    }
    return true;
}

}

SkinStore::SkinStore(std::filesystem::path root) : root_(std::move(root)) {}

StoreLock::StoreLock(const std::filesystem::path& root) {
    std::error_code ec;
    std::filesystem::create_directories(root, ec);
    if (ec)
        throw std::system_error(ec.value(), std::generic_category(), "cannot create store root " + root.string());

    const int fd = ::open((root / ".lock").c_str(), O_RDWR | O_CREAT | O_CLOEXEC, 0600);
    if (fd < 0)
        throw std::system_error(errno, std::generic_category(), "cannot open store lock " + (root / ".lock").string());

    while (::flock(fd, LOCK_EX) != 0) {
        if (errno != EINTR) {
            const int saved = errno;
            ::close(fd);
            throw std::system_error(saved, std::generic_category(), "cannot lock store " + root.string());
        }
    }
    fd_ = fd;
}

StoreLock::~StoreLock() {
    if (fd_ < 0)
        return;
    ::flock(fd_, LOCK_UN);
    ::close(fd_);
}

std::vector<std::string> SkinStore::list() const {
    std::vector<std::string> ids;

    std::error_code ec;
    std::filesystem::directory_iterator entries(root_ / "skins", ec);
    if (ec)
        return ids;

    for (const std::filesystem::directory_entry& entry : entries) {
        std::error_code entryEc;
        if (entry.is_directory(entryEc) && !entryEc)
            ids.push_back(entry.path().filename().string());
    }

    std::sort(ids.begin(), ids.end());
    return ids;
}

Result<std::filesystem::path> SkinStore::resolve(std::string_view id) const {
    const std::filesystem::path relative{std::string(id)};
    if (id.empty() || relative.is_absolute() || relative.has_parent_path() || relative.filename() == "." ||
        relative.filename() == "..")
        return std::unexpected(fail(root_ / "skins", "id", "install id must be a single directory name"));

    const std::filesystem::path candidate = root_ / "skins" / relative;

    std::error_code ec;
    const std::filesystem::path packsRoot = std::filesystem::weakly_canonical(root_ / "skins", ec);
    if (ec)
        return std::unexpected(fail(root_ / "skins", "id", "cannot resolve skins directory: " + ec.message()));

    if (!std::filesystem::is_directory(candidate, ec))
        return std::unexpected(fail(candidate, "id", "no installed skin directory for this id"));

    const std::filesystem::path resolved = std::filesystem::weakly_canonical(candidate, ec);
    if (ec)
        return std::unexpected(fail(candidate, "id", "cannot resolve skin directory: " + ec.message()));

    if (resolved.parent_path() != packsRoot)
        return std::unexpected(fail(candidate, "id", "skin directory must be a direct child of the skins directory"));

    return resolved;
}

Result<std::filesystem::path> SkinStore::active() const {
    const std::filesystem::path link = root_ / "active";

    std::error_code ec;
    if (!std::filesystem::is_symlink(link, ec))
        return std::unexpected(fail(link, "active", "no active skin link"));

    const std::filesystem::path target = std::filesystem::read_symlink(link, ec);
    if (ec)
        return std::unexpected(fail(link, "active", "cannot read active link: " + ec.message()));

    const std::filesystem::path resolved =
        std::filesystem::weakly_canonical(target.is_absolute() ? target : root_ / target, ec);
    if (ec)
        return std::unexpected(fail(link, "active", "cannot resolve active target: " + ec.message()));

    const std::filesystem::path packsRoot = std::filesystem::weakly_canonical(root_ / "skins", ec);
    if (ec)
        return std::unexpected(fail(link, "active", "cannot resolve skins directory: " + ec.message()));

    if (!isWithin(packsRoot, resolved))
        return std::unexpected(fail(link, "active", "active target escapes the skins directory"));

    if (!std::filesystem::is_directory(resolved, ec))
        return std::unexpected(fail(link, "active", "active target is not a directory"));

    return resolved;
}

Result<void> SkinStore::switchActive(std::string_view id) const {
    const auto target = resolve(id);
    if (!target)
        return std::unexpected(target.error());

    const std::filesystem::path relative = std::filesystem::path{"skins"} / std::filesystem::path{std::string(id)};
    const std::filesystem::path temporary = root_ / (".active." + std::to_string(::getpid()));

    std::error_code ec;
    std::filesystem::remove(temporary, ec);
    ec.clear();

    std::filesystem::create_symlink(relative, temporary, ec);
    if (ec)
        return std::unexpected(fail(temporary, "active", "cannot create temporary link: " + ec.message()));

    std::filesystem::rename(temporary, root_ / "active", ec);
    if (ec) {
        std::error_code cleanup;
        std::filesystem::remove(temporary, cleanup);
        return std::unexpected(fail(root_ / "active", "active", "cannot replace active link: " + ec.message()));
    }

    return {};
}

const std::filesystem::path& SkinStore::root() const noexcept {
    return root_;
}

}
