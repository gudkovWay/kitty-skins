#include "state.hpp"

#include <cstdlib>
#include <utility>

#include <hyprland/src/debug/log/Logger.hpp>
#include <hyprland/src/desktop/state/WindowState.hpp>
#include <hyprland/src/desktop/view/Window.hpp>
#include <hyprland/src/render/decorations/DecorationPositioner.hpp>
#include <hyprland/src/render/Renderer.hpp>

#include "decoration.hpp"
#include "kitty_skins/store.hpp"

namespace kitty_skins::plugin {

namespace {

CSkinDecoration* findDecoration(const PHLWINDOW& window) {
    if (!g_pState)
        return nullptr;

    for (CSkinDecoration* decoration : g_pState->decorations) {
        if (decoration && decoration->window() == window)
            return decoration;
    }

    return nullptr;
}

bool windowMatches(const PHLWINDOW& window) {
    if (!window || !window->m_isMapped || window->isHidden())
        return false;

    return window->m_class == configuredClass();
}

}

std::string defaultStoreRoot() {
    if (const char* configHome = std::getenv("XDG_CONFIG_HOME"); configHome != nullptr && *configHome != '\0')
        return std::string(configHome) + "/kitty-skins";

    if (const char* home = std::getenv("HOME"); home != nullptr && *home != '\0')
        return std::string(home) + "/.config/kitty-skins";

    return ".config/kitty-skins";
}

std::string configuredRoot() {
    if (!g_pState || !g_pState->rootConfig)
        return defaultStoreRoot();

    const std::string value = g_pState->rootConfig->value();
    return value.empty() ? defaultStoreRoot() : value;
}

std::string configuredClass() {
    if (!g_pState || !g_pState->classConfig)
        return "kitty";

    return g_pState->classConfig->value();
}

const SkinRuntime* currentRuntime() {
    if (!g_pState || !g_pState->runtime)
        return nullptr;

    return g_pState->runtime.get();
}

bool applyPack(const std::string& packRoot, std::string& error) {
    if (!g_pState) {
        error = "plugin state is not initialised";
        return false;
    }

    auto candidate = loadRuntime(packRoot);
    if (!candidate) {
        error = candidate.error();
        return false;
    }

    // Damage the previous bounds before the swap and the new bounds after it, so a tier
    // or texture change leaves no stale pixels behind.
    for (CSkinDecoration* decoration : g_pState->decorations) {
        if (decoration)
            decoration->damageEntire();
    }

    g_pState->runtime = std::move(*candidate);
    ++g_pState->generation;

    for (CSkinDecoration* decoration : g_pState->decorations) {
        if (!decoration)
            continue;

        decoration->invalidate();

        const PHLWINDOW window = decoration->window();
        if (window) {
            // Extents may differ between the old and the new tier, so the positioner has
            // to re-read this decoration's desired extents.
            g_pDecorationPositioner->forceRecalcFor(window);
            window->updateWindowDecos();
        }

        decoration->damageEntire();
    }

    return true;
}

bool reloadActivePack(std::string& error) {
    const kitty_skins::SkinStore store{configuredRoot()};
    const auto                   active = store.active();
    if (!active) {
        const kitty_skins::ValidationError& failure = active.error();
        error = failure.path.string() + ": " + failure.field + ": " + failure.message;
        return false;
    }

    return applyPack(active->string(), error);
}

void attachWindow(PHLWINDOW window) {
    if (!g_pState || !windowMatches(window) || findDecoration(window) != nullptr)
        return;

    auto             decoration = makeUnique<CSkinDecoration>(window);
    CSkinDecoration* raw        = decoration.get();
    g_pState->decorations.push_back(raw);

    if (!HyprlandAPI::addWindowDecoration(g_pluginHandle, window, std::move(decoration))) {
        std::erase(g_pState->decorations, raw);
        Log::logger->log(Log::WARN, "kitty-skins: could not attach a decoration to window \"{}\"", window->m_title);
    }
}

void detachWindow(PHLWINDOW window) {
    CSkinDecoration* decoration = findDecoration(window);
    if (!decoration)
        return;

    HyprlandAPI::removeWindowDecoration(g_pluginHandle, decoration);
}

void syncWindow(PHLWINDOW window) {
    if (!g_pState || !window)
        return;

    const bool matches  = windowMatches(window);
    const bool attached = findDecoration(window) != nullptr;

    if (matches && !attached)
        attachWindow(window);
    else if (!matches && attached)
        detachWindow(window);
}

void syncWindows() {
    if (!g_pState)
        return;

    // Copy: attaching and detaching mutate the decorations, not the window list.
    const std::vector<PHLWINDOW> windows = Desktop::windowState()->windows();

    for (const PHLWINDOW& window : windows)
        syncWindow(window);
}

void removeAllDecorations() {
    if (!g_pState)
        return;

    // Iterate a copy: a removed decoration erases itself from the registry.
    const std::vector<CSkinDecoration*> decorations = g_pState->decorations;
    for (CSkinDecoration* decoration : decorations) {
        if (decoration)
            HyprlandAPI::removeWindowDecoration(g_pluginHandle, decoration);
    }

    g_pState->decorations.clear();
}

}
