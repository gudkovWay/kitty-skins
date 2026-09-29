#include "kitty_skins/manifest.hpp"

#include <png.h>
#include <setjmp.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <optional>
#include <regex>
#include <string>
#include <unordered_set>
#include <utility>
#include <vector>

#include <glaze/glaze.hpp>

namespace kitty_skins {
namespace {

struct JsonInsets {
    double left{};
    double right{};
    double top{};
    double bottom{};
};

struct JsonEdges {
    std::string left;
    std::string right;
    std::string top;
    std::string bottom;
};

struct JsonFrame {
    std::string asset;
    JsonInsets slices;
    JsonEdges edges;
};

struct JsonTier {
    std::string id;
    double min_width{};
    double min_height{};
    double scale{};
    JsonInsets extents;
    std::vector<std::string> visible_sprites;
};

struct JsonSprite {
    std::string id;
    std::string asset;
    std::string anchor;
    double offset_x{};
    double offset_y{};
    double scale{};
    int z_index{};
};

struct JsonManifest {
    int schema{};
    std::string id;
    std::string name;
    std::string filter;
    JsonFrame frame;
    std::vector<JsonTier> tiers;
    std::vector<JsonSprite> sprites;
};

ValidationError fail(std::filesystem::path path, std::string field, std::string message) {
    return ValidationError{std::move(path), std::move(field), std::move(message)};
}

bool finite(double value) noexcept {
    return std::isfinite(value);
}

bool isWithin(const std::filesystem::path& root, const std::filesystem::path& target) {
    auto rootIt = root.begin();
    auto targetIt = target.begin();
    for (; rootIt != root.end(); ++rootIt, ++targetIt) {
        if (targetIt == target.end() || *rootIt != *targetIt)
            return false;
    }
    return true;
}

bool decodeFilter(const std::string& value, Filter& out) {
    if (value == "nearest") {
        out = Filter::nearest;
        return true;
    }
    if (value == "linear") {
        out = Filter::linear;
        return true;
    }
    return false;
}

bool decodeEdge(const std::string& value, EdgeMode& out) {
    if (value == "tile") {
        out = EdgeMode::tile;
        return true;
    }
    if (value == "stretch") {
        out = EdgeMode::stretch;
        return true;
    }
    if (value == "mirror") {
        out = EdgeMode::mirror;
        return true;
    }
    return false;
}

bool decodeAnchor(const std::string& value, Anchor& out) {
    if (value == "top-left") {
        out = Anchor::top_left;
        return true;
    }
    if (value == "top-center") {
        out = Anchor::top_center;
        return true;
    }
    if (value == "top-right") {
        out = Anchor::top_right;
        return true;
    }
    if (value == "bottom-left") {
        out = Anchor::bottom_left;
        return true;
    }
    if (value == "bottom-center") {
        out = Anchor::bottom_center;
        return true;
    }
    if (value == "bottom-right") {
        out = Anchor::bottom_right;
        return true;
    }
    return false;
}

enum class PngProbe { ok, notPng, readError };

PngProbe probePngRgba(const std::filesystem::path& file, bool& rgba) {
    rgba = false;

    std::FILE* handle = std::fopen(file.c_str(), "rb");
    if (handle == nullptr)
        return PngProbe::readError;

    png_structp png = png_create_read_struct(PNG_LIBPNG_VER_STRING, nullptr, nullptr, nullptr);
    if (png == nullptr) {
        std::fclose(handle);
        return PngProbe::readError;
    }

    png_infop info = png_create_info_struct(png);
    if (info == nullptr) {
        png_destroy_read_struct(&png, nullptr, nullptr);
        std::fclose(handle);
        return PngProbe::readError;
    }

    if (setjmp(png_jmpbuf(png)) != 0) {
        png_destroy_read_struct(&png, &info, nullptr);
        std::fclose(handle);
        return PngProbe::readError;
    }

    png_init_io(png, handle);

    unsigned char signature[8] = {};
    const size_t read = std::fread(signature, 1, sizeof(signature), handle);
    if (read != sizeof(signature) || png_sig_cmp(signature, 0, sizeof(signature)) != 0) {
        png_destroy_read_struct(&png, &info, nullptr);
        std::fclose(handle);
        return PngProbe::notPng;
    }

    png_set_sig_bytes(png, static_cast<int>(sizeof(signature)));
    png_read_info(png, info);
    rgba = png_get_color_type(png, info) == PNG_COLOR_TYPE_RGBA;

    png_destroy_read_struct(&png, &info, nullptr);
    std::fclose(handle);
    return PngProbe::ok;
}

}

Result<SkinPack> loadAndValidatePack(const std::filesystem::path& root) {
    const std::filesystem::path skinJson = root / "skin.json";

    std::error_code ec;
    const std::filesystem::path canonicalRoot = std::filesystem::weakly_canonical(root, ec);
    if (ec)
        return std::unexpected(fail(root, "root", "cannot resolve pack root: " + ec.message()));

    std::ifstream stream(skinJson, std::ios::binary);
    if (!stream)
        return std::unexpected(fail(skinJson, "skin.json", "cannot open manifest for reading"));

    std::string buffer((std::istreambuf_iterator<char>(stream)), std::istreambuf_iterator<char>());
    if (stream.bad())
        return std::unexpected(fail(skinJson, "skin.json", "cannot read manifest"));

    JsonManifest manifest{};
    const glz::error_ctx parse = glz::read_json(manifest, buffer);
    std::string parseMessage;
    if (parse)
        parseMessage = glz::format_error(parse, buffer);
    buffer.clear();
    buffer.shrink_to_fit();
    if (parse)
        return std::unexpected(fail(skinJson, "skin.json", "invalid manifest: " + parseMessage));

    if (manifest.schema != 1)
        return std::unexpected(
            fail(skinJson, "schema", "unsupported schema " + std::to_string(manifest.schema) + "; expected 1"));

    static const std::regex idPattern(R"(^[a-z0-9]+(?:-[a-z0-9]+)*$)");
    if (!std::regex_match(manifest.id, idPattern))
        return std::unexpected(fail(skinJson, "id", "must match ^[a-z0-9]+(?:-[a-z0-9]+)*$"));

    SkinPack pack{};
    pack.schema = manifest.schema;
    pack.id = manifest.id;

    if (manifest.name.empty())
        return std::unexpected(fail(skinJson, "name", "must not be empty"));
    pack.name = manifest.name;

    if (!decodeFilter(manifest.filter, pack.filter))
        return std::unexpected(fail(skinJson, "filter", "must be nearest or linear"));

    if (manifest.frame.asset.empty())
        return std::unexpected(fail(skinJson, "frame.asset", "must not be empty"));

    const std::array<const std::string*, 4> edgeValues{&manifest.frame.edges.left, &manifest.frame.edges.right,
                                                       &manifest.frame.edges.top, &manifest.frame.edges.bottom};
    const std::array<const char*, 4> edgeFields{"frame.edges.left", "frame.edges.right", "frame.edges.top",
                                                "frame.edges.bottom"};
    for (size_t index = 0; index < edgeValues.size(); ++index) {
        if (!decodeEdge(*edgeValues[index], pack.frame.edges[index]))
            return std::unexpected(fail(skinJson, edgeFields[index], "must be tile, stretch or mirror"));
    }

    if (manifest.tiers.empty())
        return std::unexpected(fail(skinJson, "tiers", "must declare at least one tier"));

    for (size_t index = 0; index < manifest.tiers.size(); ++index) {
        if (manifest.tiers[index].id.empty())
            return std::unexpected(
                fail(skinJson, "tiers[" + std::to_string(index) + "].id", "must not be empty"));
    }

    std::vector<Anchor> spriteAnchors(manifest.sprites.size());
    for (size_t index = 0; index < manifest.sprites.size(); ++index) {
        const std::string prefix = "sprites[" + std::to_string(index) + "]";
        if (manifest.sprites[index].id.empty())
            return std::unexpected(fail(skinJson, prefix + ".id", "must not be empty"));
        if (manifest.sprites[index].asset.empty())
            return std::unexpected(fail(skinJson, prefix + ".asset", "must not be empty"));
        if (!decodeAnchor(manifest.sprites[index].anchor, spriteAnchors[index]))
            return std::unexpected(fail(skinJson, prefix + ".anchor",
                                        "must be top-left, top-center, top-right, bottom-left, bottom-center or "
                                        "bottom-right"));
    }

    const size_t lastTier = manifest.tiers.size() - 1;
    const std::string lastTierPrefix = "tiers[" + std::to_string(lastTier) + "]";
    if (manifest.tiers[lastTier].id != "border-only")
        return std::unexpected(fail(skinJson, lastTierPrefix + ".id", "final tier must be border-only"));
    if (manifest.tiers[lastTier].min_width != 0.0 || manifest.tiers[lastTier].min_height != 0.0)
        return std::unexpected(
            fail(skinJson, lastTierPrefix + ".min_width", "final border-only tier must have zero minimum size"));

    for (const double slice : {manifest.frame.slices.left, manifest.frame.slices.right, manifest.frame.slices.top,
                               manifest.frame.slices.bottom}) {
        if (!finite(slice) || slice < 0.0)
            return std::unexpected(fail(skinJson, "frame.slices", "slices must be finite and non-negative"));
    }
    pack.frame.slices = Insets{manifest.frame.slices.left, manifest.frame.slices.right, manifest.frame.slices.top,
                               manifest.frame.slices.bottom};

    pack.tiers.reserve(manifest.tiers.size());
    for (size_t index = 0; index < manifest.tiers.size(); ++index) {
        const JsonTier& tier = manifest.tiers[index];
        const std::string prefix = "tiers[" + std::to_string(index) + "]";

        if (!finite(tier.scale) || tier.scale <= 0.0)
            return std::unexpected(fail(skinJson, prefix + ".scale", "scale must be finite and greater than zero"));
        if (!finite(tier.min_width) || tier.min_width < 0.0)
            return std::unexpected(
                fail(skinJson, prefix + ".min_width", "minimum width must be finite and non-negative"));
        if (!finite(tier.min_height) || tier.min_height < 0.0)
            return std::unexpected(
                fail(skinJson, prefix + ".min_height", "minimum height must be finite and non-negative"));

        const std::array<std::pair<const char*, double>, 4> extents{{{"left", tier.extents.left},
                                                                    {"right", tier.extents.right},
                                                                    {"top", tier.extents.top},
                                                                    {"bottom", tier.extents.bottom}}};
        for (const auto& [name, value] : extents) {
            if (!finite(value) || value < 0.0)
                return std::unexpected(
                    fail(skinJson, prefix + ".extents." + name, "must be finite and non-negative"));
        }

        pack.tiers.push_back(TierSpec{tier.id, LogicalSize{tier.min_width, tier.min_height}, tier.scale,
                                     Insets{tier.extents.left, tier.extents.right, tier.extents.top,
                                            tier.extents.bottom},
                                     tier.visible_sprites});
    }

    for (size_t index = 0; index < manifest.sprites.size(); ++index) {
        const JsonSprite& sprite = manifest.sprites[index];
        const std::string prefix = "sprites[" + std::to_string(index) + "]";
        if (!finite(sprite.scale) || sprite.scale <= 0.0)
            return std::unexpected(fail(skinJson, prefix + ".scale", "scale must be finite and greater than zero"));
        if (!finite(sprite.offset_x) || !finite(sprite.offset_y))
            return std::unexpected(fail(skinJson, prefix + ".offset_x", "offsets must be finite"));
    }

    std::unordered_set<std::string> spriteIds;
    for (size_t index = 0; index < manifest.sprites.size(); ++index) {
        if (!spriteIds.insert(manifest.sprites[index].id).second)
            return std::unexpected(
                fail(skinJson, "sprites[" + std::to_string(index) + "].id", "duplicate sprite id"));
    }

    for (size_t index = 0; index < manifest.tiers.size(); ++index) {
        for (const std::string& reference : manifest.tiers[index].visible_sprites) {
            if (!spriteIds.contains(reference))
                return std::unexpected(fail(skinJson, "tiers[" + std::to_string(index) + "].visible_sprites",
                                            "unknown sprite reference " + reference));
        }
    }

    pack.sprites.reserve(manifest.sprites.size());
    for (size_t index = 0; index < manifest.sprites.size(); ++index) {
        const JsonSprite& sprite = manifest.sprites[index];
        pack.sprites.push_back(SpriteSpec{sprite.id, std::filesystem::path{}, spriteAnchors[index], sprite.offset_x,
                                          sprite.offset_y, sprite.scale, sprite.z_index});
    }

    const auto resolveAsset = [&](const std::string& relative, const std::string& field,
                                  std::filesystem::path& out) -> std::optional<ValidationError> {
        const std::filesystem::path asset{relative};
        if (asset.is_absolute())
            return fail(skinJson, field, "asset path must be relative to the pack root");
        std::error_code assetEc;
        const std::filesystem::path resolved = std::filesystem::weakly_canonical(canonicalRoot / asset, assetEc);
        if (assetEc)
            return fail(canonicalRoot / asset, field, "cannot resolve asset path: " + assetEc.message());
        if (!isWithin(canonicalRoot, resolved))
            return fail(canonicalRoot / asset, field, "asset path escapes the pack root");
        out = resolved;
        return std::nullopt;
    };

    if (const auto error = resolveAsset(manifest.frame.asset, "frame.asset", pack.frame.asset))
        return std::unexpected(*error);
    for (size_t index = 0; index < pack.sprites.size(); ++index) {
        const auto error = resolveAsset(manifest.sprites[index].asset, "sprites[" + std::to_string(index) + "].asset",
                                        pack.sprites[index].asset);
        if (error)
            return std::unexpected(*error);
    }

    const auto checkPng = [&](const std::filesystem::path& asset, const std::string& field)
        -> std::optional<ValidationError> {
        std::error_code fileEc;
        if (!std::filesystem::is_regular_file(asset, fileEc))
            return fail(asset, field, "PNG asset does not exist");
        bool rgba = false;
        switch (probePngRgba(asset, rgba)) {
        case PngProbe::ok:
            break;
        case PngProbe::notPng:
            return fail(asset, field, "asset is not a PNG image");
        case PngProbe::readError:
            return fail(asset, field, "cannot decode PNG header");
        }
        if (!rgba)
            return fail(asset, field, "PNG color type must be RGBA");
        return std::nullopt;
    };

    if (const auto error = checkPng(pack.frame.asset, "frame.asset"))
        return std::unexpected(*error);
    for (size_t index = 0; index < pack.sprites.size(); ++index) {
        const auto error = checkPng(pack.sprites[index].asset, "sprites[" + std::to_string(index) + "].asset");
        if (error)
            return std::unexpected(*error);
    }

    const std::filesystem::path kittyConf = canonicalRoot / "kitty.conf";
    std::error_code kittyEc;
    const std::filesystem::path resolvedKittyConf = std::filesystem::weakly_canonical(kittyConf, kittyEc);
    if (kittyEc)
        return std::unexpected(fail(kittyConf, "kitty.conf", "cannot resolve kitty.conf: " + kittyEc.message()));
    if (!isWithin(canonicalRoot, resolvedKittyConf))
        return std::unexpected(fail(kittyConf, "kitty.conf", "kitty.conf escapes the pack root"));
    if (!std::filesystem::is_regular_file(resolvedKittyConf, kittyEc))
        return std::unexpected(fail(resolvedKittyConf, "kitty.conf", "kitty.conf does not exist"));

    pack.root = canonicalRoot;
    pack.kittyConfig = resolvedKittyConf;
    return pack;
}

const TierSpec& selectTier(const SkinPack& pack, LogicalSize size) noexcept {
    for (const auto& tier : pack.tiers) {
        if (size.width >= tier.minimum.width && size.height >= tier.minimum.height)
            return tier;
    }
    return pack.tiers.back();
}

}
