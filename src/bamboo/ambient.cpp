#include "ambient.hpp"
#include "resources.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <utility>
#include <drm_fourcc.h>
#include <hyprland/src/output/Monitor.hpp>
#include <hyprland/src/render/OpenGL.hpp>
#include <hyprland/src/render/Renderer.hpp>
#include <hyprland/src/render/gl/GLFramebuffer.hpp>

namespace bamboo02 {
namespace {
using FB = SP<Render::IFramebuffer>;
enum Buffer : unsigned { INPUT, FULL, HALF, QUARTER, OUTPUT, BUFFER_COUNT };
enum Shader : unsigned { CAPTURE, DOWN, UP, SHADER_COUNT };

GLuint framebufferID(const FB& fb) {
    return static_cast<Render::GL::CGLFramebuffer*>(fb.get())->getFBID();
}
void target(const FB& fb) {
    glBindFramebuffer(GL_DRAW_FRAMEBUFFER, framebufferID(fb));
    glViewport(0, 0, static_cast<int>(fb->m_size.x), static_cast<int>(fb->m_size.y));
}
bool supportedTF(NColorManagement::eTransferFunction tf) {
    using namespace NColorManagement;
    return tf == CM_TRANSFER_FUNCTION_LINEAR || tf == CM_TRANSFER_FUNCTION_EXT_LINEAR ||
        tf == CM_TRANSFER_FUNCTION_GAMMA22 || tf == CM_TRANSFER_FUNCTION_SRGB || tf == CM_TRANSFER_FUNCTION_EXT_SRGB;
}
} // namespace

struct Ambient::Impl {
    explicit Impl(std::filesystem::path root) : root(std::move(root)) {}
    ~Impl() {
        releaseBuffers();
        if (captureSampler) glDeleteSamplers(1, &captureSampler);
    }

    std::filesystem::path root;
    std::array<Program, SHADER_COUNT> programs;
    std::array<FB, BUFFER_COUNT> buffers;
    std::string failure;
    GLuint captureSampler = 0;
    GLint maxTexture = 0;
    int width = 0, height = 0, clientWidth = 0, clientHeight = 0;
    bool prepared = false, failed = false, cmReady = false;
    uint64_t sourceID = 0, targetID = 0;
    Render::SCMSettings cm;
    std::array<float, 9> primaries{};

