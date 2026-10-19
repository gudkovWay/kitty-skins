#include "resources.hpp"
#include "weather.hpp"
#include "ambient.hpp"
#include "client_source.hpp"

#include <array>
#include <chrono>
#include <stdexcept>
#include <string_view>
#include <unordered_map>
#include <utility>
#include <vector>
#include <lua.hpp>

#include <hyprland/src/config/values/types/IntValue.hpp>
#include <hyprland/src/config/values/types/StringValue.hpp>
#include <hyprland/src/config/shared/actions/ConfigActions.hpp>
#include <hyprland/src/debug/log/Logger.hpp>
#include <hyprland/src/desktop/state/FocusState.hpp>
#include <hyprland/src/desktop/rule/windowRule/WindowRuleApplicator.hpp>
#include <hyprland/src/desktop/state/WindowState.hpp>
#include <hyprland/src/desktop/state/ViewState.hpp>
#include <hyprland/src/desktop/view/Window.hpp>
#include <hyprland/src/event/EventBus.hpp>
#include <hyprland/src/helpers/MiscFunctions.hpp>
#include <hyprland/src/managers/SessionLockManager.hpp>
#include <hyprland/src/managers/eventLoop/EventLoopManager.hpp>
#include <hyprland/src/managers/fullscreen/FullscreenController.hpp>
#include <hyprland/src/plugins/PluginAPI.hpp>
#include <hyprland/src/layout/LayoutManager.hpp>
#include <hyprland/src/render/OpenGL.hpp>
#include <hyprland/src/render/Renderer.hpp>
#include <hyprland/src/render/decorations/IHyprWindowDecoration.hpp>
#include <hyprland/src/render/pass/PassElement.hpp>
#include <hyprland/src/state/WorkspaceState.hpp>

#ifndef VERSION
#define VERSION "0.1.0"
#endif

