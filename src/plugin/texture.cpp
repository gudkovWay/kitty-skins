#include "texture.hpp"

#include <optional>
#include <utility>

#include <GLES3/gl32.h>
#include <cairo/cairo.h>
#include <drm_fourcc.h>

#include <hyprland/src/debug/log/Logger.hpp>
#include <hyprland/src/render/OpenGL.hpp>
#include <hyprland/src/render/Renderer.hpp>

#include "kitty_skins/manifest.hpp"

namespace kitty_skins::plugin {

namespace {

using Hyprutils::Math::Vector2D;

// Atlases are uploaded from Cairo's premultiplied ARGB32, whose little-endian
// byte order is BGRA. Swizzling R/B makes the sampler read true premultiplied
// RGBA without a CPU-side conversion.
constexpr uint32_t kAssetDrmFormat = DRM_FORMAT_ARGB8888;

cairo_surface_t* decodePngSurface(const std::filesystem::path& file, std::string& error, Vector2D& sourceSize) {
    cairo_surface_t* surface = cairo_image_surface_create_from_png(file.c_str());
    if (surface == nullptr) {
        error = file.string() + ": cannot open PNG asset";
        return nullptr;
    }

    if (cairo_surface_status(surface) != CAIRO_STATUS_SUCCESS) {
        error = file.string() + ": cannot decode PNG asset";
        cairo_surface_destroy(surface);
        return nullptr;
    }

    const cairo_format_t format = cairo_image_surface_get_format(surface);
    if (format != CAIRO_FORMAT_ARGB32) {
        cairo_surface_t* converted = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, cairo_image_surface_get_width(surface),
                                                                cairo_image_surface_get_height(surface));
        if (converted == nullptr || cairo_surface_status(converted) != CAIRO_STATUS_SUCCESS) {
            error = file.string() + ": cannot convert PNG asset to ARGB32";
            cairo_surface_destroy(converted);
            cairo_surface_destroy(surface);
            return nullptr;
        }

        cairo_t* cairo = cairo_create(converted);
        cairo_set_operator(cairo, CAIRO_OPERATOR_SOURCE);
        cairo_set_source_surface(cairo, surface, 0, 0);
        cairo_paint(cairo);
        cairo_destroy(cairo);
        cairo_surface_destroy(surface);
        surface = converted;
    }

    sourceSize = Vector2D(cairo_image_surface_get_width(surface), cairo_image_surface_get_height(surface));
    if (sourceSize.x < 1.0 || sourceSize.y < 1.0) {
        error = file.string() + ": PNG asset has an empty size";
        cairo_surface_destroy(surface);
        return nullptr;
    }

