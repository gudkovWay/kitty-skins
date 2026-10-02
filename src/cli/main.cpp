#include "kitty_skins/manifest.hpp"
#include "kitty_skins/process.hpp"
#include "kitty_skins/store.hpp"

#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <optional>
#include <span>
#include <string>
#include <system_error>
#include <utility>
#include <vector>

namespace {

using kitty_skins::Result;

constexpr const char* kUsage =
    "usage: terminal-skin <list|current|validate <id-or-path>|use <id>|reload|clear <expected-pack-id>>\n";

int usageError() {
    std::cerr << kUsage;
    return 2;
}

void reportError(const kitty_skins::ValidationError& error) {
    std::cerr << error.path.string() << ": " << error.field << ": " << error.message << '\n';
}

std::filesystem::path storeRoot() {
    if (const char* root = std::getenv("KITTY_SKINS_ROOT"); root != nullptr && *root != '\0')
        return std::filesystem::path{root};

    const char* configHome = std::getenv("XDG_CONFIG_HOME");
    if (configHome != nullptr && *configHome != '\0')
        return std::filesystem::path{configHome} / "kitty-skins";

    if (const char* home = std::getenv("HOME"); home != nullptr && *home != '\0')
        return std::filesystem::path{home} / ".config" / "kitty-skins";

    return std::filesystem::path{".config"} / "kitty-skins";
}

std::string escapeLuaArgument(const std::filesystem::path& pack) {
    const std::string raw = pack.string();
    std::string escaped;
    escaped.reserve(raw.size());
    for (const char character : raw) {
        if (character == '\\' || character == '"')
            escaped.push_back('\\');
        escaped.push_back(character);
    }
    return escaped;
}

bool dispatchPack(const std::filesystem::path& pack) {
    const std::string dispatcher = "hl.plugin.kittyskins.use(\"" + escapeLuaArgument(pack) + "\")";
    const std::vector<std::string> argv{"hyprctl", "dispatch", dispatcher};
    const kitty_skins::ProcessResult result = kitty_skins::run(std::span<const std::string>{argv});

    if (result.exitCode == 0)
        return true;

    std::cerr << "terminal-skin: hyprctl dispatch failed with exit code " << result.exitCode;
    if (!result.standardError.empty())
        std::cerr << ": " << result.standardError;
    std::cerr << '\n';
    return false;
}

bool dispatchClear(const std::filesystem::path& pack) {
    const std::string dispatcher = "hl.plugin.kittyskins.clear(\"" + escapeLuaArgument(pack) + "\")";
    const std::vector<std::string> argv{"hyprctl", "dispatch", dispatcher};
    const kitty_skins::ProcessResult result = kitty_skins::run(std::span<const std::string>{argv});

    if (result.exitCode == 0)
        return true;

    std::cerr << "terminal-skin: clear dispatch failed with exit code " << result.exitCode;
    if (!result.standardError.empty())
        std::cerr << ": " << result.standardError;
    std::cerr << '\n';
    return false;
}

void signalKitty() {
    // Only the caller's own compositor instance, same UID: never other users'
    // terminals, never an unrelated session, and no signal at all outside a
    // known compositor environment (e.g. inside the sandbox).
    const std::vector<pid_t> pids = kitty_skins::findKittyProcesses(kitty_skins::ownCompositorSignature());
    try {
        kitty_skins::signalKittyProcesses(std::span<const pid_t>{pids});
    } catch (const std::system_error& error) {
        std::cerr << "terminal-skin: warning: " << error.what() << '\n';
    }
}

Result<std::filesystem::path> resolveArgument(const kitty_skins::SkinStore& store, const std::string& argument) {
    if (argument.find('/') != std::string::npos)
        return std::filesystem::path{argument};

    auto resolved = store.resolve(argument);
    if (resolved)
        return resolved;

    std::error_code ec;
    const std::filesystem::path asPath{argument};
    if (std::filesystem::is_directory(asPath, ec))
        return asPath;

    return std::unexpected(resolved.error());
}

int commandList(const kitty_skins::SkinStore& store) {
    for (const std::string& id : store.list())
        std::cout << id << '\n';
    return 0;
}

int commandCurrent(const kitty_skins::SkinStore& store) {
    const auto active = store.active();
    if (!active) {
        reportError(active.error());
        return 1;
    }

    std::cout << active->filename().string() << '\n';
    return 0;
}

int commandValidate(const kitty_skins::SkinStore& store, const std::string& argument) {
    const auto resolved = resolveArgument(store, argument);
    if (!resolved) {
        reportError(resolved.error());
        return 1;
    }

    const auto pack = kitty_skins::loadAndValidatePack(*resolved);
    if (!pack) {
        reportError(pack.error());
        return 1;
    }

    std::cout << pack->id << ": valid\n";
    return 0;
}

int commandUse(const kitty_skins::SkinStore& store, const std::string& id) {
    // Serialize against concurrent use/reload/clear on the same store.
    const kitty_skins::StoreLock lock{store.root()};

    const auto target = store.resolve(id);
    if (!target) {
        reportError(target.error());
        return 1;
    }

    const auto pack = kitty_skins::loadAndValidatePack(*target);
    if (!pack) {
        reportError(pack.error());
        return 1;
    }

    std::optional<std::filesystem::path> previous;
    if (const auto active = store.active(); active)
        previous = *active;

    const auto switched = store.switchActive(id);
    if (!switched) {
        reportError(switched.error());
        return 1;
    }

    if (dispatchPack(*target)) {
        signalKitty();
        return 0;
    }

    if (previous) {
        const auto restored = store.switchActive(previous->filename().string());
        if (!restored)
            reportError(restored.error());
        if (!dispatchPack(*previous))
            std::cerr << "terminal-skin: failed to restore the previous skin in the plugin\n";
    } else {
        std::error_code ec;
        std::filesystem::remove(store.root() / "active", ec);
    }

    signalKitty();
    return 1;
}

int commandReload(const kitty_skins::SkinStore& store) {
    // Serialize against concurrent use/reload/clear on the same store.
    const kitty_skins::StoreLock lock{store.root()};

    const auto active = store.active();
    if (!active) {
        reportError(active.error());
        return 1;
    }

    const auto pack = kitty_skins::loadAndValidatePack(*active);
    if (!pack) {
        reportError(pack.error());
        return 1;
    }

    if (!dispatchPack(*active))
        return 1;

    signalKitty();
    return 0;
}

int commandClear(const kitty_skins::SkinStore& store, const std::string& expectedId) {
    // Serialize against concurrent use/reload/clear on the same store.
    const kitty_skins::StoreLock lock{store.root()};

    const auto active = store.active();
    if (!active) {
        reportError(active.error());
        return 1;
    }

    const auto pack = kitty_skins::loadAndValidatePack(*active);
    if (!pack) {
        reportError(pack.error());
        return 1;
    }

    // Expected-pack guard: refuse to clear a selection that is not the one the
    // caller saw. The plugin additionally guards the running runtime itself.
    if (pack->id != expectedId) {
        std::cerr << "terminal-skin: active pack is \"" << pack->id << "\", not \"" << expectedId
                  << "\"; refusing to clear\n";
        return 1;
    }

    const std::filesystem::path previousId = active->filename();
    std::error_code ec;
    std::filesystem::remove(store.root() / "active", ec);
    if (ec) {
        std::cerr << "terminal-skin: cannot remove the active link: " << ec.message() << '\n';
        return 1;
    }

    if (dispatchClear(*active)) {
        signalKitty();
        return 0;
    }

    // Plugin refused (runtime mismatch or load failure): roll the link back so
    // the store still reflects the skin the compositor is actually running.
    const auto restored = store.switchActive(previousId.string());
    if (!restored) {
        reportError(restored.error());
        std::cerr << "terminal-skin: the plugin did not clear the skin; restoring the active link also failed\n";
    } else {
        std::cerr << "terminal-skin: the plugin did not clear the skin; active link restored\n";
    }
    return 1;
}

}

