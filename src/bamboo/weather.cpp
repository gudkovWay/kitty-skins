#include "weather.hpp"
#include "resources.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <limits>
#include <utility>
#include <drm_fourcc.h>
#include <GLES2/gl2ext.h>
#include <string_view>
#include <hyprland/src/output/Monitor.hpp>
#include <hyprland/src/render/OpenGL.hpp>
#include <hyprland/src/render/Renderer.hpp>

namespace bamboo02 {
namespace {
using FB = SP<Render::IFramebuffer>;

enum Target : unsigned { RAW, SOURCE, LOW_SOURCE, HALF_SOURCE, REFERENCE, MASK,
                         MOTION_A, MOTION_B, LIT, LENS, TARGET_COUNT };
enum Stage : unsigned { BACKGROUND, OCCUPANCY, MOTION, LIGHT, OPTICS, TRANSFER, CLIENT, STAGE_COUNT };

bool rgba8Target(unsigned target) {
    return target == SOURCE || target == LOW_SOURCE || target == HALF_SOURCE ||
           target == MASK || target == LIT || target == LENS;
}

// GLState covers units0..2; the lens's diagnostic reference uses a fourth.
struct FourthTextureState {
    GLint active = 0, texture = 0, sampler = 0;
    FourthTextureState() {
        glGetIntegerv(GL_ACTIVE_TEXTURE, &active);
        glActiveTexture(GL_TEXTURE3);
        glGetIntegerv(GL_TEXTURE_BINDING_2D, &texture);
        glGetIntegerv(GL_SAMPLER_BINDING, &sampler);
        glActiveTexture(active);
    }
    ~FourthTextureState() {
        glActiveTexture(GL_TEXTURE3);
        glBindTexture(GL_TEXTURE_2D, texture);
        glBindSampler(3, sampler);
        glActiveTexture(active);
    }
};

// GLState saves unit1's 2D binding and sampler, not its external binding.
// Never query/bind an external target on contexts without the extension.
struct ExternalTextureState {
    bool supported;
    GLint active = 0, binding = 0;
    explicit ExternalTextureState(bool enabled) : supported(enabled) {
        if (!supported) return;
        glGetIntegerv(GL_ACTIVE_TEXTURE, &active);
        glActiveTexture(GL_TEXTURE1);
        glGetIntegerv(GL_TEXTURE_BINDING_EXTERNAL_OES, &binding);
        glActiveTexture(active);
    }
    ~ExternalTextureState() {
        if (!supported) return;
        glActiveTexture(GL_TEXTURE1);
        glBindTexture(GL_TEXTURE_EXTERNAL_OES, binding);
        glActiveTexture(active);
    }
};

NColorManagement::PImageDescription workingDescription() {
    static const auto description = [] {
        NColorManagement::SImageDescription value;
        value.transferFunction = NColorManagement::CM_TRANSFER_FUNCTION_SRGB;
        value.primariesNameSet = true;
        value.primariesNamed = NColorManagement::CM_PRIMARIES_SRGB;
        return NColorManagement::CImageDescription::from(value);
    }();
    return description;
}

bool supportedDescription(const NColorManagement::PImageDescription& description) {
    if (!description) return false;
    const auto& value = description->value();
    const auto tf = value.transferFunction;
    return value.getPrimaries() == NColorManagement::NColorPrimaries::BT709 &&
        value.rawICC.empty() && !value.icc.present &&
        (tf == NColorManagement::CM_TRANSFER_FUNCTION_LINEAR ||
         tf == NColorManagement::CM_TRANSFER_FUNCTION_GAMMA22 ||
         tf == NColorManagement::CM_TRANSFER_FUNCTION_GAMMA28 ||
         tf == NColorManagement::CM_TRANSFER_FUNCTION_EXT_LINEAR ||
         tf == NColorManagement::CM_TRANSFER_FUNCTION_SRGB);
}

bool supportedUV(const std::array<float, 9>& uv) {
    if (!std::ranges::all_of(uv, [](float x) { return std::isfinite(x); }) ||
        uv[2] != 0 || uv[5] != 0 || uv[8] != 1 ||
        uv[0] * uv[4] - uv[1] * uv[3] == 0) return false;
    return true;
}

struct FramebufferFootprint {
    int x, y;
    bool flipX, flipY;
};

// Native UV(0,0) is the client top-left. Private weather textures instead
// store their top-left at GL top, so a positive target Y span needs a flip.
constexpr FramebufferFootprint framebufferFootprint(int originX, int originY, int spanX, int spanY) {
    return {std::min(originX,originX+spanX), std::min(originY,originY+spanY), spanX < 0, spanY > 0};
}

// Behavioral coordinate contracts, compiled with the native target. These
// cover both framebuffer Y conventions and reflected X without a GPU runner.
constexpr auto kNativeDown = framebufferFootprint(120,80,800,600);
static_assert(kNativeDown.x == 120 && kNativeDown.y == 80 && !kNativeDown.flipX && kNativeDown.flipY);
constexpr auto kNativeUp = framebufferFootprint(120,680,800,-600);
static_assert(kNativeUp.x == 120 && kNativeUp.y == 80 && !kNativeUp.flipX && !kNativeUp.flipY);
constexpr auto kReflectedX = framebufferFootprint(920,80,-800,600);
static_assert(kReflectedX.x == 120 && kReflectedX.y == 80 && kReflectedX.flipX && kReflectedX.flipY);

GLuint framebufferID(const FB& fb) {
    return static_cast<Render::GL::CGLFramebuffer*>(fb.get())->getFBID();
}
void texture(unsigned unit, const FB& fb) {
    glActiveTexture(GL_TEXTURE0 + unit);
    glBindSampler(unit, 0);
    glBindTexture(GL_TEXTURE_2D, fb->getTexture()->m_texID);
}
void target(const FB& fb) {
    glBindFramebuffer(GL_DRAW_FRAMEBUFFER, framebufferID(fb));
    glViewport(0, 0, static_cast<int>(fb->m_size.x), static_cast<int>(fb->m_size.y));
}
void clear(const FB& fb) {
    target(fb);
    glClearColor(0, 0, 0, 0);
    glClear(GL_COLOR_BUFFER_BIT);
}
void resample(const FB& from, const FB& to) {
    glBindFramebuffer(GL_READ_FRAMEBUFFER, framebufferID(from));
    glBindFramebuffer(GL_DRAW_FRAMEBUFFER, framebufferID(to));
    glBlitFramebuffer(0, 0, static_cast<int>(from->m_size.x), static_cast<int>(from->m_size.y),
                      0, 0, static_cast<int>(to->m_size.x), static_cast<int>(to->m_size.y),
                      GL_COLOR_BUFFER_BIT, GL_LINEAR);
}
}

struct Weather::Impl {
    explicit Impl(std::filesystem::path root) : root(std::move(root)) {}
    ~Impl() {
        if (clientSampler) glDeleteSamplers(1, &clientSampler);
        if (vao) glDeleteVertexArrays(1, &vao);
    }

