#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include <hyprland/src/config/values/types/StringValue.hpp>
#include <hyprland/src/desktop/DesktopTypes.hpp>
#include <hyprland/src/helpers/memory/Memory.hpp>
#include <hyprland/src/helpers/signal/Signal.hpp>
#include <hyprland/src/plugins/PluginAPI.hpp>

#include "texture.hpp"

namespace kitty_skins::plugin {

class CSkinDecoration;

// Plugin-wide state. The runtime is shared by every decoration; the decoration registry
// is non-owning because Hyprland windows own their decorations and each decoration
// removes itself again when it is destroyed.
struct SGlobalState {
    std::shared_ptr<SkinRuntime>      runtime;
    uint64_t                          generation = 0;
    std::vector<CSkinDecoration*>     decorations;
    SP<Config::Values::CStringValue>  rootConfig;
    SP<Config::Values::CStringValue>  classConfig;
    CHyprSignalListener               windowOpen;
    CHyprSignalListener               windowClose;
    CHyprSignalListener               windowClass;
    CHyprSignalListener               configReloaded;
};

inline UP<SGlobalState> g_pState;
inline HANDLE           g_pluginHandle = nullptr;

// `${XDG_CONFIG_HOME:-$HOME/.config}/kitty-skins`
std::string defaultStoreRoot();
std::string configuredRoot();
std::string configuredClass();

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

}
