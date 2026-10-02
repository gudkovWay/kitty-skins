#pragma once

#include <chrono>
#include <cstdint>
#include <memory>
#include <string>
#include <unordered_map>
#include <unordered_set>

#include <hyprland/src/config/values/types/StringValue.hpp>
#include <hyprland/src/desktop/DesktopTypes.hpp>
#include <hyprland/src/helpers/memory/Memory.hpp>
#include <hyprland/src/helpers/signal/Signal.hpp>
#include <hyprland/src/plugins/PluginAPI.hpp>
#include <hyprland/src/managers/eventLoop/EventLoopTimer.hpp>

#include "texture.hpp"

#include "silver_effect.hpp"

namespace Desktop::View {
class CWindow;
}

namespace kitty_skins::plugin {

class CSkinDecoration;

// Plugin-wide state. The global runtime is shared by every ordinary window and
// the dedicated preview window carries its own; the decoration registry is a
// raw-window map so an event resolves the owning frame in O(1) without taking
// ownership or bouncing through a window lookup.
struct SGlobalState {
    std::shared_ptr<SkinRuntime>                                  runtime;
    uint64_t                                                      generation = 0;
    // Optional silver material motion, parallel to the runtime it belongs to:
    // present only when the loaded pack declares `silver_effect`, and released
    // with that runtime. A pack without one allocates nothing here.
    std::shared_ptr<SilverMaterialRenderer>                       silverMaterial;
    std::shared_ptr<SilverMaterialRenderer>                       previewSilverMaterial;
    // Live frame preview: one dedicated window driven by the Studio through the
    // address-scoped Lua preview API, with its own runtime and generation. The
    // window identity is non-owning, so a closed window can never leave a
    // dangling pointer behind. The preview runtime is deliberately separate from
    // `runtime`: previewing a candidate pack never changes what the global
    // use/clear pair or the persisted store consider active. At most one preview
    // window and runtime exist at a time.
    PHLWINDOWREF                                                  previewWindow;
    std::shared_ptr<SkinRuntime>                                  previewRuntime;
    uint64_t                                                      previewGeneration = 0;
    std::unordered_map<Desktop::View::CWindow*, CSkinDecoration*> decorations;
    // Set while every frame is being torn down: a refresh of the reclaimed
    // native values must not be able to attach a fresh decoration from inside the
    // removal pass, least of all during plugin unload.
    bool                                                          framingSuspended = false;
    // Parsed `plugin:kittyskins:exclude`: the exact classes that never carry a
    // frame. Rebuilt from the config value at every synchronisation pass, so the
    // per-frame eligibility check never reads the config system.
    std::unordered_set<std::string>                               excludeClasses;
    // Cached `plugin:kittyskins:target`: the exact class that frames ordinary
    // windows, or the sentinel `*` in `targetIsGlobal`. Refreshed beside the
    // exclusion list at every synchronisation pass, so the per-frame target check
    // is a plain value compare and never allocates a string.
    std::string                                                   configuredTargetClass = "kitty";
    bool                                                          targetIsGlobal        = false;
    SP<Config::Values::CStringValue>                              rootConfig;
    SP<Config::Values::CStringValue>                              targetConfig;
    SP<Config::Values::CStringValue>                              excludeConfig;
    CHyprSignalListener                                           windowOpen;
    CHyprSignalListener                                           windowClose;
    CHyprSignalListener                                           windowClass;
    CHyprSignalListener                                           windowPin;
    CHyprSignalListener                                           windowFullscreen;
    CHyprSignalListener                                           windowFloating;
    CHyprSignalListener                                           windowUpdateRules;
    CHyprSignalListener                                           windowMove;
    CHyprSignalListener                                           windowActive;
    CHyprSignalListener                                           workspaceActive;
    CHyprSignalListener                                           workspaceSpecial;
    CHyprSignalListener                                           sessionLocked;
    CHyprSignalListener                                           sessionUnlocked;
    CHyprSignalListener                                           configReloaded;
    SP<CEventLoopTimer>                                            effectTimer;
    std::chrono::steady_clock::time_point                          effectEpoch = std::chrono::steady_clock::now();
};

inline UP<SGlobalState> g_pState;
inline HANDLE           g_pluginHandle = nullptr;

// `${XDG_CONFIG_HOME:-$HOME/.config}/kitty-skins`
std::string defaultStoreRoot();
std::string configuredRoot();
// True when the configured target is the global "*": every eligible window is
// framed, and ordinary windows are forced onto the adaptive layout instead of
// the source-aspect exact one. Reads the boolean cached by the last
// synchronisation pass; never touches the config system.
bool globalTargetEnabled();

// True when the window may carry a frame: mapped and visible, and either the
// dedicated preview window or a match of the configured target. Target matching
// only; the structural policy is windowFramingEligible.
bool windowMatchesTarget(const PHLWINDOW& window);

// Structural framing policy, independent of the configured target: the window
// must be a mapped, visible, ordinary decorated toplevel that is not excluded by
// its surface kind, its transient relationship, its pinned state, its content
// type, the decoration rule or the session state. Attachment, extents and
// drawing all go through this one predicate.
bool windowFramingEligible(const PHLWINDOW& window);

// True when a runtime owns the window, the configured target admits it and the
// structural policy admits it: the one decision that attaches, detaches and
// re-synchronises a frame.
bool windowFramed(const PHLWINDOW& window);

const SkinRuntime* currentRuntime();

// The silver material renderer owned by `runtime`, or null when that runtime's
// pack declares no silver effect.
SilverMaterialRenderer* silverFor(const SkinRuntime* runtime);

// The runtime and the layout generation that own one specific window: the
// preview runtime wins for the exact preview window only, every other window
// falls back to the global runtime, and the preview window never falls back.
struct SWindowRuntime {
    const SkinRuntime* runtime    = nullptr;
    uint64_t           generation = 0;
};

// True when `window` is the dedicated preview window.
bool windowIsPreview(const PHLWINDOW& window);

// Resolve the runtime and generation that own `window`.
SWindowRuntime runtimeFor(const PHLWINDOW& window);

// Resolve a live "0x..." window address against the compositor's live window
// list. Null when no live window has that address.
PHLWINDOW windowFromAddress(const std::string& address);

// Load `packRoot` into the dedicated preview window. Transactional: a failed
// load leaves the previous preview and the global runtime untouched, because the
// pack is loaded (it is already validated and immutable, so it is used in place)
// before any state is touched. A different previous preview window is detached
// first; only the new one is attached and recalculated. The global runtime, the
// store's active link and every other window stay untouched.
bool applyPreview(PHLWINDOW window, const std::string& packRoot, std::string& error);

// Remove the frame preview if, and only if, it belongs to `window` and was
// loaded from `expectedPackRoot`. A stale address or path is a guarded failure,
// so a delayed clear can never drop a newer candidate. The global runtime is
// never touched.
bool clearPreview(PHLWINDOW window, const std::string& expectedPackRoot, std::string& error);

// Drop the preview runtime and the preview window identity without touching the
// window itself; the preview generation advances so no cached layout survives.
void clearPreviewState();

// Transactionally replace the active runtime with the pack at `packRoot`. On failure the
// previous runtime, layout caches and reservations stay untouched.
bool applyPack(const std::string& packRoot, std::string& error);

// Remove the current runtime if, and only if, it was loaded from
// `expectedPackRoot`. A different or absent runtime is a guarded failure: a
// newer selection can never be cleared by a stale expectation. Removes every
// decoration with damage, disarms the candle timer and leaves the plugin fully
// usable for a subsequent use.
bool clearPack(const std::string& expectedPackRoot, std::string& error);

// Resolve `<root>/active` through the shared store and load it.
bool reloadActivePack(std::string& error);

void attachWindow(PHLWINDOW window);
void detachWindow(PHLWINDOW window);
void syncWindow(PHLWINDOW window);
void syncWindows();
void removeAllDecorations();

// One render-driven timer for all animated overlays (candle and accents). No
// timer runs for fully static packs.
void startEffectClock();
void armEffectClock();
void stopEffectClock();
// Whole 34 ms ticks since the loaded pack's effect epoch.
uint64_t effectTick();

// Decoration registry: keyed by the raw window pointer, non-owning.
void             registerDecoration(CSkinDecoration* decoration);
void             forgetDecoration(CSkinDecoration* decoration);
CSkinDecoration* decorationFor(Desktop::View::CWindow* window);

}