    void releaseBuffers() {
        for (auto& buffer : buffers) {
            if (buffer) buffer->release();
            buffer.reset();
        }
        width = height = clientWidth = clientHeight = 0;
    }
    bool load() {
        if (failed) return false;
        if (prepared) return true;
        constexpr std::array names = {"ambient-blur-capture.frag", "ambient-blur-down.frag", "ambient-blur-up.frag"};
        for (unsigned i = 0; i < SHADER_COUNT; ++i) {
            if (!programs[i].load(root / "shaders" / names[i], failure)) { failed = true; return false; }
        }
        // The capture sampler never changes the compositor texture's filtering
        // or wrapping. Copy source texels before CM, not an interpolated edge.
        glGenSamplers(1, &captureSampler);
        if (!captureSampler) { failure = "ambient: capture sampler allocation failed"; failed = true; return false; }
        glSamplerParameteri(captureSampler, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
        glSamplerParameteri(captureSampler, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
        glSamplerParameteri(captureSampler, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
        glSamplerParameteri(captureSampler, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
        glGetIntegerv(GL_MAX_TEXTURE_SIZE, &maxTexture);
        prepared = maxTexture > 0;
        if (!prepared) { failure = "ambient: texture limit unavailable"; failed = true; }
        return prepared;
    }
    bool allocate(int w, int h, int cw, int ch, NColorManagement::PImageDescription description) {
        if (w == width && h == height && cw == clientWidth && ch == clientHeight) return true;
        // Shoji uses integer division, not round-to-nearest, at EACH level.
        const int hw = std::max(1, w / 2), hh = std::max(1, h / 2);
        const int qw = std::max(1, hw / 2), qh = std::max(1, hh / 2);
        const std::array<Vector2D, BUFFER_COUNT> sizes = {Vector2D(w,h), Vector2D(w,h), Vector2D(hw,hh), Vector2D(qw,qh), Vector2D(cw,ch)};
        constexpr uint64_t MAX_BYTES = 256ULL * 1024 * 1024;
        uint64_t bytes = 0;
        for (const auto& size : sizes) {
            if (size.x < 1 || size.y < 1 || size.x > maxTexture || size.y > maxTexture) {
                failure = "ambient: target dimensions exceed GPU limit"; releaseBuffers(); return false;
            }
            const uint64_t pixels = uint64_t(size.x) * uint64_t(size.y);
            if (pixels > MAX_BYTES / 4 || bytes > MAX_BYTES - pixels * 4) {
                failure = "ambient: target set exceeds 256 MiB per-window bound"; releaseBuffers(); return false;
            }
            bytes += pixels * 4;
        }
        // Whole-set bound includes input, three reusable pyramid levels and
        // final client crop; discard the old size before allocating a new set.
        // Checked incrementally above, without a wrapping whole-set sum.
        releaseBuffers();
        for (unsigned i = 0; i < BUFFER_COUNT; ++i) {
            buffers[i] = g_pHyprRenderer->createFB("bamboo02-ambient");
            if (!buffers[i] || !buffers[i]->alloc(static_cast<int>(sizes[i].x), static_cast<int>(sizes[i].y), DRM_FORMAT_ABGR8888)) {
                failure = "ambient: RGBA8 framebuffer allocation failed"; failed = true; releaseBuffers(); return false;
            }
            buffers[i]->setImageDescription(description);
            buffers[i]->getTexture()->m_imageDescription = description;
            bindTexture(0, buffers[i]->getTexture());
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
        }
        width = w; height = h; clientWidth = cw; clientHeight = ch;
        return true;
    }
    void blur(Shader shader, Buffer source, Buffer destination, const Runtime& runtime) const {
        const auto& p = programs[shader];
        target(buffers[destination]);
        p.use(); p.integer("tex", 0);
        // Unlike the down pass (destination), the up pass divides by SOURCE.
        const auto size = buffers[shader == DOWN ? destination : source]->m_size;
        p.two("half_pixel", 0.5F / size.x, 0.5F / size.y);
        bindTexture(0, buffers[source]->getTexture());
        runtime.quad();
    }
};

Ambient::Ambient(std::filesystem::path root) : impl(std::make_unique<Impl>(std::move(root))) {}
Ambient::~Ambient() = default;
const std::string& Ambient::error() const { return impl->failure; }
bool Ambient::prepare() {
    GLState saved;
    return impl->load();
}

bool Ambient::render(PHLMONITOR monitor, const CBox& client, Vector2D logical,
                     double seconds, float alpha, const Runtime& runtime) {
    if (!impl->prepared || impl->failed) return false; // No file I/O or compilation in render.
    if (!monitor || !g_pHyprRenderer || !Render::GL::g_pHyprOpenGL || !runtime.lantern ||
        !g_pHyprRenderer->m_renderData.currentFB || !std::isfinite(seconds) || !std::isfinite(alpha) || alpha <= 0 ||
        !std::isfinite(client.x) || !std::isfinite(client.y) || !std::isfinite(client.w) || !std::isfinite(client.h) ||
        !std::isfinite(logical.x) || !std::isfinite(logical.y) || logical.x <= 0 || logical.y <= 0 ||
        !std::isfinite(monitor->m_scale) || monitor->m_scale <= 0 || client.w < 1 || client.h < 1) {
        impl->failure = "ambient: invalid render input"; return false;
    }
    if (monitor->m_transform != WL_OUTPUT_TRANSFORM_NORMAL) {
        impl->failure = "ambient: transformed output capture is unqualified"; return false;
    }
    const auto framebuffer = g_pHyprRenderer->m_renderData.currentFB;
    const auto source = framebuffer->getTexture();
    auto description = framebuffer->imageDescription();
    if (!description) description = g_pHyprRenderer->workBufferImageDescription();
    const auto targetDescription = runtime.lantern->m_imageDescription;
    if (!source || !source->ok() || (source->m_type != Render::TEXTURE_RGBA && source->m_type != Render::TEXTURE_RGBX) || !description || !targetDescription ||
        source->m_transform != HYPRUTILS_TRANSFORM_NORMAL ||
        !supportedTF(description->value().transferFunction) || description->value().icc.present || !description->value().rawICC.empty() ||
        targetDescription->value().transferFunction != NColorManagement::CM_TRANSFER_FUNCTION_SRGB ||
        targetDescription->value().icc.present || !targetDescription->value().rawICC.empty()) {
        impl->failure = "ambient: source texture/ICC/transfer requires a qualified CM adapter"; return false;
    }
    const double fw = framebuffer->m_size.x, fh = framebuffer->m_size.y;
    if (!std::isfinite(fw) || !std::isfinite(fh) || fw < 1 || fh < 1 || fw > impl->maxTexture || fh > impl->maxTexture ||
        client.w > impl->maxTexture || client.h > impl->maxTexture) {
        impl->failure = "ambient: source or client dimensions exceed GPU limit"; return false;
    }
    // This is the actual compositor projection, NOT client.x/y interpreted as
    // source-FB offsets. Qualify a 1:1 axis-aligned mapping before cropping;
    // affine translation/reflection is retained, arbitrary resampling is not.
    const auto projection = g_pHyprRenderer->projectBoxToTarget(client).copy().transpose().getMatrix();
    for (float value : projection) if (!std::isfinite(value)) {
        impl->failure = "ambient: nonfinite source projection"; return false;
    }
    const double spanX = projection[0] * 0.5 * fw, spanY = projection[4] * 0.5 * fh;
    const double originX = (projection[6] * 0.5 + 0.5) * fw, originY = (projection[7] * 0.5 + 0.5) * fh;
    constexpr double epsilon = 0.05;
    if (std::abs(projection[1]) > 0.00001 || std::abs(projection[3]) > 0.00001 ||
        std::abs(projection[2]) > 0.00001 || std::abs(projection[5]) > 0.00001 || std::abs(projection[8] - 1) > 0.00001 ||
        std::abs(std::abs(spanX) - client.w) > epsilon || std::abs(std::abs(spanY) - client.h) > epsilon ||
        std::abs(originX - std::round(originX)) > epsilon || std::abs(originY - std::round(originY)) > epsilon ||
        std::abs(client.w - std::round(client.w)) > epsilon || std::abs(client.h - std::round(client.h)) > epsilon) {
        impl->failure = "ambient: nonpixel-aligned/scaled/rotated source projection is unqualified"; return false;
    }
    // Source host: ceil(capturePadding * max(render_scale,1)), radius is NOT
    // multiplied by scale. Clamp capture itself at output edges before building
    // the pyramid, exactly as Shoji's actual_capture_rect intersection does.
    const double padding = std::ceil(24.0 * std::max(1.0, double(monitor->m_scale)));
    const double x0 = std::round(originX), y0 = std::round(originY);
    const int cw = static_cast<int>(std::round(client.w)), ch = static_cast<int>(std::round(client.h));
    const double dx = spanX > 0 ? 1.0 : -1.0, dy = spanY > 0 ? 1.0 : -1.0;
    const double x1 = x0 + dx * cw, y1 = y0 + dy * ch;
    const double left = std::clamp(std::min(x0,x1) - padding, 0.0, fw);
    const double right = std::clamp(std::max(x0,x1) + padding, 0.0, fw);
    const double bottom = std::clamp(std::min(y0,y1) - padding, 0.0, fh);
    const double top = std::clamp(std::max(y0,y1) + padding, 0.0, fh);
    if (left >= right || bottom >= top) { impl->failure = "ambient: padded source lies outside framebuffer"; return false; }
    const int w = static_cast<int>(right-left), h = static_cast<int>(top-bottom);
    const double sx = dx > 0 ? left : right, sy = dy > 0 ? bottom : top;
    {
        GLState saved;
        glDisable(GL_BLEND); glDisable(GL_SCISSOR_TEST); glDisable(GL_STENCIL_TEST);
        glDisable(GL_DEPTH_TEST); glDisable(GL_CULL_FACE);
        glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
        if (!impl->allocate(w,h,cw,ch,targetDescription)) return false;
        if (!impl->cmReady || impl->sourceID != description->id() || impl->targetID != targetDescription->id()) {
            impl->cm = g_pHyprRenderer->getCMSettings(description,targetDescription);
            const auto& cm = impl->cm;
            if (cm.needsTonemap || cm.needsSDRmod || !std::isfinite(cm.srcTFRange.min) || !std::isfinite(cm.srcTFRange.max) ||
                !std::isfinite(cm.dstTFRange.min) || !std::isfinite(cm.dstTFRange.max) ||
                cm.srcTFRange.max <= cm.srcTFRange.min || cm.dstTFRange.max <= cm.dstTFRange.min) {
                impl->failure = "ambient: tone mapping/SDR modification or invalid CM range is unqualified"; return false;
            }
            for (int row=0; row<3; ++row) for (int col=0; col<3; ++col) {
                const double value = cm.convertMatrix[row][col];
                if (!std::isfinite(value)) { impl->failure = "ambient: nonfinite primaries conversion"; return false; }
                impl->primaries[col*3+row] = value;
            }
            impl->sourceID = description->id(); impl->targetID = targetDescription->id();
            impl->cmReady = true;
        }
        const auto& cm = impl->cm;
        const auto& capture = impl->programs[CAPTURE];
        target(impl->buffers[INPUT]);
        capture.use(); capture.integer("tex",0); capture.integer("source_tf",cm.sourceTF);
        capture.integer("source_rgbx",source->m_type == Render::TEXTURE_RGBX ? 1 : 0);
        capture.four("source_uv",sx/fw,sy/fh,dx*w/fw,dy*h/fh);
        capture.two("source_range",cm.srcTFRange.min,cm.srcTFRange.max);
        capture.two("target_range",cm.dstTFRange.min,cm.dstTFRange.max);
        glUniformMatrix3fv(capture.uniform("convert_primaries"),1,GL_FALSE,impl->primaries.data());
        bindTexture(0,source); glBindSampler(0,impl->captureSampler);
        runtime.quad();
        glBindSampler(0,0);
        // Exactly four passes; no Hyprland configured blur and no second blur.
        impl->blur(DOWN,INPUT,HALF,runtime);
        impl->blur(DOWN,HALF,QUARTER,runtime);
        impl->blur(UP,QUARTER,HALF,runtime);
        impl->blur(UP,HALF,FULL,runtime);

        target(impl->buffers[OUTPUT]);
        const auto& p = runtime.ambient;
        p.use(); p.integer("b_backdrop",0); p.integer("content_lantern",1);
        p.four("b_backdrop_uv",dx*(x0-sx)/w,dy*(y0-sy)/h,double(cw)/w,double(ch)/h);
        p.two("logical_content_size",logical.x,logical.y); p.one("b_scale",monitor->m_scale);
        p.one("phase",std::fmod(std::max(0.0,seconds),96.0)/96.0); p.one("composition",0);
        bindTexture(0,impl->buffers[FULL]->getTexture()); bindTexture(1,runtime.lantern);
        runtime.quad();
    }
    // Native CM/blend happens only AFTER restoring the exact compositor state.
    Render::GL::CHyprOpenGLImpl::STextureRenderData data;
    data.a = alpha; data.allowDim = false;
    Render::GL::g_pHyprOpenGL->renderTexture(impl->buffers[OUTPUT]->getTexture(),client,data);
    impl->failure.clear();
    return true;
}
} // namespace bamboo02
