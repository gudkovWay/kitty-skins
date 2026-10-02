#include "state.hpp"

#include <cstdlib>
#include <sstream>
#include <utility>

#include <hyprland/src/debug/log/Logger.hpp>
#include <hyprland/src/desktop/state/WindowState.hpp>
#include <hyprland/src/desktop/view/Window.hpp>
#include <hyprland/src/managers/eventLoop/EventLoopManager.hpp>
#include <hyprland/src/managers/fullscreen/FullscreenController.hpp>
#include <hyprland/src/managers/SessionLockManager.hpp>
#include <hyprland/src/protocols/types/ContentType.hpp>
#include <hyprland/src/render/decorations/DecorationPositioner.hpp>
#include <hyprland/src/render/Renderer.hpp>
#include <hyprland/src/xwayland/XSurface.hpp>

#include "decoration.hpp"
#include "kitty_skins/store.hpp"
#include "pass.hpp"

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

namespace {

// The dedicated window class of the one live frame preview. The dispatchers
// drive exactly this window, and its class deliberately differs from the
// configured target, so admission is checked against the class directly instead
// of the target.
constexpr const char* PREVIEW_WINDOW_CLASS = "kitty-skin-preview";

// The "0x..." address hyprctl and the Studio use to name a live window.
std::string windowAddress(const PHLWINDOW& window) {
    if (!window)
        return "0x0";

    std::ostringstream address;
    address << "0x" << std::hex << reinterpret_cast<uintptr_t>(window.get());
    return address.str();
}

// Destroy the decoration of `window` while the window keeps living. A queued
// render pass borrows the decoration pointer, so the pass queue is discarded
// first; the cached layout — and with it the shared atlas references — is
// released with the decoration.
void detachDecoration(PHLWINDOW window) {
    if (!window)
        return;

    CSkinDecoration* decoration = decorationFor(window.get());
    if (!decoration)
        return;

    clearAllPendingPassElements();

    // The native border and rounding come back before the frame goes away, so a
    // window is never left with an ownerless zero in the highest slot.
    decoration->restoreNativeZeroing(true);

    decoration->invalidate();
    HyprlandAPI::removeWindowDecoration(g_pluginHandle, decoration);
}

// Rebuild the parsed exclusion list from the configured value. Called at every
// synchronisation pass — which is where a config reload, a pack change and the
// plugin start all arrive — so eligibility itself is a plain set lookup.
void refreshExcludeClasses() {
    if (!g_pState)
        return;

    const std::string raw = g_pState->excludeConfig ? g_pState->excludeConfig->value() : std::string{};

    g_pState->excludeClasses.clear();

    std::istringstream stream(raw);
    std::string        token;
    while (std::getline(stream, token, ',')) {
        const size_t first = token.find_first_not_of(" \t");
        if (first == std::string::npos)
            continue;

        const size_t last = token.find_last_not_of(" \t");
        g_pState->excludeClasses.insert(token.substr(first, last - first + 1));
    }
}

// Rebuild the cached target class and its global flag from the configured value.
// Called with the exclusion list at every synchronisation pass, so the per-frame
// target check never reads the config system and never allocates a string.
void refreshConfiguredTarget() {
    if (!g_pState)
        return;

    const std::string raw = g_pState->targetConfig ? g_pState->targetConfig->value() : std::string{};
    const std::string target = raw.empty() ? std::string{"kitty"} : raw;

    g_pState->configuredTargetClass = target;
    g_pState->targetIsGlobal        = target == "*";
}

// Exact window classes that never carry a frame. These are the utility and test
// surfaces that are not ordinary application toplevels and that no exposed
// window property identifies, so they are named in configuration.
bool classExcluded(const std::string& windowClass) {
    return g_pState && g_pState->excludeClasses.contains(windowClass);
}

} // namespace

bool windowMatchesTarget(const PHLWINDOW& window) {
    if (!window || !window->m_isMapped || window->isHidden())
        return false;

    // The dedicated preview window is eligible regardless of its class: it is
    // the one window the preview dispatchers drive, and its class deliberately
    // does not match the configured target.
    if (windowIsPreview(window))
        return true;

    if (!g_pState)
        return false;

    // "*" decorates every eligible mapped toplevel; any other value is an exact
    // class match. There is deliberately no regex or CSV language here. The
    // value is read from the cache refreshed at every synchronisation pass, so
    // the per-frame check allocates nothing.
    if (g_pState->targetIsGlobal)
        return true;

    return window->m_class == g_pState->configuredTargetClass;
}