    std::filesystem::path root;
    std::array<Program, STAGE_COUNT> programs;
    std::array<FB, TARGET_COUNT> buffers;
    std::string failure;
    GLuint vao = 0, clientSampler = 0;
    int width = 0, height = 0, currentMotion = 0;
    GLint maxTexture = 0;
    Vector2D logicalSize;
    bool loaded = false, failed = false, externalSampler = false;
    bool cmReady = false;
    uint64_t sourceDescriptionID = 0, endpointDescriptionID = 0;
    Render::SCMSettings sourceCM, targetCM;

    bool load() {
        if (failed) return false;
        if (loaded) return true;
        constexpr std::array names = {"rain-content-background.frag", "rain-content-mask.frag",
            "rain-motion.frag", "rain-light.frag", "rain-lens.frag", "weather-transfer.frag", "weather-client.frag"};
        for (unsigned i = 0; i < STAGE_COUNT; ++i) {
            if (!programs[i].load(root / "shaders" / names[i], failure)) {
                failed = true;
                return false;
            }
        }
        GLint extensions = 0;
        glGetIntegerv(GL_NUM_EXTENSIONS, &extensions);
        for (GLint i = 0; i < extensions; ++i) {
            const auto* name = glGetStringi(GL_EXTENSIONS, i);
            if (name && std::string_view(reinterpret_cast<const char*>(name)) == "GL_OES_EGL_image_external_essl3")
                externalSampler = true;
        }
        glGenSamplers(1, &clientSampler);
        if (clientSampler) {
            glSamplerParameteri(clientSampler, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
            glSamplerParameteri(clientSampler, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
        }
        glGenVertexArrays(1, &vao);
        glGetIntegerv(GL_MAX_TEXTURE_SIZE, &maxTexture);
        loaded = vao != 0 && clientSampler != 0;
        if (!loaded) { failure = "weather: cannot allocate vertex array/client sampler"; failed = true; }
        return loaded;
    }

    void releaseBuffers() {
        for (auto& buffer : buffers) {
            if (buffer) buffer->release();
            buffer.reset();
        }
        width = height = 0;
        currentMotion = 0;
        logicalSize = {};
    }

    bool allocate(int w, int h, const Vector2D& logical) {
        if (w == width && h == height && logical == logicalSize) return true;
        const int smallW = std::max(1, static_cast<int>(std::round(w * .125)));
        const int smallH = std::max(1, static_cast<int>(std::round(h * .125)));
        const int halfW = std::max(1, static_cast<int>(std::round(w * .5)));
        const int halfH = std::max(1, static_cast<int>(std::round(h * .5)));
        // Calculate the entire target set before creating the first texture.
        // Checked arithmetic also prevents an oversized request wrapping into
        // a deceptively small allocation/admission value.
        std::uint64_t totalBytes = 0;
        for (unsigned i = 0; i < TARGET_COUNT; ++i) {
            const bool small = i == LOW_SOURCE || i == REFERENCE || i == MOTION_A || i == MOTION_B;
            const bool half = i == HALF_SOURCE || i == MASK;
            const std::uint64_t tw = small ? smallW : half ? halfW : w;
            const std::uint64_t th = small ? smallH : half ? halfH : h;
            const std::uint64_t bpp = rgba8Target(i) ? 4 : 8;
            if (!tw || !th || tw > static_cast<std::uint64_t>(maxTexture) || th > static_cast<std::uint64_t>(maxTexture) ||
                tw > std::numeric_limits<std::uint64_t>::max() / th / bpp ||
                totalBytes > std::numeric_limits<std::uint64_t>::max() - tw * th * bpp) {
                failure = "weather: target-set dimensions or byte accounting overflow";
                releaseBuffers();
                failed = true;
                return false;
            }
            totalBytes += tw * th * bpp;
        }
        // Explicit per-window source budget, not a prediction of free VRAM.
        constexpr std::uint64_t MAX_TARGET_BYTES = 512ULL * 1024 * 1024;
        if (totalBytes > MAX_TARGET_BYTES) {
            failure = "weather: target set exceeds explicit 512 MiB per-window limit";
            releaseBuffers();
            return false;
        }
        // Avoid transiently retaining an old-size set while materializing the
        // replacement. Allocation failure must leave no partial GPU cache.
        releaseBuffers();
        for (unsigned i = 0; i < TARGET_COUNT; ++i) {
            const bool small = i == LOW_SOURCE || i == REFERENCE || i == MOTION_A || i == MOTION_B;
            const bool half = i == HALF_SOURCE || i == MASK;
            if (!buffers[i]) buffers[i] = g_pHyprRenderer->createFB("bamboo02-weather");
            const auto format = rgba8Target(i) ? DRM_FORMAT_ABGR8888 : DRM_FORMAT_ABGR16161616F;
            if (!buffers[i] || !buffers[i]->alloc(small ? smallW : half ? halfW : w,
                                                 small ? smallH : half ? halfH : h, format)) {
                failure = "weather: framebuffer allocation failed (RGBA16F required, no normalized fallback)";
                failed = true;
                releaseBuffers();
                return false;
            }
            if (i == SOURCE || i == LOW_SOURCE || i == HALF_SOURCE || i == LIT || i == LENS) {
                buffers[i]->setImageDescription(workingDescription());
                buffers[i]->getTexture()->m_imageDescription = workingDescription();
            }
            texture(0, buffers[i]);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
            clear(buffers[i]);
        }
        width = w; height = h; logicalSize = logical;
        currentMotion = 0;
        return true;
    }

    void draw(Stage stage, Target output, Target input, float phase) {
        const auto& p = programs[stage];
        target(buffers[output]);
        p.use();
        p.integer("tex", 0);
        p.two("weather_size", buffers[output]->m_size.x, buffers[output]->m_size.y);
        p.two("logical_size", logicalSize.x, logicalSize.y);
        p.one("border_px", 52);
        p.one("phase", phase);
        p.one("composition", 0);
        p.one("diagnostic_mode", 0);
        texture(0, buffers[input]);
        if (stage == OCCUPANCY) {
            p.integer("content_background", 1);
            texture(1, buffers[REFERENCE]);
        } else if (stage == MOTION) {
            p.integer("content_mask", 1);
            texture(1, buffers[MASK]);
        } else if (stage == OPTICS) {
            p.integer("drop_motion", 1); p.integer("content_mask", 2); p.integer("content_background", 3);
            texture(1, buffers[currentMotion ? MOTION_B : MOTION_A]);
            texture(2, buffers[MASK]); texture(3, buffers[REFERENCE]);
        }
        glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    }
};

Weather::Weather(std::filesystem::path root) : impl(std::make_unique<Impl>(std::move(root))) {}
Weather::~Weather() = default;
const std::string& Weather::error() const { return impl->failure; }

bool Weather::prepare() {
    GLState state;
    FourthTextureState fourth;
    return impl->load();
}

bool Weather::render(PHLMONITOR monitor, const CBox& client, const Vector2D& logicalClient,
                     const ClientSource& source, double seconds, float nativeFade) {
    if (!impl->loaded || impl->failed) return false;
    if (!monitor || !g_pHyprRenderer || !g_pHyprRenderer->m_renderData.currentFB ||
        !std::isfinite(seconds) || !std::isfinite(client.x) || !std::isfinite(client.y) ||
        !std::isfinite(client.w) || !std::isfinite(client.h) || !std::isfinite(logicalClient.x) || !std::isfinite(logicalClient.y) ||
        logicalClient.x <= 0 || logicalClient.y <= 0 || !std::isfinite(nativeFade) ||
        !std::isfinite(monitor->m_scale) || monitor->m_scale <= 0 ||
        client.w < 1 || client.h < 1 || nativeFade <= 0) return false;
    // Capture addresses the actual framebuffer; unproven transform mappings must
    // never sample another window/monitor's pixels under a guessed rectangle.
    if (monitor->m_transform != WL_OUTPUT_TRANSFORM_NORMAL ||
        !g_pHyprRenderer->m_renderData.renderModif.modifs.empty()) {
        impl->failure = "weather: transformed monitor/window capture is not qualified";
        return false;
    }
    if (!source.texture || !source.texture->ok() || !supportedUV(source.uvTransform) ||
        !std::isfinite(source.opacity) || source.opacity < 0 || source.opacity > 1 ||
        !std::isfinite(source.rounding) || source.rounding < 0 ||
        !std::isfinite(source.roundingPower) || source.roundingPower < 1 ||
        !std::isfinite(source.colorMultiplier) || source.colorMultiplier < 0 || source.colorMultiplier > 1) {
        impl->failure = "weather: invalid isolated client texture, affine UV, opacity, rounding or dim";
        return false;
    }
    const bool external = source.texture->m_type == Render::TEXTURE_EXTERNAL;
    if (source.texture->m_type != Render::TEXTURE_RGBA && source.texture->m_type != Render::TEXTURE_RGBX && !external) {
        impl->failure = "weather: unsupported committed client texture type (RGBA/RGBX/external required)";
        return false;
    }
    if (external && !impl->externalSampler) {
        impl->failure = "weather: external client texture requires GL_OES_EGL_image_external_essl3";
        return false;
    }
    if (!supportedDescription(source.description)) {
        impl->failure = "weather: client transfer/primaries/ICC require a qualified color adapter";
        return false;
    }
    const auto framebuffer = g_pHyprRenderer->m_renderData.currentFB;
    auto description = framebuffer->imageDescription();
    if (!description) description = g_pHyprRenderer->workBufferImageDescription();
    if (!description) {
        impl->failure = "weather: current framebuffer color description is unavailable";
        return false;
    }
    if (!supportedDescription(description)) {
        impl->failure = "weather: framebuffer transfer/primaries require a qualified color adapter";
        return false;
    }
    // Native ext.frag deliberately skips CM: its endpoints must return to the
    // client encoding actually blended into RAW, not an invented native target
    // conversion. Ordinary 2D surfaces really do convert to the target.
    const auto endpointDescription = external ? source.description : description;
    // Use the same native CM ranges as the surface renderer. In particular
    // internal LINEAR is absolute nits, whereas EXT_LINEAR has range 0..80.
    if (!impl->cmReady || impl->sourceDescriptionID != source.description->id() ||
        impl->endpointDescriptionID != endpointDescription->id()) {
        impl->sourceCM = g_pHyprRenderer->getCMSettings(source.description, workingDescription());
        impl->targetCM = g_pHyprRenderer->getCMSettings(workingDescription(), endpointDescription);
        impl->sourceDescriptionID = source.description->id();
        impl->endpointDescriptionID = endpointDescription->id();
        impl->cmReady = true;
    }
    const auto& sourceCM = impl->sourceCM;
    const auto& targetCM = impl->targetCM;
    if (sourceCM.needsTonemap || sourceCM.needsSDRmod || targetCM.needsTonemap || targetCM.needsSDRmod) {
        impl->failure = "weather: native tonemap/SDR modulation requires a qualified delta color adapter";
        return false;
    }
    const auto validRange = [](const Render::STFRange& range) {
        return std::isfinite(range.min) && std::isfinite(range.max) && range.max > range.min;
    };
    if (!validRange(sourceCM.srcTFRange) || !validRange(targetCM.srcTFRange) || !validRange(targetCM.dstTFRange)) {
        impl->failure = "weather: native color-management luminance range is invalid";
        return false;
    }

    GLState state;
    FourthTextureState fourth;
    ExternalTextureState externalState(impl->externalSampler);
    glDisable(GL_BLEND); glDisable(GL_SCISSOR_TEST); glDisable(GL_STENCIL_TEST);
    glDisable(GL_DEPTH_TEST); glDisable(GL_CULL_FACE);
    glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
    glBindVertexArray(impl->vao);

    const double roundedCW = std::round(client.w), roundedCH = std::round(client.h);
    const double roundedBX = std::max(1.0, std::round(52.0 * roundedCW / logicalClient.x));
    const double roundedBY = std::max(1.0, std::round(52.0 * roundedCH / logicalClient.y));
    if (roundedCW + 2 * roundedBX > impl->maxTexture || roundedCH + 2 * roundedBY > impl->maxTexture ||
        std::abs(client.x) > std::numeric_limits<int>::max() - impl->maxTexture ||
        std::abs(client.y) > std::numeric_limits<int>::max() - impl->maxTexture) {
        impl->failure = "weather: logical root exceeds GPU framebuffer size or coordinate range";
        impl->releaseBuffers();
        return false;
    }
    const int cw = static_cast<int>(roundedCW), ch = static_cast<int>(roundedCH);
    const int bx = static_cast<int>(roundedBX), by = static_cast<int>(roundedBY);
    const double fw = framebuffer->m_size.x, fh = framebuffer->m_size.y;
    if (!std::isfinite(fw) || !std::isfinite(fh) || fw < 1 || fh < 1 || fw > impl->maxTexture || fh > impl->maxTexture) {
        impl->failure = "weather: invalid native framebuffer dimensions";
        return false;
    }
    // Use the same actual target projection as native client rendering.
    // client.y is not a bottom-left framebuffer offset and must not be
    // inverted by a guessed height-y conversion.
    const auto projection = g_pHyprRenderer->projectBoxToTarget(client).copy().transpose().getMatrix();
    const auto finite = [](float value) { return std::isfinite(value); };
    if (!std::ranges::all_of(projection,finite)) {
        impl->failure = "weather: nonfinite native client projection";
        return false;
    }
    const double spanX = projection[0] * 0.5 * fw, spanY = projection[4] * 0.5 * fh;
    const double originX = (projection[6] * 0.5 + 0.5) * fw, originY = (projection[7] * 0.5 + 0.5) * fh;
    constexpr double epsilon = 0.05;
    if (std::abs(projection[1]) > 0.00001 || std::abs(projection[3]) > 0.00001 ||
        std::abs(projection[2]) > 0.00001 || std::abs(projection[5]) > 0.00001 || std::abs(projection[8]-1) > 0.00001 ||
        std::abs(std::abs(spanX)-client.w) > epsilon || std::abs(std::abs(spanY)-client.h) > epsilon ||
        std::abs(originX-std::round(originX)) > epsilon || std::abs(originY-std::round(originY)) > epsilon ||
        std::abs(originX) > std::numeric_limits<int>::max()-impl->maxTexture ||
        std::abs(originY) > std::numeric_limits<int>::max()-impl->maxTexture) {
        impl->failure = "weather: nonpixel-aligned/scaled/rotated native projection is unqualified";
        return false;
    }
    const auto footprint = framebufferFootprint(static_cast<int>(std::round(originX)),static_cast<int>(std::round(originY)),
                                               static_cast<int>(std::round(spanX)),static_cast<int>(std::round(spanY)));
    if (!impl->allocate(cw + 2 * bx, ch + 2 * by, logicalClient + Vector2D(104, 104))) return false;
    impl->buffers[RAW]->setImageDescription(description);
    impl->buffers[RAW]->getTexture()->m_imageDescription = description;
    // Timestamp discontinuities are handled inside the imported simulation:
    // rebase dt=0 and retain living heads/frozen split records.
    const float phase = static_cast<float>(std::fmod(std::max(0.0, seconds), 96.0) / 96.0);

    // RAW is only the unmodified current composite for the final delta.
    // It never feeds SOURCE, background-reference or content occupancy.
    clear(impl->buffers[RAW]);
    const int x = footprint.x, y = footprint.y;
    const int fbH = static_cast<int>(fh);
    const int left = std::max(x, 0), bottom = std::max(y, 0);
    const int right = std::min(x + cw, static_cast<int>(framebuffer->m_size.x));
    const int top = std::min(y + ch, fbH);
    if (left >= right || bottom >= top) return false;
    glBindFramebuffer(GL_READ_FRAMEBUFFER, framebufferID(framebuffer));
    glBindFramebuffer(GL_DRAW_FRAMEBUFFER, framebufferID(impl->buffers[RAW]));
    glBlitFramebuffer(left, bottom, right, top, bx + left - x, by + bottom - y,
                      bx + right - x, by + top - y, GL_COLOR_BUFFER_BIT, GL_NEAREST);

    // Borrow committed Kitty pixels directly; never mutate the native texture,
    // native pass, renderData or current composite. SOURCE8 retains genuine
    // client alpha without compositor opacity/fade or the rounded shape mask.
    const auto& capture = impl->programs[CLIENT];
    target(impl->buffers[SOURCE]);
    if (source.texture->m_texID == impl->buffers[SOURCE]->getTexture()->m_texID) {
        impl->failure = "weather: isolated client input aliases the source draw target";
        return false;
    }
    capture.use();
    capture.integer("client_rgba", 0);
    capture.integer("client_external", 1);
    capture.integer("external_sampler", external ? 1 : 0);
    capture.integer("opaque_source", source.opaque || source.texture->m_type == Render::TEXTURE_RGBX ? 1 : 0);
    capture.integer("transfer_function", sourceCM.sourceTF);
    capture.two("transfer_range", sourceCM.srcTFRange.min, sourceCM.srcTFRange.max);
    capture.two("working_range", targetCM.srcTFRange.min, targetCM.srcTFRange.max);
    capture.two("root_size", impl->width, impl->height);
    capture.two("logical_client", logicalClient.x, logicalClient.y);
    capture.four("client_rect", bx, by, cw, ch);
    glUniformMatrix3fv(capture.uniform("client_uv"), 1, GL_FALSE, source.uvTransform.data());
    const auto unit = external ? 1U : 0U;
    glActiveTexture(GL_TEXTURE0 + unit);
    glBindTexture(external ? GL_TEXTURE_EXTERNAL_OES : GL_TEXTURE_2D, source.texture->m_texID);
    // Owned sampler overrides filtering/wrap without changing borrowed texture
    // parameters; retain the native nearest-neighbour mode when requested.
    const bool nearest = source.nearest;
    glSamplerParameteri(impl->clientSampler, GL_TEXTURE_MIN_FILTER, nearest ? GL_NEAREST : source.texture->minFilter);
    glSamplerParameteri(impl->clientSampler, GL_TEXTURE_MAG_FILTER, nearest ? GL_NEAREST : source.texture->magFilter);
    glBindSampler(unit, impl->clientSampler);
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    resample(impl->buffers[SOURCE], impl->buffers[LOW_SOURCE]);
    resample(impl->buffers[SOURCE], impl->buffers[HALF_SOURCE]);
    impl->draw(BACKGROUND, REFERENCE, LOW_SOURCE, phase);
    impl->draw(OCCUPANCY, MASK, HALF_SOURCE, phase);
    const auto previous = impl->currentMotion ? MOTION_B : MOTION_A;
    const auto next = impl->currentMotion ? MOTION_A : MOTION_B;
    impl->draw(MOTION, next, previous, phase);
    impl->currentMotion = 1 - impl->currentMotion;
    impl->draw(LIGHT, LIT, SOURCE, phase);
    impl->draw(OPTICS, LENS, LIT, phase);

    // Add only the finite isolated-client contribution change. Native Kitty has
    // already rendered: another source-over would blend both client/backdrop twice.
    glBindFramebuffer(GL_DRAW_FRAMEBUFFER, framebufferID(framebuffer));
    glViewport(x - bx, y - by, impl->width, impl->height);
    glEnable(GL_SCISSOR_TEST); glScissor(left, bottom, right - left, top - bottom);
    const auto& transfer = impl->programs[TRANSFER];
    transfer.use(); transfer.integer("tex", 0);
    transfer.integer("source_srgb", 1); transfer.integer("processed_srgb", 2);
    transfer.integer("transfer_function", targetCM.targetTF);
    transfer.two("transfer_range", targetCM.dstTFRange.min, targetCM.dstTFRange.max);
    transfer.two("working_range", targetCM.srcTFRange.min, targetCM.srcTFRange.max);
    transfer.one("effect_alpha", std::clamp(nativeFade, 0.f, 1.f) * source.opacity);
    transfer.one("color_multiplier", source.colorMultiplier);
    transfer.two("root_size", impl->width, impl->height);
    transfer.four("client_rect", bx, by, cw, ch);
    transfer.two("source_flip",footprint.flipX ? 1.0F : 0.0F,footprint.flipY ? 1.0F : 0.0F);
    // Native Renderer truncates the scaled radius, then ElementRenderer
    // subtracts one physical pixel before passing its integer texture radius.
    transfer.one("rounding", std::max(0.0, std::floor(source.rounding * monitor->m_scale) - 1.0));
    transfer.one("rounding_power", source.roundingPower);
    glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_FALSE); // preserve target alpha bytes
    texture(0, impl->buffers[RAW]); texture(1, impl->buffers[SOURCE]); texture(2, impl->buffers[LENS]);
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    impl->failure.clear();
    return true;
}

} // namespace bamboo02
