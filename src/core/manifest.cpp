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

struct JsonSource {
    int         width{};
    int         height{};
    std::string sha256;
};

struct JsonExact {
    std::string atlas;
    double      aspect{};
    double      aspect_tolerance{};
    double      min_width{};
    double      min_height{};
};

struct JsonAdaptive {
    std::string atlas;
    double      scale{};
    double      min_client_width{};
    double      min_client_height{};
};

struct JsonRegion {
    std::string          id;
    std::string          atlas;
    std::array<int, 4>   rect{};
    std::string          role;
    std::string          anchor;
    std::array<double, 2> offset{};
    std::string          repeat;
    int                  z{};
};

struct JsonManifest {
    int                    schema{};
    std::string            id;
    std::string            name;
    std::string            filter;
    JsonSource             source;
    JsonInsets             aperture;
    JsonExact              exact;
    JsonAdaptive           adaptive;
    std::vector<JsonRegion> regions;
};

// Schema 1 carries keys schema 2 no longer has (frame, tiers, sprites), so a
// strict decode would fail on the first unknown key before the version could be
// read. The probe tolerates unknown keys purely to recover the version number;
// the manifest itself is then decoded strictly.
struct JsonSchemaProbe {
    int schema{};
};

struct TolerantReadOpts : glz::opts {
    bool error_on_unknown_keys = false;
};

glz::error_ctx readSchemaProbe(JsonSchemaProbe& probe, const std::string& buffer) {
    glz::context ctx{};
    return glz::read<TolerantReadOpts{}>(probe, buffer, ctx);
}

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

bool decodeRole(const std::string& value, RegionRole& out) {
    if (value == "corner-top-left") {
        out = RegionRole::corner_top_left;
        return true;
    }
    if (value == "corner-top-right") {
        out = RegionRole::corner_top_right;
        return true;
    }
    if (value == "corner-bottom-left") {
        out = RegionRole::corner_bottom_left;
        return true;
    }
    if (value == "corner-bottom-right") {
        out = RegionRole::corner_bottom_right;
        return true;
    }
    if (value == "rail-top") {
        out = RegionRole::rail_top;
        return true;
    }
    if (value == "rail-bottom") {
        out = RegionRole::rail_bottom;
        return true;
    }
    if (value == "column-left-top") {
        out = RegionRole::column_left_top;
        return true;
    }
    if (value == "column-left-middle") {
        out = RegionRole::column_left_middle;
        return true;
    }
    if (value == "column-left-bottom") {
        out = RegionRole::column_left_bottom;
        return true;
    }
    if (value == "column-right-top") {
        out = RegionRole::column_right_top;
        return true;
    }
    if (value == "column-right-middle") {
        out = RegionRole::column_right_middle;
        return true;
    }
    if (value == "column-right-bottom") {
        out = RegionRole::column_right_bottom;
        return true;
    }
    if (value == "ornament") {
        out = RegionRole::ornament;
        return true;
    }
    return false;
}

bool decodeRepeat(const std::string& value, RepeatAxis& out) {
    if (value == "none") {
        out = RepeatAxis::none;
        return true;
    }
    if (value == "x") {
        out = RepeatAxis::horizontal;
        return true;
    }
    if (value == "y") {
        out = RepeatAxis::vertical;
        return true;
    }
    return false;
}

// Roles that the adaptive layout always needs exactly one of.
bool isSingleCorner(RegionRole role) {
    return role == RegionRole::corner_top_left || role == RegionRole::corner_top_right ||
           role == RegionRole::corner_bottom_left || role == RegionRole::corner_bottom_right;
}

bool isFlexible(RegionRole role) {
    switch (role) {
        case RegionRole::rail_top:
        case RegionRole::rail_bottom:
        case RegionRole::column_left_middle:
        case RegionRole::column_right_middle:
            return true;
        default:
            return false;
    }
}

enum class PngProbe { ok, notPng, readError };