int main(int argc, char** argv) {
    std::vector<std::string> args;
    args.reserve(argc > 0 ? static_cast<size_t>(argc - 1) : 0);
    for (int index = 1; index < argc; ++index)
        args.emplace_back(argv[index]);

    if (args.empty())
        return usageError();

    const kitty_skins::SkinStore store{storeRoot()};
    const std::string& command = args[0];
    if ((command == "use" || command == "reload" || command == "clear") &&
        kitty_skins::ownCompositorSignature().empty()) {
        std::cerr << "terminal-skin: HYPRLAND_INSTANCE_SIGNATURE is required; no skin changed\n";
        return 1;
    }

    // use/reload/clear acquire the per-store flock, which reports contention or
    // I/O trouble as std::system_error instead of an exit code.
    try {
        if (command == "list") {
            if (args.size() != 1)
                return usageError();
            return commandList(store);
        }

        if (command == "current") {
            if (args.size() != 1)
                return usageError();
            return commandCurrent(store);
        }

        if (command == "validate") {
            if (args.size() != 2)
                return usageError();
            return commandValidate(store, args[1]);
        }

        if (command == "use") {
            if (args.size() != 2)
                return usageError();
            return commandUse(store, args[1]);
        }

        if (command == "reload") {
            if (args.size() != 1)
                return usageError();
            return commandReload(store);
        }

        if (command == "clear") {
            if (args.size() != 2)
                return usageError();
            return commandClear(store, args[1]);
        }
    } catch (const std::system_error& error) {
        std::cerr << "terminal-skin: " << error.what() << '\n';
        return 1;
    }

    return usageError();
}
