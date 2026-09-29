#include "texture.hpp"

#include <cmath>
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

constexpr uint32_t kAssetDrmFormat = DRM_FORMAT_ARGB8888;

// Cairo decodes the PNG; the surface is normalised to premultiplied ARGB32 so the
// GPU upload below and the GL swizzle path stay identical for every asset.
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
        cairo_surface_t* converted =
            cairo_image_surface_create(CAIRO_FORMAT_ARGB32, cairo_image_surface_get_width(surface), cairo_image_surface_get_height(surface));
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

// Filtering is applied exactly once per texture; the GL samplers read these members.
void applyFilter(const SP<Render::ITexture>& texture, kitty_skins::Filter filter) {
    const GLint mode = filter == kitty_skins::Filter::nearest ? GL_NEAREST : GL_LINEAR;
    texture->magFilter = mode;
    texture->minFilter = mode;

    texture->bind();
    texture->setTexParameter(GL_TEXTURE_MAG_FILTER, mode);
    texture->setTexParameter(GL_TEXTURE_MIN_FILTER, mode);
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

// Copy one source-pixel region of a decoded atlas into its own GPU texture.
SP<Render::ITexture> sliceRegion(const SP<Render::ITexture>& source, int x, int y, int width, int height, kitty_skins::Filter filter) {
    if (width < 1 || height < 1)
        return nullptr;

    SP<Render::ITexture> slice = g_pHyprRenderer->createTexture(false);
    if (!slice)
        return nullptr;

    slice->allocate(Vector2D(width, height), kAssetDrmFormat);
    slice->bind();
    slice->setTexParameter(GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    slice->setTexParameter(GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    slice->setTexParameter(GL_TEXTURE_SWIZZLE_R, GL_BLUE);
    slice->setTexParameter(GL_TEXTURE_SWIZZLE_B, GL_RED);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, width, height, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);

    if (slice->m_texID == 0 || source->m_texID == 0) {
        slice->unbind();
        return nullptr;
    }

    glCopyImageSubData(source->m_texID, GL_TEXTURE_2D, 0, x, y, 0, slice->m_texID, GL_TEXTURE_2D, 0, 0, 0, 0, width, height, 1);
    slice->unbind();

    slice->magFilter = filter == kitty_skins::Filter::nearest ? GL_NEAREST : GL_LINEAR;
    slice->minFilter = slice->magFilter;
    applyFilter(slice, filter);
    return slice;
}

struct SliceRect {
    int x = 0, y = 0, width = 0, height = 0;
};

SliceRect makeSlice(int x, int y, int width, int height) {
    SliceRect rect;
    rect.x      = x;
    rect.y      = y;
    rect.width  = width > 0 ? width : 0;
    rect.height = height > 0 ? height : 0;
    return rect;
}

}

std::expected<std::shared_ptr<SkinRuntime>, std::string> loadRuntime(const std::filesystem::path& packRoot) {
    auto loaded = kitty_skins::loadAndValidatePack(packRoot);
    if (!loaded) {
        const kitty_skins::ValidationError& error = loaded.error();
        return std::unexpected(error.path.string() + ": " + error.field + ": " + error.message);
    }

    auto runtime      = std::make_shared<SkinRuntime>();
    runtime->pack     = std::move(*loaded);
    const kitty_skins::Filter filter = runtime->pack.filter;

    // Base frame atlas: decode, upload, slice into eight pieces, then drop the atlas.
    std::string  error;
    Vector2D     frameSize;
    cairo_surface_t* frameSurface = decodePngSurface(runtime->pack.frame.asset, error, frameSize);
    if (frameSurface == nullptr)
        return std::unexpected(error);

    const int frameWidth  = static_cast<int>(frameSize.x);
    const int frameHeight = static_cast<int>(frameSize.y);

    SP<Render::ITexture> frameTexture = uploadSurface(frameSurface, runtime->pack.frame.asset, error);
    cairo_surface_destroy(frameSurface);
    if (!frameTexture)
        return std::unexpected(error);
    applyFilter(frameTexture, filter);

    const kitty_skins::Insets& insets = runtime->pack.frame.slices;
    const int left   = static_cast<int>(std::lround(insets.left));
    const int right  = static_cast<int>(std::lround(insets.right));
    const int top    = static_cast<int>(std::lround(insets.top));
    const int bottom = static_cast<int>(std::lround(insets.bottom));

    if (left < 0 || right < 0 || top < 0 || bottom < 0 || left + right > frameWidth || top + bottom > frameHeight)
        return std::unexpected(runtime->pack.frame.asset.string() + ": frame slices do not fit inside the frame asset");

    const std::array<SliceRect, 8> rects{
        makeSlice(0, 0, left, top),                                        // top-left
        makeSlice(frameWidth - right, 0, right, top),                      // top-right
        makeSlice(frameWidth - right, frameHeight - bottom, right, bottom), // bottom-right
        makeSlice(0, frameHeight - bottom, left, bottom),                  // bottom-left
        makeSlice(left, 0, frameWidth - left - right, top),                // top edge
        makeSlice(frameWidth - right, top, right, frameHeight - top - bottom), // right edge
        makeSlice(left, frameHeight - bottom, frameWidth - left - right, bottom), // bottom edge
        makeSlice(0, top, left, frameHeight - top - bottom),               // left edge
    };

    for (size_t index = 0; index < rects.size(); ++index) {
        const SliceRect& rect = rects[index];
        TextureAsset     asset;
        asset.sourceSize = Vector2D(rect.width, rect.height);

        if (rect.width > 0 && rect.height > 0) {
            asset.texture = sliceRegion(frameTexture, rect.x, rect.y, rect.width, rect.height, filter);
            if (!asset.texture)
                return std::unexpected(runtime->pack.frame.asset.string() + ": failed to slice frame region " + std::to_string(index));
        }

        runtime->frameSlices[index] = std::move(asset);
    }

    frameTexture.reset();

    // Ornaments are independent anchored sprites and are shared by every window.
    for (const kitty_skins::SpriteSpec& sprite : runtime->pack.sprites) {
        Vector2D         spriteSize;
        cairo_surface_t* spriteSurface = decodePngSurface(sprite.asset, error, spriteSize);
        if (spriteSurface == nullptr)
            return std::unexpected(error);

        SP<Render::ITexture> spriteTexture = uploadSurface(spriteSurface, sprite.asset, error);
        cairo_surface_destroy(spriteSurface);
        if (!spriteTexture)
            return std::unexpected(error);
        applyFilter(spriteTexture, filter);

        runtime->sprites.emplace(sprite.id, TextureAsset{std::move(spriteTexture), spriteSize});
    }

    return runtime;
}

}