bool globalTargetEnabled() {
    return g_pState && g_pState->targetIsGlobal;
}

bool windowFramingEligible(const PHLWINDOW& window) {
    if (!window)
        return false;

    // Only a mapped, visible window reserves extents or draws.
    if (!window->m_isMapped || window->isHidden())
        return false;

    // The dedicated preview window is admitted by identity, so an exclusion list
    // meant for application classes can never lock the preview out.
    if (!windowIsPreview(window) && classExcluded(window->m_class))
        return false;

    // X11 windows that asked for no border at all, and override-redirect popups
    // that no window manager is supposed to decorate.
    if (window->m_X11DoesntWantBorders || window->isX11OverrideRedirect())
        return false;

    // Dialogs and other transient surfaces: a modal window, an XWayland transient
    // child or a Wayland toplevel with a transient parent — none of them belongs
    // in the same frame band as the main window.
    if (window->isModal())
        return false;
    if (window->m_xwaylandSurface && window->m_xwaylandSurface->m_transient)
        return false;
    if (window->parent())
        return false;

    // Pinned surfaces (PiP, sticky helpers) are not part of a layout.
    if (window->m_pinned)
        return false;

    // The client declared game content: no frame band may sit over it.
    if (window->getContentType() == NContentType::CONTENT_TYPE_GAME)
        return false;

    // An explicit `decorate = false` rule is the supported per-window opt-out.
    if (!window->m_ruleApplicator || !window->m_ruleApplicator->decorate().valueOrDefault())
        return false;

    // Fullscreen and maximized content keeps the whole output.
    if (Fullscreen::controller()->isFullscreen(window, Fullscreen::FSMODE_FULLSCREEN) ||
        Fullscreen::controller()->isFullscreen(window, Fullscreen::FSMODE_MAXIMIZED))
        return false;

    // While the session is locked every ordinary frame steps aside; the unlock
    // event re-synchronises the windows.
    if (g_pSessionLockManager && g_pSessionLockManager->isSessionLocked())
        return false;

    return true;
}

bool windowFramed(const PHLWINDOW& window) {
    if (!window)
        return false;

    return runtimeFor(window).runtime != nullptr && windowMatchesTarget(window) && windowFramingEligible(window);
}

const SkinRuntime* currentRuntime() {
    if (!g_pState || !g_pState->runtime)
        return nullptr;
    return g_pState->runtime.get();
}

SilverMaterialRenderer* silverFor(const SkinRuntime* runtime) {
    if (!g_pState || runtime == nullptr)
        return nullptr;
    if (runtime == g_pState->previewRuntime.get())
        return g_pState->previewSilverMaterial.get();
    if (runtime == g_pState->runtime.get())
        return g_pState->silverMaterial.get();
    return nullptr;
}

bool windowIsPreview(const PHLWINDOW& window) {
    if (!g_pState || !window)
        return false;

    const PHLWINDOW preview = g_pState->previewWindow.lock();
    return preview && preview.get() == window.get();
}

SWindowRuntime runtimeFor(const PHLWINDOW& window) {
    SWindowRuntime resolved;
    if (!g_pState || !window)
        return resolved;

    // The preview runtime wins for the exact preview window and never falls back
    // to the global one: a preview window without a preview runtime draws
    // nothing rather than leaking the active pack into a candidate slot.
    if (windowIsPreview(window)) {
        resolved.runtime    = g_pState->previewRuntime.get();
        resolved.generation = g_pState->previewGeneration;
        return resolved;
    }

    resolved.runtime    = g_pState->runtime.get();
    resolved.generation = g_pState->generation;
    return resolved;
}