namespace bamboo02 {
namespace {
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
// Layout clearance includes exterior sculpture; the painted ring stays 52px.
constexpr std::array<double, 4> kReservedInsets = {164.0, 76.0, 164.0, 52.0}; // L/T/R/B
static_assert(kReservedInsets[0] == kReservedInsets[2], "Keep the frame horizontally centered");
static_assert(kReservedInsets[2] == kInset + 112.0, "Retain full right ornament clearance");
static_assert(kReservedInsets[1] == kInset + 24.0 && kReservedInsets[3] == kInset);

constexpr std::vector<std::string> parseTargets(std::string_view value) {
    if (value.empty() || value == "*") return {"kitty"};
    std::vector<std::string> targets;
    while (!value.empty()) {
        const auto comma = value.find(',');
        auto item = value.substr(0, comma);
        const auto first = item.find_first_not_of(" \t");
        if (first != std::string_view::npos) {
            item = item.substr(first, item.find_last_not_of(" \t") - first + 1);
            targets.emplace_back(item);
        }
        if (comma == std::string_view::npos) break;
        value.remove_prefix(comma + 1);
    }
    return targets;
}
constexpr bool matchesClass(const std::vector<std::string>& targets, std::string_view windowClass) {
    for (const auto& target : targets)
        if (target == windowClass) return true;
    return false;
}
static_assert([] {
    const auto targets = parseTargets("kitty,zen");
    return matchesClass(targets, "kitty") && matchesClass(targets, "zen") &&
        !matchesClass(targets, "kitty-test") && !matchesClass(targets, "com.ayugram.desktop");
}(), "Bamboo must attach to both exact app classes, without capturing other windows");
static_assert(matchesClass(parseTargets("kitty"), "kitty"));
static_assert(matchesClass(parseTargets(""), "kitty"));
static_assert(matchesClass(parseTargets("*"), "kitty") && !matchesClass(parseTargets("*"), "zen"));
static_assert(matchesClass(parseTargets(" kitty, zen "), "zen"));
static_assert(!matchesClass(parseTargets("kitty,zen"), "agent-test"));
class Decoration;

struct WindowScene {
    explicit WindowScene(PHLWINDOW w, std::shared_ptr<Runtime> r) : window(w), runtime(std::move(r)), weather(runtime->root), ambient(runtime->root) {}
    PHLWINDOWREF window;
    std::shared_ptr<Runtime> runtime;
    Canvas canvas;
    Weather weather;
    Ambient ambient;
    Decoration* under = nullptr;
    Decoration* over = nullptr;
    CBox assigned;
    bool assignedValid = false, alive = true, failed = false, running = false;
    bool ambientWarning = false, weatherWarning = false;
    int hoveredControl = -1, pressedControl = -1;
    double accumulated = 0.0;
    Clock::time_point resumed = Clock::now(), rendered = Clock::time_point::min();
    double seconds() const { return accumulated + (running ? std::chrono::duration<double>(Clock::now() - resumed).count() : 0.0); }
    void resume() { if (!running) { resumed = Clock::now(); running = true; } }
    void pause() { if (running) { accumulated = seconds(); running = false; } }
};
struct MinimizedWindow {
    PHLWINDOWREF window;
    PHLWORKSPACE originalWorkspace, minimizedWorkspace;
    bool restorePendingFocus = false;
};
struct Global {
    HANDLE handle = nullptr;
    std::shared_ptr<Runtime> runtime;
    SP<Config::Values::CStringValue> root, target;
    SP<Config::Values::CIntValue> reduced, enabled;
    std::vector<std::string> targetClasses = parseTargets("kitty");
    std::unordered_map<Desktop::View::CWindow*, std::shared_ptr<WindowScene>> windows;
    std::vector<MinimizedWindow> minimized;
    std::vector<CHyprSignalListener> listeners;
    SP<CEventLoopTimer> timer;
};
std::unique_ptr<Global> g;

bool reduced() { return g && g->reduced && g->reduced->value() != 0; }
bool matches(PHLWINDOW w) {
    // No wildcard interpretation, utility-window exemptions, or global target.
    return g && w && w->m_isMapped && matchesClass(g->targetClasses, w->m_class);
}
bool wanted(const WindowScene& s) {
    const auto w = s.window.lock();
    if (!g || !s.alive || s.failed || !s.runtime || !matches(w) || w->isHidden() || w->m_X11DoesntWantBorders) return false;
    if (!w->m_ruleApplicator || !w->m_ruleApplicator->decorate().valueOrDefault()) return false;
    const auto monitor = w->m_monitor.lock();
    if (!monitor || monitor->m_transform != WL_OUTPUT_TRANSFORM_NORMAL) return false;
    return !Fullscreen::controller()->isFullscreen(w, Fullscreen::FSMODE_FULLSCREEN);
}
bool visible(const WindowScene& s) {
    const auto w = s.window.lock();
    return wanted(s) && (!g_pSessionLockManager || !g_pSessionLockManager->isSessionLocked()) &&
        g_pHyprRenderer && g_pHyprRenderer->shouldRenderWindow(w);
}
void workspaceOffset(CBox& b, const PHLWINDOW& w) {
    if (w->m_workspace && !w->m_pinned) b.translate(w->m_workspace->m_renderOffset->value());
}
CBox clientGlobal(const WindowScene& s) {
    const auto w = s.window.lock();
    if (!w) return {};
    CBox b = w->getWindowMainSurfaceBox();
    workspaceOffset(b, w);
    return b;
}
CBox sceneGlobal(const WindowScene& s) {
    CBox client = clientGlobal(s);
    if (client.w < 1 || client.h < 1) return {};
    // Painted ring and exterior sculpture are different bounds. The ring
    // touches the actual client; the extra112R/24T never becomes an inner moat.
    CBox b(client.x - 52, client.y - 76, client.w + 216, client.h + 128);
    if (s.assignedValid) {
        CBox assigned = s.assigned;
        const auto w = s.window.lock();
        assigned.translate(g_pDecorationPositioner->getEdgeDefinedPoint(
            DECORATION_EDGE_TOP | DECORATION_EDGE_BOTTOM | DECORATION_EDGE_LEFT | DECORATION_EDGE_RIGHT, s.window));
        workspaceOffset(assigned, w);
        const double x = std::min(b.x, assigned.x), y = std::min(b.y, assigned.y);
        b = CBox(x, y, std::max(b.x + b.w, assigned.x + assigned.w) - x, std::max(b.y + b.h, assigned.y + assigned.h) - y);
    }
    return b;
}
CBox monitorLogical(const WindowScene& s, PHLMONITOR monitor) {
    CBox b = sceneGlobal(s);
    const auto w = s.window.lock();
    if (w && monitor) b.translate(w->m_floatingOffset - monitor->m_position);
    return b;
}
CBox clientPhysical(const WindowScene& s, PHLMONITOR monitor) {
    CBox b = clientGlobal(s);
    if (const auto w = s.window.lock(); w && monitor) {
        b.translate(w->m_floatingOffset - monitor->m_position);
        b.scale(monitor->m_scale).round();
    }
    return b;
}
void damage(const WindowScene& s) {
    if (!g_pHyprRenderer) return;
    CBox b = sceneGlobal(s);
    if (const auto w = s.window.lock()) b.translate(w->m_floatingOffset);
    if (b.w > 0 && b.h > 0) g_pHyprRenderer->damageBox(b.expand(2));
}
void armTimer() {
    if (g && g->timer && !reduced() && !g->timer->armed()) g->timer->updateTimeout(34ms);
}
void paintNative(const SP<Render::ITexture>& texture, const CBox& destination, float alpha) {
    Render::GL::CHyprOpenGLImpl::STextureRenderData data;
    data.a = alpha; data.allowDim = false;
    Render::GL::g_pHyprOpenGL->renderTexture(texture, destination, data);
}

bool resizable(PHLWINDOW w) {
    if (!w) return false;
    const auto lo = w->minSize(), hi = w->maxSize();
    return !lo || !hi || hi->x<=0 || hi->y<=0 || lo->x != hi->x || lo->y != hi->y;
}
CBox controlBox(const CBox& client,int action) {
    // index.tsx: header width is the root minus L/R beams; its children are
    // right-aligned after a flexible spacer. Plates are stable36px targets.
    const double padding = std::min(64.0,std::max(8.0,(client.w-120.0)/2.0));
    return CBox(client.x+client.w-padding-120.0+42.0*action,client.y-44.0,36,36);
}
int controlAt(const WindowScene& s,const Vector2D& point) {
    CBox client = clientGlobal(s);
    if (const auto w = s.window.lock()) client.translate(w->m_floatingOffset);
    for (int i=0; i<3; ++i) {
        const CBox b = controlBox(client,i);
        if (point.x>=b.x && point.x<b.x+b.w && point.y>=b.y && point.y<b.y+b.h) return i;
    }
    return -1;
}
void drawControls(WindowScene& s,PHLMONITOR monitor,const CBox& client,float alpha) {
    const auto w = s.window.lock();
    const bool enabled = resizable(w);
    const bool maximized = Fullscreen::controller()->isFullscreen(w,Fullscreen::FSMODE_MAXIMIZED);
    const double scale=monitor->m_scale;
    CBox logical(client.x/scale,client.y/scale,client.w/scale,client.h/scale);
    for (int i=0; i<3; ++i) {
        CBox plate=controlBox(logical,i);
        const float a=alpha*(i==1 && !enabled ? 0.45F : 1.0F);
        CBox glyph(plate.x+11,plate.y+10,14,14);
        plate.scale(scale).round(); glyph.scale(scale).round();
        paintNative(i==s.hoveredControl ? s.runtime->plateHover : s.runtime->plate,plate,a);
        const int index=i==0 ? 0 : i==2 ? 3 : maximized ? 2 : 1;
        paintNative(s.runtime->controlGlyphs[index],glyph,a);
    }
}
void hoverControls(const Vector2D& point) {
    if (!g) return;
    const auto top=Desktop::viewState()->hitTest().windowAt(point,
        Desktop::View::ALLOW_FLOATING | Desktop::View::INPUT_EXTENTS | Desktop::View::RESERVED_EXTENTS);
    for (const auto& [key,s] : g->windows) {
        const int hovered=visible(*s) && s->window.lock()==top ? controlAt(*s,point) : -1;
        if (hovered!=s->hoveredControl) { s->hoveredControl=hovered; damage(*s); }
        if (s->pressedControl>=0 && hovered!=s->pressedControl) s->pressedControl=-1;
    }
}
void forgetMinimized(PHLWINDOW w) {
    if (g) std::erase_if(g->minimized,[&](const auto& record){ return !record.window || record.window.lock()==w; });
}
Config::Actions::ActionResult minimize(PHLWINDOW w) {
    if (!g || !w || !w->m_workspace) return Config::Actions::actionError("bamboo02: no window/workspace to minimize");
    const auto monitor=w->m_monitor.lock();
    if (!monitor) return Config::Actions::actionError("bamboo02: minimize monitor unavailable");
    const auto id=getWorkspaceIDNameFromString("special:bamboo02-minimized");
    auto workspace=State::workspaceState()->query().id(id.id).run();
    if (!workspace) workspace=State::workspaceState()->create(id.id,monitor->m_id,id.name,false);
    if (!workspace) return Config::Actions::actionError("bamboo02: cannot create recoverable minimize workspace");
    const auto original=w->m_workspace;
    if (original==workspace) return Config::Actions::actionError("bamboo02: window is already in minimize workspace");
    auto result=Config::Actions::moveToWorkspace(workspace,true,w);
    if (result) {
        forgetMinimized(w);
        g->minimized.push_back({w,original,workspace});
        if (monitor->m_activeSpecialWorkspace==workspace) {
            auto hidden=Config::Actions::toggleSpecial(workspace);
            if (!hidden) return Config::Actions::actionError("bamboo02: window moved to minimize workspace, but hiding that workspace failed; recovery retained: "+hidden.error().message);
        }
    }
    return result;
}
SDispatchResult restoreMinimized(std::string argument,bool focus=true) {
    if (!g) return {.success=false,.error="bamboo02: plugin state unavailable"};
    if (!argument.empty() && argument!="all") return {.success=false,.error="bamboo02:restore expects empty argument or all"};
    const bool all=argument=="all";
    bool restored=false;
    for (size_t i=g->minimized.size(); i>0;) {
        --i;
        const auto record=g->minimized[i];
        const auto w=record.window.lock();
        if (!w || !w->m_isMapped) { g->minimized.erase(g->minimized.begin()+i); continue; }
        // An external window-manager action may already have restored it.
        if (w->m_workspace!=record.minimizedWorkspace && (!record.restorePendingFocus || w->m_workspace!=record.originalWorkspace)) {
            g->minimized.erase(g->minimized.begin()+i); continue;
        }
        auto original=State::workspaceState()->query().id(record.originalWorkspace->m_id).run();
        if (!original) {
            auto monitor=record.originalWorkspace->m_monitor.lock();
            if (!monitor) monitor=w->m_monitor.lock();
            if (!monitor) return {.success=false,.error="bamboo02: restore monitor unavailable"};
            original=State::workspaceState()->create(record.originalWorkspace->m_id,monitor->m_id,record.originalWorkspace->m_name,false);
        }
        if (!original) return {.success=false,.error="bamboo02: cannot restore original workspace"};
        if (w->m_workspace!=original) {
            auto result=Config::Actions::moveToWorkspace(original,true,w);
            if (!result) return {.success=false,.error="bamboo02: restore move failed; recovery retained: "+result.error().message};
        }
        g->minimized[i].originalWorkspace=original;
        g->minimized[i].restorePendingFocus=true;
        if (focus) {
            auto changed=Config::Actions::changeWorkspace(original);
            if (!changed) return {.success=false,.error="bamboo02: window returned to original workspace, but workspace activation failed; focus recovery retained: "+changed.error().message};
            auto focused=Config::Actions::focus(w);
            if (!focused) return {.success=false,.error="bamboo02: window and workspace restored, but window focus failed; focus recovery retained: "+focused.error().message};
        }
        g->minimized.erase(g->minimized.begin()+i);
        restored=true;
        if (!all) break;
    }
    return restored || all ? SDispatchResult{} : SDispatchResult{.success=false,.error="bamboo02: no minimized window"};
}
void activateControl(PHLWINDOW w,int action) {
    Config::Actions::ActionResult result=Config::Actions::SActionResult{};
    if (action==0) result=minimize(w);
    else if (action==2) result=Config::Actions::closeWindow(w);
    else if (resizable(w)) result=Config::Actions::fullscreenWindow(Fullscreen::FSMODE_MAXIMIZED,true,w);
    if (!result) Log::logger->log(Log::WARN,"bamboo02: header control failed: {}",result.error().message);
}


void render(const std::shared_ptr<WindowScene>& scene, bool background, float alpha) {
    if (!scene || !visible(*scene) || alpha <= 0.0F || !Render::GL::g_pHyprOpenGL) return;
    const auto monitor = g_pHyprRenderer->m_renderData.pMonitor.lock();
    if (!monitor || monitor->m_transform != WL_OUTPUT_TRANSFORM_NORMAL || monitor->m_scale <= 0) return;
    auto& s = *scene;
    const CBox client = clientPhysical(s,monitor);
    if (client.w < 1 || client.h < 1) return;
    s.resume();
    const double elapsed = s.seconds();
    if (background) {
        const Vector2D logical(client.w / monitor->m_scale, client.h / monitor->m_scale);
        if (!s.ambient.render(monitor,client,logical,reduced() ? 0.0 : elapsed,alpha,*s.runtime) && !s.ambientWarning) {
            s.ambientWarning = true;
            Log::logger->log(Log::WARN,"bamboo02: original atmosphere unavailable: {}",s.ambient.error());
        }
    } else {
        const double cw = client.w / monitor->m_scale, ch = client.h / monitor->m_scale;
        // Weather is a separate compositor postprocess module; frame and story
        // never sample or replace the native Kitty surface or texture pass.
        const auto source = clientSource(s.window.lock(),monitor,client.size());
        if ((!source || !s.weather.render(monitor,client,Vector2D(cw,ch),*source,reduced() ? 0.0 : elapsed,alpha)) && !s.weatherWarning) {
            s.weatherWarning = true;
            Log::logger->log(Log::WARN,"bamboo02: content weather unavailable: {}",source ? s.weather.error() : source.error());
        }
        const float rw = cw+104.0, rh = ch+104.0;
        CBox destination(client.x-52.0*monitor->m_scale,client.y-76.0*monitor->m_scale,
                         (rw+112.0)*monitor->m_scale,(rh+24.0)*monitor->m_scale);
        destination.round();
        const auto m = evaluate(elapsed,rw,rh,reduced());
        // Focus is native opacity-independent. No blur/focus wash touches text.
        const auto w = s.window.lock();
        const float focused = w && Desktop::focusState()->isWindowActive(w) ? 1.0F : 0.0F;
        bool ready = false;
        {
            GLState saved;
            ready = s.canvas.prepare(destination.w,destination.h);
            if (ready) {
                s.canvas.begin();
                bindTexture(0,s.runtime->frameAtlas);
                bindFrameUniforms(s.runtime->frame,m,rw,rh,monitor->m_scale,focused);
                s.runtime->frame.two("b_origin",(destination.x-client.x)/monitor->m_scale+52.0,(destination.y-client.y)/monitor->m_scale+52.0);
                s.runtime->frame.two("b_span",destination.w/monitor->m_scale,destination.h/monitor->m_scale);
                s.runtime->quad();
            }
        }
        if (!ready) {
            s.failed = true; s.pause();
            if (w) { g_pDecorationPositioner->forceRecalcFor(w); w->updateWindowDecos(); }
            Log::logger->log(Log::ERR,"bamboo02: bounded frame canvas allocation failed; releasing decoration reservation");
            return;
        }
        paintNative(s.canvas.texture(),destination,alpha);
        {
            GLState saved;
            s.canvas.begin();
            bindTexture(0,s.runtime->frameAtlas); bindTexture(1,s.runtime->sceneAtlas);
            bindSceneUniforms(s.runtime->scene,m,rw,rh,monitor->m_scale,focused);
            s.runtime->scene.two("b_origin",(destination.x-client.x)/monitor->m_scale+52.0,(destination.y-client.y)/monitor->m_scale+52.0);
            s.runtime->scene.two("b_span",destination.w/monitor->m_scale,destination.h/monitor->m_scale);
            s.runtime->quad();
        }
        paintNative(s.canvas.texture(),destination,alpha);
        drawControls(s,monitor,client,alpha);
    }
    s.rendered = Clock::now();
    armTimer();
}

class Pass final : public IPassElement {
  public:
    Pass(std::shared_ptr<WindowScene> s,bool bg,float a) : scene(std::move(s)), background(bg), alpha(a) {}
    std::vector<UP<IPassElement>> draw() override { render(scene,background,alpha); return {}; }
    bool needsLiveBlur() override { return false; }
    bool needsPrecomputeBlur() override { return false; }
    const char* passName() override { return background ? "bamboo02-background" : "bamboo02-frame-story"; }
    ePassElementType type() override { return EK_CUSTOM; }
    std::optional<CBox> boundingBox() override {
        if (!scene || !scene->alive || !g_pHyprRenderer) return std::nullopt;
        const auto monitor = g_pHyprRenderer->m_renderData.pMonitor.lock();
        if (!monitor) return std::nullopt;
        return monitorLogical(*scene,monitor);
    }
    CRegion opaqueRegion() override { return {}; }
  private:
    std::shared_ptr<WindowScene> scene;
    bool background;
    float alpha;
};

class Decoration final : public IHyprWindowDecoration {
  public:
    Decoration(PHLWINDOW w,std::shared_ptr<WindowScene> s,bool bg) : IHyprWindowDecoration(w),scene(std::move(s)),background(bg) {}
    ~Decoration() override {
        if (background) scene->under = nullptr;
        else { scene->over = nullptr; scene->alive = false; scene->pause(); }
    }
    SDecorationPositioningInfo getPositioningInfo() override {
        SDecorationPositioningInfo info;
        info.policy = DECORATION_POSITION_STICKY;
        info.edges = DECORATION_EDGE_TOP | DECORATION_EDGE_BOTTOM | DECORATION_EDGE_LEFT | DECORATION_EDGE_RIGHT;
        info.priority = 5000; info.reserved = !background;
        if (!background && wanted(*scene)) {
            info.desiredExtents.topLeft = Vector2D(kReservedInsets[0],kReservedInsets[1]);
            info.desiredExtents.bottomRight = Vector2D(kReservedInsets[2],kReservedInsets[3]);
        }
        return info;
    }
    void onPositioningReply(const SDecorationPositioningReply& reply) override {
        if (!background) { scene->assigned = reply.assignedGeometry; scene->assignedValid = reply.assignedGeometry.w>0 && reply.assignedGeometry.h>0; }
        damage(*scene);
    }
    void draw(PHLMONITOR,float const& alpha) override {
        if (!visible(*scene)) { scene->pause(); return; }
        if (alpha>0) g_pHyprRenderer->addPassElement(makeUnique<Pass>(scene,background,alpha));
    }
    bool onInputOnDeco(const eInputType type,const Vector2D& point,std::any data={}) override {
        if (background || !visible(*scene)) return false;
        const int hit=controlAt(*scene,point);
        if (type==INPUT_TYPE_MOTION) { hoverControls(point); return hit>=0; }
        if (type==INPUT_TYPE_DRAG_START && hit<0) {
            CBox client=clientGlobal(*scene);
            const auto window=scene->window.lock();
            if (!window) return false;
            client.translate(window->m_floatingOffset);
            if (point.y<client.y-48 || point.y>=client.y || point.x<client.x || point.x>=client.x+client.w) return false;
            g_layoutManager->dragController()->dragBegin(window->layoutTarget(),MBIND_MOVE);
            Desktop::focusState()->fullWindowFocus(window,Desktop::FOCUS_REASON_CLICK);
            return true;
        }
        if (type!=INPUT_TYPE_BUTTON) return false;
        const auto* event=std::any_cast<IPointer::SButtonEvent>(&data);
        if (!event || event->button!=272) return false;
        if (event->state==WL_POINTER_BUTTON_STATE_PRESSED) {
            scene->pressedControl=hit;
            return hit>=0;
        }
        const int pressed=std::exchange(scene->pressedControl,-1);
        if (hit<0 || pressed!=hit) return false;
        const auto window=scene->window.lock();
        activateControl(window,hit);
        return true;
    }
    eDecorationType getDecorationType() override { return DECORATION_CUSTOM; }
    void updateWindow(PHLWINDOW) override { damage(*scene); }
    void damageEntire() override { damage(*scene); }
    eDecorationLayer getDecorationLayer() override { return background ? DECORATION_LAYER_UNDER : DECORATION_LAYER_OVER; }
    uint64_t getDecorationFlags() override { return background ? 0 : DECORATION_ALLOWS_MOUSE_INPUT; } // No main-window shadow backplate.
    std::string getDisplayName() override { return background ? "Bamboo02 atmosphere" : "Bamboo02 frame and story"; }
  private:
    std::shared_ptr<WindowScene> scene;
    bool background;
};

void detach(PHLWINDOW w) {
    if (!g || !w) return;
    const auto it = g->windows.find(w.get());
    if (it == g->windows.end()) return;
    if (Render::GL::g_pHyprOpenGL) Render::GL::g_pHyprOpenGL->makeEGLCurrent();
    auto s = it->second;
    s->alive = false; s->pause(); damage(*s);
    if (s->under) HyprlandAPI::removeWindowDecoration(g->handle,s->under);
    if (s->over) HyprlandAPI::removeWindowDecoration(g->handle,s->over);
    g->windows.erase(it);
}
void attach(PHLWINDOW w) {
    if (!g || !g->runtime || !matches(w) || g->windows.contains(w.get())) return;
    auto s = std::make_shared<WindowScene>(w,g->runtime);
    Render::GL::g_pHyprOpenGL->makeEGLCurrent();
    {
        GLState saved;
        if (!s->weather.prepare()) {
            s->weatherWarning = true;
            Log::logger->log(Log::WARN,"bamboo02: content weather preparation failed: {}",s->weather.error());
        }
        if (!s->ambient.prepare()) {
            s->ambientWarning = true;
            Log::logger->log(Log::WARN,"bamboo02: atmosphere preparation failed: {}",s->ambient.error());
        }
    }
    auto over = makeUnique<Decoration>(w,s,false); s->over = over.get();
    if (!HyprlandAPI::addWindowDecoration(g->handle,w,std::move(over))) return;
    auto under = makeUnique<Decoration>(w,s,true); s->under = under.get();
    if (!HyprlandAPI::addWindowDecoration(g->handle,w,std::move(under))) {
        s->alive=false; HyprlandAPI::removeWindowDecoration(g->handle,s->over); return;
    }
    g->windows.emplace(w.get(),std::move(s));
}
void sync(PHLWINDOW w) { if (matches(w)) attach(w); else detach(w); }
void removeAll() {
    while (g && !g->windows.empty()) {
        const auto w = g->windows.begin()->second->window.lock();
        if (w) detach(w);
        else { g->windows.begin()->second->alive=false; g->windows.erase(g->windows.begin()); }
    }
}
SDispatchResult reload() {
    if (!g) return {.success=false,.error="bamboo02: plugin state unavailable"};
    if (!g->enabled->value()) {
        const auto restored = restoreMinimized("all", false);
        if (!restored.success) return restored;
        removeAll();
        g->runtime.reset();
        return {};
    }
    // A failed initial load leaves clients normal. A failed reconfigure retains
    // the last known-good runtime, target classes, canvases and reservation.
    const auto root = std::filesystem::path(g->root->value());
    auto loaded = loadRuntime(root);
    if (!loaded) {
        Log::logger->log(Log::ERR,"bamboo02: {}",loaded.error());
        return {.success=false,.error=loaded.error()};
    }
    removeAll(); g->runtime=std::move(*loaded);
    g->targetClasses=parseTargets(g->target->value());
    for (const auto& w : Desktop::windowState()->windows()) attach(w);
    return {};
}
int applyReloadFromLua(lua_State* state) {
    const auto result = reload();
    if (!result.success) return luaL_error(state,"%s",result.error.c_str());
    return 0;
}
int luaReload(lua_State* state) {
    if (lua_gettop(state) != 0) return luaL_error(state,"bamboo02.reload takes no arguments");
    lua_pushcfunction(state,applyReloadFromLua);
    return 1;
}
int applyRestoreFromLua(lua_State* state) {
    const auto result = restoreMinimized(lua_tostring(state,lua_upvalueindex(1)));
    if (!result.success) return luaL_error(state,"%s",result.error.c_str());
    return 0;
}
int luaRestore(lua_State* state) {
    lua_pushstring(state,luaL_optstring(state,1,""));
    lua_pushcclosure(state,applyRestoreFromLua,1);
    return 1;
}
void tick(SP<CEventLoopTimer> self,void*) {
    if (!g || reduced()) { self->updateTimeout(std::nullopt); return; }
    bool active=false;
    const auto now=Clock::now();
    for (const auto& [key,s] : g->windows) {
        // A draw lease is refreshed ONLY by an executed, non-occluded pass.
        // Discarded passes cannot keep damage alive behind an opaque window.
        if (!visible(*s) || s->rendered==Clock::time_point::min() || now-s->rendered>120ms) { s->pause(); continue; }
        damage(*s); active=true;
    }
    self->updateTimeout(active ? std::optional<Time::steady_dur>(34ms) : std::nullopt);
}
}
} // namespace bamboo02

