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

std::string configuredTarget() {
    if (!g_pState || !g_pState->targetConfig)
        return "kitty";

    const std::string value = g_pState->targetConfig->value();
    return value.empty() ? "kitty" : value;
}

bool windowMatchesTarget(const PHLWINDOW& window) {
    if (!window || !window->m_isMapped || window->isHidden())
        return false;

    // "*" decorates every eligible mapped toplevel; any other value is an exact
    // class match. There is deliberately no regex or CSV language here.
    const std::string target = configuredTarget();
    if (target == "*")
        return true;

    return window->m_class == target;
}

const SkinRuntime* currentRuntime() {
    if (!g_pState || !g_pState->runtime)
        return nullptr;

    return g_pState->runtime.get();
}

void registerDecoration(CSkinDecoration* decoration) {
    if (!g_pState || decoration == nullptr)
        return;

    const PHLWINDOW window = decoration->window();
    if (window)
        g_pState->decorations[window.get()] = decoration;
}

void forgetDecoration(CSkinDecoration* decoration) {
    if (!g_pState || decoration == nullptr)
        return;

    for (auto it = g_pState->decorations.begin(); it != g_pState->decorations.end();) {
        if (it->second == decoration)
            it = g_pState->decorations.erase(it);
        else
            ++it;
    }
}

CSkinDecoration* decorationFor(Desktop::View::CWindow* window) {
    if (!g_pState || window == nullptr)
        return nullptr;

    const auto it = g_pState->decorations.find(window);
    return it == g_pState->decorations.end() ? nullptr : it->second;
}

bool applyPack(const std::string& packRoot, std::string& error) {
    if (!g_pState) {
        error = "plugin state is not initialised";
        return false;
    }

    auto candidate = loadRuntime(packRoot);
    if (!candidate) {
        // A failed reload preserves the previous runtime and every cached resource.
        error = candidate.error();
        return false;
    }

    // Damage the previous bounds before the swap and the new bounds after it, so a
    // mode or texture change leaves no stale pixels behind.
    for (const auto& entry : g_pState->decorations) {
        if (entry.second)
            entry.second->damageEntire();
    }

    g_pState->runtime = std::move(*candidate);
    ++g_pState->generation;

    for (const auto& entry : g_pState->decorations) {
        CSkinDecoration* decoration = entry.second;
        if (!decoration)
            continue;

        // Drops the cached layout: the next frame rebuilds it from the new pack.
        decoration->invalidate();

        const PHLWINDOW window = decoration->window();
        if (window) {
            g_pDecorationPositioner->forceRecalcFor(window);
            window->updateWindowDecos();
        }

        decoration->damageEntire();
    }

    // A valid runtime now exists: attach decorations to every window the target
    // matches and make the newly reserved extents take effect.
    syncWindows();

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
    // A decoration is attached whenever a runtime exists and the target matches;
    // there is no load-order dependency on another plugin any more.
    if (!g_pState || currentRuntime() == nullptr || !windowMatchesTarget(window))
        return;
    if (decorationFor(window.get()) != nullptr)
        return;

    auto             decoration = makeUnique<CSkinDecoration>(window);
    CSkinDecoration* raw        = decoration.get();
    registerDecoration(raw);

    if (!HyprlandAPI::addWindowDecoration(g_pluginHandle, window, std::move(decoration))) {
        forgetDecoration(raw);
        Log::logger->log(Log::WARN, "kitty-skins: could not attach a decoration to window \"{}\"", window->m_title);
    }
}

void detachWindow(PHLWINDOW window) {
    CSkinDecoration* decoration = decorationFor(window.get());
    if (!decoration)
        return;

    HyprlandAPI::removeWindowDecoration(g_pluginHandle, decoration);
}

void syncWindow(PHLWINDOW window) {
    if (!g_pState || !window)
        return;

    const bool matches  = currentRuntime() != nullptr && windowMatchesTarget(window);
    const bool attached = decorationFor(window.get()) != nullptr;

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

    std::vector<CSkinDecoration*> decorations;
    decorations.reserve(g_pState->decorations.size());
    for (const auto& entry : g_pState->decorations) {
        if (entry.second)
            decorations.push_back(entry.second);
    }

    for (CSkinDecoration* decoration : decorations)
        HyprlandAPI::removeWindowDecoration(g_pluginHandle, decoration);

    g_pState->decorations.clear();
}

}