PngProbe probePngRgba(const std::filesystem::path& file, bool& rgba, int& width, int& height) {
    rgba   = false;
    width  = 0;
    height = 0;

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
    const size_t  read         = std::fread(signature, 1, sizeof(signature), handle);
    if (read != sizeof(signature) || png_sig_cmp(signature, 0, sizeof(signature)) != 0) {
        png_destroy_read_struct(&png, &info, nullptr);
        std::fclose(handle);
        return PngProbe::notPng;
    }

    png_set_sig_bytes(png, static_cast<int>(sizeof(signature)));
    png_read_info(png, info);
    rgba   = png_get_color_type(png, info) == PNG_COLOR_TYPE_RGBA;
    width  = static_cast<int>(png_get_image_width(png, info));
    height = static_cast<int>(png_get_image_height(png, info));

    png_destroy_read_struct(&png, &info, nullptr);
    std::fclose(handle);
    return PngProbe::ok;
}

bool isSha256(const std::string& value) {
    if (value.size() != 64)
        return false;
    return std::all_of(value.begin(), value.end(), [](unsigned char character) {
        return (character >= '0' && character <= '9') || (character >= 'a' && character <= 'f');
    });
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

    JsonSchemaProbe probe{};
    if (const glz::error_ctx probeError = readSchemaProbe(probe, buffer))
        return std::unexpected(fail(skinJson, "skin.json",
                                    "invalid manifest: " + glz::format_error(probeError, buffer)));

    if (probe.schema == 1)
        return std::unexpected(
            fail(skinJson, "schema", "schema 1 is obsolete and no longer supported; migrate the pack to schema 2"));
    if (probe.schema != 2)
        return std::unexpected(
            fail(skinJson, "schema", "unsupported schema " + std::to_string(probe.schema) + "; expected 2"));

    JsonManifest manifest{};
    const glz::error_ctx parse = glz::read_json(manifest, buffer);
    std::string parseMessage;
    if (parse)
        parseMessage = glz::format_error(parse, buffer);
    buffer.clear();
    buffer.shrink_to_fit();
    if (parse)
        return std::unexpected(fail(skinJson, "skin.json", "invalid manifest: " + parseMessage));

    static const std::regex idPattern(R"(^[a-z0-9]+(?:-[a-z0-9]+)*$)");
    if (!std::regex_match(manifest.id, idPattern))
        return std::unexpected(fail(skinJson, "id", "must match ^[a-z0-9]+(?:-[a-z0-9]+)*$"));

    SkinPack pack{};
    pack.schema = manifest.schema;
    pack.id     = manifest.id;

    if (manifest.name.empty())
        return std::unexpected(fail(skinJson, "name", "must not be empty"));
    pack.name = manifest.name;

    if (!decodeFilter(manifest.filter, pack.filter))
        return std::unexpected(fail(skinJson, "filter", "must be nearest or linear"));

    if (manifest.source.width < 1 || manifest.source.height < 1)
        return std::unexpected(fail(skinJson, "source", "width and height must be positive"));
    if (!isSha256(manifest.source.sha256))
        return std::unexpected(fail(skinJson, "source.sha256", "must be a 64-character lowercase hex digest"));
    pack.sourceWidth  = manifest.source.width;
    pack.sourceHeight = manifest.source.height;
    pack.sourceSha256 = manifest.source.sha256;

    const std::array<std::pair<const char*, double>, 4> bands{{{"left", manifest.aperture.left},
                                                              {"right", manifest.aperture.right},
                                                              {"top", manifest.aperture.top},
                                                              {"bottom", manifest.aperture.bottom}}};
    for (const auto& [name, value] : bands) {
        if (!finite(value) || value < 0.0)
            return std::unexpected(fail(skinJson, std::string("aperture.") + name, "must be finite and non-negative"));
    }
    // Aperture plus bands must exactly cover the source: the opening is whatever
    // is left between the bands, so the bands may not meet or overlap.
    if (manifest.aperture.left + manifest.aperture.right >= static_cast<double>(manifest.source.width))
        return std::unexpected(fail(skinJson, "aperture", "left and right bands leave no aperture opening"));
    if (manifest.aperture.top + manifest.aperture.bottom >= static_cast<double>(manifest.source.height))
        return std::unexpected(fail(skinJson, "aperture", "top and bottom bands leave no aperture opening"));
    pack.aperture = Insets{manifest.aperture.left, manifest.aperture.right, manifest.aperture.top,
                           manifest.aperture.bottom};

    if (!finite(manifest.exact.aspect) || manifest.exact.aspect <= 0.0)
        return std::unexpected(fail(skinJson, "exact.aspect", "must be finite and greater than zero"));
    if (!finite(manifest.exact.aspect_tolerance) || manifest.exact.aspect_tolerance < 0.0)
        return std::unexpected(fail(skinJson, "exact.aspect_tolerance", "must be finite and non-negative"));
    if (!finite(manifest.exact.min_width) || manifest.exact.min_width <= 0.0 ||
        !finite(manifest.exact.min_height) || manifest.exact.min_height <= 0.0)
        return std::unexpected(fail(skinJson, "exact.min_width", "minimum outer size must be finite and positive"));
    if (manifest.exact.atlas.empty())
        return std::unexpected(fail(skinJson, "exact.atlas", "must not be empty"));
    pack.exact = ExactMode{manifest.exact.aspect, manifest.exact.aspect_tolerance, manifest.exact.min_width,
                           manifest.exact.min_height};

    if (!finite(manifest.adaptive.scale) || manifest.adaptive.scale <= 0.0)
        return std::unexpected(fail(skinJson, "adaptive.scale", "must be finite and greater than zero"));
    if (!finite(manifest.adaptive.min_client_width) || manifest.adaptive.min_client_width <= 0.0 ||
        !finite(manifest.adaptive.min_client_height) || manifest.adaptive.min_client_height <= 0.0)
        return std::unexpected(fail(skinJson, "adaptive.min_client_width", "minimum client size must be finite and positive"));
    if (manifest.adaptive.atlas.empty())
        return std::unexpected(fail(skinJson, "adaptive.atlas", "must not be empty"));
    pack.adaptive = AdaptiveMode{manifest.adaptive.scale, manifest.adaptive.min_client_width,
                                 manifest.adaptive.min_client_height};

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

    if (const auto error = resolveAsset(manifest.exact.atlas, "exact.atlas", pack.exactAtlas))
        return std::unexpected(*error);
    if (const auto error = resolveAsset(manifest.adaptive.atlas, "adaptive.atlas", pack.adaptiveAtlas))
        return std::unexpected(*error);

    // Both atlases are source-pixel rasters: region rectangles index them directly.
    const auto checkAtlas = [&](const std::filesystem::path& asset, const std::string& field)
        -> std::optional<ValidationError> {
        std::error_code fileEc;
        if (!std::filesystem::is_regular_file(asset, fileEc))
            return fail(asset, field, "PNG atlas does not exist");

        bool rgba = false;
        int  width = 0;
        int  height = 0;
        switch (probePngRgba(asset, rgba, width, height)) {
        case PngProbe::ok:
            break;
        case PngProbe::notPng:
            return fail(asset, field, "atlas is not a PNG image");
        case PngProbe::readError:
            return fail(asset, field, "cannot decode PNG header");
        }
        if (!rgba)
            return fail(asset, field, "PNG color type must be RGBA");
        if (width != manifest.source.width || height != manifest.source.height)
            return fail(asset, field, "atlas is " + std::to_string(width) + "x" + std::to_string(height) +
                                          ", expected " + std::to_string(manifest.source.width) + "x" +
                                          std::to_string(manifest.source.height));
        return std::nullopt;
    };

    if (const auto error = checkAtlas(pack.exactAtlas, "exact.atlas"))
        return std::unexpected(*error);
    if (const auto error = checkAtlas(pack.adaptiveAtlas, "adaptive.atlas"))
        return std::unexpected(*error);

    // Region roles the adaptive layout cannot be built without.
    std::array<int, 13> roleCounts{};
    const auto roleIndex = [](RegionRole role) { return static_cast<size_t>(role); };

    std::unordered_set<std::string> regionIds;
    pack.regions.reserve(manifest.regions.size());

    for (size_t index = 0; index < manifest.regions.size(); ++index) {
        const JsonRegion& json = manifest.regions[index];
        const std::string prefix = "regions[" + std::to_string(index) + "]";

        if (json.id.empty())
            return std::unexpected(fail(skinJson, prefix + ".id", "must not be empty"));
        if (!regionIds.insert(json.id).second)
            return std::unexpected(fail(skinJson, prefix + ".id", "duplicate region id " + json.id));

        bool exactAtlas = false;
        if (json.atlas == "exact")
            exactAtlas = true;
        else if (json.atlas != "adaptive")
            return std::unexpected(fail(skinJson, prefix + ".atlas", "must be exact or adaptive"));

        const SourceRect rect{json.rect[0], json.rect[1], json.rect[2], json.rect[3]};
        if (rect.width <= 0 || rect.height <= 0)
            return std::unexpected(fail(skinJson, prefix + ".rect", "width and height must be positive"));
        if (rect.x < 0 || rect.y < 0 || rect.x + rect.width > manifest.source.width ||
            rect.y + rect.height > manifest.source.height)
            return std::unexpected(fail(skinJson, prefix + ".rect", "rectangle is not inside the source atlas"));

        RegionRole role{};
        if (!decodeRole(json.role, role))
            return std::unexpected(fail(skinJson, prefix + ".role", "unknown region role " + json.role));

        Anchor anchor{};
        if (!decodeAnchor(json.anchor, anchor))
            return std::unexpected(fail(skinJson, prefix + ".anchor", "unknown anchor " + json.anchor));

        RepeatAxis repeat{};
        if (!decodeRepeat(json.repeat, repeat))
            return std::unexpected(fail(skinJson, prefix + ".repeat", "must be none, x or y"));

        // Only declared-flexible roles may repeat; a repeated corner or ornament
        // would tile architecture, which the spec forbids.
        if (repeat != RepeatAxis::none && !isFlexible(role))
            return std::unexpected(fail(skinJson, prefix + ".repeat", "only rail and column shaft regions may repeat"));

        if (!finite(json.offset[0]) || !finite(json.offset[1]))
            return std::unexpected(fail(skinJson, prefix + ".offset", "offsets must be finite"));

        if (isSingleCorner(role) && ++roleCounts[roleIndex(role)] > 1)
            return std::unexpected(fail(skinJson, prefix + ".role", "corner role declared more than once"));

        if (role == RegionRole::column_left_top || role == RegionRole::column_left_bottom ||
            role == RegionRole::column_right_top || role == RegionRole::column_right_bottom) {
            if (++roleCounts[roleIndex(role)] > 1)
                return std::unexpected(fail(skinJson, prefix + ".role", "column cap role declared more than once"));
        }

        if (role == RegionRole::rail_top || role == RegionRole::rail_bottom ||
            role == RegionRole::column_left_middle || role == RegionRole::column_right_middle)
            ++roleCounts[roleIndex(role)];

        pack.regions.push_back(RegionSpec{json.id, exactAtlas, rect, role, anchor, json.offset[0], json.offset[1],
                                          repeat, json.z});
    }

    const std::array<std::pair<RegionRole, const char*>, 10> required{{
        {RegionRole::corner_top_left, "corner-top-left"},
        {RegionRole::corner_top_right, "corner-top-right"},
        {RegionRole::corner_bottom_left, "corner-bottom-left"},
        {RegionRole::corner_bottom_right, "corner-bottom-right"},
        {RegionRole::rail_top, "rail-top"},
        {RegionRole::rail_bottom, "rail-bottom"},
        {RegionRole::column_left_top, "column-left-top"},
        {RegionRole::column_left_bottom, "column-left-bottom"},
        {RegionRole::column_right_top, "column-right-top"},
        {RegionRole::column_right_bottom, "column-right-bottom"},
    }};
    for (const auto& [role, name] : required) {
        if (roleCounts[roleIndex(role)] < 1)
            return std::unexpected(fail(skinJson, "regions", std::string("missing required region role ") + name));
    }
    if (roleCounts[roleIndex(RegionRole::column_left_middle)] < 1 ||
        roleCounts[roleIndex(RegionRole::column_right_middle)] < 1)
        return std::unexpected(fail(skinJson, "regions", "each column needs at least one middle shaft region"));

    const std::filesystem::path kittyConf = canonicalRoot / "kitty.conf";
    std::error_code kittyEc;
    const std::filesystem::path resolvedKittyConf = std::filesystem::weakly_canonical(kittyConf, kittyEc);
    if (kittyEc)
        return std::unexpected(fail(kittyConf, "kitty.conf", "cannot resolve kitty.conf: " + kittyEc.message()));
    if (!isWithin(canonicalRoot, resolvedKittyConf))
        return std::unexpected(fail(kittyConf, "kitty.conf", "kitty.conf escapes the pack root"));
    if (!std::filesystem::is_regular_file(resolvedKittyConf, kittyEc))
        return std::unexpected(fail(resolvedKittyConf, "kitty.conf", "kitty.conf does not exist"));

    pack.root        = canonicalRoot;
    pack.kittyConfig = resolvedKittyConf;
    return pack;
}

}