    return surface;
}

void applyFilter(const SP<Render::ITexture>& texture, kitty_skins::Filter filter) {
    const GLint mode = filter == kitty_skins::Filter::nearest ? GL_NEAREST : GL_LINEAR;
    texture->magFilter = mode;
    texture->minFilter = mode;

    texture->bind();
    texture->setTexParameter(GL_TEXTURE_MAG_FILTER, mode);
    texture->setTexParameter(GL_TEXTURE_MIN_FILTER, mode);
    texture->setTexParameter(GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    texture->setTexParameter(GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    texture->setTexParameter(GL_TEXTURE_SWIZZLE_R, GL_BLUE);
    texture->setTexParameter(GL_TEXTURE_SWIZZLE_B, GL_RED);
    texture->unbind();
}

SP<Render::ITexture> uploadSurface(cairo_surface_t* surface, const std::filesystem::path& file, std::string& error) {
    SP<Render::ITexture> texture = g_pHyprRenderer->createTexture(surface);
    if (!texture || !texture->ok()) {
        error = file.string() + ": GPU texture allocation failed";
        return nullptr;
    }

    texture->m_drmFormat = kAssetDrmFormat;
    return texture;
}

}

std::expected<std::shared_ptr<SkinRuntime>, std::string> loadRuntime(const std::filesystem::path& packRoot) {
    auto loaded = kitty_skins::loadAndValidatePack(packRoot);
    if (!loaded) {
        const kitty_skins::ValidationError& error = loaded.error();
        return std::unexpected(error.path.string() + ": " + error.field + ": " + error.message);
    }
    if (!Render::GL::g_pHyprOpenGL)
        return std::unexpected("skin textures require the OpenGL renderer");
    Render::GL::g_pHyprOpenGL->makeEGLCurrent();

    auto runtime                     = std::make_shared<SkinRuntime>();
    runtime->pack                    = std::move(*loaded);
    const kitty_skins::Filter filter = runtime->pack.filter;

    // Decode and upload each image once. Adaptive draw operations sample their
    // declared source rectangles directly from the shared atlas, while effect
    // rasters always sample linearly regardless of the pack's atlas filter.
    const auto loadImage = [&](const std::filesystem::path& file, SP<Render::ITexture>& texture,
                               kitty_skins::Filter imageFilter) -> std::optional<std::string> {
        Vector2D         size;
        std::string      error;
        cairo_surface_t* surface = decodePngSurface(file, error, size);
        if (surface == nullptr)
            return error;

        texture = uploadSurface(surface, file, error);
        cairo_surface_destroy(surface);
        if (!texture)
            return error;

        applyFilter(texture, imageFilter);
        return std::nullopt;
    };

    if (const auto error = loadImage(runtime->pack.exactAtlas, runtime->exactAtlas, filter))
        return std::unexpected(*error);
    if (const auto error = loadImage(runtime->pack.adaptiveAtlas, runtime->adaptiveAtlas, filter))
        return std::unexpected(*error);

    if (runtime->pack.candleEffect) {
        const kitty_skins::CandleEffectSpec& effect = *runtime->pack.candleEffect;

        SP<Render::ITexture> flames;
        SP<Render::ITexture> light;
        if (const auto error = loadImage(effect.flames, flames, kitty_skins::Filter::linear))
            return std::unexpected(*error);
        if (const auto error = loadImage(effect.light, light, kitty_skins::Filter::linear))
            return std::unexpected(*error);

        // The wax visibility mask is decoded only when the pack declares streams;
        // a flame-only pack keeps its previous cost. Like the effect rasters it is
        // an exact-region RGBA image and samples linearly.
        SP<Render::ITexture> waxMask;
        if (effect.wax) {
            if (const auto error = loadImage(effect.wax->mask, waxMask, kitty_skins::Filter::linear))
                return std::unexpected(*error);
        }

        auto        renderer = std::make_unique<CandleRenderer>();
        std::string initError;
        if (!renderer->initialize(effect, flames, light, waxMask, initError))
            return std::unexpected(initError.empty() ? std::string("candle effect initialization failed") : initError);

        runtime->candles = std::move(renderer);
    }

    // Accent overlays are preloaded once at pack load: localized RGBA rasters
    // that sample linearly regardless of the pack's atlas filter, exactly like
    // the other effect rasters. A pack without accents allocates nothing here.
    runtime->accentTextures.reserve(runtime->pack.accentEffects.size());
    for (const kitty_skins::AccentEffectSpec& accent : runtime->pack.accentEffects) {
        SP<Render::ITexture> texture;
        if (const auto error = loadImage(accent.texture, texture, kitty_skins::Filter::linear))
            return std::unexpected(*error);
        runtime->accentTextures.push_back(std::move(texture));
    }

    // Layered flower effects are part of the pack contract: each declared effect
    // initializes its full region resources (background, foreground, petals and
    // the region-sized output framebuffer) up front. A declared effect that
    // cannot start fails the whole load transactionally — there is no transparent
    // placeholder and no silently dropped effect. A pack without flowers
    // allocates nothing here.
    runtime->flowers.reserve(runtime->pack.flowerEffects.size());
    for (const kitty_skins::FlowerEffectSpec& flower : runtime->pack.flowerEffects) {
        auto        renderer = std::make_unique<FlowerRenderer>();
        std::string initError;
        if (!renderer->initialize(flower, initError))
            return std::unexpected(initError.empty() ? std::string("flower effect initialization failed") : initError);
        runtime->flowers.push_back(std::move(renderer));
    }

    return runtime;
}

}
