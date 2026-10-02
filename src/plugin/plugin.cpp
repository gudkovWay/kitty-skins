#include <algorithm>
#include <cctype>
#include <stdexcept>
#include <string>
#include <utility>

#include <lua.hpp>

#include <hyprland/src/debug/log/Logger.hpp>
#include <hyprland/src/desktop/state/FocusState.hpp>
#include <hyprland/src/desktop/state/WindowState.hpp>
#include <hyprland/src/desktop/view/Window.hpp>
#include <hyprland/src/event/EventBus.hpp>
#include <hyprland/src/managers/SessionLockManager.hpp>
#include <hyprland/src/plugins/PluginAPI.hpp>

#include "decoration.hpp"
#include "pass.hpp"
#include "state.hpp"

#ifndef VERSION
#define VERSION "0.1.0"
#endif

namespace {

constexpr const char* kDispatcherName = "kittyskins.use";
constexpr const char* kClearDispatcherName = "kittyskins.clear";
// Live frame preview, driven by the wallpaper Studio: one dedicated window and
// its own candidate pack, without touching the global selection.
constexpr const char* kPreviewDispatcherName      = "kittyskins.preview";
constexpr const char* kPreviewClearDispatcherName = "kittyskins.preview.clear";
constexpr const char* kLuaNamespace        = "kittyskins";
constexpr const char* kLuaUseName          = "use";
constexpr const char* kLuaClearName        = "clear";
constexpr const char* kLuaPreviewName      = "preview";
constexpr const char* kLuaPreviewClearName = "preview_clear";

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

// Runs the guarded clear for the absolute pack path captured by
// hl.plugin.kittyskins.clear().
int clearPackFromLua(lua_State* state) {
    const char* path = lua_tostring(state, lua_upvalueindex(1));
    if (path == nullptr)
        return luaL_error(state, "kittyskins.clear: missing pack path");

    std::string error;
    if (!kitty_skins::plugin::clearPack(path, error))
        return luaL_error(state, "%s", ("kittyskins.clear: " + error).c_str());

    return 0;
}

// Mirrors use(): a dispatcher factory returning a closure that hl.dispatch(...)
// invokes, so a refused clear reaches hyprctl as an error.
int luaClear(lua_State* state) {
    const char* path = luaL_checkstring(state, 1);
    lua_pushstring(state, path);
    lua_pushcclosure(state, clearPackFromLua, 1);
    return 1;
}

int runPreviewFromLua(lua_State* state, bool clear) {
    const char* address  = lua_tostring(state, lua_upvalueindex(1));
    const char* packRoot = lua_tostring(state, lua_upvalueindex(2));
    const char* name     = clear ? "kittyskins.preview_clear" : "kittyskins.preview";
    if (address == nullptr || packRoot == nullptr)
        return luaL_error(state, "%s: missing window address or pack path", name);

    const PHLWINDOW window = kitty_skins::plugin::windowFromAddress(address);
    if (!window)
        return luaL_error(state, "%s: \"%s\" is not the address of a live window", name, address);

    std::string error;
    const bool  succeeded = clear ? kitty_skins::plugin::clearPreview(window, packRoot, error) :
                                    kitty_skins::plugin::applyPreview(window, packRoot, error);
    if (!succeeded)
        return luaL_error(state, "%s", (std::string(name) + ": " + error).c_str());

    return 0;
}

int applyPreviewFromLua(lua_State* state) {
    return runPreviewFromLua(state, false);
}

int clearPreviewFromLua(lua_State* state) {
    return runPreviewFromLua(state, true);
}

int pushPreviewClosure(lua_State* state, lua_CFunction operation) {
    const char* address  = luaL_checkstring(state, 1);
    const char* packRoot = luaL_checkstring(state, 2);
    lua_pushstring(state, address);
    lua_pushstring(state, packRoot);
    lua_pushcclosure(state, operation, 2);
    return 1;
}

int luaPreview(lua_State* state) {
    return pushPreviewClosure(state, applyPreviewFromLua);
}

int luaPreviewClear(lua_State* state) {
    return pushPreviewClosure(state, clearPreviewFromLua);
}

SDispatchResult dispatchClear(std::string argument) {
    const std::string pack = cleanArgument(std::move(argument));
    if (pack.empty())
        return SDispatchResult{.success = false, .error = "kittyskins.clear: expected a pack path"};

    std::string error;
    if (!kitty_skins::plugin::clearPack(pack, error))
        return SDispatchResult{.success = false, .error = "kittyskins.clear: " + error};

    return SDispatchResult{.success = true};
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

// Split a preview argument "<0x-address> <absolute pack path>" at the first ASCII
// whitespace, so a pack path containing spaces survives intact. Surrounding
// quotes are removed from the whole argument and from each part, matching how a
// quoted dispatcher argument is forwarded.
bool splitPreviewArgument(std::string argument, std::string& address, std::string& packRoot) {
    argument = cleanArgument(std::move(argument));

    const auto separator = std::find_if(argument.begin(), argument.end(), [](unsigned char character) {
        return std::isspace(character) != 0;
    });
    if (separator == argument.end())
        return false;

    address  = cleanArgument(argument.substr(0, static_cast<size_t>(separator - argument.begin())));
    packRoot = cleanArgument(argument.substr(static_cast<size_t>(separator - argument.begin()) + 1));

    return !address.empty() && !packRoot.empty();
}

// Parse "<0x-address> <absolute pack path>" and resolve the address against the
// live window list. Returns a null window and fills \p error on any failure.
PHLWINDOW resolvePreviewArgument(std::string argument, std::string& packRoot, const char* name, std::string& error) {
    std::string address;
    if (!splitPreviewArgument(std::move(argument), address, packRoot)) {
        error = std::string(name) + ": expected \"<window-address> <absolute pack path>\"";
        return nullptr;
    }

    const PHLWINDOW window = kitty_skins::plugin::windowFromAddress(address);
    if (!window)
        error = std::string(name) + ": \"" + address + "\" is not the address of a live window";

    return window;
}

SDispatchResult dispatchPreview(std::string argument) {
    std::string     error;
    std::string     packRoot;
    const PHLWINDOW window = resolvePreviewArgument(std::move(argument), packRoot, "kittyskins.preview", error);
    if (!window)
        return SDispatchResult{.success = false, .error = error};

    if (!kitty_skins::plugin::applyPreview(window, packRoot, error))
        return SDispatchResult{.success = false, .error = "kittyskins.preview: " + error};

    return SDispatchResult{.success = true};
}

SDispatchResult dispatchPreviewClear(std::string argument) {
    std::string     error;
    std::string     packRoot;
    const PHLWINDOW window = resolvePreviewArgument(std::move(argument), packRoot, "kittyskins.preview.clear", error);
    if (!window)
        return SDispatchResult{.success = false, .error = error};

    if (!kitty_skins::plugin::clearPreview(window, packRoot, error))
        return SDispatchResult{.success = false, .error = "kittyskins.preview.clear: " + error};

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

// Any native state transition that can change framing eligibility — fullscreen or
// maximized mode, pinned state, floating state, re-applied window rules, a
// workspace move, focus — re-runs the one attach/detach decision for that
// window. The predicate itself decides whether anything changes, so these
// listeners need no further condition.
void onWindowState(PHLWINDOW window) {
    kitty_skins::plugin::syncWindow(window);
}

// The session lock changes eligibility for every window at once: entering it
// gives every framed window its native border back, leaving it restores the
// frames.
void onSessionLockChanged() {
    kitty_skins::plugin::syncWindows();
}

// A workspace becoming active — including a special workspace being toggled on
// or off — changes the hidden state of exactly its own windows, so only those are
// re-evaluated. This is what brings a frame back to a window that was detached
// while its workspace was hidden and gets no other event when it reappears.
void onWorkspaceActivated(PHLWORKSPACE workspace) {
    if (!workspace)
        return;

    const std::vector<PHLWINDOW> windows = Desktop::windowState()->windows();
    for (const PHLWINDOW& window : windows) {
        if (window && window->m_workspace == workspace)
            kitty_skins::plugin::syncWindow(window);
    }
}

void onConfigReloaded() {
    std::string error;
    if (!kitty_skins::plugin::reloadActivePack(error))
        Log::logger->log(Log::WARN, "kitty-skins: active pack could not be reloaded: {}", error);

    // Target, exclusion list and store root may have changed: re-evaluate every
    // mapped window.
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
    // Exact classes that never carry a frame, comma separated. No Hyprland
    // property identifies a utility or test surface, so the exclusion list is
    // explicit configuration; the default names the two system pickers that are
    // never ordinary application windows.
    state.excludeConfig = makeShared<Config::Values::CStringValue>(
        "plugin:kittyskins:exclude", "Comma separated window classes that never carry a frame.",
        "xdg-desktop-portal-gtk,hyprland-share-picker");
    HyprlandAPI::addConfigValueV2(handle, state.rootConfig);
    HyprlandAPI::addConfigValueV2(handle, state.targetConfig);
    HyprlandAPI::addConfigValueV2(handle, state.excludeConfig);

    HyprlandAPI::addDispatcherV2(handle, kDispatcherName, dispatchUse);
    HyprlandAPI::addDispatcherV2(handle, kClearDispatcherName, dispatchClear);
    HyprlandAPI::addDispatcherV2(handle, kPreviewDispatcherName, dispatchPreview);
    HyprlandAPI::addDispatcherV2(handle, kPreviewClearDispatcherName, dispatchPreviewClear);

    // Lua-mode IPC cannot resolve addDispatcherV2 plugin dispatchers. Expose
    // matching factories so external callers can use
    // `hyprctl eval/dispatch hl.plugin.kittyskins.<operation>(...)`.
    const bool hasLuaUse = HyprlandAPI::addLuaFunction(handle, kLuaNamespace, kLuaUseName, luaUse);
    const bool hasLuaClear = HyprlandAPI::addLuaFunction(handle, kLuaNamespace, kLuaClearName, luaClear);
    const bool hasLuaPreview = HyprlandAPI::addLuaFunction(handle, kLuaNamespace, kLuaPreviewName, luaPreview);
    const bool hasLuaPreviewClear =
        HyprlandAPI::addLuaFunction(handle, kLuaNamespace, kLuaPreviewClearName, luaPreviewClear);
    if (!hasLuaUse || !hasLuaClear || !hasLuaPreview || !hasLuaPreviewClear)
        Log::logger->log(Log::DEBUG, "kitty-skins: one or more Lua control functions are unavailable");

    state.windowOpen        = Event::bus()->m_events.window.open.listen([](PHLWINDOW window) { onWindowOpen(window); });
    state.windowClose       = Event::bus()->m_events.window.close.listen([](PHLWINDOW window) { onWindowClose(window); });
    state.windowClass       = Event::bus()->m_events.window.class_.listen([](PHLWINDOW window) { onWindowClass(window); });
    // Every native transition that can change framing eligibility re-runs the
    // attach/detach decision for the one window it concerns; no polling and no
    // idle timer is involved.
    state.windowPin         = Event::bus()->m_events.window.pin.listen([](PHLWINDOW window) { onWindowState(window); });
    state.windowFullscreen  = Event::bus()->m_events.window.fullscreen.listen([](PHLWINDOW window) { onWindowState(window); });
    state.windowFloating    = Event::bus()->m_events.window.floating.listen([](PHLWINDOW window) { onWindowState(window); });
    state.windowUpdateRules = Event::bus()->m_events.window.updateRules.listen([](PHLWINDOW window) { onWindowState(window); });
    state.windowMove =
        Event::bus()->m_events.window.moveToWorkspace.listen([](PHLWINDOW window, PHLWORKSPACE) { onWindowState(window); });
    state.windowActive =
        Event::bus()->m_events.window.active.listen([](PHLWINDOW window, Desktop::eFocusReason) { onWindowState(window); });
    state.workspaceActive = Event::bus()->m_events.workspace.active.listen([](PHLWORKSPACE workspace) { onWorkspaceActivated(workspace); });
    state.workspaceSpecial =
        Event::bus()->m_events.workspace.specialActive.listen([](PHLWORKSPACE workspace, PHLMONITOR) { onWorkspaceActivated(workspace); });
    state.configReloaded = Event::bus()->m_events.config.reloaded.listen([] { onConfigReloaded(); });

    // The lock manager owns the session lock state the framing predicate reads.
    if (g_pSessionLockManager) {
        state.sessionLocked   = g_pSessionLockManager->m_events.lock.listen([] { onSessionLockChanged(); });
        state.sessionUnlocked = g_pSessionLockManager->m_events.unlock.listen([] { onSessionLockChanged(); });
    }

    std::string error;
    if (!kitty_skins::plugin::reloadActivePack(error))
        Log::logger->log(Log::WARN, "kitty-skins: no active skin loaded: {}", error);

    kitty_skins::plugin::syncWindows();
    kitty_skins::plugin::startEffectClock();

    return {"kitty-skins", "Data-driven window frames.", "q", VERSION};
}

APICALL EXPORT void PLUGIN_EXIT() {
    // Cancel callbacks before any decoration or GPU resource can be destroyed.
    kitty_skins::plugin::stopEffectClock();
    // 1. Clear the live and global render passes first: this destroys every queued
    //    element, including a CSkinPassElement nested inside a transformed-window
    //    pass, while plugin code is still mapped and before the decorations and
    //    runtime it points into are destroyed.
    kitty_skins::plugin::clearAllPendingPassElements();

    // 2. Decorations, which release their cached layout. The preview runtime is
    //    released with them, so no cached operation can outlive the textures it
    //    samples.
    kitty_skins::plugin::removeAllDecorations();
    kitty_skins::plugin::clearPreviewState();

    // 3. Dispatchers and Lua entry points.
    HyprlandAPI::removeDispatcher(kitty_skins::plugin::g_pluginHandle, kDispatcherName);
    HyprlandAPI::removeDispatcher(kitty_skins::plugin::g_pluginHandle, kClearDispatcherName);
    HyprlandAPI::removeDispatcher(kitty_skins::plugin::g_pluginHandle, kPreviewDispatcherName);
    HyprlandAPI::removeDispatcher(kitty_skins::plugin::g_pluginHandle, kPreviewClearDispatcherName);
    HyprlandAPI::removeLuaFunction(kitty_skins::plugin::g_pluginHandle, kLuaNamespace, kLuaUseName);
    HyprlandAPI::removeLuaFunction(kitty_skins::plugin::g_pluginHandle, kLuaNamespace, kLuaClearName);
    HyprlandAPI::removeLuaFunction(kitty_skins::plugin::g_pluginHandle, kLuaNamespace, kLuaPreviewName);
    HyprlandAPI::removeLuaFunction(kitty_skins::plugin::g_pluginHandle, kLuaNamespace, kLuaPreviewClearName);

    if (!kitty_skins::plugin::g_pState)
        return;

    // 4. Event listeners.
    kitty_skins::plugin::g_pState->windowOpen.reset();
    kitty_skins::plugin::g_pState->windowClose.reset();
    kitty_skins::plugin::g_pState->windowClass.reset();
    kitty_skins::plugin::g_pState->windowPin.reset();
    kitty_skins::plugin::g_pState->windowFullscreen.reset();
    kitty_skins::plugin::g_pState->windowFloating.reset();
    kitty_skins::plugin::g_pState->windowUpdateRules.reset();
    kitty_skins::plugin::g_pState->windowMove.reset();
    kitty_skins::plugin::g_pState->windowActive.reset();
    kitty_skins::plugin::g_pState->workspaceActive.reset();
    kitty_skins::plugin::g_pState->workspaceSpecial.reset();
    kitty_skins::plugin::g_pState->sessionLocked.reset();
    kitty_skins::plugin::g_pState->sessionUnlocked.reset();
    kitty_skins::plugin::g_pState->configReloaded.reset();

    // 5. Shared textures last.
    kitty_skins::plugin::g_pState->runtime.reset();
    kitty_skins::plugin::g_pState->rootConfig.reset();
    kitty_skins::plugin::g_pState->targetConfig.reset();
    kitty_skins::plugin::g_pState->excludeConfig.reset();
    kitty_skins::plugin::g_pState.reset();
}