PHLWINDOW windowFromAddress(const std::string& address) {
    if (!g_pState || address.empty())
        return nullptr;

    // hyprctl and the Studio print a CWindow address as "0x<hex>"; the prefix is
    // optional so an address copied from either source is accepted.
    const char* begin = address.c_str();
    if (address.size() > 2 && begin[0] == '0' && (begin[1] == 'x' || begin[1] == 'X'))
        begin += 2;

    char*                    stop = nullptr;
    const unsigned long long raw  = std::strtoull(begin, &stop, 16);
    if (stop == begin || *stop != '\0')
        return nullptr;

    // An address is only trusted against the live window list.
    for (const PHLWINDOW& window : Desktop::windowState()->windows()) {
        if (window && static_cast<unsigned long long>(reinterpret_cast<uintptr_t>(window.get())) == raw)
            return window;
    }

    return nullptr;
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

namespace {

// Start the pack's silver material, if declared. Absence is a null renderer;
// a declared effect that cannot start is an error string, so the caller can
// fail the load transactionally exactly like loadRuntime does for the candle
// effect.
std::expected<std::shared_ptr<SilverMaterialRenderer>, std::string> loadSilver(const kitty_skins::SkinPack& pack) {
    if (!pack.silverEffect)
        return nullptr;
    auto renderer = std::make_shared<SilverMaterialRenderer>();
    std::string error;
    if (!renderer->initialize(*pack.silverEffect, error))
        return std::unexpected(error.empty() ? std::string("silver effect initialization failed") : error);
    return renderer;
}

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
    // The silver material is part of the pack contract: a declared effect that
    // cannot start fails the load before anything is mutated.
    auto silver = loadSilver((*candidate)->pack);
    if (!silver) {
        error = silver.error();
        return false;
    }
    if (g_pState->effectTimer)
        g_pState->effectTimer->updateTimeout(std::nullopt);
    g_pState->effectEpoch = std::chrono::steady_clock::now();

    // Damage the previous bounds before the swap and the new bounds after it, so a
    // mode or texture change leaves no stale pixels behind.
    for (const auto& entry : g_pState->decorations) {
        if (entry.second)
            entry.second->damageEntire();
    }

    g_pState->runtime = std::move(*candidate);
    g_pState->silverMaterial = std::move(*silver);
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

bool clearPack(const std::string& expectedPackRoot, std::string& error) {
    if (!g_pState) {
        error = "plugin state is not initialised";
        return false;
    }

    const SkinRuntime* runtime = currentRuntime();
    if (!runtime) {
        error = "no skin is currently loaded";
        return false;
    }

    // Guard the runtime ownership: only the pack that is actually running may
    // be cleared, so a stale expectation can never drop a newer selection.
    std::error_code ec;
    const std::filesystem::path expected = std::filesystem::weakly_canonical(expectedPackRoot, ec);
    if (ec) {
        error = "cannot resolve pack path \"" + expectedPackRoot + "\": " + ec.message();
        return false;
    }
    const std::filesystem::path running = std::filesystem::weakly_canonical(runtime->pack.root, ec);
    if (ec) {
        error = "cannot resolve running pack root: " + ec.message();
        return false;
    }
    if (expected != running) {
        error = "the running skin was loaded from \"" + running.string() + "\"; refusing to clear \"" +
                expected.string() + "\"";
        return false;
    }

    const std::string clearedId = runtime->pack.id;

    // Disarm the effect timer: no runtime means no animated overlay may re-arm it.
    if (g_pState->effectTimer)
        g_pState->effectTimer->updateTimeout(std::nullopt);

    for (const auto& entry : g_pState->decorations) {
        if (entry.second)
            entry.second->damageEntire();
    }

    // Render passes borrow decoration pointers; discard them before removal.
    clearAllPendingPassElements();
    removeAllDecorations();
    g_pState->runtime.reset();
    g_pState->silverMaterial.reset();
    ++g_pState->generation;

    // Runtime is gone: sync detaches anything that slipped through and never
    // attaches. A later use() path reattaches from scratch.
    syncWindows();

    Log::logger->log(Log::INFO, "kitty-skins: cleared skin \"{}\"", clearedId);
    return true;
}

bool applyPreview(PHLWINDOW window, const std::string& packRoot, std::string& error) {
    if (!g_pState) {
        error = "plugin state is not initialised";
        return false;
    }
    if (!window) {
        error = "the preview window is gone";
        return false;
    }
    // Admission is checked against the preview window itself, never against the
    // preview/global target: the dedicated class deliberately does not match the
    // configured target, so a target check would refuse every first preview.
    if (!window->m_isMapped || window->isHidden()) {
        error = "the preview window is not a mapped, visible window";
        return false;
    }
    if (window->m_class != PREVIEW_WINDOW_CLASS) {
        error = "window " + windowAddress(window) + " is not a \"" + PREVIEW_WINDOW_CLASS +
                "\" window; refusing to preview in it";
        return false;
    }

    // The candidate pack is already validated and immutable, so it is loaded
    // directly — no private copy is made. Nothing is mutated before the load
    // succeeds, so a failure preserves the previous preview and the global
    // runtime untouched.
    auto candidate = loadRuntime(packRoot);
    if (!candidate) {
        error = candidate.error();
        return false;
    }
    // Same contract as applyPack: a declared silver effect that cannot start
    // fails the preview before anything is mutated, leaving the previous
    // candidate untouched.
    auto silver = loadSilver((*candidate)->pack);
    if (!silver) {
        error = silver.error();
        return false;
    }

    const PHLWINDOW previous = g_pState->previewWindow.lock();
    if (previous && previous.get() != window.get()) {
        // Only one preview exists at a time: repaint and detach the previous
        // window before the new one is installed, so its frame cannot linger
        // there without a runtime behind it.
        if (CSkinDecoration* previousDecoration = decorationFor(previous.get()))
            previousDecoration->damageEntire();
        detachDecoration(previous);
    }

    // Forget the previous preview identity, runtime and generation first: the
    // resulting bump makes every layout cached from an older candidate stale.
    clearPreviewState();
    g_pState->previewWindow  = window;
    g_pState->previewRuntime = std::move(*candidate);
    g_pState->previewSilverMaterial = std::move(*silver);

    // Attach if the window has no decoration yet, then force the relayout: a
    // moment ago this frame may have been drawn from the global runtime or from
    // an older candidate. Only this window is touched.
    attachWindow(window);

    if (CSkinDecoration* decoration = decorationFor(window.get())) {
        decoration->invalidate();
        g_pDecorationPositioner->forceRecalcFor(window);
        window->updateWindowDecos();
        decoration->damageEntire();
    }

    // The previous preview window survived its preview: preview ownership has
    // moved, so it is an ordinary window again and is re-evaluated against the
    // target and the global runtime — with target "*" it falls back to the
    // global skin instead of staying undecorated. A closing window is skipped so
    // nothing is reattached to a frame that is going away.
    if (previous && previous.get() != window.get() && previous->m_isMapped)
        syncWindow(previous);

    Log::logger->log(Log::INFO, "kitty-skins: previewing skin \"{}\" in window \"{}\"", g_pState->previewRuntime->pack.id,
                     window->m_title);
    return true;
}

bool clearPreview(PHLWINDOW window, const std::string& expectedPackRoot, std::string& error) {
    if (!g_pState) {
        error = "plugin state is not initialised";
        return false;
    }

    const PHLWINDOW current = g_pState->previewWindow.lock();
    if (!current || !g_pState->previewRuntime) {
        error = "no frame preview is active";
        return false;
    }
    if (!window || current.get() != window.get()) {
        error = "the live frame preview belongs to window " + windowAddress(current) + "; refusing to clear it";
        return false;
    }

    // Guard the runtime ownership: only the candidate that is actually
    // previewing may be cleared, so a stale expectation can never drop a newer
    // one.
    std::error_code ec;
    const std::filesystem::path expected = std::filesystem::weakly_canonical(expectedPackRoot, ec);
    if (ec) {
        error = "cannot resolve pack path \"" + expectedPackRoot + "\": " + ec.message();
        return false;
    }
    const std::filesystem::path previewing = std::filesystem::weakly_canonical(g_pState->previewRuntime->pack.root, ec);
    if (ec) {
        error = "cannot resolve the preview pack root: " + ec.message();
        return false;
    }
    if (expected != previewing) {
        error = "the live preview was loaded from \"" + previewing.string() + "\"; refusing to clear \"" + expected.string() +
                "\"";
        return false;
    }

    const std::string clearedId = g_pState->previewRuntime->pack.id;

    if (CSkinDecoration* decoration = decorationFor(current.get()))
        decoration->damageEntire();
    detachDecoration(current);
    clearPreviewState();

    // The window keeps living and is an ordinary window again: the usual target
    // rules decide whether it stays decorated.
    syncWindow(current);

    Log::logger->log(Log::INFO, "kitty-skins: cleared the frame preview of skin \"{}\"", clearedId);
    return true;
}

void clearPreviewState() {
    if (!g_pState)
        return;

    g_pState->previewWindow.reset();
    g_pState->previewRuntime.reset();
    g_pState->previewSilverMaterial.reset();
    ++g_pState->previewGeneration;
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
    // A decoration is attached whenever the window has a runtime — the preview
    // runtime for the preview window, the global one for every other window —
    // the configured target admits it and the structural policy does not exclude
    // it; there is no load-order dependency on another plugin any more.
    if (!g_pState || !window)
        return;
    // No frame is created while the frames are being torn down: reclaiming the
    // native values refreshes a window, and that refresh must not attach a fresh
    // decoration from inside the removal pass.
    if (g_pState->framingSuspended)
        return;
    if (!windowFramed(window))
        return;
    if (decorationFor(window.get()) != nullptr)
        return;

    auto             decoration = makeUnique<CSkinDecoration>(window);
    CSkinDecoration* raw        = decoration.get();
    registerDecoration(raw);

    if (!HyprlandAPI::addWindowDecoration(g_pluginHandle, window, std::move(decoration))) {
        forgetDecoration(raw);
        Log::logger->log(Log::WARN, "kitty-skins: could not attach a decoration to window \"{}\"", window->m_title);
        return;
    }

    // The frame is the visible edge now: the native border and rounding give way
    // through the highest override slot, and the window is re-evaluated so the
    // frame's own extents are laid out without a native border around them. A
    // window that cannot draw yet (an unsupported output transform) keeps its
    // native decoration until the frame is actually wanted.
    if (raw->wanted())
        raw->applyNativeZeroing();
}

void detachWindow(PHLWINDOW window) {
    if (!g_pState || !window)
        return;

    // Closing — or hiding — the preview window drops the preview before its weak
    // identity can expire, so no later lookup can resurrect a window that is
    // going away, and the candidate's textures are released with it.
    if (windowIsPreview(window)) {
        detachDecoration(window);
        clearPreviewState();
        return;
    }

    CSkinDecoration* decoration = decorationFor(window.get());
    if (!decoration)
        return;

    // Hand the native border and rounding back before the frame disappears. A
    // window that is already closing has nothing left to refresh.
    decoration->restoreNativeZeroing(true);
    HyprlandAPI::removeWindowDecoration(g_pluginHandle, decoration);
}

void syncWindow(PHLWINDOW window) {
    if (!g_pState || !window)
        return;

    const bool framed   = windowFramed(window);
    const bool attached = decorationFor(window.get()) != nullptr;

    if (framed && !attached)
        attachWindow(window);
    else if (!framed && attached)
        detachWindow(window);
}

void syncWindows() {
    if (!g_pState)
        return;

    // Every path that can change the configured exclusion list or target
    // (plugin start, config reload, pack apply) ends here, so this is where both
    // caches are re-parsed.
    refreshExcludeClasses();
    refreshConfiguredTarget();

    // Copy: attaching and detaching mutate the decorations, not the window list.
    const std::vector<PHLWINDOW> windows = Desktop::windowState()->windows();

    for (const PHLWINDOW& window : windows)
        syncWindow(window);
}


void startEffectClock() {
    if (!g_pState || g_pState->effectTimer || !g_pEventLoopManager)
        return;

    g_pState->effectTimer = makeShared<CEventLoopTimer>(
        std::nullopt, [](SP<CEventLoopTimer> self, void*) {
            if (!g_pState)
                return;
            // Each decoration decides from the runtime that owns its own window,
            // so an animated preview runs even when the global runtime is static
            // or absent; one shared timer drives every effect.
            const auto now     = std::chrono::steady_clock::now();
            bool       visible = false;
            for (const auto& entry : g_pState->decorations) {
                if (entry.second && entry.second->damageEffects(now))
                    visible = true;
            }
            // A one-shot timer remains disarmed when no effect was visible.
            // The next actual decoration draw arms it again.
            if (visible)
                self->updateTimeout(std::chrono::milliseconds(34));
        }, nullptr);
    g_pEventLoopManager->addTimer(g_pState->effectTimer);
}

void armEffectClock() {
    if (g_pState && g_pState->effectTimer && !g_pState->effectTimer->armed())
        g_pState->effectTimer->updateTimeout(std::chrono::milliseconds(34));
}

void stopEffectClock() {
    if (!g_pState || !g_pState->effectTimer)
        return;
    g_pState->effectTimer->cancel();
    if (g_pEventLoopManager)
        g_pEventLoopManager->removeTimer(g_pState->effectTimer);
    g_pState->effectTimer.reset();
}

uint64_t effectTick() {
    if (!g_pState)
        return 0;
    const auto elapsed = std::chrono::steady_clock::now() - g_pState->effectEpoch;
    return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(elapsed).count() / 34);
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

    // Each restore refreshes its window; while that happens no new frame may be
    // attached, or a teardown pass would leave a live decoration behind.
    g_pState->framingSuspended = true;

    for (CSkinDecoration* decoration : decorations) {
        // Give the native border and rounding back with a refresh: this runs when
        // the skin is cleared and when the plugin unloads, and the window keeps
        // living with its normal decoration afterwards.
        decoration->restoreNativeZeroing(true);
        HyprlandAPI::removeWindowDecoration(g_pluginHandle, decoration);
    }

    g_pState->decorations.clear();
    g_pState->framingSuspended = false;
}

}
