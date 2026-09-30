#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <unordered_map>

#include <hyprland/src/config/values/types/StringValue.hpp>
#include <hyprland/src/desktop/DesktopTypes.hpp>
#include <hyprland/src/helpers/memory/Memory.hpp>
#include <hyprland/src/helpers/signal/Signal.hpp>
#include <hyprland/src/plugins/PluginAPI.hpp>

#include "texture.hpp"

namespace Desktop::View {
class CWindow;
}

namespace kitty_skins::plugin {

class CSkinDecoration;

// Plugin-wide state. The runtime is shared by every decoration; the decoration
// registry is a raw-window map so an event resolves the owning frame in O(1)
// without taking ownership or bouncing through a window lookup.
struct SGlobalState {
    std::shared_ptr<SkinRuntime>                                  runtime;
    uint64_t                                                      generation = 0;
    std::unordered_map<Desktop::View::CWindow*, CSkinDecoration*> decorations;
    SP<Config::Values::CStringValue>                              rootConfig;
    SP<Config::Values::CStringValue>                              targetConfig;
    CHyprSignalListener                                           windowOpen;
    CHyprSignalListener                                           windowClose;
    CHyprSignalListener                                           windowClass;
    CHyprSignalListener                                           configReloaded;
};

inline UP<SGlobalState> g_pState;
inline HANDLE           g_pluginHandle = nullptr;

// `${XDG_CONFIG_HOME:-$HOME/.config}/kitty-skins`
std::string defaultStoreRoot();
std::string configuredRoot();
// The decoration target: an exact window class, or "*" for every eligible window.
std::string configuredTarget();

// True when the window is mapped, visible, and matches the configured target.
bool windowMatchesTarget(const PHLWINDOW& window);

const SkinRuntime* currentRuntime();

// Transactionally replace the active runtime with the pack at `packRoot`. On failure the
// previous runtime, layout caches and reservations stay untouched.
bool applyPack(const std::string& packRoot, std::string& error);

// Resolve `<root>/active` through the shared store and load it.
bool reloadActivePack(std::string& error);

void attachWindow(PHLWINDOW window);
void detachWindow(PHLWINDOW window);
void syncWindow(PHLWINDOW window);
void syncWindows();
void removeAllDecorations();

// Decoration registry: keyed by the raw window pointer, non-owning.
void             registerDecoration(CSkinDecoration* decoration);
void             forgetDecoration(CSkinDecoration* decoration);
CSkinDecoration* decorationFor(Desktop::View::CWindow* window);

}
