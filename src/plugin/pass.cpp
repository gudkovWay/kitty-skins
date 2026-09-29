#include "pass.hpp"

#include <hyprland/src/render/Renderer.hpp>

#include "decoration.hpp"

namespace kitty_skins::plugin {

CSkinPassElement::CSkinPassElement(const SData& data) : m_data(data) {
}

std::vector<UP<IPassElement>> CSkinPassElement::draw() {
    // Called from CRenderPass::render with the renderer state fully prepared for this
    // element: the decoration only walks its cached operations.
    if (m_data.decoration != nullptr) {
        const PHLMONITOR monitor = g_pHyprRenderer->m_renderData.pMonitor.lock();
        if (monitor)
            m_data.decoration->drawPass(monitor, m_data.alpha);
    }

    return {};
}

bool CSkinPassElement::needsLiveBlur() {
    return false;
}

bool CSkinPassElement::needsPrecomputeBlur() {
    return false;
}

const char* CSkinPassElement::passName() {
    return kSkinPassName;
}

ePassElementType CSkinPassElement::type() {
    return EK_CUSTOM;
}

std::optional<CBox> CSkinPassElement::boundingBox() {
    if (m_data.decoration == nullptr)
        return std::nullopt;

    const PHLMONITOR monitor = g_pHyprRenderer->m_renderData.pMonitor.lock();
    if (!monitor)
        return std::nullopt;

    // Monitor-local logical coordinates, as CRenderPass::simplify expects.
    return m_data.decoration->globalBoundingBox().translate(-monitor->m_position);
}

}