APICALL EXPORT std::string PLUGIN_API_VERSION() { return HYPRLAND_API_VERSION; }
APICALL EXPORT PLUGIN_DESCRIPTION_INFO PLUGIN_INIT(HANDLE handle) {
    if (std::string_view(__hyprland_api_get_hash()) != __hyprland_api_get_client_hash())
        throw std::runtime_error("bamboo02: plugin headers do not match running Hyprland");
    using namespace bamboo02;
    g=std::make_unique<Global>(); g->handle=handle;
    g->root=makeShared<Config::Values::CStringValue>("plugin:bamboo02:root","Absolute immutable bamboo02 pack directory.","");
    g->target=makeShared<Config::Values::CStringValue>("plugin:bamboo02:target","Comma-separated exact window classes; no wildcard matching.","kitty");
    g->reduced=makeShared<Config::Values::CIntValue>("plugin:bamboo02:reduced_motion","Rest story, wind and atmosphere clocks.",0);
    g->enabled=makeShared<Config::Values::CIntValue>("plugin:bamboo02:enabled","Enable only while the selected wallpaper design owns this renderer.",0);
    HyprlandAPI::addConfigValueV2(handle,g->enabled);
    HyprlandAPI::addConfigValueV2(handle,g->root); HyprlandAPI::addConfigValueV2(handle,g->target); HyprlandAPI::addConfigValueV2(handle,g->reduced);
    g->listeners.emplace_back(Event::bus()->m_events.window.open.listen([](PHLWINDOW w){ sync(w); }));
    g->listeners.emplace_back(Event::bus()->m_events.window.close.listen([](PHLWINDOW w){ forgetMinimized(w); detach(w); }));
    g->listeners.emplace_back(Event::bus()->m_events.window.class_.listen([](PHLWINDOW w){ sync(w); }));
    g->listeners.emplace_back(Event::bus()->m_events.config.reloaded.listen([]{ reload(); }));
    g->listeners.emplace_back(Event::bus()->m_events.input.mouse.move.listen([](Vector2D point,Event::SCallbackInfo&){ hoverControls(point); }));
    HyprlandAPI::addDispatcherV2(handle,"bamboo02:reload",[](std::string argument) {
        if (!argument.empty()) return SDispatchResult{.success=false,.error="bamboo02:reload takes no arguments"};
        return reload();
    });
    HyprlandAPI::addDispatcherV2(handle,"bamboo02:restore",[](std::string argument){ return restoreMinimized(std::move(argument)); });
    if (!HyprlandAPI::addLuaFunction(handle,"bamboo02","reload",luaReload) ||
        !HyprlandAPI::addLuaFunction(handle,"bamboo02","restore",luaRestore))
        throw std::runtime_error("bamboo02: Lua dispatcher registration failed");
    g->listeners.emplace_back(Event::bus()->m_events.window.fullscreen.listen([](PHLWINDOW w){
        if (g && w) { g_pDecorationPositioner->forceRecalcFor(w); w->updateWindowDecos(); }
    }));
    g->timer=makeShared<CEventLoopTimer>(std::nullopt,tick,nullptr); g_pEventLoopManager->addTimer(g->timer);
    reload();
    return {"bamboo02","Native bamboo02 frame, atmosphere and120s story.","q",VERSION};
}
APICALL EXPORT void PLUGIN_EXIT() {
    using namespace bamboo02;
    if (!g) return;
    if (g->timer) { g->timer->cancel(); g_pEventLoopManager->removeTimer(g->timer); g->timer.reset(); }
    g->listeners.clear();
    const auto restored=restoreMinimized("all",false);
    if (!restored.success) Log::logger->log(Log::ERR,"bamboo02: unload restore failed: {}",restored.error);
    HyprlandAPI::removeDispatcher(g->handle,"bamboo02:reload");
    HyprlandAPI::removeDispatcher(g->handle,"bamboo02:restore");
    HyprlandAPI::removeLuaFunction(g->handle,"bamboo02","reload");
    HyprlandAPI::removeLuaFunction(g->handle,"bamboo02","restore");
    if (Render::GL::g_pHyprOpenGL) Render::GL::g_pHyprOpenGL->makeEGLCurrent();
    // Clear nested transformed-window pass containers while this DSO is still
    // mapped. Raw pointers are never held by our passes, and alive=false also
    // makes removed-window passes harmless before the end of a frame.
    if (g_pHyprRenderer) { g_pHyprRenderer->currentPass().clear(); g_pHyprRenderer->m_renderPass.clear(); }
    removeAll(); g->runtime.reset(); g.reset();
}
