#include <cctype>
#include <stdexcept>
#include <string>
#include <utility>

#include <lua.hpp>

#include <hyprland/src/debug/log/Logger.hpp>
#include <hyprland/src/desktop/view/Window.hpp>
#include <hyprland/src/event/EventBus.hpp>
#include <hyprland/src/plugins/PluginAPI.hpp>

#include "decoration.hpp"
#include "pass.hpp"
#include "state.hpp"

#ifndef VERSION
#define VERSION "0.1.0"
#endif

namespace {

constexpr const char* kDispatcherName = "kittyskins.use";
constexpr const char* kLuaNamespace   = "kittyskins";
constexpr const char* kLuaUseName     = "use";

// Strip surrounding whitespace and one layer of quotes from a dispatcher argument.
std::string cleanArgument(std::string argument) {
    while (!argument.empty() && std::isspace(static_cast<unsigned char>(argument.front())))
        argument.erase(argument.begin());

    while (!argument.empty() && std::isspace(static_cast<unsigned char>(argument.back())))
        argument.pop_back();

    if (argument.size() >= 2 && argument.front() == '"' && argument.back() == '"')
        argument = argument.substr(1, argument.size() - 2);

    return argument;
}

// Runs the transactional load for the path captured by hl.plugin.kittyskins.use().
int applyPackFromLua(lua_State* state) {
    const char* path = lua_tostring(state, lua_upvalueindex(1));
    if (path == nullptr)
        return luaL_error(state, "kittyskins.use: missing pack path");

    std::string error;
    if (!kitty_skins::plugin::applyPack(path, error))
        return luaL_error(state, "%s", ("kittyskins.use: " + error).c_str());

    return 0;
}

// `hl.plugin.kittyskins.use(path)` is a dispatcher factory: it returns a closure that
// `hl.dispatch(...)` invokes, so a failed load reaches hyprctl as an error.
int luaUse(lua_State* state) {
    const char* path = luaL_checkstring(state, 1);
    lua_pushstring(state, path);
    lua_pushcclosure(state, applyPackFromLua, 1);
    return 1;
}

SDispatchResult dispatchUse(std::string argument) {
    const std::string pack = cleanArgument(std::move(argument));
    if (pack.empty())
        return SDispatchResult{.success = false, .error = "kittyskins.use: expected a pack path"};

    std::string error;
    if (!kitty_skins::plugin::applyPack(pack, error))
        return SDispatchResult{.success = false, .error = "kittyskins.use: " + error};

    return SDispatchResult{.success = true};
}

void onWindowOpen(PHLWINDOW window) {
    kitty_skins::plugin::attachWindow(window);
}

void onWindowClose(PHLWINDOW window) {
    kitty_skins::plugin::detachWindow(window);
}

void onWindowClass(PHLWINDOW window) {
    // A window that stopped matching the configured target must lose its decoration.
    kitty_skins::plugin::syncWindow(window);
}

void onConfigReloaded() {
    std::string error;
    if (!kitty_skins::plugin::reloadActivePack(error))
        Log::logger->log(Log::WARN, "kitty-skins: active pack could not be reloaded: {}", error);

    // Target and store root may have changed: re-evaluate every mapped window.
    kitty_skins::plugin::syncWindows();
}

}

APICALL EXPORT std::string PLUGIN_API_VERSION() {
    return HYPRLAND_API_VERSION;
}

APICALL EXPORT PLUGIN_DESCRIPTION_INFO PLUGIN_INIT(HANDLE handle) {
    // Reject an ABI mismatch before any global state exists.
    const std::string COMPOSITOR_HASH = __hyprland_api_get_hash();
    const std::string CLIENT_HASH     = __hyprland_api_get_client_hash();
    if (COMPOSITOR_HASH != CLIENT_HASH)
        throw std::runtime_error("kitty-skins: plugin headers do not match the running Hyprland");

    kitty_skins::plugin::g_pluginHandle = handle;
    kitty_skins::plugin::g_pState       = makeUnique<kitty_skins::plugin::SGlobalState>();

    kitty_skins::plugin::SGlobalState& state = *kitty_skins::plugin::g_pState;

    state.rootConfig = makeShared<Config::Values::CStringValue>("plugin:kittyskins:root", "Directory holding the Kitty skin store.",
                                                               kitty_skins::plugin::defaultStoreRoot());
    // Default target is the exact class "kitty"; "*" decorates every eligible
    // mapped toplevel; any other value is an exact class match.
    state.targetConfig = makeShared<Config::Values::CStringValue>("plugin:kittyskins:target",
                                                                  "Window class to frame, or \"*\" for every eligible window.", "kitty");
    HyprlandAPI::addConfigValueV2(handle, state.rootConfig);
    HyprlandAPI::addConfigValueV2(handle, state.targetConfig);

    HyprlandAPI::addDispatcherV2(handle, kDispatcherName, dispatchUse);

    // The Lua config surface is what `hyprctl dispatch hl.plugin.kittyskins.use(...)`
    // resolves to; it is unavailable in legacy hyprlang configurations.
    if (!HyprlandAPI::addLuaFunction(handle, kLuaNamespace, kLuaUseName, luaUse))
        Log::logger->log(Log::DEBUG, "kitty-skins: Lua config surface unavailable, use the kittyskins.use dispatcher");

    state.windowOpen     = Event::bus()->m_events.window.open.listen([](PHLWINDOW window) { onWindowOpen(window); });
    state.windowClose    = Event::bus()->m_events.window.close.listen([](PHLWINDOW window) { onWindowClose(window); });
    state.windowClass    = Event::bus()->m_events.window.class_.listen([](PHLWINDOW window) { onWindowClass(window); });
    state.configReloaded = Event::bus()->m_events.config.reloaded.listen([] { onConfigReloaded(); });

    std::string error;
    if (!kitty_skins::plugin::reloadActivePack(error))
        Log::logger->log(Log::WARN, "kitty-skins: no active skin loaded: {}", error);

    kitty_skins::plugin::syncWindows();

    return {"kitty-skins", "Data-driven window frames.", "q", VERSION};
}

APICALL EXPORT void PLUGIN_EXIT() {
    // 1. Clear the live and global render passes first: this destroys every queued
    //    element, including a CSkinPassElement nested inside a transformed-window
    //    pass, while plugin code is still mapped and before the decorations and
    //    runtime it points into are destroyed.
    kitty_skins::plugin::clearAllPendingPassElements();

    // 2. Decorations, which release their cached layout.
    kitty_skins::plugin::removeAllDecorations();

    // 3. Dispatcher and Lua entry point.
    HyprlandAPI::removeDispatcher(kitty_skins::plugin::g_pluginHandle, kDispatcherName);
    HyprlandAPI::removeLuaFunction(kitty_skins::plugin::g_pluginHandle, kLuaNamespace, kLuaUseName);

    if (!kitty_skins::plugin::g_pState)
        return;

    // 4. Event listeners.
    kitty_skins::plugin::g_pState->windowOpen.reset();
    kitty_skins::plugin::g_pState->windowClose.reset();
    kitty_skins::plugin::g_pState->windowClass.reset();
    kitty_skins::plugin::g_pState->configReloaded.reset();

    // 5. Shared textures last.
    kitty_skins::plugin::g_pState->runtime.reset();
    kitty_skins::plugin::g_pState->rootConfig.reset();
    kitty_skins::plugin::g_pState->targetConfig.reset();
    kitty_skins::plugin::g_pState.reset();
}
