#include "pass.hpp"

#include <string>

#include <hyprland/src/desktop/view/Window.hpp>
#include <hyprland/src/output/Monitor.hpp>
#include <hyprland/src/render/Renderer.hpp>

#include "decoration.hpp"

namespace kitty_skins::plugin {

CSkinPassElement::CSkinPassElement(CSkinDecoration* decoration, float alpha) : m_decoration(decoration), m_alpha(alpha) {
}

std::vector<UP<IPassElement>> CSkinPassElement::draw() {
    // The pass owns the element and drives it synchronously, so the borrowed
    // decoration is alive for the duration of this call. The monitor currently
    // being rendered is authoritative: a window can be drawn on a monitor other
    // than the one it nominally lives on during workspace animations.
    if (m_decoration && g_pHyprRenderer) {
        const PHLMONITOR monitor = g_pHyprRenderer->m_renderData.pMonitor.lock();
        if (monitor)
            m_decoration->renderPass(monitor, m_alpha);
    }

    // A custom element may hand back further elements to draw; this frame paints
    // itself directly and contributes none.
    return {};
}

bool CSkinPassElement::needsLiveBlur() {
    return false;
}

bool CSkinPassElement::needsPrecomputeBlur() {
    return false;
}

const char* CSkinPassElement::passName() {
    return kPassName;
}

ePassElementType CSkinPassElement::type() {
    return EK_CUSTOM;
}

std::optional<CBox> CSkinPassElement::boundingBox() {
    if (!m_decoration || !g_pHyprRenderer)
        return std::nullopt;

    // The monitor currently being rendered owns the box: a window can be drawn on
    // a monitor other than the one it nominally lives on during workspace
    // animations. Monitor-local logical, as CRenderPass::simplify expects.
    const PHLMONITOR monitor = g_pHyprRenderer->m_renderData.pMonitor.lock();
    if (!monitor || monitor->m_transform != WL_OUTPUT_TRANSFORM_NORMAL)
        return std::nullopt;

    const CBox box = m_decoration->monitorLogicalBoundingBox(monitor);
    if (box.w < 1.0 || box.h < 1.0)
        return std::nullopt;

    return box;
}

CRegion CSkinPassElement::opaqueRegion() {
    // The frame never occludes anything: the aperture must stay the client's.
    return {};
}

void clearAllPendingPassElements() {
    if (!g_pHyprRenderer)
        return;

    // A queued CSkinPassElement can sit either directly in the live render pass or
    // nested inside a CTransformedWindowPassElement::m_data.pass committed to the
    // global pass. Selective removal on the top-level passes cannot reach the
    // nested copy, so clear both outright while plugin code is still mapped: every
    // queued element, ours included, is destroyed here instead of after unload.
    // When the live pass aliases the global pass the second clear finds an empty
    // pass and is harmless.
    g_pHyprRenderer->currentPass().clear();
    g_pHyprRenderer->m_renderPass.clear();
}

}
